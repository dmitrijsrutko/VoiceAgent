"""The sentinel the agent answers with to decline a silence — and the guard
that keeps it from ever being spoken.

Chapter 9 gave the agent a way to say "I considered speaking into this pause
and I would rather not": a reserved word, rather than an empty reply, so that
*chose silence* and *produced nothing* stay distinguishable — the first is the
feature and the second is a bug.

A reserved word in a model's vocabulary is a liability as well as a mechanism,
and this one escaped. The base system prompt told the agent how to decline on
every call, while only the initiative path ever filtered the answer, so an
ordinary turn was primed to emit a token nothing downstream would catch. Asked
"Nothing specifically, what's on yours?", a real conversation on the public
instance was answered — aloud — with `NOTHING`.

Two changes came out of that, and both are here rather than in one place,
because a prompt is a request and a guard is a guarantee:

- The instruction now lives only in the per-call note `Initiative` appends
  (`nudge_prompt`), never in the base prompt. A turn that nobody asked to
  consider a silence is never told this token means anything.
- `guard` watches an ordinary reply anyway. Models do not always do as they are
  told, and this one is cheap to check.

The first version of that prompt change caused a second leak of the same family,
which is why the guard covers two things rather than one. Removing the named
sentinel left "saying nothing is always available to you" in the base prompt
with nothing to point at, and the model improvised a channel: it began replying
with the *system's* bracketed format — `[waiting]`, `[Listening.]`,
`[Silence — 5 seconds]` — which the synthesizer duly spoke. Measured on a
history truncated by barge-in, where the model has least to go on: 5 in 16
against 0 in 16 before the change. A reserved format is as leakable as a
reserved word.
"""

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from string import punctuation

from voice_agent.streams import closing

logger = logging.getLogger(__name__)

DECLINE = "NOTHING"
"""What the model answers to say it has nothing worth saying."""


def is_decline(reply: str) -> bool:
    """Whether a whole reply is the sentinel and nothing else.

    Forgiving about how it comes back — quoted, punctuated, in a case of its
    own — because a decline misread as a line is the failure that gets spoken
    out loud. Measured: `NOTHING?` and `NOTHING,` were both read as lines.
    """
    line = reply.strip().strip("\"'").strip()
    return bool(line) and line.strip(punctuation).strip().casefold() == DECLINE.casefold()


ASIDE_OPENERS = "[("
"""What a stage direction starts with.

Decidable on the first fragment, which is why this is a separate check from the
sentinel's rather than folded into it: nothing has to be held to know a reply
opened with a bracket. Nor is the closing bracket looked for — `[laughs] Sure`
is no more speakable than `[laughs]`, and a voice agent has no legitimate reply
that opens this way.
"""


def is_aside(reply: str) -> bool:
    """Whether a reply is a stage direction rather than something to say."""
    stripped = reply.lstrip()
    return bool(stripped) and stripped[0] in ASIDE_OPENERS


def _still_possible(seen: str) -> bool:
    """Whether what has arrived so far could still turn out to be the sentinel.

    The prefix test is what keeps `guard` from costing anything. As soon as the
    reply diverges — at `Hey`, or at the space in `No problem` — it can never
    be the sentinel, and everything held is released. Only a reply that really
    does begin "N", "NO", "NOT"… is held, and never for more than seven
    characters.
    """
    head = seen.lstrip().lstrip("\"'").lstrip()
    if is_decline(head):
        return True
    # Trailing whitespace is *not* stripped before the prefix test: a space
    # inside the word is proof of divergence, and stripping it kept "No
    # problem." held for a fragment longer than it had to be. Trailing
    # punctuation still is, so a `NOTHING` on its way to `NOTHING.` survives —
    # and a completed sentinel is caught by the line above whatever trails it.
    return DECLINE.casefold().startswith(head.rstrip(punctuation).casefold())


async def guard(
    source: AsyncIterator[str],
    instead: Callable[[], AsyncIterator[str]],
) -> AsyncGenerator[str, None]:
    """Stream a reply, unless it is not speech at all — then ask again.

    Two things are not speech: the whole reply being the decline sentinel, and
    a reply that opens as a stage direction. They are checked differently
    because they are knowable at different moments. An aside is certain at its
    first non-blank character, so nothing is held for it. The sentinel is only
    certain when the stream ends — `No.` starts identically and is a perfectly
    good answer — so the reply is held while it could still *become* the
    reserved word: seven characters at the very most, in practice one fragment.
    Chapter 7 exists to start the voice before the reply is finished, and a
    guard that waited for the last token to check the first would undo it.

    A leak costs one extra call, which is the right trade for a rare event: the
    alternative is speaking `NOTHING` or `[waiting]` aloud, and the alternative
    to *that* is saying nothing at all, which to a direct question reads as a
    crash.
    """
    held: list[str] = []
    released = False
    opened_as_aside = False
    # `closing`, not a bare `async for`: this is a wrapper that loops over a
    # provider's generator and yields onward, which is the exact shape that
    # leaves the provider suspended — and billed — when the wrapper itself is
    # closed mid-reply. `streams.closing` says so in its own docstring, and a
    # test for it caught this the first time it was written without one.
    async with closing(source) as fragments:
        async for fragment in fragments:
            if released:
                yield fragment
                continue
            held.append(fragment)
            seen = "".join(held)
            if is_aside(seen):
                # Known from the first non-blank character, so nothing streams
                # and nothing is spoken before the retry replaces it.
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
        # A reply that merely *starts* like the sentinel and then ends — "No."
        # — is an ordinary short answer and has been waiting on this check.
        for piece in held:
            yield piece
        return

    leak = "a stage direction" if opened_as_aside else "the decline sentinel"
    logger.warning("%s reached an ordinary turn; asking again", leak)
    async with closing(instead()) as retry:
        async for fragment in retry:
            yield fragment
