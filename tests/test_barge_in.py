"""Barge-in: the user talks over the agent, the agent stops, and the history
keeps what was heard — driven through `Session`, with the browser's half of the
exchange (`playback`, `interrupted`) played by the test."""

import asyncio

import pytest

from tests.conftest import FakeLLM, FakeSTT, FakeTTS, ms_for
from tests.test_session import SLOW_REPLY, RecordingChannel, session_for
from voice_agent import session as session_module
from voice_agent.conversation import Message
from voice_agent.heard import Spoken
from voice_agent.session import Session
from voice_agent.stt.base import Transcript
from voice_agent.tts.base import AudioChunk


async def speak_over(session: Session, words: str = "wait") -> None:
    """One partial transcript with words in it, as the recognizer sends them."""
    assert session.mic is not None
    session.mic._stt.script = [Transcript(words, is_final=False)]  # type: ignore[attr-defined]
    session.mic._stt._remaining = iter(session.mic._stt.script)  # type: ignore[attr-defined]
    if not session.mic.listening:
        await session.mic.start()
    session.mic.feed(b"\x00\x00")


def answer(session: Session, channel: RecordingChannel, played_ms: float | None) -> None:
    """The browser's answer to the latest `interrupt`, with that interrupt's id."""
    interrupt = [f for f in channel.frames if f["type"] == "interrupt"][-1]
    session.heard(int(interrupt["id"]), played_ms)  # type: ignore[call-overload]


def talking_agent(
    reply: str = SLOW_REPLY, pace: float = 0.02, **latency: float
) -> tuple[Session, RecordingChannel, FakeLLM, FakeTTS]:
    llm, tts = FakeLLM(replies=[reply, "Sure."], pace=pace), FakeTTS()
    session, channel, _ = session_for(llm, FakeSTT(script=[]), latency=latency, tts=tts)
    return session, channel, llm, tts


async def test_words_over_the_agent_stop_it_and_keep_only_what_was_heard() -> None:
    session, channel, llm, tts = talking_agent()

    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)  # "one two thre" voiced
    await speak_over(session)
    await channel.wait_for("interrupt")
    answer(session, channel, ms_for("one two thr"))  # the browser got partway into "three"
    await channel.wait_for("truncated")

    assert llm.active == 0, "the reply was still being written after the user interrupted"
    assert tts.active == 0, "the voice was still being synthesized after the user interrupted"
    assert session.conversation.messages == [
        Message("user", "tell me something long"),
        Message("assistant", "one two"),
    ], "the history holds words the user never heard"
    kinds = channel.kinds()
    assert kinds.index("interrupt") < kinds.index("truncated")
    reply_end = next(f for f in channel.frames if f["type"] == "reply_end")
    assert reply_end["interrupted"] is True
    truncated = next(f for f in channel.frames if f["type"] == "truncated")
    assert truncated["heard_chars"] == len("one two")
    assert truncated["timed"] is True and truncated["estimated"] is False


async def test_the_browser_is_told_before_the_reply_is_stopped() -> None:
    """Silence is the part the user notices. It must not wait on the synthesizer
    closing its socket, which can take up to a second."""
    session, channel, _, _ = talking_agent()

    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)
    await speak_over(session)
    await channel.wait_for("reply_end")

    kinds = channel.kinds()
    assert kinds.index("interrupt") < kinds.index("reply_end")


async def test_a_reply_nobody_heard_a_word_of_leaves_no_answer_behind() -> None:
    session, channel, _, _ = talking_agent()

    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)
    await speak_over(session)
    await channel.wait_for("interrupt")
    answer(session, channel, 0.0)
    await channel.wait_for("truncated")

    assert session.conversation.messages == [Message("user", "tell me something long")]


async def test_a_reply_that_had_already_finished_playing_is_kept_whole() -> None:
    session, channel, _, _ = talking_agent(reply="short answer", pace=0)

    await session.submit("hi")
    await channel.wait_for("audio_end")
    await speak_over(session)
    await channel.wait_for("interrupt")
    answer(session, channel, None)  # the browser had nothing playing any more
    await asyncio.sleep(0.05)

    assert session.conversation.messages[-1] == Message("assistant", "short answer ")
    assert "truncated" not in channel.kinds()


async def test_words_after_the_browser_finished_playing_are_not_an_interruption() -> None:
    session, channel, _, _ = talking_agent(reply="short answer", pace=0)

    await session.submit("hi")
    await channel.wait_for("audio_end")
    session.playback(True)
    session.playback(False)
    await speak_over(session)
    await asyncio.sleep(0.05)

    assert "interrupt" not in channel.kinds()


async def test_words_while_the_agent_is_still_thinking_do_not_cancel_it() -> None:
    """A partial might be a cough the recognizer half-heard and never commits.
    Cancelling on it would leave the user with no answer at all."""
    llm, tts = FakeLLM(replies=["Riga."], delay=0.2), FakeTTS()
    session, channel, conversation = session_for(llm, FakeSTT(script=[]), tts=tts)

    await session.submit("capital of Latvia?")
    await speak_over(session, "um")
    await channel.wait_for("reply_end")

    assert "interrupt" not in channel.kinds()
    assert conversation.messages[-1] == Message("assistant", "Riga. ")


async def test_a_new_question_while_the_agent_is_thinking_replaces_the_old_answer() -> None:
    """The user carried on talking before any answer was spoken. Both questions
    stay, as two messages; the answer to the first is never given."""
    llm, tts = FakeLLM(replies=["first answer", "second answer"], delay=0.2), FakeTTS()
    session, channel, conversation = session_for(llm, tts=tts)

    await session.submit("what is the capital")
    await asyncio.sleep(0.05)
    await session.submit("of Latvia")
    await channel.wait_for("reply_end", count=2)
    await asyncio.sleep(0.05)

    assert conversation.messages == [
        Message("user", "what is the capital"),
        Message("user", "of Latvia"),
        Message("assistant", "second answer "),
    ]
    assert [m.role for m in llm.seen[1]] == ["user", "user"]
    assert llm.active == 0 and tts.active == 0


async def test_typing_over_the_agent_interrupts_it_and_the_next_turn_sees_what_was_heard() -> None:
    """The next turn must wait for the cut: sent the whole reply as context,
    the model answers as if the user had heard all of it."""
    session, channel, llm, _ = talking_agent()

    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)
    await session.submit("stop, something else")
    await channel.wait_for("interrupt")
    await asyncio.sleep(0.05)  # the browser takes a round trip to answer
    answer(session, channel, ms_for("one two "))
    await channel.wait_for("reply_end", count=2)

    assert llm.seen[1] == [
        Message("user", "tell me something long"),
        Message("assistant", "one two"),
        Message("user", "stop, something else"),
    ]


async def test_a_browser_that_never_answers_does_not_hold_up_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "ANSWER_TIMEOUT_SECONDS", 0.1)
    session, channel, llm, _ = talking_agent()

    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)
    await session.submit("stop")
    await channel.wait_for("reply_end", count=2)

    truncated = next(f for f in channel.frames if f["type"] == "truncated")
    assert truncated["estimated"] is True
    assert len(llm.seen) == 2


async def test_ending_while_an_interruption_is_being_settled_still_ends_last() -> None:
    session, channel, llm, tts = talking_agent()

    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes", count=3)
    await speak_over(session)
    await channel.wait_for("interrupt")
    await session.submit("bye")
    answer(session, channel, ms_for("one "))
    await asyncio.sleep(0.1)

    after = channel.kinds()[channel.kinds().index("ended") + 1 :]
    assert set(after) <= {"listening"}, f"the interrupted reply went on after the end: {after}"
    assert llm.active == 0 and tts.active == 0


async def test_no_guess_is_made_while_the_agent_is_audible() -> None:
    """A guess carries the history as it was when it started, and an audible
    reply is about to be cut down to what was heard."""
    session, channel, llm, _ = talking_agent(reply="short answer", pace=0)

    await session.submit("hi")
    await channel.wait_for("audio_end")
    await asyncio.sleep(0.05)  # the turn is over; only its voice is still playing
    assert not session._turns, "the test would pass on the running-turn guard instead"
    await session._on_partial("and another thing", repeated=True)
    await asyncio.sleep(0.05)

    assert len(llm.seen) == 1, "a guess was started over the agent's own voice"


async def test_an_interrupted_greeting_is_cut_to_an_estimate() -> None:
    """The greeting is cached as bare audio, with no timing to go on."""
    llm = FakeLLM()
    session, channel, conversation = session_for(llm, FakeSTT(script=[]))
    greeting = conversation.add_assistant("Hello there, how can I help?")
    voice = Spoken(message=greeting)
    voice.add(AudioChunk(b"\x00" * 48_000))  # one second
    session.voiced(voice)

    await speak_over(session)
    await channel.wait_for("interrupt")
    answer(session, channel, 500.0)
    await channel.wait_for("truncated")

    assert conversation.messages == [Message("assistant", "Hello there,")]
    assert next(f for f in channel.frames if f["type"] == "truncated")["timed"] is False


async def test_a_turn_overtaken_before_it_began_still_keeps_its_question() -> None:
    """Typed "b" over a speaking reply waits for that reply's cut; typed "c"
    arrives first. "b" had not begun, so cancelling it lost the question."""
    llm, tts = FakeLLM(replies=[SLOW_REPLY, "answer"], pace=0.02), FakeTTS()
    session, channel, conversation = session_for(llm, tts=tts)

    await session.submit("a")
    await channel.wait_for("audio_bytes", count=3)
    await session.submit("b")  # waits for the browser to say how much of "a" it played
    await asyncio.sleep(0.05)
    await session.submit("c")
    answer(session, channel, ms_for("one two "))
    await channel.wait_for("reply_end", count=2)
    await asyncio.sleep(0.05)

    assert [(m.role, m.content) for m in conversation.messages] == [
        ("user", "a"),
        ("assistant", "one two"),
        ("user", "b"),
        ("user", "c"),
        ("assistant", "answer "),
    ]
    assert [m.content for m in llm.seen[-1]][-2:] == ["b", "c"]


async def test_a_late_answer_does_not_settle_the_next_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser answered the first interruption only after its timeout.
    Unmatched, that answer would cut the second reply by the first one's amount."""
    monkeypatch.setattr(session_module, "ANSWER_TIMEOUT_SECONDS", 0.2)
    llm, tts = FakeLLM(replies=[SLOW_REPLY, SLOW_REPLY, "x"], pace=0.02), FakeTTS()
    session, channel, conversation = session_for(llm, tts=tts)

    await session.submit("a")
    await channel.wait_for("audio_bytes", count=3)
    await session.submit("b")  # the browser stays silent: estimated
    await channel.wait_for("truncated")
    await channel.wait_for("audio_bytes", count=10)
    await session.submit("c")
    await channel.wait_for("interrupt", count=2)
    session.heard(1, 0.0)  # the first interruption's answer, arriving now
    answer(session, channel, ms_for("one two "))
    await channel.wait_for("truncated", count=2)

    assert ("assistant", "one two") in [(m.role, m.content) for m in conversation.messages[2:]]


async def test_an_unprompted_line_talked_over_keeps_only_what_was_heard() -> None:
    """The regression test for reusing `run_turn` rather than giving the clock
    a path of its own: everything barge-in learned has to apply to a line
    nobody asked for, with no new code and no user turn above it to lean on."""
    session, channel, _, tts = talking_agent()
    assert session.mic is not None
    await session.mic.start()  # the clock only ever speaks from an open mic

    await session.speak(SLOW_REPLY, rung=1)
    await channel.wait_for("audio_bytes", count=3)
    await speak_over(session, "sorry")
    await channel.wait_for("interrupt")
    answer(session, channel, ms_for("one two thr"))
    await channel.wait_for("truncated")

    assert tts.active == 0
    assert session.conversation.messages == [Message("assistant", "one two")]
