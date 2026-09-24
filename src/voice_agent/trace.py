"""The technical trace: one machine-readable file per run, as a span tree.

Where `record.py` writes the conversation for a person to read, this writes what
the machine did for a program to read — every provider request and response with
its full body, every log line, and the timings around them.

**A span tree**, because the latency budget (`AGENTS.md` §7) is one: spans carry
a parent, so which synthesis belonged to which turn is in the data.

**OpenTelemetry-shaped, not OpenTelemetry.** The vocabulary is theirs (`span`,
`parent`, `gen_ai.*`); the transport is a local file, because this holds whole
prompts and replies, which OTel backends truncate. Exporting over OTLP would be
a mapping over data already shaped for it.

Spans are written as **two lines, start and end**. A trace written for debugging
has to make a *hang* visible, and an unpaired start is exactly that: the span
that never finished, which is the one being looked for. It also means a process
killed mid-turn still leaves everything up to that moment.
"""

import contextlib
import json
import logging
import os
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

from voice_agent import timing

logger = logging.getLogger(__name__)

TRACES_DIR = Path("traces")

REDACTED = "***"
SECRET_SUBSTRINGS = (
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "password",
    "passwd",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "bearer",
    "credential",
    "private_key",
)
"""Key names that are unambiguously credentials wherever they appear.

Precise on purpose. The first version matched `token` as a substring, which
redacted `gen_ai.usage.output_tokens` — every token count in the file, which is
most of what the file is for. A rule that destroys the data it was protecting is
not a safe default; it is a broken one."""

SECRET_KEYS = frozenset({"key", "token", "auth", "authorization"})
"""Names that are a credential as the *whole* key and something else as part of
a longer one."""

KEY_SHAPES = ("sk-", "xi-", "bearer ")
"""A net under the names: a value that looks like a credential is redacted
whoever put it there.

The names above only catch what somebody thought to name carefully. This catches
the one that arrives under `params`, or in a field a vendor added last week.
Limited to values that are one long unbroken token, so prose that merely
mentions an API key is left alone."""


def key_shaped(value: str) -> bool:
    text = value.strip()
    bare = text.removeprefix("Bearer ").removeprefix("bearer ")
    return len(text) > 20 and " " not in bare and text.casefold().startswith(KEY_SHAPES)


MAX_STRING = 20_000
"""A single string longer than this is truncated. Whole prompts are the point of
this file, so the limit is generous — it exists to stop one runaway value
turning a line into a megabyte."""


def secretish(key: str) -> bool:
    lowered = key.casefold()
    return lowered in SECRET_KEYS or any(h in lowered for h in SECRET_SUBSTRINGS)


def scrub(value: Any, key: str = "") -> Any:
    """Redact secrets and make the value JSON-safe, recursively.

    Applied to everything on its way out rather than at each call site: a trace
    that leaks a key once has leaked it, and remembering to redact at twelve
    call sites is a thing that works until it does not.
    """
    if key and secretish(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        if key_shaped(value):
            return REDACTED
        if len(value) > MAX_STRING:
            return f"{value[:MAX_STRING]}…(+{len(value) - MAX_STRING})"
        return value
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)


class Trace:
    """One run's trace file. Every write is best-effort: a trace that cannot be
    written must never take down the thing it was watching."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: Any = None
        self._broken = False

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> Any:
        if self._file is None and not self._broken:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._file = self._path.open("a", encoding="utf-8")
            except OSError as exc:
                self._broken = True
                logger.warning("not writing a trace: %s", exc)
        return self._file

    def emit(self, kind: str, fields: Mapping[str, Any] | None = None) -> None:
        handle = self._open()
        if handle is None:
            return
        now = timing.now()
        current = _span.get()
        event: dict[str, Any] = {
            "kind": kind,
            "at": datetime.now().strftime("%H:%M:%S.%f")[:-3],
            "mono": round(now, 6),
        }
        if current is not None:
            event["trace"] = current.trace
            event.setdefault("span", current.id)
        event.update({k: scrub(v, k) for k, v in (fields or {}).items()})
        try:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
        except (OSError, TypeError, ValueError) as exc:
            self._broken = True
            logger.warning("trace writing stopped: %s", exc)

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None


class Span:
    """A node in the tree: an id, its parent, and the trace it belongs to."""

    __slots__ = ("id", "parent", "trace")

    def __init__(self, trace: str, parent: str | None) -> None:
        self.id = token_hex(4)
        self.parent = parent
        self.trace = trace


_span: ContextVar[Span | None] = ContextVar("trace_span", default=None)
_trace: ContextVar[Trace | None] = ContextVar("trace_file", default=None)


def install(trace: Trace | None) -> None:
    """Make this the trace for everything that follows on this task."""
    _trace.set(trace)


def current() -> Trace | None:
    return _trace.get()


@contextlib.contextmanager
def span(
    name: str, attrs: Mapping[str, Any] | None = None, trace_id: str | None = None
) -> Iterator[Span]:
    """Open a span around a block, nested under whatever is already open.

    The parent is found rather than passed: a `ContextVar` is how OTel
    propagates context, and `llm/http.py` already uses the same mechanism for
    `Call`, so this is the project's own idiom rather than an import.
    """
    parent = _span.get()
    node = Span(trace_id or (parent.trace if parent else "-"), parent.id if parent else None)
    writer = _trace.get()
    _span.set(node)
    if writer is not None:
        writer.emit("span.start", {"name": name, "parent": node.parent, **(attrs or {})})
    started = timing.now()
    failure: BaseException | None = None
    try:
        yield node
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if writer is not None:
            ended: dict[str, Any] = {
                "name": name,
                "ms": round((timing.now() - started) * 1000, 3),
            }
            if failure is not None:
                ended["error"] = f"{type(failure).__name__}: {failure}"
            writer.emit("span.end", ended)
        # Restored, not `reset(token)`. This context manager spans a `yield`
        # inside an async generator, and a generator finalized by the event
        # loop's hook rather than by its caller unwinds in a *different*
        # context — where `ContextVar.reset` raises. Setting the old value back
        # is equivalent here, since the default is `None`, and cannot raise.
        _span.set(parent)


def event(kind: str, attrs: Mapping[str, Any] | None = None) -> None:
    """Something with no duration: a chunk arrived, a session reconnected."""
    writer = _trace.get()
    if writer is not None:
        writer.emit(kind, attrs)


class TraceHandler(logging.Handler):
    """Every `logger.*` call, into the same file as the spans.

    One file and one format means one thing to grep.
    """

    def emit(self, record: logging.LogRecord) -> None:
        writer = _trace.get()
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.emit(
                "log",
                {
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                    "exc": self.format(record) if record.exc_info else None,
                },
            )


def open_trace(directory: Path | None) -> Trace | None:
    if directory is None:
        return None
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return Trace(directory / f"{stamp}-{os.getpid()}.jsonl")
