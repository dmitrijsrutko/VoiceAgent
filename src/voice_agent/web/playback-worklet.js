// Runs on the audio thread: one continuous output fed from a queue of PCM.
//
// Replaces one AudioBufferSourceNode per chunk. Every scheduled source node is
// work on *every* render quantum until it plays, so a 100-second reply (~4,600
// chunks, all scheduled within two seconds) cost the audio thread ~780 µs per
// 128-frame quantum on average — measured by offline rendering — and missed
// real-time deadlines: clicks and pitch wobble that eased as the nodes played
// out. A queue costs the same per quantum however long the reply is.
//
// The logic is exported so node can execute it; the processor class at the
// bottom exists only inside an AudioWorkletGlobalScope.

// Float samples in arrival order, read out in fixed-size quanta with no seams.
export class PcmQueue {
  constructor() {
    this.chunks = [];
    this.head = 0;          // index of the chunk being read
    this.offset = 0;        // samples already read from that chunk
    this.started = false;   // has any sample been played
    this.ended = false;     // has the server closed the stream
    this.dry = false;       // is the queue currently starved
    this.gaps = 0;
    this.gapFrames = 0;
  }

  push(samples) {
    if (samples.length) this.chunks.push(samples);
  }

  end() {
    this.ended = true;
  }

  get drained() {
    return this.ended && this.head === this.chunks.length;
  }

  // Fill `out` from the queue; any shortfall is silence. A shortfall between
  // the first sample and the end of the stream is a gap: the network fell
  // behind playback. Returns how many samples were real audio.
  pull(out) {
    let written = 0;
    while (written < out.length && this.head < this.chunks.length) {
      const chunk = this.chunks[this.head];
      const n = Math.min(out.length - written, chunk.length - this.offset);
      // A plain loop, not `out.set(chunk.subarray(...))`: `subarray` allocates
      // a view every quantum, and garbage collection on the audio thread is a
      // glitch of its own. This path allocates nothing.
      for (let i = 0; i < n; i++) out[written + i] = chunk[this.offset + i];
      written += n;
      this.offset += n;
      if (this.offset === chunk.length) {
        this.chunks[this.head++] = null;  // release it; compacted below
        this.offset = 0;
      }
    }
    // Drop consumed slots only once they are at least half the array. Audio
    // arrives far faster than it plays, so nearly the whole reply is queued: a
    // compaction every fixed number of chunks copied everything still waiting,
    // O(queued) each time. Waiting until the consumed part dominates means a
    // compaction copies fewer slots than were consumed since the last one —
    // amortized O(1) per chunk however long the reply.
    if (this.head > 256 && this.head * 2 > this.chunks.length) {
      this.chunks = this.chunks.slice(this.head);
      this.head = 0;
    }
    out.fill(0, written);

    if (written > 0) {
      this.started = true;
      this.dry = false;
    }
    const missing = out.length - written;
    if (missing && this.started && !this.ended) {
      if (!this.dry) this.gaps += 1;
      this.dry = true;
      this.gapFrames += missing;
    }
    return written;
  }
}

// How often a playing stream reports how far it has got: every 8 render quanta,
// ~43 ms at 24 kHz. Often enough for words to light up as they are said; each
// report is one small message across threads.
export const POSITION_QUANTA = 8;

// The processor's behaviour, independent of AudioWorkletProcessor. `post` sends
// a message back to the page; `rate` is the context's sample rate.
//
// Page -> here:  start {stream} · chunk {stream, samples} · end {stream} · drop {stream}
//                stop {stream}
// Here -> page:  playing {stream} once audio is audible · finished {stream, gaps, gapMs}
//                stopped {stream, played} — samples played, or null if it had already finished
//                position {stream, played} — samples played so far, while playing
export function createPlayback(post, rate) {
  let stream = null;  // { id, queue, playing, played }

  return {
    message(msg) {
      if (msg.type === "start") {
        // A new stream replaces whatever was playing, immediately.
        stream = { id: msg.stream, queue: new PcmQueue(), playing: false, played: 0, quanta: 0 };
        return;
      }
      if (msg.type === "stop") {
        // Always answered, even for a stream this thread has already finished:
        // the page is waiting on it to say how much of the reply was heard, and
        // "all of it" is an answer too.
        const current = stream && msg.stream === stream.id;
        post({ type: "stopped", stream: msg.stream, played: current ? stream.played : null });
        if (current) stream = null;
        return;
      }
      if (!stream || msg.stream !== stream.id) return;  // for a stream already replaced
      if (msg.type === "chunk") stream.queue.push(msg.samples);
      else if (msg.type === "end") stream.queue.end();
      else if (msg.type === "drop") stream = null;
    },

    process(out) {
      if (!stream) {
        out.fill(0);
        return;
      }
      const s = stream;
      const written = s.queue.pull(out);
      // Counted here, at the moment samples leave for the speaker — the only
      // place that knows how much of a reply was actually played.
      s.played += written;
      if (written > 0 && !s.playing) {
        s.playing = true;
        post({ type: "playing", stream: s.id });
      }
      // Counted in samples played, not time passed: a stream that ran dry
      // mid-reply has not moved on, and the words must not either.
      if (written > 0 && ++s.quanta % POSITION_QUANTA === 0) {
        post({ type: "position", stream: s.id, played: s.played });
      }
      if (s.queue.drained) {
        stream = null;
        post({
          type: "finished",
          stream: s.id,
          gaps: s.queue.gaps,
          gapMs: Math.round((s.queue.gapFrames * 1000) / rate),
        });
      }
    },
  };
}

if (typeof registerProcessor === "function") {
  class Playback extends AudioWorkletProcessor {
    constructor() {
      super();
      this.playback = createPlayback((msg) => this.port.postMessage(msg), sampleRate);
      this.port.onmessage = (event) => this.playback.message(event.data);
    }

    process(inputs, outputs) {
      this.playback.process(outputs[0][0]);
      return true;
    }
  }
  registerProcessor("playback", Playback);
}
