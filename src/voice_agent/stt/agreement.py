"""Turning volatile partial transcripts into a prefix that will not change.

Scribe emits exactly one committed transcript per utterance, at the end. There
is no stream of stable segments to act on early, so stability has to be
manufactured: **LocalAgreement** treats a prefix as settled once the recognizer
has produced it unchanged `agree_after` times in a row.

Two details come from the recognizer's real behaviour rather than from the
literature. Partials revise *punctuation and capitalisation* as often as they
revise words — in one seven-second utterance, three of seven transitions were
`'Prize Problems'` -> `'Prize problems'` and `'cryptography.'` ->
`'cryptography,'` — so comparison has to be normalised or agreement stalls at
the first comma the model reconsiders. And the prefix must be **append-only**:
a cache prefix that rewrites its own middle is not a prefix.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

TRIM = ".,!?;:—-…\"'"


def normalize(word: str) -> str:
    """What two spellings of the same word have in common."""
    return word.strip(TRIM).casefold()


def _same(left: Sequence[str], right: Sequence[str]) -> bool:
    return [normalize(w) for w in left] == [normalize(w) for w in right]


def same_words(left: str, right: str) -> bool:
    """Whether two transcripts say the same thing.

    Compared normalised, because a committed transcript adds the punctuation
    its partials were still arguing about — "what is the capital of Latvia"
    and "What is the capital of Latvia?" are the same question.
    """
    return _same(left.split(), right.split())


@dataclass
class StablePrefix:
    agree_after: int = 2
    """How many successive partials must contain a prefix before it counts.

    2 is the standard setting. Partials arrive about once a second from this
    recognizer, so this costs roughly a second of lag — which the measured
    0.3-1.0 s head start before the commit can still absorb."""

    _recent: list[list[str]] = field(default_factory=list, repr=False)
    _stable: list[str] = field(default_factory=list, repr=False)
    repeated: bool = False
    """Whether the last partial added nothing to the one before it.

    The closest thing to a turn-completion signal available without building a
    turn detector: the recognizer has stopped finding new words, so the speaker
    has probably stopped producing them. In captured traces it fires 0.3-1.0 s
    before the commit. It is a guess, and everything acting on it must be able
    to be wrong cheaply."""

    contradictions: int = 0
    """Times agreement produced something that disagreed with already-stable
    text. Should be zero; counted rather than asserted, because the cost of
    being wrong here is a wasted guess rather than a broken turn."""

    @property
    def text(self) -> str:
        return " ".join(self._stable)

    def update(self, partial: str) -> str | None:
        """Feed one partial. Returns the stable prefix if it grew, else None."""
        words = partial.split()
        self.repeated = bool(self._recent) and _same(words, self._recent[-1])
        self._recent.append(words)
        if len(self._recent) > self.agree_after:
            self._recent.pop(0)
        if len(self._recent) < self.agree_after:
            return None

        agreed = self._agreed()
        if len(agreed) <= len(self._stable):
            return None
        if not self._continues(agreed):
            self.contradictions += 1
            return None

        # Keep the spelling each word had when it settled, and only append.
        self._stable.extend(agreed[len(self._stable) :])
        return self.text

    def holds_for(self, committed: str) -> bool:
        """Did the committed transcript actually start with what we called
        stable? The honest test of whether agreement was worth trusting."""
        final = [normalize(w) for w in committed.split()]
        stable = [normalize(w) for w in self._stable]
        return final[: len(stable)] == stable

    def reset(self) -> None:
        self._recent.clear()
        self._stable.clear()

    def _agreed(self) -> list[str]:
        agreed: list[str] = []
        for column in zip(*self._recent, strict=False):
            if len({normalize(word) for word in column}) != 1:
                break
            agreed.append(column[-1])
        return agreed

    def _continues(self, agreed: list[str]) -> bool:
        return all(
            normalize(settled) == normalize(new)
            for settled, new in zip(self._stable, agreed, strict=False)
        )
