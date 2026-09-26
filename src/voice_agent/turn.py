"""One exchange: the user's text in, the streamed reply and its speech out.

The reply is spoken while it is still being written. Every fragment goes two
ways at once — to the page as text, and to the synthesizer — so the voice starts
once the synthesizer has enough of the first sentence, not once the reasoning
engine has finished the last one.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from voice_agent import timing
from voice_agent.channel import Channel, audio_start
from voice_agent.conversation import Conversation, Message
from voice_agent.decline import guard
from voice_agent.errors import VoiceAgentError
from voice_agent.heard import Spoken
from voice_agent.llm import LLM
from voice_agent.llm.base import Usage
from voice_agent.streams import closing
from voice_agent.timing import elapsed_ms
from voice_agent.tts import TTS
from voice_agent.tts.base import pcm_seconds

logger = logging.getLogger(__name__)

FELL_BEHIND_MS = 500
"""Synthesis this far behind playback is logged: the listener heard a pause."""

SLOW_FIRST_TOKEN_MS = 3000
"""A first token this late is logged (typical TTFT is ~0.5-1.2 s)."""


@dataclass(frozen=True, slots=True)
class Context:
    """What every turn of one conversation is run with."""

    channel: Channel
    conversation: Conversation
    engine: LLM
    speaker: TTS | None
    system_prompt: str


@dataclass(eq=False, slots=True)
class Turn:
    """One turn, from the moment it is submitted.

    An interruption treats a turn by where it is: one already running is
    cancelled and keeps what it wrote; one still waiting has not begun, so
    there is only a question to record.
    """

    text: str | None
    """What the user said. `None` for a turn the agent started itself."""
    said: str | None = None
    """The line an unprompted turn already decided to say."""
    after: asyncio.Task[None] | None = None
    """The cut of the reply this turn interrupted, which it waits for."""
    voice: Spoken = field(default_factory=Spoken)
    """What the reply's speech says and when, to work out what was heard."""
    interrupted: bool = False
    """Talked over rather than torn down. A torn-down turn drops a question left
    without an answer; a talked-over one keeps both, the answer cut down to
    what was heard."""
    started: bool = False


async def run_turn(
    ctx: Context,
    turn: Turn,
    fragments: AsyncIterator[str] | None = None,
    usage: Usage | None = None,
    report: dict[str, object] | None = None,
) -> float | None:
    """Stream one reply to the page and the synthesizer. Returns how much of the
    spoken reply is still to play.

    `fragments` is the reply when it was not generated here — a claimed guess,
    or an unprompted line — and is otherwise asked of the engine.
    """
    channel, conversation, speaker = ctx.channel, ctx.conversation, ctx.speaker
    text, voice = turn.text, turn.voice
    question = conversation.add_user(text) if text is not None else None
    await channel.send_json({"type": "reply_start"})

    started = timing.now()
    # Started before the first token, so that connecting to the synthesizer
    # happens while the reasoning engine is still thinking rather than after.
    speech = Speech(channel, speaker, started, voice) if speaker is not None else None
    produced: list[str] = []
    reply: Message | None = None
    try:
        first_token_at: float | None = None
        usage = usage if usage is not None else Usage()

        def ask() -> AsyncIterator[str]:
            return ctx.engine.stream(ctx.system_prompt, conversation.context, usage)

        # Guarded whatever the source; a retry always asks the engine, since
        # re-running a guess would only produce the guess again.
        source = guard(fragments if fragments is not None else ask(), ask)
        try:
            async with closing(source) as fragments:
                async for fragment in fragments:
                    if first_token_at is None:
                        first_token_at = timing.now()
                    produced.append(fragment)
                    if speech is not None:
                        speech.say(fragment)
                    await channel.send_json({"type": "delta", "text": fragment})
        except VoiceAgentError as exc:
            # Fail closed: drop the question too, so no call resends a dangling
            # one; close any audio already begun so the page stops waiting.
            if question is not None:
                conversation.replace(question, None)
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
        generation_started = first_token_at if first_token_at is not None else timing.now()
        reply = conversation.add_assistant(written)
        if speech is not None:
            speech.voice.message = reply
        ttft_ms = elapsed_ms(started, first_token_at)
        await channel.send_json(
            {
                **(report or {}),
                **reply_report(written, len(produced), ttft_ms, generation_started, usage),
            }
        )
        if ttft_ms >= SLOW_FIRST_TOKEN_MS:
            # On the terminal too, since the page's notes are gone with the tab.
            logger.warning(
                "slow first token for session %s: %d ms (accepted at %s ms, %d attempt(s))",
                conversation.id,
                ttft_ms,
                usage.accepted_ms,
                usage.attempts,
            )
        return await speech.done() if speech is not None else None
    except asyncio.CancelledError:
        if not turn.interrupted:
            # The socket closed, or the user ended the conversation. Cancelled
            # before a reply existed, the same rule as a failure: no question
            # without an answer. Nothing more is sent, so whatever ended the
            # turn is the last word.
            if reply is None and question is not None:
                conversation.replace(question, None)
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


NORMAL_ENDS = frozenset({None, "stop", "end_turn"})
"""The reasons a reply ends when nothing went wrong: DeepSeek's and Anthropic's."""


def reply_report(
    written: str, fragments: int, ttft_ms: int, generation_started: float, usage: Usage
) -> dict[str, object]:
    """The `reply_end` frame of a reply that finished."""
    return {
        "type": "reply_end",
        "text": written,
        "chars": len(written),
        # Split at the first token: `ttft_ms` is dead air the user hears and
        # the number the latency budget targets; `generation_ms` is throughput,
        # which streaming hides behind text already on screen.
        "ttft_ms": ttft_ms,
        "generation_ms": elapsed_ms(generation_started),
        # A fragment is often one token, but not by contract.
        "fragments": fragments,
        "output_tokens": usage.output_tokens,
        "prompt_tokens": usage.prompt_tokens,
        "cached_tokens": usage.cached_tokens,
        # Set only when a connection had to be opened first.
        "connect_ms": usage.connect_ms,
        # Splits a slow first token: late accept = network or refusal; quick
        # accept then a long wait = the provider queueing.
        "accepted_ms": usage.accepted_ms,
        "attempts": usage.attempts,
        # Only an unusual end: a reply cut by `length` or a filter still has
        # text, and would otherwise read as a whole answer.
        "finish": None if usage.finish_reason in NORMAL_ENDS else usage.finish_reason,
    }


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
        self._last_chunk_at: float | None = None
        self._late_ms = 0
        """The most a chunk arrived after the audio before it would have
        finished playing: a pause the listener heard, not one in the voice."""
        self._late_after = ""
        self._audio_ended = False
        self._task = asyncio.create_task(self._run())

    def say(self, fragment: str) -> None:
        if self._first_text_at is None:
            self._first_text_at = timing.now()
        self._text.put_nowait(fragment)

    def finish(self) -> None:
        """All of the text has been said."""
        self._text_ended_at = timing.now()
        self._text.put_nowait(None)

    async def done(self) -> float | None:
        """Wait for the last chunk; returns how much audio is still to play."""
        await self._task
        return self._remaining()

    async def cancel(self) -> None:
        if not self._task.done() and self._last_chunk_at is not None and not self._audio_ended:
            # A stalled synthesis leaves no `audio_end` behind, so this is the
            # only trace of where it stopped.
            logger.warning(
                "synthesis unfinished when its turn ended: %.1f s since the last chunk, after %r",
                timing.now() - self._last_chunk_at,
                self._voiced_tail(),
            )
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
                    self._note_lateness()
                    if self._first_sent_at is None:
                        # Announced on the first chunk rather than on connecting,
                        # so a synthesis that fails outright opens no stream,
                        # and a blank reply announces nothing at all.
                        await self._channel.send_json(audio_start())
                        self._first_sent_at = timing.now()
                    # Recorded before the write: a chunk being written when the
                    # user interrupts may already be playing.
                    if ends_ms := self.voice.add(chunk):
                        # Ahead of the audio they time, so the page never plays
                        # a word it cannot yet place in the text.
                        await self._channel.send_json(
                            {
                                "type": "marks",
                                # Where this audio starts: earlier marks are capped
                                # there on the page, as in the server's own timeline.
                                "from_ms": round(pcm_seconds(self._sent) * 1000),
                                "ends_ms": [round(end) for end in ends_ms],
                            }
                        )
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
                # Whether the voice started before the reply was fully written.
                "audio_before_reply_end": self._text_ended_at is None
                or self._first_sent_at < self._text_ended_at,
                "late_ms": self._late_ms,
                "late_after": self._late_after,
            }
        )
        if self._late_ms >= FELL_BEHIND_MS:
            logger.warning(
                "synthesis fell %d ms behind playback after %r", self._late_ms, self._late_after
            )

    def _note_lateness(self) -> None:
        """How late this chunk is against playback, assuming playback began
        when the first chunk was sent (the browser starts a little after, so
        this errs towards zero)."""
        now = timing.now()
        self._last_chunk_at = now
        if self._first_sent_at is None:
            return
        late_ms = round((now - self._first_sent_at - pcm_seconds(self._sent)) * 1000)
        if late_ms > self._late_ms:
            self._late_ms = late_ms
            self._late_after = self._voiced_tail()

    def _voiced_tail(self) -> str:
        """The last words voiced so far, to place a pause in the text."""
        return "".join(self.voice.chars)[-30:]

    def _remaining(self) -> float | None:
        """Playback left, assuming it began when the first chunk was sent.

        Approximate in the safe direction: the browser starts a little after
        the send, and pauses if the text runs dry mid-reply, so this can only
        under-count — and the browser's own `playback` hold covers the
        difference."""
        if self._first_sent_at is None:
            return None
        return max(0.0, pcm_seconds(self._sent) - (timing.now() - self._first_sent_at))
