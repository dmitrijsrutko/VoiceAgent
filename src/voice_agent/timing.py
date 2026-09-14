"""Stage timings, measured on one monotonic clock."""

import time


def elapsed_ms(start: float, end: float | None = None) -> int:
    return round(((time.perf_counter() if end is None else end) - start) * 1000)
