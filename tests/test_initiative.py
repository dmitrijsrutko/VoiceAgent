"""The clock: when the agent speaks first, and — mostly — when it does not.

The ladder is driven by `tick()` rather than by the ticker task, so nothing
here sleeps for the seconds it is measuring. The one thing that is deliberately
*not* faked is the yield rule: `quiet` is a real callable returning `None`, and
whether a nudge is abandoned when it starts returning `None` mid-decision is
exactly what these tests are for.
"""

import asyncio

import pytest

from tests.conftest import FakeLLM
from voice_agent.conversation import Conversation
from voice_agent.decline import DECLINE
from voice_agent.initiative import Initiative, Rung, nudge_prompt, spoken_line

WAIT = 2.0
"""Ceiling on any wait in this file, so a hang fails as a test rather than
sitting there."""

LADDER = (
    Rung(5.0, "invite", "usually stay quiet"),
    Rung(10.0, "offer", "usually say something"),
    Rung(20.0, "withdraw", "almost always say something"),
)


class Clock:
    """A settable silence, and a record of what was said into it."""

    def __init__(self, quiet: float | None = 0.0) -> None:
        self.quiet = quiet
        self.spoken: list[tuple[str, int]] = []
        self.reports: list[dict[str, object]] = []
        self.arrived = asyncio.Event()

    def __call__(self) -> float | None:
        return self.quiet

    async def speak(self, line: str, rung: int) -> None:
        self.spoken.append((line, rung))

    async def report(self, payload: dict[str, object]) -> None:
        self.reports.append(payload)
        self.arrived.set()

    async def wait_for(self, count: int) -> None:
        async with asyncio.timeout(WAIT):
            while len(self.reports) < count:
                self.arrived.clear()
                await self.arrived.wait()

    def decisions(self) -> list[object]:
        return [r["decision"] for r in self.reports]


def clock_for(
    llm: FakeLLM, quiet: float | None = 0.0, conversation: Conversation | None = None
) -> tuple[Initiative, Clock]:
    clock = Clock(quiet)
    initiative = Initiative(
        llm,
        "system",
        conversation if conversation is not None else Conversation(id="test"),
        quiet=clock,
        speak=clock.speak,
        report=clock.report,
        ladder=LADDER,
    )
    return initiative, clock


async def test_silence_shorter_than_the_first_rung_says_nothing() -> None:
    llm = FakeLLM(["Still there?"])
    initiative, clock = clock_for(llm, quiet=4.9)

    await initiative.tick()

    assert clock.spoken == []
    assert clock.reports == []
    assert llm.seen == []  # not even considered: no call, no cost


async def test_the_ladder_climbs_once_per_rung_and_then_stops() -> None:
    """Three rungs is the budget. A fourth line is never produced however long
    the silence runs — an agent without a ceiling is not a partner."""
    llm = FakeLLM(["one", "two", "three", "four"])
    initiative, clock = clock_for(llm)

    for quiet in (5.0, 6.0, 10.0, 12.0, 20.0, 60.0, 600.0):
        clock.quiet = quiet
        await initiative.tick()

    assert [line for line, _ in clock.spoken] == ["one", "two", "three"]
    assert [rung for _, rung in clock.spoken] == [1, 2, 3]


async def test_a_declined_rung_is_spent_not_retried() -> None:
    """A rung is an opportunity, not a debt. Left unspent, the agent would
    re-decide the same rung once a second for the rest of the silence."""
    llm = FakeLLM([DECLINE])
    initiative, clock = clock_for(llm)

    for _ in range(6):
        clock.quiet = 9.0  # past rung one, short of rung two, for six ticks
        await initiative.tick()

    assert clock.spoken == []
    assert clock.decisions() == ["declined"]
    assert len(llm.seen) == 1  # one call for the rung, not one per tick


async def test_a_decline_records_nothing_and_costs_no_speech() -> None:
    llm = FakeLLM([DECLINE])
    initiative, clock = clock_for(llm)
    clock.quiet = 5.0

    await initiative.tick()

    assert clock.spoken == []
    assert clock.reports[0]["decision"] == "declined"
    assert clock.reports[0]["line"] == ""
    assert clock.reports[0]["rung"] == 1


@pytest.mark.parametrize("reply", [DECLINE, f"{DECLINE}.", f'"{DECLINE}"', "  nothing  ", ""])
async def test_the_sentinel_is_read_forgivingly(reply: str) -> None:
    """A decline misread as a line is the one failure that gets spoken aloud,
    so the parsing errs towards silence."""
    assert spoken_line(reply) is None


@pytest.mark.parametrize("reply", ["Nothing much happens in winter.", "I have nothing to add."])
async def test_a_real_sentence_containing_the_word_is_still_a_line(reply: str) -> None:
    assert spoken_line(reply) == reply


async def test_the_user_speaking_mid_decision_cancels_the_line() -> None:
    """Deciding takes a few hundred milliseconds. If the user starts talking in
    them, there is no line worth saying over them."""
    llm = FakeLLM(["Shall we start?"], delay=0.02)
    initiative, clock = clock_for(llm)
    clock.quiet = 5.0

    async def interrupt() -> None:
        await asyncio.sleep(0.01)
        clock.quiet = None

    await asyncio.gather(initiative.tick(), interrupt())

    assert clock.spoken == []
    assert clock.decisions() == ["yielded"]  # not a decline: it *had* something


async def test_a_moment_that_does_not_count_is_never_considered() -> None:
    llm = FakeLLM(["hello?"])
    initiative, _ = clock_for(llm, quiet=None)

    await initiative.tick()

    assert llm.seen == []


async def test_a_user_turn_hands_the_budget_back() -> None:
    llm = FakeLLM(["one", "two", "three", "again"])
    initiative, clock = clock_for(llm)
    for quiet in (5.0, 10.0, 20.0):
        clock.quiet = quiet
        await initiative.tick()

    initiative.reset()
    clock.quiet = 5.0
    await initiative.tick()

    assert [line for line, _ in clock.spoken] == ["one", "two", "three", "again"]


async def test_the_nudge_is_asked_after_the_history_and_never_recorded() -> None:
    """The prompt's cache contract: history first, the volatile part last. And
    the note describing the pause is not a message the conversation keeps."""
    llm = FakeLLM(["Go on."])
    conversation = Conversation(id="test")
    conversation.add_user("hello")
    conversation.add_assistant("Hi there.")
    initiative, clock = clock_for(llm, conversation=conversation)
    clock.quiet = 5.0

    await initiative.tick()

    asked = llm.seen[0]
    assert [m.content for m in asked[:2]] == ["hello", "Hi there."]
    assert asked[-1].role == "user" and "said nothing" in asked[-1].content
    assert [m.content for m in conversation.messages] == ["hello", "Hi there."]


async def test_a_provider_failure_is_reported_and_does_not_kill_the_clock() -> None:
    """The clock is a luxury and must never end a session — but it must not fail
    in silence either, which looks identical to deciding to say nothing.

    The version of this test that shipped asserted nothing at all: it started
    the ticker, yielded once with `sleep(0)`, and stopped it, while the ticker's
    first act is to sleep for a whole tick. It never ran a single cycle.
    """
    llm = FakeLLM(fail=True)
    clock = Clock(quiet=30.0)  # past every rung, so each tick tries and fails
    initiative = Initiative(
        llm,
        "system",
        Conversation(id="test"),
        quiet=clock,
        speak=clock.speak,
        report=clock.report,
        ladder=LADDER,
        tick=0.01,
    )

    initiative.start()
    await clock.wait_for(len(LADDER))
    await initiative.stop()

    assert clock.spoken == []
    assert clock.decisions() == ["failed"] * len(LADDER), "a failure must be visible, not logged"
    assert all("provider exploded" in str(r["message"]) for r in clock.reports)


async def test_a_failed_rung_is_still_spent() -> None:
    """An outage must not turn the clock into a once-a-second retry loop. The
    budget is a ceiling, and it counts attempts rather than successes."""
    llm = FakeLLM(fail=True)
    initiative, clock = clock_for(llm, quiet=30.0)

    for _ in range(10):
        await initiative.tick()

    assert len(clock.reports) == len(LADDER)


async def test_an_empty_ladder_never_starts_a_clock() -> None:
    """`--initiative off` is the reactive agent, not a clock that says no."""
    llm = FakeLLM()
    clock = Clock(quiet=600.0)
    initiative = Initiative(
        llm,
        "system",
        Conversation(id="test"),
        quiet=clock,
        speak=clock.speak,
        report=clock.report,
        ladder=(),
    )
    initiative.start()
    await initiative.tick()
    await initiative.stop()

    assert llm.seen == [] and clock.reports == []


def test_the_pause_is_described_to_the_model_as_not_the_user() -> None:
    prompt = nudge_prompt(Rung(12.0, "invite them in", "usually stay quiet"), 12.4)
    assert prompt.startswith("[This is not the other person speaking")
    assert "12 seconds" in prompt
    assert "invite them in" in prompt
    assert DECLINE in prompt


def test_each_rung_carries_its_own_willingness_to_speak() -> None:
    """Measured against a real model, one disposition for every rung made the
    agent decline eleven times in twelve — including at forty-five seconds of
    dead silence, where saying nothing is neglect rather than tact."""
    from voice_agent.initiative import LADDER as SHIPPED

    dispositions = [rung.disposition for rung in SHIPPED]
    assert len(set(dispositions)) == len(dispositions)
    assert nudge_prompt(SHIPPED[0], 15.0) != nudge_prompt(SHIPPED[-1], 28.0)


def test_the_ladder_finishes_before_the_ears_close() -> None:
    """Measured live: at 7/20/45 the microphone's idle watchdog stopped
    listening at 32 s and the withdrawal at 45 s never arrived — the ears
    closed without the agent ever saying goodbye. The last rung has to land
    inside that window, with room for the line itself."""
    from voice_agent.config import DEFAULT_INITIATIVE_DELAYS, parse_delays
    from voice_agent.initiative import LADDER as SHIPPED
    from voice_agent.mic import IDLE_TIMEOUT_SECONDS

    # Asserted against the shipped default, not `load_settings()`: reading the
    # ambient environment made this fail for anyone with the clock turned off.
    assert SHIPPED[-1].after < IDLE_TIMEOUT_SECONDS - 1.0
    assert parse_delays(DEFAULT_INITIATIVE_DELAYS)[-1] < IDLE_TIMEOUT_SECONDS - 1.0


async def test_speech_during_the_decision_stops_the_line_being_spoken() -> None:
    """The one rule this module claims: it speaks into silence, never over
    someone. Deciding takes several hundred milliseconds, and a user who starts
    talking inside that window does not make the silence *unavailable* — they
    make it **shorter**. Watching only for `None` missed exactly this, and the
    agent spoke over them.
    """
    llm = FakeLLM(["Shall we pick this back up?"], delay=0.05)
    initiative, clock = clock_for(llm)
    clock.quiet = 6.0

    async def starts_talking() -> None:
        await asyncio.sleep(0.01)
        # What the recognizer's first partial does: `_heard_speech_at` is bumped,
        # so the silence restarts. It is a small number, not `None`.
        clock.quiet = 0.2

    await asyncio.gather(initiative.tick(), starts_talking())

    assert clock.spoken == [], "the agent spoke over someone who had started talking"
    assert clock.decisions() == ["yielded"]


@pytest.mark.parametrize("reply", [f"{DECLINE}?", f"{DECLINE},", f"{DECLINE}...", f"  {DECLINE}  "])
async def test_every_shape_of_the_sentinel_is_read_as_silence(reply: str) -> None:
    """`rstrip(".!")` let `NOTHING?` through, and the agent said it out loud."""
    assert spoken_line(reply) is None


async def test_an_answer_that_runs_on_is_not_spoken() -> None:
    """The worst failure this feature has: a paragraph nobody asked for, since
    the user did not even open the exchange. Reported rather than dropped, so a
    brevity instruction that stops landing is visible."""
    from voice_agent.initiative import MAX_LINE_CHARS

    llm = FakeLLM(["word " * (MAX_LINE_CHARS // 2)])
    initiative, clock = clock_for(llm)
    clock.quiet = 5.0

    await initiative.tick()

    assert clock.spoken == []
    assert clock.decisions() == ["overran"]


async def test_a_line_just_inside_the_cap_is_spoken() -> None:
    from voice_agent.initiative import MAX_LINE_CHARS

    line = "x" * (MAX_LINE_CHARS - 1)
    llm = FakeLLM([line])
    initiative, clock = clock_for(llm)
    clock.quiet = 5.0

    await initiative.tick()

    assert clock.spoken == [(line, 1)]


async def test_every_consideration_reports_what_it_cost() -> None:
    """A call on a timer is a spend decision, and this project has lost a
    month's quota to one before."""
    llm = FakeLLM(["Go on."])
    initiative, clock = clock_for(llm)
    clock.quiet = 5.0

    await initiative.tick()

    report = clock.reports[0]
    assert isinstance(report["prompt_tokens"], int) and report["prompt_tokens"] > 0
    assert isinstance(report["output_tokens"], int) and report["output_tokens"] > 0


def test_the_first_rung_does_not_call_five_seconds_a_long_silence() -> None:
    """`nudge_prompt` injects the real number, so the disposition has to agree
    with it.

    The rung that used to be first sat at fifteen seconds and opened "This is a
    long silence now". Moved to five without rewriting, the model would be told
    "they have said nothing for about 5 seconds" and, in the next breath, that
    this is a long silence — a flat contradiction that can only push it towards
    speaking when it should not.
    """
    from voice_agent.initiative import LADDER as SHIPPED

    prompt = nudge_prompt(SHIPPED[0], SHIPPED[0].after)

    assert "about 5 seconds" in prompt
    assert "long silence" not in SHIPPED[0].disposition


def test_the_first_rung_follows_through_rather_than_inviting() -> None:
    """The seven-second rung was deleted for having no legal move: its job was
    to invite the user in, and the greeting has already done that, so every
    option was forbidden and the paid call had a foregone conclusion.

    This one is about the exchange that just happened instead, which is a move
    that exists whenever anything has been said. Pinned because re-describing it
    as an invitation would quietly recreate a rung that can never fire.
    """
    from voice_agent.initiative import LADDER as SHIPPED

    first = SHIPPED[0].intent.casefold()

    assert "follow through" in first
    assert "still there" in first  # named, as a thing not to do
    assert "invitation you have already made" in first


def test_the_shipped_ladder_starts_where_the_default_says_it_does() -> None:
    """The delays and the rungs are separate places and have disagreed before."""
    from voice_agent.config import DEFAULT_INITIATIVE_DELAYS, parse_delays
    from voice_agent.initiative import LADDER as SHIPPED

    delays = parse_delays(DEFAULT_INITIATIVE_DELAYS)

    assert len(delays) == len(SHIPPED) == 3
    assert delays[0] == 5.0
    assert [rung.after for rung in SHIPPED] == list(delays)
