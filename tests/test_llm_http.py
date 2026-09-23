"""The adapters on the wire: streamed replies, their usage, and connections
kept open between turns — through the real SDK clients, against a local HTTP
server that counts the connections it accepts. No provider is called."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.anthropic_provider import AnthropicLLM
from voice_agent.llm.base import LLM, Usage
from voice_agent.llm.http import KEEPALIVE_SECONDS
from voice_agent.llm.openai_compatible import (
    DEEPSEEK,
    OPENAI,
    OpenAICompatibleLLM,
    OpenAICompatibleSpec,
)


def sse(*events: tuple[str | None, dict[str, Any] | str]) -> bytes:
    lines = []
    for name, data in events:
        if name:
            lines.append(f"event: {name}")
        lines.append(f"data: {data if isinstance(data, str) else json.dumps(data)}\n")
    return ("\n".join(lines) + "\n").encode()


OPENAI_REPLY = sse(
    (None, {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}),
    (None, {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}),
    (None, "[DONE]"),
)  # fmt: skip

ANTHROPIC_REPLY = sse(
    ("message_start", {"type": "message_start", "message": {
        "id": "m", "type": "message", "role": "assistant", "model": "m", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 0}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "hi"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 1}}),
    ("message_stop", {"type": "message_stop"}),
)  # fmt: skip

MODELS = {"object": "list", "data": [], "has_more": False, "first_id": None, "last_id": None}
MODEL = {"id": "m", "type": "model", "capabilities": {"effort": {"supported": True}}}


class Provider:
    """A keep-alive HTTP/1.1 server answering both SDKs' requests."""

    def __init__(
        self, openai_reply: bytes = OPENAI_REPLY, cut_after: int | None = None, refuse: int = 0
    ) -> None:
        self.openai_reply = openai_reply
        self.cut_after = cut_after
        self.refuse = refuse
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> "Provider":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._server.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                length = next(
                    (int(line.split(":")[1]) for line in head.split("\r\n")
                     if line.lower().startswith("content-length:")),
                    0,
                )  # fmt: skip
                request = await reader.readexactly(length)
                if request:
                    self.requests.append(json.loads(request))
                path = head.split(" ")[1]
                if self.refuse and "models" not in path:
                    self.refuse -= 1
                    # Overloaded, retry in a millisecond: both SDKs honour it.
                    busy = b'{"error": {"type": "overloaded_error", "message": "busy"}}'
                    writer.write(
                        b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                        b"retry-after-ms: 1\r\nContent-Length: %d\r\n\r\n%s" % (len(busy), busy)
                    )
                    await writer.drain()
                    continue
                if "/models/" in path:
                    body, kind = json.dumps(MODEL).encode(), "application/json"
                elif "models" in path:
                    body, kind = json.dumps(MODELS).encode(), "application/json"
                elif "messages" in path:
                    body, kind = ANTHROPIC_REPLY, "text/event-stream"
                else:
                    body, kind = self.openai_reply, "text/event-stream"
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body[: self.cut_after]
                )
                if self.cut_after is not None:
                    await writer.drain()
                    writer.close()
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()


def openai_compatible(port: int, like: OpenAICompatibleSpec = DEEPSEEK) -> LLM:
    return OpenAICompatibleLLM(
        OpenAICompatibleSpec(like.provider, "LOCAL_API_KEY", "m", f"http://127.0.0.1:{port}")
    )


def anthropic(port: int) -> LLM:
    return AnthropicLLM("m")


ADAPTERS = pytest.mark.parametrize(
    "build", [openai_compatible, anthropic], ids=["openai", "anthropic"]
)


@pytest.fixture(autouse=True)
def local_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")


async def reply(llm: LLM) -> tuple[str, Usage]:
    usage = Usage()
    text = "".join([t async for t in llm.stream("be brief", [Message("user", "hi")], usage)])
    return text, usage


def serve(monkeypatch: pytest.MonkeyPatch, provider: Provider) -> None:
    """The Anthropic client takes its address from the environment."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{provider.port}")


@ADAPTERS
async def test_the_second_reply_reuses_the_connection_the_first_one_opened(
    build: Callable[[int], LLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Provider() as provider:
        serve(monkeypatch, provider)
        llm = build(provider.port)

        first_text, first = await reply(llm)
        second_text, second = await reply(llm)

    assert first_text == second_text == "hi"
    assert isinstance(first.connect_ms, int), "the first reply's connection went unreported"
    assert second.connect_ms is None, "a reused connection was reported as opened"
    assert provider.connections == 1


@ADAPTERS
async def test_connecting_ahead_leaves_the_first_reply_nothing_to_open(
    build: Callable[[int], LLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Provider() as provider:
        serve(monkeypatch, provider)
        llm = build(provider.port)

        await llm.connect()
        _, usage = await reply(llm)

    assert usage.connect_ms is None
    assert provider.connections == 1


@ADAPTERS
async def test_an_idle_connection_is_kept_for_minutes_not_seconds(
    build: Callable[[int], LLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both SDKs default to 5 s, shorter than listening to almost any reply.
    Read off the pool rather than waited out."""
    async with Provider() as provider:
        serve(monkeypatch, provider)
        llm = build(provider.port)
        http = llm._client._client  # type: ignore[attr-defined]  # the SDK's httpx2 client

    assert http._transport._pool._keepalive_expiry == KEEPALIVE_SECONDS >= 60


async def test_concurrent_calls_are_each_charged_only_for_their_own_connection() -> None:
    """Calls run concurrently — a guess and a turn — and each must be
    charged only for the connections its own request opened."""
    async with Provider() as provider:
        llm = openai_compatible(provider.port)
        results = await asyncio.gather(reply(llm), reply(llm))

    opened = [usage.connect_ms for _, usage in results]
    assert all(isinstance(ms, int) for ms in opened), opened
    assert provider.connections == 2


def usage_chunk(usage: dict[str, Any]) -> bytes:
    return sse(
        (None, {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]}),
        (None, {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]}),
        (None, {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [], "usage": usage}),
        (None, "[DONE]"),
    )  # fmt: skip


async def test_a_streamed_openai_compatible_reply_reports_its_token_usage() -> None:
    """Without `include_usage` a stream reports nothing; with it, one final chunk
    with no choices carries the counts — DeepSeek's cache hits included."""
    reported = {"prompt_tokens": 1390, "completion_tokens": 3, "prompt_cache_hit_tokens": 1152}
    async with Provider(openai_reply=usage_chunk(reported)) as provider:
        llm = openai_compatible(provider.port)
        usage = Usage()
        text = [
            fragment async for fragment in llm.stream("be brief", [Message("user", "hi")], usage)
        ]

    assert text == ["Hel", "lo"]
    assert provider.requests[0]["stream_options"] == {"include_usage": True}
    assert (usage.prompt_tokens, usage.cached_tokens, usage.output_tokens) == (1390, 1152, 3)


async def test_openai_nests_its_cache_hits_differently_and_is_read_too() -> None:
    reported = {
        "prompt_tokens": 900,
        "completion_tokens": 12,
        "prompt_tokens_details": {"cached_tokens": 768},
    }
    async with Provider(openai_reply=usage_chunk(reported)) as provider:
        _, usage = await reply(openai_compatible(provider.port, OPENAI))

    assert (usage.prompt_tokens, usage.cached_tokens, usage.output_tokens) == (900, 768, 12)


async def test_an_error_sent_inside_the_stream_fails_the_reply() -> None:
    body = sse((None, {"error": {"message": "overloaded"}}))
    async with Provider(openai_reply=body) as provider:
        with pytest.raises(ProviderError, match="overloaded"):
            await reply(openai_compatible(provider.port))


async def test_a_connection_lost_mid_reply_fails_as_a_provider_error() -> None:
    """Not wrapped by the SDK, since the body is read here: left raw, it would
    escape every handler that expects a provider to fail as a ProviderError."""
    async with Provider(cut_after=len(OPENAI_REPLY) // 2) as provider:
        with pytest.raises(ProviderError, match="interrupted"):
            await reply(openai_compatible(provider.port))


@ADAPTERS
async def test_a_request_the_sdk_retried_says_how_many_attempts_it_took(
    build: Callable[[int], LLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal retried with a backoff is invisible otherwise: the turn is just slow."""
    async with Provider(refuse=2) as provider:
        serve(monkeypatch, provider)
        llm = build(provider.port)
        await llm.connect()
        text, usage = await reply(llm)

    assert text == "hi"
    assert usage.attempts == 3
    assert isinstance(usage.accepted_ms, int)


@ADAPTERS
async def test_a_request_accepted_first_time_took_one_attempt(
    build: Callable[[int], LLM], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Provider() as provider:
        serve(monkeypatch, provider)
        llm = build(provider.port)
        await llm.connect()
        _, usage = await reply(llm)

    assert usage.attempts == 1
    assert isinstance(usage.accepted_ms, int) and usage.accepted_ms >= 0


async def test_a_model_lookup_a_turn_makes_itself_is_not_counted_as_its_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a startup connect, an Anthropic turn looks the model up first. The
    connection that opens is the turn's cost; the lookup is not a retry."""
    async with Provider() as provider:
        serve(monkeypatch, provider)
        _, usage = await reply(anthropic(provider.port))

    assert usage.attempts == 1
    assert isinstance(usage.connect_ms, int)
