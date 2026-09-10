import pytest

from voice_agent.conversation import Message
from voice_agent.errors import ConfigError
from voice_agent.llm import create_llm
from voice_agent.llm.anthropic_provider import AnthropicLLM, to_anthropic_messages
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
