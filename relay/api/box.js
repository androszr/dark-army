// relay/api/box.js — the whole relay: a sealed mailbox between two devices.
//
// One Vercel serverless function, zero npm dependencies, talking to Upstash
// Redis over its REST API with the platform's own fetch. It carries opaque
// base64 envelopes between "the phone" and "the Mac" and can read none of
// them: the security boundary is the AEAD on both ends, never this file.
//
// POST /api/box?ch=<32 hex>&dir=to-mac|to-phone  body = one envelope
//   -> LPUSH + LTRIM 0 31 + EXPIRE 120. Nothing lives longer than two
//      minutes, and a flooded channel keeps only its newest 32 envelopes.
// GET  /api/box?ch=<32 hex>&dir=to-mac|to-phone[&wait=1]
//   -> RPOP. With wait=1, polls every POLL_MS for up to 20s, then 204.
//
// Rate cap: 240/min per channel *per method*, so the Mac's held GET
// long-poll never spends the phone's POST budget. INCR + EXPIRE 60 NX ->
// 429: the TTL is set only when the window opens — re-issuing it on every
// request would slide the window forever, and a steady long-poll would
// climb past the cap and then 429 permanently.
// `ch` is caller-chosen, so rotating it would mint fresh caps on the owner's
// Upstash bill: two more windows, per method, per sender (a hashed address)
// and across the relay (a spend ceiling one person's devices never near).
// Everything else -> 4xx in one word. There is deliberately no logging of
// anything carried — no bodies, no channels, nothing.

const MAX_BODY_BYTES = 1024 * 1024; // over the Mac/phone frame cap, with room
const TTL_SECONDS = 120;
const KEEP_NEWEST = 32;
const RATE_PER_MINUTE = 240;
const ADDRESS_RATE_PER_MINUTE = 480;
const GLOBAL_RATE_PER_MINUTE = 2400;
const WAIT_MS = 20000;
// The held GET is a poll loop, not a blocking pop, because Upstash's REST
// API has no BRPOP/BLPOP and no stream BLOCK — the platform forces it. The
// cadence is therefore the only lever, and it is billed: every tick is one
// RPOP. At 2000ms an empty 20s window costs ~10 commands instead of ~50,
// which is what the Mac's continuous idle polling used to spend around the
// clock. Latency on a carried frame rises by at most one tick.
const POLL_MS = 2000;
// The two ends are not symmetric, and the tick is paid twice per trip
// (measured 21 Sep 2026: every away press waited 0–4 s in pure ticks).
// A phone's held GET on `to-phone` starts the moment it has posted its
// request, and the Mac's answer lands 1–3 s later, so the first
// FAST_WINDOW_MS of that wait tick at POLL_FAST_MS — ~6 extra RPOPs per
// request, only while a request is in flight, never around the clock —
// and fall back to POLL_MS for the rest of the hold. The Mac's held GET
// on `to-mac` is continuous inside its active window, so it keeps the
// slow tick; its pickup is cut on the Mac's side instead.
const POLL_FAST_MS = 500;
const FAST_WINDOW_MS = 3000;

const CH_SHAPE = /^[0-9a-f]{32}$/;
const DIRS = new Set(["to-mac", "to-phone"]);

const crypto = require("node:crypto");

// Vercel's edge sets `x-real-ip`; hashed and truncated, so the 60 s rate
// key holds no address anyone could ask for.
function addressKey(req) {
  const h = req.headers || {};
  const raw = String(h["x-real-ip"] || String(h["x-forwarded-for"] || "")
    .split(",")[0] || (req.socket && req.socket.remoteAddress) || "?").trim();
  return crypto.createHash("sha256").update(raw).digest("hex").slice(0, 16);
}

async function redis(commands) {
  // One pipelined call to Upstash's REST endpoint. Throws on transport
  // failure; the caller answers 503 without saying why in any detail.
  const url = process.env.KV_REST_API_URL;
  const token = process.env.KV_REST_API_TOKEN;
  if (!url || !token) throw new Error("unconfigured");
  const resp = await fetch(`${url}/pipeline`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(commands),
  });
  if (!resp.ok) throw new Error("redis");
  const rows = await resp.json();
  return rows.map((row) => {
    // Upstash answers a failed pipeline command as `{error}` in that row.
    // Mapped silently to undefined, a refused `EXPIRE rateKey 60 NX` would
    // leave the rate key immortal and bring the permanent-429 lockout back
    // with nothing saying why — a row error fails loudly (callers' catch
    // answers 503), never degrades. No detail travels: not a log line.
    if (row.error !== undefined) throw new Error("redis");
    return row.result;
  });
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function answer(res, status, text) {
  res.statusCode = status;
  res.setHeader("Content-Type", "text/plain; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(text || "");
}

module.exports = async (req, res) => {
  let query;
  try {
    query = new URL(req.url, "http://relay").searchParams;
  } catch {
    return answer(res, 400, "bad request");
  }
  const ch = query.get("ch") || "";
  const dir = query.get("dir") || "";
  if (!CH_SHAPE.test(ch) || !DIRS.has(dir)) {
    return answer(res, 400, "bad request");
  }
  const key = `box:${ch}:${dir}`;
  const method = req.method === "GET" ? "get" : "post";
  const rateKey = `rate:${ch}:${method}`;
  const addressRateKey = `rate:addr:${addressKey(req)}:${method}`;
  const globalRateKey = `rate:box:${method}`;

  try {
    const [count, , fromAddress] = await redis([
      ["INCR", rateKey], ["EXPIRE", rateKey, String(60), "NX"],
      ["INCR", addressRateKey], ["EXPIRE", addressRateKey, String(60), "NX"],
    ]);
    let over = Number(count) > RATE_PER_MINUTE || Number(fromAddress) > ADDRESS_RATE_PER_MINUTE;
    // Only a sender within its own caps spends the ceiling, so one address
    // hammering alone can never fill it and lock the owner out.
    if (!over) {
      const [total] = await redis([["INCR", globalRateKey], ["EXPIRE", globalRateKey, String(60), "NX"]]);
      over = Number(total) > GLOBAL_RATE_PER_MINUTE;
    }
    if (over) return answer(res, 429, "slow down");
  } catch {
    return answer(res, 503, "unavailable");
  }

  if (req.method === "POST") {
    let body;
    try {
      body = await readBody(req);
    } catch {
      return answer(res, 413, "too large");
    }
    if (!body) return answer(res, 400, "empty");
    try {
      await redis([
        ["LPUSH", key, body],
        ["LTRIM", key, "0", String(KEEP_NEWEST - 1)],
        ["EXPIRE", key, String(TTL_SECONDS)],
      ]);
    } catch {
      return answer(res, 503, "unavailable");
    }
    return answer(res, 200, "ok");
  }

  if (req.method === "GET") {
    const wait = query.get("wait") === "1";
    const started = Date.now();
    const deadline = started + (wait ? WAIT_MS : 0);
    for (;;) {
      let popped;
      try {
        [popped] = await redis([["RPOP", key]]);
      } catch {
        return answer(res, 503, "unavailable");
      }
      if (popped !== null && popped !== undefined) {
        return answer(res, 200, String(popped));
      }
      if (Date.now() >= deadline) {
        return answer(res, 204, "");
      }
      const fast = dir === "to-phone"
        && Date.now() - started < FAST_WINDOW_MS;
      await sleep(fast ? POLL_FAST_MS : POLL_MS);
    }
  }

  return answer(res, 405, "method not allowed");
};
