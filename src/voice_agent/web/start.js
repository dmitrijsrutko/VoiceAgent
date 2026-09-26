// The start screen: what this agent is and could be, before a socket exists.

import { startNotices, startStack } from "./ui.js";

// What the server wrote into the page (`server.with_facts`). Empty when the
// page was served some other way, which leaves a start screen claiming nothing.
export function servedFacts() {
  try {
    return JSON.parse(document.getElementById("facts").textContent) || {};
  } catch {
    return {};
  }
}

const LABELS = {
  stt: { assemblyai: "AssemblyAI", elevenlabs: "ElevenLabs Scribe" },
};
// Display names for the provider a model belongs to. Not a claim about what
// runs: the options themselves, and their titles, come from the server.
const PROVIDERS = { anthropic: "Claude", deepseek: "DeepSeek", openai: "OpenAI" };
const label = (group, name) => LABELS[group][name] ?? name;

// Role cards are text somebody wrote — soon, anybody — and this page builds
// HTML from strings, so what a card says is escaped on the way in.
const escape = (text) =>
  String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function picked(group) {
  return startStack.querySelector(`input[name="${group}"]:checked`)?.value ?? "";
}

// The chosen role, engine, ears and voice model as a query string.
export function stackQuery() {
  const stack = new URLSearchParams();
  for (const group of ["role", "llm", "stt", "tts"]) {
    const value = picked(group);
    if (value) stack.set(group, value);
  }
  return stack.toString();
}

// The ears currently picked, or the only ones there are.
export function chosenEars(known) {
  const choices = known.choices?.stt ?? [];
  return choices.find((o) => o.name === picked("stt")) ?? choices[0] ?? known.ears;
}

function notice(html) {
  const el = document.createElement("div");
  el.className = "notice";
  el.innerHTML = html;
  startNotices.appendChild(el);
  return el;
}

// One radio group of what the server offered, default first. A single option
// is only drawn when asked (the voice, so the page says what speaks); otherwise
// there is nothing to choose.
// Options sit in a grid of equal cells, so every group lines up the same way
// at every width.
function choose(group, title, options, describe, { single = false, byProvider = false } = {}) {
  if (options.length < (single ? 1 : 2)) return;
  const ordered = [...options].sort((a, b) => Number(!!b.default) - Number(!!a.default));
  const radios = (list) =>
    `<div class="opts">` + list.map((o) =>
      `<label><input type="radio" name="${group}" value="${o.name}"` +
      `${o.default ? " checked" : ""}><span>${describe(o)}</span></label>`).join("") + `</div>`;
  let body = radios(ordered);
  if (byProvider) {
    // The default's provider first, but each provider's models in menu order,
    // so the two rows line up column for column (low / high / max).
    const names = [...new Set(ordered.map((o) => o.provider))];
    body = `<div class="rows">` + names.map((p) =>
      `<div class="row"><b>${escape(PROVIDERS[p] ?? p)}</b>` +
      `${radios(options.filter((o) => o.provider === p))}</div>`).join("") + `</div>`;
  }
  const el = document.createElement("div");
  el.className = "pick";
  el.innerHTML = `<span>${title}</span>${body}`;
  startStack.appendChild(el);
}

export function showStart(known) {
  choose("role", "Role", known.choices?.role ?? [],
    (o) => `${escape(o.title)} <small>${escape(o.summary)}</small>`);
  // The model options carry their own titles: what a choice is called is the
  // server's to say, so the page cannot advertise a model it will not run.
  choose("llm", "Model", known.choices?.llm ?? [], (o) => escape(o.title), { byProvider: true });
  choose("stt", "Ears", known.choices?.stt ?? [],
    (o) => `${label("stt", o.name)} <small>${o.languages.length} languages</small>`);
  // Like the models, the voice options are titled by the server.
  if (known.voice) {
    choose("tts", "Voice", known.choices?.tts ?? [],
      (o) => `${escape(o.title)} <small>${escape(o.hint)}</small>`, { single: true });
  }

  if (!known.ears) notice("This agent has <strong>no microphone</strong> — typing only.");
  if (!known.voice) notice("This agent is <strong>silent</strong> — it will not speak.");
  if (known.recording) {
    notice(
      "This conversation is <strong>written down on the server</strong> — what is said, " +
      "what is typed, and how long each part took.<br>No audio is ever stored.");
  }
}
