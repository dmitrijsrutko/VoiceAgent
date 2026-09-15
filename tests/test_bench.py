"""The LLM latency bench: what it calls, in what order, and what it reports."""

from collections.abc import AsyncIterator, Sequence

from tests.conftest import FakeLLM
from voice_agent.bench import Result, Sample, parse, report, run
from voice_agent.conversation import Message
from voice_agent.errors import ConfigError, ProviderError
from voice_agent.llm import LLM
from voice_agent.llm.base import Usage


class Reopening(FakeLLM):
    """Opens a new connection on every other call, as a pool that drops them does."""

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        self.connect_ms = 30 if len(self.seen) % 2 else None
        async for fragment in super().stream(system, messages, usage):
            yield fragment


def test_a_target_names_a_provider_and_optionally_a_model() -> None:
    assert parse("deepseek") == ("deepseek", None)
    assert parse("anthropic:claude-haiku-4-5") == ("anthropic", "claude-haiku-4-5")


async def test_every_target_is_called_once_per_round_taking_turns() -> None:
    calls: list[str] = []

    def build(provider: str, model: str | None) -> LLM:
        llm = Reopening()
        original = llm.stream

        def stream(*args: object, **kwargs: object) -> AsyncIterator[str]:
            calls.append(provider)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        llm.stream = stream  # type: ignore[method-assign]
        return llm

    results = await run(["a", "b"], rounds=3, build=build)

    assert calls == ["a", "b", "a", "b", "a", "b"]
    assert [len(r.samples) for r in results] == [3, 3]
    assert [s.connect_ms for s in results[0].samples] == [None, 30, None]


async def test_each_target_connects_before_it_is_timed_as_the_agent_does() -> None:
    engines: list[FakeLLM] = []

    def build(provider: str, model: str | None) -> LLM:
        engines.append(FakeLLM())
        return engines[-1]

    results = await run(["a"], rounds=2, build=build)

    assert engines[0].connects == 1
    assert isinstance(results[0].connect_ms, int)


async def test_a_provider_without_a_key_is_skipped_not_fatal() -> None:
    def build(provider: str, model: str | None) -> LLM:
        if provider == "openai":
            raise ConfigError("OPENAI_API_KEY is not set. Put it in .env")
        return FakeLLM()

    results = await run(["deepseek", "openai"], rounds=2, build=build)

    assert results[1].skipped is not None and results[1].samples == []
    assert len(results[0].samples) == 2
    assert "openai: skipped: OPENAI_API_KEY is not set" in report(results)


async def test_a_failing_call_is_counted_and_the_others_carry_on() -> None:
    class Flaky(FakeLLM):
        async def stream(
            self, system: str, messages: Sequence[Message], usage: Usage | None = None
        ) -> AsyncIterator[str]:
            if len(self.seen) == 1:
                self.seen.append([])
                raise ProviderError("overloaded")
            async for fragment in super().stream(system, messages, usage):
                yield fragment

    results = await run(["x"], rounds=3, build=lambda p, m: Flaky())

    assert len(results[0].samples) == 2
    assert results[0].errors == ["overloaded"]


def test_the_report_counts_calls_that_had_to_reconnect() -> None:
    """Every call reconnecting is the regression this bench exists to catch:
    measured, that was every streamed DeepSeek call before it was fixed."""
    samples = [
        Sample(t, t + 300, 25 if t == 320 else None, 20, accepted_ms=a, attempts=n)
        for t, a, n in ((300, 90, 1), (320, 110, 3), (310, 100, 1))
    ]
    line = report([Result("deepseek", connect_ms=540, samples=samples)]).splitlines()[1]

    assert "540 ms" in line
    assert " 100 ms " in line, "the accepted median is missing"
    assert " 310 ms" in line
    assert "300-320 ms" in line
    assert "1 of 3" in line
    assert line.split()[-2] == "2", f"two retries were not counted: {line}"
