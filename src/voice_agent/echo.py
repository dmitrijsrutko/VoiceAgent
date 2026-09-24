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


def is_echo_final(heard: str, said: str) -> bool:
    """Whether a committed turn is so plainly the agent's own words that it
    may be dropped unanswered."""
    got = words(heard)
    if len(got) < FINAL_MIN_WORDS:
        return False
    spoken = set(words(said))
    return sum(1 for w in got if w in spoken) / len(got) >= FINAL_ECHO_SHARE
