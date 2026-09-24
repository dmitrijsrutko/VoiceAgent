"""Name -> recognition backend. The only place that knows which ears exist."""

import os
from collections.abc import Callable
from dataclasses import dataclass

from voice_agent.errors import ConfigError
from voice_agent.stt import assemblyai_stt, elevenlabs_stt
from voice_agent.stt.assemblyai_stt import AssemblyAISTT
from voice_agent.stt.base import STT
from voice_agent.stt.elevenlabs_stt import ElevenLabsSTT

NO_EARS = "none"
"""Not a backend: the explicit way to run the agent deaf, so typing still works
for anyone without a recognition key."""


@dataclass(frozen=True, slots=True)
class Ears:
    key: str
    languages: tuple[str, ...]
    """What it can hear, from the module's own constant rather than an
    instance: the start screen says it before a choice is made, and possibly
    without holding every key."""
    sample_rate: int
    build: Callable[[float | None], STT]


EARS: dict[str, Ears] = {
    "assemblyai": Ears(
        "ASSEMBLYAI_API_KEY",
        assemblyai_stt.LANGUAGES,
        assemblyai_stt.SAMPLE_RATE,
        lambda silence: AssemblyAISTT(silence_seconds=silence),
    ),
    "elevenlabs": Ears(
        # Scribe realtime is billed on the same key as synthesis.
        "ELEVENLABS_API_KEY",
        elevenlabs_stt.LANGUAGES,
        elevenlabs_stt.SAMPLE_RATE,
        lambda silence: ElevenLabsSTT(silence_seconds=silence),
    ),
}


def available() -> tuple[str, ...]:
    """The recognizers this deployment could actually use, in `EARS` order.
    An empty key counts as absent, as in `llm.registry.available`."""
    return tuple(name for name, e in EARS.items() if os.environ.get(e.key, "").strip())


def describe(name: str) -> dict[str, object]:
    """What a choice of ears would mean, without building one."""
    ears = EARS[name]
    return {"name": name, "languages": list(ears.languages), "sample_rate": ears.sample_rate}


def create_stt(provider: str, silence_seconds: float | None = None) -> STT | None:
    if provider == NO_EARS:
        return None
    try:
        ears = EARS[provider]
    except KeyError:
        known = ", ".join([*sorted(EARS), NO_EARS])
        raise ConfigError(f"unknown ears {provider!r}; expected one of: {known}") from None
    return ears.build(silence_seconds)
