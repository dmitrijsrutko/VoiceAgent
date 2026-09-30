// A conversation redrawn from the frames the server kept of it: an ended one
// opened from its link, or a live one reloaded. The same bubbles, notes and
// thoughts as when it happened, so "stats" and "thoughts" show and hide the
// same things on it. Pure — frames in, entries out, no DOM — so node runs it;
// `app.js` draws the entries.

import {
  audioLine, committedLine, echoLine, initiativeLine, quietLine, replyLines, thoughtLine,
  truncatedLine, unpromptedLine,
} from "./telemetry.js";

// The lines under a finished reply. Shared with the live handler, so the two
// cannot drift. `cut`: the user talked over it, and its truncation says so.
export function endLines(msg, cut) {
  if (msg.interrupted) return cut ? [] : ["✋ interrupted before it was spoken"];
  if (msg.initiative) return [unpromptedLine(msg)];
  if (msg.resumed) return ["↩ picked up where it was cut off: whatever cut in said nothing more"];
  return replyLines(msg);
}

const DECLINES = new Set(["nothing", "unchanged"]);
export const declined = (msg) => DECLINES.has(msg.decision);

// Each entry is one line in the log: `{ text, cls, notes: [{ text, cls }] }`,
// plus `shown` on a reply only partly heard.
export function review(frames) {
  const entries = [];
  const line = (text, cls) => { const e = { text, cls, notes: [] }; entries.push(e); return e; };
  let reply = null;   // the latest agent bubble
  let writing = null; // the reply still being written, if any
  let voiced = null;  // the bubble whose audio is playing
  let cut = null;     // the reply the user talked over
  let quiet = null;   // { entry, count } — a run of declines, one line

  for (const msg of frames) {
    switch (msg.type) {
      case "greeting":
        reply = line(msg.text, "msg agent");
        break;
      case "reply_start":
        cut = null;
        writing = reply = line("", "msg agent");
        break;
      case "reply_end": {
        quiet = null;
        if (!writing) break;
        writing = null;
        reply.text = msg.text ?? "";
        if (msg.interrupted && !reply.text) {
          entries.splice(entries.indexOf(reply), 1);
          break;
        }
        for (const text of endLines(msg, cut !== null)) reply.notes.push({ text, cls: "telemetry" });
        break;
      }
      case "interrupt":
        cut = reply;
        voiced = null;  // live, the player stops: a cut reply's audio gets no note
        break;
      case "truncated":
        if (!cut) break;
        cut.shown = msg.heard_chars;
        cut.notes.push({ text: truncatedLine(msg), cls: "telemetry" });
        break;
      case "audio_start":
        voiced = cut ? null : reply;
        break;
      case "audio_end":
        if (voiced) voiced.notes.push({ text: audioLine(msg), cls: "telemetry" });
        voiced = null;
        break;
      case "said":
        line(msg.text, "msg user");
        break;
      case "transcript":
        if (!msg.final || !msg.text.trim()) break;
        line(msg.text, "msg user").notes.push({ text: committedLine(msg), cls: "telemetry" });
        quiet = null;
        break;
      case "initiative":
        line(initiativeLine(msg), "note think thought");
        break;
      case "echo_ignored":
        line(echoLine(msg), "note think telemetry");
        break;
      case "thought":
        if (declined(msg)) {
          if (!quiet) quiet = { entry: line("", "note think thought"), count: 0 };
          quiet.count += 1;
          quiet.entry.text = quietLine(quiet.count, msg);
        } else {
          quiet = null;
          line(thoughtLine(msg), "note think thought");
        }
        break;
      case "audio_error":
        if (reply) reply.notes.push({ text: `🔇 ${msg.message}`, cls: "shown" });
        break;
      case "error":
        // As live: the reply it cut short goes.
        if (writing) { entries.splice(entries.indexOf(writing), 1); writing = reply = null; }
        line(msg.message, "error");
        break;
    }
  }
  // Still being written when the page loaded: its text is not known yet.
  if (writing) entries.splice(entries.indexOf(writing), 1);
  return entries;
}
