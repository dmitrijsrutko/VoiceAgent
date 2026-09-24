# Roadmap — where this project could go

**This is a thinking document, not a commitment.** `AGENTS.md` §2 forbids
building ahead of the current chapter, and that rule outranks everything here.
Nothing below should be implemented because it appears below. It exists so
that when a chapter is chosen, the reasoning behind it is already written down
and the alternatives that were passed over are visible.

Distilled from material Dmitrijs gathered while researching real-time
conversational voice agents, plus notes and corrections of my own (§5). See
[CHANGELOG.md](../CHANGELOG.md) for what has actually shipped.

---

## 1. The arithmetic that drives everything

The end-to-end budget in `AGENTS.md` §7 is not an aspiration; it is the thing
every architectural choice is answerable to. Written as a sum, it shows where
the money actually goes:

| Design | Endpointing | LLM prefill | LLM → first clause | TTS first chunk | Total |
| --- | --- | --- | --- | --- | --- |
| **Naive cascade** | 700 ms (fixed VAD) | 200 ms | 300 ms | 200 ms | **~1.4 s** |
| **Optimized** | 200 ms (semantic) | ~30 ms (warm cache) | 120 ms | 150 ms | **~500 ms** |
| **+ speculation hit** | ~0 | ~0 | already decoding | 150 ms | **~250–300 ms** |

Three readings of this table matter more than the numbers:

1. **Endpointing is the largest single term in the naive design.** A fixed
   500–800 ms silence threshold dwarfs every optimization in the model layer.
   Shaving 150 ms off prefill while sitting on 700 ms of dead air is wasted work.
2. **The wins come from overlap, not from speed.** No stage gets meaningfully
   faster. Stages stop waiting for each other. That is an architectural
   property, which is why `AGENTS.md` §7 insists on it from the first chapter
   rather than as a later optimization.
3. **Speculation collapses the first two terms to zero** by paying money for
   latency — the only lever here that trades cost rather than complexity.

---

## 2. Themes

Each theme is worth several chapters. Roughly ordered by how early the project
would feel their absence.

### A. Turn-taking: knowing when the caller is finished

The naive mechanism is a fixed silence threshold and it is bad at both ends: too
short and the agent interrupts someone who paused to think; too long and it
feels sluggish. The threshold cannot be tuned to fix this, because the problem
is that silence duration is the wrong signal.

- **VAD** (voice activity detection) answers "is this frame speech?" — a
  20 ms-frame classifier. Options range from energy/spectral thresholds
  (G.729, GSM, WebRTC) through small DNNs (Silero, TEN VAD). VAD is necessary
  and nowhere near sufficient.
  *Measured here in Chapter 3:* 0.7 s cut users off mid-sentence and had the
  agent answer fragments; 1.5 s fixed that and pushed first audio to roughly
  3.7 s after the user stops speaking. Neither is acceptable, which is the
  point — the threshold has no correct value.
- **Semantic turn detection** answers the real question: "is this *utterance*
  finished?" A small classifier over the partial transcript plus prosodic
  features predicts P(turn complete). "I'd like to check my balance" is
  complete; "I'd like to check my" is not — at identical silence durations.
  Confident completions can fire at 100–200 ms; ambiguous ones fall back to the
  timeout. LiveKit's turn detector and Pipecat's Smart Turn are prior art;
  training one on your own call logs is tractable.
- **Evaluation vocabulary worth adopting**: FEC (front-end clipping), MSC
  (mid-speech clipping), OVER (noise held as speech after the utterance), NDS
  (noise detected as speech). These name the four ways turn detection fails,
  and a chapter that touches endpointing should report them rather than a
  single accuracy number.

### B. Barge-in: yielding instantly when interrupted

A human interrupts a human and is heard immediately. Anything slower reads as
the agent not listening. The loop:

```
[agent speaking] → mic captures → AEC strips the agent's own voice
                                → VAD on the cleaned signal
                                → speech confirmed → cancel synthesis,
                                   flush the audio queue, transition to listening
```

- **Acoustic echo cancellation comes first**, or the agent interrupts itself on
  its own output. In a browser this is largely free via `getUserMedia`
  constraints (`echoCancellation: true`); over telephony and in native clients
  it is real work.
- **The latency requirement is inverted here**: a VAD that takes >200 ms to
  confirm means the user talks over the agent for an uncomfortably long time.
  This argues for a *different, faster, more trigger-happy* detector than the
  one used for endpointing — the cost of a false positive (agent stops
  briefly) is far lower than the cost of a false negative (agent talks over
  the user).
- **False positives are the practical problem**: a cough, a door, a dog. Worth
  measuring against real recordings before tuning by intuition.
- *Built in Chapter 8, with the trigger deliberately naive:* the recognizer's
  first words stop the agent, measured at 0.85–1.6 s from speech onset — all
  recognizer lag. What was heard comes from ElevenLabs' per-character
  alignment and the samples the page actually played. The faster, trigger-happy
  detector argued for below is still to build.
- **The subtle part is state, not audio.** Conversation history must record
  *what the caller actually heard* — the truncated utterance — not what the
  agent intended to say. Getting this wrong means every subsequent turn
  reasons about a sentence that was never spoken. This is a correctness bug
  disguised as an audio bug, and it is the reason barge-in touches the
  conversation model and not just the player.

### C. Streaming and the critical path

The hard constraint that shapes the whole cascade: transformer inference splits
into **prefill** (process the prompt, build the KV cache) and **decode**
(generate autoregressively). Decode cannot begin until the prompt is final,
because every generated token attends over the entire prompt. There is no way
to feed tokens into a running generation. But *prefill can be done
incrementally*, and on a real agent — system prompt, history, retrieved
account data — prefill is the larger half of TTFT.

- **Warm the cache on finalized ASR segments.** Streaming ASR emits volatile
  hypotheses and finalized segments (`is_final`, stability scores,
  LocalAgreement-style confirmation). On each finalization, fire a
  prefill-only request. When end-of-turn fires, the prompt is
  `[cached prefix] + [last few volatile words]` and only a handful of tokens
  need prefilling. **Only ever warm on finalized text** — warming on a
  hypothesis the ASR later revises invalidates the cache from the divergence
  point and buys nothing.
- **Cache granularity decides whether this is worth doing.** Self-hosted
  engines (vLLM, SGLang) cache at block granularity (~16 tokens) with
  automatic prefix caching / RadixAttention, and incremental warming genuinely
  pays. Hosted APIs have minimum-prefix thresholds and explicit breakpoints:
  you get reliable hits on system prompt + history, but the growing partial
  transcript sits past the last breakpoint and is re-prefilled each time.
  Since that tail is short, little is lost — the win is having history cached,
  which is nearly free.
- **Prompt structure is the cache contract**: `system → tools → stable history
  → active transcript`. Any volatile element early in the prompt (a
  `Current time: HH:MM:SS` stamp, a session id) invalidates everything after
  it and forces a full prefill. Verify with the provider's cache-read token
  counters rather than assuming.
- *Built in Chapter 7, with a measured correction:* the TTS service's own chunk
  schedule, fed raw tokens, already cuts on word boundaries and carries context
  across cuts; our own clause cutting stays the fallback if cuts are heard.
  **Chunk TTS on clause boundaries, not sentences and not finer.** Splitting
  mid-clause costs prosody — the synthesizer needs lookahead to get intonation
  right, and a robotic first chunk is a worse trade than 80 ms more latency.

### D. Speculation: buying latency with money

*Built in Chapter 5, with two measured corrections to what follows.* The saving
is `min(head start, time-to-first-token)`, not the sum of the head start and the
network floor — the floor is paid inside the head start. And the gate matters
less than the recognizer: on turns under about three seconds this recognizer is
still emitting new words when the commit lands, so no threshold, however clever,
finds a head start that does not exist.

Once turn-completion probability exists, it can be spent. When P(complete)
crosses a threshold, start a full generation on the current transcript. If the
caller stops there, TTFT is effectively zero — audio is already being produced.
If they keep talking, cancel and discard. The threshold is the dial:
0.9 speculates rarely and wastes little; 0.6 wins more often and burns more
tokens. On an agent replacing human handling time, wasted decode tokens are cheap.

**One hard rule: never speculate anything with side effects.** If a turn might
trigger a tool call that writes state, sends a message, or moves money,
speculation stops at that boundary. Speculate the reasoning; gate the
execution on real end-of-turn.

### E. Cascade versus duplex

Everything above assumes a cascade. The alternative is a duplex
speech-to-speech model — audio tokens in, audio tokens out, no stage boundaries
(GPT Realtime, Gemini Live, Moshi). The model genuinely processes while the
user speaks and turn-taking becomes emergent rather than a state machine.

The trade is not latency versus quality, it is **naturalness versus control**.
A cascade keeps the text domain, and with it: inspecting the transcript before
it reaches the LLM, reliable tool calls, steerable responses, transcript-level
evaluation, and your own choice of voice — which matters a great deal if the
voice itself is the product. The honest design question is which failure mode
your product punishes harder. Worth building a duplex backend behind the same
interface eventually, purely to be able to A/B the two.

### F. Evaluation: golden conversations

A voice agent is nondeterministic across three models at once, and its
regressions are mostly not crashes: a product name starts being mispronounced,
the greeting picks up an odd intonation, a prompt tweak that improved refunds
quietly breaks escalation. No unit test catches any of that.

A golden suite is a fixed set of conversations replayed through the whole
pipeline on every model, prompt, or vendor change. In increasing difficulty:

1. **Transcript assertions** — right tool called, escalated when it should,
   refused when it should. Often LLM-as-judge over the transcript.
2. **Latency assertions** — time-to-first-audio stayed under budget. Easy to
   measure, easy to regress, routinely forgotten.
3. **Pronunciation regression** — a list of terms that matter (brand names,
   SKUs, "SEPA", account-number readback) with known-good audio, compared by
   ASR round-trip or audio distance. This is the voice-specific one.
4. **MOS sampling** — humans rating naturalness 1–5. Does not scale, so sample.
   Automated proxies (UTMOS, DNSMOS) catch gross regressions and are
   unreliable for fine judgments.

Golden sets only cover known failures. **Sampled review of real calls** finds
the rest — weighted toward escalations, long calls, low ASR confidence, and
mid-turn hangups. Human-rated *resolution* is the real quality metric;
everything else is a proxy for it.

### G. Cost, and why it belongs next to evaluation

Three vendors, three metering units, one number that matters:

| Stage | Metered by | Notes |
| --- | --- | --- |
| STT | audio seconds | includes silence unless gated on VAD |
| LLM | tokens | input dominates — full history resent every turn |
| TTS | characters | only what is actually spoken |

Levers, cheapest first: **cache the greeting and common answers** as
pre-synthesized audio (zero TTS cost *and* zero latency — usually the first
optimization anyone ships); **prompt-cache the system prompt and history**
(different mechanism, same instinct); **route with a small model** — intent
classification does not need a frontier model, so escalate to the big one only
for turns that need reasoning.

Every cost lever is a potential quality regression: a smaller router misses
edge cases, a cached answer goes stale, trimmed history makes the agent forget
what was said four turns ago. **The eval harness is what makes cost
optimization safe** — it is the brake that lets you drive the cost down fast.
That is why these are one theme and not two.

### H. Failure, and compliance as architecture

- **Never dead air.** A stage timeout degrades audibly — "let me get you to a
  person" — because silence on a phone call reads as a dropped call.
- **Acknowledgment sounds during thinking.** Perceived latency is design
  material, not only an engineering number.
- **Escalation carries context.** A handoff that makes the caller repeat
  themselves is worse than no agent.
- **Telephony drops resume with context** if the caller calls back.
- **Recording is regulated.** Two-party consent states, GDPR lawful basis (not
  merely disclosure), retention with actual deletion, PII redaction before a
  reviewer sees a transcript — and voice recordings are **biometric data**
  under several regimes, which raises the bar again. Disclosure belongs in the
  greeting, which makes it a UX decision as much as a legal one.

### I. The development loop is its own architecture problem

Every turn of iteration costs money and quota, and the pipeline has three
metered vendors in it. That is a design constraint on the *project*, not just
the product.

**This is not theoretical — it happened.** A month's 10,000 credits went in
about three days of building Chapters 2 to 4, and the single largest consumer
was not the agent talking but a keep-alive that streamed silence to a
duration-metered recognizer five times a second. Two lessons: anything that
sends audio on a timer is a spend decision, and a project whose inner loop
depends on a metered vendor will stop dead in the middle of a chapter.

**The free tiers do not survive a debugging session.** ElevenLabs' free plan is
10,000 credits per month; at 0.5 credits per character on Flash that is ~20,000
characters, or roughly 150–200 spoken replies of the length this agent
produces — and STT would draw from the same pool. It also requires attribution
and is non-commercial, so it is a prototype tool, not a shipping one.

**So run local backends for the inner loop and hosted ones for the demo.**
Candidates worth evaluating when the relevant chapter arrives:

- **STT**: `faster-whisper` (`small` or `distil-large-v3`) with Silero VAD.
  Real-time factor comfortably under 1.0 on CPU. `whisper-streaming` implements
  the LocalAgreement partial/final protocol, which is exactly what §2C's
  incremental-prefill work needs.
- **TTS**: Kokoro-82M or Piper. Faster than real time on CPU. Neither matches
  ElevenLabs on naturalness — which does not matter for measuring latency
  budgets or exercising barge-in logic, where the unit is milliseconds, not MOS.

The structural payoff is that this forces the provider interface to be real
rather than nominal: a local backend and a hosted one behind the same protocol,
swappable per run. That is the same shape §2E wants for A/B-ing cascade against
duplex, and it is the honest test of whether the `TTS` and future `STT`
protocols actually abstract anything.

**Other credit sources worth knowing**: Groq serves Whisper on a usable free
tier and is fast enough for streaming STT; Deepgram gives new accounts
substantial credit and its streaming STT with built-in endpointing is close to
a reference implementation for §2A; ElevenLabs runs a startup grant (12 months,
33M characters) aimed specifically at real-time voice agents, which is the
difference between a quota that constrains design and one that does not.

**A caution learned the hard way in Chapter 2**: scoped API keys can lack the
permissions that diagnostic endpoints need, and which stock voices a plan may
use is neither stable nor inferable from documentation. Probe capabilities
against the endpoint you actually call, and record what was verified rather
than what was documented.

---

### J. Cooperative interjection: the sparring partner

The agent stops only answering and starts leading. It drives, steers, asks the
sharper question, and cuts in when it's worth it. Background thinking lives in
`prompts/interjecting_agent_prompt.md`; this is the design it led to.

- **The relationship licenses the interruption.** The first role is a general
  sparring partner: the user sets a topic and a goal, and so opts in to being
  interrupted. Roles are data (prompt files) with a license, triggers, and an
  **assertiveness dial**. Patient: one "hm?" then wait. Normal: one "sorry—"
  then yield. Assertive: persist until the user stops. Firm, never insulting.
- **Self-repair first; false positives cost more than misses.** The cheap end of
  the ladder (backchannel, a marked "hm?", a recast) should carry most of the
  value. Hard interrupts are the exception.
- **Three loops at three speeds.** *Perceive* (32 ms: VAD floor state, Ch 16).
  *Think* (on each stable-prefix update: a dedicated fast model keeps running
  notes and at most **one pending thought**, with type, urgency 0–3, a
  one-line *why*, and a draft line, as in Inner Thoughts, Liu et al., CHI 2025).
  *Act* (deterministic code: urgency × opening × dial × budget → silent,
  backchannel, take the next opening, cut in, or yield). The thinker never
  decides timing and the actor never decides content. A separate lightweight
  turn controller with a style dial is also where the 2026 full-duplex work
  landed; even trained, interruptive-take F1 is ~0.6, so the controller stays
  simple and the dial stays explicit.
- **Sounding like a person cutting in.** Flash v2.5 has no audio tags, and
  `eleven_v3` has them (`[interrupting]`) but isn't real-time. So: a bank of
  v3 openers ("mm—", "wait, wait—", "sorry, can I stop you there—"),
  synthesised once and cached like the greeting. The body streams on Flash
  behind the opener, which hides its latency the way people do.
- **Ch 8 needs a new state.** "The user kept talking over my interjection" is not
  "the user interrupted my turn".
- **Offline evaluation** as the benchmarks do it (Full-Duplex-Bench, HumDial):
  scripted scenarios with labelled error windows. Fire inside the window, stay
  silent on clean scripts, and count thoughts computed but never spoken.

- **Roles are data.** A role card (`prompts/roles/`) holds the job, what is
  worth it, the moves and the assertiveness. The code holds timing, the move
  vocabulary and the safety floor. Built-in roles come first (Ch 17), then a
  start-screen picker, then roles written by users, which are untrusted text
  and can never lift the floor.
- **Learned in Ch 17:** asked to decide *when* as well as *what*, the thinker
  fires almost everywhere (false fires 9/10 on scripted clean pauses). Timing
  belongs to the actor, which knows the floor. The thinker should say how much
  a thought matters and whether it can wait.

Chapters: **16** ears that hear pauses (shipped) · **17** the inner voice,
thoughts shown but not spoken (shipped) · **18** cut in at a pause · **19** speak over,
with the dial · **20** lead: an agenda and a goal state for the whole session.

## 3. Candidate chapter order

Not a commitment — the order will change as earlier chapters teach us things.

**Shipped, in the order it actually happened** (see CHANGELOG): a voice (batched
TTS), ears (streaming STT with the vendor's endpointing), acting before the turn
ends (prefill warming), speculation, streaming synthesis, synthesis while the
reply is written, barge-in with what was heard, karaoke, and an LLM latency
bench with connections kept between turns. The original list put measurement
fifth and speculation eleventh; in practice instrumentation grew chapter by
chapter, and speculation came early because it was cheap to try.

**Where the time goes now**, measured: the recognizer takes ~1.0 s (AssemblyAI)
or 1.8–1.9 s (Scribe) from the end of speech, as the Chapter 16 VAD hears it, to
a committed turn, and 0.85–1.6 s to notice barge-in; time to first
token is ~0.5–0.9 s depending on provider; first audio after the first words is
164–313 ms; connection setup is ~0 per turn. Network hops are not a significant
term here (see §5).

**Next, in order:**

1. **The sparring partner** (§2J), chapters 17–20. The voice detector this
   list used to open with shipped in Chapter 16, on the server rather than in
   the page. VAD-triggered barge-in follows once echo is checked.
2. **Semantic turn detection** — our own end-of-turn decision from VAD silence
   plus transcript completeness, instead of waiting for the vendor's commit.
   Reports FEC / MSC / OVER / NDS. Builds on Chapter 16's VAD.
3. **Golden conversation suite** — replay scripted audio (Chapter 8's scripted
   client already does this) with latency and pronunciation gates.
4. **Cost accounting** — per-call cost sheet; small-model routing, gated by 3.
5. **Deployment / telephony transport** — 8 kHz mono, jitter, drops, resume;
   colocation and WebRTC; the server-in-the-media-path question.
6. **A duplex speech-to-speech backend** behind the same interface, to A/B
   against the cascade.

## 4. Build-order instinct worth keeping

Ship the happy path on a handful of intents with escalation as the safety net
*first*. Interruption handling and the eval harness come next. The intent long
tail follows resolution data. Test the core risk — do callers accept the agent
at all? — before building breadth.

## 5. Notes and corrections from my own reading

The source material is broadly sound. Points where I would qualify it:

- **`--enable-prefix-caching` is no longer needed on current vLLM.** Prefix
  caching is on by default in the V1 engine. Check the version rather than
  copying the flag.
- **Browser AEC is mostly a constraint, not a component.** `getUserMedia({audio:
  {echoCancellation: true, noiseSuppression: true, autoGainControl: true}})`
  gets you the browser's own AEC, which is good. The hard AEC problems start
  with telephony, speakerphones, and native clients.
- **Barge-in and endpointing want *different* detectors.** The material treats
  "VAD" as one component. It should be two, tuned in opposite directions:
  endpointing must avoid cutting off a thinking pause (favor false negatives);
  barge-in must yield fast (favor false positives). One shared threshold is a
  guaranteed compromise at both ends.
- **Cache-warming on hosted APIs is worth less than it reads** — and Chapter 4
  measured how much less. On DeepSeek it is ~60-90 ms on a conversation's first
  turn and **~10 ms after**, because the provider's cache is already 80-87%
  warm from the previous turn's own call. TTFT is dominated by a ~716 ms
  network-and-queue floor that no prefill trick touches. Do it for history if it
  is free; do not build a chapter around it expecting latency.
- **"First audio within 700 ms" and "≤ 800 ms end to end" are the same claim**
  measured from different points. Pick one definition, write it into the eval
  harness, and never quote the other.
- **Batched TTS is a larger term than it looks.** Measured live in Chapter 2 it
  was 658 ms of a 1,893 ms first-audio time — ~35%, entirely serial. An earlier
  estimate using a 151 ms stub understated it by 4× and produced the wrong
  conclusion about where the latency was. Stubs are not measurements.
- **`max_tokens=1` cache-warming costs a full prefill every time.** It is only
  a win if the warmed prefix is genuinely reused before it expires. Measure
  cache-read tokens before assuming it helps.
- **"Hop cost beats hop count" holds, and here the hops are already cheap.**
  A common ordering for latency work is: instrument every leg, colocate, keep
  connections warm, stream with zero buffering, take your server out of the
  media path — and only then model-layer work such as semantic endpointing.
  Measured from this project, TCP handshakes to DeepSeek, ElevenLabs, OpenAI and
  Anthropic all complete in 7–15 ms at nearby CDN edges, and the server is
  localhost to the browser, so the first four were nearly free or already true.
  (Keeping connections warm hid a real bug — every streamed call reconnected —
  which only per-call instrumentation found.) What remains is behind the edges:
  the recognizer's commit and the model's first token. That puts semantic
  endpointing *first* for this project, not last. The ordering is right for a
  deployed system whose server sits far from users or vendors, and it becomes
  relevant again with the telephony chapter.
- **Removing the server from the media path** (browser straight to the vendor
  with short-lived tokens) saves ~nothing on localhost and costs the server its
  view of the audio: no server-side VAD, recording or telephony, heard-text
  timing arriving at the browser instead, and partial transcripts relayed back
  for speculation. Production voice stacks mostly keep the agent in the media
  path but *at the edge*, next to the vendors, with WebRTC to the user.
