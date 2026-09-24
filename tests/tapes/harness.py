"""Replay a conversation from a tape, on virtual time, and print what happened.

A tape is what the *user* did, timed: when they spoke (the voice detector's
view), what the recognizer made of it, what they typed. Everything else is
simulated deterministically — the browser (playback, interruption answers), the
reasoning engine, the synthesizer — and the loop's clock only moves when every
task is waiting, so a 30 s silence replays in milliseconds and the same tape
always prints the same transcript.

The printed transcript is compared with a golden file. It pins today's
turn-taking — barge-in, echo, hold-and-merge, resume, speculation, the clock,
the inner voice — so that moving that logic somewhere else shows up as a diff.
"""

import asyncio
import json
import re
import selectors
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voice_agent import roles, thinker, timing
from voice_agent.config import build_prompt
from voice_agent.conversation import Conversation, Message
from voice_agent.greeting import Greeting
from voice_agent.initiative import LADDER, Rung
from voice_agent.llm.base import Usage
from voice_agent.server import handle_text, ladder_for
from voice_agent.session import Session
from voice_agent.stt.base import Transcript
from voice_agent.tts.base import Alignment, AudioChunk, pcm_seconds
from voice_agent.vad import WINDOW_BYTES

FRAME_SECONDS = 0.032
"""The page posts one VAD window per frame."""

NETWORK_SECONDS = 0.02
"""Each way between the page and the server."""

SPEECH_SECONDS_PER_CHAR = 0.06
"""The simulated voice: about 16 characters a second, a natural speaking rate."""

SPEECH = b"\x01" * WINDOW_BYTES
SILENCE = b"\x00" * WINDOW_BYTES


# ---------------------------------------------------------------- virtual time


class _VirtualSelector:
    """Real I/O first (a thread finishing wakes the loop through its pipe);
    otherwise, instead of sleeping until the next timer, jump to it."""

    def __init__(self, loop: "VirtualLoop") -> None:
        self._inner = selectors.DefaultSelector()
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        events = self._inner.select(0)
        if events or (timeout is not None and timeout <= 0):
            return events
        if timeout is None:
            return self._inner.select(None)
        self._loop.virtual += timeout
        return []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self.virtual = 0.0
        super().__init__(_VirtualSelector(self))  # type: ignore[arg-type]  # duck-typed

    def time(self) -> float:
        return self.virtual


# ---------------------------------------------------------------------- tapes


@dataclass(slots=True)
class Tape:
    name: str
    greeting: str = ""
    role: str = ""
    ladder: tuple[Rung, ...] = LADDER
    ttft: float = 0.5
    pace: float = 0.03
    replies: list[tuple[str, list[str]]] = field(default_factory=list)
    thoughts: list[tuple[str, list[str]]] = field(default_factory=list)
    events: list[tuple[float, str, str]] = field(default_factory=list)


def parse(text: str, name: str) -> Tape:
    """One directive or timed event per line; a line starting `#` is a comment.

        greeting <text>            role <card>            ladder 5,15,28 | off
        llm <ttft> <pace>          reply <match> = <text>  think <match> = <json>
        <t> listen | unlisten | voice on | voice off | partial <text>
            | final <text> | type <text> | end
    A `reply` or `think` repeated for the same match is used in order, the last
    one again after that. `<match>` is a substring of the last message.
    """
    tape = Tape(name)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        if re.fullmatch(r"\d+(\.\d+)?", head):
            verb, _, arg = rest.partition(" ")
            tape.events.append((float(head), verb, arg))
        elif head == "greeting":
            tape.greeting = rest
        elif head == "role":
            tape.role = rest
        elif head == "ladder":
            tape.ladder = () if rest == "off" else ladder_for([float(x) for x in rest.split(",")])
        elif head == "llm":
            ttft, pace = rest.split()
            tape.ttft, tape.pace = float(ttft), float(pace)
        elif head in ("reply", "think"):
            match, _, answer = rest.partition(" = ")
            table = tape.replies if head == "reply" else tape.thoughts
            for key, answers in table:
                if key == match:
                    answers.append(answer)
                    break
            else:
                table.append((match, [answer]))
        else:
            raise ValueError(f"{name}: cannot read {raw!r}")
    return tape


# ---------------------------------------------------------------- the actors


class Log:
    def __init__(self, started: float) -> None:
        self.started = started
        self.lines: list[str] = []

    def __call__(self, who: str, what: str) -> None:
        self.lines.append(f"{timing.now() - self.started:7.3f}  {who:<3} {what}")


class ScriptedLLM:
    """Answers by the last message: the first `match` it contains."""

    def __init__(
        self, answers: list[tuple[str, list[str]]], ttft: float, pace: float, log: Log, name: str
    ) -> None:
        self.provider, self.model = "scripted", name
        self._answers = [(match, list(texts)) for match, texts in answers]
        self._ttft, self._pace, self._log, self._name = ttft, pace, log, name

    async def connect(self) -> None:
        return None

    def _answer(self, last: str) -> str:
        for match, texts in self._answers:
            if match in last:
                return texts.pop(0) if len(texts) > 1 else texts[0]
        return "Okay."

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        last = messages[-1].content if messages else ""
        answer = self._answer(last)
        asked = " ".join(last.split())
        # The thinker's request ends with why it was asked; a reply's, with the question.
        shown = asked[-50:] if self._name == "thk" else short(asked, 50)
        self._log(self._name, f"ask {shown!r}")
        finished = False
        try:
            await asyncio.sleep(self._ttft)
            words = answer.split(" ")
            for index, word in enumerate(words):
                yield word if index == len(words) - 1 else word + " "
                await asyncio.sleep(self._pace)
            finished = True
        finally:
            if not finished:
                self._log(self._name, "stopped early")


class PacedTTS:
    """Speaks each fragment as it arrives, at a steady speaking rate, timed
    per character like the real voice."""

    provider, voice = "paced", "paced-1"

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        first = True
        async for fragment in text:
            if not fragment:
                continue
            if first:
                await asyncio.sleep(0.15)
                first = False
            per_char = SPEECH_SECONDS_PER_CHAR * 1000
            ends = tuple((i + 1) * per_char for i in range(len(fragment)))
            pcm = b"\x00" * round(len(fragment) * SPEECH_SECONDS_PER_CHAR * 48_000)
            yield AudioChunk(pcm, Alignment(fragment, ends))


class TapeSTT:
    """The recognizer's side of the tape: each transcript at its time, for as
    long as a listening session is open. Words said while nobody listened are
    lost, as they would be."""

    provider, model, languages, sample_rate = "tape", "tape-1", (), 16000

    def __init__(self, script: list[tuple[float, Transcript]], started: float) -> None:
        self._script = script
        self._started = started

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        ended = asyncio.Event()

        async def drain() -> None:
            async for _ in audio:
                pass
            ended.set()

        reader = asyncio.create_task(drain())
        try:
            while self._script:
                at, transcript = self._script[0]
                wait = self._started + at - timing.now()
                if wait < -FRAME_SECONDS:
                    self._script.pop(0)  # said before this session opened
                    continue
                if wait > 0:
                    ending = asyncio.ensure_future(ended.wait())
                    await asyncio.wait({ending}, timeout=wait)
                    ending.cancel()
                if ended.is_set():
                    return
                self._script.pop(0)
                yield transcript
            await ended.wait()
        finally:
            reader.cancel()


class ScriptedVAD:
    """Speech where the tape says the user's voice is on: the frame says so."""

    def probabilities(self, pcm: bytes) -> list[float]:
        return [0.9 if pcm[:1] == b"\x01" else 0.02 for _ in range(len(pcm) // WINDOW_BYTES)]


class Browser:
    """The page, as far as the server can tell: it plays what it is sent at
    real speed, says when playback starts and stops, answers an interruption
    with how much it played, and sends microphone frames while listening."""

    def __init__(self, log: Log) -> None:
        self._log = log
        self.session: Session | None = None
        self.listening = False
        self.voice_on = False
        self._cut = False
        self._stream: dict[str, Any] | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    # Server -> page.

    async def send_json(self, payload: dict[str, object]) -> None:
        kind = str(payload["type"])
        line = describe(payload)
        if line is not None:
            self._log(">", line)
        if kind == "reply_start":
            self._cut = False
        elif kind == "audio_start":
            self._stream = None if self._cut else {"bytes": 0, "playing_at": None, "ended": False}
        elif kind == "audio_end" and self._stream is not None:
            self._stream["ended"] = True
            self._schedule_finish(self._stream)
        elif kind == "interrupt":
            self._interrupted(int(payload["id"]))  # type: ignore[call-overload]
        elif kind == "listening":
            self.listening = bool(payload["active"])

    async def send_bytes(self, data: bytes) -> None:
        stream = self._stream
        if stream is None:
            return
        stream["bytes"] += len(data)
        if stream["playing_at"] is None:
            stream["playing_at"] = timing.now() + NETWORK_SECONDS
            self._later(NETWORK_SECONDS, self._playback(stream, True))

    # Page -> server.

    def send(self, payload: dict[str, object], label: str) -> None:
        async def deliver() -> None:
            assert self.session is not None
            self._log("<", label)
            await handle_text(self, self.session, json.dumps(payload))  # type: ignore[arg-type]

        self._later(NETWORK_SECONDS, deliver())

    def _later(self, delay: float, work: Any) -> None:
        async def run() -> None:
            await asyncio.sleep(delay)
            await work

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _playback(self, stream: dict[str, Any], active: bool) -> None:
        if self._stream is not stream:
            return
        if not active:
            self._stream = None
        self.send({"type": "playback", "active": active}, f"playback {'on' if active else 'off'}")

    def _schedule_finish(self, stream: dict[str, Any]) -> None:
        start = stream["playing_at"] if stream["playing_at"] is not None else timing.now()
        left = start + pcm_seconds(stream["bytes"]) - timing.now()
        self._later(max(left, 0.0) + NETWORK_SECONDS, self._playback(stream, False))

    def _interrupted(self, interrupt_id: int) -> None:
        self._cut = True
        stream, self._stream = self._stream, None
        played: float | None = None
        if stream is not None and stream["playing_at"] is not None:
            played = min(
                (timing.now() - stream["playing_at"]) * 1000,
                pcm_seconds(stream["bytes"]) * 1000,
            )
            self.send({"type": "playback", "active": False}, "playback off (cut)")
        answer = {"type": "interrupted", "id": interrupt_id, "played_ms": played}
        self.send(answer, f"interrupted {interrupt_id} played {fmt_ms(played)}")

    async def microphone(self) -> None:
        while True:
            await asyncio.sleep(FRAME_SECONDS)
            if self.listening and self.session is not None and self.session.mic is not None:
                self.session.mic.feed(SPEECH if self.voice_on else SILENCE)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


# ------------------------------------------------------------------- replaying


def short(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_ms(value: float | None) -> str:
    return "none" if value is None else f"{round(value)}ms"


FIELDS: dict[str, tuple[str, ...]] = {
    "reply_end": (
        "interrupted", "speculated", "merged", "initiative", "resumed", "ttft_ms", "text",
    ),
    "audio_end": ("seconds", "first_audio_ms", "cached"),
    "truncated": ("played_ms", "heard_chars", "chars", "estimated"),
    "interrupt": ("id",),
    "transcript": ("final", "text"),
    "floor": ("state", "agent"),
    "listening": ("active", "reason"),
    "initiative": ("rung", "decision", "line"),
    "thought": ("reason", "decision", "move", "urgency", "line"),
    "echo_ignored": ("stage", "text"),
    "resumed": ("chars",),
    "greeting": ("text",),
    "error": ("message",),
    "listen_error": ("message",),
    "ended": ("reason",),
}  # fmt: skip
QUIET = frozenset({"delta", "marks", "audio_start", "reply_start"})


def describe(payload: dict[str, object]) -> str | None:
    kind = str(payload["type"])
    if kind in QUIET:
        return None
    parts = [kind]
    for key in FIELDS.get(kind, ()):
        value = payload.get(key)
        if value is None or value is False or value == "" or value == 0:
            continue
        if isinstance(value, float):
            value = round(value, 2)
        shown = repr(short(value)) if isinstance(value, str) else str(value)
        parts.append(f"{key}={shown}")
    return " ".join(parts)


def _transcripts(tape: Tape) -> list[tuple[float, Transcript]]:
    return [
        (at, Transcript(arg, is_final=verb == "final"))
        for at, verb, arg in tape.events
        if verb in ("partial", "final")
    ]


async def replay(tape: Tape, cache: Path) -> str:
    started = timing.now()
    log = Log(started)
    browser = Browser(log)
    conversation = Conversation(id=f"tape-{tape.name}")
    speaker = PacedTTS()
    engine = ScriptedLLM(tape.replies, tape.ttft, tape.pace, log, "llm")
    card = roles.load(tape.role) if tape.role else None
    inner = ScriptedLLM(tape.thoughts, 1.0, 0.0, log, "thk") if card is not None else None
    greeting = Greeting(tape.greeting, speaker, cache_dir=cache)
    await greeting.prepare()
    log.started = started = timing.now()
    listener = TapeSTT(_transcripts(tape), started)
    prompt = build_prompt(
        (),
        "female",
        tuple(rung.after for rung in tape.ladder),
        base="You are a test agent.",
        greeting=greeting.text,
        role=card.when_speaking if card is not None else "",
    )
    session = Session(
        browser,  # type: ignore[arg-type]
        conversation,
        engine,
        speaker,
        prompt,
        listener,
        tape.ladder,
        role=card,
        thinker=inner,
        thinker_prompt=thinker.system_prompt(card) if card is not None else None,
    )
    if session.mic is not None:
        session.mic.detector = ScriptedVAD  # the tape says when the user's voice is on
    browser.session = session
    microphone = asyncio.create_task(browser.microphone())
    try:
        session.voiced(await greeting.deliver(browser, conversation))  # type: ignore[arg-type]
        session.start()
        for at, verb, arg in tape.events:
            await asyncio.sleep(max(0.0, started + at - timing.now()))
            await _act(browser, verb, arg, tape.name)
            if verb == "end":
                break
    finally:
        await session.close()
        microphone.cancel()
        await browser.close()
    log.lines.append("")
    log.lines.extend(f"{m.role:>9}: {m.content}" for m in conversation.messages)
    return "\n".join(log.lines) + "\n"


async def _act(browser: Browser, verb: str, arg: str, name: str) -> None:
    if verb == "listen":
        browser.send({"type": "listen_start"}, "listen_start")
    elif verb == "unlisten":
        browser.send({"type": "listen_stop"}, "listen_stop")
    elif verb == "voice":
        browser.voice_on = arg == "on"
    elif verb == "type":
        browser.send({"type": "message", "text": arg}, f"typed {arg!r}")
    elif verb in ("partial", "final", "end"):
        pass  # the recognizer's own schedule; `end` stops the tape
    else:
        raise ValueError(f"{name}: unknown event {verb!r}")


def run(tape: Tape, cache: Path, limit: float = 600.0) -> str:
    """Replay on a fresh virtual loop; `limit` virtual seconds is a hang."""
    loop = VirtualLoop()

    async def bounded() -> str:
        async with asyncio.timeout(limit):
            return await replay(tape, cache)

    try:
        with timing.using(loop.time):
            return loop.run_until_complete(bounded())
    finally:
        loop.close()


def load(path: Path) -> Tape:
    return parse(path.read_text(encoding="utf-8"), path.stem)


Runner = Callable[[Tape, Path], str]
