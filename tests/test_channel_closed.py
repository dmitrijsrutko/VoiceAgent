"""Writing to a socket that has gone is dropped, not raised: at the time limit
the recognizer's last transcript landed after the hang-up and crashed the
listening task ("Cannot call send once a close message has been sent")."""

import pytest
from starlette.websockets import WebSocketDisconnect

from voice_agent.channel import Channel

pytestmark = pytest.mark.anyio


class GoneSocket:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def send_json(self, payload: dict[str, object]) -> None:
        self.calls += 1
        raise self.error

    async def send_bytes(self, data: bytes) -> None:
        self.calls += 1
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        WebSocketDisconnect(code=1006),
        RuntimeError('Cannot call "send" once a close message has been sent.'),
    ],
)
async def test_a_gone_socket_drops_writes_instead_of_raising(error: Exception) -> None:
    socket = GoneSocket(error)
    channel = Channel(socket)  # type: ignore[arg-type]

    await channel.send_json({"type": "transcript", "text": "last words", "final": True})
    await channel.send_json({"type": "listen_error", "message": "after it"})
    await channel.send_bytes(b"\x00\x00")

    assert channel.closed
    assert socket.calls == 1, "it kept writing to a socket it knew was gone"


async def test_a_protocol_mistake_on_a_live_socket_is_still_raised() -> None:
    """Starlette raises RuntimeError for real mistakes too; only a socket that
    is actually disconnected may swallow one."""
    from starlette.websockets import WebSocketState

    socket = GoneSocket(RuntimeError('Expected ASGI message "websocket.send"'))
    socket.application_state = WebSocketState.CONNECTED  # type: ignore[attr-defined]
    channel = Channel(socket)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        await channel.send_json({"type": "ready"})
    assert not channel.closed
