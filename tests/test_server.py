"""End-to-end tests over a real WebSocket; only the provider is faked."""

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import FakeLLM, FakeSTT, FakeTTS, pcm_for, receive
from voice_agent.config import load_system_prompt
from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import Usage
from voice_agent.server import create_app
from voice_agent.session import is_exit_command
from voice_agent.sessions import SessionStore
from voice_agent.tts.base import MEDIA_TYPE


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

    # The number the project is judged on covers the whole turn up to the
    # first chunk — and so includes the provider's own wait for it.
    first_audio = int(audio["first_audio_ms"])  # type: ignore[call-overload]
    assert first_audio >= int(audio["synthesis_first_byte_ms"])  # type: ignore[call-overload]
    assert int(audio["synthesis_ms"]) >= int(audio["synthesis_first_byte_ms"])  # type: ignore[call-overload]


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
    prompt = load_system_prompt()

    assert "language the user is speaking" in prompt
    assert "greeting is not evidence" in prompt.lower()


async def test_the_mute_window_is_what_is_left_to_play_not_the_whole_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser starts playing the first chunk while the rest is still being
    made. Counting the full length from when synthesis *ends* would keep the
    user's silence uncounted for the time already spent playing."""
    import time

    from voice_agent import turn

    clock = [100.0]
    monkeypatch.setattr(time, "perf_counter", lambda: clock[0])

    class OneSecondTTS(FakeTTS):
        async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
            yield b"\x00" * 24_000  # half a second
            clock[0] += 0.3  # the provider is slow with the rest
            yield b"\x00" * 24_000

    class Sink:
        async def send_json(self, payload: dict[str, object]) -> None: ...

        async def send_bytes(self, data: bytes) -> None: ...

    speech = turn.Speech(Sink(), OneSecondTTS(), clock[0])  # type: ignore[arg-type]
    speech.say("hello")
    speech.finish()
    left = await speech.done()

    assert left == pytest.approx(0.7), "a 1 s reply, 0.3 s into playing it"
