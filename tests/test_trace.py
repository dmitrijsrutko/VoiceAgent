"""The technical trace: the span tree, the bodies, and the secrets that must
never reach it."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM, FakeSTT, FakeTTS, receive
from tests.test_server import start
from voice_agent import trace
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore


@pytest.fixture
def traced(tmp_path: Path) -> Any:
    """A trace installed for the duration of one test, then taken away again —
    a `ContextVar` left set would leak into whatever ran next."""
    writer = trace.open_trace(tmp_path / "traces")
    assert writer is not None
    trace.install(writer)
    yield writer
    writer.close()
    trace.install(None)


def lines(writer: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in writer.path.read_text().splitlines() if line.strip()]


def test_a_span_is_written_as_a_start_and_an_end(traced: Any) -> None:
    with trace.span("turn", {"said": "hello"}, trace_id="c1"):
        pass

    start_line, end_line = lines(traced)
    assert start_line["kind"] == "span.start" and start_line["name"] == "turn"
    assert start_line["said"] == "hello" and start_line["trace"] == "c1"
    assert end_line["kind"] == "span.end" and end_line["span"] == start_line["span"]
    assert end_line["ms"] >= 0


def test_a_spans_start_is_on_disk_before_its_body_runs(traced: Any) -> None:
    """Why a span is two lines rather than one written on close.

    A process killed mid-turn still leaves the start, so the span that never
    finished shows up as the one with no end — which is exactly what you are
    looking for when something hangs. Asserted by reading the file from inside
    the span, since a real death cannot be staged in-process.
    """
    with trace.span("turn", trace_id="c1"):
        written = lines(traced)
        assert [line["kind"] for line in written] == ["span.start"]

    assert [line["kind"] for line in lines(traced)] == ["span.start", "span.end"]


def test_a_span_that_raises_records_what_went_wrong(traced: Any) -> None:
    with pytest.raises(RuntimeError), trace.span("turn", trace_id="c1"):
        raise RuntimeError("wedged")

    assert "RuntimeError: wedged" in lines(traced)[-1]["error"]


def test_spans_nest_under_whatever_is_already_open(traced: Any) -> None:
    """Which synthesis belonged to which turn is answered by the data, not by
    reading timestamps and guessing."""
    with trace.span("turn", trace_id="c1") as turn:
        with trace.span("llm"):
            pass
        with trace.span("tts"):
            pass

    starts = {line["name"]: line for line in lines(traced) if line["kind"] == "span.start"}
    assert starts["llm"]["parent"] == turn.id
    assert starts["tts"]["parent"] == turn.id
    assert starts["turn"]["parent"] is None
    assert {line["trace"] for line in lines(traced)} == {"c1"}


def test_a_conversation_traces_the_prompt_and_the_reply(traced: Any, tmp_path: Path) -> None:
    llm = FakeLLM(["the answer"])
    client = TestClient(
        create_app(
            llm=llm,
            tts=FakeTTS(),
            stt=FakeSTT(script=[]),
            store=SessionStore(),
            greeting="",
            sessions_dir=tmp_path / "s",
        )
    )
    key = start(client)
    with client.websocket_connect(f"/ws/{key}") as socket:
        receive(socket)
        socket.send_json({"type": "user_message", "text": "the question"})
        while receive(socket).get("type") != "reply_end":
            pass

    written = lines(traced)
    llm_start = next(w for w in written if w.get("name") == "llm" and w["kind"] == "span.start")
    assert llm_start["gen_ai.request.model"] == "fake-1"
    assert any(m["content"] == "the question" for m in llm_start["messages"])
    reply = next(w for w in written if w["kind"] == "llm.reply")
    assert reply["text"].strip() == "the answer"
    assert reply["gen_ai.usage.output_tokens"] > 0
    turn = next(w for w in written if w.get("name") == "turn" and w["kind"] == "span.start")
    assert llm_start["parent"] == turn["span"]


@pytest.mark.parametrize(
    "key",
    ["api_key", "API_KEY", "xi-api-key", "Authorization", "access_token", "db_password", "token"],
)
def test_a_secret_never_reaches_the_file(traced: Any, key: str) -> None:
    """A trace that leaks a key once has leaked it. Redaction happens on the way
    out rather than at each call site, because remembering at twelve call sites
    is a thing that works until it does not."""
    with trace.span("call", {key: "sk-live-do-not-log-me", "nested": {key: "also-secret"}}):
        pass

    body = traced.path.read_text()
    assert "sk-live-do-not-log-me" not in body
    assert "also-secret" not in body
    assert trace.REDACTED in body


def test_a_log_call_lands_in_the_same_file(traced: Any) -> None:
    """Until this chapter nothing configured logging at all, so the project's
    eight `logger.info` calls went nowhere. This is the guard."""
    handler = trace.TraceHandler()
    log = logging.getLogger("voice_agent.test")
    log.setLevel(logging.DEBUG)
    log.addHandler(handler)
    try:
        log.info("something worth knowing")
    finally:
        log.removeHandler(handler)

    entry = next(line for line in lines(traced) if line["kind"] == "log")
    assert entry["msg"] == "something worth knowing"
    assert entry["logger"] == "voice_agent.test"
    assert entry["level"] == "INFO"


def test_tracing_off_writes_nothing(tmp_path: Path) -> None:
    trace.install(None)
    with trace.span("turn", trace_id="c1"):
        trace.event("llm.reply", {"text": "x"})
    assert not list(tmp_path.glob("**/*.jsonl"))


def test_every_line_is_valid_json_with_a_clock_and_a_monotonic_stamp(
    traced: Any,
) -> None:
    with trace.span("turn", trace_id="c1"):
        trace.event("stt.partial", {"text": "half a sen"})

    for line in lines(traced):
        assert line["kind"] and line["at"] and isinstance(line["mono"], float)


def test_a_runaway_value_is_truncated_rather_than_swallowing_the_line(
    traced: Any,
) -> None:
    with trace.span("turn", {"huge": "x" * (trace.MAX_STRING + 500)}, trace_id="c1"):
        pass

    start_line = lines(traced)[0]
    assert len(start_line["huge"]) < trace.MAX_STRING + 100
    assert start_line["huge"].endswith("(+500)")


def test_a_trace_that_cannot_be_written_does_not_break_anything(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")
    writer = trace.Trace(blocked / "trace.jsonl")
    trace.install(writer)
    try:
        with trace.span("turn", trace_id="c1"):
            trace.event("llm.reply", {"text": "x"})
    finally:
        trace.install(None)

    assert "not writing a trace" in caplog.text


@pytest.mark.parametrize("key", ["prompt_tokens", "gen_ai.usage.output_tokens", "max_tokens"])
def test_token_counts_are_not_mistaken_for_credentials(traced: Any, key: str) -> None:
    """The first redaction rule matched `token` as a substring and blanked every
    token count in the file — which is most of what the file is for. A rule that
    destroys the data is not a safe default."""
    with trace.span("llm", {key: 1981}):
        pass

    assert lines(traced)[0][key] == 1981


def test_a_credential_is_redacted_even_under_an_innocent_name(traced: Any) -> None:
    """The net under the key names: the one that lands in `params`, or in a field
    a vendor added last week."""
    with trace.span("call", {"params": {"whatever": "sk-ant-api03-nEvErLoGmEnEvErLoGmE"}}):
        pass

    body = traced.path.read_text()
    assert "nEvErLoGmE" not in body and trace.REDACTED in body


def test_prose_that_merely_mentions_a_key_is_left_alone(traced: Any) -> None:
    """Redaction must not eat the conversation. Whole prompts are the point."""
    said = "You can find sk- prefixed keys in the dashboard, under settings."
    with trace.span("llm", {"messages": [{"role": "user", "content": said}]}):
        pass

    assert lines(traced)[0]["messages"][0]["content"] == said


async def test_tracing_a_stream_still_closes_the_provider(traced: Any) -> None:
    """Chapter 7 added `closing()` for one reason, written in its docstring: an
    interrupted turn leaves the provider's generator suspended at its `yield`,
    and its HTTP stream open and billed until garbage collection.

    `Traced` sits between that helper and the thing it protects. `async for`
    does not close its iterator, so a wrapper that only loops leaves the
    provider exactly as it was before Chapter 7 fixed it — and `closing()` never
    had a test, which is why wrapping it went unnoticed.
    """
    from voice_agent.llm.traced import Traced

    closed: list[str] = []

    class Provider:
        provider, model = "fake", "fake-1"

        async def connect(self) -> None: ...

        async def warm(self, system: str, messages: Any) -> Any: ...

        async def stream(self, system: str, messages: Any, usage: Any = None) -> Any:
            try:
                for word in ("one ", "two ", "three "):
                    yield word
            finally:
                closed.append("provider")

    outer: Any = Traced(Provider()).stream("system", [])
    async for _ in outer:
        break  # a turn reads one fragment and is then interrupted
    await outer.aclose()  # exactly what `closing()` does

    assert closed == ["provider"], "the provider's stream was left open, and billed"


def test_the_console_stays_quiet_while_the_trace_hears_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Putting `voice_agent` at DEBUG so its INFO reaches the trace also pushed
    every INFO line to the terminal, because propagation consults *handler*
    levels and ignores ancestor logger levels. The console's level has to be set
    on the handler, and this is what stops the two drifting apart again."""
    from voice_agent.cli import start_logging

    monkeypatch.setenv("VOICE_AGENT_TRACE", str(tmp_path / "traces"))
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        start_logging()
        assert all(h.level >= logging.WARNING for h in root.handlers if h not in before)

        log = logging.getLogger("voice_agent.mic")
        log.info("recognizer ended the session; reconnecting (1)")
        log.warning("listening failed")

        printed = capsys.readouterr().err
        assert "reconnecting" not in printed, "an INFO line reached the terminal"
        assert "listening failed" in printed, "a warning did not reach the terminal"

        written = (tmp_path / "traces").glob("*.jsonl")
        body = "".join(f.read_text() for f in written)
        assert "reconnecting" in body, "the INFO line did not reach the trace"
    finally:
        for handler in list(root.handlers):
            if handler not in before:
                root.removeHandler(handler)
        project = logging.getLogger("voice_agent")
        project.handlers.clear()
        project.setLevel(logging.NOTSET)
        trace.install(None)
