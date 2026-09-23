"""AssemblyAI Universal-Streaming speech recognition, over the v3 WebSocket.

A raw WebSocket rather than the SDK: the protocol is four message types, less
code than bridging the SDK's callbacks into an async iterator.

Endpointing is the service's. `--vad-silence` maps onto its turn-silence
*window* (milliseconds), so the flag means the same on both recognizers.

Two things here are cost control rather than correctness:

- Audio goes up as **binary frames** (not base64 in JSON, unlike Scribe);
  frames outside 50-1000 ms of audio close the socket with 3007.
- The session is **explicitly terminated**. An abandoned socket keeps billing
  until the three-hour cap, so `Terminate` is sent when the audio runs out.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import websockets

from voice_agent import trace
from voice_agent.config import require_env
from voice_agent.errors import ProviderError
from voice_agent.stt.base import Transcript

ENDPOINT = "wss://streaming.assemblyai.com/v3/ws"

DEFAULT_MODEL = "universal-3-5-pro"
"""Singular `speech_model`, a *string*. The pre-recorded API takes a plural
`speech_models` **array** and treats it as an ordered fallback list; streaming
takes neither. Passing the array shape here is the most common way to get this
endpoint wrong."""

LANGUAGES = (
    "en",
    "es",
    "fr",
    "de",
    "it",
    "pt",
    "tr",
    "nl",
    "sv",
    "no",
    "da",
    "fi",
    "hi",
    "vi",
    "ar",
    "he",
    "ja",
    "zh",
)
"""The 18 languages `universal-3-5-pro` transcribes, as ISO 639-1 codes.

Anything else is not refused but transcribed as confident nonsense, and
`language_codes` is silently ignored. Use `--stt elevenlabs` for other
languages; `whisper-rt` covers more but emits no partials at all."""

SAMPLE_RATE = 16000
"""What we ask for; the page opens its microphone at this rate, so nothing resamples."""

ENCODING = "pcm_s16le"
"""16-bit signed little-endian, mono — what the capture worklet already sends."""

DEFAULT_SILENCE_SECONDS = 1.5
"""How long a pause means "I'm done": the same default as Scribe, so the two
recognizers compare on their own latency. `--vad-silence` tunes it."""

SILENCE_FLOOR_MS = 50
SILENCE_CEILING_MS = 10_000
"""The service's own clamp on both turn-silence bounds. Applied here so a wild
--vad-silence is corrected before it becomes a 3006, not after."""

MIN_SILENCE_FRACTION = 0.5
"""Where the floor of the turn-silence window sits relative to its ceiling.

The floor is what governs in practice (measured: 200 ms -> 830 ms to the final,
900 ms -> 1453 ms), so half makes `--vad-silence 1.5` feel like 1.5 s."""

FLUSH_GRACE_SECONDS = 2.0
"""After `Terminate`, how long to wait for a last turn before closing the
socket ourselves. Usually the service's `Termination` ends the loop first."""

BEGIN = "Begin"
TURN = "Turn"
TERMINATION = "Termination"

CLOSE_HINTS = {
    1008: "check ASSEMBLYAI_API_KEY — the key was missing or rejected.",
    3005: "the service cancelled the session.",
    3006: "the service rejected a message as malformed.",
    3007: "an audio chunk was outside the 50-1000 ms window, or arrived faster than real time.",
    3008: "the session hit the three-hour cap.",
    3009: "too many concurrent streaming sessions on this account.",
}
"""The close code *is* the error on this endpoint — it rarely sends an error
payload — and the codes alone say nothing about what to do."""


def explain(exc: Exception) -> str:
    """One actionable line from whatever the socket died of. Read off `rcvd`,
    the close frame the service sent (`exc.code` is deprecated)."""
    code = getattr(getattr(exc, "rcvd", None), "code", None)
    hint = CLOSE_HINTS.get(code) if isinstance(code, int) else None
    return f"assemblyai transcription failed: {exc}" + (f" — {hint}" if hint else "")


def clamp(milliseconds: float) -> int:
    return int(min(max(round(milliseconds), SILENCE_FLOOR_MS), SILENCE_CEILING_MS))


class AssemblyAISTT:
    def __init__(
        self,
        model: str | None = None,
        silence_seconds: float | None = None,
        api_key: str | None = None,
    ) -> None:
        self.provider = "assemblyai"
        self.model = model or DEFAULT_MODEL
        self.sample_rate = SAMPLE_RATE
        self.languages = LANGUAGES
        self.silence_seconds = silence_seconds or DEFAULT_SILENCE_SECONDS
        self._api_key = api_key or require_env("ASSEMBLYAI_API_KEY")

    @property
    def max_turn_silence_ms(self) -> int:
        """The configured pause, in the unit this service speaks."""
        return clamp(self.silence_seconds * 1000)

    @property
    def min_turn_silence_ms(self) -> int:
        return clamp(self.max_turn_silence_ms * MIN_SILENCE_FRACTION)

    @property
    def url(self) -> str:
        return f"{ENDPOINT}?" + urlencode(
            {
                "speech_model": self.model,
                "sample_rate": self.sample_rate,
                "encoding": ENCODING,
                "min_turn_silence": self.min_turn_silence_ms,
                "max_turn_silence": self.max_turn_silence_ms,
            }
        )

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        # Set before closing the socket ourselves: an orderly shutdown is not a failure.
        closing = asyncio.Event()

        async def pump(socket: websockets.ClientConnection) -> None:
            async for data in audio:
                await socket.send(data)  # binary frame: no envelope, no base64
            # Audio over (listening stopped): terminate, which also stops billing.
            with contextlib.suppress(websockets.WebSocketException):
                await socket.send(json.dumps({"type": "Terminate"}))
                await asyncio.sleep(FLUSH_GRACE_SECONDS)
                closing.set()
                await socket.close()

        trace.event(
            "stt.session",
            {
                "provider": self.provider,
                "sample_rate": self.sample_rate,
                "max_turn_silence_ms": self.max_turn_silence_ms,
            },
        )
        try:
            async with websockets.connect(
                # The raw key, no `Bearer` (only the Voice Agent API wants one).
                self.url,
                additional_headers={"Authorization": self._api_key},
            ) as socket:
                task = asyncio.create_task(pump(socket))
                try:
                    async for transcript in self._turns(socket):
                        yield transcript
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        except websockets.InvalidStatus as exc:
            raise ProviderError(
                f"assemblyai refused the stream (HTTP {exc.response.status_code}) — "
                "check ASSEMBLYAI_API_KEY and that the account has streaming enabled"
            ) from exc
        except (websockets.WebSocketException, OSError) as exc:
            if closing.is_set():
                return  # our own orderly shutdown, not a failure
            raise ProviderError(explain(exc)) from exc

    async def _turns(self, socket: websockets.ClientConnection) -> AsyncIterator[Transcript]:
        """What the user has said, up to the service's own goodbye. `finalized`
        is per session: `turn_order` restarts at zero on every connection."""
        finalized: set[int] = set()
        async for raw in socket:
            if isinstance(raw, bytes):
                continue
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            if payload.get("error"):
                raise ProviderError(f"assemblyai transcription failed: {payload['error']}")

            kind = payload.get("type")
            if kind == BEGIN:
                # Traced because unknown parameters are ignored, not refused. It
                # omits the turn-silence bounds even when they are in effect.
                trace.event("stt.begin", {k: v for k, v in payload.items() if k != "type"})
                continue
            if kind == TERMINATION:
                return
            if kind != TURN:
                continue  # SpeechStarted and friends: nothing downstream acts on them

            text = str(payload.get("transcript", ""))
            if not payload.get("end_of_turn"):
                trace.event("stt.partial", {"text": text})
                yield Transcript(text=text, is_final=False)
                continue

            # One final per turn: a turn can arrive again once formatted, which
            # would make the agent answer the same sentence twice.
            order = payload.get("turn_order")
            if isinstance(order, int):
                if order in finalized:
                    continue
                finalized.add(order)
            trace.event("stt.committed", {"text": text})
            yield Transcript(text=text, is_final=True)
