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


async def whole_samples(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Re-cut a provider's byte stream so that no chunk splits a sample.

    HTTP chunking knows nothing about 16-bit samples, and an odd-length chunk
    is ordinary. Played as it arrives, a split sample shifts every sample after
    it by one byte — the rest of the reply becomes full-scale noise. A trailing
    odd byte is held back and prepended to the next chunk instead.
    """
    carry = b""
    async for chunk in chunks:
        data = carry + chunk
        cut = len(data) - len(data) % BYTES_PER_SAMPLE
        carry = data[cut:]
        if cut:
            yield data[:cut]


@dataclass(frozen=True, slots=True)
class Voice:
    id: str
    name: str
    usable: bool
    """False for a voice the account can see but not synthesize with — the
    distinction that cost an afternoon in chapter 2."""


class TTS(Protocol):
    """Text in, a stream of speech out.

    The whole text still goes in at once: this interface streams the *output*
    only. Every chunk is PCM in the format above and holds whole samples, so a
    consumer may play each one the moment it arrives. Streaming the *input* —
    speaking a reply while it is still being written — is a later chapter.
    """

    @property
    def provider(self) -> str: ...

    @property
    def voice(self) -> str: ...

    def stream(self, text: str) -> AsyncIterator[bytes]:
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
