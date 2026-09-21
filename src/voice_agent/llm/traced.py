"""Every reasoning call, traced, whoever makes it.

A wrapper around the `LLM` protocol rather than a span inside each adapter.
`stream` is called from four places — a turn, a speculation, a warm, an
unprompted decision — and implemented by two adapters, so instrumenting the
*seam* between them covers every combination once, and covers a backend added
in a later chapter for free. It is also what lets the span tree be tested at
all: a faked provider is traced exactly like a real one, where instrumentation
living inside the vendor adapters could only ever be exercised against the
vendor.
"""

from collections.abc import AsyncIterator, Sequence

from voice_agent import trace
from voice_agent.conversation import Message
from voice_agent.llm.base import LLM, MAX_OUTPUT_TOKENS, Usage, Warmth
from voice_agent.streams import closing


class Traced:
    def __init__(self, inner: LLM) -> None:
        self._inner = inner

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    async def connect(self) -> None:
        await self._inner.connect()

    async def warm(self, system: str, messages: Sequence[Message]) -> Warmth:
        with trace.span("llm.warm", self._request(system, messages)):
            warmth = await self._inner.warm(system, messages)
        trace.event(
            "llm.warmed",
            {
                "gen_ai.usage.input_tokens": warmth.prompt_tokens,
                "cached_tokens": warmth.cached_tokens,
            },
        )
        return warmth

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        # Its own `Usage` when the caller wants none, so the trace can still say
        # what the call cost — a warm or a discarded guess is a real bill.
        counted = usage if usage is not None else Usage()
        produced: list[str] = []
        with trace.span("llm", self._request(system, messages)):
            # `closing`, not a bare `async for`: closing this wrapper does not
            # close what it wraps, and the provider's stream is billed until it
            # is closed. Chapter 7 learned that one layer down.
            async with closing(self._inner.stream(system, messages, counted)) as inner:
                async for fragment in inner:
                    produced.append(fragment)
                    yield fragment
            trace.event(
                "llm.reply",
                {
                    "text": "".join(produced),
                    "gen_ai.usage.input_tokens": counted.prompt_tokens,
                    "gen_ai.usage.output_tokens": counted.output_tokens,
                    "cached_tokens": counted.cached_tokens,
                    "accepted_ms": counted.accepted_ms,
                    "connect_ms": counted.connect_ms,
                    "attempts": counted.attempts,
                },
            )

    def _request(self, system: str, messages: Sequence[Message]) -> dict[str, object]:
        """OpenTelemetry's GenAI attribute names where they have settled, so
        exporting this over OTLP later is a mapping rather than a rewrite."""
        return {
            "gen_ai.system": self.provider,
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": self.model,
            "gen_ai.request.max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
