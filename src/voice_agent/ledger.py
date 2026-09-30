"""The session records read back, for `/admin`: one row per file, and sums.

The records are the only thing that survives a deploy, so they are the source
of truth: nothing here is counted in memory. Reading is tolerant by design —
a record is written by a live conversation that may die mid-line, and older
records predate the `totals` line — so an unparseable line is skipped and a
file that cannot be read at all becomes a row with its `error`, never a raise.
"""

import contextlib
import re
import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

TOPIC_CHARS = 140
"""A topic is a table cell, not a summary."""

BLOCK = re.compile(r"^## (\d\d):(\d\d):(\d\d) — (.+)$")
NOTE = re.compile(r"^`(.*)`$")
VERDICT = re.compile(r"^\*\*([A-Z ]+)\*\* (\d+)/(\d+)")
BRACKETED = re.compile(r"\(([^)]*)\)")

COUNTED = ("replies", "you", "prompt_tokens", "cached_tokens", "output_tokens", "tts_chars")


@dataclass
class SessionSummary:
    file: str
    id: str = ""
    started: datetime | None = None
    role: str = ""
    llm: str = ""
    ears: str = ""
    voice: str = ""
    judge: str = ""
    visitor: str = ""
    client: str = ""
    mobile: bool = False
    duration_s: float = 0.0
    replies: int = 0
    you: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    tts_chars: int = 0
    mic_s: float = 0.0
    judge_tokens: int = 0
    outcome: str = ""
    """`WIN`/`LOSE` as the judge wrote it, `no contest`, `failed`, or blank."""
    score: str = ""
    topic: str = ""
    ended: bool = False
    measured: bool = False
    """Has at least one `totals` line. Older records' counts are summed from
    per-reply notes instead, and they never counted the microphone."""
    error: str = ""

    def as_json(self) -> dict[str, Any]:
        row = asdict(self)
        row["started"] = self.started.isoformat(sep=" ") if self.started else None
        return row


def pairs(note: str) -> dict[str, str]:
    """`key value · key value` into a dict; parts without a value are skipped."""
    found: dict[str, str] = {}
    for part in note.split(" · "):
        key, _, value = part.strip().partition(" ")
        if value:
            found[key] = value
    return found


def number(raw: str | None) -> float:
    try:
        return float(raw) if raw is not None else 0.0
    except ValueError:
        return 0.0


def settings(row: SessionSummary, line: str) -> None:
    """The header's second line: `started … · role … · llm … · ears … · …`."""
    for key, value in pairs(line).items():
        if key == "started":
            with contextlib.suppress(ValueError):
                row.started = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        elif key == "llm" or key == "voice":
            # The menu option is in brackets; without one, the model is the name.
            chosen = BRACKETED.search(value)
            setattr(row, key, chosen.group(1) if chosen else value.split()[0])
        elif key in ("role", "ears", "judge", "visitor"):
            setattr(row, key, value)
        elif "/" in key and not row.llm:  # older records name the model unlabelled
            chosen = BRACKETED.search(value)
            row.llm = chosen.group(1) if chosen else key


def clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TOPIC_CHARS else text[: TOPIC_CHARS - 1] + "…"


def parse(path: Path) -> SessionSummary:
    row = SessionSummary(file=path.name)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        row.error = str(exc)
        return row
    if lines and lines[0].startswith("# Conversation "):
        row.id = lines[0].removeprefix("# Conversation ").strip()
    if len(lines) > 1 and lines[1].startswith("started "):
        settings(row, lines[1])

    speaker = ""
    clocks: list[int] = []
    connected = 0.0
    socket_s = 0.0
    judged_length = 0.0
    first_words = ""
    stated = ""
    fallback = dict.fromkeys(COUNTED, 0)
    for number_, line in enumerate(lines):
        if block := BLOCK.match(line):
            hours, minutes, seconds, speaker = block.groups()
            clocks.append(int(hours) * 3600 + int(minutes) * 60 + int(seconds))
            if speaker.startswith("you"):
                fallback["you"] += 1
                if not first_words:
                    # Past the block's `at` note: its millisecond, not its words.
                    body = next(
                        (
                            ln
                            for ln in lines[number_ + 1 :]
                            if ln.strip() and not ln.startswith("`at ")
                        ),
                        "",
                    )
                    first_words = "" if body.startswith(("#", "`")) else body
            elif speaker == "agent":
                fallback["replies"] += 1
            continue
        if speaker.startswith("judge"):
            if verdict := VERDICT.match(line):
                row.outcome, you, advocate = verdict.groups()
                row.score = f"{you}/{advocate}"
            elif line.startswith("No contest"):
                row.outcome = "no contest"
            elif line.startswith("The judge failed"):
                row.outcome = "failed"
            elif line.startswith("- stated: "):
                stated = line.removeprefix("- stated: ")
        note = NOTE.match(line)
        if note is None:
            continue
        text = note.group(1)
        if text == "ended":
            row.ended = True
        elif text.startswith("client: "):
            client = text.removeprefix("client: ")
            row.mobile = " · mobile" in client
            row.client = client.split(" · ")[0]
        elif text.startswith("totals · "):
            row.measured = True
            found = pairs(text)
            for key in COUNTED:
                setattr(row, key, getattr(row, key) + int(number(found.get(key))))
            row.mic_s += number(found.get("mic_s"))
            connected += number(found.get("connected_s"))
        elif text.startswith("socket closed: "):
            socket_s += number(pairs(text).get("after_s"))
        elif speaker.startswith("judge"):
            found = pairs(text)
            if "model" in found:
                row.judge_tokens += int(
                    number(found.get("prompt_tokens")) + number(found.get("output_tokens"))
                )
            judged_length = judged_length or number(found.get("length_s"))
        elif speaker.startswith("agent"):
            found = pairs(text)
            for key in ("prompt_tokens", "cached_tokens", "output_tokens"):
                fallback[key] += int(number(found.get(key)))
            if row.voice and row.voice != "silent":
                fallback["tts_chars"] += int(number(found.get("chars")))

    if not row.measured:
        for key, value in fallback.items():
            setattr(row, key, value)
    span = 0.0
    if len(clocks) > 1:
        span = (clocks[-1] - clocks[0]) % 86400  # a conversation over midnight
    row.duration_s = connected or socket_s or judged_length or span
    row.topic = clip(stated or first_words)
    return row


_cache: dict[Path, tuple[float, int, SessionSummary]] = {}


def read(directory: Path | None) -> list[SessionSummary]:
    """Every record in `directory`, newest first. Cached per file by mtime and
    size, so a page load re-reads only what changed."""
    if directory is None or not directory.is_dir():
        return []
    rows = []
    for path in directory.glob("*.md"):
        try:
            stat = path.stat()
        except OSError:
            continue
        cached = _cache.get(path)
        if cached is None or cached[:2] != (stat.st_mtime, stat.st_size):
            cached = (stat.st_mtime, stat.st_size, parse(path))
            _cache[path] = cached
        rows.append(cached[2])
    return sorted(rows, key=lambda row: row.file, reverse=True)


@dataclass
class Stats:
    sessions: int = 0
    engaged: int = 0
    """Sessions where the user said at least one thing: `GET /` mints a record
    for every page that connects, including the ones nobody spoke in."""
    duration_s: float = 0.0
    avg_duration_s: float = 0.0
    median_duration_s: float = 0.0
    replies: int = 0
    you: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    judge_tokens: int = 0
    tts_chars: int = 0
    mic_s: float = 0.0
    judged: int = 0
    wins: int = 0
    visitors: int = 0
    mobile: int = 0
    by: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    """`by[dimension][value]` → sessions, duration and tokens for that value."""


EMPTY_CELL = ("sessions", "duration_s", "prompt_tokens", "output_tokens")

DIMENSIONS = ("role", "llm", "ears", "voice", "judge")


def aggregate(rows: Iterable[SessionSummary], since: datetime | None = None) -> Stats:
    chosen = [
        row
        for row in rows
        if not row.error and (since is None or (row.started is not None and row.started >= since))
    ]
    stats = Stats(sessions=len(chosen))
    engaged = [row for row in chosen if row.you]
    durations = [row.duration_s for row in engaged]
    stats.engaged = len(engaged)
    stats.duration_s = sum(row.duration_s for row in chosen)
    if durations:
        stats.avg_duration_s = round(statistics.fmean(durations), 1)
        stats.median_duration_s = round(statistics.median(durations), 1)
    for key in (*COUNTED, "judge_tokens"):
        setattr(stats, key, sum(getattr(row, key) for row in chosen))
    stats.mic_s = round(sum(row.mic_s for row in chosen), 1)
    judged = [row for row in chosen if row.outcome in ("WIN", "LOSE")]
    stats.judged = len(judged)
    stats.wins = sum(row.outcome == "WIN" for row in judged)
    stats.visitors = len({row.visitor for row in chosen if row.visitor})
    stats.mobile = sum(row.mobile for row in chosen)
    for dimension in DIMENSIONS:
        table: dict[str, dict[str, float]] = {}
        for row in chosen:
            value = getattr(row, dimension) or "—"
            cell = table.setdefault(value, dict.fromkeys(EMPTY_CELL, 0))
            cell["sessions"] += 1
            cell["duration_s"] += row.duration_s
            cell["prompt_tokens"] += row.prompt_tokens
            cell["output_tokens"] += row.output_tokens
        stats.by[dimension] = table
    return stats


def windows(rows: list[SessionSummary], deployed: datetime, now: datetime) -> dict[str, Stats]:
    """The page's four views of the same rows. Records say when they started to
    the second, so the deploy is too: a session in its first second is its own."""
    return {
        "deploy": aggregate(rows, deployed.replace(microsecond=0)),
        "day": aggregate(rows, now - timedelta(days=1)),
        "week": aggregate(rows, now - timedelta(days=7)),
        "all": aggregate(rows),
    }


SECTIONS = (
    ("Role", "role", "role"),
    ("Reasoning", "llm", "llm"),
    ("Ears", "stt", "ears"),
    ("Voice", "tts", "voice"),
    ("Judge", "judge", "judge"),
)
"""The start screen's groups in its order: (title, the menu's key, the record's field)."""

PROVIDERS = {
    "anthropic": "Claude",
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
    "elevenlabs": "ElevenLabs",
}
EARS = {"assemblyai": "AssemblyAI", "elevenlabs": "ElevenLabs Scribe"}
"""Display names as `web/start.js` words them (its `PROVIDERS` and `LABELS`)."""

NOT_OFFERED = {
    "none": "none — plain assistant, not on the start screen",
    "deaf": "none — typing only",
    "silent": "none — silent",
    "—": "not recorded (older records)",
}


def label(group: str, option: dict[str, Any]) -> str:
    """An offered option as the start screen names it. A model's title leaves out
    its vendor, and the three DeepSeek tiers share one title, so both are added."""
    name = str(option.get("name", ""))
    provider = str(option.get("provider", ""))
    vendor = PROVIDERS.get(provider, provider)
    if group == "llm":
        hint = option.get("hint")
        return f"{vendor} {option.get('title')}" + (f" · {hint}" if hint else "")
    if group == "stt":
        return EARS.get(name, name)
    if group == "tts":
        return f"{vendor} {option.get('title')}"
    return str(option.get("title") or name)


def breakdown(stats: Stats, menu: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Every option the start screen offers, used or not, in its order; then what
    the records hold that it no longer offers. A judge is only ever picked for a
    judged role, so the other sessions are no judge's at all and are left out."""
    sections = []
    for title, group, field_name in SECTIONS:
        used = dict(stats.by.get(field_name, {}))
        if field_name == "judge":
            used.pop("—", None)
        rows: list[dict[str, Any]] = []
        for option in menu.get(group, []):
            name = str(option.get("name", ""))
            cell = used.pop(name, None) or dict.fromkeys(EMPTY_CELL, 0)
            rows.append({"name": name, "label": label(group, option), "offered": True, **cell})
        for name, cell in sorted(used.items(), key=lambda item: -item[1]["sessions"]):
            shown = NOT_OFFERED.get(name, f"{name} — no longer offered")
            rows.append({"name": name, "label": shown, "offered": False, **cell})
        sections.append({"title": title, "rows": rows})
    return sections
