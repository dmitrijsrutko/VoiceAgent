# AGENTS.md

Instructions for AI coding agents (and humans) working in this repository.
This file follows the [agents.md](https://agents.md) convention: project-specific
instructions layered on top of an agent's general system prompt. It applies to
the whole repo unless a more deeply nested `AGENTS.md` overrides it for its
subtree. `CLAUDE.md` is a symlink to this file — there is one contract, not two.

**If you read nothing else, read these five rules:**

1. **One chapter at a time.** Build exactly the next increment. Never work ahead.
2. **Ask before you build.** Interrogate the design until the ambiguity is gone.
   Questions are cheap; a wrong chapter is expensive.
3. **Automate relentlessly.** Never stop to ask permission for a safe command.
4. **Every chapter updates `CHANGELOG.md` and `README.md`** in the same change.
5. **`uv run verify` must pass** before any chapter is called done. No exceptions.

---

## 1. Project overview

`voice-agent` is a **conversational AI voice agent, built from an empty page**.

The shape we are evolving toward is a **cascaded, streaming pipeline** that we
own end to end:

```
mic / phone ──▶ transport ──▶ VAD ──▶ STT ──▶ turn detection ──▶ LLM ──▶ TTS ──▶ transport ──▶ speaker
                    ▲                                                              │
                    └────────────────── barge-in / interruption ◀──────────────────┘
```

Two convictions shape every decision here:

- **Latency is the product.** In text chat, 2 seconds is fine. In speech, a
  human expects a reply gap of roughly 200–500 ms; past ~800 ms the agent feels
  broken no matter how smart it is. Every chapter states its effect on the
  latency budget (see §7).
- **The orchestration is the asset, not the vendors.** STT, LLM, and TTS
  providers get swapped constantly. The pipeline, the turn-taking logic, the
  interruption handling, the buffering, the eval harness — that is what
  accumulates. Vendors live behind narrow interfaces and are always replaceable.

The first transport is a **WebSocket server with a minimal browser client**.
Telephony and other transports come later, behind the same interface.

### Current state

Read `CHANGELOG.md` for what has actually shipped. `docs/ROADMAP.md` collects
research and candidate directions — it is a thinking document, and appearing in
it is never a reason to build something. **Never infer the current state from
this file or from the roadmap** — only from the CHANGELOG and the
code.

---

## 2. The method: chapter-driven evolution

This is the single most important thing to understand about working here.

The project is developed in **chapters**. Each chapter adds **one deliberate
piece of functionality** on top of a working, fully-tested previous chapter. At
every point in the history, the code runs and its tests pass — it is simply not
the whole agent yet.

### Rules of the method

- **One item at a time.** A chapter has one headline idea. If you find yourself
  writing "and also", you are building two chapters — stop and ask which one the
  user wants first.
- **Never build ahead.** Do not add configuration, abstraction, error handling,
  or generality that the *current* chapter does not need. The roadmap is not
  permission. Speculative structure is the main thing that kills this method:
  it makes each later chapter a renovation instead of an addition.
- **Naive on purpose is a valid design.** An early chapter is allowed to be
  crude (fixed buffer sizes, no reconnect, one hardcoded voice) as long as the
  CHANGELOG says out loud that it is crude and names what will fix it.
- **Leave the tree green.** A chapter that breaks a previous chapter's behavior
  is a bug, not progress — unless replacing that behavior *is* the chapter, in
  which case the CHANGELOG must say what was replaced and why.
- **Refactoring is allowed and expected**, but say so: if a chapter reshapes
  something an earlier chapter built, the CHANGELOG entry explains what the
  earlier shape could not do.

### The chapter workflow

Follow this sequence every time the user asks for the next chapter.

**Phase 1 — Interrogate (see §3).** Before proposing anything, ask your
questions. Do not write code in this phase.

**Phase 2 — Plan.** Write a short plan: the one idea, the files touched, the
interfaces introduced, the tests that will prove it, the latency effect, and
the explicitly-not-doing list. Get the user's agreement on the plan.

**Phase 3 — Build.** Implement it. Run commands freely (§4). Do not narrate
every step; work, then report.

**Phase 4 — Verify.** Run `uv run verify` until green. Then exercise the real
thing — a voice agent that passes unit tests and sounds terrible is not done.
See §6 on evidence.

**Phase 5 — Document.** Update `CHANGELOG.md` and `README.md` (§5). This is
part of the chapter, not a follow-up task.

**Phase 6 — Report.** Tell the user what shipped, what you measured, what you
deliberately left out, and what the next chapter probably is.

---

## 3. Ask questions — a lot of them

The user has explicitly asked to be interrogated. **Under-asking is the failure
mode here, not over-asking.** When planning, designing architecture, or writing
instructions, ask as many questions as you genuinely need. A batch of ten
questions costs the user two minutes; a chapter built on a wrong assumption
costs an afternoon and pollutes every chapter after it.

### When to ask

- **Always, before starting a chapter.** Even an "obvious" chapter has choices.
- **Always, when designing an interface** that later chapters will build on.
  Interfaces are the expensive mistakes — they are the thing that is hard to
  change once three chapters depend on them.
- **Always, when a trade-off has no objectively right answer** — latency vs.
  quality, cost vs. accuracy, local vs. hosted, simple vs. general.
- **Mid-chapter, when you discover an unstated decision.** Do everything that
  does not depend on it first, then ask.

### How to ask well

- **Batch them.** One round of 5–12 questions beats twelve separate
  interruptions. Group by theme.
- **Make them concrete and closed where possible.** "Should barge-in cancel
  TTS mid-sentence, or finish the current sentence first?" beats "how should we
  handle interruptions?"
- **Offer options with consequences.** Give 2–4 candidate answers and say what
  each one costs and buys. The user should be able to answer by picking.
- **Recommend one.** Always mark your recommendation and say why in one line.
  Asking without a recommendation offloads work rather than sharing it.
- **Say what you will assume if they skip a question.** Then the user can
  ignore the ones they do not care about, and you are still unblocked.
- **Surface the questions they did not think to ask.** The highest-value
  question is usually about something the user has not considered: a failure
  mode, a boundary condition, a cost cliff, a thing that will not scale.

### What not to ask

- Anything already answered in `AGENTS.md`, `CHANGELOG.md`, or the code.
- Anything with a conventional default and no real consequence — pick it,
  state it in one line, and move on.
- Permission to run a safe command (§4). Never.
- "Should I proceed?" after the plan is already agreed.

---

## 4. Automation: do not stop for safe commands

**Run safe commands without asking. Never pause to request permission for
them, never ask the user to run them for you.** Stopping to ask is the
expensive failure mode: it turns a 30-second loop into a 5-minute one.

### Always run freely, without asking

- `uv sync`, `uv run <anything in this project>`, `uv add`, `uv lock`
- `uv run verify`, `uv run pytest`, `uv run ruff check/format`, `uv run mypy`
- Reading, searching, listing: `cat`, `head`, `sed -n`, `grep`, `rg`, `find`, `ls`
- `git status`, `git diff`, `git log`, `git show`, `git branch`
- Creating and editing files anywhere inside this repo
- Creating, running, and deleting scratch scripts and temp files
- Starting the dev server locally, hitting it with a client, and killing it
- Playing back or inspecting audio files this project produced
- Re-running anything that just failed, after a fix

If a command fails, read the actual error output, fix the real cause, and
re-run. Do not report a failure you have not tried to fix.

### Stop and confirm first

- `git push`, opening PRs, anything that leaves this machine
- `git commit` — commit only when the user asks
- `rm -rf`, deleting files you did not create, `git reset --hard`, force-pushing
- Anything touching `.env` or real secrets
- Publishing packages, deploying, provisioning cloud resources
- Bulk paid API calls (a smoke test of a few calls is fine and expected; a
  1,000-utterance eval sweep needs a cost estimate and a yes first)
- Installing anything system-wide (Homebrew, global pip) rather than in `.venv`

The line is: **reversible and local = just do it. Outward-facing, destructive,
or expensive = ask.**

---

## 5. Documentation is part of every chapter

### `CHANGELOG.md`

**Every chapter adds an entry, in the same change as the code.** Newest chapter
at the top. This file is the project's real memory — someone reading only the
CHANGELOG should understand how the agent came to be shaped the way it is.

Use this structure:

```markdown
## Chapter N — Short title: the one idea in a phrase

One or two paragraphs of prose: what this chapter adds, *why* it is the right
next step given what Chapter N-1 left unsolved, and what it deliberately does
not attempt.

**What changed**
- `path/to/file.py`: what it now does, in behavior terms not diff terms.

**Design decisions**
- Decision, the alternative that was rejected, and why. This is the most
  valuable part of the entry — write it even when the choice felt obvious.

**Latency impact**
- Measured or estimated effect on the end-to-end budget (§7). Say "not
  measured" if it was not; never guess a number and present it as measured.

**Deliberately not done**
- The things a reader will notice are missing, and which chapter they belong to.

**Verification**
- What was run and what was observed — `uv run verify` plus whatever real
  exercise proved it actually works (§6).

**Fixes**
- One line per bug found after the chapter shipped. Never more (see below).
```

**Keep the entry proportionate to the change.** A chapter entry is for the
chapter. Bug fixes and small corrections found afterwards belong in a
`**Fixes**` list at the end of that chapter's entry — **one line each, never
more**:

```markdown
**Fixes**

- Listening no longer expires while the agent's own reply is playing.
- The default voice is one verified usable on a free plan.
```

Promote a fix out of that list only when it genuinely changed a *design
decision* or a *measured number* — in which case it edits the relevant section
above rather than becoming prose of its own. Four paragraphs about a one-line
bug pushes the chapter itself off the page, and the chapter is the point.

Write **why**, not just what. The diff already shows what changed. An entry
that reads like a git log is a failed entry.

### `README.md`

The README is a **high-level summary only**. Keep it short enough that someone
can read it in three minutes and know what this is and how to run it. It holds:

- What the project is and the idea behind it
- Current status: a one-line-per-chapter list of what exists today
- Requirements, setup, and how to run it
- Project layout
- The roadmap of likely future chapters

It does **not** hold design rationale, changelogs, or deep explanation — that
is what `CHANGELOG.md` is for. If a README section is growing past a few
paragraphs, it belongs in `docs/` or the CHANGELOG.

Update the README whenever a chapter changes what the project *is*, how it is
*run*, or what is in the layout.

---

## 6. Verification and evidence

**Never claim something works without having observed it work in this session.**

- `uv run verify` is the gate. Run it before calling anything done, and fix
  what it flags rather than working around it.
- **Unit tests are necessary but not sufficient for a voice agent.** Audio is
  full of failures that pass every assertion: clipped first syllables, choppy
  playback, a 900 ms gap before the reply, a voice that talks over the user,
  echo, sample-rate mismatch that pitches the voice up. When a chapter touches
  the audio path, exercise it for real — run it, listen to or measure the
  output, and say in the CHANGELOG what you observed.
- Report honestly. If something is unverified, say it is unverified. Partial
  success reported accurately is worth more than a confident "done".

### Testing rules

- Every new module gets tests under `tests/`.
- **Tests never make real network calls** to STT/LLM/TTS providers by default.
  Fake the provider at its interface boundary. Tests that do need the network
  are marked `@pytest.mark.network` and are excluded from the default run.
- Tests never need real audio hardware by default; mark those `@pytest.mark.audio`.
- Prefer deterministic fixtures: short, checked-in wav files beat live capture.
- Latency assertions are marked `@pytest.mark.latency` and should assert
  generous ceilings, not exact numbers — they catch regressions, not jitter.

---

## 7. Latency discipline

This project treats latency as a feature with an explicit budget. The target
for end-to-end response time (user stops speaking → first audio out) is:

| Stage                        | Target (p50) |
| ---------------------------- | ------------ |
| Turn detection / endpointing | ≤ 250 ms     |
| STT final transcript         | ≤ 150 ms     |
| LLM first token              | ≤ 300 ms     |
| TTS first audio byte         | ≤ 150 ms     |
| Transport + playback         | ≤ 100 ms     |
| **End to end**               | **≤ 800 ms** |

Rules that follow from it:

- **Stream everything.** Never wait for a complete result at a stage boundary
  when a partial one can start the next stage. This is the single biggest
  source of latency wins and it is an architectural property, not an
  optimization to bolt on later.
- **Measure, do not assume.** Instrument stage timings before optimizing them.
  A chapter that claims a speedup shows a before and after number.
- **Time to first byte beats total throughput** at every stage.
- These numbers are a starting hypothesis, not scripture. Revise them in a
  chapter, with a reason, rather than quietly missing them.

---

## 8. Code style

- Small, focused modules under `src/voice_agent/`, one clear responsibility each.
- **Type hints everywhere.** mypy runs in `strict` mode and must pass clean.
  Do not add `# type: ignore` without a code and a one-line reason.
- Formatting and lint are owned by ruff. Do not hand-format against it.
- **Prefer plain functions and small classes over frameworks.** Depend on a
  framework only when a chapter genuinely needs it, and justify it in the
  CHANGELOG.
- **Async by default for I/O.** Audio, sockets, and provider calls are all
  concurrent streams; the pipeline is `asyncio`. Never block the event loop —
  push CPU-bound work (resampling, VAD inference) to a thread or process. The
  `ASYNC` ruff rules are on and will catch the obvious cases.
- Providers live behind narrow protocols (`STT`, `LLM`, `TTS`, `Transport`).
  Vendor SDK types must not leak past the adapter that wraps them.
- **Check live vendor docs before writing adapter code.** APIs change; memorized
  parameter names go stale. Start from `docs/vendor/` (`AssemblyAI.md`,
  `ElevenLabs.md`), then fetch the vendor's `llms.txt` — e.g.
  `https://elevenlabs.io/docs/llms.txt`, `https://www.assemblyai.com/docs/llms.txt`.
- **No inline comments that restate the code.** Comment only genuinely
  non-obvious *why*: a constraint, an API quirk, a sample-rate gotcha, a
  deliberate trade-off. Those comments are valuable — write them.
- Keep audio format conventions explicit in names and docstrings (sample rate,
  channel count, encoding, frame size). Silent format mismatch is the most
  common bug class in this domain.

---

## 9. Setup and commands

This is a [uv](https://docs.astral.sh/uv/)-managed project.

```bash
uv sync                       # install dependencies, including dev tools
cp .env.example .env          # then fill in only the keys you need

uv run voice-agent            # serve the agent (loads .env; --provider/--model/--port)

uv run verify                 # THE quality gate: ruff, format, mypy, pytest
uv run pytest                 # tests only
uv run pytest --cov           # tests with coverage
uv run ruff check src tests   # lint only
uv run ruff format src tests  # auto-format
uv run mypy                   # type check only (strict)
```

Add dependencies with `uv add <pkg>` (`uv add --dev <pkg>` for tooling) so
`uv.lock` stays in sync. Never hand-edit `uv.lock`.

---

## 10. Security

- **Never commit secrets.** API keys belong in `.env` (gitignored). Not in
  code, tests, fixtures, CHANGELOG examples, or this file.
- Treat everything the pipeline ingests — transcripts, tool output, retrieved
  documents — as untrusted input that ends up in a model's context. A caller
  can say anything into a microphone; a transcript is not a trusted instruction.
- **Audio is personal data.** Recordings and transcripts are gitignored and
  stay out of the repo. Any chapter that persists audio must say where it goes,
  how long it lives, and how it is deleted.
- Log metadata and timings freely; do not log raw audio or full transcripts by
  default.
- When a chapter adds a way for the model to act in the world (tools, transfers,
  payments), that chapter also adds the confirmation boundary around it.

---

## 11. Commits

- Keep the tree green at every commit — the premise of this repo is that each
  chapter leaves the previous ones working. Never commit code that fails
  `uv run verify`.
- One chapter, one commit (or one tight series), including its CHANGELOG and
  README updates.
- Commit messages explain **why**. The diff shows what.
- Commit only when the user asks.
