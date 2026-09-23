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
export const start = document.getElementById("start");
export const begin = document.getElementById("begin");
export const startNotices = document.getElementById("start-notices");
export const startStack = document.getElementById("start-stack");

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

// The utterance still being spoken, which stays the last thing in the log
// until it is committed or dropped. Its bubble is made at the first partial, so
// a reply to the *previous* utterance that starts after the user has already
// carried on used to be drawn below it — and the user's words, finishing in
// that bubble, then read as said before the reply they actually followed.
let pinned = null;

export function pinLast(el) { pinned = el; }

export function add(text, cls) {
  const el = document.createElement("div");
  el.className = cls;
  el.textContent = text;
  const below = pinned?.parentNode === wrap ? pinned : null;
  stick(() => wrap.insertBefore(el, below));
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

// A reply's text, spoken part as normal and the rest dimmed. Kept in its own
// first child so telemetry notes, appended after it, are never overwritten —
// `textContent +=` on the bubble itself wiped a note that landed mid-reply.
export function paintText(el, text, shown) {
  let body = el.firstElementChild?.classList.contains("text") ? el.firstElementChild : null;
  if (!body) {
    body = document.createElement("span");
    body.className = "text";
    // A bubble made with its text already in place (the greeting, history).
    if (el.firstChild?.nodeType === Node.TEXT_NODE) el.firstChild.remove();
    el.prepend(body);
  }
  const rest = document.createElement("span");
  rest.className = "unheard";
  rest.textContent = text.slice(shown);
  body.replaceChildren(document.createTextNode(text.slice(0, shown)), rest);
}

export function ms(v) { return v < 1000 ? `${v} ms` : `${(v / 1000).toFixed(1)} s`; }

export function paintListening(listening, speaking) {
  listen.classList.toggle("on", listening);
  listen.textContent = listening ? "⏹ stop" : "🎤 listen";
  status.textContent = !listening ? "" : speaking ? "· listening — talk to interrupt" : "· listening";
}
