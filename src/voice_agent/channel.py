"""The one socket a conversation writes to, and the frames shared by every
producer of audio on it."""

import asyncio
from typing import TYPE_CHECKING

from fastapi import WebSocket

from voice_agent.tts.base import MEDIA_TYPE, SAMPLE_RATE

if TYPE_CHECKING:
    from voice_agent.record import Record


class Channel:
    """Serializes writes to one WebSocket.

    Two producers write to the same socket: the receive loop's replies, and the
    microphone's transcript task. Audio no longer needs its binary frame kept
    adjacent to a JSON one — speech is a run of binary frames bracketed by
    `audio_start` and `audio_end`, and a JSON message landing inside that run
    is harmless — so each write is locked on its own.
    """

    def __init__(self, websocket: WebSocket, record: "Record | None" = None) -> None:
        self._websocket = websocket
        self._lock = asyncio.Lock()
        self._record = record
        """Where the conversation is written down, if it is. Recording here
        rather than at each call site is what keeps the file from drifting from
        what the browser was actually sent."""

    async def send_json(self, payload: dict[str, object]) -> None:
        async with self._lock:
            await self._websocket.send_json(payload)
            if self._record is not None:
                self._record.frame(payload)

    async def send_bytes(self, data: bytes) -> None:
        async with self._lock:
            await self._websocket.send_bytes(data)
            if self._record is not None:
                # Counted, never written: speech is biometric data.
                self._record.audio(len(data))


def audio_start() -> dict[str, object]:
    return {"type": "audio_start", "media_type": MEDIA_TYPE, "sample_rate": SAMPLE_RATE}
