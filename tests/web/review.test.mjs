// Executed tests for redrawing a conversation from its kept frames: the same
// bubbles, notes and thoughts as live, so "stats" and "thoughts" work on it.

import assert from "node:assert/strict";
import { test } from "node:test";

import { endLines, review } from "../../src/voice_agent/web/review.js";
import { replyLines } from "../../src/voice_agent/web/telemetry.js";

const reply = (text, extra = {}) => [
  { type: "reply_start" },
  { type: "reply_end", text, chars: text.length, ttft_ms: 420, generation_ms: 80, ...extra },
];
const thought = (decision, extra = {}) => ({
  type: "thought", decision, reason: "user_pause", consider_ms: 900, ...extra,
});

test("bubbles come back in order, with the notes they had live", () => {
  const entries = review([
    { type: "greeting", text: "Hi." },
    { type: "said", text: "typed words" },
    ...reply("Answer."),
    { type: "transcript", text: "spoken words", final: true, stt_ms: 120 },
  ]);
  assert.deepEqual(entries.map((e) => [e.cls, e.text]), [
    ["msg agent", "Hi."],
    ["msg user", "typed words"],
    ["msg agent", "Answer."],
    ["msg user", "spoken words"],
  ]);
  const notes = entries[2].notes;
  assert.deepEqual(notes.map((n) => n.text), replyLines(reply("Answer.")[1]));
  assert.ok(notes.every((n) => n.cls === "telemetry"), "a reply's cost is telemetry, hidden until stats");
  assert.equal(entries[3].notes.length, 1, "a committed transcript keeps its timing");
});

test("thoughts are their own lines, a run of declines collapsed into one", () => {
  const entries = review([
    ...reply("Answer."),
    thought("nothing"),
    thought("nothing"),
    thought("thought", { move: "challenge", urgency: 2, line: "But why?", why: "a gap" }),
  ]);
  const thoughts = entries.filter((e) => e.cls.includes("thought"));
  assert.equal(thoughts.length, 2);
  assert.match(thoughts[0].text, /^🤫 2 considerations/);
  assert.match(thoughts[1].text, /But why\?/);
});

test("a reply talked over keeps only what was heard, and says so", () => {
  const entries = review([
    { type: "reply_start" },
    { type: "audio_start" },
    { type: "interrupt", id: 1 },
    { type: "reply_end", interrupted: true, text: "A long answer", chars: 13 },
    { type: "truncated", heard_chars: 6, played_ms: 400 },
    { type: "audio_end", seconds: 1 },
  ]);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].shown, 6);
  assert.equal(entries[0].notes.length, 1, "the truncation note, and no audio note for a cut reply");
});

test("an interrupted reply with no text is not drawn", () => {
  assert.deepEqual(review([{ type: "reply_start" }, { type: "reply_end", interrupted: true, text: "" }]), []);
});

test("an error takes the reply it cut short with it, as live", () => {
  const entries = review([{ type: "reply_start" }, { type: "error", message: "provider exploded" }]);
  assert.deepEqual(entries.map((e) => e.cls), ["error"]);
});

test("the live handler and the review choose the same lines", () => {
  assert.deepEqual(endLines({ interrupted: true }, false), ["✋ interrupted before it was spoken"]);
  assert.deepEqual(endLines({ interrupted: true }, true), []);
  assert.match(endLines({ resumed: true }, false)[0], /^↩/);
});

test("a reply still being written when the page loaded is not drawn empty", () => {
  assert.deepEqual(review([{ type: "said", text: "hi" }, { type: "reply_start" }]).map((e) => e.cls), ["msg user"]);
});
