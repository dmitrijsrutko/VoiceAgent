"""Name -> reasoning engine. The only place that knows which LLMs exist."""

from collections.abc import Callable

from voice_agent.errors import ConfigError
from voice_agent.llm.anthropic_provider import AnthropicLLM
from voice_agent.llm.base import LLM
from voice_agent.llm.openai_compatible import DEEPSEEK, OPENAI, OpenAICompatibleLLM

BUILDERS: dict[str, Callable[[str | None], LLM]] = {
    "deepseek": lambda model: OpenAICompatibleLLM(DEEPSEEK, model),
    "openai": lambda model: OpenAICompatibleLLM(OPENAI, model),
    "anthropic": lambda model: AnthropicLLM(model),
}


def create_llm(provider: str, model: str | None = None) -> LLM:
    try:
        build = BUILDERS[provider]
    except KeyError:
        known = ", ".join(sorted(BUILDERS))
        raise ConfigError(f"unknown provider {provider!r}; expected one of: {known}") from None
    return build(model)
