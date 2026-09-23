// Microphone capture: permission, the capture graph, PCM16 frames out.

// A real file rather than a Blob URL: the worklet is now parsed under test and
// served like every other module. Resolved against this module so the page
// does not hard-code where static files are mounted.
const WORKLET = new URL("./capture-worklet.js", import.meta.url);

// `onFrame` receives each Int16Array of PCM; whether to send it (the half-duplex
// gate) is the caller's decision, not the microphone's.
//
// Permission is asked here, when the microphone is actually wanted: the start
// click, before the greeting can play.
export async function buildMic(sampleRate, onFrame) {
  // Capture at the recognizer's own rate so nothing resamples anywhere.
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true,
             channelCount: 1, sampleRate },
  });
  const context = new AudioContext({ sampleRate });
  await context.audioWorklet.addModule(WORKLET);
  const node = new AudioWorkletNode(context, "capture");
  node.port.onmessage = (e) => onFrame(e.data);
  context.createMediaStreamSource(stream).connect(node);
  return { context, node, stream };
}
