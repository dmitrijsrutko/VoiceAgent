// Executed tests for the karaoke rule. Its cases mirror tests/test_heard.py:
// the highlight and the interruption cut must agree on what was heard.

import assert from "node:assert/strict";
import { test } from "node:test";

import { addMarks, spokenChars } from "../../src/voice_agent/web/karaoke.js";

// Each character spoken over `step` ms, ending at (i + 1) * step.
const evenly = (text, step = 100) => [...text].map((_, i) => (i + 1) * step);

test("a word heard in part is not lit up", () => {
  const text = "one two three";
  assert.equal(text.slice(0, spokenChars(text, evenly(text), 1000)).trimEnd(), "one two");
});

test("a word lights up once its last letter has sounded", () => {
  const text = "one two three";
  assert.equal(text.slice(0, spokenChars(text, evenly(text), 700)), "one two");
});

test("punctuation before the cut stays with its word", () => {
  const text = "Riga, the capital";
  assert.equal(text.slice(0, spokenChars(text, evenly(text), 700)).trimEnd(), "Riga,");
});

test("speech paused mid-word does not light up the half word", () => {
  // Timed so far: "one two thre". Written: more.
  const text = "one two three four";
  assert.equal(text.slice(0, spokenChars(text, evenly("one two thre"), 5000)).trimEnd(), "one two");
});

test("played past the end lights up everything", () => {
  const text = "one two";
  assert.equal(spokenChars(text, evenly(text), 5000), text.length);
});

test("nothing played lights up nothing", () => {
  assert.equal(spokenChars("one two", evenly("one two"), 0), 0);
});

test("with no timing the text is shown whole", () => {
  assert.equal(spokenChars("Hello there", [], 0), "Hello there".length);
});

test("marks for new audio cap earlier characters at where that audio starts", () => {
  // As measured live: the first segment timed past the start of the second.
  const ends = [250, 500, 750, 1000];
  addMarks(ends, 500, [600, 700, 800, 900]);

  assert.deepEqual(ends, [250, 500, 500, 500, 600, 700, 800, 900]);
  assert.equal(spokenChars("one two ", ends, 900), "one two ".length);
});
