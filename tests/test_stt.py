"""The ElevenLabs adapter against a real WebSocket speaking Scribe's protocol.

Worth the machinery: the shutdown path here produced a user-visible bug — an
orderly close being reported as "transcription failed" at the end of every
session — and that only reproduces against a real socket.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.frames import Close

from voice_agent.config import load_settings
from voice_agent.errors import ConfigError, ProviderError
from voice_agent.stt import create_stt, elevenlabs_stt
from voice_agent.stt.elevenlabs_stt import ElevenLabsSTT


async def audio_of(chunks: int) -> AsyncIterator[bytes]:
    for _ in range(chunks):
        yield b"\x00\x01" * 1600


async def scribe(connection: ServerConnection) -> None:
    """A stand-in that answers each audio chunk the way Scribe does."""
    await connection.send(json.dumps({"message_type": "session_started", "session_id": "t"}))
    heard = 0
    async for raw in connection:
        message = json.loads(raw)
        if message["commit"]:
            await connection.send(
                json.dumps({"message_type": "committed_transcript", "text": "all of it"})
            )
            continue
        heard += 1
        await connection.send(
            json.dumps({"message_type": "partial_transcript", "text": f"partial {heard}"})
        )


async def failing(connection: ServerConnection) -> None:
    await connection.send(
        json.dumps({"message_type": "scribe_quota_exceeded_error", "error": "out of credits"})
    )
    async for _ in connection:
        pass


Handler = Callable[[ServerConnection], Awaitable[None]]
Endpoint = Callable[[Handler], Awaitable[ElevenLabsSTT]]


@pytest.fixture
async def endpoint(monkeypatch: pytest.MonkeyPatch) -> Endpoint:
    async def build(handler: Handler) -> ElevenLabsSTT:
        server = await serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(elevenlabs_stt, "ENDPOINT", f"ws://127.0.0.1:{port}")
        monkeypatch.setattr(elevenlabs_stt, "FLUSH_GRACE_SECONDS", 0.05)
        return ElevenLabsSTT(api_key="test")

    return build


async def test_partials_and_the_closing_commit_come_through(endpoint: Endpoint) -> None:
    stt = await endpoint(scribe)

    transcripts = [t async for t in stt.stream(audio_of(3))]

    assert [(t.text, t.is_final) for t in transcripts] == [
        ("partial 1", False),
        ("partial 2", False),
        ("partial 3", False),
        ("all of it", True),
    ]


class RudePeer:
    """A server that never answers our close frame.

    This is what ElevenLabs actually does, and it is why a compliant test
    server cannot reproduce the bug: the `websockets` library's own server
    politely completes the handshake, so against it the shutdown looks clean
    and the regression hides.
    """

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        self.closed = asyncio.Event()
        self.sent: list[str] = []

    async def __aenter__(self) -> "RudePeer":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        self.closed.set()

    async def __aiter__(self) -> AsyncIterator[str]:
        for message in self.messages:
            yield message
        await self.closed.wait()  # raise only once *we* have closed
        raise websockets.ConnectionClosedError(None, Close(1000, "OK"))


async def test_our_own_shutdown_is_not_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the audio ends we commit, wait, then close. The peer never answers,
    so the library raises `sent 1000 (OK); no close frame received` — which was
    being shown to the user as "transcription failed" at the end of every
    listening session."""
    peer = RudePeer([json.dumps({"message_type": "committed_transcript", "text": "done"})])
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: peer)
    monkeypatch.setattr(elevenlabs_stt, "FLUSH_GRACE_SECONDS", 0.01)

    transcripts = [t async for t in ElevenLabsSTT(api_key="test").stream(audio_of(1))]

    assert [(t.text, t.is_final) for t in transcripts] == [("done", True)]
    assert peer.closed.is_set()  # we really did go through the closing path


async def test_a_real_error_payload_still_raises(endpoint: Endpoint) -> None:
    stt = await endpoint(failing)

    with pytest.raises(ProviderError, match="quota"):
        async for _ in stt.stream(audio_of(1)):
            pass


async def test_an_unreachable_service_is_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(elevenlabs_stt, "ENDPOINT", "ws://127.0.0.1:1")
    with pytest.raises(ProviderError, match="transcription failed"):
        async for _ in ElevenLabsSTT(api_key="test").stream(audio_of(1)):
            pass


def test_the_vad_threshold_is_carried_on_the_url() -> None:
    """Endpointing is configured at connect time, so a wrong value here is a
    silently sluggish agent rather than a failure."""
    url = ElevenLabsSTT(api_key="test", silence_seconds=0.7).url

    assert "commit_strategy=vad" in url
    assert "vad_silence_threshold_secs=0.7" in url


def test_none_is_deafness_not_a_backend() -> None:
    assert create_stt("none") is None


def test_an_unknown_backend_names_the_ones_that_exist() -> None:
    with pytest.raises(ConfigError, match="elevenlabs, none"):
        create_stt("whisper")


def test_a_missing_key_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY"):
        create_stt("elevenlabs")


def test_websockets_is_a_direct_dependency() -> None:
    """It arrives transitively with uvicorn[standard]; this module imports it
    directly, so it must be declared directly too."""
    assert websockets.__name__ == "websockets"


def test_the_default_pause_matches_the_service_default() -> None:
    """0.7 s was tried first and was worse: it committed "Or rather..." as a
    finished turn and had the agent answer a fragment. Being cut off mid-thought
    is a more obvious failure than waiting an extra beat."""
    assert elevenlabs_stt.DEFAULT_SILENCE_SECONDS == 1.5
    assert "vad_silence_threshold_secs=1.5" in ElevenLabsSTT(api_key="test").url


def test_the_pause_is_tunable_because_no_single_value_is_right(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Named rather than taken from the settings' default: since chapter 12 the
    # default ears are AssemblyAI, and this test is about *this* backend
    # reading the shared knob.
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("VOICE_AGENT_STT", "elevenlabs")
    monkeypatch.setenv("VOICE_AGENT_VAD_SILENCE", "2.5")
    settings = load_settings()

    backend = create_stt(settings.ears_provider, settings.vad_silence)

    assert isinstance(backend, ElevenLabsSTT)
    assert backend.silence_seconds == 2.5
    assert "vad_silence_threshold_secs=2.5" in backend.url


def test_an_unset_pause_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.delenv("VOICE_AGENT_VAD_SILENCE", raising=False)

    assert load_settings().vad_silence is None
    backend = create_stt("elevenlabs", None)
    assert isinstance(backend, ElevenLabsSTT)
    assert backend.silence_seconds == 1.5
