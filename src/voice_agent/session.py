"""One connected conversation: what arrives from the user, and the turns it starts.

A turn runs in its own task, never on the coroutine that produced its text. A
spoken turn used to run *inside* the microphone's task and a typed one inside
the socket's receive loop, so for the length of a reply neither could read
anything — stopping the microphone waited for the whole answer, and after five
seconds cancelled it mid-sentence. Owning the turn here is also what makes it
cancellable, which is the thing interruption will need.
"""

import asyncio
import contextlib

from voice_agent.channel import Channel
from voice_agent.conversation import Conversation
from voice_agent.llm import LLM
from voice_agent.mic import Mic
from voice_agent.speculation import Speculation, Speculator
from voice_agent.stt import STT
from voice_agent.tts import TTS
from voice_agent.turn import run_turn
from voice_agent.warming import Warmer

EXIT_COMMANDS = frozenset({"exit", "quit", "bye", "goodbye"})
"""Typing or saying any of these ends the conversation. Matched on the whole
message, case- and punctuation-insensitively, so that "Bye!" ends it but
"goodbye is a strange word" does not."""


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
    ) -> None:
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
        self._turns: set[asyncio.Task[None]] = set()
        self.mic = (
            Mic(
                listener,
                channel,
                self.submit,
                on_partial=self._on_partial,
                on_session=self.forget_utterance,
                extra_report=self._warmer.report,
            )
            if listener is not None
            else None
        )

    async def _on_partial(self, stable: str, repeated: bool) -> None:
        history = self.conversation.messages
        if not repeated:
            # More words arrived, so whatever we were generating answers a
            # question that is still being asked. Cancelling now is what keeps
            # a wrong guess cheap.
            await self._speculator.abandon()
            self._warmer.on_growth(stable, history)
            return
        if not self._turns:
            # Not while a turn is running: its reply is not in the history yet,
            # and a guess adopted without it would answer the wrong
            # conversation. A warm has no such problem — it only prefills.
            self._speculator.on_settled(stable, history)

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
        if is_exit_command(text):
            # `exit` produces no reply, so a guess at it answers nothing. Left
            # alone it would run to completion unread and uncounted.
            await self._speculator.abandon()
            await self.end()
            return
        claimed = await self._speculator.claim(text)
        report = self._speculator.report()
        self._speculator.reset()
        task = asyncio.create_task(self._take_turn(text, claimed, report))
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    async def _take_turn(
        self, text: str, claimed: Speculation | None, report: dict[str, object]
    ) -> None:
        try:
            async with self._lock:
                with self.mic.busy() if self.mic is not None else contextlib.nullcontext():
                    seconds = await run_turn(
                        self._channel,
                        self.conversation,
                        self._engine,
                        self._speaker,
                        self._system_prompt,
                        text,
                        fragments=claimed.stream() if claimed is not None else None,
                        usage=claimed.usage if claimed is not None else None,
                        report=report,
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
            with contextlib.suppress(asyncio.CancelledError):
                await turn

    async def end(self) -> None:
        self.conversation.end()
        # Cancelled before announcing, so nothing from a reply can follow it.
        await self._cancel_turns()
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
        if self.mic is not None:
            await self.mic.stop(announce=False)
        # A turn or a guess outlives the browser that prompted it otherwise, and
        # goes on generating — and being billed for — a reply to a question
        # nobody is waiting for any more.
        await self._cancel_turns()
        await self.forget_utterance()
