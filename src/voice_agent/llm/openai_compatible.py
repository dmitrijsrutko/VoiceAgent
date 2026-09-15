"""OpenAI and DeepSeek, which speak the same wire protocol.

DeepSeek implements the OpenAI chat-completions API, so one adapter serves
both and the only difference is the base URL, the key, and the default model.
Writing two near-identical adapters to make that difference look bigger than
it is would be dishonest about the API surface.
"""

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, OpenAIError
from openai.types.chat import ChatCompletionMessageParam

from voice_agent.config import require_env
from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import MAX_OUTPUT_TOKENS, Usage, Warmth
from voice_agent.llm.http import http_client, record_call


@dataclass(frozen=True, slots=True)
class OpenAICompatibleSpec:
    provider: str
    api_key_env: str
    default_model: str
    base_url: str | None = None


OPENAI = OpenAICompatibleSpec(
    provider="openai",
    api_key_env="OPENAI_API_KEY",
    default_model="gpt-4o-mini",
)

DEEPSEEK = OpenAICompatibleSpec(
    provider="deepseek",
    api_key_env="DEEPSEEK_API_KEY",
    default_model="deepseek-chat",
    base_url="https://api.deepseek.com",
)


def to_openai_messages(
    system: str, messages: Sequence[Message]
) -> list[ChatCompletionMessageParam]:
    """The whole conversation, with the system prompt as the first message.

    This is where "context is resent on every call" actually happens: the API
    is stateless, so the entire history goes up each time.
    """
    payload: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
    for message in messages:
        if message.role == "user":
            payload.append({"role": "user", "content": message.content})
        else:
            payload.append({"role": "assistant", "content": message.content})
    return payload


class OpenAICompatibleLLM:
    def __init__(
        self,
        spec: OpenAICompatibleSpec,
        model: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.provider = spec.provider
        self.model = model or spec.default_model
        self._client = client or AsyncOpenAI(
            api_key=require_env(spec.api_key_env),
            base_url=spec.base_url,
            http_client=http_client(DefaultAsyncHttpxClient),
        )

    async def connect(self) -> None:
        try:
            await self._client.models.list()
        except OpenAIError as exc:
            raise ProviderError(f"{self.provider} connect failed: {exc}") from exc

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        call = record_call()
        try:
            chunks = await self._client.chat.completions.create(
                model=self.model,
                messages=to_openai_messages(system, messages),
                max_tokens=MAX_OUTPUT_TOKENS,
                stream=True,
                # Without this a streamed reply reports no usage at all. With it,
                # one extra final chunk carries the counts and has no choices.
                stream_options={"include_usage": True},
            )
            if usage is not None:
                call.fill(usage)
            async for event in read_to_the_end(chunks.response):
                reported = event.get("usage")
                if reported and usage is not None:
                    usage.prompt_tokens = reported["prompt_tokens"]
                    usage.cached_tokens = cached_tokens(reported)
                    usage.output_tokens = reported["completion_tokens"]
                choices = event.get("choices") or []
                delta = (choices[0].get("delta") or {}).get("content") if choices else None
                if delta:
                    yield delta
        except OpenAIError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc
        except httpx2.HTTPError as exc:
            # The SDK wraps failures to connect, but the body is read here
            # straight from httpx2: a connection lost mid-reply arrives raw.
            raise ProviderError(f"{self.provider} reply interrupted: {exc!r}") from exc

    async def warm(self, system: str, messages: Sequence[Message]) -> Warmth:
        try:
            # One token, not zero: the API's minimum. The reply is discarded;
            # the point is the prefix the provider now holds in cache.
            completion = await self._client.chat.completions.create(
                model=self.model,
                messages=to_openai_messages(system, messages),
                max_tokens=1,
                stream=False,
            )
        except OpenAIError as exc:
            raise ProviderError(f"{self.provider} warm failed: {exc}") from exc

        usage = completion.usage
        if usage is None:
            return Warmth(prompt_tokens=0, cached_tokens=0)
        return Warmth(
            prompt_tokens=usage.prompt_tokens, cached_tokens=cached_tokens(usage.model_dump())
        )


async def read_to_the_end(response: httpx2.Response) -> AsyncIterator[dict[str, Any]]:
    """A streamed reply's events, read to the end of the body.

    Not the SDK's own iterator: that stops at `data: [DONE]` and closes the
    response with the body's last bytes unread, and a connection closed
    mid-body cannot go back to the pool. Measured against DeepSeek, every
    streamed call opened a new connection that way, even back to back.
    One event per `data:` line, which is how both providers send them.
    """
    try:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line.removeprefix("data:").strip()
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            if event.get("error"):
                raise ProviderError(f"the stream reported an error: {event['error']}")
            yield event
    finally:
        await response.aclose()


def cached_tokens(reported: dict[str, Any]) -> int:
    """DeepSeek reports `prompt_cache_hit_tokens`; OpenAI nests the same idea
    under `prompt_tokens_details.cached_tokens`. Neither is in the SDK's shared
    type, so both are read defensively."""
    cached = reported.get("prompt_cache_hit_tokens")
    if cached is None:
        details = reported.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
    return int(cached or 0)
