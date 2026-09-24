// Executed tests for the time-limit countdown.

import assert from "node:assert/strict";
import { test } from "node:test";

import { countdown } from "../../src/voice_agent/web/timer.js";

test("nothing shows until the last minute", () => {
  assert.deepEqual(countdown(61_000), { show: false, urgent: false, text: "" });
});

test("the last minute counts down, and turns urgent near the end", () => {
  assert.deepEqual(countdown(60_000), { show: true, urgent: false, text: "⏳ 1:00" });
  assert.deepEqual(countdown(42_300), { show: true, urgent: false, text: "⏳ 0:43" });
  assert.deepEqual(countdown(9_000), { show: true, urgent: true, text: "⏳ 0:09" });
  assert.equal(countdown(-500).text, "⏳ 0:00");
});
