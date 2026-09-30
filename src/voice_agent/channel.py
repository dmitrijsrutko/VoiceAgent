"""The one socket a conversation writes to, and the frames shared by every
producer of audio on it."""

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from voice_agent import trace
from voice_agent.tts.base import MEDIA_TYPE, SAMPLE_RATE

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from voice_agent.record import Record
    from voice_agent.timeline import Timeline

logger = logging.getLogger(__name__)

GONE = (WebSocketDisconnect, RuntimeError, OSError)
"""What writing to a socket that has gone raises: the peer dropped it
(`WebSocketDisconnect`), or we already closed it (starlette's RuntimeError,
"Cannot call send once a close message has been sent"). A RuntimeError on a
socket still connected is a real mistake (sending before accepting, a wrong
message type) and is raised, not taken for a hang-up."""


UNTRACED = frozenset({"delta", "transcript", "floor", "marks"})
"""Frames in the trace another way — tokens (`llm.reply`), partials and commits
(`stt.*`), the floor (`floor`) — or bulky: `marks` repeats every character's
timing, chunk by chunk."""

TRACE_FIELDS = frozenset({"type", "kind", "at", "mono", "trace", "span", "parent"})

PAGE_TEXT_LIMIT = 500
"""A client-supplied string is cut to this in the trace."""


def page_event(direction: str, payload: Mapping[str, Any]) -> None:
    """One frame to or from the page, in the trace at its millisecond: the
    record's headings are whole seconds, and a race lives in the gap."""
    kind = str(payload.get("type", ""))
    if direction == "out" and kind in UNTRACED:
        return
    attrs: dict[str, Any] = {"type": kind}
    for key, value in payload.items():
        if key in TRACE_FIELDS:
            continue  # the trace's own: a frame must not relabel its line
        if direction == "in":
            # From the client: scalars only, strings cut, so a page cannot
            # fill the trace.
            if isinstance(value, str):
                attrs[key] = value[:PAGE_TEXT_LIMIT]
            elif isinstance(value, int | float | bool) or value is None:
                attrs[key] = value
        elif key == "history" and isinstance(value, list):
            # The whole conversation, on every connect: its size is enough.
            attrs["history_messages"] = len(value)
        else:
            attrs[key] = value
    trace.event(f"page.{direction}", attrs)


class Channel:
    """Serializes writes to one WebSocket.

    Two producers write to the same socket: the receive loop's replies, and the
    microphone's transcript task. Audio no longer needs its binary frame kept
    adjacent to a JSON one — speech is a run of binary frames bracketed by
    `audio_start` and `audio_end`, and a JSON message landing inside that run
    is harmless — so each write is locked on its own.
    """

    def __init__(
        self,
        websocket: WebSocket,
        record: "Record | None" = None,
        timeline: "Timeline | None" = None,
        frames: list[dict[str, object]] | None = None,
    ) -> None:
        self._websocket = websocket
        self._lock = asyncio.Lock()
        self.timeline = timeline
        """The round's timings, for the judge; tapped here for the same reason
        as the record."""
        self._record = record
        """Where the conversation is written down, if it is. Recording here
        rather than at each call site is what keeps the file from drifting from
        what the browser was actually sent."""
        self._frames = frames
        """Where what the page is sent is kept, for redrawing it on a reload."""
        self.closed = False
        """The socket is gone. Every write after that is dropped, not raised:
        the recognizer's last transcript, a thought, a closing frame all land
        after a time-limit hang-up, and none of them may crash anything (live:
        "listening crashed", then an unretrieved task exception)."""

    async def send_json(self, payload: dict[str, object]) -> None:
        async with self._lock:
            # Kept even when the socket has gone: a reply is in the history
            # before its `reply_end` is sent, and the next page load must not
            # be missing what a closed tab never received.
            if self._frames is not None and shown(payload):
                self._frames.append(dict(payload))
            if not await self._write(self._websocket.send_json(payload)):
                return
            page_event("out", payload)
            if self.timeline is not None:
                self.timeline.frame(payload)
            if self._record is not None:
                self._record.frame(payload)

    def typed(self, text: str) -> None:
        """A typed turn, kept with the frames: the page drew it itself, and the
        server never echoes it back."""
        if self._frames is not None:
            self._frames.append({"type": "said", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        async with self._lock:
            if not await self._write(self._websocket.send_bytes(data)):
                return
            if self._record is not None:
                # Counted, never written: speech is biometric data.
                self._record.audio(len(data))

    async def _write(self, sending: "Coroutine[object, object, None]") -> bool:
        """Send, unless the socket has gone. Whether it was sent."""
        if self.closed:
            sending.close()  # never awaited: no "coroutine was never awaited" warning
            return False
        try:
            await sending
        except GONE as exc:
            state = getattr(self._websocket, "application_state", WebSocketState.DISCONNECTED)
            if isinstance(exc, RuntimeError) and state != WebSocketState.DISCONNECTED:
                raise
            self.closed = True
            logger.info("socket gone (%s); further frames are dropped", type(exc).__name__)
            return False
        return True


UNSHOWN = frozenset({"ready", "delta", "marks", "floor", "transcript_dropped", "mic_level"})
"""Frames a reload does not redraw: `ready` carries the whole history, text
arrives whole in `reply_end`, and the rest paint something only while live."""


def shown(payload: dict[str, object]) -> bool:
    """Whether a frame is part of what a reload redraws. A transcript only once
    committed: partials are one frame per recognizer update."""
    kind = payload.get("type")
    return kind not in UNSHOWN and (kind != "transcript" or bool(payload.get("final")))


def audio_start() -> dict[str, object]:
    return {"type": "audio_start", "media_type": MEDIA_TYPE, "sample_rate": SAMPLE_RATE}
