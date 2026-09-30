"""Name -> synthesis backend. The only place that knows which voices exist."""

from collections.abc import Callable
from dataclasses import dataclass

from voice_agent.errors import ConfigError
from voice_agent.tts.base import TTS
from voice_agent.tts.elevenlabs_dialogue_tts import ElevenLabsDialogueTTS
from voice_agent.tts.elevenlabs_tts import ElevenLabsTTS

NO_VOICE = "none"
"""Not a backend: the explicit way to run the agent silently, so the project
still works for anyone without a synthesis key."""

BUILDERS: dict[str, Callable[[str | None, str | None], TTS]] = {
    "elevenlabs": lambda voice, model: ElevenLabsTTS(voice, model),
}
"""What `create_tts` — the provider-level path `--tts` and the environment use —
can build. The menu is not limited to these: an `Option` names its own adapter,
because two of a provider's models can live on two different endpoints."""

DEPRECATED = frozenset(
    {"eleven_turbo_v2_5", "eleven_turbo_v2", "eleven_monolingual_v1", "eleven_multilingual_v1"}
)
"""Refused by `create_tts`. Per elevenlabs.io/docs/overview/models, checked
2026-09-26: Turbo is superseded by Flash ("we recommend using the Flash models
over Turbo models in all use cases"); the v1 models were removed 2026-07-09.
`eleven_multilingual_v2` is not here: its vendor has not deprecated it, this
project's menu simply no longer offers it."""


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
    build: Callable[[str | None, str | None], TTS]


MENU: tuple[Option, ...] = (
    Option(
        "v4-turbo",
        "elevenlabs",
        "eleven_v4_turbo",
        "v4 Turbo",
        "most emotive, realtime",
        build=ElevenLabsDialogueTTS,
    ),
    Option(
        "flash-v2.5",
        "elevenlabs",
        "eleven_flash_v2_5",
        "Flash v2.5",
        "legacy, fastest",
        build=ElevenLabsTTS,
    ),
)
"""The default first, as in every other group, because the page draws the default
leftmost. That used to mean the fastest; it now means the most expressive, with
the faster model kept beside it as legacy — v4 Turbo speaks over the dialogue
socket, where Flash v2.5 has no counterpart, so the two are also two protocols.
"""

DEFAULT_CHOICE = "v4-turbo"
"""What a conversation speaks with unless it picks another: Eleven v4 Turbo,
ElevenLabs' real-time model for agents, and the leftmost option on the page."""

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
