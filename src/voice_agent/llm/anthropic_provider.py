"""Anthropic, whose API differs from the OpenAI wire format in three ways.

The system prompt is a top-level parameter rather than the first message; the
conversation must open with a user turn, where ours opens with the greeting;
and the SDK exposes streaming as an async context manager with a text-only view
of the event stream. All three are handled here so nothing above this module
notices.
"""

from collections.abc import AsyncIterator, Sequence

from anthropic import AnthropicError, AsyncAnthropic, DefaultAsyncHttpxClient, Omit, omit
from anthropic.types import (
    CacheControlEphemeralParam,
    MessageParam,
    OutputConfigParam,
    TextBlockParam,
)

from voice_agent.config import require_env
from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import MAX_OUTPUT_TOKENS, Usage, Warmth
from voice_agent.llm.http import http_client, record_call

ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
"""Named once, so `registry.KEYS` can say what this backend needs without
restating it. The OpenAI-compatible backends carry theirs on their spec."""

DEFAULT_MODEL = "claude-opus-5"

EFFORT: OutputConfigParam = {"effort": "low"}
"""Thinking is on by default on this model family. Reasoning before the first
token is exactly what a spoken conversation cannot afford, and low effort is
the supported way to shorten it — disabling thinking outright is documented to
cause the model to narrate tool calls and leak reasoning tags into the reply.

Sent only to a model that accepts it. Claude Haiku 4.5 rejects the parameter
outright (400, "This model does not support the effort parameter"), which made
`--model claude-haiku-4-5` fail every turn."""


CACHE_THROUGH_LAST: CacheControlEphemeralParam = {"type": "ephemeral"}
"""Passed top-level, which marks the last block of the request as a breakpoint.

Unlike the OpenAI-compatible backends, Anthropic caches only what is explicitly
marked. With only the system prompt marked, every turn re-processed the whole
conversation, and a warm prefilled history the real call could not read back —
the cost of warming with none of the benefit."""

OPENING = "(call connected)"
"""The user turn the API requires before the agent's greeting. Fixed text, so
the start of every conversation's cached prefix is identical."""


def cacheable(system: str) -> list[TextBlockParam]:
    """Mark the system prompt as a breakpoint of its own, so it is shared by
    every conversation rather than cached only as the start of each one."""
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def to_anthropic_messages(messages: Sequence[Message]) -> list[MessageParam]:
    """The conversation only. The system prompt is passed separately."""
    payload: list[MessageParam] = [
        {"role": message.role, "content": message.content} for message in messages
    ]
    if payload and payload[0]["role"] == "assistant":
        payload.insert(0, {"role": "user", "content": OPENING})
    return payload


class AnthropicLLM:
    def __init__(self, model: str | None = None, client: AsyncAnthropic | None = None) -> None:
        self.provider = "anthropic"
        self.model = model or DEFAULT_MODEL
        self._client = client or AsyncAnthropic(
            api_key=require_env(ANTHROPIC_API_KEY),
            http_client=http_client(DefaultAsyncHttpxClient),
        )

        self._supports_effort: bool | None = None

    async def connect(self) -> None:
        await self._effort()

    async def _effort(self) -> OutputConfigParam | Omit:
        """Asked of the Models API rather than written down per model name, since
        which models accept `effort` changes with every release. Asked once: at
        startup, or by the first call if connecting then failed."""
        if self._supports_effort is None:
            try:
                info = await self._client.models.retrieve(self.model)
            except AnthropicError as exc:
                raise ProviderError(f"{self.provider} connect failed: {exc}") from exc
            self._supports_effort = bool(info.capabilities and info.capabilities.effort.supported)
        return EFFORT if self._supports_effort else omit

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        call = record_call()
        try:
            effort = await self._effort()
            # Only a turn whose startup connect failed looks the model up. A
            # connection it opened is this turn's cost; its request is not.
            call.restart()
            async with self._client.messages.stream(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=cacheable(system),
                output_config=effort,
                messages=to_anthropic_messages(messages),
                cache_control=CACHE_THROUGH_LAST,
            ) as stream:
                if usage is not None:
                    call.fill(usage)
                async for text in stream.text_stream:
                    yield text
                if usage is not None:
                    final = (await stream.get_final_message()).usage
                    cached = final.cache_read_input_tokens or 0
                    written = final.cache_creation_input_tokens or 0
                    # Anthropic's `input_tokens` excludes both cache reads and
                    # cache writes; the prompt is all three together.
                    usage.prompt_tokens = final.input_tokens + cached + written
                    usage.cached_tokens = cached
                    usage.output_tokens = final.output_tokens
        except AnthropicError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc

    async def warm(self, system: str, messages: Sequence[Message]) -> Warmth:
        try:
            # Zero output tokens: prefill only, the documented way to warm.
            message = await self._client.messages.create(
                model=self.model,
                max_tokens=0,
                system=cacheable(system),
                # The same as the reply's: a different effort is a different
                # cache, and the warm would prefill one the reply cannot read.
                output_config=await self._effort(),
                messages=to_anthropic_messages(messages),
                cache_control=CACHE_THROUGH_LAST,
            )
        except AnthropicError as exc:
            raise ProviderError(f"{self.provider} warm failed: {exc}") from exc

        usage = message.usage
        cached = usage.cache_read_input_tokens or 0
        written = usage.cache_creation_input_tokens or 0
        return Warmth(prompt_tokens=usage.input_tokens + cached + written, cached_tokens=cached)
