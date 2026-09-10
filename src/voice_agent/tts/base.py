"""The one interface every speech synthesis backend implements."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AudioClip:
    """A finished piece of speech, ready to play.

    `media_type` travels with the bytes because the browser needs it to decode
    them, and because the format is a per-provider choice this project intends
    to change later — PCM frames when playback becomes incremental.
    """

    data: bytes
    media_type: str
    seconds: float | None = None
    """How long this takes to play, when the backend can say.

    Not decoration: while a reply is playing the browser is muted, so the
    server must not count that stretch as the user being silent. Trusting the
    browser to report playback is not enough — a lost message there stops the
    microphone with a message blaming the user."""

    def __len__(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class Voice:
    id: str
    name: str
    usable: bool
    """False for a voice the account can see but not synthesize with — the
    distinction that cost an afternoon in chapter 2."""


class TTS(Protocol):
    """Text in, one complete clip out.

    Batched on purpose in this chapter, and the signature says so rather than
    hiding it behind an iterator that would yield exactly once. Streaming
    synthesis widens this to an async iterator of frames, and that widening is
    the point of the chapter that does it — the cost of *not* streaming should
    be visible until then.
    """

    @property
    def provider(self) -> str: ...

    @property
    def voice(self) -> str: ...

    async def synthesize(self, text: str) -> AudioClip: ...

    async def list_voices(self) -> list[Voice]:
        """The voices this account may actually use.

        Not a convenience: which voices a plan can use turned out to be neither
        stable nor inferable from documentation, and getting it wrong fails at
        synthesis time with a payment error rather than at startup. A backend
        has to be able to answer this about itself.
        """
        ...
