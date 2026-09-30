"""The language memory: a conversation's guess at what language it is in.

The failure these tests are written around is live, from 2026-09-30: a sentence
of Russian was decoded as Turkish ("Karandaz oğğl, lütshe çem ruchka"), and the
attempt to repeat it committed nothing at all. The memory has two jobs and they
are tested one at a time — never let a single sighting pin a conversation, and
never let a pin confirm itself.
"""

import pytest

from voice_agent.language import LanguageMemory
from voice_agent.stt.base import LanguageHint


@pytest.fixture
def memory() -> LanguageMemory:
    return LanguageMemory()


def test_nothing_is_known_before_anything_is_heard(memory: LanguageMemory) -> None:
    """The first session must guess: there is nothing else it can honestly do,
    and a hint invented out of nothing is worse than none."""
    assert memory.hint() is None


def test_the_first_sighting_is_a_candidate_and_pins_nothing(memory: LanguageMemory) -> None:
    """The headline rule. A pin is a strong prior, so a single utterance may not
    produce one — and a misdetection arrives as exactly that, one utterance."""
    memory.record("rus", None)

    assert memory.hint() == LanguageHint(pin=None, candidates=("rus",))


def test_the_second_sighting_pins_it(memory: LanguageMemory) -> None:
    memory.record("rus", None)

    memory.record("rus", LanguageHint(candidates=("rus",)))

    assert memory.hint() == LanguageHint(pin="rus", candidates=("rus",))


def test_a_pin_coming_back_is_not_confirmation(memory: LanguageMemory) -> None:
    """The loop that would make a wrong pin permanent: the recognizer was told
    to expect Russian, so it says Russian again. Only a language heard with no
    pin in force counts toward pinning."""
    memory.record("rus", None)
    memory.record("rus", LanguageHint(candidates=("rus",)))
    assert memory.pin == "rus"

    # Sessions run on the pin report it back, as a recognizer steered to a
    # language does. None of them may be read as a second sighting.
    memory.record("rus", LanguageHint(pin="rus", candidates=("rus",)))
    memory.record("rus", LanguageHint(pin="rus", candidates=("rus",)))

    assert memory.hint() == LanguageHint(pin="rus", candidates=("rus",))


def test_a_language_that_contradicts_the_pin_demotes_it(memory: LanguageMemory) -> None:
    """A recognizer answering with something other than the language it was
    told to expect is the clearest evidence there is that the pin is wrong, so
    the next session must not open pinned to it — nor narrowed to it *first*,
    which would be the same trap one round later. The pin's language stays a
    candidate: the session that saw the contradiction was the biased one."""
    memory.record("rus", None)
    memory.record("rus", LanguageHint(candidates=("rus",)))

    memory.record("kaz", LanguageHint(pin="rus", candidates=("rus",)))

    assert memory.hint() == LanguageHint(pin=None, candidates=("kaz", "rus"))


def test_a_contradicting_language_with_no_pin_is_heard_as_a_candidate(
    memory: LanguageMemory,
) -> None:
    """Unpinned, the recognizer's answer is just what it heard: it becomes the
    candidate the next session is narrowed to, and pins on its second sighting."""
    memory.record("rus", None)

    memory.record("kaz", LanguageHint(candidates=("rus",)))
    assert memory.hint() == LanguageHint(pin=None, candidates=("kaz", "rus"))

    memory.record("kaz", LanguageHint(candidates=("kaz", "rus")))
    assert memory.hint() == LanguageHint(pin="kaz", candidates=("kaz", "rus"))


def test_at_most_two_candidates_are_kept(memory: LanguageMemory) -> None:
    """This is a hint about *this* conversation. A running tab of every language
    ever heard would end up naming every language and narrowing nothing."""
    for code in ("rus", "kaz", "tur"):
        memory.record(code, None)

    assert memory.hint() == LanguageHint(pin=None, candidates=("tur", "kaz"))


def test_a_commit_with_no_language_changes_nothing(memory: LanguageMemory) -> None:
    """A recognizer that reports no language must leave the memory exactly as it
    was, rather than emptying it or counting as a sighting."""
    memory.record("rus", None)
    memory.record("rus", LanguageHint(candidates=("rus",)))

    memory.record(None, LanguageHint(pin="rus", candidates=("rus",)))

    assert memory.hint() == LanguageHint(pin="rus", candidates=("rus",))
