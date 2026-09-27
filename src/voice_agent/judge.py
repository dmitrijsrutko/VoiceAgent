"""The judge: once a judged round ends, one model reads it and rules.

One call per round, after the conversation, so it costs nothing in latency and
may think longer than a reply can. The verdict is
JSON the page draws (`web/verdict.js`); what the judge is asked for is data
(`prompts/judge.md`), and what code can measure — pauses, talk share — is
measured here and handed to it rather than left to its impression.
"""

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from voice_agent import prompts, timing
from voice_agent.conversation import Message
from voice_agent.errors import ConfigError
from voice_agent.llm import LLM, create_llm
from voice_agent.llm.base import Usage
from voice_agent.llm.registry import Choice, available
from voice_agent.llm.traced import Traced
from voice_agent.timeline import Turn, stats, with_think_times

logger = logging.getLogger(__name__)

JUDGES: tuple[Choice, ...] = (
    Choice("deepseek-high", "deepseek", "deepseek-flash", "DeepSeek V4.1 Flash", "high"),
    Choice("opus-5-5", "anthropic", "claude-opus-5-5", "Claude Opus 5.5", "medium"),
)
"""The models a round may be judged by: the conversation's own. DeepSeek first and
the default, at `high`: at `max` it kept the user waiting too long. Opus 5.5 at
`medium`, its own default: at `max` it spent all 32 000 tokens thinking about a
10-turn round and wrote no verdict. Both by the user's decision. Named, not left to
`create_llm`, which would give the conversation's `low`."""

BY_NAME = {choice.name: choice for choice in JUDGES}

MAX_TOKENS = 32_000
"""Room for a max-effort chain of thought and a long JSON verdict after it. The
conversation's 8 192 is sized for a spoken reply and would cut the verdict off."""

MIN_TURNS = 2
MIN_WORDS = 30
"""Below either, there is nothing to rule on and no call is made."""

CRITERIA = (
    "clarity",
    "evidence",
    "logic",
    "rebuttal",
    "listening",
    "consistency",
    "economy",
    "composure",
    "originality",
    "persuasion",
)


def offered() -> tuple[Choice, ...]:
    """The judges this deployment holds a key for; the default when none does,
    so a misconfiguration fails naming the missing key."""
    keys = set(available())
    return tuple(c for c in JUDGES if c.provider in keys) or (JUDGES[0],)


def build(name: str) -> LLM:
    choice = BY_NAME[name]
    return Traced(create_llm(choice.provider, choice.model, choice.effort, max_tokens=MAX_TOKENS))


def clock(seconds: float) -> str:
    whole = max(0, round(seconds))
    return f"{whole // 60:02d}:{whole % 60:02d}"


def quoted(text: str) -> str:
    """What was said, unable to close the `<transcript>` it sits in."""
    return text.replace("<", "\u2039").replace(">", "\u203a")  # single angle quotes


def describe(turn: Turn) -> str:
    about = "≈" if turn.approximate else ""
    if turn.who == "advocate":
        if turn.cut is not None:
            whole = f" of {turn.of:.1f} s" if turn.of else ""
            heard = f"cut off by YOU after {turn.cut:.1f} s{whole}"
        elif turn.spoken is not None:
            heard = f"spoke {about}{turn.spoken:.1f} s"
        else:
            heard = "shown, not spoken"
        return f'[{clock(turn.at)}] ADVOCATE ({heard}): "{quoted(turn.text)}"'
    parts = []
    if turn.think is not None:
        parts.append(
            f"started {-turn.think:.1f} s before the ADVOCATE finished (talked over it)"
            if turn.think < 0
            else f"answered after {about}{turn.think:.1f} s"
        )
    if turn.typed:
        parts.append("typed")
    elif turn.spoken is not None:
        parts.append(f"spoke {about}{turn.spoken:.1f} s")
    parts.append(f"{turn.words} words")
    return f'[{clock(turn.at)}] YOU ({" · ".join(parts)}): "{quoted(turn.text)}"'


def render(turns: Sequence[Turn], measured: dict[str, object]) -> str:
    """The judge's user message: the round, then the numbers."""
    lines = "\n".join(describe(t) for t in turns)
    numbers = "\n".join(f"- {key}: {value}" for key, value in measured.items() if value is not None)
    return (
        f"<transcript>\n{lines}\n</transcript>\n\n"
        f"Measured statistics (seconds; talk_share is YOU's percent of all speech):\n{numbers}\n\n"
        "Rule on this round now, as the JSON object you were asked for."
    )


def parse(text: str) -> dict[str, Any]:
    """The verdict in a reply, held to the shape the page draws. Raises
    `ValueError` naming what is wrong."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    raw = json.loads(text[start : end + 1])
    if not isinstance(raw, dict):
        raise ValueError("the reply is not a JSON object")
    for key in ("split", "headline", "reasoning"):
        if not raw.get(key):
            raise ValueError(f"the verdict has no {key!r}")
    split = raw["split"]
    you = split.get("you") if isinstance(split, dict) else None
    if isinstance(you, bool) or not isinstance(you, int | float):
        raise ValueError("the split has no number for 'you'")
    you = min(99, max(1, round(you)))
    if you == 50:  # not allowed; the named outcome breaks the tie
        you = 51 if str(raw.get("outcome", "")).lower() == "win" else 49
    raw["split"] = {"you": you, "advocate": 100 - you}
    raw["outcome"] = "win" if you > 50 else "lose"
    cards = raw.get("scorecard")
    raw["scorecard"] = [
        {**card, "score": min(10, max(1, round(card["score"])))}
        for card in (cards if isinstance(cards, list) else [])
        if isinstance(card, dict)
        and isinstance(card.get("score"), int | float)
        and not isinstance(card.get("score"), bool)
    ]
    return raw


def too_short(turns: Iterable[Turn]) -> bool:
    yours = [t for t in turns if t.who == "you"]
    return len(yours) < MIN_TURNS or sum(t.words for t in yours) < MIN_WORDS


def heading(name: str, model: str, turns: list[Turn]) -> dict[str, Any]:
    """Who ruled and what was measured: in every ruling, whatever its outcome."""
    choice = BY_NAME.get(name)
    return {
        "judge": {"name": name, "title": choice.title if choice else name, "model": model},
        "stats": stats(turns),
    }


def unavailable(name: str, turns: list[Turn], exc: Exception) -> dict[str, Any]:
    """The ruling when the judge could not even be built (a missing key)."""
    choice = BY_NAME.get(name)
    model = choice.model if choice else ""
    return {**heading(name, model, turns), "status": "failed", "error": str(exc)[:200]}


async def rule(llm: LLM, name: str, turns: list[Turn]) -> dict[str, Any]:
    """The ruling the page shows: a verdict, a no-contest, or a failure that says
    why. Never raises: a judge that fails must not take the record down with it."""
    ruling = heading(name, llm.model, turns)
    measured = ruling["stats"]
    if too_short(turns):
        return {**ruling, "status": "no_contest"}
    usage = Usage()
    started = timing.now()
    reply: list[str] = []
    try:
        async for fragment in llm.stream(
            prompts.load("judge"), [Message("user", render(turns, measured))], usage
        ):
            reply.append(fragment)
        verdict = parse("".join(reply))
    except Exception as exc:  # any failure is reported on the page, never raised
        logger.warning("the judge failed: %r", exc)
        return {
            **ruling,
            "status": "failed",
            "error": str(exc)[:200],
            "ms": round((timing.now() - started) * 1000),
        }
    return {
        **ruling,
        "status": "done",
        "verdict": verdict,
        "ms": round((timing.now() - started) * 1000),
        "usage": {"prompt_tokens": usage.prompt_tokens, "output_tokens": usage.output_tokens},
    }


HEADING = re.compile(r"^## (\d\d):(\d\d):(\d\d) — (.+)$")
NUMBER = r"(-?\d+(?:\.\d+)?)"
WORDS_PER_SECOND = 2.5
"""Conversational speech, for a record that never wrote down when the user began."""


def note(notes: str, kind: str, key: str) -> float | None:
    """`key`'s value in the record's `kind: … · key N · …` note."""
    for line in notes.splitlines():
        if line.startswith(f"`{kind}") and (found := re.search(rf"\b{key} {NUMBER}", line)):
            return float(found.group(1))
    return None


def from_record(text: str) -> list[Turn]:
    """A saved round (`record.py`) as turns, for judging it again offline.

    The record keeps one-second headings and no speech onsets, so every time is
    an estimate and says so: a user turn's start is backed off from when the
    VAD heard them stop by their word count.
    """
    blocks: list[tuple[float, str, list[str], list[str]]] = []
    base: int | None = None
    for line in text.splitlines():
        if match := HEADING.match(line):
            h, m, s, speaker = match.groups()
            second = int(h) * 3600 + int(m) * 60 + int(s)
            base = second if base is None else base
            blocks.append((float(second - base), speaker.strip(), [], []))
        elif blocks and line.strip():
            _, _, lines, noted = blocks[-1]
            (noted if line.startswith("`") else lines).append(line)

    turns: list[Turn] = []
    for at, speaker, lines, noted in blocks:
        body, notes = "\n".join(lines).strip(), "\n".join(noted)
        if not body:
            continue
        if speaker.startswith("agent"):
            played = note(notes, "truncated", "played_ms")
            full = note(notes, "audio_end", "seconds")
            cut = played / 1000 if played is not None else None
            spoken = cut if cut is not None else full
            turns.append(
                Turn("advocate", at, body, spoken=spoken, cut=cut, of=full, approximate=True)
            )
        elif speaker.startswith("you"):
            if "typed" in speaker:
                turns.append(Turn("you", at, body, typed=True, approximate=True))
                continue
            stop_ms = note(notes, "final", "speech_end_ms") or 0.0
            spoken = round(len(body.split()) / WORDS_PER_SECOND, 1)
            start = max(0.0, at - stop_ms / 1000 - spoken)
            turns.append(Turn("you", start, body, spoken=spoken, approximate=True))
    return with_think_times(sorted(turns, key=lambda t: t.at))


def main(record: Path, name: str | None = None) -> None:
    """`voice-agent --judge RECORD`: rule on a saved round, print the ruling."""
    chosen = name or JUDGES[0].name
    if chosen not in BY_NAME:
        raise SystemExit(f"voice-agent: no judge {chosen!r}; one of: {', '.join(BY_NAME)}")
    turns = from_record(record.read_text(encoding="utf-8"))
    try:
        llm = build(chosen)
    except ConfigError as exc:
        raise SystemExit(f"voice-agent: {exc}") from exc
    ruling = asyncio.run(rule(llm, chosen, turns))
    print(json.dumps(ruling, ensure_ascii=False, indent=2))
