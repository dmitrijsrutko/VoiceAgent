"""LocalAgreement, driven by a real partial sequence captured from Scribe."""

import itertools

import pytest

from voice_agent.stt.agreement import StablePrefix, normalize

# Verbatim from a live session: seven seconds of speech, eight partials, then
# the commit. Three of the seven transitions revise punctuation or case rather
# than words, which is the whole reason comparison is normalised.
TRACE = [
    "I would like to understand-",
    "I would like to understand how the Mill",
    "I would like to understand how the Millennium Prize Problems",
    "I would like to understand how the Millennium Prize problems relate to modern",
    "I would like to understand how the Millennium Prize problems relate to modern cryptography.",
    "I would like to understand how the Millennium Prize problems relate to modern "
    "cryptography, and whether any of them",
    "I would like to understand how the Millennium Prize problems relate to modern "
    "cryptography, and whether any of them have practical consequences.",
    "I would like to understand how the Millennium Prize problems relate to modern "
    "cryptography, and whether any of them have practical consequences.",
]
COMMITTED = TRACE[-1]


def feed(trace: list[str], agree_after: int = 2) -> tuple[StablePrefix, list[str]]:
    prefix = StablePrefix(agree_after=agree_after)
    return prefix, [grown for p in trace if (grown := prefix.update(p)) is not None]


def test_agreement_tracks_the_real_utterance() -> None:
    prefix, growths = feed(TRACE)

    assert growths, "nothing ever stabilised"
    assert prefix.text == COMMITTED.rstrip(), (
        "the whole utterance should settle by the last partial"
    )
    assert prefix.contradictions == 0


def test_the_stable_prefix_only_ever_grows() -> None:
    """A cache prefix that rewrites its own middle is not a prefix."""
    _, growths = feed(TRACE)

    for earlier, later in itertools.pairwise(growths):
        assert later.startswith(earlier), f"{earlier!r} was rewritten into {later!r}"


def test_what_we_called_stable_is_really_what_was_committed() -> None:
    prefix, _ = feed(TRACE)

    assert prefix.holds_for(COMMITTED)


def raw_progress(trace: list[str]) -> list[int]:
    """How many words a non-normalising agreement would have settled on."""
    recent: list[list[str]] = []
    settled = []
    for partial in trace:
        recent.append(partial.split())
        if len(recent) > 2:
            recent.pop(0)
        if len(recent) < 2:
            settled.append(0)
            continue
        count = 0
        for column in zip(*recent, strict=False):
            if len(set(column)) != 1:
                break
            count += 1
        settled.append(count)
    return settled


def test_normalising_is_never_behind_raw_comparison_and_is_sometimes_ahead() -> None:
    """Raw comparison does not stall permanently — a later partial confirms the
    new spelling and it catches up. What it loses is a *round* at each revision:
    measured on this trace, one word at each of the three respellings, about a
    second each at the recognizer's one-partial-per-second cadence.
    """
    prefix = StablePrefix()
    normalised = []
    for partial in TRACE:
        prefix.update(partial)
        normalised.append(len(prefix.text.split()))
    raw = raw_progress(TRACE)

    assert all(n >= r for n, r in zip(normalised, raw, strict=True))
    assert sum(n > r for n, r in zip(normalised, raw, strict=True)) == 3


def test_nothing_is_stable_until_it_has_been_said_twice() -> None:
    prefix = StablePrefix()

    assert prefix.update("hello there") is None
    assert prefix.update("hello there friend") == "hello there"


def test_a_revised_word_never_becomes_stable() -> None:
    """Acting on a hypothesis means acting on words the user never said."""
    prefix = StablePrefix()
    prefix.update("send it to Bob")
    prefix.update("send it to Rob")

    assert prefix.text == "send it to"


@pytest.mark.parametrize(
    ("a", "b"),
    [("Problems", "problems"), ("cryptography.", "cryptography,"), ("understand-", "understand")],
)
def test_normalisation_covers_the_revisions_seen_in_practice(a: str, b: str) -> None:
    assert normalize(a) == normalize(b)


def test_a_higher_threshold_trades_lag_for_certainty() -> None:
    _, two = feed(TRACE, agree_after=2)
    _, three = feed(TRACE, agree_after=3)

    assert len(three) < len(two), "a stricter threshold should stabilise less often"


def test_reset_forgets_the_previous_utterance() -> None:
    prefix, _ = feed(TRACE)
    prefix.reset()

    assert prefix.text == ""
    assert prefix.update("something else entirely") is None
