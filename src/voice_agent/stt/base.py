"""The one interface every speech recognition backend implements."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Transcript:
    """One version of what the user has said so far.

    `is_final` is the whole point. A streaming recognizer emits two kinds of
    result: volatile hypotheses that it will rewrite as more audio arrives, and
    committed text that will not change. Only committed text is safe to act on
    — building a turn from a hypothesis means acting on words the user never
    said.
    """

    text: str
    is_final: bool


class STT(Protocol):
    """Audio in, transcripts out, for as long as the audio lasts.

    The audio argument is an async iterator rather than a `send()` method so
    that a listening session has the same shape as every other stream in this
    project, and so that ending the iterator is what ends the session.
    """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def languages(self) -> tuple[str, ...]:
        """Which languages this backend can transcribe, in the vendor's own
        code convention — AssemblyAI answers in two-letter codes, ElevenLabs in
        three-letter ones, and normalising them by hand would invent facts.

        On the protocol because it is not a detail of one vendor: a recognizer
        handed a language it does not have returns confident nonsense rather
        than an error, so what it can hear has to be answerable *before*
        somebody speaks. It reaches the user twice — as a line appended to the
        system prompt, so the agent stops promising to listen in languages it
        cannot, and on the page, so a person can see it before they open their
        mouth."""
        ...

    @property
    def sample_rate(self) -> int:
        """The rate the backend expects. The browser captures at this rate
        rather than resampling, so a mismatch is a configuration bug, not
        something to paper over with a converter."""
        ...

    def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]: ...


CHUNK_MS = 100
"""What a recognizer is sent at a time. The page posts 32 ms frames, sized for
the VAD; AssemblyAI closes the socket on chunks under 50 ms, and a chunk per
32 ms would triple the messages either way for no earlier word."""


async def batched(audio: AsyncIterator[bytes], sample_rate: int) -> AsyncIterator[bytes]:
    """Regroup PCM16 frames into `CHUNK_MS` chunks. A tail under half a chunk
    when the audio ends is dropped: under 50 ms, it is the one chunk the
    recognizer would refuse, and it cannot hold a word."""
    size = sample_rate * 2 * CHUNK_MS // 1000
    pending = bytearray()
    async for frame in audio:
        pending += frame
        while len(pending) >= size:
            yield bytes(pending[:size])
            del pending[:size]
    if len(pending) >= size // 2:
        yield bytes(pending)
