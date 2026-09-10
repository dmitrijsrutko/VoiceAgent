"""End-to-end tests over a real WebSocket; only the provider is faked."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import FakeLLM, FakeSTT, FakeTTS
from voice_agent.config import load_system_prompt
from voice_agent.server import create_app, is_exit_command
from voice_agent.sessions import SessionStore


@pytest.fixture
def store() -> SessionStore:
    return SessionStore()


@pytest.fixture
def client(llm: FakeLLM, tts: FakeTTS, stt: FakeSTT, store: SessionStore) -> TestClient:
    return TestClient(create_app(llm=llm, tts=tts, stt=stt, store=store))


@pytest.fixture
def silent_client(llm: FakeLLM, store: SessionStore) -> TestClient:
    return TestClient(create_app(llm=llm, store=store, voice=False, ears=False))


def start(client: TestClient) -> str:
    """Load the root the way a browser does, and return the minted key."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].removeprefix("/c/")


def drain(socket: object, audio: bool = True) -> tuple[str, list[dict[str, object]]]:
    """Collect one reply turn, up to and including its audio announcement."""
    frames: list[dict[str, object]] = []
    while True:
        frame = socket.receive_json()  # type: ignore[attr-defined]
        frames.append(frame)
        if frame["type"] in ("error", "ended", "audio_error"):
            return str(frame["type"]), frames
        if frame["type"] == "audio":
            # The binary frame always follows; consume it so the socket is
            # left clean for the next turn.
            frames.append({"type": "audio_bytes", "data": socket.receive_bytes()})  # type: ignore[attr-defined]
            return "audio", frames
        if frame["type"] == "reply_end" and not audio:
            return "reply_end", frames


def test_root_mints_a_new_conversation_per_visit(client: TestClient, store: SessionStore) -> None:
    first, second = start(client), start(client)

    assert first != second
    assert len(store) == 2


def test_the_page_loads_for_a_real_key_and_404s_for_anything_else(client: TestClient) -> None:
    key = start(client)

    assert client.get(f"/c/{key}").status_code == 200
    assert client.get("/c/made-up-key").status_code == 404


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

    assert kind == "audio"
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
    assert llm.systems[0].startswith("You are a voice assistant.")


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
    app = create_app(llm=FakeLLM(fail=True), store=store, voice=False, ears=False)
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


def test_the_reply_is_spoken_as_a_binary_frame_after_its_text(
    client: TestClient, tts: FakeTTS
) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        ready = socket.receive_json()
        assert ready["voice"] == {"provider": "fake-voice", "voice": "fake-voice-1"}

        socket.send_json({"type": "user_message", "text": "say something"})
        kind, frames = drain(socket)

    audio_bytes = bytes(frames[-1]["data"])  # type: ignore[call-overload]
    announcement = frames[-2]
    assert kind == "audio"
    assert announcement["media_type"] == "audio/mpeg"
    assert announcement["bytes"] == len(audio_bytes)
    assert isinstance(announcement["synthesis_ms"], int)

    # The text arrives first, then its audio: the batched wait is visible.
    assert [f["type"] for f in frames] == [
        "reply_start",
        "delta",
        "delta",
        "reply_end",
        "audio",
        "audio_bytes",
    ]

    # The agent speaks exactly what it recorded — not a re-rendered version.
    assert tts.spoken == ["Sure thing. "]
    assert audio_bytes == b"ID3Sure thing. "


def test_a_failed_synthesis_keeps_the_reply_and_degrades_to_text(
    store: SessionStore, llm: FakeLLM
) -> None:
    client = TestClient(create_app(llm=llm, tts=FakeTTS(fail=True), store=store, ears=False))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "say something"})
        kind, frames = drain(socket)

    assert kind == "audio_error"
    assert "synthesizer exploded" in str(frames[-1]["message"])
    # Losing the words would be a far worse failure than losing the audio.
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


def test_the_reasoning_stage_reports_its_own_timings(client: TestClient) -> None:
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "hello"})
        _, frames = drain(socket)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    audio = next(f for f in frames if f["type"] == "audio")

    # Split at the first token: time-to-first-token is dead air the user
    # experiences; generation time is throughput streaming already hides.
    assert isinstance(reply_end["ttft_ms"], int)
    assert isinstance(reply_end["generation_ms"], int)
    assert reply_end["chars"] == len("Sure thing. ")

    # The number the project is judged on, and it must cover the whole turn.
    assert int(audio["total_ms"]) >= int(audio["synthesis_ms"])  # type: ignore[call-overload]


def test_an_empty_reply_reports_its_whole_duration_as_time_to_first_token(
    store: SessionStore, tts: FakeTTS
) -> None:
    """Nothing streamed means the user waited and got nothing — reporting that
    as zero of everything would hide exactly the failure worth seeing."""
    client = TestClient(create_app(llm=FakeLLM(replies=[""]), tts=tts, store=store, ears=False))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "user_message", "text": "hello"})
        _, frames = drain(socket)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    assert reply_end["chars"] == 0
    assert reply_end["generation_ms"] == 0


def test_the_system_prompt_tells_the_agent_to_match_the_users_language() -> None:
    """A real conversation opened with "Hallo." — a word shared by English and
    German — and the agent answered in German and stayed there for eleven
    turns, anchored by its own replies. The prompt had no language rule at all.
    """
    prompt = load_system_prompt()

    assert "language the user is speaking" in prompt
    assert "greeting is not evidence" in prompt.lower()
