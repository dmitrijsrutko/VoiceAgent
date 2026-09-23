"""The sentinel must never be spoken, and must never cost more than it has to.

Written after `NOTHING` was said out loud on the public instance, in reply to
someone saying "Nothing specifically. What's on yours?".
"""

from collections.abc import AsyncGenerator, AsyncIterator

import pytest

from voice_agent.decline import DECLINE, _still_possible, guard, is_aside, is_decline


async def stream(*fragments: str) -> AsyncIterator[str]:
    for fragment in fragments:
        yield fragment


async def collect(source: AsyncIterator[str]) -> str:
    return "".join([fragment async for fragment in source])


@pytest.mark.parametrize(
    "reply", [DECLINE, f"{DECLINE}.", f'"{DECLINE}"', f"{DECLINE}?", "  nothing  ", "Nothing!"]
)
def test_the_sentinel_is_recognised_however_it_comes_back(reply: str) -> None:
    assert is_decline(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "Nothing specifically — what's on your mind?",
        "Nothing wrong with a quiet moment.",
        "No.",
        "Not much either.",
        "There is nothing I can do about that.",
    ],
)
def test_an_ordinary_answer_that_merely_contains_the_word_is_not_a_decline(reply: str) -> None:
    """The recognition is whole-reply. A turn is allowed to talk about nothing."""
    assert not is_decline(reply)


async def fail() -> AsyncIterator[str]:
    """A retry that must never be reached. The empty loop is what makes this an
    async generator rather than a coroutine, without an unreachable `yield`."""
    for _ in range(0):
        yield ""
    raise AssertionError("the guard asked again when it had no reason to")


async def test_an_ordinary_reply_passes_through_unchanged() -> None:
    assert await collect(guard(stream("Hey", " there", "."), fail)) == "Hey there."


async def test_fragments_keep_their_shape() -> None:
    """Chapter 7 speaks the reply as it is written, and the synthesizer's chunk
    schedule reads the pieces it is given. A guard that re-joined them would
    change when the voice starts."""
    pieces = [f async for f in guard(stream("Hey", " there", "."), fail)]

    assert pieces == ["Hey", " there", "."]


async def test_a_leaked_sentinel_is_never_yielded_and_the_model_is_asked_again() -> None:
    async def instead() -> AsyncIterator[str]:
        yield "Not much either. "
        yield "What brings you here?"

    said = await collect(guard(stream(DECLINE), instead))

    assert DECLINE not in said
    assert said == "Not much either. What brings you here?"


async def test_a_sentinel_arriving_in_pieces_is_still_caught() -> None:
    async def instead() -> AsyncIterator[str]:
        yield "Fair enough."

    assert await collect(guard(stream("NOT", "H", "ING"), instead)) == "Fair enough."


async def test_a_short_answer_that_starts_like_the_sentinel_survives() -> None:
    """`No.` is a prefix of the sentinel and a perfectly good reply. It is the
    one case that cannot be decided until the stream ends."""
    assert await collect(guard(stream("No."), fail)) == "No."


async def test_nothing_is_held_once_the_reply_has_diverged() -> None:
    """The cost of the guard is the point: it must not buffer a whole reply
    waiting to check its first word, or Chapter 7's streaming is undone."""
    released: list[str] = []

    async def slow() -> AsyncIterator[str]:
        yield "Hey"
        released.append("first fragment was released before the second was asked for")
        yield " there."

    assert await collect(guard(slow(), fail)) == "Hey there."
    assert released, "the guard buffered past the point the reply could be the sentinel"


async def test_an_empty_reply_is_left_alone() -> None:
    """Empty is a different failure from declining, and not this guard's to fix."""
    assert await collect(guard(stream(), fail)) == ""


@pytest.mark.parametrize("seen", ["", "N", "no", "NOT", '"noth', "  NOTHING"])
def test_a_prefix_of_the_sentinel_is_still_possible(seen: str) -> None:
    assert _still_possible(seen)


@pytest.mark.parametrize("seen", ["Hey", "No ", "Not much", "NOTHINGS", "I"])
def test_a_reply_that_has_diverged_is_not(seen: str) -> None:
    assert not _still_possible(seen)


class Countable:
    """A provider's stream that says whether it was left open."""

    def __init__(self, *fragments: str) -> None:
        self._fragments = fragments
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[str]:
        for fragment in self._fragments:
            yield fragment

    def __call__(self) -> AsyncIterator[str]:
        return self._run()

    async def _run(self) -> AsyncIterator[str]:
        try:
            for fragment in self._fragments:
                yield fragment
        finally:
            self.closed = True


async def test_closing_the_guard_closes_the_provider_underneath_it() -> None:
    """The guard loops over a provider's generator and yields onward, which is
    the exact shape `streams.closing` exists for: without it the provider stays
    suspended at its yield, and billed, when the reader stops mid-reply."""
    provider = Countable("Hey", " there", ", here is a long answer.")
    guarded: AsyncGenerator[str, None] = guard(provider(), fail)

    assert await anext(guarded) == "Hey"
    await guarded.aclose()

    assert provider.closed, "a reply nobody will receive is still being generated"


# --- stage directions -------------------------------------------------------
#
# The second leak of the same family, caused by the first fix: removing the
# named sentinel left the base prompt offering silence with no way to express
# it, and the model borrowed the system's own bracketed channel.


@pytest.mark.parametrize(
    "reply",
    [
        "[waiting]",
        "[Listening.]",
        "[Silence — 5 seconds]",
        "(pause)",
        "  [I'll wait]",
        "[*waiting for the person to speak*]",
        "[laughs] Sure, I can help with that.",
    ],
)
def test_a_reply_that_opens_as_a_stage_direction_is_not_speech(reply: str) -> None:
    """The closing bracket is not looked for: `[laughs] Sure` is no more
    speakable than `[laughs]`, and the whole reply is synthesised either way."""
    assert is_aside(reply)


@pytest.mark.parametrize(
    "reply",
    ["", "   ", "Sure, I can help.", "Take your time.", "No.", "I'd say [sic] is wrong there."],
)
def test_ordinary_speech_is_not_a_stage_direction(reply: str) -> None:
    assert not is_aside(reply)


async def test_a_stage_direction_is_never_spoken_and_the_model_is_asked_again() -> None:
    async def instead() -> AsyncIterator[str]:
        yield "Take your time."

    said = await collect(guard(stream("[wait", "ing]"), instead))

    assert "[" not in said
    assert said == "Take your time."


async def test_an_aside_is_caught_on_its_first_fragment() -> None:
    """Nothing after the opening bracket is even read, so no part of a stage
    direction can reach the synthesizer while the retry is still in flight."""
    read: list[str] = []

    async def source() -> AsyncIterator[str]:
        for fragment in ("[", "waiting", "]"):
            read.append(fragment)
            yield fragment

    async def instead() -> AsyncIterator[str]:
        yield "Sure."

    assert await collect(guard(source(), instead)) == "Sure."
    assert read == ["["], "the guard kept reading past a decided aside"
