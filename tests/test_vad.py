"""The VAD on real speech: a checked-in recording with a known pause.

`fixtures/pause.wav` is one synthesized phrase (ElevenLabs Flash, trimmed),
laid out as 320 ms silence · 2001 ms speech · 704 ms pause · the same speech ·
960 ms silence. The labels come from how it was built, not from a detector.
"""

import wave
from pathlib import Path

import numpy as np

from voice_agent.floor import Floor
from voice_agent.vad import VAD, WINDOW_BYTES, WINDOW_MS

FIXTURE = Path(__file__).parent / "fixtures" / "pause.wav"
SPEECH_MS = ((320, 2321), (3025, 5026))
TOLERANCE_MS = 2 * WINDOW_MS


def pcm() -> bytes:
    with wave.open(str(FIXTURE)) as clip:
        assert (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) == (16000, 1, 2)
        return bytes(clip.readframes(clip.getnframes()))


def test_the_floor_follows_the_recording_to_within_two_windows() -> None:
    floor = Floor(WINDOW_MS)
    changes = [c for p in VAD().probabilities(pcm()) for c in floor.push(p)]

    starts = [c.at_ms for c in changes if c.state == "speaking"]
    stops = [c.at_ms for c in changes if c.state == "micro_pause"]
    assert len(starts) == len(stops) == 2, [(c.state, c.at_ms) for c in changes]
    for (start, stop), heard_start, heard_stop in zip(SPEECH_MS, starts, stops, strict=True):
        assert abs(heard_start - start) <= TOLERANCE_MS
        assert abs(heard_stop - stop) <= TOLERANCE_MS
    # The 704 ms gap is a pause, not the end of the turn.
    assert "yielded" not in [c.state for c in changes]


def test_frames_of_any_size_give_the_same_answer() -> None:
    """The page sends one window a frame, but nothing guarantees it: a partial
    window waits for the next frame rather than being scored short."""
    audio = pcm()[: 40 * WINDOW_BYTES]
    whole = VAD().probabilities(audio)

    vad = VAD()
    pieces = [p for i in range(0, len(audio), 700) for p in vad.probabilities(audio[i : i + 700])]

    assert np.allclose(whole, pieces)


def test_silence_and_noise_are_not_speech() -> None:
    rng = np.random.default_rng(0)
    noise = (rng.normal(0, 300, 16000)).astype("<i2").tobytes()  # 1 s of quiet hiss
    silence = bytes(32000)

    assert max(VAD().probabilities(silence)) < 0.35
    assert max(VAD().probabilities(noise)) < 0.35
