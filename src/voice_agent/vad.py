"""Voice activity detection on the microphone audio: is this 32 ms speech?

Silero VAD v5, run through onnxruntime rather than torch — the same model its
own `OnnxWrapper` runs, at a fraction of the install. The recognizer can say
*what* was said, but only ~1 s late; this says *that* someone is speaking,
within a window.
"""

from functools import cache
from pathlib import Path

import numpy as np
import onnxruntime  # type: ignore[import-untyped]  # ships no stubs or py.typed

SAMPLE_RATE = 16000
"""The only rate handled. Both recognizers are opened at it, so the page
already captures at it and nothing resamples."""

WINDOW_SAMPLES = 512
"""What the model accepts at 16 kHz: 32 ms. Not a tuning choice."""

WINDOW_BYTES = WINDOW_SAMPLES * 2
"""PCM16 mono."""

WINDOW_MS = WINDOW_SAMPLES * 1000 // SAMPLE_RATE

CONTEXT_SAMPLES = 64
"""v5 expects the tail of the previous window prepended to each one; without
it the probabilities come out noticeably lower on the same audio."""

MODEL_PATH = Path(__file__).parent / "models" / "silero_vad.onnx"


@cache
def load() -> onnxruntime.InferenceSession:
    """Loaded once per process (~50 ms) and shared: `run` is thread-safe, and the
    recurrent state lives in each `VAD`, not in the session."""
    options = onnxruntime.SessionOptions()
    # One window at a time: more threads only add scheduling overhead.
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    return onnxruntime.InferenceSession(
        str(MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"]
    )


class VAD:
    """One conversation's detector. Audio arrives in frames of any size; it is
    cut into whole windows, and a partial window waits for the next frame."""

    def __init__(self) -> None:
        self._session = load()
        self._pending = bytearray()
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def probabilities(self, pcm: bytes) -> list[float]:
        """Speech probability for each whole window completed by `pcm`, in order.
        CPU-bound (~0.1-0.3 ms a window): call it off the event loop."""
        self._pending += pcm
        whole = len(self._pending) // WINDOW_BYTES * WINDOW_BYTES
        if not whole:
            return []
        samples = np.frombuffer(bytes(self._pending[:whole]), dtype="<i2")
        del self._pending[:whole]
        scaled = samples.astype(np.float32) / 32768.0
        return [self._window(w) for w in scaled.reshape(-1, WINDOW_SAMPLES)]

    def _window(self, window: np.ndarray) -> float:
        x = np.concatenate([self._context, window[np.newaxis, :]], axis=1)
        output, self._state = self._session.run(
            None,
            {"input": x, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)},
        )
        self._context = x[:, -CONTEXT_SAMPLES:]
        return float(output[0][0])
