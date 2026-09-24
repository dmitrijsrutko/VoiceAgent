"""One clock for the whole pipeline.

Read through `timing.now()` (the module attribute, not an imported name) rather
than `time.perf_counter()` directly, so that `using` can swap it: the tapes run
a conversation on a virtual event loop, and every timer, deadline and
measurement then moves together. Not `loop.time()` in production: under uvloop
it has millisecond resolution and a base of its own.
"""

import contextlib
import time
from collections.abc import Callable, Iterator

now: Callable[[], float] = time.perf_counter


@contextlib.contextmanager
def using(clock: Callable[[], float]) -> Iterator[None]:
    global now
    previous, now = now, clock
    try:
        yield
    finally:
        now = previous


def elapsed_ms(start: float, end: float | None = None) -> int:
    return round(((now() if end is None else end) - start) * 1000)
