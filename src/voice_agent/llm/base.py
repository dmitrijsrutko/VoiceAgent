"""The one interface every reasoning-engine backend implements."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from voice_agent.conversation import Message

MAX_OUTPUT_TOKENS = 1024
"""Deliberately small. This agent's replies are meant to be spoken — one to
three sentences — so a large ceiling would only buy the chance to generate a
long answer nobody wants to listen to."""


@dataclass(slots=True)
class Usage:
    """What one streamed reply cost: the tokens the provider says it used, and
    what its HTTP request went through before the first token.

    Filled in by the adapter as its stream finishes, so it reads zero until
    then — and stays zero for a provider that reports nothing, which the page
    shows as absence rather than as a count of zero. Passed in rather than
    returned because `stream` already yields text; a claimed speculation hands
    its own `Usage` to the turn that adopts it.
    """

    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    connect_ms: int | None = None
    """TCP and TLS setup before the request could go out; None when an open
    connection was reused, or the adapter cannot tell."""
    accepted_ms: int | None = None
    """From the call to the response's headers: the provider has accepted the
    request. What follows, up to the first token, is the provider queueing and
    prefilling — a long wait after a quick accept is on the provider's side.
    Only where the provider answers before generating: measured, DeepSeek
    accepts in ~320-390 ms, while Anthropic holds its headers until the first
    token is ready, so there the two numbers are the same."""
    attempts: int = 0
    """HTTP requests the call took. More than one means the SDK retried after a
    refusal (429, 5xx) or a dropped connection, with a backoff in between. Zero
    when the adapter cannot tell."""


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

    async def connect(self) -> None:
        """Open a connection before the first call needs one.

        Free: it lists models, which no provider bills. The first request in a
        process also pays for DNS and TLS setup — measured at ~250 ms more than
        any request after it — and the first question is the worst time for it.
        """
        ...
