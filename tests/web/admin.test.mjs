// Executed tests for the admin page's session table: sorting and the count.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BREAKDOWN_COLUMNS, COLUMNS, countLine, nextSort, sortBreakdown, sortRows, validSort,
} from "../../src/voice_agent/web/admin.js";

const row = (id, fields = {}) => ({ id, started: `2026-09-2${id} 10:00:00`, duration_s: 0,
  role: "", judge: "", outcome: "", score: "", ended: false, you: 0, replies: 0,
  prompt_tokens: 0, output_tokens: 0, ...fields });

const ids = (rows) => rows.map((r) => r.id).join("");

test("durations sort as numbers, not as text", () => {
  const rows = [row(1, { duration_s: 10 }), row(2, { duration_s: 9 }), row(3, { duration_s: 100 })];
  assert.equal(ids(sortRows(rows, "time", "asc")), "213");
  assert.equal(ids(sortRows(rows, "time", "desc")), "312");
});

test("blanks sort last in either direction", () => {
  const rows = [row(1), row(2, { judge: "opus" }), row(3, { judge: "deepseek-high" })];
  assert.equal(ids(sortRows(rows, "judge", "asc")), "321");
  assert.equal(ids(sortRows(rows, "judge", "desc")), "231");
});

test("results rank by outcome, then by the user's side of the split", () => {
  const rows = [
    row(1, { outcome: "LOSE", score: "44/56" }),
    row(2, { outcome: "WIN", score: "51/49" }),
    row(3),
    row(4, { outcome: "WIN", score: "58/42" }),
    row(5, { ended: true }),
  ];
  assert.equal(ids(sortRows(rows, "result", "desc")), "42153");
});

test("equal keys keep the order they came in", () => {
  const rows = [row(3, { role: "da" }), row(1, { role: "da" }), row(2, { role: "da" })];
  assert.equal(ids(sortRows(rows, "role", "asc")), "312");
  assert.equal(ids(sortRows(rows, "role", "desc")), "312");
});

test("turns and tokens sort on their totals", () => {
  const rows = [row(1, { you: 1, replies: 1, prompt_tokens: 5000 }),
    row(2, { you: 5, replies: 5, output_tokens: 10 })];
  assert.equal(ids(sortRows(rows, "turns", "desc")), "21");
  assert.equal(ids(sortRows(rows, "tok in/out", "desc")), "12");
});

test("an unknown or unsortable column leaves the order alone", () => {
  const rows = [row(2), row(1)];
  assert.equal(ids(sortRows(rows, "", "asc")), "21");
  assert.equal(ids(sortRows(rows, "nope", "asc")), "21");
});

const option = (name, sessions, fields = {}) => ({ name, label: name, offered: true, sessions,
  duration_s: 0, prompt_tokens: 0, output_tokens: 0, ...fields });
const names = (rows) => rows.map((r) => r.name).join(",");
const MENU = [option("haiku", 2), option("sonnet", 10), option("opus", 0),
  option("max", 50, { offered: false }), option("deaf", 1, { offered: false })];

test("a breakdown with no sort is the start screen's order", () => {
  assert.equal(names(sortBreakdown(MENU, undefined, undefined)), "haiku,sonnet,opus,max,deaf");
});

test("a breakdown sorts numerically, and offered options stay above the rest", () => {
  assert.equal(names(sortBreakdown(MENU, "sessions", "desc")), "sonnet,haiku,opus,max,deaf");
  assert.equal(names(sortBreakdown(MENU, "sessions", "asc")), "opus,haiku,sonnet,deaf,max");
  assert.equal(names(sortBreakdown(MENU, "option", "asc")), "haiku,opus,sonnet,deaf,max");
});

test("a third click on a breakdown column returns to the start screen's order", () => {
  const column = BREAKDOWN_COLUMNS.find((c) => c.label === "sessions");
  const first = nextSort(undefined, column, { cycle: true });
  const second = nextSort(first, column, { cycle: true });
  assert.deepEqual(first, { label: "sessions", direction: "desc" });
  assert.deepEqual(second, { label: "sessions", direction: "asc" });
  assert.equal(nextSort(second, column, { cycle: true }), null);
});

test("the sessions table only ever flips", () => {
  const column = { label: "time", first: "desc" };
  const flipped = nextSort({ label: "time", direction: "desc" }, column);
  assert.deepEqual(flipped, { label: "time", direction: "asc" });
  assert.deepEqual(nextSort(flipped, column), { label: "time", direction: "desc" });
});

test("a sort read back from storage is used only if it still makes sense", () => {
  const ok = { label: "sessions", direction: "desc" };
  assert.equal(validSort(ok, BREAKDOWN_COLUMNS), ok);
  assert.equal(validSort({ label: "gone", direction: "desc" }, BREAKDOWN_COLUMNS), null);
  assert.equal(validSort({ label: "sessions", direction: "up" }, BREAKDOWN_COLUMNS), null);
  assert.equal(validSort("sessions", BREAKDOWN_COLUMNS), null);
  assert.equal(validSort(null, BREAKDOWN_COLUMNS), null);
  assert.equal(validSort({ label: "", direction: "asc" }, COLUMNS), null, "the link column never sorts");
});

test("the count says how many of how many", () => {
  assert.equal(countLine(206, 206), "206 sessions");
  assert.equal(countLine(12, 206), "12 of 206 sessions");
  assert.equal(countLine(1, 1), "1 session");
  assert.equal(countLine(0, 206), "0 of 206 sessions");
});
