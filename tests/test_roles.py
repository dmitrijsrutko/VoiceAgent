"""Role cards: the shipped ones load, a new one needs no code, and a broken one
is refused at startup with a reason."""

from pathlib import Path

import pytest

from voice_agent import roles
from voice_agent.errors import ConfigError
from voice_agent.thinker import system_prompt

CARD = """+++
name = "Test coach"
summary = "Keeps a speaker on time."
opening = "What are you rehearsing?"
moves = ["redirect", "summarise"]
assertiveness = "patient"
+++

A note for whoever edits this card, ignored.

## Job

Keep a rehearsal on its outline. Braces {like these} are just text.

## Worth it

Going over time on a section.

## Not worth it

Filler words.

## When speaking

You are a patient rehearsal coach.
"""


def write(directory: Path, slug: str, text: str) -> Path:
    path = directory / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_shipped_role_loads() -> None:
    shipped = roles.available()

    assert "devils_advocate" in shipped
    assert roles.DEFAULT_ROLE == roles.NO_ROLE  # a role is chosen, never assumed
    for slug in shipped:
        role = roles.load(slug)
        assert role.opening and set(role.moves) <= set(roles.MOVES)


def test_a_new_role_is_a_file_not_code(tmp_path: Path) -> None:
    """The point of roles as data: a second card, nowhere mentioned in the
    code, loads and reaches the thinker's instructions whole."""
    write(tmp_path, "coach", CARD)

    role = roles.load("coach", tmp_path)
    prompt = system_prompt(role)

    assert role.moves == ("redirect", "summarise")
    assert role.assertiveness == "patient"
    assert "note for whoever edits" not in role.job
    assert "Keep a rehearsal on its outline" in prompt
    assert "{like these}" in prompt  # role text is never a format string
    assert "Devil" not in prompt
    for placeholder in ("{name}", "{job}", "{moves}", "{assertiveness}"):
        assert placeholder not in prompt


def test_the_thinker_prompt_drops_its_developer_preamble() -> None:
    prompt = system_prompt(roles.load("devils_advocate"))

    assert "Filled per conversation" not in prompt
    assert prompt.startswith("You are the inner voice")


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        (lambda t: t.replace('"redirect"', '"insult"'), "unknown moves"),
        (lambda t: t.replace('"patient"', '"rude"'), "assertiveness"),
        (lambda t: t.replace("## Not worth it\n\nFiller words.\n", ""), "Not worth it"),
        (lambda t: t.replace("+++\n", "", 1), "front matter"),
        (lambda t: t.replace('opening = "What are you rehearsing?"', 'opening = ""'), "opening"),
        (lambda t: t.replace('moves = ["redirect", "summarise"]', "moves = []"), "moves"),
    ],
)
def test_a_broken_card_is_refused_with_a_reason(
    tmp_path: Path, change: object, complaint: str
) -> None:
    assert callable(change)
    write(tmp_path, "broken", change(CARD))

    with pytest.raises(ConfigError, match=complaint):
        roles.load("broken", tmp_path)


def test_a_role_name_cannot_walk_out_of_the_roles_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="letters, digits"):
        roles.load("../system_prompt", tmp_path)


def test_an_unknown_role_names_the_ones_there_are(tmp_path: Path) -> None:
    write(tmp_path, "coach", CARD)

    with pytest.raises(ConfigError, match="available: coach"):
        roles.load("nobody", tmp_path)


def test_a_placeholder_written_in_a_card_stays_literal(tmp_path: Path) -> None:
    """Filled in one pass over the template: a role's own text is inserted,
    never scanned again, so it cannot pull other values into itself."""
    sneaky = CARD.replace("Braces {like these}", "Say {moves} and {assertiveness}")
    write(tmp_path, "sneaky", sneaky)

    prompt = system_prompt(roles.load("sneaky", tmp_path))

    assert "Say {moves} and {assertiveness}" in prompt
