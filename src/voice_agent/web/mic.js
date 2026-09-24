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
// Why the microphone could not be opened, in words that say what to do. The
// browser's own text ("not allowed by the user agent or the platform in the
// current context") names no setting and no way out.
export function micFailure(err) {
  switch (err?.name) {
    case "NotAllowedError":
      return "the microphone is blocked for this page. Allow it in the browser's settings for " +
        "this site — on an iPhone: the aA menu → Website Settings → Microphone, and Settings → " +
        "Apps → your browser → Microphone. Inside another app's own browser — Telegram, " +
        "Instagram, WhatsApp — open the link in Safari or Chrome instead. Then reload.";
    case "NotFoundError":
      return "no microphone was found on this device.";
    case "NotReadableError":
      return "the microphone is busy — another app, or a call, may be using it.";
    case "SecurityError":
      return "the microphone needs a secure page: https.";
    default:
      return err?.message || String(err);
  }
}

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
