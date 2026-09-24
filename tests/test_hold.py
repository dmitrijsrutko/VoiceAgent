"""A turn that looks unfinished waits a moment for the rest of the sentence.

Live (08:10 session): thinking pauses split "…при по" | "сещении ресторанов"
and "…едящие ст" | "ейк", and the agent answered each half.
"""

import asyncio

import pytest

from tests.conftest import FakeLLM, FakeSTT
from tests.test_session import session_for
from voice_agent import session as session_module
from voice_agent.conversation import Message
from voice_agent.session import looks_unfinished

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("text", "unfinished"),
    [
        ("Каждый раз при по", True),
        ("…и больше людей,", True),
        ("Вопрос не в этом, дело в том...", True),
        ("Вопрос не в этом, дело в том…", True),
        ("Давай обсудим стейк.", False),
        ("Ты знаешь, сколько людей едят стейк?", False),
        ("Отлично!", False),
        ("", False),
    ],
)
def test_what_looks_unfinished(text: str, unfinished: bool) -> None:
    assert looks_unfinished(text) is unfinished


@pytest.fixture
def quick_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "HOLD_SECONDS", 0.2)
    monkeypatch.setattr(session_module, "CONTINUATION_CAP_SECONDS", 0.5)


def listening_session(llm: FakeLLM) -> tuple[session_module.Session, object]:
    session, channel, _ = session_for(llm, FakeSTT(script=[]))
    return session, channel


async def test_the_rest_of_the_sentence_joins_the_first_half(quick_hold: None) -> None:
    llm = FakeLLM()
    session, channel = listening_session(llm)

    await session._spoken("Каждый раз при по")
    assert session.mic is not None
    session.mic.partial = "сещении"  # the user carries on within the hold
    await asyncio.sleep(0.15)
    session.mic.partial = ""
    await session._spoken("сещении ресторанов.")
    await channel.wait_for("reply_end")  # type: ignore[attr-defined]

    assert session.conversation.messages[0] == Message(
        "user", "Каждый раз при по сещении ресторанов."
    )
    assert len(llm.seen) == 1
    reply_end = next(f for f in channel.frames if f["type"] == "reply_end")  # type: ignore[attr-defined]
    assert reply_end.get("merged") == 2
    await session.close()


async def test_a_finished_sentence_is_answered_at_once(quick_hold: None) -> None:
    llm = FakeLLM(delay=0.0)
    session, _ = listening_session(llm)

    await session._spoken("Давай обсудим стейк.")
    await asyncio.sleep(0.05)

    assert llm.seen, "a finished sentence was held"
    await session.close()


async def test_a_fragment_nobody_continues_is_answered_after_the_hold(quick_hold: None) -> None:
    llm = FakeLLM()
    session, channel = listening_session(llm)

    await session._spoken("Вопрос не в этом, дело в том...")
    await asyncio.sleep(0.05)
    assert not llm.seen, "an unfinished turn was answered at once"
    await channel.wait_for("reply_end")  # type: ignore[attr-defined]

    assert session.conversation.messages[0].content == "Вопрос не в этом, дело в том..."
    await session.close()


async def test_typed_text_is_never_held(quick_hold: None) -> None:
    llm = FakeLLM()
    session, _ = listening_session(llm)

    await session.submit("и ещё")
    await asyncio.sleep(0.05)

    assert llm.seen
    await session.close()


async def test_closing_drops_a_held_fragment(quick_hold: None) -> None:
    llm = FakeLLM()
    session, _ = listening_session(llm)

    await session._spoken("Каждый раз при по")
    await session.close()
    await asyncio.sleep(0.4)

    assert not llm.seen
