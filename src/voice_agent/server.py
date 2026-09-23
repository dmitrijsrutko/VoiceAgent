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
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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
from voice_agent.config import build_prompt, load_settings
from voice_agent.conversation import Conversation, Message
from voice_agent.errors import ConfigError, SessionNotFoundError, VoiceAgentError
from voice_agent.greeting import Greeting
from voice_agent.initiative import LADDER, Rung
from voice_agent.limits import Live, MintLimit, budget_reason, client_address
from voice_agent.llm import LLM
from voice_agent.llm.registry import DEFAULT_MODELS
from voice_agent.llm.registry import available as llm_available
from voice_agent.pool import Pool
from voice_agent.record import Record
from voice_agent.session import Session
from voice_agent.sessions import SessionStore
from voice_agent.stt import STT
from voice_agent.stt.registry import NO_EARS, describe
from voice_agent.stt.registry import available as stt_available
from voice_agent.tts import TTS, create_tts
from voice_agent.tts.base import SAMPLE_RATE

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
PAGE_PATH = WEB_DIR / "index.html"


def serialize(messages: Sequence[Message]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


FACTS_BLOCK = '<script id="facts" type="application/json">{}</script>'
"""Where `chat_page` puts what this agent is, for the page to read before it
has a socket to ask over.

The default in `web/index.html` is an empty object, so a page served any other
way — opened from disk, cached by something — degrades to a start screen that
claims nothing rather than to a crash.
"""


def facts(
    speaker: TTS | None,
    listener: STT | None,
    recording: bool,
    engines: Sequence[str] = (),
    listeners: Sequence[str] = (),
    default_engine: str = "",
    default_ears: str = "",
    model_named: Callable[[str], str] = DEFAULT_MODELS.__getitem__,
) -> dict[str, object]:
    """What the agent is, and — since chapter 15 — what it could be instead.

    One function because there are two places that say it: the `ready` frame,
    and the page itself, which needs it *before* the socket exists so that
    pressing "start" can be an informed act rather than one explained a moment
    too late. Two computations of the same facts is exactly the drift between
    what the page claims and what the server does that chapter 14 went out of
    its way to prevent.

    `choices` lists only what this deployment holds a key for. Offering a
    backend that cannot be built is offering an error, and which keys are set
    differs between a laptop and a deployment — it should.
    """
    return {
        "voice": (
            {
                "provider": speaker.provider,
                "voice": speaker.voice,
                # Before any audio, because the browser builds its output
                # context on a gesture that precedes the reply.
                "sample_rate": SAMPLE_RATE,
            }
            if speaker
            else None
        ),
        "ears": (
            {
                "provider": listener.provider,
                "sample_rate": listener.sample_rate,
                # So the page can say what it can hear before anybody speaks.
                "languages": list(listener.languages),
            }
            if listener
            else None
        ),
        "recording": recording,
        "choices": {
            "llm": [
                # The model this deployment would *actually* run, override
                # included — not the registry's default for the provider. The
                # page said `claude-opus-5` beside a deployment configured for
                # `claude-haiku-4-5`, which is the same lie about itself that
                # every other fact here is assembled in one place to prevent.
                {"name": name, "model": model_named(name), "default": name == default_engine}
                for name in engines
            ],
            # Described rather than built: `stt.registry.describe` reads the
            # modules' own language constants, so the page can say what each
            # recognizer hears without this process holding every key.
            "stt": [{**describe(name), "default": name == default_ears} for name in listeners],
        },
    }


def with_facts(page: str, known: dict[str, object]) -> str:
    """Put the facts into the page it is about.

    `<` is escaped rather than trusted: every value here is a server-side
    constant today, but a `</script>` reaching the page verbatim would end the
    block early and leave the rest of the document as text, and foreclosing
    that costs one call.
    """
    payload = json.dumps(known).replace("<", "\\u003c")
    return page.replace(FACTS_BLOCK, FACTS_BLOCK.replace("{}", payload), 1)


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


async def refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Turn a connection away with a reason the page can actually read.

    Accepted first, and only then closed. Closing *before* accepting looks
    right and is what this did for thirteen chapters, but it abandons the
    handshake — a browser gets HTTP 403, and therefore close code 1006 with an
    empty reason, which is indistinguishable from the network dropping. The
    test client papers over the difference by surfacing the code anyway, so
    this cost a real client to notice (AGENTS.md §6).

    A reason must fit in 123 UTF-8 bytes or the close frame is invalid, which
    would lose the whole message rather than the tail of it.
    """
    await websocket.accept()
    await websocket.close(code=code, reason=reason)


async def expire(after: float, session: Session, websocket: WebSocket) -> None:
    """End a conversation that has run out of its budget, and hang up.

    The socket is closed as well as the conversation, and that is the load-
    bearing half. `chat_socket`'s loop only re-reads `conversation.ended` after
    the next frame arrives, and a browser sitting in silence sends none — so
    ending without closing would leave the socket, and the slot it occupies,
    held by a conversation that is already over.
    """
    await asyncio.sleep(after)
    await session.end(budget_reason(after))
    with contextlib.suppress(RuntimeError):
        # Whoever else noticed the socket was done may have closed it first.
        await websocket.close()


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
    sessions = store if store is not None else SessionStore(settings.max_stored)
    # All three are inert unless configured, which is how every chapter before
    # this one — and every local run of this one — still behaves. `limits.py`
    # says what each bounds and why a public address needs it.
    live = Live(settings.max_live)
    mints = MintLimit(settings.mints_per_ip)
    speaker: TTS | None = tts
    if speaker is None and voice:
        speaker = create_tts(settings.voice_provider, settings.voice)

    def model_named(provider: str) -> str:
        """The model a choice of provider would actually mean here."""
        return model_for(provider) or DEFAULT_MODELS[provider]

    def model_for(provider: str) -> str | None:
        """`VOICE_AGENT_MODEL` names a model, and a model belongs to one
        provider. Applying the deployment's `claude-haiku-4-5` to a DeepSeek
        conversation would ask DeepSeek for a model it has never heard of, so
        the override holds only for the provider it was set alongside."""
        return settings.model if provider == settings.provider else None

    # Which engines and ears a conversation may choose between. Only what this
    # deployment holds a key for: offering a backend that cannot be built is
    # offering an error. With no key for anything, the configured default is
    # offered regardless, so a misconfigured deployment fails the way it always
    # did — at the first call, naming the variable that is missing — rather
    # than by presenting an empty page with nothing to press.
    engines = llm_available() or (settings.provider,)
    listeners = () if not ears else (stt_available() or (settings.ears_provider,))
    default_engine = settings.provider if settings.provider in engines else next(iter(engines), "")
    default_ears = (
        settings.ears_provider
        if settings.ears_provider in listeners
        else next(iter(listeners), NO_EARS)
    )
    pool = Pool(model_for, settings.vad_silence, engine=llm, ears=stt)

    opening = Greeting(settings.greeting if greeting is None else greeting, speaker)
    ladder = ladder_for(settings.initiative if initiative is None else initiative)
    # From the ladder, not from the setting: these are the rungs this session
    # will actually run, so the agent's account of its own clock cannot drift
    # from the clock. It was asked once and invented "a few seconds".
    delays = tuple(rung.after for rung in ladder)
    # The same shape as `tts`/`voice` above: a place to put it, and a switch
    # that turns it off without having to name one.
    record_dir = (sessions_dir or settings.sessions) if record else None

    def chosen(conversation: Conversation, asked: Mapping[str, str]) -> tuple[str, str]:
        """Which stack this conversation runs, deciding it if it has not been.

        Pinned on first connect. A reconnect ignores whatever the query string
        says, because the history was produced by the engine already chosen and
        the prompt names the ears already chosen.

        An unknown or unavailable name falls back to the default rather than
        refusing the socket — this is a URL a stranger can type, not
        configuration, and the `ready` frame reports what actually ran, which
        the page prints. Repair that announces itself is not the silent kind
        this project refuses.
        """
        if conversation.engine is None:
            wanted = asked.get("llm", "")
            conversation.engine = wanted if wanted in engines else default_engine
            heard = asked.get("stt", "")
            conversation.ears = heard if heard in listeners else default_ears
        return conversation.engine, conversation.ears or NO_EARS

    # Whether the warm-up below has finished. Off localhost something else —
    # a platform health check — decides when this process starts receiving
    # people, and it must not decide that while the greeting is still being
    # synthesised. A list because `lifespan` closes over it.
    warm: list[bool] = [False]

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Synthesised once, at startup, before anyone is waiting on it. The
        # first synthesis in a process took 3.1 s against 250-290 ms for every
        # one after, and paying that on someone's first question is the worst
        # possible moment for it. The reasoning connections are opened
        # alongside, for the same reason — and every engine that may be chosen,
        # not only the default, because `connect` lists models and no provider
        # bills for that. Measured in `--bench-llm`, the first request in a
        # process pays 297 ms (anthropic) to 1172 ms (deepseek) of DNS and TLS,
        # and somebody who picks the other one should not pay it for choosing.
        await asyncio.gather(opening.prepare(), *(connect(pool.engine(name)) for name in engines))
        warm[0] = True
        yield

    app = FastAPI(title="voice-agent", lifespan=lifespan)
    # The page's scripts, as native ES modules. Served as files rather than
    # inlined so each one can be imported — and executed — by node under test.
    app.mount("/static", PageModules(directory=WEB_DIR), name="static")

    @app.get("/healthz")
    async def healthz() -> Response:
        """Ready, not merely alive.

        A 200 here means the greeting is synthesised and the reasoning engine
        is connected — so a deployment that routes on this check never hands
        somebody a machine that would make them wait 3.1 s for hello.
        """
        if not warm[0]:
            return Response("warming", status_code=503, media_type="text/plain")
        return Response("ok", media_type="text/plain")

    @app.get("/")
    async def new_conversation(request: Request) -> Response:
        """Minting the key here is what makes the link the conversation:
        every page load starts a new one and lands on its own URL.

        Which is also why this is the door worth rate-limiting. A load costs
        nothing, but the conversation it mints can be connected to, and a
        connection is a recognizer stream, a reasoning connection and a clock
        that spends money on silence.
        """
        address = client_address(request.headers, request.client.host if request.client else None)
        if not mints.allow(address):
            return HTMLResponse(
                "<h1>429 — too many new conversations</h1>"
                "<p>This public demo limits how many conversations one address "
                "may start. Try again in a few minutes, or carry on in a link "
                "you already have.</p>",
                status_code=429,
            )
        conversation = sessions.create()
        return RedirectResponse(url=f"/c/{conversation.id}", status_code=303)

    @app.get("/c/{key}")
    async def chat_page(key: str) -> HTMLResponse:
        """The page, carrying what this agent is.

        Served with the facts already in it rather than fetched afterwards,
        because the page's first job is now to ask whether to begin — and it
        cannot ask honestly without saying what beginning entails. There is no
        socket at that point to ask over.
        """
        try:
            sessions.get(key)
        except SessionNotFoundError:
            return HTMLResponse("<h1>404 — no such conversation</h1>", status_code=404)
        page = PAGE_PATH.read_text(encoding="utf-8")
        known = facts(
            speaker,
            # The default's ears, for a page that has not chosen yet; the
            # `choices` beside it say what every option would hear.
            pool.ears(default_ears) if default_ears != NO_EARS else None,
            record_dir is not None,
            engines,
            listeners,
            default_engine,
            default_ears,
            model_named,
        )
        return HTMLResponse(with_facts(page, known))

    @app.websocket("/ws/{key}")
    async def chat_socket(websocket: WebSocket, key: str) -> None:
        try:
            conversation = sessions.get(key)
        except SessionNotFoundError:
            await refuse(websocket, 4404, "No such conversation — start a new one from the root.")
            return

        # Claimed here rather than on the page, because holding a socket is what
        # costs: the recognizer's stream, the reasoning connection and the clock
        # all belong to a connection, none of them to a page that was loaded.
        if not live.take():
            await refuse(
                websocket,
                4429,
                "This public demo is busy — only a few conversations at once. Try again shortly.",
            )
            return

        # Released in the `finally` below rather than at the end of the loop:
        # a slot that leaked when `accept()` or the greeting failed would
        # shrink the cap by one for the life of the process.
        try:
            # One span for the whole connection. `asyncio` copies the context
            # into every task created inside it, so the microphone's task and
            # each turn find their parent here without being handed it.
            with trace.span("conversation", {"session": conversation.id}, trace_id=conversation.id):
                await websocket.accept()
                engine_name, ears_name = chosen(conversation, websocket.query_params)
                engine = pool.engine(engine_name)
                listener = pool.ears(ears_name)
                # Per conversation since the ears are chosen rather than
                # configured, and what the agent can hear is one of the things
                # this prompt states. Stable for the conversation's life, which
                # is what a provider's prefix cache needs.
                system_prompt = build_prompt(
                    listener.languages if listener else (), settings.voice_gender, delays
                )
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
                        **facts(
                            speaker,
                            listener,
                            record_dir is not None,
                            engines,
                            listeners,
                            engine_name,
                            ears_name,
                            model_named,
                        ),
                        "history": serialize(conversation.messages),
                        "ended": conversation.ended,
                    }
                )

                session.voiced(await opening.deliver(channel, conversation))
                # After the greeting, so the first silence the clock measures is the one
                # that follows the agent's own voice rather than the socket opening.
                session.start()
                budget = (
                    asyncio.create_task(expire(settings.session_budget, session, websocket))
                    if settings.session_budget is not None
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
        finally:
            live.release()

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
