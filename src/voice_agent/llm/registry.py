"""Name -> reasoning engine, and the models a conversation may run.

The only place that knows which LLMs exist, and the only place that knows which
of their models this agent offers.
"""

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from voice_agent.errors import ConfigError
from voice_agent.llm.anthropic_provider import (
    ANTHROPIC_API_KEY,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    MODELS,
    AnthropicLLM,
)
from voice_agent.llm.base import LLM, MAX_OUTPUT_TOKENS, Effort
from voice_agent.llm.openai_compatible import DEEPSEEK, OPENAI, OpenAICompatibleLLM


@dataclass(frozen=True, slots=True)
class Engine:
    key: str
    """The environment variable it needs. Adapters call `require_env` when
    built, so this is how to ask whether one is usable without raising."""
    default_model: str
    """What it runs when nothing overrides it, so a page can say which model a
    choice means before the choice is made."""
    models: tuple[str, ...]
    """Every model this provider serves, the default among them. A model name
    means nothing to another vendor, so `check_model` holds a configured pair
    to this list rather than letting the first turn discover it."""
    default_effort: Effort | None
    """What this engine asks for when a caller names no effort — which may be
    `None`, meaning it has no such parameter to set. Not the same as `low`."""
    build: Callable[[str | None, Effort | None, int, bool], LLM]
    """`(model, effort, max_tokens, json_output)` already resolved by `create_llm`: an
    adapter is handed an instruction, never a policy."""


ENGINES: dict[str, Engine] = {
    "deepseek": Engine(
        DEEPSEEK.api_key_env,
        DEEPSEEK.default_model,
        DEEPSEEK.models,
        DEEPSEEK.default_effort,
        lambda m, e, t, j: OpenAICompatibleLLM(DEEPSEEK, m, e, max_tokens=t, json_output=j),
    ),
    "openai": Engine(
        OPENAI.api_key_env,
        OPENAI.default_model,
        OPENAI.models,
        OPENAI.default_effort,
        lambda m, e, t, j: OpenAICompatibleLLM(OPENAI, m, e, max_tokens=t, json_output=j),
    ),
    "anthropic": Engine(
        ANTHROPIC_API_KEY,
        DEFAULT_MODEL,
        MODELS,
        DEFAULT_EFFORT,
        # No structured-output format sent (`output_config.format` would need the
        # ruling's schema): a caller that asks relies on its prompt and retry.
        lambda m, e, t, j: AnthropicLLM(m, e, max_tokens=t),
    ),
}


@dataclass(frozen=True, slots=True)
class Choice:
    """One model a conversation may run, and how hard it is asked to think."""

    name: str
    """The id `?llm=` carries and the conversation is pinned to. Stable, because
    a conversation's link names it."""
    provider: str
    model: str
    title: str
    """What the page shows: the model's own name, and nothing claimed about it.
    A title is not a promise about speed or price."""
    effort: Effort | None = None
    """What to ask for, or `None` to take the engine's own default. An option
    that does not set an effort is not claiming the model has none — only the
    adapter can know that, and it asks."""
    hint: str = ""
    """The page's grey line under the title: what sets this option apart from
    its neighbours (the tier, or how hard it thinks), like a voice's hint. The
    tier as the vendor ranks it, not a measurement: Chapter 21 has the numbers."""


CHOICES: tuple[Choice, ...] = (
    Choice("haiku-4-5", "anthropic", "claude-haiku-4-5", "Haiku 4.5", hint="fastest"),
    Choice("sonnet-5-5", "anthropic", "claude-sonnet-5-5", "Sonnet 5.5", "low", "balanced"),
    Choice("opus-5-5", "anthropic", "claude-opus-5-5", "Opus 5.5", "medium", "smartest"),
    Choice("deepseek-off", "deepseek", "deepseek-flash", "V4.1 Flash", "off", "fastest"),
    Choice("deepseek-low", "deepseek", "deepseek-flash", "V4.1 Flash", "low", "balanced"),
    Choice("deepseek-high", "deepseek", "deepseek-flash", "V4.1 Flash", "high", "smartest"),
)
"""The menu, fastest first: `offered()` falls back to the first, which should be
the cheapest. The page draws each vendor's row in this order too, so fastest is
on the left and smartest on the right, in the same three words for every vendor.

One rule makes the columns comparable across vendors: **fastest** does not think
(DeepSeek switched off; Haiku 4.5 has no thinking), **balanced** thinks the least
it can (`low` on both), **smartest** thinks as its vendor sets by default
(DeepSeek `high`; Opus 5.5 `medium`). Claude's tiers are different models;
DeepSeek's are one model at different efforts, because that is what it offers.

Haiku 4.5 is the reason a choice may not set an effort rather than set `None`
to mean "none": it is the model that rejects the parameter, and only the adapter
can discover that, by asking. Its `Choice.effort` is `None`.

A title names the model **without its vendor**, because the page groups the
options under the vendor's own name — "Claude" over three, "DeepSeek" over three.
Repeating it in every option made each one wide enough that a group wrapped,
which is the one thing the grouping exists to avoid.
"""

DEFAULT_CHOICE = "deepseek-off"
"""What a conversation runs unless it picks another: the fastest of the top row,
so the page's default is its leftmost option, as in every other group."""

BY_NAME: dict[str, Choice] = {choice.name: choice for choice in CHOICES}


def check_model(provider: str, model: str | None) -> None:
    """Refuse a model that belongs to another provider, naming the ones that fit.

    Nothing to check for an unknown provider — `create_llm` is what names the
    providers that exist — or for no model at all, which means the default.
    """
    if model is None or provider not in ENGINES:
        return
    known = ENGINES[provider].models
    if model not in known:
        raise ConfigError(
            f"{provider} does not serve {model!r}; it serves: {', '.join(known)}. "
            f"Add it to that provider's MODELS when the vendor ships a new one."
        )


def available() -> tuple[str, ...]:
    """The engines this deployment could actually use, in `ENGINES` order. A
    key set but empty counts as absent: `.env.example` ships every name with
    nothing after the `=`."""
    return tuple(name for name, e in ENGINES.items() if os.environ.get(e.key, "").strip())


def offered() -> tuple[Choice, ...]:
    """The menu this deployment can actually run, in `CHOICES` order.

    Only choices whose provider holds a key, since every adapter calls
    `require_env` when built. With no key for anything the default is offered
    anyway, so a misconfigured deployment fails on the first call naming the
    missing variable rather than with an empty page.
    """
    keys = set(available())
    return tuple(choice for choice in CHOICES if choice.provider in keys) or (CHOICES[0],)


def default_choice(have: Iterable[Choice]) -> str:
    """`DEFAULT_CHOICE` where this deployment can run it, else the first it can.

    Over the sequence, not a set of names: "the first one offered" has to mean
    the first in menu order, and a set has no order to mean.
    """
    choices = tuple(have)
    for choice in choices:
        if choice.name == DEFAULT_CHOICE:
            return choice.name
    return choices[0].name


def create_llm(
    provider: str,
    model: str | None = None,
    effort: Effort | None = None,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    json_output: bool = False,
) -> LLM:
    """Build one engine, resolving `None` to the engine's own defaults.

    This is the only place the policy lives: a caller says what it wants and
    nothing else, and an adapter is handed the resolved instruction. Every
    caller — the pool, the bench, the inner voice — comes through here, so
    "no preference" means one thing rather than one thing per provider.
    """
    try:
        engine = ENGINES[provider]
    except KeyError:
        known = ", ".join(sorted(ENGINES))
        raise ConfigError(f"unknown provider {provider!r}; expected one of: {known}") from None
    check_model(provider, model)
    effort = engine.default_effort if effort is None else effort
    return engine.build(model, effort, max_tokens, json_output)
