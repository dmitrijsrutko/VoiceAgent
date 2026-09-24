"""The inner voice against a fake engine: what it is asked, what it makes of
the answer, and how often it may ask."""

import asyncio
import json

import pytest

from tests.conftest import FakeLLM
from voice_agent import roles
from voice_agent.conversation import Conversation
from voice_agent.thinker import Malformed, Thinker, Thought, parse_reply, request

pytestmark = pytest.mark.anyio

ROLE = roles.load("devils_advocate")
WAIT = 2.0


def reply(thought: dict[str, object] | None = None, notes: str = "position: X") -> str:
    return json.dumps({"notes": notes, "thought": thought})


CHALLENGE = {"move": "challenge", "urgency": 2, "why": "no evidence", "line": "Says who?"}


class Reports:
    def __init__(self) -> None:
        self.frames: list[dict[str, object]] = []
        self.arrived = asyncio.Event()

    async def __call__(self, payload: dict[str, object]) -> None:
        self.frames.append(payload)
        self.arrived.set()

    async def wait(self, count: int) -> None:
        async with asyncio.timeout(WAIT):
            while len(self.frames) < count:
                self.arrived.clear()
                await self.arrived.wait()


def thinker_with(
    llm: FakeLLM, reports: Reports, hearing: str | None = None, **kwargs: object
) -> Thinker:
    """The user keeps talking — each ask hears one more word — unless a fixed
    `hearing` says they have stopped."""
    conversation = Conversation(id="t")
    conversation.add_assistant("Give me a position you hold.")
    conversation.add_user("Remote work is always more productive.")
    words = iter(range(10**6))
    return Thinker(
        llm,
        ROLE,
        conversation,
        hearing=(lambda: hearing)
        if hearing is not None
        else (lambda: f"and everyone knows it {next(words)}"),
        report=reports,
        **kwargs,  # type: ignore[arg-type]
    )


# --- parsing ---------------------------------------------------------------


def test_a_thought_is_read_from_the_reply() -> None:
    notes, thought = parse_reply(reply(CHALLENGE), ROLE)

    assert notes == "position: X"
    assert thought == Thought("challenge", 2, "no evidence", "Says who?")


def test_null_urgency_zero_and_an_empty_line_all_mean_nothing() -> None:
    assert parse_reply(reply(None), ROLE)[1] is None
    assert parse_reply(reply({**CHALLENGE, "urgency": 0}), ROLE)[1] is None
    assert parse_reply(reply({**CHALLENGE, "line": " "}), ROLE)[1] is None
    assert parse_reply(reply({**CHALLENGE, "line": None}), ROLE)[1] is None
    held = {key: value for key, value in CHALLENGE.items() if key != "line"}
    assert parse_reply(reply(held), ROLE)[1] is None


def test_a_missing_why_does_not_lose_the_thought() -> None:
    _, thought = parse_reply(reply({**CHALLENGE, "why": None}), ROLE)

    assert thought is not None and thought.why == ""


def test_a_fenced_reply_is_still_read() -> None:
    fenced = f"```json\n{reply(CHALLENGE)}\n```"

    assert parse_reply(fenced, ROLE)[1] is not None


@pytest.mark.parametrize(
    "bad",
    [
        "I think they are wrong.",
        reply({**CHALLENGE, "move": "insult"}),
        reply({**CHALLENGE, "urgency": 7}),
        reply({**CHALLENGE, "urgency": True}),
        json.dumps({"notes": 3, "thought": None}),
    ],
)
def test_anything_else_is_malformed(bad: str) -> None:
    with pytest.raises(Malformed):
        parse_reply(bad, ROLE)


def test_notes_cannot_grow_without_bound() -> None:
    notes, _ = parse_reply(reply(None, notes="word " * 500), ROLE)

    assert len(notes.split()) == 60


# --- what it is asked --------------------------------------------------------


def test_the_request_carries_everything_it_thinks_from() -> None:
    conversation = Conversation(id="t")
    conversation.add_assistant("Give me a position.")
    conversation.add_user("Cities should ban cars.")
    held = Thought("clarify", 1, "vague", "Which cities?")

    ask = request(conversation, "starting with", "pos: ban cars", held, "micro_pause")

    assert "Partner: Give me a position." in ask
    assert "User: Cities should ban cars." in ask
    assert "starting with" in ask
    assert "pos: ban cars" in ask
    assert "Which cities?" in ask
    assert "just paused" in ask


# --- considering -------------------------------------------------------------


async def test_a_thought_is_reported_and_held() -> None:
    reports = Reports()
    thinker = thinker_with(FakeLLM([reply(CHALLENGE)]), reports)

    await thinker.consider("micro_pause")

    (frame,) = reports.frames
    assert frame["type"] == "thought"
    assert frame["decision"] == "thought"
    assert (frame["move"], frame["urgency"], frame["line"]) == ("challenge", 2, "Says who?")
    assert frame["reason"] == "micro_pause"
    assert frame["prompt_tokens"]
    assert thinker.pending is not None and thinker.notes == "position: X"


async def test_nothing_drops_the_thought_it_was_holding() -> None:
    reports = Reports()
    thinker = thinker_with(FakeLLM([reply(CHALLENGE), reply(None)]), reports)

    await thinker.consider("micro_pause")
    await thinker.consider("micro_pause")

    assert [f["decision"] for f in reports.frames] == ["thought", "nothing"]
    assert thinker.pending is None


async def test_it_asks_with_the_role_and_as_an_observer() -> None:
    llm = FakeLLM([reply(None)])
    thinker = thinker_with(llm, Reports())

    await thinker.consider("pause")

    assert "Devil's advocate" in llm.systems[0]
    (messages,) = llm.seen
    assert len(messages) == 1 and messages[0].role == "user"
    assert "and everyone knows it" in messages[0].content


async def test_a_malformed_reply_is_reported_and_changes_nothing() -> None:
    reports = Reports()
    thinker = thinker_with(FakeLLM([reply(CHALLENGE), "not json at all"]), reports)
    await thinker.consider("micro_pause")

    await thinker.consider("micro_pause")

    assert reports.frames[-1]["decision"] == "malformed"
    assert thinker.pending is not None  # the last good thought stands


async def test_a_failing_engine_is_reported_not_silent() -> None:
    reports = Reports()

    await thinker_with(FakeLLM(fail=True), reports).consider("micro_pause")

    assert reports.frames[-1]["decision"] == "failed"
    assert "exploded" in str(reports.frames[-1]["message"])


async def test_the_cap_is_a_hard_stop_reported_once() -> None:
    reports = Reports()
    llm = FakeLLM([reply(None)])
    thinker = thinker_with(llm, reports, per_minute=2)

    for _ in range(4):
        await thinker.consider("micro_pause")

    assert len(llm.seen) == 2
    assert [f["decision"] for f in reports.frames] == ["nothing", "nothing", "capped"]


async def test_a_burst_of_triggers_costs_two_calls_not_five() -> None:
    """Single-flight: while one call is out, only the latest reason is kept."""
    reports = Reports()
    llm = FakeLLM([reply(None)], delay=0.05)
    thinker = thinker_with(llm, reports)

    for reason in ("micro_pause", "micro_pause", "monologue", "micro_pause", "reply"):
        thinker.trigger(reason)
    await reports.wait(2)
    await asyncio.sleep(0.1)

    assert len(llm.seen) == 2
    assert [f["reason"] for f in reports.frames] == ["micro_pause", "reply"]
    await thinker.stop()


async def test_it_thinks_on_a_timer_through_a_monologue_and_at_the_pause() -> None:
    reports = Reports()
    thinker = thinker_with(FakeLLM([reply(None)]), reports, monologue_seconds=0.03)

    thinker.floor("speaking")
    await reports.wait(1)
    thinker.floor("micro_pause")
    await reports.wait(2)
    await asyncio.sleep(0.1)  # the timer is stopped: nothing more arrives

    reasons = [f["reason"] for f in reports.frames]
    assert reasons[0] == "monologue"
    assert reasons[-1] == "micro_pause"
    assert len(reasons) <= 3
    await thinker.stop()


async def test_stop_ends_the_timer_and_the_call() -> None:
    llm = FakeLLM([reply(None)], delay=1.0)
    thinker = thinker_with(llm, Reports(), monologue_seconds=0.01)
    thinker.floor("speaking")
    thinker.trigger("micro_pause")
    await asyncio.sleep(0.02)  # the call is out, and the timer has fired into it
    assert len(llm.seen) == 1

    await thinker.stop()
    await asyncio.sleep(0.05)

    assert llm.active == 0
    assert len(llm.seen) == 1  # no timer left to ask again


async def test_once_stopped_nothing_starts_it_again() -> None:
    """Floor events and finished replies can arrive after the session let go;
    none may start a call or a timer, which would bill for nobody."""
    llm = FakeLLM([reply(None)])
    thinker = thinker_with(llm, Reports(), monologue_seconds=0.01)
    await thinker.stop()

    thinker.floor("speaking")
    thinker.trigger("reply")
    thinker.floor("micro_pause")
    await asyncio.sleep(0.05)

    assert not llm.seen


async def test_the_microphone_stopping_ends_the_monologue_timer() -> None:
    """Pressing stop mid-sentence leaves the floor at `speaking`: without a
    word from the microphone, the timer would go on asking every few seconds."""
    llm = FakeLLM([reply(None)])
    thinker = thinker_with(llm, Reports(), monologue_seconds=0.03)

    thinker.floor("speaking")
    thinker.floor("stopped")
    await asyncio.sleep(0.1)

    assert not llm.seen
    await thinker.stop()


async def test_nothing_new_heard_is_not_asked_again() -> None:
    """Live, a run of pauses inside one sentence bought the same thought six
    times. The held thought stands, and the report says so, unbilled."""
    reports = Reports()
    llm = FakeLLM([reply(CHALLENGE)])
    thinker = thinker_with(llm, reports, hearing="and everyone knows it", per_minute=1)

    await thinker.consider("micro_pause")
    await thinker.consider("micro_pause")
    await thinker.consider("monologue")

    assert len(llm.seen) == 1
    decisions = [(f["decision"], f.get("urgency"), f["prompt_tokens"]) for f in reports.frames]
    assert decisions == [("thought", 2, decisions[0][2]), ("unchanged", 2, 0), ("unchanged", 2, 0)]


async def test_a_new_word_is_asked_about() -> None:
    llm = FakeLLM([reply(None)])
    thinker = thinker_with(llm, Reports())

    await thinker.consider("micro_pause")
    await thinker.consider("micro_pause")

    assert len(llm.seen) == 2


def test_the_thinker_reads_the_recent_conversation_and_its_notes_for_the_rest() -> None:
    conversation = Conversation(id="t")
    for i in range(20):
        conversation.add_user(f"line {i}")

    ask = request(conversation, "", "the position so far", None, "reply")

    assert "line 19" in ask and "line 8" in ask
    assert "line 7" not in ask
    assert "(8 earlier lines: see your notes)" in ask
