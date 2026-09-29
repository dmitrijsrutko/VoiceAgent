"""The records read back: one row per file, whatever state the file is in."""

# ruff: noqa: E501 — the fixtures are records verbatim, and records have long lines

from datetime import datetime
from pathlib import Path

from voice_agent import ledger

MEASURED = """# Conversation abc
started 2026-09-28 10:00:00 · role devils_advocate · llm deepseek/deepseek-flash (deepseek-low) · ears assemblyai · voice elevenlabs eleven_flash_v2_5 (flash-v2.5) EXAV · judge deepseek-high · visitor 0123456789ab

## 10:00:00 — agent (greeting)

Give me a position.

`client: Safari on iOS · mobile`

## 10:00:05 — you (spoken)

Cats are better than dogs.

## 10:00:07 — agent

Are they?

`chars 9 · output_tokens 50 · prompt_tokens 900`

`ended`

`totals · ended_at 10:01:00 · connected_s 60 · replies 1 · you 1 · prompt_tokens 1000 · cached_tokens 400 · output_tokens 60 · tts_chars 30 · mic_s 42`

## 10:01:10 — judge (deepseek-high)

**WIN** 58/42 — A strong round.

**Position**
- stated: Cats are better companions than dogs.

`model deepseek-flash · ms 1000 · prompt_tokens 100 · output_tokens 200`

`length_s 55 · your_turns 1`
"""

OLD = """# Conversation old
started 2026-09-20 09:00:00 · role none · llm anthropic/claude-haiku-4-5 (haiku-4-5) · ears deaf · voice silent

## 09:00:00 — agent (greeting)

Hi.

## 09:00:03 — you (typed)

What is the weather like on the moon, and is it windy there at all?

## 09:00:05 — agent

No wind.

`chars 8 · output_tokens 20 · prompt_tokens 300 · cached_tokens 100`

## 09:00:30 — you (typed)

ok

## 09:00:31 — agent

Bye.

`chars 4 · output_tokens 5 · prompt_tokens 320`

`socket closed: page disconnected (code 1001) · after_s 40`
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_measured_record_reads_its_totals_not_the_per_reply_notes(tmp_path: Path) -> None:
    row = ledger.parse(write(tmp_path, "2026-09-28-100000-abc.md", MEASURED))

    assert (row.id, row.role, row.llm, row.ears, row.voice, row.judge) == (
        "abc",
        "devils_advocate",
        "deepseek-low",
        "assemblyai",
        "flash-v2.5",
        "deepseek-high",
    )
    assert row.started == datetime(2026, 9, 28, 10, 0, 0)
    assert (row.visitor, row.client, row.mobile) == ("0123456789ab", "Safari on iOS", True)
    assert row.measured
    assert (row.prompt_tokens, row.cached_tokens, row.output_tokens) == (1000, 400, 60)
    assert (row.tts_chars, row.mic_s, row.duration_s) == (30, 42, 60)
    assert (row.outcome, row.score, row.judge_tokens) == ("WIN", "58/42", 300)
    assert row.topic == "Cats are better companions than dogs."
    assert row.ended


def test_an_older_record_is_summed_from_its_notes(tmp_path: Path) -> None:
    row = ledger.parse(write(tmp_path, "2026-09-20-090000-old.md", OLD))

    assert not row.measured
    assert (row.you, row.replies) == (2, 2)
    assert (row.prompt_tokens, row.cached_tokens, row.output_tokens) == (620, 100, 25)
    assert row.tts_chars == 0, "a silent conversation synthesised nothing"
    assert row.duration_s == 40, "the socket's own count, when there is no totals line"
    assert row.topic.startswith("What is the weather like on the moon")
    assert (row.outcome, row.voice, row.ears) == ("", "silent", "deaf")


def test_a_reconnected_conversation_adds_up_every_connection(tmp_path: Path) -> None:
    twice = MEASURED + (
        "\n`totals · ended_at 10:05:00 · connected_s 30 · replies 2 · you 2 · "
        "prompt_tokens 10 · cached_tokens 0 · output_tokens 5 · tts_chars 3 · mic_s 8`\n"
    )
    row = ledger.parse(write(tmp_path, "x.md", twice))

    assert (row.replies, row.prompt_tokens, row.duration_s, row.mic_s) == (3, 1010, 90, 50)


def test_a_broken_file_is_a_row_not_a_crash(tmp_path: Path) -> None:
    row = ledger.parse(write(tmp_path, "cut.md", "# Conversation cut\nstarted garbage · llm\n## 1"))

    assert row.id == "cut"
    assert row.started is None
    assert not row.error


def test_rows_come_newest_first_and_the_windows_filter_by_start(tmp_path: Path) -> None:
    write(tmp_path, "2026-09-28-100000-abc.md", MEASURED)
    write(tmp_path, "2026-09-20-090000-old.md", OLD)

    rows = ledger.read(tmp_path)
    assert [row.id for row in rows] == ["abc", "old"]

    views = ledger.windows(rows, deployed=datetime(2026, 9, 25), now=datetime(2026, 9, 28, 12))
    assert views["deploy"].sessions == 1
    assert views["day"].sessions == 1
    assert views["week"].sessions == 1
    assert views["all"].sessions == 2
    everything = views["all"]
    assert (everything.judged, everything.wins, everything.visitors, everything.mobile) == (
        1,
        1,
        1,
        1,
    )
    assert everything.duration_s == 100
    assert everything.by["llm"]["deepseek-low"]["sessions"] == 1
    assert everything.by["ears"]["deaf"]["prompt_tokens"] == 620


MENU = {
    "role": [
        {"name": "devils_advocate", "title": "Devil's advocate"},
        {"name": "partner", "title": "Thinking partner"},
    ],
    "llm": [
        {"name": "haiku-4-5", "title": "Haiku 4.5", "hint": "fastest", "provider": "anthropic"},
        {"name": "deepseek-low", "title": "V4.1 Flash", "hint": "balanced", "provider": "deepseek"},
        {
            "name": "deepseek-high",
            "title": "V4.1 Flash",
            "hint": "smartest",
            "provider": "deepseek",
        },
    ],
    "stt": [{"name": "assemblyai"}, {"name": "elevenlabs"}],
    "tts": [{"name": "flash-v2.5", "title": "Flash v2.5", "provider": "elevenlabs"}],
    "judge": [
        {"name": "deepseek-high", "title": "DeepSeek V4.1 Flash"},
        {"name": "opus", "title": "Claude Opus 5.5"},
    ],
}


def test_the_breakdown_is_the_start_screen_with_zeros_for_what_nobody_picked(
    tmp_path: Path,
) -> None:
    write(tmp_path, "2026-09-28-100000-abc.md", MEASURED)
    write(tmp_path, "2026-09-20-090000-old.md", OLD)
    sections = ledger.breakdown(ledger.aggregate(ledger.read(tmp_path)), MENU)
    by_title = {s["title"]: s["rows"] for s in sections}

    assert [s["title"] for s in sections] == ["Role", "Reasoning", "Ears", "Voice", "Judge"]
    reasoning = by_title["Reasoning"]
    assert [r["label"] for r in reasoning[:2]] == [
        "Claude Haiku 4.5 · fastest",
        "DeepSeek V4.1 Flash · balanced",
    ], "every offered option, in menu order, named as the start screen names it"
    assert [(r["name"], r["sessions"]) for r in reasoning] == [
        ("haiku-4-5", 1),
        ("deepseek-low", 1),
        ("deepseek-high", 0),
    ], "deepseek-high was never picked, and is listed anyway"
    assert [r["label"] for r in by_title["Ears"]] == [
        "AssemblyAI",
        "ElevenLabs Scribe",
        "none — typing only",
    ]
    assert by_title["Ears"][1]["sessions"] == 0
    assert by_title["Ears"][2]["offered"] is False
    assert [r["name"] for r in by_title["Role"]] == ["devils_advocate", "partner", "none"]
    assert by_title["Role"][2]["label"] == "none — plain assistant, not on the start screen"
    judges = by_title["Judge"]
    assert [(r["name"], r["sessions"]) for r in judges] == [("deepseek-high", 1), ("opus", 0)], (
        "an unjudged session is no judge's, so it is not a row"
    )


def test_a_retired_option_follows_the_offered_ones_only_when_used() -> None:
    stats = ledger.Stats(
        by={"llm": {"deepseek-max": dict.fromkeys(ledger.EMPTY_CELL, 0.0) | {"sessions": 3.0}}}
    )
    reasoning = ledger.breakdown(stats, MENU)[1]["rows"]

    assert [r["name"] for r in reasoning] == [
        "haiku-4-5",
        "deepseek-low",
        "deepseek-high",
        "deepseek-max",
    ]
    assert reasoning[3]["label"] == "deepseek-max — no longer offered"
    assert ledger.breakdown(ledger.Stats(), MENU)[1]["rows"][-1]["offered"] is True


def test_a_model_without_a_hint_is_named_without_one() -> None:
    plain = {"name": "x", "title": "Model X", "provider": "anthropic"}

    assert ledger.label("llm", plain) == "Claude Model X"


def test_no_directory_is_no_rows(tmp_path: Path) -> None:
    assert ledger.read(None) == []
    assert ledger.read(tmp_path / "missing") == []
