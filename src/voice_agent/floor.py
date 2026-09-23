"""Who holds the floor: the user's speech and silences, as named states.

Built from VAD probabilities alone, one per 32 ms window, and timed in *audio*
milliseconds rather than by the clock, so the same audio always gives the same
states. The states are the vocabulary a later chapter's interjections will be
timed against:

    yielded ──speech──▶ speaking ──200 ms quiet──▶ micro_pause
                           ▲                            │ 600 ms
                           └────────speech──────── pause ──1.5 s──▶ yielded
"""

from dataclasses import dataclass
from typing import Literal

State = Literal["speaking", "micro_pause", "pause", "yielded"]

SPEECH_ON = 0.5
"""Probability at which a window counts as speech: Silero's own default."""

SPEECH_OFF = 0.35
"""Below this, speech has stopped. The gap to `SPEECH_ON` is hysteresis — a
syllable that dips to 0.4 mid-word is not a pause. Silero's `threshold - 0.15`."""

ONSET_WINDOWS = 2
"""Consecutive speech windows (64 ms) before the user is taken to be speaking,
so a single click or cough does not claim the floor."""

MICRO_PAUSE_MS = 200
"""A gap between phrases, or a breath. Shorter gaps are inside words (stop
consonants run to ~100 ms); this is where a backchannel could fit."""

PAUSE_MS = 600
"""Long enough that a listener starts taking it as a place to come in: the upper
end of the 200-500 ms gap people leave between turns."""

YIELD_MS = 1500
"""The floor is given up. The same as both recognizers' default end-of-turn
silence, so the two can be compared on the same pause."""

_AFTER_SPEECH: tuple[tuple[int, State], ...] = (
    (MICRO_PAUSE_MS, "micro_pause"),
    (PAUSE_MS, "pause"),
    (YIELD_MS, "yielded"),
)


@dataclass(frozen=True, slots=True)
class Transition:
    """A change of state. `at_ms` is when it began in the audio — the first
    speech window, or the first silent one — and `detected_ms` when it could be
    told, which is later by construction: a pause is only a pause once it has
    lasted."""

    state: State
    at_ms: int
    detected_ms: int

    @property
    def lag_ms(self) -> int:
        return self.detected_ms - self.at_ms


class Floor:
    """One conversation's floor, fed one probability per window."""

    def __init__(self, window_ms: int) -> None:
        self.window_ms = window_ms
        self.state: State = "yielded"
        """Before anyone speaks, the floor is not the user's."""
        self._now = 0
        self._quiet_from: int | None = 0
        self._onset_from: int | None = None
        self._onset_windows = 0

    def push(self, probability: float) -> list[Transition]:
        start, self._now = self._now, self._now + self.window_ms
        if self.state == "speaking":
            return self._while_speaking(probability, start)
        return self._while_quiet(probability, start)

    def _while_speaking(self, probability: float, start: int) -> list[Transition]:
        if probability >= SPEECH_OFF:
            self._quiet_from = None
            return []
        if self._quiet_from is None:
            self._quiet_from = start
        return self._silence_grew()

    def _while_quiet(self, probability: float, start: int) -> list[Transition]:
        if probability < SPEECH_ON:
            self._onset_from, self._onset_windows = None, 0
            return self._silence_grew()
        if self._onset_from is None:
            self._onset_from = start
        self._onset_windows += 1
        if self._onset_windows < ONSET_WINDOWS:
            return []
        began = self._onset_from
        self._onset_from, self._onset_windows, self._quiet_from = None, 0, None
        self.state = "speaking"
        return [Transition("speaking", began, self._now)]

    def _silence_grew(self) -> list[Transition]:
        assert self._quiet_from is not None
        quiet = self._now - self._quiet_from
        changes: list[Transition] = []
        for threshold, state in _AFTER_SPEECH:
            if quiet >= threshold and self._rank(state) > self._rank(self.state):
                self.state = state
                changes.append(Transition(state, self._quiet_from, self._now))
        return changes

    @staticmethod
    def _rank(state: State) -> int:
        return ("speaking", "micro_pause", "pause", "yielded").index(state)
