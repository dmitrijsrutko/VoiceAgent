"""One exchange: the user's text in, the streamed reply and its speech out."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

from voice_agent.channel import Channel, audio_start
from voice_agent.conversation import Conversation
from voice_agent.errors import VoiceAgentError
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
) -> float | None:
    """Returns how much of the spoken reply is still to play when this returns —
    the window during which the browser stays muted and the user cannot be
    heard."""
    conversation.add_user(text)
    await channel.send_json({"type": "reply_start"})

    started = time.perf_counter()
    first_token_at: float | None = None
    produced: list[str] = []
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
                await channel.send_json({"type": "delta", "text": fragment})
    except VoiceAgentError as exc:
        # Fail closed: drop the user turn too, so a failed exchange never leaves
        # a dangling question in the context that the next call would resend.
        conversation.messages.pop()
        logger.warning("turn failed for session %s: %s", conversation.id, exc)
        await channel.send_json({"type": "error", "message": str(exc)})
        return None
    except asyncio.CancelledError:
        # Cancelled before a reply existed — the socket closed, or the user
        # ended the conversation. The same rule: no question without an answer.
        conversation.messages.pop()
        raise

    reply = "".join(produced)
    # Falls back to "now" when nothing streamed, so an empty reply reports its
    # whole duration as time-to-first-token rather than as zero of everything.
    generation_started = first_token_at if first_token_at is not None else time.perf_counter()
    conversation.add_assistant(reply)
    await channel.send_json(
        {
            **(report or {}),
            "type": "reply_end",
            "text": reply,
            "chars": len(reply),
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

    # A blank reply has nothing to say aloud. Streamed, it would produce no
    # chunks and so no audio at all — asking a provider for it only buys an
    # error or a billed request for silence.
    if speaker is not None and reply.strip():
        return await speak(channel, speaker, reply, started)
    return None


async def speak(channel: Channel, speaker: TTS, reply: str, turn_started: float) -> float | None:
    """Synthesize the whole reply, sending its audio as it is produced.

    Still deliberately after `reply_end`: the text goes in whole, so only the
    output streams. What the browser hears first is now the first chunk rather
    than the last one.

    Returns how much audio is still to play, which is less than its length:
    the browser starts playing the first chunk while the rest is being made.
    """
    synthesis_started = time.perf_counter()
    first_sent_at: float | None = None
    sent = chunks = 0
    try:
        async with closing(speaker.stream(reply)) as audio:
            async for chunk in audio:
                if first_sent_at is None:
                    # Announced on the first chunk rather than before the
                    # request, so a synthesis that fails outright looks exactly
                    # as it did when it was batched: an error, and no audio begun.
                    await channel.send_json(audio_start())
                    first_sent_at = time.perf_counter()
                await channel.send_bytes(chunk)
                sent += len(chunk)
                chunks += 1
    except VoiceAgentError as exc:
        # The reply itself is fine; only its voice failed. Degrade to text
        # rather than discarding a good answer — losing the words is a far
        # worse failure than losing the audio.
        logger.warning("synthesis failed: %s", exc)
        if first_sent_at is not None:
            # Audio already began, so close it: the browser plays what it has,
            # and the mute window below still covers it.
            await end_audio(channel, sent, chunks, synthesis_started, first_sent_at, turn_started)
        await channel.send_json({"type": "audio_error", "message": str(exc)})
        return remaining(sent, first_sent_at)

    if first_sent_at is None:
        return None
    await end_audio(channel, sent, chunks, synthesis_started, first_sent_at, turn_started)
    return remaining(sent, first_sent_at)


async def end_audio(
    channel: Channel,
    sent: int,
    chunks: int,
    synthesis_started: float,
    first_sent_at: float,
    turn_started: float,
) -> None:
    await channel.send_json(
        {
            "type": "audio_end",
            "bytes": sent,
            # Binary frames sent: one per provider chunk, so this is also what
            # the page's player queued.
            "chunks": chunks,
            "seconds": pcm_seconds(sent),
            # The provider's latency, separated from the pipeline's.
            "synthesis_first_byte_ms": elapsed_ms(synthesis_started, first_sent_at),
            "synthesis_ms": elapsed_ms(synthesis_started),
            # The number the whole project is judged on: send to first audio.
            "first_audio_ms": elapsed_ms(turn_started, first_sent_at),
        }
    )


def remaining(sent: int, first_sent_at: float | None) -> float | None:
    """Playback left, assuming it began when the first chunk was sent.

    Approximate in the safe direction: the browser starts a little after the
    send, so this slightly under-counts, and the browser's own `playback` hold
    covers the difference."""
    if first_sent_at is None:
        return None
    return max(0.0, pcm_seconds(sent) - (time.perf_counter() - first_sent_at))
