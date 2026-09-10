"""ElevenLabs speech synthesis."""

from elevenlabs.client import AsyncElevenLabs
from elevenlabs.core import ApiError

from voice_agent.config import require_env
from voice_agent.errors import ProviderError
from voice_agent.tts.base import AudioClip, Voice

HINTS = {
    "paid_plan_required": (
        "that voice is a Voice Library voice, which free accounts cannot use via the API. "
        f"Try a stock voice, e.g. --voice {'EXAVITQu4vr4xnSDxMaL'} (Sarah)."
    ),
    "missing_permissions": (
        "the API key lacks a permission this call needs — enable it on the key "
        "in the ElevenLabs dashboard (Profile -> API Keys)."
    ),
    "quota_exceeded": "the account's monthly character quota is exhausted.",
}
"""ElevenLabs' own messages say what went wrong but not what to do about it,
and the SDK's exception stringifies the entire response including every header.
Neither belongs in front of a user."""


def explain(exc: ApiError) -> str:
    """Turn an SDK exception into one actionable line."""
    detail = exc.body.get("detail", {}) if isinstance(exc.body, dict) else {}
    if not isinstance(detail, dict):
        return f"elevenlabs synthesis failed ({exc.status_code}): {detail}"
    message = detail.get("message") or f"HTTP {exc.status_code}"
    hint = HINTS.get(str(detail.get("code")))
    return f"elevenlabs synthesis failed: {message}" + (f" — {hint}" if hint else "")


DEFAULT_VOICE = "EXAVITQu4vr4xnSDxMaL"
"""Sarah, confirmed `category == "premade"` against a real free account.

Chosen empirically, not from documentation. Rachel (21m00Tcm4TlvDq8ikWAM) and
Aria (9BWtsMINqrJLrRacOk9x) are both widely recommended as safe stock voices —
Aria is ElevenLabs' own current in-app default — and neither is in the premade
set at all; both fail with 402 `paid_plan_required` on a free plan. Run
`uv run voice-agent --list-voices` rather than trusting any id written down
anywhere, this one included."""

USABLE_CATEGORY = "premade"
"""Voice Library voices are visible to a free account but not synthesizable by
one, so listing everything the API returns would reproduce the original bug in
a new place."""

DEFAULT_MODEL = "eleven_flash_v2_5"
"""The low-latency model (~75 ms to first byte) rather than the highest-quality
one. This is a voice agent: a reply that sounds slightly better but lands half
a second later is the worse trade."""

OUTPUT_FORMAT = "mp3_44100_128"
MEDIA_TYPE = "audio/mpeg"
BITS_PER_SECOND = 128_000
"""Constant-bitrate MP3, so the clip's duration follows from its size."""


class ElevenLabsTTS:
    def __init__(
        self,
        voice: str | None = None,
        model: str | None = None,
        client: AsyncElevenLabs | None = None,
    ) -> None:
        self.provider = "elevenlabs"
        self.voice = voice or DEFAULT_VOICE
        self.model = model or DEFAULT_MODEL
        self._client = client or AsyncElevenLabs(api_key=require_env("ELEVENLABS_API_KEY"))

    async def synthesize(self, text: str) -> AudioClip:
        try:
            # `convert` yields chunks even for a batched request — the response
            # is chunked HTTP. Joining them here is what makes this batched:
            # nothing downstream sees a fragment until the clip is whole.
            chunks = [
                chunk
                async for chunk in self._client.text_to_speech.convert(
                    voice_id=self.voice,
                    model_id=self.model,
                    text=text,
                    output_format=OUTPUT_FORMAT,
                )
            ]
        except ApiError as exc:
            raise ProviderError(explain(exc)) from exc
        data = b"".join(chunks)
        return AudioClip(data=data, media_type=MEDIA_TYPE, seconds=len(data) * 8 / BITS_PER_SECOND)

    async def list_voices(self) -> list[Voice]:
        try:
            response = await self._client.voices.get_all()
        except ApiError as exc:
            raise ProviderError(explain(exc)) from exc
        return [
            Voice(
                id=voice.voice_id,
                name=voice.name or voice.voice_id,
                usable=voice.category == USABLE_CATEGORY,
            )
            for voice in response.voices
        ]
