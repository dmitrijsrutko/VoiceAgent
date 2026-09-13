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
  Half-duplex on purpose — the mic mutes while the agent speaks, and removing
  that is the barge-in chapter.
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

## Requirements

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/)

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
uv run voice-agent                          # DeepSeek + ElevenLabs, on :8000
uv run voice-agent --provider anthropic --model claude-sonnet-5
uv run voice-agent --tts openai --voice nova
uv run voice-agent --tts none               # no synthesis key needed
uv run voice-agent --stt none               # typing only, no recognition
uv run voice-agent --vad-silence 2.0        # wait longer before ending a turn
uv run voice-agent --list-voices            # what this account can actually use
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
  server.py               chapter 1/3: routes, the socket, Channel, Mic, the turn
  conversation.py         chapter 1: Message + Conversation — the context itself
  sessions.py             chapter 1: in-memory store, one conversation per link
  config.py               chapter 1: env settings + system-prompt loading
  errors.py               errors this project raises deliberately
  verify.py               the `uv run verify` quality gate
  web/index.html          chapter 1: the chat page (no framework, no build step)
  llm/
    base.py               the LLM protocol every reasoning backend implements
    registry.py           name -> backend
    openai_compatible.py  OpenAI and DeepSeek (same wire format)
    anthropic_provider.py Anthropic (system prompt and streaming differ)
  stt/                    chapter 3: speech recognition
    base.py               Transcript + the STT protocol
    registry.py           name -> backend, or None for deafness
    elevenlabs_stt.py     Scribe realtime over a raw WebSocket (VAD endpointing)
  tts/                    chapter 2: speech synthesis
    base.py               AudioClip + Voice + the TTS protocol
    registry.py           name -> backend, or None for silence
    elevenlabs_tts.py     ElevenLabs (default)
    openai_tts.py         OpenAI
docs/ROADMAP.md           research notes and candidate future chapters
tests/
.env.example              provider keys and settings
```

## Roadmap (likely future chapters)

Nothing below is committed to, and nothing below should be built ahead of its
turn — the order will change as earlier chapters teach us things.
[docs/ROADMAP.md](docs/ROADMAP.md) has the long version: the latency
arithmetic, the themes behind these chapters, and the alternatives considered.

Barge-in: letting the user interrupt, which means undoing Chapter 3's
half-duplex gate, and recording what the caller actually *heard* rather than
what the agent meant to say. Semantic turn detection, to replace the single
silence threshold and take endpointing back from the vendor — currently 1.3 s
of a 2.9 s round trip, and not measurable from inside the project. Streaming
synthesis, to stop paying 500 ms serially. Then context management and prompt
caching for the reasoning stage, which grows worse every turn.

After that the work turns to making it feel human: barge-in and interruption
handling, streaming every stage boundary so nothing waits for a complete
result, speculative and partial-transcript execution, turn-detection quality
beyond simple silence thresholds, backchannels and filler while the model
thinks. Then the surrounding machinery: conversation memory, tool calling with
confirmation boundaries, provider swapping and A/B comparison, an eval harness
with recorded conversations, tracing and per-stage latency metrics, cost
tracking, telephony transport, and deployment.

See [CHANGELOG.md](CHANGELOG.md) for what has actually shipped.

## License

MIT
