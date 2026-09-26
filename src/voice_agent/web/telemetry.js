// What each server frame says about its own cost, as the lines drawn under a
// bubble. Pure functions of a message — no DOM — so node runs them.

export function ms(v) { return v < 1000 ? `${v} ms` : `${(v / 1000).toFixed(1)} s`; }

const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;

// A finished reply: when it started, what it cost, and whether it was guessed.
export function replyLines(msg) {
  // Tokens only when the provider reported them: "0 tokens" would read as a measurement.
  const tokens = msg.output_tokens ? ` · ${msg.output_tokens} tokens` : "";
  // A guess was sent before the turn began, so its accept time is on another clock.
  const accepted = msg.accepted_ms != null && !msg.speculated ? ` (accepted at ${ms(msg.accepted_ms)})` : "";
  const connect = msg.connect_ms != null ? ` · 🔌 opened a connection first, ${ms(msg.connect_ms)}` : "";
  const retried = msg.attempts > 1 ? ` · ↻ sent ${msg.attempts}× — refused or dropped before` : "";
  const lines = [
    `💭 thought for ${ms(msg.ttft_ms)}${accepted}${connect}${retried} · ${msg.chars} chars${tokens} in ${msg.fragments} fragments · ${ms(msg.generation_ms)}`,
  ];
  if (msg.prompt_tokens) {
    const pct = Math.round(100 * msg.cached_tokens / msg.prompt_tokens);
    lines.push(`📥 ${msg.prompt_tokens} prompt tokens · ${msg.cached_tokens} cached (${pct}%)`);
  }
  if (msg.speculated) {
    lines.push(`⚡ started ${ms(msg.speculation_lead_ms)} before the turn ended — the reply was already being written`);
  }
  if (msg.speculations_discarded) {
    lines.push(`🗑 ${plural(msg.speculations_discarded, "guess", "guesses")} discarded · ${msg.speculation_wasted_chars} chars wasted`);
  }
  return lines;
}

// An unprompted line's timings are zero by construction — it was written before
// the turn began — so it says where the real cost is instead.
export const unpromptedLine = (msg) => `🗣 unprompted · rung ${msg.initiative} · ${msg.chars} chars · decided above`;

export function audioLine(msg) {
  const length = `${msg.seconds.toFixed(1)} s of speech · ${(msg.bytes / 1024).toFixed(0)} kB in ${msg.chunks} chunks`;
  const early = msg.audio_before_reply_end ? " · spoke before the reply was written" : "";
  // A pause the listener heard because the voice arrived late, and where.
  const late = msg.late_ms >= 500 ? ` · ⚠ voice ${ms(msg.late_ms)} late after “${msg.late_after}”` : "";
  return `🔊 ${length} · audio at ${ms(msg.first_audio_ms)}${early} · ${ms(msg.synthesis_first_byte_ms)} from first words to first sound · synthesized in ${ms(msg.synthesis_ms)}${late}`;
}

export function truncatedLine(msg) {
  const guess = msg.estimated ? " (estimated)" : msg.timed ? "" : " (approximate)";
  return `✋ interrupted after ${ms(msg.played_ms)} · heard ${msg.heard_chars} of ${msg.chars} chars${guess} · stopped ${ms(msg.stop_ms)} after the interruption was noticed`;
}

export function committedLine(msg) {
  // By the VAD when it heard you stop: the real wait. The recognizer's own
  // clock starts at its last word, which trails your voice.
  let line = msg.speech_end_ms != null
    ? `🎙 committed ${ms(msg.speech_end_ms)} after you stopped speaking · ${ms(msg.endpoint_ms)} after your last recognised word`
    : `🎙 committed ${ms(msg.endpoint_ms)} after your last recognised word`;
  if (msg.first_words_ms != null) line += ` · first words shown ${ms(msg.first_words_ms)} after you began`;
  if (msg.stable_words) {
    // A prefix that did not hold means agreement acted on words never said.
    line += ` · ${msg.stable_words} words settled early${msg.prefix_held ? "" : " ⚠ prefix did not hold"}`;
  }
  return line;
}

export const gapsLine = (s) => `⚠ ${plural(s.gaps, "gap")} · ${ms(s.gapMs)} of silence mid-reply`;

const VERDICTS = {
  spoke: "💬 had something to say",
  declined: "🤫 nothing worth saying",
  yielded: "🤫 had something, but you started talking",
  overran: "✂️ its answer ran on, so it went unsaid",
};

// Every consideration, spoken or not: the declines are the behaviour being built.
export function initiativeLine(msg) {
  const verdict = msg.decision === "failed"
    ? `⚠️ could not decide — ${msg.message}`
    : VERDICTS[msg.decision] || msg.decision;
  const cached = msg.cached_tokens ? `, ${msg.cached_tokens} cached` : "";
  const cost = msg.prompt_tokens ? ` · ${msg.prompt_tokens} in${cached}, ${msg.output_tokens} out` : "";
  return `${verdict} · ${(msg.quiet_ms / 1000).toFixed(0)}s quiet · rung ${msg.rung}/${msg.rungs} · decided in ${ms(msg.consider_ms)}${cost}`;
}

const MOVES = { challenge: "challenge", clarify: "ask", redirect: "redirect", summarise: "sum up" };
const WHEN = ["", "a “hm?”", "the next pause", "cutting in now"];

// The inner voice: what it would say, and why. Declines are counted elsewhere,
// since most considerations end in one; failures say what failed.
export function thoughtLine(msg) {
  const cost = msg.prompt_tokens
    ? ` · ${msg.prompt_tokens} in${msg.cached_tokens ? `, ${msg.cached_tokens} cached` : ""}, ${msg.output_tokens} out`
    : "";
  const timing = ` · ${ms(msg.consider_ms)} after ${msg.reason.replace("_", " ")}${cost}`;
  if (msg.decision === "thought") {
    const move = MOVES[msg.move] || msg.move;
    return `💭 would ${move} (${WHEN[msg.urgency] || `urgency ${msg.urgency}`}): “${msg.line}” — ${msg.why}${timing}`;
  }
  if (msg.decision === "capped") return "💭 thinking paused: this minute's budget of calls is spent";
  if (msg.decision === "nothing") return `🤫 nothing worth saying${timing}`;
  if (msg.decision === "unchanged") return `🤫 nothing new to think about · after ${msg.reason.replace("_", " ")}`;
  return `⚠️ could not think (${msg.decision}) — ${msg.message}${timing}`;
}

// The running line for declines: how many in a row, and the latest one.
export const quietLine = (count, msg) =>
  `🤫 ${plural(count, "consideration")}, nothing worth saying · last ${thoughtLine(msg)
    .replace("🤫 nothing worth saying · ", "").replace("🤫 nothing new to think about · ", "")}`;
