"""Prefilling the reasoning engine on agreed-stable text while the user is
still speaking."""

import asyncio
import logging
import time
from collections.abc import Sequence

from voice_agent.conversation import Message
from voice_agent.errors import VoiceAgentError
from voice_agent.llm import LLM
from voice_agent.llm.base import Warmth
from voice_agent.timing import elapsed_ms

logger = logging.getLogger(__name__)

WARM_MIN_NEW_WORDS = 48
"""How much the agreed prefix must grow before warming again.

Measured on a real 22-word turn, warming on every growth fired **seven** calls
and the provider's cached-token count never moved off 1024: the utterance is
far too short to complete another 64-token cache block, so warms two through
seven were billed prefill that cached nothing. One block is roughly 48 words of
speech, so that is the threshold — long utterances still warm more than once,
short ones warm exactly once and keep the earliest, longest lead."""

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


class Warmer:
    """Warms the engine for the utterance in progress, and reports what it found.

    Reported once, on the committed transcript, rather than per warm: the
    interesting question is how much of the prompt was already paid for by the
    time the turn ended, not the shape of each individual call.
    """

    def __init__(self, engine: LLM, system: str) -> None:
        self._engine = engine
        self._system = system
        self._task: asyncio.Task[None] | None = None
        self._words = 0
        self.attempted = 0
        self.count = 0
        self.last: Warmth | None = None
        self.finished_at: float | None = None

    def on_growth(self, stable: str, history: Sequence[Message]) -> None:
        """Prefill on the agreed prefix, without blocking the transcript.

        Fire-and-forget on purpose: a warm that arrives late is worthless
        but a warm that delays a partial is actively harmful, and at most
        one is in flight because a second would only re-prefill what the
        first is already caching.
        """
        if not WARM_ON_STABLE or (self._task is not None and not self._task.done()):
            return
        words = len(stable.split())
        # `_words` alone decides this: 0 means nothing has been warmed for this
        # turn yet. Consulting the warm *count* as well would make the per-turn
        # reset redundant, and a reset nothing depends on is a reset that
        # quietly stops happening.
        if self._words and words - self._words < WARM_MIN_NEW_WORDS:
            return
        self._words = words
        # Counted separately from a warm that *landed*. Without this, a missing
        # warm line is ambiguous between "nothing was warmed" and "a warm was
        # paid for and arrived too late to help" — and those want opposite
        # responses.
        self.attempted += 1
        self._task = asyncio.create_task(self._warm([*history, Message("user", stable)]))

    async def _warm(self, messages: Sequence[Message]) -> None:
        try:
            warmth = await self._engine.warm(self._system, messages)
        except VoiceAgentError as exc:
            logger.info("warm failed, which costs only the warm: %s", exc)
            return
        self.count += 1
        self.last = warmth
        self.finished_at = time.perf_counter()

    def forget(self) -> None:
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
        if self._task is not None and not self._task.done():
            # Cancelling is what keeps it out of the next turn's books. A guard
            # inside the task cannot help: once `warm()` returns there is no
            # await before it records, so there is no point at which a late
            # check could run.
            self._task.cancel()
        self._task = None
        self._words = 0
        self.attempted = self.count = 0
        self.last = None
        self.finished_at = None

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
