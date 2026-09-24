// Executed tests for the coarse browser facts the page reports.

import assert from "node:assert/strict";
import { test } from "node:test";

import { clientFacts } from "../../src/voice_agent/web/client.js";

const SAFARI_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";
const CHROME_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36";
const TELEGRAM_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Telegram-iOS/11.0";
const CHROME_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/129.0 Mobile/15E148 Safari/604.1";

test("an iPhone's Safari is Safari on iOS, mobile, not in an app", () => {
  assert.deepEqual(clientFacts(SAFARI_IPHONE), { browser: "Safari", os: "iOS", mobile: true, in_app: null });
});

test("Chrome on Android, and Chrome on an iPhone", () => {
  assert.deepEqual(clientFacts(CHROME_ANDROID), { browser: "Chrome", os: "Android", mobile: true, in_app: null });
  assert.equal(clientFacts(CHROME_IPHONE).browser, "Chrome");
  assert.equal(clientFacts(CHROME_IPHONE).os, "iOS");
});

test("a link opened inside Telegram says so: its browser usually cannot use the microphone", () => {
  assert.equal(clientFacts(TELEGRAM_IPHONE).in_app, "Telegram");
});
