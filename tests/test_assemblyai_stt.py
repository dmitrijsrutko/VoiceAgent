"""The AssemblyAI adapter against a real WebSocket speaking the v3 protocol.

Worth a real socket rather than a mock, for the same reason `test_stt.py` is:
the things that actually break here — audio going up in the wrong frame type, a
session that is never terminated, a formatted turn arriving twice — are all
properties of the conversation with the server, not of the object.
"""

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.frames import Close

from voice_agent import trace, vad
from voice_agent.config import load_settings
from voice_agent.errors import ConfigError, ProviderError
from voice_agent.stt import assemblyai_stt, create_stt
from voice_agent.stt.assemblyai_stt import AssemblyAISTT, explain
from voice_agent.stt.base import LanguageHint, batched


async def audio_of(chunks: int) -> AsyncIterator[bytes]:
    """Whole 100 ms chunks, so each arrives at the server as it was sent."""
    for _ in range(chunks):
        yield b"\x00\x01" * 1600


def turn(order: int, text: str, end: bool, formatted: bool = False, language: str = "") -> str:
    payload = {
        "type": "Turn",
        "turn_order": order,
        "transcript": text,
        "end_of_turn": end,
        "turn_is_formatted": formatted,
    }
    if language:
        payload["language_code"] = language
        payload["language_confidence"] = 0.99
    return json.dumps(payload)


class Recorder:
    """A stand-in server that answers audio the way the v3 endpoint does, and
    remembers how it was spoken to."""

    def __init__(self, tail: list[str] | None = None) -> None:
        self.binary: list[bytes] = []
        self.text: list[str] = []
        self.tail = tail if tail is not None else [turn(0, "all of it", end=True)]
        self.url = ""
        """What the adapter asked for, query string included: the session's
        parameters are the request, and the fake server is the only place they
        can be observed as sent."""

    async def __call__(self, connection: ServerConnection) -> None:
        request = connection.request
        self.url = request.path if request is not None else ""
        await connection.send(json.dumps({"type": "Begin", "id": "t", "expires_at": 0}))
        heard = 0
        async for raw in connection:
            if isinstance(raw, bytes):
                self.binary.append(raw)
                heard += 1
                await connection.send(turn(0, f"partial {heard}", end=False))
                continue
            self.text.append(raw)
            if json.loads(raw).get("type") == "Terminate":
                for message in self.tail:
                    await connection.send(message)
                await connection.send(
                    json.dumps(
                        {
                            "type": "Termination",
                            "audio_duration_seconds": 1,
                            "session_duration_seconds": 1,
                        }
                    )
                )


async def failing(connection: ServerConnection) -> None:
    await connection.send(json.dumps({"type": "Error", "error": "insufficient balance"}))
    async for _ in connection:
        pass


Handler = Callable[[ServerConnection], Awaitable[None]]
Endpoint = Callable[[Handler], Awaitable[AssemblyAISTT]]


@pytest.fixture
async def endpoint(monkeypatch: pytest.MonkeyPatch) -> Endpoint:
    async def build(handler: Handler) -> AssemblyAISTT:
        server = await serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(assemblyai_stt, "ENDPOINT", f"ws://127.0.0.1:{port}")
        monkeypatch.setattr(assemblyai_stt, "FLUSH_GRACE_SECONDS", 0.05)
        return AssemblyAISTT(api_key="test")

    return build


async def test_partials_and_the_end_of_turn_final_come_through(endpoint: Endpoint) -> None:
    stt = await endpoint(Recorder())

    transcripts = [t async for t in stt.stream(audio_of(3))]

    assert [(t.text, t.is_final) for t in transcripts] == [
        ("partial 1", False),
        ("partial 2", False),
        ("partial 3", False),
        ("all of it", True),
    ]


async def test_a_turn_finalized_twice_is_one_turn(endpoint: Endpoint) -> None:
    """The service can deliver a turn again once it has been formatted. Passed
    through, the second copy drives an entire extra turn — the agent answering
    the same sentence twice — because nothing downstream can tell them apart."""
    recorder = Recorder(
        tail=[
            turn(0, "what is the capital of latvia", end=True),
            turn(0, "What is the capital of Latvia?", end=True, formatted=True),
        ]
    )
    stt = await endpoint(recorder)

    finals = [t async for t in stt.stream(audio_of(1)) if t.is_final]

    assert [t.text for t in finals] == ["what is the capital of latvia"]


async def test_the_session_is_terminated_when_the_audio_runs_out(endpoint: Endpoint) -> None:
    """Not politeness: an abandoned session keeps billing until the three-hour
    cap, so this is the cost control."""
    recorder = Recorder()
    stt = await endpoint(recorder)

    async for _ in stt.stream(audio_of(1)):
        pass

    assert [json.loads(m)["type"] for m in recorder.text] == ["Terminate"]


async def test_audio_goes_up_as_binary_frames(endpoint: Endpoint) -> None:
    """This endpoint takes raw PCM frames. Scribe takes base64 inside JSON and
    so does the Voice Agent API, so sending an envelope here is the easy
    mistake — and it fails as silence, not as an error."""
    recorder = Recorder()
    stt = await endpoint(recorder)

    async for _ in stt.stream(audio_of(2)):
        pass

    assert recorder.binary == [b"\x00\x01" * 1600] * 2


async def test_an_error_payload_raises(endpoint: Endpoint) -> None:
    stt = await endpoint(failing)

    with pytest.raises(ProviderError, match="insufficient balance"):
        async for _ in stt.stream(audio_of(1)):
            pass


async def test_an_unreachable_service_is_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assemblyai_stt, "ENDPOINT", "ws://127.0.0.1:1")

    with pytest.raises(ProviderError, match="transcription failed"):
        async for _ in AssemblyAISTT(api_key="test").stream(audio_of(1)):
            pass


def test_the_model_is_the_singular_streaming_string() -> None:
    """Streaming takes `speech_model` as one string; the pre-recorded API takes
    a plural `speech_models` array. Sending either shape to the other endpoint
    is the most common way to get this API wrong."""
    url = AssemblyAISTT(api_key="test").url

    assert "speech_model=universal-3-6-pro" in url
    assert "speech_models" not in url


def test_the_session_asks_for_the_language_it_hears() -> None:
    """Reporting only — it is what makes a wrong hint correctable, and without
    it a turn carries nothing to correct it with (measured 2026-09-30)."""
    assert "language_detection=true" in AssemblyAISTT(api_key="test").url


def test_a_language_hint_goes_out_as_repeated_codes() -> None:
    """`language_codes` is the vendor's steering list, and its own validation
    parses it as repeated query parameters. A comma-joined value is *not*
    ignored: the service reads "en,es" as one invalid code and closes the
    session with 3006, which is also why a hint is filtered to the codes this
    model accepts before it reaches a URL."""
    pinned = AssemblyAISTT(api_key="test").session_url(LanguageHint(pin="ru"))
    narrowed = AssemblyAISTT(api_key="test").session_url(LanguageHint(candidates=("ru", "en")))

    assert "language_codes=ru" in pinned
    assert "language_codes=ru&language_codes=en" in narrowed
    assert "language_codes=en%2Ces" not in narrowed


def test_a_hint_carrying_a_language_this_model_never_hears_is_dropped() -> None:
    """The hint is written by a conversation, not by this adapter, so a code
    from another recognizer's convention (Scribe's `rus`) must not reach the
    URL: the service answers an invalid code with 3006 and the session never
    starts, where an unfiltered-out hint would have cost only a weaker bias."""
    url = AssemblyAISTT(api_key="test").session_url(LanguageHint(pin="rus", candidates=("ru",)))

    assert "language_codes=ru" in url
    assert "rus" not in url


def test_a_pin_and_its_candidates_are_one_list_without_repeats() -> None:
    """The vendor has one parameter for both tiers: a single element is its
    monolingual session, a longer list still code-switches. The pin is also a
    candidate, and sending it twice would be one code sent twice."""
    url = AssemblyAISTT(api_key="test").session_url(LanguageHint(pin="ru", candidates=("ru", "en")))

    assert url.count("language_codes=") == 2
    assert "language_codes=ru&language_codes=en" in url


async def test_the_language_comes_back_on_the_commit(endpoint: Endpoint) -> None:
    """A turn's language is what corrects a wrong hint, so it rides with the
    commit — `Transcript.language`, which `language.py` reads."""
    recorder = Recorder(tail=[turn(0, "привет", end=True, language="ru")])
    stt = await endpoint(recorder)

    finals = [t async for t in stt.stream(audio_of(1)) if t.is_final]

    assert [(t.text, t.language) for t in finals] == [("привет", "ru")]


async def test_a_language_held_from_a_session_we_did_not_guide(endpoint: Endpoint) -> None:
    """The hint is an argument to the session, not state on the adapter: this is
    the leak that made one conversation's language open the next one's first
    sentence, so a session opened without one carries no language either."""
    recorder = Recorder()
    stt = await endpoint(recorder)

    await anext(stt.stream(audio_of(1)))

    assert "language_codes" not in recorder.url


async def test_the_hint_reaches_the_session_that_is_opened(endpoint: Endpoint) -> None:
    """`stream` takes the hint and the URL is built from it: the microphone asks
    the conversation afresh for every listening session, so this argument is the
    only path a language has into a recognizer session."""
    recorder = Recorder()
    stt = await endpoint(recorder)

    async for _ in stt.stream(audio_of(1), LanguageHint(pin="ru", candidates=("ru", "en"))):
        pass

    assert "language_codes=ru&language_codes=en" in recorder.url


async def test_the_trace_names_the_model_and_the_hint(
    endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What makes a misdetection explainable afterwards: which model heard it,
    and what it was told to expect. The turn-silence bounds are not echoed by
    the service (`Begin` omits them), so the trace is where they are pinned."""
    seen: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        trace,
        "event",
        lambda kind, fields=None: seen.append((kind, dict(fields or {}))),
    )
    stt = await endpoint(Recorder(tail=[turn(0, "привет", end=True, language="ru")]))

    async for _ in stt.stream(audio_of(1), LanguageHint(pin="ru", candidates=("ru", "en"))):
        pass

    events = dict(seen)
    assert events["stt.session"] == {
        "provider": "assemblyai",
        "model": "universal-3-6-pro",
        "sample_rate": 16000,
        "max_turn_silence_ms": 1500,
        "language": "ru",
        "candidates": ["ru", "en"],
    }
    assert events["stt.committed"] == {"text": "привет", "language": "ru"}


async def test_an_empty_commit_still_carries_its_language(endpoint: Endpoint) -> None:
    """A language heard over an utterance the model could not write down is
    exactly the evidence that the hint in force was wrong. Dropping it because
    the text was empty threw that away on the other recognizer, live."""
    recorder = Recorder(tail=[turn(0, "", end=True, language="ru")])
    stt = await endpoint(recorder)

    finals = [t async for t in stt.stream(audio_of(1)) if t.is_final]

    assert [(t.text, t.language) for t in finals] == [("", "ru")]


def test_the_pause_reaches_the_url_in_milliseconds() -> None:
    """The flag is seconds and the service is milliseconds. A factor of a
    thousand here is not an error, it is an agent that waits 25 minutes."""
    url = AssemblyAISTT(api_key="test", silence_seconds=0.7).url

    assert "max_turn_silence=700" in url
    assert "min_turn_silence=560" in url


def test_a_wild_pause_is_clamped_to_what_the_service_accepts() -> None:
    """Corrected before it becomes a rejected connection, not after."""
    assert AssemblyAISTT(api_key="test", silence_seconds=600).max_turn_silence_ms == 10_000
    assert AssemblyAISTT(api_key="test", silence_seconds=0.01).max_turn_silence_ms == 50
    assert AssemblyAISTT(api_key="test", silence_seconds=0.01).min_turn_silence_ms == 50


def test_the_sample_rate_is_the_one_the_browser_captures_at() -> None:
    """The page opens its AudioContext at whatever the chosen ears report, so
    these drifting apart pitches the transcript's accuracy, not the audio."""
    assert AssemblyAISTT(api_key="test").sample_rate == assemblyai_stt.SAMPLE_RATE == 16000


def test_a_missing_key_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="ASSEMBLYAI_API_KEY"):
        create_stt("assemblyai")


def test_assemblyai_is_the_default_pair_of_ears(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICE_AGENT_STT", raising=False)

    assert load_settings().ears_provider == "assemblyai"


def test_the_pause_is_tunable_on_this_backend_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test")
    monkeypatch.setenv("VOICE_AGENT_STT", "assemblyai")
    monkeypatch.setenv("VOICE_AGENT_VAD_SILENCE", "2.5")
    settings = load_settings()

    backend = create_stt(settings.ears_provider, settings.vad_silence)

    assert isinstance(backend, AssemblyAISTT)
    assert backend.silence_seconds == 2.5
    assert "max_turn_silence=2500" in backend.url


async def test_a_terminated_session_ends_the_stream_without_an_error(endpoint: Endpoint) -> None:
    """The service answers `Terminate` with `Termination`. Reaching it is what
    ends the loop — and it must not surface as a failed transcription, which is
    the bug chapter 3 shipped against Scribe's silent close."""
    stt = await endpoint(Recorder())

    transcripts = [t async for t in stt.stream(audio_of(1))]

    assert transcripts[-1].is_final


def test_a_close_code_is_translated_into_advice() -> None:
    """The close code *is* the error on this endpoint — it rarely sends an error
    payload — and the number alone says nothing about what to do.

    Pinned because `explain` reads `code` off the library's exception, and that
    is exactly the attribute that gets renamed in a major version. Without this
    the hints would become unreachable silently, which is worse than never
    having written them.
    """
    chunk = websockets.ConnectionClosedError(Close(3007, "bad chunk"), None)
    unauthorized = websockets.ConnectionClosedError(Close(1008, "nope"), None)

    assert "50-1000 ms" in explain(chunk)
    assert "ASSEMBLYAI_API_KEY" in explain(unauthorized)


def test_an_unmapped_failure_still_says_something_useful() -> None:
    """Every hint is an extra, never the whole message."""
    assert explain(OSError("connection refused")) == (
        "assemblyai transcription failed: connection refused"
    )


def test_the_browser_frame_is_one_vad_window() -> None:
    """The worklet posts one VAD window per frame, so the server hears a pause
    a window late. The two constants live in different languages, so nothing
    but this test ties them together."""
    worklet = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "voice_agent"
        / "web"
        / "capture-worklet.js"
    ).read_text(encoding="utf-8")
    samples = int(re.findall(r"new Int16Array\((\d+)\)", worklet)[0])

    assert samples == vad.WINDOW_SAMPLES
    assert assemblyai_stt.SAMPLE_RATE == vad.SAMPLE_RATE


async def test_the_recognizer_is_sent_chunks_this_endpoint_will_accept() -> None:
    """A chunk outside 50-1000 ms closes the session with 3007. The page's 32 ms
    frames are under that floor, so they are regrouped — tail included."""

    async def frames() -> AsyncIterator[bytes]:
        for _ in range(10):  # 320 ms: three whole chunks and a 20 ms tail
            yield b"\x00\x01" * vad.WINDOW_SAMPLES

    rate = assemblyai_stt.SAMPLE_RATE
    chunks = [chunk async for chunk in batched(frames(), rate)]

    for chunk in chunks:
        chunk_ms = len(chunk) / 2 / rate * 1000
        assert 50 <= chunk_ms <= 1000, f"{chunk_ms:.0f} ms — 3007 territory"
    assert len(chunks) == 3


def test_the_languages_are_the_thirty_two_the_service_accepts() -> None:
    """The set that cost an evening, and then stopped being true.

    Spoken Russian used to come back as confident nonsense ("Раскажем не pravalo
    вывnutriny produkt kitaia") because it was not among the eighteen this model
    heard. 3.6 Pro hears it, so the list is pinned by membership as well as
    count: `ru`, `ko` and the two codes the vendor spells in 639-3 are the ones
    a careless edit would drop, and the service itself refuses any code outside
    this set with 3006 rather than ignoring it.
    """
    assert len(assemblyai_stt.LANGUAGES) == 32
    assert len(set(assemblyai_stt.LANGUAGES)) == 32, "a code appears twice"
    assert {"ru", "ko", "yue", "nn", "af", "zh"} <= set(assemblyai_stt.LANGUAGES)
    assert AssemblyAISTT(api_key="test").languages == assemblyai_stt.LANGUAGES


def test_scribe_hears_what_this_backend_cannot() -> None:
    """The documented escape hatch has to actually be one. Russian is no longer
    the example — Thai is: it is in Scribe's published hundred and in none of
    this model's thirty-two."""
    from voice_agent.stt import elevenlabs_stt

    assert "tha" in elevenlabs_stt.LANGUAGES
    assert "th" not in assemblyai_stt.LANGUAGES
    assert len(elevenlabs_stt.LANGUAGES) > len(assemblyai_stt.LANGUAGES)
