// Messages the page sends up the socket. The server's side of this contract is
// `handle_text` in server.py; keeping the shapes in one place keeps them from
// drifting apart across the page.

export const userMessage = (text) => JSON.stringify({ type: "user_message", text });

export const listenStart = () => JSON.stringify({ type: "listen_start" });

export const listenStop = () => JSON.stringify({ type: "listen_stop" });

// `report` carries playback telemetry (gaps) when playback ends.
export const playback = (active, report = {}) =>
  JSON.stringify({ type: "playback", active, ...report });
