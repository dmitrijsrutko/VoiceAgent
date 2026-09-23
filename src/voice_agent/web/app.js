// Wiring: the socket, the messages it carries, and the page's two controls.
// Each module below owns one job; this file owns the state they share.

import { buildMic } from "./mic.js";
import { addMarks, spokenChars } from "./karaoke.js";
import { createPlayer } from "./player.js";
import { interrupted, listenStart, listenStop, playback, userMessage } from "./protocol.js";
import {
  add, begin, form, input, listen, meta, ms, mute, note, paintListening as paint, paintText, pinLast,
  setEnabled, start, startNotices, startStack, status, stick, wrap,
} from "./ui.js";

const key = location.pathname.split("/").pop();

let bubble = null;         // the agent reply still being written, if any
let reply = null;          // the latest agent bubble — whatever audio arrives is its voice
let live = null;           // the user bubble being transcribed into, if any
let listening = false;
let speaking = false;      // the agent's voice is audible
let cut = null;            // the reply the user talked over; its late audio is ignored until the next reply
let mic = null;            // { context, node, stream, rate } once built
let micRefused = false;    // the start click asked and was refused; not asked again unprompted

// Karaoke: per agent bubble, its text, each character's end time, and how far
// the voice has got. `shown` is fixed once the reply finishes (all of it) or
// is cut (what was heard); until then it follows the audio actually played.
const spoken = new WeakMap();  // bubble -> { text, ends, playedMs, shown }
const repaint = new Set();
let frame = 0;

function speechOf(el) {
  let s = spoken.get(el);
  if (!s) {
    // Only the text itself: `textContent` would include telemetry notes.
    const own = el.firstChild?.nodeType === Node.TEXT_NODE ? el.firstChild.data : "";
    s = { text: own, ends: [], playedMs: 0, shown: null };
    spoken.set(el, s);
  }
  return s;
}

function paintSpeech(el) {
  const s = speechOf(el);
  // Muted, nothing plays and nothing would light the words up: shown whole.
  // Decided here rather than by dropping marks, so unmuting mid-reply still
  // has the full timeline — marks skipped while muted misaligned every later one.
  const following = !mute.checked;
  paintText(el, s.text, s.shown ?? (following ? spokenChars(s.text, s.ends, s.playedMs) : s.text.length));
}

function schedule(el) {
  // Positions arrive every ~40 ms; the text is repainted at most once a frame.
  repaint.add(el);
  frame ||= requestAnimationFrame(() => {
    frame = 0;
    for (const bubble of repaint) paintSpeech(bubble);
    repaint.clear();
  });
}
let sampleRate = 16000;

// Not opened here. Opening the socket *is* starting the conversation — it is
// what makes the server greet, and start the clock that decides whether to
// speak into a silence — so it waits for someone to say they are ready.
let ws = null;

function sending() {
  return ws !== null && ws.readyState === WebSocket.OPEN;
}

function paintListening() { paint(listening, speaking); }

function setSpeaking(active, report = {}) {
  // Transition-guarded, and the only place `speaking` is assigned. A clip that
  // replaces another fires no `onended` for the one it replaced, so a naive
  // "tell the server on every play" leaks a hold the server never gets back —
  // and a stuck hold keeps the idle timer suspended with nothing said.
  if (speaking === active) return;
  speaking = active;
  if (sending()) ws.send(playback(active, report));
  paintListening();
}

const player = createPlayer({
  makeContext: (rate) => new AudioContext({ sampleRate: rate }),
  isMuted: () => mute.checked,
  onSpeaking: setSpeaking,
  onWaiting: (resume) => {
    status.textContent = "· click anywhere to hear the agent";
    document.addEventListener("pointerdown", () => {
      status.textContent = "";
      resume();
    }, { once: true });
  },
  // In the log, not the status line: the status is repainted on every change
  // of listening or speaking, which would wipe the one message explaining silence.
  onError: (err) => add("audio unavailable: " + err.message, "error"),
  onFinished: (s) => {
    if (s.bubble) {
      // Played to the end — or muted, with nothing to follow: all of it is out.
      const speech = speechOf(s.bubble);
      speech.shown = speech.text.length;
      schedule(s.bubble);
    }
    if (s.gaps && s.bubble) note(s.bubble, `⚠ ${s.gaps} gap${s.gaps > 1 ? "s" : ""} · ${ms(s.gapMs)} of silence mid-reply`);
  },
  onPosition: (bubble, playedMs) => {
    if (!bubble) return;
    // Less what is still on its way to the speaker, as for an interruption.
    speechOf(bubble).playedMs = playedMs - player.outputLatency() * 1000;
    schedule(bubble);
  },
});

function sendFrame(pcm) {
  // Full duplex: sent while the agent talks too, so the user can talk over it.
  // The browser's echo cancellation (requested in mic.js) is what keeps the
  // agent's own voice out of these frames.
  if (listening && sending()) ws.send(pcm.buffer);
}

// What the server wrote into the page (`server.with_facts`). Read here rather
// than waited for on the socket, because the start screen has to say what
// starting entails *before* anything is opened — and the socket is the thing
// being opened.
function servedFacts() {
  try {
    return JSON.parse(document.getElementById("facts").textContent) || {};
  } catch {
    return {};
  }
}

function notice(html) {
  const el = document.createElement("div");
  el.className = "notice";
  el.innerHTML = html;
  startNotices.appendChild(el);
}

const ENGINE_LABELS = { anthropic: "Anthropic", openai: "OpenAI", deepseek: "DeepSeek" };
const EARS_LABELS = { assemblyai: "AssemblyAI", elevenlabs: "ElevenLabs Scribe" };
const VOICE_LABELS = { elevenlabs: "ElevenLabs", openai: "OpenAI" };

function picked(group) {
  return startStack.querySelector(`input[name="${group}"]:checked`)?.value ?? "";
}

// One radio group. Only what the server offered: it lists what this deployment
// holds a key for, and a choice that cannot be built is not a choice. The
// default is drawn first, whatever order the server's registry keeps.
// `single` draws a group of one anyway — the voice, which is one of a kind
// today but belongs beside the other two parts of the stack.
function choose(group, label, options, describe, { single = false } = {}) {
  if (options.length < (single ? 1 : 2)) return;  // nothing to choose between; the notices say what it is
  const ordered = [...options].sort((a, b) => Number(!!b.default) - Number(!!a.default));
  const el = document.createElement("div");
  el.className = "pick";
  el.innerHTML =
    `<span>${label}</span>` +
    ordered.map((o) =>
      `<label><input type="radio" name="${group}" value="${o.name}"` +
      `${o.default ? " checked" : ""}>${describe(o)}</label>`).join("");
  startStack.appendChild(el);
}

// The languages line is rebuilt whenever the ears change, because that is the
// fact most worth knowing before choosing and it differs most between the two:
// Scribe hears 100 languages, AssemblyAI 18.
function describeEars(known) {
  const choices = known.choices?.stt ?? [];
  const ears = choices.find((o) => o.name === picked("stt")) ?? choices[0] ?? known.ears;
  const langs = ears?.languages ?? [];
  if (!langs.length) return "";
  return `It hears <strong>${langs.length}</strong> languages: <code>${langs.join(" ")}</code>.`;
}

// Both of these were dismissible notes in the log, said on connect. They are
// the two things somebody should know *before* they decide to begin, not a
// moment after — so they are now what the button is surrounded by.
function describe(known) {
  const engines = known.choices?.llm ?? [];
  const ears = known.choices?.stt ?? [];
  choose("llm", "Reasoning engine", engines,
    (o) => `${ENGINE_LABELS[o.name] ?? o.name} <small>${o.model}</small>`);
  choose("stt", "Ears", ears,
    (o) => `${EARS_LABELS[o.name] ?? o.name} <small>${o.languages.length} languages</small>`);

  // Drawn as a group of one, and never sent: the only other synthesis backend
  // this project has waits for the whole reply before it starts, which undoes
  // the two chapters spent getting the voice to begin before the reply is written.
  if (known.voice) {
    choose("tts", "Voice", [{ name: known.voice.provider, voice: known.voice.voice, default: true }],
      (o) => `${VOICE_LABELS[o.name] ?? o.name} <small>${o.voice.slice(0, 10)}</small>`,
      { single: true });
  }

  const heard = document.createElement("div");
  heard.className = "notice";
  const paintEars = () => { heard.innerHTML = describeEars(known); };
  paintEars();
  if (heard.innerHTML) startNotices.appendChild(heard);
  startStack.addEventListener("change", paintEars);

  if (!known.ears) notice("This agent has <strong>no microphone</strong> — typing only.");
  if (!known.voice) notice("This agent is <strong>silent</strong> — it will not speak.");
  if (known.recording) {
    notice(
      "This conversation is <strong>written down on the server</strong> — what is said, " +
      "what is typed, and how long each part took.<br>No audio is ever stored.");
  }
}

function connect() {
  // The stack rides with the socket, because opening it is what starts the
  // conversation. Ignored by the server on a reconnect, which keeps whatever
  // this conversation was started on.
  const stack = new URLSearchParams();
  for (const group of ["llm", "stt"]) {
    const value = picked(group);
    if (value) stack.set(group, value);
  }
  const query = stack.toString();
  ws = new WebSocket(
    `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/${key}` +
    (query ? `?${query}` : ""));
  ws.binaryType = "arraybuffer";
  ws.onmessage = onMessage;
  // The server's reason, when it gave one. A refusal at the door — no such
  // conversation, or too many of them at once — otherwise looks exactly like
  // the network dropping, which is the one thing it is not.
  ws.onclose = (event) => {
    status.textContent = "· disconnected";
    setEnabled(false);
    if (event.reason) add(event.reason, "note");
  };
  ws.onerror = () => { status.textContent = "· connection error"; };
}

function onMessage(event) {
  if (event.data instanceof ArrayBuffer) {
    // Binary frames only ever arrive between `audio_start` and `audio_end`.
    player.chunk(event.data);
    return;
  }

  const msg = JSON.parse(event.data);

  if (msg.type === "ready") {
    const voice = msg.voice ? `${msg.voice.provider} ${msg.voice.voice.slice(0, 10)}` : "silent";
    const langs = msg.ears && msg.ears.languages ? msg.ears.languages : [];
    const heard = langs.length ? ` · ${langs.length} languages` : "";
    const ears = msg.ears ? `🎤 ${msg.ears.provider} ${msg.ears.sample_rate / 1000}kHz${heard}` : "🎤 deaf";
    meta.textContent = `${msg.provider} · ${msg.model} · 🔊 ${voice} · ${ears} · ${msg.session.slice(0, 8)}…`;
    // The codes themselves on hover: too long for the bar, and the count alone
    // is enough to make someone look before they speak.
    meta.title = langs.length ? `heard: ${langs.join(" ")}` : "";
    sampleRate = msg.ears ? msg.ears.sample_rate : 16000;
    // Built before the socket at the rate the page expected. Should the server
    // disagree, a microphone at the wrong rate would pitch every word it sends.
    // The rate *asked for*, not the context's: a browser that does not honour
    // the request would otherwise reopen the microphone on every start.
    if (mic?.rate && mic.rate !== sampleRate) {
      mic.stream.getTracks().forEach((t) => t.stop());
      mic.context.close();
      mic = null;
    }
    if (msg.voice) player.setRate(msg.voice.sample_rate);
    // Enabled before the attempt, not after it: a microphone that was refused
    // or is busy must leave a way to try again, and the conversation carries
    // on by typing either way.
    listen.disabled = !msg.ears;
    // The whole point of the button that opened this socket — somebody who has
    // just said they are ready should not have to say it twice.
    // Unless the start click was just refused: asking again here put a second
    // prompt over the greeting, and a second "microphone failed" in the log.
    // The listen button is still there to try again.
    if (msg.ears && !micRefused) beginListening();
    for (const m of msg.history) add(m.content, "msg " + (m.role === "user" ? "user" : "agent"));
    if (msg.ended) { add("This conversation has ended.", "note"); setEnabled(false); }
    else setEnabled(true);

  } else if (msg.type === "greeting") {
    reply = add(msg.text, "msg agent");
    spoken.set(reply, { text: msg.text, ends: [], playedMs: 0, shown: null });

  } else if (msg.type === "reply_start") {
    cut = null;
    bubble = reply = add("", "msg agent cursor");
    spoken.set(bubble, { text: "", ends: [], playedMs: 0, shown: null });

  } else if (msg.type === "delta") {
    if (bubble) {
      speechOf(bubble).text += msg.text;
      stick(() => paintSpeech(bubble));
    }

  } else if (msg.type === "marks") {
    if (reply && !cut) {
      addMarks(speechOf(reply).ends, msg.from_ms, msg.ends_ms);
      schedule(reply);
    }

  } else if (msg.type === "reply_end") {
    if (bubble && msg.interrupted) {
      bubble.classList.remove("cursor");
      if (!bubble.textContent) bubble.remove();
      else if (!cut) note(bubble, "✋ interrupted before it was spoken");
    } else if (bubble && msg.initiative) {
      bubble.classList.remove("cursor");
      // Its own timings are zero by construction, not by measurement: the line
      // was written before this turn started, so there was nothing to wait for.
      // Printing "thought for 0 ms" put a number on screen that only looked
      // like one — what deciding actually cost is in the note above.
      note(bubble, `🗣 unprompted · rung ${msg.initiative} · ${msg.chars} chars · decided above`);
    } else if (bubble) {
      bubble.classList.remove("cursor");
      // Tokens only when the provider reported them: a missing count shown as
      // "0 tokens" would read as a measurement.
      const tokens = msg.output_tokens ? ` · ${msg.output_tokens} tokens` : "";
      // When the provider accepted the request splits a slow first token into
      // "on the way there" and "waiting on the provider". A guess was sent
      // before the turn began, so its accept time is on another clock.
      const accepted = msg.accepted_ms != null && !msg.speculated ? ` (accepted at ${ms(msg.accepted_ms)})` : "";
      // Only when unusual: a reused connection and one attempt are the norm.
      const connect = msg.connect_ms != null ? ` · 🔌 opened a connection first, ${ms(msg.connect_ms)}` : "";
      const retried = msg.attempts > 1 ? ` · ↻ sent ${msg.attempts}× — refused or dropped before` : "";
      note(bubble, `💭 thought for ${ms(msg.ttft_ms)}${accepted}${connect}${retried} · ${msg.chars} chars${tokens} in ${msg.fragments} fragments · ${ms(msg.generation_ms)}`);
      if (msg.prompt_tokens) {
        // What this turn resent as context, and how much the provider already
        // held — the number that grows every turn.
        const cachedPct = Math.round(100 * msg.cached_tokens / msg.prompt_tokens);
        note(bubble, `📥 ${msg.prompt_tokens} prompt tokens · ${msg.cached_tokens} cached (${cachedPct}%)`);
      }
      if (msg.speculated) {
        note(bubble, `⚡ started ${ms(msg.speculation_lead_ms)} before the turn ended — the reply was already being written`);
      }
      if (msg.speculations_discarded) {
        // The cost of guessing wrong, on screen rather than assumed.
        note(bubble, `🗑 ${msg.speculations_discarded} guess${msg.speculations_discarded > 1 ? "es" : ""} discarded · ${msg.speculation_wasted_chars} chars wasted`);
      }
    }
    bubble = null;
    setEnabled(true);

  } else if (msg.type === "interrupt") {
    // Silence first; everything else can wait. Frames carry no reply id, so
    // audio of this reply still in flight is ignored until the next one starts.
    cut = reply;
    player.stop((playedMs) => {
      if (!sending()) return;
      // Less what is still on its way to the speaker: pulled from the queue is
      // not the same as out of the speaker.
      const latency = (player.outputLatency?.() ?? 0) * 1000;
      ws.send(interrupted(msg.id, playedMs === null ? null : Math.max(0, Math.round(playedMs - latency))));
    });

  } else if (msg.type === "truncated") {
    if (cut) {
      speechOf(cut).shown = msg.heard_chars;
      schedule(cut);
      const guess = msg.estimated ? " (estimated)" : msg.timed ? "" : " (approximate)";
      note(cut, `✋ interrupted after ${ms(msg.played_ms)} · heard ${msg.heard_chars} of ${msg.chars} chars${guess} · stopped ${ms(msg.stop_ms)} after the interruption was noticed`);
    }

  } else if (msg.type === "audio_start") {
    if (cut) return;  // the rest of a reply the user talked over
    // Speech streams while the reply is still being written, so this can come
    // before `reply_end` — the text keeps arriving into the same bubble.
    player.start(reply);

  } else if (msg.type === "audio_end") {
    const voiced = player.end();
    if (voiced) {
      // The reply's own length matters as much as the time taken to make it:
      // a 31-second answer is a product problem no latency work can fix.
      const length = `${msg.seconds.toFixed(1)} s of speech · ${(msg.bytes / 1024).toFixed(0)} kB in ${msg.chunks} chunks`;
      if (msg.cached) {
        const origin = msg.synthesis_ms
          ? `synthesized at startup in ${ms(msg.synthesis_ms)}`
          : "reused from an earlier run";
        note(voiced, `🔊 ${length} · ${origin} · no wait for you`);
      } else {
        // "Before the reply was written" is the point of streaming the text in:
        // without it, speech waits for the last word the model writes.
        const early = msg.audio_before_reply_end ? " · spoke before the reply was written" : "";
        note(voiced, `🔊 ${length} · audio at ${ms(msg.first_audio_ms)}${early} · ${ms(msg.synthesis_first_byte_ms)} from first words to first sound · synthesized in ${ms(msg.synthesis_ms)}`);
      }
    }

  } else if (msg.type === "transcript") {
    if (!msg.text.trim() && !msg.final) return;
    if (!live) { live = add("", "msg user volatile"); pinLast(live); }
    stick(() => { live.textContent = msg.text; });
    if (msg.final) {
      live.classList.remove("volatile");
      let line = `🎙 committed ${ms(msg.endpoint_ms)} after your last recognised word`;
      if (msg.stable_words) {
        // How much of the turn was settled before it ended, and whether that
        // guess held. A false `prefix_held` means agreement acted on words you
        // never said, which is the only way this can be wrong.
        line += ` · ${msg.stable_words} words settled early${msg.prefix_held ? "" : " ⚠ prefix did not hold"}`;
      }
      note(live, line);
      if (!msg.warms && msg.warms_attempted) {
        // Billed and useless. Saying nothing here makes a wasted warm look
        // identical to no warm at all.
        note(live, `🔥 ${msg.warms_attempted} warm started but did not land before the turn ended`);
      }
      if (msg.warms) {
        const pct = msg.warm_prompt_tokens
          ? Math.round(100 * msg.warm_cached_tokens / msg.warm_prompt_tokens)
          : 0;
        note(live, `🔥 warmed ${msg.warms}× · ${msg.warm_cached_tokens}/${msg.warm_prompt_tokens} tokens already cached (${pct}%) · last one ${ms(msg.warm_lead_ms)} before the turn ended`);
      }
      live = null;
      pinLast(null);
    }

  } else if (msg.type === "transcript_dropped") {
    // Words the recognizer started transcribing and then took back. The bubble
    // is removed rather than left in place: it never became a turn, nothing
    // downstream ever saw it, and leaving it behind is worse than cosmetic —
    // the *next* utterance would be written into it, appearing wherever this
    // one was rather than at the end of the conversation.
    if (live) { stick(() => live.remove()); live = null; pinLast(null); }

  } else if (msg.type === "initiative") {
    // Every consideration, spoken or not. The declines are the point: an agent
    // that talks unprompted is easy, one that keeps deciding not to is the
    // thing being built, and it is invisible unless it is drawn.
    const quiet = `${(msg.quiet_ms / 1000).toFixed(0)}s quiet`;
    const cached = msg.cached_tokens ? `, ${msg.cached_tokens} cached` : "";
    const cost = msg.prompt_tokens
      ? ` · ${msg.prompt_tokens} in${cached}, ${msg.output_tokens} out`
      : "";
    const took = `decided in ${ms(msg.consider_ms)}`;
    const verdict = {
      spoke: "💬 had something to say",
      declined: "🤫 nothing worth saying",
      yielded: "🤫 had something, but you started talking",
      overran: "✂️ its answer ran on, so it went unsaid",
      // Never silent: a clock that fails without saying so looks exactly like
      // one that decided to stay quiet, and those are opposite facts.
      failed: `⚠️ could not decide — ${msg.message}`,
    }[msg.decision] || msg.decision;
    add(`${verdict} · ${quiet} · rung ${msg.rung}/${msg.rungs} · ${took}${cost}`, "note think");

  } else if (msg.type === "listening") {
    listening = msg.active;
    paintListening();
    if (msg.reason) add(msg.reason + " — press listen to resume", "note");

  } else if (msg.type === "listen_error") {
    listening = false;
    paintListening();
    add(msg.message, "error");

  } else if (msg.type === "audio_error") {
    // Possibly after `audio_end` when synthesis failed mid-reply, and possibly
    // before `reply_end`: the text carries on either way, so `bubble` is left
    // alone and the note goes on the reply whose voice this was.
    if (reply) note(reply, `🔇 ${msg.message}`);

  } else if (msg.type === "error") {
    if (bubble) { bubble.remove(); bubble = reply = null; }
    add(msg.message, "error");
    setEnabled(true);

  } else if (msg.type === "ended") {
    add(msg.reason || "Conversation ended. Reload to start a new one.", "note");
    setEnabled(false);
  }
}

// Shared by the button and by the start of a conversation, which want exactly
// the same thing. `sampleRate` is only known from the `ready` frame, so this
// can never run before one has arrived — which is why starting to listen is
// triggered there rather than in the click that opened the socket.
async function beginListening() {
  listen.disabled = true;
  try {
    player.resume();  // a gesture: the one moment autoplay is allowed
    if (!mic) mic = await buildMic(sampleRate, sendFrame);
    await mic.context.resume();
    ws.send(listenStart());
  } catch (err) {
    // In the log, not the status line: `paintListening` repaints that on every
    // change of listening or speaking, so the one message explaining why
    // nobody can be heard would be wiped by the next one.
    add("microphone failed: " + err.message, "error");
  } finally {
    listen.disabled = false;
  }
}

listen.onclick = () => (listening ? ws.send(listenStop()) : beginListening());

begin.onclick = async () => {
  // First, and with nothing awaited above it: this is the user gesture, and it
  // is the only moment the browser will allow audio to start. Everything else
  // here can happen a tick later; this cannot.
  const resumed = player.resume();
  start.remove();
  add("Just talk — it is already listening. Or type. Say or type “exit” to end.", "note");
  // Awaited *before* the socket opens, because the greeting follows it by about
  // 20 ms — comfortably fast enough to beat a resume that has been asked for
  // but has not finished. `player.chunk` would then see a context still reading
  // "suspended" and tell the user to click to hear audio that was already on
  // its way, which is the exact complaint this whole screen exists to end.
  // Swallowed rather than guarded: a refusal here is reported when the audio
  // actually fails, and must not cost the conversation.
  await resumed.catch(() => {});
  // The microphone is asked for before the conversation exists, not on `ready`.
  // Asked after, the permission prompt appeared while the greeting was already
  // playing — and on a phone nothing is heard behind that prompt, so the intro
  // was lost to it. Refused or failed, the conversation still starts: typing
  // works, and the listen button stays there to try again.
  const rate = earsRate(servedFacts());
  if (rate) {
    try {
      mic = { ...(await buildMic(rate, sendFrame)), rate };
    } catch (err) {
      micRefused = true;
      add("microphone failed: " + err.message, "error");
    }
    // Opening the microphone can make the system pause playback (iOS switches
    // its audio session); resuming again is free when nothing was paused.
    await player.resume().catch(() => {});
  }
  connect();
};

// The rate the chosen ears will ask for, so the microphone built before the
// socket opens is the one `ready` would have built. Null when there are none.
function earsRate(known) {
  const choices = known.choices?.stt ?? [];
  const ears = choices.find((o) => o.name === picked("stt")) ?? choices[0] ?? known.ears;
  return ears?.sample_rate ?? null;
}

describe(servedFacts());
setEnabled(false);

form.onsubmit = (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || !sending()) return;
  add(text, "msg user");
  // The words go first. Everything after is audio, and a browser that refuses
  // the output context (one without a requested sample rate) throws here —
  // which must cost the voice, never the message.
  ws.send(userMessage(text));
  input.value = "";
  setEnabled(false);
  try {
    player.dropUnheard();
    player.resume();  // still inside the gesture: the one moment autoplay is allowed
  } catch (err) {
    add("audio unavailable: " + err.message, "error");
  }
};

setEnabled(false);
