"""One exchange: the user's text in, the streamed reply and its speech out.

The reply is spoken while it is still being written. Every fragment goes two
ways at once — to the page as text, and to the synthesizer — so the voice starts
once the synthesizer has enough of the first sentence, not once the reasoning
engine has finished the last one.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

from voice_agent.channel import Channel, audio_start
from voice_agent.conversation import Conversation, Message
from voice_agent.errors import VoiceAgentError
from voice_agent.heard import Spoken
from voice_agent.llm import LLM
from voice_agent.llm.base import Usage
from voice_agent.timing import elapsed_ms
from voice_agent.tts import TTS
from voice_agent.tts.base import pcm_seconds

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def closing[T](stream: AsyncIterator[T]) -> AsyncIterator[AsyncIterator[T]]:
    """Close a provider's stream when the turn stops reading it, however it stops.

    Cancelling a turn does not close the generator it was reading: the
    cancellation usually lands in a socket write *between* fragments, which
    leaves the generator suspended at its `yield` — and the provider's HTTP
    stream open, and billed, until garbage collection gets round to it.
    """
    try:
        yield stream
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()


class Interruption:
    """Whether a turn being cancelled was talked over rather than torn down.

    The two end differently. A conversation that ends, or a socket that closes,
    drops a question left without its answer. A user who talks over the reply
    has asked the question and heard some of the answer, so both stay — the
    answer cut down, later, to what was heard.
    """

    def __init__(self) -> None:
        self.requested = False


async def run_turn(
    channel: Channel,
    conversation: Conversation,
    engine: LLM,
    speaker: TTS | None,
    system_prompt: str,
    text: str,
    fragments: AsyncIterator[str] | None = None,
    report: dict[str, object] | None = None,
    usage: Usage | None = None,
    voice: Spoken | None = None,
    interruption: Interruption | None = None,
) -> float | None:
    """Returns how much of the spoken reply is still to play when this returns.

    `voice` is filled with what the reply's speech says and when, so that
    whoever interrupts it can work out what was heard.
    """
    conversation.add_user(text)
    await channel.send_json({"type": "reply_start"})

    started = time.perf_counter()
    # Started before the first token, so that connecting to the synthesizer
    # happens while the reasoning engine is still thinking rather than after.
    speech = Speech(channel, speaker, started, voice) if speaker is not None else None
    produced: list[str] = []
    reply: Message | None = None
    try:
        first_token_at: float | None = None
        # A claimed speculation is already generating — possibly already finished.
        # Everything after this point is identical either way, which is the point:
        # a turn does not know whether its reply was guessed at.
        usage = usage if usage is not None else Usage()
        source = (
            fragments
            if fragments is not None
            else engine.stream(system_prompt, conversation.messages, usage)
        )
        try:
            async with closing(source) as fragments:
                async for fragment in fragments:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    produced.append(fragment)
                    if speech is not None:
                        speech.say(fragment)
                    await channel.send_json({"type": "delta", "text": fragment})
        except VoiceAgentError as exc:
            # Fail closed: drop the user turn too, so a failed exchange never leaves
            # a dangling question in the context that the next call would resend.
            # Part of the answer may already have been heard; it stops here, and
            # the audio that began is closed so the page stops waiting for it.
            conversation.messages.pop()
            if speech is not None:
                await speech.interrupt()
            logger.warning("turn failed for session %s: %s", conversation.id, exc)
            await channel.send_json({"type": "error", "message": str(exc)})
            return None

        if speech is not None:
            speech.finish()
        written = "".join(produced)
        # Falls back to "now" when nothing streamed, so an empty reply reports its
        # whole duration as time-to-first-token rather than as zero of everything.
        generation_started = first_token_at if first_token_at is not None else time.perf_counter()
        reply = conversation.add_assistant(written)
        if speech is not None:
            speech.voice.message = reply
        await channel.send_json(
            {
                **(report or {}),
                "type": "reply_end",
                "text": written,
                "chars": len(written),
                # Split at the first token, because the halves mean different
                # things. `ttft_ms` is dead air the user actually experiences and
                # is the number the latency budget targets; `generation_ms` is
                # throughput, which streaming already hides behind text appearing
                # on screen. A single "reply took N ms" would blur the one that
                # matters into the one that does not.
                "ttft_ms": elapsed_ms(started, first_token_at),
                "generation_ms": elapsed_ms(generation_started),
                # Events the provider sent, and what it says they cost. Not the same
                # number: a fragment is often one token but not by contract.
                "fragments": len(produced),
                "output_tokens": usage.output_tokens,
                "prompt_tokens": usage.prompt_tokens,
                "cached_tokens": usage.cached_tokens,
            }
        )
        return await speech.done() if speech is not None else None
    except asyncio.CancelledError:
        if interruption is None or not interruption.requested:
            # The socket closed, or the user ended the conversation. Cancelled
            # before a reply existed, the same rule as a failure: no question
            # without an answer. Nothing more is sent, so whatever ended the
            # turn is the last word.
            if reply is None:
                conversation.messages.pop()
            raise
        # Talked over. This cancellation was the interruption asking the turn to
        # stop, not to disappear, so it is absorbed here rather than propagated.
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        if reply is None:
            await _record_interrupted(channel, conversation, "".join(produced), speech)
        return None
    finally:
        # The one guarantee that the voice never outlives its turn, however the
        # turn ends: finished (a no-op), failed, or cancelled at any await —
        # including the `reply_end` write, which queues behind audio frames.
        if speech is not None:
            await speech.cancel()


async def _record_interrupted(
    channel: Channel, conversation: Conversation, written: str, speech: "Speech | None"
) -> None:
    """Keep what was written of a reply the user talked over.

    With a voice it is recorded whole and cut down once the browser says how
    much it played. Without one, what was on screen is what was received.
    """
    if written.strip():
        reply = conversation.add_assistant(written if speech is not None else written.rstrip())
        if speech is not None:
            speech.voice.message = reply
    await channel.send_json(
        {"type": "reply_end", "interrupted": True, "text": written, "chars": len(written)}
    )


class Speech:
    """One reply's voice, synthesized while its text is still arriving.

    Runs as its own task beside the reasoning engine's stream: text is handed
    in with `say` as it is written, and audio goes out as it is made, in
    whatever interleaving the two produce.
    """

    def __init__(
        self, channel: Channel, speaker: TTS, turn_started: float, voice: Spoken | None = None
    ) -> None:
        self.voice = voice if voice is not None else Spoken()
        self._channel = channel
        self._speaker = speaker
        self._turn_started = turn_started
        self._text: asyncio.Queue[str | None] = asyncio.Queue()
        self._first_text_at: float | None = None
        self._text_ended_at: float | None = None
        self._first_sent_at: float | None = None
        self._sent = 0
        self._chunks = 0
        self._audio_ended = False
        self._task = asyncio.create_task(self._run())

    def say(self, fragment: str) -> None:
        if self._first_text_at is None:
            self._first_text_at = time.perf_counter()
        self._text.put_nowait(fragment)

    def finish(self) -> None:
        """All of the text has been said."""
        self._text_ended_at = time.perf_counter()
        self._text.put_nowait(None)

    async def done(self) -> float | None:
        """Wait for the last chunk; returns how much audio is still to play."""
        await self._task
        return self._remaining()

    async def cancel(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def interrupt(self) -> None:
        """Stop speaking, and close the audio already begun."""
        await self.cancel()
        if self._first_sent_at is not None:
            await self._end_audio()

    async def _fragments(self) -> AsyncIterator[str]:
        while (fragment := await self._text.get()) is not None:
            yield fragment

    async def _run(self) -> None:
        try:
            async with closing(self._speaker.stream(self._fragments())) as audio:
                async for chunk in audio:
                    if self._first_sent_at is None:
                        # Announced on the first chunk rather than on connecting,
                        # so a synthesis that fails outright opens no stream,
                        # and a blank reply announces nothing at all.
                        await self._channel.send_json(audio_start())
                        self._first_sent_at = time.perf_counter()
                    # Recorded before the write: a chunk being written when the
                    # user interrupts may already be playing.
                    self.voice.add(chunk)
                    await self._channel.send_bytes(chunk.pcm)
                    self._sent += len(chunk.pcm)
                    self._chunks += 1
        except VoiceAgentError as exc:
            # The reply itself is fine; only its voice failed. Degrade to text
            # rather than discarding a good answer — losing the words is a far
            # worse failure than losing the audio. The text keeps streaming.
            logger.warning("synthesis failed: %s", exc)
            if self._first_sent_at is not None:
                # Audio already began, so close it: the browser plays what it
                # has, and its playback hold still covers it.
                await self._end_audio()
            await self._channel.send_json({"type": "audio_error", "message": str(exc)})
            return
        if self._first_sent_at is not None:
            await self._end_audio()

    async def _end_audio(self) -> None:
        assert self._first_sent_at is not None
        if self._audio_ended:
            # A synthesis that failed has already closed its audio; the reply
            # failing after it must not close it a second time.
            return
        self._audio_ended = True
        synthesis_started = self._first_text_at or self._first_sent_at
        await self._channel.send_json(
            {
                "type": "audio_end",
                "bytes": self._sent,
                # Binary frames sent: one per provider chunk, so this is also
                # what the page's player queued.
                "chunks": self._chunks,
                "seconds": pcm_seconds(self._sent),
                # From the first text handed over, so it now includes the
                # synthesizer waiting for enough of the sentence to speak.
                "synthesis_first_byte_ms": elapsed_ms(synthesis_started, self._first_sent_at),
                "synthesis_ms": elapsed_ms(synthesis_started),
                # The number the whole project is judged on: send to first audio.
                "first_audio_ms": elapsed_ms(self._turn_started, self._first_sent_at),
                # Whether the voice started before the reasoning engine finished
                # writing — the thing this chapter exists to make true.
                "audio_before_reply_end": self._text_ended_at is None
                or self._first_sent_at < self._text_ended_at,
            }
        )

    def _remaining(self) -> float | None:
        """Playback left, assuming it began when the first chunk was sent.

        Approximate in the safe direction: the browser starts a little after
        the send, and pauses if the text runs dry mid-reply, so this can only
        under-count — and the browser's own `playback` hold covers the
        difference."""
        if self._first_sent_at is None:
            return None
        return max(0.0, pcm_seconds(self._sent) - (time.perf_counter() - self._first_sent_at))
