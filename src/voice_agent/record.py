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
        """Something the page never saw: a mic expiry, a turn that died."""
        self._write(f"\n`{text}`\n")

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

    def _header(self, payload: Mapping[str, Any]) -> None:
        """Written once per conversation; a reconnect says so instead.

        Reloading the link resumes the same conversation, so it is the same
        file — a second header would read as a second conversation.
        """
        if self._path.exists() and self._path.stat().st_size:
            self._write(f"\n## {self._clock()} — reconnected\n")
            return
        voice, ears = payload.get("voice") or {}, payload.get("ears") or {}
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"started {started} · {payload.get('provider')}/{payload.get('model')}"
        # The option, in brackets: provider and model cannot say which of the
        # six ran, because the three DeepSeek tiers are one provider and one
        # model differing only in the effort sent with the request. Live, that
        # made a session's tier unrecoverable — the startup log builds all six,
        # so nothing else in the record or the logs names the one chosen.
        if payload.get("choice"):
            line += f" ({payload['choice']})"
        if voice:
            line += f" · voice {voice.get('provider')} {voice.get('voice')}"
        if ears:
            line += f" · ears {ears.get('provider')}"
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
