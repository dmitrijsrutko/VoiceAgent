"""Anthropic, whose API differs from the OpenAI wire format in two ways.

The system prompt is a top-level parameter rather than the first message, and
the SDK exposes streaming as an async context manager with a text-only view of
the event stream. Both are handled here so nothing above this module notices.
"""

from collections.abc import AsyncIterator, Sequence

from anthropic import AnthropicError, AsyncAnthropic
from anthropic.types import MessageParam, OutputConfigParam, TextBlockParam

from voice_agent.config import require_env
from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import MAX_OUTPUT_TOKENS, Warmth

DEFAULT_MODEL = "claude-opus-5"

EFFORT: OutputConfigParam = {"effort": "low"}
"""Thinking is on by default on this model family. Reasoning before the first
token is exactly what a spoken conversation cannot afford, and low effort is
the supported way to shorten it — disabling thinking outright is documented to
cause the model to narrate tool calls and leak reasoning tags into the reply."""


def cacheable(system: str) -> list[TextBlockParam]:
    """Mark the system prompt as a cache breakpoint.

    Unlike the OpenAI-compatible backends, Anthropic caches only what is
    explicitly marked. Without this the warming call would prefill a prompt
    that the real call could not read back, which is worse than not warming at
    all: the cost with none of the benefit.
    """
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def to_anthropic_messages(messages: Sequence[Message]) -> list[MessageParam]:
    """The conversation only. The system prompt is passed separately."""
    return [{"role": message.role, "content": message.content} for message in messages]


class AnthropicLLM:
    def __init__(self, model: str | None = None, client: AsyncAnthropic | None = None) -> None:
        self.provider = "anthropic"
        self.model = model or DEFAULT_MODEL
        self._client = client or AsyncAnthropic(api_key=require_env("ANTHROPIC_API_KEY"))

    async def stream(self, system: str, messages: Sequence[Message]) -> AsyncIterator[str]:
        try:
            async with self._client.messages.stream(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=cacheable(system),
                output_config=EFFORT,
                messages=to_anthropic_messages(messages),
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except AnthropicError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc

    async def warm(self, system: str, messages: Sequence[Message]) -> Warmth:
        try:
            message = await self._client.messages.create(
                model=self.model,
                max_tokens=1,
                system=cacheable(system),
                output_config=EFFORT,
                messages=to_anthropic_messages(messages),
            )
        except AnthropicError as exc:
            raise ProviderError(f"{self.provider} warm failed: {exc}") from exc

        usage = message.usage
        cached = usage.cache_read_input_tokens or 0
        written = usage.cache_creation_input_tokens or 0
        return Warmth(prompt_tokens=usage.input_tokens + cached + written, cached_tokens=cached)
