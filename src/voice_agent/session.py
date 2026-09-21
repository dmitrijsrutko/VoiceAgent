"""One connected conversation: what arrives from the user, and the turns it starts.

A turn runs in its own task, never on the coroutine that produced its text. A
spoken turn used to run *inside* the microphone's task and a typed one inside
the socket's receive loop, so for the length of a reply neither could read
anything — stopping the microphone waited for the whole answer, and after five
seconds cancelled it mid-sentence. Owning the turn here is also what makes it
cancellable, which is what interruption needs.

Interruption has one rule: a new turn from the user stops whatever the agent is
still doing with the last one — writing it, speaking it, or both. Speech can
start that turn early: the first words recognized while the agent is audible
stop it there and then, long before those words are committed.

A turn no longer has to come from the user at all. `Initiative` can start one
out of a silence, and it goes through exactly the same door — the same lock,
the same voice, the same truncation when it is talked over. The session owns
the *yield rule* for that (`quiet_for`, below), because it is the only thing
that can see every reason not to speak; the policy of when and what lives in
`initiative.py`.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass

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
from voice_agent.turn import Interruption, run_turn
from voice_agent.warming import Warmer

logger = logging.getLogger(__name__)

EXIT_COMMANDS = frozenset({"exit", "quit", "bye", "goodbye"})
"""Typing or saying any of these ends the conversation. Matched on the whole
message, case- and punctuation-insensitively, so that "Bye!" ends it but
"goodbye is a strange word" does not."""


ANSWER_TIMEOUT_SECONDS = 1.0
"""How long to wait for the browser to say how much of an interrupted reply it
played. It answers within a round trip; a browser that never does must not
hold up the next turn, which falls back to the server's own estimate."""


def is_exit_command(text: str) -> bool:
    return text.strip().strip(".!?").casefold() in EXIT_COMMANDS


@dataclass(eq=False, slots=True)
class Turn:
    """One turn from the user, from the moment it is submitted.

    Kept together because an interruption treats a turn by where it is: one
    already running is cancelled and keeps what it wrote; one still waiting for
    the turn before it has not begun, so there is nothing to cancel — only a
    question to record.
    """

    text: str | None
    """What the user said. `None` for a turn the agent started itself."""
    said: str | None
    """The line an unprompted turn already decided to say, which it speaks
    instead of generating one. `None` for an ordinary turn."""
    voice: Spoken
    interruption: Interruption
    after: asyncio.Task[None] | None
    """The cut of the reply this turn interrupted, which it waits for. Captured
    when it is submitted: a later cut can be waiting on this very turn."""
    started: bool = False


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
        """`ladder` empty means the agent never speaks first — the reactive
        agent of every chapter before this one, unchanged."""
        self.conversation = conversation
        self._channel = channel
        self._engine = engine
        self._speaker = speaker
        self._system_prompt = system_prompt
        self._warmer = Warmer(engine, system_prompt)
        self._speculator = Speculator(engine, system_prompt)
        # Turns queue rather than overlap: a committed transcript can arrive
        # while a typed turn is still streaming.
        self._lock = asyncio.Lock()
        self._turns: dict[asyncio.Task[None], Turn] = {}
        self._voice: Spoken | None = None
        """The agent's latest speech, which the user may still be hearing."""
        self._interrupts = 0
        self._answer: tuple[int, asyncio.Future[float | None]] | None = None
        """The interruption waiting to hear how much the browser played, by id:
        an answer that arrives after its own timeout must not settle the next."""
        self._settling: asyncio.Task[None] | None = None
        self.mic = (
            Mic(
                listener,
                channel,
                self.submit,
                on_partial=self._on_partial,
                on_session=self.forget_utterance,
                on_speech=self._on_speech,
                extra_report=self._warmer.report,
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
        """Begin considering whether to speak. Called once the greeting is out,
        so the first silence measured is the one *after* the agent's own voice.

        Explicit rather than started in `__init__`, which is not guaranteed to
        run on the event loop — and because a session that never starts the
        clock is exactly the `--initiative off` agent.
        """
        if self.mic is not None:
            self._initiative.start()

    def quiet_for(self) -> float | None:
        """Seconds of silence it would be safe to speak into, or `None` for
        "not now".

        The one place that knows every reason to hold back, which is why the
        clock asks rather than deciding for itself. `None` is deliberately not
        zero: a moment that does not count must not be confused with a silence
        that has only just begun.

        Note what this deliberately does *not* answer: whether the user is
        talking *right now*. Nothing in this session can — a first partial only
        restarts the silence, it does not make the moment unavailable — so a
        shrinking number is the clock's own signal for that, and a real voice
        detector is what would replace it.
        """
        if self.conversation.ended or self._turns or self._unsettled():
            return None
        if self.mic is None or not self.mic.listening or self.mic.held:
            return None
        return self.mic.quiet_for

    async def _on_partial(self, stable: str, repeated: bool) -> None:
        history = self.conversation.messages
        if not repeated:
            # More words arrived, so whatever we were generating answers a
            # question that is still being asked. Cancelling now is what keeps
            # a wrong guess cheap.
            await self._speculator.abandon()
            self._warmer.on_growth(stable, history)
            return
        if not self._turns and not self._unsettled():
            # Not while a turn is running: its reply is not in the history yet,
            # and a guess adopted without it would answer the wrong
            # conversation. Nor while the reply is audible or being cut down to
            # what was heard, for the same reason. A warm has no such problem —
            # it only prefills.
            self._speculator.on_settled(stable, history)

    def _unsettled(self) -> bool:
        """The last reply may still be in the user's ear, or is being cut down
        to what they heard of it.

        Asks `Spoken.audible`, which is the right question — speculation must
        not adopt a guess while a reply whose history is about to change is
        still playing, and the microphone's holds cannot answer that: the turn's
        hold is released when the reply is *written*, seconds before its voice
        finishes. What made this dangerous was not the signal but that it could
        not terminate; `audible` is now bounded by the duration of the audio
        actually sent, so this is both correct and self-clearing.
        """
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

        The browser is told first, so it goes quiet without waiting for the
        synthesizer's socket to close. What the reply is recorded as is settled
        in the background — it needs the browser's answer, and whoever called
        this (the microphone, mid-transcript) must not wait for it.
        """
        voice = self._voice
        audible = voice is not None and voice.audible
        live = {
            task: turn
            for task, turn in self._turns.items()
            if not task.done() and not turn.interruption.requested
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
            turn.interruption.requested = True
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
        # Waited on, never awaited directly: a turn cancelled again by `end()`
        # re-raises its cancellation, which must not read as this task's own.
        # A turn that was talked over returns normally, and the reply is only in
        # the history once it has.
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
                    # From deciding to interrupt — the recognizer's first word,
                    # or a message arriving — to the browser being told.
                    "stop_ms": stopped_ms,
                    "estimated": estimated,
                    "timed": bool(voice.ends_ms),
                }
            )

    async def submit(self, text: str) -> None:
        """Start a turn. A spoken turn and a typed one are the same turn.

        Returns once the turn has *started*, so whoever called — the
        microphone, or the socket's receive loop — keeps reading while the
        reply streams.
        """
        if self.conversation.ended:
            # The recognizer flushes a last commit after listening stops, and
            # it can land after the conversation it belonged to has ended.
            return
        self._warmer.forget()  # already reported on the transcript frame
        # The user has spoken, so the budget is theirs again — whatever the
        # agent spent on the silence before it.
        self._initiative.reset()
        if is_exit_command(text):
            # `exit` produces no reply, so a guess at it answers nothing. Left
            # alone it would run to completion unread and uncounted.
            await self._speculator.abandon()
            await self.end()
            return
        # Whatever the agent is still doing answers a question the user has
        # moved on from — typed, or spoken while it was still thinking.
        await self.interrupt()
        claimed = await self._speculator.claim(text)
        report = self._speculator.report()
        self._speculator.reset()
        self._begin(
            Turn(text, None, Spoken(), Interruption(), after=self._settling), claimed, report
        )

    async def speak(self, line: str, rung: int) -> None:
        """Say something nobody asked for.

        The line has already been decided and vetted by `Initiative`; all that
        is left is to say it. It becomes an ordinary turn — queued on the same
        lock, cancelled by the same interruption, cut down to what was heard by
        the same code — so nothing downstream has to know it was unprompted.
        """
        if self.conversation.ended or self.quiet_for() is None:
            # Checked again here, at the last possible moment: `Initiative`
            # cleared this line a socket write ago, and the yield rule belongs
            # to whoever can see every reason to hold back.
            return
        turn = Turn(None, line, Spoken(), Interruption(), after=self._settling)
        self._begin(turn, None, {"initiative": rung})

    def _begin(self, turn: Turn, claimed: Speculation | None, report: dict[str, object]) -> None:
        self._voice = turn.voice
        task = asyncio.create_task(self._take_turn(turn, claimed, report))
        self._turns[task] = turn
        task.add_done_callback(self._forget_turn)

    def _forget_turn(self, task: asyncio.Task[None]) -> None:
        self._turns.pop(task, None)
        if not task.cancelled() and (failure := task.exception()) is not None:
            # A turn can die on its own before anyone cancels it: the browser
            # closes mid-speech, and the next audio write raises the socket's
            # error. Gone from `_turns`, nobody would ever retrieve it — asyncio
            # printed it as a traceback on every such disconnect.
            logger.info("a turn ended with %r", failure)

    async def _take_turn(
        self, turn: Turn, claimed: Speculation | None, report: dict[str, object]
    ) -> None:
        try:
            async with self._lock:
                if turn.after is not None:
                    # The question goes into the history after the answer it
                    # interrupted has been cut down to what was heard.
                    await asyncio.wait({turn.after})
                if turn.interruption.requested:
                    # Overtaken by a newer turn before it began. The user still
                    # asked it; only the answer is no longer wanted. An
                    # unprompted line has no question to keep, so it simply
                    # never happened.
                    if turn.text is not None:
                        self.conversation.add_user(turn.text)
                    return
                turn.started = True
                kind = "turn.unprompted" if turn.text is None else "turn"
                # A turn reads its reply from exactly one source, and which one
                # is what distinguishes the three kinds of turn there are.
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
                        self._channel,
                        self.conversation,
                        self._engine,
                        self._speaker,
                        self._system_prompt,
                        turn.text,
                        fragments=fragments,
                        usage=claimed.usage if claimed is not None else None,
                        report=report,
                        voice=turn.voice,
                        interruption=turn.interruption,
                    )
                if self.mic is not None and seconds:
                    self.mic.expect_silence(seconds)
        finally:
            if claimed is not None:
                # A no-op for a guess the turn read to the end; for a turn
                # cancelled first, it stops the generation still being billed.
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
                # A turn cancelled because the socket closed can end with the
                # socket's own error instead: its write lost the race with the
                # receive loop noticing. Re-raised, it aborted the rest of the
                # cleanup — the turns after it, and any guess still billing.
                logger.info("a turn ended with %r as it was cancelled", exc)

    async def _cancel_settling(self) -> None:
        settling, self._settling = self._settling, None
        if settling is not None:
            settling.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await settling

    async def end(self) -> None:
        self.conversation.end()
        # Stopped first: a tick that fires during the teardown would otherwise
        # start a turn into a conversation that is already over.
        await self._initiative.stop()
        # Cancelled before announcing, so nothing from a reply can follow it.
        await self._cancel_turns()
        await self._cancel_settling()
        await self._channel.send_json({"type": "ended"})
        if self.mic is not None:
            await self.mic.stop()

    async def forget_utterance(self) -> None:
        """Everything known about the utterance in progress is void."""
        self._warmer.forget()
        await self._speculator.abandon()
        self._speculator.reset()

    async def close(self) -> None:
        """The socket is gone."""
        await self._initiative.stop()
        if self.mic is not None:
            await self.mic.stop(announce=False)
        # A turn or a guess outlives the browser that prompted it otherwise, and
        # goes on generating — and being billed for — a reply to a question
        # nobody is waiting for any more.
        await self._cancel_turns()
        await self._cancel_settling()
        await self.forget_utterance()
