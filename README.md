# voice-agent

A conversational AI voice agent, built from an empty page — one chapter at a time.

**Try it: <https://voice-agent-chapters.fly.dev>** — pick a reasoning engine and
a pair of ears, press **start conversation**, then just talk. Headphones help:
the agent listens while it speaks, so you can cut it off mid-sentence.
Conversations are written down on the server, and the page says so before you
start.

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

Built in **chapters**, each one deliberate piece on top of a working, tested
previous one. [CHANGELOG.md](CHANGELOG.md) has the reasoning and the numbers.

- **0 — The empty page.** The method, the working contract ([AGENTS.md](AGENTS.md)), and one quality gate: `uv run verify`.
- **1 — A talking loop.** Browser ↔ WebSocket ↔ a streaming LLM behind one interface (Anthropic, OpenAI, DeepSeek); the link is the conversation.
- **2 — A voice.** Replies are synthesized (ElevenLabs) and played; every stage's cost is shown under the reply.
- **3 — Ears.** Speech recognition with a live transcript; speaking and typing enter through the same door.
- **4 — Acting early.** Partials that agree twice are treated as settled (LocalAgreement), and the greeting is synthesized at startup.
- **5 — Speculation.** The reply starts before the turn is committed, and is cancelled if the user carries on.
- **6 — Streaming synthesis.** Speech plays as it is made, as raw PCM through an AudioWorklet.
- **7 — Streaming synthesis input.** The agent speaks while the reply is still being written (first audio 1.7–1.9 s → 1.0–1.2 s).
- **8 — Barge-in.** Talk over the agent and it stops; the history keeps only what was heard. Karaoke highlights the words as they are spoken.
- **9 — The clock.** The agent may speak into a silence, and usually decides not to.
- **10 — The record.** Each conversation is written to a Markdown file as it happens. No audio is stored.
- **11 — The trace.** A JSONL span tree of every provider call, for programs to read.
- **12 — A second pair of ears.** AssemblyAI (default, 18 languages) or ElevenLabs Scribe (100 languages).
- **13 — The clock, retuned.** Considers speaking at 5, 15 and 28 s of silence, and knows its own delays.
- **14 — Off localhost.** A public instance on Fly.io, next to the vendors, with spend caps that are off unless configured.
- **15 — The vendors become a choice.** The start screen picks engine and ears per conversation; backends are shared and kept warm.
- **Simplification.** Warming removed, the server and client split into smaller parts, telemetry behind a **details** switch, and comments cut to their constraints.
- **16 — Ears that hear pauses.** A voice detector on the server shows who holds the floor, ~0.75 s before AssemblyAI commits (~1.6 s before Scribe).

## Requirements

- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- Optional: node ≥ 22, to run the page's own tests (`uv run verify` skips them without it, and says so)

## Setup and usage

```bash
uv sync                                     # install, including dev tools
cp .env.example .env                        # fill in only the keys you need
uv run voice-agent                          # Anthropic + AssemblyAI + ElevenLabs, on :8000
uv run voice-agent --provider deepseek      # or openai; --model overrides the model
uv run voice-agent --stt elevenlabs         # Scribe, for languages AssemblyAI lacks
uv run voice-agent --tts none --stt none    # silent and deaf: typing only
uv run voice-agent --initiative off         # never speak first
uv run voice-agent --help                   # everything else
```

Open <http://127.0.0.1:8000>, press **start conversation**, and talk or type.
Keep the link to come back to the same conversation; type or say `exit` to end
it. Conversations live in memory, so a restart clears them.

## Development

```bash
uv run verify     # ruff + format check + mypy (strict) + pytest (+ node tests)
```

`uv run verify` is the single gate: run it before committing.

## Project layout

```
AGENTS.md, CHANGELOG.md   the working contract; the chapter log
prompts/system_prompt.md  the agent's runtime system prompt
src/voice_agent/
  cli.py, config.py       `uv run voice-agent`; settings from the environment
  server.py               routes, and one conversation's socket (`serve`)
  pool.py                 which engines/ears a conversation may pick; one shared instance of each
  session.py              one connected conversation: turns, interruption, cancellation
  turn.py                 one exchange: the reply streamed to the page and the voice
  mic.py                  one listening session: transcripts, expiry, keepalive
  vad.py, floor.py        speech per 32 ms window (Silero, in models/); who holds the floor
  speculation.py          answering before the question finishes
  initiative.py, decline.py  speaking into a silence; keeping the decline word unspoken
  heard.py                what the user actually heard of an interrupted reply
  greeting.py             the opening line, synthesized once and cached
  record.py, trace.py     the conversation as Markdown; the span tree as JSONL
  limits.py, sessions.py  the public caps; the in-memory conversation store
  llm/ stt/ tts/          one protocol each, the vendor adapters, and a registry
  web/                    the page: native ES modules, no build step
    app.js                the socket and one handler per server message
    start.js              the start screen
    telemetry.js          the lines under each bubble (tested in node)
    floor.js              the floor strip (tested in node)
    player.js, karaoke.js, playback-worklet.js   playback and highlighting (tested in node)
    mic.js, capture-worklet.js, ui.js, protocol.js
docs/                     ROADMAP.md (research), DEPLOY.md (runbook), vendor/ (AssemblyAI.md, ElevenLabs.md + skills snapshot)
tests/                    pytest; tests/web/ holds node tests
```

## Deployment

One always-on machine, one region, one volume on Fly.io: conversations live in
process memory and the greeting is cached per process, so a second instance
would break links. [docs/DEPLOY.md](docs/DEPLOY.md) is the runbook and
`fly.toml` the configuration. The public instance turns on the caps in
`limits.py` and records conversations (never audio); the span-tree trace is off
there.

## Roadmap

Measured so far, the round trip is dominated by the recognizer (about 1.0 s from
the end of speech to a commit on AssemblyAI, 1.8–1.9 s on Scribe) and time to
first token (about 0.5–0.9 s). Likely next, in order:

1. **The inner voice**: a fast model thinks alongside the conversation in a sparring-partner role, shown on the page but not spoken.
2. **Cutting in at a pause**, then **speaking over** the user, with an assertiveness dial per role.
3. **Leading**: the role has an agenda and steers toward it.
4. **Semantic turn detection**: end the turn when the words sound finished.
5. **A golden conversation suite**, so later changes are measured rather than felt.
6. **Cost accounting and small-model routing.**
7. **Telephony transport.**

[docs/ROADMAP.md](docs/ROADMAP.md) has the long version.

## License

MIT
