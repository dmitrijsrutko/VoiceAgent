// Wiring: the socket, the messages it carries, and the page's two controls.
// Each module below owns one job; this file owns the state they share.

import { buildMic, warmUpMicPermission } from "./mic.js";
import { createPlayer } from "./player.js";
import { listenStart, listenStop, playback, userMessage } from "./protocol.js";
import {
  add, form, input, listen, meta, ms, mute, note, paintListening as paint, setEnabled, status, stick,
} from "./ui.js";

const key = location.pathname.split("/").pop();

let bubble = null;         // the agent reply still being written, if any
let reply = null;          // the latest agent bubble — whatever audio arrives is its voice
let live = null;           // the user bubble being transcribed into, if any
let listening = false;
let speaking = false;      // half-duplex gate: don't let the agent hear itself
let mic = null;            // { context, node, stream } once built
let sampleRate = 16000;

const proto = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(`${proto}//${location.host}/ws/${key}`);
ws.binaryType = "arraybuffer";

function paintListening() { paint(listening, speaking); }

function setSpeaking(active, report = {}) {
  // Transition-guarded, and the only place `speaking` is assigned. A clip that
  // replaces another fires no `onended` for the one it replaced, so a naive
  // "tell the server on every play" leaks a hold the server never gets back —
  // and a stuck hold means the microphone is muted forever with nothing said.
  if (speaking === active) return;
  speaking = active;
  if (ws.readyState === WebSocket.OPEN) ws.send(playback(active, report));
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
    if (s.gaps && s.bubble) note(s.bubble, `⚠ ${s.gaps} gap${s.gaps > 1 ? "s" : ""} · ${ms(s.gapMs)} of silence mid-reply`);
  },
});

function sendFrame(pcm) {
  // Half-duplex: while the agent is talking, its own voice is reaching this
  // microphone. Drop those frames rather than transcribing the agent.
  if (listening && !speaking && ws.readyState === WebSocket.OPEN) ws.send(pcm.buffer);
}

async function prepareMic() {
  try {
    await warmUpMicPermission();
    listen.disabled = false;
  } catch (err) {
    listen.title = "microphone blocked: " + err.name;
    status.textContent = "· microphone unavailable";
  }
}

ws.onmessage = (event) => {
  if (event.data instanceof ArrayBuffer) {
    // Binary frames only ever arrive between `audio_start` and `audio_end`.
    player.chunk(event.data);
    return;
  }

  const msg = JSON.parse(event.data);

  if (msg.type === "ready") {
    const voice = msg.voice ? `${msg.voice.provider} ${msg.voice.voice.slice(0, 10)}` : "silent";
    const ears = msg.ears ? `🎤 ${msg.ears.provider} ${msg.ears.sample_rate / 1000}kHz` : "🎤 deaf";
    meta.textContent = `${msg.provider} · ${msg.model} · 🔊 ${voice} · ${ears} · ${msg.session.slice(0, 8)}…`;
    sampleRate = msg.ears ? msg.ears.sample_rate : 16000;
    if (msg.voice) player.setRate(msg.voice.sample_rate);
    if (msg.ears) prepareMic();
    for (const m of msg.history) add(m.content, "msg " + (m.role === "user" ? "user" : "agent"));
    if (msg.ended) { add("This conversation has ended.", "note"); setEnabled(false); }
    else setEnabled(true);

  } else if (msg.type === "greeting") {
    reply = add(msg.text, "msg agent");

  } else if (msg.type === "reply_start") {
    bubble = reply = add("", "msg agent cursor");

  } else if (msg.type === "delta") {
    if (bubble) stick(() => { bubble.textContent += msg.text; });

  } else if (msg.type === "reply_end") {
    if (bubble) {
      bubble.classList.remove("cursor");
      // Tokens only when the provider reported them: a missing count shown as
      // "0 tokens" would read as a measurement.
      const tokens = msg.output_tokens ? ` · ${msg.output_tokens} tokens` : "";
      note(bubble, `💭 thought for ${ms(msg.ttft_ms)} · ${msg.chars} chars${tokens} in ${msg.fragments} fragments · ${ms(msg.generation_ms)}`);
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

  } else if (msg.type === "audio_start") {
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
    if (!live) live = add("", "msg user volatile");
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
    }

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
    add("Conversation ended. Reload to start a new one.", "note");
    setEnabled(false);
  }
};

ws.onclose = () => { status.textContent = "· disconnected"; setEnabled(false); };
ws.onerror = () => { status.textContent = "· connection error"; };

listen.onclick = async () => {
  if (listening) {
    ws.send(listenStop());
    return;
  }
  listen.disabled = true;
  try {
    player.resume();  // a gesture: the one moment autoplay is allowed
    if (!mic) mic = await buildMic(sampleRate, sendFrame);
    await mic.context.resume();
    ws.send(listenStart());
  } catch (err) {
    add("microphone failed: " + err.message, "error");
  } finally {
    listen.disabled = false;
  }
};

form.onsubmit = (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || ws.readyState !== WebSocket.OPEN) return;
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
