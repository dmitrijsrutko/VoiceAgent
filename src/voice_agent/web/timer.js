// The conversation's time limit, as the page shows it: nothing until the last
// minute, then a countdown that turns urgent near the end. Pure — no DOM — so
// node runs it.

export const WARN_MS = 60_000;
export const URGENT_MS = 15_000;

export function countdown(remainingMs) {
  if (remainingMs > WARN_MS) return { show: false, urgent: false, text: "" };
  const seconds = Math.max(0, Math.ceil(remainingMs / 1000));
  const text = `⏳ ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  return { show: true, urgent: remainingMs <= URGENT_MS, text };
}
