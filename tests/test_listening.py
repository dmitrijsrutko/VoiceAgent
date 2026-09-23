"""End-to-end listening over a real WebSocket; only the recognizer is faked."""

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM, FakeSTT, FakeTTS, receive
from voice_agent import mic as mic_module
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore
from voice_agent.stt.base import Transcript

FRAME = b"\x00\x01" * 800  # 100 ms of 16 kHz PCM16, as the browser sends it


@pytest.fixture
def store() -> SessionStore:
    return SessionStore()


def start(client: TestClient) -> str:
    return client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")


def text_frame(socket: object) -> dict[str, Any]:
    """The next frame that is not the reply's audio. Speech now streams while
    the reply is still being written, so its frames interleave with the text
    these tests are about, in an order that depends on scheduling."""
    while (frame := receive(socket))["type"] in (
        "audio_bytes",
        "audio_start",
        "audio_end",
        "marks",
    ):
        pass
    return frame


def build(store: SessionStore, stt: FakeSTT, llm: FakeLLM | None = None) -> TestClient:
    return TestClient(
        create_app(
            llm=llm or FakeLLM(), tts=FakeTTS(), stt=stt, store=store, voice=False, greeting=""
        )
    )


def test_speaking_produces_volatile_then_committed_text(store: SessionStore) -> None:
    stt = FakeSTT()
    client = build(store, stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        assert text_frame(socket) == {"type": "listening", "active": True}

        for _ in range(3):
            socket.send_bytes(FRAME)

        transcripts = [text_frame(socket) for _ in range(3)]

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
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        for _ in range(3):
            socket.send_bytes(FRAME)

        kinds = []
        while "reply_end" not in kinds:
            kinds.append(text_frame(socket)["type"])

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
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        socket.send_bytes(FRAME)
        assert text_frame(socket)["final"] is False

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
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        socket.send_bytes(FRAME)  # the blank commit
        socket.send_bytes(FRAME)  # a real partial

        # The blank one produced no frame, so this is the first thing back.
        assert text_frame(socket)["text"] == "a real one"

    assert llm.seen == []
    assert store.get(key).messages == []


def test_words_the_recognizer_takes_back_are_taken_off_the_page(
    store: SessionStore,
) -> None:
    """An utterance that comes to nothing has to be taken back, not just
    dropped server-side.

    The page draws a bubble on the first partial and writes every later partial
    into that same bubble, so one left behind does not merely linger — the
    *next* utterance appears wherever the abandoned one was. Seen live: noise
    was transcribed and taken back, two unprompted lines arrived in the gap,
    and the question the user then asked was drawn above both of them, as
    though they had answered it before it was asked.
    """
    stt = FakeSTT(
        script=[
            Transcript("is anyone", is_final=False),  # noise, drawn on the page
            Transcript("  ", is_final=True),  # ... and committed to nothing
            Transcript("what is the time", is_final=False),
        ]
    )
    client = build(store, stt, FakeLLM())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        socket.send_bytes(FRAME)
        assert text_frame(socket)["text"] == "is anyone"
        socket.send_bytes(FRAME)
        assert text_frame(socket)["type"] == "transcript_dropped"
        socket.send_bytes(FRAME)
        assert text_frame(socket)["text"] == "what is the time"


def test_saying_exit_ends_the_conversation(store: SessionStore) -> None:
    """A spoken exit is the same exit as a typed one."""
    stt = FakeSTT(script=[Transcript("Goodbye.", is_final=True)])
    client = build(store, stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        socket.send_bytes(FRAME)
        assert text_frame(socket)["final"] is True
        assert text_frame(socket) == {"type": "ended"}

    assert store.get(key).ended


def test_listening_stops_on_request(store: SessionStore) -> None:
    client = build(store, FakeSTT())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        assert text_frame(socket)["active"] is True
        socket.send_json({"type": "listen_stop"})
        assert text_frame(socket) == {"type": "listening", "active": False}


def test_audio_before_listening_starts_is_dropped_not_buffered(store: SessionStore) -> None:
    stt = FakeSTT()
    client = build(store, stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_bytes(FRAME)  # user's mic is on but they never pressed listen
        socket.send_json({"type": "listen_start"})
        assert text_frame(socket)["active"] is True

    assert stt.heard == []


def test_a_failing_recognizer_reports_and_leaves_typing_working(store: SessionStore) -> None:
    client = build(store, FakeSTT(fail=True))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        socket.send_bytes(FRAME)
        error = text_frame(socket)

        assert error["type"] == "listen_error"
        assert "recognizer exploded" in error["message"]

        # Ears are not load-bearing for typing.
        socket.send_json({"type": "user_message", "text": "typed instead"})
        while text_frame(socket)["type"] != "reply_end":
            pass

    assert [m.content for m in store.get(key).messages] == ["typed instead", "Sure thing. "]


def test_a_deaf_agent_says_so_rather_than_failing_silently(store: SessionStore) -> None:
    client = TestClient(
        create_app(llm=FakeLLM(), store=store, voice=False, ears=False, greeting="")
    )
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        assert text_frame(socket)["ears"] is None
        socket.send_json({"type": "listen_start"})
        assert text_frame(socket) == {
            "type": "listen_error",
            "message": "this agent has no ears",
        }


def test_the_ready_frame_advertises_the_capture_rate(store: SessionStore) -> None:
    """The browser opens its AudioContext at exactly this rate, so that a
    mismatch is a configuration bug rather than a silent resample."""
    client = build(store, FakeSTT())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        # The rate is what this test is about; the frame also carries the
        # languages the recognizer understands, which has its own test.
        ears = text_frame(socket)["ears"]
        assert ears["provider"] == "fake-ears"
        assert ears["sample_rate"] == 16000


def test_listening_expires_after_a_silent_stretch(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamed silence is billed, and a backgrounded tab keeps the microphone
    running, so an abandoned session has to close itself."""
    monkeypatch.setattr(mic_module, "IDLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(mic_module, "WATCHDOG_TICK_SECONDS", 0.01)
    client = build(store, FakeSTT(script=[]))  # ears that hear only silence
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        assert text_frame(socket)["active"] is True
        socket.send_bytes(FRAME)  # audio arriving; nobody talking

        expiry = text_frame(socket)

    assert expiry["type"] == "listening"
    assert expiry["active"] is False
    assert "no speech" in expiry["reason"]


def test_speaking_keeps_the_session_alive(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mic_module, "IDLE_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(mic_module, "WATCHDOG_TICK_SECONDS", 0.01)
    stt = FakeSTT(script=[Transcript(f"word {i}", is_final=False) for i in range(6)])
    client = build(store, stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)

        for _ in range(6):
            time.sleep(0.1)  # longer in total than the idle window
            socket.send_bytes(FRAME)
            assert text_frame(socket)["type"] == "transcript"


def test_the_session_cap_stops_even_a_talkative_room(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the idle timer cannot catch: continuous partials forever."""
    monkeypatch.setattr(mic_module, "IDLE_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(mic_module, "SESSION_CAP_SECONDS", 0.05)
    monkeypatch.setattr(mic_module, "WATCHDOG_TICK_SECONDS", 0.01)
    client = build(store, FakeSTT())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)

        expiry = text_frame(socket)

    assert expiry["active"] is False
    assert "stopped after" in expiry["reason"]


def test_the_agent_talking_does_not_count_against_the_user(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user is not expected to speak while the agent replies. An unpaused
    timer would blame them for the agent talking.

    Asserted *after* the turn rather than during it: an expiry that fires
    mid-turn cannot announce itself until the turn releases, so looking only at
    the frames up to `reply_end` would pass even with the pause removed. This
    test was verified to fail when `Mic.busy` is disabled.
    """
    monkeypatch.setattr(mic_module, "IDLE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(mic_module, "WATCHDOG_TICK_SECONDS", 0.01)
    stt = FakeSTT(
        script=[
            Transcript("hello there", is_final=True),
            Transcript("still here", is_final=False),
        ]
    )
    client = build(store, stt, FakeLLM(delay=0.5))  # a reply longer than the idle window
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)

        socket.send_bytes(FRAME)
        while receive(socket)["type"] != "audio_end":
            pass  # through the reply's audio, which the turn also covers

        # Still listening: the next frame is speech, not an expiry notice. That
        # speech is over a reply the browser never said had finished, so it
        # also interrupts it — announced before the words themselves.
        socket.send_bytes(FRAME)
        while (following := text_frame(socket))["type"] == "interrupt":
            pass

    assert following["type"] == "transcript", f"listening expired during the turn: {following}"


AGREEING = [
    Transcript("what is the capital", is_final=False),
    Transcript("what is the capital of Latvia", is_final=False),
    Transcript("What is the capital of Latvia?", is_final=True),
]


def test_the_turn_still_sends_the_committed_text_not_the_agreed_prefix(
    store: SessionStore,
) -> None:
    llm = FakeLLM()
    client = build(store, FakeSTT(script=AGREEING), llm)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        for _ in range(3):
            socket.send_bytes(FRAME)
        while text_frame(socket)["type"] != "reply_end":
            pass

    assert store.get(key).messages[0].content == "What is the capital of Latvia?"


def test_the_commit_reports_whether_the_agreed_prefix_held(store: SessionStore) -> None:
    client = build(store, FakeSTT(script=AGREEING))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        for _ in range(3):
            socket.send_bytes(FRAME)
        frames: list[dict[str, Any]] = []
        while True:
            frame = receive(socket)
            frames.append(frame)
            if frame["type"] == "transcript" and frame["final"]:
                break

    commit = frames[-1]
    assert commit["prefix_held"] is True
    assert commit["stable_words"] == 4


def test_a_prefix_that_did_not_hold_is_reported_as_such(store: SessionStore) -> None:
    """If agreement settled on words the user never said, that must be visible
    rather than quietly wrong."""
    script = [
        Transcript("send it to Bob", is_final=False),
        Transcript("send it to Bob now", is_final=False),
        Transcript("Send it to Rob now.", is_final=True),
    ]
    client = build(store, FakeSTT(script=script))
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        for _ in range(3):
            socket.send_bytes(FRAME)
        while True:
            frame = receive(socket)
            if frame["type"] == "transcript" and frame["final"]:
                break

    assert frame["prefix_held"] is False


def test_stopping_mid_sentence_does_not_poison_the_next_utterance(
    store: SessionStore,
) -> None:
    """A session that ends without a commit — the user stopping mid-sentence,
    or a reconnect — used to carry its settled words forward. There they wedge
    agreement entirely and are then reported as words that "settled early" on
    an utterance nobody said them in."""
    llm = FakeLLM()
    abandoned = [
        Transcript("please cancel the order", is_final=False),
        Transcript("please cancel the order now", is_final=False),
    ]
    fresh = [
        Transcript("what is the weather", is_final=False),
        Transcript("what is the weather today", is_final=False),
        Transcript("What is the weather today?", is_final=True),
    ]
    client = build(store, FakeSTT(script=abandoned + fresh), llm)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        for _ in range(2):  # the abandoned half-sentence
            socket.send_bytes(FRAME)
            text_frame(socket)
        socket.send_json({"type": "listen_stop"})
        text_frame(socket)

        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        commit = None
        for _ in range(3):
            socket.send_bytes(FRAME)
        while True:
            frame = receive(socket)
            if frame["type"] == "transcript" and frame["final"]:
                commit = frame
            if frame["type"] == "reply_end":
                break

    assert commit is not None
    assert commit["prefix_held"] is True, f"stale words leaked into the next turn: {commit}"
    assert commit["stable_words"] == 4, commit


def test_stopping_the_mic_mid_reply_neither_waits_for_nor_cuts_the_reply(
    store: SessionStore,
) -> None:
    """A spoken turn used to run *inside* the microphone's task, so stopping
    the microphone had to wait for the whole reply — and after five seconds
    cancelled it, leaving a question with no answer in the history."""
    words = "one two three four five six seven eight nine ten"
    llm = FakeLLM(replies=[words], pace=0.05)
    client = build(store, FakeSTT(), llm)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        for _ in range(3):
            socket.send_bytes(FRAME)
        while text_frame(socket)["type"] != "reply_start":
            pass
        socket.send_json({"type": "listen_stop"})

        frames: list[dict[str, Any]] = []
        while not {"reply_end", "listening"} <= {f["type"] for f in frames}:
            frames.append(receive(socket))

    kinds = [f["type"] for f in frames]
    assert kinds.index("listening") < kinds.index("reply_end")
    assert frames[kinds.index("reply_end")]["text"].split() == words.split()
    assert [m.role for m in store.get(key).messages] == ["user", "assistant"]
