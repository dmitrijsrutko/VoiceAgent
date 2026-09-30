"""What the after-voice echo rules would drop, measured on real conversations.

    uv run python scripts/echo_eval.py [fly-archive/sessions] > report.txt

Reads recorded conversations (personal data: the report stays out of git) and,
for every spoken user turn, finds the agent line before it and estimates when
that voice stopped: its heading second, plus `first_audio_ms`, plus what was
played (`truncated played_ms`) or its whole audio (`audio_end seconds`).
Headings have one-second resolution, so every gap is good to about a second.

Three rules are compared:
- before: a turn committed after the voice was never dropped;
- first:  `is_echo_final` against the whole reply, window counted from the end
          of the audio plus the 10 s playback grace (the first attempt);
- now:    `is_replay`, window counted from the voice's end.
A turn that cut the voice off (an `interrupt` note from the mic, on the voice
or on the turn) went through the barge-in path instead and is counted apart.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from voice_agent.echo import FINAL_MIN_WORDS, is_echo_final, is_replay, words
from voice_agent.heard import PLAYBACK_GRACE_SECONDS
from voice_agent.session import ECHO_WINDOW_SECONDS

HEADING = re.compile(r"^## (\d\d):(\d\d):(\d\d) — (.+)$")
CLOCK = r"(\d\d):(\d\d):(\d\d)\.(\d{3})"
AT = re.compile(rf"^at {CLOCK}$")
ENDED = re.compile(rf"\bended_at {CLOCK}")
VISITOR = re.compile(r"visitor (\w+)")
NUMBER = r"([\d.]+)"


@dataclass
class Block:
    second: int
    speaker: str
    text: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    visitor: str = ""
    at: float | None = None
    """Its millisecond (`at` note), when the record has one."""

    def note(self, key: str) -> float | None:
        for line in self.notes:
            if match := re.search(rf"\b{key} {NUMBER}", line):
                return float(match.group(1))
        return None


@dataclass
class Case:
    session: str
    at: str
    visitor: str
    heard: str
    said: str
    gap: float
    """Seconds from the voice's end to the turn's heading."""
    gap_first: float
    """The same, from where the first attempt thought the voice ended."""
    over: bool
    """The mic cut the voice before this turn: the barge-in path judged it."""
    level: str = ""
    """The mic during the voice (`mic_level`), when the record has it."""


def hms(second: int) -> str:
    return f"{second // 3600:02}:{second // 60 % 60:02}:{second % 60:02}"


def seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def blocks(text: str) -> list[Block]:
    found: list[Block] = []
    visitor = VISITOR.search(text)
    current = visitor.group(1) if visitor else ""
    for line in text.splitlines():
        if match := HEADING.match(line):
            h, m, s, speaker = match.groups()
            found.append(Block(int(h) * 3600 + int(m) * 60 + int(s), speaker.strip()))
            found[-1].visitor = current
        elif found and line.strip():
            if (seen := VISITOR.search(line)) and found[-1].speaker == "reconnected":
                current = seen.group(1)
                found[-1].visitor = current
            elif line.startswith("`"):
                note = line.strip("`")
                if found[-1].at is None and (at := AT.match(note)):
                    found[-1].at = seconds(*at.groups())
                found[-1].notes.append(note)
            else:
                found[-1].text.append(line.strip())
    return found


def cases(path: Path) -> list[Case]:
    found: list[Case] = []
    voice: Block | None = None
    played: float | None = None
    over = False
    ended_at: float | None = None
    level = ""
    for block in blocks(path.read_text(encoding="utf-8")):
        mic_cut = block.speaker != "you (typed)" and any(
            n.startswith("interrupt") for n in block.notes
        )
        if block.speaker.startswith("agent") and block.note("seconds") is not None:
            voice, played, over = block, block.note("played_ms"), mic_cut
            ended_at, level = None, ""
        else:
            # A cut is noted on whichever block was open when it was measured,
            # often the turn that cut in, not the voice it cut.
            if played is None and voice is not None:
                played = block.note("played_ms")
            over = over or mic_cut
        # So is the mic's level once the voice stops: on the voice's own block
        # when nothing followed it, else on the next.
        for note in block.notes:
            if note.startswith("mic_level") and (stamp := ENDED.search(note)):
                ended_at, level = seconds(*stamp.groups()), note
        if block.speaker == "you (spoken)" and voice is not None:
            start = voice.second + (voice.note("first_audio_ms") or 0) / 1000
            audio = voice.note("seconds") or 0.0
            end = start + (played / 1000 if played is not None else audio)
            end_first = end if played is not None else start + audio + PLAYBACK_GRACE_SECONDS
            clock: float = block.second
            if block.at is not None and ended_at is not None:
                # To the millisecond: when the page said the voice stopped.
                end = end_first = ended_at
                clock = block.at
            found.append(
                Case(
                    session=path.stem[18:26],
                    at=hms(block.second),
                    visitor=block.visitor,
                    heard=" ".join(block.text),
                    said=" ".join(voice.text),
                    gap=round(clock - end, 1),
                    gap_first=round(clock - end_first, 1),
                    over=over,
                    level=level,
                )
            )
            over = False  # only the turn that cut in went the barge-in way
    return found


def near(case: Case) -> bool:
    """Close to the line: a shorter run, or most of the words anywhere."""
    got, spoken = words(case.heard), set(words(case.said))
    shared = sum(1 for w in got if w in spoken) / len(got) if got else 0
    return len(got) >= FINAL_MIN_WORDS - 1 and shared >= 0.6


def show(case: Case) -> str:
    return (
        f"  {case.session} {case.at} visitor {case.visitor} gap {case.gap:+.1f}s\n"
        f"    heard: {case.heard[:140]}\n    said:  {case.said[:140]}"
        + (f"\n    mic:   {case.level}" if case.level else "")
    )


def main(directory: Path) -> None:
    found = [c for path in sorted(directory.glob("*.md")) for c in cases(path)]
    barged = [c for c in found if c.over]
    every = [c for c in found if not c.over]
    window = ECHO_WINDOW_SECONDS
    first = [c for c in every if c.gap_first <= window and is_echo_final(c.heard, c.said)]
    now = [c for c in every if c.gap <= window and is_replay(c.heard, c.said)]
    # One second either side: headings are whole seconds.
    edge = [
        c for c in every if abs(c.gap - window) <= 1 and is_replay(c.heard, c.said) and c not in now
    ]
    close = [
        c for c in every if c.gap <= window + 4 and near(c) and c not in now and c not in first
    ]
    print(f"{len(every)} spoken turns after an agent voice", end="; ")
    print(f"{len(barged)} more cut it off (barge-in path)")
    print(f"before: 0 dropped · first: {len(first)} dropped · now: {len(now)} dropped\n")
    for title, group in (
        ("NOW drops", now),
        ("FIRST ATTEMPT drops", first),
        ("NOW misses only by the window's last second", edge),
        ("NEAR MISSES (not dropped by either)", close),
        ("BARGE-IN PATH, would replay", [c for c in barged if is_replay(c.heard, c.said)]),
    ):
        print(f"== {title}: {len(group)}")
        for case in group:
            print(show(case))
        print()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fly-archive/sessions"))
