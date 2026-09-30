"""How loud the microphone is, while the agent speaks and while nobody does.

A number per window, never the audio: whether the agent's voice leaks into the
microphone shows as the level during its voice against the quiet baseline,
without keeping a sample of what anyone said (AGENTS.md §10). Input is the
page's PCM: 16-bit little-endian, mono.
"""

import math
import statistics
from collections import deque
from typing import Any

import numpy as np

WINDOW_SECONDS = 0.25
"""One level per this much audio: four a second of agent speech in the trace."""

BASELINE_SECONDS = 10.0
"""The quiet baseline is the median of this much recent quiet."""

FLOOR_DBFS = -90.0
"""Digital silence has no level; it is reported as this, not -inf."""


def dbfs(sum_squares: float, samples: int) -> float:
    """RMS of int16 samples, in dB below full scale."""
    if samples == 0 or sum_squares <= 0:
        return FLOOR_DBFS
    rms = math.sqrt(sum_squares / samples) / 32768
    return max(FLOOR_DBFS, 20 * math.log10(rms))


class Level:
    """The microphone's level in windows, sorted by whether the agent was
    audible when each closed."""

    def __init__(self, sample_rate: int) -> None:
        self._window = max(1, int(sample_rate * WINDOW_SECONDS))
        self._sum = 0.0
        self._samples = 0
        self._agent = False
        """Which side the open window is on."""
        self._during: list[float] = []
        self._quiet: deque[float] = deque(maxlen=int(BASELINE_SECONDS / WINDOW_SECONDS))

    def add(self, pcm: bytes, agent: bool, speaking: bool) -> float | None:
        """Take a frame. Returns the level when it closes a window, else None.

        A window counts towards the voice when `agent`, towards the baseline
        when neither the agent nor the user (`speaking`) is heard."""
        if agent != self._agent:
            # A window is one side or the other: mixed, one loud frame of the
            # voice outweighs a quarter-second of quiet.
            self._agent, self._sum, self._samples = agent, 0.0, 0
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float64)
        self._sum += float(np.dot(samples, samples))
        self._samples += samples.size
        if self._samples < self._window:
            return None
        level = dbfs(self._sum, self._samples)
        self._sum, self._samples = 0.0, 0
        if agent:
            self._during.append(level)
        elif not speaking:
            self._quiet.append(level)
        return level

    def voice_ended(self) -> dict[str, Any] | None:
        """The level while the voice that just stopped was audible, beside the
        baseline; None if no window closed during it. Starts the next voice."""
        during, self._during = self._during, []
        if not during:
            return None
        ranked = sorted(during)
        return {
            "dbfs_p50": round(statistics.median(ranked), 1),
            "dbfs_p95": round(ranked[min(len(ranked) - 1, int(0.95 * len(ranked)))], 1),
            "dbfs_max": round(ranked[-1], 1),
            "baseline_dbfs": round(statistics.median(self._quiet), 1) if self._quiet else None,
            "windows": len(ranked),
        }
