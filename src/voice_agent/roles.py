"""What the agent is in a conversation: a role card, loaded from a file.

A role is data, never code. It says what the agent is for, what is worth
stepping in for and what is not, which moves it may make, and how it speaks. It
does not decide timing, invent moves, or lift the safety floor: those belong to
the code (`thinker.py`, and the chapters that act on thoughts), so a role
written by anyone — later, by users — can change the agent's judgement but
never its manners.

A card is Markdown with TOML front matter between `+++` lines:

    +++
    name = "Devil's advocate"
    summary = "…"
    opening = "…"                 # the greeting; it asks for what to work on
    moves = ["challenge", …]      # a subset of MOVES
    assertiveness = "assertive"   # one of ASSERTIVENESS
    +++
    ## Job / ## Worth it / ## Not worth it / ## When speaking
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from voice_agent import prompts
from voice_agent.errors import ConfigError

ROLES_DIR = prompts.DIR / "roles"
"""Resolved relative to the source checkout, like the system prompt."""

NO_ROLE = "none"
"""The plain assistant: no role section, no thinker."""

DEFAULT_ROLE = NO_ROLE
"""What the start screen has selected before anyone chooses. `VOICE_AGENT_ROLE`
/ `--role` pre-select a card instead; each conversation still picks its own."""

MOVES = ("challenge", "clarify", "redirect", "summarise")
"""The fixed vocabulary of what a thought can propose. The contract with the
chapters that will speak thoughts: they know how to deliver each of these, so a
role picks from the list rather than adding to it."""

ASSERTIVENESS = ("patient", "normal", "assertive")
"""How readily the role presses. Calibrates the thinker's urgency now; decides
how hard the agent takes the floor once it can."""

SECTIONS = ("Job", "Worth it", "Not worth it", "When speaking")

SLUG = re.compile(r"^[a-z0-9_]{1,40}$")
"""A role name is a file name: nothing that could walk out of `ROLES_DIR`."""

FRONT = re.compile(r"\A\+\+\+\n(.*?)\n\+\+\+\n(.*)\Z", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Role:
    slug: str
    name: str
    summary: str
    opening: str
    moves: tuple[str, ...]
    assertiveness: str
    job: str
    worth_it: str
    not_worth_it: str
    when_speaking: str


def parse(text: str, slug: str) -> Role:
    """A card's text as a `Role`, or a `ConfigError` naming what is wrong."""
    match = FRONT.match(text.replace("\r\n", "\n"))
    if match is None:
        raise ConfigError(f"role {slug!r}: no +++ front matter at the top")
    try:
        front = tomllib.loads(match.group(1))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"role {slug!r}: front matter is not TOML: {exc}") from exc
    found = prompts.sections(match.group(2))

    missing = [s for s in SECTIONS if not found.get(s)]
    if missing:
        raise ConfigError(f"role {slug!r}: missing or empty sections: {', '.join(missing)}")
    for key in ("name", "summary", "opening", "assertiveness"):
        if not isinstance(front.get(key), str) or not front[key].strip():
            raise ConfigError(f"role {slug!r}: {key!r} must be a non-empty string")
    moves = front.get("moves")
    if not isinstance(moves, list) or not moves or not all(isinstance(m, str) for m in moves):
        raise ConfigError(f"role {slug!r}: 'moves' must be a non-empty list of strings")
    unknown = [m for m in moves if m not in MOVES]
    if unknown:
        raise ConfigError(
            f"role {slug!r}: unknown moves {unknown}; a role picks from {list(MOVES)}"
        )
    if front["assertiveness"] not in ASSERTIVENESS:
        raise ConfigError(
            f"role {slug!r}: assertiveness must be one of {list(ASSERTIVENESS)}, "
            f"not {front['assertiveness']!r}"
        )
    return Role(
        slug=slug,
        name=front["name"].strip(),
        summary=front["summary"].strip(),
        opening=front["opening"].strip(),
        moves=tuple(dict.fromkeys(moves)),
        assertiveness=front["assertiveness"],
        job=found["Job"],
        worth_it=found["Worth it"],
        not_worth_it=found["Not worth it"],
        when_speaking=found["When speaking"],
    )


def load(slug: str, directory: Path | None = None) -> Role:
    if not SLUG.match(slug):
        raise ConfigError(f"role name {slug!r}: lowercase letters, digits and _ only")
    path = (ROLES_DIR if directory is None else directory) / f"{slug}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        known = ", ".join(available(directory)) or "none"
        raise ConfigError(f"no role {slug!r}; available: {known}") from None
    return parse(text, slug)


def available(directory: Path | None = None) -> tuple[str, ...]:
    root = ROLES_DIR if directory is None else directory
    return tuple(sorted(p.stem for p in root.glob("*.md") if SLUG.match(p.stem)))
