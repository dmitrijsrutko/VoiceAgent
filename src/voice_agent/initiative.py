"""The clock: what lets the agent speak without having been spoken to.

A ticker notices a silence the user has left and asks the model whether there is
anything worth saying into it. Declining is the normal answer, and every
consideration is reported to the page, declines included.

Two rules:

- **It only ever speaks into silence**, never over the user.
- **The budget is hard**: one line per rung, then quiet until the user speaks.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from voice_agent import timing, trace
from voice_agent.conversation import Conversation, Message
from voice_agent.decline import DECLINE, is_decline
from voice_agent.errors import VoiceAgentError
from voice_agent.llm import LLM
from voice_agent.llm.base import Usage
from voice_agent.streams import closing
from voice_agent.timing import elapsed_ms

logger = logging.getLogger(__name__)

TICK_SECONDS = 1.0
"""How often the ladder is checked."""

MAX_LINE_CHARS = 300
"""Longer than this and an unprompted line is treated as malformed, not spoken:
the brevity instruction did not land."""


@dataclass(frozen=True, slots=True)
class Rung:
    """One step of the escalation: when it may fire (`after`, seconds into the
    silence), what it is for, and how willing the agent should be to take it.
    The willingness rises as the silence grows: tactful early is neglectful late.
    """

    after: float
    intent: str
    disposition: str


LADDER: tuple[Rung, ...] = (
    Rung(
        5.0,
        "Follow through on what you just said. The shortest useful addition to "
        "it: the obvious next step, or the one thing in your last answer most "
        "likely to need unpacking — offered so they can wave it away in a word. "
        "Stay on what the two of you were already talking about. This is not the "
        "moment to open a new subject, to reword an invitation you have already "
        "made, or to ask whether they are still there.",
        "A pause this short is usually someone thinking, or drawing breath before "
        "they answer, and saying nothing is the ordinary right answer here — take "
        "it unless what you have is both genuinely useful and genuinely short. "
        "Two cases call for silence almost always: if what you just said was "
        "long, they are still taking it in, and five seconds is nowhere near "
        "enough time to have finished; and if nothing has been said yet beyond "
        "your own opening, there is nothing to follow through on and the "
        "invitation has already been made.",
    ),
    Rung(
        15.0,
        "Change register. Do not restate your earlier invitation in different "
        "words — offer something concrete instead: a specific thing the two of "
        "you could talk about, drawn from this conversation if there is one, or "
        "an easy way in if there is not.",
        "This is a long silence now. Staying quiet is still allowed, but someone "
        "sitting with another person for this long would usually say something. "
        "Two things make it right to stay quiet: if anything you could offer "
        "would land as pushing, or if they have already told you why they have "
        "gone quiet — they are busy, thinking, or coming back in a moment. A "
        "silence someone has explained is not a silence to fill.",
    ),
    Rung(
        28.0,
        "Withdraw, gracefully. Say plainly that you will be quiet now and that "
        "they can start again whenever they like. This is the last thing you "
        "will say unprompted, so it should sound like a door left open, not "
        "like giving up on them. If the two of you were talking about something, "
        "name it, so the line could not have been said to anybody else; if "
        "nothing has been said yet, keep it simple and warm — but say it either "
        "way.",
        "You should almost always take this one. Leaving someone in silence with "
        "no sign you are still here is worse than one short line, and this is "
        "your last chance to give them one. Stay quiet only if you have already "
        "told them you would.",
    ),
)
"""Three rungs: follow through on what was just said, offer something concrete
(never "are you still there?"), then withdraw and hand control back. After the
withdrawal, silence until the user speaks."""


def nudge_prompt(rung: Rung, quiet: float) -> str:
    """The message that asks the model whether to speak. Used for one call and
    never recorded; placed after the history so the cached prefix survives."""
    return (
        "[This is not the other person speaking. It is your own sense of the pause.]\n\n"
        f"They have said nothing for about {quiet:.0f} seconds. Nobody has asked you "
        "anything. You may say one short thing now, unprompted, or you may stay quiet.\n\n"
        f"{rung.disposition}\n\n"
        f"If you do speak: {rung.intent}\n\n"
        "Then keep it to one sentence, two at the very most. Do not repeat the shape "
        "of anything you have already said in this conversation — read it back and say "
        "something new. Do not apologise for speaking, do not describe the pause, and "
        "do not ask whether they are still there unless nothing else fits.\n\n"
        "Reply with that line alone — no quotation marks, no preamble. Or, to stay "
        f"quiet, reply with exactly: {DECLINE}"
    )


def spoken_line(reply: str) -> str | None:
    """The line to say, or `None` if the model declined (see `decline.py`)."""
    line = reply.strip().strip("\"'").strip()
    if not line or is_decline(reply):
        return None
    return line


class Initiative:
    """One session's clock: the ladder, the budget, and the decision to speak.

    Knows nothing of `Session`: it sees `quiet` (usable silence, or `None` for
    "not now") and can `speak`. The yield rule stays with the session.
    """

    def __init__(
        self,
        engine: LLM,
        system_prompt: str,
        conversation: Conversation,
        quiet: Callable[[], float | None],
        speak: Callable[[str, int], Awaitable[None]],
        report: Callable[[dict[str, object]], Awaitable[None]],
        ladder: Sequence[Rung] = LADDER,
        tick: float = TICK_SECONDS,
    ) -> None:
        self._engine = engine
        self._system_prompt = system_prompt
        self._conversation = conversation
        self._quiet = quiet
        self._speak = speak
        self._report = report
        self._ladder = tuple(ladder)
        self._tick = tick
        self._rung = 0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None and self._ladder:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        # Never awaited from inside its own task, which would deadlock.
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def reset(self) -> None:
        """The user said something, so the budget is theirs again."""
        self._rung = 0

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            try:
                await self.tick()
            except VoiceAgentError as exc:
                logger.info("an unprompted turn was not taken: %s", exc)
            except Exception:
                # A defect must not stop the ticker silently.
                logger.exception("the initiative clock failed")

    async def tick(self) -> None:
        """One check of the ladder. Public so tests drive it without sleeping."""
        if self._rung >= len(self._ladder):
            return  # budget spent; silent until the user speaks
        quiet = self._quiet()
        if quiet is None:
            return
        rung = self._ladder[self._rung]
        if quiet < rung.after:
            return
        # Spent before the call, whatever comes back: an opportunity, not a debt.
        index, self._rung = self._rung, self._rung + 1
        await self._consider(index, rung, quiet)

    async def _consider(self, index: int, rung: Rung, quiet: float) -> None:
        started = timing.now()
        # A call on a timer is a spend decision, so its cost is reported too.
        usage = Usage()
        nudge = nudge_prompt(rung, quiet)
        try:
            with trace.span(
                "initiative.consider",
                {"rung": index + 1, "quiet_ms": round(quiet * 1000), "nudge": nudge},
                trace_id=self._conversation.id,
            ):
                reply = await self._ask(nudge, usage)
        except VoiceAgentError as exc:
            # Reported: a clock failing silently looks like one choosing silence.
            # The rung stays spent, so an outage is not a retry loop.
            logger.warning("could not decide whether to speak: %s", exc)
            await self._note(index, quiet, "failed", started, usage, message=str(exc))
            return

        line = spoken_line(reply)
        if line is None:
            decision = "declined"
        elif len(line) > MAX_LINE_CHARS:
            decision, line = "overran", None
        elif self._broken(quiet):
            decision, line = "yielded", None
        else:
            decision = "spoke"
        await self._note(index, quiet, decision, started, usage, line=line or "")
        # `Session.speak` re-checks the moment itself.
        if line is not None:
            await self._speak(line, index + 1)

    async def _note(
        self,
        index: int,
        quiet: float,
        decision: str,
        started: float,
        usage: Usage,
        line: str = "",
        message: str = "",
    ) -> None:
        """One consideration, drawn on the page whatever it decided."""
        await self._report(
            {
                "type": "initiative",
                "rung": index + 1,
                "rungs": len(self._ladder),
                "quiet_ms": round(quiet * 1000),
                "decision": decision,
                "consider_ms": elapsed_ms(started),
                "line": line,
                "message": message,
                "prompt_tokens": usage.prompt_tokens,
                "cached_tokens": usage.cached_tokens,
                "output_tokens": usage.output_tokens,
            }
        )

    def _broken(self, before: float) -> bool:
        """Did the silence survive the deciding? Someone who starts talking
        restarts it through the recognizer's first partial, so `quiet` comes
        back *smaller* — long before the commit would make it `None`."""
        after = self._quiet()
        return after is None or after < before

    async def _ask(self, nudge: str, usage: Usage | None = None) -> str:
        """One small call, drained rather than streamed: nobody is waiting on
        it, so deciding first and speaking second is affordable."""
        messages: list[Message] = [
            *self._conversation.context,
            Message(role="user", content=nudge),
        ]
        fragments: list[str] = []
        async with closing(self._engine.stream(self._system_prompt, messages, usage)) as stream:
            async for fragment in stream:
                fragments.append(fragment)
        return "".join(fragments)
