"""The sentinel the agent answers with to decline a silence, and the guard that
keeps it from ever being spoken.

Declining is a reserved word rather than an empty reply, so *chose silence* and
*produced nothing* stay distinguishable. Only the per-call nudge
(`initiative.nudge_prompt`) mentions it; the base prompt never does. `guard`
checks ordinary replies anyway, for the sentinel and for stage directions
(`[waiting]`), because a prompt is a request and a guard is a guarantee.
"""

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from string import punctuation

from voice_agent.streams import closing

logger = logging.getLogger(__name__)

DECLINE = "NOTHING"
"""What the model answers to say it has nothing worth saying."""


def is_decline(reply: str) -> bool:
    """Whether a whole reply is the sentinel and nothing else, forgiving quotes,
    punctuation and case (`NOTHING?` is still a decline)."""
    line = reply.strip().strip("\"'").strip()
    return bool(line) and line.strip(punctuation).strip().casefold() == DECLINE.casefold()


ASIDE_OPENERS = "[("
"""What a stage direction starts with. Decidable on the first character, and no
reply that opens this way is speakable (`[laughs] Sure` included)."""


def is_aside(reply: str) -> bool:
    """Whether a reply is a stage direction rather than something to say."""
    stripped = reply.lstrip()
    return bool(stripped) and stripped[0] in ASIDE_OPENERS


def _still_possible(seen: str) -> bool:
    """Whether what has arrived so far could still become the sentinel. Once
    it diverges, everything held is released: at most seven characters wait."""
    head = seen.lstrip().lstrip("\"'").lstrip()
    if is_decline(head):
        return True
    # Trailing space is not stripped (it proves divergence: "No problem");
    # trailing punctuation is, so "NOTHING." is still caught.
    return DECLINE.casefold().startswith(head.rstrip(punctuation).casefold())


async def guard(
    source: AsyncIterator[str],
    instead: Callable[[], AsyncIterator[str]],
) -> AsyncGenerator[str, None]:
    """Stream a reply, unless it is not speech at all — then ask again.

    An aside is certain at its first character, so nothing is held for it. The
    sentinel is only certain at the end (`No.` starts the same way), so a reply
    is held only while it could still become it — never the whole reply, which
    would stop the voice starting early. A leak costs one extra call.
    """
    held: list[str] = []
    released = False
    opened_as_aside = False
    # `closing`, not a bare `async for`: a wrapper closed mid-reply would
    # otherwise leave the provider generating, and billing.
    async with closing(source) as fragments:
        async for fragment in fragments:
            if released:
                yield fragment
                continue
            held.append(fragment)
            seen = "".join(held)
            if is_aside(seen):
                opened_as_aside = True
                break
            if not _still_possible(seen):
                released = True
                for piece in held:
                    yield piece
                held.clear()

    if released or not held:
        return
    if not opened_as_aside and not is_decline("".join(held)):
        # Only started like the sentinel ("No."): an ordinary short answer.
        for piece in held:
            yield piece
        return

    leak = "a stage direction" if opened_as_aside else "the decline sentinel"
    logger.warning("%s reached an ordinary turn; asking again", leak)
    async with closing(instead()) as retry:
        async for fragment in retry:
            yield fragment
