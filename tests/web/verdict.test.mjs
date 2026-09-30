// Executed tests for the judge's ruling card.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { progress, renderRuling, statChips } from "../../src/voice_agent/web/verdict.js";

const ruling = JSON.parse(readFileSync(new URL("../fixtures/ruling_sample.json", import.meta.url)));

test("a ruling shows the outcome, the split and every section", () => {
  const html = renderRuling(ruling);
  assert.match(html, /class="verdict lose"/);
  assert.match(html, /You 30<\/span><span class="adv">70 Advocate/);
  for (const section of ["Your position", "Scorecard", "Moments", "Did it land?", "How to improve", "Rematch brief"]) {
    assert.ok(html.includes(section), section);
  }
  assert.equal((html.match(/class="v-row"/g) ?? []).length, 10);
  assert.ok(html.includes("Пил по кругу"));
  assert.match(html, /Judged by DeepSeek V4\.1 Flash in 41 s/);
});

test("a section the judge left out is not drawn empty", () => {
  // Seen live: a ruling with no "persuasion" read "Moved the advocate: ."
  const { persuasion, position, ...rest } = ruling.verdict;
  const partial = { ...ruling, verdict: { ...rest, position: { stated: "Тезис" } } };
  const html = renderRuling(partial);
  assert.ok(!html.includes("Did it land?"));
  assert.ok(!html.includes("Moved the advocate"));
  assert.match(html, /<dt>Stated<\/dt><dd>Тезис<\/dd><\/dl>/);
  assert.ok(!html.includes("<dt>Held</dt>"));
});

test("a win is drawn as one", () => {
  const won = { ...ruling, verdict: { ...ruling.verdict, outcome: "win", split: { you: 62, advocate: 38 } } };
  assert.match(renderRuling(won), /class="verdict win".*🏆 WIN/s);
});

test("what the model wrote is escaped", () => {
  const hostile = { ...ruling, verdict: { ...ruling.verdict, headline: "<img src=x onerror=alert(1)>" } };
  const html = renderRuling(hostile);
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&#60;img"));
});

test("no contest and a failed judge each say so", () => {
  assert.match(renderRuling({ status: "no_contest", judge: { title: "J" }, stats: {} }), /No contest/);
  const failed = renderRuling({ status: "failed", error: "no JSON object", judge: { title: "J" } });
  assert.match(failed, /No verdict/);
  assert.match(failed, /no JSON object/);
});

test("measured numbers become chips, estimates marked", () => {
  const html = statChips({ length_s: 191, your_turns: 10, words_per_turn: 6.7, think_median_s: 3.3, think_longest_s: 7.4, talk_overs: 0, approximate: true });
  assert.ok(html.includes("⏱ 3:11"));
  assert.ok(html.includes("≈3.3 s to answer"));
  assert.ok(!html.includes("talked over"));
});

test("the wait fills a bar against the usual time", () => {
  assert.match(progress("J", 0), /▱{12}  0 s of ~30 s/);
  assert.match(progress("J", 15.7), /▰{6}▱{6}  15 s of ~30 s/);
  const full = progress("J", 30);
  assert.match(full, /▰{12}  30 s of ~30 s/);
  assert.ok(!full.includes("longer"));
});

test("past the usual time it says so and keeps counting", () => {
  const late = progress("J", 41.2);
  assert.match(late, /Taking longer than usual/);
  assert.match(late, /▰{12}  41 s</);
});

test("the judge's name is escaped in the wait", () => {
  assert.ok(progress("<b>x</b>", 1).includes("&#60;b&#62;x"));
});
