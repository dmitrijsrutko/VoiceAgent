from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from voice_agent.conversation import Message
from voice_agent.errors import ConfigError
from voice_agent.llm import create_llm
from voice_agent.llm.anthropic_provider import AnthropicLLM, to_anthropic_messages
from voice_agent.llm.base import Usage
from voice_agent.llm.openai_compatible import (
    DEEPSEEK,
    OPENAI,
    OpenAICompatibleLLM,
    to_openai_messages,
)

CONVERSATION = [
    Message("user", "hello"),
    Message("assistant", "hi"),
    Message("user", "and again"),
]


def test_openai_wire_puts_the_system_prompt_first_and_keeps_history_in_order() -> None:
    assert to_openai_messages("be brief", CONVERSATION) == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "and again"},
    ]


def test_anthropic_wire_carries_history_only_the_system_prompt_is_separate() -> None:
    assert to_anthropic_messages(CONVERSATION) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "and again"},
    ]


def test_deepseek_is_openai_compatible_with_its_own_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    llm = OpenAICompatibleLLM(DEEPSEEK)

    assert llm.provider == "deepseek"
    assert llm.model == "deepseek-chat"
    assert DEEPSEEK.base_url == "https://api.deepseek.com"
    assert OPENAI.base_url is None


def test_registry_builds_each_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "test")

    assert create_llm("deepseek").provider == "deepseek"
    assert create_llm("openai").provider == "openai"
    assert create_llm("anthropic").provider == "anthropic"


def test_model_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert AnthropicLLM("claude-sonnet-5").model == "claude-sonnet-5"


def test_unknown_provider_names_the_ones_that_exist() -> None:
    with pytest.raises(ConfigError, match="anthropic, deepseek, openai"):
        create_llm("gpt-9000")


def test_a_missing_key_fails_loudly_rather_than_at_the_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        create_llm("deepseek")


def chunk(content: str | None, usage: dict[str, Any] | None = None) -> SimpleNamespace:
    """An OpenAI-shaped stream chunk; the usage chunk has no choices."""
    choices = [] if content is None else [SimpleNamespace(delta=SimpleNamespace(content=content))]
    reported = None
    if usage is not None:
        reported = SimpleNamespace(
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            model_dump=lambda: usage,
        )
    return SimpleNamespace(choices=choices, usage=reported)


class FakeCompletions:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self.chunks = chunks
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> AsyncIterator[SimpleNamespace]:
        self.requests.append(request)

        async def stream() -> AsyncIterator[SimpleNamespace]:
            for item in self.chunks:
                yield item

        return stream()


async def test_a_streamed_openai_compatible_reply_reports_its_token_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `include_usage` a stream reports nothing; with it, one final chunk
    with no choices carries the counts — DeepSeek's cache hits included."""
    completions = FakeCompletions(
        [
            chunk("Hel"),
            chunk("lo"),
            chunk(
                None,
                {"prompt_tokens": 1390, "completion_tokens": 3, "prompt_cache_hit_tokens": 1152},
            ),
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm = OpenAICompatibleLLM(DEEPSEEK, client=client)  # type: ignore[arg-type]
    usage = Usage()

    text = [fragment async for fragment in llm.stream("be brief", CONVERSATION, usage)]

    assert text == ["Hel", "lo"]
    assert completions.requests[0]["stream_options"] == {"include_usage": True}
    assert usage == Usage(prompt_tokens=1390, cached_tokens=1152, output_tokens=3)


async def test_openai_nests_its_cache_hits_differently_and_is_read_too() -> None:
    reported = {
        "prompt_tokens": 900,
        "completion_tokens": 12,
        "prompt_tokens_details": {"cached_tokens": 768},
    }
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions([chunk(None, reported)]))
    )
    usage = Usage()

    llm = OpenAICompatibleLLM(OPENAI, client=client)  # type: ignore[arg-type]
    _ = [f async for f in llm.stream("s", CONVERSATION, usage)]

    assert usage == Usage(prompt_tokens=900, cached_tokens=768, output_tokens=12)


class FakeAnthropicStream:
    def __init__(self, texts: list[str], usage: SimpleNamespace) -> None:
        self._texts = texts
        self._usage = usage

    async def __aenter__(self) -> "FakeAnthropicStream":
        return self

    async def __aexit__(self, *_: object) -> None: ...

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def texts() -> AsyncIterator[str]:
            for text in self._texts:
                yield text

        return texts()

    async def get_final_message(self) -> SimpleNamespace:
        return SimpleNamespace(usage=self._usage)


async def test_an_anthropic_reply_counts_cache_reads_and_writes_as_prompt() -> None:
    """Anthropic's `input_tokens` excludes both; the prompt is all three."""
    final = SimpleNamespace(
        input_tokens=20,
        output_tokens=7,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=30,
    )
    client = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **_: FakeAnthropicStream(["Hi", " there"], final))
    )
    usage = Usage()

    text = [f async for f in AnthropicLLM(client=client).stream("s", CONVERSATION, usage)]  # type: ignore[arg-type]

    assert text == ["Hi", " there"]
    assert usage == Usage(prompt_tokens=1050, cached_tokens=1000, output_tokens=7)
