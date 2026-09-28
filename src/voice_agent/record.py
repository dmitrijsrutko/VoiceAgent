"""The conversation as a file you can read afterwards.

One Markdown file per conversation, written as it happens, so telemetry
survives a reload.

It taps `Channel`, the one door every frame to the browser goes through, so the
record cannot drift from what the user saw, and a new frame type is recorded
without anyone remembering to.

**No audio is ever written.** Binary frames are counted, never kept: voice is
biometric data, and timings have been enough to diagnose every bug so far.
"""

import contextlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path("sessions")

SPEAKERS = {
    "greeting": "agent (greeting)",
    "reply_end": "agent",
    "transcript": "you (spoken)",
}
"""Frames carrying something the user read or heard. Everything else is a note
attached to whatever came before it."""

NOISE = frozenset({"delta", "marks", "audio_start", "reply_start", "floor"})
"""One frame per token, per audio chunk, or per change of who is speaking.
Keeping them would bury the conversation in its own telemetry; the trace holds
them."""

SKIP_KEYS = frozenset({"type", "text", "history"})
"""Rendered elsewhere: `type` heads the block, `text` is its prose, and
`history` is the whole conversation replayed on connect."""

MAX_VALUE_CHARS = 120
"""Longer values are a payload, not a measurement, and belong in the trace —
but they are cut short with a marker rather than dropped. Absence has to mean
"the frame did not carry it", never "it was too long to show"."""


def render(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    text = str(value) if isinstance(value, int | str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_VALUE_CHARS else f"{text[:MAX_VALUE_CHARS]}…"


def attributes(payload: Mapping[str, Any]) -> str:
    """Every field the frame carries, as `key value · key value`.

    Generic rather than phrased like the page: a second renderer would drift,
    and this way a new field shows up on its own, unrounded.
    """
    parts = []
    for key, value in payload.items():
        # Falsy is omitted, and absence therefore means zero. Kept every turn,
        # `speculated no · speculation_lead_ms 0 · speculations_discarded 0`
        # is four fields saying nothing happened, on every line. The trace
        # keeps the full picture; this file is the one meant to be read.
        if key in SKIP_KEYS or not value:
            continue
        parts.append(f"{key} {render(value)}")
    return " · ".join(parts)


def review(verdict: Mapping[str, Any]) -> str:
    """The whole ruling as Markdown: everything the page's card shows, so the
    record can be read without the page. A part the judge left out is left out
    here too, never written as "None"."""

    def get(mapping: Any, key: str) -> Any:
        value = mapping.get(key) if isinstance(mapping, Mapping) else None
        return value if value not in (None, "", [], {}) else None

    def section(title: str, rows: list[str]) -> str:
        return f"**{title}**\n" + "\n".join(rows) if rows else ""

    def labelled(pairs: list[tuple[str, Any]]) -> list[str]:
        return [f"- {label}: {value}" for label, value in pairs if value is not None]

    def items(values: Any) -> list[str]:
        return [f"- {value}" for value in values] if isinstance(values, list) else []

    split = get(verdict, "split") or {}
    position = get(verdict, "position")
    moments = get(verdict, "moments")
    persuasion = get(verdict, "persuasion")
    rematch = get(verdict, "rematch")
    fun = get(verdict, "fun")
    cards = [
        f"- {get(card, 'criterion')} {get(card, 'score')}/10 — {get(card, 'evidence')}"
        + (f" ({get(card, 'at')})" if get(card, "at") else "")
        for card in get(verdict, "scorecard") or []
        if get(card, "criterion") and get(card, "score") is not None
    ]
    moment_rows = [
        f"- {label} ({get(moment, 'at')}): "
        f"{get(moment, 'quote') or get(moment, 'objection')} — {get(moment, 'why')}"
        for label, moment in (
            ("best", get(moments, "best")),
            ("weakest", get(moments, "worst")),
            ("unanswered", get(moments, "unanswered")),
        )
        if isinstance(moment, Mapping)
    ]
    fallacies = [
        f"- {get(f, 'name')} ({get(f, 'at')}): {get(f, 'quote')}"
        for f in get(verdict, "fallacies") or []
        if get(f, "name")
    ]
    moved = get(persuasion, "advocate_moved")
    landed = " ".join(
        part
        for part in (
            f"moved the advocate: {moved}." if moved else "",
            str(get(persuasion, "audience") or ""),
        )
        if part
    )
    fun_line = " · ".join(
        str(part)
        for part in (
            get(fun, "nickname"),
            f"badge {get(fun, 'badge')}" if get(fun, "badge") else None,
            get(fun, "crowd"),
            get(fun, "roast"),
        )
        if part
    )
    parts = [
        f"**{str(get(verdict, 'outcome') or '').upper()}** "
        f"{split.get('you')}/{split.get('advocate')} — {get(verdict, 'headline') or ''}",
        str(get(verdict, "reasoning") or ""),
        section(
            "Position",
            labelled(
                [
                    ("stated", get(position, "stated")),
                    ("held", get(position, "survived")),
                    ("fell", get(position, "fell")),
                ]
            ),
        ),
        section("Scorecard", cards),
        section("Moments", moment_rows),
        section("Fallacies", fallacies),
        f"**Did it land?** {landed}" if landed else "",
        f"**Timing** {get(verdict, 'timing')}" if get(verdict, "timing") else "",
        section("How to improve", items(get(verdict, "improve"))),
        section(
            "Rematch",
            items(get(rematch, "prepare"))
            + labelled(
                [
                    ("next attack", get(rematch, "next_attack")),
                    ("missed angle", get(rematch, "missed_angle")),
                ]
            ),
        ),
        f"**Fun** {fun_line}" if fun_line else "",
    ]
    return "\n\n".join(part for part in parts if part.strip())


class Record:
    """One conversation's file, appended to as the conversation happens.

    A record that cannot be written is never allowed to break a conversation:
    every failure here is logged once and then swallowed, and the agent carries
    on without it.
    """

    def __init__(self, path: Path, prompt_id: str = "", trace: str = "") -> None:
        self._path = path
        self._prompt_id = prompt_id
        self._trace = trace
        self._file: Any = None
        self._audio_frames = 0
        self._broken = False
        self._onsets = 0
        self._onsets_over_agent = 0

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> Any:
        if self._file is None and not self._broken:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self._path.open("a", encoding="utf-8")
            except OSError as exc:
                self._broken = True
                logger.warning("conversation not being recorded: %s", exc)
        return self._file

    def _write(self, text: str) -> None:
        handle = self._open()
        if handle is None:
            return
        try:
            handle.write(text)
        except OSError as exc:  # a full disk must not end the conversation
            self._broken = True
            logger.warning("conversation recording stopped: %s", exc)

    @staticmethod
    def _clock() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def block(self, speaker: str, body: str = "", notes: str = "") -> None:
        """One turn, or one thing worth its own heading.

        A blank line before the speech and before every technical line. It
        makes the file scannable — the words stand apart from the numbers —
        and it is also what Markdown needs: adjacent lines are one paragraph,
        so without the blank lines a rendered view runs every measurement
        together into a single run-on line.
        """
        self._write(f"\n## {self._clock()} — {speaker}\n")
        if body.strip():
            self._write(f"\n{body.strip()}\n")
        if notes:
            self.note(notes)

    def note(self, text: str) -> None:
        """Something the page never saw: a mic expiry, a turn that died.

        Flushed at once: a conversation whose teardown never finishes would
        otherwise lose every note after its last message."""
        self._write(f"\n`{text}`\n")
        self.flush()

    def said(self, text: str, how: str = "typed") -> None:
        """Input the browser never echoes back, so `Channel` cannot see it."""
        self.block(f"you ({how})", text)
        self.flush()

    def audio(self, size: int) -> None:
        """A binary frame went out. Counted, never kept."""
        self._audio_frames += 1

    def frame(self, payload: Mapping[str, Any]) -> None:
        kind = str(payload.get("type", ""))
        if kind == "floor" and payload.get("state") == "speaking":
            # Counted, not written: one line at the end is what the echo check
            # needs, and a line per onset would bury the conversation.
            self._onsets += 1
            self._onsets_over_agent += bool(payload.get("agent"))
        if kind in NOISE:
            return
        if kind == "thought" and payload.get("decision") in ("nothing", "unchanged"):
            return  # most considerations end here; the trace keeps every one
        if kind == "ready":
            self._header(payload)
            return
        if kind == "transcript" and not payload.get("final"):
            return  # a partial the recognizer is still rewriting

        if kind in SPEAKERS:
            text = str(payload.get("text", ""))
            # Reset here as well as on `audio_end`: a reply whose audio never
            # closes would otherwise lend its count to the next one.
            self._audio_frames = 0
            self.block(SPEAKERS[kind], text, attributes(payload))
            self.flush()
            return

        details = attributes(payload)
        if kind == "audio_end":
            details = f"{details} · frames {self._audio_frames}"
            self._audio_frames = 0
        self.note(f"{kind}: {details}" if details else kind)
        if kind == "ended":
            self.flush()

    @staticmethod
    def _settings(payload: Mapping[str, Any]) -> str:
        """Role, model, ears and voice, every one named even when absent: a
        record that leaves one out cannot say whether it was off or unlogged."""
        role, voice, ears = (payload.get(k) or {} for k in ("role", "voice", "ears"))
        model = f"{payload.get('provider')}/{payload.get('model')}"
        # The option, in brackets: provider and model cannot say which of the
        # six ran, because the three DeepSeek tiers are one provider and one
        # model differing only in the effort sent with the request. Live, that
        # made a session's tier unrecoverable — the startup log builds all six,
        # so nothing else in the record or the logs names the one chosen.
        if payload.get("choice"):
            model += f" ({payload['choice']})"
        judge = payload.get("judge") or {}
        return " · ".join(
            (
                f"role {role.get('slug') or role.get('name')}" if role else "role none",
                f"llm {model}",
                f"ears {ears.get('provider')}" if ears else "ears deaf",
                (
                    f"voice {voice.get('provider')} {voice.get('model')} "
                    f"({voice.get('choice')}) {voice.get('voice')}"
                    if voice
                    else "voice silent"
                ),
                *((f"judge {judge.get('name')}",) if judge else ()),
            )
        )

    def _header(self, payload: Mapping[str, Any]) -> None:
        """Written once per conversation; a reconnect says so instead.

        Reloading the link resumes the same conversation, so it is the same
        file — a second header would read as a second conversation.
        """
        settings = self._settings(payload)
        if self._path.exists() and self._path.stat().st_size:
            self._write(f"\n## {self._clock()} — reconnected\n{settings}\n")
            return
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"started {started} · {settings}"
        self._write(f"# Conversation {payload.get('session')}\n{line}\n")
        origin = " · ".join(
            part
            for part in (
                f"prompt {self._prompt_id}" if self._prompt_id else "",
                f"trace {self._trace}" if self._trace else "",
            )
            if part
        )
        if origin:
            self._write(f"{origin}\n")
        self.flush()

    def ruling(self, ruling: Mapping[str, Any]) -> None:
        """The judge's ruling, after the conversation it rules on."""
        judge = ruling.get("judge") or {}
        verdict = ruling.get("verdict") or {}
        status = ruling.get("status")
        if status == "done":
            body = review(verdict)
        elif status == "no_contest":
            body = "No contest: too little was said to rule on."
        else:
            body = f"The judge failed: {ruling.get('error', '')}"
        usage = ruling.get("usage") or {}
        self.block(
            f"judge ({judge.get('name')})",
            body,
            attributes({"model": judge.get("model"), "ms": ruling.get("ms"), **usage}),
        )
        self.note(attributes(ruling.get("stats") or {}))
        self.flush()

    def flush(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
            except OSError:
                self._broken = True

    def close(self) -> None:
        if self._onsets:
            self.note(
                f"floor: the user was heard starting to speak {self._onsets} times, "
                f"{self._onsets_over_agent} of them while the agent was talking "
                "(talking over it, or its own voice coming back as echo)"
            )
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None
