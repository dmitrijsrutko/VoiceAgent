"""Where a reply's voice fell behind its own playback."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.conftest import timed
from voice_agent import timing
from voice_agent.channel import Channel
from voice_agent.llm.base import Usage
from voice_agent.tts.base import AudioChunk
from voice_agent.turn import Speech, reply_report


class Socket:
    def __init__(self) -> None:
        self.json: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.json.append(payload)

    async def send_bytes(self, data: bytes) -> None:
        pass


class Stalling:
    """Voices one chunk, stalls, then voices the rest."""

    provider = "fake-voice"
    voice = "fake-voice-1"

    def __init__(self, stall: float) -> None:
        self.stall = stall

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
        async for _ in text:
            pass
        yield timed(b"Dost")
        await asyncio.sleep(self.stall)
        yield timed(b"oevs")


async def spoken(stall: float) -> dict[str, Any]:
    socket = Socket()
    speech = Speech(Channel(socket), Stalling(stall), 0.0)  # type: ignore[arg-type]
    speech.say("Dostoevsky")
    speech.finish()
    await speech.done()
    return next(frame for frame in socket.json if frame["type"] == "audio_end")


async def test_a_stall_longer_than_the_audio_before_it_is_reported_with_its_place() -> None:
    end = await spoken(stall=0.3)

    assert end["late_ms"] >= 200
    assert end["late_after"] == "Dost"


async def test_a_stream_that_keeps_ahead_reports_no_lateness() -> None:
    end = await spoken(stall=0.0)

    assert end["late_ms"] == 0
    assert end["late_after"] == ""


async def test_a_synthesis_still_stalled_when_its_turn_ends_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stall that never recovers sends no `audio_end`, so the log is the only
    place left to say where the voice stopped."""
    speech = Speech(Channel(Socket()), Stalling(stall=10.0), 0.0)  # type: ignore[arg-type]
    speech.say("Dostoevsky")
    speech.finish()
    await asyncio.sleep(0.05)
    await speech.cancel()

    assert "synthesis unfinished when its turn ended" in caplog.text
    assert "'Dost'" in caplog.text


@pytest.mark.parametrize(
    "reason, shown",
    [("stop", None), ("end_turn", None), (None, None), ("length", "length")],
)
def test_the_record_names_only_an_unusual_end(reason: str | None, shown: str | None) -> None:
    """A reply cut by `length` or a filter still has text; the record says so,
    and stays quiet about the normal end every other turn has."""
    report = reply_report("Yes.", 1, 10, timing.now(), Usage(finish_reason=reason))

    assert report["finish"] == shown
