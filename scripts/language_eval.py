"""What the recognizer was told about the language, session by session.

    uv run python scripts/language_eval.py [fly-archive/traces]

Reads the traces (personal data: the report stays out of git) and prints, per
conversation, every recognizer session in order with the hint it was opened with
and every language the commits reported. This is the only place the hint is
visible — no frame carries it, so a record cannot answer "was this listening
session guessing, narrowed, or pinned?", and a live failure of the kind of
2026-09-30 (one conversation's language reaching another's first sentence) looks
exactly like a bad recognizer until this is read.

    session 08:18:31  no hint (it guesses)
        heard rus over 7 words
    session 08:22:32  narrowed to rus
    session 08:23:01  pinned rus (narrowed to rus)
"""

import json
import sys
from pathlib import Path

EVENTS = ("stt.session", "stt.committed")


def sessions(path: Path) -> dict[str, list[str]]:
    """Every conversation in one trace file: what each listening session was
    told, and what each commit reported, in the order they happened."""
    found: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("kind")
        if kind not in EVENTS:
            continue
        found.setdefault(str(event.get("trace", "?")), []).append(describe(kind, event))
    return found


def describe(kind: str, event: dict[str, object]) -> str:
    at = str(event.get("at", ""))
    if kind == "stt.committed":
        text = str(event.get("text", ""))
        words = len(text.split())
        said = f"{words} word{'s' if words != 1 else ''}" if words else "no words"
        # An empty commit is listed on purpose: it used to be dropped, and with
        # it the only evidence that the guess in force was wrong.
        return f"    heard {event.get('language') or '(no language)'} over {said}"
    pin = event.get("language")
    # `candidates` is absent from a trace written before this field existed, and
    # from a session that really was not narrowed: both read as none.
    sent = event.get("candidates")
    candidates = [str(code) for code in sent] if isinstance(sent, list) else []
    # Nothing says "narrowed" on its own: it is what a non-empty candidate set
    # means, and it is sent whether or not a pin is (see `session_url`).
    narrowed = "narrowed" if candidates else "not narrowed"
    if pin:
        return f"session {at}  pinned {pin} ({narrowed} to {candidates})"
    if candidates:
        return f"session {at}  {narrowed} to {candidates}"
    return f"session {at}  no hint (it guesses)"


def main(directory: Path) -> None:
    files = sorted(directory.glob("*.jsonl"))
    if not files:
        raise SystemExit(f"no traces in {directory} — run scripts/pull-fly.sh first")
    for path in files:
        for trace, lines in sessions(path).items():
            print(f"{path.name}  {trace}")
            print("\n".join(f"  {line}" for line in lines))
            print()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fly-archive/traces"))
