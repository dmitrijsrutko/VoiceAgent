"""OpenAI and DeepSeek, which speak the same wire protocol.

DeepSeek implements the OpenAI chat-completions API, so one adapter serves
both and the only difference is the base URL, the key, and the default model.
Writing two near-identical adapters to make that difference look bigger than
it is would be dishonest about the API surface.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam

from voice_agent.config import require_env
from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import MAX_OUTPUT_TOKENS, Usage, Warmth


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
        )

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
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
            async for chunk in chunks:
                if chunk.usage is not None and usage is not None:
                    reported = chunk.usage.model_dump()
                    usage.prompt_tokens = chunk.usage.prompt_tokens
                    usage.cached_tokens = cached_tokens(reported)
                    usage.output_tokens = chunk.usage.completion_tokens
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except OpenAIError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc

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


def cached_tokens(reported: dict[str, Any]) -> int:
    """DeepSeek reports `prompt_cache_hit_tokens`; OpenAI nests the same idea
    under `prompt_tokens_details.cached_tokens`. Neither is in the SDK's shared
    type, so both are read defensively."""
    cached = reported.get("prompt_cache_hit_tokens")
    if cached is None:
        details = reported.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens", 0)
    return int(cached or 0)
