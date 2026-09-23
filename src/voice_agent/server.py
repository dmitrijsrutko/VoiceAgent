"""The WebSocket server and the conversation loop that runs over it.

One connection per conversation, carrying four things: control messages as
JSON in both directions, microphone audio as binary frames going up, and
synthesized speech as binary frames coming down. The link in the URL is the
conversation: reconnecting to it replays the history and carries on.

The agent is bi-capable. A turn can be started by typing or by speaking, and
by the time a turn begins nothing downstream knows which it was: speech
becomes text at the edge.

It is **full-duplex**: the browser keeps sending microphone audio while the
agent speaks, and the user can talk over it. The browser's echo cancellation is
what keeps the agent from hearing, and interrupting, its own voice.

It is also **mixed-initiative**: once the greeting is out, a per-session clock
considers whether a silence is worth speaking into, and usually decides not.
"""

import asyncio
import contextlib
import json
import logging
import math
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from voice_agent import trace
from voice_agent.channel import Channel
from voice_agent.config import Settings, build_prompt, load_settings
from voice_agent.conversation import Conversation, Message
from voice_agent.errors import ConfigError, SessionNotFoundError, VoiceAgentError
from voice_agent.greeting import Greeting
from voice_agent.initiative import LADDER, Rung
from voice_agent.limits import Live, MintLimit, budget_reason, client_address
from voice_agent.llm import LLM
from voice_agent.pool import Pool, Stack
from voice_agent.record import Record
from voice_agent.session import Session
from voice_agent.sessions import SessionStore
from voice_agent.stt import STT
from voice_agent.stt.registry import NO_EARS
from voice_agent.tts import TTS, create_tts
from voice_agent.tts.base import SAMPLE_RATE

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
PAGE_PATH = WEB_DIR / "index.html"


def serialize(messages: Sequence[Message]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


FACTS_BLOCK = '<script id="facts" type="application/json">{}</script>'
"""Where `chat_page` writes what this agent is, for the page to read before it
has a socket. Empty by default, so a page served any other way claims nothing."""


def with_facts(page: str, known: dict[str, object]) -> str:
    """Put the facts into the page. `<` is escaped so a `</script>` in any
    value cannot end the block early."""
    payload = json.dumps(known).replace("<", "\\u003c")
    return page.replace(FACTS_BLOCK, FACTS_BLOCK.replace("{}", payload), 1)


class PageModules(StaticFiles):
    """The page's scripts, revalidated on every load.

    Without `Cache-Control` a reload can pair a fresh `app.js` with a stale
    `player.js`, and a module importing a name its cached neighbour lacks fails
    to link. `no-cache` still allows a 304.
    """

    def file_response(
        self,
        full_path: os.PathLike[str] | str,
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response


async def connect(engine: LLM) -> None:
    """Only a head start: a connection that fails now is retried by the first turn."""
    try:
        await engine.connect()
    except VoiceAgentError as exc:
        logger.warning("could not connect to %s before the first turn: %s", engine.provider, exc)


async def refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Accept, then close with a reason. Closing before accepting gives the
    browser a 1006 with no reason at all. `reason` must fit in 123 bytes."""
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


async def expire(after: float, session: Session, websocket: WebSocket) -> None:
    """End a conversation that ran out of budget, and hang up. The socket must
    be closed too: a silent browser sends nothing that would end the loop."""
    await asyncio.sleep(after)
    await session.end(budget_reason(after))
    with contextlib.suppress(RuntimeError):
        await websocket.close()  # possibly already closed by the other side


def ladder_for(delays: Sequence[float]) -> tuple[Rung, ...]:
    """The configured delays on the fixed rungs. More delays than rungs is an
    error rather than silently dropped."""
    if len(delays) > len(LADDER):
        raise ConfigError(
            f"VOICE_AGENT_INITIATIVE has {len(delays)} delays but there are "
            f"only {len(LADDER)} rungs to put them on."
        )
    return tuple(
        Rung(after, rung.intent, rung.disposition)
        for after, rung in zip(delays, LADDER, strict=False)
    )


def record_for(directory: Path | None, conversation_id: str, prompt: str) -> Record | None:
    """One file per conversation, named to sort by time, and reopened on resume."""
    if directory is None:
        return None
    # The stem's tail, not a glob: `*-{id}` would also match an id ending in this one.
    existing = sorted(
        path
        for path in directory.glob("*.md")
        if path.stem.endswith(f"-{conversation_id}") and len(path.stem) == len(conversation_id) + 18
    )
    if existing:
        path = existing[-1]
    else:
        path = directory / f"{datetime.now():%Y-%m-%d-%H%M%S}-{conversation_id}.md"
    running = trace.current()
    return Record(
        path,
        prompt_id=f"system_prompt.md@{sha256(prompt.encode()).hexdigest()[:7]}",
        trace=running.path.name if running is not None else "",
    )


@dataclass
class Agent:
    """Everything one process serves conversations with."""

    settings: Settings
    sessions: SessionStore
    live: Live
    mints: MintLimit
    speaker: TTS | None
    stack: Stack
    pool: Pool
    opening: Greeting
    ladder: tuple[Rung, ...]
    record_dir: Path | None
    ready: bool = False
    """The greeting is synthesised and the engines connected; `/healthz` waits on it."""

    def facts(self, listener: STT | None, engine: str, ears: str) -> dict[str, object]:
        """What this agent is, for the page before the socket and for `ready`
        after it — one function, so the two cannot disagree."""
        return {
            "voice": (
                {
                    "provider": self.speaker.provider,
                    "voice": self.speaker.voice,
                    "sample_rate": SAMPLE_RATE,
                }
                if self.speaker
                else None
            ),
            "ears": (
                {
                    "provider": listener.provider,
                    "sample_rate": listener.sample_rate,
                    "languages": list(listener.languages),
                }
                if listener
                else None
            ),
            "recording": self.record_dir is not None,
            "choices": self.stack.choices(engine, ears),
        }


def create_app(
    llm: LLM | None = None,
    store: SessionStore | None = None,
    tts: TTS | None = None,
    stt: STT | None = None,
    voice: bool = True,
    ears: bool = True,
    greeting: str | None = None,
    initiative: Sequence[float] | None = None,
    sessions_dir: Path | None = None,
    record: bool = True,
    settings: Settings | None = None,
) -> FastAPI:
    """The app. The keyword arguments are test seams: `llm`/`stt`/`tts` inject
    fakes, `voice=False`/`ears=False` run silent or deaf (as `…_TTS=none` and
    `…_STT=none` do), and the rest override one setting each."""
    settings = settings or load_settings()
    speaker = (
        tts if tts is not None or not voice else create_tts(settings.voice_provider, settings.voice)
    )
    stack = Stack(settings.provider, settings.model, settings.ears_provider, hears=ears)
    agent = Agent(
        settings=settings,
        sessions=store if store is not None else SessionStore(settings.max_stored),
        live=Live(settings.max_live),
        mints=MintLimit(settings.mints_per_ip),
        speaker=speaker,
        stack=stack,
        pool=Pool(stack.model_for, settings.vad_silence, engine=llm, ears=stt),
        opening=Greeting(settings.greeting if greeting is None else greeting, speaker),
        ladder=ladder_for(settings.initiative if initiative is None else initiative),
        record_dir=(sessions_dir or settings.sessions) if record else None,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Before anyone is waiting: the first synthesis in a process takes
        # seconds, and every offered engine's first request pays DNS and TLS
        # (`connect` only lists models, which nobody bills).
        await asyncio.gather(
            agent.opening.prepare(),
            *(connect(agent.pool.engine(name)) for name in stack.engines),
        )
        agent.ready = True
        yield

    app = FastAPI(title="voice-agent", lifespan=lifespan)
    app.mount("/static", PageModules(directory=WEB_DIR), name="static")

    @app.get("/healthz")
    async def healthz() -> Response:
        """Ready, not merely alive, so a platform never routes to a cold machine."""
        if not agent.ready:
            return Response("warming", status_code=503, media_type="text/plain")
        return Response("ok", media_type="text/plain")

    @app.get("/")
    async def new_conversation(request: Request) -> Response:
        """Mint a conversation and redirect to its link. Rate-limited per
        address, because a conversation can be connected to, and that costs."""
        address = client_address(request.headers, request.client.host if request.client else None)
        if not agent.mints.allow(address):
            return HTMLResponse(
                "<h1>429 — too many new conversations</h1>"
                "<p>This public demo limits how many conversations one address "
                "may start. Try again in a few minutes, or carry on in a link "
                "you already have.</p>",
                status_code=429,
            )
        conversation = agent.sessions.create()
        return RedirectResponse(url=f"/c/{conversation.id}", status_code=303)

    @app.get("/c/{key}")
    async def chat_page(key: str) -> HTMLResponse:
        """The page, with the facts already in it: the start screen says what
        starting entails before there is a socket to ask over."""
        try:
            agent.sessions.get(key)
        except SessionNotFoundError:
            return HTMLResponse("<h1>404 — no such conversation</h1>", status_code=404)
        ears_default = stack.default_ears
        listener = agent.pool.ears(ears_default) if ears_default != NO_EARS else None
        known = agent.facts(listener, stack.default_engine, ears_default)
        return HTMLResponse(with_facts(PAGE_PATH.read_text(encoding="utf-8"), known))

    @app.websocket("/ws/{key}")
    async def chat_socket(websocket: WebSocket, key: str) -> None:
        await serve(agent, websocket, key)

    return app


async def serve(agent: Agent, websocket: WebSocket, key: str) -> None:
    """One conversation's socket, from admission to hang-up."""
    try:
        conversation = agent.sessions.get(key)
    except SessionNotFoundError:
        await refuse(websocket, 4404, "No such conversation — start a new one from the root.")
        return
    # Counted per socket, not per page: the socket is what holds a recognizer,
    # a reasoning connection and a clock.
    if not agent.live.take():
        await refuse(
            websocket,
            4429,
            "This public demo is busy — only a few conversations at once. Try again shortly.",
        )
        return
    try:
        # One span per connection; tasks created inside inherit it.
        with trace.span("conversation", {"session": conversation.id}, trace_id=conversation.id):
            await websocket.accept()
            await converse(agent, websocket, conversation)
    finally:
        agent.live.release()


async def converse(agent: Agent, websocket: WebSocket, conversation: Conversation) -> None:
    engine_name, ears_name = agent.stack.choose(conversation, websocket.query_params)
    engine = agent.pool.engine(engine_name)
    listener = agent.pool.ears(ears_name)
    # Per conversation, because it states what these ears can hear; stable for
    # the conversation's life, which is what a provider's prefix cache needs.
    system_prompt = build_prompt(
        listener.languages if listener else (),
        agent.settings.voice_gender,
        tuple(rung.after for rung in agent.ladder),
        greeting=agent.opening.text,
    )
    recording = record_for(agent.record_dir, conversation.id, system_prompt)
    channel = Channel(websocket, recording)
    session = Session(
        channel, conversation, engine, agent.speaker, system_prompt, listener, agent.ladder
    )
    await channel.send_json(
        {
            "type": "ready",
            "session": conversation.id,
            "provider": engine.provider,
            "model": engine.model,
            **agent.facts(listener, engine_name, ears_name),
            "history": serialize(conversation.messages),
            "ended": conversation.ended,
        }
    )
    session.voiced(await agent.opening.deliver(channel, conversation))
    # After the greeting, so the clock measures the silence after the agent's voice.
    session.start()
    budget_seconds = agent.settings.session_budget
    budget = (
        asyncio.create_task(expire(budget_seconds, session, websocket))
        if budget_seconds is not None
        else None
    )
    try:
        while not conversation.ended:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if (frame := message.get("bytes")) is not None:
                if session.mic is not None:
                    session.mic.feed(frame)
            elif (raw := message.get("text")) is not None:
                await handle_text(channel, session, raw, recording)
    except WebSocketDisconnect:
        return
    finally:
        if budget is not None:
            budget.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await budget
        await session.close()
        if recording is not None:
            recording.close()


async def handle_text(
    channel: Channel, session: Session, raw: str, record: Record | None = None
) -> None:
    try:
        payload = json.loads(raw)
        kind = str(payload["type"])
    except (ValueError, KeyError, TypeError):
        await channel.send_json({"type": "error", "message": "malformed message"})
        return

    if kind == "playback":
        # The user is not expected to speak for however long the audio lasts —
        # which for a long answer is far more than the idle window — and only
        # the browser knows when playback actually ends.
        session.playback(bool(payload.get("active")))
        # Chunks that arrived after the previous one had finished playing. Only
        # the browser can see them, and a stutter nobody logs is a stutter
        # nobody fixes. Coerced first: these are client-supplied, and a string
        # logged verbatim can forge log lines.
        try:
            gaps, gap_ms = int(payload.get("gaps", 0)), int(payload.get("gap_ms", 0))
        except (TypeError, ValueError):
            return
        if gaps:
            logger.info("playback stuttered: %d gaps, %d ms of silence", gaps, gap_ms)
            if record is not None:
                record.note(f"playback: gaps {gaps} · gap_ms {gap_ms}")
        return

    if kind == "interrupted":
        # Client-supplied, so coerced: anything but a finite, non-negative
        # number of milliseconds means "nothing was playing".
        played, interrupt_id = payload.get("played_ms"), payload.get("id")
        valid = isinstance(played, int | float) and not isinstance(played, bool)
        if isinstance(interrupt_id, int) and not isinstance(interrupt_id, bool):
            session.heard(
                interrupt_id,
                float(played) if valid and math.isfinite(played) and played >= 0 else None,
            )
        return

    if kind in ("listen_start", "listen_stop"):
        mic = session.mic
        if mic is None:
            await channel.send_json({"type": "listen_error", "message": "this agent has no ears"})
            return
        # Mic announces its own state changes, so an expiry and an explicit
        # stop reach the browser through exactly one path.
        await (mic.start() if kind == "listen_start" else mic.stop())
        return

    text = str(payload.get("text", "")).strip()
    if text:
        # Typed input is the one thing `Channel` cannot see: the page already
        # has it and the server never echoes it back. A spoken turn arrives as
        # a committed `transcript` frame and is recorded there.
        if record is not None:
            record.said(text)
        await session.submit(text)
