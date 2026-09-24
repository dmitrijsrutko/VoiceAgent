// Executed tests for what the page says when the microphone cannot be opened.

import assert from "node:assert/strict";
import { test } from "node:test";

import { micFailure } from "../../src/voice_agent/web/mic.js";

const failure = (name, message = "raw browser text") => Object.assign(new Error(message), { name });

test("a blocked microphone says where to allow it, and names in-app browsers", () => {
  const text = micFailure(failure("NotAllowedError"));
  assert.match(text, /blocked for this page/);
  assert.match(text, /Website Settings → Microphone/);
  assert.match(text, /open the link in Safari or Chrome/);
});

test("a busy, missing or insecure microphone each says which", () => {
  assert.match(micFailure(failure("NotReadableError")), /busy/);
  assert.match(micFailure(failure("NotFoundError")), /no microphone/);
  assert.match(micFailure(failure("SecurityError")), /https/);
});

test("anything else keeps the browser's own words", () => {
  assert.equal(micFailure(failure("AbortError", "something odd")), "something odd");
});
