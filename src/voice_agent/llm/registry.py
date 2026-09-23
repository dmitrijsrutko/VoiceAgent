"""Name -> reasoning engine. The only place that knows which LLMs exist."""

import os
from collections.abc import Callable

from voice_agent.errors import ConfigError
from voice_agent.llm.anthropic_provider import ANTHROPIC_API_KEY, DEFAULT_MODEL, AnthropicLLM
from voice_agent.llm.base import LLM
from voice_agent.llm.openai_compatible import DEEPSEEK, OPENAI, OpenAICompatibleLLM

BUILDERS: dict[str, Callable[[str | None], LLM]] = {
    "deepseek": lambda model: OpenAICompatibleLLM(DEEPSEEK, model),
    "openai": lambda model: OpenAICompatibleLLM(OPENAI, model),
    "anthropic": lambda model: AnthropicLLM(model),
}

KEYS: dict[str, str] = {
    "deepseek": DEEPSEEK.api_key_env,
    "openai": OPENAI.api_key_env,
    "anthropic": ANTHROPIC_API_KEY,
}
"""What each backend needs in the environment. Adapters call `require_env` when
built, so this is how to ask whether one is usable without raising."""

DEFAULT_MODELS: dict[str, str] = {
    "deepseek": DEEPSEEK.default_model,
    "openai": OPENAI.default_model,
    "anthropic": DEFAULT_MODEL,
}
"""What each backend runs when nothing overrides it — so a page can say which
model a choice means before the choice is made."""


def available() -> tuple[str, ...]:
    """The engines this deployment could actually use, in `BUILDERS` order.

    A key that is set but empty counts as absent: `.env.example` ships every
    name with nothing after the `=`, so treating "" as configured would offer
    every provider on a machine that has none of them.
    """
    return tuple(name for name in BUILDERS if os.environ.get(KEYS[name], "").strip())


def create_llm(provider: str, model: str | None = None) -> LLM:
    try:
        build = BUILDERS[provider]
    except KeyError:
        known = ", ".join(sorted(BUILDERS))
        raise ConfigError(f"unknown provider {provider!r}; expected one of: {known}") from None
    return build(model)
