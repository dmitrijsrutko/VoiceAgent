"""End-to-end tests over a real WebSocket; only the provider is faked."""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import FakeLLM, FakeSTT, FakeTTS, pcm_for, receive
from voice_agent import prompts
from voice_agent import turn as turn_module
from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import Usage
from voice_agent.server import create_app
from voice_agent.session import is_exit_command
from voice_agent.sessions import SessionStore
from voice_agent.tts.base import MEDIA_TYPE, AudioChunk


@pytest.fixture
def store() -> SessionStore:
    return SessionStore()


@pytest.fixture
def client(llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, store: SessionStore) -> TestClient:
    return TestClient(create_app(llm=llm, tts=tts, stt=stt, store=store, greeting=""))


@pytest.fixture
def silent_client(llm: FakeLLM, store: SessionStore) -> TestClient:
    return TestClient(create_app(llm=llm, store=store, voice=False, ears=False, greeting=""))


def start(client: TestClient) -> str:
    """Load the root the way a browser does, and return the minted key."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].removeprefix("/c/")


def drain(socket: object, audio: bool = True) -> tuple[str, list[dict[str, object]]]:
    """Collect one reply turn: its text, and the end of its audio.

    Returns how the audio ended — or the error that ended the turn. The two
    streams interleave, so the audio can finish before the text does."""
    frames: list[dict[str, object]] = []
    while True:
        frame = receive(socket)
        frames.append(frame)
        if frame["type"] in ("error", "ended"):
            return str(frame["type"]), frames
        kinds = [f["type"] for f in frames]
        if "reply_end" not in kinds:
            continue
        if not audio:
            return "reply_end", frames
        ends = [kind for kind in kinds if kind in ("audio_end", "audio_error")]
        if ends:
            return str(ends[-1]), frames


def spoken_bytes(frames: list[dict[str, object]]) -> bytes:
    return b"".join(bytes(f["data"]) for f in frames if f["type"] == "audio_bytes")  # type: ignore[call-overload]


def test_root_mints_a_new_conversation_per_visit(client: TestClient, store: SessionStore) -> None:
    first, second = start(client), start(client)

    assert first != second
    assert len(store) == 2


def test_the_page_loads_for_a_real_key_and_404s_for_anything_else(client: TestClient) -> None:
    key = start(client)

    assert client.get(f"/c/{key}").status_code == 200
    assert client.get("/c/made-up-key").status_code == 404


@pytest.mark.parametrize(
    "module", ["app.js", "player.js", "capture-worklet.js", "playback-worklet.js"]
)
def test_the_page_modules_are_served_as_javascript(client: TestClient, module: str) -> None:
    """A module script served with a non-JavaScript type is refused by the
    browser outright — the page would load with no behaviour at all."""
    response = client.get(f"/static/{module}")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    # Revalidated every load, so a fresh module is never linked against a
    # stale cached neighbour.
    assert response.headers["cache-control"] == "no-cache"


def test_a_turn_streams_back_and_is_recorded(
    client: TestClient, store: SessionStore, llm: FakeLLM
) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        ready = socket.receive_json()
        assert ready["type"] == "ready"
        assert ready["provider"] == "fake"
        assert ready["history"] == []

        socket.send_json({"type": "user_message", "text": "hello there"})
        kind, frames = drain(socket)

    assert kind == "audio_end"
    assert frames[0]["type"] == "reply_start"
    assert [f["text"] for f in frames if f["type"] == "delta"] == ["Sure ", "thing. "]
    assert next(f for f in frames if f["type"] == "reply_end")["text"] == "Sure thing. "

    conversation = store.get(key)
    assert [(m.role, m.content) for m in conversation.messages] == [
        ("user", "hello there"),
        ("assistant", "Sure thing. "),
    ]


def test_the_whole_conversation_is_resent_as_context_every_turn(
    client: TestClient, llm: FakeLLM
) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        for text in ("first", "second", "third"):
            socket.send_json({"type": "user_message", "text": text})
            drain(socket)

    # Each call carries every prior turn plus the new question — the growing
    # context that later chapters will have to manage.
    assert [len(seen) for seen in llm.seen] == [1, 3, 5]
    assert [m.content for m in llm.seen[-1]] == [
        "first",
        "Sure thing. ",
        "second",
        "Sure thing. ",
        "third",
    ]
    assert llm.systems[0].startswith("You are a woman")  # the voice's identity, then the persona
    assert "You are a voice assistant." in llm.systems[0]


def test_reconnecting_to_the_link_resumes_the_same_conversation(client: TestClient) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "remember me"})
        drain(socket)

    with client.websocket_connect(f"/ws/{key}") as socket:
        ready = socket.receive_json()

    assert [m["content"] for m in ready["history"]] == ["remember me", "Sure thing. "]
    assert ready["ended"] is False


def test_exit_ends_the_conversation_and_it_stays_ended(
    client: TestClient, store: SessionStore, llm: FakeLLM
) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "Bye!"})
        kind, _ = drain(socket)

    assert kind == "ended"
    assert store.get(key).ended
    assert llm.seen == []  # an exit is never sent to the model

    with client.websocket_connect(f"/ws/{key}") as socket:
        assert socket.receive_json()["ended"] is True


def test_a_failed_turn_rolls_back_the_user_message(store: SessionStore) -> None:
    app = create_app(llm=FakeLLM(fail=True), store=store, voice=False, ears=False, greeting="")
    client = TestClient(app)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "this will fail"})
        kind, frames = drain(socket)

    assert kind == "error"
    assert "provider exploded" in str(frames[-1]["message"])
    assert store.get(key).messages == []


def test_an_unknown_key_cannot_open_a_socket(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/ws/not-a-real-key") as socket,
    ):
        socket.receive_json()

    assert excinfo.value.code == 4404


def test_blank_and_malformed_input_are_ignored_not_forwarded(
    client: TestClient, llm: FakeLLM
) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "   "})
        socket.send_text("not json at all")
        assert socket.receive_json()["type"] == "error"

    assert llm.seen == []


@pytest.mark.parametrize("text", ["exit", "EXIT", " Quit ", "bye", "Goodbye."])
def test_exit_commands(text: str) -> None:
    assert is_exit_command(text)


@pytest.mark.parametrize("text", ["exiting", "goodbye is a strange word", "quitting time"])
def test_non_exit_commands(text: str) -> None:
    assert not is_exit_command(text)


def test_the_reply_is_spoken_as_a_stream_of_binary_frames(client: TestClient, tts: FakeTTS) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        ready = socket.receive_json()
        assert ready["voice"] == {
            "provider": "fake-voice",
            "voice": "fake-voice-1",
            "sample_rate": 24000,
        }

        socket.send_json({"type": "user_message", "text": "say something"})
        kind, frames = drain(socket)

    types = [f["type"] for f in frames]
    audio_bytes = spoken_bytes(frames)
    opening = frames[types.index("audio_start")]
    closing = frames[types.index("audio_end")]
    assert kind == "audio_end"
    assert opening == {"type": "audio_start", "media_type": MEDIA_TYPE, "sample_rate": 24000}
    assert closing["bytes"] == len(audio_bytes)
    assert closing["seconds"] == len(audio_bytes) / 48_000
    assert isinstance(closing["synthesis_ms"], int)

    # More than one frame, all inside the stream — one frame would be batched
    # synthesis, renamed.
    audio_at = [i for i, t in enumerate(types) if t == "audio_bytes"]
    assert len(audio_at) > 1
    assert types.index("audio_start") < audio_at[0] and audio_at[-1] < types.index("audio_end")
    assert closing["chunks"] == len(audio_at), "chunks does not match frames sent"

    # The agent speaks exactly what it recorded — not a re-rendered version.
    assert tts.spoken == ["Sure thing. "]
    assert audio_bytes == pcm_for("Sure thing. ")


def test_each_word_s_timing_reaches_the_page_before_its_audio(
    client: TestClient, tts: FakeTTS
) -> None:
    """What the page lights the text up by. A mark arriving after its audio
    would leave a word playing that the page cannot yet place."""
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "say something"})
        _, frames = drain(socket)

    types = [f["type"] for f in frames]
    ends = [end for f in frames if f["type"] == "marks" for end in f["ends_ms"]]  # type: ignore[attr-defined]
    assert len(ends) == len(tts.spoken[0]), "a character of the reply has no timing"
    assert ends == sorted(ends), "a later character ends before an earlier one"
    # The fake voice times every chunk, so each audio frame must have had its
    # own marks sent first.
    for i, kind in enumerate(types):
        if kind == "audio_bytes":
            seen = types[: i + 1]
            assert seen.count("marks") >= seen.count("audio_bytes"), "audio sent ahead of its marks"


def test_the_voice_starts_before_the_reply_has_finished_being_written(
    store: SessionStore, tts: FakeTTS
) -> None:
    """The chapter in one assertion. Synthesizing after `reply_end` passes every
    other test here, and makes the user wait for the whole reply to be written."""
    words = "one two three four five six seven eight nine ten"
    client = TestClient(
        create_app(
            llm=FakeLLM(replies=[words], pace=0.02), tts=tts, store=store, ears=False, greeting=""
        )
    )
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "count"})
        _, frames = drain(socket)

    types = [f["type"] for f in frames]
    assert types.index("audio_bytes") < types.index("reply_end"), "speech waited for the reply"
    assert next(f for f in frames if f["type"] == "audio_end")["audio_before_reply_end"] is True
    assert spoken_bytes(frames) == pcm_for(words + " ")


@pytest.mark.parametrize("voice_fails_first", [False, True])
def test_a_reply_that_fails_after_speaking_began_closes_its_audio(
    store: SessionStore, voice_fails_first: bool
) -> None:
    """Part of the answer has been heard, so audio is already playing. The page
    reopens the microphone only when the audio it began is closed — once, even
    when the voice had already failed and closed it itself."""
    tts = FakeTTS(fail_after=1) if voice_fails_first else FakeTTS()

    class DyingLLM(FakeLLM):
        async def stream(
            self, system: str, messages: Sequence[Message], usage: Usage | None = None
        ) -> AsyncIterator[str]:
            for word in ("Riga ", "is ", "the ", "capital "):
                yield word
                await asyncio.sleep(0.02)
            raise ProviderError("connection lost mid-reply")

    client = TestClient(create_app(llm=DyingLLM(), tts=tts, store=store, ears=False, greeting=""))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "tell me"})
        kind, frames = drain(socket)

    types = [f["type"] for f in frames]
    assert kind == "error"
    assert "audio_bytes" in types, "the test did not get as far as speaking"
    assert "audio_end" in types, "audio that began was never closed"
    assert types.index("audio_end") < types.index("error")
    assert types.count("audio_end") == 1
    assert tts.active == 0, "synthesis outlived the reply it was speaking"
    assert store.get(key).messages == [], "a failed exchange stayed in the context"


def test_a_failed_synthesis_keeps_the_reply_and_degrades_to_text(
    store: SessionStore, llm: FakeLLM
) -> None:
    client = TestClient(
        create_app(llm=llm, tts=FakeTTS(fail=True), store=store, ears=False, greeting="")
    )
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "say something"})
        kind, frames = drain(socket)

    assert kind == "audio_error"
    assert "synthesizer exploded" in str(
        next(f for f in frames if f["type"] == "audio_error")["message"]
    )
    # Announced lazily, so a synthesis that never produced anything leaves no
    # half-open stream for the browser to wait on.
    assert "audio_start" not in [f["type"] for f in frames]
    # Losing the words would be a far worse failure than losing the audio.
    assert [m.content for m in store.get(key).messages] == ["say something", "Sure thing. "]


def test_a_synthesis_that_dies_mid_reply_closes_the_audio_it_began(
    store: SessionStore, llm: FakeLLM
) -> None:
    client = TestClient(
        create_app(llm=llm, tts=FakeTTS(fail_after=2), store=store, ears=False, greeting="")
    )
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "say something"})
        # Read to `audio_error`, which the server always sends on this path,
        # rather than to `audio_end`, which is the thing under test: waiting on
        # a frame that may never come turns a regression into a hung suite.
        frames = []
        while (frame := receive(socket))["type"] != "audio_error":
            frames.append(frame)
        # The text streams on regardless; read it to its end so the socket's
        # close cannot cancel the turn before the reply is recorded.
        rest = [frame]
        while "reply_end" not in [f["type"] for f in frames + rest]:
            rest.append(receive(socket))

    types = [f["type"] for f in frames if f["type"] != "reply_end"]
    # What was sent is closed and accounted for, then the failure is reported.
    assert "audio_end" in types, "audio that began was never closed"
    assert types[-1] == "audio_end", "the error was reported before the audio was closed"
    assert types.count("audio_bytes") == 2
    closing = next(f for f in frames if f["type"] == "audio_end")
    assert closing["bytes"] == len(spoken_bytes(frames)) == 8
    assert [m.content for m in store.get(key).messages] == ["say something", "Sure thing. "]


def test_the_agent_still_works_with_no_voice_at_all(
    silent_client: TestClient, store: SessionStore
) -> None:
    key = start(silent_client)

    with silent_client.websocket_connect(f"/ws/{key}") as socket:
        assert socket.receive_json()["voice"] is None
        socket.send_json({"type": "user_message", "text": "hello"})
        kind, _ = drain(socket, audio=False)

    assert kind == "reply_end"
    assert [m.content for m in store.get(key).messages] == ["hello", "Sure thing. "]


def test_exit_is_never_synthesized(client: TestClient, tts: FakeTTS) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "exit"})
        kind, _ = drain(socket)

    assert kind == "ended"
    assert tts.spoken == []


def test_ending_hangs_up_rather_than_waiting_for_the_page(client: TestClient) -> None:
    """After `ended` the page sends nothing. A server still waiting to hear
    from it held the conversation's live slot until the tab was closed."""
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "exit"})
        kind, _ = drain(socket)
        assert kind == "ended"
        with pytest.raises(WebSocketDisconnect):
            receive(socket)


def test_the_reasoning_stage_reports_its_own_timings(client: TestClient) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "hello"})
        _, frames = drain(socket)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    audio = next(f for f in frames if f["type"] == "audio_end")

    # Split at the first token: time-to-first-token is dead air the user
    # experiences; generation time is throughput streaming already hides.
    assert isinstance(reply_end["ttft_ms"], int)
    assert isinstance(reply_end["generation_ms"], int)
    assert reply_end["chars"] == len("Sure thing. ")
    # Fragments are what arrived; tokens are what the provider says they cost.
    # The fake reports twice as many tokens as fragments so the two cannot be
    # confused for each other.
    assert reply_end["fragments"] == 2
    assert reply_end["output_tokens"] == 4
    assert reply_end["prompt_tokens"] == 101
    assert reply_end["cached_tokens"] == 64
    assert reply_end["connect_ms"] is None, "a reused connection was reported as opened"
    assert reply_end["accepted_ms"] == 40
    assert reply_end["attempts"] == 1

    # The number the project is judged on covers the whole turn up to the
    # first chunk — and so includes the provider's own wait for it.
    first_audio = int(audio["first_audio_ms"])  # type: ignore[call-overload]
    assert first_audio >= int(audio["synthesis_first_byte_ms"])  # type: ignore[call-overload]
    assert int(audio["synthesis_ms"]) >= int(audio["synthesis_first_byte_ms"])  # type: ignore[call-overload]


def test_a_turn_that_opened_a_connection_says_what_it_cost(store: SessionStore) -> None:
    llm = FakeLLM()
    llm.connect_ms = 27
    client = TestClient(create_app(llm=llm, store=store, voice=False, ears=False, greeting=""))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "hello"})
        _, frames = drain(socket, audio=False)

    assert next(f for f in frames if f["type"] == "reply_end")["connect_ms"] == 27


def test_a_slow_first_token_is_logged_with_where_the_time_went(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The page's notes are gone with the tab; the terminal keeps this one."""
    monkeypatch.setattr(turn_module, "SLOW_FIRST_TOKEN_MS", 50)
    client = TestClient(
        create_app(llm=FakeLLM(delay=0.1), store=store, voice=False, ears=False, greeting="")
    )
    key = start(client)

    with caplog.at_level(logging.WARNING), client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "hello"})
        drain(socket, audio=False)

    assert "slow first token" in caplog.text
    assert "accepted at 40 ms, 1 attempt(s)" in caplog.text


def test_the_reasoning_engine_is_connected_at_startup_not_on_the_first_question(
    store: SessionStore,
) -> None:
    llm = FakeLLM()

    with TestClient(create_app(llm=llm, store=store, voice=False, ears=False, greeting="")):
        assert llm.connects == 1
        assert llm.seen == [], "connecting billed a real call"


def test_an_engine_that_cannot_connect_at_startup_still_starts(store: SessionStore) -> None:
    """The connection is a head start, not a precondition: the first turn opens
    one anyway, and reports the failure itself if it persists."""
    llm = FakeLLM(fail=True)

    with TestClient(
        create_app(llm=llm, store=store, voice=False, ears=False, greeting="")
    ) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
    assert llm.connects == 1


def test_an_empty_reply_reports_its_whole_duration_as_time_to_first_token(
    store: SessionStore, tts: FakeTTS
) -> None:
    """Nothing streamed means the user waited and got nothing — reporting that
    as zero of everything would hide exactly the failure worth seeing."""
    client = TestClient(
        create_app(llm=FakeLLM(replies=[""]), tts=tts, store=store, ears=False, greeting="")
    )
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "hello"})
        _, frames = drain(socket, audio=False)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    assert reply_end["chars"] == 0
    assert reply_end["generation_ms"] == 0
    assert "".join(tts.spoken) == "", "silence was sent to the synthesizer"
    assert "audio_start" not in [f["type"] for f in frames], "silence was announced as speech"


def test_the_system_prompt_tells_the_agent_to_match_the_users_language() -> None:
    """A real conversation opened with "Hallo." — a word shared by English and
    German — and the agent answered in German and stayed there for eleven
    turns, anchored by its own replies. The prompt had no language rule at all.
    """
    prompt = prompts.load("system_prompt")

    assert "language the user is speaking" in prompt
    assert "greeting is not evidence" in prompt.lower()


def test_the_language_and_voice_rules_are_the_last_things_the_agent_reads() -> None:
    """Stated in the persona alone, Haiku answered a Russian question in English
    after one English reply, in 11 of 16 replays of a live conversation; repeated
    last, with examples, 0 of 16. The gender line, left under the hearing and
    clock notes, was ignored the same way («понял» in a woman's voice)."""
    from voice_agent.config import build_prompt

    LANGUAGE = prompts.rules()["Language"]

    prompt = build_prompt(("en", "ru"), "female", (5.0,), base="persona")
    closing = prompt.split("\n\n")[-2:]

    assert closing[0] == LANGUAGE
    assert closing[1].startswith("Voice rule")
    assert "previous reply was in another" in LANGUAGE


def test_the_greeting_is_stated_but_the_language_rule_stays_last() -> None:
    from voice_agent.config import build_prompt

    LANGUAGE = prompts.rules()["Language"]

    prompt = build_prompt(("en",), "female", (), base="persona", greeting="Hi there.")

    assert "\u201cHi there.\u201d" in prompt
    assert prompt.index("Hi there.") < prompt.index(LANGUAGE)


def test_the_agent_is_a_woman_or_a_man_before_it_is_anything_else() -> None:
    """A grammar rule at the end was ignored («понял» in a woman's voice); an
    identity is the first thing read. A neutral voice gets none."""
    from voice_agent.config import build_prompt

    assert build_prompt((), "female", (), base="persona").startswith("You are a woman")
    assert build_prompt((), "male", (), base="persona").startswith("You are a man")
    assert build_prompt((), "neutral", (), base="persona").startswith("persona")


@pytest.mark.parametrize("gender", ["female", "male", "neutral"])
def test_the_gender_rule_is_about_the_agent_alone(gender: str) -> None:
    """Corrected on its own gender, a model made Dostoevsky feminine instead."""
    from voice_agent.config import with_voice_gender

    SELF_ONLY = prompts.rules()["Voice scope"]

    line = with_voice_gender("rules", gender)

    assert SELF_ONLY in line
    assert "{scope}" not in line


def test_the_system_prompt_says_what_a_cut_off_reply_means() -> None:
    """The history marks an interrupted reply only by where it stops, so the
    model has to be told what that means."""
    assert "stops mid-sentence is where you were interrupted" in prompts.load("system_prompt")


async def test_the_mute_window_is_what_is_left_to_play_not_the_whole_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser starts playing the first chunk while the rest is still being
    made. Counting the full length from when synthesis *ends* would keep the
    user's silence uncounted for the time already spent playing."""
    from voice_agent import timing, turn

    clock = [100.0]
    monkeypatch.setattr(timing, "now", lambda: clock[0])

    class OneSecondTTS(FakeTTS):
        async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
            yield AudioChunk(b"\x00" * 24_000)  # half a second
            clock[0] += 0.3  # the provider is slow with the rest
            yield AudioChunk(b"\x00" * 24_000)

    class Sink:
        async def send_json(self, payload: dict[str, object]) -> None: ...

        async def send_bytes(self, data: bytes) -> None: ...

    speech = turn.Speech(Sink(), OneSecondTTS(), clock[0])  # type: ignore[arg-type]
    speech.say("hello")
    speech.finish()
    left = await speech.done()

    assert left == pytest.approx(0.7), "a 1 s reply, 0.3 s into playing it"


async def test_marks_are_timed_from_the_reply_s_first_sample() -> None:
    """ElevenLabs times each segment from its own start. Sent as it arrives,
    the second segment's words would light up during the first."""
    from voice_agent import turn
    from voice_agent.tts.base import Alignment

    class TwoSegments(FakeTTS):
        async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
            async for _ in text:
                pass
            half_second = b"\x00" * 24_000
            yield AudioChunk(half_second, Alignment("ab", (250.0, 500.0)))
            yield AudioChunk(half_second, Alignment("cd", (250.0, 500.0)))

    class Sink:
        def __init__(self) -> None:
            self.marks: list[object] = []
            self.starts: list[object] = []

        async def send_json(self, payload: dict[str, object]) -> None:
            if payload["type"] == "marks":
                self.marks.extend(payload["ends_ms"])  # type: ignore[arg-type]
                self.starts.append(payload["from_ms"])

        async def send_bytes(self, data: bytes) -> None: ...

    sink = Sink()
    speech = turn.Speech(sink, TwoSegments(), 0.0)  # type: ignore[arg-type]
    speech.say("abcd")
    speech.finish()
    await speech.done()

    assert sink.marks == [250, 500, 750, 1000]
    assert sink.starts == [0, 500], "the page cannot cap earlier marks without the start"


# --- The clock, configured ----------------------------------------------------


def test_the_delays_are_configurable_but_the_intents_are_not() -> None:
    """What each rung is *for* — follow through, offer something concrete, then
    withdraw — is what makes the agent bearable to sit with. Only the when is a
    knob.

    Sized from `LADDER` rather than from a written-down number: the rungs have
    changed twice now, and every place that restated their count instead of
    reading it went stale without failing.
    """
    from voice_agent.initiative import LADDER
    from voice_agent.server import ladder_for

    delays = [9.0 * (i + 1) for i in range(len(LADDER))]
    ladder = ladder_for(delays)

    assert [rung.after for rung in ladder] == delays
    assert [rung.intent for rung in ladder] == [rung.intent for rung in LADDER]


def test_more_delays_than_rungs_is_refused_not_trimmed() -> None:
    """Asking for four and silently getting two is the kind of quiet
    disagreement between what was configured and what is running that this
    project refuses everywhere else."""
    from voice_agent.errors import ConfigError
    from voice_agent.initiative import LADDER
    from voice_agent.server import ladder_for

    too_many = [float(i + 1) for i in range(len(LADDER) + 1)]
    with pytest.raises(ConfigError, match=f"only {len(LADDER)} rungs"):
        ladder_for(too_many)


def test_fewer_delays_than_rungs_shortens_the_ladder() -> None:
    from voice_agent.server import ladder_for

    assert len(ladder_for([5.0])) == 1
    assert ladder_for([]) == ()


@pytest.mark.parametrize("raw", ["off", "none", "", "0"])
def test_the_clock_can_be_turned_off(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--initiative off` must restore exactly the reactive agent of every
    chapter before this one, not a clock that keeps deciding no."""
    from voice_agent.config import load_settings

    monkeypatch.setenv("VOICE_AGENT_INITIATIVE", raw)
    assert load_settings().initiative == ()


@pytest.mark.parametrize("raw", ["7,5", "seven", "-1", "7,7"])
def test_a_malformed_clock_refuses_to_start_rather_than_guessing(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo that silently produced a mute agent — or one that speaks every
    half second — is worse than a server that will not start and says why."""
    from voice_agent.config import load_settings
    from voice_agent.errors import ConfigError

    monkeypatch.setenv("VOICE_AGENT_INITIATIVE", raw)
    with pytest.raises(ConfigError):
        load_settings()


# --- Speaking about itself ---------------------------------------------------


def test_the_agent_is_told_which_gender_its_voice_has() -> None:
    """Russian, Polish, Hebrew and others mark the speaker's gender on ordinary
    past-tense verbs, so a woman's voice saying «я понял» contradicts itself in
    the same breath. Heard live before this was set."""
    from voice_agent.config import with_voice_gender

    assert "«я поняла»" in with_voice_gender("rules", "female")
    assert "«я понял»" in with_voice_gender("rules", "male")
    assert "avoid the choice" in with_voice_gender("rules", "neutral")


def test_the_gender_goes_last_so_the_cached_prefix_is_untouched() -> None:
    """Everything above it is what the provider caches, and this never changes
    between turns (ROADMAP §2C)."""
    from voice_agent.config import with_voice_gender

    assert with_voice_gender("rules", "female").startswith("rules\n\n")


@pytest.mark.parametrize("raw", ["woman", "f", "", "FEMALE "])
def test_an_unknown_voice_gender_is_refused(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from voice_agent.config import load_settings
    from voice_agent.errors import ConfigError

    monkeypatch.setenv("VOICE_AGENT_VOICE_GENDER", raw)
    if raw.strip().casefold() in ("female",):
        assert load_settings().voice_gender == "female"  # spacing and case are fine
        return
    with pytest.raises(ConfigError):
        load_settings()


def test_the_system_prompt_the_agent_receives_carries_it(
    llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is useless unless it reaches the model, so this asserts on what
    the provider was actually sent."""
    monkeypatch.setenv("VOICE_AGENT_VOICE_GENDER", "male")
    client = TestClient(create_app(llm=llm, tts=tts, stt=stt, store=store, greeting=""))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        receive(socket)
        socket.send_json({"type": "user_message", "text": "привет"})
        while receive(socket)["type"] != "reply_end":
            pass

    assert "a man's, so" in llm.systems[0]


def test_the_agent_is_told_which_languages_it_can_hear(llm: FakeLLM, store: SessionStore) -> None:
    """A recognizer that cannot hear a language returns nonsense rather than an
    error, so the agent has to be told — otherwise it promises Russian it can
    never receive, which the prompt's own "do not invent capabilities" forbids.
    """
    client = TestClient(
        create_app(
            llm=llm, stt=FakeSTT(languages=("en", "es")), store=store, voice=False, greeting=""
        )
    )
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        receive(socket)
        socket.send_json({"type": "user_message", "text": "hello"})
        drain(socket, audio=False)

    assert "en, es" in llm.systems[0]
    assert "never offer, promise or agree to listen" in llm.systems[0]


def test_a_recognizer_with_no_stated_limit_says_nothing_about_languages(
    llm: FakeLLM, store: SessionStore
) -> None:
    """Telling the agent it hears an empty set would be worse than silence."""
    client = TestClient(create_app(llm=llm, stt=FakeSTT(), store=store, voice=False, greeting=""))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        receive(socket)
        socket.send_json({"type": "user_message", "text": "hello"})
        drain(socket, audio=False)

    assert "Your hearing is a speech recognizer" not in llm.systems[0]


def test_the_page_is_told_what_the_ears_understand(llm: FakeLLM, store: SessionStore) -> None:
    """So somebody can see it before they open their mouth."""
    client = TestClient(
        create_app(
            llm=llm, stt=FakeSTT(languages=("en", "es")), store=store, voice=False, greeting=""
        )
    )
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        ready = receive(socket)

    assert ready["ears"]["languages"] == ["en", "es"]


def test_the_agent_is_told_when_it_may_speak_first() -> None:
    """Asked live how long it waits, the agent answered "a few seconds" when
    the first rung was at fifteen. It had never been told, so it did what a
    model does with a question about its own body and made a number up."""
    from voice_agent.config import with_initiative

    said = with_initiative("rules", (5.0, 15.0, 28.0))

    assert "5, 15 and 28 seconds" in said
    assert "tell them plainly instead of guessing" in said


def test_an_agent_that_never_speaks_first_is_told_nothing_about_a_clock() -> None:
    from voice_agent.config import with_initiative

    assert with_initiative("rules", ()) == "rules"


def test_the_prompt_quotes_the_ladder_that_is_actually_running(
    llm: FakeLLM, store: SessionStore
) -> None:
    """The delays reach the prompt from the built ladder, not from the setting,
    so the agent's account of its clock cannot drift away from the clock."""
    client = TestClient(
        create_app(
            llm=llm, store=store, voice=False, ears=False, greeting="", initiative=[3.0, 9.0]
        )
    )
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        receive(socket)
        socket.send_json({"type": "user_message", "text": "hello"})
        drain(socket, audio=False)

    assert "3 and 9 seconds" in llm.systems[0]


def served_facts(page: str) -> dict[str, object]:
    """What the server wrote into the page for the start screen to read."""
    block = re.search(r'<script id="facts" type="application/json">(.*?)</script>', page)
    assert block, "the page carries no facts"
    loaded = json.loads(block.group(1))
    assert isinstance(loaded, dict)
    return loaded


def test_the_page_carries_the_same_facts_the_socket_will_send(
    client: TestClient, store: SessionStore
) -> None:
    """The start screen has to say what starting entails before there is a
    socket to ask over, so the facts are written into the page. Two statements
    of the same three things is exactly the drift between what the page claims
    and what the server does that they must not be allowed."""
    key = start(client)
    served = served_facts(client.get(f"/c/{key}").text)

    with client.websocket_connect(f"/ws/{key}") as socket:
        ready = receive(socket)

    assert served["recording"] == ready["recording"]
    assert served["voice"] == ready["voice"]
    assert served["ears"] == ready["ears"]


def test_a_silent_deaf_unrecorded_agent_says_so_on_the_page(llm: FakeLLM) -> None:
    """Every fact the start screen renders is a `null` or a `false` away from
    claiming something the server will not do."""
    silent = TestClient(create_app(llm=llm, voice=False, ears=False, greeting="", record=False))
    key = start(silent)
    served = served_facts(silent.get(f"/c/{key}").text)

    assert (served["voice"], served["ears"], served["recording"]) == (None, None, False)
    # And offers no ears to choose between, having none.
    choices = served["choices"]
    assert isinstance(choices, dict)
    assert choices["stt"] == []


def test_a_closing_tag_in_the_facts_cannot_end_the_block_early() -> None:
    """Every value is a server-side constant today. A `</script>` reaching the
    page verbatim would end the block and leave the rest of the document as
    text, and foreclosing that costs one call."""
    from voice_agent.server import FACTS_BLOCK, with_facts

    page = with_facts(f"<html>{FACTS_BLOCK}</html>", {"voice": "</script><b>evil"})

    assert "</script><b>" not in page
    assert served_facts(page) == {"voice": "</script><b>evil"}


# --- chapter 15: the stack is chosen when the conversation starts -----------


def stacked(
    llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """An app that really does offer a choice. The fakes stand in for every
    backend (`Pool` returns an injected one for any name), so what is under
    test is the *selection*, not the vendors."""
    for name in (
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "ASSEMBLYAI_API_KEY",
        "ELEVENLABS_API_KEY",
    ):
        monkeypatch.setenv(name, "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("VOICE_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("VOICE_AGENT_STT", "assemblyai")
    return TestClient(create_app(llm=llm, tts=tts, stt=stt, greeting=""))


def test_the_page_offers_only_backends_this_deployment_has_keys_for(
    llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offering a backend that cannot be built is offering an error."""
    client = stacked(llm, tts, stt, monkeypatch)
    choices = served_facts(client.get(f"/c/{start(client)}").text)["choices"]
    assert isinstance(choices, dict)

    assert [o["name"] for o in choices["llm"]] == ["deepseek", "anthropic"]
    assert [o["name"] for o in choices["stt"]] == ["assemblyai", "elevenlabs"]
    assert [o["name"] for o in choices["llm"] if o["default"]] == ["anthropic"]
    assert [o["name"] for o in choices["stt"] if o["default"]] == ["assemblyai"]


def test_ears_configured_off_stay_off_even_with_recognizer_keys(
    llm: FakeLLM, tts: FakeTTS, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--stt none` means deaf. With a recognizer key in the environment it was
    offered anyway, and the page asked for a microphone."""
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "sk-test")
    monkeypatch.setenv("VOICE_AGENT_STT", "none")
    client = TestClient(create_app(llm=llm, tts=tts, greeting=""))
    known = served_facts(client.get(f"/c/{start(client)}").text)
    choices = known["choices"]
    assert isinstance(choices, dict)

    assert known["ears"] is None
    assert choices["stt"] == []


def test_the_page_says_what_each_recognizer_hears_without_holding_its_key(
    llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chapter 12's lesson, at the moment of choosing rather than mid-sentence."""
    client = stacked(llm, tts, stt, monkeypatch)
    choices = served_facts(client.get(f"/c/{start(client)}").text)["choices"]
    assert isinstance(choices, dict)
    heard = {o["name"]: o["languages"] for o in choices["stt"]}

    assert "ru" not in heard["assemblyai"]
    assert "rus" in heard["elevenlabs"]


def test_the_query_string_chooses_the_stack(
    llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket opening is what starts a conversation, so the choice rides
    with it."""
    store = SessionStore()
    stacked(llm, tts, stt, monkeypatch)  # for the environment it sets
    client = TestClient(create_app(llm=llm, tts=tts, stt=stt, store=store, greeting=""))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}?llm=deepseek&stt=elevenlabs") as socket:
        receive(socket)

    assert (store.get(key).engine, store.get(key).ears) == ("deepseek", "elevenlabs")


def test_a_reconnect_keeps_the_stack_it_started_on(
    llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history was produced by the engine already chosen and the prompt
    names the ears already chosen; a reconnect that swapped either would leave
    the agent contradicting its own transcript."""
    client = stacked(llm, tts, stt, monkeypatch)
    store = SessionStore()
    client = TestClient(create_app(llm=llm, tts=tts, stt=stt, store=store, greeting=""))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}?llm=deepseek&stt=elevenlabs") as socket:
        receive(socket)
    chose = (store.get(key).engine, store.get(key).ears)

    with client.websocket_connect(f"/ws/{key}?llm=anthropic&stt=assemblyai") as socket:
        receive(socket)

    assert (store.get(key).engine, store.get(key).ears) == chose
    assert chose == ("deepseek", "elevenlabs")


@pytest.mark.parametrize("query", ["?llm=bogus&stt=bogus", "?llm=openai", ""])
def test_an_unknown_or_keyless_choice_falls_back_to_the_default(
    llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """A URL a stranger can type, not configuration — so it is repaired rather
    than refused. `openai` is a real backend with no key here, which is the
    same situation from the visitor's side."""
    store = SessionStore()
    for name in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "ASSEMBLYAI_API_KEY"):
        monkeypatch.setenv(name, "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("VOICE_AGENT_PROVIDER", "anthropic")
    client = TestClient(create_app(llm=llm, tts=tts, stt=stt, store=store, greeting=""))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}{query}") as socket:
        receive(socket)

    assert store.get(key).engine == "anthropic"


def test_the_page_names_the_model_the_deployment_would_actually_run(
    llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found on the deployed page: it advertised `claude-opus-5` — the
    registry's default for the provider — beside a deployment configured for
    `claude-haiku-4-5`. Every other fact on that screen is assembled in one
    place precisely so the page cannot say what the server will not do, and
    the model had slipped out of that arrangement."""
    stacked(llm, tts, stt, monkeypatch)
    monkeypatch.setenv("VOICE_AGENT_MODEL", "claude-haiku-4-5")
    client = TestClient(create_app(tts=tts, stt=stt, greeting=""))

    choices = served_facts(client.get(f"/c/{start(client)}").text)["choices"]
    assert isinstance(choices, dict)
    advertised = {o["name"]: o["model"] for o in choices["llm"]}

    assert advertised["anthropic"] == "claude-haiku-4-5"
    # And the override belongs to its provider: DeepSeek keeps its own default,
    # since asking DeepSeek for a Claude model would simply fail.
    assert advertised["deepseek"] == "deepseek-chat"
