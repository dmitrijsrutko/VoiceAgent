// Executed tests for the page side of playback, wired to the real audio-thread
// logic through a fake port. Run by `node --test`, driven from pytest.

import assert from "node:assert/strict";
import { test } from "node:test";

import { createPlayer, pcmToFloat32 } from "../../src/voice_agent/web/player.js";
import { createPlayback } from "../../src/voice_agent/web/playback-worklet.js";

// A context whose rendering only happens when a test asks, and only while it
// is running — as a real context renders nothing while suspended.
function harness({ state = "running", muted = false } = {}) {
  const h = { made: [], speaking: [], waiting: [], finished: [], errors: [], muted };
  const processor = createPlayback((msg) => h.node.port.onmessage({ data: msg }), 24000);
  // Messages cross the port by structured clone with transfer, exactly as they
  // cross threads in a browser: a transferred buffer is detached on this side.
  h.node = { port: { onmessage: null, postMessage: (msg, transfer = []) => processor.message(structuredClone(msg, { transfer })) } };
  h.ctx = {
    state,
    resume() { h.ctx.state = "running"; return Promise.resolve(); },
  };
  h.render = (quanta = 1) => {
    if (h.ctx.state !== "running") return;
    for (let i = 0; i < quanta; i++) processor.process(new Float32Array(128));
  };
  h.player = createPlayer({
    makeContext: (rate) => { h.made.push(rate); return h.ctx; },
    makeNode: async () => h.node,
    isMuted: () => h.muted,
    onSpeaking: (active, report) => h.speaking.push(report === undefined ? [active] : [active, report]),
    onWaiting: (resume) => h.waiting.push(resume),
    onFinished: (s) => h.finished.push(s),
    onError: (err) => h.errors.push(err),
  });
  return h;
}

// 480 samples = 20 ms at 24 kHz, a little under four quanta.
const CHUNK = () => new Int16Array(480).fill(1000).buffer;
const lastSpeaking = (h) => h.speaking.at(-1)?.[0];

test("pcm16 maps onto the float range and ignores a trailing odd byte", () => {
  const bytes = new Int16Array([-32768, 0, 16384, 32767]).buffer;
  const withOddByte = new Uint8Array(bytes.byteLength + 1);
  withOddByte.set(new Uint8Array(bytes));

  const out = pcmToFloat32(withOddByte.buffer);

  assert.equal(out.length, 4);
  assert.equal(out[0], -1);
  assert.equal(out[1], 0);
  assert.equal(out[2], 0.5);
  assert.ok(out[3] > 0.9999 && out[3] < 1);
});

test("chunks posted before the worklet is ready arrive in order once it is", async () => {
  const h = harness();
  h.player.start(null);
  h.player.chunk(CHUNK());
  h.player.end();
  assert.equal(h.speaking.length, 0, "nothing can play before the node exists");

  await h.player.ready();
  h.render(4);

  assert.deepEqual(h.speaking.map(([a]) => a), [true, false]);
});

test("speech plays through a context at the announced rate", async () => {
  const h = harness();
  h.player.setRate(22050);
  h.player.start(null);
  await h.player.ready();

  assert.deepEqual(h.made, [22050]);
});

test("the gate closes when audio is audible and opens only after end and the last sample", async () => {
  const h = harness();
  await h.player.ready();
  h.player.start("bubble");
  h.player.chunk(CHUNK());
  h.player.chunk(CHUNK());
  assert.deepEqual(h.speaking, [], "closed before anything was audible");

  h.render();
  assert.equal(lastSpeaking(h), true);

  assert.equal(h.player.end(), "bubble");
  assert.equal(lastSpeaking(h), true, "released on audio_end while audio is still coming out");

  h.render(5);
  assert.equal(lastSpeaking(h), true, "released before the last sample played");

  h.render(3);
  assert.deepEqual(h.speaking.at(-1), [false, { gaps: 0, gap_ms: 0 }]);
  assert.equal(h.finished.length, 1);
  assert.equal(h.finished[0].bubble, "bubble");
});

test("a reply that ran dry mid-stream reports its gap when it finishes", async () => {
  const h = harness();
  await h.player.ready();
  h.player.start("bubble");
  h.player.chunk(CHUNK());
  h.render(10);  // 480 real samples, then 800 missing
  h.player.chunk(CHUNK());
  h.player.end();
  h.render(10);

  assert.equal(h.finished[0].gaps, 1);
  assert.equal(h.finished[0].gapMs, Math.round((10 * 128 - 480) * 1000 / 24000));
  assert.deepEqual(h.speaking.at(-1), [false, { gaps: 1, gap_ms: h.finished[0].gapMs }]);
});

test("a suspended context waits for a gesture without holding the gate or dropping audio", async () => {
  const h = harness({ state: "suspended" });
  await h.player.ready();
  h.player.start(null);
  h.player.chunk(CHUNK());
  h.player.chunk(CHUNK());
  h.render(4);

  assert.equal(h.waiting.length, 1, "the gesture is asked for once per stream");
  assert.deepEqual(h.speaking, [], "the mic is muted for audio nobody can hear");

  h.waiting[0]();
  h.render();
  assert.equal(lastSpeaking(h), true, "queued speech did not play on resume");
});

test("sending a message drops speech still waiting on the gesture", async () => {
  const h = harness({ state: "suspended" });
  await h.player.ready();
  h.player.start("greeting");
  h.player.chunk(CHUNK());

  h.player.dropUnheard();
  await h.ctx.resume();
  h.render(8);

  assert.deepEqual(h.speaking, [], "the greeting would start and be cut off mid-word");
  assert.equal(h.player.end(), null);
});

test("speech that is audible is not dropped on send", async () => {
  const h = harness();
  await h.player.ready();
  h.player.start(null);
  h.player.chunk(CHUNK());
  h.render();

  h.player.dropUnheard();
  h.player.end();
  h.render(4);

  assert.equal(h.finished.length, 1, "audible speech was dropped");
});

test("a new reply replaces one still playing, and the old one's reports are ignored", async () => {
  const h = harness();
  await h.player.ready();
  h.player.start("first");
  h.player.chunk(CHUNK());
  h.player.end();
  h.render();
  assert.equal(lastSpeaking(h), true);

  h.player.start("second");
  assert.equal(lastSpeaking(h), false, "the replaced reply's gate was never released");
  h.player.chunk(CHUNK());
  h.render();
  assert.equal(lastSpeaking(h), true);
  assert.equal(h.finished.length, 0, "the replaced stream reported itself finished");

  h.player.end();
  h.render(4);
  assert.equal(lastSpeaking(h), false);
  assert.deepEqual(h.finished.map((s) => s.bubble), ["second"]);
});

test("muted speech queues nothing and holds nothing", async () => {
  const h = harness({ muted: true });
  await h.player.ready();
  h.player.start("bubble");
  h.player.chunk(CHUNK());
  assert.equal(h.player.end(), "bubble");
  h.render(8);

  assert.deepEqual(h.speaking.map(([active]) => active), [false]);
  assert.equal(h.finished.length, 1);
});

test("a context that cannot load the worklet reports it instead of failing silently", async () => {
  const h = harness();
  const failing = createPlayer({
    makeContext: () => h.ctx,
    makeNode: async () => { throw new Error("no AudioWorklet"); },
    isMuted: () => false, onSpeaking() {}, onWaiting() {}, onFinished() {},
    onError: (err) => h.errors.push(err),
  });
  failing.start(null);
  await failing.ready();

  assert.equal(h.errors[0]?.message, "no AudioWorklet");
});

test("a report still in flight for a replaced reply does not release the new reply's gate", async () => {
  // The worklet posts `finished` for stream 1 just as the page starts stream 2:
  // the message crosses threads and lands after the replacement.
  const h = harness();
  await h.player.ready();
  h.player.start("first");
  h.player.chunk(CHUNK());
  h.render();
  h.player.start("second");
  h.player.chunk(CHUNK());
  h.render();
  assert.equal(lastSpeaking(h), true);

  h.node.port.onmessage({ data: { type: "finished", stream: 1, gaps: 0, gapMs: 0 } });
  h.node.port.onmessage({ data: { type: "playing", stream: 1 } });

  assert.equal(lastSpeaking(h), true, "a late report for the old reply unmuted the mic mid-reply");
  assert.equal(h.finished.length, 0);
});

test("a reply's samples are counted even though posting transfers them away", async () => {
  // Transferring detaches the array on this side. Counting it afterwards read
  // zero, so `end()` believed nothing was queued and released the gate at once.
  const h = harness();
  await h.player.ready();
  h.player.start("bubble");
  h.player.chunk(CHUNK());
  h.render();
  h.player.end();

  assert.equal(lastSpeaking(h), true, "the gate reopened on audio_end while the reply was playing");
  h.render(4);
  assert.equal(lastSpeaking(h), false);
  assert.equal(h.finished[0].sent, 480);
});

test("stopping silences the reply, releases the gate, and reports how much was played", async () => {
  const h = harness();
  await h.player.ready();
  h.player.start("reply");
  h.player.chunk(CHUNK());  // 20 ms
  h.render(2);              // 256 samples ≈ 10.7 ms played
  assert.equal(lastSpeaking(h), true);

  const reports = [];
  h.player.stop((ms) => reports.push(ms));
  h.player.chunk(CHUNK());  // the rest of the reply, still arriving
  h.render(8);

  assert.equal(lastSpeaking(h), false, "the gate stayed closed after the user interrupted");
  assert.equal(reports.length, 1);
  assert.ok(Math.abs(reports[0] - (256 * 1000) / 24000) < 0.01, `reported ${reports[0]} ms`);
  assert.equal(h.finished.length, 0, "a stopped reply reported itself finished");
});

test("stopping with nothing playing answers at once: nothing to cut", () => {
  const h = harness();
  const reports = [];
  h.player.stop((ms) => reports.push(ms));

  assert.deepEqual(reports, [null]);
});

test("a stop that crosses the reply finishing still gets its answer", async () => {
  const h = harness();
  await h.player.ready();
  h.player.start("reply");
  h.player.chunk(CHUNK());
  h.player.end();
  // The worklet finishes the stream, but its report has not reached the page.
  const held = [];
  h.node.port.onmessage = ((deliver) => (event) => held.push(() => deliver(event)))(h.node.port.onmessage);
  h.render(8);
  const reports = [];
  h.player.stop((ms) => reports.push(ms));
  held.forEach((deliver) => deliver());

  assert.deepEqual(reports, [null], "the page waited forever for an answer about a finished reply");
});
