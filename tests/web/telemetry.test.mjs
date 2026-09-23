// Executed tests for the lines drawn under each bubble.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  audioLine, committedLine, gapsLine, initiativeLine, ms, replyLines, truncatedLine,
} from "../../src/voice_agent/web/telemetry.js";

const reply = {
  ttft_ms: 480, chars: 42, fragments: 9, generation_ms: 1200, attempts: 1,
  output_tokens: 12, prompt_tokens: 0, cached_tokens: 0,
};

test("milliseconds switch to seconds at one second", () => {
  assert.equal(ms(999), "999 ms");
  assert.equal(ms(1500), "1.5 s");
});

test("a reply with no reported prompt tokens has no cache line, and no NaN", () => {
  const lines = replyLines(reply);
  assert.equal(lines.length, 1);
  assert.ok(!lines.join().includes("NaN"));
});

test("the cache share is a percentage of the prompt", () => {
  const [, cache] = replyLines({ ...reply, prompt_tokens: 2000, cached_tokens: 1500 });
  assert.equal(cache, "📥 2000 prompt tokens · 1500 cached (75%)");
});

test("missing tokens are not shown as zero tokens", () => {
  assert.ok(!replyLines({ ...reply, output_tokens: 0 })[0].includes("tokens"));
});

test("a guessed reply says so, and its accept time is not shown", () => {
  const lines = replyLines({ ...reply, speculated: true, speculation_lead_ms: 600, accepted_ms: 300 });
  assert.ok(!lines[0].includes("accepted at"));
  assert.ok(lines.some((l) => l.startsWith("⚡ started 600 ms")));
});

test("discarded guesses are counted, in the right number", () => {
  const one = replyLines({ ...reply, speculations_discarded: 1, speculation_wasted_chars: 8 });
  const two = replyLines({ ...reply, speculations_discarded: 2, speculation_wasted_chars: 8 });
  assert.ok(one.at(-1).startsWith("🗑 1 guess discarded"));
  assert.ok(two.at(-1).startsWith("🗑 2 guesses discarded"));
});

test("retries and a fresh connection appear only when they happened", () => {
  assert.ok(!replyLines(reply)[0].includes("🔌"));
  const line = replyLines({ ...reply, attempts: 2, connect_ms: 90 })[0];
  assert.ok(line.includes("🔌 opened a connection first, 90 ms"));
  assert.ok(line.includes("↻ sent 2×"));
});

test("audio from the startup cache says it cost the user nothing", () => {
  const line = audioLine({ seconds: 2, bytes: 2048, chunks: 1, cached: true, synthesis_ms: 300 });
  assert.ok(line.endsWith("synthesized at startup in 300 ms · no wait for you"));
});

test("a cut is marked approximate unless it was timed", () => {
  const base = { played_ms: 700, heard_chars: 10, chars: 40, stop_ms: 50 };
  assert.ok(truncatedLine({ ...base, timed: true }).includes("heard 10 of 40 chars ·"));
  assert.ok(truncatedLine(base).includes("(approximate)"));
  assert.ok(truncatedLine({ ...base, estimated: true }).includes("(estimated)"));
});

test("a prefix that did not hold is flagged", () => {
  const line = committedLine({ endpoint_ms: 500, stable_words: 3, prefix_held: false });
  assert.ok(line.includes("⚠ prefix did not hold"));
});

test("gaps are pluralised", () => {
  assert.equal(gapsLine({ gaps: 1, gapMs: 80 }), "⚠ 1 gap · 80 ms of silence mid-reply");
  assert.ok(gapsLine({ gaps: 3, gapMs: 80 }).startsWith("⚠ 3 gaps"));
});

test("an initiative that failed says why, rather than looking like a decline", () => {
  const line = initiativeLine({
    decision: "failed", message: "timeout", quiet_ms: 5200, rung: 1, rungs: 3, consider_ms: 900,
  });
  assert.ok(line.startsWith("⚠️ could not decide — timeout · 5s quiet · rung 1/3"));
});

test("a voice that arrived late says how late, and where", () => {
  const base = { seconds: 3, bytes: 2048, chunks: 2, first_audio_ms: 800, synthesis_first_byte_ms: 400, synthesis_ms: 900 };
  assert.ok(!audioLine({ ...base, late_ms: 120 }).includes("late"));
  assert.ok(audioLine({ ...base, late_ms: 2300, late_after: "Достоевский —" }).endsWith("⚠ voice 2.3 s late after “Достоевский —”"));
});
