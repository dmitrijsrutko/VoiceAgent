"""The AssemblyAI adapter against a real WebSocket speaking the v3 protocol.

Worth a real socket rather than a mock, for the same reason `test_stt.py` is:
the things that actually break here — audio going up in the wrong frame type, a
session that is never terminated, a formatted turn arriving twice — are all
properties of the conversation with the server, not of the object.
"""

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.frames import Close

from voice_agent import vad
from voice_agent.config import load_settings
from voice_agent.errors import ConfigError, ProviderError
from voice_agent.stt import assemblyai_stt, create_stt
from voice_agent.stt.assemblyai_stt import AssemblyAISTT, explain
from voice_agent.stt.base import batched


async def audio_of(chunks: int) -> AsyncIterator[bytes]:
    """Whole 100 ms chunks, so each arrives at the server as it was sent."""
    for _ in range(chunks):
        yield b"\x00\x01" * 1600


def turn(order: int, text: str, end: bool, formatted: bool = False) -> str:
    return json.dumps(
        {
            "type": "Turn",
            "turn_order": order,
            "transcript": text,
            "end_of_turn": end,
            "turn_is_formatted": formatted,
        }
    )


class Recorder:
    """A stand-in server that answers audio the way the v3 endpoint does, and
    remembers how it was spoken to."""

    def __init__(self, tail: list[str] | None = None) -> None:
        self.binary: list[bytes] = []
        self.text: list[str] = []
        self.tail = tail if tail is not None else [turn(0, "all of it", end=True)]

    async def __call__(self, connection: ServerConnection) -> None:
        await connection.send(json.dumps({"type": "Begin", "id": "t", "expires_at": 0}))
        heard = 0
        async for raw in connection:
            if isinstance(raw, bytes):
                self.binary.append(raw)
                heard += 1
                await connection.send(turn(0, f"partial {heard}", end=False))
                continue
            self.text.append(raw)
            if json.loads(raw).get("type") == "Terminate":
                for message in self.tail:
                    await connection.send(message)
                await connection.send(
                    json.dumps(
                        {
                            "type": "Termination",
                            "audio_duration_seconds": 1,
                            "session_duration_seconds": 1,
                        }
                    )
                )


async def failing(connection: ServerConnection) -> None:
    await connection.send(json.dumps({"type": "Error", "error": "insufficient balance"}))
    async for _ in connection:
        pass


Handler = Callable[[ServerConnection], Awaitable[None]]
Endpoint = Callable[[Handler], Awaitable[AssemblyAISTT]]


@pytest.fixture
async def endpoint(monkeypatch: pytest.MonkeyPatch) -> Endpoint:
    async def build(handler: Handler) -> AssemblyAISTT:
        server = await serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(assemblyai_stt, "ENDPOINT", f"ws://127.0.0.1:{port}")
        monkeypatch.setattr(assemblyai_stt, "FLUSH_GRACE_SECONDS", 0.05)
        return AssemblyAISTT(api_key="test")

    return build


async def test_partials_and_the_end_of_turn_final_come_through(endpoint: Endpoint) -> None:
    stt = await endpoint(Recorder())

    transcripts = [t async for t in stt.stream(audio_of(3))]

    assert [(t.text, t.is_final) for t in transcripts] == [
        ("partial 1", False),
        ("partial 2", False),
        ("partial 3", False),
        ("all of it", True),
    ]


async def test_a_turn_finalized_twice_is_one_turn(endpoint: Endpoint) -> None:
    """The service can deliver a turn again once it has been formatted. Passed
    through, the second copy drives an entire extra turn — the agent answering
    the same sentence twice — because nothing downstream can tell them apart."""
    recorder = Recorder(
        tail=[
            turn(0, "what is the capital of latvia", end=True),
            turn(0, "What is the capital of Latvia?", end=True, formatted=True),
        ]
    )
    stt = await endpoint(recorder)

    finals = [t async for t in stt.stream(audio_of(1)) if t.is_final]

    assert [t.text for t in finals] == ["what is the capital of latvia"]


async def test_the_session_is_terminated_when_the_audio_runs_out(endpoint: Endpoint) -> None:
    """Not politeness: an abandoned session keeps billing until the three-hour
    cap, so this is the cost control."""
    recorder = Recorder()
    stt = await endpoint(recorder)

    async for _ in stt.stream(audio_of(1)):
        pass

    assert [json.loads(m)["type"] for m in recorder.text] == ["Terminate"]


async def test_audio_goes_up_as_binary_frames(endpoint: Endpoint) -> None:
    """This endpoint takes raw PCM frames. Scribe takes base64 inside JSON and
    so does the Voice Agent API, so sending an envelope here is the easy
    mistake — and it fails as silence, not as an error."""
    recorder = Recorder()
    stt = await endpoint(recorder)

    async for _ in stt.stream(audio_of(2)):
        pass

    assert recorder.binary == [b"\x00\x01" * 1600] * 2


async def test_an_error_payload_raises(endpoint: Endpoint) -> None:
    stt = await endpoint(failing)

    with pytest.raises(ProviderError, match="insufficient balance"):
        async for _ in stt.stream(audio_of(1)):
            pass


async def test_an_unreachable_service_is_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assemblyai_stt, "ENDPOINT", "ws://127.0.0.1:1")

    with pytest.raises(ProviderError, match="transcription failed"):
        async for _ in AssemblyAISTT(api_key="test").stream(audio_of(1)):
            pass


def test_the_model_is_the_singular_streaming_string() -> None:
    """Streaming takes `speech_model` as one string; the pre-recorded API takes
    a plural `speech_models` array. Sending either shape to the other endpoint
    is the most common way to get this API wrong."""
    url = AssemblyAISTT(api_key="test").url

    assert "speech_model=universal-3-5-pro" in url
    assert "speech_models" not in url


def test_the_pause_reaches_the_url_in_milliseconds() -> None:
    """The flag is seconds and the service is milliseconds. A factor of a
    thousand here is not an error, it is an agent that waits 25 minutes."""
    url = AssemblyAISTT(api_key="test", silence_seconds=0.7).url

    assert "max_turn_silence=700" in url
    assert "min_turn_silence=350" in url


def test_a_wild_pause_is_clamped_to_what_the_service_accepts() -> None:
    """Corrected before it becomes a rejected connection, not after."""
    assert AssemblyAISTT(api_key="test", silence_seconds=600).max_turn_silence_ms == 10_000
    assert AssemblyAISTT(api_key="test", silence_seconds=0.01).max_turn_silence_ms == 50
    assert AssemblyAISTT(api_key="test", silence_seconds=0.01).min_turn_silence_ms == 50


def test_the_sample_rate_is_the_one_the_browser_captures_at() -> None:
    """The page opens its AudioContext at whatever the chosen ears report, so
    these drifting apart pitches the transcript's accuracy, not the audio."""
    assert AssemblyAISTT(api_key="test").sample_rate == assemblyai_stt.SAMPLE_RATE == 16000


def test_a_missing_key_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="ASSEMBLYAI_API_KEY"):
        create_stt("assemblyai")


def test_elevenlabs_scribe_is_the_default_pair_of_ears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICE_AGENT_STT", raising=False)

    assert load_settings().ears_provider == "elevenlabs"


def test_the_pause_is_tunable_on_this_backend_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test")
    monkeypatch.setenv("VOICE_AGENT_STT", "assemblyai")
    monkeypatch.setenv("VOICE_AGENT_VAD_SILENCE", "2.5")
    settings = load_settings()

    backend = create_stt(settings.ears_provider, settings.vad_silence)

    assert isinstance(backend, AssemblyAISTT)
    assert backend.silence_seconds == 2.5
    assert "max_turn_silence=2500" in backend.url


async def test_a_terminated_session_ends_the_stream_without_an_error(endpoint: Endpoint) -> None:
    """The service answers `Terminate` with `Termination`. Reaching it is what
    ends the loop — and it must not surface as a failed transcription, which is
    the bug chapter 3 shipped against Scribe's silent close."""
    stt = await endpoint(Recorder())

    transcripts = [t async for t in stt.stream(audio_of(1))]

    assert transcripts[-1].is_final


def test_a_close_code_is_translated_into_advice() -> None:
    """The close code *is* the error on this endpoint — it rarely sends an error
    payload — and the number alone says nothing about what to do.

    Pinned because `explain` reads `code` off the library's exception, and that
    is exactly the attribute that gets renamed in a major version. Without this
    the hints would become unreachable silently, which is worse than never
    having written them.
    """
    chunk = websockets.ConnectionClosedError(Close(3007, "bad chunk"), None)
    unauthorized = websockets.ConnectionClosedError(Close(1008, "nope"), None)

    assert "50-1000 ms" in explain(chunk)
    assert "ASSEMBLYAI_API_KEY" in explain(unauthorized)


def test_an_unmapped_failure_still_says_something_useful() -> None:
    """Every hint is an extra, never the whole message."""
    assert explain(OSError("connection refused")) == (
        "assemblyai transcription failed: connection refused"
    )


def test_the_browser_frame_is_one_vad_window() -> None:
    """The worklet posts one VAD window per frame, so the server hears a pause
    a window late. The two constants live in different languages, so nothing
    but this test ties them together."""
    worklet = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "voice_agent"
        / "web"
        / "capture-worklet.js"
    ).read_text(encoding="utf-8")
    samples = int(re.findall(r"new Int16Array\((\d+)\)", worklet)[0])

    assert samples == vad.WINDOW_SAMPLES
    assert assemblyai_stt.SAMPLE_RATE == vad.SAMPLE_RATE


async def test_the_recognizer_is_sent_chunks_this_endpoint_will_accept() -> None:
    """A chunk outside 50-1000 ms closes the session with 3007. The page's 32 ms
    frames are under that floor, so they are regrouped — tail included."""

    async def frames() -> AsyncIterator[bytes]:
        for _ in range(10):  # 320 ms: three whole chunks and a 20 ms tail
            yield b"\x00\x01" * vad.WINDOW_SAMPLES

    rate = assemblyai_stt.SAMPLE_RATE
    chunks = [chunk async for chunk in batched(frames(), rate)]

    for chunk in chunks:
        chunk_ms = len(chunk) / 2 / rate * 1000
        assert 50 <= chunk_ms <= 1000, f"{chunk_ms:.0f} ms — 3007 territory"
    assert len(chunks) == 3


def test_russian_is_not_among_the_languages_this_backend_hears() -> None:
    """The fact that cost an evening, pinned.

    Spoken Russian is not refused by this backend — it comes back as confident
    nonsense ("Раскажем не pravalo вывnutriny produkt kitaia"), and the agent
    answers it. If this list ever grows to include `ru`, that is a real change
    worth noticing rather than absorbing silently.
    """
    assert "ru" not in assemblyai_stt.LANGUAGES
    assert len(assemblyai_stt.LANGUAGES) == 18
    assert AssemblyAISTT(api_key="test").languages == assemblyai_stt.LANGUAGES


def test_scribe_hears_what_this_backend_cannot() -> None:
    """The documented escape hatch has to actually be one."""
    from voice_agent.stt import elevenlabs_stt

    assert "rus" in elevenlabs_stt.LANGUAGES
    assert len(elevenlabs_stt.LANGUAGES) > len(assemblyai_stt.LANGUAGES)
