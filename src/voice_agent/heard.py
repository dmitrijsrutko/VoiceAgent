"""What the user actually heard of the agent's voice.

An interrupted reply is recorded as the part that was *heard*, not the part
that was written. The reasoning engine writes far faster than the voice speaks,
so by the time someone talks over the agent the whole reply usually exists —
and a history holding all of it has the agent reasoning, every turn after,
about sentences nobody ever heard. That is a correctness bug disguised as an
audio one.

Two halves of the answer live in two places. Only the browser knows how much
audio it played; only the server knows which characters that audio was. The
browser reports a duration and this module turns it into words.
"""

import time
from dataclasses import dataclass, field

from voice_agent.conversation import Message
from voice_agent.tts.base import AudioChunk, pcm_seconds


@dataclass(slots=True)
class Spoken:
    """One stretch of the agent's voice: what it says, and when each part ends.

    Filled as audio is sent. `chars` and `ends_ms` are the timeline — each
    voiced character and the moment its sound is over, in milliseconds from
    the first sample — and are empty for a voice that reports no timing.
    """

    message: Message | None = None
    """The assistant message this voices, once the conversation holds one."""
    chars: list[str] = field(default_factory=list)
    ends_ms: list[float] = field(default_factory=list)
    sent_bytes: int = 0
    started_at: float | None = None
    """When its first audio was sent. Nothing is audible before that."""
    playing: bool = False
    """The browser has said it is audible."""
    finished: bool = False
    """The browser has said it stopped: played to the end, or cut off."""
    interrupted_at: float | None = None

    def add(self, chunk: AudioChunk) -> None:
        if self.started_at is None:
            self.started_at = time.perf_counter()
        if chunk.alignment is not None:
            # Timed from the start of the chunk it came with, which is however
            # much audio was sent before it.
            offset = pcm_seconds(self.sent_bytes) * 1000
            self.chars.extend(chunk.alignment.chars)
            self.ends_ms.extend(offset + end for end in chunk.alignment.ends_ms)
        self.sent_bytes += len(chunk.pcm)

    @property
    def audible(self) -> bool:
        """Possibly still coming out of the user's speaker."""
        return self.started_at is not None and not self.finished and self.interrupted_at is None

    def playback(self, active: bool) -> None:
        # A stop is only this voice's once it has been heard to start: the
        # browser reports the end of the reply it replaced first.
        if active:
            self.playing = True
        elif self.playing:
            self.finished = True

    def estimate_played_ms(self) -> float:
        """How far playback got, assuming it started when the first audio was
        sent — for when the browser does not say."""
        if self.started_at is None:
            return 0.0
        until = self.interrupted_at or time.perf_counter()
        return min((until - self.started_at) * 1000, pcm_seconds(self.sent_bytes) * 1000)

    def heard(self, played_ms: float) -> str:
        """The text whose sound had ended `played_ms` into playback, cut back to
        the last whole word. A word the user heard half of is not one they heard."""
        if self.ends_ms:
            full = "".join(self.chars)
            count = next(
                (i for i, end in enumerate(self.ends_ms) if end > played_ms), len(self.ends_ms)
            )
        else:
            # No timing: assume the text is spread evenly over the audio. Crude —
            # speaking rate is not constant, so over a long reply this can be
            # several words out — and stated as such.
            full = self.message.content if self.message is not None else ""
            total_ms = pcm_seconds(self.sent_bytes) * 1000
            fraction = min(1.0, played_ms / total_ms) if total_ms else 0.0
            count = round(len(full) * fraction)
        said = full[:count]
        # Judged against what was *written* where the voice matches it: speech
        # can stop mid-word — a generated segment ended "The Millennium Priz" —
        # and the voiced characters alone would call that word finished.
        written = self.message.content if self.message is not None else ""
        reference = written if written.startswith(said) else full
        cut_mid_word = len(said) < len(reference) and not reference[len(said)].isspace()
        if cut_mid_word and not said[-1:].isspace():
            said = said[: max(said.rfind(" "), 0)]
        return said.rstrip()


def truncated(content: str, said: str) -> str | None:
    """What an interrupted reply becomes in the conversation: unchanged if it
    was heard in full, the heard part if not, nothing at all if no word of it
    was heard.

    No marker. "… [interrupted]" appended here was tried, and seen live: after
    two interruptions the model began ending its own replies with it — out
    loud. A reply's text is the model's own voice, and it imitates it. The
    system prompt says what a reply that stops mid-sentence means instead.
    """
    if said.strip() == content.strip():
        return content
    if not said.strip():
        return None
    return said.rstrip()
