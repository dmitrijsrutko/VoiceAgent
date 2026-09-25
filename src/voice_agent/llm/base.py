"""The one interface every reasoning-engine backend implements."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from voice_agent.conversation import Message
from voice_agent.errors import ProviderError

Effort = Literal["low", "high", "max"]
"""How hard a provider is asked to think before it answers, where it can be.

Narrower than either vendor's own vocabulary on purpose. DeepSeek maps
`minimal` and `low` onto `low`, and `medium`, `high` and `xhigh` onto `high`,
so of the seven values it accepts only three mean anything different — and
Anthropic takes all three of these among its five. They are therefore the levels
a menu can honestly offer, and an adapter translates this into whatever its own
SDK wants. A vendor's type never crosses this boundary.

`None` is not a level. At the registry's `create_llm` it means "this engine's own
default", which may itself be nothing; an adapter is handed a resolved value, so
`None` reaching one is the instruction to send nothing at all.
"""

MAX_OUTPUT_TOKENS = 8192
"""The reply ceiling — raised from 1024, because it is not only the reply's.

A provider that thinks before it answers spends this budget on the reasoning
too. Measured against `deepseek-flash`: one hard turn at effort `max` used **4389
output tokens**, and at 1024 the chain of thought (444, then 1024) filled the
whole allowance and the turn produced no answer at all — live, twice. 8192 leaves
room for the deepest footprint measured so far and for an answer after it.

The answers themselves stay short: the prompt asks for one to three sentences,
and the measured replies ran 587-737 characters at both `high` and `max`. This is
not a licence to ramble — it is so that thinking cannot starve the answer. What
it costs is latency, which the ceiling does not set: that same turn took 23.7 s
to its first token, and `turn.SLOW_FIRST_TOKEN_MS` is what reports that.
"""


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

    How much of the wait an accept splits off is a property of the *model*, not
    the vendor. Measured over 259 archived turns on Claude Haiku 4.5, the two
    numbers differ by -38 ms on average (min -590, max +419): it holds its
    headers until the first token is ready, so there is nothing to split. Claude
    Opus 5.5 does not — one live session accepted at 1405 ms and produced its
    first token at 3852 ms, every turn, a 2.4 s gap that reading this field as
    "the two are the same" would have hidden."""
    attempts: int = 0
    """HTTP requests the call took. More than one means the SDK retried after a
    refusal (429, 5xx) or a dropped connection, with a backoff in between. Zero
    when the adapter cannot tell."""


def refuse_silent_reply(
    provider: str, model: str, effort: Effort | None, wrote: bool, usage: Usage | None
) -> None:
    """Refuse a reply the provider billed for and never sent.

    A thinking model can spend its whole output budget reasoning and emit no
    answer at all. The stream then ends normally, `usage` reports the tokens, and
    without this the pipeline records a successful turn that said nothing — seen
    live as a 3.2 s silence and 444 billed tokens against an empty record.

    The effort is named because a session record keeps only provider and model,
    so this message is the one place the level that caused it survives.
    """
    if wrote or usage is None or not usage.output_tokens:
        return
    at = f" at effort {effort}" if effort is not None else ""
    raise ProviderError(
        f"{provider} sent no text for {model}{at} "
        f"after reporting {usage.output_tokens} output tokens"
    )


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
