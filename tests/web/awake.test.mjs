// Executed tests for the screen wake lock: taken for the conversation, let go
// when it ends, and never asked for in a loop.

import assert from "node:assert/strict";
import { test } from "node:test";

import { createAwake, screenLine, screenNote, worthRecording } from "../../src/voice_agent/web/awake.js";

// A document that can be hidden, and a wake lock service that answers however a
// test wants. As in the browser, a request resolves with a sentinel that the
// page can release, that the system can take back, and whose `release` event
// fires either way.
//
// `pending` is the third answer a platform gives: neither yes nor no, yet. The
// test settles it itself with `grant()` or `refuse(name)`.
function harness({ supported = true, refuse = null, pending = false } = {}) {
  const h = { asked: 0, states: [], locks: [] };

  const sentinel = () => {
    const held = {
      released: false,
      handlers: [],
      addEventListener(_name, fn) { held.handlers.push(fn); },
      // Firing on release is what a browser does: a lock the page lets go of
      // reports itself released, exactly as one the system takes back does.
      release() {
        held.released = true;
        held.taken();
        return Promise.resolve();
      },
      taken() { for (const fn of held.handlers) fn(); },  // the platform let go
    };
    return held;
  };

  const doc = {
    visibilityState: "visible",
    listeners: [],
    addEventListener(_name, fn) { doc.listeners.push(fn); },
    fire() { for (const fn of doc.listeners) fn(); },
    hide() { doc.visibilityState = "hidden"; doc.fire(); },
    show() { doc.visibilityState = "visible"; doc.fire(); },
  };

  let settle = null;  // the pending answer's yes/no
  const wakeLock = supported
    ? {
        request() {
          h.asked += 1;
          if (pending) return new Promise((yes, no) => { settle = { yes, no }; });
          if (refuse) return Promise.reject(Object.assign(new Error("no"), { name: refuse }));
          const held = sentinel();
          h.locks.push(held);
          return Promise.resolve(held);
        },
      }
    : undefined;

  h.doc = doc;
  h.grant = () => { const held = sentinel(); h.locks.push(held); settle.yes(held); };
  h.refuse = (name) => settle.no(Object.assign(new Error("no"), { name }));

  h.awake = createAwake({ wakeLock, doc, onState: (state, err) => h.states.push([state, err]) });
  return h;
}

test("the lock is taken when the conversation is wanted, and let go when it ends", async () => {
  const h = harness();

  await h.awake.want();

  assert.equal(h.asked, 1);
  assert.equal(h.awake.state, "held");
  assert.equal(h.locks[0].released, false);

  await h.awake.release();

  assert.equal(h.awake.state, "released");
  assert.equal(h.locks[0].released, true);
});

test("a lock that was never held is not announced as let go", async () => {
  const said = (h) => h.states.map(([state]) => state);

  const idle = harness();  // a review of an ended conversation: nothing was asked
  await idle.awake.release();
  assert.deepEqual(said(idle), []);
  assert.equal(idle.awake.state, "idle");

  // The reason is worth more than the release: "let go" would be the last word
  // about a screen the platform never let this page hold.
  const refused = harness({ refuse: "NotAllowedError" });
  await refused.awake.want();
  await refused.awake.release();
  assert.deepEqual(said(refused), ["refused"]);
  assert.equal(refused.awake.state, "refused");

  const unsupported = harness({ supported: false });
  await unsupported.awake.want();
  await unsupported.awake.release();
  assert.deepEqual(said(unsupported), ["unsupported"]);
  assert.equal(unsupported.awake.state, "unsupported");
});

test("asking still works after letting go of nothing", async () => {
  const h = harness();

  await h.awake.release();  // nothing was asked
  await h.awake.want();
  assert.equal(h.awake.state, "held");

  await h.awake.release();
  assert.equal(h.awake.state, "released");

  await h.awake.want();  // and a round after that is a new ask
  assert.equal(h.awake.state, "held");
});

test("nothing is asked before the tap, and a second want asks nothing again", async () => {
  const h = harness();

  h.doc.hide();
  h.doc.show();  // a glance at another tab, from the start screen
  assert.equal(h.asked, 0);
  assert.equal(h.awake.state, "idle");

  await h.awake.want();
  await h.awake.want();

  assert.equal(h.asked, 1);
});

test("a hidden page is not asked, and becoming visible is what asks again", async () => {
  const h = harness();
  h.doc.hide();

  await h.awake.want();
  assert.equal(h.asked, 0);
  assert.equal(h.awake.state, "idle");

  h.doc.show();
  await h.awake.settled();

  assert.equal(h.asked, 1);
  assert.equal(h.awake.state, "held");
});

test("hiding drops the lock, coming back holds it again — and the old one stays dead", async () => {
  const h = harness();
  await h.awake.want();
  const first = h.locks[0];

  h.doc.hide();
  assert.equal(h.awake.state, "dropped");

  h.doc.show();
  await h.awake.settled();
  assert.equal(h.asked, 2);
  assert.equal(h.awake.state, "held");

  // The platform's release for the lock we already forgot arrives late: it must
  // not report the lock we are holding now as gone.
  first.taken();
  assert.equal(h.awake.state, "held");
});

test("a lock the system takes back is reported, and not asked for in a loop", async () => {
  const h = harness();
  await h.awake.want();

  h.locks[0].taken();  // low battery: the platform let go on its own

  assert.equal(h.awake.state, "dropped");
  assert.equal(h.asked, 1, "asked again with no reason to think the answer changed");

  h.doc.hide();
  h.doc.show();
  await h.awake.settled();
  assert.equal(h.asked, 2);
});

test("a lock granted after the round ended is released at once", async () => {
  const h = harness();

  const want = h.awake.want();
  h.awake.release();  // the conversation ended while the platform was deciding
  await want;

  assert.equal(h.locks[0].released, true, "a lock nobody is waiting for must not be held");
  // Nothing was ever held, so there is nothing to announce: the page never said
  // anything about the screen, and still does not.
  assert.equal(h.awake.state, "idle");
  assert.deepEqual(h.states, []);

  await h.awake.want();  // the next round is a new ask
  assert.equal(h.asked, 2);
});

test("a refusal is a word with a way out, never a throw", async () => {
  const h = harness({ refuse: "NotAllowedError" });

  await h.awake.want();

  assert.equal(h.awake.state, "refused");
  assert.match(screenNote("refused"), /Low Power Mode/);
  assert.equal(screenLine("refused"), "screen: refused by the system");
});

test("an ask abandoned by hiding is not a refusal, and the return asks again", async () => {
  const h = harness({ pending: true });

  const want = h.awake.want();
  h.doc.hide();  // the page went away while the platform was deciding
  h.refuse("NotAllowedError");
  await want;

  // Nothing said: no state, and so no note naming a setting that is not the
  // reason. A hidden document is refused by every platform, which says nothing
  // about this one.
  assert.deepEqual(h.states, []);
  assert.equal(h.awake.state, "idle");

  h.doc.show();
  h.grant();  // the platform answers the second ask with a yes
  await h.awake.settled();

  assert.equal(h.asked, 2);
  assert.equal(h.awake.state, "held");
});

test("an ask whose conversation ended is not a failure either", async () => {
  const h = harness({ pending: true });

  const want = h.awake.want();
  h.awake.release();  // ⏹ End, while the platform was still deciding
  h.refuse("NotAllowedError");
  await want;

  // Neither the refusal nor a release: nothing was ever held, so the page has
  // nothing to say about the screen — and no note blaming the wrong setting.
  assert.equal(h.awake.state, "idle");
  assert.deepEqual(h.states, []);
});

test("a refusal is not asked about again until the page comes back", async () => {
  const h = harness({ refuse: "NotAllowedError" });

  await h.awake.want();
  assert.equal(h.asked, 1);

  // Nothing asks on its own while the page sits there refused.
  await new Promise((done) => setImmediate(done));
  assert.equal(h.asked, 1);

  h.doc.hide();
  h.doc.show();
  await h.awake.settled();

  assert.equal(h.asked, 2);
});

test("a browser without the API says so rather than throwing", async () => {
  const h = harness({ supported: false });

  await h.awake.want();

  assert.equal(h.awake.state, "unsupported");
  assert.match(screenNote("unsupported"), /cannot keep the screen awake/);
});

test("the ordinary states have nothing to say to the user", () => {
  assert.equal(screenNote("held"), "");
  assert.equal(screenNote("dropped"), "");  // every glance at another tab
  assert.equal(screenNote("released"), "");

  assert.equal(screenLine("held"), "screen: kept awake");

  const err = Object.assign(new Error("something odd"), { name: "InvalidStateError" });
  assert.match(screenNote("failed", err), /something odd/);
});

test("the record keeps the words that describe a lock, and only those", () => {
  for (const state of ["held", "dropped", "refused", "failed", "unsupported"]) {
    assert.equal(worthRecording(state), true, `${state} describes a lock`);
  }
  // The start screen, and the end of every ordinary round: a line either would
  // put in every record to say nothing.
  assert.equal(worthRecording("idle"), false);
  assert.equal(worthRecording("released"), false);
});
