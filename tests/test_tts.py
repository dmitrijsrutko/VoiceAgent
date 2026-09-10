from types import SimpleNamespace

import pytest
from elevenlabs.core import ApiError

from voice_agent.errors import ConfigError
from voice_agent.tts import AudioClip, create_tts
from voice_agent.tts.elevenlabs_tts import DEFAULT_VOICE, ElevenLabsTTS, explain
from voice_agent.tts.openai_tts import OpenAITTS


def test_a_clip_carries_the_media_type_the_browser_needs() -> None:
    clip = AudioClip(data=b"1234", media_type="audio/mpeg")
    assert len(clip) == 4
    assert clip.media_type == "audio/mpeg"


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
