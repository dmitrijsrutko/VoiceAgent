"""What a conversation has learned about the language it is being spoken in.

A recognizer does not refuse a language it does not know: it decodes the audio
with the wrong acoustic model and returns confident nonsense. The ElevenLabs
trace of 2026-09-30 is the shape of it — a sentence of Russian written as
Turkish letters ("Karandaz oğğl, lütshe çem ruchka"), then an attempt at the
same sentence committed as nothing at all.

The recognizer can be told what to expect, and the vendor prices the two ways
of telling it very differently: `language_code` is a strong prior, and
`secondary_languages` merely narrows detection to a set, where its own answer
stays an answer. So the memory is **two-tier**:

- the first time a language is heard it is a *candidate* — sent as
  `secondary_languages`, which makes the run more reliable without making a
  misdetection final;
- the second time it is *pinned* as `language_code` — worth a strong prior,
  because it has now been seen twice.

Both recognizers take the same two tiers in their own spelling: AssemblyAI gets
one `language_codes` list, where a single element is its monolingual session and
the bias is soft rather than a hard pin.

`record` closes the loop that matters: a commit contradicting the pin we sent
*demotes* the pin back to a candidate instead of being read as confirmation, and
a language heard during a session we did not guide at all is taken at face
value. Without both, a pin that the recognizer itself produced could confirm
itself forever.

Codes are kept in the vendor's own convention and never normalised: this is
state a vendor is handed back, one recognizer listens at a time, and mapping
639-3 to 639-1 by hand is a way to invent a fact.
"""

from dataclasses import dataclass, field

from voice_agent.stt.base import LanguageHint

CANDIDATES_KEPT = 2
"""Languages remembered as candidates, most recent first.

Two, not a list: this is a hint about *this* conversation, so a running tab of
every language ever heard would eventually name every language and narrow
nothing. Two also covers the case that started this — speech that is partly
Russian and partly Kazakh.
"""


@dataclass
class LanguageMemory:
    """One conversation's language: what has been heard, and what is pinned."""

    candidates: list[str] = field(default_factory=list)
    """Most recent first, at most `CANDIDATES_KEPT`. The first is what the
    next session is narrowed to."""

    pin: str | None = None
    """Sent as `language_code`. A strong prior, so only set once a language has
    been heard twice."""

    def hint(self) -> LanguageHint | None:
        """What to tell the next recognizer session, or `None` before anything
        has been heard — in which case the recognizer guesses, as it must."""
        if not self.candidates and self.pin is None:
            return None
        return LanguageHint(pin=self.pin, candidates=tuple(self.candidates))

    def record(self, detected: str | None, hint: LanguageHint | None) -> None:
        """One commit's language, and the hint that session was opened with.

        `hint` is what makes a contradiction readable. A language coming back
        from a session *we* pinned is the recognizer echoing our own prior and
        says nothing; a different one from that session is the strongest
        evidence there is, and the pin has to go — taking the language that
        contradicted it with it, or the next session would open narrowed to the
        very language that was just shown to be wrong.
        """
        if not detected:
            return
        if hint is not None and hint.pin is not None and detected != hint.pin:
            self.pin = None
        elif detected == self.pin:
            return
        # Reordered before the test for a second sighting: after the new one is
        # put in front of it, that reading is `list[1:]`.
        already_known = detected in self.candidates
        self.candidates = [detected, *(c for c in self.candidates if c != detected)][
            :CANDIDATES_KEPT
        ]
        if already_known:
            self.pin = detected
