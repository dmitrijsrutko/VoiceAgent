"""Closing a stream when whoever was reading it stops.

One helper with three callers — a turn, an unprompted decision, and the tracing
wrapper around every reasoning call — because all three read a provider's
generator and all three can stop early.
"""

import contextlib
from collections.abc import AsyncIterator


@contextlib.asynccontextmanager
async def closing[T](stream: AsyncIterator[T]) -> AsyncIterator[AsyncIterator[T]]:
    """Close a provider's stream when the reader stops, however it stops.

    Cancelling a turn does not close the generator it was reading: the
    cancellation usually lands in a socket write *between* fragments, which
    leaves the generator suspended at its `yield` — and the provider's HTTP
    stream open, and billed, until garbage collection gets round to it.

    Nor does `async for` close what it iterates, which is the same bug one
    layer up: a wrapper that loops over a provider's generator and yields
    onward leaves the provider suspended when the wrapper itself is closed.
    That is why the tracing wrapper uses this too, and why it has a test.
    """
    try:
        yield stream
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            await aclose()
