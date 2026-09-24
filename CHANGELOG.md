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
