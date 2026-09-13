"""The WebSocket server and the conversation loop that runs over it.

One connection per conversation, carrying four things: control messages as
JSON in both directions, microphone audio as binary frames going up, and
synthesized speech as binary frames coming down. The link in the URL is the
conversation: reconnecting to it replays the history and carries on.

The agent is bi-capable. A turn can be started by typing or by speaking, and
by the time a turn begins nothing downstream knows which it was — speech
becomes text at the edge, and the rest of the pipeline is unchanged from the
chapter that had no ears.

It is deliberately **half-duplex**: the browser stops sending microphone audio
while the agent is speaking, so the agent cannot transcribe its own voice.
Removing that restriction is what the barge-in chapter is for.
"""

import asyncio
import contextlib
import json
import logging
import time
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from hashlib import sha256
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from voice_agent.config import load_settings, load_system_prompt
from voice_agent.conversation import Conversation, Message
from voice_agent.errors import ProviderError, SessionNotFoundError, VoiceAgentError
from voice_agent.llm import LLM, create_llm
from voice_agent.llm.base import Warmth
from voice_agent.sessions import SessionStore
from voice_agent.speculation import Speculation
from voice_agent.stt import STT, create_stt
from voice_agent.stt.agreement import StablePrefix
from voice_agent.tts import TTS, AudioClip, create_tts

logger = logging.getLogger(__name__)

PAGE_PATH = Path(__file__).parent / "web" / "index.html"
GREETING_CACHE = Path(".cache")

IDLE_TIMEOUT_SECONDS = 30.0
"""How long with no *speech* before listening stops. Not "no audio": the
microphone streams silence continuously, so frames never stop arriving. The
signal that nobody is talking is the absence of partial transcripts."""

SESSION_CAP_SECONDS = 300.0
"""A backstop the idle timer cannot provide, for a room that produces
continuous partials — a television, a conversation nearby. Scribe enforces its
own session limit regardless; better to hit ours, with an explanation."""

MAX_HOLD_SECONDS = 60.0
"""How long expiry may be suspended before the hold is assumed lost.

Nothing legitimate holds this long — the longest reply so far was 18 s of
audio. A hold that is never released, because a tab closed or a browser event
never fired, would otherwise stop the microphone forever *and* silently, since
a suspended timer announces nothing. This converts the worst failure mode
observed in this project into a one-minute hiccup."""

KEEPALIVE_GAP_SECONDS = 10.0
"""How long the recognizer may go without audio before we top it up.

Measured against the real service: Scribe closes a realtime session after
roughly 15 seconds with no audio — and closes it *normally*, code 1000, so the
stream simply ends rather than raising. The browser stops sending while a reply
plays, so any answer longer than ~15 s silently killed the ears.

The first version filled *every* gap, sending silence five times a second. That
is continuous real-time audio to a service metered by audio duration, and it
quietly consumed a month's quota. Topping up shortly before the deadline
instead costs about 2% of that. If the assumption behind the interval is ever
wrong, the session simply ends and the reconnect below catches it — the failure
mode is a hiccup, not silence."""

KEEPALIVE_BURST_SECONDS = 0.2
"""How much silence one top-up sends."""

MAX_RECONNECTS = 3
"""If the recognizer drops us anyway, reconnect rather than going quietly deaf.
Silent failure is the worst outcome available here: the page still says
"listening" and the user keeps talking to nothing."""

WATCHDOG_TICK_SECONDS = 1.0

WARM_MIN_NEW_WORDS = 48
"""How much the agreed prefix must grow before warming again.

Measured on a real 22-word turn, warming on every growth fired **seven** calls
and the provider's cached-token count never moved off 1024: the utterance is
far too short to complete another 64-token cache block, so warms two through
seven were billed prefill that cached nothing. One block is roughly 48 words of
speech, so that is the threshold — long utterances still warm more than once,
short ones warm exactly once and keep the earliest, longest lead."""

SPECULATE = True
"""Whether to start the real reply on a prefix the user has not finished saying.

The bet: when a partial adds no new words the speaker has probably stopped, and
the ~0.8 s the reasoning engine needs can be spent now rather than after the
recognizer gets round to committing. Worth min(head start, time-to-first-token)
— measured at 0.27 s and 0.80 s on two real utterances, so 0.3-0.8 s, not the
1.0-1.7 s an earlier estimate claimed by counting the network floor separately
from the head start that contains it.

Losing the bet costs only what the generation produced before it was cancelled.
That is only true while the agent has no tools: a speculation that could send a
message or move money is not a bet, it is an action."""

WARM_ON_STABLE = True
"""Whether to prefill the reasoning engine on agreed-stable transcript text
while the user is still speaking.

Honest about its worth: measured against DeepSeek, this saves ~60-90 ms on the
*first* turn of a conversation and ~10 ms on every turn after, because the
provider's cache is already 80-87% warm from the previous turn's own call. It
is kept because the machinery — agreeing a stable prefix and acting on it
before the turn ends — is the part that matters, and pointing it at a real
generation instead of a discarded one is worth 1.0-1.7 s. This is that change
with the payoff switched off."""

EXIT_COMMANDS = frozenset({"exit", "quit", "bye", "goodbye"})
"""Typing or saying any of these ends the conversation. Matched on the whole
message, case- and punctuation-insensitively, so that "Bye!" ends it but
"goodbye is a strange word" does not."""


def elapsed_ms(start: float, end: float | None = None) -> int:
    return round(((time.perf_counter() if end is None else end) - start) * 1000)


def human_seconds(value: float) -> str:
    if value >= 60 and value % 60 == 0:
        minutes = int(value // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{value:.0f}s"


def is_exit_command(text: str) -> bool:
    return text.strip().strip(".!?").casefold() in EXIT_COMMANDS


def serialize(messages: Sequence[Message]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


class Warmings:
    """What the warming calls for the current turn found.

    Reported once, on the committed transcript, rather than per warm: the
    interesting question is how much of the prompt was already paid for by the
    time the turn ended, not the shape of each individual call.
    """

    def __init__(self) -> None:
        self.attempted = 0
        self.count = 0
        self.last: Warmth | None = None
        self.finished_at: float | None = None

    def attempt(self) -> None:
        """A warm has been fired. Counted separately from one that *landed*.

        Without this, a missing warm line is ambiguous between "nothing was
        warmed" and "a warm was paid for and arrived too late to help" — and
        those want opposite responses.
        """
        self.attempted += 1

    def record(self, warmth: Warmth) -> None:
        self.count += 1
        self.last = warmth
        self.finished_at = time.perf_counter()

    def report(self) -> dict[str, object]:
        if self.last is None:
            return {"warms": self.count, "warms_attempted": self.attempted}
        return {
            "warms": self.count,
            "warms_attempted": self.attempted,
            "warm_prompt_tokens": self.last.prompt_tokens,
            "warm_cached_tokens": self.last.cached_tokens,
            # How far ahead of the turn ending the last warm landed. A warm
            # that does not land before the turn ends is cancelled and never
            # recorded, so everything reported here arrived in time to help.
            "warm_lead_ms": elapsed_ms(self.finished_at) if self.finished_at else 0,
        }

    def reset(self) -> None:
        self.attempted = self.count = 0
        self.last = None
        self.finished_at = None


class Guesses:
    """What speculating cost this turn, and what it bought.

    Reported rather than assumed. Chapter 4 shipped a warm that fired seven
    times for one turn and cached nothing, and the only reason that was ever
    noticed is that the count was on screen.
    """

    def __init__(self) -> None:
        self.discarded = 0
        self.wasted_chars = 0
        self.adopted = False
        self.lead_ms = 0

    def discard(self, chars: int) -> None:
        self.discarded += 1
        self.wasted_chars += chars

    def adopt(self, lead_ms: int) -> None:
        self.adopted = True
        self.lead_ms = lead_ms

    def report(self) -> dict[str, object]:
        return {
            "speculated": self.adopted,
            "speculation_lead_ms": self.lead_ms,
            "speculations_discarded": self.discarded,
            "speculation_wasted_chars": self.wasted_chars,
        }

    def reset(self) -> None:
        self.discarded = self.wasted_chars = self.lead_ms = 0
        self.adopted = False


class Channel:
    """Serializes writes to one WebSocket.

    Two producers now write to the same socket: the receive loop's replies, and
    the microphone's transcript task. Interleaving them would be harmless for
    JSON but fatal for audio, whose `audio` frame and binary frame must arrive
    adjacent — hence `send_audio` holding the lock across both.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._lock = asyncio.Lock()

    async def send_json(self, payload: dict[str, object]) -> None:
        async with self._lock:
            await self._websocket.send_json(payload)

    async def send_audio(self, announcement: dict[str, object], data: bytes) -> None:
        async with self._lock:
            await self._websocket.send_json(announcement)
            await self._websocket.send_bytes(data)


class Greeting:
    """The agent's opening line, synthesised once for the life of the process.

    Cached rather than re-synthesised per visitor because it never changes:
    that is zero synthesis cost and zero wait on every conversation after the
    first, and it is the cheapest optimisation available in a voice pipeline.
    """

    def __init__(self, text: str, speaker: TTS | None, cache_dir: Path | None = None) -> None:
        self.text = text.strip()
        self._speaker = speaker
        # Resolved now rather than bound as a default argument, so that a test
        # can point it somewhere disposable instead of at the working tree.
        self._cache_dir = GREETING_CACHE if cache_dir is None else cache_dir
        self._clip: AudioClip | None = None
        self._synthesis_ms = 0
        self._attempted = False

    @property
    def _cache_file(self) -> Path:
        """Keyed by everything that changes the audio, so editing the greeting
        or switching voice produces a different file rather than a stale one."""
        assert self._speaker is not None
        key = f"{self._speaker.provider}:{self._speaker.voice}:{self.text}"
        return self._cache_dir / f"greeting-{sha256(key.encode()).hexdigest()[:16]}.json"

    def _load(self) -> AudioClip | None:
        try:
            saved = json.loads(self._cache_file.read_text())
            return AudioClip(
                data=b64decode(saved["audio"]),
                media_type=saved["media_type"],
                seconds=saved["seconds"],
            )
        except (OSError, ValueError, KeyError):
            return None

    def _save(self, clip: AudioClip) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_file.write_text(
                json.dumps(
                    {
                        "media_type": clip.media_type,
                        "seconds": clip.seconds,
                        "audio": b64encode(clip.data).decode(),
                    }
                )
            )
        except OSError as exc:  # a cache that cannot be written is not an error
            logger.info("could not cache the greeting: %s", exc)

    async def prepare(self) -> None:
        if not self.text or self._speaker is None or self._clip is not None:
            return
        if self._attempted:
            # Tried once and failed. Retrying per visitor makes every page load
            # wait on a request already known to fail — which is exactly what
            # an exhausted quota looks like — for a greeting that will be text
            # either way. A restart is the retry.
            return
        self._attempted = True
        # Across restarts, not just within one process: the greeting is a fixed
        # string, and re-synthesising it on every `uv run voice-agent` bills for
        # bytes we already have. On a free plan that is a meaningful fraction of
        # a month's quota spent on a sentence that never changes.
        self._clip = self._load()
        if self._clip is not None:
            return
        started = time.perf_counter()
        try:
            self._clip = await self._speaker.synthesize(self.text)
        except VoiceAgentError as exc:
            # An agent that cannot greet must still be able to converse.
            logger.warning("could not prepare the greeting: %s", exc)
            return
        self._synthesis_ms = elapsed_ms(started)
        self._save(self._clip)
        logger.info("greeting synthesised in %d ms and cached", self._synthesis_ms)

    async def deliver(self, channel: Channel, conversation: Conversation) -> None:
        """Greet a conversation that has not started yet."""
        if not self.text:
            return
        await self.prepare()  # only does anything if startup could not
        # Checked *after* the await, and with nothing awaited between here and
        # the append: two tabs opening the same link would otherwise both pass
        # an earlier check, both wait, and both greet.
        if conversation.messages or conversation.ended:
            return
        conversation.add_assistant(self.text)
        await channel.send_json({"type": "greeting", "text": self.text})
        if self._clip is None:
            return
        await channel.send_audio(
            {
                "type": "audio",
                "media_type": self._clip.media_type,
                "bytes": len(self._clip),
                "seconds": self._clip.seconds,
                "synthesis_ms": self._synthesis_ms,
                "total_ms": 0,
                "cached": True,
            },
            self._clip.data,
        )


class Mic:
    """One listening session: audio frames in, transcripts out.

    Holds a queue rather than handing the recognizer the socket directly,
    because the recognizer consumes audio at its own pace while the receive
    loop must stay free to accept the next frame.

    Listening is metered, so it also expires: after `idle_timeout` with no
    speech, or `session_cap` in total. The watchdog is paused while a turn is
    in progress — during the agent's own reply the browser stops sending audio
    (half-duplex), so no transcript can arrive and an unpaused timer would
    punish the user for the agent talking.
    """

    def __init__(
        self,
        stt: STT,
        channel: Channel,
        on_final: Callable[[str], Awaitable[None]],
        on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
        on_session: Callable[[], Awaitable[None]] | None = None,
        extra_report: Callable[[], dict[str, object]] | None = None,
        idle_timeout: float | None = None,
        session_cap: float | None = None,
    ) -> None:
        self._stt = stt
        self._channel = channel
        self._on_final = on_final
        self._on_partial = on_partial
        self._on_session = on_session
        self._extra_report = extra_report
        self._agreement = StablePrefix()
        self.idle_timeout = IDLE_TIMEOUT_SECONDS if idle_timeout is None else idle_timeout
        self.session_cap = SESSION_CAP_SECONDS if session_cap is None else session_cap
        self._frames: asyncio.Queue[bytes | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._listening = False
        self._holds: set[str] = set()
        self._held_since = 0.0
        self._client_frames = 0
        self._last_frame_at = 0.0
        self._keepalive: asyncio.Task[None] | None = None
        self._started = 0.0
        self._heard_speech_at = 0.0

    @property
    def listening(self) -> bool:
        return self._listening

    async def start(self) -> None:
        if self._listening:
            return
        self._listening = True
        self._started = self._heard_speech_at = time.perf_counter()
        self._last_frame_at = self._started
        self._frames = asyncio.Queue()
        self._task = asyncio.create_task(self._run())
        self._watchdog = asyncio.create_task(self._watch())
        self._keepalive = asyncio.create_task(self._keep_alive())
        await self._channel.send_json({"type": "listening", "active": True})

    def feed(self, pcm: bytes) -> None:
        if self._frames is not None:
            self._client_frames += 1
            self._last_frame_at = time.perf_counter()
            self._frames.put_nowait(pcm)

    async def _keep_alive(self) -> None:
        """Fill any gap in the microphone's audio with silence.

        Counted separately from real frames, so "the browser sent no audio"
        stays a diagnosis rather than being papered over by the silence this
        sends on the browser's behalf.
        """
        silence = b"\x00" * int(self._stt.sample_rate * 2 * KEEPALIVE_BURST_SECONDS)
        while True:
            await asyncio.sleep(KEEPALIVE_BURST_SECONDS)
            if self._frames is None:
                continue
            if time.perf_counter() - self._last_frame_at >= KEEPALIVE_GAP_SECONDS:
                self._last_frame_at = time.perf_counter()
                self._frames.put_nowait(silence)

    def expect_silence(self, seconds: float) -> None:
        """Do not start counting the user's silence until the agent has stopped
        talking.

        The browser mutes its microphone for as long as a reply plays, so the
        user *cannot* be heard during it. Deriving that window from the clip we
        just sent, rather than from a message the browser promises to send when
        playback ends, means a lost or missing message cannot produce the worst
        outcome available here: stopping the microphone and blaming the user
        for silence they were never given a chance to break.
        """
        self._heard_speech_at = max(self._heard_speech_at, time.perf_counter() + seconds)

    def hold(self, name: str, held: bool) -> None:
        """Suspend or resume expiry, keyed by *what* is suspending it.

        Named rather than counted because two independent things suspend it —
        the turn, and the browser playing the reply — and they overlap: audio
        starts playing before `run_turn` returns, so a boolean would let the
        turn's exit clear the playback's hold. A counter fixes that but is
        unforgiving: a client that reports one clip interrupting another sends
        two holds and one release, and the count sticks above zero forever.
        A set is idempotent, so the server survives a client that miscounts.

        Releasing counts as fresh activity: the user has just been given
        something to respond to and should get the whole idle window to do it.
        """
        was_free = not self._holds
        self._holds.add(name) if held else self._holds.discard(name)
        if was_free and self._holds:
            self._held_since = time.perf_counter()
        if not self._holds:
            self._heard_speech_at = time.perf_counter()

    @contextlib.contextmanager
    def busy(self) -> Iterator[None]:
        self.hold("turn", True)
        try:
            yield
        finally:
            self.hold("turn", False)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK_SECONDS)
            now = time.perf_counter()

            # The hard cap is checked *before* the hold, and is not pausable.
            # A cap that a stuck hold can defeat is not a cap, and a stuck hold
            # is exactly the case it most needs to catch.
            if now - self._started >= self.session_cap:
                await self.stop(reason=f"listening stopped after {human_seconds(self.session_cap)}")
                return

            if self._holds:
                if now - self._held_since < MAX_HOLD_SECONDS:
                    continue
                logger.warning(
                    "expiry holds %s stuck for %.0fs; releasing",
                    sorted(self._holds),
                    now - self._held_since,
                )
                self._holds.clear()
                self._heard_speech_at = now
                continue

            if now - self._heard_speech_at >= self.idle_timeout:
                # Two different failures wear the same timeout. "You said
                # nothing" and "your browser sent us nothing" need different
                # words, because only one of them is the user's doing.
                if self._client_frames:
                    reason = f"listening stopped — no speech for {human_seconds(self.idle_timeout)}"
                else:
                    reason = "listening stopped — the browser sent no audio"
                await self.stop(reason=reason)
                return

    async def stop(self, reason: str | None = None, announce: bool = True) -> None:
        if not self._listening:
            return
        self._listening = False

        for name in ("_watchdog", "_keepalive"):
            helper: asyncio.Task[None] | None = getattr(self, name)
            setattr(self, name, None)
            if helper is not None and helper is not asyncio.current_task():
                helper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await helper

        if self._frames is not None:
            self._frames.put_nowait(None)  # ends the audio iterator, which ends the session
        task, self._task = self._task, None
        # `_run` calls stop() itself when the recognizer gives up, so it can be
        # the task we are being asked to wait for. Awaiting it there deadlocks
        # until the timeout and then cancels the very coroutine that is trying
        # to announce why listening ended.
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            if not task.done():
                task.cancel()
        self._frames = None

        if announce:
            payload: dict[str, object] = {"type": "listening", "active": False}
            if reason is not None:
                payload["reason"] = reason
            with contextlib.suppress(Exception):
                await self._channel.send_json(payload)

    async def _audio(self) -> AsyncIterator[bytes]:
        assert self._frames is not None
        while (chunk := await self._frames.get()) is not None:
            yield chunk

    async def _run(self) -> None:
        """Consume transcripts for as long as we are meant to be listening.

        A recognizer session can end *without* failing — Scribe closes an idle
        one with a normal close code, so the stream simply stops. Treating that
        as the end of listening is how the agent went deaf while the page still
        said "listening" and the user kept talking to nothing. So: if the
        stream ends while we still hold the microphone, reconnect and say so.
        """
        for attempt in range(MAX_RECONNECTS + 1):
            try:
                await self._consume()
            except VoiceAgentError as exc:
                logger.warning("listening failed: %s", exc)
                await self._channel.send_json({"type": "listen_error", "message": str(exc)})
                await self.stop()
                return

            if not self._listening:
                return  # a normal stop; the audio iterator was closed by us

            if attempt == MAX_RECONNECTS:
                await self.stop(reason="listening stopped — the recognizer kept dropping out")
                return

            logger.info("recognizer ended the session; reconnecting (%d)", attempt + 1)
            await self._channel.send_json(
                {"type": "listening", "active": True, "reason": "reconnected to the recognizer"}
            )

    async def _consume(self) -> None:
        # Every recognizer session starts from nothing. Agreement was only
        # being cleared on a commit, so a session that ended without one — the
        # user stopping mid-sentence, or a reconnect — carried its settled
        # words into the next utterance. There they wedge agreement completely
        # (nothing can extend a prefix the user is not saying) and are then
        # reported as "4 words settled early, prefix did not hold" against
        # words nobody repeated.
        self._agreement.reset()
        if self._on_session is not None:
            await self._on_session()
        heard_at = time.perf_counter()
        async for transcript in self._stt.stream(self._audio()):
            self._heard_speech_at = time.perf_counter()
            self._client_frames = 0
            if not transcript.is_final:
                # The recognizer will rewrite this text; agreement decides which
                # of it is safe to act on before the turn is over. `repeated`
                # says the recognizer found nothing new this time, which is the
                # closest thing to "they have stopped talking" available here.
                self._agreement.update(transcript.text)
                if not self._agreement.repeated:
                    # Only a partial carrying a new word means words are still
                    # being produced. Bumping this on every partial made
                    # `endpoint_ms` measure from the recognizer's last *message*
                    # rather than its last *word* — which made it identical to
                    # the speculation lead on every turn, since a repeat is
                    # exactly what starts a speculation.
                    heard_at = time.perf_counter()
                await self._channel.send_json(
                    {"type": "transcript", "text": transcript.text, "final": False}
                )
                if self._agreement.text and self._on_partial is not None:
                    await self._on_partial(self._agreement.text, self._agreement.repeated)
                continue

            if not transcript.text.strip():
                # A commit with nothing in it — the flush at the end of a
                # session, or a stretch of noise. Reporting it would put an
                # empty bubble and a meaningless endpointing figure on screen.
                heard_at = time.perf_counter()
                self._agreement.reset()
                continue

            if self._agreement.contradictions:
                # Should be zero: agreement is a bet that the recognizer will
                # not unsay something it has already said twice. If this ever
                # fires, the bet is not safe on this recognizer and everything
                # built on it needs rethinking.
                logger.warning(
                    "agreement contradicted itself %d time(s) this turn",
                    self._agreement.contradictions,
                )

            await self._channel.send_json(
                {
                    **(self._extra_report() if self._extra_report else {}),
                    "type": "transcript",
                    "text": transcript.text,
                    "final": True,
                    # Measured from the last *partial transcript*, not from
                    # the moment the user actually stopped talking — which
                    # this server cannot see, because it does not run a VAD.
                    # It therefore understates the real endpointing cost by
                    # however long the recognizer lagged behind the audio;
                    # measured externally that gap was ~800 ms on a 1.65 s
                    # utterance. Delegating endpointing to the STT vendor
                    # also delegates the ability to measure it.
                    "endpoint_ms": elapsed_ms(heard_at),
                    # Was the text we called stable really how the turn began?
                    # The honest test of whether agreement — and everything
                    # built on it — was worth trusting.
                    "prefix_held": self._agreement.holds_for(transcript.text),
                    "stable_words": len(self._agreement.text.split()),
                }
            )
            self._agreement.reset()
            heard_at = time.perf_counter()
            await self._on_final(transcript.text)


def create_app(
    llm: LLM | None = None,
    store: SessionStore | None = None,
    tts: TTS | None = None,
    stt: STT | None = None,
    voice: bool = True,
    ears: bool = True,
    greeting: str | None = None,
) -> FastAPI:
    """`voice=False` / `ears=False` run the agent silent or deaf, which is also
    what VOICE_AGENT_TTS=none and VOICE_AGENT_STT=none do. Neither capability
    may be load-bearing for the other, or for typing."""
    settings = load_settings()
    engine = llm if llm is not None else create_llm(settings.provider, settings.model)
    sessions = store if store is not None else SessionStore()
    speaker: TTS | None = tts
    if speaker is None and voice:
        speaker = create_tts(settings.voice_provider, settings.voice)
    listener: STT | None = stt
    if listener is None and ears:
        listener = create_stt(settings.ears_provider, settings.vad_silence)
    system_prompt = load_system_prompt()
    opening = Greeting(settings.greeting if greeting is None else greeting, speaker)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Synthesised once, at startup, before anyone is waiting on it. The
        # first synthesis in a process took 3.1 s against 250-290 ms for every
        # one after, and paying that on someone's first question is the worst
        # possible moment for it.
        await opening.prepare()
        yield

    app = FastAPI(title="voice-agent", lifespan=lifespan)

    @app.get("/")
    async def new_conversation() -> RedirectResponse:
        """Minting the key here is what makes the link the conversation:
        every page load starts a new one and lands on its own URL."""
        conversation = sessions.create()
        return RedirectResponse(url=f"/c/{conversation.id}", status_code=303)

    @app.get("/c/{key}")
    async def chat_page(key: str) -> HTMLResponse:
        try:
            sessions.get(key)
        except SessionNotFoundError:
            return HTMLResponse("<h1>404 — no such conversation</h1>", status_code=404)
        return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"))

    @app.websocket("/ws/{key}")
    async def chat_socket(websocket: WebSocket, key: str) -> None:
        try:
            conversation = sessions.get(key)
        except SessionNotFoundError:
            await websocket.close(code=4404, reason="no such conversation")
            return

        await websocket.accept()
        channel = Channel(websocket)
        turn_lock = asyncio.Lock()

        mic: Mic | None = None
        guess: Speculation | None = None
        guesses = Guesses()
        warming: asyncio.Task[None] | None = None
        warms = Warmings()
        warmed_words = 0

        async def abandon_guess() -> None:
            """Drop a guess the recognizer has just proved premature."""
            nonlocal guess
            if guess is None:
                return
            stale, guess = guess, None
            guesses.discard(await stale.abandon())

        async def on_partial(stable: str, repeated: bool) -> None:
            nonlocal guess
            if not repeated:
                # More words arrived, so whatever we were generating answers a
                # question that is still being asked. Cancelling now is what
                # keeps a wrong guess cheap.
                await abandon_guess()
                await warm_on(stable)
                return
            if SPECULATE and guess is None:
                guess = Speculation(engine, system_prompt, conversation.messages, stable)

        async def forget_utterance() -> None:
            """Everything known about the utterance in progress is void."""
            forget_warming()
            await abandon_guess()
            guesses.reset()

        def forget_warming() -> None:
            """Discard everything known about warming the current utterance.

            Called both when a turn commits and when a recognizer session
            starts. The second matters as much as the first: a session that
            ends without a commit — the user stopping mid-sentence, or a
            reconnect — otherwise leaves its word count behind, and the
            throttle then reads the next utterance as a continuation of one
            nobody finished and declines to warm it at all.

            A warm still in flight when the turn ends can no longer help it, and
            left alone it lands in the *next* turn's books — reporting an
            impossible lead ("7.6 s before the turn ended" on a two-second
            utterance) and, because the growth counter was never reset either,
            blocking that turn's own warm. Both were visible in a real session
            before they were understood.
            """
            nonlocal warming, warmed_words
            if warming is not None and not warming.done():
                # Cancelling is what keeps it out of the next turn's books.
                # A guard inside the task cannot help: once `warm()` returns
                # there is no await before it records, so there is no point at
                # which a late check could run.
                warming.cancel()
            warming = None
            warmed_words = 0
            warms.reset()

        async def warm_on(stable: str) -> None:
            """Prefill on the agreed prefix, without blocking the transcript.

            Fire-and-forget on purpose: a warm that arrives late is worthless
            but a warm that delays a partial is actively harmful, and at most
            one is in flight because a second would only re-prefill what the
            first is already caching.
            """
            nonlocal warming, warmed_words
            if not WARM_ON_STABLE or (warming is not None and not warming.done()):
                return
            words = len(stable.split())
            # `warmed_words` alone decides this: 0 means nothing has been warmed
            # for this turn yet. Consulting the warm *count* as well would make
            # the per-turn reset redundant, and a reset nothing depends on is a
            # reset that quietly stops happening.
            if warmed_words and words - warmed_words < WARM_MIN_NEW_WORDS:
                return
            warmed_words = words
            warms.attempt()

            async def run() -> None:
                try:
                    warms.record(
                        await engine.warm(
                            system_prompt, [*conversation.messages, Message("user", stable)]
                        )
                    )
                except VoiceAgentError as exc:
                    logger.info("warm failed, which costs only the warm: %s", exc)

            warming = asyncio.create_task(run())

        async def claim_guess(committed: str) -> Speculation | None:
            """Keep the guess if the turn that arrived is the one it answered."""
            nonlocal guess
            if guess is None:
                return None
            candidate, guess = guess, None
            # `exit` produces no reply, so a guess at it answers nothing. Left
            # claimed it would run to completion unread and uncounted.
            if is_exit_command(committed) or not candidate.answers(committed):
                guesses.discard(await candidate.abandon())
                return None
            guesses.adopt(elapsed_ms(candidate.started_at))
            return candidate

        async def take_turn(text: str) -> None:
            # A spoken turn and a typed one are the same turn. The lock is not
            # decoration: a committed transcript arrives on the microphone task,
            # not the receive loop, so without it a fast second utterance could
            # start a turn while the first is still streaming.
            forget_warming()  # already reported on the transcript frame
            claimed = await claim_guess(text)
            async with turn_lock:
                with mic.busy() if mic is not None else contextlib.nullcontext():
                    seconds = await run_turn(
                        channel,
                        conversation,
                        engine,
                        speaker,
                        system_prompt,
                        text,
                        fragments=claimed.stream() if claimed is not None else None,
                        report=guesses.report(),
                    )
                if mic is not None and seconds:
                    mic.expect_silence(seconds)
            guesses.reset()  # reported inside the turn, so cleared after it

        if listener is not None:
            mic = Mic(
                listener,
                channel,
                take_turn,
                on_partial=on_partial,
                on_session=forget_utterance,
                extra_report=warms.report,
            )

        await channel.send_json(
            {
                "type": "ready",
                "session": conversation.id,
                "provider": engine.provider,
                "model": engine.model,
                "voice": (
                    {"provider": speaker.provider, "voice": speaker.voice} if speaker else None
                ),
                "ears": (
                    {"provider": listener.provider, "sample_rate": listener.sample_rate}
                    if listener
                    else None
                ),
                "history": serialize(conversation.messages),
                "ended": conversation.ended,
            }
        )

        await opening.deliver(channel, conversation)

        try:
            while not conversation.ended:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if (frame := message.get("bytes")) is not None:
                    if mic is not None:
                        mic.feed(frame)
                elif (raw := message.get("text")) is not None:
                    await handle_text(channel, conversation, mic, take_turn, raw)
        except WebSocketDisconnect:
            return
        finally:
            if mic is not None:
                await mic.stop(announce=False)
            # A guess outlives the browser that prompted it otherwise, and goes
            # on generating — and being billed for — a reply to a question
            # nobody is waiting for any more.
            await forget_utterance()

    return app


async def handle_text(
    channel: Channel,
    conversation: Conversation,
    mic: Mic | None,
    take_turn: Callable[[str], Awaitable[None]],
    raw: str,
) -> None:
    try:
        payload = json.loads(raw)
        kind = str(payload["type"])
    except (ValueError, KeyError, TypeError):
        await channel.send_json({"type": "error", "message": "malformed message"})
        return

    if kind == "playback":
        # The browser is muted while it plays the reply (half-duplex), so no
        # transcript can arrive for however long the audio lasts — which for a
        # long answer is far more than the idle window. Only the browser knows
        # when playback actually ends.
        if mic is not None:
            mic.hold("playback", bool(payload.get("active")))
        return

    if kind in ("listen_start", "listen_stop"):
        if mic is None:
            await channel.send_json({"type": "listen_error", "message": "this agent has no ears"})
            return
        # Mic announces its own state changes, so an expiry and an explicit
        # stop reach the browser through exactly one path.
        await (mic.start() if kind == "listen_start" else mic.stop())
        return

    text = str(payload.get("text", "")).strip()
    if not text:
        return

    if is_exit_command(text):
        conversation.end()
        if mic is not None:
            await mic.stop()
        await channel.send_json({"type": "ended"})
        return

    await take_turn(text)


async def run_turn(
    channel: Channel,
    conversation: Conversation,
    engine: LLM,
    speaker: TTS | None,
    system_prompt: str,
    text: str,
    fragments: AsyncIterator[str] | None = None,
    report: dict[str, object] | None = None,
) -> float | None:
    """Returns how long the spoken reply lasts, when that is known — the window
    during which the browser will be muted and the user cannot be heard."""
    if is_exit_command(text):
        conversation.end()
        await channel.send_json({"type": "ended"})
        return None

    conversation.add_user(text)
    await channel.send_json({"type": "reply_start"})

    started = time.perf_counter()
    first_token_at: float | None = None
    produced: list[str] = []
    # A claimed speculation is already generating — possibly already finished.
    # Everything after this point is identical either way, which is the point:
    # a turn does not know whether its reply was guessed at.
    source = (
        fragments if fragments is not None else engine.stream(system_prompt, conversation.messages)
    )
    try:
        async for fragment in source:
            if first_token_at is None:
                first_token_at = time.perf_counter()
            produced.append(fragment)
            await channel.send_json({"type": "delta", "text": fragment})
    except (ProviderError, VoiceAgentError) as exc:
        # Fail closed: drop the user turn too, so a failed exchange never leaves
        # a dangling question in the context that the next call would resend.
        conversation.messages.pop()
        logger.warning("turn failed for session %s: %s", conversation.id, exc)
        await channel.send_json({"type": "error", "message": str(exc)})
        return None

    reply = "".join(produced)
    # Falls back to "now" when nothing streamed, so an empty reply reports its
    # whole duration as time-to-first-token rather than as zero of everything.
    generation_started = first_token_at if first_token_at is not None else time.perf_counter()
    conversation.add_assistant(reply)
    await channel.send_json(
        {
            **(report or {}),
            "type": "reply_end",
            "text": reply,
            "chars": len(reply),
            # Split at the first token, because the halves mean different
            # things. `ttft_ms` is dead air the user actually experiences and
            # is the number the latency budget targets; `generation_ms` is
            # throughput, which streaming already hides behind text appearing
            # on screen. A single "reply took N ms" would blur the one that
            # matters into the one that does not.
            "ttft_ms": elapsed_ms(started, first_token_at),
            "generation_ms": elapsed_ms(generation_started),
        }
    )

    if speaker is not None:
        return await speak(channel, speaker, reply, started)
    return None


async def speak(channel: Channel, speaker: TTS, reply: str, turn_started: float) -> float | None:
    """Synthesize the whole reply, then send it.

    Batched, and deliberately after `reply_end`: the text is already on screen
    while this runs, so the wait this chapter introduces is visible rather than
    hidden inside the turn.
    """
    synthesis_started = time.perf_counter()
    try:
        clip = await speaker.synthesize(reply)
    except VoiceAgentError as exc:
        # The reply itself is fine; only its voice failed. Degrade to text
        # rather than discarding a good answer — losing the words is a far
        # worse failure than losing the audio.
        logger.warning("synthesis failed: %s", exc)
        await channel.send_json({"type": "audio_error", "message": str(exc)})
        return None

    await channel.send_audio(
        {
            "type": "audio",
            "media_type": clip.media_type,
            "bytes": len(clip),
            "seconds": clip.seconds,
            "synthesis_ms": elapsed_ms(synthesis_started),
            # The number the whole project is judged on: send to first audio.
            "total_ms": elapsed_ms(turn_started),
        },
        clip.data,
    )
    return clip.seconds
