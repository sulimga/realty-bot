// Чиста логіка (без chrome.*) — щоб можна було тестувати в node.
const NOISE = /^(_ga|_gid|_gat|_fbp|_fbc|_gcl|_hj|_clck|_clsk|__gads|__gpi|FPID|FPLC|_ttp|_tt_|_uet|_pin_|IDE$|NID$)/;

function jwtExp(token) {
  try {
    const p = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const json = JSON.parse(atob(p + "=".repeat((4 - (p.length % 4)) % 4)));
    return typeof json.exp === "number" ? json.exp : null;
  } catch (e) { return null; }
}

function minutesLeft(token, nowSec) {
  const exp = jwtExp(token);
  return exp == null ? null : Math.floor((exp - nowSec) / 60);
}

// cookies: [{name, value}], token: "eyJ…" | null  ->  текст для бота
function buildPayload(token, cookies) {
  const seen = new Set();
  const pairs = [];
  for (const c of cookies) {
    if (!c.name || NOISE.test(c.name) || seen.has(c.name)) continue;
    seen.add(c.name);
    pairs.push(c.name + "=" + c.value);
  }
  const lines = [];
  if (token) lines.push("Authorization: Bearer " + token);
  if (pairs.length) lines.push("Cookie: " + pairs.join("; "));
  return lines.join("\n");
}

if (typeof module !== "undefined") module.exports = { jwtExp, minutesLeft, buildPayload, NOISE };
