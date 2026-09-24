"""The agent's opening line, and the cold start it exists to absorb."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM, FakeTTS, pcm_for, receive
from voice_agent import greeting as greeting_module
from voice_agent import roles
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore
from voice_agent.tts.base import MEDIA_TYPE, AudioChunk

HELLO = "Hi, I'm a voice agent."


ROLE_OPENINGS = {roles.load(slug).opening for slug in roles.available()}


def plain(spoken: list[str]) -> list[str]:
    """What was synthesised, less the role cards' openings, which every process
    prepares at startup whichever role a conversation picks."""
    return [text for text in spoken if text not in ROLE_OPENINGS]


class SilentChannel:
    """A Channel that swallows everything; these tests are about state."""

    async def send_json(self, payload: dict[str, object]) -> None: ...

    async def send_bytes(self, data: bytes) -> None: ...


@pytest.fixture(autouse=True)
def disposable_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The greeting cache is on disk by design; tests must not share one, and
    must not write into the working tree."""
    monkeypatch.setattr(greeting_module, "GREETING_CACHE", tmp_path / "cache")


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
        pcm = socket.receive_bytes()
        end = socket.receive_json()

    assert greeting == {"type": "greeting", "text": HELLO}
    # The same start / frames / end shape as a live reply: one audio path.
    assert start == {"type": "audio_start", "media_type": MEDIA_TYPE, "sample_rate": 24000}
    assert end["type"] == "audio_end" and end["cached"] is True
    assert end["chunks"] == 1
    assert pcm == pcm_for(HELLO)
    assert end["bytes"] == len(pcm)
    assert plain(tts.spoken) == [HELLO]


def test_the_greeting_is_synthesised_once_for_the_whole_process(store: SessionStore) -> None:
    """It never changes, so re-synthesising it per visitor would be paying the
    same cost repeatedly for the same bytes."""
    tts = FakeTTS()
    client = build(store, tts)

    with client:
        for _ in range(3):
            with client.websocket_connect(f"/ws/{open_conversation(client)}") as socket:
                socket.receive_json()
                assert socket.receive_json()["type"] == "greeting"
                while receive(socket)["type"] != "audio_end":
                    pass

    assert plain(tts.spoken) == [HELLO], "the greeting was synthesised more than once"


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

    assert plain(tts.spoken) == ["Sure thing. "], "only the reply should have been spoken"
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


async def test_the_greeting_survives_a_restart(tmp_path: Path) -> None:
    """A fixed sentence re-synthesised on every `uv run voice-agent` bills for
    bytes we already have. On a free plan that is a meaningful slice of a
    month's quota spent on words that never change."""
    from voice_agent.greeting import Greeting

    first, second = FakeTTS(), FakeTTS()
    await Greeting(HELLO, first, cache_dir=tmp_path).prepare()
    await Greeting(HELLO, second, cache_dir=tmp_path).prepare()

    assert first.spoken == [HELLO]
    assert second.spoken == [], "the second process re-synthesised what was already on disk"


def test_changing_the_greeting_does_not_serve_the_old_one(tmp_path: Path) -> None:
    from voice_agent.greeting import Greeting

    speaker = FakeTTS()
    original = Greeting("first version", speaker, cache_dir=tmp_path)
    changed = Greeting("second version", speaker, cache_dir=tmp_path)

    assert original._cache_file != changed._cache_file


async def test_a_failed_greeting_is_not_retried_for_every_visitor(tmp_path: Path) -> None:
    """An exhausted quota makes every attempt fail. Retrying per page load adds
    a doomed round trip to each one, for a greeting that will be text anyway."""
    from voice_agent.greeting import Greeting

    speaker = FakeTTS(fail=True)
    opening = Greeting(HELLO, speaker, cache_dir=tmp_path)

    for _ in range(3):
        await opening.prepare()

    assert len(speaker.spoken) == 1, f"tried {len(speaker.spoken)} times"


async def test_two_tabs_opening_the_same_link_greet_once(tmp_path: Path) -> None:
    """Preparation is awaited, so a check made before it is stale by the time
    the greeting is appended. Two tabs on one link would both greet."""
    from voice_agent.conversation import Conversation
    from voice_agent.greeting import Greeting

    class SlowTTS(FakeTTS):
        async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
            await asyncio.sleep(0.05)  # long enough for the other tab to arrive
            async for chunk in super().stream(text):
                yield chunk

    opening = Greeting(HELLO, SlowTTS(), cache_dir=tmp_path)
    conversation = Conversation(id="shared")
    channel = SilentChannel()

    await asyncio.gather(
        opening.deliver(channel, conversation),  # type: ignore[arg-type]
        opening.deliver(channel, conversation),  # type: ignore[arg-type]
    )

    assert [m.content for m in conversation.messages] == [HELLO]


def test_a_cache_in_another_audio_format_is_not_played(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read back as PCM, a greeting cached as MP3 by an earlier chapter does
    not fail — it plays as noise. The format is part of the key, so a cache in
    any other format is simply not found."""
    from voice_agent.greeting import Greeting

    speaker = FakeTTS()
    current = Greeting(HELLO, speaker, cache_dir=tmp_path)._cache_file
    monkeypatch.setattr(greeting_module, "MEDIA_TYPE", "audio/mpeg")
    other = Greeting(HELLO, speaker, cache_dir=tmp_path)._cache_file

    assert current != other


async def test_a_greeting_that_dies_mid_synthesis_is_not_cached(tmp_path: Path) -> None:
    """A truncated greeting written to disk would be replayed, truncated, on
    every visit from then on."""
    from voice_agent.greeting import Greeting

    await Greeting(HELLO, FakeTTS(fail_after=2), cache_dir=tmp_path).prepare()
    fresh = FakeTTS()
    await Greeting(HELLO, fresh, cache_dir=tmp_path).prepare()

    assert fresh.spoken == [HELLO], "a partial greeting was cached"


def write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def read(path: Path) -> bytes:
    return path.read_bytes()


def listing(directory: Path) -> list[str]:
    return [entry.name for entry in directory.iterdir()]


async def test_a_damaged_cache_file_is_resynthesised_not_replayed(tmp_path: Path) -> None:
    """Raw PCM has no structure to fail on: a file cut short loads as a shorter
    greeting and would be replayed, truncated, on every visit."""
    from voice_agent.greeting import Greeting

    speaker = FakeTTS()
    opening = Greeting(HELLO, speaker, cache_dir=tmp_path)
    write(opening._cache_file, pcm_for(HELLO)[:-1])  # an odd length: a split sample

    await opening.prepare()

    assert speaker.spoken == [HELLO], "a damaged cache file was trusted"
    assert read(opening._cache_file) == pcm_for(HELLO)


async def test_the_cache_is_written_atomically(tmp_path: Path) -> None:
    """Written aside and renamed, so an interrupted write never leaves a
    truncated greeting under the real name."""
    from voice_agent.greeting import Greeting

    opening = Greeting(HELLO, FakeTTS(), cache_dir=tmp_path)
    await opening.prepare()

    assert listing(tmp_path) == [opening._cache_file.name]
