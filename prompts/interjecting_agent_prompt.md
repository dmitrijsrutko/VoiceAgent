# Context: cooperative interjection ("reverse barge-in") — a candidate future chapter

Background thinking for consideration, not a plan or a spec. Per AGENTS.md, nothing gets built ahead of its turn. What I want from you is a view on whether this belongs in docs/ROADMAP.md, where it sits in the order, and what it would reuse. Please read AGENTS.md, CHANGELOG.md and docs/ROADMAP.md first. Don't implement anything yet.

## The idea

Chapter 8 lets the user interrupt the agent. The reverse: the agent interjects when there's a reason to, at a natural opening (a pause, a clause boundary), not mid-word.

Reasons to interject: the user says something factually wrong, heads somewhere unproductive, or is about to say something that shouldn't be said aloud (e.g. starting to read out a card number).

Working names: "reverse barge-in" for the capability, "cooperative interjection" for the behaviour. The research area is mixed-initiative dialogue and system-initiated interruption.

## Principles

- **Self-repair first.** People strongly prefer to fix their own mistakes (conversation analysis). Interjecting too early takes that chance away. The question isn't "did I detect an error" but "will this error survive if I wait?"
- **A dial, not a switch.** Cheapest first: backchannel ("mm-hm") → marked backchannel ("hmm?", rising pitch, invites self-repair) → recast (use the correct term in the next turn, no call-out) → short insertion at a clause boundary → hard interrupt only when continuing causes harm. Most of the value should come from the cheap end.
- **False positives cost far more than misses.** Conservative thresholds; poor recall is acceptable.
- **Budget it.** Cap interjections per exchange.
- **Yield gracefully.** If the user keeps talking through an interjection, the agent stops.

## What already exists that this would build on

My reading of the README. Correct me where I'm wrong.

- **LocalAgreement stable prefix (Ch 4, `warming.py`).** The natural input for any "is something wrong?" judgement. Judging only settled text avoids interjecting over a word the recognizer later rewrites.
- **Speculation (Ch 5, `speculation.py`).** The same pattern as preparing an interjection ahead of time and holding it until an opening appears. Could its cancel-on-continue logic and its "cost of wrong guesses" counter carry over?
- **Streaming synthesis and the playback queue (Ch 6–7).** Interjections need to start fast. Short, fixed ones ("hmm?", "mm-hm") might be pre-synthesised and cached like the greeting (`greeting.py`).
- **Open mic while speaking and heard-text tracking (Ch 8, `heard.py`).** The agent can already speak while listening. "What the user actually heard" now needs to cover overlap in both directions.

## The conflict I expect with Chapter 8

Barge-in currently fires on the first recognized word while the agent speaks. If the agent interjects *while the user is still talking*, the user's continuing speech would immediately trigger barge-in and cut the interjection. That might be exactly the right yield behaviour, or it might need a distinct mode: "agent spoke into an opening and the user carried on" is not the same event as "user interrupted the agent's turn." Worth thinking about how session.py and turn.py would tell these apart.

## Dependencies on existing roadmap items

- **Semantic turn detection.** Finding an opening to interject uses the same signals as finding the end of a turn, used inverted: a legitimate micro-opening rather than turn end. It probably has to land first, or at least be designed with this second use in mind.
- **Local VAD in the page.** Barge-in currently takes ~0.85–1.6 s of recognizer lag. Openings are roughly 200–500 ms wide, so interjection timing is likely impossible without a faster local signal.
- **Backchannels and filler** are already on the roadmap. The cheapest rungs of the ladder overlap with that item. Maybe this isn't a separate chapter at all but an extension of backchannels: from "fill silence while thinking" to "respond to what the user is saying."

## Where it fits

The relationship has to license interruption. Good fits: compliance (stop sensitive data being spoken), tutoring and language practice, procedural checklists, interview practice. Poor fits: general support, sales.

For a demo in this project, the sensitive-data case looks best: a clear trigger, pattern-based detection, and a yes/no measure (did the digits reach the transcript?).

## Measurement, in the project's own style

In keeping with "every chapter has a number to beat" and on-screen annotation:
- Interjection precision: was the flagged thing actually a problem?
- Timing: did it land in an opening, or cut across speech? Milliseconds from opening to first audio.
- Yield: how often did the user talk over it, and how fast did the agent stop?
- Cost of speculation: judgements run and interjections prepared but never fired.
- Perceived rudeness: only measurable by human listening; maybe a simple rating in the page.

## Questions for you

1. Does this belong in ROADMAP.md, and where in the order relative to local VAD, semantic turn detection, and backchannels?
2. Is it one chapter or several? Could the first step be only the cheap rungs (marked backchannels) with no real interruption?
3. Where should the "is something wrong?" judgement live: rules, a small fast model, or the main LLM? How does that interact with warming and speculation already sharing the LLM?
4. How does the Chapter 8 barge-in logic need to change to tell "user carried on over my interjection" from "user interrupted my turn"?
5. How could it be tested without live audio? Scripted transcripts with known error points fed through the partial/stable path?
6. What would you not build, and why?

Please push back if this is premature or if I've misread the codebase.
