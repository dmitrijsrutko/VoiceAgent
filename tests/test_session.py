"""`Session` driven directly: when turns start, and what ending one leaves behind.

Through a socket these are races — whether a stray frame lands before the close
depends on scheduling. Here every wait has a deadline and every frame is kept.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from starlette.websockets import WebSocketDisconnect

from tests.conftest import FakeLLM, FakeSTT, FakeTTS
from voice_agent.conversation import Conversation
from voice_agent.session import Session
from voice_agent.stt.base import Transcript

WAIT_TIMEOUT = 2.0
SLOW_REPLY = "one two three four five six seven eight nine ten"


class RecordingChannel:
    def __init__(self, latency: dict[str, float] | None = None) -> None:
        self.frames: list[dict[str, object]] = []
        self.arrived = asyncio.Event()
        self.latency = latency or {}
        """Seconds a write of each frame type takes. A real socket write
        suspends, and one that never does hides every ordering bug that depends
        on another task running during it."""

    async def send_json(self, payload: dict[str, object]) -> None:
        self.frames.append(payload)
        self.arrived.set()
        await asyncio.sleep(self.latency.get(str(payload["type"]), 0.0))

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append({"type": "audio_bytes"})
        self.arrived.set()
        await asyncio.sleep(self.latency.get("audio_bytes", 0.0))

    def kinds(self) -> list[object]:
        return [frame["type"] for frame in self.frames]

    async def wait_for(self, kind: str, count: int = 1) -> None:
        async with asyncio.timeout(WAIT_TIMEOUT):
            while self.kinds().count(kind) < count:
                self.arrived.clear()
                await self.arrived.wait()


def session_for(
    llm: FakeLLM,
    stt: FakeSTT | None = None,
    latency: dict[str, float] | None = None,
    tts: FakeTTS | None = None,
) -> tuple[Session, RecordingChannel, Conversation]:
    channel = RecordingChannel(latency)
    conversation = Conversation(id="test")
    session = Session(channel, conversation, llm, tts, "system", stt)  # type: ignore[arg-type]
    return session, channel, conversation


async def test_a_transcript_after_the_conversation_ended_starts_no_turn() -> None:
    """The recognizer flushes a last commit after listening stops; arriving
    after `bye`, it used to start a billed turn nobody would see."""
    llm = FakeLLM()
    session, channel, conversation = session_for(llm)
    conversation.end()

    await session.submit("hello")
    await asyncio.sleep(0.05)

    assert llm.seen == []
    assert channel.frames == []


async def test_ending_mid_reply_stops_it_and_announces_last() -> None:
    llm = FakeLLM(replies=[SLOW_REPLY], pace=0.01)
    session, channel, conversation = session_for(llm, latency={"ended": 0.2})

    await session.submit("tell me something long")
    await channel.wait_for("delta")
    await session.submit("bye")

    assert channel.kinds()[-1] == "ended", "a reply frame followed the end"
    assert llm.active == 0, "the reply was still generating after the end"
    assert conversation.messages == [], "a question was left without its answer"


async def test_closing_mid_reply_stops_generating() -> None:
    llm = FakeLLM(replies=[SLOW_REPLY], pace=0.05)
    session, channel, conversation = session_for(llm)

    await session.submit("tell me something long")
    await channel.wait_for("delta")
    await session.close()

    assert llm.active == 0, "a reply nobody will receive is still being billed"
    assert "reply_end" not in channel.kinds()
    assert conversation.messages == []


async def test_no_guess_is_made_while_a_turn_is_running() -> None:
    """The running turn's reply is not in the history yet, so a guess made
    during it would be adopted as an answer to the wrong conversation.

    Since Chapter 8 the second commit also interrupts the first reply: with no
    voice, what was on screen is kept, marked as interrupted."""
    llm = FakeLLM(replies=["Riga is the capital of Latvia", "About 600,000."], pace=0.1)
    stt = FakeSTT(
        script=[
            Transcript("What is the capital of Latvia?", is_final=True),
            Transcript("and how many people", is_final=False),
            Transcript("and how many people", is_final=False),  # settles -> would guess
            Transcript("And how many people?", is_final=True),
        ]
    )
    session, channel, _ = session_for(llm, stt)
    assert session.mic is not None

    await session.mic.start()
    for _ in range(4):
        session.mic.feed(b"\x00\x00")
    await channel.wait_for("reply_end", count=2)
    await session.close()

    assert len(llm.seen) == 2
    assert [m.role for m in llm.seen[1]] == ["user", "assistant", "user"]
    assert not any(f.get("speculated") for f in channel.frames if f["type"] == "reply_end")
    assert [f.get("interrupted", False) for f in channel.frames if f["type"] == "reply_end"] == [
        True,
        False,
    ]


async def test_ending_mid_sentence_stops_the_voice_too() -> None:
    """Speech now runs beside the reply in a task of its own. Cancelling the
    turn without it would leave the synthesizer speaking — and billing — a
    reply that the conversation has already dropped."""
    llm, tts = FakeLLM(replies=[SLOW_REPLY], pace=0.01), FakeTTS()
    session, channel, _ = session_for(llm, latency={"ended": 0.2}, tts=tts)

    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes")
    await session.submit("bye")

    assert channel.kinds()[-1] == "ended", "a frame of the reply followed the end"
    assert tts.active == 0, "the voice was still being synthesized after the end"
    assert llm.active == 0


async def test_closing_mid_sentence_stops_the_voice_too() -> None:
    llm, tts = FakeLLM(replies=[SLOW_REPLY], pace=0.05), FakeTTS()
    session, channel, conversation = session_for(llm, tts=tts)

    await session.submit("tell me something long")
    await channel.wait_for("audio_bytes")
    await session.close()

    assert tts.active == 0, "a voice nobody will hear is still being synthesized"
    assert "audio_end" not in channel.kinds()
    assert conversation.messages == []


async def test_ending_while_the_reply_end_is_being_sent_stops_the_voice() -> None:
    """The one await between the text finishing and waiting on the voice. A
    write there queues behind audio frames for the socket, so a cancel landing
    in it is ordinary — and the voice used to outlive the turn, sending audio
    after `ended`."""
    llm, tts = FakeLLM(replies=[SLOW_REPLY], pace=0.005), FakeTTS()
    # Audio still being sent when the text is done: speech is slower than text.
    latency = {"reply_end": 0.3, "audio_bytes": 0.05}
    session, channel, _ = session_for(llm, latency=latency, tts=tts)

    await session.submit("tell me something long")
    await channel.wait_for("reply_end")
    await session.submit("bye")
    await asyncio.sleep(0.2)  # room for an orphaned voice to send something

    assert channel.kinds()[-1] == "ended", "the voice sent audio after the end"
    assert tts.active == 0, "the voice outlived its turn"


class VanishingLLM(FakeLLM):
    """A reply whose writer dies as the turn is cancelled — as a write to a
    socket that has just gone does, raising the socket's error instead."""

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        self.active += 1
        try:
            for word in SLOW_REPLY.split():
                yield word + " "
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise WebSocketDisconnect(code=1006) from None
        finally:
            self.active -= 1


async def test_a_turn_that_fails_while_being_cancelled_does_not_abort_closing() -> None:
    """Seen live: the browser closed mid-reply, and the turn ended with the
    socket's error rather than a cancellation. Closing re-raised it — an ERROR
    traceback per disconnect — and stopped there, never cancelling the turn
    queued behind it or abandoning a guess still being generated."""
    llm = VanishingLLM()
    session, channel, _ = session_for(llm)

    await session.submit("tell me something long")
    await session.submit("and then this")  # queued behind the first
    await channel.wait_for("delta")
    await asyncio.sleep(0.05)  # inside the reply, between words

    await session.close()  # must not raise

    assert llm.active == 0
    assert all(turn.done() for turn in session._turns), "a turn was left running"


class DisconnectingChannel(RecordingChannel):
    """A socket the browser closed: the next audio write fails."""

    async def send_bytes(self, data: bytes) -> None:
        raise WebSocketDisconnect(code=1006)


async def test_a_turn_that_dies_on_a_closed_socket_is_not_an_unretrieved_traceback() -> None:
    """Seen live: the page closed while a reply was still speaking. The turn
    failed on its next audio write before `close()` could cancel it, and asyncio
    printed "Task exception was never retrieved" for it."""
    import gc

    unretrieved: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _, context: unretrieved.append(context))
    try:
        channel = DisconnectingChannel()
        session = Session(channel, Conversation(id="t"), FakeLLM(), FakeTTS(), "system", None)  # type: ignore[arg-type]
        await session.submit("hello")
        await asyncio.wait(set(session._turns), timeout=WAIT_TIMEOUT)
        del session
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)

    assert not unretrieved, unretrieved[0].get("message")
