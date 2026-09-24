# Scenarios for the inner voice

Scripted conversations with labelled moments, replayed through the real
thinker by `uv run voice-agent --replay-thinker` (billed), and through a fake
one by `tests/test_replay.py` (free).

- `role:` on the first line names the role card.
- `Partner:` is a line the agent said. The thinker is asked after it (`reply`).
- `User:` is one utterance. Each ` | ` is a micro-pause: the thinker is asked
  there, hearing everything said so far in the utterance.
- A label right after a pause scores what the thinker said there:
  - `{expect challenge}`: a window. A thought of urgency 2 or more, at this
    pause or the next, is a **hit**; naming this move is a **right move**.
  - `{clean}`: nothing urgent is warranted. Urgency 2 or more is a **false fire**.
  - Unlabelled pauses are asked but not scored.
