"""ElevenLabs speech synthesis, fed the reply while it is still being written.

Reached over the `stream-input` WebSocket rather than an HTTP endpoint, which
takes a text that is already whole. Tokens are forwarded exactly as the
reasoning engine writes them — half words, no spaces added — and deciding when
there is enough text to say something aloud is left to the service's chunk
schedule. Measured, it does more than count: past each threshold it speaks a
prefix ending on a word and holds the unfinished tail back, voicing it with
whatever arrives next. Where the reply is cut is not this project's code.

The SDK binds this endpoint only as a blocking, synchronous client, so it is
spoken to directly, as Scribe is.
"""

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Literal
from urllib.parse import urlencode

import websockets
from elevenlabs.client import AsyncElevenLabs
from elevenlabs.core import ApiError

from voice_agent.config import require_env
from voice_agent.errors import ProviderError
from voice_agent.tts.base import Voice, whole_samples

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
    return describe(detail, f"HTTP {exc.status_code}")


def describe(detail: dict[str, object], fallback: str) -> str:
    """One line from an error's `message` and `code`, over HTTP or the socket."""
    message = detail.get("message") or detail.get("error") or fallback
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

OUTPUT_FORMAT: Literal["pcm_24000"] = "pcm_24000"
"""Raw PCM at `tts.base.SAMPLE_RATE`. Asserted equal to it in the tests, since
the two drifting apart would pitch the voice rather than fail."""

ENDPOINT = "wss://api.elevenlabs.io/v1/text-to-speech"

CLOSE_TIMEOUT_SECONDS = 1.0

CHUNK_LENGTH_SCHEDULE = [120, 160, 250, 290]
"""Characters of text the service waits for before speaking each part; the last
value repeats. This is the service's own default, stated so it is a decision.

Started at `[50, 120, 160, 250]` for time to first sound — 223 ms against 346 ms
on a replayed reply. Listened to live, the 50-character first piece was voiced
as an utterance of its own: "The Millennium Prize Problems are seven big…",
then a pause no gap counter sees, because no audio was missing. A reasoning
engine writing ~760 characters a second reaches 120 in about 100 ms more, and a
natural first phrase is worth that.

Not `auto_mode`, which sounds like the smarter choice and is the opposite: it
switches buffering *off* and speaks every message the moment it arrives. Fed
tokens, it voiced each one as an utterance of its own — "Da · ug · ava" — and a
33-second reply came out 60 seconds long. It is for clients that send whole
sentences, which this one does not."""


class ElevenLabsTTS:
    def __init__(
        self,
        voice: str | None = None,
        model: str | None = None,
        client: AsyncElevenLabs | None = None,
        api_key: str | None = None,
    ) -> None:
        self.provider = "elevenlabs"
        self.voice = voice or DEFAULT_VOICE
        self.model = model or DEFAULT_MODEL
        self._api_key = api_key or require_env("ELEVENLABS_API_KEY")
        self._client = client or AsyncElevenLabs(api_key=self._api_key)

    @property
    def url(self) -> str:
        return f"{ENDPOINT}/{self.voice}/stream-input?" + urlencode(
            {"model_id": self.model, "output_format": OUTPUT_FORMAT}
        )

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        async def pump(socket: websockets.ClientConnection) -> None:
            # A single space opens the stream and carries its settings.
            await socket.send(
                json.dumps(
                    {
                        "text": " ",
                        "generation_config": {"chunk_length_schedule": CHUNK_LENGTH_SCHEDULE},
                    }
                )
            )
            async for fragment in text:
                # Never an empty text: that is the message which ends the stream.
                if fragment:
                    await socket.send(json.dumps({"text": fragment}))
            # "That is all": the service voices whatever it is still holding back,
            # however short, then ends the stream.
            await socket.send(json.dumps({"text": ""}))

        try:
            async with websockets.connect(
                self.url,
                additional_headers={"xi-api-key": self._api_key},
                # A cancelled synthesis closes the socket and would wait this
                # long for the service to answer — holding up the turn, and so
                # `ended`, that cancelled it. The default is 10 s, and this
                # vendor's Scribe endpoint never answers a close at all.
                close_timeout=CLOSE_TIMEOUT_SECONDS,
            ) as socket:
                task = asyncio.create_task(pump(socket))
                try:
                    async for chunk in whole_samples(self._audio(socket)):
                        yield chunk
                finally:
                    # A turn that stops reading — cancelled, or failed — must
                    # not leave text flowing into a socket that is closing.
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, websockets.WebSocketException):
                        await task
        except websockets.InvalidStatus as exc:
            # Refused at the handshake with an empty body, so the status is all
            # there is to go on. Measured: an unknown voice id is a bare 403.
            raise ProviderError(
                f"elevenlabs refused the stream (HTTP {exc.response.status_code}) — check the "
                "voice id with --list-voices and the key's text-to-speech permission"
            ) from exc
        except (websockets.WebSocketException, OSError) as exc:
            raise ProviderError(f"elevenlabs synthesis interrupted: {exc}") from exc

    async def _audio(self, socket: websockets.ClientConnection) -> AsyncIterator[bytes]:
        async for raw in socket:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            if audio := payload.get("audio"):
                yield base64.b64decode(audio)
            elif "error" in payload or "message" in payload:
                raise ProviderError(describe(payload, "the stream was refused"))
            if payload.get("isFinal"):
                return

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
