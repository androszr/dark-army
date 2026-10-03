// relay/api/push.js — the one buzz: forward a tiny nudge to Apple's push
// service, which is the only road onto a sleeping phone.
//
// POST /api/push?ch=<32 hex>  body = {tok, env, title, badge, kind?, work?,
//   need?, face?, session_id?, card_id?, act?, request_id?} -> one APNs HTTP/2
//   request. 200 when Apple accepted it. `session_id` / `card_id` are the
//   buzz's subject: opaque ids the phone resolves against the picture it
//   already holds when the Mac is out of reach; they pick no category.
// The same route carries the phone's live card: body = {tok, env, event,
//   nickname?, slug?, kind?, work?, since?, session_id?, run_id?, working?,
//   needs_you?, standing_by?, cost_usd?, tokens_k?, cost_usd_hour?,
//   tokens_k_hour?} where `event` is
//   "update" or "end" — an ActivityKit push to the activity's own token,
//   its content-state built here from the same closed set of short fields.
//   The seven fleet fields are optional numbers; absent stays absent.
//   No `title` on that leg, so a mailbox deployed before it existed answers
//   400 and the Mac logs a refused update; nothing is ever mis-sent as a buzz.
//
// Unlike everything else the mailbox carries, this payload cannot be sealed:
// iOS renders the banner from the plaintext `aps` dict, so whatever rides
// here transits Vercel and Apple readable. The `aps` dict is therefore built
// **in this file** from a few caller fields — a short title, a badge count,
// a kind word from a closed set that chooses the sound here, `work` (one
// clamped line naming the card or session, the subtitle), `need` (one
// clamped line saying what is needed — the agent's own summary or question,
// or a tool's bare name — the body), and the lock-screen leg's act word plus
// opaque ids that only pick a category — so the route can never carry an
// arbitrary payload, the command being approved, a file path, a project
// name (one exception, the person's decision of 3 Oct 2026: a `picks` buzz
// or card with no nickname known names its project) or a sound name. Blank or absent `work` / `need` is normal, not a 400.
//
// APNs specifics, both learned the hard way elsewhere: Vercel's global
// fetch (undici) speaks HTTP/1.1 only and APNs requires HTTP/2, so
// `node:http2` is used explicitly; Node's default ECDSA signatures are DER
// and APNs wants raw (r‖s), so the JWT is signed with
// `dsaEncoding: "ieee-p1363"` or every request dies with InvalidProviderToken.
//
// The route is authenticated: `ch` is caller-chosen and proves nothing, so
// without a password anyone holding the relay URL could buzz any device
// token with any title. The Mac sends `Authorization: Bearer <PUSH_SECRET>`
// and the comparison is constant-time. No secret configured means the route
// is off (503), never open.
//
// Rate caps: 30/min per channel plus 120/min across all channels (a rotated
// `ch` must not dodge the cap), box.js's INCR + `EXPIRE 60 NX` pattern (the
// TTL set only when the window opens). Nothing carried is ever logged.
const http2 = require("node:http2");
const crypto = require("node:crypto");
const MAX_BODY_BYTES = 4096;
const RATE_PER_MINUTE = 30;
const GLOBAL_RATE_PER_MINUTE = 120;
const MAX_TITLE_CHARS = 120;
// `relay_client.PUSH_WORK_CHARS`, pinned equal by test_phone_buzz_kinds.py
// and clamped again here because this file builds the payload.
const MAX_WORK_CHARS = 80;
// `relay_client.PUSH_NEED_CHARS`, pinned equal by test_phone_buzz_kinds.py.
const MAX_NEED_CHARS = 120;
const MAX_BADGE = 999;
// The sound is chosen here, from the kind word alone. Each value names a file
// bundled in the phone app (ios/BobPhone/Resources/sounds); an absent or
// unknown kind is the platform default, exactly the buzz before kinds existed.
const SOUNDS = {
  // A burst of refused knocks at the phone doors plays the permission cue:
  // the most urgent sound bundled, and a new file is a pbxproj entry.
  security: "buzz-permission.wav",
  permission: "buzz-permission.wav",
  question: "buzz-question.wav",
  // A review waiting on picks plays the question cue: no new file.
  picks: "buzz-question.wav",
  attention: "buzz-question.wav",
  finished: "buzz-finished.wav",
};
Object.setPrototypeOf(SOUNDS, null); // `SOUNDS["constructor"]` must be undefined
const CH_SHAPE = /^[0-9a-f]{32}$/;
const TOK_SHAPE = /^[0-9a-f]{32,200}$/;
const ACTS = ["permission", "acknowledge", "review"];  // pinned to relay_client.PUSH_ACTS
const ID_SHAPE = /^[A-Za-z0-9._:-]{1,120}$/;
// The live card's leg, pinned to relay_client.ACTIVITY_* / live_activity.KINDS.
const ACTIVITY_EVENTS = ["update", "end"];
const ACTIVITY_KINDS = ["permission", "question", "picks", "attention"];
// identity.NAMES lowercased, pinned by test_phone_buzz_kinds.py. The one
// face check for the buzz and the live card alike: a face off this list is
// dropped, and one the list knows always reaches the phone.
const FACE_SLUGS = [
  "cipher", "vex", "ledger", "mira", "hex", "relay", "forge", "watch",
  "audit", "proxy", "quiet", "nyx", "canon", "velvet", "androll",
  "captcha", "sawa", "franio", "zosia", "ptys",
];
const MAX_NICKNAME_CHARS = 40;
// A card the Mac has gone quiet on (asleep, relay down) dims after this long
// rather than sitting on the Lock Screen all night; iOS ends it at 8h anyway.
const ACTIVITY_STALE_SECONDS = 1800;
// The fleet card's counts and token figure, and the cost cap. An int key
// present but out of shape is `bad activity`; an absent key stays absent.
const FLEET_INT_KEYS = ["working", "needs_you", "standing_by", "tokens_k", "tokens_k_hour"];
const FLEET_USD_KEYS = ["cost_usd", "cost_usd_hour"];
const MAX_FLEET_INT = 999999;
const MAX_FLEET_USD = 1000000;
// Apple recommends refreshing the provider token between 20 and 60 minutes.
const JWT_TTL_MS = 50 * 60 * 1000;

// Module scope survives between invocations on a warm function — the JWT is
// minted at most once per TTL, not once per buzz.
let jwtCache = { token: "", born: 0 };

function apnsJwt() {
  const now = Date.now();
  if (jwtCache.token && now - jwtCache.born < JWT_TTL_MS) {
    return jwtCache.token;
  }
  const key = process.env.APNS_KEY_P8;
  const kid = process.env.APNS_KEY_ID;
  const team = process.env.APNS_TEAM_ID;
  if (!key || !kid || !team) throw new Error("unconfigured");
  const b64url = (obj) => Buffer.from(JSON.stringify(obj)).toString("base64url");
  const head = b64url({ alg: "ES256", kid });
  const claims = b64url({ iss: team, iat: Math.floor(now / 1000) });
  const signature = crypto
    .sign("sha256", Buffer.from(`${head}.${claims}`), {
      key,
      dsaEncoding: "ieee-p1363",
    })
    .toString("base64url");
  jwtCache = { token: `${head}.${claims}.${signature}`, born: now };
  return jwtCache.token;
}

// Bounded well under Vercel's own limit (vercel.json maxDuration 10): a hung
// Apple connection must come back as this route's 503, not the platform 504.
const APNS_TIMEOUT_MS = 8000;
// An offline phone gets one collapsed banner when it wakes, not a backlog of
// every buzz it slept through: same collapse id, and Apple stops trying after
// a short expiration window.
const APNS_COLLAPSE_ID = "bob";
const APNS_EXPIRATION_SECONDS = 600;

function apnsSend(host, deviceToken, payload, leg) {
  // One request, one session. A held pool buys nothing at alert cadence.
  // `leg` chooses the push type and topic: the buzz (default) is an alert
  // on the app's topic with the collapse id; the live card is a
  // `liveactivity` push on the live-activity topic suffix, priority 10
  // and no collapse id (an update and an end must both land, in order).
  return new Promise((resolve, reject) => {
    const jwt = apnsJwt();
    const session = http2.connect(`https://${host}`);
    const timer = setTimeout(() => {
      // destroy(), not close(): close waits for the very stream that hung.
      session.destroy();
      reject(new Error("timeout"));
    }, APNS_TIMEOUT_MS);
    session.on("error", (err) => {
      clearTimeout(timer);
      session.destroy();
      reject(err);
    });
    const legHeaders = leg === "liveactivity" ? {
      "apns-topic": `${process.env.APNS_TOPIC}.push-type.liveactivity`,
      "apns-push-type": "liveactivity",
    } : {
      "apns-topic": process.env.APNS_TOPIC,
      "apns-push-type": "alert",
      "apns-collapse-id": payload.receipt_id || APNS_COLLAPSE_ID,
    };
    const req = session.request({
      ":method": "POST",
      ":path": `/3/device/${deviceToken}`,
      authorization: `bearer ${jwt}`,
      ...legHeaders,
      "apns-priority": "10",
      "apns-expiration": String(
        Math.floor(Date.now() / 1000) + APNS_EXPIRATION_SECONDS),
      "content-type": "application/json",
    });
    let status = 0;
    req.on("response", (headers) => {
      status = Number(headers[":status"]) || 0;
    });
    req.on("data", () => {});
    req.on("end", () => {
      clearTimeout(timer);
      session.close();
      resolve(status);
    });
    req.on("error", (err) => {
      clearTimeout(timer);
      session.destroy();
      reject(err);
    });
    req.end(JSON.stringify(payload));
  });
}

async function redis(commands) {
  // box.js's helper verbatim: one pipelined Upstash call, a row error fails
  // loudly (the caller answers 503) and no detail travels — not a log line.
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

function answer(res, status, text) {
  res.statusCode = status;
  res.setHeader("Content-Type", "text/plain; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(text || "");
}

module.exports = async (req, res) => {
  if (req.method !== "POST") return answer(res, 405, "method not allowed");
  let query;
  try {
    query = new URL(req.url, "http://relay").searchParams;
  } catch {
    return answer(res, 400, "bad request");
  }
  const ch = query.get("ch") || "";
  if (!CH_SHAPE.test(ch)) return answer(res, 400, "bad request");
  // Unconfigured is one answer for all of it: the push password and the
  // APNs credentials name the same deployment, and a route missing any of
  // them is off, not open.
  // APNS_TOPIC belongs in this list: it used to default to "" at send time,
  // and Apple's MissingTopic refusal came back as a generic 502 that looked
  // like an outage rather than a missing env var.
  const secret = process.env.PUSH_SECRET || "";
  if (!secret || !process.env.APNS_KEY_P8 || !process.env.APNS_KEY_ID
      || !process.env.APNS_TEAM_ID || !process.env.APNS_TOPIC) {
    return answer(res, 503, "unconfigured");
  }
  const want = Buffer.from(`Bearer ${secret}`, "utf8");
  const got = Buffer.from(String(req.headers.authorization || ""), "utf8");
  // timingSafeEqual demands equal lengths; the length check leaks only the
  // secret's length, which the shape of a Bearer header gives away anyway.
  if (got.length !== want.length || !crypto.timingSafeEqual(got, want)) {
    return answer(res, 401, "unauthorized");
  }
  const rateKey = `rate:${ch}:push`;
  try {
    const [count, , total] = await redis([
      ["INCR", rateKey],
      ["EXPIRE", rateKey, String(60), "NX"],
      // The global window: rotating `ch` must not mint a fresh 30/min cap.
      ["INCR", "rate:push"],
      ["EXPIRE", "rate:push", String(60), "NX"],
    ]);
    if (Number(count) > RATE_PER_MINUTE) return answer(res, 429, "slow down");
    if (Number(total) > GLOBAL_RATE_PER_MINUTE) {
      return answer(res, 429, "slow down");
    }
  } catch {
    return answer(res, 503, "unavailable");
  }
  let body;
  try {
    body = JSON.parse(await readBody(req));
  } catch {
    return answer(res, 400, "bad request");
  }
  if (typeof body !== "object" || body === null) {
    return answer(res, 400, "bad request");
  }
  const tok = String(body.tok || "");
  const env = String(body.env || "");
  const kind = String(body.kind || "");
  const work = String(body.work || "").trim().slice(0, MAX_WORK_CHARS);
  // Clamped by code point, as the Mac clamps, so a trailing "…" or an emoji
  // at the edge lands whole rather than as half a surrogate pair.
  const need = Array.from(String(body.need || "").trim()).slice(0, MAX_NEED_CHARS).join("");
  if (!TOK_SHAPE.test(tok) || !["prod", "dev"].includes(env)) {
    return answer(res, 400, "bad request");
  }
  const host = env === "dev" ? "api.sandbox.push.apple.com" : "api.push.apple.com";
  let payload;
  let leg = "alert";
  if (body.event !== undefined) {
    // The live card: content-state built here from the closed set (nickname,
    // face slug, kind word, clamped work line, clock, opaque session id, send
    // time); nothing else the caller sends reaches Apple. An `end` carries the
    // last state the Mac sent (its kind may be absent) plus a dismissal date.
    const event = String(body.event || "");
    const slug = String(body.slug || "");
    const sid = body.session_id === undefined ? "" : String(body.session_id);
    // A review run's id (kind `picks`): joined only when present.
    const runId = body.run_id === undefined ? "" : String(body.run_id);
    const since = body.since === undefined ? 0 : Number(body.since);
    if (!ACTIVITY_EVENTS.includes(event)
        || (slug !== "" && !FACE_SLUGS.includes(slug))
        || (sid !== "" && !ID_SHAPE.test(sid))
        || (runId !== "" && !ID_SHAPE.test(runId))
        || !Number.isFinite(since) || since < 0
        || (event === "update" && (!ACTIVITY_KINDS.includes(kind)
                                   || body.since === undefined))
        || (kind !== "" && !ACTIVITY_KINDS.includes(kind))) {
      return answer(res, 400, "bad activity");
    }
    const timestamp = Math.floor(Date.now() / 1000);
    payload = {
      aps: {
        timestamp,
        event,
        "stale-date": timestamp + ACTIVITY_STALE_SECONDS,
        "content-state": {
          nickname: String(body.nickname || "").trim().slice(0, MAX_NICKNAME_CHARS),
          slug,
          kind: kind || "attention",
          work,
          since,
          session_id: sid,
          updated_at: timestamp, // the relay's clock: the card's "N min ago".
        },
      },
    };
    if (runId) payload.aps["content-state"].run_id = runId;
    if (event === "end") payload.aps["dismissal-date"] = timestamp;
    for (const key of FLEET_INT_KEYS) {
      if (body[key] === undefined) continue;
      const v = body[key];
      if (!Number.isInteger(v) || v < 0 || v > MAX_FLEET_INT) {
        return answer(res, 400, "bad activity");
      }
      payload.aps["content-state"][key] = v;
    }
    for (const key of FLEET_USD_KEYS) {
      if (body[key] === undefined) continue;
      const usd = body[key];
      if (typeof usd !== "number" || !Number.isFinite(usd) || usd < 0 || usd > MAX_FLEET_USD) {
        return answer(res, 400, "bad activity");
      }
      payload.aps["content-state"][key] = Math.round(usd * 100) / 100;
    }
    leg = "liveactivity";
  } else {
  const title = String(body.title || "").trim().slice(0, MAX_TITLE_CHARS);
  const badge = Math.min(MAX_BADGE, Math.max(0, Number(body.badge) || 0));
  if (!title) return answer(res, 400, "bad request");
  // The whole payload, built here, never a caller-supplied dict.
  payload = {
    aps: {
      alert: { title },
      badge,
      sound: SOUNDS[kind] || "default",
      "thread-id": "bob",
    },
  };
  // Absent rather than empty: `subtitle: ""` draws a blank second line on iOS.
  if (work) payload.aps.alert.subtitle = work;
  // Absent rather than empty, for the same reason: a blank third line.
  if (need) payload.aps.alert.body = need;
  if (body.destination_version !== undefined || body.receipt_id !== undefined) {
    if (body.destination_version !== 1 || typeof body.receipt_id !== "string"
        || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.receipt_id)) {
      return answer(res, 400, "bad destination");
    }
    payload.destination_version = 1;
    payload.receipt_id = body.receipt_id.toLowerCase();
  }
  if (body.session_id !== undefined || body.card_id !== undefined) {  // the subject
    const sid = String(body.session_id || "");
    const cid = String(body.card_id || "");
    if ((sid !== "" && !ID_SHAPE.test(sid)) || (cid !== "" && !ID_SHAPE.test(cid))
        || (sid === "" && cid === "")) return answer(res, 400, "bad subject");
    if (sid) payload.session_id = sid;
    if (cid) payload.card_id = cid;
  }
  // A cast slug, so the phone may adjust the banner. Off the closed list
  // is omitted, not a 400: a bad portrait field must not swallow the buzz.
  // No image rides. The live-activity branch above never reaches this.
  if (typeof body.face === "string" && FACE_SLUGS.includes(body.face)) {
    payload.face = body.face;
    payload.aps["mutable-content"] = 1;
  }
  if (body.act !== undefined) {  // lock-screen leg: an act word and opaque ids
    const act = String(body.act || "");
    const sid = String(body.session_id || "");
    const rid = String(body.request_id || "");
    const okRid = act !== "permission" || ID_SHAPE.test(rid);
    if (act === "review") {  // a foreground button: the run's short id alone
      const run = String(body.run_id || "");
      if (!ID_SHAPE.test(run)) return answer(res, 400, "bad act");
      payload.aps.category = "bob.review";
      payload.act = act;
      payload.run_id = run;
    } else {
      if (!ACTS.includes(act) || !ID_SHAPE.test(sid) || !okRid) return answer(res, 400, "bad act");
      payload.aps.category = act === "permission" ? "bob.permission" : "bob.acknowledge";
      payload.act = act;
      payload.session_id = sid;
      if (act === "permission") payload.request_id = rid;
    }
  }
  }
  let status;
  try {
    status = await apnsSend(host, tok, payload, leg);
    if (status === 403) {
      // ExpiredProviderToken / InvalidProviderToken: the cached JWT may be
      // signed with a key that has since been rotated. Mint fresh, once.
      jwtCache = { token: "", born: 0 };
      status = await apnsSend(host, tok, payload, leg);
    }
  } catch {
    return answer(res, 503, "unavailable");
  }
  if (status === 200) return answer(res, 200, leg === "liveactivity" ? "ok shape=2" : "ok");
  // The live card's leg tells a transient answer from a refusal: Apple's
  // 5xx and 429 (a server hiccup, a shed connection, too many pushes to
  // one token) are "unavailable", the same 503 an unreachable Apple gets
  // above, which the Mac reads as unreachable and retries on its clock.
  // A 4xx (BadDeviceToken, an ended activity's token, an expired JWT) is
  // still "apple refused", which the Mac remembers for the body's life.
  // Without this one 503 on an `end` wedged the Lock Screen card until the
  // picture changed. The alert leg keeps its one word for every non-200.
  if (leg === "liveactivity" && (status >= 500 || status === 429)) {
    return answer(res, 503, "unavailable");
  }
  // Apple's refusal reasons (BadDeviceToken, expired JWT, …) stay between
  // this function and Apple: one word back, nothing logged.
  return answer(res, 502, "apple refused");
};
