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
endpoint has VAD endpointing built in: connect with `commit_strategy=vad` and
`vad_silence_threshold_secs=0.7` and it emits a committed transcript after a
pause. That is this chapter's turn detection in its entirety — zero VAD code —
and it is the right naive step. It also has a cost that only became visible
once it ran, recorded under "Latency impact" below.

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
- `cli.py`: `--stt {elevenlabs,none}`.

**Design decisions**

- **Endpointing delegated to the STT vendor.** Simplest possible thing that
  works, and it works well. The cost, now that it has run: a second backend
  must supply its own endpointing, the semantic-turn-detection chapter has to
  take this back out rather than swap it, and — unexpectedly — *the ability to
  measure endpointing goes with it* (below).
- **A toggle, not always-on.** Continuous streaming bills a metered API for
  silence; on a free plan that is how a quota disappears overnight. Permission
  is requested on page load and the tracks immediately stopped, so the grant is
  remembered, the first press of listen is instant, and the recording indicator
  does not sit lit for the whole session.
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

**Latency impact**

Full live loop, nothing faked — a 1.65 s spoken question through Scribe,
DeepSeek and ElevenLabs:

| Stage | Measured |
| --- | --- |
| End of speech → committed transcript | **1,322 ms** |
| LLM first token | 782 ms |
| Full reply text (118 chars) | 1,111 ms |
| Synthesis | 512 ms |
| Turn start → first audio | 1,600 ms |
| **End of speech → first audio** | **~2.9 s** |

**The instrumentation was wrong and the live run caught it.** The server
reported `endpoint_ms=509`; the true figure, timed externally from the moment
the audio actually stopped, was **1,322 ms** — 2.6× larger. The server measures
from the last *partial transcript*, because without its own VAD it cannot see
when the user stopped talking; everything the recognizer spends lagging behind
the audio is invisible to it. The metric is now labelled for what it actually
measures ("committed 509 ms after your last recognised word") and the gap is
documented at the point of measurement.

That is the real lesson of delegating endpointing: **handing the decision to
the vendor also hands over the ability to measure it.** The roadmap says
endpointing is the largest term in a naive cascade, and at 1.3 s of the 2.9 s
total it is — but the project cannot currently see that number from the inside.

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

- `uv run verify` green: ruff, ruff format, mypy strict (30 files), 63 tests.
- Ten new listening tests with only the recognizer faked: volatile-then-committed
  sequencing, a committed transcript starting a turn by itself and arriving at
  the reasoning engine as an ordinary user message, volatile text *never*
  reaching it, an empty commit starting no turn, a spoken `exit` ending the
  conversation, stop-on-request, audio before listening starts being dropped
  rather than buffered, a failing recognizer leaving typing working, a deaf
  agent saying so, and the capture rate being advertised.
- **Verified live against the real Scribe realtime API**, end to end through the
  actual server with a real WebSocket client playing the browser's part —
  same 100 ms PCM16 frames, same rate, same half-duplex rules. Volatile
  transcripts revised themselves in flight (`'What is the tallest-'` →
  `'What is the tallest building in Riga?'`), the commit fired on the pause,
  DeepSeek answered, ElevenLabs spoke it, and 113 kB of audio came back down
  the socket.
- Scribe's realtime protocol was confirmed by connecting before any code was
  written — session config, `partial_transcript` / `committed_transcript`
  payloads, and the `input_audio_chunk` send shape all came from a live socket
  and the installed SDK's types rather than from documentation.

**Follow-up within this chapter: the recognizer hangs up on a quiet session**

Reported as "listening stops after 31 sec, I continue speaking, nobody
listens" — and this time with **no message at all**, which is what made it
different from every previous version of this complaint.

Measured against the real service rather than reasoned about: **Scribe closes a
realtime session after roughly 15 seconds with no audio, and closes it
*normally* — code 1000.** A normal close does not raise; iterating the socket
simply stops. So `stream()` returned, the transcript loop ended, `_run`
returned, and nothing happened: no exception, no error frame, `listening` still
true, the page still saying "listening", and the user's audio going into a
queue with nobody at the other end. The browser stops sending while a reply
plays, so **any answer longer than ~15 seconds killed the ears** — a 31.7 s
monologue guaranteed it.

Two fixes, one for the cause and one for the class of failure:

- **The gap gets filled with silence.** `Mic` now feeds the recognizer a frame
  of silence whenever the browser has sent nothing for 200 ms, so a session
  never goes quiet enough to be hung up on. Injected frames are counted
  separately from real ones, so "the browser sent no audio" stays a diagnosis
  rather than being papered over by the silence sent on the browser's behalf.
  This is metered audio, but a microphone that stayed open would have cost the
  same.
- **A stream that ends while we still hold the microphone is a reconnect, not
  an ending.** Up to three times, each announced to the page; after that,
  listening stops with a reason. The rule underneath: *silent* failure is the
  worst outcome available in this system, and it has now been the cause of
  three separate reports.

Writing the reconnect surfaced a latent deadlock: `_run` now calls `stop()`,
and `stop()` awaited `self._task` — which *is* `_run`. It waited on itself for
five seconds and then cancelled the coroutine that was trying to announce why
listening had ended. Found by a test, not by reading.

**Verified live end to end**: a real Scribe session, a real server, a browser
stand-in that speaks, goes silent for 40 seconds, then speaks again. The second
utterance was transcribed. The same script against the unfixed service confirms
the close at ~18 s. The earlier run of it also demonstrated the new expiry
wording doing its job — it reported *"the browser sent no audio"*, not "no
speech", correctly declining to blame the user.

**Follow-up within this chapter: the agent blamed the user for its own monologue**

A reply came back as **491 kB — 31 seconds of speech** — and the session then
died with *"listening stopped — no speech for 30s"*, while the user was in fact
talking. The browser mutes its microphone for the whole of a reply, so the user
could not be heard during it; the timeout then reported that as their silence.

The previous follow-up had a mechanism for exactly this: the browser reports
when playback ends and the server holds expiry open meanwhile. **The cause
could not be determined from the evidence** — "the playback message never
arrived" and "audio frames stopped flowing" produce an identical timeline — and
that ambiguity is itself the defect. The fix is therefore not another patch to
the same mechanism but the removal of its dependency:

- **A clip now carries its own duration**, computed by the adapter that knows
  its format (constant-bitrate MP3, so duration follows from size).
- **The server pushes its idle clock past the end of the audio it just sent**
  (`Mic.expect_silence`). It no longer needs the browser to tell it that the
  agent is talking — it already knows, because it did the talking. The
  browser's playback messages remain as a second signal for pause and mute, but
  nothing load-bearing depends on them now.
- **The expiry message distinguishes two failures that wore the same words**:
  "no speech for 30s" when audio was arriving and was quiet, and "the browser
  sent no audio" when nothing was arriving at all. Only one of those is the
  user's doing, and telling them apart is what makes the next report of this
  diagnosable instead of a guess.

The reply's length is now shown alongside its size — `🔊 31.4 s of speech ·
491 kB · synthesized in 941 ms` — because a 31-second answer is a product
problem that no amount of latency work will fix, and it was invisible.

Both mechanisms were sabotage-checked: removing `expect_silence` or collapsing
the two expiry reasons fails a test in under a second.

**Follow-up within this chapter: the browser client had no tests, and it showed**

Fixing the playback-hold leak broke the page completely. The edit replaced a
span of the client identified by its start and end text — and the span between
`tellPlayback` and the WebSocket setup contained not just those two functions
but **the entire microphone block**: the AudioWorklet, `warmUpMicPermission`
and `buildMic`. The `ready` handler still called `warmUpMicPermission()`, threw
a `ReferenceError` on the first message from the server, and never reached
`setEnabled(true)`. Every control on the page stayed disabled.

**`uv run verify` passed.** Ninety-odd tests, mypy strict, and none of it
touches a line of JavaScript — and the browser client is now a third of this
chapter's surface area: audio capture, format conversion, the half-duplex gate,
playback lifecycle, and transcript rendering all live there and have no
server-side counterpart to notice their absence.

Added `tests/test_web_client.py`: static checks on the page for the failures
that have actually occurred, not a substitute for running it.

- **Every function the script calls must be defined.** This is the one that
  matters; deleting a definition while leaving its call sites is invisible to
  everything else. Verified by re-deleting the block: it reports
  `called but never defined: ['buildMic', 'warmUpMicPermission']`.
- Every element `getElementById` reaches for must exist in the markup.
- The capture pipeline's six required pieces are present.
- `!speaking` is still in the microphone send path — without it the agent
  transcribes its own voice.
- `speaking` is assigned in exactly one place outside its declaration, which is
  the invariant the leaked-hold fix depends on.

Two notes on method. The first two versions of the "every function is defined"
check failed on `async` and on `let speaking = false` — false positives in the
test, not the page; a checker whose failures are mostly noise gets ignored,
which is worse than not having one. And one of the fixes for that *silently did
not apply*, because `ruff format` had reflowed the set it was matching against
— the same class of mistake as the original bug. Edits to source now assert
that they changed something.

**Follow-up within this chapter: the agent went deaf silently, and spoke German**

A second real conversation surfaced two more defects, one of them the worst
failure mode this project has produced.

**1. A leaked playback hold muted the microphone permanently, with no message.**
`player.pause()` fires no `onended`, so when one clip replaced another the
browser sent *two* holds and *one* release. The previous follow-up had made the
hold a counter, so it stuck at 1 and never returned to zero. From there:
the idle timer never fired, the watchdog skipped **every** check while held so
the 5-minute cap could not save it either, and the browser's own `speaking`
flag stayed true so it stopped sending microphone frames. The agent was deaf
for the rest of the session and said nothing about it — the observable
behaviour was simply that talking stopped working.

Three fixes, deliberately layered, because the previous single-mechanism fix is
what failed:

- **Holds are now named, not counted** (`hold("playback", …)`,
  `hold("turn", …)`). A set is idempotent, so a client that miscounts cannot
  wedge the server. The counter was chosen last round because two holds
  overlap; that was right, and unforgiving.
- **The hard cap is checked before the hold and is not pausable.** A cap that a
  stuck hold can defeat is not a cap, and a stuck hold is exactly what it most
  needs to catch.
- **A hold expires after 60 s** (`MAX_HOLD_SECONDS`). Nothing legitimate holds
  that long — the longest reply so far was 18 s of audio — so a hold lost to a
  closed tab or a missed browser event becomes a one-minute hiccup instead of a
  permanent silent failure.
- The browser side is transition-guarded, and `speaking` is now assigned in
  exactly one place.

**These tests hung instead of failing.** Written through the WebSocket, a
broken expiry means "the frame never arrives", which blocks the suite rather
than failing a test — the first run took 62 seconds and *passed*, because the
60 s safety net eventually rescued it. They now drive `Mic` directly with a
deadline on every wait, and all three fail in under a second when the mechanism
they cover is removed. **A timing test that can hang is not a test; it is a
timeout with an opinion.**

**2. The agent answered in German for eleven turns.** The conversation opened
with `"Hallo."` — a word shared by English and German, and a *guess* by a
recognizer with no language hint. DeepSeek replied in German, and every
subsequent turn carried its own German replies in context, so it stayed there
even as the user wrote in English, and eventually claimed it had been answering
in English when it had not.

`prompts/system_prompt.md` had **no language rule at all**. It now says: answer
in the language of what the user *just said*, not of what you said last; a
greeting is not evidence, because "Hallo"/"Hi"/"Ciao"/"Salut" are shared and a
transcribed one is a spelling guess; and if you have already answered in the
wrong language, just switch without explaining. That the model's own prior
turns can anchor it against the user's actual language is a prompt-design
failure, not a model one — and worth noting that a *text* chat would have
recovered on the second turn. Speech made it sticky, because there was no
typed word to contradict the guess.

**Follow-up within this chapter: 0.7 s was the wrong number**

The pause that ends a turn is now **1.5 s**, Scribe's own default, up from the
0.7 s this chapter shipped with. Configurable via `VOICE_AGENT_VAD_SILENCE` /
`--vad-silence`, because no fixed value is right.

0.7 s was chosen on the reasoning that 1.5 s "is an eternity in conversation".
It is. It is also better. In a real conversation 0.7 s committed
`"Or rather..."` and `"Not Ethereum, but rather..."` as finished turns, and the
agent dutifully answered the fragments — *"Go ahead, take your time"* is the
agent replying to half a sentence. Being interrupted mid-thought reads as the
agent not listening; waiting an extra beat merely reads as slow.

**This makes the latency worse, and that is the honest trade.** Endpointing was
already the largest single term in the round trip — 1.3 s of a 2.9 s
end-to-end — and this adds ~0.8 s to it, putting first audio somewhere near
3.7 s after the user stops speaking. Two chapters of measurement now agree on
the same conclusion from opposite directions: the number cannot be tuned into
correctness, because "checking my balance" and a mid-sentence pause are
indistinguishable by duration alone. Semantic turn detection is not a
refinement to schedule eventually; it is the only thing that resolves this, and
it is now the most valuable unbuilt chapter.

**Follow-up within this chapter: three bugs a real conversation found**

A seven-round spoken conversation about the Riemann hypothesis ended itself.
Three separate defects, all invisible to the tests that existed.

1. **Playback time was billed to the user's idle budget.** The previous
   follow-up paused the expiry timer "while a turn is in progress" — but a turn
   ends when the audio *bytes are sent*, not when they finish playing. The last
   reply was 235 kB of MP3, which at 128 kbps is **15 seconds of speech**. The
   browser is muted for all of it (half-duplex), so no transcript can arrive;
   15 s of the agent talking plus ~15 s of the user listening and thinking hit
   the 30 s window exactly. Fixed by having the browser report playback start
   and end — only it knows when a clip actually finishes — and by making the
   hold a **counter rather than a flag**, because the turn's hold and the
   playback's hold overlap and a boolean lets the turn's exit clear the
   playback's.

   The lesson is narrower than "test more": the previous fix was verified
   against a *short* reply. Every test clip was a few hundred bytes, so the
   playback window was always shorter than the idle window and the bug could
   not appear. The regression test now uses a clip long enough to matter.

2. **An orderly shutdown was reported as a failure.** At the end of every
   listening session the user saw `elevenlabs transcription failed: sent 1000
   (OK); no close frame received`. When the audio ends the adapter commits,
   waits, and closes — and ElevenLabs never answers the close frame, so the
   library raises. Now suppressed when the close was ours.

   **This one resisted testing twice.** A test against the `websockets`
   library's own server passed with the fix removed, because a compliant server
   politely completes the handshake and the bug cannot occur. A second attempt
   passed too, because the check had been written in two places and only one
   was sabotaged — which is itself a finding: the inner handler was redundant
   and has been deleted. The test now drives the branch through a stub peer
   that never answers a close, reproduces the user's exact error string, and
   was confirmed to fail without the fix.

3. **The chat log stopped scrolling.** `#log` is a flex child with
   `overflow-y: auto` but no `min-height: 0`, so it defaulted to
   `min-height: auto`, grew to fit its content instead of scrolling, and pushed
   the conversation off the bottom of the page. One line, and a comment saying
   why it is load-bearing.

Also: a committed transcript with no words in it is no longer reported at all.
A session's closing flush produces exactly that, and it was putting an empty
bubble and a meaningless endpointing figure on screen.

**On the sabotage checks.** Three of this chapter's regression tests passed
with the code they exist to protect removed. All three are now verified to fail
without it. A negative or absence-based assertion is worth nothing until it has
been seen to fail, and this chapter is the evidence: the ratio was three out of
three.

**Follow-up within this chapter: listening expires**

An open microphone is a metered resource — Scribe bills for streamed silence,
and a backgrounded tab keeps the AudioWorklet running — so a listening session
now closes itself.

- **30 s with no speech**, and a **5 minute** hard cap regardless of activity.
  The cap catches what the idle timer cannot: a room producing continuous
  partials (a television, a nearby conversation). Scribe enforces its own
  session limit anyway; better to hit ours, with an explanation.
- **"Inactivity" means no *speech*, not no *audio*.** The microphone streams
  silence continuously, so frames never stop arriving. The signal that nobody
  is talking is the absence of partial transcripts.
- **The watchdog pauses while a turn is in progress.** During the agent's own
  reply the browser stops sending audio (half-duplex), so no transcript *can*
  arrive; an unpaused timer would blame the user for the agent talking. The
  moment the turn ends counts as fresh activity — the user has just been given
  something to respond to and should get the full window to do it.
- Server-side rather than in the browser: that is where the cost is incurred,
  it is authoritative, and it still protects a backgrounded tab.
- `Mic` now announces its own state changes, so an expiry and an explicit stop
  reach the browser through exactly one path and the page can explain why
  listening stopped.

**A vacuous test, caught by sabotage.** The first version of the
"agent talking doesn't count against the user" test passed *with the pause
disabled*. An expiry that fires mid-turn cannot announce itself until the turn
releases the microphone, so a test reading only the frames up to `reply_end`
never sees it. The test now asserts *after* the turn — that the session is
still listening — and was verified to fail when `Mic.busy` is removed. A
negative assertion that has never been seen to fail is not evidence of
anything.

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
- `cli.py`: `--tts {elevenlabs,openai,none}` and `--voice`.
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

**Follow-up within this chapter: the reasoning stage reports itself too**

Chapter 2 made the synthesis cost visible in the UI and that turned out to be
the most useful thing in it — so the reasoning stage now does the same. Each
reply carries two annotations:

```
💭 thought for 853 ms · 153 chars in 422 ms
🔊 155 kB · synthesized in 524 ms · audio at 1.8 s
```

- `reply_end` gained `ttft_ms`, `generation_ms` and `chars`; the `audio` frame
  gained `total_ms`.
- **The reasoning number is split at the first token on purpose.** The two
  halves mean different things: `ttft_ms` is dead air the user actually
  experiences and is the number §7 budgets, while `generation_ms` is throughput
  that streaming already hides behind text appearing on screen. A single
  "reply took N ms" would blur the one that matters into the one that does not.
- **`total_ms` is send-to-first-audio** — the number the whole project is
  judged on, and previously the only one that had to be reconstructed by hand
  from a stopwatch script rather than read off the screen.
- An empty reply reports its whole duration as time-to-first-token rather than
  as zero of everything, so a turn where the user waited and got nothing looks
  like a failure instead of an instant success. Tested.
- Fixed a fixture bug found while testing that: the fake reasoning engine
  streamed one blank fragment for an empty reply instead of streaming nothing.

Measured live on two real turns (DeepSeek + ElevenLabs) after the change:
853/909 ms to first token, 524/505 ms synthesis, **1.8 s and 1.9 s to first
audio** — consistent with the earlier one-off measurement, and now readable
without instrumenting anything by hand.

**Follow-up within this chapter: the default voice was wrong**

The first live attempt failed with `402 paid_plan_required` — "Free users
cannot use library voices via the API". The interesting part is not the fix but
what the diagnosis showed:

- The project's key is a **scoped** key lacking `voices_read` and `user_read`,
  so the standard advice — list `/v1/voices` and pick one with
  `category == "premade"` — **cannot run at all**. Diagnosis had to go through
  the synthesis endpoint itself, probing candidate ids two characters at a time.
- **Rachel (`21m00Tcm4TlvDq8ikWAM`) is blocked on a free account**, despite being
  the most widely cited "safe stock voice" — and so is **Aria
  (`9BWtsMINqrJLrRacOk9x`)**, which is ElevenLabs' own current in-app default.
  Verified working on the same key: Sarah, Brian, George, Laura, Bill, Adam.
- So the real defect was not the id but the *assumption*: which voices a plan
  can use is not stable and is not inferable from documentation. The default is
  now `EXAVITQu4vr4xnSDxMaL` (Sarah), chosen because it was empirically
  verified, with the full probe result recorded in the source and a test
  asserting the default never drifts back to Rachel or Aria.
- Added `explain()`, which turns an ElevenLabs `ApiError` into one actionable
  line. The SDK's exception stringifies the entire HTTP response — every
  header, the trace id, the CORS policy — and that was being rendered verbatim
  in the browser. It now says what happened *and* what to do: which stock voice
  to try, or that the key is missing a permission that must be enabled in the
  dashboard.
- Once the key was granted `voices_read`, `/v1/voices` confirmed the diagnosis
  authoritatively: the account sees 21 `premade` voices, Sarah among them, and
  neither Rachel nor Aria is in that set. The empirical probe and the catalogue
  agree.
- **Added `list_voices()` to the `TTS` protocol and `--list-voices` to the
  CLI**, so this question never has to be answered by probing again. It is not
  a convenience: a wrong voice id fails at *synthesis* time with a payment
  error rather than at startup, so a backend has to be able to say what an
  account may actually use. ElevenLabs answers it with an API call and marks
  Voice Library and cloned voices `usable=False` — listing everything the API
  returns would reproduce the original bug in a new place. OpenAI answers it
  from a fixed table with no call at all, and that asymmetry is precisely why
  the method belongs on the backend rather than in one shared helper.

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
