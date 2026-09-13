from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from elevenlabs.core import ApiError

from voice_agent.errors import ConfigError, ProviderError
from voice_agent.tts import create_tts
from voice_agent.tts.base import SAMPLE_RATE, pcm_seconds, whole_samples
from voice_agent.tts.elevenlabs_tts import DEFAULT_VOICE, OUTPUT_FORMAT, ElevenLabsTTS, explain
from voice_agent.tts.openai_tts import RESPONSE_FORMAT, OpenAITTS


async def chunks_of(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def collect(stream: AsyncIterator[bytes]) -> list[bytes]:
    return [chunk async for chunk in stream]


async def test_no_chunk_splits_a_sample() -> None:
    """HTTP chunking knows nothing about 16-bit samples. A chunk played with a
    dangling byte shifts every sample after it: the rest of the reply is noise."""
    out = await collect(whole_samples(chunks_of(b"abc", b"d", b"e", b"fgh", b"")))

    assert all(len(chunk) % 2 == 0 for chunk in out)
    assert b"".join(out) == b"abcdefgh", "a byte was lost or reordered"
    assert b"" not in out, "an empty chunk would announce audio that is not there"


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
    voices = await ElevenLabsTTS(client=client).list_voices()  # type: ignore[arg-type]

    assert [(v.id, v.usable) for v in voices] == [
        ("stock-1", True),
        ("lib-1", False),
        ("mine-1", False),
    ]


async def test_a_voice_with_no_name_still_lists() -> None:
    client = FakeVoicesClient([SimpleNamespace(voice_id="v1", name=None, category="premade")])
    assert (await ElevenLabsTTS(client=client).list_voices())[0].name == "v1"  # type: ignore[arg-type]


async def test_openai_voices_need_no_api_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The asymmetry with ElevenLabs — a fixed set with no plan tiers behind
    it — is why list_voices belongs on the backend, not in a shared helper."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    voices = await OpenAITTS().list_voices()

    assert all(v.usable for v in voices)
    assert "alloy" in {v.id for v in voices}


class FakeElevenLabs:
    """`text_to_speech.stream` as the SDK shapes it: a call that returns an
    async iterator, and does its request — and fails — on iteration."""

    def __init__(
        self, parts: list[bytes], fail_after: int | None = None, drop_after: int | None = None
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.text_to_speech = SimpleNamespace(stream=self._stream)
        self._parts = parts
        self._fail_after = fail_after
        self._drop_after = drop_after

    def _stream(self, voice_id: str, **kwargs: object) -> AsyncIterator[bytes]:
        self.calls.append({"voice_id": voice_id, **kwargs})
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        for index, part in enumerate(self._parts):
            if index == self._fail_after:
                raise FakeApiError(429, {"detail": {"code": "quota_exceeded", "message": "no"}})
            if index == self._drop_after:
                raise httpx.ReadError("connection reset")
            yield part


async def test_elevenlabs_streams_pcm_in_whole_samples() -> None:
    client = FakeElevenLabs([b"\x01\x02\x03", b"\x04"])
    out = await collect(ElevenLabsTTS(client=client).stream("hello"))  # type: ignore[arg-type]

    assert out == [b"\x01\x02", b"\x03\x04"]
    assert client.calls[0]["output_format"] == "pcm_24000"
    assert client.calls[0]["text"] == "hello"


async def test_an_elevenlabs_failure_mid_stream_is_explained() -> None:
    """The request happens on iteration, so a failure can arrive after audio
    has already been handed on — it must still surface as a ProviderError."""
    client = FakeElevenLabs([b"\x01\x02", b"\x03\x04"], fail_after=1)
    received: list[bytes] = []

    with pytest.raises(ProviderError, match="character quota"):
        async for chunk in ElevenLabsTTS(client=client).stream("hello"):  # type: ignore[arg-type]
            received.append(chunk)

    assert received == [b"\x01\x02"]


async def test_an_elevenlabs_connection_lost_mid_stream_is_a_provider_error() -> None:
    """The SDK passes transport failures through as raw httpx errors. One that
    escapes as anything but a ProviderError takes the connection down with it
    rather than letting the turn fall back to text."""
    client = FakeElevenLabs([b"\x01\x02", b"\x03\x04"], drop_after=1)

    with pytest.raises(ProviderError, match="interrupted"):
        await collect(ElevenLabsTTS(client=client).stream("hello"))  # type: ignore[arg-type]


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
                raise httpx.RemoteProtocolError("peer closed connection")
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
    out = await collect(OpenAITTS(client=client).stream("hello"))  # type: ignore[arg-type]

    assert b"".join(out) == b"\x01\x02\x03\x04\x05\x06"
    assert all(len(chunk) % 2 == 0 for chunk in out)
    assert calls[0]["response_format"] == "pcm"


async def test_an_openai_connection_lost_mid_stream_is_a_provider_error() -> None:
    def create(**_: object) -> FakeStreamingResponse:
        return FakeStreamingResponse([b"\x01\x02", b"\x03\x04"], drop_after=1)

    with pytest.raises(ProviderError, match="interrupted"):
        await collect(OpenAITTS(client=openai_client(create)).stream("hello"))  # type: ignore[arg-type]
