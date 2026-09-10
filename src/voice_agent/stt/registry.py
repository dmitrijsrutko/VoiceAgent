"""Name -> recognition backend. The only place that knows which ears exist."""

from collections.abc import Callable

from voice_agent.errors import ConfigError
from voice_agent.stt.base import STT
from voice_agent.stt.elevenlabs_stt import ElevenLabsSTT

NO_EARS = "none"
"""Not a backend: the explicit way to run the agent deaf, so typing still works
for anyone without a recognition key."""

BUILDERS: dict[str, Callable[[float | None], STT]] = {
    "elevenlabs": lambda silence: ElevenLabsSTT(silence_seconds=silence),
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
