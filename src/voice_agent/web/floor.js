// Who holds the floor, as a strip of the last few seconds: the server's VAD
// says when the user speaks and how long each silence has lasted. The model is
// pure — no DOM — so node runs it; `paintFloor` draws it.

import { ms } from "./telemetry.js";

export const SPAN_MS = 12000;

const QUIET = new Set(["micro_pause", "pause", "yielded"]);

// Events in arrival order, each `{state, at, agent}` with `at` in page time,
// already moved back by the server's `lag_ms` to when the change began. A
// silence is one segment however far it escalates: micro_pause → pause →
// yielded all date from the moment the speech stopped.
export function record(events, msg, now) {
  const at = now - msg.lag_ms;
  const last = events[events.length - 1];
  if (last && QUIET.has(last.state) && QUIET.has(msg.state)) {
    last.state = msg.state;
  } else {
    events.push({ state: msg.state, at, agent: msg.agent });
  }
  // Keep one event older than the window: it is the state the window opens in.
  while (events.length > 1 && events[1].at < now - SPAN_MS) events.shift();
  return events;
}

// Segments as fractions of the strip, left to right. A silence is labelled with
// its length once it is long enough to be one; speech over the agent's own
// voice is flagged, since it may be echo rather than the user.
export function segments(events, now, span = SPAN_MS) {
  const from = now - span;
  return events.map((event, i) => {
    const end = i + 1 < events.length ? events[i + 1].at : now;
    const start = Math.max(event.at, from);
    return {
      state: event.state,
      left: (start - from) / span,
      width: Math.max(0, end - start) / span,
      label: QUIET.has(event.state) ? ms(Math.round(end - event.at)) : "",
      echo: event.state === "speaking" && event.agent,
    };
  }).filter((s) => s.width > 0);
}

export function paintFloor(el, events, now) {
  const parts = segments(events, now).map((s) => {
    const span = document.createElement("span");
    span.className = `floor-${s.state}${s.echo ? " floor-echo" : ""}`;
    span.style.left = `${(s.left * 100).toFixed(2)}%`;
    span.style.width = `${(s.width * 100).toFixed(2)}%`;
    // A label only where it fits: a 12 s strip gives ~0.5 % per 60 ms.
    if (s.label && s.width > 0.06) span.textContent = s.label;
    if (s.echo) span.title = "speech while the agent was talking — the user, or echo";
    return span;
  });
  el.replaceChildren(...parts);
}
