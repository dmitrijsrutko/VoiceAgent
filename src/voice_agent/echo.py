"""Is what the recognizer heard the user, or the agent's own voice coming back?

A phone on speaker can leak the agent's voice past the browser's echo
cancellation, and the recognizer transcribes it like anyone else. Live, the
agent said "I can help you sign up" and heard "Hello, I can help." — then cut
itself off and answered itself.

Judged by words, not sound: most of what was heard is in what the agent is
saying. A misheard echo ("Hi. Give me a position" → "Hi. Physician help.")
gets past this; a clean one does not.
"""

import re
from typing import Literal

Verdict = Literal["echo", "user", "unsure"]

MIN_WORDS = 2
"""Fewer heard words than this cannot be called echo: one word in common is
what any reply shares with any other."""

ECHO_SHARE = 0.6
"""The share of the heard words that must appear in the agent's own speech."""

FINAL_MIN_WORDS = 3
FINAL_ECHO_SHARE = 0.85
"""Stricter, for dropping a committed turn outright. At the 60 % of a partial,
a user quoting the agent back ("Да, я говорю, что стейк самая вкусная еда")
reads as echo; delaying their barge-in is tolerable, losing their turn is not.
The cost: a misheard echo with extra words ("Hello, I can help." is 75 %) can
still be answered."""

RECENT_WORDS = 30
"""How much of the reply a barge-in is judged against. Measured against the
71-word reply that swallowed a user's question: 30 interrupts it immediately and
still recognises echo 26 words back; 45 and above never interrupt it. Chosen
inside that range rather than at its edge, since where the edge falls depends on
the wording. Raises, not lowers, the chance of the agent cutting itself off on
its own leaked voice — the failure the module's docstring says is the cheaper
one to make."""

WORD = re.compile(r"[\w']+")


def words(text: str) -> list[str]:
    return [w.casefold() for w in WORD.findall(text)]


def verdict(heard: str, said: str) -> Verdict:
    """`echo` when most of what was heard is in what the agent said; `user`
    when there is evidence it is not; `unsure` when a word or two cannot tell
    yet — a single word the agent also used waits for the next one."""
    got = words(heard)
    if not got:
        return "unsure"
    spoken = set(words(said))
    shared = sum(1 for w in got if w in spoken)
    if len(got) >= MIN_WORDS and shared / len(got) >= ECHO_SHARE:
        return "echo"
    if len(got) < MIN_WORDS and shared == len(got):
        return "unsure"
    return "user"


def recent(text: str, keep: int = RECENT_WORDS) -> str:
    """The tail of what the agent has said, for judging a barge-in against.

    A reply's text is complete within a second of the model finishing, while its
    audio plays for as long as it takes to say — so judging a partial against the
    *whole* reply compares it with words that will not be heard for another
    twenty seconds. Live, a user's question — "What is the best or biggest…" —
    shared `what`, `is`, `the` and `or` with a long reply and was judged the
    agent's own voice, so the agent talked on and only a word it had never said
    ("wait", seven times) could break through.

    A window fixes that, and costs echo coverage: an echo can only be recognised
    while the words it leaked are still inside the window. Measured against that
    reply, `RECENT_WORDS` of 30 interrupts the user's question immediately and
    still recognises echo up to 26 words back; 45 and above never interrupts it
    at all.

    The window is anchored to the end of the text, which is not where playback
    is. Anchoring it properly needs the playout position, which only the browser
    tracks (`karaoke.js`) — a chapter of its own.
    """
    matches = list(WORD.finditer(text))
    if len(matches) <= keep:
        return text
    return text[matches[-keep].start() :]


def is_echo_final(heard: str, said: str) -> bool:
    """Whether a committed turn is so plainly the agent's own words that it
    may be dropped unanswered.

    Judged against the whole reply, not `recent`: dropping a real turn is the
    worse mistake (`echo.py` says so), so this stays the stricter test.
    """
    got = words(heard)
    if len(got) < FINAL_MIN_WORDS:
        return False
    spoken = set(words(said))
    return sum(1 for w in got if w in spoken) / len(got) >= FINAL_ECHO_SHARE
