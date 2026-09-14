// Karaoke: how much of a reply has been spoken, from how much audio has played.
//
// The page's half of `heard.py`. The server sends each character's end time
// (`marks`), the worklet reports samples played, and this says where the words
// the listener has heard stop. Deliberately the same rule as the server's cut
// after an interruption — a word counts once its last letter has sounded — so
// the highlight freezes exactly where the history is cut.

const SPACE = /\s/;

// Extend a reply's timeline with the marks for audio starting at `fromMs`.
// Earlier characters are capped there — nothing said before this audio can
// still be sounding once it starts — the same rule as `Spoken.add` in heard.py.
export function addMarks(ends, fromMs, endsMs) {
  for (let i = 0; i < ends.length; i++) ends[i] = Math.min(ends[i], fromMs);
  ends.push(...endsMs);
}

// Characters of `text` spoken after `playedMs`, snapped back to a whole word.
// `endsMs[i]` is when character i finishes sounding, from the reply's first
// sample. With no timing there is nothing to follow, and the text is shown whole.
export function spokenChars(text, endsMs, playedMs) {
  if (!endsMs.length) return text.length;
  let count = 0;
  while (count < endsMs.length && count < text.length && endsMs[count] <= playedMs) count++;
  if (count >= text.length) return text.length;
  // Judged against the written text, which may run past what has been timed so
  // far: speech can pause mid-word while the next segment is made.
  if (!SPACE.test(text[count]) && !SPACE.test(text[count - 1] ?? " ")) {
    count = Math.max(text.lastIndexOf(" ", count - 1), 0);
  }
  return count;
}
