"""Who said what, when, and how long the user took to answer: a round as the judge reads it.

It taps `Channel` like `Record` does, so it sees exactly what the page was sent,
and adds what only the page knows: when its playback went quiet. Raw events are
kept as they arrive and paired into turns only when asked, because a reply's
`audio_end` can reach the socket before its `reply_end` and the greeting's text
before its audio.

Times are seconds from the first thing recorded — the greeting — on
`timing.now()`, so a link opened and left on its start screen does not count.
"""

import statistics
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from voice_agent import timing

Who = Literal["you", "advocate"]


@dataclass(frozen=True, slots=True)
class Turn:
    who: Who
    at: float
    """When it started: the agent's first audio, the user's first sound (or the
    commit, when no onset was heard), a typed line's arrival."""
    text: str
    spoken: float | None = None
    """Seconds of speech: the agent's as heard, the user's from onset to the
    moment the VAD heard them stop."""
    think: float | None = None
    """User only: onset minus the end of the agent's previous turn. Negative when
    they started while the agent was still speaking."""
    cut: float | None = None
    """Agent only: heard for this many seconds before the user cut it off."""
    of: float | None = None
    """Agent only: how long the whole reply would have played."""
    typed: bool = False
    approximate: bool = False
    """Rebuilt from a record's one-second headings, not measured."""
    unheard: bool = False
    """User only: they spoke and the recognizer kept no words; `text` is the
    marker the agent was given, not anything they said."""

    @property
    def end(self) -> float:
        return self.at + (self.spoken or 0.0)

    @property
    def words(self) -> int:
        return 0 if self.unheard else len(self.text.split())


@dataclass(frozen=True, slots=True)
class Event:
    at: float
    kind: str
    data: Mapping[str, Any]


class Timeline:
    def __init__(self) -> None:
        self._start: float | None = None
        self.events: list[Event] = []

    def _add(self, kind: str, data: Mapping[str, Any]) -> None:
        now = timing.now()
        if self._start is None:
            self._start = now
        self.events.append(Event(now - self._start, kind, data))

    def frame(self, payload: Mapping[str, Any]) -> None:
        """A frame sent to the page."""
        kind = str(payload.get("type", ""))
        if kind == "floor" and payload.get("state") == "speaking":
            self._add("onset", {"agent": bool(payload.get("agent"))})
        elif kind == "transcript" and payload.get("final") and str(payload.get("text", "")).strip():
            self._add(
                "said",
                {
                    "text": payload["text"],
                    "end_ms": payload.get("speech_end_ms"),
                    "unheard": bool(payload.get("unheard")),
                },
            )
        elif kind == "echo_ignored" and payload.get("stage") == "final":
            # The agent's own voice, heard back and not answered: not the user's
            # turn. Only the commit it names (its text is cut at 160 characters).
            echo = str(payload.get("text", ""))
            for index in range(len(self.events) - 1, -1, -1):
                event = self.events[index]
                if event.kind == "said":
                    if echo and str(event.data["text"]).startswith(echo):
                        del self.events[index]
                    break
        elif kind in ("greeting", "reply_start"):
            # What opens an agent turn; its voice and, for a reply, its text follow.
            self._add("open", {"text": payload.get("text", "")})
        elif kind == "reply_end":
            self._add("text", {"text": payload.get("text", "")})
        elif kind in ("audio_start", "audio_end", "truncated"):
            self._add(kind, payload)

    def typed(self, text: str) -> None:
        self._add("typed", {"text": text})

    def playback(self, active: bool) -> None:
        """The page's player went quiet: the agent's voice really ended here."""
        if not active:
            self._add("quiet", {})

    def turns(self) -> list[Turn]:
        opens = [i for i, e in enumerate(self.events) if e.kind == "open"]
        turns: list[Turn] = []
        for n, index in enumerate(opens):
            hi = opens[n + 1] if n + 1 < len(opens) else len(self.events)
            turn = self._agent(self.events[index], self.events[index + 1 : hi])
            if turn is not None:
                turns.append(turn)

        commit = -1.0
        for event in self.events:
            if event.kind == "typed":
                turns.append(Turn("you", event.at, str(event.data["text"]), typed=True))
            elif event.kind == "said":
                end_ms = event.data.get("end_ms")
                stopped = event.at - (end_ms / 1000 if isinstance(end_ms, int | float) else 0.0)
                # A sound while the agent spoke may be its own voice coming back;
                # it counts as the user's start only if the agent was then cut off.
                barged = any(
                    e.kind == "truncated" for e in self.events if commit < e.at <= event.at
                )
                onset = next(
                    (
                        e.at
                        for e in self.events
                        if e.kind == "onset"
                        and commit < e.at <= stopped
                        and (barged or not e.data["agent"])
                    ),
                    None,
                )
                at = event.at if onset is None else onset
                spoken = None if onset is None else max(0.0, stopped - onset)
                unheard = bool(event.data.get("unheard"))
                turns.append(
                    Turn("you", at, str(event.data["text"]), spoken=spoken, unheard=unheard)
                )
                commit = event.at
        return with_think_times(sorted(turns, key=lambda t: t.at))

    def _agent(self, opened: Event, window: list[Event]) -> Turn | None:
        """One agent turn from what followed its opening frame, up to the next;
        `None` for a reply that wrote nothing (cancelled before its first word)."""
        text = str(opened.data.get("text") or "") or next(
            (str(e.data.get("text") or "") for e in window if e.kind == "text"), ""
        )
        if not text.strip():
            return None
        start = next((e.at for e in window if e.kind == "audio_start"), opened.at)
        audio_end = next((e for e in window if e.kind == "audio_end"), None)
        seconds = audio_end.data.get("seconds") if audio_end is not None else None
        full = float(seconds) if isinstance(seconds, int | float) else None
        truncated = next((e for e in window if e.kind == "truncated"), None)
        played = truncated.data.get("played_ms") if truncated is not None else None
        cut = played / 1000 if isinstance(played, int | float) else None
        quiet = next(
            (e.at for e in window if e.kind == "quiet" and e.at >= start and cut is None), None
        )
        spoken = cut if cut is not None else (quiet - start if quiet is not None else full)
        return Turn("advocate", start, text.strip(), spoken=spoken, cut=cut, of=full)


def with_think_times(turns: list[Turn]) -> list[Turn]:
    """Each user turn's pause since the agent's last turn began to end."""
    out: list[Turn] = []
    last: Turn | None = None
    for turn in turns:
        if turn.who == "advocate":
            last = turn
        elif last is not None and turn.think is None:
            # A typed line that stops the agent is not talking over it; + 0.0
            # turns a rounded -0.0 into 0.0.
            gap = max(0.0, turn.at - last.end) if turn.typed else turn.at - last.end
            turn = replace(turn, think=round(gap, 1) + 0.0)
        out.append(turn)
    return out


def stats(turns: list[Turn]) -> dict[str, object]:
    """Measured, not judged: what the page shows beside the verdict and the
    judge is told to trust over its own impression of timing."""
    yours = [t for t in turns if t.who == "you"]
    theirs = [t for t in turns if t.who == "advocate"]
    thinks = [t.think for t in yours if t.think is not None and t.think >= 0]
    you_talk = sum(t.spoken or 0.0 for t in yours)
    they_talk = sum(t.spoken or 0.0 for t in theirs)
    words = sum(t.words for t in yours)
    total = you_talk + they_talk
    return {
        "length_s": round(max((t.end for t in turns), default=0.0), 1),
        "your_turns": len(yours),
        "your_words": words,
        "words_per_turn": round(words / len(yours), 1) if yours else 0,
        # Typed turns have no seconds: with nothing spoken there is no share.
        "talk_share": round(100 * you_talk / total) if you_talk and total else None,
        "think_median_s": round(statistics.median(thinks), 1) if thinks else None,
        "think_longest_s": round(max(thinks), 1) if thinks else None,
        "talk_overs": sum(1 for t in yours if t.think is not None and t.think < 0),
        "cut_offs": sum(1 for t in theirs if t.cut is not None),
        "longest_turn_s": round(max(t.spoken or 0.0 for t in yours), 1) if you_talk else None,
        "approximate": any(t.approximate for t in turns),
    }
