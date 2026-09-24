"""The HTTP connection an LLM adapter talks over: kept open between turns, and
what each call's requests did — connections opened, attempts, when the provider
accepted — recorded against the call."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import httpx2

from voice_agent import timing
from voice_agent.llm.base import Usage

KEEPALIVE_SECONDS = 300.0
"""How long an idle connection is kept for the next call.

Both SDKs default to 5 s, and a turn almost never comes that soon: the user has
just listened to a reply. So nearly every turn opened a new connection —
measured against DeepSeek at ~27 ms (TCP 13 ms, TLS 14 ms) to its nearest edge.
Measured idle, DeepSeek's, OpenAI's and Anthropic's edges all kept a connection
for 150 s; at 290 s DeepSeek's had closed it and the other two had not. One
closed in the meantime is noticed by the pool and replaced, at the old cost."""

LIMITS = httpx2.Limits(
    max_connections=1000, max_keepalive_connections=100, keepalive_expiry=KEEPALIVE_SECONDS
)
"""The SDKs' own pool sizes, with only the idle limit changed."""


@dataclass(slots=True)
class Call:
    """What one call's HTTP requests did before the provider sent its first token."""

    started: float = field(default_factory=timing.now)
    attempts: int = 0
    opened: int = 0
    connecting: float = 0.0
    accepted_at: float | None = None
    _since: float = 0.0

    def restart(self) -> None:
        """Time and count from the next request, keeping connection cost so far."""
        self.started = timing.now()
        self.attempts = 0
        self.accepted_at = None

    def fill(self, usage: Usage) -> None:
        usage.attempts = self.attempts
        usage.connect_ms = round(self.connecting * 1000) if self.opened else None
        if self.accepted_at is not None:
            usage.accepted_ms = round((self.accepted_at - self.started) * 1000)


_calling: ContextVar[Call | None] = ContextVar("llm_call", default=None)


def record_call() -> Call:
    """Record the HTTP requests this task makes from here on.

    Set and never reset: an adapter's `stream` is an async generator, which can
    be closed from a context other than the one that started it, and a reset
    from there fails. The next call recorded in the same task replaces this one.
    """
    record = Call()
    _calling.set(record)
    return record


async def _trace(event: str, info: dict[str, Any]) -> None:
    """httpcore's `trace` extension, called in the task making the request.
    Opening a connection is TCP (with DNS inside it), then TLS; a response's
    headers mean the provider has accepted the request."""
    record = _calling.get()
    if record is None:
        return
    now = timing.now()
    if event in ("connection.connect_tcp.started", "connection.start_tls.started"):
        record._since = now
    elif event in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
        record.connecting += now - record._since
        if event == "connection.connect_tcp.complete":
            record.opened += 1
    elif event == "http11.receive_response_headers.complete":
        record.accepted_at = now


async def _attach_trace(request: httpx2.Request) -> None:
    """Runs for every attempt, so a request the SDK retried counts twice."""
    record = _calling.get()
    if record is not None:
        record.attempts += 1
    request.extensions["trace"] = _trace


def http_client[Client: httpx2.AsyncClient](default: Callable[..., Client]) -> Client:
    """The SDK's own default client — its timeouts, and for Anthropic its TCP
    keepalive socket options — with the idle limit raised and connection
    opening traced."""
    return default(limits=LIMITS, event_hooks={"request": [_attach_trace]})
