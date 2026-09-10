"""OpenAI speech synthesis — the second backend, which is the point of it.

An interface with one implementation is a guess. This one differs from
ElevenLabs in every detail that matters (voices are names rather than ids, the
format is a separate parameter, the response is a binary body rather than a
chunk stream), which is what makes the `TTS` protocol worth having.
"""

from typing import Literal

from openai import AsyncOpenAI, OpenAIError

from voice_agent.config import require_env
from voice_agent.errors import ProviderError
from voice_agent.tts.base import AudioClip, Voice

DEFAULT_VOICE = "alloy"
DEFAULT_MODEL = "gpt-4o-mini-tts"
RESPONSE_FORMAT: Literal["mp3"] = "mp3"
MEDIA_TYPE = "audio/mpeg"

VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
)
"""A fixed set with no plan tiers behind it, so this needs no API call — the
asymmetry with ElevenLabs is exactly why `list_voices` belongs on the backend
rather than in one shared helper."""


class OpenAITTS:
    def __init__(
        self,
        voice: str | None = None,
        model: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.provider = "openai"
        self.voice = voice or DEFAULT_VOICE
        self.model = model or DEFAULT_MODEL
        self._client = client or AsyncOpenAI(api_key=require_env("OPENAI_API_KEY"))

    async def synthesize(self, text: str) -> AudioClip:
        try:
            response = await self._client.audio.speech.create(
                model=self.model,
                # The SDK's type narrows to its stock voice names; the API
                # itself accepts any voice string the account has.
                voice=self.voice,
                input=text,
                response_format=RESPONSE_FORMAT,
            )
            data = await response.aread()
        except OpenAIError as exc:
            raise ProviderError(f"openai synthesis failed: {exc}") from exc
        return AudioClip(data=data, media_type=MEDIA_TYPE)

    async def list_voices(self) -> list[Voice]:
        return [Voice(id=name, name=name, usable=True) for name in VOICES]
