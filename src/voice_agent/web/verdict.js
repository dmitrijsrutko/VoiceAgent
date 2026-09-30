// The judge's ruling on a round, as HTML. Pure — strings in, a string out — so
// node runs it. Everything in a ruling was written by a model reading what a
// user said, so every piece of it is escaped on the way in.

const escape = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

const LABELS = {
  clarity: "Clarity", evidence: "Evidence", logic: "Logic", rebuttal: "Rebuttal",
  listening: "Listening", consistency: "Consistency", economy: "Economy",
  composure: "Composure", originality: "Originality", persuasion: "Persuasion",
};

const at = (moment) => (moment?.at ? ` <span class="v-at">${escape(moment.at)}</span>` : "");

function clock(seconds) {
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

// The measured numbers, as chips; `≈` when rebuilt from a record.
export function statChips(stats = {}) {
  const about = stats.approximate ? "≈" : "";
  const chips = [
    stats.length_s != null && `⏱ ${clock(stats.length_s)}`,
    stats.your_turns != null && `💬 ${stats.your_turns} turns · ${stats.words_per_turn} words each`,
    stats.talk_share != null && `🗣 you spoke ${stats.talk_share}% of the time`,
    stats.think_median_s != null && `🤔 ${about}${stats.think_median_s} s to answer (longest ${about}${stats.think_longest_s} s)`,
    stats.talk_overs ? `✋ talked over it ${stats.talk_overs}×` : null,
    stats.cut_offs ? `✂️ cut it off ${stats.cut_offs}×` : null,
  ].filter(Boolean);
  return `<div class="v-stats">${chips.map((c) => `<span>${escape(c)}</span>`).join("")}</div>`;
}

function moment(icon, title, m) {
  if (!m || !(m.quote || m.objection)) return "";
  return `<div class="v-moment"><b>${icon} ${title}</b>${at(m)}` +
    `<q>${escape(m.quote ?? m.objection)}</q><p>${escape(m.why)}</p></div>`;
}

// Every part of a verdict but the split, headline and reasoning is optional
// (`judge.parse`): a section the judge left out is not drawn, rather than drawn
// empty.
const section = (title, body) => (body ? `<h4>${title}</h4>${body}` : "");

function positionRows(position) {
  const rows = [["Stated", position.stated], ["Held", position.survived], ["Fell", position.fell]]
    .filter(([, text]) => text)
    .map(([label, text]) => `<dt>${label}</dt><dd>${escape(text)}</dd>`).join("");
  return rows && `<dl>${rows}</dl>`;
}

function landed(persuasion) {
  const moved = persuasion.advocate_moved
    ? `Moved the advocate: <b>${escape(persuasion.advocate_moved)}</b>. ` : "";
  const audience = persuasion.audience ? escape(persuasion.audience) : "";
  return moved || audience ? `<p>${moved}${audience}</p>` : "";
}

const list = (items) =>
  Array.isArray(items) && items.length ? `<ol>${items.map((i) => `<li>${escape(i)}</li>`).join("")}</ol>` : "";

// The wait for a ruling, as text: a bar against the usual time, then a count
// that keeps going. Rulings measured so far took 26–37 s.
export const EXPECTED_S = 30;
const CELLS = 12;

export function progress(title, elapsed, expected = EXPECTED_S) {
  const seconds = Math.max(0, Math.floor(elapsed));
  const filled = Math.min(CELLS, Math.floor((seconds / expected) * CELLS));
  const bar = "▰".repeat(filled) + "▱".repeat(CELLS - filled);
  const late = seconds > expected;
  const line = late
    ? "Taking longer than usual — still thinking…"
    : `${escape(title)} is weighing your arguments…`;
  const count = late ? `${seconds} s` : `${seconds} s of ~${expected} s`;
  return `<p class="v-wait"><span class="v-scale">⚖️</span> ${line}</p>` +
    `<p class="v-progress">${bar}  ${count}</p>`;
}

export function renderRuling(ruling) {
  const judge = ruling?.judge ?? {};
  const signed = `<footer>Judged by ${escape(judge.title ?? judge.name)}` +
    `${ruling?.ms ? ` in ${Math.round(ruling.ms / 1000)} s` : ""}</footer>`;
  if (ruling?.status === "no_contest") {
    return `<section class="verdict none"><div class="v-outcome">No contest</div>` +
      `<p class="v-headline">Too little was said to rule on. State a position in a sentence, ` +
      `defend it for a few turns, and the judge will score the round.</p>` +
      `${statChips(ruling.stats)}${signed}</section>`;
  }
  if (ruling?.status !== "done" || !ruling.verdict) {
    return `<section class="verdict none"><div class="v-outcome">No verdict</div>` +
      `<p class="v-headline">The judge could not rule on this round: ${escape(ruling?.error || "unknown error")}.</p>` +
      `${statChips(ruling?.stats)}${signed}</section>`;
  }
  const v = ruling.verdict;
  const win = v.outcome === "win";
  const you = Number(v.split?.you) || 0;
  const fun = v.fun ?? {};
  const cards = (v.scorecard ?? []).map((c) => {
    const score = Math.max(1, Math.min(10, Number(c.score) || 1));
    return `<div class="v-row"><span>${escape(LABELS[c.criterion] ?? c.criterion)}</span>` +
      `<span class="v-bar"><i style="width:${score * 10}%"></i></span><b>${score}</b>` +
      `<small>${escape(c.evidence)}${at(c)}</small></div>`;
  }).join("");
  const fallacies = (v.fallacies ?? []).filter((f) => f?.name).map((f) =>
    `<li><b>${escape(f.name)}</b>${at(f)} — <q>${escape(f.quote)}</q></li>`).join("");
  const rematch = v.rematch ?? {};
  const position = v.position ?? {};
  const persuasion = v.persuasion ?? {};
  return `<section class="verdict ${win ? "win" : "lose"}">` +
    `<div class="v-top"><span class="v-outcome">${win ? "🏆 WIN" : "LOSE"}</span>` +
    (fun.badge ? `<span class="v-badge">🎖 ${escape(fun.badge)}</span>` : "") + `</div>` +
    `<div class="v-split" role="img" aria-label="You ${you}, advocate ${100 - you}">` +
    `<span class="you" style="width:${you}%">You ${you}</span>` +
    `<span class="adv">${100 - you} Advocate</span></div>` +
    `<p class="v-headline">${escape(v.headline)}</p>` +
    `<p>${escape(v.reasoning)}</p>` +
    statChips(ruling.stats) +
    (v.timing ? `<p class="v-muted">${escape(v.timing)}</p>` : "") +
    section("Your position", positionRows(position)) +
    section("Scorecard", cards && `<div class="v-card">${cards}</div>`) +
    section("Moments",
      moment("💪", "Best", v.moments?.best) +
      moment("🩹", "Weakest", v.moments?.worst) +
      moment("❓", "Left unanswered", v.moments?.unanswered)) +
    section("Fallacies", fallacies && `<ul>${fallacies}</ul>`) +
    section("Did it land?", landed(persuasion)) +
    section("How to improve", list(v.improve)) +
    section("Rematch brief",
      list(rematch.prepare) +
      (rematch.next_attack ? `<p><b>Next attack:</b> ${escape(rematch.next_attack)}</p>` : "") +
      (rematch.missed_angle ? `<p><b>The angle you missed:</b> ${escape(rematch.missed_angle)}</p>` : "")) +
    `<div class="v-fun">` +
    (fun.nickname ? `<p>🎭 Your debating name: <b>${escape(fun.nickname)}</b></p>` : "") +
    (fun.crowd ? `<p>📣 ${escape(fun.crowd)}</p>` : "") +
    (fun.roast ? `<p>🔥 ${escape(fun.roast)}</p>` : "") +
    `</div>${signed}</section>`;
}
