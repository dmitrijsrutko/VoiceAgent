"""The agent's opening line, synthesised once and cached on disk."""

import logging
from hashlib import sha256
from pathlib import Path

from voice_agent import timing
from voice_agent.channel import Channel, audio_start
from voice_agent.conversation import Conversation
from voice_agent.errors import VoiceAgentError
from voice_agent.heard import Spoken
from voice_agent.timing import elapsed_ms
from voice_agent.tts import TTS
from voice_agent.tts.base import BYTES_PER_SAMPLE, MEDIA_TYPE, AudioChunk, once, pcm_seconds

logger = logging.getLogger(__name__)

GREETING_CACHE = Path(".cache")


class Greeting:
    """The agent's opening line, synthesised once for the life of the process.

    Cached rather than re-synthesised per visitor because it never changes:
    that is zero synthesis cost and zero wait on every conversation after the
    first, and it is the cheapest optimisation available in a voice pipeline.
    """

    def __init__(self, text: str, speaker: TTS | None, cache_dir: Path | None = None) -> None:
        self.text = text.strip()
        self._speaker = speaker
        # Resolved now rather than bound as a default argument, so that a test
        # can point it somewhere disposable instead of at the working tree.
        self._cache_dir = GREETING_CACHE if cache_dir is None else cache_dir
        self._pcm: bytes | None = None
        self._synthesis_ms = 0
        self._attempted = False

    @property
    def _cache_file(self) -> Path:
        """Keyed by everything that changes the audio, so editing the greeting
        or switching voice produces a different file rather than a stale one."""
        assert self._speaker is not None
        # The format is part of the key: MP3 read back as PCM plays as noise.
        key = f"{self._speaker.provider}:{self._speaker.voice}:{MEDIA_TYPE}:{self.text}"
        return self._cache_dir / f"greeting-{sha256(key.encode()).hexdigest()[:16]}.pcm"

    def _load(self) -> bytes | None:
        try:
            pcm = self._cache_file.read_bytes()
        except OSError:
            return None
        # Raw PCM has no structure to fail on, so a damaged file would play as
        # a shorter greeting on every visit. An odd length is the one damage it
        # can reveal; the atomic write below is what prevents the rest.
        if not pcm or len(pcm) % BYTES_PER_SAMPLE:
            return None
        return pcm

    def _save(self, pcm: bytes) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            # Written aside and renamed into place: a process killed mid-write
            # leaves a stray temporary file, never a truncated greeting.
            partial = self._cache_file.with_suffix(".partial")
            partial.write_bytes(pcm)
            partial.replace(self._cache_file)
        except OSError as exc:  # a cache that cannot be written is not an error
            logger.info("could not cache the greeting: %s", exc)

    async def prepare(self) -> None:
        if not self.text or self._speaker is None or self._pcm is not None:
            return
        if self._attempted:
            # Tried once and failed. Retrying per visitor makes every page load
            # wait on a request already known to fail — which is exactly what
            # an exhausted quota looks like — for a greeting that will be text
            # either way. A restart is the retry.
            return
        self._attempted = True
        # Across restarts, not just within one process: the greeting is a fixed
        # string, and re-synthesising it on every `uv run voice-agent` bills for
        # bytes we already have. On a free plan that is a meaningful fraction of
        # a month's quota spent on a sentence that never changes.
        self._pcm = self._load()
        if self._pcm is not None:
            return
        started = timing.now()
        try:
            # Joined, not streamed: nobody is waiting on it yet, and a partial
            # greeting cached to disk would be replayed truncated forever.
            pcm = b"".join([chunk.pcm async for chunk in self._speaker.stream(once(self.text))])
        except VoiceAgentError as exc:
            # An agent that cannot greet must still be able to converse.
            logger.warning("could not prepare the greeting: %s", exc)
            return
        self._synthesis_ms = elapsed_ms(started)
        if not pcm:
            return
        self._pcm = pcm
        self._save(pcm)
        logger.info("greeting synthesised in %d ms and cached", self._synthesis_ms)

    async def deliver(self, channel: Channel, conversation: Conversation) -> Spoken | None:
        """Greet a conversation that has not started yet. Returns the greeting's
        voice, which the user can talk over like any other."""
        if not self.text:
            return None
        await self.prepare()  # only does anything if startup could not
        # Checked *after* the await, and with nothing awaited between here and
        # the append: two tabs opening the same link would otherwise both pass
        # an earlier check, both wait, and both greet.
        if conversation.messages or conversation.ended:
            return None
        message = conversation.add_assistant(self.text)
        conversation.opening = message
        await channel.send_json({"type": "greeting", "text": self.text})
        if self._pcm is None:
            return None
        # Untimed: cached as bare PCM, so what an interrupted greeting was heard
        # of is estimated. It is one short sentence.
        voice = Spoken(message=message)
        voice.add(AudioChunk(self._pcm))
        # The same start / frames / end shape as a live reply, so the browser
        # has one audio path rather than a special case for the greeting.
        await channel.send_json(audio_start())
        await channel.send_bytes(self._pcm)
        await channel.send_json(
            {
                "type": "audio_end",
                "bytes": len(self._pcm),
                "chunks": 1,
                "seconds": pcm_seconds(len(self._pcm)),
                "synthesis_ms": self._synthesis_ms,
                "cached": True,
            }
        )
        return voice
