"""The scenario replay, with a fake inner voice: the scripts parse, the pauses
land where the labels say, and the score counts what it claims to."""

import json
from collections.abc import AsyncIterator, Sequence

import pytest

from voice_agent import replay
from voice_agent.conversation import Message
from voice_agent.llm.base import Usage

pytestmark = pytest.mark.anyio

SCRIPT = """role: devils_advocate

Partner: What do you hold?
User: Cars are bad. | {clean} Everyone agrees. | {expect challenge} Obviously.
"""


class Keyword:
    """Challenges whenever what the user is saying right now contains a word."""

    provider = "fake"
    model = "keyword"

    def __init__(self, word: str) -> None:
        self.word = word
        self.calls = 0

    async def connect(self) -> None:
        return None

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        self.calls += 1
        now = messages[-1].content.split("# The user, right now")[1].split("# Your notes")[0]
        thought = (
            {"move": "challenge", "urgency": 2, "why": "claim", "line": "Says who?"}
            if self.word in now
            else None
        )
        if usage is not None:
            usage.prompt_tokens, usage.output_tokens = 1000, 50
        yield json.dumps({"notes": "", "thought": thought})


def test_pauses_and_labels_land_where_the_script_says() -> None:
    scenario = replay.parse(SCRIPT, "cars")

    partner, user = scenario.steps
    assert [a.reason for a in partner.asks] == ["reply"]
    assert [(a.hearing, a.label) for a in user.asks] == [
        ("Cars are bad.", "clean"),
        ("Cars are bad. Everyone agrees.", "expect challenge"),
        ("Cars are bad. Everyone agrees. Obviously.", ""),
    ]
    assert user.text == "Cars are bad. Everyone agrees. Obviously."


def test_every_shipped_scenario_parses() -> None:
    scenarios = replay.load()

    assert {s.name for s in scenarios} >= {"remote_work", "self_repair", "no_position"}
    for scenario in scenarios:
        assert any(a.label for step in scenario.steps for a in step.asks)


async def test_a_thought_in_the_window_is_a_hit_and_on_a_clean_pause_a_false_fire() -> None:
    scenario = replay.parse(SCRIPT, "cars")

    hit = await replay.replay(scenario, Keyword("agrees"))
    late = await replay.replay(scenario, Keyword("Obviously"))
    early = await replay.replay(scenario, Keyword("bad"))

    assert (hit.hits, hit.right_moves, hit.false_fires) == (1, 1, 0)
    assert (late.hits, late.false_fires) == (1, 0)  # one pause late is still in the window
    assert (early.hits, early.false_fires) == (1, 1)  # fired on the clean pause, and kept on


async def test_the_report_totals_calls_and_cost() -> None:
    engine = Keyword("agrees")
    score = await replay.replay(replay.parse(SCRIPT, "cars"), engine)

    text = replay.report([score])

    assert engine.calls == 4
    assert "TOTAL hits 1/1 · false fires 0/1 · 4 calls" in text
    assert score.cost == pytest.approx(4 * (1000 * 1.0 + 50 * 5.0) / 1e6)


async def test_a_scenario_longer_than_the_live_cap_is_scored_whole() -> None:
    """The live cap would stop a long replay silently and score the previous
    answer again; the replay runs uncapped and checks one answer per ask."""
    pauses = " | ".join(f"claim {i}" for i in range(30))
    scenario = replay.parse(f"role: devils_advocate\n\nUser: {pauses}\n", "long")

    score = await replay.replay(scenario, Keyword("never said"))

    assert len(score.outcomes) == 30
    assert all(o.decision == "nothing" for o in score.outcomes)


def test_a_label_at_the_end_of_a_line_scores_the_final_pause() -> None:
    (_, user) = replay.parse(
        "role: devils_advocate\n\nPartner: Hi\nUser: Hello. | {clean}\n", "x"
    ).steps

    assert [(a.hearing, a.label) for a in user.asks] == [("Hello.", "clean")]
