"""The WebSocket server and the conversation loop that runs over it.

One connection per conversation, carrying four things: control messages as
JSON in both directions, microphone audio as binary frames going up, and
synthesized speech as binary frames coming down. The link in the URL is the
conversation: reconnecting to it replays the history and carries on.

The agent is bi-capable. A turn can be started by typing or by speaking, and
by the time a turn begins nothing downstream knows which it was — speech
becomes text at the edge, and the rest of the pipeline is unchanged from the
chapter that had no ears.

It is **full-duplex**: the browser keeps sending microphone audio while the
agent speaks, and the user can talk over it. The browser's echo cancellation is
what keeps the agent from hearing, and interrupting, its own voice.

It is also **mixed-initiative**: a turn no longer has to be started by the user.
Once the greeting is out, a per-session clock starts considering whether the
silence is worth speaking into — and usually decides it is not.
"""

import asyncio
import contextlib
import json
import logging
import math
import os
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from voice_agent import trace
from voice_agent.channel import Channel
from voice_agent.config import load_settings, load_system_prompt, with_voice_gender
from voice_agent.conversation import Message
from voice_agent.errors import ConfigError, SessionNotFoundError, VoiceAgentError
from voice_agent.greeting import Greeting
from voice_agent.initiative import LADDER, Rung
from voice_agent.llm import LLM, create_llm
from voice_agent.llm.traced import Traced
from voice_agent.record import Record
from voice_agent.session import Session
from voice_agent.sessions import SessionStore
from voice_agent.stt import STT, create_stt
from voice_agent.tts import TTS, create_tts
from voice_agent.tts.base import SAMPLE_RATE

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
PAGE_PATH = WEB_DIR / "index.html"


def serialize(messages: Sequence[Message]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


class PageModules(StaticFiles):
    """The page's scripts, revalidated on every load.

    Without a `Cache-Control` header browsers cache these heuristically, so a
    reload after an edit can pair a fresh `app.js` with a stale `player.js` —
    and a module that imports a name its cached neighbour lacks fails to link,
    leaving a page that silently does nothing. `no-cache` still lets the ETag
    answer with a 304; it only forbids using a copy without asking.
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
    """Only a head start: a connection that cannot be opened now is opened by
    the first turn instead, and that turn reports the failure if it persists."""
    try:
        await engine.connect()
    except VoiceAgentError as exc:
        logger.warning("could not connect to %s before the first turn: %s", engine.provider, exc)


def ladder_for(delays: Sequence[float]) -> tuple[Rung, ...]:
    """The escalation, with the configured delays on the written-down rungs.

    Only the *when* is configurable. What each rung is for — offer something
    concrete, then withdraw — and how readily it should be taken are the parts
    that make the agent tolerable to sit with, and they are not knobs.

    Surplus delays are refused rather than dropped. Asking for four and silently
    getting two is the kind of quiet disagreement between what was configured
    and what is running that this project rejects everywhere else — `_delays`
    already refuses to repair a malformed value.
    """
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
    """One file per conversation, named so the folder sorts by time.

    Reopened rather than recreated when a link is resumed: the same
    conversation is the same file, and `Record` notices and says "reconnected"
    instead of writing a second header.
    """
    if directory is None:
        return None
    # Matched on the stem's tail rather than by glob: `*-{id}` would also match
    # a conversation whose id merely ends with this one.
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
        # So the readable file says which machine-readable one to open next.
        trace=running.path.name if running is not None else "",
    )


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
) -> FastAPI:
    """`voice=False` / `ears=False` run the agent silent or deaf, which is also
    what VOICE_AGENT_TTS=none and VOICE_AGENT_STT=none do. Neither capability
    may be load-bearing for the other, or for typing."""
    settings = load_settings()
    # Wrapped here rather than in `create_llm`, so a faked provider injected by
    # a test is traced exactly like a real one.
    engine = Traced(llm if llm is not None else create_llm(settings.provider, settings.model))
    sessions = store if store is not None else SessionStore()
    speaker: TTS | None = tts
    if speaker is None and voice:
        speaker = create_tts(settings.voice_provider, settings.voice)
    listener: STT | None = stt
    if listener is None and ears:
        listener = create_stt(settings.ears_provider, settings.vad_silence)
    system_prompt = with_voice_gender(load_system_prompt(), settings.voice_gender)
    opening = Greeting(settings.greeting if greeting is None else greeting, speaker)
    ladder = ladder_for(settings.initiative if initiative is None else initiative)
    # The same shape as `tts`/`voice` above: a place to put it, and a switch
    # that turns it off without having to name one.
    record_dir = (sessions_dir or settings.sessions) if record else None

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Synthesised once, at startup, before anyone is waiting on it. The
        # first synthesis in a process took 3.1 s against 250-290 ms for every
        # one after, and paying that on someone's first question is the worst
        # possible moment for it. The reasoning engine's connection is opened
        # alongside, for the same reason.
        await asyncio.gather(opening.prepare(), connect(engine))
        yield

    app = FastAPI(title="voice-agent", lifespan=lifespan)
    # The page's scripts, as native ES modules. Served as files rather than
    # inlined so each one can be imported — and executed — by node under test.
    app.mount("/static", PageModules(directory=WEB_DIR), name="static")

    @app.get("/")
    async def new_conversation() -> RedirectResponse:
        """Minting the key here is what makes the link the conversation:
        every page load starts a new one and lands on its own URL."""
        conversation = sessions.create()
        return RedirectResponse(url=f"/c/{conversation.id}", status_code=303)

    @app.get("/c/{key}")
    async def chat_page(key: str) -> HTMLResponse:
        try:
            sessions.get(key)
        except SessionNotFoundError:
            return HTMLResponse("<h1>404 — no such conversation</h1>", status_code=404)
        return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"))

    @app.websocket("/ws/{key}")
    async def chat_socket(websocket: WebSocket, key: str) -> None:
        try:
            conversation = sessions.get(key)
        except SessionNotFoundError:
            await websocket.close(code=4404, reason="no such conversation")
            return

        # One span for the whole connection. `asyncio` copies the context
        # into every task created inside it, so the microphone's task and
        # each turn find their parent here without being handed it.
        with trace.span("conversation", {"session": conversation.id}, trace_id=conversation.id):
            await websocket.accept()
            recording = record_for(record_dir, conversation.id, system_prompt)
            channel = Channel(websocket, recording)
            session = Session(
                channel, conversation, engine, speaker, system_prompt, listener, ladder
            )

            await channel.send_json(
                {
                    "type": "ready",
                    "session": conversation.id,
                    "provider": engine.provider,
                    "model": engine.model,
                    "voice": (
                        {
                            "provider": speaker.provider,
                            "voice": speaker.voice,
                            # Before any audio, because the browser builds its
                            # output context on a gesture that precedes the reply.
                            "sample_rate": SAMPLE_RATE,
                        }
                        if speaker
                        else None
                    ),
                    "ears": (
                        {"provider": listener.provider, "sample_rate": listener.sample_rate}
                        if listener
                        else None
                    ),
                    "history": serialize(conversation.messages),
                    "ended": conversation.ended,
                }
            )

            session.voiced(await opening.deliver(channel, conversation))
            # After the greeting, so the first silence the clock measures is the one
            # that follows the agent's own voice rather than the socket opening.
            session.start()

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
                await session.close()
                if recording is not None:
                    recording.close()

    return app


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
