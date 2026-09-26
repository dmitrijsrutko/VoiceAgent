# Changelog

This project is built up in **chapters**, each adding one deliberate piece of
functionality on top of a working, fully-tested previous chapter. At every
point in this history the code runs and its tests pass — it is simply not the
whole voice agent yet.

Guiding idea: **the orchestration is the asset, not the vendors.** STT, LLM,
and TTS providers get swapped constantly; the pipeline, the turn-taking logic,
the interruption handling, and the latency work are what accumulate chapter by
chapter and are what actually make a voice agent feel human.

Newest chapter first. Each entry says *why* the chapter was the right next
step — the diff already says what changed. The chapter entry format is
specified in [AGENTS.md](AGENTS.md#5-documentation-is-part-of-every-chapter).

## Chapter 27 — The voice is a choice: Multilingual v2 beside Flash v2.5

The agent spoke only through `eleven_flash_v2_5`, which was chosen for time to first
byte. This chapter aims at how the voice *sounds*. The start screen offers a second
ElevenLabs model, **Multilingual v2** (the vendor's "most advanced, emotionally-aware"
model), and makes it the default. Next chapter adds and evaluates Eleven v3
Conversational.

**What changed**
- `tts/registry.py`: `MENU` has two `Option`s (name, provider, model, title, hint):
  `multilingual-v2` (default) and `flash-v2.5`. `DEPRECATED` lists the models the
  vendor has retired, and `create_tts` refuses them.
- `backends.py`: the voice is picked like the ears. `?tts=` is pinned on
  `Conversation.voice` at first connect, an unknown name falls back to the default,
  and one `ElevenLabsTTS` is built lazily per option and shared.
- `server.py`: `Agent.speaker` is gone, and each conversation resolves its own. The
  `voice` facts carry `model` and `choice`, and the per-connect log line names both.
- `record.py` / `app.js`: the header and the page's meta line name the voice model.
- `start.js`: Voice is a real picker built from `choices.tts`, titled by the server,
  and `stackQuery` sends `tts`.
- `TTS` protocol: gains a `model` property.

**Design decisions**
- **Same adapter, same socket.** Multilingual v2 runs on `stream-input` with the same
  `pcm_24000`, chunk schedule and `alignment`, so word marks, barge-in and echo are
  untouched. Only the URL's `model_id` differs.
- **An option maps to a provider and a model, not just a model id.** v3 Conversational
  is on the Text-to-Dialogue socket, so it will be a second adapter behind `TTS`.
- **Same `chunk_length_schedule` and no `voice_settings`.** The model is the only
  variable. Tuning either comes later.
- **Multilingual v2 is the default by the user's decision**, knowing it is slower and
  bills about 2× Flash per character. It hears 29 languages, not 32 (no Hungarian,
  Norwegian or Vietnamese). The prompt's languages come from the ears, so nothing
  depends on it.
- **No deprecated models** (checked on elevenlabs.io/docs/overview/models, 2026-09-26).
  Turbo v2/v2.5 are superseded by Flash, and the v1 models were removed 2026-07-09. A
  test keeps them off the menu.

**Latency impact** — measured with 10 live syntheses, 5 replies per model, interleaved,
fed in 4-character fragments at 760 chars/s. This is first audio from the first
fragment, including connect:

| Model | p50 | min–max |
| --- | --- | --- |
| Flash v2.5 | 378 ms | 362–417 |
| Multilingual v2 | 857 ms | 800–935 |

The default costs **about +480 ms** on every reply, and on its own it takes the §7
target (TTS ≤ 150 ms) past the whole 800 ms budget. Audio length was the same for both
(±0.1 s on 4 of 5 replies).

**Deliberately not done** — v3 Conversational (next chapter), per-model tuning, a
voice-id picker, and a CLI/env default (the default is a constant, like the LLM's).

**Verification** — `uv run verify`: 715 tests pass, and the tapes did not move. The
live A/B above produced 10 non-silent 24 kHz clips, and both model ids were accepted by
the socket. In headless Chrome the start screen shows Voice with two options and
Multilingual v2 checked. One typed conversation per model on a local server, over
`?tts=`, voiced its greeting and its reply with no late audio. The record headers named
`eleven_multilingual_v2 (multilingual-v2)` and `eleven_flash_v2_5 (flash-v2.5)`. Not
observed: the per-connect log line (local logging is off), a spoken barge-in, and a
human listening comparison.

**Fixes**
- A missing `ELEVENLABS_API_KEY` served, then failed every page with a 500, once voices became lazy. `create_app` builds the default voice at startup again, so the server stops with the key's name.

## Refactor — The greeting is spoken like any other reply

The opening line was synthesised at startup, cached as raw PCM under `.cache/`
(keyed by provider, voice, format and text, written atomically, checked for
damage, never retried after a failure) and sent through its own untimed
delivery. That was a lot of machinery for one sentence, and it made the first
message the one message that behaved differently.

**What changed**
- `greeting.py`: the `Greeting` class becomes `greet()`, which appends the
  line and voices it through `turn.Speech`, the same streaming path as every
  reply: word marks, real `audio_end` timings, `audio_error` on a failed voice.
- `server.py`: `Agent.openings` holds plain text; the lifespan no longer
  synthesises anything. `/healthz` waits on the VAD and engine connections only.
- `telemetry.js`: the "cached" audio line is gone.

**Design decisions**
- Synthesised fresh per conversation, not cached: one TTS request per visitor
  is a cost we accept for one audio path. An interrupted greeting is now cut by
  its word marks rather than estimated.

**Latency impact** — the greeting now waits for TTS first byte (150 ms in the
tapes' fake; the real provider's first byte live). Not measured live.

**Verification** — `uv run verify`. Tape goldens regenerated: every tape shifts
by the greeting's 150 ms synthesis; no decision changed.

## Chapter 26 — A start screen for phones, and a record that names the whole stack

The start screen was built at desktop width. On a phone the model rows could
not wrap (`nowrap`, kept from Chapter 23 so a vendor's models never split), so
DeepSeek's three options ran off the right edge; and whether the two vendor
groups sat side by side or stacked depended on the window, so the list read
left, right or vertical from one screen to the next. The global `input` style
also reached the radio buttons, padding and bordering each one. Meanwhile the
defaults were the ones the page opened with before roles existed.

**What changed**
- Defaults: role **Devil's advocate** (`roles.DEFAULT_ROLE`), model
  **`deepseek-max`** (`llm.registry.DEFAULT_CHOICE`), ears **ElevenLabs Scribe**
  (`config.DEFAULT_EARS_PROVIDER`). Each still falls back to the first thing
  offered when its key is missing.
- "None" is no longer a role: `Backends.choices()` offers only cards, and
  `?role=none` falls back to the default like any unknown name. `NO_ROLE` stays
  as what runs with no cards at all, or when an operator sets
  `VOICE_AGENT_ROLE=none` — which the test suite now does explicitly
  (`conftest._plain_assistant`), because its pipeline tests are about the plain
  assistant. `--role` no longer lists `none`.
- The "It will play …" line under the pickers is gone; a role's summary is
  already on its option.
- So is "It hears 100 languages: afr amh …": a hundred codes took half a phone
  screen. Each Ears option still says how many languages it hears.
- Layout: every picker is a grid of equal cells (two per line on desktop, one on
  a phone); models are always one vendor per line, the vendor's name in its own
  column and its models in three equal cells, in menu order — so low / high /
  max line up with Haiku / Sonnet / Opus. Under 480 px the vendor's name sits
  above its row. The text-box style is scoped to `#input`.
- Debugging: the record's header names all four settings, always, and says
  `role none` / `ears deaf` / `voice silent` rather than leaving one out; a
  reconnect restates them. `converse()` logs one line per connect —
  `conversation <id>: role=… llm=… (provider/model) ears=… voice=…` — so the
  Fly logs say what a session ran on without its record. The `ready` frame's
  `role` carries the card's `slug`.

**Latency.** Unchanged for a visitor who picks; a visitor who does not now gets
`deepseek-max`, which Chapter 21 measured at up to 23.7 s on a hard turn,
instead of Haiku 4.5. That is the user's call and is recorded here as such.

**Not doing.** No user-made roles, no role picker redesign beyond layout.

## Chapter 25 — Thirty minutes, not six

The deployed instance ended every conversation at 360 seconds. A real
conversation with the agent — one that gets somewhere — reaches that ceiling
while it is still going, and the record shows it: sessions ending mid-thought
with `ended: reason This public demo limits a conversation to 6 minutes`.

**What changed**
- `fly.toml`: `VOICE_AGENT_SESSION_BUDGET` 360 → **1800**.
- `docs/DEPLOY.md` and `.env.example`: the documented value follows.
- Nothing in code. `limits.py` already derives the spoken reason from the number
  (`f"{minutes:g} minutes"`), so the message says "30 minutes" on its own —
  which is why this is a one-line change rather than a new string to maintain.

**Design decisions**
- **The budget is also the spend ceiling for one address.** Every turn is charged
  for the whole conversation's history, and the history grows with the session,
  so a longer conversation costs more than proportionally. With `VOICE_AGENT_MAX_LIVE`
  at 4 and `deepseek-max` reasoning for thousands of tokens a turn, thirty
  minutes is a materially larger exposure than six. Raised on request, with that
  said once rather than argued: `MAX_LIVE` remains the bound that matters most,
  and `--purge-sessions` and the caps are all still there.
- **Not a per-conversation token cap.** A budget in seconds is what a person
  understands; a token budget is what actually costs. If spend needs bounding
  rather than duration, that is a different change and belongs with cost
  accounting (README's next chapters).

**Latency impact** — none. This bounds how long a conversation may run, not how
fast any part of it is.

**Verification** — `uv run verify` unchanged and passing; the value is read from
`fly.toml` at deploy, and `limits.py`'s existing tests cover the parsing and the
spoken reason. Confirmed after deploying by reading the banner and the caps the
live instance reports.

## Chapter 24 — The record says which option ran, not just which model

Chapter 19 made the model a choice of six, and the record kept naming only the
provider and the model. That was enough while one provider meant one model. It
stopped being enough the moment three of the six became *one* provider and *one*
model differing only in the effort sent with the request — `deepseek-low`,
`deepseek-high` and `deepseek-max` all record as `deepseek/deepseek-flash`.

Live, that cost a diagnosis. A session waited **3–12 s for its first token on 10
of 14 turns** and could not be traced to a tier at all. The logs cannot help
either: every engine is built at startup, so `building the … engine` names all
six and never the one chosen. (The recognizer *is* traceable — `building the
elevenlabs recognizer` at 19:22:48 is that conversation's own pick — precisely
because recognizers are built lazily and engines are not.)

**What changed**
- `server.py`: the `ready` frame carries `choice`, the menu option's id.
- `record.py`: the session header names it in brackets —
  `deepseek/deepseek-flash (deepseek-max) · voice … · ears …`.
- `tests/test_record.py`: a test that connects with `?llm=deepseek-max` and
  asserts the header says so. Nothing tested this before, which is why the gap
  survived three chapters.

**Design decisions**
- **The option id, not the effort.** The id is what `?llm=` carries and what the
  conversation is pinned to, so it is the thing a reader can act on; the effort
  is one field inside it and would need a second lookup to be useful.
- **In the header, beside the model rather than instead of it.** The model is
  what the provider was asked for; the option is what the deployment offered.
  Both are true and they can differ — a fallback shows the model that ran and the
  option that was asked for only if the choice is recorded before the fallback.

**Latency impact** — none. One extra field on a frame sent once per conversation.

**Deliberately not done**
- Nothing is logged per turn, so the choice is on the header only. A turn-level
  line would answer "which option is slow right now" across sessions, which is a
  different question and a separate change.
- The 3–12 s turns themselves are untouched. This chapter makes them traceable,
  not faster.

**Verification** — `uv run verify`: ruff, format, mypy (strict) and **704 tests**
pass. The header shape was confirmed by the new test, which also asserts the
model is still named beside the option.

## Chapter 23 — Two lines, not four: the model list reads as two groups

Chapter 19 put the six models in one wrapping row and it came out ragged — the
options broke wherever they happened to fit, one or two to a line. Grouping them
by provider fixed the order and not the wrapping, because every title still
carried its vendor's name: "DeepSeek V4.1 Flash — low effort" is wide enough that
three of them do not fit the start column, so DeepSeek split across two lines and
the list was as tall as before. Rendered headlessly and counted, rather than
assumed.

**What changed**
- `llm/registry.py`: titles lose the vendor — `Haiku 4.5`, `Sonnet 5`,
  `Opus 5.5`, `V4.1 Flash — low/high/max`. The page already prints "Claude" and
  "DeepSeek" once, over the group.
- `web/start.js` / `index.html`: the groups sit in `.rows`, and a `.row` never
  wraps internally, so a provider's three models are always on one line.

**Design decisions**
- **The vendor belongs to the group, not to every option.** Saying it six times
  was not just noise: it was what pushed a group past the column width, so
  removing it is what makes one line per provider possible at all.
- **Still two lines, not one.** The start column is `34em`; six readable labels
  plus two group names need roughly twice that. One line is reachable by widening
  the whole column, which changes every other group on the screen, and was not
  this change.
- **The page names vendors, never models.** `PROVIDERS` maps `anthropic` to
  "Claude" for a heading; the options and their titles still come from the server
  (`test_the_model_options_are_named_by_the_server_not_the_page`).

**Verification** — `uv run verify`: ruff, format, mypy (strict) and **703 tests**
pass. The start screen was then rendered by headless Chrome against a local
server and looked at: `Model` draws two lines, `Claude` over three options and
`DeepSeek` over three, with no vendor name repeated inside a button.

## Chapter 22 — The tail of the reply: a barge-in is judged against what was just said

From the record of session `EsMV3ILeZm9rgDgdXCKUkA`. The agent answered a
question about photosynthesis with **71 words / 419 characters**, which its voice
took **26.471 seconds** to say:

```
`chars 419 · fragments 84 · output_tokens 283 · ...`
`audio_end: bytes 1270596 · chunks 19 · seconds 26.471 · ...`
```

The prompt asks for one to three sentences (`prompts/system_prompt.md:21`); the
model wrote five to seven. The user then tried to ask a question over it and had
to repeat themselves seven times:

```
What is the best or biggest— wait, wait, wait, wait, wait, wait, wait.
```

The interruption landed **15.36 seconds** into the reply (`truncated: played_ms
15360`), and the record shows only one `interrupt` and one `echo_ignored: stage
partial` for all of it.

**Why their own question would not interrupt.** Run against the project's own
`echo.verdict`, the partials of that question are judged the agent's own voice:

```
 2 words  shared=2/2  verdict=echo     'What is'
 3 words  shared=3/3  verdict=echo     'What is the'
 5 words  shared=4/5  verdict=echo     'What is the best or'
 6 words  shared=4/6  verdict=echo     'What is the best or biggest'
 7 words  shared=4/7  verdict=user     <-- INTERRUPT   '...biggest wait'
```

Their question shares `what`, `is`, `the` and `or` with a reply about
photosynthesis — because a reply's *text* is complete within a second of the
model finishing, while its *audio* plays for another twenty-six. Judging a
partial against the whole reply compares it with words that will not be heard for
another twenty seconds. Only `wait` — a word the agent never said — pulled the
share under `ECHO_SHARE`, which is why the one thing that worked was the one word
that was not part of their question.

**What changed**
- `echo.py`: `RECENT_WORDS = 30` and `recent(text, keep)`, the tail of what the
  agent has said.
- `session._on_speech` judges a barge-in against `echo.recent(said)`.
- `is_echo_final` is deliberately **not** windowed: dropping a real turn is the
  worse mistake, so the committed-turn test stays the stricter, whole-reply one.

**Why 30, and what it costs.** Measured against this reply:

| window | user's question interrupts | echo still caught back to |
| --- | --- | --- |
| 15 | immediately | 11 words |
| **30** | **immediately** | **26 words** |
| 40 | immediately | 36 words |
| 45 | never | 41 words |

The window trades one failure for the other: a shorter memory interrupts the user
at once but stops recognising an echo once the words it leaked fall out of the
window. 30 sits inside that range rather than at its edge — where the edge falls
depends on the wording — and the module's own docstring says the cheaper mistake
is cutting itself off, not swallowing the user.

**Design decisions**
- **Not better echo cancellation, and not by preference.** The browser is already
  asked for `echoCancellation: true, noiseSuppression: true, autoGainControl:
  true` (`web/mic.js`), and the agent's own playback goes through a separate
  `AudioContext` and AudioWorklet, which is the case where that cancellation is
  least reliable. Cancelling it ourselves means an adaptive filter against the
  PCM we synthesised, aligned to a playout clock we do not have; the browser has
  the real render clock and we do not.
- **The text gate cannot ever be immediate.** Recognizer partials arrive 0.6-2 s
  after the words; the audio path is the only one that can react in tens of
  milliseconds. The server already runs Silero on the raw mic frames, so what is
  missing is not speed but a way to tell the user's voice from the agent's in
  that domain.
- **The correct anchor is the playout position, not the end of the text.** The
  browser already tracks it — `karaoke.js` maps words to played samples — and the
  server never sees it. A client that reported it would let the gate match what
  is actually in the air, which fixes the misalignment this chapter works around.

**Latency impact** — none. This decides *whether* to interrupt, not how fast the
pipeline runs. It does not shorten the 26 seconds.

**Deliberately not done**
- **The long reply is untouched.** The agent will still talk for 26 seconds; what
  changed is that a user can now stop it immediately. Bounding spoken length in
  the pipeline, rather than trusting the prompt, is the other half.
- Anchoring the gate to the real playout position, above.
- The record still does not say which of the six models ran, so this session's
  tier remains unknown.

**Verification** — `uv run verify`: ruff, format, mypy (strict) and **703 tests**
pass. Two are new: the real question and the real reply from this incident, which
the whole-reply test still judges `echo` (the trap) and the windowed test judges
`user` — a regression test that would have caught the live failure.

## Chapter 21 — Room to think: the output cap is not only the reply's

Chapter 20's guard fired in production within three minutes of being deployed,
which is how this was found at all:

```
WARNING voice_agent.turn: turn failed for session TnUksyRe9BvNAikGO6FZpQ:
  deepseek sent no text for deepseek-flash at effort max after reporting 1024 output tokens
```

That session ran `deepseek-max`, and the escalation across it is the whole
diagnosis:

| turn | output tokens | ttft | reply | outcome |
| --- | --- | --- | --- | --- |
| 14:41:51 | 18 | 664 ms | 63 chars | fine |
| 14:42:03 | 77 | 1113 ms | 75 chars | fine |
| 14:42:19 | 151 | 1126 ms | 407 chars | fine |
| 14:42:47 | 640 | 4144 ms | 396 chars | fine |
| 14:43:15 | **1024** = the cap | — | **0 chars** | failed |

So the mechanism Chapter 20 inferred is confirmed rather than suspected:
`max_tokens` is shared with the chain of thought, and at effort `max` a hard turn
spends the whole allowance reasoning and never answers. The guard did its job —
the turn failed loudly, named the model and the level, dropped the dangling
question, and the record shows an error where it would have shown silence.

**What changed**
- `llm/base.py`: `MAX_OUTPUT_TOKENS` **1024 → 8192**, and its docstring rewritten
  around what the number actually governs.
- `tests/test_llm.py`: the cap is pinned above the measured reasoning footprint,
  so nobody lowers it back to a value that reintroduces the silence.

**Why 8192.** Measured, not chosen: with the cap lifted for a probe, a hard turn
at `max` used **4389 output tokens** and answered — 4.3x the old ceiling, which
is exactly why 1024 failed. 8192 leaves room for the deepest footprint measured
so far and for an answer after it. The same probe at `high` used 1393.

**Design decisions**
- **The ceiling is a shared budget, so it is not a style knob.** It was 1024
  because replies are "one to three sentences"; under a thinking model that
  reasoning has to fit in the same number, and the brevity it was protecting is
  already enforced by the prompt — measured replies ran 587-737 characters at
  both `high` and `max`, regardless of the cap.
- **The guard stays.** 8192 is headroom, not a guarantee. A turn that exhausts
  even that should still fail visibly, and Chapter 20's refusal is what makes it
  do so.
- **`max` stays in the menu**, on the user's call, knowing the cost below.

**Latency impact** — measured, and it is the uncomfortable half of this chapter.
- Raising the cap does not make `deepseek-max` fast; it makes it *answer*.
- The same hard turn took **23.7 s to its first token**, against a 300 ms target
  for the LLM and an 800 ms end-to-end budget. `high` took 8.0 s. Easy turns at
  `max` still run 1.5-4 s.
- `max` effort is therefore a capability setting for a patient user, not a
  conversational one. It is offered because it was asked for, and
  `turn.SLOW_FIRST_TOKEN_MS` logs every occurrence over 3 s.
- Nothing else moved: `deepseek-low` and the Anthropic tiers behave as before,
  and the prompt's brevity instruction is what keeps answers short.

**Deliberately not done**
- No separate reasoning budget, and no per-provider cap: DeepSeek exposes no
  reasoning token limit, only the shared `max_tokens`, and one ceiling keeps the
  adapters interchangeable.
- The record still does not carry which of the six options ran. The guard's
  message is the only place the level survives.

**Verification** — `uv run verify`: ruff, format, mypy (strict) and **701 tests**
pass. The measurement above is four real billed calls — two at `max` on hard
prompts, two on easier ones — taken through the real adapter with the cap lifted
for the duration of the probe. The live catch is the production occurrence quoted
at the top, read back from the deployed machine's own log and its session record.

## Chapter 20 — Silence is a failure: a reply the provider billed for and never sent

Found by reading the deployed instance's own record, not by a test. One live
turn at 14:21:34 is recorded as a **successful reply that said nothing**:

```
## 14:21:34 — agent
                                        ← no text
`ttft_ms 3243 · output_tokens 444 · prompt_tokens 2967 · accepted_ms 379 · attempts 1`
```

The record omits falsy fields, so a missing `chars` means zero characters and a
missing `fragments` means the stream yielded nothing. Somebody waited **3.2
seconds for silence**, and DeepSeek billed **444 output tokens** for it. The
initiative covered for it twenty seconds later, which is why it read as a slow
turn rather than a broken one.

**Why it was ours.** Chapter 18 switched DeepSeek from `deepseek-chat` to
`deepseek-flash`, and `deepseek-flash` thinks by default. The token tail moved
exactly where thinking would move it:

| | 79 archived sessions | that live session |
| --- | --- | --- |
| agent replies with no text | 0 | 1 |
| calls reporting over 700 output tokens | **0 of 599** | 1 (exactly 1024, the cap) |
| highest output tokens ever recorded | 341 | 1024 |

**The mechanism, inferred rather than proven.** `MAX_OUTPUT_TOKENS` is sent as
`max_tokens`, and in thinking mode the reasoning appears to share that budget
with the answer. The adapter reads only `delta.content` and deliberately drops
`reasoning_content` — correct, it is not speech — so a turn that spends the
budget thinking yields nothing, ends normally, and was recorded as a reply.
`turn.py` already anticipated an empty stream, but only to get the timings right;
nothing noticed that the turn had said nothing at all. Three real calls at `low`,
`high` and `max` all returned text, so this is intermittent and the mechanism is
consistent with the numbers rather than demonstrated by them.

**What changed**
- `llm/base.py`: `refuse_silent_reply(provider, model, effort, wrote, usage)`,
  which raises `ProviderError` when a provider reported output tokens and the
  caller received no text.
- Both adapters call it once their stream ends and their `usage` is filled, so
  the guarantee holds on every provider rather than only where it was seen.
- Nothing else: the turn's existing failure path already drops the dangling
  question, stops any audio begun, logs a warning and tells the page.

**Design decisions**
- **In the adapter, not in the turn.** The adapter is the only place that knows
  both that the provider billed tokens and that it yielded no text. Putting it
  there also covers the initiative and the inner voice, which drain the same
  streams and would fail the same way.
- **The message names the effort.** A session record keeps only provider and
  model, so `deepseek-low`, `deepseek-high` and `deepseek-max` are
  indistinguishable in the archive — which is exactly why this took a
  reconstruction to diagnose. Naming the level in the error is the one place it
  survives. Recording the option id properly is left for its own chapter.
- **Only when the provider said it generated something.** `usage.output_tokens`
  of zero, or no `usage` at all, means unknown rather than silent; refusing
  those would invent failures out of missing information.

**Latency impact** — none. This changes what happens after a stream ends, not
how long any stage takes.

**Deliberately not done**
- The record still does not carry which of the six options ran.
- `MAX_OUTPUT_TOKENS` is unchanged at 1024, and the menu still offers `high` and
  `max`, so the condition this guards against can still occur — it is now a
  visible failure instead of an invisible one.
- The deployment has **not** been redeployed, so the live instance still runs
  the code without this guard.

**Verification** — `uv run verify`: ruff, format, mypy (strict) and **700 tests**
pass. The new tests are a unit check of the refusal and its three non-firing
cases, and a wire test in which a stream carries only `reasoning_content`, the
usage chunk and `[DONE]` — the deployed failure replayed against the local
provider — asserting the turn now fails and names the effort.

## Chapter 19 — The model is the choice: the page offers six, and Haiku 4.5 is the default

Chapter 15 let a visitor pick a *provider*, which was a proxy for a decision
nobody makes. Nobody wants Anthropic; they want the fast one or the careful one.
So the models are the choice now, fastest first, with the provider a property of
each rather than something to pick. Six options, and Claude Haiku 4.5 first and
by default.

| `?llm=` | provider | model | effort |
| --- | --- | --- | --- |
| `haiku-4-5` (default) | anthropic | `claude-haiku-4-5` | none — the model rejects the parameter |
| `sonnet-5` | anthropic | `claude-sonnet-5` | `low` |
| `opus-5-5` | anthropic | `claude-opus-5-5` | `low` |
| `deepseek-low` | deepseek | `deepseek-flash` | `low` |
| `deepseek-high` | deepseek | `deepseek-flash` | `high` |
| `deepseek-max` | deepseek | `deepseek-flash` | `max` |

**What changed**
- `llm/registry.py`: `Choice` and `CHOICES` — the menu — plus `offered()`,
  `default_choice()` and `BY_NAME`. **`Engine.default_effort`** says what each
  engine asks for when a caller names none, and `create_llm` is where that is
  resolved: `None` at the seam means one thing now, not one thing per provider.
- `llm/base.py`: `Effort = Literal["low", "high", "max"]`, the pipeline's own
  vocabulary and narrower than either vendor's.
- Both adapters take an effort per instance, resolved. `AnthropicLLM.effort`
  replaces the module constant `EFFORT`; `OpenAICompatibleSpec.default_effort`
  replaces its `reasoning_effort`. An adapter is handed an instruction, so `None`
  reaching one means send nothing — a mechanism, no longer a policy.
- `llm/anthropic_provider.py`: **`DEFAULT_MODEL` was `claude-opus-5` — the
  *previous* Opus, not 5.5.** It is `claude-opus-5-5` now, and `MODELS` lists the
  four Anthropic currently serves.
- `backends.py`: the pool offers the menu and is keyed on the *option*, so one
  model at two efforts is two adapters holding two connections.
- `web/start.js`: the group is "Model", and each option shows the title the
  server sent.
- **`VOICE_AGENT_PROVIDER`, `VOICE_AGENT_MODEL`, `--provider`, `--model` and the
  two `fly.toml` lines are gone.** The page owns the choice now, so a second way
  to set it was a second source of truth. `Settings` loses the pair, and with it
  Chapter 18's `__post_init__` check: `check_model` runs in `create_llm`, which
  is where every engine is built.

**Design decisions**
- **The title comes from the server, not the page.** `start.js` held a map of
  provider names; a model's name is a fact about the model, and a page naming
  models itself could advertise one the server will not run — the exact bug
  Chapter 17's test was written for.
- **`low`/`high`/`max`, not the vendors' words.** DeepSeek maps `minimal` and
  `low` onto `low`, and `medium`, `high` and `xhigh` onto `high`, so offering
  its seven values would have meant three options sending an identical request.
  These three are the levels that differ.
- **`None` means "no preference", in one place.** The first cut let `None` mean
  *send nothing* on Anthropic and *the spec's default* on DeepSeek, so the same
  argument asked for two different things and `create_llm("anthropic")` — the
  bench's default target — silently stopped requesting `low`, letting Opus 5.5
  think at its vendor `medium`. `Engine.default_effort` resolves it at
  `create_llm`, and an engine whose default is nothing (`openai`) says so.
- **Haiku is why a choice may omit an effort rather than set `None` for "none".**
  The model rejects the parameter with a 400, and only the adapter can discover
  that, by asking the Models API. So the three Anthropic options set no effort of
  their own — they run at the engine's `low` — and the adapter drops what the
  model cannot use. A menu that declared "no effort" for Haiku would be asserting
  a fact about a model that it cannot know and that changes with every release.
- **Every Anthropic tier keeps `low`.** Anthropic's tiers differ by model, which
  is what they are. Letting Opus think at `medium` would buy depth a spoken
  reply has no time to hear.
- **The check moved from `Settings` to `create_llm`.** Chapter 18 put it where a
  pair was configured; nothing configures a pair any more. The menu declares
  them and `create_llm` builds them, so that is where a contradiction can be
  caught.
- **OpenAI is not offered.** It has no menu entry, so the page cannot reach it,
  while the engine, the spec and the tests stay. Deleting it was not this
  chapter's business.

**Latency impact** — not measured, and this chapter changes no timing: it
changes *which* model a conversation runs, not how fast any of them is. The
default is Haiku 4.5, the fastest to first token this project has recorded from
`iad` (435 ms p50 against DeepSeek's 915 ms — Chapter 14), and the DeepSeek
tiers send the `low`/`high`/`max` that Chapter 18 measured against that
provider's `high` default with no separable difference on a trivial question.

**Deliberately not done**
- No prices and no speed words: a title names the model and claims nothing else.
- Fable 5.1 is served by Anthropic and declared in `MODELS`, and is not offered,
  so Opus 5.5 is the top of the menu.
- Voice and ears stay separate groups; this chapter is the model only.
- `--bench-llm` still takes `PROVIDER[:MODEL]` and cannot name an effort, so it
  measures providers and not the menu's options: `deepseek-max` is unreachable
  from it, and the default targets no longer line up with what ships. Widening
  that flag is a follow-up rather than something this chapter did.

**Verification** — `uv run verify`: ruff, format, mypy (strict) and **695 tests**
pass, including `node --test` on the page's modules. Exercised for real: the
server was started and the start screen fetched over HTTP, which served exactly
the six options above with `haiku-4-5` marked default and OpenAI absent (no key).
Every entry was then built and connected against its real provider —
`models.list`/`retrieve`, which nobody bills — and all six resolved. Haiku is
handed the engine's `low` like its neighbours and the adapter drops it, since the
model reports it supports no effort; that omission is covered by a test rather
than by the live check, which only connects. `start.js` parses under
`node --check`.

Three gaps this chapter had to be honest about. **Starting the server caught one
real defect**: the startup banner still printed `args.provider` after the flag
was removed, so `uv run voice-agent` died on its last line — and no test runs
`main()`. **A self-review of the diff caught another**, the `None` split above:
`create_llm("anthropic")` had quietly stopped asking for `low`, which no test
noticed because the product path always passes an effort explicitly. It is fixed
and pinned by a test that goes through `create_llm` to the wire. And **the page's
rendering is not executed by any test**: `tests/web/` covers `player.js` and its
neighbours, and `start.js` only ever had cheap structural checks, so a new one
was added for the invariant that changed — the option label comes from the server
and the page keeps no map of model names. A live call to each of the six models
is not something this chapter did.

## Chapter 18 — Whose model: the provider declares its lineup, and a contradiction stops the server

Two faults, one cause: nothing checked that a model belonged to the provider it
was configured with. `--provider` and `--model` are independent flags, so
`--provider deepseek --model claude-haiku-4-5` built an adapter that asked
DeepSeek for a Claude model and came back 400 on every turn — a real
conversation in `sessions/` did exactly that. The DeepSeek default had gone
stale as well: the code ran `deepseek-chat`, which DeepSeek's API no longer
lists among the names it accepts. Both are answered by the provider declaring
what it serves, and one check holding a configured pair to that declaration.

**What changed**
- `llm/registry.py`: `Engine.models`, and `check_model(provider, model)`, which
  refuses a model belonging to another provider and names the ones that fit.
- `llm/openai_compatible.py`: the spec carries `models`, and DeepSeek's default
  is now `deepseek-flash` — the name the API itself reports. The spec carries
  `reasoning_effort` too, sent only by a provider that declares one.
- `llm/anthropic_provider.py`: `MODELS`.
- `config.py`: `Settings.__post_init__` calls `check_model`, so the environment
  and the flags are held to the same list, before anything connects.
- `.env`: five keys no line of code has ever read were removed (Deepgram,
  Cartesia, two Twilio, one OTLP endpoint). It is gitignored, so this leaves no
  diff — housekeeping, not behaviour.

**Design decisions**
- **A declared list, not a naming rule.** `claude-*` / `gpt-*` / `deepseek-*`
  prefixes would need no maintenance at all, and would be a guess about how
  vendors name their models. The list is the truth, and a vendor's next model is
  one line. A wrong pair is refused rather than guessed at.
- **Not the provider's Models API.** It is authoritative and cannot go stale,
  and Anthropic's adapter already looks its model up for the effort check.
  Rejected because it makes configuration depend on the network: a deployment
  that cannot reach a provider should still say what it is set to do.
- **Checked where settings are built, not where engines are.** `create_llm` sees
  every pair too, but by then a caller has already chosen one. Settings is the
  thing a person configures.
- **`omit`, not `None`, for the provider with no such parameter.** The SDK's
  overloads want their own sentinel; `None` would have gone on the wire as a
  literal null. It is the sentinel the Anthropic adapter already uses.
- **`low`, not off.** DeepSeek thinks by default at effort `high`. `none` would
  be the literally shortest wait, but `low` still reasons and measured the same,
  so the bound comes without giving the capability up.

**Latency impact** — measured, and the effort dial is a null result.
- The shipped change, `deepseek:deepseek-chat` against `deepseek`
  (deepseek-flash at `low`), five short calls each: **ttft p50 1016 ms → 950 ms**,
  ranges 946–1255 and 699–1324. The ranges overlap, so this is not a speedup; it
  is the same model class answering about as fast under a name the API accepts.
- The effort setting alone, `low` against the provider's default `high`, five
  calls each: **826 ms against 884 ms p50**, ranges 715–1245 and 608–1202, with
  `high` holding the faster minimum. No separable difference at this size.
- The reason is the question: "why is the sky blue" needs no chain of thought,
  so both efforts think briefly and land together. `low` is kept as a bound on
  the worst case, not as a measured saving. **A hard turn, where a long chain of
  thought would actually appear, is unmeasured.**

**Deliberately not done**
- `--bench-llm deepseek:claude-haiku-4-5` still builds the pair and lets the
  vendor refuse it. The check governs what a deployment is configured to run; a
  developer aiming the bench at a vendor can read the 400. *(Superseded by
  Chapter 19, which moved `check_model` into `create_llm`: the bench now refuses
  it too.)*
- The start screen still offers only each provider's default model. The declared
  list makes offering a real choice possible, which is a chapter of its own.
- `deepseek-v4-pro` is declared but never recommended; the default is `flash`.

**Verification** — `uv run verify`: ruff, format, mypy (strict) and **683 tests**
pass. Also exercised for real: `uv run voice-agent --bench-llm
deepseek:deepseek-chat deepseek` (ten short billed calls) confirmed the new
`deepseek-flash` + `reasoning_effort=low` request is accepted with no error and
no retry, and produced the numbers above.

## Refactor 3 — The conductor: every turn-taking decision on one inbox

Seven decisions — barge-in, echo, hold-and-merge, resume, speculation, speaking
into a silence and the inner voice's timing — were made in callbacks and timer
tasks that ran concurrently and interleaved at every `await`. Each Chapter 17
live fix was an interaction between two of them (echo against barge-in, a hold
against a resume, the thinker after the mic stopped), and Chapters 18–20 each
add another decision. So the decisions now take turns.

**What changed**
- `events.py` (new): one small type per input. `Partial`, `Final`,
  `NewSession` and `FloorChanged` come from the microphone; `Playback` and
  `Typed` from the page; `Speak` from the clock; `HoldOver` and `ResumeDue`
  from timers; `End` from the time limit.
- `session.py`: `Session` is the conductor. `post()` puts an event on its
  inbox. One task, started by the first event, handles them in order in
  `_handle`, a single `match`. A handler that raises is logged, and the next
  event is still handled.
- **Timers are events that arrive later** (`_after`). The hold's 100 ms polling
  loop is gone: a partial or the voice detector's `speaking` during a hold
  moves its deadline from 1.2 s to the 8 s cap. The resume's sleeping task is
  gone as well. A token ignores a timer that fires after its hold or resume was
  cancelled.
- `Session._agent_busy()` is the one test of "a reply is being written, is
  audible, or is being cut down". `quiet_for`, speculation and the inner voice
  used to derive it each on their own.
- `mic.py`: the five `on_*` callbacks are one `post` sink. The microphone never
  waits on a decision, and no decision runs inside its task. Two behaviours
  follow from that. A decision that raises is logged, and listening carries
  on: it used to crash listening ("listening crashed"). And the microphone
  keeps reading while a decision runs, instead of waiting for it.
- `server.py`: playback and typed messages are posted. The time limit calls
  `Session.finish`, which ends the conversation in turn with everything else.

**Design decisions**
- **Posted, not awaited.** A microphone that awaited each decision would
  deadlock the moment a decision stops the microphone (a spoken "bye"), which
  today only works because the callback happens to run inside the
  microphone's own task.
- **The microphone still sends its own frames** (transcripts, floor). Only the
  decisions moved. The one visible effect: at the same instant, an `interrupt`
  or `echo_ignored` frame now follows the transcript that caused it rather
  than preceding it. Three goldens changed by exactly that, and no timing moved.
- **The initiative clock and the thinker keep their own timers.** They are
  sources, not decisions: the clock's line arrives as `Speak`, and the
  thinker's thoughts decide nothing yet.
- **The cost is ~190 lines.** Session, microphone and events are 1,236 lines
  against 1,044 before. The next chapter's rules (own endpointing, speaking a
  thought) become cases in `_handle` instead of new callbacks.

**Latency impact**
- None measured on the tapes: every event is handled at the virtual instant it
  was posted. Live, a decision can wait behind the one before it; the slowest
  is a speculation's cancel, which awaits a task cancellation.

**Verification**
- `uv run verify`: 672 tests. Every tape matches, with the three reorderings
  above reviewed and accepted. The warnings summary shows no unawaited
  coroutine and no destroyed task (the socket `ResourceWarning`s are the
  adapter tests' loopback servers, as before).
- Live, under uvloop (silent and deaf, one billed reply): a typed question was
  answered, `exit` gave `ended`, the server hung up with 1000, and a second
  conversation was admitted. Trace spans have sub-millisecond precision.
- Not yet: a live *spoken* session. That is the check before this ships.

**Fixes**

- `timing.now()` read the event loop's clock, which under uvloop is
  millisecond-grained and on its own base. It is `perf_counter` again, and only
  the tapes swap it (`timing.using`).
- Ending a conversation left its socket open: the receive loop waited for a
  page that sends nothing after `ended`, holding a live slot. The loop now
  races the page against `Session.ended`, then hangs up. A spoken "bye" had
  the same gap from before. There is a test, and an `exit` tape.
- A tape with no golden file used to write one and pass; it now fails.
- `Session.state` named five states when only two were used; it is
  `_agent_busy()` until a chapter needs more.

## Refactor 2 — Less to carry: one voice, one settings path, prompts as data

The second step of the review. These changes are mechanical and change no
behaviour: the tapes' goldens and the generated prompts are byte-identical
before and after.

**What changed**
- **OpenAI TTS removed.** It was batch-only and never offered on the page.
  `list_voices` is no longer on the `TTS` protocol; `--list-voices` asks
  ElevenLabs directly.
- **Flags no longer write to `os.environ`.** The environment gives the
  defaults, `cli.with_flags` overrides them into one `Settings`, and that
  object is what the server, logging and `--purge-sessions` read.
- **`Backends`** (`backends.py`, was `pool.py`): what is offered, what each
  conversation picked, and the one shared instance of each backend, in one
  class instead of `Stack` plus `Pool`.
- **One spec per backend.** `llm.registry.ENGINES` holds each engine's key and
  default model; `stt.registry.EARS` holds each recognizer's key, languages and
  rate. These replace five parallel dictionaries.
- **`prompts/rules.md`**: the identity, voice, language, hearing, clock and
  greeting rules, with their measured reasons in its preamble. `prompts.py`
  reads every prompt file past its preamble, when used and never at import;
  `config.py` takes the rules by heading. It is ~100 lines shorter, and a wording change no longer
  touches Python.
- Smaller:
  - A failed or cancelled turn removes its question by identity, not by
    popping the last message.
  - A record's path is kept on its `Conversation` instead of found by scanning
    the sessions directory on every connect.
  - `Mic.stop` no longer walks its tasks by attribute name.
  - The unused `SPECULATE` flag is gone.

**Design decisions**
- **`create_app`'s keyword arguments stay.** Now that settings flow one way,
  they are only test seams, and moving ~40 call sites onto `Settings` would buy
  little.
- **The frame queue stays unbounded.** A stalled recognizer can hold at most a
  listening session's worth (~11 MB at the 6-minute cap).
- **The page is still read from disk per request.** It costs microseconds and
  lets a page edit show without a restart.

**Verification**
- `uv run verify`: 671 tests (4 OpenAI TTS tests removed). Every tape matches
  its golden. `build_prompt` is byte-identical over 72 combinations of gender,
  languages, delays, greeting and role. `--initiative 3,1` is refused in one
  line.

## Refactor 1 — Tapes: conversations replayed on virtual time

The first step of the post-Chapter-17 review. Turn-taking is about to move into
one place, and it is the part of the agent that unit tests cover least well:
barge-in, echo, hold-and-merge, resume, speculation, the clock and the inner
voice each have tests of their own, but nothing pinned how they behave
*together* in a conversation. Before anything moves, that behaviour is recorded.

**What changed**
- `timing.now()` is the one clock. Every `perf_counter` and `monotonic` read
  in `src/` now goes through it, so a replay can swap in its virtual loop's
  clock and every timer, deadline and measurement moves together.
- The voice detector runs inline instead of on a worker thread (measured
  108 µs a window inline, 146 µs with the hop), and `Mic` takes a `detector`.
- `tests/tapes/`: a `.tape` is what a user did, timed (voice on and off, the
  recognizer's partials and finals, typing). `harness.py` replays one on a
  virtual-time loop, with a simulated browser (playback at real speed,
  interruption answers) and scripted engine, voice and thinker, and prints every
  frame. `test_tapes.py` compares that with a `.golden` file. Eight tapes:
  typed turn and clock, barge-in, echo, phantom resume, hold-and-merge,
  speculation won and lost, thinker, listening expiry.

**Design decisions**
- **Tapes hold only the user's side.** The browser is simulated rather than
  recorded, because the traces we have lack playback events and the public
  instance has no trace. Recording real tapes from live sessions comes later,
  on the same format.
- **Goldens, not assertions.** A refactor that should change nothing must
  produce no diff; one that should change timing shows exactly what moved.
  `UPDATE_GOLDEN=1` rewrites them.

**What the tapes already show** (today's behaviour, pinned, not yet changed)
- The withdraw rung fires 28 s after the agent's own unprompted line, not 28 s
  into the user's silence: an unprompted line restarts the silence.
- The thinker's "partner has just finished speaking" fires when the reply is
  written, while its voice still has about a second to play.

**Latency impact**
- None. The VAD hop removed saves ~40 µs a frame.

**Verification**
- `uv run verify`: 675 tests. The tapes replay in about 0.1 s each (a 60 s
  conversation included), and identically across runs and processes.

## Chapter 17 — The inner voice: a devil's advocate thinks alongside you, out loud only on the page

Chapter 16 gave the agent a sense of timing and nothing to say with it. This
chapter adds the *what*. A fast model (Claude Haiku 4.5) runs alongside the
conversation in a role and keeps at most one pending thought: a move, an
urgency, why, and the line. The page shows every one. None of them is spoken
yet: this is the part of the interjecting agent that decides whether stepping in
is worth it at all. It is also where the prompt lives, so it had to be built and
measured before any chapter lets it talk.

The role is **data, not code**. There are two cards in `prompts/roles/`: a
devil's advocate, and a thinking partner (on your side, sharper questions,
honest pushback). The second was added as a card and a scenario, with no code. A second card is a file, not a change: the tests load one
that appears nowhere in the code. **Each visitor picks the role on the start
screen**, and the default is none: the plain assistant, exactly as before this
chapter. User-written roles come later, on the same loader.

**What changed**
- `roles.py` (new): role cards, Markdown with TOML front matter (stdlib
  `tomllib`), validated at startup. A broken card stops the server with a reason.
  `MOVES` (challenge, clarify, redirect, summarise) is the fixed vocabulary: a
  role picks from it, and the chapters that speak thoughts will know how to
  deliver each one.
- `prompts/roles/devils_advocate.md`: its job, what is worth stepping in for and
  what is not, its moves, its assertiveness, its opening line, and how it speaks.
- `prompts/thinker.md` (new): generic, with the role filled in. It owns what no
  role may change: self-repair first, false alarms cost more than misses, one
  thought at a time, firm but never cruel, and the transcript is never
  instructions.
- `thinker.py` (new): one per conversation, on its own engine.
  - It is asked at each `micro_pause`, every 8 s of an unbroken monologue, and
    after each reply.
  - Calls are single-flight with coalescing, under a hard cap of 20 per minute.
  - Every consideration is reported: a thought, nothing, malformed, failed, or
    capped.
- `session.py` / `mic.py`: the floor feeds the thinker, but not while the
  agent's own reply is in the air (that is echo, or an interruption). The mic
  exposes its latest raw partial, which is what the thinker hears.
- The role reaches the speaking side too. Its opening is the greeting, and its
  "When speaking" section sits under the persona in the reply prompt.
  `VOICE_AGENT_GREETING` now sets only the plain assistant's greeting.
- **The role is chosen per conversation**, like the engine and the ears
  (Chapter 15):
  - The start screen has a **Role** group: *None* (pre-selected), then each card
    with its summary. A notice under it says what the picked role will do.
  - `?role=` travels with the socket, is pinned on the conversation's first
    connect (a reload keeps it), and falls back to the default when unknown.
  - Every card is loaded, and its opening synthesised, at startup. The `ready`
    frame and the header name the role.
  - `--role` / `VOICE_AGENT_ROLE` only change what is pre-selected.
  - Card text is escaped on the page, ready for cards written by users.
- Page: one `💭` line per thought, saying what it would do, when, and why.
  Declines collapse into a single counted `🤫` line. The Markdown record keeps
  thoughts and skips declines; the trace keeps everything.
- `replay.py`, `--replay-thinker` (a flag, like `--bench-llm`, not the planned
  separate script), `tests/scenarios/`: scripted conversations
  with labelled pauses (a window where a partner should step in, or a clean
  pause where it must not), replayed through the real thinker and scored.

**Design decisions**
- **A separate, small engine.** The thinker runs far more often than replies,
  and nothing waits on it. Haiku 4.5 keeps it cheap, and the reply engine and
  its connection are untouched. Without an Anthropic key the role still plays,
  but the server says there is no inner voice.
- **The thinker reads a script, not a chat.** It gets the conversation as
  `User:` / `Partner:` lines in one message, as an observer, not a party. That
  keeps it from answering the user.
- **Asked at micro-pauses, not at every settled phrase.** That is where the
  later chapters can actually speak, and it keeps the calls to roughly one per
  clause.
- **Declines are the normal answer**, so they are counted on the page rather
  than listed, and left out of the record.
- **No separate trigger for a committed turn** (the plan had one). A spoken
  commit always follows the micro-pause that already asked. A *typed* message,
  though, is only thought about after the reply to it.
- **The thinker hears the recognizer, which lags speech.** At a micro-pause it
  judges the latest partial, which trails the voice by ~0.6 s on AssemblyAI and
  ~2 s on Scribe (Chapter 16). On Scribe it can be thinking about the sentence
  before last. That caps how well-timed anything built on it can be until the
  words arrive sooner.

**What the replay found** (2 replays of 3 scenarios, 33 calls each, $0.15 in
total):
- **Hits 5/5, but false fires 10/10**, then **9/10** after one revision. The
  thinker almost always proposes something at urgency 2. It is right about
  *what* is weak and wrong about *when* to say it. It asks for the reason one
  clause into the first sentence, and challenges evidence the user is still in
  the middle of giving.
- The revision tied urgency to where the user is in their turn ("most good
  thoughts are urgency 1: hold it until they finish"). It moved one early fire
  and not much else.
- **The cause is a design question, not wording.** The prompt asked the thinker
  to decide *when* to speak, which breaks this project's own rule that the
  thinker decides what and the actor decides when. At a micro-pause, from text
  alone, the end of a sentence and the end of a turn look the same; the floor
  knows the difference and the text does not. Left open for the next chapter to
  decide with the user.
- **The first live session confirmed it.** Devil's advocate on aikido, ~4.5
  minutes, gave 50 recorded thoughts, nearly all urgency 2: on "Hello.",
  straight after every reply, and at 6–10 pauses in a row inside one
  monologue. Spoken, that would interrupt at almost every pause. A fourth
  scenario in that session's shape (`shifting_ground`) replayed at hits 10/10,
  false fires 11/15 across all four.
- **After the second live session's fixes** (move definitions, language), all
  five scenarios gave hits 16/16 and false fires 12/20. `challenge` went from
  almost never to 16 of 54 thoughts; right moves on `shifting_ground` rose
  from 1/5 to 4/5; there were no malformed replies. The thinking partner scored
  6/6 hits and 2/5 false fires, and stayed quiet through a stretch of
  brainstorming that was going well.

**Latency and cost**
- Thinker call: median 1.8–2.5 s in the replays, and ~1.4 s live after a reply
  (about 1.6–1.8k prompt tokens, 70–100 out). That misses the ≤ 1 s target.
  **Nothing is cached**: the static prefix is below Haiku's minimum cacheable
  size.
- Cost: ~$0.002 a call. That is ~$0.02–0.03 a minute at the expected 10–15
  pauses a minute (estimated from the replay's per-call cost, not measured over
  a real minute).
- Reply latency: **not measured**. The reply path doesn't wait on the thinker.
- After going live (below):
  - Hold-and-merge adds up to **1.2 s**, and only on a turn that looks
    unfinished. Finished sentences are not delayed.
  - A resume replays at most the one sentence that was cut.
  - The echo guard costs nothing while the agent is silent.

**Deliberately not done**
- Speaking a thought, the opener bank, yielding (Ch 18); overlap and the
  assertiveness behaviour (Ch 19); an agenda (Ch 20).
- User-written roles, and showing a card's full text on the page (the summary
  only).
- A per-role `budget` of interjections (in the plan's card). Nothing could use
  it before a thought can be spoken, so it comes with Chapter 18.
- Structured outputs for the JSON. The reply is parsed tolerantly and anything
  else is reported as malformed; there was none in 66 calls.

**Verification**
- `uv run verify`: 665 tests. They cover roles, the thinker, replay scoring,
  the wiring through session and server, and, after going live, the echo
  guard, resume, hold-and-merge, the closed channel and the log file. Plus
  74 page tests.
- Two paid replays as above.
- A typed live check of the picker: the start screen offered *None* (selected)
  and *Devil's advocate*. A plain conversation got the plain greeting and no
  thoughts; a devil's advocate one got the card's opening, a reply in role, and
  a thought.
- A typed live session with no voice and no ears: the role's greeting opened,
  replies argued the other side, and the thinker held back after each reply.
  There were no errors.
- Not yet: a spoken session where thoughts follow micro-pauses.
- A review pass before commit fixed the following:
  - **The monologue timer could keep calling Haiku after listening stopped.**
    Pressing stop mid-sentence leaves the floor at `speaking`. Shutdown also
    stopped the thinker before the mic, whose last floor events could start the
    timer again. Now the mic reports `stopped`, and a stopped thinker is final:
    nothing restarts it.
  - Role text could expand placeholders in the thinker's prompt (`{moves}`
    written in a card). The prompt is now filled in one pass.
  - A replay longer than the live cap would silently score one answer twice. It
    now runs uncapped and checks one answer per pause.
  - Smaller: one default role instead of two constants; the thinker prompt is
    built once per process; the replay says why it has nothing to run in the
    image. Each fix has a test.

**After going live: what real conversations forced**

The live sessions broke four things no replay could show. Each fix changes
how the agent takes turns, so each is recorded here rather than as a one-line
fix:

- **Its own voice, heard back.** On a phone on speaker the agent said "I can
  help you sign up", heard "Hello, I can help.", cut itself off and answered
  itself.
  - Words heard over the agent that mostly repeat what it is saying (≥ 60 %)
    no longer interrupt it.
  - A committed turn is dropped only when every partial read as echo and it
    is ≥ 85 % its words (at least 3 of them).
  - The stricter bar is because a user *quoting* the agent back reads as
    60 % echo: holding back their barge-in is tolerable, losing their turn is
    not. The cost is that a misheard echo with extra words can still be
    answered.
- **An interruption by nobody.** A phantom partial cut a reply after
  "Отлично.", no words followed, and 16 s of silence came before the clock
  spoke. An interruption with no words within 2.5 s now resumes from the
  sentence it cut. It never resumes over half a sentence of the user's still
  waiting to be finished.
- **Half a sentence answered.** Thinking pauses made the recognizer commit
  "…при по" | "сещении", and each half got its own answer. A turn that looks
  unfinished (no closing punctuation, a trailing comma or "…") waits up to
  1.2 s. Pieces that arrive in time are answered as one. Both recognizers
  punctuate finished sentences, which is what makes the signal usable.
- **Logs that vanish.** Fly keeps ~100 log lines and loses them on every
  deploy. The server now also writes its log to the volume (`VOICE_AGENT_LOGS`,
  ~55 MB rotating). `scripts/pull-fly.sh` copies logs and records into
  `fly-archive/` before each deploy. That log is how the time-limit crash
  below was found.

**Fixes**

- Replies in role are one objection or question in two sentences (live: 12–29 s spoken, 5 of 10 talked over; now 63–194 chars).
- The devil's advocate no longer repeats its opening line when the user only says hello.
- The thinker skips the call when nothing new has been heard; the held thought stands, unbilled.
- The thinker reads the last six exchanges and its notes, capped at 60 words, not the whole conversation (prompt grew 1.5k → 2.5k tokens live).
- A thought with an empty line is read as nothing, not malformed (1 in 50 live, 1 in 54 replayed).
- The record ends with a floor summary: speech onsets, and how many came while the agent was talking, so echo can be checked afterwards.
- The replay parser no longer adds a duplicate pause for a line ending in `| {label}`.
- A thought with a null or missing line is read as nothing, and a missing `why` as empty (3 of 17 malformed in a live session).
- The thinker writes its line in the language the user is speaking (one English line turned up in a Russian conversation).
- `challenge` versus `clarify` is spelled out, and a role's first-listed move wins a tie (live: 11 of 11 thoughts were `clarify`).
- The public demo's conversation limit is 6 minutes, not 5 (a live argument was cut off mid-point), and listening lasts as long.
- The microphone is asked for in the same tick as the start tap, not after awaiting the audio resume; WebKit (every iPhone browser) can refuse the later request, and the error now says where to allow it.
- "Didn't catch that" asks again instead of reading out every language (live: 624 characters, ~100 languages).
- The female voice's one-word «Поняла.» and the user's own gender are named in the voice rule (typed replay: masculine self-forms 2 → 0 of 20).
- The thinking partner answers when asked for a view or a suggestion (live, asked «я спрашиваю у тебя», it refused).
- The record notes the browser family (never the user-agent string) and any microphone failure, so a phone that fails is visible afterwards.
- Writing to a socket that has gone is dropped, not raised: the time-limit hang-up no longer crashes listening with an unretrieved task exception.
- The recognizer's contradiction count resets every turn (it climbed 15 → 21 and was reported as each turn's own).
- The last minute of a time-limited conversation shows a countdown, with one note at a minute left.
- An ended conversation releases the microphone, resets the listen button and offers "Start a new conversation"; the limit's message no longer says "reload", which reopened the ended conversation.

## Chapter 16 — Ears that hear pauses: the agent knows who holds the floor

This is the first step toward a sparring partner that leads the conversation
and cuts in when it's worth it, not only when it's asked. Cutting in well is
about timing: a human leaves a 200–500 ms opening, and the recognizer only
reports the end of speech 1–2 s later. Everything that follows (the background
thinker, speaking into a pause, speaking over the user) needs a faster signal.
So this chapter builds only that signal, and measures it. The agent says
nothing new.

A Silero VAD now runs on the server over the audio the page already sends. It
turns each 32 ms window into a **floor state**: `speaking`, `micro_pause`
(≥ 200 ms), `pause` (≥ 600 ms) or `yielded` (≥ 1.5 s). The state goes to the
page as a live strip and to the trace, and it times each commit from when the
user actually stopped.

**What changed**
- `vad.py` (new): Silero VAD v5 through onnxruntime, with the model vendored
  in `models/` (MIT, 2.3 MB). One session per process, recurrent state per
  conversation, frames of any size cut into 512-sample windows.
- `floor.py` (new): the state machine. It uses audio time rather than the
  clock, so the same audio always gives the same states, and it has hysteresis
  on both edges.
- `mic.py`: every page frame also goes to the VAD, off the event loop, in
  arrival order. Floor changes are sent as `floor` frames and carry whether the
  agent's reply was playing, so echo can be told apart. A committed transcript
  now carries `speech_end_ms` (the VAD's stop → commit) and `first_words_ms`
  (the VAD's onset → first partial).
- `capture-worklet.js`: 32 ms frames instead of 100 ms, one VAD window each.
  `stt/base.batched` regroups them into the same 100 ms chunks both
  recognizers received before. AssemblyAI closes the socket on chunks under
  50 ms.
- Page: `floor.js` draws the last 12 s as a strip under the header, behind
  **details**. Silences are labelled with their length, and speech heard while
  the agent talks is shown red. The line under a commit now leads with the
  VAD's timing.
- `record.py`: floor frames stay out of the Markdown record. The trace keeps
  them.
- `server.py`: the model loads at startup, off the loop, next to the greeting,
  so the first person to press listen doesn't pay for it.

**Design decisions**
- **On the server, not in the page** (ROADMAP §3 said the page). The audio
  already passes through the server, so there is one implementation, it runs
  under pytest, and the actor that uses it will live here too. The page would
  only have saved the network hop.
- **Silero over webrtcvad or an energy gate.** Silero is robust to noise and to
  AGC-boosted rooms, with no torch needed. On the fixture: 0.11 ms per window,
  starts exact and ends 15–47 ms late.
- **States, not probabilities, on the wire.** The states are the words the next
  chapters will time interjections against. Thresholds are named constants with
  their reasons; there is no configuration surface until something needs one.
- **The frame shrank, the chunk didn't.** The recognizers see exactly what they
  saw before. Only the VAD sees the finer grain.

**Latency impact**
Measured by streaming `tests/fixtures/pause.wav` in real time to a local server
(5 sessions):

| Ears | Stop → commit (VAD) | Old `endpoint_ms` | Onset → first words |
|---|---|---|---|
| AssemblyAI (3 runs) | 977–985 ms | 1216–1317 ms | 625–629 ms |
| Scribe (2 runs) | 1795–1938 ms | 672–711 ms | 2134–2210 ms |

The floor calls a `micro_pause` 224 ms after the stop and a `pause` 608 ms
after it. That is **~750 ms ahead of AssemblyAI's commit and ~1.6 s ahead of
Scribe's**. On Scribe, the old clock understated the wait by over a second. On
AssemblyAI it read *higher* than the VAD's. That means the last new word
arrived ~300 ms before the VAD heard the speech end, which is **unexplained**.
One suspect: the fixture says the same phrase twice, and the model may finish
the second one early. A recording without repetition should settle it.

- VAD inference: 0.11 ms per 32 ms window, off the loop. Loading the model
  adds ~39 MB peak RSS (macOS; Linux not measured).
- Effect on reply latency: **not measured**. Nothing on the reply path waits
  on the VAD, but the extra frames and `floor` messages share the loop.

**Deliberately not done**
- No new speech, no thinker, no roles. Those are chapters 17–20 (ROADMAP §2J).
- The clock (`initiative.py`) still measures silence from the recognizer. Moving
  it to the VAD changes behaviour, so it gets its own entry.
- Barge-in is not VAD-triggered yet, although it could be ~1 s faster. The echo
  check below has to come first.
- No pitch, breath or filled-pause cues. The floor is speech or not speech.
- A planned VAD-backed `quiet_for` was left out: nothing reads it yet.
- The VAD handles 16 kHz only. Ears at another rate go without a floor.

**Verification**
- `uv run verify`: 547 tests pass. New: `test_floor.py`, `test_vad.py`
  (against a checked-in recording with labelled speech, ±64 ms), floor frames
  through `Mic`, and the strip and commit line in node.
- Five live sessions as above; floor frames reached the client and the trace
  with no errors. The client was a script, not a browser.
- A review pass before commit fixed four defects in the new bookkeeping:
  - An onset heard but never transcribed (a cough, echo) dated the next
    utterance.
  - An empty commit or a reconnect froze the first-words measure.
  - The strip kept painting after listening stopped.
  - A restarted session reset a detector a worker thread could still be using.
    It now gets a fresh one.
  Each has a test, except the strip, which is DOM-bound.
- **Not yet checked: echo.** Whether browser AEC leaves enough of the agent's
  voice for the VAD to call `speaking` needs a real speaker and microphone. The
  strip shows it in red if it happens.

## Docs — ElevenLabs reference for coding agents

Not a chapter, and no code changes. AssemblyAI ships one coding-agent prompt
(`docs/vendor/AssemblyAI.md`). ElevenLabs ships no such file. Its guidance is
split between `llms.txt` and the `elevenlabs/skills` repo.
`docs/vendor/ElevenLabs.md` now indexes both and pins a snapshot of four
skills (speech-to-text, text-to-speech, speech-engine, agents) under
`docs/vendor/elevenlabs/`. It also records why the managed Speech Engine and
Agents runtimes don't fit an agent that interjects: they own turn-taking and
only answer the user. AGENTS.md §8 gains one rule: check live vendor docs
before writing adapter code.

## Simplification — a review, and less to carry

Not a chapter: a review of the requirements, the architecture and the code,
before building further. The core held up: streaming LLM into streaming TTS,
barge-in that keeps only what was heard, the choice of stack, the caps. What had
grown heavy was around it: a feature that never earned its keep, four functions
too large to hold in one's head, comments that read as a diary, and docs about
ten times larger than their own rules allow. This pass removes weight and
changes behaviour only where stated.

**What changed**
- **Warming removed** (`warming.py`, `LLM.warm`, the 🔥 notes). Measured in
  Chapter 4 at ~10 ms after the first turn, inside the noise, and only ever on
  DeepSeek. Speculation covers the same gap.
- **Telemetry behind a `details` switch.** The notes under each bubble are
  still written, but hidden unless the header's **details** is on (remembered
  per browser). Errors and "press listen" stay visible.
- **No trace on the public instance** (`VOICE_AGENT_TRACE = "off"` in
  `fly.toml`): it held whole prompts and replies, against AGENTS.md §10.
- `server.py`: `create_app` went from 270 lines to 90. Process state is one
  `Agent`, the socket is a module-level `serve()`/`converse()`, and `facts()`
  takes 3 arguments instead of 8.
- `pool.py`: `Stack` owns which engines and ears are offered and which each
  conversation picked (it was closures inside `create_app`).
- `turn.py`: `run_turn(ctx, turn, …)` replaces 11 parameters. `Turn` lives here,
  and a flag replaces the one-bool `Interruption` class. The `reply_end` report
  is its own function.
- `web/`: `app.js` dispatches through one handler per server message (it was a
  235-line if/else). The start screen is `start.js`, and the note text is
  `telemetry.js`, which node now executes.
- Comments and docstrings cut to their constraints, down from about 2,000 lines
  to about 1,400. The stories stay here, in the CHANGELOG.
- Hygiene: `prompts/AssemblyAI.md` (a vendor guide for coding agents, never
  loaded) moved to `docs/vendor/`. Five unused `.env.example` names are gone.
  The CLI's choices come from the registries, and `--vad-silence`'s help no
  longer claims a default it does not have.
- README down from about 4,100 words to about 1,100, with one line per chapter.
  The two multi-page fix entries below are condensed to their facts.

**Design decisions**
- **The registries were left as they are.** A generic `Registry[T]` would add
  a layer to three 50-line modules that are already consistent enough.
- **Config still flows through the environment.** The CLI writes flags into
  `os.environ` so there is one parser. `create_app` now takes the `Settings`
  the CLI loaded rather than loading them again.
- **No server-side protocol module.** The handler table in `app.js` is now the
  one list of server frames on the client. Typing every frame on the server
  would be a larger change than this pass.

**Latency impact**
- None intended. Warming's measured effect was within the noise.

**Deliberately not done**
- Speculation and agreement stay, still unmeasured on AssemblyAI (the
  default ears). That measurement is the next thing worth doing to them.
- Initiative keeps its three rungs.

**Verification**
- `uv run verify` green after every step: 520 tests. That is 6 warming tests
  removed, 12 telemetry cases added in node, and the source-text checks moved
  onto the new modules.
- A before/after comparison of every module's syntax tree, with docstrings
  removed, confirmed that the comment pass changed no code.
- The page rendered in headless Chrome: three pickers with the defaults first,
  and the details switch present.
- A typed conversation driven through the page in headless Chrome (silent,
  deaf, a real click): greeting, reply, notes hidden until **details** is on,
  and no errors on the page or the server.

**Fixes**

- Haiku answered Russian in English after one English reply (11 of 16 in a replay of a live conversation), and, corrected on its own gender, made Dostoevsky feminine. The language rule is now also the prompt's last line, with examples (0 of 32 on the same replay), and the gender line says it covers only the agent itself.
- That rule still let about 1 in 30 first replies go English on the live site: the English greeting was the only turn before the question. The greeting now stays in the page's history but out of the model's context, stated in the prompt as already said instead (8 of 8 Russian after the change; the residual rate is too small to measure frugally).
- A reply paused audibly at «…Достоевский —», exactly where synthesis split it, but a resynthesis showed no silence in the audio and the record kept no timing. `audio_end` now carries how far the voice fell behind playback and after which words (`late_ms`, `late_after`, a ⚠ note on the page, logged from 500 ms), a synthesis still unfinished when its turn ends is logged, and the page's playback gaps go into the record.
- «понял» in a woman's voice again, the gender line buried under the hearing and clock notes. The prompt now opens with who the agent is ("You are a woman…"), ends with the voice rule after the language rule, and treats an earlier wrong form as a mistake rather than a precedent: masculine forms 3 of 5 → 0 of 5 on a replay whose history already held one.
- `--stt none` is honoured when recognizer keys are present. Since Chapter 15 the page still offered ears, and asked for the microphone.

## Earlier

Chapters 0–15, with the fixes and measurements between them, are in
[docs/history.md](docs/history.md).
