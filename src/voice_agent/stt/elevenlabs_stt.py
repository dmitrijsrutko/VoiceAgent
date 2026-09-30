"""ElevenLabs Scribe realtime speech recognition.

Reached over a raw WebSocket rather than through the SDK: the SDK ships the
*types* for this endpoint (`PartialTranscriptPayload`, `CommittedTranscriptPayload`)
but binds no client method to it, so there is nothing to call.

Endpointing is delegated to the service. Connecting with
`commit_strategy=vad` makes Scribe decide when the user's turn has ended, after
`vad_silence_threshold_secs` of silence, and emit a committed transcript.
"""

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import websockets

from voice_agent import trace
from voice_agent.config import require_env
from voice_agent.errors import ProviderError
from voice_agent.stt.base import LanguageHint, Transcript, batched

ENDPOINT = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
DEFAULT_MODEL = "scribe_v2_realtime"
SAMPLE_RATE = 16000
"""What Scribe wants. The browser opens its AudioContext at this rate so no
resampling happens anywhere."""

LANGUAGES = (
    "afr",
    "amh",
    "ara",
    "hye",
    "asm",
    "ast",
    "aze",
    "bel",
    "ben",
    "bos",
    "bul",
    "mya",
    "yue",
    "cat",
    "ceb",
    "nya",
    "hrv",
    "ces",
    "dan",
    "nld",
    "eng",
    "est",
    "fil",
    "fin",
    "fra",
    "ful",
    "glg",
    "lug",
    "kat",
    "deu",
    "ell",
    "guj",
    "hau",
    "heb",
    "hin",
    "hun",
    "isl",
    "ibo",
    "ind",
    "gle",
    "ita",
    "jpn",
    "jav",
    "kea",
    "kan",
    "kaz",
    "khm",
    "kor",
    "kur",
    "kir",
    "lao",
    "lav",
    "lin",
    "lit",
    "luo",
    "ltz",
    "mkd",
    "msa",
    "mal",
    "mlt",
    "zho",
    "mri",
    "mar",
    "mon",
    "nep",
    "nso",
    "nor",
    "oci",
    "ori",
    "pus",
    "fas",
    "pol",
    "por",
    "pan",
    "ron",
    "rus",
    "srp",
    "sna",
    "snd",
    "sin",
    "slk",
    "slv",
    "som",
    "spa",
    "swa",
    "swe",
    "tam",
    "tgk",
    "tel",
    "tha",
    "tur",
    "ukr",
    "umb",
    "urd",
    "uzb",
    "vie",
    "cym",
    "wol",
    "xho",
    "zul",
)
"""The languages Scribe transcribes, as ISO 639-3 codes.

Three-letter, where AssemblyAI's are two-letter. Left in each vendor's own
convention rather than normalised: mapping 639-3 to 639-1 by hand across a
hundred entries is a way to invent a fact, and only one backend is listening at
a time so the two lists never have to line up.

This is the reason `--stt elevenlabs` is the answer for Russian (`rus`, here;
absent from AssemblyAI's eighteen) and for most of the world besides."""

DEFAULT_SILENCE_SECONDS = 1.5
"""How long a pause means "I'm done" — Scribe's own default.

Started at 0.7 s on the theory that Scribe's default was sluggish. It is, but
0.7 s is worse: in a real conversation it committed "Or rather..." and "Not
Ethereum, but rather..." as finished turns and had the agent answer fragments.
Being interrupted mid-thought is a far more obvious failure than waiting an
extra beat.

The cost is real and goes on the latency budget: this is the largest single
term in the round trip and raising it makes it larger. That trade — cut people
off, or feel slow — is exactly what no single threshold can win, and it is the
whole argument for semantic turn detection later. Tune with
VOICE_AGENT_VAD_SILENCE / --vad-silence."""

FLUSH_GRACE_SECONDS = 2.0
"""After the audio ends, how long to wait for a last committed transcript
before closing the socket."""

PARTIAL = "partial_transcript"
COMMITTED = "committed_transcript"
COMMITTED_DETECTED = "committed_transcript_with_timestamps"
"""The commit again, with `language_code`, when language detection is on.
Measured: it can be the only commit sent, so either one is the turn — once."""


def explain(payload: dict[str, object]) -> str:
    kind = str(payload.get("message_type", "error"))
    detail = payload.get("error") or payload.get("message") or payload
    hint = ""
    if "quota" in kind:
        hint = " — the account's monthly character quota is exhausted."
    elif "auth" in kind:
        hint = " — check ELEVENLABS_API_KEY and its permissions."
    return f"elevenlabs transcription failed ({kind}): {detail}{hint}"


class ElevenLabsSTT:
    def __init__(
        self,
        model: str | None = None,
        silence_seconds: float | None = None,
        api_key: str | None = None,
    ) -> None:
        self.provider = "elevenlabs"
        self.model = model or DEFAULT_MODEL
        self.sample_rate = SAMPLE_RATE
        self.languages = LANGUAGES
        self.silence_seconds = silence_seconds or DEFAULT_SILENCE_SECONDS
        self._api_key = api_key or require_env("ELEVENLABS_API_KEY")

    @property
    def url(self) -> str:
        """The session with no language known, as a fresh conversation opens it."""
        return self.session_url()

    def session_url(self, language: LanguageHint | None = None) -> str:
        """One recognizer session's URL.

        **The language hint is an argument, never state.** This adapter is one
        instance shared by every conversation on the server (`Backends.ears`),
        so a `language` attribute was not per conversation: a hint from the
        last conversation that spoke reached the next conversation's *first*
        sentence, which then decoded in a language nobody in the room was
        speaking. It is the reason a conversation newly opened from a shared
        link could produce a sentence of confident nonsense.

        `candidates` goes out as repeated `secondary_languages`, which narrows
        detection to a set — the vendor's description — and still lets the
        recognizer answer with a language outside it. `pin` goes out as
        `language_code`, a strong prior. Both may be sent together; the
        candidate set is what makes a wrong pin survivable.
        """
        query: dict[str, object] = {
            "model_id": self.model,
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": self.silence_seconds,
            "include_language_detection": "true",
        }
        if language is not None:
            if language.pin:
                query["language_code"] = language.pin
            if language.candidates:
                query["secondary_languages"] = list(language.candidates)
        return f"{ENDPOINT}?" + urlencode(query, doseq=True)

    async def stream(
        self, audio: AsyncIterator[bytes], language: LanguageHint | None = None
    ) -> AsyncIterator[Transcript]:
        # Set before we close the socket ourselves. Without it, our own orderly
        # shutdown surfaces as `ConnectionClosedError: sent 1000 (OK); no close
        # frame received` — the peer never answers our close — and gets reported
        # to the user as "transcription failed" at the end of every session.
        closing = asyncio.Event()

        def chunk(data: bytes, commit: bool) -> str:
            return json.dumps(
                {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(data).decode(),
                    "commit": commit,
                    "sample_rate": self.sample_rate,
                }
            )

        async def pump(socket: websockets.ClientConnection) -> None:
            async for data in batched(audio, self.sample_rate):
                await socket.send(chunk(data, commit=False))
            # The audio ran out mid-utterance (the user stopped listening rather
            # than stopping speaking), so commit explicitly instead of waiting
            # for a VAD pause that will never arrive.
            with contextlib.suppress(websockets.WebSocketException):
                await socket.send(chunk(b"", commit=True))
                await asyncio.sleep(FLUSH_GRACE_SECONDS)
                closing.set()
                await socket.close()

        trace.event(
            "stt.session",
            {
                "provider": self.provider,
                "sample_rate": self.sample_rate,
                # What was actually sent, so a live failure names its own hint
                # rather than leaving it to be inferred from the adapter's code.
                # No separate "narrowed": it is what `candidates` not being empty
                # means, and a second field saying it could disagree with this one.
                "language": language.pin if language else None,
                "candidates": list(language.candidates) if language else [],
            },
        )
        try:
            async with websockets.connect(
                self.session_url(language), additional_headers={"xi-api-key": self._api_key}
            ) as socket:
                task = asyncio.create_task(pump(socket))
                # Which of the two commit events already gave this segment's
                # turn, and its text: the other one, if it comes, is the same.
                answered: tuple[str, str] | None = None
                try:
                    async for raw in socket:
                        payload = json.loads(raw)
                        kind = payload.get("message_type")
                        if kind == PARTIAL:
                            text = str(payload.get("text", ""))
                            trace.event("stt.partial", {"text": text})
                            yield Transcript(text=text, is_final=False)
                        elif kind in (COMMITTED, COMMITTED_DETECTED):
                            text = str(payload.get("text", ""))
                            language_code = payload.get("language_code")
                            detected = language_code if isinstance(language_code, str) else None
                            if answered is not None and answered[0] != kind and answered[1] == text:
                                answered = None  # the pair's second half
                                continue
                            answered = (kind, text)
                            trace.event(
                                "stt.committed",
                                {"text": text, "language": detected, "kind": kind},
                            )
                            # The language is carried even when the text is
                            # empty: a guess of which language the audio was in
                            # is evidence either way, and dropping it left a
                            # misdetection with nothing to correct it.
                            yield Transcript(text=text, is_final=True, language=detected)
                        elif isinstance(kind, str) and "error" in kind:
                            raise ProviderError(explain(payload))
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        except (websockets.WebSocketException, OSError) as exc:
            if closing.is_set():
                return  # our own orderly shutdown, not a failure
            raise ProviderError(f"elevenlabs transcription failed: {exc}") from exc
