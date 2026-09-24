"""Starting the reply before the user has finished asking for it.

The recognizer's commit arrives up to a second after the speaker actually
stopped, and the reasoning engine then takes most of another second to produce
its first token. Both of those are dead air. A speculation spends that dead air
generating the reply that is *probably* about to be needed, so that when the
turn really ends the answer already exists.

It is a bet, and the point of the design is that losing it is cheap: the
generation is cancelled the instant the recognizer finds another word, so a
wrong guess costs only what it managed to produce in the meantime rather than
a whole reply. It must never have side effects, which holds while the agent
has no tools.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence

from voice_agent import timing
from voice_agent.conversation import Message
from voice_agent.errors import VoiceAgentError
from voice_agent.llm.base import LLM, Usage
from voice_agent.stt.agreement import same_words
from voice_agent.timing import elapsed_ms

logger = logging.getLogger(__name__)


class Speculation:
    """One in-flight guess at the reply, and the fragments it has produced."""

    def __init__(self, engine: LLM, system: str, history: Sequence[Message], text: str) -> None:
        self.text = text
        self.chars = 0
        self.usage = Usage()
        """Filled in as the guess finishes; a turn that adopts it reports this."""
        self.started_at = timing.now()
        self._fragments: asyncio.Queue[str | None] = asyncio.Queue()
        self._failure: VoiceAgentError | None = None
        self._task = asyncio.create_task(
            self._generate(engine, system, [*history, Message("user", text)])
        )

    async def _generate(self, engine: LLM, system: str, messages: Sequence[Message]) -> None:
        try:
            async for fragment in engine.stream(system, messages, self.usage):
                self.chars += len(fragment)
                self._fragments.put_nowait(fragment)
        except VoiceAgentError as exc:
            # Held rather than raised: nobody is awaiting this task, and a
            # speculation that fails must fail the way a wrong one does —
            # quietly, leaving the real turn to do the work properly.
            self._failure = exc
        finally:
            self._fragments.put_nowait(None)

    def answers(self, committed: str) -> bool:
        """Whether the turn that actually arrived is the one this guessed at.

        Compared normalised, because the commit adds the punctuation its own
        partials were still arguing about. The model saw the question without
        a question mark; that is the same question.
        """
        return self._failure is None and same_words(self.text, committed)

    async def stream(self) -> AsyncIterator[str]:
        """Everything generated so far, then the rest as it arrives."""
        while (fragment := await self._fragments.get()) is not None:
            yield fragment
        if self._failure is not None:
            raise self._failure

    async def abandon(self) -> int:
        """Stop generating. Returns the characters the guess cost."""
        # Cancelling the task is enough to release the provider's stream: the
        # task is always suspended *inside* the generator's own await, so the
        # cancellation is delivered there and its cleanup runs.
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        # A fire-and-forget task's exception is nobody's by default. Provider
        # failures are already held as `_failure`; anything reaching here is a
        # defect, and must not pass silently.
        # `.exception()` itself raises on a cancelled task, hence the guard.
        finished = self._task.done() and not self._task.cancelled()
        if finished and (failure := self._task.exception()) is not None:
            logger.warning("speculation failed unexpectedly: %r", failure)
        return self.chars


class Speculator:
    """The guess for the utterance in progress, and what guessing cost.

    Reported rather than assumed: a wasted guess is a real bill, and only a
    count on screen makes it visible.
    """

    def __init__(self, engine: LLM, system: str) -> None:
        self._engine = engine
        self._system = system
        self._guess: Speculation | None = None
        self.discarded = 0
        self.wasted_chars = 0
        self.adopted = False
        self.lead_ms = 0

    def on_settled(self, stable: str, history: Sequence[Message]) -> None:
        """The recognizer found nothing new: bet that the speaker has stopped."""
        if self._guess is None:
            self._guess = Speculation(self._engine, self._system, history, stable)

    async def abandon(self) -> None:
        """Drop a guess the recognizer has just proved premature."""
        if self._guess is None:
            return
        stale, self._guess = self._guess, None
        self._discard(await stale.abandon())

    async def claim(self, committed: str) -> Speculation | None:
        """Keep the guess if the turn that arrived is the one it answered."""
        if self._guess is None:
            return None
        candidate, self._guess = self._guess, None
        if not candidate.answers(committed):
            self._discard(await candidate.abandon())
            return None
        self.adopted = True
        self.lead_ms = elapsed_ms(candidate.started_at)
        return candidate

    def _discard(self, chars: int) -> None:
        self.discarded += 1
        self.wasted_chars += chars

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
