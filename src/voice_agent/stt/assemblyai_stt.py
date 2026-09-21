"""AssemblyAI Universal-Streaming speech recognition, over the v3 WebSocket.

Reached over a raw WebSocket rather than through the `assemblyai` SDK, for the
same reason Scribe is: the protocol is four message types, and a callback-based
client bridged back into an async iterator would be more code than the protocol
it hides — plus a dependency this project does not otherwise need.

Endpointing is delegated to the service, as it was in chapter 3. The difference
is the unit: Scribe takes one silence threshold in seconds, AssemblyAI takes a
*window* in milliseconds — `min_turn_silence` before it may end a turn it is
confident about, `max_turn_silence` before it ends one regardless. The single
`--vad-silence` knob maps onto the ceiling of that window, so the flag keeps
meaning what it has always meant no matter which ears are listening.

Two things here are cost control rather than correctness, and both are easy to
leave out by accident:

- Audio goes up as **binary frames**. Scribe wants base64 inside JSON and the
  Voice Agent API wants base64 inside JSON; this endpoint wants neither, and
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

Written down because the failure mode for anything outside this set is not an
error — it is confident nonsense. Measured: Russian read aloud came back as
"Раскажем не pravalo вывnutriny produkt kitaia и государствены dolk.", and the
agent answered the nonsense in English. Passing `language_codes=ru` changes
nothing; it is accepted at connect and silently ignored.

For a language that is not here, use `--stt elevenlabs`: Scribe covers it and
keeps partials, punctuation and `--vad-silence` working. AssemblyAI's own
`whisper-rt` model does cover 99 languages, but emits **no partial transcripts
at all** — which switches off warming (ch. 4), speculation (ch. 5) and the live
transcript, and makes `--vad-silence` inert. It is not a drop-in and is not
offered here."""

SAMPLE_RATE = 16000
"""What we ask for, within the endpoint's 8000-96000 range. The browser opens
its AudioContext at whatever `sample_rate` the server reports for the chosen
ears, so nothing resamples anywhere."""

ENCODING = "pcm_s16le"
"""16-bit signed little-endian, mono — what the capture worklet already sends."""

DEFAULT_SILENCE_SECONDS = 1.5
"""How long a pause means "I'm done", carried over from chapter 3 rather than
re-derived. The argument there still holds: 0.7 s was tried against Scribe and
was worse, committing "Or rather..." as a finished turn. Keeping the same
default across both backends is also what makes the two comparable — a latency
difference measured between them is then the recognizer's, not the threshold's.
Tune with VOICE_AGENT_VAD_SILENCE / --vad-silence."""

SILENCE_FLOOR_MS = 50
SILENCE_CEILING_MS = 10_000
"""The service's own clamp on both turn-silence bounds. Applied here so a wild
--vad-silence is corrected before it becomes a 3006, not after."""

MIN_SILENCE_FRACTION = 0.5
"""Where the floor of the turn-silence window sits relative to its ceiling.

The service may end a turn any time after `min_turn_silence` if it is confident
the speaker is finished, and must end it by `max_turn_silence`. Measured
against the real service on an unfinished phrase, `min_turn_silence` is the one
that governs in practice and it tracks closely: 200 ms -> 830 ms to the final,
900 ms -> 1453 ms, 2500 ms -> 1869 ms. The ceiling never fired in any test,
because a sentence that sounds finished ends the turn confidently first.

Half, therefore, is what makes `--vad-silence 1.5` actually feel like a second
and a half: the floor lands at 750 ms and the final at roughly 1.3 s. Passing
the whole value as the floor would overshoot by half a second."""

FLUSH_GRACE_SECONDS = 2.0
"""After the audio ends and `Terminate` goes up, how long to wait for a last
turn before closing the socket ourselves. Usually unused: the service answers
`Terminate` with `Termination` well inside this, and that is what actually ends
the loop."""

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
    """One actionable line from whatever the socket died of.

    Read off `rcvd`, the close frame the *service* sent, rather than the
    exception's own `code` — that shortcut is deprecated (websockets 13.1) and
    warns on every use. A failure with no close frame from the peer simply has
    no hint, which is correct: the codes above are things the service said.
    """
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
        # Set before we close the socket ourselves, so an orderly shutdown is
        # not reported to the user as a failed transcription — the bug chapter
        # 3 shipped against Scribe, which this path would otherwise repeat.
        closing = asyncio.Event()

        async def pump(socket: websockets.ClientConnection) -> None:
            async for data in audio:
                await socket.send(data)  # binary frame: no envelope, no base64
            # The audio ran out mid-utterance (the user stopped listening rather
            # than stopping speaking). Terminate rather than wait for a VAD
            # pause that will never arrive — and so the session stops billing.
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
                # The raw key, with no `Bearer` prefix. The prefix is required
                # on exactly one AssemblyAI product — the Voice Agent API — and
                # generalising either way is how this returns a 1008.
                self.url,
                additional_headers={"Authorization": self._api_key},
            ) as socket:
                task = asyncio.create_task(pump(socket))
                try:
                    async for transcript in self._turns(socket):
                        yield transcript
                finally:
                    # A listening session that stops reading must not leave
                    # audio flowing into a socket that is closing.
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
        """What the user has said, up to the service's own goodbye.

        `finalized` is local to the session on purpose. `turn_order` counts from
        zero again on every connection, so remembering it across sessions would
        make the first turn after a reconnect look like one already answered.
        """
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
                # The accepted configuration, traced because an unrecognised
                # query parameter here is *ignored* rather than refused.
                # It reports the model and mode but **not** the turn-silence
                # bounds — measured, those come back absent even when they are
                # demonstrably in effect — so this documents what was accepted,
                # it does not confirm the pause landed. Only timing does that.
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

            # One final per turn, however many the service sends. A turn can
            # arrive twice — once as it ends, again once formatted — and the
            # second would drive a whole extra turn through the pipeline: the
            # agent answering the same sentence twice. Downstream cannot tell
            # them apart, so they are collapsed here, where `turn_order` still
            # exists.
            order = payload.get("turn_order")
            if isinstance(order, int):
                if order in finalized:
                    continue
                finalized.add(order)
            trace.event("stt.committed", {"text": text})
            yield Transcript(text=text, is_final=True)
