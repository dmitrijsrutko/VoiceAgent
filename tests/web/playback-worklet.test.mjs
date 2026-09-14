// Executed tests for the audio-thread side of playback: the PCM queue and the
// processor's message handling. Run by `node --test`, driven from pytest.

import assert from "node:assert/strict";
import { test } from "node:test";

import { POSITION_QUANTA, PcmQueue, createPlayback } from "../../src/voice_agent/web/playback-worklet.js";

const quantum = () => new Float32Array(4);
const samples = (...values) => Float32Array.from(values);

test("samples come out in order across chunk boundaries, with no seams", () => {
  const q = new PcmQueue();
  q.push(samples(1, 2, 3));
  q.push(samples(4, 5));
  q.push(samples(6, 7, 8, 9));

  const a = quantum(), b = quantum();
  assert.equal(q.pull(a), 4);
  assert.equal(q.pull(b), 4);

  assert.deepEqual([...a, ...b], [1, 2, 3, 4, 5, 6, 7, 8]);
});

test("a shortfall is filled with silence", () => {
  const q = new PcmQueue();
  q.push(samples(1, 2));
  const out = Float32Array.from([9, 9, 9, 9]);

  assert.equal(q.pull(out), 2);
  assert.deepEqual([...out], [1, 2, 0, 0]);
});

test("silence before the first sample is waiting, not a gap", () => {
  const q = new PcmQueue();
  q.pull(quantum());
  q.pull(quantum());

  assert.equal(q.gaps, 0);
});

test("running dry mid-stream is one gap however many quanta it lasts", () => {
  const q = new PcmQueue();
  q.push(samples(1, 2, 3, 4, 5, 6));
  q.pull(quantum());          // 4 real samples
  q.pull(quantum());          // 2 real, 2 missing: the gap starts
  q.pull(quantum());          // 4 missing: the same gap
  q.push(samples(1, 2, 3, 4));
  q.pull(quantum());          // audio again

  assert.equal(q.gaps, 1);
  assert.equal(q.gapFrames, 6);
});

test("silence after the stream ends is the end, not a gap", () => {
  const q = new PcmQueue();
  q.push(samples(1, 2));
  q.end();
  q.pull(quantum());

  assert.equal(q.gaps, 0);
  assert.equal(q.drained, true);
});

test("a queue is not drained until the server has ended it", () => {
  const q = new PcmQueue();
  q.push(samples(1));
  q.pull(quantum());

  assert.equal(q.drained, false, "the reply could still be arriving");
});

test("a long reply is read through without losing or reordering samples", () => {
  const q = new PcmQueue();
  let next = 0;
  for (let c = 0; c < 1000; c++) q.push(Float32Array.from({ length: 512 }, () => next++));
  q.end();

  const out = new Float32Array(128);
  let expected = 0;
  while (!q.drained) {
    const n = q.pull(out);
    for (let i = 0; i < n; i++) assert.equal(out[i], expected++);
  }
  assert.equal(expected, next);
});

test("compaction costs amortized O(1) per chunk, however much is still queued", () => {
  // Count every slot copied by compaction. A whole reply arrives long before it
  // plays, so this is the case that matters: everything queued up front.
  let copied = 0;
  class CountingArray extends Array {
    slice(start = 0, end = this.length) {
      copied += Math.max(0, end - start);
      return super.slice(start, end);
    }
  }
  const q = new PcmQueue();
  q.chunks = new CountingArray();
  const CHUNKS = 20000;  // ~7 minutes of speech in 512-sample chunks
  for (let c = 0; c < CHUNKS; c++) q.push(new Float32Array(512));
  q.end();

  const out = new Float32Array(128);
  while (!q.drained) q.pull(out);

  assert.ok(copied <= CHUNKS, `compaction copied ${copied} slots to play ${CHUNKS} chunks`);
});

test("chunks pushed while earlier ones play come out in order through compactions", () => {
  const q = new PcmQueue();
  let pushed = 0, expected = 0;
  const out = new Float32Array(128);
  for (let round = 0; round < 400; round++) {
    for (let c = 0; c < 3; c++) q.push(Float32Array.from({ length: 100 }, () => pushed++));
    for (let r = 0; r < 2; r++) {
      const n = q.pull(out);
      for (let i = 0; i < n; i++) assert.equal(out[i], expected++);
    }
  }
  q.end();
  while (!q.drained) {
    const n = q.pull(out);
    for (let i = 0; i < n; i++) assert.equal(out[i], expected++);
  }
  assert.equal(expected, pushed);
});

function processor() {
  const posted = [];
  const p = createPlayback((msg) => posted.push(msg), 1000);
  return { p, posted, render: (n = 1) => { for (let i = 0; i < n; i++) p.process(quantum()); } };
}

test("the processor reports audible once, then finished after end and the last sample", () => {
  const { p, posted, render } = processor();
  p.message({ type: "start", stream: 1 });
  p.message({ type: "chunk", stream: 1, samples: samples(1, 2, 3, 4, 5, 6) });
  render();
  assert.deepEqual(posted, [{ type: "playing", stream: 1 }]);

  p.message({ type: "end", stream: 1 });
  assert.equal(posted.length, 1, "finished before the last samples played");
  render();

  assert.deepEqual(posted.at(-1), { type: "finished", stream: 1, gaps: 0, gapMs: 0 });
});

test("gaps are reported in milliseconds at the context rate", () => {
  const { p, posted, render } = processor();
  p.message({ type: "start", stream: 1 });
  p.message({ type: "chunk", stream: 1, samples: samples(1, 2) });
  render(3);  // 2 real samples, then 10 missing
  p.message({ type: "chunk", stream: 1, samples: samples(1) });
  p.message({ type: "end", stream: 1 });
  render();

  assert.deepEqual(posted.at(-1), { type: "finished", stream: 1, gaps: 1, gapMs: 10 });
});

test("a new stream replaces the old one, and messages for the old one are ignored", () => {
  const { p, posted, render } = processor();
  p.message({ type: "start", stream: 1 });
  p.message({ type: "chunk", stream: 1, samples: samples(1, 1, 1, 1, 1, 1, 1, 1) });
  p.message({ type: "start", stream: 2 });
  p.message({ type: "chunk", stream: 1, samples: samples(9, 9, 9, 9) });  // late, stale
  p.message({ type: "chunk", stream: 2, samples: samples(2, 2, 2, 2) });
  const out = quantum();
  p.process(out);

  assert.deepEqual([...out], [2, 2, 2, 2]);
  assert.deepEqual(posted, [{ type: "playing", stream: 2 }]);
});

test("a dropped stream goes silent and reports nothing", () => {
  const { p, posted } = processor();
  p.message({ type: "start", stream: 1 });
  p.message({ type: "chunk", stream: 1, samples: samples(1, 2, 3, 4) });
  p.message({ type: "drop", stream: 1 });
  const out = Float32Array.from([9, 9, 9, 9]);
  p.process(out);

  assert.deepEqual([...out], [0, 0, 0, 0]);
  assert.deepEqual(posted, []);
});

test("a stopped stream goes silent and says how many samples actually played", () => {
  // Only this thread knows: what was pulled for the speaker, not what arrived.
  const { p, posted, render } = processor();
  p.message({ type: "start", stream: 1 });
  p.message({ type: "chunk", stream: 1, samples: samples(1, 1, 1, 1, 1, 1) });
  render();  // 4 of 6 played
  p.message({ type: "stop", stream: 1 });
  const out = Float32Array.from([9, 9, 9, 9]);
  p.process(out);

  assert.deepEqual([...out], [0, 0, 0, 0]);
  assert.deepEqual(posted.at(-1), { type: "stopped", stream: 1, played: 4 });
});

test("stopping a stream that already finished still answers, with nothing to cut", () => {
  const { p, posted, render } = processor();
  p.message({ type: "start", stream: 1 });
  p.message({ type: "chunk", stream: 1, samples: samples(1, 1) });
  p.message({ type: "end", stream: 1 });
  render(2);
  p.message({ type: "stop", stream: 1 });

  assert.deepEqual(posted.at(-1), { type: "stopped", stream: 1, played: null });
});

test("a playing stream reports how far it has got, in samples actually played", () => {
  const { p, posted } = processor();
  p.message({ type: "start", stream: 1 });
  p.message({ type: "chunk", stream: 1, samples: new Float32Array(4 * POSITION_QUANTA) });
  for (let i = 0; i < POSITION_QUANTA; i++) p.process(quantum());

  assert.deepEqual(posted.filter((m) => m.type === "position"), [
    { type: "position", stream: 1, played: 4 * POSITION_QUANTA },
  ]);
});

test("a stream that has run dry does not report moving on", () => {
  // Starved quanta play silence, not the reply: its words must wait too.
  const { p, posted } = processor();
  p.message({ type: "start", stream: 1 });
  p.message({ type: "chunk", stream: 1, samples: new Float32Array(4) });
  for (let i = 0; i < 4 * POSITION_QUANTA; i++) p.process(quantum());

  assert.deepEqual(posted.filter((m) => m.type === "position"), []);
});
