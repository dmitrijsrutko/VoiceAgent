"""The one interface every speech synthesis backend implements."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

SAMPLE_RATE = 24_000
"""Hz. Chosen because both backends produce it natively — ElevenLabs as
`pcm_24000`, OpenAI as its only `pcm` rate — so neither needs resampling."""

BYTES_PER_SAMPLE = 2
"""16-bit signed little-endian, mono."""

MEDIA_TYPE = f"audio/pcm;rate={SAMPLE_RATE};encoding=s16le;channels=1"
"""Travels with the stream so the browser never has to assume a format. A
wrong rate does not fail, it plays — pitched up or down."""


def pcm_seconds(n_bytes: int) -> float:
    """How long `n_bytes` of this module's PCM takes to play."""
    return n_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)


@dataclass(frozen=True, slots=True)
class Alignment:
    """Which characters a stretch of audio voices, and when each one ends.

    `ends_ms[i]` is when the sound of `chars[i]` is over, in milliseconds from
    the start of the chunk this arrived with. It may reach past that chunk's
    own audio: ElevenLabs times a whole generated segment on its first message
    and sends the rest of that segment's audio untimed.
    """

    chars: str
    ends_ms: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """PCM in this module's format, and — where the backend can say — the text
    it voices. Timing is what makes "what did the user actually hear" answerable
    after an interruption; a backend without it leaves that to an estimate."""

    pcm: bytes
    alignment: Alignment | None = None


async def whole_samples(chunks: AsyncIterator[AudioChunk]) -> AsyncIterator[AudioChunk]:
    """Re-cut a provider's byte stream so that no chunk splits a sample.

    HTTP chunking knows nothing about 16-bit samples, and an odd-length chunk
    is ordinary. Played as it arrives, a split sample shifts every sample after
    it by one byte — the rest of the reply becomes full-scale noise. A trailing
    odd byte is held back and prepended to the next chunk instead; timing that
    arrives on a chunk too short to send rides on the next one.
    """
    carry = b""
    pending: Alignment | None = None
    async for chunk in chunks:
        data = carry + chunk.pcm
        pending = chunk.alignment or pending
        cut = len(data) - len(data) % BYTES_PER_SAMPLE
        carry = data[cut:]
        if cut:
            yield AudioChunk(data[:cut], pending)
            pending = None


@dataclass(frozen=True, slots=True)
class Voice:
    id: str
    name: str
    usable: bool
    """False for a voice the account can see but not synthesize with — the
    distinction that cost an afternoon in chapter 2."""


async def once(text: str) -> AsyncIterator[str]:
    """A text that is already whole, as the stream `TTS.stream` takes."""
    yield text


class TTS(Protocol):
    """A stream of text in, a stream of speech out.

    Both ends stream. Text is handed over as the reasoning engine writes it, in
    fragments of no particular shape — half a word, a comma, a sentence — and
    the backend decides when it has enough to say something aloud. Every chunk
    out holds PCM in the format above in whole samples, so a consumer may play
    each one the moment it arrives, and says which text it voices when the
    backend knows.

    A blank text is spoken as nothing: no chunks, and no error.
    """

    @property
    def provider(self) -> str: ...

    @property
    def voice(self) -> str: ...

    def stream(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        """Raises `ProviderError` — possibly after some chunks have already
        been yielded, which a consumer that has started playing must handle."""
        ...

    async def list_voices(self) -> list[Voice]:
        """The voices this account may actually use.

        Not a convenience: which voices a plan can use turned out to be neither
        stable nor inferable from documentation, and getting it wrong fails at
        synthesis time with a payment error rather than at startup. A backend
        has to be able to answer this about itself.
        """
        ...
