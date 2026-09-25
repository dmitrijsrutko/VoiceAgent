"""The agent hearing itself, and an interruption that turns out to be nobody:
the two ways a live session went wrong without the user doing anything.

Both shapes are from the record: at 20:23 the agent said "I can help you sign
up" and heard "Hello, I can help."; at 20:37 a reply was cut after "Отлично."
by words nobody followed up, and 16 s of silence came after it.
"""

import asyncio

import pytest

from tests.test_barge_in import answer, speak_over, talking_agent
from tests.test_session import RecordingChannel
from voice_agent import session as session_module
from voice_agent.conversation import Message
from voice_agent.echo import is_echo_final, recent, verdict
from voice_agent.heard import resume_from

pytestmark = pytest.mark.anyio

SAID = "I can help you sign up. What would you like to create an account for?"


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("Hello, I can help.", "echo"),
        ("you sign up", "echo"),
        ("wait, no", "user"),
        ("stop", "user"),
        ("help", "unsure"),  # one word it also said: the next partial decides
        ("", "unsure"),
        ("no, I want to sign up for something else entirely", "user"),
    ],
)
def test_what_was_heard_is_judged_against_what_was_said(heard: str, expected: str) -> None:
    assert verdict(heard, SAID) == expected


LONG = (
    "The short version: plants pull in water, carbon dioxide and sunlight, and turn them into "
    "sugar for themselves — oxygen is basically the leftover they throw away. That waste is "
    "what filled our atmosphere and everything that breathes now depends on it. Which piece do "
    "you want to dig into first — how the light actually gets converted, or the bigger picture "
    "of how plants and the atmosphere keep each other in balance?"
)
"""The 71-word reply that played for 26 seconds on the deployed instance."""


def test_a_question_is_not_mistaken_for_the_agents_own_long_reply() -> None:
    """Live: the user asked "What is the best or biggest—" over that reply and had
    to say "wait" seven times before anything interrupted.

    Against the whole reply their question shares `what`, `is`, `the` and `or`
    with it and reads as the agent's own voice, so the agent talked on. Only a
    word it had never said could break through — and "wait" is not in a reply
    about photosynthesis. The window is what lets their own question interrupt.
    """
    asked = "What is the best or biggest"

    assert verdict(asked, LONG) == "echo", "the trap the window exists for"
    assert verdict(asked, recent(LONG)) == "user", "their question must reach the agent"


def test_the_window_is_only_the_tail() -> None:
    assert recent("one two three", keep=2) == "two three"
    assert recent("one two three", keep=9) == "one two three", "a short reply is judged whole"


def test_a_resume_starts_at_the_sentence_it_was_cut_in() -> None:
    written = "Отлично. Деревня — это контент. Можешь снимать жизнь там."

    assert resume_from(written, "Отлично.") == "Деревня — это контент. Можешь снимать жизнь там."
    assert resume_from(written, "Отлично. Деревня — это") == (
        "Деревня — это контент. Можешь снимать жизнь там."
    )
    assert resume_from(written, "") == written
    assert resume_from(written, written) == ""


@pytest.fixture
def quick_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "RESUME_AFTER_SECONDS", 0.1)


def heard_frames(channel: RecordingChannel, kind: str) -> list[dict[str, object]]:
    return [f for f in channel.frames if f["type"] == kind]


async def test_its_own_words_coming_back_do_not_interrupt_it() -> None:
    session, channel, _, _ = talking_agent()
    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)

    await speak_over(session, "one two three")  # its own reply, back through the speaker
    await channel.wait_for("echo_ignored")
    await asyncio.sleep(0.05)

    assert "interrupt" not in channel.kinds()
    await session.close()


async def test_its_own_words_committed_as_a_turn_are_not_answered() -> None:
    session, channel, llm, _ = talking_agent()
    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=5)  # "one two three four" voiced
    await speak_over(session, "one two three")
    await channel.wait_for("echo_ignored")

    # An echo repeats what was already voiced, never what is still to come.
    await session.submit("one two three")

    finals = [f for f in heard_frames(channel, "echo_ignored") if f["stage"] == "final"]
    assert finals, "its own voice was answered as a turn"
    assert Message("user", "one two three") not in session.conversation.messages
    assert len(llm.seen) == 1
    await session.close()


def test_a_turn_is_dropped_only_when_it_is_plainly_the_agents_own_words() -> None:
    """Quoting the agent back is the user talking: 60 % overlap may delay a
    barge-in, but only near-total overlap loses a turn."""
    said = "Ты говоришь, что стейк самая вкусная еда на планете, но вкус субъективен."
    quote = "Да, я говорю, что стейк самая вкусная еда"

    assert verdict(quote, said) == "echo"  # enough to hold a barge-in back
    assert not is_echo_final(quote, said)  # not enough to drop the turn
    assert is_echo_final("что стейк самая вкусная еда", said)
    assert not is_echo_final("вкус субъективен", said)  # too short to be sure


async def test_a_quote_of_the_agent_is_answered_not_dropped() -> None:
    reply = "one two three four five six seven eight nine ten"
    session, channel, llm, _ = talking_agent(reply=reply)
    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=5)
    await speak_over(session, "one two three")  # heard over it: held back as possible echo
    await channel.wait_for("echo_ignored")

    await session.submit("yes you said one two three four")
    await channel.wait_for("reply_end", count=2)

    assert Message("user", "yes you said one two three four") in session.conversation.messages
    assert not [f for f in heard_frames(channel, "echo_ignored") if f["stage"] == "final"]
    assert len(llm.seen) == 2
    await session.close()


async def test_an_interruption_judged_the_users_is_never_dropped_later() -> None:
    session, channel, _, _ = talking_agent()
    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=5)
    await speak_over(session, "wait, stop")
    await channel.wait_for("interrupt")

    await session.submit("one two three four")  # the words happen to repeat its reply
    await channel.wait_for("reply_end", count=2)

    assert Message("user", "one two three four") in session.conversation.messages
    await session.close()


async def test_a_real_interruption_still_cuts_in_at_once() -> None:
    session, channel, _, _ = talking_agent()
    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)

    await speak_over(session, "wait, stop")
    await channel.wait_for("interrupt")

    assert not heard_frames(channel, "echo_ignored")
    await session.close()


async def test_an_interruption_nobody_follows_up_is_picked_up_again(quick_resume: None) -> None:
    """20:37: cut after one sentence, then nothing. It carries on from the
    sentence it was cut in, rather than leaving the silence to the clock."""
    session, channel, _, _ = talking_agent(reply="First part. Second part is longer here.")
    await session.submit("go on")
    await channel.wait_for("audio_bytes", count=3)

    await speak_over(session, "hm")
    await channel.wait_for("interrupt")
    answer(session, channel, 0.0)
    await channel.wait_for("resumed")
    await channel.wait_for("reply_end", count=2)

    ends = heard_frames(channel, "reply_end")
    assert ends[-1].get("resumed") is True
    assert session.conversation.messages[-1].content.startswith("First part.")
    await session.close()


async def test_an_interruption_followed_by_words_is_never_picked_up(quick_resume: None) -> None:
    session, channel, _, _ = talking_agent()
    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)

    await speak_over(session, "wait")
    await channel.wait_for("interrupt")
    answer(session, channel, 0.0)
    await session.submit("actually, tell me about Riga")
    await asyncio.sleep(0.3)

    assert "resumed" not in channel.kinds()
    await session.close()


async def test_no_resume_while_half_a_sentence_of_theirs_is_waiting(
    quick_resume: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They cut in with an unfinished phrase and paused to think: that pause
    is theirs, not a silence to carry on into."""
    monkeypatch.setattr(session_module, "HOLD_SECONDS", 1.0)
    session, channel, _, _ = talking_agent()
    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)
    await speak_over(session, "hm")
    await channel.wait_for("interrupt")
    answer(session, channel, 0.0)

    await session._spoken("but wait,")
    await asyncio.sleep(0.4)

    assert "resumed" not in channel.kinds()
    await session.close()


async def test_closing_cancels_a_pending_resume(quick_resume: None) -> None:
    session, channel, _, _ = talking_agent()
    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)
    await speak_over(session, "hm")
    await channel.wait_for("interrupt")

    await session.close()
    await asyncio.sleep(0.3)

    assert "resumed" not in channel.kinds()
