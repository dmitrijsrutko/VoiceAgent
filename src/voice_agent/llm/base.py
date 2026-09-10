"""The one interface every reasoning-engine backend implements."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from voice_agent.conversation import Message

MAX_OUTPUT_TOKENS = 1024
"""Deliberately small. This agent's replies are meant to be spoken — one to
three sentences — so a large ceiling would only buy the chance to generate a
long answer nobody wants to listen to."""


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

    def stream(self, system: str, messages: Sequence[Message]) -> AsyncIterator[str]:
        """Yield the reply in fragments, in order, as the provider produces them.

        Not `async def`: an implementation is an async *generator* function, so
        calling it returns the iterator without awaiting.
        """
        ...
