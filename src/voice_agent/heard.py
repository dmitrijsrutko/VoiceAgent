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

PLAYBACK_GRACE_SECONDS = 10.0
"""How long after its own audio must have ended a voice is still treated as
possibly audible, when the browser has not said otherwise.

Covers playback starting later than the send and drifting behind it. Generous
on purpose: being wrong in this direction costs one late speculation, while
being wrong in the other costs the feature for the rest of the session."""


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

    def add(self, chunk: AudioChunk) -> list[float]:
        """Record a chunk about to be sent. Returns the end times it adds to
        the timeline, from the reply's first sample: what the page needs to
        light each word up as it is spoken."""
        if self.started_at is None:
            self.started_at = time.perf_counter()
        added: list[float] = []
        if chunk.alignment is not None:
            # Timed from the start of the chunk it came with, which is however
            # much audio was sent before it.
            offset = pcm_seconds(self.sent_bytes) * 1000
            # Nothing said earlier can still be sounding once this audio starts.
            # Measured: the first segment's timing runs about twice as long as
            # its own audio (14 characters "ending" at 1022 ms, with the next
            # segment starting at 499 ms), while every later segment fits.
            # Uncapped, it holds back every word after it until 1022 ms.
            self.ends_ms[:] = [min(end, offset) for end in self.ends_ms]
            added = [offset + end for end in chunk.alignment.ends_ms]
            self.chars.extend(chunk.alignment.chars)
            self.ends_ms.extend(added)
        self.sent_bytes += len(chunk.pcm)
        return added

    @property
    def audible(self) -> bool:
        """Possibly still coming out of the user's speaker.

        Bounded, because the browser's word is not always going to arrive. The
        only thing that clears `finished` is a `playback: false` message, and a
        closed tab or a dropped frame means it never comes — after which this
        stays true for the life of the session and everything gated on it goes
        quiet for good, silently. That wedged the initiative clock (measured:
        not one tick in 58 seconds) and it wedges speculation the same way.

        The server does not have to take the browser's word for it, because it
        knows how long the audio it sent lasts: nothing can still be sounding
        once that duration, plus a generous allowance for playback starting
        late, has passed since the first chunk went out. Believing the browser
        while it is talking and falling back on arithmetic when it stops is what
        makes this both accurate and terminating.
        """
        if self.started_at is None or self.finished or self.interrupted_at is not None:
            return False
        return time.perf_counter() - self.started_at <= self.sounds_for()

    def sounds_for(self) -> float:
        """An upper bound, in seconds from the first chunk, on when this voice
        must have stopped. Grows while audio is still being sent."""
        return pcm_seconds(self.sent_bytes) + PLAYBACK_GRACE_SECONDS

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
