"""Expiry rules, tested against `Mic` directly rather than through a socket.

These are timing behaviours, and a broken one means "the expiry never fires".
Through a WebSocket that is an indefinite block — the suite hangs instead of
failing, which is how two of these bugs stayed hidden. Here every wait has a
timeout, so a regression fails in under a second and says what it was waiting
for.
"""

import asyncio
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.conftest import FakeSTT
from voice_agent import mic as mic_module
from voice_agent.floor import Transition
from voice_agent.mic import Mic
from voice_agent.stt.base import Transcript
from voice_agent.vad import WINDOW_BYTES

WAIT_TIMEOUT = 2.0

pytestmark = pytest.mark.anyio


class RecordingChannel:
    """Captures frames and lets a test wait for one with a deadline."""

    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []
        self.arrived = asyncio.Event()

    async def send_json(self, payload: dict[str, object]) -> None:
        self.frames.append(payload)
        self.arrived.set()

    async def wait_for(self, **match: object) -> dict[str, object]:
        """Wait for a frame matching every given field.

        Wrapped in a deadline by every caller — a broken expiry means "the
        frame never arrives", which without one blocks the whole suite instead
        of failing the one test.
        """
        async with asyncio.timeout(WAIT_TIMEOUT):
            while True:
                for frame in self.frames:
                    if all(frame.get(key) == value for key, value in match.items()):
                        return frame
                self.arrived.clear()
                await self.arrived.wait()


class SilentSTT:
    """Ears that hear nothing, however long the microphone stays open."""

    provider = "silent"
    model = "silent-1"
    sample_rate = 16000
    nothing: tuple[Transcript, ...] = ()

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        async for _ in audio:
            pass
        for transcript in self.nothing:  # never runs; makes this a generator
            yield transcript


async def never_called(text: str) -> None:  # pragma: no cover - asserted unused
    raise AssertionError(f"a turn started unexpectedly: {text!r}")


@pytest.fixture(autouse=True)
def fast_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mic_module, "WATCHDOG_TICK_SECONDS", 0.01)


async def running_mic(channel: RecordingChannel, **kwargs: float) -> Mic:
    mic = Mic(SilentSTT(), channel, never_called, **kwargs)  # type: ignore[arg-type]
    await mic.start()
    return mic


async def test_silence_expires_the_session() -> None:
    channel = RecordingChannel()
    mic = await running_mic(channel, idle_timeout=0.05, session_cap=60.0)

    assert await channel.wait_for(type="listening", active=True)
    mic.feed(b"\x00" * 1600)  # audio arriving, nobody talking

    expiry = await channel.wait_for(type="listening", active=False)

    assert mic.listening is False
    assert expiry["reason"]


async def test_a_hold_pauses_expiry() -> None:
    channel = RecordingChannel()
    mic = await running_mic(channel, idle_timeout=0.05, session_cap=60.0)
    mic.hold("turn", True)

    await asyncio.sleep(0.2)  # several idle windows

    assert mic.listening is True
    await mic.stop()


async def test_holds_are_idempotent_so_a_miscounting_client_cannot_wedge_it() -> None:
    """A clip replacing another fires no `onended`, so a browser can send two
    holds and one release. A counter would stick above zero forever and the
    microphone would stay muted with nothing said."""
    channel = RecordingChannel()
    mic = await running_mic(channel, idle_timeout=0.05, session_cap=60.0)

    mic.hold("playback", True)
    mic.hold("playback", True)  # a clip interrupting the previous one
    mic.hold("playback", False)  # ...and only one release for the pair

    # A counter would still read 1 here and never expire again.
    await channel.wait_for(type="listening", active=False)
    assert mic.listening is False


async def test_a_hold_that_is_never_released_expires_by_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab that closes mid-clip must not mute the microphone forever — and
    silently, since a suspended timer announces nothing."""
    monkeypatch.setattr(mic_module, "MAX_HOLD_SECONDS", 0.1)
    channel = RecordingChannel()
    mic = await running_mic(channel, idle_timeout=0.05, session_cap=60.0)

    mic.hold("playback", True)  # never released

    await asyncio.sleep(0.4)
    assert mic.listening is False


async def test_a_long_reply_s_playback_hold_is_not_mistaken_for_a_stuck_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 156-second reply held playback past the one-minute ceiling, the hold was
    declared lost, the idle clock was reset to *now* — and listening stopped with
    the agent still talking. The server already knows the reply's length."""
    monkeypatch.setattr(mic_module, "MAX_HOLD_SECONDS", 0.1)
    channel = RecordingChannel()
    mic = await running_mic(channel, idle_timeout=0.1, session_cap=60.0)

    mic.expect_silence(0.8)  # a reply far longer than the hold ceiling
    mic.hold("playback", True)  # the browser playing it

    await asyncio.sleep(0.5)
    assert mic.listening is True, "listening stopped while the reply was still playing"

    # A hold that outlives the reply is still released — and then expires.
    await channel.wait_for(type="listening", active=False)


async def test_the_hard_cap_is_not_pausable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap a stuck hold can defeat is not a cap, and a stuck hold is exactly
    what it most needs to catch."""
    monkeypatch.setattr(mic_module, "MAX_HOLD_SECONDS", 60.0)
    channel = RecordingChannel()
    mic = await running_mic(channel, idle_timeout=60.0, session_cap=0.1)

    mic.hold("turn", True)  # held for the whole session

    await asyncio.sleep(0.4)
    assert mic.listening is False
    assert "stopped after" in str(channel.frames[-1].get("reason"))


async def test_the_agent_talking_is_not_counted_as_the_user_being_silent() -> None:
    """The failure this exists for: a 31-second reply, during which the browser
    mutes its microphone, followed by "no speech for 30s" — blaming the user
    for a silence they were never given the chance to break.

    Derived from the clip the server itself sent, so a browser that never
    reports playback cannot cause it.
    """
    channel = RecordingChannel()
    mic = await running_mic(channel, idle_timeout=0.1, session_cap=60.0)

    mic.expect_silence(0.5)  # a reply longer than the idle window

    await asyncio.sleep(0.3)
    assert mic.listening is True, "expired while the agent was still speaking"

    # ...and it does still expire once the reply has finished playing.
    await channel.wait_for(type="listening", active=False)


async def test_expiry_says_whether_the_browser_was_sending_anything() -> None:
    """ "You said nothing" and "your browser sent us nothing" wear the same
    timeout and need different words — only one of them is the user's doing."""
    channel = RecordingChannel()
    await running_mic(channel, idle_timeout=0.05, session_cap=60.0)

    expiry = await channel.wait_for(type="listening", active=False)

    assert expiry["reason"] == "listening stopped — the browser sent no audio"


async def test_a_user_who_really_was_silent_is_told_so() -> None:
    channel = RecordingChannel()
    mic = await running_mic(channel, idle_timeout=0.15, session_cap=60.0)
    mic.feed(b"\x00" * 1600)  # audio is arriving; it is just quiet

    expiry = await channel.wait_for(type="listening", active=False)

    assert "no speech" in str(expiry["reason"])


class DroppingSTT:
    """Ears whose session ends normally after a while, as Scribe's does.

    The failure this models is not an error: the socket closes with code 1000,
    the stream simply stops, and nothing raises. That is why it went unnoticed.
    """

    provider = "dropping"
    model = "dropping-1"
    sample_rate = 16000

    def __init__(self, frames_before_dropping: int = 2) -> None:
        self.limit = frames_before_dropping
        self.sessions = 0

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        self.sessions += 1
        session = self.sessions
        seen = 0
        async for _ in audio:
            seen += 1
            if seen >= self.limit:
                return  # a normal close, not a failure
            yield Transcript(f"session {session} heard {seen}", is_final=False)


async def test_a_recognizer_that_ends_the_session_is_reconnected_not_ignored() -> None:
    """Scribe closes an idle session with a normal code after ~15 s of no
    audio. Treating that as the end of listening left the page saying
    "listening" while the user talked to nobody."""
    channel = RecordingChannel()
    stt = DroppingSTT(frames_before_dropping=2)
    mic = Mic(stt, channel, never_called, idle_timeout=60.0, session_cap=60.0)  # type: ignore[arg-type]
    await mic.start()

    for _ in range(6):
        mic.feed(b"\x00" * 1600)
        await asyncio.sleep(0.02)

    reconnect = await channel.wait_for(type="listening", reason="reconnected to the recognizer")

    assert reconnect["active"] is True
    assert stt.sessions > 1, "the session was never re-established"
    await mic.stop()


async def test_repeated_drops_eventually_stop_and_say_so() -> None:
    channel = RecordingChannel()
    stt = DroppingSTT(frames_before_dropping=1)
    mic = Mic(stt, channel, never_called, idle_timeout=60.0, session_cap=60.0)  # type: ignore[arg-type]
    await mic.start()

    for _ in range(8):
        mic.feed(b"\x00" * 1600)
        await asyncio.sleep(0.02)

    expiry = await channel.wait_for(type="listening", active=False)

    assert "kept dropping out" in str(expiry["reason"])
    assert mic.listening is False


async def test_silence_is_sent_when_the_browser_goes_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root cause: while a reply plays, the browser sends nothing, and
    ~15 s of that ends the recognizer's session. The gap gets filled."""
    monkeypatch.setattr(mic_module, "KEEPALIVE_GAP_SECONDS", 0.1)
    channel = RecordingChannel()

    class Counting:
        provider, model, sample_rate = "counting", "counting-1", 16000
        frames = 0
        nothing: tuple[Transcript, ...] = ()

        async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
            async for _ in audio:
                Counting.frames += 1
            for transcript in self.nothing:  # never runs; makes this a generator
                yield transcript

    mic = Mic(Counting(), channel, never_called, idle_timeout=60.0, session_cap=60.0)  # type: ignore[arg-type]
    await mic.start()

    await asyncio.sleep(0.7)  # the browser sends nothing at all
    await mic.stop()

    assert Counting.frames >= 2, "the recognizer was left with no audio at all"


async def test_stopping_from_a_transcript_drains_cleanly() -> None:
    """A spoken "bye" stops the microphone from inside its own task, with
    frames still queued behind the one that carried the word."""
    channel = RecordingChannel()
    stt = FakeSTT(script=[Transcript("bye", is_final=True)])

    async def stop_on_final(text: str) -> None:
        await mic.stop()

    mic = Mic(stt, channel, stop_on_final)  # type: ignore[arg-type]
    await mic.start()
    task = mic._task
    assert task is not None
    for _ in range(3):
        mic.feed(b"\x00\x00")

    async with asyncio.timeout(WAIT_TIMEOUT):
        await asyncio.wait({task})
    assert task.exception() is None


async def test_a_recognizer_defect_is_reported_not_silent() -> None:
    """Only provider errors were caught. Anything else ended the listening task
    without a word, leaving a page that says "listening" to nobody."""
    channel = RecordingChannel()

    class BrokenSTT(SilentSTT):
        async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
            async for _ in audio:
                raise RuntimeError("unexpected payload")
            for transcript in self.nothing:
                yield transcript

    mic = Mic(BrokenSTT(), channel, never_called)  # type: ignore[arg-type]
    await mic.start()
    mic.feed(b"\x00\x00")

    await channel.wait_for(type="listen_error")
    assert not mic.listening


async def test_the_floor_reaches_the_page_as_the_user_speaks_and_pauses() -> None:
    """The page's audio runs through the VAD as well as the recognizer, and each
    change of floor is sent — marked when the agent's own voice is playing, so
    echo can be told from the user."""
    channel = RecordingChannel()
    mic = Mic(SilentSTT(), channel, never_called)  # type: ignore[arg-type]
    await mic.start()
    mic.hold("playback", True)

    with wave.open(str(Path(__file__).parent / "fixtures" / "pause.wav")) as clip:
        audio = clip.readframes(clip.getnframes())
    for i in range(0, len(audio), WINDOW_BYTES):
        mic.feed(audio[i : i + WINDOW_BYTES])

    await channel.wait_for(type="floor", state="pause")
    floors = [f for f in channel.frames if f["type"] == "floor"]
    assert [f["state"] for f in floors[:3]] == ["speaking", "micro_pause", "pause"]
    assert all(f["agent"] is True for f in floors)
    await mic.stop()


async def test_a_stopped_mic_stops_hearing() -> None:
    mic = Mic(SilentSTT(), RecordingChannel(), never_called)  # type: ignore[arg-type]
    await mic.start()
    hearing = mic._hearing
    assert hearing is not None

    await mic.stop()

    assert hearing.done()
    mic.feed(b"\x00" * WINDOW_BYTES)  # after stop: dropped, not queued for nobody


async def test_a_new_session_gets_a_new_detector() -> None:
    """Never a reset one: a worker thread from the last session may still be
    inside the old detector, since a thread cannot be cancelled."""
    mic = Mic(SilentSTT(), RecordingChannel(), never_called)  # type: ignore[arg-type]
    await mic.start()
    first = mic._vad
    await mic.stop()
    await mic.start()

    assert mic._vad is not None and mic._vad is not first
    await mic.stop()


async def test_speech_that_never_became_words_does_not_date_the_next_utterance() -> None:
    """A cough, or the agent's own echo, is heard as speech but never
    transcribed. Its onset must not be where the next real utterance's
    first-words time is measured from."""
    mic = Mic(SilentSTT(), RecordingChannel(), never_called)  # type: ignore[arg-type]
    await mic.start()
    long_ago = time.perf_counter() - 30

    await mic._report(Transition("speaking", 0, 64), long_ago)
    await mic._report(Transition("yielded", 64, 1564), long_ago + 1.5)
    assert mic._began_at is None

    now = time.perf_counter()
    await mic._report(Transition("speaking", 0, 64), now)
    assert mic._began_at == pytest.approx(now - 0.064)
    await mic.stop()


def onset(mic: Mic) -> tuple[int | None, float | None]:
    """Read through a call, so a type checker does not carry a narrowing across
    the awaits that change it."""
    return mic._first_words_ms, mic._began_at


async def test_a_commit_of_nothing_starts_the_measures_afresh() -> None:
    """An empty commit ends the utterance as surely as a real one. Left set, its
    first-words time would block every later onset and be reported again."""
    channel = RecordingChannel()
    stt = FakeSTT(script=[Transcript("uh", is_final=False), Transcript("", is_final=True)])
    began = asyncio.Event()

    async def session_began() -> None:
        began.set()

    mic = Mic(stt, channel, never_called, on_session=session_began)  # type: ignore[arg-type]
    await mic.start()
    # A new recognizer session clears the onset itself, so set it after one.
    async with asyncio.timeout(WAIT_TIMEOUT):
        await began.wait()
    mic._began_at = time.perf_counter()

    mic.feed(b"\x00\x00")
    await channel.wait_for(type="transcript", final=False)
    assert onset(mic)[0] is not None
    mic.feed(b"\x00\x00")
    await channel.wait_for(type="transcript_dropped")

    assert onset(mic) == (None, None)
    await mic.stop()
