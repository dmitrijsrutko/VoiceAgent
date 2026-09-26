"""Every reasoning call, traced, whoever makes it.

A wrapper around the `LLM` protocol rather than a span inside each adapter:
one seam covers every caller and every backend, and a faked provider is traced
exactly like a real one, so the span tree can be tested.
"""

from collections.abc import AsyncIterator, Sequence

from voice_agent import trace
from voice_agent.conversation import Message
from voice_agent.llm.base import LLM, MAX_OUTPUT_TOKENS, Usage
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

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        # Its own `Usage` when the caller wants none, so the trace can still say
        # what the call cost — a discarded guess is a real bill.
        counted = usage if usage is not None else Usage()
        produced: list[str] = []
        with trace.span("llm", self._request(system, messages)):
            # `closing`, not a bare `async for`: closing this wrapper does not
            # close what it wraps, and the provider's stream is billed until it
            # is closed.
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
                    "finish_reason": counted.finish_reason,
                    "reasoning_chars": counted.reasoning_chars,
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
