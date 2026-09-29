// The admin page: one fetch of /admin/api/stats, drawn. Every value that came
// from a conversation (topic, client, ids) goes in through textContent, never
// innerHTML: a topic is whatever a visitor said.

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    node.append(child instanceof Node ? child : String(child ?? ""));
  }
  return node;
}

export function duration(seconds) {
  const s = Math.round(seconds || 0);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

export function count(n) {
  const v = Number(n) || 0;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e4) return `${(v / 1e3).toFixed(1)}k`;
  return v.toLocaleString("en");
}

function bytes(n) {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${units[i]}`;
}

function tile(label, value, hint = "", tone = "") {
  return el("div", { class: "tile" },
    el("div", { class: "label" }, label),
    el("div", { class: `value ${tone}` }, value),
    hint ? el("div", { class: "hint" }, hint) : "");
}

function drawHealth(health) {
  const box = $("health");
  box.replaceChildren(
    tile("status", health.ready ? "ready" : "warming", "", health.ready ? "good" : "warn"),
    tile("uptime", duration(health.uptime_s), `since ${health.started_at}`),
    tile("live sockets", `${health.live}${health.max_live ? ` / ${health.max_live}` : ""}`),
    tile("links in memory", count(health.stored)),
    tile("records", health.recording ? count(health.records) : "off", bytes(health.records_bytes)),
    tile("volume free", bytes(health.free_bytes), health.total_bytes ? `of ${bytes(health.total_bytes)}` : ""),
    tile("image", health.image ? health.image.split(":").pop() : "local", health.image || ""),
  );
}

function drawUsage(s) {
  const winRate = s.judged ? ` · ${Math.round((100 * s.wins) / s.judged)}% won` : "";
  $("usage").replaceChildren(
    tile("sessions", count(s.sessions), `${count(s.engaged)} with a word said`),
    tile("visitors", count(s.visitors), `${count(s.mobile)} on mobile`),
    tile("total time", duration(s.duration_s)),
    tile("avg session", duration(s.avg_duration_s), `median ${duration(s.median_duration_s)}`),
    tile("turns", `${count(s.you)} / ${count(s.replies)}`, "user / agent"),
    tile("LLM tokens in", count(s.prompt_tokens), `${count(s.cached_tokens)} cached`),
    tile("LLM tokens out", count(s.output_tokens)),
    tile("judge tokens", count(s.judge_tokens), `${count(s.judged)} judged${winRate}`),
    tile("TTS characters", count(s.tts_chars)),
    tile("mic audio", duration(s.mic_s), "sent to the ears"),
  );
}

function drawQuotas(quotas) {
  const box = $("quotas");
  box.replaceChildren();
  for (const [name, q] of Object.entries(quotas)) {
    if (q.console) {
      box.append(el("div", { class: "tile" },
        el("div", { class: "label" }, name),
        el("div", { class: "hint" }, "no balance API — "),
        el("a", { href: q.console, target: "_blank", rel: "noopener" }, "open console")));
    } else if (!q.ok) {
      box.append(tile(name, "unavailable", q.error, "bad"));
    } else if (name === "elevenlabs") {
      const left = (q.limit ?? 0) - (q.used ?? 0);
      const share = q.limit ? left / q.limit : 1;
      const resets = q.resets ? new Date(q.resets * 1000).toLocaleDateString() : "";
      box.append(tile("elevenlabs characters left", count(left),
        `${count(q.used)} of ${count(q.limit)} used · ${q.tier ?? ""} · resets ${resets}`,
        share < 0.1 ? "bad" : share < 0.25 ? "warn" : "good"));
    } else if (name === "deepseek") {
      const balance = (q.balances || []).map((b) => `${b.total} ${b.currency}`).join(" · ") || "—";
      box.append(tile("deepseek balance", balance, q.available ? "available" : "insufficient",
        q.available ? "good" : "bad"));
    }
  }
}

// The start screen's groups, every offered option listed even at zero (muted),
// then what the records hold that is no longer offered (italic, set apart).
// Each table sorts on its own, by section title; none chosen is menu order.
let breakdownSorts = {};
try {
  const stored = JSON.parse(localStorage.getItem("admin.breakdownSort"));
  if (stored && typeof stored === "object" && !Array.isArray(stored)) breakdownSorts = stored;
} catch { /* storage off, or not JSON */ }

function sortSection(title, label) {
  const column = BREAKDOWN_COLUMNS.find((c) => c.label === label);
  if (!column) return;
  const next = nextSort(validSort(breakdownSorts[title], BREAKDOWN_COLUMNS), column, { cycle: true });
  if (next) breakdownSorts[title] = next;
  else delete breakdownSorts[title];
  try { localStorage.setItem("admin.breakdownSort", JSON.stringify(breakdownSorts)); } catch { /* storage off */ }
  drawBreakdowns(data.windows[chosen].breakdown);
}

function drawBreakdowns(sections) {
  $("breakdowns").replaceChildren(...sections.map((section) => {
    const state = validSort(breakdownSorts[section.title], BREAKDOWN_COLUMNS);
    const rows = sortBreakdown(section.rows, state?.label, state?.direction);
    return el("div", { class: "card" }, el("table", { class: "breakdown", "data-section": section.title },
      el("thead", {}, el("tr", {}, ...BREAKDOWN_COLUMNS.map((c) => sortHeader(state, c.label,
        c.label === "option" ? section.title : c.label,
        { class: c.label === "option" ? "option" : "num" })))),
      el("tbody", {}, ...rows.map((r, i) => el("tr", {
        class: [r.sessions ? "" : "zero", r.offered ? "" : "retired",
          !r.offered && rows[i - 1]?.offered ? "first-retired" : ""].join(" ").trim(),
      },
      el("td", { class: "option" }, r.label), el("td", { class: "num" }, count(r.sessions)),
      el("td", { class: "num" }, duration(r.duration_s)),
      el("td", { class: "num" }, count(r.prompt_tokens)),
      el("td", { class: "num" }, count(r.output_tokens)))))));
  }));
}

const text = (value) => String(value ?? "").toLowerCase();
const OUTCOME_RANK = { WIN: 4, LOSE: 3, "no contest": 2, failed: 1 };

// What each column sorts on, and which way a first click sorts it: newest,
// longest and biggest first; words A to Z. `null` is blank, and blanks sort
// last whichever way, so a sort on a sparse column opens on its values.
export const COLUMNS = [
  { label: "started", key: (r) => r.started || null, first: "desc" },
  { label: "time", key: (r) => r.duration_s, first: "desc" },
  ...["role", "llm", "ears", "voice", "judge", "client", "visitor", "topic"].map((name) => ({
    label: name, key: (r) => text(r[name]) || null, first: "asc",
  })),
  {
    label: "result",
    // Outcome first, then the user's side of the split: WIN 58/42 above WIN 51/49.
    key: (r) => (OUTCOME_RANK[r.outcome] ? OUTCOME_RANK[r.outcome] * 1000
      + (Number.parseInt(r.score, 10) || 0) : r.ended ? 0 : null),
    first: "desc",
  },
  { label: "turns", key: (r) => (r.you || 0) + (r.replies || 0), first: "desc" },
  { label: "tok in/out", key: (r) => (r.prompt_tokens || 0) + (r.output_tokens || 0), first: "desc" },
  { label: "", key: null },
];

// Stable, and blanks last in either direction: equal keys keep the order the
// server sent (newest first; the start screen's order).
function ordered(rows, key, direction) {
  const sign = direction === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = key(a), y = key(b);
    if (x === y) return 0;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return (x < y ? -1 : 1) * sign;
  });
}

export function sortRows(rows, label, direction) {
  const column = COLUMNS.find((c) => c.label === label && c.key);
  return column ? ordered(rows, column.key, direction) : [...rows];
}

// A breakdown table's columns. `option` is headed by the section's own title.
export const BREAKDOWN_COLUMNS = [
  { label: "option", key: (r) => text(r.label) || null, first: "asc" },
  { label: "sessions", key: (r) => r.sessions, first: "desc" },
  { label: "time", key: (r) => r.duration_s, first: "desc" },
  { label: "tok in", key: (r) => r.prompt_tokens, first: "desc" },
  { label: "tok out", key: (r) => r.output_tokens, first: "desc" },
];

// Sorted within each group — offered, then the rest — so what the start screen
// offers stays above the divider. No direction is the start screen's order.
export function sortBreakdown(rows, label, direction) {
  const column = BREAKDOWN_COLUMNS.find((c) => c.label === label);
  if (!column || !direction) return [...rows];
  const offered = rows.filter((r) => r.offered);
  const rest = rows.filter((r) => !r.offered);
  return [...ordered(offered, column.key, direction), ...ordered(rest, column.key, direction)];
}

// A sort read back from storage, or null: a label from an older page, or a
// value edited by hand, must not leave a table silently unsorted.
export function validSort(value, columns) {
  if (!value || typeof value !== "object") return null;
  if (value.direction !== "asc" && value.direction !== "desc") return null;
  return columns.some((c) => c.key && c.label === value.label) ? value : null;
}

// Same column: reverse, then (where there is a natural order) back to it.
export function nextSort(current, column, { cycle = false } = {}) {
  if (current?.label !== column.label) return { label: column.label, direction: column.first };
  if (current.direction !== column.first) return cycle ? null : { label: column.label, direction: column.first };
  return { label: column.label, direction: column.first === "asc" ? "desc" : "asc" };
}

function sortHeader(state, label, shown, attrs) {
  const active = state?.label === label;
  const marker = active ? (state.direction === "asc" ? " ▲" : " ▼") : "";
  return el("th", { ...attrs, "aria-sort": active ? `${state.direction}ending` : "none" },
    el("button", { class: "sort", type: "button", "data-sort": label }, `${shown}${marker}`));
}

function sessionRow(r) {
  const result = r.outcome === "WIN" || r.outcome === "LOSE"
    ? el("span", { class: r.outcome === "WIN" ? "good" : "bad" }, `${r.outcome} ${r.score}`)
    : r.outcome || (r.ended ? "ended" : "");
  return el("tr", { "data-search": [r.started, r.role, r.llm, r.ears, r.voice, r.judge,
    r.client, r.visitor, r.topic, r.outcome, r.id].join(" ").toLowerCase() },
  el("td", {}, (r.started || r.file).slice(0, 16)),
  el("td", { class: "num" }, duration(r.duration_s)),
  el("td", {}, r.role), el("td", {}, r.llm), el("td", {}, r.ears),
  el("td", {}, r.voice), el("td", {}, r.judge),
  el("td", {}, `${r.client}${r.mobile ? " · mobile" : ""}`),
  el("td", {}, r.visitor),
  el("td", { class: "topic" }, r.topic),
  el("td", {}, result),
  el("td", { class: "num" }, `${r.you} / ${r.replies}`),
  el("td", { class: "num" }, `${count(r.prompt_tokens)} / ${count(r.output_tokens)}`),
  el("td", {}, el("a", { href: r.link, target: "_blank", rel: "noopener" },
    r.review ? "review ↗" : "record ↗")));
}

const NEWEST_FIRST = { label: "started", direction: "desc" };
let sorting = NEWEST_FIRST;
try {
  sorting = validSort(JSON.parse(localStorage.getItem("admin.sort")), COLUMNS) || NEWEST_FIRST;
} catch { /* storage off, or not JSON */ }

function header(column) {
  return column.key ? sortHeader(sorting, column.label, column.label, {}) : el("th", {}, "");
}

function drawSessions() {
  if (!data) return;
  $("sessions").replaceChildren(
    el("thead", {}, el("tr", {}, ...COLUMNS.map(header))),
    el("tbody", {}, ...sortRows(data.sessions, sorting.label, sorting.direction).map(sessionRow)));
  applyFilter();
}

function sortBy(label) {
  const column = COLUMNS.find((c) => c.label === label && c.key);
  if (!column) return;
  sorting = nextSort(sorting, column);
  try { localStorage.setItem("admin.sort", JSON.stringify(sorting)); } catch { /* storage off */ }
  drawSessions();
}

export function countLine(shown, total) {
  const noun = total === 1 ? "session" : "sessions";
  return shown === total ? `${total} ${noun}` : `${shown} of ${total} ${noun}`;
}

function applyFilter() {
  const needle = $("filter").value.trim().toLowerCase();
  const rows = $("sessions").querySelectorAll("tbody tr");
  let shown = 0;
  for (const row of rows) {
    row.hidden = needle !== "" && !row.dataset.search.includes(needle);
    shown += row.hidden ? 0 : 1;
  }
  $("count").textContent = countLine(shown, rows.length);
}

let data = null;
let chosen = "deploy";
try { chosen = localStorage.getItem("admin.window") || chosen; } catch { /* storage off */ }

function choose(name) {
  // A window saved by an older page may not exist any more.
  chosen = !data || data.windows[name] ? name : "deploy";
  name = chosen;
  try { localStorage.setItem("admin.window", name); } catch { /* storage off */ }
  for (const button of $("windows").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.window === name));
  }
  if (data) {
    drawUsage(data.windows[name]);
    drawBreakdowns(data.windows[name].breakdown);
  }
}

async function load() {
  const response = await fetch("/admin/api/stats", { cache: "no-store" });
  if (!response.ok) {
    $("asof").replaceChildren(el("span", { class: "error" }, `stats failed: HTTP ${response.status}`));
    return;
  }
  data = await response.json();
  $("asof").textContent = `as of ${data.now} · reload the page to refresh`;
  drawHealth(data.health);
  drawQuotas(data.quotas);
  drawSessions();
  choose(chosen);
}

if (typeof document !== "undefined" && $("windows")) {
  $("windows").addEventListener("click", (event) => {
    const name = event.target?.dataset?.window;
    if (name) choose(name);
  });
  $("filter").addEventListener("input", applyFilter);
  $("sessions").addEventListener("click", (event) => {
    const label = event.target?.closest?.("button.sort")?.dataset.sort;
    if (label) sortBy(label);
  });
  $("breakdowns").addEventListener("click", (event) => {
    const button = event.target?.closest?.("button.sort");
    const title = button?.closest("table")?.dataset.section;
    if (button && title) sortSection(title, button.dataset.sort);
  });
  load().catch((err) => {
    $("asof").replaceChildren(el("span", { class: "error" }, `stats failed: ${err}`));
  });
}
