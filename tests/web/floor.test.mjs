// Executed tests for the floor strip's model: how server floor frames become
// segments of the last few seconds.

import assert from "node:assert/strict";
import { test } from "node:test";

import { SPAN_MS, record, segments } from "../../src/voice_agent/web/floor.js";

const frame = (state, lag_ms = 0, agent = false) => ({ state, lag_ms, agent });

test("a change is dated back by the server's lag to when it began", () => {
  const events = record([], frame("micro_pause", 224), 10_000);
  assert.equal(events[0].at, 10_000 - 224);
});

test("a silence that escalates stays one segment, dated from when speech stopped", () => {
  const events = [];
  record(events, frame("speaking", 64), 1_064);
  record(events, frame("micro_pause", 224), 2_224);
  record(events, frame("pause", 608), 2_608);
  assert.deepEqual(events.map((e) => [e.state, e.at]), [["speaking", 1_000], ["pause", 2_000]]);
});

test("segments tile the strip and label a silence with its length", () => {
  const events = [];
  record(events, frame("speaking", 0), 1_000);
  record(events, frame("pause", 600), 2_600);
  const [speech, pause] = segments(events, 2_704, 2_000);
  assert.equal(speech.label, "");
  assert.equal(pause.label, "704 ms");
  // A 2 s window ending at 2704 starts at 704: speech 1000-2000, pause 2000-2704.
  assert.ok(Math.abs(speech.left - 0.148) < 1e-9 && Math.abs(speech.width - 0.5) < 1e-9);
  assert.ok(Math.abs(pause.width - 0.352) < 1e-9);
});

test("events older than the window are dropped, keeping the one it opens in", () => {
  const events = [];
  record(events, frame("speaking"), 0);
  record(events, frame("pause"), 1_000);
  record(events, frame("speaking"), SPAN_MS + 5_000);
  assert.deepEqual(events.map((e) => e.state), ["pause", "speaking"]);
});

test("speech while the agent is talking is flagged as possible echo", () => {
  const [seg] = segments(record([], frame("speaking", 0, true), 500), 1_000);
  assert.equal(seg.echo, true);
});
