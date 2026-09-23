"""One connected conversation: what arrives from the user, and the turns it starts.

A turn runs in its own task, never on the coroutine that produced its text, so
the microphone and the socket keep reading while a reply streams, and the turn
can be cancelled.

Interruption has one rule: a new turn from the user stops whatever the agent is
still doing with the last one. The first words recognized while the agent is
audible count, long before they are committed.

`Initiative` starts turns out of silence through the same door. The session
owns the *yield rule* (`quiet_for`) because only it sees every reason not to
speak; `initiative.py` owns when and what.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence

from voice_agent import trace
from voice_agent.channel import Channel
from voice_agent.conversation import Conversation
from voice_agent.heard import Spoken, truncated
from voice_agent.initiative import LADDER, Initiative, Rung
from voice_agent.llm import LLM
from voice_agent.mic import Mic
from voice_agent.speculation import Speculation, Speculator
from voice_agent.stt import STT
from voice_agent.timing import elapsed_ms
from voice_agent.tts import TTS
from voice_agent.tts.base import once
from voice_agent.turn import Context, Turn, run_turn

logger = logging.getLogger(__name__)

EXIT_COMMANDS = frozenset({"exit", "quit", "bye", "goodbye"})
"""Typing or saying any of these, as the whole message, ends the conversation."""


ANSWER_TIMEOUT_SECONDS = 1.0
"""How long to wait for the browser to say how much of an interrupted reply it
played, before falling back to the server's own estimate."""


def is_exit_command(text: str) -> bool:
    return text.strip().strip(".!?").casefold() in EXIT_COMMANDS


class Session:
    def __init__(
        self,
        channel: Channel,
        conversation: Conversation,
        engine: LLM,
        speaker: TTS | None,
        system_prompt: str,
        listener: STT | None,
        ladder: Sequence[Rung] | None = None,
    ) -> None:
        """An empty `ladder` means the agent never speaks first."""
        self.conversation = conversation
        self._channel = channel
        self._context = Context(channel, conversation, engine, speaker, system_prompt)
        self._speculator = Speculator(engine, system_prompt)
        # Turns queue rather than overlap.
        self._lock = asyncio.Lock()
        self._turns: dict[asyncio.Task[None], Turn] = {}
        self._voice: Spoken | None = None
        """The agent's latest speech, which the user may still be hearing."""
        self._interrupts = 0
        self._answer: tuple[int, asyncio.Future[float | None]] | None = None
        """The interruption waiting to hear how much the browser played, by id,
        so a late answer cannot settle the next one."""
        self._settling: asyncio.Task[None] | None = None
        self.mic = (
            Mic(
                listener,
                channel,
                self.submit,
                on_partial=self._on_partial,
                on_session=self.forget_utterance,
                on_speech=self._on_speech,
            )
            if listener is not None
            else None
        )
        self._initiative = Initiative(
            engine,
            system_prompt,
            conversation,
            quiet=self.quiet_for,
            speak=self.speak,
            report=channel.send_json,
            ladder=LADDER if ladder is None else ladder,
        )

    def start(self) -> None:
        """Begin considering whether to speak; called once the greeting is out."""
        if self.mic is not None:
            self._initiative.start()

    def quiet_for(self) -> float | None:
        """Seconds of silence it would be safe to speak into, or `None` for
        "not now" (which is not the same as a silence just begun).

        It cannot say whether the user is talking right now — only a voice
        detector could; a shrinking number is the clock's signal for that.
        """
        if self.conversation.ended or self._turns or self._unsettled():
            return None
        if self.mic is None or not self.mic.listening or self.mic.held:
            return None
        return self.mic.quiet_for

    async def _on_partial(self, stable: str, repeated: bool) -> None:
        history = self.conversation.context
        if not repeated:
            # More words: any guess answers a question still being asked.
            await self._speculator.abandon()
            return
        if not self._turns and not self._unsettled():
            # Not while a reply is running, audible or being cut: the history
            # the guess would answer is about to change.
            self._speculator.on_settled(stable, history)

    def _unsettled(self) -> bool:
        """The last reply may still be in the user's ear, or is being cut down
        to what they heard. `audible` rather than the microphone's holds, which
        are released when the reply is written, before its voice finishes."""
        audible = self._voice is not None and self._voice.audible
        return audible or (self._settling is not None and not self._settling.done())

    def voiced(self, voice: Spoken | None) -> None:
        """Speech that did not come from a turn — the greeting."""
        if voice is not None:
            self._voice = voice

    def playback(self, active: bool) -> None:
        """The browser started or stopped playing the agent's voice."""
        if self._voice is not None:
            self._voice.playback(active)
        if self.mic is not None:
            self.mic.hold("playback", active)

    async def _on_speech(self) -> None:
        """Words recognized. Over the agent's voice, they are an interruption."""
        if self._voice is not None and self._voice.audible:
            # Its history is about to change under it.
            await self._speculator.abandon()
            await self.interrupt()

    def heard(self, interrupt_id: int, played_ms: float | None) -> None:
        """The browser's answer to `interrupt`: how far into the reply it got,
        or `None` if nothing was playing any more."""
        if self._answer is None:
            return
        expected, answer = self._answer
        if interrupt_id == expected and not answer.done():
            answer.set_result(played_ms)

    async def interrupt(self) -> None:
        """Stop the agent: its voice at once, and any reply still being written.

        The browser is told first, so it goes quiet at once. What the reply is
        recorded as is settled in the background, since it needs the browser's
        answer and the caller must not wait for it.
        """
        voice = self._voice
        audible = voice is not None and voice.audible
        live = {
            task: turn
            for task, turn in self._turns.items()
            if not task.done() and not turn.interrupted
        }
        if not live and not audible:
            return
        triggered = time.perf_counter()
        answer: asyncio.Future[float | None] | None = None
        if audible:
            assert voice is not None
            voice.interrupted_at = triggered
            self._interrupts += 1
            answer = asyncio.get_running_loop().create_future()
            self._answer = (self._interrupts, answer)
            await self._channel.send_json({"type": "interrupt", "id": self._interrupts})
        for task, turn in live.items():
            turn.interrupted = True
            if turn.started:
                task.cancel()
        self._settling = asyncio.create_task(
            self._settle(voice, list(live), answer, triggered, self._settling)
        )

    async def _settle(
        self,
        voice: Spoken | None,
        turns: list[asyncio.Task[None]],
        answer: asyncio.Future[float | None] | None,
        triggered: float,
        previous: asyncio.Task[None] | None,
    ) -> None:
        """Cut the interrupted reply down to what the user heard."""
        stopped_ms = elapsed_ms(triggered)
        # Waited on, not awaited: a turn cancelled again by `end()` re-raises
        # its cancellation, which must not read as this task's own.
        pending = {*turns, *([previous] if previous is not None else [])}
        if pending:
            await asyncio.wait(pending)
        if voice is None or voice.message is None:
            return  # silent agent, or nothing written: nothing to cut
        estimated = False
        played_ms: float | None = None
        if answer is not None:
            try:
                async with asyncio.timeout(ANSWER_TIMEOUT_SECONDS):
                    played_ms = await answer
            except TimeoutError:
                logger.info("the browser did not say how much it played; estimating")
                played_ms, estimated = voice.estimate_played_ms(), True
            if played_ms is None:
                return  # it had already finished: heard in full
            said = voice.heard(played_ms)
        elif voice.started_at is None:
            said = ""  # cancelled before a word of it was voiced
        else:
            return
        written = voice.message.content
        self.conversation.replace(voice.message, truncated(written, said))
        if answer is not None:
            await self._channel.send_json(
                {
                    "type": "truncated",
                    "played_ms": round(played_ms or 0),
                    "heard_chars": len(said),
                    "chars": len(written),
                    # From deciding to interrupt to the browser being told.
                    "stop_ms": stopped_ms,
                    "estimated": estimated,
                    "timed": bool(voice.ends_ms),
                }
            )

    async def submit(self, text: str) -> None:
        """Start a turn, spoken or typed alike. Returns once it has *started*,
        so the caller keeps reading while the reply streams."""
        if self.conversation.ended:
            return  # a recognizer's last commit can land after the end
        self._initiative.reset()  # the user spoke: the silence budget starts over
        if is_exit_command(text):
            await self._speculator.abandon()
            await self.end()
            return
        # Whatever the agent is still doing answers a question the user has moved on from.
        await self.interrupt()
        claimed = await self._speculator.claim(text)
        report = self._speculator.report()
        self._speculator.reset()
        self._begin(Turn(text, after=self._settling), claimed, report)

    async def speak(self, line: str, rung: int) -> None:
        """Say a line `Initiative` decided on, as an ordinary turn: the same
        lock, interruption and truncation, so nothing downstream knows."""
        if self.conversation.ended or self.quiet_for() is None:
            return  # checked again at the last moment
        turn = Turn(None, line, after=self._settling)
        self._begin(turn, None, {"initiative": rung})

    def _begin(self, turn: Turn, claimed: Speculation | None, report: dict[str, object]) -> None:
        self._voice = turn.voice
        task = asyncio.create_task(self._take_turn(turn, claimed, report))
        self._turns[task] = turn
        task.add_done_callback(self._forget_turn)

    def _forget_turn(self, task: asyncio.Task[None]) -> None:
        self._turns.pop(task, None)
        if not task.cancelled() and (failure := task.exception()) is not None:
            # E.g. the browser closed mid-speech and a write raised. Retrieved
            # here, or asyncio prints a traceback on every such disconnect.
            logger.info("a turn ended with %r", failure)

    async def _take_turn(
        self, turn: Turn, claimed: Speculation | None, report: dict[str, object]
    ) -> None:
        try:
            async with self._lock:
                if turn.after is not None:
                    # After the answer it interrupted has been cut to what was heard.
                    await asyncio.wait({turn.after})
                if turn.interrupted:
                    # Overtaken before it began: the question is kept, the
                    # answer is not wanted. An unprompted line just never happened.
                    if turn.text is not None:
                        self.conversation.add_user(turn.text)
                    return
                turn.started = True
                kind = "turn.unprompted" if turn.text is None else "turn"
                if claimed is not None:
                    fragments = claimed.stream()  # a guess made before the question ended
                elif turn.said is not None:
                    fragments = once(turn.said)  # a line the agent decided to say
                else:
                    fragments = None  # generated now, from the question just asked
                with (
                    self.mic.busy() if self.mic is not None else contextlib.nullcontext(),
                    trace.span(
                        kind,
                        {"said": turn.text or turn.said, "speculated": claimed is not None},
                        trace_id=self.conversation.id,
                    ),
                ):
                    seconds = await run_turn(
                        self._context,
                        turn,
                        fragments,
                        usage=claimed.usage if claimed is not None else None,
                        report=report,
                    )
                if self.mic is not None and seconds:
                    self.mic.expect_silence(seconds)
        finally:
            if claimed is not None:
                # Stops a guess still generating (and billing) if the turn was cancelled.
                await claimed.abandon()

    async def _cancel_turns(self) -> None:
        turns = list(self._turns)
        for turn in turns:
            turn.cancel()
        for turn in turns:
            try:
                await turn
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # A write that lost the race with the socket closing. Not
                # re-raised: the rest of the cleanup must still run.
                logger.info("a turn ended with %r as it was cancelled", exc)

    async def _cancel_settling(self) -> None:
        settling, self._settling = self._settling, None
        if settling is not None:
            settling.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await settling

    async def end(self, reason: str = "") -> None:
        """End the conversation; `reason` says why when it was not `exit`."""
        self.conversation.end()
        # First, so no tick starts a turn during the teardown.
        await self._initiative.stop()
        # Cancelled before announcing, so nothing from a reply can follow it.
        await self._cancel_turns()
        await self._cancel_settling()
        ended: dict[str, object] = {"type": "ended"}
        if reason:
            ended["reason"] = reason
        await self._channel.send_json(ended)
        if self.mic is not None:
            await self.mic.stop()

    async def forget_utterance(self) -> None:
        """Everything known about the utterance in progress is void."""
        await self._speculator.abandon()
        self._speculator.reset()

    async def close(self) -> None:
        """The socket is gone."""
        await self._initiative.stop()
        if self.mic is not None:
            await self.mic.stop(announce=False)
        # Otherwise a turn or a guess goes on generating, and billing, for nobody.
        await self._cancel_turns()
        await self._cancel_settling()
        await self.forget_utterance()
