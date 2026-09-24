"""What the user actually heard of the agent's voice.

An interrupted reply is recorded as the part that was *heard*, not the part
that was written: the model writes far faster than the voice speaks, and a
history holding unheard sentences has the agent reasoning about them.

Two halves of the answer live in two places. Only the browser knows how much
audio it played; only the server knows which characters that audio was. The
browser reports a duration and this module turns it into words.
"""

import re
from dataclasses import dataclass, field

from voice_agent import timing
from voice_agent.conversation import Message
from voice_agent.tts.base import AudioChunk, pcm_seconds

PLAYBACK_GRACE_SECONDS = 10.0
"""How long after its audio must have ended a voice still counts as possibly
audible, when the browser has not said otherwise. Generous: too long costs a
late speculation, too short costs correctness."""


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
            self.started_at = timing.now()
        added: list[float] = []
        if chunk.alignment is not None:
            # Timed from the start of the chunk it came with, which is however
            # much audio was sent before it.
            offset = pcm_seconds(self.sent_bytes) * 1000
            # Nothing earlier can still be sounding once this audio starts. The
            # first segment's timing runs long (measured: ~2x its own audio).
            self.ends_ms[:] = [min(end, offset) for end in self.ends_ms]
            added = [offset + end for end in chunk.alignment.ends_ms]
            self.chars.extend(chunk.alignment.chars)
            self.ends_ms.extend(added)
        self.sent_bytes += len(chunk.pcm)
        return added

    @property
    def text(self) -> str:
        """What this voice says: the written reply once there is one, else the
        characters voiced so far (the greeting)."""
        return self.message.content if self.message is not None else "".join(self.chars)

    @property
    def audible(self) -> bool:
        """Possibly still coming out of the user's speaker.

        The browser's word while it gives it; otherwise bounded by the duration
        of the audio sent, so a lost `playback: false` (a closed tab) cannot
        leave this true forever and wedge everything gated on it.
        """
        if self.started_at is None or self.finished or self.interrupted_at is not None:
            return False
        return timing.now() - self.started_at <= self.sounds_for()

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
        until = self.interrupted_at or timing.now()
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
            # No timing: assume the text is spread evenly over the audio (crude).
            full = self.message.content if self.message is not None else ""
            total_ms = pcm_seconds(self.sent_bytes) * 1000
            fraction = min(1.0, played_ms / total_ms) if total_ms else 0.0
            count = round(len(full) * fraction)
        said = full[:count]
        # Against the written text where it matches: speech can stop mid-word
        # ("The Millennium Priz"), which the voiced characters alone would miss.
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

    No "[interrupted]" marker: the model imitates its own history, and began
    saying it aloud.
    """
    if said.strip() == content.strip():
        return content
    if not said.strip():
        return None
    return said.rstrip()


SENTENCE_END = re.compile(r"[.!?…](?=\s)")


def resume_from(written: str, said: str) -> str:
    """What to say after an interruption nobody finished: the reply from the
    start of the sentence it was cut in. Empty when all of it was heard.

    From the sentence, not the word: "Отлично." heard, then "Деревня — это
    контент…" said whole reads as the agent carrying on; "…это контент" cut
    in mid-phrase reads as a glitch."""
    if len(said.strip()) >= len(written.strip()):
        return ""
    # Over the whole text, not just the heard part: a full stop exactly at the
    # cut ("Отлично.") is only a sentence end once the space after it is seen.
    ends = [m.end() for m in SENTENCE_END.finditer(written) if m.end() <= len(said)]
    return written[ends[-1] if ends else 0 :].strip()
