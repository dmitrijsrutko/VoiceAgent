"""The one interface every reasoning-engine backend implements."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from voice_agent.conversation import Message

MAX_OUTPUT_TOKENS = 1024
"""Deliberately small. This agent's replies are meant to be spoken — one to
three sentences — so a large ceiling would only buy the chance to generate a
long answer nobody wants to listen to."""


@dataclass(frozen=True, slots=True)
class Warmth:
    """What a warming call found already cached.

    `cached` is what the provider says it served from cache, so this reports
    the state *before* the warm rather than after — which is the honest thing
    to show, since it is what the real call would otherwise have had to pay
    for.
    """

    prompt_tokens: int
    cached_tokens: int


@dataclass(slots=True)
class Usage:
    """What the provider says one streamed reply cost, in tokens.

    Filled in by the adapter as its stream finishes, so it reads zero until
    then — and stays zero for a provider that reports nothing, which the page
    shows as absence rather than as a count of zero. Passed in rather than
    returned because `stream` already yields text; a claimed speculation hands
    its own `Usage` to the turn that adopts it.
    """

    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    """A streaming, provider-agnostic reasoning engine.

    One method, on purpose: everything the pipeline needs from a provider is
    "given the conversation so far, stream the reply as it is generated".
    Vendor SDK types never cross this boundary — adapters translate both ways,
    and adapters are the only place that imports a vendor SDK.
    """

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        """Yield the reply in fragments, in order, as the provider produces them.

        A fragment is whatever the provider sends in one event — often one
        token, but not by contract — so fragments are not a token count; the
        provider's own count lands in `usage` once the stream ends.

        Not `async def`: an implementation is an async *generator* function, so
        calling it returns the iterator without awaiting.
        """
        ...

    async def warm(self, system: str, messages: Sequence[Message]) -> Warmth:
        """Prefill this prompt without generating a reply.

        The request is real and is billed for its input tokens; only the output
        is thrown away. What it buys is that the provider's prefix cache holds
        this prompt when the real call arrives moments later.
        """
        ...
