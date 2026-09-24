"""`uv run voice-agent --replay-thinker` — the inner voice against scripted
conversations with labelled moments, scored. See `tests/scenarios/README.md`.

Every call is real and billed. The thinker is driven pause by pause, in order,
exactly as a live session would ask it, but without waiting on audio.
"""

import asyncio
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from voice_agent import roles
from voice_agent.conversation import Conversation
from voice_agent.llm import LLM, create_llm
from voice_agent.llm.traced import Traced
from voice_agent.thinker import THINKER_MODEL, Thinker

SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "tests" / "scenarios"

WINDOW = 2
"""Pauses a labelled window spans: the one it is on, and the next. Saying it
one breath late is still saying it."""

URGENT = 2
"""The urgency from which a thought counts as stepping in."""

PRICE_PER_MILLION = (1.00, 5.00)
"""Claude Haiku 4.5, input and output, in dollars (2026). Cached reads are
billed lower; this ignores that, so it overstates."""

LABEL = re.compile(r"^\{(expect \w+|clean)\}\s*")


@dataclass(frozen=True, slots=True)
class Ask:
    """One moment the thinker is asked, and how that moment is scored."""

    reason: str
    hearing: str
    label: str = ""
    """`expect <move>`, `clean`, or empty for unscored."""


@dataclass(frozen=True, slots=True)
class Step:
    speaker: str
    text: str
    asks: tuple[Ask, ...]


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    role: str
    steps: tuple[Step, ...]


def parse(text: str, name: str) -> Scenario:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("role:"):
        raise ValueError(f"{name}: the first line must be `role: <card>`")
    role = lines[0].removeprefix("role:").strip()
    steps: list[Step] = []
    for line in lines[1:]:
        speaker, _, rest = line.partition(":")
        if speaker == "Partner":
            steps.append(Step("Partner", rest.strip(), (Ask("reply", ""),)))
        elif speaker == "User":
            steps.append(user_step(rest.strip()))
        else:
            raise ValueError(f"{name}: a line starts with `Partner:` or `User:`: {line!r}")
    return Scenario(name, role, tuple(steps))


def user_step(text: str) -> Step:
    """Each ` | ` is a pause the thinker is asked at; a label opening the chunk
    after it scores that pause. The end of the utterance is a pause too, and a
    line ending in `| {label}` labels that one."""
    chunks = text.split("|")
    said: list[str] = []
    asks: list[Ask] = []
    label = ""
    for index, chunk in enumerate(chunks):
        chunk = chunk.strip()
        if index > 0:
            match = LABEL.match(chunk)
            label = match.group(1) if match else ""
            chunk = chunk[match.end() :] if match else chunk
            asks[-1] = Ask("micro_pause", asks[-1].hearing, label)
        if chunk:
            said.append(chunk)
            asks.append(Ask("micro_pause", " ".join(said)))
    return Step("User", " ".join(said), tuple(asks))


def load(names: Sequence[str] = (), directory: Path = SCENARIOS_DIR) -> list[Scenario]:
    paths = (
        [directory / f"{n}.md" for n in names]
        if names
        else sorted(p for p in directory.glob("*.md") if p.stem != "README")
    )
    return [parse(p.read_text(encoding="utf-8"), p.stem) for p in paths]


@dataclass(slots=True)
class Outcome:
    """What the thinker said at one ask."""

    ask: Ask
    decision: str
    move: str = ""
    urgency: int = 0
    line: str = ""
    consider_ms: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class Score:
    scenario: str
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def windows(self) -> list[tuple[int, str]]:
        return [
            (i, o.ask.label.removeprefix("expect "))
            for i, o in enumerate(self.outcomes)
            if o.ask.label.startswith("expect")
        ]

    def _urgent(self, index: int) -> Outcome | None:
        outcome = self.outcomes[index]
        return outcome if outcome.urgency >= URGENT else None

    @property
    def hits(self) -> int:
        return sum(1 for i, _ in self.windows if self._window_hit(i) is not None)

    @property
    def right_moves(self) -> int:
        return sum(
            1
            for i, move in self.windows
            if (hit := self._window_hit(i)) is not None and hit.move == move
        )

    def _window_hit(self, start: int) -> Outcome | None:
        for index in range(start, min(start + WINDOW, len(self.outcomes))):
            if self.outcomes[index].ask.reason == "micro_pause" and self._urgent(index):
                return self.outcomes[index]
        return None

    @property
    def cleans(self) -> int:
        return sum(1 for o in self.outcomes if o.ask.label == "clean")

    @property
    def false_fires(self) -> int:
        return sum(1 for o in self.outcomes if o.ask.label == "clean" and o.urgency >= URGENT)

    @property
    def cost(self) -> float:
        tokens_in = sum(o.prompt_tokens for o in self.outcomes)
        tokens_out = sum(o.output_tokens for o in self.outcomes)
        return (tokens_in * PRICE_PER_MILLION[0] + tokens_out * PRICE_PER_MILLION[1]) / 1e6


async def replay(scenario: Scenario, engine: LLM, role_dir: Path | None = None) -> Score:
    role = roles.load(scenario.role, role_dir)
    conversation = Conversation(id=f"replay-{scenario.name}")
    frames: list[dict[str, object]] = []
    hearing = ""

    async def report(payload: dict[str, object]) -> None:
        frames.append(payload)

    # No cap: the replay's budget is stated before it runs, and a capped call
    # reports nothing, which would score the previous answer twice.
    thinker = Thinker(
        engine, role, conversation, hearing=lambda: hearing, report=report, per_minute=10**6
    )
    score = Score(scenario.name)
    for step in scenario.steps:
        if step.speaker == "Partner":
            conversation.add_assistant(step.text)
        for ask in step.asks:
            hearing = ask.hearing
            before = len(frames)
            await thinker.consider(ask.reason)
            if len(frames) != before + 1:
                got = len(frames) - before
                raise RuntimeError(f"{scenario.name}: an ask reported {got} frames, not one")
            frame = frames[-1]
            score.outcomes.append(
                Outcome(
                    ask,
                    str(frame["decision"]),
                    move=str(frame.get("move", "")),
                    urgency=int(frame.get("urgency", 0)),  # type: ignore[call-overload]
                    line=str(frame.get("line", "")),
                    consider_ms=int(frame["consider_ms"]),  # type: ignore[call-overload]
                    prompt_tokens=int(frame["prompt_tokens"]),  # type: ignore[call-overload]
                    output_tokens=int(frame["output_tokens"]),  # type: ignore[call-overload]
                )
            )
        if step.speaker == "User":
            conversation.add_user(step.text)
            hearing = ""
    return score


def report(scores: Sequence[Score]) -> str:
    rows = []
    for score in scores:
        latencies = [o.consider_ms for o in score.outcomes if o.consider_ms]
        median = f"{statistics.median(latencies):.0f} ms" if latencies else "-"
        rows.append(
            f"{score.scenario:<14} hits {score.hits}/{len(score.windows)} "
            f"(right move {score.right_moves}) · false fires {score.false_fires}/{score.cleans} "
            f"· {len(score.outcomes)} calls · median {median} · ${score.cost:.4f}"
        )
        for o in score.outcomes:
            mark = o.ask.label or "·"
            said = f"{o.move} u{o.urgency}: {o.line}" if o.decision == "thought" else o.decision
            rows.append(f"    [{mark:<16}] {o.ask.reason:<11} {o.ask.hearing[-48:]!r:<52} → {said}")
    total_hits = sum(s.hits for s in scores)
    total_windows = sum(len(s.windows) for s in scores)
    total_false = sum(s.false_fires for s in scores)
    total_clean = sum(s.cleans for s in scores)
    calls = sum(len(s.outcomes) for s in scores)
    cost = sum(s.cost for s in scores)
    rows.append(
        f"\nTOTAL hits {total_hits}/{total_windows} · false fires {total_false}/{total_clean} "
        f"· {calls} calls · ${cost:.4f}"
    )
    return "\n".join(rows)


def main(names: Sequence[str]) -> None:
    scenarios = load(names)
    if not scenarios:
        # The image leaves `tests/` out: this is a tool for a checkout.
        raise SystemExit(f"no scenarios in {SCENARIOS_DIR}; run this from a source checkout")
    engine = Traced(create_llm("anthropic", THINKER_MODEL))
    calls = sum(len(step.asks) for s in scenarios for step in s.steps)
    print(f"replaying {len(scenarios)} scenario(s): {calls} billed calls to {THINKER_MODEL}\n")

    async def run() -> list[Score]:
        return [await replay(s, engine) for s in scenarios]

    print(report(asyncio.run(run())))
