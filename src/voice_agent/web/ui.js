// The chat log and controls: appending, scrolling, and enabling.

export const wrap = document.getElementById("wrap");
export const log = document.getElementById("log");
export const form = document.getElementById("form");
export const input = document.getElementById("input");
export const send = document.getElementById("send");
export const meta = document.getElementById("meta");
export const status = document.getElementById("status");
export const mute = document.getElementById("mute");
export const listen = document.getElementById("listen");

const NEAR_BOTTOM_PX = 48;

export function stick(mutate) {
  // Whether we were at the bottom has to be read *before* the change, since
  // the change is what moves the bottom. And only then: yanking someone back
  // down while they are scrolled up reading earlier turns is worse than the
  // problem this solves.
  const following = log.scrollHeight - log.scrollTop - log.clientHeight < NEAR_BOTTOM_PX;
  mutate();
  if (following) log.scrollTop = log.scrollHeight;
}

export function add(text, cls) {
  const el = document.createElement("div");
  el.className = cls;
  el.textContent = text;
  stick(() => wrap.appendChild(el));
  return el;
}

export function note(el, text) {
  // Telemetry is always the *last* thing appended to a turn, so a note that
  // does not scroll is a note nobody ever sees.
  const tag = document.createElement("span");
  tag.style.cssText = "display:block; font-size:11px; opacity:.5; margin-top:4px;";
  tag.textContent = text;
  stick(() => el.appendChild(tag));
}

export function setEnabled(on) {
  input.disabled = !on;
  send.disabled = !on;
  if (on) input.focus();
}

// Split a bubble's text at what was heard, dimming the rest. The bubble's
// telemetry notes are elements after the text and are left where they are.
export function dimUnheard(el, heardChars) {
  const text = el.firstChild;
  if (!text || text.nodeType !== Node.TEXT_NODE || heardChars >= text.data.length) return;
  const rest = text.splitText(heardChars);
  const span = document.createElement("span");
  span.className = "unheard";
  span.title = "not heard: you interrupted";
  el.replaceChild(span, rest);
  span.appendChild(rest);
}

export function ms(v) { return v < 1000 ? `${v} ms` : `${(v / 1000).toFixed(1)} s`; }

export function paintListening(listening, speaking) {
  listen.classList.toggle("on", listening);
  listen.textContent = listening ? "⏹ stop" : "🎤 listen";
  status.textContent = !listening ? "" : speaking ? "· listening — talk to interrupt" : "· listening";
}
