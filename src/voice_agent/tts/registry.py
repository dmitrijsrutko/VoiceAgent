"""Name -> synthesis backend. The only place that knows which voices exist."""

from collections.abc import Callable

from voice_agent.errors import ConfigError
from voice_agent.tts.base import TTS
from voice_agent.tts.elevenlabs_tts import ElevenLabsTTS
from voice_agent.tts.openai_tts import OpenAITTS

NO_VOICE = "none"
"""Not a backend: the explicit way to run the agent silently, so the project
still works for anyone without a synthesis key."""

BUILDERS: dict[str, Callable[[str | None], TTS]] = {
    "elevenlabs": lambda voice: ElevenLabsTTS(voice),
    "openai": lambda voice: OpenAITTS(voice),
}


def create_tts(provider: str, voice: str | None = None) -> TTS | None:
    if provider == NO_VOICE:
        return None
    try:
        build = BUILDERS[provider]
    except KeyError:
        known = ", ".join([*sorted(BUILDERS), NO_VOICE])
        raise ConfigError(f"unknown voice {provider!r}; expected one of: {known}") from None
    return build(voice)
