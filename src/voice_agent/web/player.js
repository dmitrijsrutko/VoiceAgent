// The streaming speech player: PCM chunks in, one continuous output out.
//
// The page side of playback-worklet.js. It converts chunks, posts them to the
// worklet, and turns what the worklet reports into the page's rules: when the
// half-duplex gate closes and opens, what waits for an autoplay gesture, what a
// new reply replaces. Deliberately free of the DOM and the socket — everything
// it needs from the page arrives as callbacks, so node can execute it.

const WORKLET = new URL("./playback-worklet.js", import.meta.url);

// PCM s16le mono -> float samples. Int16Array reads in platform byte order,
// which is little endian on every platform a browser runs on; a trailing odd
// byte (never sent, but cheap to tolerate) is ignored.
export function pcmToFloat32(bytes) {
  const samples = new Int16Array(bytes, 0, bytes.byteLength >> 1);
  const out = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i++) out[i] = samples[i] / 32768;
  return out;
}

async function playbackNode(context) {
  await context.audioWorklet.addModule(WORKLET);
  const node = new AudioWorkletNode(context, "playback", { outputChannelCount: [1] });
  node.connect(context.destination);
  return node;
}

export function createPlayer({
  makeContext, makeNode = playbackNode, isMuted, onSpeaking, onWaiting, onFinished, onError,
  onPosition,
}) {
  let output = null;       // the AudioContext speech plays through, made on first use
  let ready = null;        // resolves once the worklet node is wired
  let port = null;
  const pending = [];      // messages posted before the worklet was ready, in order
  let rate = 24000;        // the voice's PCM rate, as announced in `ready`
  let speech = null;       // the reply currently streaming in / playing, if any
  let streams = 0;
  const stopping = new Map();  // stream id -> callback waiting to hear how much played

  function context() {
    // At the voice's own rate, not the device's: the worklet then plays the
    // stream sample for sample and the browser resamples the output once.
    if (!output) {
      output = makeContext(rate);
      ready = makeNode(output).then((node) => {
        port = node.port;
        port.onmessage = (event) => receive(event.data);
        for (const [msg, transfer] of pending) port.postMessage(msg, transfer);
        pending.length = 0;
      }, (err) => onError?.(err));
    }
    return output;
  }

  function post(msg, transfer = []) {
    context();
    if (port) port.postMessage(msg, transfer);
    else pending.push([msg, transfer]);
  }

  function finish(s, gaps, gapMs) {
    speech = null;
    s.gaps = gaps;
    s.gapMs = gapMs;
    onFinished(s);
    onSpeaking(false, { gaps, gap_ms: gapMs });
  }

  function receive(msg) {
    if (msg.type === "stopped") {
      // Answered by stream id, not by `speech`: the stream was cleared when the
      // stop was asked for, and a `finished` can cross the stop on the way.
      const report = stopping.get(msg.stream);
      stopping.delete(msg.stream);
      report?.(msg.played === null ? null : (msg.played * 1000) / rate);
      return;
    }
    // Reports about a stream that has since been replaced or dropped are stale.
    if (!speech || msg.stream !== speech.id) return;
    if (msg.type === "playing") {
      // Audible now — which on a suspended context means only after resume, so
      // the gate is never held for audio nobody can hear.
      speech.waiting = false;
      onSpeaking(true);
    } else if (msg.type === "position") {
      // Milliseconds of this reply played so far, for lighting up its words.
      onPosition?.(speech.bubble, (msg.played * 1000) / rate);
    } else if (msg.type === "finished") {
      // Sent only once the server has closed the stream *and* the last sample
      // has played: either alone would release the gate while audio is still
      // coming out.
      finish(speech, msg.gaps, msg.gapMs);
    }
  }

  return {
    setRate(value) { rate = value; },

    // A gesture: the one moment autoplay is allowed.
    resume() { return context().resume(); },

    // Seconds between a sample leaving the worklet and leaving the speaker.
    outputLatency() { return output?.outputLatency ?? 0; },

    // Resolves once the output is wired. Tests and benchmarks wait on it.
    ready() { context(); return ready; },

    start(bubble) {
      if (speech) onSpeaking(false);  // the worklet drops the old stream on `start`
      speech = { id: ++streams, sent: 0, waiting: false, gaps: 0, gapMs: 0, bubble };
      post({ type: "start", stream: speech.id });
    },

    chunk(bytes) {
      const s = speech;
      if (!s || isMuted()) return;
      const samples = pcmToFloat32(bytes);
      // Counted before posting: the buffer is transferred, not copied, and a
      // transferred array reads as empty — which made every reply look like it
      // queued nothing, so `end()` reopened the gate while the agent spoke.
      s.sent += samples.length;
      post({ type: "chunk", stream: s.id, samples }, [samples.buffer]);
      const ctx = context();
      if (ctx.state !== "running" && !s.waiting) {
        // Autoplay is refused until a gesture, and the greeting arrives before
        // any. The audio stays queued in the worklet and plays on resume; the
        // gate stays open meanwhile, or a user who never clicks would hold the
        // microphone muted with nothing being said.
        s.waiting = true;
        onWaiting(() => ctx.resume());
      }
    },

    // Returns the bubble whose speech the server just closed, if any.
    end() {
      if (!speech) return null;
      const s = speech;
      if (s.sent === 0) finish(s, 0, 0);  // muted, so nothing was queued and nothing will play
      else post({ type: "end", stream: s.id });
      return s.bubble;
    },

    // The user talked over the agent: silence now, and say how many
    // milliseconds of the reply had been played — `null` when nothing was
    // playing, which means whatever was sent was heard in full.
    stop(report) {
      const s = speech;
      if (!s) { report(null); return; }
      speech = null;
      onSpeaking(false);
      if (s.sent === 0) { report(null); return; }  // muted: read, not heard, and read in full
      stopping.set(s.id, report);
      post({ type: "stop", stream: s.id });
    },

    dropUnheard() {
      // Speech still waiting on the autoplay gesture — the greeting, in practice.
      // Sending a message is that gesture, so resuming would start the greeting
      // only for the reply to cut it off a second later, mid-word. The user has
      // moved on; the greeting's text is already on screen.
      if (!speech || !speech.waiting) return;
      post({ type: "drop", stream: speech.id });
      speech = null;
    },
  };
}
