// Messages the page sends up the socket. The server's side of this contract is
// `handle_text` in server.py; keeping the shapes in one place keeps them from
// drifting apart across the page.

export const userMessage = (text) => JSON.stringify({ type: "user_message", text });

export const listenStart = () => JSON.stringify({ type: "listen_start" });

export const listenStop = () => JSON.stringify({ type: "listen_stop" });

// How much of an interrupted reply was played, in milliseconds; null if none was
// playing. Carries the interrupt's id, so a late answer cannot settle the next one.
export const interrupted = (id, playedMs) =>
  JSON.stringify({ type: "interrupted", id, played_ms: playedMs });

// `report` carries playback telemetry (gaps) when playback ends.
export const playback = (active, report = {}) =>
  JSON.stringify({ type: "playback", active, ...report });
