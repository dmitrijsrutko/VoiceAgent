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
"""How long with no *speech* before listening stops. Not "no audio": the
microphone streams silence continuously, so frames never stop arriving. The
signal that nobody is talking is the absence of partial transcripts."""

SESSION_CAP_SECONDS = 300.0
"""A backstop the idle timer cannot provide, for a room that produces
continuous partials — a television, a conversation nearby. Scribe enforces its
own session limit regardless; better to hit ours, with an explanation."""

MAX_HOLD_SECONDS = 60.0
"""How long expiry may be suspended before the hold is assumed lost — beyond
the end of any reply the server knows is still playing, which can be far longer
(156 s has been heard). A hold that is never released, because a tab closed or a browser event
never fired, would otherwise stop the microphone forever *and* silently, since
a suspended timer announces nothing. This converts the worst failure mode
observed in this project into a one-minute hiccup."""

KEEPALIVE_GAP_SECONDS = 10.0
"""How long the recognizer may go without audio before we top it up.

Measured against the real service: Scribe closes a realtime session after
roughly 15 seconds with no audio — and closes it *normally*, code 1000, so the
stream simply ends rather than raising. Until Chapter 8 the browser stopped
sending while a reply played, so any answer longer than ~15 s silently killed
the ears; it now sends throughout, and this covers a browser that does not.

The first version filled *every* gap, sending silence five times a second. That
is continuous real-time audio to a service metered by audio duration, and it
quietly consumed a month's quota. Topping up shortly before the deadline
instead costs about 2% of that. If the assumption behind the interval is ever
wrong, the session simply ends and the reconnect below catches it — the failure
mode is a hiccup, not silence."""

KEEPALIVE_BURST_SECONDS = 0.2
"""How much silence one top-up sends."""

MAX_RECONNECTS = 3
"""If the recognizer drops us anyway, reconnect rather than going quietly deaf.
Silent failure is the worst outcome available here: the page still says
"listening" and the user keeps talking to nothing."""

WATCHDOG_TICK_SECONDS = 1.0


def human_seconds(value: float) -> str:
    if value >= 60 and value % 60 == 0:
        minutes = int(value // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{value:.0f}s"


class Mic:
    """One listening session: audio frames in, transcripts out.

    Holds a queue rather than handing the recognizer the socket directly,
    because the recognizer consumes audio at its own pace while the receive
    loop must stay free to accept the next frame.

    Listening is metered, so it also expires: after `idle_timeout` with no
    speech, or `session_cap` in total. The watchdog is paused while a turn is
    in progress — the user is not expected to speak while the agent talks, and
    an unpaused timer would punish them for the agent talking.
    """

    def __init__(
        self,
        stt: STT,
        channel: Channel,
        on_final: Callable[[str], Awaitable[None]],
        on_partial: Callable[[str, bool], Awaitable[None]] | None = None,
        on_session: Callable[[], Awaitable[None]] | None = None,
        on_speech: Callable[[], Awaitable[None]] | None = None,
        extra_report: Callable[[], dict[str, object]] | None = None,
        idle_timeout: float | None = None,
        session_cap: float | None = None,
    ) -> None:
        self._stt = stt
        self._channel = channel
        self._on_final = on_final
        self._on_partial = on_partial
        self._on_session = on_session
        self._on_speech = on_speech
        self._extra_report = extra_report
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
        """Whether partial text has been sent that the page is still showing.

        Only this distinguishes an utterance that came to nothing — worth
        taking back — from a stretch of silence that was never drawn at all."""

    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def held(self) -> bool:
        """Something is expected to be filling the silence — a turn running, or
        the browser still playing a reply."""
        return bool(self._holds)

    @property
    def quiet_for(self) -> float:
        """Seconds since the recognizer last produced anything.

        Not since the user stopped talking, which this server cannot see: it
        runs no VAD, and the recognizer trails real speech by several hundred
        milliseconds (measured at ~800 ms on a 1.65 s utterance). So this reads
        systematically late, and everything built on it inherits that.

        Negative while a reply is still playing, because `expect_silence` puts
        the clock in the future — which reads correctly as "not quiet yet".
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
        """Fill any gap in the microphone's audio with silence.

        Counted separately from real frames, so "the browser sent no audio"
        stays a diagnosis rather than being papered over by the silence this
        sends on the browser's behalf.
        """
        silence = b"\x00" * int(self._stt.sample_rate * 2 * KEEPALIVE_BURST_SECONDS)
        while True:
            await asyncio.sleep(KEEPALIVE_BURST_SECONDS)
            if self._frames is None:
                continue
            if time.perf_counter() - self._last_frame_at >= KEEPALIVE_GAP_SECONDS:
                self._last_frame_at = time.perf_counter()
                self._frames.put_nowait(silence)

    def expect_silence(self, seconds: float) -> None:
        """Do not start counting the user's silence until the agent has stopped
        talking.

        The user is not expected to speak while a reply plays — talking over it
        interrupts it, which releases the hold. Deriving that window from the clip we
        just sent, rather than from a message the browser promises to send when
        playback ends, means a lost or missing message cannot produce the worst
        outcome available here: stopping the microphone and blaming the user
        for silence they were never given a chance to break.
        """
        self._heard_speech_at = max(self._heard_speech_at, time.perf_counter() + seconds)

    def hold(self, name: str, held: bool) -> None:
        """Suspend or resume expiry, keyed by *what* is suspending it.

        Named rather than counted because two independent things suspend it —
        the turn, and the browser playing the reply — and they overlap: audio
        starts playing before `run_turn` returns, so a boolean would let the
        turn's exit clear the playback's hold. A counter fixes that but is
        unforgiving: a client that reports one clip interrupting another sends
        two holds and one release, and the count sticks above zero forever.
        A set is idempotent, so the server survives a client that miscounts.

        Releasing counts as fresh activity: the user has just been given
        something to respond to and should get the whole idle window to do it.
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

            # The hard cap is checked *before* the hold, and is not pausable.
            # A cap that a stuck hold can defeat is not a cap, and a stuck hold
            # is exactly the case it most needs to catch.
            if now - self._started >= self.session_cap:
                await self.stop(reason=f"listening stopped after {human_seconds(self.session_cap)}")
                return

            if self._holds:
                # Not stuck while the server itself knows the reply is still
                # playing: `expect_silence` has already pushed the idle clock
                # past its end. A fixed ceiling alone let a 156-second reply's
                # playback hold be declared lost a minute in, and the mic stop
                # half a minute later with the agent still talking.
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
                # Two different failures wear the same timeout. "You said
                # nothing" and "your browser sent us nothing" need different
                # words, because only one of them is the user's doing.
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
        # `_run` calls stop() itself when the recognizer gives up, so it can be
        # the task we are being asked to wait for. Awaiting it there deadlocks
        # until the timeout and then cancels the very coroutine that is trying
        # to announce why listening ended.
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
        # Bound once: `stop()` clears the attribute straight away when it runs
        # on this session's own task — a spoken "bye" — while frames queued
        # ahead of the sentinel are still being drained from this queue.
        frames = self._frames
        assert frames is not None
        while (chunk := await frames.get()) is not None:
            yield chunk

    async def _run(self) -> None:
        """Consume transcripts for as long as we are meant to be listening.

        A recognizer session can end *without* failing — Scribe closes an idle
        one with a normal close code, so the stream simply stops. Treating that
        as the end of listening is how the agent went deaf while the page still
        said "listening" and the user kept talking to nothing. So: if the
        stream ends while we still hold the microphone, reconnect and say so.
        """
        for attempt in range(MAX_RECONNECTS + 1):
            try:
                await self._consume()
            except VoiceAgentError as exc:
                logger.warning("listening failed: %s", exc)
                await self._fail(str(exc))
                return
            except Exception:
                # A defect, not a provider failure — but left to escape it ends
                # this task silently while the page still says "listening".
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
        """Take back partial text the page is showing but nothing will finish.

        The page draws a bubble on the first partial and writes every later one
        into it, so a bubble left behind does not merely linger — the *next*
        utterance lands in it, and appears wherever the abandoned one was
        instead of at the end of the conversation. Seen live: two unprompted
        lines arrived in the gap, and the user's question was drawn above both.
        """
        if not self._drawn:
            return
        self._drawn = False
        await self._channel.send_json({"type": "transcript_dropped"})

    async def _fail(self, message: str) -> None:
        await self._channel.send_json({"type": "listen_error", "message": message})
        await self.stop()

    async def _consume(self) -> None:
        # Every recognizer session starts from nothing. Agreement was only
        # being cleared on a commit, so a session that ended without one — the
        # user stopping mid-sentence, or a reconnect — carried its settled
        # words into the next utterance. There they wedge agreement completely
        # (nothing can extend a prefix the user is not saying) and are then
        # reported as "4 words settled early, prefix did not hold" against
        # words nobody repeated.
        self._agreement.reset()
        if self._on_session is not None:
            await self._on_session()
        # A recognizer session starts from nothing, so anything the last one had
        # begun to transcribe is void — on a reconnect, mid-utterance.
        await self._drop()
        heard_at = time.perf_counter()
        async for transcript in self._stt.stream(self._audio()):
            self._heard_speech_at = time.perf_counter()
            self._client_frames = 0
            if not transcript.is_final:
                if transcript.text.strip() and self._on_speech is not None:
                    # First, before anything else is sent: this is the earliest
                    # sign the user is talking, and it may be over the agent.
                    await self._on_speech()
                # The recognizer will rewrite this text; agreement decides which
                # of it is safe to act on before the turn is over. `repeated`
                # says the recognizer found nothing new this time, which is the
                # closest thing to "they have stopped talking" available here.
                self._agreement.update(transcript.text)
                if not self._agreement.repeated:
                    # Only a partial carrying a new word means words are still
                    # being produced. Bumping this on every partial made
                    # `endpoint_ms` measure from the recognizer's last *message*
                    # rather than its last *word* — which made it identical to
                    # the speculation lead on every turn, since a repeat is
                    # exactly what starts a speculation.
                    heard_at = time.perf_counter()
                self._drawn = self._drawn or bool(transcript.text.strip())
                await self._channel.send_json(
                    {"type": "transcript", "text": transcript.text, "final": False}
                )
                if self._agreement.text and self._on_partial is not None:
                    await self._on_partial(self._agreement.text, self._agreement.repeated)
                continue

            if not transcript.text.strip():
                # A commit with nothing in it — the flush at the end of a
                # session, or a stretch of noise. Its *text* is not worth
                # reporting: an empty bubble and a meaningless endpointing
                # figure on screen. But the fact that it happened is, because
                # the partials leading up to it have already been drawn, and
                # only this says they came to nothing.
                heard_at = time.perf_counter()
                self._agreement.reset()
                await self._drop()
                continue

            if self._agreement.contradictions:
                # Should be zero: agreement is a bet that the recognizer will
                # not unsay something it has already said twice. If this ever
                # fires, the bet is not safe on this recognizer and everything
                # built on it needs rethinking.
                logger.warning(
                    "agreement contradicted itself %d time(s) this turn",
                    self._agreement.contradictions,
                )

            await self._channel.send_json(
                {
                    **(self._extra_report() if self._extra_report else {}),
                    "type": "transcript",
                    "text": transcript.text,
                    "final": True,
                    # Measured from the last *partial transcript*, not from
                    # the moment the user actually stopped talking — which
                    # this server cannot see, because it does not run a VAD.
                    # It therefore understates the real endpointing cost by
                    # however long the recognizer lagged behind the audio;
                    # measured externally that gap was ~800 ms on a 1.65 s
                    # utterance. Delegating endpointing to the STT vendor
                    # also delegates the ability to measure it.
                    "endpoint_ms": elapsed_ms(heard_at),
                    # Was the text we called stable really how the turn began?
                    # The honest test of whether agreement — and everything
                    # built on it — was worth trusting.
                    "prefix_held": self._agreement.holds_for(transcript.text),
                    "stable_words": len(self._agreement.text.split()),
                }
            )
            self._agreement.reset()
            self._drawn = False
            heard_at = time.perf_counter()
            await self._on_final(transcript.text)
