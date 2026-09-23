"""Name -> recognition backend. The only place that knows which ears exist."""

import os
from collections.abc import Callable

from voice_agent.errors import ConfigError
from voice_agent.stt import assemblyai_stt, elevenlabs_stt
from voice_agent.stt.assemblyai_stt import AssemblyAISTT
from voice_agent.stt.base import STT
from voice_agent.stt.elevenlabs_stt import ElevenLabsSTT

NO_EARS = "none"
"""Not a backend: the explicit way to run the agent deaf, so typing still works
for anyone without a recognition key."""

BUILDERS: dict[str, Callable[[float | None], STT]] = {
    "assemblyai": lambda silence: AssemblyAISTT(silence_seconds=silence),
    "elevenlabs": lambda silence: ElevenLabsSTT(silence_seconds=silence),
}

KEYS: dict[str, str] = {
    "assemblyai": "ASSEMBLYAI_API_KEY",
    # Scribe realtime is billed on the same key as synthesis.
    "elevenlabs": "ELEVENLABS_API_KEY",
}
"""What each backend needs before it can be built. Both constructors call
`require_env`, so this is the only way to ask whether one is usable without
raising."""

LANGUAGES: dict[str, tuple[str, ...]] = {
    "assemblyai": assemblyai_stt.LANGUAGES,
    "elevenlabs": elevenlabs_stt.LANGUAGES,
}
"""What each backend can hear, read from the modules' own constants.

Deliberately not `STT.languages`, which is the same tuple but only reachable
through an instance — and an instance needs a key. A page offering a choice of
ears has to say what each one hears *before* one is chosen and possibly without
holding every key, and this is what makes that answerable."""

SAMPLE_RATES: dict[str, int] = {
    "assemblyai": assemblyai_stt.SAMPLE_RATE,
    "elevenlabs": elevenlabs_stt.SAMPLE_RATE,
}


def available() -> tuple[str, ...]:
    """The recognizers this deployment could actually use, in `BUILDERS` order.
    An empty key counts as absent, as in `llm.registry.available`."""
    return tuple(name for name in BUILDERS if os.environ.get(KEYS[name], "").strip())


def describe(name: str) -> dict[str, object]:
    """What a choice of ears would mean, without building one."""
    return {
        "name": name,
        "languages": list(LANGUAGES[name]),
        "sample_rate": SAMPLE_RATES[name],
    }


def create_stt(provider: str, silence_seconds: float | None = None) -> STT | None:
    if provider == NO_EARS:
        return None
    try:
        build = BUILDERS[provider]
    except KeyError:
        known = ", ".join([*sorted(BUILDERS), NO_EARS])
        raise ConfigError(f"unknown ears {provider!r}; expected one of: {known}") from None
    return build(silence_seconds)
