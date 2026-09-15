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

## Measurement — LLM latency by provider, and connections kept between turns

A small step between chapters. The roadmap's next large items (a local voice
detector, then semantic turn detection) attack the recognizer's 0.85–1.6 s. Before
them it was worth knowing the other large term: time to first token, which
Chapter 4 measured on DeepSeek alone as a ~716 ms floor. This adds a repeatable
way to compare providers from here, fixes the connection handling the
comparison exposed, and makes connection cost visible per turn.

The finding that shaped the fixes was not the one expected. Raising the SDKs'
5-second keep-alive was meant to save a reconnect after a pause. But **every
streamed DeepSeek call opened a new connection, even back to back**. The OpenAI
SDK stops reading at `data: [DONE]` and closes the response with the body's last
bytes unread, and a connection closed mid-body cannot go back to the pool.
Keep-alive alone would have changed nothing.

**What changed**

- `llm/http.py` (new): both SDKs' own default clients, with idle connections kept
  300 s instead of 5 s, and each call's connection setup (TCP, DNS inside it,
  TLS) recorded against that call through httpcore's `trace` extension.
- `llm/openai_compatible.py`: the stream is read to the end of the body, one
  event per `data:` line, instead of through the SDK's iterator. An error event
  inside the stream, and a connection lost mid-reply (raw `httpx2`), both fail as
  `ProviderError`, closing the gap Chapter 6 noted for the LLM adapters.
- `LLM.connect()`: opens the connection at server startup, alongside the
  greeting, without billing anything. A failure is logged, not fatal.
- `llm/anthropic_provider.py`: `connect()` asks the Models API whether the model
  accepts `effort`, and it is sent only if so. **`--model claude-haiku-4-5` failed
  every turn**: Haiku 4.5 rejects the parameter with a 400.
- `reply_end` gains `connect_ms` (null when a connection was reused),
  `accepted_ms` (call to response headers) and `attempts` (HTTP requests the
  call took). The 💭 note reads "thought for 933 ms (accepted at 320 ms)", and
  adds "🔌 opened a connection first" or "↻ sent 3×" only when that happened.
  A first token later than 3 s is also logged on the terminal with the same
  split. Prompted by a live turn that "thought for 8.5 s", and nothing on the
  page could say whether that was the network, a retry or the provider.
- `uv run voice-agent --bench-llm [PROVIDER[:MODEL] ...]` (`bench.py`): each target
  connects as the agent does, then takes five short streamed turns with the real
  system prompt, round-robin. It reports connect time, accept and TTFT medians,
  TTFT range, total, reconnects, retries and tokens. A target without a key is
  skipped.
- `tts/openai_tts.py`: caught `httpx.HTTPError`, but the OpenAI SDK now raises
  `httpx2` errors, so a connection lost mid-synthesis escaped as a crash.
  `httpx2` is declared as the runtime dependency it actually is. `httpx` moves
  to dev, since only the test client still uses it.

**Design decisions**

- **Read the body ourselves rather than drain after the SDK.** The SDK closes the
  response inside its own iterator, so nothing can be read after it. Parsing
  `data:` lines is ~15 lines, and it is the wire format both providers use.
- **Connection cost goes in `Usage`,** the per-call record a turn already
  receives, and one a claimed speculation already hands over. It is recorded
  in a `ContextVar` set per call, so a warm, a guess and a turn running at once
  are each charged only for their own connections (tested).
- **Capabilities from the Models API, not a list of model names.** Which models
  accept `effort` changes every release. The lookup replaces the free startup
  request rather than adding one, and an unknown model name now shows up at startup.
- **300 s, not forever.** Measured idle, DeepSeek's, OpenAI's and Anthropic's edges
  all kept a connection for 150 s. At 290 s DeepSeek's had closed and the others
  had not. A closed one is noticed by the pool and replaced at the old cost.
- **Accept time and attempts are traced from the HTTP layer, not the adapters.**
  Response headers and each attempt pass through the same hook and `trace`
  extension that time connections. So SDK retries, which run inside one
  adapter call and are otherwise invisible, are counted in both providers alike.
- **The split works for DeepSeek, not for Anthropic.** DeepSeek sends headers in
  ~300–390 ms and then generates, so a slow first token after a quick accept is
  its queue. Anthropic holds headers until the first token (572 vs 575 ms
  median), so there the two numbers coincide and say nothing more.
- **The bench connects first,** because the agent does now. A "cold call on a
  fresh client" would measure a situation no turn is in any more.
- **Taking the server out of the media path: considered, not done.** Browser to
  server is localhost here, and every vendor's TCP handshake completes in 7–15 ms
  at a nearby edge. The legs that cost are behind those edges and inside the
  vendors, which no routing change reaches. It becomes a real question with
  deployment or telephony (docs/ROADMAP.md).

**Latency impact**

TTFT by provider, two runs of five turns each after connecting, short question
with the real system prompt, from Riga:

| Target | TTFT p50 | Range | Total p50 | Reconnects |
| --- | --- | --- | --- | --- |
| DeepSeek `deepseek-chat` | 930 / 718 ms | 565–1024 ms | 954–1213 ms | 0 of 10 |
| Claude Haiku 4.5 | **523 / 552 ms** | 409–647 ms | 728–765 ms | 0 of 10 |
| Claude Opus 5, effort low | 759 / 716 ms | 574–800 ms | 1767–1810 ms | 0 of 10 |
| Claude Sonnet 5, effort low | 1164 ms | 1027–1257 ms | 1706 ms | 0 of 5 |
| OpenAI `gpt-4o-mini` | not measured: no key | | | |

- **Haiku 4.5 is ~200–400 ms faster to first token than DeepSeek**, and steadier:
  its spread is under 250 ms, DeepSeek's more than 450 ms. The default provider is
  unchanged; that trades answer quality, and is a separate decision.
- **Opus 5 at low effort starts about as soon as DeepSeek** but writes slower. With
  Chapter 7 speaking while writing, TTFT is the number the user waits on.
- **Connections:** before, 25–32 ms of setup on every streamed DeepSeek call, and
  the first request in a process ~250 ms more than later ones. After, none per turn:
  live through the server, both the first turn and one after 8 s idle reported no
  connection opened. Small next to TTFT, but paid on every turn.
- **The 8.5-second turn** (DeepSeek, right after a barge-in) was not reproduced:
  ten calls with the same question afterwards were accepted in ~320 ms and
  answered in 570–1220 ms. It opened no new connection: the page was running the
  new script and showed no 🔌. That leaves provider queueing or an SDK retry
  after a refusal, which the new fields now tell apart.
- Measured with a one-line conversation. TTFT grows with context: Chapter 4
  measured +88 ms at 1.1k tokens and +390 ms at 11k on DeepSeek.

**Deliberately not done**

- Changing the default provider or model.
- OpenAI, Groq and Gemini in the comparison: OpenAI has an adapter and needs only
  a key; the other two would be new registry entries.
- Pre-opening the ElevenLabs socket before `reply_start` (~70 ms, which already
  overlaps TTFT), and anything about the recognizer's socket, which is already
  held open for a whole listening session.
- A TTFT percentile across real conversations rather than a bench: the golden
  conversation suite's job.

**Verification**

- `uv run verify` passes: 281 tests. New tests drive both real SDK clients against a local
  HTTP server that counts accepted connections: a second reply reuses the first's
  connection; connecting ahead leaves a reply nothing to open; the idle limit is
  300 s; concurrent calls are charged separately; usage from both providers'
  wire formats; an error event and a connection cut mid-body fail as
  `ProviderError`; two refusals (503, `retry-after-ms`) retried by either SDK
  count as three attempts; a model lookup a turn makes itself is not counted as
  a retry. Also: the slow-first-token log line. Also: effort only for models that accept it, connect at
  startup (and a failed one still starts), `connect_ms` in `reply_end`, bench
  ordering, skipping and reporting.
- **Twelve sabotages, eleven caught:** the SDK's iterator restored; a 5-s
  keep-alive; connect cost unreported in either adapter; effort always sent; no
  startup connect; `connect_ms` dropped from the frame; one record shared across
  calls; the bench not connecting; stream errors ignored; `httpx2` errors
  unmapped. Missed: counting TLS completion as a second connection, which has no
  observable effect, since only whether one was opened is reported.
- **Six more for the accept/attempt split, all caught:** attempts not counted;
  accept time never recorded; the Anthropic lookup counted as a retry (first
  missed, then a test added); no slow-turn log; `accepted_ms` dropped from the
  frame; the bench's retries column zeroed.
- Live: the bench above against real DeepSeek and Anthropic; typed turns through the
  server (`accepted_ms` 296, `attempts` 1, first token 895 ms); the reconnect on
  every streamed call reproduced before the fix.

## Karaoke — the reply lights up word by word as it is spoken

The text of a reply is on screen long before the voice reaches it: the model
writes 15–45× faster than it speaks. Chapter 8 already dimmed the part of an
interrupted reply that was never heard. This extends that one visual idea to
the whole of playback: words not yet spoken are dimmed and brighten as the
voice reaches them, and an interruption simply freezes them where they stopped.

It was cheap because the hard parts already existed. ElevenLabs' per-character
timing is recorded for barge-in, and the worklet already counts the samples it
plays. This change only puts the two in front of the page.

**What changed**

- `turn.py`: before each timed audio chunk, a `marks` message with its
  characters' end times (from the reply's first sample) and `from_ms`, where
  that audio starts. Untimed voices — the cached greeting, OpenAI — send none.
- `playback-worklet.js`: a playing stream posts `position` every 8 quanta
  (~43 ms); `player.js` passes it on as `onPosition(bubble, ms)`.
- `web/karaoke.js` (new, tested in node): `spokenChars` — how much of the text
  has been spoken, snapped back to a whole word — and `addMarks`.
- The page keeps each reply as text plus timeline, and repaints at most once a
  frame into a text span ahead of the telemetry notes. That replaces
  `textContent +=`, which also wiped a note landing mid-reply. Finished (or
  muted) reveals everything; `truncated` freezes at what was heard.

**Design decisions**

- **The clock is samples played, not time passed.** A stream that runs dry
  mid-reply has not moved on, so neither do the words; and it is the same count
  the page reports when interrupted.
- **One word rule on both sides.** `karaoke.js` mirrors `heard.py`, with tests
  written as the same cases, so the highlight freezes exactly where the history
  is cut.
- **A segment's timing is capped where the next segment's audio starts.**
  Measured live: the first generated segment is timed about twice as long as
  its own audio — 14 characters "ending" at 1022 ms with the next segment's
  audio at 499 ms, and 987 ms against 499 ms in an earlier probe — while every
  later segment fits its audio and the last mark lands within the reply's
  length. Uncapped, the timeline runs backwards there, and every word after
  it waits until the inflated end. The server's `Spoken` applies the same cap,
  which corrects Chapter 8's cut too (see its Fixes).
- **No highlight without timing**, rather than an even sweep that would run
  visibly ahead of or behind the voice.
- **Dimming starts with the first marks,** so typed and silent replies look as
  before. While muted the text is shown whole; marks are still recorded, so
  unmuting mid-reply lines up (skipping them misaligned every later mark —
  found in review).

**Latency impact**

- None on the audio path: marks are small JSON frames sent between audio
  frames. Measured live, about 1 kB of marks for an 11-second reply.

**Verification**

- `uv run verify` passes: 255 tests, including the page's node tests.
- New tests: marks precede their audio, cover the reply's characters, and are
  offset by the audio before them; position is reported only for samples
  actually played and only for the current stream; the word rule mirrors
  `heard.py`; capping earlier marks at a new segment's start, on both sides.
- **Seven sabotages, all caught:** marks after the audio; marks not offset
  (first missed — the fake voice's sub-millisecond timings rounded to zero —
  until a test with coarse timings was added); no word snap; position counting
  starved quanta; no-timing text dimmed; the cap removed on the server; the cap
  removed on the page. Not automatable: the page revealing everything when a
  reply finishes, which lives in `app.js`.
- Live, real ElevenLabs: marks arrive before the first audio, the page's own
  `addMarks` over them yields a timeline that never runs backwards, it covers
  the reply's characters, and the greeting sends none. Whether the highlight
  *looks* in step with the voice is for a listener in a browser, not yet done.

## Chapter 8 — Barge-in: the user can talk over the agent, and the history keeps what was heard

Since Chapter 3 the agent was half-duplex. The page dropped microphone audio for
as long as a reply played, so the only way to stop a 30-second answer was to
wait for it to end. Chapter 7 made that worse by starting to speak sooner. This
chapter removes the gate. The user talks over the agent, the agent stops, and
the conversation records the part of the reply the user **heard**, not the part
the model wrote.

That last part is the hard one, and it is not audio work. The model writes
15–45× faster than the voice speaks, so when someone interrupts, the whole reply
usually already exists and is already in the history. Left there, every later
turn reasons about sentences nobody heard. The answer needs two facts held in
two places. Only the browser knows how much audio it played. Only the server
knows which characters that audio was, from the per-character timing ElevenLabs
sends with its audio and Chapter 7 threw away.

**What changed**

- **One rule, in `session.py`:** a new turn from the user stops whatever the
  agent is still doing with the last one — writing it, speaking it, or both.
  `interrupt()` is reached three ways:
  - a partial transcript with words in it while the agent is audible (the early
    trigger, from a new `Mic` callback);
  - a typed message;
  - a committed transcript while the agent is still thinking. That cancels the
    reply; the user's earlier question stays, so two user messages in a row.
- **`interrupt()` sends `interrupt {id}` to the page first**, then stops the
  turns in flight, then cuts the reply down in the background. The page reports
  back `interrupted {id, played_ms}`; an answer with another id is ignored, so
  one arriving after its own timeout cannot settle the next interruption. The next turn waits for the cut, up to 1 s,
  before its question joins the history. Without an answer it falls back to the
  server's own estimate and says so (`estimated`).
- **`heard.py` (new):** `Spoken` records what a stretch of speech voices and when
  each character ends. `heard()` returns the text whose sound had ended, cut
  back to the last whole word — judged against the written reply, because
  speech can stop mid-word ("The Millennium Priz" was one live segment). The
  reply becomes just those words, or is removed if not a word of it was heard,
  or is left alone if the page says nothing was playing any more. `Conversation.replace` rewrites a message by
  identity.
- **`tts/base.py`:** `TTS.stream` yields `AudioChunk(pcm, alignment)` instead of
  bytes. `elevenlabs_tts.py` fills `alignment`; OpenAI has none.
  `whole_samples` carries timing across a re-cut.
- **A turn is one record** (`Turn`: its text, voice, interruption, the cut it
  waits for, and whether it has started). A turn already running is cancelled;
  one still waiting for the turn before it is not — it records its question
  and returns. Cancelling it there, outside `run_turn`, lost the question.
- **`turn.py`:** a turn cancelled *because it was talked over* keeps its question
  and records what was written, then returns normally (`Interruption`,
  `uncancel()`). Cancelled because the conversation ended or the socket closed,
  it still drops the question, as before. `reply_end` gains `interrupted`.
- **The greeting** returns its `Spoken`, so it can be interrupted too.
- **The page:**
  - Microphone frames are sent while the agent speaks; the browser's echo
    cancellation (already requested) is what keeps the agent's voice out.
  - On `interrupt`, `player.stop()` silences the reply and the worklet reports
    the samples it actually pulled for the speaker, less `outputLatency`.
    Leftover audio of that reply is ignored until the next `reply_start`.
  - On `truncated`, the unheard part of the reply is dimmed, with a note:
    played time, heard vs written characters, and time to stop.

**Design decisions**

- **The trigger is the recognizer's words, not our own VAD.** Scribe realtime
  sends no speech-started event (checked against its SDK message types) and the
  TTS socket knows nothing about the microphone, so the options were Scribe's
  first partial or a voice detector of our own in the page. Words are slow
  (below) but ignore coughs and doors, and need no new component. A fast local
  VAD that pauses first and lets words confirm is the next increment; it also
  gives turn detection the speech-end timestamp this server cannot see today.
- **Any word interrupts**, including "mm-hmm". One rule to measure before
  filtering by intuition.
- **Words while the agent is still thinking do not cancel it; a commit does.**
  A partial can be noise that never commits, and cancelling on it would leave
  the user with no answer at all.
- **Timing from ElevenLabs, not a proportional guess.** Measured live with one
  probe: `alignment` spells out exactly the text sent (`normalizedAlignment`
  rewrites it: "—" as "--", a leading space). It arrives on the first audio
  message of each generated segment, timed from that message's start, and
  covers the untimed messages after it. A proportional estimate is still used
  where there is no timing — OpenAI and the cached greeting — and is crude:
  speaking rate is not constant.
- **No marker in the reply; the system prompt explains instead.** The first
  version appended `… [interrupted]` to the cut reply, so the model would know
  the rest went unheard. Listened to live, after two interruptions DeepSeek
  began ending its *own* replies with that marker — breaking a story off early
  and having the voice say "interrupted" where nobody had. A reply's text is
  the model's own past output and it imitates it. The history now holds only
  the heard words (as OpenAI Realtime's truncation and LiveKit do), and
  `prompts/system_prompt.md` gains one rule: a reply that stops mid-sentence is
  where the user cut in. With nothing heard, the reply is removed. Replaying
  that live conversation to DeepSeek five times each: with the marker, 4 of 5
  replies ended "… [interrupted]" of their own accord; with heard words and the
  prompt rule, 0 of 5.
- **The page is told before the reply is stopped.** Cancelling the synthesizer
  can wait up to 1 s for its socket to close, and silence is the part the user
  notices.
- **"Audible" ends only when the page says so** (`playback` false after true),
  or on interruption. A reply the page finished long ago can therefore still
  draw an `interrupt`; the page answers "nothing playing" and nothing is cut.
  A false positive costs one message; a false negative would talk over the user.
- **Cost:** Scribe now also bills for the audio sent while the agent speaks.

**Latency impact**

Live: a scripted browser against the real server (DeepSeek, ElevenLabs flash,
Scribe). It asks for a ~40-second answer and, 2.5 s or 5 s into playback, streams
a pre-recorded "Wait, stop. Tell me about Tallinn instead." as microphone audio.
Measured from the recorded speech's first voiced sample:

| Run | Speech onset → `interrupt` sent | Of which the server's own reaction |
| --- | --- | --- |
| 1–5 | **850, 1161, 1249, 1643 ms** (one run's figure was not captured) | 0 ms (`stop_ms`) |

- **All of it is the recognizer.** The interrupt goes out in the same moment the
  first partial with words arrives. Up to ~1.6 s of the agent talking over the
  user is well past the ~200 ms a human expects; it is the baseline the local
  VAD chapter has to beat.
- **What was heard, live:** 3.8 s in, the history kept "Riga was founded in
  1201 by Bishop Albert, a German cleric who" (62 of 611
  characters); 5.9 s in, 102 of 592. Both read as what that much speech says.
- **Not measured:** time from `interrupt` to silence in a real browser, and
  self-interruption on laptop speakers. The scripted client has no speaker, so
  it cannot hear its own echo.

**Deliberately not done**

- A local VAD that pauses on speech and resumes on a false alarm — next.
- An echo guard that ignores partials matching the agent's own words: only if
  self-interruption is observed on speakers.
- Backchannel filtering ("mm-hmm" does not interrupt).
- Anything about billing for text sent to the synthesizer but never played.

**Verification**

- `uv run verify` passes: ruff, format, mypy strict, 250 tests; the page's node
  tests pass (31).
- New tests:
  - Words over the agent stop it: the reply and the voice are no longer being
    made, the question stays, the answer becomes the heard words and
    nothing more, and `interrupt` goes out before `reply_end`.
  - The system prompt says what a reply that stops mid-sentence means.
  - Nothing heard removes the answer; nothing playing keeps it whole; words
    after the page reported the end are not an interruption.
  - Words while thinking do not cancel; a new question while thinking replaces
    the answer and keeps both questions.
  - A typed message interrupts, and the next turn's context holds the cut reply.
  - A page that never answers falls back to an estimate; ending mid-settle still
    ends last; no guess is started over the agent's voice; an interrupted
    greeting is cut by estimate.
  - A turn overtaken before it began keeps its question; a late answer to one
    interruption does not settle the next.
  - `heard()`: part of a word is not heard, including a word the voice stopped
    halfway through; punctuation stays; segments are offset by the audio
    before them. The adapter reads `alignment` and ignores
    malformed timing. The worklet reports samples played on stop, and answers
    for a stream that already finished; the player releases the gate on stop.
- **Sixteen sabotages, all caught** (one only after its test was fixed: it had
  been passing on the running-turn guard instead). They are: no truncation; a
  running turn not cancelled; a waiting turn cancelled; the page's answer
  matched without its id; the word boundary judged on voiced text; `uncancel` skipped; partials never interrupting; the next
  turn not waiting for the cut; partials during thinking cancelling; the page
  told after the stop; a partial word counted as heard; segment timing not
  offset; the half-duplex gate restored; the worklet counting nothing; a guess
  over the agent's voice; timing lost in a re-cut.
- A pre-commit review found three of those by reproducing them — the lost
  question, the half word (a cut reply ending "one two thre") and the unmatched
  answer — before any test existed for them.
- Live, as above: two runs at each of two interruption points, then one more
  after the review fixes, with the server log checked each time. Clean after
  the fix below.

**Fixes**

- A turn that dies on its own on a closed socket is logged, not printed as "Task exception was never retrieved" (seen live, pre-existing since Chapter 7).
- The first segment's inflated timing is capped where the next segment starts, so an early interruption no longer undercounts what was heard (found building karaoke).

## Chapter 7 — Streaming synthesis input: the voice starts while the reply is still being written

Chapter 6 streamed synthesis *output*, but the text still went in whole, after
`reply_end`. First audio therefore waited for the reasoning engine to finish
writing the whole reply: 400–900 ms more on a 20–40-second answer, growing with
its length. This chapter hands every token to ElevenLabs as it is written, over
its `stream-input` WebSocket, so the voice starts once the synthesizer has
enough of the first sentence. On long replies that is now well before the reply
has finished being written.

The hard question was not transport but **who decides when there is enough text
to speak**. Audio once generated is final: when "ug" arrives, the sound for "Da"
cannot become the start of "Daugava". Something has to choose when to commit.
We answered that by measuring rather than by building a sentence splitter first.
The service's own chunk schedule decides. Past each character threshold, it
speaks a prefix that ends on a word and holds back the unfinished tail, which it
then voices together with the next text to arrive. Where a reply is cut is not
this project's code.

**What changed**

- `tts/base.py`: `TTS.stream` takes an `AsyncIterator[str]` rather than a string.
  It replaces the old signature rather than adding a second one; `once(text)`
  adapts a whole text.
- `tts/elevenlabs_tts.py`: synthesis goes over the `stream-input` WebSocket,
  spoken to directly as Scribe is, since the SDK binds it only as a blocking
  client.
  - Tokens are forwarded exactly as written: half words, no spaces added, and
    empty fragments skipped, since an empty text ends the stream.
  - The schedule is the service default, `[120, 160, 250, 290]`, stated explicitly (see Fixes). The final empty text makes the
    service voice whatever it is still holding.
  - A handshake refused with an empty 403 names what to check.
- `tts/openai_tts.py`: crude on purpose. `/audio/speech` takes whole text only,
  so the reply is gathered and synthesized once it is written: the batched wait,
  kept for this backend.
- `turn.py`: a turn's voice is a `Speech` task, started at `reply_start` so the
  socket connects while the model is still thinking.
  - Each fragment goes out as a `delta` and into the synthesizer at once, and
    audio frames interleave with the text.
  - If the reply fails after speaking began, the audio is closed before the
    error is sent. A cancelled turn cancels its voice.
  - `audio_end` gains `audio_before_reply_end`. `synthesis_first_byte_ms` now
    runs from the first words handed over, so it includes the service's buffer.
- The page: `audio_start` no longer detaches the reply bubble, which would have
  dropped every delta after the voice began. The audio line says "spoke before
  the reply was written" and shows the words-to-sound time.

**Design decisions**

- **Our own sentence cutting was not built.** Pipecat and LiveKit Agents both
  aggregate sentences client-side, and it was the first plan. It was dropped
  when measurement showed the service's schedule already cuts on word
  boundaries and carries context across cuts. At the cut there was no
  measurable pause in 2 of 3 runs and ~180 ms in the third, against ~480 ms for
  a real sentence end. That is one voice and one model, a few runs each, so the
  thresholds are indicative, not a spec. If length-based cuts are ever heard,
  the fix is a first cut at the first clause or 50 characters, whichever comes
  first — our code, sent with `auto_mode`.
- **Not `auto_mode`, although it sounds like the smarter option and the docs
  recommend it for LLM output.** It switches buffering *off* and speaks each
  message the moment it arrives; it is meant for clients that send whole
  sentences. One real DeepSeek reply (112 tokens) was replayed with its original
  timing into each mode:

  | Mode | First audio after first token | Audio length |
  | --- | --- | --- |
  | `auto_mode`, raw tokens | 303 ms | **59.9 s**: every token its own utterance, "Da · ug · ava" |
  | default schedule `[120,160,250,290]`, raw tokens | 346 ms | 32.9 s |
  | schedule `[50,120,160,250]`, raw tokens | 223 ms | 33.0 s |
  | `auto_mode`, whole sentences | 234 ms | 31.5 s |

  Pipecat reaches the same rule: auto mode on for sentence aggregation, off for
  token streaming.
- **No word re-cutting.** The endpoint's docs say each text should end in a
  space. A whole-word re-cutter was written, then removed: 4-character slices
  sent mid-word were voiced correctly, and re-cutting only delays text.
- **Larger later chunks cost nothing.** The model writes 15–45× faster than
  speech, so after the first piece the text is far ahead of the voice, and more
  lookahead buys intonation at no latency. Not the whole remaining reply,
  though: the service caps a chunk at 500 characters, and once barge-in exists,
  text already sent is billed whether or not it is heard.
- **The socket opens at `reply_start`, not on the first token.** The handshake
  (~70 ms measured) then overlaps time-to-first-token. A blank reply opens a
  socket and sends only the end message: measured, that returns `isFinal` and
  no audio, so it bills nothing. The rule that a blank reply is never
  *announced* as speech still holds.
- **No pre-roll.** With streaming input, the gap risk is the text running dry
  rather than network jitter. The worst case is a stalled model after a short
  first piece, measured at ~1 s of silence when text stopped for 2 s. The page's
  gap counter reports it if it happens.
- **A reply that fails mid-speech still fails closed.** The question is dropped
  from the history, as before, even though part of the answer was heard;
  recording what was heard is barge-in's job. The audio is closed first, or the
  page would keep the microphone muted waiting for it.

**Latency impact**

Live, real DeepSeek and real ElevenLabs `eleven_flash_v2_5`, typed turns. The
previous commit and this change ran back to back on the same prompts. "First
audio" is send to first audio byte, confirmed within 10 ms by the client:

| Reply | Before: first audio | After: first audio | Spoke before the reply was written |
| --- | --- | --- | --- |
| ~1 s ("Riga.") | 1127–2063 ms | 676–1239 ms | no: 5 chars, under the first threshold |
| ~17–23 s | 1287–1491 ms | **881–1309 ms** | yes |
| ~32–40 s | 1682–1887 ms | **975–1228 ms** | yes |

- **Long replies no longer wait for the reply to be written.** First audio now
  runs 460–800 ms before `reply_end` on 35-second answers. The saving grows
  with reply length, since it is the generation time that is no longer waited
  for.
- **Short replies: no measured change.** The whole reply is under 50 characters
  and is voiced when it ends. The run-to-run spread is time-to-first-token
  (512–1692 ms here), which this chapter does not touch.
- **First words to first sound:** 164–313 ms, the service's buffer plus its
  synthesis. That is what first audio is now built from: time-to-first-token
  plus this.
- **Gaps:** none in 6 replies of 17–40 s, simulated from each chunk's real
  arrival time against a playback clock. Not yet counted by a browser.

**Deliberately not done**

- Our own clause or sentence cutting, and per-sentence requests for OpenAI:
  both wait for evidence that they are needed.
- Speculative synthesis of a reply before its turn commits: next.
- Barge-in, multi-context sockets, and using the service's per-character
  `alignment` to record what was actually heard.
- A pre-roll buffer, and reconnecting a synthesis socket that drops mid-reply.
  The reply degrades to text, as before.

**Verification**

- `uv run verify` passes: ruff, format, mypy strict, 217 tests.
- New tests:
  - The voice starts before `reply_end` with a paced model; this is the
    chapter in one assertion.
  - Against a local WebSocket speaking `stream-input`: tokens are forwarded
    verbatim with the schedule and no empty fragment; the URL has no
    `auto_mode`; audio arrives while text is still being sent, in whole
    samples; an error payload, a dropped connection, a refused handshake and an
    unreachable host are all `ProviderError`s; a reader that stops closes the
    socket without waiting on unwritten text.
  - A reply that fails after speaking closes its audio once, even when the
    voice had already failed and closed it.
  - Ending or closing mid-sentence stops synthesis.
  - OpenAI gathers the text and skips a blank one.
- **Six sabotages, all caught:**
  - speech fed only after the reply;
  - audio left open on failure;
  - the voice not cancelled with its turn;
  - an empty fragment forwarded;
  - the sender task not cancelled (a timeout-based version of that test passed
    anyway, because the cleanup under test swallowed the timeout's own
    cancellation; it is now timed);
  - the double-`audio_end` guard removed.
- **Live, against the real service:** ending with "bye" mid-sentence sends
  `ended` last with nothing after it, 40–42 ms after the "bye". The greeting,
  started with an empty cache, was synthesized over the new socket in 429 ms,
  cached and played. Closing the socket mid-reply first logged an ERROR
  traceback (see Fixes). An earlier "no traceback" had been read from a log
  that captured nothing. Not re-run live since the fix; covered by a unit test.
- **Not yet verified:** listening to it in the browser (the WAVs above were
  listened to, the page was not), the microphone path, and OpenAI live (no key).

**Fixes**

- A turn cancelled while its `reply_end` was being written left its voice running, sending audio after `ended`; one `finally` now cancels the voice however the turn ends.
- The first piece waits for 120 characters, not 50: listened to live, a 50-character first piece ("The Millennium Prize Problems are seven big…") was spoken as its own utterance with an unnatural pause after it, and at ~760 chars/s the wait costs ~100 ms. The latency table above was measured with 50, so first audio is now slightly later.
- The synthesis socket closes with a 1 s timeout, not the 10 s default, so a cancelled voice cannot hold up `ended`.

## Refactor — A turn gets its own task, and `server.py` its own modules

By this point `server.py` was 1,192 lines, more than half the source. Its
socket handler was a 200-line closure whose speculation and warming state lived
in `nonlocal` variables, each with a separate stats class next to it. The
structural problem was **where a turn ran**: on whichever coroutine produced
its text. A spoken turn ran inside the microphone's task and a typed one inside
the receive loop, so for the length of a reply neither could read anything.
That was a bug as well as a shape. Pressing stop listening mid-reply was not
seen until the whole reply and its audio had gone out. If the reply took more
than five seconds, `Mic.stop()` cancelled it mid-stream, leaving a question with
no answer in the history and an `audio_start` with no `audio_end`. The session
cap could do the same. Barge-in has to cancel a turn while still reading audio,
so this had to change before that chapter.

**What changed**

- `session.py`: `Session` owns one connection. `submit()` starts a turn in its
  own task and returns at once; turns queue behind a lock rather than overlap.
  Exit commands are recognised here only (they were checked in three places).
  Ending the conversation or closing the socket cancels any turn in flight.
- `warming.py`: `Warmer` owns the warm task, the growth throttle and the report
  (it merges `Warmings` with the closure state). `speculation.py`: `Speculator`
  does the same for the guess (it merges `Guesses` with the closure state).
- `turn.py`, `mic.py`, `greeting.py`, `channel.py`, `timing.py`: moved out of
  `server.py` unchanged. `server.py` keeps the routes and the receive loop.
- `run_turn` drops the user message when it is cancelled before a reply exists,
  the same fail-closed rule it already applied to provider errors. It also closes
  the LLM and TTS streams explicitly, however the turn stops.
- A transcript that arrives after the conversation ended starts no turn, and
  `ended` is sent only after the turns it stops are gone.

**Design decisions**

- Queue, not reject, a turn that arrives while one is running. This matches
  what the lock did before; barge-in is the chapter that decides otherwise.
- No speculation while a turn is running: that turn's reply is not in the
  history yet, and a guess adopted without it answers the wrong conversation.
  Warming carries on, because a prefill cannot be adopted as a reply. The old
  shape never had to decide this, since no partial could arrive during a turn.
- A socket that closes mid-turn now cancels the turn rather than finishing a
  reply nobody will receive.
- Provider streams are closed explicitly rather than trusted to cancellation.
  A cancel usually lands in a socket write *between* fragments, which leaves the
  generator suspended, and with it the provider's HTTP stream open and billed
  until garbage collection. `Speculation` is unaffected, because its task is
  only ever suspended inside the generator.
- `tests/test_session.py` drives `Session` against a channel whose writes take
  time. A fake write that never suspends is what hid the ordering bugs above.

**Latency impact**

- None on the critical path. Transcripts and control messages are no longer
  delayed by the length of a reply.

**Deliberately not done**

- Two tabs on one link still take turns without a shared lock, so a failed
  turn's `pop()` can remove the other tab's message. That belongs with
  persistence.

**Verification**

- `uv run verify`: 203 passed. The regression test (stop listening
  mid-reply → the stop is acknowledged before `reply_end`, the reply is
  complete, and the history alternates) failed on the old shape. Each test for
  this entry's edge cases (late commit, `ended` last, streams closed, no guess
  mid-turn, the mic stopping itself) fails when its fix is reverted.
- Not exercised with a real microphone yet.

**Fixes**

- Closing a session no longer aborts its cleanup when a turn ends with the socket's error instead of a cancellation (seen live as an ERROR traceback on disconnect mid-reply).


## Instrumentation — how many chunks, fragments and tokens a turn was

Every turn already reported its timings and its size in characters and bytes.
What it did not say is how the stream was cut up or what it cost in the unit
providers bill: a long answer's `🔊` line gave 7,340 kB but not that it was
~7,200 WebSocket frames, and the `💭` line gave 2,328 characters but no tokens.
Those are the numbers Chapter 7's text chunking and any cost work will reason
in, so they are on screen before that work starts.

**What changed**

- `llm/base.py`: `Usage` — prompt, cached and output tokens as the provider
  reports them. `stream()` takes one optionally and the adapter fills it in as
  the stream ends. DeepSeek and OpenAI need `stream_options={"include_usage":
  True}` to report anything while streaming; Anthropic's comes from the final
  message, where the prompt is `input_tokens` plus cache reads plus cache writes.
  The cached-token reading shared with warming moved into one helper.
- `speculation.py`: a guess carries its own `Usage`, and a turn that adopts it
  reports that — the turn itself made no call to count.
- `server.py`: `reply_end` adds `fragments`, `output_tokens`, `prompt_tokens`
  and `cached_tokens`; `audio_end` adds `chunks`.
- The page: `💭 thought for 526 ms · 2328 chars · 512 tokens in 498 fragments ·
  2.9 s`, a `📥 1390 prompt tokens · 1152 cached (83%)` line, and `🔊 … kB in
  7168 chunks`. The numbers here show the format, not a measurement.

**Design decisions**

- **Fragments and tokens are both shown, because they are not the same thing.**
  A fragment is one provider event, which for DeepSeek was one token every time
  (140 of 140, live) and for Anthropic is routinely several. The fake engine in
  the tests reports twice as many tokens as fragments so that reporting one as
  the other fails.
- **A `Usage` passed in, not a value yielded at the end.** `stream()` stays an
  iterator of text, so the speculation replay and the turn loop are unchanged;
  a mutable record per call is also safe with a guess and a warm running at once,
  where an attribute on the shared adapter would not be.
- **Tokens absent rather than zero** when a provider reports none — "0 tokens"
  on screen would read as a measurement.
- **No tokens-per-second.** A turn that adopted a guess reads fragments that were
  generated before it started, so its generation time is not the provider's;
  the rate would be confidently wrong on exactly the turns that matter.

**Latency impact**

- None measurable: one extra final chunk from the provider, and four integer
  fields per message.

**Verification**

- `uv run verify` green: 194 tests.
- Live, DeepSeek and ElevenLabs: a 3-token reply reported 3 fragments, 1,171
  prompt tokens and 1,024 cached; a 140-token reply 140 fragments; the server's
  `chunks` (1,844) matched the binary frames the client counted.
- Sabotages caught: not requesting stream usage, reading usage only from chunks
  that carry text (the usage chunk has none), dropping Anthropic's cache writes
  from the prompt, an adopted guess losing its usage, reporting fragments as
  tokens, and not counting chunks. One sabotage was void — swapping `continue`
  for `break` on the text-less chunk changes nothing, since usage is read first.
- Anthropic's usage path is exercised against a fake stream only; no Anthropic
  key was used.

## Refactor — The browser client, split into modules it can test

Chapter 1 put the page's script inline in `index.html`: no framework, no build
step, one file. That was right for a chat box. By Chapter 6 the script was ~400
lines doing five jobs — the socket protocol, the chat log, microphone capture
with its worklet embedded as a string, the streaming player, and the autoplay
and half-duplex rules — sharing a dozen mutable globals. The cost was specific:
**none of it could be executed under test.** `test_web_client.py` checked the
player by searching its source for strings like `Math.max(now, s.next)`, so the
scheduling arithmetic, the gap counter and the gate had never actually run in
the suite, and the checker had twice read comments and template literals as
code. Chapters 7, 8 and barge-in all add browser logic; this was the point to
stop that pile from growing in a file nothing can import.

No behaviour changes: message formats, on-screen text and every rule the
Chapter 6 review fixed are the same. This is a move, not a rewrite.

**What changed**

- `web/`: `index.html` is markup and styles only, loading `app.js` as a module.
  `player.js` owns playback and takes the page as callbacks (`makeContext`,
  `makeNode`, `isMuted`, `onSpeaking`, `onWaiting`, `onFinished`, `onError`),
  so it has no DOM or socket in it. `playback-worklet.js` holds the audio-thread
  side, with its queue and message handling exported so node can run them.
  `mic.js`, `ui.js`, `protocol.js` hold their own jobs; `capture-worklet.js` is
  a real file loaded by URL instead of a Blob. `app.js` wires them and owns the
  state they share — including `speaking`, still assigned in exactly one place.
- `server.py`: serves `web/` at `/static`, with `Cache-Control: no-cache` so
  a reload can never link a fresh module against a stale cached neighbour.
- `tests/web/*.test.mjs`: 26 executed tests under `node --test`, run from
  pytest. The player is wired to the real worklet logic through a fake port that
  passes messages by structured clone with transfer, as a browser does.
- `tests/test_web_client.py`: the text-matching checks the node tests replace
  are gone. What stays is structural: every module parses; every name a module
  calls is defined in it or imported — checked per module, since a name defined
  in a sibling is still a ReferenceError; every import is actually exported;
  element ids exist in the markup; the mic send path keeps its gate; and the
  wiring order in `onsubmit`, which is not the player's to test.

**Design decisions**

- **Native ES modules, not TypeScript and a bundler.** Modules give imports,
  scope and executability with nothing to install and nothing to build. A
  toolchain would be the project's first non-Python dependency, and would
  justify itself with types and components this page does not yet need.
- **Node's built-in test runner, not a browser test framework.** It ships with
  node, which the suite already used for `node --check`. It cannot test audio or
  the DOM, which is why the logic worth testing was moved behind callbacks
  rather than the tests being given a fake browser.
- **The player takes the page as callbacks.** The alternative — a module that
  imports `ui.js` and touches `document` — can only be tested with a DOM
  emulator. Callbacks keep the rules (when the gate opens, what counts as a
  gap, what a gesture drops) in plain code.
- **The structural checks stay.** An executed test of `player.js` cannot see
  `app.js` calling a function nobody defines; that exact failure once shipped
  with every Python test green.

**Latency impact**

- None intended. The page now makes six module requests instead of zero,
  locally and once per load, before any audio; not measured.

**Deliberately not done**

- No executed tests for `app.js`, `mic.js` or `ui.js`: they are DOM and device
  wiring. `app.js` was exercised once by a scratch script with fake browser
  globals — greeting waits for a gesture, sending drops it, a reply plays at
  24 kHz and releases the gate with its gap report — but that is not in the
  suite.
- No linting or formatting for JavaScript; the project has no JS tooling and
  this step does not add any.

**Verification**

- `uv run verify` green: ruff, ruff format, mypy strict, 190 tests — one of which
  runs the 26 node tests.
- **Sabotages, all caught — but not on the first run either time.** Deleting a
  function from `app.js` passed because the call checker read all modules as one
  text and found the same name exported by `ui.js`; it now checks each module
  against its own definitions and imports. Against the worklet player, trusting
  a report for a replaced stream passed because a wired test cannot produce the
  race; a test now delivers the late report directly.
- Served for real: all six modules return 200 as `text/javascript`, and the page
  loads the entry module.
- **Driven in headless Chrome against the live server** over the DevTools
  protocol: every module and both worklets load from `/static`, a typed message
  goes out, and a real 126.7-second ElevenLabs reply streams in as 5,942 frames
  and plays with the gate closed for 126.73 s and zero gaps. That run found a
  bug no test had: the player counted a chunk's samples after posting them, and
  a transferred array reads as empty, so every reply's gate reopened 40 ms in.
  The fake port now transfers for real, and the bug fails four tests.

**Fixes**

- A typed message is sent before any audio setup, so a browser that refuses the 24 kHz output context loses the voice, not the words.
- Page tests skip on node older than 22 with a reason saying so, rather than failing as if a module were broken; README lists node as optional.

## Chapter 6 — Streaming synthesis: the voice starts while it is still being made

Chapter 2 made synthesis batched on purpose — the whole reply in, one MP3 out —
and measured the cost as a wait that grew with the answer: 144 ms for a
sentence, 1.3 s for a 40-second reply, all of it sitting between the text
appearing and the first sound. Chapter 5 cut time-to-first-*token* and said out
loud that first *audio* still waited. This chapter removes the synthesis half
of that wait: audio now comes back as a stream of raw PCM frames, and the
browser plays each one the moment it arrives. The provider's first byte lands
in 130-166 ms regardless of how long the reply is.

It streams the *output* only. The whole reply text still goes in at once, after
`reply_end`, so the wait for the reasoning engine to finish writing is untouched
— on a long answer that is now the larger half. Feeding the synthesizer text
while it is still being generated is Chapter 7, and it is a different problem:
where to cut text without damaging prosody. Keeping the two apart meant this
chapter's hard part — transport and playback — was debugged on its own.

**What changed**

- `tts/base.py`: the `TTS` protocol is now `stream(text) -> AsyncIterator[bytes]`,
  PCM s16le mono at 24 kHz, every chunk holding whole samples. `AudioClip` and
  `synthesize` are gone — this *replaces* Chapter 2's batched interface rather
  than adding a mode beside it. `whole_samples` re-cuts a provider stream so no
  chunk splits a sample; `pcm_seconds` derives duration from size.
- `tts/elevenlabs_tts.py`: the `/stream` endpoint at `pcm_24000`, errors mapped
  on iteration since that is where the request actually happens.
  `tts/openai_tts.py`: `with_streaming_response` with `response_format="pcm"`.
- `server.py`: speech is `audio_start` → N binary frames → `audio_end`, replacing
  the `audio` announcement plus one binary frame. `audio_end` reports
  `synthesis_first_byte_ms` (the provider), `synthesis_ms` (the whole stream),
  and `first_audio_ms` — send to first chunk, which replaces `total_ms` as the
  number the project is judged on. `ready` announces the voice's sample rate.
  The greeting is cached as PCM and delivered through the same three-part shape.
  A blank reply is no longer sent to the synthesizer.
- The page: the `<audio>` element is replaced by an AudioWorklet that plays a
  queue of PCM as one continuous output, on an `AudioContext` running at the
  stream's rate. If the queue runs dry mid-reply that is a **gap**, counted on
  the audio thread, shown (`⚠ N gaps · T of silence mid-reply`) and logged by
  the server. The telemetry line is now
  `🔊 <length> of speech · <size> kB · audio at <first_audio> · first byte after
  <synthesis_first_byte> · synthesized in <synthesis>`.

**Design decisions**

- **Raw PCM, not MP3 chunks or Opus.** Both backends produce 24 kHz s16le
  natively, the browser needs no decoder, every chunk is playable the instant it
  lands, and duration is arithmetic. MP3 through MediaSource buffers before it
  plays and splits frames across chunks; Opus needs WebCodecs and a container.
  The price is ~384 kbps against 128 — nothing on localhost, and a real question
  for the telephony chapter, which will want μ-law or Opus anyway.
- **An AudioWorklet queue, not one `AudioBufferSourceNode` per chunk.** Shipped
  the other way first — no cross-thread protocol, a gap is one comparison — and
  it passed every test, every seam check and a live 58-second reply. Then a
  100-second reply was listened to: clicky and pitch-warped at the start,
  recovering towards the end. Every scheduled source node is work on every
  128-frame render quantum until it plays, and a whole reply is scheduled
  within two seconds. Timed by offline rendering in Chrome:

  | Reply | Chunks | Per-quantum cost, one node per chunk |
  | --- | --- | --- |
  | 20 s | 940 | 84 µs |
  | 58 s | 2,700 | 332 µs |
  | 98 s | 4,600 | 783 µs |
  | 192 s | 9,000 | 2,677 µs |

  Those are averages against a 5,333 µs budget that the device callback, the
  resampler and the OS share; the spikes around them are what missed deadlines,
  and the load fell as nodes played out — the recovery that was heard. With the
  worklet queue, each second of rendering timed separately: steady-state cost is
  flat at a 1.1-1.6 µs median (2-3 µs max) with 0, 4,600 or 20,000 chunks
  queued. The one length-dependent cost is taking the messages in — ~180-200 µs
  per quantum averaged over the first second for 20,000 chunks, once. The output
  was bit-identical to the source over 20 s of 1,024-byte chunks, the queue
  counts real underruns rather than late arrivals, and it already drops a stream
  instantly, which barge-in will want. An earlier headless "no overload" check
  had measured throughput against a fake audio device that never misses a
  deadline — the wrong question.
- **Queue operations are amortized O(1).** Appending is an array push, a render
  quantum copies 128 samples from at most a couple of chunks, and played chunks
  are released. Consumed slots are compacted away only once they are half the
  array: the first version compacted every 256 chunks, which re-copied
  everything still queued — and since a reply arrives far faster than it plays,
  that is nearly all of it. A test counts the copies: 768,229 slots to play
  20,000 chunks before, at most 20,000 now. Honestly, no timing showed the
  difference at these sizes; the fix removes a quadratic term, not a heard
  problem. Memory is O(audio not yet played), and nothing of played audio is kept.
  The per-quantum copy is a plain loop rather than `set(subarray(...))`, so the
  audio thread allocates nothing while playing: a view per quantum is garbage
  collected on the thread that must not pause.
- **The output context runs at the stream's rate, not the device's.** Planned
  the other way, changed on measurement: rendering ElevenLabs-sized chunks
  offline at 48 kHz put 632 clicked samples at the joins (36.8 dB against a
  continuous render), and at 24 kHz none. The worklet keeps this: it plays the
  stream sample for sample and the browser resamples the output once, rather
  than the worklet carrying a resampler of its own. The microphone context
  already relied on a requested rate, so the support argument did not hold.
- **No pre-roll.** Playback starts on the first chunk. Synthesis measured at
  ~36× real time on localhost, so a jitter buffer would spend latency on every
  turn to guard against stutters not yet observed. The gap counter is what
  would justify one.
- **`audio_start` is sent lazily, on the first chunk.** A synthesis that fails
  outright then looks exactly as it did batched — an `audio_error` and no audio
  begun — rather than a stream opened and never closed. One that fails
  mid-reply closes what it sent with `audio_end`, then reports the error; the
  text is kept either way.
- **The mute window is what is left to play, not the reply's length.** Audio
  is already playing while the rest arrives, so the server now returns length
  minus time since the first chunk. It errs short — the browser starts a moment
  after the send — and the browser's own `playback` hold covers the difference.
- **The half-duplex gate reopens only when `audio_end` has arrived *and* the
  last sample has played.** Either alone releases it while the agent is still
  audible.
- **Replaced, not toggled.** A `STREAM_SYNTHESIS` switch would have kept two
  audio protocols alive in the server and the browser for a comparison that
  was measured once, below, before the batched path was removed.
- **The greeting's cache key includes the audio format**, and a synthesis that
  fails partway is not cached. An MP3 cache read back as PCM would not fail; it
  would play as noise, every visit.

**Latency impact**

Live, real DeepSeek and real ElevenLabs (`eleven_flash_v2_5`), typed turns,
two runs each, timed server-side and confirmed within 40 ms by the client.
"First audio" is turn start to the first audio byte sent:

| Reply | Batched synthesis | Batched first audio | Streaming first byte | Streaming first audio |
| --- | --- | --- | --- | --- |
| ~1 s of speech | 144-429 ms | 757-1521 ms | 130-140 ms | 789-1060 ms |
| ~20 s | 749-850 ms | 2041-2107 ms | 154 ms | **1310-1391 ms** |
| ~40 s | 1219-1259 ms | 2873-3239 ms | 155-166 ms | **1555-1683 ms** |

- Synthesis's contribution to first audio is now flat at ~150 ms instead of
  growing with the reply — inside the §7 budget's 150 ms for that stage.
- On a short reply the difference is within run-to-run noise: synthesis was
  already small, and time-to-first-token (543-982 ms here) dominates.
- On long replies what remains is the reasoning engine's full generation time
  (370-1060 ms), which this chapter does not touch. That is Chapter 7's target.
- Browser playback latency was **not measured**. Gaps were: zero across a
  126.7-second reply played by the page in headless Chrome.

**Deliberately not done**

- No streaming *input*: the synthesizer still waits for the whole reply (Chapter 7).
- No speculative synthesis of a reply before its turn commits (Chapter 8).
- No barge-in, and so no record of what was actually heard — though the worklet
  can already drop a stream instantly.
- No jitter buffer, and no reconnect or resume mid-stream.
- One WebSocket message per provider chunk — ~1,800 for a 40-second reply. Fine
  locally; coalescing belongs with a transport that is not localhost.
- Only the output format both current backends share. A backend without 24 kHz
  PCM would need resampling behind the interface.

**Verification**

- `uv run verify` green: ruff, ruff format, mypy strict, 164 tests (169 after the fixes below).
- New tests: odd-sized chunks re-cut to whole samples with nothing lost; both
  adapters request PCM and stream in whole samples; an ElevenLabs error raised
  mid-stream still surfaces as `ProviderError`; a reply arrives as more than one
  binary frame, between `audio_start` and `audio_end`, byte-identical to what
  was synthesized; a failure before the first chunk opens no stream; a failure
  mid-reply closes the audio it began and keeps the text; the mute window is
  the remainder; a blank reply is never synthesized; the greeting cache key
  carries the format and a partial greeting is not cached; the page's player
  plays a queue without seams, counts gaps only mid-stream, runs its context at
  the stream rate, keeps queued speech through the autoplay fallback, and
  reopens the gate only after the last sample.
- **Six sabotages, all caught**: dropping the odd-byte carry, returning the full
  length as the mute window, announcing `audio_start` eagerly, caching a partial
  greeting, leaving the format out of the cache key, and not closing audio on a
  mid-stream failure (caught as a hang, the way a browser would wait).
- **Live audio, inspected numerically** (ElevenLabs): the captured PCM of 38 s
  and 43 s replies has RMS ~5.7k, no clipping, and ~3,000 zero crossings per
  second — speech. The same bytes shifted by one byte, the failure
  `whole_samples` guards against, measure RMS 18.2k, 2.1% clipped, 10,600
  crossings per second.
- **`whole_samples` is defensive, not a fix for anything observed**: 290
  ElevenLabs chunks were all exactly 1,024 bytes. It stays because nothing in
  HTTP chunking promises that, and the failure it prevents is total.
- **Listened to, and it failed**: replies up to 32 s sounded clean; a 100-second
  one was clicky and pitch-warped — the cause and the redesign are under Design
  decisions. The worklet player has since been driven end to end in headless
  Chrome (see the refactor entry) but **not yet listened to**.
- **Not verified**: the OpenAI backend live (no key in this environment).

**Fixes**

- A connection lost mid-synthesis degrades the reply to text instead of dropping the socket: both SDKs pass transport errors through as raw `httpx` exceptions, now mapped to `ProviderError` (`httpx` declared as a runtime dependency). The LLM adapters still have this gap.
- The greeting cache is written atomically and an odd-length file is re-synthesised: raw PCM, unlike the old JSON, loaded a truncated file as a valid shorter greeting.
- Sending a first message drops a greeting still waiting on autoplay, rather than starting it for the reply to cut off mid-word.
- The browser's gap counts are coerced to integers before logging; they are client-supplied.
- The mid-stream-failure test reads to a frame the server always sends, so a regression fails instead of hanging the suite; the mute-window test uses a fake clock.

## Chapter 5 — Speculation: answering before the question finishes

When a partial transcript adds no new words, the speaker has probably stopped.
The agent takes that as its cue and starts generating the real reply — not a
discarded prefill, the actual answer — so that when the recognizer finally
commits, the reply already exists. Measured live, this turned time-to-first-token
from **844 ms into 11 ms** on a turn where it fired.

It is a bet, and the design is built around losing it cheaply: the generation is
cancelled the moment the recognizer finds another word, so a wrong guess costs
only what it produced in the meantime. That argument holds *only* because this
agent has no tools. A speculation that could send a message or move money is not
a bet, it is an action, and the chapter that adds tools has to defend that line.

**What changed**

- `stt/agreement.py`: `repeated` — whether the latest partial added nothing to
  the one before it. The closest thing to a turn-completion signal available
  without building a turn detector. Plus `same_words`, which compares
  transcripts normalised, because a commit adds the punctuation its own partials
  were still arguing about.
- `speculation.py`: `Speculation` — one in-flight guess. Holds the text it
  guessed at, the fragments produced so far, and the characters it has cost.
  `answers()` asks whether the turn that arrived is the one it guessed at;
  `stream()` replays what it has and continues; `abandon()` cancels and reports
  the waste.
- `server.py`: the lifecycle. A settled prefix starts a guess; more speech
  cancels it; a commit either claims it or discards it. `Guesses` counts what
  the turn spent — started, discarded, characters wasted — reported on
  `reply_end` rather than assumed.
- `web/index.html`: `⚡ started 782 ms before you finished` when a guess is
  claimed, and `🗑 1 guess discarded · 34 chars wasted` when one is not.

**Design decisions**

- **Guess on the agreed-stable prefix, never on a raw partial.** The recognizer
  rewrites partials; a guess at words the user never said is the one failure
  this must not have. Chapter 4 existed to make this possible.
- **Cancel, do not discard.** Letting a doomed generation finish and then
  throwing it away pays for every token. Cancelling on the next word pays only
  for the lead time — tens of characters — which is the whole cost argument.
- **Claim by comparing what was said, not by trusting the guess.** The model saw
  the question without its question mark, so the comparison is normalised; but a
  commit that says something else discards the guess and the turn regenerates.
- **One guess in flight.** A second would be generating an answer to the same
  words the first is already on.
- **The cost is on screen.** Chapter 4 shipped a warm that fired seven times and
  cached nothing, and the only reason anyone noticed is that the count was
  visible. Discarded guesses and wasted characters get the same treatment.

**Latency impact**

Live, real Scribe and real DeepSeek, on a 7.1 s utterance:

| | without speculation | with |
| --- | --- | --- |
| Time to first token | 844 ms | **11 ms** |
| Guess started before the commit | — | 782 ms |
| Characters wasted | — | 0 |

**But it does not fire on short turns, and that is not a weak gate.** Measured
across repeated runs:

| Utterance | Partials | Gate fired |
| --- | --- | --- |
| 2.9 s | 4 | **0 / 3** |
| 7.1 s | 8 | **2 / 2** |

On the short utterance the recognizer was still delivering words when the commit
arrived — its last partial was `'...people live ther'` and the complete text
first appeared *in the commit itself*. There was no interval in which the
transcript was finished and the turn was not, so there was nothing to speculate
on. The head start exists only when the recognizer catches up before the VAD
timer expires, which on this service means longer turns. Fixing that is not a
better gate; it is less recognizer lag or less endpointing.

**Correction to an earlier estimate.** Chapter 4 predicted speculation was worth
1.0-1.7 s. That double-counted: it added the head start *and* the ~716 ms
network floor, but the floor is paid *inside* the head start. The saving is
`min(head start, time-to-first-token)` — measured at 833 ms on the turn above,
and zero on turns where the gate never fires.

**Deliberately not done**

- No speculative synthesis. Audio still waits for the commit, so this chapter
  improves time-to-first-*token*, not time-to-first-*audio*. Wasted synthesis is
  billed per character.
- No turn-completion probability, so no threshold to tune between speculating
  rarely and speculating often. The gate is one binary signal.
- No side-effect boundary, because there are no side effects yet. That is a
  prerequisite for the tools chapter, not for this one.

**Verification**

- `uv run verify` green: ruff, ruff format, mypy strict, 153 tests.
- Eight speculation tests: a settled prefix starting the reply early, the
  *committed* text being what is recorded rather than the guess, a guess the
  user talks through being discarded and never reaching them, the cost of a
  wrong guess being reported, an un-guessed turn behaving exactly as before,
  one guess in flight, a failing guess leaving the turn to do the work, and a
  commit that says something else discarding the guess.
- **A guess is abandoned when the browser disconnects**, or it goes on
  generating — and being billed for — a reply nobody is waiting for. Cancelling
  the task is sufficient to release the provider's stream: the task is always
  suspended *inside* the generator's own await, so the cancellation is delivered
  there and its cleanup runs. An explicit `aclose()` was written first and
  removed after measurement showed it changed nothing.
- **The disconnect path itself is not covered by a test**, and could not be:
  the test client either tears down the event loop on disconnect — killing the
  task whether or not the code cancelled it, which looks like a pass — or holds
  the handler open so its cleanup never runs at all. Neither can tell the fix
  from its absence. The mechanism is tested directly instead, and the caller is
  covered by reading it.
- **Every one of the four safety properties was sabotage-checked, and the first
  attempt caught none of them.** Adopting any guess without checking what was
  said, and never cancelling on new speech, both left the suite green: the two
  mechanisms cover for each other, so either alone rescued the scenario. Two
  further tests were written to isolate them — a commit that differs from the
  guess with nothing in between to cancel on, and an assertion that the second
  guess was *adopted* rather than the turn regenerating. All four sabotages now
  fail.

  The abandonment test needed three attempts of its own. Asserting only that
  nothing was left generating passed without the cancellation, because awaiting
  the task also ends with nothing generating — that is waiting for the guess to
  finish, which is the opposite of abandoning it. It now asserts that the guess
  was stopped *partway* (40 of 40 characters is the failure) and that abandoning
  returned promptly.

**Fixes**

- `endpoint_ms` is measured from the recognizer's last *word*, not its last
  *message*. A repeated partial was resetting the clock, which made the figure
  identical to the speculation lead on every turn — both were timing the same
  instant, since a repeat is exactly what starts a speculation. Found by reading
  a real session log where the two columns matched to the millisecond, five
  turns in a row.
- The page says a guess started so long "before the turn ended" rather than
  "before you finished". It is measured to the commit, and the commit is not
  when the speaker stopped.
- A guess in flight when the browser disconnects is cancelled, rather than
  generating — and being billed for — a reply nobody is waiting for.
- A spoken `exit` abandons its guess instead of claiming it. `exit` produces no
  reply, so the guess would have run to completion unread and uncounted.
- A warm that was fired but never landed is counted and shown. A missing warm
  line was ambiguous between "nothing was warmed" and "a warm was billed and
  arrived too late to help", and those two want opposite responses. Found by
  reading a session log in which a turn speculated, settled five words, and
  reported no warm at all.
- An unexpected exception inside a guess is logged rather than swallowed. A
  fire-and-forget task's exception belongs to nobody by default, and provider
  failures are already handled — anything else reaching there is a defect.

## Chapter 4 — Acting before the turn ends: agreed-stable text, and prefill

The agent starts using what you are saying before you finish saying it. Every
partial transcript feeds a **LocalAgreement** filter; text the recognizer has
produced unchanged twice in a row is treated as settled, and the reasoning
engine is prefilled on it while the user is still talking.

The headline number is deliberately unflattering, and was measured before the
chapter was written rather than after: **prefill warming saves ~60-90 ms on the
first turn of a conversation and ~10 ms on every turn after.** It was built
anyway, for a reason the "Design decisions" section makes plain — the stable
prefix, not the warming, is the deliverable.

**What changed**

- `stt/agreement.py`: `StablePrefix`, LocalAgreement over successive partials.
  Two details come from the recognizer's real behaviour rather than the
  literature, and both were found by capturing a live trace before writing any
  code:
  - **Comparison is normalised, the original spelling is emitted.** Partials
    revise punctuation and capitalisation as often as words — three of seven
    transitions in a seven-second utterance were `'Prize Problems'` ->
    `'Prize problems'` and `'cryptography.'` -> `'cryptography,'`. Raw
    comparison does not stall permanently, but it loses a round at every
    respelling: about a second each, at this recognizer's one-partial-per-second
    cadence.
  - **The prefix is append-only.** A cache prefix that rewrites its own middle
    is not a prefix. Words keep the spelling they had when they settled, and
    agreement that contradicts settled text is counted rather than applied.
- `llm/base.py`: `Warmth` (prompt tokens, cached tokens) and `warm()` on the
  `LLM` protocol — prefill this prompt, discard the output.
- `llm/openai_compatible.py`: warming for DeepSeek and OpenAI, reading
  `prompt_cache_hit_tokens` or `prompt_tokens_details.cached_tokens` — the same
  idea under two names, neither in the SDK's shared type.
- `llm/anthropic_provider.py`: warming, plus `cache_control` on the system
  prompt in **both** `warm()` and `stream()`. Anthropic caches only what is
  explicitly marked, so warming without a matching breakpoint on the real call
  would be the cost with none of the benefit.
- `server.py`: `Mic` runs agreement over partials and reports, on the commit,
  how many words settled early and **whether the agreed prefix actually held**.
  Warming is fire-and-forget with at most one in flight.
- `config.py`, `server.py`: **a greeting**, synthesised once at startup and
  cached for the life of the process. It is the opening line the roadmap asks
  for, and it moves the synthesis engine's cold start off the user's first real
  question — measured at 3.1 s for a process's first synthesis against 250-290 ms
  for every one after. Delivered in 5-8 ms on every visit; the first real turn
  then synthesises in 318 ms rather than seconds. Set `VOICE_AGENT_GREETING` to
  change it, or to empty to open in silence.
- `web/index.html`: both numbers are on screen —
  `🎙 … · 22 words settled early` and
  `🔥 warmed 1× · 896/1151 tokens already cached (78%) · last one 5.6 s before
  the turn ended`.

**Design decisions**

- **Built despite the measurement, and the measurement is in the entry.** The
  provider's cache is already 80-87% warm from the previous turn's own call, so
  warming adds almost nothing. What it does add is the machinery: agreeing a
  stable prefix and acting on it before the turn is over. Pointing that same
  signal at a *real* generation instead of a discarded one is worth up to
  ~830 ms — measured in Chapter 5, which also corrected the estimate written
  here first: 1.0-1.7 s double-counted the network floor, which is paid inside
  the head start rather than alongside it. This chapter is that change with the
  payoff switched off, which makes the next one a small, measured delta rather
  than a leap.
- **Only agreed text is ever warmed.** Prefilling a hypothesis caches a prompt
  the real call will not match — the spend with none of the benefit — and, once
  the same signal drives real generation, it becomes acting on words the user
  never said. Tested with a partial reading `"send it to Bob"` revised to
  `"Rob"`.
- **Warming is throttled to one cache block of growth (~48 words).** The first
  live run fired **seven** warms for one 22-word turn and the cached-token count
  never moved off 1024: the utterance is far too short to complete another
  64-token block, so six of the seven were billed prefill that cached nothing.
  Long utterances still warm more than once; short ones warm once and keep the
  earliest, longest lead.
- **A failing warm costs only the warm.** It is an optimisation, and it is
  logged at info and swallowed. It must never be able to break a turn. Tested.
- **The greeting is cached, not re-synthesised per visitor.** It never changes,
  so the second visitor onward gets it for no synthesis cost and no wait. The
  roadmap lists this as the cheapest optimisation available in a voice pipeline
  and it is: zero latency *and* zero spend.
- **The greeting joins the conversation history.** Otherwise the agent does not
  know it has already said hello and greets again on the next turn. Tested.
- **A greeting that cannot be synthesised is still said in text.** An agent that
  cannot greet aloud must still be able to converse.
- **Browsers block audio before any user gesture**, and the greeting arrives
  before there has been one. The page now holds the clip and plays it on the
  first click rather than silently dropping it.
- **`prefix_held` is reported on every turn.** Agreement is a bet that the
  recognizer will not change its mind. The bet is cheap to check against the
  committed transcript, and a chapter that acts on predicted text without
  reporting how often the prediction was wrong is not measuring the thing that
  matters.

**Latency impact**

Measured against real DeepSeek before the chapter was written:

| | median TTFT |
| --- | --- |
| Cold prompt, no warming | 770 ms |
| Same prompt, warmed | 708 ms |
| **Difference** | **62 ms**, inside a 527-1228 ms cold-run spread |

And the reason it is so small — TTFT is almost all fixed cost:

| Context | Median TTFT | Prefill cost |
| --- | --- | --- |
| 71 tokens | 716 ms | — |
| 1,128 tokens | 803 ms | +88 ms |
| 3,690 tokens | 1,004 ms | +288 ms |
| 11,027 tokens | 1,105 ms | +390 ms |

There is a **~716 ms floor** — network round trip and queueing — that warming
cannot touch, and prefill at this project's context size is only ~88 ms of it.
Worse, across five consecutive turns of a real conversation with **no warming
at all**, the cache was already 87%, 84%, 80% and 87% warm from turn two
onward. So warming's real marginal contribution is the ~150 uncached tokens,
and block granularity caps that at ~128: **≈10 ms.**

The head start is the part worth keeping. The last two partials of an utterance
are usually identical, so agreement settles the whole turn **0.3-1.0 s before
the recognizer commits** — dead air in which something more useful than a
discarded token could be happening.

**Deliberately not done**

- No speculative generation. The stable prefix starts a throwaway call, not a
  real one, so the head start is left on the table on purpose. Chapter 5 spends
  it, and measures what it was actually worth.
- No semantic turn detection, which is still the largest single term.
- No cancellation machinery, no discarded-token accounting, and no side-effect
  boundary — all of which speculation needs and none of which warming does.
- No A/B harness in the repo: the numbers above came from throwaway scripts, so
  a future regression in warming would not be caught automatically.

**Verification**

- `uv run verify` green: ruff, ruff format, mypy strict, 141 tests.
- Agreement is tested against a **verbatim live trace** — eight real partials
  from a seven-second utterance — not invented input. Covered: the whole
  utterance settling by the last partial, the prefix only ever growing, the
  settled text matching what was committed, nothing settling until said twice,
  a revised word never settling, and normalisation never being behind raw
  comparison and being ahead at exactly the three measured respellings.
- Server tests: only agreed text is warmed, the turn still sends the *committed*
  text rather than the warmed prefix, `prefix_held` is reported and is `False`
  when agreement was wrong, and a failing warm leaves the turn intact.
- **Verified live end to end**: real Scribe partials through real agreement into
  a real DeepSeek warm — 22 words settled early, `prefix_held=True`, one warm
  at 78% cached landing 5.6 s before the turn ended.
- **The Anthropic path is unverified live** (no key available), and this chapter
  changed its `stream()` as well as adding `warm()` — the system prompt is now
  marked as a cache breakpoint, without which warming would prefill a prompt the
  real call could not read back. Two specific risks go with that: Anthropic has
  a minimum cacheable prefix, below which marking one silently does nothing;
  and `max_tokens=1` on a model whose thinking is on by default may be rejected.
  A rejected warm is logged and swallowed, so the failure mode is "warming does
  not work on Anthropic" rather than a broken turn — but it has not been seen
  to work either.

**Fixes**

- A warm still in flight when its turn commits is cancelled, so it can no longer
  be credited to the next turn — which was reporting impossible leads like
  "7.6 s before the turn ended" on a two-second utterance, and made the warm
  line vanish from the turn that actually paid for it.
- Every turn warms again; the growth throttle no longer carries over from the
  previous turn.
- The first synthesis of a process no longer lands on the user's first question.
- Agreement and warming state reset when a recognizer *session* starts, not only
  when a turn commits. A session ending without one — stopping mid-sentence, or
  a reconnect — used to carry its settled words forward, where they wedged
  agreement entirely and were then reported as words that "settled early" on an
  utterance nobody said them in.
- The autoplay fallback no longer revokes the clip it is waiting to replay, so
  a greeting blocked before the first click can actually be heard after it.
- A greeting that fails to synthesise is not retried for every visitor, which
  on an exhausted quota added a doomed round trip to every page load.
- Two tabs opening the same link greet once. Preparation is awaited, so a check
  made before it is stale by the time the greeting is appended.
- The recognizer keep-alive tops up every 10 s instead of five times a second.
  It was streaming continuous real-time audio to a service metered by audio
  duration, and it consumed most of a month's quota on silence.
- The greeting is cached to disk, so restarting the server no longer
  re-synthesises a sentence that never changes.
- Anthropic: a greeted conversation opens with a fixed user turn, since the API requires one first. Unit-tested only; no Anthropic key was available to test against the live API.
- Anthropic: the whole conversation is cached, not only the system prompt, and a warm bills no output (`max_tokens=0`). Unit-tested only; the cache hit on the second turn is not yet measured.

## Chapter 3 — Ears: the agent listens, and turn detection is borrowed

The agent hears. Press **listen**, talk, pause — the transcript appears live,
rewriting itself as more audio arrives, firms up when the pause is long enough,
and becomes an ordinary user turn. The loop is closed: speech in, speech out.

The agent is **bi-capable**, and that is a property of the design rather than a
feature: speech becomes text at the edge, so by the time `run_turn` is called
nothing downstream knows whether the user typed or spoke. Chapter 1's
conversation, Chapter 2's synthesis, `exit`, the failure rollback — all
unchanged, all working identically for both.

The interesting decision is **who decides the turn ended**. Scribe's realtime
endpoint has VAD endpointing built in: connect with `commit_strategy=vad` and a
silence threshold and it emits a committed transcript after a pause. That is
this chapter's turn detection in its entirety — zero VAD code — and it is the
right naive step. Its costs only became visible once it ran.

**What changed**

- `stt/base.py`: `Transcript` (text plus `is_final`) and the `STT` protocol —
  `provider`, `model`, `sample_rate`, and `stream(audio) -> AsyncIterator[Transcript]`.
  Audio arrives as an async iterator rather than through a `send()` method, so
  a listening session has the same shape as every other stream here and ending
  the iterator is what ends the session.
- `stt/elevenlabs_stt.py`: Scribe realtime over a **raw WebSocket**. The SDK
  ships this endpoint's *types* (`PartialTranscriptPayload`,
  `CommittedTranscriptPayload`) but binds no client method to it, so there is
  nothing to call — verified by inspecting the installed package, not assumed.
- `server.py`, restructured around the fact that transcripts now arrive
  concurrently with the receive loop:
  - `Channel` serializes writes to the socket. Two producers write to it now,
    and while interleaved JSON would be harmless, the `audio` frame and its
    binary frame must arrive adjacent — hence `send_audio` holding the lock
    across both.
  - `Mic` owns one listening session: a queue in, transcripts out. A queue
    rather than handing the recognizer the socket, because the recognizer
    consumes audio at its own pace while the receive loop must stay free.
    It also expires the session, keeps it alive, and reconnects it (below).
  - `run_turn` was split out of the old `handle_message`, so a spoken turn and
    a typed one enter through the same door.
  - A turn lock, which is not decoration: a committed transcript arrives on the
    microphone task, so without it a fast second utterance could start a turn
    while the first is still streaming.
  - The receive loop now handles both text and binary frames.
- `web/index.html`: an **AudioWorklet** built from a Blob URL, so the page stays
  one file with no build step. It converts float samples to PCM16 and posts
  ~100 ms chunks. The `AudioContext` is opened at the recognizer's own sample
  rate (advertised in the `ready` frame) so nothing resamples anywhere. Plus a
  listen toggle, live volatile/committed rendering, and the half-duplex gate.
- `prompts/system_prompt.md`: a language rule. The agent answers in the
  language of what the user *just said*, not of what it said last, and treats a
  greeting as no evidence at all — "Hallo"/"Hi"/"Ciao" are shared between
  languages and a transcribed one is a recognizer's spelling guess.
- `cli.py`: `--stt {elevenlabs,none}` and `--vad-silence`.

**Design decisions**

- **Endpointing delegated to the STT vendor.** Simplest possible thing that
  works, and it works well. The costs, now that it has run: a second backend
  must supply its own endpointing, the semantic-turn-detection chapter has to
  take this back out rather than swap it, and — unexpectedly — *the ability to
  measure endpointing goes with it* (see Latency impact).
- **The pause that ends a turn is 1.5 s**, the service's own default, tunable
  via `--vad-silence`. 0.7 s was tried first on the theory that 1.5 s is an
  eternity in conversation. It is, and it is still better: at 0.7 s the agent
  committed `"Or rather..."` and `"Not Ethereum, but rather..."` as finished
  turns and answered the fragments. Being interrupted mid-thought reads as the
  agent not listening; waiting a beat merely reads as slow. No fixed value wins
  this trade, which is the whole argument for semantic turn detection.
- **A toggle, not always-on, and listening expires.** Continuous streaming
  bills a metered API for silence; on a free plan that is how a quota
  disappears overnight. Permission is requested on page load and the tracks
  immediately stopped, so the grant is remembered and the recording indicator
  does not sit lit all session. A session ends after 30 s with no speech, or
  5 minutes regardless. "No speech" means *no partial transcripts* — the
  microphone streams silence continuously, so frames never stop arriving.
- **Expiry is suspended by named holds, and the hard cap is not pausable.** Two
  things suspend it — the turn, and the browser playing the reply — and they
  overlap, so a boolean lets one clear the other. A counter fixes that and then
  sticks above zero forever if a client miscounts. A set is idempotent. Holds
  also expire after 60 s, and the cap is checked before them: a cap a stuck
  hold can defeat is not a cap.
- **The server derives the playback window from the clip it sent**, rather than
  trusting the browser to report when playback ends. A clip carries its own
  duration (constant-bitrate MP3, so duration follows from size) and the idle
  clock starts after it. The browser's playback messages remain a second signal
  for pause and mute, but nothing load-bearing depends on them.
- **The recognizer's session is kept alive with silence.** Measured against the
  real service: Scribe closes a realtime session after ~15 s with no audio, and
  closes it *normally* — code 1000, so the stream just ends and nothing raises.
  The browser stops sending while a reply plays, so any answer longer than ~15 s
  silently killed the ears. `Mic` now fills any gap, and a stream that ends
  while we still hold the microphone is a reconnect, not an ending.
- **Half-duplex: the browser stops sending audio while the agent speaks.** Free,
  total, and impossible to get subtly wrong. Browser echo cancellation would
  mostly work, but with no barge-in logic yet there is nothing useful to do
  with a partial transcript of the agent's own voice. Removing this restriction
  *is* the barge-in chapter.
- **Volatile transcripts never reach the reasoning engine.** Acting on a
  hypothesis means acting on words the user never said. Tested explicitly with
  a partial reading "delete every".
- **Capture at the recognizer's rate, don't resample.** The `ready` frame
  advertises `sample_rate` and the browser opens its `AudioContext` there, so a
  mismatch is a configuration bug rather than a silent quality loss.
- **Ears are not load-bearing.** `--stt none` is a first-class mode, and a
  recognizer that fails mid-session reports the failure and leaves typing
  working. Tested.
- **Failures must never be silent.** Three separate reports in this chapter were
  the same shape: the agent stopped hearing and said nothing, so the page still
  read "listening" while the user talked to nobody. Every path that stops or
  degrades listening now announces itself, and expiry distinguishes "no speech
  for 30s" from "the browser sent no audio" — only one of those is the user's
  doing.

**Latency impact**

Full live loop, nothing faked — a 1.65 s spoken question through Scribe,
DeepSeek and ElevenLabs, at the original 0.7 s threshold:

| Stage | Measured |
| --- | --- |
| End of speech → committed transcript | **1,322 ms** |
| LLM first token | 782 ms |
| Full reply text (118 chars) | 1,111 ms |
| Synthesis | 512 ms |
| **End of speech → first audio** | **~2.9 s** |

Raising the threshold to 1.5 s adds ~0.8 s to the largest term, putting first
audio near **3.7 s**. Endpointing is now well over half the round trip.

**The instrumentation was wrong and the live run caught it.** The server
reported `endpoint_ms=509`; the true figure, timed externally from the moment
the audio actually stopped, was **1,322 ms** — 2.6× larger. The server measures
from the last *partial transcript*, because without its own VAD it cannot see
when the user stopped talking; everything the recognizer spends lagging behind
the audio is invisible to it. The metric is now labelled for what it actually
measures, and the gap is documented at the point of measurement.

That is the real lesson of delegating endpointing: **handing the decision to
the vendor also hands over the ability to measure it.** Endpointing is the
largest term in a naive cascade, and here it is — but the project cannot
currently see that number from the inside.

**Deliberately not done**

- No barge-in. The agent cannot be interrupted; the mic is muted while it talks.
- No semantic turn detection — a single silence threshold decides every turn,
  which will cut off anyone who pauses mid-sentence.
- No streaming to the LLM: the committed transcript is sent whole, so nothing
  overlaps recognition with reasoning. No prefill warming on stable segments.
- No second STT backend, so the `STT` protocol is still a guess.
- No audio persistence, and therefore still no consent, retention or
  biometric-data questions — those arrive with the first recording.

**Verification**

- `uv run verify` green: ruff, ruff format, mypy strict, 109 tests.
- Listening tests with only the recognizer faked: volatile-then-committed
  sequencing, a committed transcript starting a turn by itself and arriving at
  the reasoning engine as an ordinary user message, volatile text *never*
  reaching it, a blank commit reported not at all, a spoken `exit` ending the
  conversation, audio before listening starts being dropped rather than
  buffered, a failing recognizer leaving typing working, and a deaf agent
  saying so.
- Expiry rules are tested against `Mic` directly rather than through a socket,
  with a deadline on every wait. Through the socket a broken expiry means "the
  frame never arrives", which hangs the suite instead of failing a test — the
  first version of these took 62 seconds and *passed*, rescued by a 60 s safety
  net.
- **Every absence-based assertion here was verified to fail with the code it
  protects removed.** Three of them initially passed without it. A negative
  assertion that has never been seen to fail is not evidence of anything.
- **Verified live against the real Scribe realtime API**, end to end through the
  actual server with a real WebSocket client playing the browser's part. A full
  turn: volatile transcripts revised themselves in flight
  (`'What is the tallest-'` → `'What is the tallest building in Riga?'`), the
  commit fired on the pause, DeepSeek answered, ElevenLabs spoke it. Separately,
  a session that goes silent for 40 seconds and then speaks again is still
  heard — the same script against the unfixed service shows the close at 18.6 s.
- Scribe's realtime protocol was confirmed by connecting before any code was
  written — session config, payload shapes, and the send shape all came from a
  live socket and the installed SDK's types rather than from documentation.

**Fixes**


- A recognizer session that ends on its own is reconnected, not silently ignored.
- A leaked playback hold no longer mutes the microphone permanently and silently.
- A reply longer than the one-minute hold ceiling no longer stops listening mid-reply: a hold is not treated as stuck while the server knows its reply is still playing (156 s was heard).
- Blank committed transcripts are not reported at all.
- An orderly close of the recognizer socket is no longer reported as a failure.
- `stop()` no longer deadlocks when called from the task it waits on.
- An unexpected error in the recognizer loop is reported as `listen_error` rather than silently ending listening.
- The chat log scrolls again (`min-height: 0` on a flex child), and the
  telemetry line at the end of each turn is now scrolled into view.
- The browser client gained static tests and a `node --check` pass, after an
  edit deleted the entire microphone block and every Python test still passed.

## Chapter 2 — Giving it a voice: the agent speaks, batched

The agent talks. It still cannot hear — there is no microphone, no VAD, no
speech recognition — but every reply is now synthesized and played in the
browser. Half the cascade exists: text in, reasoning out, speech out.

Synthesis is **batched on purpose**: the whole reply is generated, then
synthesized as one clip, then played. This contradicts §7's "stream
everything", and it is the right place to start anyway, because the cost is now
*measurable* rather than argued about. Fully live — real DeepSeek, real
ElevenLabs — first audio lands **1,893 ms** after the user presses send, of
which **658 ms is synthesis that could have overlapped generation entirely**.
The streaming-synthesis chapter has a real baseline to beat instead of an
assertion.

The chapter also produced its first unwelcome number. Real DeepSeek TTFT was
**646 ms on the first turn and 1,229 ms on the second** — against a §7 budget of
300 ms for LLM first token. Nothing was optimized here and nothing should be
yet; measurement comes before optimization, and this is the measurement saying
the budget is not currently met by a wide margin.

**What changed**

- `tts/base.py`: `AudioClip` (bytes plus media type) and the `TTS` protocol —
  `provider`, `voice`, and `async synthesize(text) -> AudioClip`. One method,
  batched, and the signature says so.
- `tts/elevenlabs_tts.py`: ElevenLabs, the default. Stock voice "Rachel" so the
  project runs with nothing but a key, `eleven_flash_v2_5` for latency, MP3 at
  44.1 kHz. The SDK's `convert` yields chunks even for a batched request
  (chunked HTTP); joining them here is precisely what makes this chapter
  batched.
- `tts/openai_tts.py`: the second backend, which is the point of it — voices
  are names not ids, format is a separate parameter, and the response is a
  binary body rather than a chunk stream. An interface with one implementation
  is a guess.
- `tts/registry.py`: `create_tts(name, voice)`, with `"none"` returning `None`
  rather than a null backend. Silence is a supported mode, not a degraded one:
  the text path must never depend on the voice path working.
- `server.py`: after `reply_end`, the reply is synthesized and sent as **two
  frames** — an `audio` JSON frame carrying media type, byte count and
  `synthesis_ms`, immediately followed by one binary frame of encoded audio.
  A synthesis failure emits `audio_error` and keeps the reply.
- `web/index.html`: `ws.binaryType = "arraybuffer"`; the binary frame becomes a
  `Blob` and plays. A mute toggle, a click-to-play fallback if the browser
  refuses autoplay, and each reply is annotated with its clip size and
  synthesis time so the batched wait is visible in the UI, not just in a log.
- `cli.py`: `--tts {elevenlabs,openai,none}`, `--voice`, `--list-voices`.
- **Every reply is annotated with what each stage spent**, which turned out to
  be the most useful thing in the chapter:

  ```
  💭 thought for 853 ms · 153 chars in 422 ms
  🔊 31.4 s of speech · 491 kB · synthesized in 524 ms · audio at 1.8 s
  ```

  The reasoning figure is split at the first token on purpose: `ttft_ms` is
  dead air the user experiences and is what §7 budgets, while `generation_ms`
  is throughput that streaming already hides behind text appearing on screen.
  A single "reply took N ms" would blur the one that matters into the one that
  does not. `total_ms` is send-to-first-audio — the number the whole project is
  judged on. The clip's own length is shown too: a 31-second answer is a
  product problem no latency work can fix, and it was invisible.
- **Renamed `providers/` to `llm/`** (refactor of Chapter 1). The name was
  fine while there was one kind of provider; with a second kind it had to say
  which one it meant. No behavior changed.

**Design decisions**

- **Audio rides the same WebSocket as the control messages**, as a binary frame
  announced by the JSON frame before it. Rejected base64-in-JSON (33% larger,
  and a shape that gets replaced) and a separate `/audio/<turn>` endpoint
  (needs server-side storage and a cleanup policy, and is a dead end for
  realtime). One socket carrying both control and audio is exactly what
  microphone frames will need travelling the other way.
- **MP3, not PCM.** ~10× smaller and the browser decodes it with no Web Audio
  code, which is right for playing one finished clip. PCM is what streaming
  playback, AEC and barge-in will need, and those chapters will change this —
  deliberately, and with a reason recorded when they do.
- **`synthesize() -> AudioClip`, not an async iterator.** A streaming signature
  wrapping a batched call would be a fake stream, and would hide exactly the
  latency this chapter exists to expose. The widening is known debt, named here.
- **Audio is sent *after* `reply_end`, not instead of it.** The text lands on
  screen while synthesis runs, so the user sees the delay the batched approach
  costs. Hiding it behind a single combined frame would make the next chapter's
  improvement invisible.
- **A synthesis failure degrades to text; an LLM failure rolls back the turn.**
  These are deliberately asymmetric. A reply with no voice is still a good
  reply; a user question with no reply is a corrupted context (Chapter 1).
- **`synthesis_ms` is measured and reported from this chapter on.** The roadmap
  rule that no chapter may claim a speedup without a before and after only
  works if the "before" is recorded when it is cheap to record.
- **`--tts none` is a first-class mode**, not a fallback. Someone without a
  synthesis key must still be able to run and develop the agent.

**Latency impact**

Measured end to end with nothing faked — real DeepSeek, real ElevenLabs, a
112-character reply:

| Stage | Measured | §7 budget |
| --- | --- | --- |
| LLM first token | 847 ms | 300 ms |
| Full reply text | 1,231 ms | — |
| Synthesis (batched) | 658 ms | 150 ms |
| **First audio** | **1,893 ms** | **800 ms** |

The agent is **2.4× over budget**, and the two stages that exist are both over
individually. Three observations worth carrying forward:

- **The LLM stage is the largest single term** (847 ms of 1,893 ms) and it grows
  with the conversation: an earlier run measured 646 ms on turn 1 and 1,229 ms
  on turn 2. That is Chapter 1's unbounded, uncached context doing exactly what
  its own entry predicted it would.
- **Synthesis is the second largest** (658 ms, ~35%) and is *entirely* serial
  cost that streaming would overlap. An earlier measurement using a stubbed
  151 ms synthesis badly understated this; a stub is not a measurement, and
  substituting one for the real thing produced the wrong conclusion about
  where the latency was.
- Nothing here was optimized and nothing should be yet. This is the "before".

**Deliberately not done**

- No listening. No microphone, VAD, endpointing, STT, or barge-in.
- No streaming synthesis, no clause-boundary chunking, no cached greeting audio.
- No prompt caching, no context management — the turn-2 regression above is
  left standing on purpose so a later chapter can be measured against it.
- No voice cloning, voice settings (stability, similarity), or SSML.
- No audio persistence: clips are synthesized per turn and never stored, which
  also means no consent, retention, or biometric-data questions arise yet.
  They will the moment anything is recorded — see `docs/ROADMAP.md` §2H.

**Verification**

- `uv run verify` green: ruff, ruff format, mypy (strict, 26 files), 43 tests.
- New tests, provider faked: the reply arriving as `reply_start → delta* →
  reply_end → audio → binary`, the announced byte count matching the frame,
  **the agent speaking exactly the text it recorded**, a failed synthesis
  degrading to text while keeping the reply, `--tts none` working end to end,
  `exit` never being synthesized, and both backends' defaults and overrides.
- **Exercised live against real DeepSeek**, with a stub returning a genuine
  128,058-byte WAV produced by macOS `say`: two full turns, first-token and
  first-audio timings as tabled above, and the binary frame verified intact by
  SHA-256 on both turns. The audio was real, decodable audio — not a
  placeholder — so the binary path is proven to carry playable bytes unaltered.
- **Chapter 1's LLM path is now confirmed against a real provider** (DeepSeek),
  closing the gap that entry recorded. The Chapter 0 voice system prompt is
  visibly working: the model answered "Riga has just over six hundred thousand
  people living there" — spoken-form numbers, no markdown, one sentence.
- **Verified live against the real ElevenLabs API.** A full turn ran end to end
  with nothing faked: DeepSeek wrote "Riga is famous for its stunning Art
  Nouveau architecture and its vibrant old town, a UNESCO World Heritage site",
  ElevenLabs synthesized it, and the browser received a 109,549-byte binary
  frame. The bytes were written to disk and confirmed to be genuine, decodable
  audio — `ID3v2.4 / MPEG layer III, 128 kbps, 44.1 kHz mono, 6.82 s` — not a
  plausible-looking blob.
- **The OpenAI TTS adapter remains unverified live** (no `OPENAI_API_KEY`).
  Its wire usage was written against the installed SDK's inspected signatures
  and is unit-tested at the interface.

**Fixes**

- The default voice is one verified usable on a free plan. Which voices count
  as "stock" is neither stable nor inferable from documentation — Rachel and
  Aria are both blocked on a free account despite being the most widely
  recommended ids, and `--list-voices` now answers the question properly.
- ElevenLabs errors are translated into one actionable line instead of the
  SDK's stringified HTTP response, headers and all.

## Chapter 1 — A talking loop with no voice: text in, reasoning out

The agent can hold a conversation. There is no audio anywhere in it — no
microphone, no speech recognition, no synthesis. What exists is the shape
everything later plugs into: a browser connected to a server over a duplex
socket, a conversation that accumulates in memory, and a reasoning engine that
streams its reply back a fragment at a time. Swap the browser's keyboard for a
microphone and its text bubbles for a speaker, and the surrounding machinery
does not change. That is the whole point of building this chapter speechless.

Three decisions here are deliberately *not* naive, because each is a shape
that later chapters must inherit rather than replace. The transport is a
**WebSocket**, though a `POST /chat` would have been shorter: audio frames need
duplex, and a request/response endpoint would be torn out rather than extended
in the capture chapter. The provider interface is **streaming**, though
returning a finished string would have been simpler: §7 commits to streaming
every stage boundary, and a `-> str` signature would have to be widened to an
async iterator by every adapter the moment text-to-speech arrives. And the
**system prompt is the voice one** written in Chapter 0 — the agent already
answers in one or two spoken-sounding sentences with no markdown, because
that behavior should be under test long before anything speaks it aloud.

Everything else is as naive as it looks: one process, one dictionary, no
persistence, no reconnect, no rate limiting, no auth beyond an unguessable key.

**What changed**

- `conversation.py`: `Message` (role + content) and `Conversation` (an ordered
  list of them, plus an `ended` flag). This is "the context": the entire list
  is resent to the model on every single call, because the APIs are stateless.
  No trimming, no summarization, no cap — the context grows without limit and
  will eventually need chapter 6-style management. It is meant to be visible.
- `sessions.py`: `SessionStore`, a dict from key to `Conversation`. Keys are
  `secrets.token_urlsafe(16)` — 128 bits — because the key is the *only* thing
  protecting a conversation, so it must be unguessable, not merely unique.
- `server.py`: `GET /` mints a conversation and 303-redirects to `/c/<key>`;
  `GET /c/<key>` serves the chat page or 404s; `WS /ws/<key>` runs the loop.
  On connect the server replays the conversation's history, which is what makes
  reloading the link resume rather than restart. Typing `exit` (or `quit`,
  `bye`, `goodbye`) ends the conversation permanently and is never sent to the
  model.
- `providers/base.py`: the `LLM` protocol — `provider`, `model`, and
  `stream(system, messages) -> AsyncIterator[str]`. One method. Vendor SDK
  types never cross this boundary; adapters are the only modules that import a
  vendor SDK.
- `providers/openai_compatible.py`: OpenAI and DeepSeek through a single
  adapter — DeepSeek implements the OpenAI chat-completions wire format, so
  they differ only in base URL, key, and default model.
- `providers/anthropic_provider.py`: Anthropic, which differs in two ways the
  adapter absorbs — the system prompt is a top-level parameter rather than the
  first message, and streaming is an async context manager over a typed event
  stream.
- `providers/registry.py`: `create_llm(name, model)`. Default provider is
  **deepseek**; an unknown name lists the ones that exist.
- `config.py`: environment-driven settings, plus `load_system_prompt()`, which
  strips the developer-facing preamble above the first horizontal rule in
  `prompts/system_prompt.md` — that note explains the file, and is not an
  instruction to the agent.
- `cli.py`: `uv run voice-agent`, with `--provider`, `--model`, `--host`,
  `--port`.
- `web/index.html`: the chat page. One file, no build step, no framework, no
  dependencies — plain WebSocket plus DOM. Fragments append into the open
  assistant bubble as they arrive, so streaming is visible rather than merely
  implemented.

**Design decisions**

- **DeepSeek as the default provider** (user's choice) with OpenAI and
  Anthropic as peers. Because DeepSeek is OpenAI-wire-compatible, "provider
  agnostic" is proven by the *Anthropic* adapter, not by having three of them —
  Anthropic is the one whose request shape genuinely differs.
- **`asyncio` generators as the interface, not callbacks or queues.** An async
  iterator is the narrowest thing that expresses "fragments, in order, as they
  arrive", and it composes: the TTS chapter consumes this iterator directly
  rather than subscribing to it.
- **A failed turn rolls back the user message.** Rejected the alternative of
  keeping it: a dangling user turn with no answer would be resent as context on
  the next call, quietly corrupting every later exchange with a question the
  model already appeared to ignore. Failing closed keeps the context honest.
- **`max_tokens` is 1024, deliberately low.** This agent's replies are meant to
  be spoken. A large ceiling would only buy the chance to generate a long
  answer nobody wants to listen to.
- **Anthropic runs at `effort: "low"`.** Thinking is on by default on that
  model family, and reasoning before the first token is precisely what a spoken
  conversation cannot afford. Rejected disabling thinking outright: it is
  documented to make the model narrate tool calls in visible text and leak
  reasoning tags into the reply.
- **`python-dotenv`, loaded only in `cli.py` — this supersedes Chapter 0's
  decision** that nothing would load `.env` automatically. The reason the
  original decision existed (loading secrets should be a visible act) is
  preserved by confining the load to the entry point: importing the library
  still has no side effects, and `uv run --env-file .env` still works. What
  changed is that the friction was being paid on every single run for a
  property that only matters at the boundary.
- **The browser client has no framework and no build step.** A dependency-free
  HTML file keeps the chapter's one idea in view; the moment it needs a bundler
  it stops being possible to read the whole client in one sitting.
- **`fastapi` + `uvicorn` over bare `starlette` or the `websockets` library.**
  FastAPI is a thin layer over Starlette that supplies routing, the WebSocket
  primitive, and — importantly for §6 — a test client that drives real sockets
  in-process. Uvicorn is the async server the audio path will need anyway.

**Latency impact**

- **Not measured.** There is no instrumentation yet, and no stage boundaries to
  measure between. What this chapter buys against the §7 budget is structural,
  not numeric: because the reply streams, the LLM's *first token* is available
  to the next stage rather than its last, which is the precondition for the
  ≤ 300 ms first-token target ever being meetable. Measurement is its own
  chapter, and it should arrive before any optimization claims do.

**Deliberately not done**

- No audio of any kind. No VAD, STT, TTS, barge-in, or turn detection.
- No persistence — a server restart loses every conversation. No reconnect or
  message replay if the socket drops mid-reply; the browser just reports
  disconnected.
- No context management: history grows unbounded and will eventually exceed the
  window. No token counting, no cost tracking, no usage reporting.
- No tools, no memory, no retrieval, no multi-user auth beyond the unguessable
  key, no rate limiting, no session expiry (conversations leak until restart).
- No observability. No cancellation: a reply cannot be interrupted mid-stream,
  which is exactly the gap the barge-in chapter exists to close.

**Verification**

- `uv run verify` green: ruff, ruff format, mypy (strict, 20 files, zero
  errors), and 32 passing tests.
- Tests cover, with only the provider faked: link minting and per-visit
  uniqueness, the page 404ing on an unknown key, a socket being refused with
  code 4404 on an unknown key, a full streaming turn arriving as
  `reply_start` → `delta`* → `reply_end` and being recorded, **the whole
  conversation being resent as context on every call** (asserted as 1, 3, 5
  messages across three turns), reconnecting to the link resuming history,
  `exit` ending a conversation permanently without ever reaching the model, a
  failed turn rolling back the user message, blank and malformed input being
  ignored, and both providers' message-mapping shapes.
- Exercised for real, not just through the test client: a live `uvicorn`
  process was driven over an actual `websockets` connection — `GET /` returned
  303 to a minted key, the chat page served 5,162 bytes containing the client
  JS, an unknown key returned 404, two full turns streamed back fragment by
  fragment, `exit` closed the conversation, and reconnecting reported
  `ended=True` with four messages of history.
- **Not verified against a real provider at the time of writing** — no API keys
  were present. *Confirmed in Chapter 2 against real DeepSeek:* streaming,
  context accumulation, and the system prompt all behave as designed. The
  OpenAI and Anthropic LLM adapters remain unverified live.

## Chapter 0 — The empty page: method, contract, and a quality gate

There is no voice agent here yet, and that is the point. This chapter
establishes the three things that every later chapter depends on — **the
method** (build one increment at a time, never ahead), **the contract**
(`AGENTS.md`: how agents and humans work in this repo), and **the gate**
(`uv run verify`: the single command that decides whether a chapter is done).

Starting from an empty package rather than a scaffold is deliberate. A
generated framework skeleton would pre-commit us to an architecture — a web
framework, an audio library, a provider SDK — before a single chapter has
justified it. Every dependency in this project should arrive attached to the
chapter that needed it, with a CHANGELOG entry explaining what it does and
which alternative was rejected. So Chapter 0 ships zero runtime dependencies.

The target shape is written down (a cascaded streaming pipeline: VAD → STT →
turn detection → LLM → TTS, over a WebSocket transport, with barge-in), and
the latency budget it has to hit is written down with it. Neither is
implemented. Both exist so that Chapter 1 has something to be measured against.

**What changed**

- `pyproject.toml`: uv-managed project, `src/` layout, Python ≥ 3.12, no
  runtime dependencies. Dev tooling only: ruff, mypy (strict), pytest,
  pytest-asyncio, pytest-cov. Pytest markers registered up front for the three
  test categories this domain needs — `audio` (needs real hardware), `network`
  (calls a real provider), `latency` (measures wall-clock time) — so they can
  be excluded from the default run from the very first test that needs one.
- `src/voice_agent/verify.py`: the `uv run verify` gate. Runs ruff check,
  ruff format --check, mypy, and pytest in that order, stopping at the first
  failure. Scoped explicitly to `src` and `tests` rather than `.`, because the
  agent will later write recordings, session logs, and traces into the working
  directory and those artifacts must not be able to fail the project's own gate.
- `AGENTS.md`: the working contract. Covers the chapter method, the mandate to
  ask questions aggressively during planning and design, the automation
  boundary (safe commands run without asking; outward-facing, destructive, or
  expensive ones stop and confirm), the CHANGELOG/README obligation, evidence
  standards for a domain where passing unit tests prove very little, the
  latency budget, code style, and security. `CLAUDE.md` is a symlink to it so
  there is one contract rather than two that drift.
- `prompts/system_prompt.md`: the *voice agent's own* runtime system prompt —
  speak briefly, never emit markdown, expect a lossy transcript, yield
  instantly on interruption, confirm consequential actions. Written now, before
  anything can load it, so that the behavior we are building toward is
  specified rather than improvised in the chapter that first calls an LLM.
- `README.md`: high-level summary, status, setup, layout, roadmap.
- `.env.example`: placeholders for the provider categories the roadmap
  anticipates. Nothing loads `.env` automatically — secrets are loaded by an
  explicit `uv run --env-file .env`, so that reading them is always a visible act.
- `.claude/settings.json`: encodes §4 of `AGENTS.md` mechanically — the safe
  read/test/lint/run commands are pre-approved so an agent never stalls on a
  permission prompt for `uv run pytest`.
- `.github/workflows/ci.yml`: CI runs exactly `uv run verify`, so that the
  local gate and the remote gate can never disagree.
- `tests/test_skeleton.py`: asserts the package imports, that its version
  matches `pyproject.toml`, and that the four documents the method depends on
  exist. Small, but it makes `uv run verify` a real passing gate from commit one
  rather than something switched on later.

**Design decisions**

- **uv over Poetry/pip-tools/PDM.** Single fast tool for environments, locking,
  running, and building; `uv run` makes the exact command in `AGENTS.md`, in CI,
  and on the developer's machine identical. Consistent with the existing
  `AgentHarness` project.
- **`src/` layout over a flat package.** Tests import the installed package,
  not a directory that happens to be on `sys.path`, so packaging mistakes fail
  in CI instead of in the first person to `pip install` it.
- **mypy `strict` from the first line.** Retrofitting strict typing onto a
  streaming async pipeline is far more expensive than starting with it, and the
  audio path is exactly where type confusion (bytes vs. frames vs. samples,
  sample-rate mismatches) causes bugs that pass every test and sound wrong.
- **Zero runtime dependencies in Chapter 0.** Rejected the alternative of
  pre-installing the anticipated stack (a web framework, an audio library, the
  provider SDKs): it would let later chapters skip the question of whether the
  dependency is actually needed, which is the exact question this method exists
  to force.
- **Python ≥ 3.12**, not 3.13+: the audio, DSP, and ML wheels this project will
  eventually pull in are still most reliably available on 3.12, and nothing in
  the roadmap needs anything newer.
- **`asyncio` named as the concurrency model up front** (with ruff's `ASYNC`
  rules enabled) even though nothing is async yet. It is an architectural
  commitment, not a library choice: a streaming pipeline with barge-in cannot
  be retrofitted onto blocking code, and the linter should be enforcing it from
  the first coroutine.
- **The voice system prompt is a file, not a string constant.** It will change
  far more often than the code that loads it, and it should be reviewable and
  diffable on its own.

**Latency impact**

- None. There is no pipeline yet. The budget this project commits to — ≤ 800 ms
  p50 from end of user speech to first audio out, broken down per stage — is
  recorded in `AGENTS.md` §7 as the hypothesis Chapter 1 onward will be measured
  against.

**Deliberately not done**

- No audio capture, no VAD, no STT, no LLM call, no TTS, no transport, no
  server, no browser client, no CLI entry point. All of these are chapters.
- No provider interfaces or abstract base classes. Defining `STT`/`LLM`/`TTS`
  protocols before a single concrete implementation exists would be designing
  an interface from imagination; each one gets defined by the chapter that
  first needs it, and generalized by the chapter that adds a second backend.
- No Docker, no deployment, no observability stack, no eval harness. Each is
  its own chapter, once there is something to containerize, trace, or evaluate.

**Verification**

- `uv sync` and `uv run verify` run clean: ruff check, ruff format --check,
  mypy (strict, zero errors), and pytest all pass.
