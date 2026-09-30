"""Conversations replayed from tapes on virtual time, against golden transcripts.

Each `tests/tapes/*.tape` is what a user did; its `.golden` is what the agent
did in reply, frame by frame and to the millisecond of virtual time. A change to
turn-taking shows up here as a diff to read, not as a feeling in a live call.

`UPDATE_GOLDEN=1 uv run pytest tests/test_tapes.py` rewrites the goldens after
a change that is meant to alter them; the diff is then the review.
"""

import os
from pathlib import Path

import pytest

from tests.tapes.harness import load, run
from voice_agent.stt.base import LanguageHint

TAPES = sorted((Path(__file__).parent / "tapes").glob("*.tape"))


@pytest.mark.parametrize("tape", TAPES, ids=[t.stem for t in TAPES])
def test_the_replay_matches_its_golden(tape: Path) -> None:
    got = run(load(tape))
    golden = tape.with_suffix(".golden")
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(got, encoding="utf-8")
    assert golden.exists(), f"{golden.name} is missing: run with UPDATE_GOLDEN=1 and review it"
    assert got == golden.read_text(encoding="utf-8")


def test_a_replay_is_deterministic() -> None:
    tape = load(TAPES[0])
    assert run(tape) == run(load(TAPES[0]))


def test_a_silence_of_minutes_replays_in_well_under_a_second() -> None:
    import time

    started = time.perf_counter()
    run(load(Path(__file__).parent / "tapes" / "typed_then_silence.tape"))
    assert time.perf_counter() - started < 5.0


def test_a_replay_opens_later_sessions_on_what_was_heard() -> None:
    """What the recognizer was told, session by session. The golden shows each
    commit's language but nothing about the hint a *later* session opened on,
    and that is the fix the whole change is: one guessing session, then the
    conversation's own answer."""
    hints: list[list[LanguageHint | None]] = []

    run(load(Path(__file__).parent / "tapes" / "language_hint.tape"), hints=hints)

    (sessions,) = hints
    assert sessions[0] is None, "the first session cannot know: it must guess"
    # Two listening sessions: the tape's third commit lands after the last
    # `listen` was stopped, so there is no third session to open.
    assert [(h.candidates, h.pin) for h in sessions[1:] if h] == [(("rus",), None)]


def test_production_reads_the_high_resolution_clock() -> None:
    """Not the loop's: under uvloop that has millisecond resolution and its own
    base. Only the tapes swap it, and only while they run."""
    import time

    from voice_agent import timing

    assert timing.now is time.perf_counter
    with timing.using(lambda: 1.0):
        assert timing.now() == 1.0
    assert timing.now is time.perf_counter
