"""The role reaches both halves of the agent: the one that speaks (greeting and
system prompt) and the one that thinks (the inner voice, fed by the floor)."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM
from tests.test_session import RecordingChannel
from voice_agent import roles
from voice_agent.config import DEFAULT_GREETING
from voice_agent.conversation import Conversation
from voice_agent.server import create_app
from voice_agent.session import Session
from voice_agent.sessions import SessionStore

pytestmark = pytest.mark.anyio

ROLE = roles.load("devils_advocate")
NOTHING = json.dumps({"notes": "", "thought": None})


def role_session(thinker: FakeLLM, llm: FakeLLM | None = None) -> tuple[Session, RecordingChannel]:
    channel = RecordingChannel()
    session = Session(
        channel,  # type: ignore[arg-type]
        Conversation(id="t"),
        llm or FakeLLM(),
        None,
        "system",
        None,
        (),
        role=ROLE,
        thinker=thinker,
    )
    return session, channel


async def test_a_pause_asks_the_inner_voice() -> None:
    thinker = FakeLLM([NOTHING])
    session, channel = role_session(thinker)

    await session._on_floor("micro_pause")
    await channel.wait_for("thought")

    assert channel.frames[-1]["reason"] == "micro_pause"
    await session.close()


async def test_the_inner_voice_is_asked_again_after_the_reply() -> None:
    thinker = FakeLLM([NOTHING])
    session, channel = role_session(thinker)

    await session.submit("Cities should ban cars.")
    await channel.wait_for("thought")

    assert channel.frames[-1]["reason"] == "reply"
    assert "Cities should ban cars." in thinker.seen[0][0].content
    await session.close()


async def test_the_floor_is_ignored_while_the_reply_runs() -> None:
    """Speech over the agent's own reply is echo or an interruption: not a
    pause in the user's argument."""
    thinker = FakeLLM([NOTHING])
    session, channel = role_session(thinker, FakeLLM(["one two three"], pace=0.05))

    await session.submit("hello")
    await asyncio.sleep(0.02)  # the reply is being written
    await session._on_floor("micro_pause")
    await channel.wait_for("thought")

    assert [f["reason"] for f in channel.frames if f["type"] == "thought"] == ["reply"]
    await session.close()


async def test_no_role_means_no_inner_voice() -> None:
    thinker = FakeLLM([NOTHING])
    channel = RecordingChannel()
    session = Session(
        channel,  # type: ignore[arg-type]
        Conversation(id="t"),
        FakeLLM(),
        None,
        "system",
        None,
        (),
        thinker=thinker,
    )

    await session._on_floor("micro_pause")
    await asyncio.sleep(0.05)

    assert not thinker.seen
    await session.close()


def app_with(llm: FakeLLM, preselected: str | None = None) -> TestClient:
    return TestClient(
        create_app(llm=llm, store=SessionStore(), voice=False, ears=False, role=preselected)
    )


def converse(
    client: TestClient, key: str, query: str = "", greeted: bool = True
) -> tuple[dict[str, object], str]:
    """Connect, take the ready frame and the greeting, say one thing. A
    reconnect is not greeted again (`greeted=False`)."""
    greeting = ""
    with client.websocket_connect(f"/ws/{key}{query}") as socket:
        ready = socket.receive_json()
        while greeted and (frame := socket.receive_json())["type"] != "greeting":
            pass
        if greeted:
            greeting = str(frame["text"])
        socket.send_text(json.dumps({"type": "user_message", "text": "hi"}))
        while socket.receive_json()["type"] != "reply_end":
            pass
    return ready, greeting


def mint(client: TestClient) -> str:
    return client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")


def test_a_conversation_that_picks_the_role_plays_it() -> None:
    llm = FakeLLM()
    with app_with(llm) as client:
        ready, greeting = converse(client, mint(client), "?role=devils_advocate")

    assert greeting == ROLE.opening
    assert ready["role"] == {"slug": ROLE.slug, "name": ROLE.name, "summary": ROLE.summary}
    assert "# Your role in this conversation" in llm.systems[0]


def test_by_default_the_devil_s_advocate_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICE_AGENT_ROLE", raising=False)
    llm = FakeLLM()
    with app_with(llm) as client:
        ready, greeting = converse(client, mint(client))

    assert greeting == ROLE.opening
    assert ready["role"] == {"slug": ROLE.slug, "name": ROLE.name, "summary": ROLE.summary}


def test_the_operator_may_still_run_the_plain_assistant() -> None:
    """Never offered on the page, but `VOICE_AGENT_ROLE=none` (as the test
    suite sets) still runs every conversation without a role."""
    llm = FakeLLM()
    with app_with(llm) as client:
        ready, greeting = converse(client, mint(client))

    assert greeting == DEFAULT_GREETING
    assert ready["role"] is None
    assert "Your role in this conversation" not in llm.systems[0]


@pytest.mark.parametrize("asked", ["../../etc", "none"])
def test_an_unknown_role_falls_back_to_the_default(asked: str) -> None:
    """`none` included: the plain assistant is not something a link can ask for."""
    with app_with(FakeLLM(), preselected="devils_advocate") as client:
        ready, greeting = converse(client, mint(client), f"?role={asked}")

    assert ready["role"] == {"slug": ROLE.slug, "name": ROLE.name, "summary": ROLE.summary}
    assert greeting == ROLE.opening


def test_a_reconnect_keeps_the_role_it_started_with() -> None:
    """The history was argued in role; resuming the link must not drop it."""
    llm = FakeLLM()
    with app_with(llm) as client:
        key = mint(client)
        converse(client, key, "?role=devils_advocate")
        ready, _ = converse(client, key, "?role=none", greeted=False)

    assert ready["role"] == {"slug": ROLE.slug, "name": ROLE.name, "summary": ROLE.summary}
    assert all("Your role in this conversation" in system for system in llm.systems)


def test_the_start_screen_offers_only_cards_and_preselected_as_asked() -> None:
    with app_with(FakeLLM(), preselected="devils_advocate") as client:
        page = client.get(f"/c/{mint(client)}").text

    served = json.loads(page.split('id="facts" type="application/json">')[1].split("</script>")[0])
    offered = served["choices"]["role"]
    assert [o["name"] for o in offered] == ["devils_advocate", "thinking_partner"]
    assert [o["name"] for o in offered if o["default"]] == ["devils_advocate"]


def test_a_broken_role_stops_the_server_at_startup() -> None:
    from voice_agent.errors import ConfigError

    with pytest.raises(ConfigError, match="no role"):
        create_app(llm=FakeLLM(), voice=False, ears=False, role="nobody")


def test_a_role_conversation_thinks_and_a_plain_one_does_not() -> None:
    """One process, both kinds of conversation: the inner voice goes only
    where a role was picked."""
    thinker = FakeLLM([NOTHING])
    app = create_app(llm=FakeLLM(), store=SessionStore(), voice=False, ears=False, thinker=thinker)
    with TestClient(app) as client:
        converse(client, mint(client))
        assert not thinker.seen
        converse(client, mint(client), "?role=devils_advocate")

    assert thinker.seen


async def test_nothing_thinks_after_the_socket_closed() -> None:
    thinker = FakeLLM([NOTHING])
    session, _ = role_session(thinker)
    await session.close()

    await session._on_floor("speaking")
    await session._on_floor("micro_pause")
    await asyncio.sleep(0.05)

    assert not thinker.seen
