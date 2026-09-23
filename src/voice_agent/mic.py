"""One listening session: microphone audio in, transcripts out, and the timers
that stop a metered recognizer from running forever."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

from voice_agent.channel import Channel
from voice_agent.errors import VoiceAgentError
from voice_agent.stt import STT
from voice_agent.stt.agreement import StablePrefix
from voice_agent.timing import elapsed_ms

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 30.0
"""How long with no *speech* before listening stops. The microphone streams
silence continuously, so the signal is the absence of partial transcripts."""

SESSION_CAP_SECONDS = 300.0
"""A backstop for a room that never stops producing partials (a television)."""

MAX_HOLD_SECONDS = 60.0
"""How long expiry may be suspended before the hold is assumed lost (a closed
tab, an event that never fired). Replies known to still be playing are exempt."""

KEEPALIVE_GAP_SECONDS = 10.0
"""How long the recognizer may go without audio before it is topped up. Scribe
ends a session after ~15 s without audio, with a *normal* close. Topping up only
near that deadline, not every gap: continuous silence is billed as audio."""

KEEPALIVE_BURST_SECONDS = 0.2
"""How much silence one top-up sends."""

MAX_RECONNECTS = 3
"""Reconnect a dropped recognizer rather than going quietly deaf while the page
still says "listening"."""

WATCHDOG_TICK_SECONDS = 1.0


def human_seconds(value: float) -> str:
    if value >= 60 and value % 60 == 0:
        minutes = int(value // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{value:.0f}s"


class Mic:
    """One listening session: audio frames in, transcripts out.

    A queue sits between the socket and the recognizer, so the receive loop
    stays free while the recognizer consumes at its own pace. Listening expires
    after `idle_timeout` without speech or `session_cap` in total; expiry is
    held while the agent is talking.
    """

    def __init__(
        self,
        stt: STT,
        channel: Channel,
        on_final: Callable[[str], Awaitable[None]],
        on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
        on_session: Callable[[], Awaitable[None]] | None = None,
        on_speech: Callable[[], Awaitable[None]] | None = None,
        idle_timeout: float | None = None,
        session_cap: float | None = None,
    ) -> None:
        self._stt = stt
        self._channel = channel
        self._on_final = on_final
        self._on_partial = on_partial
        self._on_session = on_session
        self._on_speech = on_speech
        self._agreement = StablePrefix()
        self.idle_timeout = IDLE_TIMEOUT_SECONDS if idle_timeout is None else idle_timeout
        self.session_cap = SESSION_CAP_SECONDS if session_cap is None else session_cap
        self._frames: asyncio.Queue[bytes | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._listening = False
        self._holds: set[str] = set()
        self._held_since = 0.0
        self._client_frames = 0
        self._last_frame_at = 0.0
        self._keepalive: asyncio.Task[None] | None = None
        self._started = 0.0
        self._heard_speech_at = 0.0
        self._drawn = False
        """Partial text is on the page that no commit has finished yet."""

    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def held(self) -> bool:
        """Something is expected to fill the silence: a turn, or a reply playing."""
        return bool(self._holds)

    @property
    def quiet_for(self) -> float:
        """Seconds since the recognizer last produced anything.

        Reads late: there is no VAD here, and the recognizer trails speech by
        several hundred milliseconds. Negative while a reply is still playing
        (`expect_silence` puts the clock in the future).
        """
        return time.perf_counter() - self._heard_speech_at

    async def start(self) -> None:
        if self._listening:
            return
        self._listening = True
        self._started = self._heard_speech_at = time.perf_counter()
        self._last_frame_at = self._started
        self._frames = asyncio.Queue()
        self._task = asyncio.create_task(self._run())
        self._watchdog = asyncio.create_task(self._watch())
        self._keepalive = asyncio.create_task(self._keep_alive())
        await self._channel.send_json({"type": "listening", "active": True})

    def feed(self, pcm: bytes) -> None:
        if self._frames is not None:
            self._client_frames += 1
            self._last_frame_at = time.perf_counter()
            self._frames.put_nowait(pcm)

    async def _keep_alive(self) -> None:
        """Top up a gap in the microphone's audio with silence. Not counted as
        client frames, so "the browser sent no audio" stays diagnosable."""
        silence = b"\x00" * int(self._stt.sample_rate * 2 * KEEPALIVE_BURST_SECONDS)
        while True:
            await asyncio.sleep(KEEPALIVE_BURST_SECONDS)
            if self._frames is None:
                continue
            if time.perf_counter() - self._last_frame_at >= KEEPALIVE_GAP_SECONDS:
                self._last_frame_at = time.perf_counter()
                self._frames.put_nowait(silence)

    def expect_silence(self, seconds: float) -> None:
        """Start counting the user's silence only once the reply has played.

        Derived from the audio sent rather than from a browser message, so a
        lost message cannot stop the microphone mid-reply.
        """
        self._heard_speech_at = max(self._heard_speech_at, time.perf_counter() + seconds)

    def hold(self, name: str, held: bool) -> None:
        """Suspend or resume expiry, keyed by what is suspending it.

        A set, not a flag or a counter: the turn and the browser's playback
        overlap, and a client may release fewer times than it held. Releasing
        restarts the idle window — the user has just been given something to
        answer.
        """
        was_free = not self._holds
        self._holds.add(name) if held else self._holds.discard(name)
        if was_free and self._holds:
            self._held_since = time.perf_counter()
        if not self._holds:
            self._heard_speech_at = time.perf_counter()

    @contextlib.contextmanager
    def busy(self) -> Iterator[None]:
        self.hold("turn", True)
        try:
            yield
        finally:
            self.hold("turn", False)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK_SECONDS)
            now = time.perf_counter()

            # Before the hold, and not pausable: a stuck hold is what the cap is for.
            if now - self._started >= self.session_cap:
                await self.stop(reason=f"listening stopped after {human_seconds(self.session_cap)}")
                return

            if self._holds:
                # Not stuck while a reply is known to be playing.
                if now - self._held_since < MAX_HOLD_SECONDS or now < self._heard_speech_at:
                    continue
                logger.warning(
                    "expiry holds %s stuck for %.0fs; releasing",
                    sorted(self._holds),
                    now - self._held_since,
                )
                self._holds.clear()
                self._heard_speech_at = now
                continue

            if now - self._heard_speech_at >= self.idle_timeout:
                # "You said nothing" and "your browser sent nothing" differ in
                # whose doing it is.
                if self._client_frames:
                    reason = f"listening stopped — no speech for {human_seconds(self.idle_timeout)}"
                else:
                    reason = "listening stopped — the browser sent no audio"
                await self.stop(reason=reason)
                return

    async def stop(self, reason: str | None = None, announce: bool = True) -> None:
        if not self._listening:
            return
        self._listening = False

        for name in ("_watchdog", "_keepalive"):
            helper: asyncio.Task[None] | None = getattr(self, name)
            setattr(self, name, None)
            if helper is not None and helper is not asyncio.current_task():
                helper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await helper

        if self._frames is not None:
            self._frames.put_nowait(None)  # ends the audio iterator, which ends the session
        task, self._task = self._task, None
        # `_run` calls stop() itself, so this may be the current task; awaiting
        # it would deadlock.
        if task is not None and task is not asyncio.current_task():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            if not task.done():
                task.cancel()
        self._frames = None

        if announce:
            payload: dict[str, object] = {"type": "listening", "active": False}
            if reason is not None:
                payload["reason"] = reason
            with contextlib.suppress(Exception):
                await self._channel.send_json(payload)

    async def _audio(self) -> AsyncIterator[bytes]:
        # Bound once: `stop()` may clear the attribute while queued frames drain.
        frames = self._frames
        assert frames is not None
        while (chunk := await frames.get()) is not None:
            yield chunk

    async def _run(self) -> None:
        """Consume transcripts while listening. A recognizer session can end
        without failing (Scribe closes an idle one normally), so a stream that
        ends while still listening is reconnected, and the page is told."""
        for attempt in range(MAX_RECONNECTS + 1):
            try:
                await self._consume()
            except VoiceAgentError as exc:
                logger.warning("listening failed: %s", exc)
                await self._fail(str(exc))
                return
            except Exception:
                # A defect; escaping would end listening silently.
                logger.exception("listening crashed")
                await self._fail("listening stopped after an internal error")
                return

            if not self._listening:
                return  # a normal stop; the audio iterator was closed by us

            if attempt == MAX_RECONNECTS:
                await self.stop(reason="listening stopped — the recognizer kept dropping out")
                return

            logger.info("recognizer ended the session; reconnecting (%d)", attempt + 1)
            await self._channel.send_json(
                {"type": "listening", "active": True, "reason": "reconnected to the recognizer"}
            )

    async def _drop(self) -> None:
        """Take back partial text nothing will finish. Left on the page, the
        next utterance would be written into that bubble, wherever it sits."""
        if not self._drawn:
            return
        self._drawn = False
        await self._channel.send_json({"type": "transcript_dropped"})

    async def _fail(self, message: str) -> None:
        await self._channel.send_json({"type": "listen_error", "message": message})
        await self.stop()

    async def _consume(self) -> None:
        # Every recognizer session starts from nothing: settled words and drawn
        # partials from the last one are void.
        self._agreement.reset()
        if self._on_session is not None:
            await self._on_session()
        await self._drop()
        heard_at = time.perf_counter()
        async for transcript in self._stt.stream(self._audio()):
            self._heard_speech_at = time.perf_counter()
            self._client_frames = 0
            if not transcript.is_final:
                if transcript.text.strip() and self._on_speech is not None:
                    # First: the earliest sign the user is talking, maybe over the agent.
                    await self._on_speech()
                # `repeated` = nothing new this time, the closest available sign
                # that the user has stopped.
                self._agreement.update(transcript.text)
                if not self._agreement.repeated:
                    # From the last new *word*, not the last message.
                    heard_at = time.perf_counter()
                self._drawn = self._drawn or bool(transcript.text.strip())
                await self._channel.send_json(
                    {"type": "transcript", "text": transcript.text, "final": False}
                )
                if self._agreement.text and self._on_partial is not None:
                    await self._on_partial(self._agreement.text, self._agreement.repeated)
                continue

            if not transcript.text.strip():
                # An empty commit (a flush, or noise): nothing to report, but the
                # partials already drawn came to nothing.
                heard_at = time.perf_counter()
                self._agreement.reset()
                await self._drop()
                continue

            if self._agreement.contradictions:
                # Should be zero; if not, agreement is unsafe on this recognizer.
                logger.warning(
                    "agreement contradicted itself %d time(s) this turn",
                    self._agreement.contradictions,
                )

            await self._channel.send_json(
                {
                    "type": "transcript",
                    "text": transcript.text,
                    "final": True,
                    # From the last partial, not from when the user stopped
                    # talking, which only a VAD could see: it understates.
                    "endpoint_ms": elapsed_ms(heard_at),
                    # Whether the text called stable really was how the turn began.
                    "prefix_held": self._agreement.holds_for(transcript.text),
                    "stable_words": len(self._agreement.text.split()),
                }
            )
            self._agreement.reset()
            self._drawn = False
            heard_at = time.perf_counter()
            await self._on_final(transcript.text)
