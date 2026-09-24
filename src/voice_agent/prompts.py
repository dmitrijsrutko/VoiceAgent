"""The prompt files in `prompts/`, read when used, never at import.

Every file opens with a note for whoever edits it; only what follows the first
`---` line is sent. Resolved relative to the source checkout, which the server
runs from (the image keeps that layout).
"""

from pathlib import Path

DIR = Path(__file__).resolve().parents[2] / "prompts"

PREAMBLE_SEPARATOR = "\n---\n"


def body(text: str) -> str:
    """A prompt file's text, without the note above its first rule."""
    _, separator, rest = text.partition(PREAMBLE_SEPARATOR)
    return (rest if separator else text).strip()


def load(name: str) -> str:
    """`prompts/<name>.md`, past its note. Read on every call, so an edit shows
    in the next conversation without a restart."""
    return body((DIR / f"{name}.md").read_text(encoding="utf-8"))


def sections(text: str) -> dict[str, str]:
    """`## Heading` → its text, trimmed. Anything before the first heading is
    ignored, so a file can open with a note for whoever edits it."""
    found: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                found[current] = "\n".join(lines).strip()
            current, lines = line[3:].strip(), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        found[current] = "\n".join(lines).strip()
    return found


def rules() -> dict[str, str]:
    """The pieces `config.build_prompt` puts around the persona, by heading."""
    return sections(load("rules"))
