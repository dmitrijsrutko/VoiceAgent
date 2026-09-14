"""OpenAI speech synthesis — the second backend, which is the point of it.

An interface with one implementation is a guess. This one differs from
ElevenLabs in every detail that matters (voices are names rather than ids, the
format is a separate parameter, streaming is a response wrapper rather than a
separate endpoint), which is what makes the `TTS` protocol worth having.

Crude on purpose since Chapter 7: `/audio/speech` takes whole text only, so the
reply is gathered in full and synthesized after it has been written — the
batched wait that ElevenLabs no longer has. Sentence-sized requests, cut on our
side, would fix it; nothing here needs that yet.
"""

from collections.abc import AsyncIterator
from typing import Literal

import httpx
from openai import AsyncOpenAI, OpenAIError

from voice_agent.config import require_env
from voice_agent.errors import ProviderError
from voice_agent.tts.base import Voice, whole_samples

DEFAULT_VOICE = "alloy"
DEFAULT_MODEL = "gpt-4o-mini-tts"
RESPONSE_FORMAT: Literal["pcm"] = "pcm"
"""OpenAI's `pcm` has no rate parameter: it is always 24 kHz, 16-bit signed
little-endian — which is why `tts.base.SAMPLE_RATE` is 24 kHz."""

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

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        whole = "".join([fragment async for fragment in text])
        if not whole.strip():
            return
        try:
            # `with_streaming_response`, because plain `create` reads the whole
            # body before returning — which is the batched behaviour, renamed.
            async with self._client.audio.speech.with_streaming_response.create(
                model=self.model,
                # The SDK's type narrows to its stock voice names; the API
                # itself accepts any voice string the account has.
                voice=self.voice,
                input=whole,
                response_format=RESPONSE_FORMAT,
            ) as response:
                async for chunk in whole_samples(response.iter_bytes()):
                    yield chunk
        except OpenAIError as exc:
            raise ProviderError(f"openai synthesis failed: {exc}") from exc
        except httpx.HTTPError as exc:
            # The SDK wraps failures to *connect*, but `iter_bytes` reads the
            # body straight from httpx: a connection lost mid-reply arrives raw.
            raise ProviderError(f"openai synthesis interrupted: {exc!r}") from exc

    async def list_voices(self) -> list[Voice]:
        return [Voice(id=name, name=name, usable=True) for name in VOICES]
