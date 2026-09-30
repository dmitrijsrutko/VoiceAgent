"""The microphone's level: dBFS per window, the voice against the quiet."""

import math

import numpy as np
import pytest

from voice_agent.level import FLOOR_DBFS, WINDOW_SECONDS, Level, dbfs

RATE = 16000
FRAME = 512  # samples: what the page sends, one VAD window


def tone(amplitude: float, samples: int = FRAME) -> bytes:
    """A 440 Hz sine at `amplitude` of full scale, as 16-bit PCM."""
    t = np.arange(samples) / RATE
    return (amplitude * 32767 * np.sin(2 * math.pi * 440 * t)).astype("<i2").tobytes()


def window(level: Level, pcm_frame: bytes, agent: bool, speaking: bool = False) -> float:
    """Feed frames until one window closes; its level."""
    while (closed := level.add(pcm_frame, agent, speaking)) is None:
        pass
    return closed


def test_a_sine_reads_at_its_rms() -> None:
    """A sine's RMS is its peak over √2: at half scale, -9.03 dBFS."""
    got = window(Level(RATE), tone(0.5), agent=False)
    assert got == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.5)


def test_silence_is_a_floor_not_minus_infinity() -> None:
    assert window(Level(RATE), b"\x00\x00" * FRAME, agent=False) == FLOOR_DBFS
    assert dbfs(0.0, 0) == FLOOR_DBFS


def test_one_level_per_window() -> None:
    level = Level(RATE)
    frames = int(RATE * WINDOW_SECONDS / FRAME) + 1
    closed = [level.add(tone(0.1), agent=True, speaking=False) for _ in range(frames * 3)]
    assert sum(c is not None for c in closed) == 3


def test_a_voice_is_summed_up_against_the_quiet_before_it() -> None:
    """The leak shows as the level during the voice over the quiet baseline."""
    level = Level(RATE)
    for _ in range(4):
        window(level, tone(0.001), agent=False)  # the room: about -63 dBFS
    for _ in range(6):
        window(level, tone(0.05), agent=True)  # the voice coming back: about -29
    summary = level.voice_ended()

    assert summary is not None
    assert summary["windows"] == 6
    assert summary["dbfs_p50"] == pytest.approx(-29.0, abs=0.5)
    assert summary["baseline_dbfs"] == pytest.approx(-63.0, abs=0.5)
    assert level.voice_ended() is None  # the next voice starts empty


def test_the_user_speaking_is_not_the_quiet_baseline() -> None:
    level = Level(RATE)
    window(level, tone(0.5), agent=False, speaking=True)
    window(level, tone(0.05), agent=True)
    summary = level.voice_ended()

    assert summary is not None and summary["baseline_dbfs"] is None


def test_a_window_never_mixes_the_voice_with_the_quiet() -> None:
    """Half a window of voice, then quiet: the voice's part is dropped rather
    than credited to the baseline by whichever frame closes the window."""
    level = Level(RATE)
    half = int(RATE * WINDOW_SECONDS / FRAME / 2)
    for _ in range(half):
        assert level.add(tone(0.5), agent=True, speaking=False) is None
    quiet = window(level, tone(0.001), agent=False)

    assert quiet == pytest.approx(-63.0, abs=0.5)
    assert level.voice_ended() is None
