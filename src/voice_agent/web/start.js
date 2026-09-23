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
  llm: { anthropic: "Anthropic", openai: "OpenAI", deepseek: "DeepSeek" },
  stt: { assemblyai: "AssemblyAI", elevenlabs: "ElevenLabs Scribe" },
  tts: { elevenlabs: "ElevenLabs", openai: "OpenAI" },
};
const label = (group, name) => LABELS[group][name] ?? name;

function picked(group) {
  return startStack.querySelector(`input[name="${group}"]:checked`)?.value ?? "";
}

// The chosen engine and ears as a query string. The voice is never sent: it
// is one of a kind.
export function stackQuery() {
  const stack = new URLSearchParams();
  for (const group of ["llm", "stt"]) {
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
// is only drawn when asked (the voice); otherwise there is nothing to choose.
function choose(group, title, options, describe, { single = false } = {}) {
  if (options.length < (single ? 1 : 2)) return;
  const ordered = [...options].sort((a, b) => Number(!!b.default) - Number(!!a.default));
  const el = document.createElement("div");
  el.className = "pick";
  el.innerHTML =
    `<span>${title}</span>` +
    ordered.map((o) =>
      `<label><input type="radio" name="${group}" value="${o.name}"` +
      `${o.default ? " checked" : ""}>${describe(o)}</label>`).join("");
  startStack.appendChild(el);
}

function languagesLine(known) {
  const langs = chosenEars(known)?.languages ?? [];
  return langs.length
    ? `It hears <strong>${langs.length}</strong> languages: <code>${langs.join(" ")}</code>.`
    : "";
}

export function showStart(known) {
  choose("llm", "Reasoning engine", known.choices?.llm ?? [],
    (o) => `${label("llm", o.name)} <small>${o.model}</small>`);
  choose("stt", "Ears", known.choices?.stt ?? [],
    (o) => `${label("stt", o.name)} <small>${o.languages.length} languages</small>`);
  if (known.voice) {
    choose("tts", "Voice", [{ name: known.voice.provider, voice: known.voice.voice, default: true }],
      (o) => `${label("tts", o.name)} <small>${o.voice.slice(0, 10)}</small>`, { single: true });
  }

  const heard = notice(languagesLine(known));
  if (!heard.innerHTML) heard.remove();
  // The languages line follows the ears: the two recognizers differ most there.
  startStack.addEventListener("change", () => { heard.innerHTML = languagesLine(known); });

  if (!known.ears) notice("This agent has <strong>no microphone</strong> — typing only.");
  if (!known.voice) notice("This agent is <strong>silent</strong> — it will not speak.");
  if (known.recording) {
    notice(
      "This conversation is <strong>written down on the server</strong> — what is said, " +
      "what is typed, and how long each part took.<br>No audio is ever stored.");
  }
}
