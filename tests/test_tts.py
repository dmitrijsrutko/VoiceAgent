import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace

import httpx2
import pytest
from elevenlabs.core import ApiError
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from voice_agent.errors import ConfigError, ProviderError
from voice_agent.streams import closing
from voice_agent.tts import create_tts, elevenlabs_tts
from voice_agent.tts.base import (
    SAMPLE_RATE,
    Alignment,
    AudioChunk,
    once,
    pcm_seconds,
    whole_samples,
)
from voice_agent.tts.elevenlabs_tts import (
    CHUNK_LENGTH_SCHEDULE,
    DEFAULT_VOICE,
    OUTPUT_FORMAT,
    ElevenLabsTTS,
    explain,
)
from voice_agent.tts.openai_tts import RESPONSE_FORMAT, OpenAITTS


async def chunks_of(*parts: bytes | AudioChunk) -> AsyncIterator[AudioChunk]:
    for part in parts:
        yield part if isinstance(part, AudioChunk) else AudioChunk(part)


async def collect(stream: AsyncIterator[AudioChunk]) -> list[bytes]:
    return [chunk.pcm async for chunk in stream]


async def test_no_chunk_splits_a_sample() -> None:
    """HTTP chunking knows nothing about 16-bit samples. A chunk played with a
    dangling byte shifts every sample after it: the rest of the reply is noise."""
    out = await collect(whole_samples(chunks_of(b"abc", b"d", b"e", b"fgh", b"")))

    assert all(len(chunk) % 2 == 0 for chunk in out)
    assert b"".join(out) == b"abcdefgh", "a byte was lost or reordered"
    assert b"" not in out, "an empty chunk would announce audio that is not there"


async def test_timing_survives_being_re_cut_into_whole_samples() -> None:
    """Timing on a chunk too short to send must ride on the next one rather
    than vanish — lost, what the user heard of those words is unknowable."""
    timing = Alignment("Hi", (40.0, 90.0))
    out = [
        chunk async for chunk in whole_samples(chunks_of(AudioChunk(b"a", timing), b"bcd", b"ef"))
    ]

    assert [chunk.pcm for chunk in out] == [b"abcd", b"ef"]
    assert out[0].alignment == timing
    assert all(chunk.alignment is None for chunk in out[1:])


def test_pcm_duration_follows_from_its_size() -> None:
    assert pcm_seconds(SAMPLE_RATE * 2) == 1.0


def test_both_backends_ask_for_the_rate_the_browser_is_told() -> None:
    """A mismatch here does not fail — it plays, at the wrong pitch."""
    assert SAMPLE_RATE == 24_000
    assert f"pcm_{SAMPLE_RATE}" == OUTPUT_FORMAT
    assert RESPONSE_FORMAT == "pcm"  # OpenAI's pcm is fixed at 24 kHz s16le


def test_registry_builds_each_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    elevenlabs = create_tts("elevenlabs")
    openai = create_tts("openai")

    assert elevenlabs is not None and elevenlabs.provider == "elevenlabs"
    assert openai is not None and openai.provider == "openai"


def test_none_is_silence_not_a_backend() -> None:
    assert create_tts("none") is None


def test_defaults_are_the_low_latency_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    # Flash rather than the highest-quality model: this is a voice agent.
    assert ElevenLabsTTS().model == "eleven_flash_v2_5"
    assert OpenAITTS().model == "gpt-4o-mini-tts"


def test_voice_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")

    assert ElevenLabsTTS("my-cloned-voice").voice == "my-cloned-voice"
    assert OpenAITTS("nova").voice == "nova"


def test_unknown_backend_names_the_ones_that_exist() -> None:
    with pytest.raises(ConfigError, match="elevenlabs, openai, none"):
        create_tts("robot-voice")


def test_a_missing_key_fails_at_startup_not_at_the_first_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="ELEVENLABS_API_KEY"):
        create_tts("elevenlabs")


class FakeApiError(ApiError):
    def __init__(self, status_code: int, body: object) -> None:
        super().__init__(status_code=status_code, body=body)


def test_a_blocked_voice_says_what_to_do_about_it() -> None:
    """ElevenLabs says what went wrong but not how to fix it, and the SDK's own
    exception stringifies every response header. Neither helps a user."""
    error = FakeApiError(
        402,
        {
            "detail": {
                "code": "paid_plan_required",
                "message": "Free users cannot use library voices via the API.",
            }
        },
    )
    message = explain(error)

    assert "Free users cannot use library voices" in message
    assert "EXAVITQu4vr4xnSDxMaL" in message
    assert "date" not in message and "content-type" not in message


def test_a_scoped_key_is_explained_too() -> None:
    error = FakeApiError(
        401,
        {
            "detail": {
                "code": "missing_permissions",
                "message": "missing the permission voices_read",
            }
        },
    )
    assert "dashboard" in explain(error)


def test_an_unrecognized_error_still_reports_the_message() -> None:
    error = FakeApiError(500, {"detail": {"code": "who_knows", "message": "boom"}})
    assert explain(error) == "elevenlabs synthesis failed: boom"


def test_a_non_json_body_does_not_crash_the_explainer() -> None:
    assert "503" in explain(FakeApiError(503, "upstream unavailable"))


def test_the_default_voice_is_one_verified_to_work_on_a_free_account() -> None:
    """Rachel and Aria are both widely cited as safe defaults and both return
    402 on a free plan. This asserts we did not drift back to one of them."""
    assert ElevenLabsTTS.__init__.__defaults__ is not None
    assert DEFAULT_VOICE not in {"21m00Tcm4TlvDq8ikWAM", "9BWtsMINqrJLrRacOk9x"}


class FakeVoicesClient:
    def __init__(self, voices: list[SimpleNamespace]) -> None:
        self.voices = SimpleNamespace(get_all=self._get_all)
        self._voices = voices

    async def _get_all(self) -> SimpleNamespace:
        return SimpleNamespace(voices=self._voices)


async def test_library_voices_are_listed_as_visible_but_not_usable() -> None:
    """The bug this method exists to prevent: a Voice Library voice is returned
    by the API on a free plan and then fails at synthesis time with a 402."""
    client = FakeVoicesClient(
        [
            SimpleNamespace(voice_id="stock-1", name="Sarah", category="premade"),
            SimpleNamespace(voice_id="lib-1", name="Rachel", category="professional"),
            SimpleNamespace(voice_id="mine-1", name="My Clone", category="cloned"),
        ]
    )
    voices = await ElevenLabsTTS(client=client, api_key="test").list_voices()  # type: ignore[arg-type]

    assert [(v.id, v.usable) for v in voices] == [
        ("stock-1", True),
        ("lib-1", False),
        ("mine-1", False),
    ]


async def test_a_voice_with_no_name_still_lists() -> None:
    client = FakeVoicesClient([SimpleNamespace(voice_id="v1", name=None, category="premade")])
    assert (await ElevenLabsTTS(client=client, api_key="test").list_voices())[0].name == "v1"  # type: ignore[arg-type]


async def test_openai_voices_need_no_api_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The asymmetry with ElevenLabs — a fixed set with no plan tiers behind
    it — is why list_voices belongs on the backend, not in a shared helper."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    voices = await OpenAITTS().list_voices()

    assert all(v.usable for v in voices)
    assert "alloy" in {v.id for v in voices}


async def tokens(*parts: str, pace: float = 0.0) -> AsyncIterator[str]:
    for part in parts:
        yield part
        await asyncio.sleep(pace)


class StreamInput:
    """A stand-in for `stream-input`: records every message, and answers each
    piece of text with its bytes as "audio" once the stream is closed — or at
    once, with `eager`, as a service speaking while text still arrives."""

    def __init__(self, parts: list[bytes] | None = None, eager: bool = False) -> None:
        self.received: list[dict[str, object]] = []
        self.paths: list[str] = []
        self.headers: list[str | None] = []
        self.closed = asyncio.Event()
        self.parts = parts
        self.eager = eager

    async def __call__(self, connection: ServerConnection) -> None:
        assert connection.request is not None
        self.paths.append(connection.request.path)
        self.headers.append(connection.request.headers.get("xi-api-key"))
        try:
            async for raw in connection:
                message = json.loads(raw)
                self.received.append(message)
                text = message["text"]
                if text == "":
                    for part in self.parts or []:
                        await send_audio(connection, part)
                    await connection.send(json.dumps({"audio": None, "isFinal": True}))
                elif self.eager and text.strip():
                    await send_audio(connection, text.encode())
        finally:
            self.closed.set()


async def send_audio(
    connection: ServerConnection, data: bytes, alignment: dict[str, object] | None = None
) -> None:
    payload = {"audio": base64.b64encode(data).decode(), "isFinal": None, "alignment": alignment}
    await connection.send(json.dumps(payload))


Serve = Callable[[Callable[[ServerConnection], Awaitable[None]]], Awaitable[ElevenLabsTTS]]


@pytest.fixture
async def endpoint(monkeypatch: pytest.MonkeyPatch) -> Serve:
    async def build(handler: Callable[[ServerConnection], Awaitable[None]]) -> ElevenLabsTTS:
        server = await serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(elevenlabs_tts, "ENDPOINT", f"ws://127.0.0.1:{port}")
        return ElevenLabsTTS(api_key="test-key", client=SimpleNamespace())  # type: ignore[arg-type]

    return build


async def test_tokens_are_forwarded_exactly_as_the_reasoning_engine_wrote_them(
    endpoint: Serve,
) -> None:
    """Half words, no spaces added: deciding when there is enough to speak is
    the service's job, and measured, it does that job better than re-cutting."""
    service = StreamInput(parts=[b"\x01\x02"])
    tts = await endpoint(service)

    await collect(tts.stream(tokens("R", "iga", " is", "", " the capital.")))

    texts = [m["text"] for m in service.received]
    assert texts == [" ", "R", "iga", " is", " the capital.", ""]
    assert service.received[0]["generation_config"] == {
        "chunk_length_schedule": CHUNK_LENGTH_SCHEDULE
    }
    assert "" not in texts[:-1], "an empty fragment would have ended the stream early"


def test_buffering_is_left_on_not_switched_off_by_auto_mode() -> None:
    """`auto_mode` speaks every message the moment it arrives: fed tokens, a
    33-second reply came out 60 seconds long, one token at a time."""
    tts = ElevenLabsTTS(api_key="k", client=SimpleNamespace())  # type: ignore[arg-type]

    assert "auto_mode" not in tts.url
    assert "output_format=pcm_24000" in tts.url
    assert f"/{DEFAULT_VOICE}/stream-input" in tts.url
    assert CHUNK_LENGTH_SCHEDULE[0] >= 50, "the service rejects values under 50"
    assert all(50 <= n <= 500 for n in CHUNK_LENGTH_SCHEDULE)


async def test_audio_comes_back_in_whole_samples_and_the_key_in_a_header(endpoint: Serve) -> None:
    service = StreamInput(parts=[b"\x01\x02\x03", b"\x04"])
    tts = await endpoint(service)

    out = await collect(tts.stream(once("hello")))

    assert out == [b"\x01\x02", b"\x03\x04"]
    assert service.headers == ["test-key"]


async def test_audio_arrives_while_text_is_still_being_sent(endpoint: Serve) -> None:
    service = StreamInput(eager=True)
    tts = await endpoint(service)
    heard_before_the_end: list[bytes] = []
    finished = False

    async def slow_text() -> AsyncIterator[str]:
        nonlocal finished
        yield "ab"
        await asyncio.sleep(0.2)
        yield "cd"
        finished = True

    async for chunk in tts.stream(slow_text()):
        if not finished:
            heard_before_the_end.append(chunk.pcm)

    assert heard_before_the_end == [b"ab"]


async def test_an_error_payload_is_a_provider_error(endpoint: Serve) -> None:
    async def refusing(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.send(json.dumps({"message": "quota gone", "code": "quota_exceeded"}))
        await connection.close()

    tts = await endpoint(refusing)

    with pytest.raises(ProviderError, match="character quota"):
        await collect(tts.stream(once("hello")))


async def test_a_connection_lost_mid_stream_is_a_provider_error(endpoint: Serve) -> None:
    """Unmapped, a dropped socket is not a VoiceAgentError, and the turn could
    not fall back to text."""

    async def dropping(connection: ServerConnection) -> None:
        await connection.recv()
        await send_audio(connection, b"\x01\x02")
        connection.transport.abort()

    tts = await endpoint(dropping)

    with pytest.raises(ProviderError, match="interrupted"):
        await collect(tts.stream(tokens("hello", pace=0.1)))


async def test_a_refused_handshake_says_what_to_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured: an unknown voice is a bare 403 with an empty body."""

    def forbid(connection: ServerConnection, request: object) -> Response:
        return Response(403, "Forbidden", Headers(), b"")

    async def unused(connection: ServerConnection) -> None: ...

    async with serve(unused, "127.0.0.1", 0, process_request=forbid) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(elevenlabs_tts, "ENDPOINT", f"ws://127.0.0.1:{port}")
        tts = ElevenLabsTTS(api_key="k", client=SimpleNamespace())  # type: ignore[arg-type]

        with pytest.raises(ProviderError, match=r"403.*--list-voices"):
            await collect(tts.stream(once("hello")))


async def test_an_unreachable_service_is_a_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(elevenlabs_tts, "ENDPOINT", "ws://127.0.0.1:1")
    tts = ElevenLabsTTS(api_key="k", client=SimpleNamespace())  # type: ignore[arg-type]

    with pytest.raises(ProviderError):
        await collect(tts.stream(once("hello")))


async def test_a_reader_that_stops_closes_the_socket(endpoint: Serve) -> None:
    """A cancelled turn stops reading between chunks. The socket — and the text
    still flowing into it — must close with it, not wait for collection."""
    service = StreamInput(eager=True)
    tts = await endpoint(service)

    async def unfinished() -> AsyncIterator[str]:
        yield "more "
        await asyncio.sleep(2)  # a reply still being written when the turn is cancelled

    async with closing(tts.stream(unfinished())) as stream:
        await anext(stream)
        stopped = time.perf_counter()

    # Timed rather than wrapped in a timeout: a timeout's cancellation lands in
    # the very cleanup under test, which swallows it and looks like success.
    assert time.perf_counter() - stopped < 0.5, "closing waited on text that never came"
    async with asyncio.timeout(1):
        await service.closed.wait()


class FakeStreamingResponse:
    def __init__(self, parts: list[bytes], drop_after: int | None = None) -> None:
        self._parts = parts
        self._drop_after = drop_after

    async def __aenter__(self) -> "FakeStreamingResponse":
        return self

    async def __aexit__(self, *_: object) -> None: ...

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        for index, part in enumerate(self._parts):
            if index == self._drop_after:
                raise httpx2.RemoteProtocolError("peer closed connection")
            yield part


def openai_client(create: object) -> SimpleNamespace:
    streaming = SimpleNamespace(create=create)
    return SimpleNamespace(
        audio=SimpleNamespace(speech=SimpleNamespace(with_streaming_response=streaming))
    )


async def test_openai_streams_pcm_in_whole_samples() -> None:
    calls: list[dict[str, object]] = []

    def create(**kwargs: object) -> FakeStreamingResponse:
        calls.append(kwargs)
        return FakeStreamingResponse([b"\x01", b"\x02\x03\x04\x05", b"\x06"])

    client = openai_client(create)
    out = await collect(OpenAITTS(client=client).stream(tokens("hel", "lo")))  # type: ignore[arg-type]

    assert b"".join(out) == b"\x01\x02\x03\x04\x05\x06"
    assert all(len(chunk) % 2 == 0 for chunk in out)
    assert calls[0]["response_format"] == "pcm"
    # It has no streaming input: the text is gathered and sent once, whole.
    assert [c["input"] for c in calls] == ["hello"]


async def test_openai_is_not_asked_to_speak_a_blank_text() -> None:
    calls: list[dict[str, object]] = []

    def create(**kwargs: object) -> FakeStreamingResponse:
        calls.append(kwargs)
        return FakeStreamingResponse([b"\x00\x00"])

    out = await collect(OpenAITTS(client=openai_client(create)).stream(tokens(" ", "")))  # type: ignore[arg-type]

    assert out == [] and calls == []


async def test_an_openai_connection_lost_mid_stream_is_a_provider_error() -> None:
    def create(**_: object) -> FakeStreamingResponse:
        return FakeStreamingResponse([b"\x01\x02", b"\x03\x04"], drop_after=1)

    with pytest.raises(ProviderError, match="interrupted"):
        await collect(OpenAITTS(client=openai_client(create)).stream(once("hello")))  # type: ignore[arg-type]


async def test_the_service_s_character_timing_comes_with_the_audio(endpoint: Serve) -> None:
    """Measured live: `alignment` spells out the text as sent, timed from the
    start of the audio message it arrives on, and most messages carry none."""

    async def timing(connection: ServerConnection) -> None:
        async for raw in connection:
            if json.loads(raw)["text"] == "":
                timed = {
                    "chars": ["H", "i", " "],
                    "charStartTimesMs": [0, 50, 120],
                    "charDurationsMs": [50, 70, 30],
                }
                # What the service also sends, and what must not be read: its
                # rewritten spelling.
                await send_audio(connection, b"\x01\x02", timed)
                await send_audio(connection, b"\x03\x04")
                await connection.send(json.dumps({"audio": None, "isFinal": True}))

    tts = await endpoint(timing)
    chunks = [chunk async for chunk in tts.stream(once("Hi "))]

    assert chunks[0].alignment == Alignment("Hi ", (50.0, 120.0, 150.0))
    assert chunks[1].alignment is None


def test_malformed_timing_is_ignored_rather_than_trusted() -> None:
    assert elevenlabs_tts.alignment(None) is None
    assert (
        elevenlabs_tts.alignment({"chars": ["a"], "charStartTimesMs": [], "charDurationsMs": []})
        is None
    )
    assert elevenlabs_tts.alignment({"chars": "ab"}) is None
