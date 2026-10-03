// The screen stays on for exactly as long as the conversation does.
//
// A phone locks its screen after a minute or two without a touch, and a locked
// screen is a suspended page: playback stops, the microphone stops, and a round
// carried on without anybody in it. Nothing the page does with timers or with
// traffic changes that — the screen is the operating system's, and only the
// platform can be asked to leave it alone. This is the asking.
//
// Everything the browser provides arrives as an argument, so node executes this
// (`tests/web/awake.test.mjs`). Three rules, each one a live failure avoided:
//
//  - asked for inside the user's tap, and only ever while the page is visible:
//    WebKit refuses a request made without transient activation, and every
//    platform refuses one made from a hidden document;
//  - asked for on `want` and on becoming visible, never on a timer: a device
//    that refused — low power mode, an in-app browser — must not be asked again
//    every second, and a device whose lock was taken back must not be asked in
//    a loop that only the platform can end;
//  - the outcome is one word, so the page can say what happened instead of
//    assuming the screen is being held.

// The state, and what each word means to whoever reads it:
//   idle        nothing asked yet: no conversation is running
//   held        the screen is being kept on
//   dropped     it was held and is not any more — the page was hidden, or the
//               system took it back, which low battery does
//   refused     the platform said no
//   failed      the request failed for some other reason
//   unsupported no wake lock here: iOS before 16.4, an in-app browser
//   released    let go on purpose, because the conversation ended
export const WORDS = {
  idle: "not asked yet",
  held: "kept awake",
  dropped: "dropped",
  refused: "refused by the system",
  failed: "failed",
  unsupported: "not supported by this browser",
  released: "let go",
};

// The page's own line, kept up to date rather than appended: telemetry, so it
// is shown with "stats" and hidden otherwise.
export function screenLine(state) {
  return `screen: ${WORDS[state] ?? state}`;
}

// Which words the record keeps: the ones that describe a lock the conversation
// had. Not `idle`, which is the start screen, and not `released`, which every
// ordinary round ends with — a line for either would be in every record to say
// nothing. The page still shows both, where they answer "is the screen being
// held right now?".
export function worthRecording(state) {
  return state !== "idle" && state !== "released";
}

// What to tell the user when the screen will not be held, in words that name
// the setting to change. Empty for the states that need no saying — including
// `dropped`, which happens on every glance at another tab and on coming back.
export function screenNote(state, err) {
  switch (state) {
    case "unsupported":
      return "this browser cannot keep the screen awake — if the phone sleeps, the agent " +
        "stops hearing you. Safari or Chrome over https can; an in-app browser cannot.";
    case "refused":
      return "the system would not keep the screen awake — low power mode refuses it " +
        "(on an iPhone: Settings → Battery → Low Power Mode). If the screen sleeps, the " +
        "agent stops hearing you.";
    case "failed":
      return `the screen could not be kept awake: ${err?.message || String(err)}`;
    default:
      return "";
  }
}

// The default state callback: a page that only reads `state` has nothing to be
// told. A named function rather than an inline arrow, so the parameter list
// stays one this project's own checks can read.
const nothing = () => {};

export function createAwake({ wakeLock, doc, onState = nothing } = {}) {
  const service = wakeLock === undefined ? navigator?.wakeLock : wakeLock;
  const page = doc === undefined ? document : doc;
  let wanted = false;    // a conversation is running: the screen should stay on
  let sentinel = null;   // the platform's handle, while it is held
  let inflight = null;   // the ask in progress, so a caller can wait for the answer
  let state = "idle";

  const hidden = () => page?.visibilityState === "hidden";

  function paint(next, err) {
    if (next === state) return;
    state = next;
    onState(state, err);
  }

  function gone() {
    // The platform's lock is not ours any more: it released it because the page
    // went away, or took it back. Nothing to release here, and the next moment
    // worth asking again is the next time the page is visible.
    const held = state === "held";
    sentinel = null;
    // Only a lock that was actually held can be dropped: a refusal stays a
    // refusal, however often the page is hidden and shown again.
    if (held) paint(wanted ? "dropped" : "released");
  }

  async function take() {
    if (!service) {
      paint("unsupported");
      return;
    }
    try {
      const held = await service.request("screen");
      // The answer can arrive after the reason for asking went away: the round
      // ended, or the page was hidden while the platform was deciding.
      if (!wanted || hidden()) {
        held.release?.();
        return;
      }
      sentinel = held;
      held.addEventListener?.("release", () => {
        // A lock already replaced or let go by us is not ours to report.
        if (sentinel === held) gone();
      });
      paint("held");
    } catch (err) {
      // An ask nobody is waiting for any more — the page went away, or the round
      // ended — is not a refusal. The word and the note would both name a reason
      // that is not the reason: every platform refuses a hidden document, so the
      // page would blame low power mode for a tab switch. The failure half of
      // the rule the success path above already states.
      if (!wanted || hidden()) return;
      // NotAllowedError is the ordinary no: low power mode, low battery, an
      // in-app browser.
      paint(err?.name === "NotAllowedError" ? "refused" : "failed", err);
    }
  }

  function ask() {
    if (!wanted || sentinel || inflight || hidden()) return;
    inflight = take()
      .catch(() => {})  // every failure is already a word, never a rejection
      .finally(() => { inflight = null; });
  }

  // Hidden is when the platform releases the lock itself, so the handle is
  // dropped here rather than waited for: a stale one would block every later
  // ask. Coming back is the one moment worth asking again.
  page?.addEventListener("visibilitychange", () => (hidden() ? gone() : ask()));

  return {
    want() {
      wanted = true;
      ask();
      return inflight ?? Promise.resolve();
    },

    release() {
      // Only a lock that was actually held is let go. A page that asked and was
      // refused keeps saying so — the reason is worth more than the release, and
      // it is the last word about the screen — and a page that never asked at
      // all, which is how a review of an ended conversation walks the ruling
      // path, says nothing. `wanted` still goes false either way: that is what
      // makes an answer still on its way release the lock it was granted.
      wanted = false;
      const held = sentinel;
      sentinel = null;
      held?.release?.();
      if (state === "held") paint("released");
      return Promise.resolve();
    },

    // The ask in flight, if any. Tests wait on it.
    settled() {
      return inflight ?? Promise.resolve();
    },

    get state() {
      return state;
    },
  };
}
