// Wiring: the socket, the messages it carries, and the page's two controls.
// Each module below owns one job; this file owns the state they share.

import { paintFloor, record as recordFloor } from "./floor.js";
import { buildMic, micFailure } from "./mic.js";
import { WARN_MS, countdown } from "./timer.js";
import { addMarks, spokenChars } from "./karaoke.js";
import { createPlayer } from "./player.js";
import { clientFacts } from "./client.js";
import {
  clientError, clientInfo, interrupted, listenStart, listenStop, playback, userMessage,
} from "./protocol.js";
import { chosenEars, servedFacts, showStart, stackQuery } from "./start.js";
import {
  audioLine, committedLine, gapsLine, initiativeLine, quietLine, replyLines, thoughtLine,
  truncatedLine, unpromptedLine,
} from "./telemetry.js";
import {
  add, begin, details, floor, form, input, listen, meta, mute, note, paintListening as paint, paintText,
  pinLast, setEnabled, start, status, stick, timer, wrap,
} from "./ui.js";

const key = location.pathname.split("/").pop();

let bubble = null;         // the agent reply still being written, if any
let reply = null;          // the latest agent bubble — whatever audio arrives is its voice
let live = null;           // the user bubble being transcribed into, if any
let listening = false;
let speaking = false;      // the agent's voice is audible
let cut = null;            // the reply the user talked over; its late audio is ignored until the next reply
let mic = null;            // { context, node, stream, rate } once built
const floorEvents = [];   // the floor by the server's VAD, for the strip
let quiet = null;          // { el, count } — the inner voice's run of declines, one line
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

// Page-side failures the server would never otherwise hear of — a refused
// microphone above all. One from the start tap happens before the socket
// exists, so reports wait for it.
const unsent = [];
function report(message) {
  if (sending()) ws.send(message);
  else unsent.push(message);
}

function micFailed(err) {
  add("microphone failed: " + micFailure(err), "error");
  report(clientError("microphone", err));
}

function paintListening() { paint(listening, speaking); }

// The time limit, counted from this connection like the server's own. Shown
// only in the last minute, with one note when it starts.
let deadline = null;
let warned = false;
setInterval(() => {
  if (deadline === null) return;
  const left = deadline - performance.now();
  const shown = countdown(left);
  timer.hidden = !shown.show;
  timer.textContent = shown.text;
  timer.classList.toggle("urgent", shown.urgent);
  if (shown.show && !warned && left > WARN_MS - 5_000) {
    warned = true;
    add("One minute left in this conversation.", "note");
  }
}, 250);

// The conversation is over, by the server or by the connection: nothing can
// be said into it any more. The microphone is let go — the browser's indicator
// goes off — and the way forward is a new conversation, not a reload (a reload
// reopens this one, ended).
let over = false;
function endConversation(why) {
  if (over) return;
  over = true;
  deadline = null;
  timer.hidden = true;
  listening = false;
  paintListening();
  listen.disabled = true;
  if (mic) {
    mic.stream.getTracks().forEach((t) => t.stop());
    mic.context.close();
    mic = null;
  }
  status.textContent = why === "ended" ? "· ended" : "· disconnected";
  setEnabled(false);
  const again = document.createElement("button");
  again.id = "again";
  again.type = "button";
  again.textContent = "Start a new conversation";
  again.onclick = () => { location.href = "/"; };
  stick(() => wrap.appendChild(again));
}

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
    if (s.gaps && s.bubble) note(s.bubble, gapsLine(s));
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

function connect() {
  // The stack rides with the socket, because opening it is what starts the
  // conversation. A reconnect keeps what the conversation started on.
  const query = stackQuery();
  ws = new WebSocket(
    `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/${key}` +
    (query ? `?${query}` : ""));
  ws.binaryType = "arraybuffer";
  ws.onmessage = onMessage;
  // The server's reason, when it gave one: a refusal at the door otherwise
  // looks exactly like the network dropping.
  ws.onclose = (event) => {
    if (event.reason) add(event.reason, "note");
    endConversation("disconnected");
  };
  ws.onerror = () => { status.textContent = "· connection error"; };
}

function onMessage(event) {
  if (event.data instanceof ArrayBuffer) {
    player.chunk(event.data);  // only ever between `audio_start` and `audio_end`
    return;
  }
  const msg = JSON.parse(event.data);
  handlers[msg.type]?.(msg);
}

function newReply(text, cls) {
  const el = add(text, cls);
  spoken.set(el, { text, ends: [], playedMs: 0, shown: null });
  return el;
}

function endBubble() {
  bubble.classList.remove("cursor");
  const done = bubble;
  bubble = null;
  return done;
}

function releaseLive() {
  live = null;
  pinLast(null);
}

// Every frame the server sends, by type.
const handlers = {
  ready(msg) {
    if (msg.budget_seconds) {
      deadline = performance.now() + msg.budget_seconds * 1000;
      warned = false;
    }
    // Once per socket: which browser this is, then anything that failed before it opened.
    ws.send(clientInfo(clientFacts(navigator.userAgent)));
    while (unsent.length) ws.send(unsent.shift());
    const voice = msg.voice ? `${msg.voice.provider} ${msg.voice.voice.slice(0, 10)}` : "silent";
    const langs = msg.ears?.languages ?? [];
    const heard = langs.length ? ` · ${langs.length} languages` : "";
    const ears = msg.ears ? `🎤 ${msg.ears.provider} ${msg.ears.sample_rate / 1000}kHz${heard}` : "🎤 deaf";
    const role = msg.role ? ` · 🎭 ${msg.role.name}` : "";
    meta.textContent = `${msg.provider} · ${msg.model}${role} · 🔊 ${voice} · ${ears} · ${msg.session.slice(0, 8)}…`;
    meta.title = langs.length ? `heard: ${langs.join(" ")}` : "";
    sampleRate = msg.ears ? msg.ears.sample_rate : 16000;
    // Compared with the rate the page *asked* for: a browser that ignores the
    // request would otherwise reopen the microphone on every start.
    if (mic?.rate && mic.rate !== sampleRate) {
      mic.stream.getTracks().forEach((t) => t.stop());
      mic.context.close();
      mic = null;
    }
    if (msg.voice) player.setRate(msg.voice.sample_rate);
    listen.disabled = !msg.ears;
    // Pressing start was saying "ready"; not again after a refused microphone,
    // which would put a second prompt over the greeting.
    if (msg.ears && !micRefused) beginListening();
    for (const m of msg.history) add(m.content, "msg " + (m.role === "user" ? "user" : "agent"));
    if (msg.ended) { add("This conversation has ended.", "note"); setEnabled(false); }
    else setEnabled(true);
  },

  greeting(msg) { reply = newReply(msg.text, "msg agent"); },

  reply_start() {
    cut = null;
    bubble = reply = newReply("", "msg agent cursor");
  },

  delta(msg) {
    if (!bubble) return;
    speechOf(bubble).text += msg.text;
    stick(() => paintSpeech(bubble));
  },

  marks(msg) {
    if (!reply || cut) return;
    addMarks(speechOf(reply).ends, msg.from_ms, msg.ends_ms);
    schedule(reply);
  },

  reply_end(msg) {
    setEnabled(true);
    quiet = null;
    if (!bubble) return;
    const done = endBubble();
    if (msg.interrupted) {
      if (!done.textContent) done.remove();
      else if (!cut) note(done, "✋ interrupted before it was spoken");
    } else if (msg.initiative) {
      note(done, unpromptedLine(msg));
    } else if (msg.resumed) {
      note(done, "↩ picked up where it was cut off: whatever cut in said nothing more");
    } else {
      for (const line of replyLines(msg)) note(done, line);
    }
  },

  interrupt(msg) {
    // Silence first. Frames carry no reply id, so this reply's audio still in
    // flight is ignored until the next reply starts.
    cut = reply;
    player.stop((playedMs) => {
      if (!sending()) return;
      // Less what is still on its way to the speaker.
      const latency = player.outputLatency() * 1000;
      ws.send(interrupted(msg.id, playedMs === null ? null : Math.max(0, Math.round(playedMs - latency))));
    });
  },

  truncated(msg) {
    if (!cut) return;
    speechOf(cut).shown = msg.heard_chars;
    schedule(cut);
    note(cut, truncatedLine(msg));
  },

  audio_start() {
    // Can come before `reply_end`: speech streams while the text is written.
    if (!cut) player.start(reply);
  },

  audio_end(msg) {
    const voiced = player.end();
    if (voiced) note(voiced, audioLine(msg));
  },

  transcript(msg) {
    if (!msg.text.trim() && !msg.final) return;
    if (!live) { live = add("", "msg user volatile"); pinLast(live); }
    stick(() => { live.textContent = msg.text; });
    if (!msg.final) return;
    live.classList.remove("volatile");
    note(live, committedLine(msg));
    quiet = null;  // the next run of declines starts below what was just said
    releaseLive();
  },

  transcript_dropped() {
    // Words the recognizer took back. Removed, or the next utterance would be
    // written into this bubble, wherever it sits.
    if (live) { stick(() => live.remove()); releaseLive(); }
  },

  initiative(msg) { add(initiativeLine(msg), "note think telemetry"); },

  floor(msg) { recordFloor(floorEvents, msg, performance.now()); },

  echo_ignored(msg) {
    const what = msg.stage === "final" ? "not answered" : "not an interruption";
    add(`🔁 heard its own voice (“${msg.text}”) — ${what}`, "note think telemetry");
  },

  resumed() {},  // the resumed reply carries its own note

  // One line per thought; declines update a single line until something is
  // worth saying, or it would bury the conversation.
  thought(msg) {
    if (msg.decision === "nothing" || msg.decision === "unchanged") {
      if (!quiet) quiet = { el: add("", "note think telemetry"), count: 0 };
      quiet.count += 1;
      const line = quietLine(quiet.count, msg);
      stick(() => { quiet.el.textContent = line; });
      return;
    }
    quiet = null;
    add(thoughtLine(msg), "note think telemetry");
  },

  listening(msg) {
    listening = msg.active;
    paintListening();
    // A new session starts its floor from nothing; the old one's last state
    // would otherwise keep being painted, growing, with nobody listening.
    if (!msg.active) { floorEvents.length = 0; floor.replaceChildren(); }
    if (msg.reason) add(msg.reason + " — press listen to resume", "note");
  },

  listen_error(msg) {
    listening = false;
    paintListening();
    add(msg.message, "error");
  },

  audio_error(msg) {
    // The text carries on; the note goes on the reply whose voice failed.
    if (reply) note(reply, `🔇 ${msg.message}`, "shown");  // a failure, not telemetry
  },

  error(msg) {
    if (bubble) { bubble.remove(); bubble = reply = null; }
    add(msg.message, "error");
    setEnabled(true);
  },

  ended(msg) {
    add(msg.reason || "Conversation ended. Start a new one below.", "note");
    endConversation("ended");
  },
};

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
    micFailed(err);
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
  // The microphone is asked for in the same tick, not after awaiting the
  // resume: WebKit (every iPhone browser) counts an await as the end of the
  // tap, and refuses a request made after it with NotAllowedError. Asked here,
  // before the conversation exists: asked on `ready`, the permission prompt
  // covered the greeting, and on a phone the intro was lost to it.
  const rate = chosenEars(servedFacts())?.sample_rate;
  const opening = rate ? buildMic(rate, sendFrame) : null;
  opening?.catch(() => {});  // reported below; not an unhandled rejection meanwhile
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
  // Refused or failed, the conversation still starts: typing works, and the
  // listen button stays there to try again.
  if (opening) {
    try {
      mic = { ...(await opening), rate };
    } catch (err) {
      micRefused = true;
      micFailed(err);
    }
    // Opening the microphone can make the system pause playback (iOS switches
    // its audio session); resuming again is free when nothing was paused.
    await player.resume().catch(() => {});
  }
  connect();
};

showStart(servedFacts());
setEnabled(false);

// Telemetry under every bubble, for whoever wants to see what each part cost.
// Remembered per browser; storage may be unavailable, and that only forgets it.
function showDetails(on) {
  document.body.classList.toggle("details", on);
  try { localStorage.setItem("details", on ? "1" : ""); } catch {}
}
try { details.checked = localStorage.getItem("details") === "1"; } catch {}
showDetails(details.checked);
details.onchange = () => showDetails(details.checked);

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

// The strip moves with time, not only with events: a pause grows while nothing
// arrives. Painted only while it can be seen.
setInterval(() => {
  if (floorEvents.length && details.checked) paintFloor(floor, floorEvents, performance.now());
}, 100);
