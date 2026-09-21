"""The clock: what lets the agent speak without having been spoken to.

Every turn before this chapter was started by the user — typed, or committed by
the recognizer. Nothing in the process ever woke up on its own, so a user who
said nothing got nothing, forever. This is the smallest honest piece of a
mixed-initiative agent: a ticker that notices a silence the user has left and
asks whether there is anything worth saying into it.

The headline is not that the agent speaks unprompted. It is that it considers
speaking and usually decides not to. Declining is the normal answer, it is
reported to the page like any other event, and the ratio of declines to nudges
is what this chapter is judged on — not a latency number.

Two rules hold the whole design up:

- **It only ever speaks into silence.** Never over the user. Silence-filling
  and talking over someone need the same machinery — a clock, a yield rule, a
  budget, a judgement — but only one of them can be embarrassing while the
  thresholds are being tuned.
- **The budget is hard.** Three unprompted lines per stretch of silence, then
  quiet for good until the user speaks. A proactive agent without a ceiling is
  not a partner, it is a nuisance.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from string import punctuation

from voice_agent import trace
from voice_agent.conversation import Conversation, Message
from voice_agent.errors import VoiceAgentError
from voice_agent.llm import LLM
from voice_agent.llm.base import Usage
from voice_agent.streams import closing
from voice_agent.timing import elapsed_ms

logger = logging.getLogger(__name__)

TICK_SECONDS = 1.0
"""How often the ladder is checked. The same cadence as the microphone's
watchdog, and far finer than the delays it measures — the thresholds here are
seconds, so a second of granularity costs nothing."""

MAX_LINE_CHARS = 300
"""Longer than this and the answer is treated as malformed rather than spoken.

`MAX_OUTPUT_TOKENS` is 1024, and a model that ignores "one sentence, two at the
very most" would otherwise deliver a paragraph nobody asked for — the worst
failure this feature has available, since the user did not even open the
exchange. Two long sentences fit comfortably inside this."""

DECLINE = "NOTHING"
"""What the model answers to say it has nothing worth saying. A sentinel rather
than an empty reply, so that "the model chose silence" and "the call produced
nothing" stay distinguishable — the first is the feature, the second is a bug."""


@dataclass(frozen=True, slots=True)
class Rung:
    """One step of the escalation: when it may fire, what it is for, and how
    willing the agent should be to take it.

    `after` is counted from the last thing the user said, not from the previous
    rung, so the ladder is read as absolute positions in a silence.

    `disposition` is the correction that came out of measuring this against a
    real model. Weighted the same at every rung, the veto made the agent
    reticent to the point of uselessness — eleven declines out of twelve
    considerations, including at forty-five seconds of dead silence. The bias
    towards saying nothing has to *fall* as the silence grows, because what is
    tactful at seven seconds is neglectful at forty-five.
    """

    after: float
    intent: str
    disposition: str


LADDER: tuple[Rung, ...] = (
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
"""Two rungs: offer something, then withdraw. The delays matter least.

There used to be a third, at seven seconds, whose job was to leave the door open
and invite the user in. It never once fired in eighteen measured considerations,
and reading it back the reason was structural rather than shy: the rules forbid
rewording an invitation already made, and the greeting *is* an invitation. Every
move available to that rung was prohibited, so the model correctly said nothing.
A rung with no legal move is not caution, it is a paid call with a foregone
conclusion — so it is gone, and a seven-second pause is simply not the agent's
to fill.

What remains is the pair that does work. The first *offers something concrete*
rather than asking again — "are you there? … ARE YOU THERE?" is the needy
pattern, and it is what makes proactive agents unbearable to sit with. The
second is a withdrawal, which is a social act in its own right: it hands control
back explicitly and earns the trust that lets the agent speak first at all.
After it, silence until the user says something."""


def nudge_prompt(rung: Rung, quiet: float) -> str:
    """The transient message that asks the model whether to speak.

    Appended after the history for one call and never recorded. After, not
    before: a volatile element early in the prompt invalidates the provider's
    prefix cache from that point on, and the history is the part worth caching.
    """
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
    """The line to say, or `None` if the model declined.

    Forgiving about how the sentinel comes back — quoted, punctuated, in a
    sentence of its own — because a decline misread as a line is the one
    failure that gets spoken out loud.
    """
    line = reply.strip().strip("\"'").strip()
    if not line:
        return None
    # Every kind of trailing punctuation, not a chosen few: `NOTHING?` and
    # `NOTHING,` were read as lines and synthesised aloud, which is precisely
    # the failure this function exists to prevent.
    if line.strip(punctuation).strip().casefold() == DECLINE.casefold():
        return None
    return line


class Initiative:
    """One session's clock: the ladder, the budget, and the decision to speak.

    Deliberately knows nothing about `Session`. What it can see of the
    conversation arrives as `quiet` — seconds of usable silence, or `None` for
    "not now" — and what it can do arrives as `speak`. That keeps the yield
    rule in one place (the session, which owns the state) and the policy in
    another (here), and it makes this testable without a socket.
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
        # Never awaited from inside the ticker's own task: cancelling and then
        # awaiting yourself deadlocks until something else tears the session
        # down. Unreachable today — a nudge runs in its own task — but it is one
        # chapter away from being reachable, and `Mic.stop` already guards it.
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
                # A defect here must not take the ticker down silently and
                # leave the agent mute for the rest of the conversation.
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
        # Consumed before the call, whatever comes back. A rung is an
        # opportunity, not a debt: a decline must not leave the agent
        # re-deciding the same rung once a second for the rest of the silence.
        index, self._rung = self._rung, self._rung + 1
        await self._consider(index, rung, quiet)

    async def _consider(self, index: int, rung: Rung, quiet: float) -> None:
        started = time.perf_counter()
        # A call on a timer is a spend decision. This project has already lost a
        # month's quota to one, so what a decision costs goes on screen beside
        # what it decided — including, and especially, the ones that say nothing.
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
            # Reported, not merely logged. The rung stays spent — the budget is
            # a ceiling, and an outage must not turn the clock into a retry loop
            # — but a clock that fails in silence looks exactly like one that
            # chose to say nothing, and those are opposite facts. This project's
            # position on that is already written down in `mic.py`.
            logger.warning("could not decide whether to speak: %s", exc)
            await self._note(index, quiet, "failed", started, usage, message=str(exc))
            return

        line = spoken_line(reply)
        if line is None:
            decision = "declined"
        elif len(line) > MAX_LINE_CHARS:
            # Not spoken and not silently dropped: an answer this long means the
            # brevity instruction is not landing, which is a thing to see.
            decision, line = "overran", None
        elif self._broken(quiet):
            decision, line = "yielded", None
        else:
            decision = "spoke"
        await self._note(index, quiet, decision, started, usage, line=line or "")
        # One socket write stands between the gate above and the line going out.
        # `Session.speak` re-checks for itself, so what is left is the width of
        # that write.
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
        """Did the silence we decided about survive the deciding?

        The subtle half of the yield rule, and the half the first version got
        wrong. Someone who starts talking mid-decision does not make the moment
        *unavailable* — `quiet` does not become `None`, because none of the
        conditions the session watches for have changed yet. What happens is
        that the recognizer's first partial restarts the silence, so `quiet`
        comes back **smaller than it went in**. Watching only for `None` waited
        for the commit, a second or more later, by which time the agent was
        already talking over them.

        A shrinking silence is therefore the signal, and it needs no new state
        and no new source — only the number we already had.
        """
        after = self._quiet()
        return after is None or after < before

    async def _ask(self, nudge: str, usage: Usage | None = None) -> str:
        """One small call, drained rather than streamed.

        Nobody is waiting for this — the agent chose the moment — so an
        unprompted turn is the one thing in this project with no latency budget.
        That is what makes deciding first and speaking second affordable here,
        where on a reply to a question it would be unthinkable.
        """
        messages: list[Message] = [
            *self._conversation.messages,
            Message(role="user", content=nudge),
        ]
        fragments: list[str] = []
        async with closing(self._engine.stream(self._system_prompt, messages, usage)) as stream:
            async for fragment in stream:
                fragments.append(fragment)
        return "".join(fragments)
