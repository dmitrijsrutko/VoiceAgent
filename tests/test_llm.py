from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic import omit

from voice_agent.conversation import Message
from voice_agent.errors import ConfigError, ProviderError
from voice_agent.llm import create_llm
from voice_agent.llm.anthropic_provider import (
    CACHE_THROUGH_LAST,
    DEFAULT_EFFORT,
    OPENING,
    AnthropicLLM,
    to_anthropic_messages,
)
from voice_agent.llm.base import MAX_OUTPUT_TOKENS, Usage, refuse_silent_reply
from voice_agent.llm.openai_compatible import (
    DEEPSEEK,
    OPENAI,
    OpenAICompatibleLLM,
    to_openai_messages,
)
from voice_agent.llm.registry import CHOICES, ENGINES, check_model

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


def test_anthropic_wire_opens_with_a_user_turn_before_the_greeting() -> None:
    """The API rejects a conversation whose first message is the assistant's,
    and every conversation here opens with the agent's greeting."""
    greeted = [Message("assistant", "Hi, how can I help?"), Message("user", "hello")]

    assert to_anthropic_messages(greeted) == [
        {"role": "user", "content": OPENING},
        {"role": "assistant", "content": "Hi, how can I help?"},
        {"role": "user", "content": "hello"},
    ]


def test_deepseek_is_openai_compatible_with_its_own_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    llm = OpenAICompatibleLLM(DEEPSEEK)

    assert llm.provider == "deepseek"
    assert llm.model == "deepseek-flash"
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


def test_a_model_from_another_provider_is_refused_by_name() -> None:
    """The pair that reached a live conversation: provider `deepseek` carrying
    a Claude model, which 400'd on its first turn."""
    with pytest.raises(ConfigError) as raised:
        check_model("deepseek", "claude-haiku-4-5")

    assert "claude-haiku-4-5" in str(raised.value)
    assert "deepseek-flash" in str(raised.value), "the models that fit went unnamed"


def test_create_llm_refuses_a_model_from_another_provider() -> None:
    """The pair that reached a live conversation: provider `deepseek` carrying a
    Claude model, which 400'd on its first turn. `create_llm` is the one place
    every engine is built, so it is where a pair is refused."""
    with pytest.raises(ConfigError, match="deepseek-flash"):
        create_llm("deepseek", "claude-haiku-4-5")


def test_the_output_cap_leaves_room_for_a_chain_of_thought() -> None:
    """`max_tokens` is shared with the reasoning, so this is not a style choice.

    At 1024 a live `deepseek-max` turn filled the allowance thinking and sent no
    answer; the guard in `refuse_silent_reply` turned that into a visible
    failure. Measured against `deepseek-flash`, one hard turn at `max` used 4389
    output tokens. Dropping the cap back under that footprint reintroduces the
    silence, so the number is pinned rather than left to taste.
    """
    assert MAX_OUTPUT_TOKENS > 4389, "the cap has to exceed the measured reasoning footprint"


def test_a_billed_reply_with_no_text_is_refused() -> None:
    """Seen live on the deployed instance: DeepSeek spent its output budget
    thinking, streamed no `content` at all, and the turn was recorded as a
    successful reply that said nothing. Silence is a failure, not a reply."""
    with pytest.raises(ProviderError, match="444 output tokens") as raised:
        refuse_silent_reply("deepseek", "deepseek-flash", "max", False, Usage(output_tokens=444))

    assert "at effort max" in str(raised.value), (
        "the record keeps only provider and model, so this message is the one "
        "place the level that caused it survives"
    )


@pytest.mark.parametrize(
    "wrote, usage",
    [(True, Usage(output_tokens=444)), (False, Usage()), (False, None)],
    ids=["wrote text", "billed nothing", "usage unknown"],
)
def test_a_reply_that_was_not_billed_is_left_alone(wrote: bool, usage: Usage | None) -> None:
    refuse_silent_reply("deepseek", "deepseek-flash", "low", wrote, usage)


def test_create_llm_carries_the_effort_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """An option is (provider, model, effort), and all three have to reach the
    adapter: the menu's three DeepSeek tiers differ in nothing else."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    llm = create_llm("deepseek", "deepseek-flash", "max")

    assert isinstance(llm, OpenAICompatibleLLM)
    assert (llm.provider, llm.model, llm.effort) == ("deepseek", "deepseek-flash", "max")


def test_a_caller_that_names_no_effort_gets_the_engine_s_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What `Engine.default_effort` is for. `None` used to mean "send nothing"
    on Anthropic and "the spec's default" on DeepSeek, so two engines disagreed
    about what the same argument asked for — and `create_llm("anthropic")`, the
    bench's default target, quietly stopped requesting the low effort a spoken
    reply wants. Every caller now says "no preference" and means it once."""
    for name in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "sk-test")

    anthropic = create_llm("anthropic")
    deepseek = create_llm("deepseek")
    openai = create_llm("openai")

    assert isinstance(anthropic, AnthropicLLM)
    assert isinstance(deepseek, OpenAICompatibleLLM)
    assert isinstance(openai, OpenAICompatibleLLM)

    assert ENGINES["anthropic"].default_effort == DEFAULT_EFFORT == "low"
    assert (anthropic.effort, deepseek.effort, openai.effort) == ("low", "low", None)
    assert openai.effort is None, "OpenAI has no such parameter, and that is not `low`"


def test_every_offered_model_could_actually_be_built() -> None:
    """The menu is data, so nothing but a test stands between a typo in it and a
    conversation that cannot start. This is that test: the model belongs to the
    provider that serves it, and the id `?llm=` carries is unique."""
    for choice in CHOICES:
        assert choice.name, "an option needs an id for `?llm=` to carry"
        check_model(choice.provider, choice.model)
        assert choice.title, "and something for the page to show"

    names = [choice.name for choice in CHOICES]
    assert len(names) == len(set(names)), "two options share an id"


def test_every_default_is_one_of_its_own_provider_s_models() -> None:
    """A default outside its lineup would refuse the very model a deployment
    runs when nothing is overridden."""
    for name, engine in ENGINES.items():
        assert engine.default_model in engine.models, name


def test_a_declared_model_and_no_override_at_all_are_both_fine() -> None:
    check_model("deepseek", "deepseek-v4-pro")
    check_model("deepseek", None)
    # An unknown provider is `create_llm`'s to name, not this check's.
    check_model("no-such-provider", "claude-haiku-4-5")


def test_a_missing_key_fails_loudly_rather_than_at_the_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        create_llm("deepseek")


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
        messages=SimpleNamespace(stream=lambda **_: FakeAnthropicStream(["Hi", " there"], final)),
        models=models(effort=True),
    )
    usage = Usage()

    text = [f async for f in AnthropicLLM(client=client).stream("s", CONVERSATION, usage)]  # type: ignore[arg-type]

    assert text == ["Hi", " there"]
    assert usage == Usage(prompt_tokens=1050, cached_tokens=1000, output_tokens=7)


async def test_anthropic_caches_the_conversation_not_only_the_system_prompt() -> None:
    """Only what is marked is cached. With the system prompt alone marked,
    every turn re-read the whole history."""
    calls: dict[str, dict[str, Any]] = {}
    final = SimpleNamespace(
        input_tokens=1, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )

    def stream(**kwargs: Any) -> FakeAnthropicStream:
        calls["stream"] = kwargs
        return FakeAnthropicStream(["ok"], final)

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream), models=models(effort=True))
    llm = AnthropicLLM(client=client)  # type: ignore[arg-type]

    [_ async for _ in llm.stream("s", CONVERSATION, Usage())]

    assert calls["stream"]["cache_control"] == CACHE_THROUGH_LAST


def models(effort: bool) -> SimpleNamespace:
    """The Models API's answer for any model: whether it accepts `effort`."""
    lookups: list[str] = []

    async def retrieve(model: str) -> SimpleNamespace:
        lookups.append(model)
        effort_capability = SimpleNamespace(supported=effort)
        return SimpleNamespace(capabilities=SimpleNamespace(effort=effort_capability))

    return SimpleNamespace(retrieve=retrieve, lookups=lookups)


@pytest.mark.parametrize("supported", [True, False])
async def test_effort_is_sent_only_to_a_model_that_accepts_it(supported: bool) -> None:
    """Claude Haiku 4.5 rejects the parameter with a 400, so every turn failed."""
    calls: list[dict[str, Any]] = []
    final = SimpleNamespace(
        input_tokens=1, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )

    def stream(**kwargs: Any) -> FakeAnthropicStream:
        calls.append(kwargs)
        return FakeAnthropicStream(["ok"], final)

    lookup = models(effort=supported)
    client = SimpleNamespace(messages=SimpleNamespace(stream=stream), models=lookup)
    llm = AnthropicLLM("some-model", DEFAULT_EFFORT, client=client)  # type: ignore[arg-type]

    await llm.connect()
    [_ async for _ in llm.stream("s", CONVERSATION, Usage())]

    sent = [call["output_config"] for call in calls]
    assert sent == ([{"effort": DEFAULT_EFFORT}] if supported else [omit])
    assert lookup.lookups == ["some-model"], "the model was looked up more than once"


@pytest.mark.parametrize("asked", ["low", "high", "max"])
async def test_the_effort_this_conversation_asked_for_is_the_one_sent(asked: str) -> None:
    """Effort is per conversation now, which is what lets one model be offered
    at three levels. `low` was the only value before, baked into the module."""
    calls: list[dict[str, Any]] = []
    final = SimpleNamespace(
        input_tokens=1, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )

    def stream(**kwargs: Any) -> FakeAnthropicStream:
        calls.append(kwargs)
        return FakeAnthropicStream(["ok"], final)

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream), models=models(effort=True))
    llm = AnthropicLLM("some-model", asked, client=client)  # type: ignore[arg-type]

    await llm.connect()
    [_ async for _ in llm.stream("s", CONVERSATION, Usage())]

    assert [call["output_config"] for call in calls] == [{"effort": asked}]


async def test_no_effort_means_nothing_is_sent_even_where_it_is_supported() -> None:
    """Haiku 4.5 gets no effort at all from the menu. The capability check alone
    would not be enough: this model reports that it supports one."""
    calls: list[dict[str, Any]] = []
    final = SimpleNamespace(
        input_tokens=1, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )

    def stream(**kwargs: Any) -> FakeAnthropicStream:
        calls.append(kwargs)
        return FakeAnthropicStream(["ok"], final)

    client = SimpleNamespace(messages=SimpleNamespace(stream=stream), models=models(effort=True))
    llm = AnthropicLLM("some-model", None, client=client)  # type: ignore[arg-type]

    await llm.connect()
    [_ async for _ in llm.stream("s", CONVERSATION, Usage())]

    assert [call["output_config"] for call in calls] == [omit]
    assert llm.effort is None
