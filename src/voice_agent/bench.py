"""`uv run voice-agent --bench-llm` — time to first token, provider by provider,
through the same adapters, prompt and connection handling the agent uses.

Every call is real and billed: a short question, a one-sentence answer."""

import asyncio
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from voice_agent import prompts
from voice_agent.conversation import Message
from voice_agent.errors import VoiceAgentError
from voice_agent.llm import LLM, create_llm
from voice_agent.llm.base import Usage

DEFAULT_TARGETS = ("deepseek", "openai", "anthropic:claude-haiku-4-5", "anthropic")

ROUNDS = 5
"""Each after the target has connected, as the agent connects at startup."""

QUESTION = "In one sentence, why is the sky blue?"


@dataclass(frozen=True, slots=True)
class Sample:
    ttft_ms: int
    total_ms: int
    connect_ms: int | None
    output_tokens: int
    accepted_ms: int | None = None
    attempts: int = 1


@dataclass(slots=True)
class Result:
    target: str
    skipped: str | None = None
    connect_ms: int | None = None
    samples: list[Sample] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse(target: str) -> tuple[str, str | None]:
    provider, _, model = target.partition(":")
    return provider, model or None


async def measure(llm: LLM, system: str, messages: Sequence[Message]) -> Sample:
    usage = Usage()
    started = time.perf_counter()
    first: float | None = None
    async for _ in llm.stream(system, messages, usage):
        if first is None:
            first = time.perf_counter()
    ended = time.perf_counter()
    return Sample(
        ttft_ms=round(((first or ended) - started) * 1000),
        total_ms=round((ended - started) * 1000),
        connect_ms=usage.connect_ms,
        output_tokens=usage.output_tokens,
        accepted_ms=usage.accepted_ms,
        attempts=usage.attempts,
    )


async def run(
    targets: Sequence[str],
    rounds: int = ROUNDS,
    build: Callable[[str, str | None], LLM] = create_llm,
) -> list[Result]:
    """Round-robin, so a provider's slow minute is spread across the others
    rather than landing on whichever one happened to run then."""
    system = prompts.load("system_prompt")
    messages = [Message("user", QUESTION)]
    results = [Result(target) for target in targets]
    engines: dict[str, LLM] = {}
    for result in results:
        try:
            engine = build(*parse(result.target))
            started = time.perf_counter()
            await engine.connect()
            result.connect_ms = round((time.perf_counter() - started) * 1000)
        except VoiceAgentError as exc:
            result.skipped = str(exc)
            continue
        engines[result.target] = engine

    for _ in range(rounds):
        for result in results:
            if result.target not in engines:
                continue
            try:
                result.samples.append(await measure(engines[result.target], system, messages))
            except VoiceAgentError as exc:
                result.errors.append(str(exc))
    return results


def report(results: Sequence[Result]) -> str:
    header = [
        "target", "connect", "accepted p50", "ttft p50", "range", "total p50", "reconnects",
        "retries", "tokens",
    ]  # fmt: skip
    rows: list[list[str]] = []
    notes: list[str] = []
    for result in results:
        if result.skipped or not result.samples:
            reason = result.skipped or (result.errors[0] if result.errors else "no samples")
            notes.append(f"{result.target}: {'skipped' if result.skipped else 'failed'}: {reason}")
            continue
        samples = result.samples
        ttfts = [s.ttft_ms for s in samples]
        rows.append(
            [
                result.target,
                f"{result.connect_ms} ms",
                accepted(samples),
                f"{median(ttfts)} ms",
                f"{min(ttfts)}-{max(ttfts)} ms",
                f"{median([s.total_ms for s in samples])} ms",
                f"{sum(s.connect_ms is not None for s in samples)} of {len(samples)}",
                str(sum(max(s.attempts - 1, 0) for s in samples)),
                str(median([s.output_tokens for s in samples])),
            ]
        )
        if result.errors:
            notes.append(
                f"{result.target}: {len(result.errors)} call(s) failed: {result.errors[0]}"
            )
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(
            cell.ljust(width) if i == 0 else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        )
        for row in [header, *rows]
    ]
    lines += ["", *notes] if notes else []
    lines.append(
        "\nconnect: the startup request that opens the connection (first in the process, so it "
        "also pays DNS and TLS setup). accepted: call to response headers; the rest of ttft is "
        "the provider. reconnects: calls that opened a new connection anyway. retries: extra "
        "attempts the SDK made after a refusal or a dropped connection."
    )
    return "\n".join(lines)


def accepted(samples: Sequence[Sample]) -> str:
    known = [s.accepted_ms for s in samples if s.accepted_ms is not None]
    return f"{median(known)} ms" if known else "?"


def median(values: Sequence[int]) -> int:
    return round(statistics.median(values))


def main(targets: Sequence[str]) -> None:
    chosen = list(targets) or list(DEFAULT_TARGETS)
    print(f"{len(chosen)} target(s), {ROUNDS} short billed calls each: {QUESTION!r}\n")
    print(report(asyncio.run(run(chosen))))
