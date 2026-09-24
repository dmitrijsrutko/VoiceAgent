"""Name -> reasoning engine. The only place that knows which LLMs exist."""

import os
from collections.abc import Callable
from dataclasses import dataclass

from voice_agent.errors import ConfigError
from voice_agent.llm.anthropic_provider import ANTHROPIC_API_KEY, DEFAULT_MODEL, AnthropicLLM
from voice_agent.llm.base import LLM
from voice_agent.llm.openai_compatible import DEEPSEEK, OPENAI, OpenAICompatibleLLM


@dataclass(frozen=True, slots=True)
class Engine:
    key: str
    """The environment variable it needs. Adapters call `require_env` when
    built, so this is how to ask whether one is usable without raising."""
    default_model: str
    """What it runs when nothing overrides it, so a page can say which model a
    choice means before the choice is made."""
    build: Callable[[str | None], LLM]


ENGINES: dict[str, Engine] = {
    "deepseek": Engine(
        DEEPSEEK.api_key_env, DEEPSEEK.default_model, lambda m: OpenAICompatibleLLM(DEEPSEEK, m)
    ),
    "openai": Engine(
        OPENAI.api_key_env, OPENAI.default_model, lambda m: OpenAICompatibleLLM(OPENAI, m)
    ),
    "anthropic": Engine(ANTHROPIC_API_KEY, DEFAULT_MODEL, AnthropicLLM),
}


def available() -> tuple[str, ...]:
    """The engines this deployment could actually use, in `ENGINES` order. A
    key set but empty counts as absent: `.env.example` ships every name with
    nothing after the `=`."""
    return tuple(name for name, e in ENGINES.items() if os.environ.get(e.key, "").strip())


def create_llm(provider: str, model: str | None = None) -> LLM:
    try:
        engine = ENGINES[provider]
    except KeyError:
        known = ", ".join(sorted(ENGINES))
        raise ConfigError(f"unknown provider {provider!r}; expected one of: {known}") from None
    return engine.build(model)
