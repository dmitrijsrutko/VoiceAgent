"""End-to-end listening over a real WebSocket; only the recognizer is faked."""

import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM, FakeSTT, FakeTTS
from voice_agent import server
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore
from voice_agent.stt.base import Transcript

FRAME = b"\x00\x01" * 800  # 100 ms of 16 kHz PCM16, as the browser sends it


@pytest.fixture
def store() -> SessionStore:
    return SessionStore()


def start(client: TestClient) -> str:
    return client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")


def build(store: SessionStore, stt: FakeSTT, llm: FakeLLM | None = None) -> TestClient:
    return TestClient(
        create_app(llm=llm or FakeLLM(), tts=FakeTTS(), stt=stt, store=store, voice=False)
    )


def test_speaking_produces_volatile_then_committed_text(store: SessionStore) -> None:
    stt = FakeSTT()
    client = build(store, stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        assert socket.receive_json() == {"type": "listening", "active": True}

        for _ in range(3):
            socket.send_bytes(FRAME)

        transcripts = [socket.receive_json() for _ in range(3)]

    # Volatile hypotheses get rewritten; only the last one is committed.
    assert [(t["text"], t["final"]) for t in transcripts] == [
        ("what is", False),
        ("what is the capital", False),
        ("What is the capital of Latvia?", True),
    ]
    # Endpointing cost is measured from the last new words to the commit.
    assert isinstance(transcripts[-1]["endpoint_ms"], int)
    assert stt.heard == [FRAME] * 3


def test_a_committed_transcript_starts_a_turn_by_itself(store: SessionStore) -> None:
    """Speech becomes a user message. Nothing downstream knows it was spoken."""
    llm = FakeLLM()
    client = build(store, FakeSTT(), llm)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        for _ in range(3):
            socket.send_bytes(FRAME)

        kinds = []
        while "reply_end" not in kinds:
            kinds.append(socket.receive_json()["type"])

    assert kinds == [
        "transcript",
        "transcript",
        "transcript",
        "reply_start",
        "delta",
        "delta",
        "reply_end",
    ]
    assert [(m.role, m.content) for m in store.get(key).messages] == [
        ("user", "What is the capital of Latvia?"),
        ("assistant", "Sure thing. "),
    ]
    # The reasoning engine received it as an ordinary user turn.
    assert [m.content for m in llm.seen[0]] == ["What is the capital of Latvia?"]


def test_volatile_text_never_reaches_the_reasoning_engine(store: SessionStore) -> None:
    """Acting on a hypothesis means acting on words the user never said."""
    llm = FakeLLM()
    stt = FakeSTT(script=[Transcript("delete every", is_final=False)])
    client = build(store, stt, llm)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        socket.send_bytes(FRAME)
        assert socket.receive_json()["final"] is False

    assert llm.seen == []
    assert store.get(key).messages == []


def test_an_empty_commit_is_not_reported_at_all(store: SessionStore) -> None:
    """Silence that endpoints must not send an empty question to the model, and
    must not put an empty bubble and a meaningless endpointing figure on screen.
    A session's closing flush produces exactly this."""
    llm = FakeLLM()
    stt = FakeSTT(
        script=[
            Transcript("   ", is_final=True),
            Transcript("a real one", is_final=False),
        ]
    )
    client = build(store, stt, llm)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        socket.send_bytes(FRAME)  # the blank commit
        socket.send_bytes(FRAME)  # a real partial

        # The blank one produced no frame, so this is the first thing back.
        assert socket.receive_json()["text"] == "a real one"

    assert llm.seen == []
    assert store.get(key).messages == []


def test_saying_exit_ends_the_conversation(store: SessionStore) -> None:
    """A spoken exit is the same exit as a typed one."""
    stt = FakeSTT(script=[Transcript("Goodbye.", is_final=True)])
    client = build(store, stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        socket.send_bytes(FRAME)
        assert socket.receive_json()["final"] is True
        assert socket.receive_json() == {"type": "ended"}

    assert store.get(key).ended


def test_listening_stops_on_request(store: SessionStore) -> None:
    client = build(store, FakeSTT())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        assert socket.receive_json()["active"] is True
        socket.send_json({"type": "listen_stop"})
        assert socket.receive_json() == {"type": "listening", "active": False}


def test_audio_before_listening_starts_is_dropped_not_buffered(store: SessionStore) -> None:
    stt = FakeSTT()
    client = build(store, stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_bytes(FRAME)  # user's mic is on but they never pressed listen
        socket.send_json({"type": "listen_start"})
        assert socket.receive_json()["active"] is True

    assert stt.heard == []


def test_a_failing_recognizer_reports_and_leaves_typing_working(store: SessionStore) -> None:
    client = build(store, FakeSTT(fail=True))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        socket.send_bytes(FRAME)
        error = socket.receive_json()

        assert error["type"] == "listen_error"
        assert "recognizer exploded" in error["message"]

        # Ears are not load-bearing for typing.
        socket.send_json({"type": "user_message", "text": "typed instead"})
        while socket.receive_json()["type"] != "reply_end":
            pass

    assert [m.content for m in store.get(key).messages] == ["typed instead", "Sure thing. "]


def test_a_deaf_agent_says_so_rather_than_failing_silently(store: SessionStore) -> None:
    client = TestClient(create_app(llm=FakeLLM(), store=store, voice=False, ears=False))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        assert socket.receive_json()["ears"] is None
        socket.send_json({"type": "listen_start"})
        assert socket.receive_json() == {
            "type": "listen_error",
            "message": "this agent has no ears",
        }


def test_the_ready_frame_advertises_the_capture_rate(store: SessionStore) -> None:
    """The browser opens its AudioContext at exactly this rate, so that a
    mismatch is a configuration bug rather than a silent resample."""
    client = build(store, FakeSTT())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        assert socket.receive_json()["ears"] == {"provider": "fake-ears", "sample_rate": 16000}


def test_listening_expires_after_a_silent_stretch(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamed silence is billed, and a backgrounded tab keeps the microphone
    running, so an abandoned session has to close itself."""
    monkeypatch.setattr(server, "IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(server, "WATCHDOG_TICK_SECONDS", 0.01)
    client = build(store, FakeSTT(script=[]))  # ears that hear only silence
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        assert socket.receive_json()["active"] is True
        socket.send_bytes(FRAME)  # audio arriving; nobody talking

        expiry = socket.receive_json()

    assert expiry["type"] == "listening"
    assert expiry["active"] is False
    assert "no speech" in expiry["reason"]


def test_speaking_keeps_the_session_alive(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "IDLE_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(server, "WATCHDOG_TICK_SECONDS", 0.01)
    stt = FakeSTT(script=[Transcript(f"word {i}", is_final=False) for i in range(6)])
    client = build(store, stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()

        for _ in range(6):
            time.sleep(0.1)  # longer in total than the idle window
            socket.send_bytes(FRAME)
            assert socket.receive_json()["type"] == "transcript"


def test_the_session_cap_stops_even_a_talkative_room(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the idle timer cannot catch: continuous partials forever."""
    monkeypatch.setattr(server, "IDLE_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(server, "SESSION_CAP_SECONDS", 0.05)
    monkeypatch.setattr(server, "WATCHDOG_TICK_SECONDS", 0.01)
    client = build(store, FakeSTT())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()

        expiry = socket.receive_json()

    assert expiry["active"] is False
    assert "stopped after" in expiry["reason"]


def test_the_agent_talking_does_not_count_against_the_user(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser stops sending audio while the agent replies, so no
    transcript can arrive. An unpaused timer would blame the user for that.

    Asserted *after* the turn rather than during it: an expiry that fires
    mid-turn cannot announce itself until the turn releases, so looking only at
    the frames up to `reply_end` would pass even with the pause removed. This
    test was verified to fail when `Mic.busy` is disabled.
    """
    monkeypatch.setattr(server, "IDLE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(server, "WATCHDOG_TICK_SECONDS", 0.01)
    stt = FakeSTT(
        script=[
            Transcript("hello there", is_final=True),
            Transcript("still here", is_final=False),
        ]
    )
    client = build(store, stt, FakeLLM(delay=0.5))  # a reply longer than the idle window
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()

        socket.send_bytes(FRAME)
        while socket.receive_json()["type"] != "audio":
            pass
        socket.receive_bytes()  # the reply's audio, which the turn also covers

        # Still listening: the next frame is speech, not an expiry notice.
        socket.send_bytes(FRAME)
        following = socket.receive_json()

    assert following["type"] == "transcript", f"listening expired during the turn: {following}"
