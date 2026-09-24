# AGENTS.md

Instructions for AI coding agents (and humans) working in this repository, per
the [agents.md](https://agents.md) convention. `CLAUDE.md` is a symlink to this
file — there is one contract, not two.

**If you read nothing else, read these five rules:**

1. **One chapter at a time.** Build exactly the next increment. Never work ahead.
2. **Ask before you build.** Interrogate the design until the ambiguity is gone.
   Questions are cheap; a wrong chapter is expensive.
3. **Automate relentlessly.** Never stop to ask permission for a safe command.
4. **Every chapter updates `CHANGELOG.md` and `README.md`** in the same change.
5. **`uv run verify` must pass** before any chapter is called done. No exceptions.

---

## 1. Project overview

`voice-agent` is a conversational AI voice agent, built from an empty page: a
**cascaded, streaming pipeline** we own end to end.

```
mic / phone ──▶ transport ──▶ VAD ──▶ STT ──▶ turn detection ──▶ LLM ──▶ TTS ──▶ transport ──▶ speaker
                    ▲                                                              │
                    └────────────────── barge-in / interruption ◀──────────────────┘
```

- **Latency is the product.** In speech a human expects a 200–500 ms gap; past
  ~800 ms the agent feels broken however smart it is. Every chapter states its
  effect on the budget (§7).
- **The orchestration is the asset, not the vendors.** Vendors sit behind
  narrow interfaces and get swapped. The pipeline, turn-taking, interruption
  handling and eval harness are what accumulate.

The current state is in `CHANGELOG.md` (recent) and `docs/history.md`
(Chapters 0–15), and in the code — never in this file or `docs/ROADMAP.md`,
which is a thinking document, not permission.

## 2. The method: chapter-driven evolution

Each chapter adds **one deliberate piece** on top of a working, fully tested
previous one. At every point in the history the code runs and its tests pass.

- **One headline idea.** Writing "and also" means two chapters: ask which first.
- **Never build ahead.** No configuration, abstraction or generality the
  current chapter does not need.
- **Naive on purpose is valid**, if the CHANGELOG says it is crude and names
  what will fix it.
- **Leave the tree green.** Breaking an earlier chapter's behaviour is a bug,
  unless replacing it *is* the chapter and the CHANGELOG says why.
- **Refactoring is expected**, and said so: the entry explains what the earlier
  shape could not do.

**Workflow per chapter:** interrogate (§3) → a short plan (the idea, files,
interfaces, tests, latency effect, not-doing list) agreed with the user →
build → `uv run verify`, then exercise the real thing (§6) → CHANGELOG and
README (§5) → report what shipped, what was measured, what was left out.

## 3. Ask questions — a lot of them

The user has asked to be interrogated; under-asking is the failure mode.

- **Always ask** before a chapter, before an interface later chapters build on,
  when a trade-off has no objectively right answer, and when an unstated
  decision turns up mid-chapter (do everything that does not depend on it first).
- **Ask well:** batch 5–12 questions by theme; make them concrete and closed;
  give 2–4 options with what each costs and buys; **recommend one** and say
  why; say what you will assume if a question is skipped; surface the question
  they did not think to ask (a failure mode, a cost cliff).
- **Do not ask** what `AGENTS.md`, the CHANGELOG or the code already answers,
  anything with a conventional default and no real consequence, permission for
  a safe command, or "should I proceed?" after the plan is agreed.

## 4. Automation

**Reversible and local: just do it.** `uv sync/run/add/lock`, `uv run verify`
and its parts, reading and searching, `git status/diff/log/show/branch`,
editing files in the repo, scratch scripts, running and killing the local
server, inspecting audio this project produced, re-running after a fix. If a
command fails, fix the real cause and re-run.

**Outward-facing, destructive or expensive: confirm first.** `git push`, PRs,
`git commit` (only when asked), `rm -rf` or deleting files you did not create,
`git reset --hard`, anything touching `.env` or secrets, deploying or
provisioning, bulk paid API calls (a few smoke calls are fine; a sweep needs a
cost estimate and a yes), system-wide installs.

## 5. Documentation is part of every chapter

**`CHANGELOG.md`** — every chapter adds an entry, newest first, in the same
change as the code. It is the project's memory: it says *why*; the diff says
what.

```markdown
## Chapter N — Short title: the one idea in a phrase

A paragraph: what this adds, why it is the right next step, what it does not attempt.

**What changed**         — per file, in behaviour terms
**Design decisions**     — the decision, the rejected alternative, why (the most valuable part)
**Latency impact**       — measured, or "not measured"; never a guess presented as a measurement
**Deliberately not done** — what a reader will miss, and which chapter it belongs to
**Verification**         — `uv run verify` plus the real exercise that proved it (§6)
**Fixes**                — one line per bug found after the chapter shipped, never more
```

- **Keep an entry to ~60 lines.** Promote a fix out of the one-line list only
  when it changed a design decision or a measured number, and then by editing
  that section.
- Older chapters move to `docs/history.md` when the CHANGELOG stops being
  readable in one sitting.

**`README.md`** — a three-minute summary: what this is, one line per chapter,
setup and running, the layout, the likely next chapters. Rationale belongs in
the CHANGELOG. Update it whenever a chapter changes what the project is, how it
runs, or the layout.

**Docstrings and comments carry constraints, not stories.** A comment says the
non-obvious *why* in a line or two: a constraint, an API quirk, a format
gotcha, a deliberate trade-off. How it was found — the live session, the
measurement, the bug it replaced — goes in the CHANGELOG.

## 6. Verification and evidence

**Never claim something works without having observed it work in this session.**

- `uv run verify` is the gate: ruff, format, mypy (strict), pytest, and the
  page's node tests when node ≥ 22 is present.
- **Tapes** (`tests/tapes/`) replay whole conversations on virtual time against
  golden transcripts. A change to turn-taking shows up there as a diff: run
  `UPDATE_GOLDEN=1 uv run pytest tests/test_tapes.py`, read the diff, and say in
  the CHANGELOG what moved and why. A behaviour worth keeping gets a tape.
- **Unit tests are not enough for a voice agent**: clipped syllables, choppy
  playback, a 900 ms gap, echo, a pitched-up voice all pass every assertion.
  When a chapter touches the audio path, run it for real and write down what
  you observed. Unverified is reported as unverified.
- Tests never call real providers (fake at the interface; real calls are
  `@pytest.mark.network`) and never need audio hardware (`@pytest.mark.audio`).
  Latency assertions (`@pytest.mark.latency`) use generous ceilings.

## 7. Latency discipline

End-to-end target, user stops speaking → first audio out:

| Stage                        | Target (p50) |
| ---------------------------- | ------------ |
| Turn detection / endpointing | ≤ 250 ms     |
| STT final transcript         | ≤ 150 ms     |
| LLM first token              | ≤ 300 ms     |
| TTS first audio byte         | ≤ 150 ms     |
| Transport + playback         | ≤ 100 ms     |
| **End to end**               | **≤ 800 ms** |

**Stream everything** — never wait for a whole result when a partial one can
start the next stage. **Measure, do not assume** — a claimed speedup shows a
before and after. **Time to first byte beats throughput.** The numbers are a
hypothesis: revise them in a chapter, with a reason.

## 8. Code style and architecture

- Small modules under `src/voice_agent/`, one responsibility each. Type hints
  everywhere; mypy strict must pass; `# type: ignore` only with a code and a
  reason. Ruff owns formatting and lint.
- Plain functions and small classes over frameworks; a new dependency is
  justified in the CHANGELOG.
- **Async for I/O.** Never block the event loop: CPU work over ~1 ms goes to a
  thread. (The VAD, at ~0.1 ms a window, runs inline.)
- **Turn-taking decisions live in `Session._handle`.** Inputs are `events`
  posted to one inbox; a timer is an event that arrives later (`_after`). Add a
  case there rather than a callback or a task that decides something on its own.
- **One clock: `timing.now()`**, read through the module, never
  `time.perf_counter()` directly: the tapes swap it for their virtual loop's.
- **Prompts are data**: `prompts/` holds the system prompt, the rules around it
  (`rules.md`), the inner voice's instructions and the role cards. Code fills
  them in; it does not contain their wording.
- Providers live behind narrow protocols (`STT`, `LLM`, `TTS`); vendor SDK
  types never leak past their adapter. **Check live vendor docs before adapter
  code**: start from `docs/vendor/`, then the vendor's `llms.txt`.
- Keep audio formats explicit in names and docstrings (rate, channels,
  encoding, frame size): silent format mismatch is this domain's most common bug.

## 9. Setup and commands

```bash
uv sync                       # install, including dev tools
cp .env.example .env          # fill in only the keys you need
uv run voice-agent            # serve the agent (flags override VOICE_AGENT_* variables)
uv run verify                 # THE gate
uv run pytest                 # tests only (--cov for coverage)
uv run ruff check src tests   # lint; `ruff format` to format
uv run mypy                   # type check (strict)
```

Add dependencies with `uv add` (`--dev` for tooling). Never hand-edit `uv.lock`.

## 10. Security

- Never commit secrets: keys live in `.env` (gitignored), nowhere else.
- Everything the pipeline ingests — transcripts above all — is untrusted input
  that reaches a model. A transcript is not an instruction.
- **Audio is personal data.** Nothing persists audio; a chapter that does says
  where it goes, how long it lives and how it is deleted. Log metadata and
  timings freely, not raw audio or full transcripts by default.
- A chapter that lets the model act in the world (tools, transfers, payments)
  adds the confirmation boundary around it in the same chapter.

## 11. Commits

One chapter, one commit (or one tight series), including its CHANGELOG and
README, never failing `uv run verify`. Messages explain why. Commit only when
the user asks.
