// What kind of browser this is, coarsely: enough to tell "Safari on an iPhone"
// or "inside Telegram" from a record, never the user-agent string itself —
// that is close to a fingerprint, and the record is kept on a server.

const IN_APP = [
  ["Telegram", /Telegram/i], ["Instagram", /Instagram/], ["Facebook", /FBAN|FBAV|FB_IAB/],
  ["WhatsApp", /WhatsApp/i], ["WeChat", /MicroMessenger/], ["TikTok", /musical_ly|TikTok/i],
  ["Snapchat", /Snapchat/], ["LINE", /\bLine\//], ["LinkedIn", /LinkedInApp/],
];

const BROWSERS = [
  ["Edge", /EdgiOS|EdgA|Edg\//], ["Opera", /OPR\/|OPiOS/], ["Samsung Internet", /SamsungBrowser/],
  ["Yandex", /YaBrowser/], ["Firefox", /FxiOS|Firefox\//], ["Chrome", /CriOS|Chrome\//],
  ["Safari", /Version\/[\d.]+.*Safari/],
];

const first = (table, ua) => table.find(([, re]) => re.test(ua))?.[0] ?? null;

export function clientFacts(ua) {
  const os = /iPhone|iPad|iPod/.test(ua) ? "iOS"
    : /Android/.test(ua) ? "Android"
    : /Mac OS X/.test(ua) ? "macOS"
    : /Windows/.test(ua) ? "Windows"
    : /Linux/.test(ua) ? "Linux" : "other";
  return {
    browser: first(BROWSERS, ua) ?? "other",
    os,
    mobile: /Mobi|iPhone|iPod|Android/.test(ua) || /iPad/.test(ua),
    in_app: first(IN_APP, ua),
  };
}
