// Runs on the audio thread. Converts float samples to the PCM16 the recognizer
// wants and posts them in 32 ms frames (512 samples at 16 kHz): one VAD window
// each, so the server hears a pause a window late rather than a 100 ms buffer
// late. The server regroups them into larger chunks for the recognizer.
class Capture extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Int16Array(512); this.n = 0; }
  process(inputs) {
    const ch = inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice()); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("capture", Capture);
