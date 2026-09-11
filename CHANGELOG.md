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
  signal at a *real* generation instead of a discarded one is worth an estimated
  1.0-1.7 s. This chapter is that change with the payoff switched off, which
  makes the next one a small, measured delta rather than a leap.
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
  real one, so the 1.0-1.7 s is left on the table on purpose.
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
- Blank committed transcripts are not reported at all.
- An orderly close of the recognizer socket is no longer reported as a failure.
- `stop()` no longer deadlocks when called from the task it waits on.
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
