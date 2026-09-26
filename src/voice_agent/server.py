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
import re
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

from voice_agent import roles, trace, vad
from voice_agent.backends import Backends
from voice_agent.channel import Channel
from voice_agent.config import DEFAULT_GREETING, Settings, build_prompt, load_settings
from voice_agent.conversation import Conversation, Message
from voice_agent.errors import ConfigError, SessionNotFoundError, VoiceAgentError
from voice_agent.events import Playback, Typed
from voice_agent.greeting import greet
from voice_agent.initiative import LADDER, Rung
from voice_agent.limits import Live, MintLimit, budget_reason, client_address
from voice_agent.llm import LLM, create_llm
from voice_agent.llm.traced import Traced
from voice_agent.record import Record
from voice_agent.session import Session
from voice_agent.sessions import SessionStore
from voice_agent.stt import STT
from voice_agent.stt.registry import NO_EARS
from voice_agent.thinker import THINKER_MODEL, system_prompt
from voice_agent.tts import TTS
from voice_agent.tts.base import SAMPLE_RATE
from voice_agent.tts.registry import NO_VOICE

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


def load_roles(preselected: str) -> dict[str, roles.Role]:
    """Every role card, read and validated at startup: a broken card stops the
    server with a reason, rather than failing the conversation that picks it.
    So does pre-selecting a card that is not there. `none` is the operator's
    plain assistant: never offered on the page, but it may be pre-selected."""
    cards = {slug: roles.load(slug) for slug in roles.available()}
    if preselected != roles.NO_ROLE and preselected not in cards:
        roles.load(preselected)  # raises, naming the cards there are
    return cards


def thinker_engine(cards: dict[str, roles.Role]) -> LLM | None:
    """The inner voice's own engine, apart from the reply engine, shared by
    every conversation that picks a role. Without an Anthropic key a role
    still plays, but thinks nothing, and the log says so."""
    if not cards:
        return None
    try:
        return Traced(create_llm("anthropic", THINKER_MODEL))
    except ConfigError as exc:
        logger.warning("roles will have no inner voice: %s", exc)
        return None


async def refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Accept, then close with a reason. Closing before accepting gives the
    browser a 1006 with no reason at all. `reason` must fit in 123 bytes."""
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


async def expire(after: float, session: Session, websocket: WebSocket) -> None:
    """End a conversation that ran out of budget, and hang up. The socket must
    be closed too: a silent browser sends nothing that would end the loop."""
    await asyncio.sleep(after)
    await session.finish(budget_reason(after))
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


def record_for(directory: Path | None, conversation: Conversation, prompt: str) -> Record | None:
    """One file per conversation, named to sort by time, and reopened on resume.
    Kept on the conversation, which lives exactly as long as its link does."""
    if directory is None:
        return None
    if conversation.record is None:
        stamp = f"{datetime.now():%Y-%m-%d-%H%M%S}"
        conversation.record = directory / f"{stamp}-{conversation.id}.md"
    running = trace.current()
    return Record(
        conversation.record,
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
    backends: Backends
    openings: dict[str, str]
    """The first line, per role: the plain greeting under `roles.NO_ROLE`, and
    each card's own opening."""
    ladder: tuple[Rung, ...]
    roles: dict[str, roles.Role]
    thinker: LLM | None
    thinker_prompts: dict[str, str]
    """The inner voice's instructions per role: fixed for the process."""
    record_dir: Path | None
    ready: bool = False
    """The engines are connected; `/healthz` waits on it."""

    def facts(
        self,
        listener: STT | None,
        speaker: TTS | None,
        engine: str,
        ears: str,
        role: str,
        voice: str,
    ) -> dict[str, object]:
        """What this agent is, for the page before the socket and for `ready`
        after it — one function, so the two cannot disagree."""
        card = self.roles.get(role)
        return {
            "role": (
                {"slug": card.slug, "name": card.name, "summary": card.summary} if card else None
            ),
            "voice": (
                {
                    "provider": speaker.provider,
                    "voice": speaker.voice,
                    "model": speaker.model,
                    "choice": voice,
                    "sample_rate": SAMPLE_RATE,
                }
                if speaker
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
            "choices": self.backends.choices(engine, ears, role, voice),
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
    role: str | None = None,
    thinker: LLM | None = None,
) -> FastAPI:
    """The app. The keyword arguments are test seams: `llm`/`stt`/`tts` inject
    fakes, `voice=False`/`ears=False` run silent or deaf (as `…_TTS=none` and
    `…_STT=none` do), and the rest override one setting each. An injected
    `llm` gets no inner voice unless a `thinker` is injected too: a fake's
    canned replies are for turns."""
    settings = settings or load_settings()
    preselected = settings.role if role is None else role
    cards = load_roles(preselected)
    if thinker is not None:
        inner: LLM | None = Traced(thinker) if cards else None
    else:
        inner = thinker_engine(cards) if llm is None else None
    plain = greeting if greeting is not None else settings.greeting
    backends = Backends(
        settings.ears_provider,
        silence=settings.vad_silence,
        hears=ears,
        roles=tuple(cards.values()),
        default_role=preselected,
        engine=llm,
        ears=stt,
        voice_provider=settings.voice_provider if voice else NO_VOICE,
        voice=settings.voice,
        speaker=tts,
    )
    # The default voice is built now: a missing synthesis key stops the server
    # here, as it did before voices were a choice, not every page load after.
    backends.speaker(backends.default_voice)
    agent = Agent(
        settings=settings,
        sessions=store if store is not None else SessionStore(settings.max_stored),
        live=Live(settings.max_live),
        mints=MintLimit(settings.mints_per_ip),
        backends=backends,
        openings={
            roles.NO_ROLE: (DEFAULT_GREETING if plain is None else plain).strip(),
            **{slug: card.opening.strip() for slug, card in cards.items()},
        },
        ladder=ladder_for(settings.initiative if initiative is None else initiative),
        roles=cards,
        thinker=inner,
        thinker_prompts={slug: system_prompt(card) for slug, card in cards.items()},
        record_dir=(sessions_dir or settings.sessions) if record else None,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Before anyone is waiting: every offered engine's first request pays DNS and TLS
        # (`connect` only lists models, which nobody bills). The VAD model loads
        # here too, off the loop, rather than inside the first user's `listen`.
        await asyncio.gather(
            asyncio.to_thread(vad.load),
            *(connect(backends.engine(choice.name)) for choice in backends.menu),
            *([connect(agent.thinker)] if agent.thinker is not None else []),
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
        ears_default = backends.default_ears
        listener = backends.ears(ears_default) if ears_default != NO_EARS else None
        speaker = backends.speaker(backends.default_voice)
        known = agent.facts(
            listener,
            speaker,
            backends.default_engine,
            ears_default,
            backends.default_role,
            backends.default_voice,
        )
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
    engine_name, ears_name, role_name, voice_name = agent.backends.choose(
        conversation, websocket.query_params
    )
    engine = agent.backends.engine(engine_name)
    listener = agent.backends.ears(ears_name)
    speaker = agent.backends.speaker(voice_name)
    role = agent.roles.get(role_name)
    # One line per connect naming what it runs on, so the Fly logs can say
    # which settings a misbehaving conversation had without its record.
    logger.info(
        "conversation %s: role=%s llm=%s (%s/%s) ears=%s voice=%s",
        conversation.id,
        role_name,
        engine_name,
        engine.provider,
        engine.model,
        ears_name if listener is not None else "deaf",
        f"{speaker.provider}/{speaker.model}/{speaker.voice} ({voice_name})"
        if speaker
        else "silent",
    )
    opening = agent.openings[role_name if role is not None else roles.NO_ROLE]
    # Per conversation, because it states what these ears can hear; stable for
    # the conversation's life, which is what a provider's prefix cache needs.
    system_prompt = build_prompt(
        listener.languages if listener else (),
        agent.settings.voice_gender,
        tuple(rung.after for rung in agent.ladder),
        greeting=opening,
        role=role.when_speaking if role is not None else "",
    )
    recording = record_for(agent.record_dir, conversation, system_prompt)
    channel = Channel(websocket, recording)
    session = Session(
        channel,
        conversation,
        engine,
        speaker,
        system_prompt,
        listener,
        agent.ladder,
        role=role,
        thinker=agent.thinker if role is not None else None,
        thinker_prompt=agent.thinker_prompts.get(role_name),
    )
    await channel.send_json(
        {
            "type": "ready",
            "session": conversation.id,
            "provider": engine.provider,
            "model": engine.model,
            # The menu option, so the record says which of the six ran: three
            # DeepSeek tiers share a provider and a model.
            "choice": engine_name,
            **agent.facts(listener, speaker, engine_name, ears_name, role_name, voice_name),
            # From this connection: the page counts down the last minute.
            "budget_seconds": agent.settings.session_budget,
            "history": serialize(conversation.messages),
            "ended": conversation.ended,
        }
    )
    session.voiced(await greet(channel, conversation, speaker, opening))
    # After the greeting, so the clock measures the silence after the agent's voice.
    session.start()
    budget_seconds = agent.settings.session_budget
    budget = (
        asyncio.create_task(expire(budget_seconds, session, websocket))
        if budget_seconds is not None
        else None
    )
    ending = asyncio.ensure_future(session.ended.wait())
    try:
        # A reconnect to an ended conversation gets its history and nothing more.
        # Otherwise the loop runs until `end()` has finished, not merely begun.
        already_over = conversation.ended
        while not already_over:
            # Raced with the ending: after `ended` the page sends nothing, and a
            # loop waiting on it would hold a live slot until the tab closed.
            receiving = asyncio.ensure_future(websocket.receive())
            await asyncio.wait({receiving, ending}, return_when=asyncio.FIRST_COMPLETED)
            if not receiving.done():
                receiving.cancel()
                with contextlib.suppress(RuntimeError):
                    await websocket.close()  # possibly closed already, by the time limit
                break
            message = receiving.result()
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
        ending.cancel()
        if budget is not None:
            budget.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await budget
        await session.close()
        if recording is not None:
            recording.close()


CLIENT_WORD = re.compile(r"[^\w .:/()-]")


def client_note(kind: str, payload: dict[str, object]) -> str:
    """The page's report as one line, each field cut short and stripped of
    anything that could forge a line in the record."""

    def field(key: str, limit: int = 40) -> str:
        value = payload.get(key)
        if isinstance(value, bool):
            return "yes" if value else "no"
        return CLIENT_WORD.sub("", str(value))[:limit] if value is not None else ""

    if kind == "client":
        in_app = field("in_app")
        where = f" · inside {in_app}" if in_app and in_app != "None" else ""
        mobile = " · mobile" if payload.get("mobile") is True else ""
        return f"client: {field('browser')} on {field('os')}{mobile}{where}"
    return f"client error: {field('what')} · {field('name')} · {field('message', 160)}"


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
        session.post(Playback(bool(payload.get("active"))))
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

    if kind in ("client", "client_error"):
        # What the page knows and the server cannot: which browser this is, and
        # a microphone that was refused. Written down so a phone that fails is
        # visible afterwards. Client-supplied: short words only, never logged raw.
        if record is not None:
            record.note(client_note(kind, payload))
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
        session.post(Typed(text))
