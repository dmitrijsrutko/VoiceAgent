"""Name -> synthesis backend. The only place that knows which voices exist."""

from collections.abc import Callable
from dataclasses import dataclass

from voice_agent.errors import ConfigError
from voice_agent.tts.base import TTS
from voice_agent.tts.elevenlabs_tts import ElevenLabsTTS

NO_VOICE = "none"
"""Not a backend: the explicit way to run the agent silently, so the project
still works for anyone without a synthesis key."""

BUILDERS: dict[str, Callable[[str | None, str | None], TTS]] = {
    "elevenlabs": lambda voice, model: ElevenLabsTTS(voice, model),
}

DEPRECATED = frozenset(
    {"eleven_turbo_v2_5", "eleven_turbo_v2", "eleven_monolingual_v1", "eleven_multilingual_v1"}
)
"""Refused by `create_tts`. Per elevenlabs.io/docs/overview/models, checked
2026-09-26: Turbo is superseded by Flash ("we recommend using the Flash models
over Turbo models in all use cases"); the v1 models were removed 2026-07-09."""


@dataclass(frozen=True, slots=True)
class Option:
    """One voice model the page may offer: which backend, which of its models,
    and what the page calls it. A builder per option, not a model id per
    provider, so a model on another endpoint can be another adapter."""

    name: str
    provider: str
    model: str
    title: str
    hint: str


MENU: tuple[Option, ...] = (
    Option(
        "multilingual-v2",
        "elevenlabs",
        "eleven_multilingual_v2",
        "Multilingual v2",
        "most expressive",
    ),
    Option("flash-v2.5", "elevenlabs", "eleven_flash_v2_5", "Flash v2.5", "fastest"),
)

DEFAULT_CHOICE = "multilingual-v2"
"""What a conversation speaks with unless it picks another. Not the fastest
to first audio (that is Flash): the one that sounds most alive."""

BY_NAME: dict[str, Option] = {option.name: option for option in MENU}


def offered(provider: str) -> tuple[Option, ...]:
    """The configured backend's voice models, in `MENU` order. No key check:
    they share the backend's one key, so either all can speak or none can."""
    return tuple(option for option in MENU if option.provider == provider)


def default_choice(have: tuple[Option, ...]) -> str | None:
    """`DEFAULT_CHOICE` where it is offered, else the first that is."""
    names = [option.name for option in have]
    return DEFAULT_CHOICE if DEFAULT_CHOICE in names else next(iter(names), None)


def create_tts(provider: str, voice: str | None = None, model: str | None = None) -> TTS | None:
    if provider == NO_VOICE:
        return None
    if model in DEPRECATED:
        raise ConfigError(f"voice model {model!r} is deprecated by its vendor")
    try:
        build = BUILDERS[provider]
    except KeyError:
        known = ", ".join([*sorted(BUILDERS), NO_VOICE])
        raise ConfigError(f"unknown voice {provider!r}; expected one of: {known}") from None
    return build(voice, model)
