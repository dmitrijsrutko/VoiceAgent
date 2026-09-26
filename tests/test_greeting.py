"""The agent's opening line, spoken like any other reply."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM, FakeTTS, pcm_for, receive
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore
from voice_agent.tts.base import MEDIA_TYPE, AudioChunk

HELLO = "Hi, I'm a voice agent."


class SilentChannel:
    """A Channel that swallows everything; these tests are about state."""

    async def send_json(self, payload: dict[str, object]) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...


@pytest.fixture
def store() -> SessionStore:
    return SessionStore()


def open_conversation(client: TestClient) -> str:
    return client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")


def build(store: SessionStore, tts: FakeTTS | None = None, text: str = HELLO) -> TestClient:
    app = create_app(llm=FakeLLM(), tts=tts or FakeTTS(), store=store, ears=False, greeting=text)
    return TestClient(app)


def test_a_new_conversation_is_greeted_in_text_and_speech(store: SessionStore) -> None:
    tts = FakeTTS()
    client = build(store, tts)

    with client, client.websocket_connect(f"/ws/{open_conversation(client)}") as socket:
        socket.receive_json()  # ready
        greeting = socket.receive_json()
        start = socket.receive_json()
        pcm = b""
        while (frame := receive(socket))["type"] != "audio_end":
            if frame["type"] == "audio_bytes":
                pcm += frame["data"]
        end = frame

    assert greeting == {"type": "greeting", "text": HELLO}
    # Spoken through the same path as a live reply.
    assert start == {"type": "audio_start", "media_type": MEDIA_TYPE, "sample_rate": 24000}
    assert pcm == pcm_for(HELLO)
    assert end["bytes"] == len(pcm)
    assert tts.spoken == [HELLO]


def test_every_conversation_synthesises_its_own_greeting(store: SessionStore) -> None:
    """No cache: the greeting is voiced fresh, like any other reply."""
    tts = FakeTTS()
    client = build(store, tts)

    with client:
        for _ in range(3):
            with client.websocket_connect(f"/ws/{open_conversation(client)}") as socket:
                socket.receive_json()
                assert socket.receive_json()["type"] == "greeting"
                while receive(socket)["type"] != "audio_end":
                    pass

    assert tts.spoken == [HELLO] * 3


def test_the_greeting_becomes_part_of_the_conversation(store: SessionStore) -> None:
    """Otherwise the agent does not know it has already said hello, and greets
    again on the next turn."""
    client = build(store)

    with client:
        key = open_conversation(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            while receive(socket)["type"] != "audio_end":
                pass

    assert [(m.role, m.content) for m in store.get(key).messages] == [("assistant", HELLO)]


def test_the_model_is_told_about_the_greeting_rather_than_shown_it(store: SessionStore) -> None:
    """As a turn in the history, a fixed English greeting anchored the reply to
    a Russian question in English. The page still replays it; the model reads it
    as a fact in its instructions."""
    llm = FakeLLM()
    app = create_app(llm=llm, tts=FakeTTS(), store=store, ears=False, greeting=HELLO)
    with TestClient(app) as client:
        key = open_conversation(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            while receive(socket)["type"] != "audio_end":
                pass
            # Heard to the end, so the message does not cut the greeting short.
            socket.send_json({"type": "playback", "active": True})
            socket.send_json({"type": "playback", "active": False})
            socket.send_json({"type": "user_message", "text": "Привет"})
            while receive(socket)["type"] != "reply_end":
                pass

    assert [m.content for m in llm.seen[0]] == ["Привет"]
    assert HELLO in llm.systems[0]
    assert store.get(key).messages[0].content == HELLO


def test_a_cut_greeting_stays_out_of_the_models_context() -> None:
    """Talked over, the greeting is replaced by what was heard of it, and the
    replacement must not slip into the model's context."""
    from voice_agent.conversation import Conversation

    conversation = Conversation("k")
    conversation.opening = conversation.add_assistant(HELLO)
    conversation.add_user("hello?")
    conversation.replace(conversation.opening, "Hi, I'm")

    assert [m.content for m in conversation.context] == ["hello?"]
    assert [m.content for m in conversation.messages] == ["Hi, I'm", "hello?"]


def test_reconnecting_does_not_greet_again(store: SessionStore) -> None:
    client = build(store)

    with client:
        key = open_conversation(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            while receive(socket)["type"] != "audio_end":
                pass

        with client.websocket_connect(f"/ws/{key}") as socket:
            ready = socket.receive_json()
            socket.send_json({"type": "user_message", "text": "hello"})
            following = socket.receive_json()

    assert [m["content"] for m in ready["history"]] == [HELLO]
    assert following["type"] == "reply_start", "it greeted a second time"


def test_an_empty_greeting_opens_in_silence(store: SessionStore) -> None:
    tts = FakeTTS()
    client = build(store, tts, text="")

    with client:
        key = open_conversation(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            socket.receive_json()
            socket.send_json({"type": "user_message", "text": "hello"})
            assert socket.receive_json()["type"] == "reply_start"
            while receive(socket)["type"] != "audio_end":
                pass

    assert tts.spoken == ["Sure thing. "], "only the reply should have been spoken"
    assert store.get(key).messages[0].role == "user", "something greeted anyway"


def test_a_greeting_that_cannot_be_synthesised_is_still_said_in_text(
    store: SessionStore,
) -> None:
    """An agent that cannot greet aloud must still be able to converse."""
    client = build(store, FakeTTS(fail=True))

    with client:
        key = open_conversation(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            socket.receive_json()
            assert socket.receive_json() == {"type": "greeting", "text": HELLO}
            # Reported like any reply whose voice failed.
            assert socket.receive_json()["type"] == "audio_error"
            socket.send_json({"type": "user_message", "text": "hello"})
            assert socket.receive_json()["type"] == "reply_start"

    assert store.get(key).messages[0].content == HELLO


def test_a_silent_agent_greets_in_text(store: SessionStore) -> None:
    client = TestClient(
        create_app(llm=FakeLLM(), store=store, voice=False, ears=False, greeting=HELLO)
    )

    with client, client.websocket_connect(f"/ws/{open_conversation(client)}") as socket:
        socket.receive_json()
        assert socket.receive_json() == {"type": "greeting", "text": HELLO}


async def test_two_tabs_opening_the_same_link_greet_once() -> None:
    """Synthesis is awaited, so a check made after it would be stale by the time
    the greeting is appended. Two tabs on one link would both greet."""
    from voice_agent.conversation import Conversation
    from voice_agent.greeting import greet

    class SlowTTS(FakeTTS):
        async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
            await asyncio.sleep(0.05)  # long enough for the other tab to arrive
            async for chunk in super().stream(text):
                yield chunk

    conversation = Conversation(id="shared")
    channel = SilentChannel()
    speaker = SlowTTS()

    await asyncio.gather(
        greet(channel, conversation, speaker, HELLO),  # type: ignore[arg-type]
        greet(channel, conversation, speaker, HELLO),  # type: ignore[arg-type]
    )

    assert [m.content for m in conversation.messages] == [HELLO]
