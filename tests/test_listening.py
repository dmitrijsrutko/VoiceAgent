"""End-to-end listening over a real WebSocket; only the recognizer is faked."""

import asyncio
import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM, FakeSTT, FakeTTS, receive
from voice_agent import mic as mic_module
from voice_agent.errors import ProviderError
from voice_agent.llm.base import Warmth
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
        assert text_frame(socket)["ears"] == {"provider": "fake-ears", "sample_rate": 16000}


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


def test_only_agreed_text_is_ever_warmed(store: SessionStore) -> None:
    """Warming on a hypothesis prefills a prompt the real call will not match:
    the spend with none of the benefit. Only twice-agreed words go up."""
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

    assert llm.warmed, "nothing was warmed at all"
    warmed = [m[-1].content for m in llm.warmed]
    assert warmed == ["what is the capital"], warmed
    # "of Latvia" was only ever said once before the commit, so it never settled.
    assert all("Latvia" not in text for text in warmed)


def test_the_turn_still_sends_the_committed_text_not_the_warmed_prefix(
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
    assert commit["warms"] == 1
    assert commit["warm_cached_tokens"] == 1024
    assert isinstance(commit["warm_lead_ms"], int)


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


def test_a_failing_warm_costs_only_the_warm(store: SessionStore) -> None:
    """Warming is an optimisation. It must never be able to break a turn."""
    llm = FakeLLM()

    async def explode(system: str, messages: object) -> object:
        raise ProviderError("warm exploded")

    llm.warm = explode  # type: ignore[method-assign, assignment]
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

    assert [m.content for m in store.get(key).messages] == [
        "What is the capital of Latvia?",
        "Sure thing. ",
    ]


def test_each_turn_warms_again_rather_than_only_the_first(store: SessionStore) -> None:
    """The growth counter was never reset between turns, so after turn one the
    throttle blocked every later warm. A real session showed warm lines on some
    turns and not others because of it."""
    llm = FakeLLM()
    script = [
        Transcript("what is the capital", is_final=False),
        Transcript("what is the capital of Latvia", is_final=False),
        Transcript("What is the capital of Latvia?", is_final=True),
        Transcript("and how many people", is_final=False),
        Transcript("and how many people live there", is_final=False),
        Transcript("And how many people live there?", is_final=True),
    ]
    client = build(store, FakeSTT(script=script), llm)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        text_frame(socket)
        socket.send_json({"type": "listen_start"})
        text_frame(socket)
        replies = 0
        for _ in range(6):
            socket.send_bytes(FRAME)
        while replies < 2:
            frame = receive(socket)
            if frame["type"] == "reply_end":
                replies += 1

    assert len(llm.warmed) == 2, f"only {len(llm.warmed)} turns warmed: {llm.warmed}"
    assert llm.warmed[0][-1].content == "what is the capital"
    assert llm.warmed[1][-1].content == "and how many people"


def test_a_warm_that_lands_after_its_turn_is_not_credited_to_the_next(
    store: SessionStore,
) -> None:
    """Real spend, no benefit — and reporting it against the following turn
    produced an impossible lead of 7.6 s on a two-second utterance.

    Turn two is written so that nothing ever agrees (every partial starts with
    a different word), so it warms nothing of its own. Any warm it reports can
    only have leaked from turn one.
    """
    release = threading.Event()

    async def blocked_warm(system: str, messages: object) -> Warmth:
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
        return Warmth(prompt_tokens=1200, cached_tokens=1024)

    llm = FakeLLM()
    llm.warm = blocked_warm  # type: ignore[method-assign]
    script = [
        Transcript("alpha beta", is_final=False),
        Transcript("alpha beta gamma", is_final=False),  # agrees -> warms, and blocks
        Transcript("Alpha beta gamma.", is_final=True),
        Transcript("xxx one", is_final=False),
        Transcript("yyy two", is_final=False),  # never agrees -> no warm of its own
        Transcript("Zzz three.", is_final=True),
    ]
    client = build(store, FakeSTT(script=script), llm)
    key = start(client)

    def run_turn(socket: object) -> dict[str, object]:
        commit: dict[str, object] = {}
        for _ in range(3):
            socket.send_bytes(FRAME)  # type: ignore[attr-defined]
        while True:
            frame = receive(socket)
            if frame["type"] == "transcript" and frame["final"]:
                commit = frame
            if frame["type"] == "reply_end":
                return commit

    try:
        with client.websocket_connect(f"/ws/{key}") as socket:
            text_frame(socket)
            socket.send_json({"type": "listen_start"})
            text_frame(socket)

            run_turn(socket)
            release.set()  # turn one's warm completes only now, far too late
            time.sleep(0.2)  # ...and lands squarely inside turn two's window
            second = run_turn(socket)
    finally:
        release.set()

    assert second["warms"] == 0, f"turn two inherited turn one's warm: {second}"


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
    assert llm.warmed, "agreement was wedged, so nothing warmed"
    assert llm.warmed[-1][-1].content == "what is the weather"


def test_a_warm_that_does_not_land_in_time_is_still_counted(store: SessionStore) -> None:
    """A missing warm line is otherwise ambiguous between "nothing was warmed"
    and "a warm was billed and arrived too late to help" — and those two want
    opposite responses from whoever is reading the screen."""
    release = threading.Event()

    async def slow_warm(system: str, messages: object) -> Warmth:
        await asyncio.get_running_loop().run_in_executor(None, release.wait, 5)
        return Warmth(prompt_tokens=1200, cached_tokens=1024)

    llm = FakeLLM()
    llm.warm = slow_warm  # type: ignore[method-assign]
    client = build(store, FakeSTT(script=AGREEING), llm)
    key = start(client)

    try:
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
    finally:
        release.set()

    assert frame["warms"] == 0, "a warm that never returned was reported as landed"
    assert frame["warms_attempted"] == 1, "the warm that was paid for is invisible"


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
