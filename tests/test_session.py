"""`Session` driven directly: when turns start, and what ending one leaves behind.

Through a socket these are races — whether a stray frame lands before the close
depends on scheduling. Here every wait has a deadline and every frame is kept.
"""

import asyncio

from tests.conftest import FakeLLM, FakeSTT
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

    async def send_bytes(self, data: bytes) -> None: ...

    def kinds(self) -> list[object]:
        return [frame["type"] for frame in self.frames]

    async def wait_for(self, kind: str, count: int = 1) -> None:
        async with asyncio.timeout(WAIT_TIMEOUT):
            while self.kinds().count(kind) < count:
                self.arrived.clear()
                await self.arrived.wait()


def session_for(
    llm: FakeLLM, stt: FakeSTT | None = None, latency: dict[str, float] | None = None
) -> tuple[Session, RecordingChannel, Conversation]:
    channel = RecordingChannel(latency)
    conversation = Conversation(id="test")
    session = Session(channel, conversation, llm, None, "system", stt)  # type: ignore[arg-type]
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
    during it would be adopted as an answer to the wrong conversation."""
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
