"""What a conversation reacts to: one type per kind of input.

Every input a turn-taking decision depends on arrives as one of these, on one
inbox, handled in order by one task (`Session`). The recognizer, the voice
detector, the browser and the clocks only *post*; none of them calls into the
decisions, so no two decisions interleave at an `await`.
"""

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Partial:
    """The recognizer's latest hypothesis of the utterance in progress."""

    text: str
    stable: str
    """The prefix partials have agreed on so far (LocalAgreement)."""
    repeated: bool
    """Nothing new since the last partial: a sign the user has stopped."""


@dataclass(frozen=True, slots=True)
class Final:
    """A committed transcript: a spoken turn, or a piece of one."""

    text: str


@dataclass(frozen=True, slots=True)
class NewSession:
    """A recognizer session began: whatever was known of an utterance is void."""


@dataclass(frozen=True, slots=True)
class FloorChanged:
    """The voice detector's floor changed, or listening stopped (`stopped`)."""

    state: str


@dataclass(frozen=True, slots=True)
class Playback:
    """The browser started or stopped playing the agent's voice."""

    active: bool


@dataclass(frozen=True, slots=True)
class Typed:
    text: str


@dataclass(frozen=True, slots=True)
class Speak:
    """The clock decided on a line to say into a silence."""

    line: str
    rung: int


@dataclass(frozen=True, slots=True)
class HoldOver:
    """A held fragment waited long enough; `hold` says which hold, so a timer
    that fires after the hold was released or replaced is ignored."""

    hold: int


@dataclass(frozen=True, slots=True)
class ResumeDue:
    """Time to decide whether the words that cut the agent off were anybody."""

    submits: int
    heard: str
    token: int


@dataclass(frozen=True, slots=True)
class End:
    """End the conversation; `done` resolves once it has."""

    reason: str
    done: "asyncio.Future[None]"


MicEvent = Partial | Final | NewSession | FloorChanged
"""What the microphone posts."""

Event = MicEvent | Playback | Typed | Speak | HoldOver | ResumeDue | End
