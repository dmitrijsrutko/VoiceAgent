"""OpenAI and DeepSeek, which speak the same wire protocol.

DeepSeek implements the OpenAI chat-completions API, so one adapter serves
both and the only difference is the base URL, the key, and the default model.
Writing two near-identical adapters to make that difference look bigger than
it is would be dishonest about the API surface.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam

from voice_agent.config import require_env
from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import MAX_OUTPUT_TOKENS


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

    async def stream(self, system: str, messages: Sequence[Message]) -> AsyncIterator[str]:
        try:
            chunks = await self._client.chat.completions.create(
                model=self.model,
                messages=to_openai_messages(system, messages),
                max_tokens=MAX_OUTPUT_TOKENS,
                stream=True,
            )
            async for chunk in chunks:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except OpenAIError as exc:
            raise ProviderError(f"{self.provider} request failed: {exc}") from exc
