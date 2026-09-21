# voice-agent

A conversational AI voice agent, built from an empty page — one chapter at a time.

## The idea

> Latency is the product. The orchestration is the asset.

In text chat, a two-second reply is fine. In speech, a human expects a gap of
200–500 ms; past ~800 ms the agent feels broken no matter how smart it is. And
the parts that make it smart — the STT, LLM and TTS vendors — are the parts
that get swapped every few months. What accumulates is the **pipeline**: the
streaming, the turn-taking, the interruption handling, the buffering, the
measurement. That is what gets built here.

The shape we are evolving toward:

```
mic / phone ──▶ transport ──▶ VAD ──▶ STT ──▶ turn detection ──▶ LLM ──▶ TTS ──▶ transport ──▶ speaker
                    ▲                                                              │
                    └────────────────── barge-in / interruption ◀──────────────────┘
```

Cascaded and owned end to end, with vendors behind narrow, swappable
interfaces. First transport: a WebSocket server with a minimal browser client.

## Where this stands today

Built in **chapters**, each adding one deliberate piece of functionality on top
of a working, fully-tested previous chapter. See [CHANGELOG.md](CHANGELOG.md)
for the full log and the reasoning behind each step.

- **Chapter 0 — the empty page.** No voice agent yet, on purpose. This chapter
  establishes the method (one increment at a time, never ahead), the working
  contract for agents and humans ([AGENTS.md](AGENTS.md)), the target behavior
  of the agent we are building ([prompts/system_prompt.md](prompts/system_prompt.md)),
  the latency budget it has to hit, and the single quality gate every later
  chapter must keep green (`uv run verify`). Zero runtime dependencies: every
  dependency should arrive attached to the chapter that needed it.
- **Chapter 1 — a talking loop with no voice.** Text in, reasoning out. A
  browser chat page talks to the server over a WebSocket; loading `/` mints a
  conversation and redirects to its own link, and reloading that link resumes
  it. The conversation accumulates in memory and the whole of it is resent as
  context on every call. The reply streams back token by token from a
  provider-agnostic `LLM` interface with three backends — **DeepSeek**
  (default), OpenAI, and Anthropic. Typing `exit` ends the conversation. No
  audio anywhere yet: this is the shape the microphone will plug into.
- **Chapter 2 — giving it a voice.** The agent speaks. Every reply is
  synthesized and played in the browser, sent as a binary frame on the same
  WebSocket. Provider-agnostic with two backends — **ElevenLabs** (default) and
  OpenAI — or `--tts none` to stay silent. Synthesis is batched on purpose: the
  whole reply, then one clip. That costs latency, and every reply is annotated
  with what each stage spent — `💭 thought for 853 ms · 153 chars in 422 ms` /
  `🔊 155 kB · synthesized in 524 ms · audio at 1.8 s` — so the next chapter has
  a number to beat rather than an assertion. Still no ears.
- **Chapter 3 — ears.** The agent listens. Press **listen** and talk: the
  transcript appears live and rewrites itself as more audio arrives, firms up
  after a 1.5 s pause (tunable), and becomes an ordinary user turn. Microphone audio goes
  up the same WebSocket as PCM16 frames from an AudioWorklet. Turn detection is
  borrowed from ElevenLabs Scribe's own VAD — zero VAD code, and a cost
  recorded in the CHANGELOG. The agent is **bi-capable**: typing and speaking
  enter through the same door, and nothing downstream knows which was used.
  Half-duplex on purpose — the mic muted while the agent spoke, until Chapter 8.
- **Chapter 4 — acting before the turn ends.** Partial transcripts feed a
  LocalAgreement filter; text the recognizer has said twice is treated as
  settled, and the reasoning engine is prefilled on it while you are still
  talking. Measured honestly: warming saves ~60-90 ms on a conversation's first
  turn and ~10 ms after, because the provider's cache is already 80-87% warm
  from the previous turn. The stable prefix is the point — it settles the whole
  turn 0.3-1.0 s before the recognizer commits, which is what Chapter 5
  spends.
  The agent also opens with a greeting, synthesised once at startup — which
  absorbs the synthesis engine's cold start (3.1 s on a process's first call)
  rather than paying it on your first question.
- **Chapter 5 — speculation.** When a partial adds no new words, the agent
  assumes you have stopped and starts generating the *real* reply, cancelling it
  if you carry on. Measured live, time-to-first-token fell from 844 ms to 11 ms
  on a turn where it fired. It does not fire on short turns — the recognizer is
  still delivering words when the turn commits, so there is no dead air to use —
  and the cost of every wrong guess is counted on screen.
- **Chapter 6 — streaming synthesis.** The reply's audio plays as it is made.
  The whole text still goes in at once, but speech comes back as raw PCM
  (24 kHz, 16-bit, mono) over the same WebSocket, and the browser plays it as
  it arrives through an AudioWorklet queue. The provider's first byte now lands in
  130-166 ms whatever the reply's length, where batched synthesis took 144 ms
  to 1.3 s; measured live, a 20-second answer starts
  speaking ~0.7 s earlier and a 40-second one 1.2-1.7 s earlier. Any stutter —
  the queue running dry mid-reply — is counted on screen.
- **Refactor — the browser client as modules.** The page's script, which had
  grown to ~400 lines inline, is split into native ES modules served from
  `/static`. Still no build step and no npm; the player's logic now runs under
  `node --test` instead of being checked as text.
- **Chapter 7 — streaming synthesis input.** The agent speaks while the reply is
  still being written. Tokens go to ElevenLabs as they arrive, and its chunk
  schedule, not our code, decides when there is enough to say aloud. Measured
  live, a 35-second answer starts speaking 460-800 ms before the reply has
  finished, and first audio fell from 1.7-1.9 s to 1.0-1.2 s (measured with a 50-char first piece, since raised to 120 for a natural first phrase). Its `auto_mode`,
  fed tokens, voiced every token as a separate utterance, which is why it is off.
- **Chapter 8 — barge-in.** Talk over the agent and it stops. The mic stays
  open while it speaks, the first recognized word stops the voice and any reply
  still being written, and the history keeps only what you heard — worked out
  from ElevenLabs' per-character timing and how much audio the page played.
  Measured live, ~0.85–1.6 s from starting to speak
  to the stop, all of it recognizer lag: the baseline a local VAD has to beat.
- **Karaoke.** While the agent speaks, words not yet spoken are dimmed and
  light up as the voice reaches them — from the same per-character timing and
  played-sample count barge-in uses, so an interruption freezes them at the cut.
- **Measurement — LLM latency by provider.** `--bench-llm` compares time to
  first token through the real adapters: from here, Claude Haiku 4.5 ~520–580 ms,
  DeepSeek ~720–930 ms. Connections are now kept between turns; they had been
  reopened on every streamed call. Each turn shows when the provider accepted the
  request, and whether a connection or a retry was needed.
- **The agent's grammar matches its voice.** Russian and many other languages
  put the *speaker's* gender on ordinary past-tense verbs, so the agent was
  saying «я понял» — a man's form — in a woman's voice. `--voice-gender`
  (`female` by default, `male`, or `neutral`) tells it which to use, and the
  startup line shows it next to the voice so a mismatch is visible.
- **Chapter 13 — the clock, retuned.** The agent now considers speaking
  unprompted at 5, 15 and 28 seconds of silence rather than 15 and 28, and the
  first of those is a new, deliberately small move: follow through on what was
  just said, not a fresh topic and not "are you still there". The silence is
  counted from when the agent's *own voice stops*, so the old first nudge landed
  35 seconds after a 20-second answer. It also knows its own delays now, and
  says them when asked instead of guessing.
- **Chapter 12 — a second pair of ears.** Speech recognition now has two
  backends behind one protocol: AssemblyAI Universal-Streaming (the default)
  and ElevenLabs Scribe (`--stt elevenlabs`). Nothing downstream of a
  transcript changed. `--vad-silence` stays the single endpointing knob and
  means the same thing on both, though the services take it in different units.
  **AssemblyAI transcribes 18 languages and Russian is not among them** — a
  language it does not know is turned into confident nonsense rather than
  refused, so use `--stt elevenlabs` for those (Scribe covers 100, including
  Russian). What the ears understand is now said in three places: the startup
  banner, the page's status bar and a note in the log, and the agent's own
  system prompt, so it stops offering to listen in languages it cannot hear. AssemblyAI has **no** standalone text-to-speech, so the voice stays
  ElevenLabs — their synthesis exists only inside a managed voice-agent bundle
  that would replace this pipeline wholesale.
- **Chapter 11 — the trace.** A machine-readable companion to the record:
  `traces/<date>-<pid>.jsonl`, one JSON object per line, holding every reasoning
  call with its whole prompt and reply, every synthesis, every recognizer event,
  and every log line. It is a **span tree** — `conversation → turn → llm / tts` —
  using OpenTelemetry's vocabulary and `gen_ai.*` attribute names without taking
  the dependency, because a local file can hold a whole prompt and an OTLP
  backend mostly will not. Spans are written as a start and an end, so a span
  with no end is a hang. API keys are redacted by name and by shape. It also
  brings the first logging configuration this project has ever had: until now
  nothing configured it, so every `logger.info` call went nowhere.
- **Chapter 10 — the record.** Every conversation writes itself to
  `sessions/<date>-<id>.md` as it happens: turns, timings, decisions, what each
  one cost. It is tapped off the one socket every frame already goes through, so
  it cannot drift from what the page showed, and it prints whatever fields a
  frame carries rather than re-phrasing them. No audio is ever written — binary
  frames are counted and discarded. Nothing expires; `--purge-sessions` is the
  delete, and it asks first.
- **Chapter 9 — the clock.** The agent can speak first. Every turn before this
  was started by the user; nothing in the process ever woke up on its own, so a
  silence lasted forever. Now a ticker watches the silence and, at two points in
  it, asks the agent whether there is anything worth saying — with an explicit
  licence to answer "nothing", which is what it usually does. It only ever
  speaks *into* silence, never over you: someone who starts talking mid-decision
  makes the silence *shorter*, and that is the signal to hold back. After two
  rungs it is quiet until you speak. The second withdraws — "I'm here whenever
  you're ready" — and measured against a real model that is most of what it
  does. Every consideration is drawn on the page, declines and failures
  included, with what deciding cost, because an agent that keeps choosing not to
  interrupt is the thing being built and it is invisible otherwise. A third rung
  at seven seconds was built and then deleted: it never once fired, because its
  job was to invite you in and the greeting had already done that.

## Requirements

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)
- Optional: node ≥ 22, to run the page's own tests (`uv run verify` skips them
  without it, and says so)

## Setup

```bash
uv sync                 # install dependencies, including dev tools
cp .env.example .env    # then fill in only the provider keys you need
```

`uv run voice-agent` loads `.env` itself. Library code never does, so
importing the package has no hidden side effects — to run anything else with
those secrets, pass them explicitly:

```bash
uv run --env-file .env <command>
```

## Usage

```bash
uv run voice-agent                          # DeepSeek + AssemblyAI ears + ElevenLabs voice, on :8000
uv run voice-agent --provider anthropic --model claude-sonnet-5
uv run voice-agent --tts openai --voice nova
uv run voice-agent --voice-gender male       # match how the agent refers to itself
uv run voice-agent --tts none               # no synthesis key needed
uv run voice-agent --stt elevenlabs         # Scribe: needed for Russian and other
                                            # languages AssemblyAI does not cover
uv run voice-agent --stt none               # typing only, no recognition
uv run voice-agent --vad-silence 2.0        # wait longer before ending a turn
uv run voice-agent --initiative off         # never speak first; purely reactive
uv run voice-agent --sessions off           # do not write conversations down
uv run voice-agent --purge-sessions         # delete every recorded conversation
uv run voice-agent --trace off               # no technical trace
uv run voice-agent --initiative 5,20        # when to consider speaking into a silence
uv run voice-agent --list-voices            # what this account can actually use
uv run voice-agent --bench-llm              # time to first token per provider (a few billed calls)
uv run voice-agent --bench-llm anthropic:claude-haiku-4-5 deepseek
uv run voice-agent --port 9000
```

Open <http://127.0.0.1:8000> — you land on a fresh conversation at `/c/<key>`.
Type, or press **listen** and talk; the agent answers out loud either way. Keep that link to come back to the same
conversation; type `exit` to end it. Everything is in memory, so restarting the
server clears it all.

## Development

```bash
uv run verify                 # ruff + format check + mypy (strict) + pytest
uv run pytest                 # tests only
uv run pytest --cov           # tests with coverage
uv run pytest -m "not audio"  # skip tests needing real audio hardware
uv run ruff check src tests   # lint only
uv run ruff format src tests  # auto-format
uv run mypy                   # type check only
```

`uv run verify` is the single gate — run it before committing. CI runs exactly
the same command, so local and remote can never disagree.

## Project layout

```
AGENTS.md                 the working contract for agents and humans (CLAUDE.md symlinks here)
CHANGELOG.md              the chapter log — what was built, and why
prompts/
  system_prompt.md        the voice agent's own runtime system prompt
src/voice_agent/
  cli.py                  chapter 1: `uv run voice-agent` (entry point)
  server.py               chapter 1: routes, the socket's receive loop
  session.py              one connected conversation: starts, interrupts and cancels turns
  turn.py                 chapter 1/2/6/7/8: one exchange — the reply spoken while it is written
  heard.py                chapter 8: what the user actually heard of an interrupted reply
  channel.py              serialized writes to the socket, shared audio framing
  mic.py                  chapter 3: one listening session — transcripts, expiry, keepalive
  greeting.py             chapter 2: the opening line, synthesised once and cached on disk
  warming.py              chapter 4: prefill on agreed-stable text
  speculation.py          chapter 5: answering before the question finishes
  initiative.py           chapter 9: whether to speak into a silence, and when to stay quiet
  streams.py              closing a provider's stream when whoever read it stops
  record.py               chapter 10: the conversation, written down as it happens
  trace.py                chapter 11: the span tree, the bodies, and the redaction
  timing.py               stage timings on one monotonic clock
  bench.py                `--bench-llm`: time to first token, provider by provider
  conversation.py         chapter 1: Message + Conversation — the context itself
  sessions.py             chapter 1: in-memory store, one conversation per link
  config.py               chapter 1: env settings + system-prompt loading
  errors.py               errors this project raises deliberately
  verify.py               the `uv run verify` quality gate
  web/                    the browser client: native ES modules, no framework, no build step
    index.html            markup and styles; loads app.js as a module
    app.js                wiring: the socket, message handling, shared page state
    player.js             streaming playback rules: stop on interrupt, autoplay, replacement (tested in node)
    playback-worklet.js   the audio-thread queue that plays PCM seamlessly and counts gaps (tested in node)
    mic.js                microphone permission and the capture graph
    capture-worklet.js    the audio-thread processor that emits PCM16 frames
    karaoke.js            which words have been spoken, from timing and samples played (tested in node)
    ui.js                 the chat log: append, scroll, enable, paint spoken text
    protocol.js           the messages the page sends
  llm/
    base.py               the LLM protocol every reasoning backend implements
    registry.py           name -> backend
    http.py               connections kept between turns, and what opening one cost
    traced.py             a span around every reasoning call, whoever makes it
    openai_compatible.py  OpenAI and DeepSeek (same wire format)
    anthropic_provider.py Anthropic (system prompt and streaming differ)
  stt/                    chapters 3, 12: speech recognition
    base.py               Transcript + the STT protocol
    registry.py           name -> backend, or None for deafness
    assemblyai_stt.py     Universal-Streaming v3 (default); 18 languages, no ru
    elevenlabs_stt.py     Scribe realtime over a raw WebSocket (VAD endpointing)
  tts/                    chapter 2: speech synthesis
    base.py               the TTS protocol: a text stream in, PCM chunks out
    registry.py           name -> backend, or None for silence
    elevenlabs_tts.py     ElevenLabs (default): tokens in over the stream-input WebSocket
    openai_tts.py         OpenAI: whole text only, so it waits for the reply
docs/ROADMAP.md           research notes and candidate future chapters
tests/                    pytest suite; tests/web/ holds node tests for the page (`node --test`)
.env.example              provider keys and settings
```

## Roadmap (likely future chapters)

Nothing below is committed to, and nothing below should be built ahead of its
turn — the order will change as earlier chapters teach us things.
[docs/ROADMAP.md](docs/ROADMAP.md) has the long version: the latency
arithmetic, the themes behind these chapters, and the alternatives considered.

Measured so far, the round trip is dominated by two vendor-side waits, not by
network hops: the recognizer (1.3 s to commit a turn, 0.85–1.6 s to notice
barge-in) and time to first token (~0.5–0.9 s). So, in order:

1. **A voice detector in the page** (Silero or WebRTC VAD). Barge-in in a few hundred
   milliseconds instead of the recognizer's second. It also gives the first honest
   "user stopped speaking" timestamp, and stops paying the recognizer for silence.
   Chapter 9 made this load-bearing rather than merely next. It is not only that
   the clock measures silence from the recognizer's last output, which trails
   real speech by several hundred milliseconds — it is that **nothing in the
   agent can currently answer "is someone speaking right now?"** The clock
   infers it from the silence getting shorter. A voice detector is the first
   component that would simply know.
2. **Semantic turn detection.** The turn ends when the words sound finished,
   not when the vendor commits, reported as FEC / MSC / OVER / NDS. It goes after
   the largest term, and needs step 1's timestamps.
3. **A golden conversation suite.** Scripted audio replayed through the whole pipeline,
   with latency and pronunciation as gates, so later changes are measured
   rather than felt.
4. **Cost accounting and small-model routing**, made safe by step 3.
5. **Deployment or telephony transport.** Colocation, WebRTC, and whether the
   server belongs in the media path become real questions here, not on localhost.
6. **A duplex speech-to-speech backend** behind the same interface, to A/B against
   the cascade.

Then, continuing what Chapter 9 started — a **mixed-initiative** agent that
holds a share of the initiative instead of waiting to be addressed:
**backchannels** (a cached "mm-hm" placed *over* the user without claiming the
floor — the first utterance that is not a turn), **transition-relevance-place
detection** (the same classifier as semantic turn detection, asked "is this a
place I could come in?"), a **graded floor policy** from backchannel through to
the rare hard interrupt with a per-exchange budget and a concession rule, and
**always-on judgement** with a line prepared and held ready. Evaluation for all
of it is interruption precision, uptake, and sampled human ratings of
intrusiveness — the decline log Chapter 9 draws is its first row.

Smaller along the way: choosing the default model from the bench's numbers;
tool calling with confirmation boundaries.

See [CHANGELOG.md](CHANGELOG.md) for what has actually shipped.

## License

MIT
