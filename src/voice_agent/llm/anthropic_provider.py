"""Anthropic, whose API differs from the OpenAI wire format in two ways.

The system prompt is a top-level parameter rather than the first message, and
the SDK exposes streaming as an async context manager with a text-only view of
the event stream. Both are handled here so nothing above this module notices.
"""

from collections.abc import AsyncIterator, Sequence

from anthropic import AnthropicError, AsyncAnthropic
from anthropic.types import MessageParam, OutputConfigParam

from voice_agent.config import require_env
from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import MAX_OUTPUT_TOKENS

DEFAULT_MODEL = "claude-opus-5"

EFFORT: OutputConfigParam = {"effort": "low"}
"""Thinking is on by default on this model family. Reasoning before the first
token is exactly what a spoken conversation cannot afford, and low effort is
the supported way to shorten it — disabling thinking outright is documented to
cause the model to narrate tool calls and leak reasoning tags into the reply."""


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
                system=system,
                output_config=EFFORT,
                messages=to_anthropic_messages(messages),
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except AnthropicError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc
