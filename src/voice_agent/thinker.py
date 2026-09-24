"""The inner voice: what, if anything, is most worth saying next.

A small, fast model runs alongside the conversation in the session's role and
holds at most one pending thought: a move, an urgency, why, and the line. It
never speaks. This chapter shows every thought on the page; later chapters
decide when one is said.

It is asked at the moments that matter for speaking: when the user pauses (the
floor's `micro_pause`), every few seconds of an unbroken monologue, and after
the agent's own reply. Calls are single-flight: while one is out, only the
latest reason to ask again is kept, so a burst of pauses costs two calls, not
ten. A per-minute cap is a hard stop.
"""

import asyncio
import contextlib
import json
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from voice_agent import prompts, timing, trace
from voice_agent.conversation import Conversation, Message
from voice_agent.errors import VoiceAgentError
from voice_agent.llm import LLM
from voice_agent.llm.base import Usage
from voice_agent.roles import Role
from voice_agent.streams import closing
from voice_agent.timing import elapsed_ms

logger = logging.getLogger(__name__)

THINKER_MODEL = "claude-haiku-4-5"
"""Fast and cheap enough to run on every pause; the reply engine is untouched."""


MONOLOGUE_SECONDS = 8.0
"""How often to think while the user talks without pausing: a monologue is
exactly where a partner most needs an opinion, and it has no pauses to ask at."""

CALLS_PER_MINUTE = 20
"""A hard cap. Pauses run ~5-15 a minute in speech, so this binds only on a
runaway, where it is the cost control."""

MAX_NOTES_WORDS = 60
"""Notes are fed back every call; a model that lets them grow would grow the
bill with them. The prompt asks for 40; this is the hard stop."""

RECENT_MESSAGES = 12
"""How much of the conversation the thinker reads verbatim: the last six
exchanges. Older ground lives in its notes. Without this the prompt grew by a
third over one live session (1.5k → 2.5k tokens), and so did every call."""


@dataclass(frozen=True, slots=True)
class Thought:
    move: str
    urgency: int
    why: str
    line: str


class Malformed(ValueError):
    """The reply was not the promised JSON, or named a move the role lacks."""


def system_prompt(role: Role, template: str | None = None) -> str:
    """The thinker's instructions with the role filled in. Stable for the
    conversation, so the provider caches it. Placeholders are replaced by name
    rather than `str.format`, so braces in a role's text are just text."""
    text = prompts.load("thinker") if template is None else prompts.body(template)
    values = {
        "name": role.name,
        "summary": role.summary,
        "job": role.job,
        "worth_it": role.worth_it,
        "not_worth_it": role.not_worth_it,
        "assertiveness": role.assertiveness,
        "moves": ", ".join(role.moves),
    }
    # One pass over the template: a role's own text is inserted, never scanned,
    # so a `{moves}` written in a card stays literally that.
    placeholder = re.compile(r"\{(" + "|".join(values) + r")\}")
    return placeholder.sub(lambda m: values[m.group(1)], text).strip()


def parse_reply(reply: str, role: Role) -> tuple[str, Thought | None]:
    """`(notes, thought)` from the model's JSON, or `Malformed`. Tolerates a
    code fence or prose around the object; nothing else."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end < start:
        raise Malformed("no JSON object in the reply")
    try:
        data = json.loads(reply[start : end + 1])
    except json.JSONDecodeError as exc:
        raise Malformed(f"not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise Malformed("the reply is not an object")
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise Malformed("notes is not a string")
    notes = " ".join(notes.split()[:MAX_NOTES_WORDS])
    raw = data.get("thought")
    if raw is None:
        return notes, None
    if not isinstance(raw, dict):
        raise Malformed("thought is neither null nor an object")
    move, urgency = raw.get("move"), raw.get("urgency")
    why, line = raw.get("why"), raw.get("line")
    if move not in role.moves:
        raise Malformed(f"move {move!r} is not one of this role's {list(role.moves)}")
    if not isinstance(urgency, int) or isinstance(urgency, bool) or not 0 <= urgency <= 3:
        raise Malformed(f"urgency {urgency!r} is not 0-3")
    if urgency == 0 or not isinstance(line, str) or not line.strip():
        # A thought with nothing to say is no thought: how the model says
        # "hold" — an empty line, a null one (3 of 17 live), or none at all.
        return notes, None
    why = why if isinstance(why, str) else ""
    return notes, Thought(move, urgency, why.strip(), line.strip())


def transcript(conversation: Conversation) -> str:
    """The conversation as the thinker reads it: a script, not a chat. It is an
    observer of the exchange, not a party to it."""
    messages = conversation.messages
    lines = [
        f"{'Partner' if m.role == 'assistant' else 'User'}: {m.content}"
        for m in messages[-RECENT_MESSAGES:]
    ]
    if len(messages) > RECENT_MESSAGES:
        lines.insert(0, f"({len(messages) - RECENT_MESSAGES} earlier lines: see your notes)")
    return "\n".join(lines) if lines else "(nothing yet)"


def request(
    conversation: Conversation,
    hearing: str,
    notes: str,
    pending: Thought | None,
    reason: str,
) -> str:
    holding = (
        "none"
        if pending is None
        else f"{pending.move}, urgency {pending.urgency}: “{pending.line}” ({pending.why})"
    )
    return (
        f"# The conversation so far\n\n{transcript(conversation)}\n\n"
        f"# The user, right now\n\n{hearing.strip() or '(not speaking)'}\n\n"
        f"# Your notes\n\n{notes or '(none yet)'}\n\n"
        f"# The thought you are holding\n\n{holding}\n\n"
        f"# Why you are asked now\n\n{REASONS.get(reason, reason)}"
    )


REASONS = {
    "micro_pause": "The user has just paused, briefly.",
    "pause": "The user has paused for over half a second.",
    "monologue": "The user has been talking for a while without pausing.",
    "reply": "Your partner has just finished speaking.",
}


class Thinker:
    """One conversation's inner voice.

    Knows nothing of `Session`: it is told what the user is saying (`hearing`),
    asked to `trigger`, and fed the floor; it reports every consideration.
    """

    def __init__(
        self,
        engine: LLM,
        role: Role,
        conversation: Conversation,
        hearing: Callable[[], str],
        report: Callable[[dict[str, object]], Awaitable[None]],
        prompt: str | None = None,
        per_minute: int = CALLS_PER_MINUTE,
        monologue_seconds: float = MONOLOGUE_SECONDS,
    ) -> None:
        self._engine = engine
        self._role = role
        self._conversation = conversation
        self._hearing = hearing
        self._report = report
        self._system = system_prompt(role) if prompt is None else prompt
        self._per_minute = per_minute
        self._monologue_seconds = monologue_seconds
        self.notes = ""
        self.pending: Thought | None = None
        self._calls: deque[float] = deque()
        self._capped_reported = False
        self._task: asyncio.Task[None] | None = None
        self._again: str | None = None
        self._monologue: asyncio.Task[None] | None = None
        self._stopped = False
        self._last_heard: tuple[int, str] | None = None
        """What the last call was about: nothing new since, no new call."""
        """Final. Floor events and finished replies can still arrive after the
        session has let go; none of them may start a call, or a timer."""

    def trigger(self, reason: str) -> None:
        """Ask for a thought. If one is being thought already, remember only
        the latest reason and ask once more when it is done."""
        if self._stopped:
            return
        if self._task is not None and not self._task.done():
            self._again = reason
            return
        self._task = asyncio.create_task(self._run(reason))

    def floor(self, state: str) -> None:
        """The user's floor changed: think at each pause, and on a timer while
        they talk without one. `stopped` is the microphone going away."""
        if self._stopped:
            return
        if state == "speaking":
            if self._monologue is None or self._monologue.done():
                self._monologue = asyncio.create_task(self._while_talking())
            return
        self.hush()
        if state == "micro_pause":
            self.trigger("micro_pause")

    def hush(self) -> None:
        """Stop thinking on the clock: the user is not the one holding the floor."""
        self._stop_monologue()

    async def stop(self) -> None:
        self._stopped = True
        self._again = None
        self._stop_monologue()
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _stop_monologue(self) -> None:
        if self._monologue is not None:
            self._monologue.cancel()
            self._monologue = None

    async def _while_talking(self) -> None:
        while True:
            await asyncio.sleep(self._monologue_seconds)
            self.trigger("monologue")

    async def _run(self, reason: str) -> None:
        next_reason: str | None = reason
        while next_reason is not None:
            try:
                await self.consider(next_reason)
            except Exception:
                # A defect must not end the inner voice silently.
                logger.exception("the thinker failed")
            next_reason, self._again = self._again, None

    def _allowed(self) -> bool:
        now = timing.now()
        while self._calls and now - self._calls[0] >= 60:
            self._calls.popleft()
        if len(self._calls) >= self._per_minute:
            return False
        self._calls.append(now)
        self._capped_reported = False
        return True

    async def consider(self, reason: str) -> None:
        """One call, reported whatever it decided. Public so tests drive it."""
        hearing = self._hearing()
        heard = (len(self._conversation.messages), hearing.strip())
        if heard == self._last_heard:
            # The same words would buy the same answer, so the held thought
            # stands, unbilled and uncapped. Live, a run of pauses in one
            # sentence asked six times over for one thought.
            await self._note(reason, "unchanged", 0, Usage(), thought=self.pending)
            return
        if not self._allowed():
            if not self._capped_reported:
                self._capped_reported = True
                await self._note(reason, "capped", 0, Usage())
            return
        started = timing.now()
        usage = Usage()
        ask = request(self._conversation, hearing, self.notes, self.pending, reason)
        try:
            with trace.span(
                "thinker.consider",
                {"reason": reason, "role": self._role.slug},
                trace_id=self._conversation.id,
            ):
                reply = await self._ask(ask, usage)
        except VoiceAgentError as exc:
            logger.warning("the thinker could not think: %s", exc)
            await self._note(reason, "failed", elapsed_ms(started), usage, message=str(exc))
            return
        try:
            notes, thought = parse_reply(reply, self._role)
        except Malformed as exc:
            logger.info("the thinker replied malformed: %s", exc)
            await self._note(reason, "malformed", elapsed_ms(started), usage, message=str(exc))
            return
        self.notes, self.pending = notes, thought
        self._last_heard = heard
        await self._note(
            reason,
            "thought" if thought is not None else "nothing",
            elapsed_ms(started),
            usage,
            thought=thought,
        )

    async def _ask(self, ask: str, usage: Usage) -> str:
        """Drained, not streamed: nothing downstream can use half a thought."""
        fragments: list[str] = []
        messages = [Message(role="user", content=ask)]
        async with closing(self._engine.stream(self._system, messages, usage)) as stream:
            async for fragment in stream:
                fragments.append(fragment)
        return "".join(fragments)

    async def _note(
        self,
        reason: str,
        decision: str,
        consider_ms: int,
        usage: Usage,
        thought: Thought | None = None,
        message: str = "",
    ) -> None:
        payload: dict[str, object] = {
            "type": "thought",
            "reason": reason,
            "decision": decision,
            "consider_ms": consider_ms,
            "notes": self.notes,
            "prompt_tokens": usage.prompt_tokens,
            "cached_tokens": usage.cached_tokens,
            "output_tokens": usage.output_tokens,
        }
        if thought is not None:
            payload.update(
                move=thought.move, urgency=thought.urgency, why=thought.why, line=thought.line
            )
        if message:
            payload["message"] = message
        await self._report(payload)
