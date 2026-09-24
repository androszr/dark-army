// relay-ws/tests/server.test.mjs — the socket relay against a real listener
// on an ephemeral port, the `ws` client playing both sides.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import http from "node:http";
import WebSocket from "ws";
import relay from "../server.js";

const CH = "0123456789abcdef0123456789abcdef";
let server;
let base;

before(async () => {
  server = relay.start(0);
  await new Promise((resolve) => server.once("listening", resolve));
  base = `ws://127.0.0.1:${server.address().port}`;
});

after(async () => {
  await server.stop();
});

function open(ch, side, options = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${base}/ws?ch=${ch}&side=${side}`, options);
    ws.inbox = [];
    ws.waiters = [];
    ws.on("message", (data) => {
      const text = data.toString();
      const waiter = ws.waiters.shift();
      if (waiter) waiter(text);
      else ws.inbox.push(text);
    });
    ws.once("open", () => resolve(ws));
    ws.once("error", reject);
    ws.once("unexpected-response", (_req, res) => reject(new Error(String(res.statusCode))));
  });
}

function next(ws, ms = 2000) {
  if (ws.inbox.length) return Promise.resolve(ws.inbox.shift());
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("no frame")), ms);
    ws.waiters.push((text) => {
      clearTimeout(timer);
      resolve(text);
    });
  });
}

function closed(ws) {
  return new Promise((resolve) => {
    if (ws.readyState === ws.CLOSED) resolve({ code: ws.closeCode });
    ws.once("close", (code, reason) => resolve({ code, reason: reason.toString() }));
  });
}

function upgradeStatus(path, headers = {}) {
  return new Promise((resolve) => {
    const ws = new WebSocket(`${base}${path}`, { headers });
    ws.on("unexpected-response", (_req, res) => {
      resolve(res.statusCode);
      ws.terminate();
    });
    ws.on("error", () => {});
    ws.on("open", () => {
      resolve(101);
      ws.terminate();
    });
  });
}

test("a bad channel or side is 400", async () => {
  assert.equal(await upgradeStatus(`/ws?ch=short&side=mac`), 400);
  assert.equal(await upgradeStatus(`/ws?ch=${CH}&side=tablet`), 400);
  assert.equal(await upgradeStatus(`/other?ch=${CH}&side=mac`), 400);
  assert.equal(await upgradeStatus(`/ws?ch=${CH.toUpperCase()}&side=mac`), 400);
});

test("an upgrade carrying an Origin header is 403", async () => {
  assert.equal(await upgradeStatus(`/ws?ch=${CH}&side=mac`,
    { Origin: "https://example.test" }), 403);
});

test("/healthz is 200 and everything else 404", async () => {
  const port = server.address().port;
  const get = (path) => new Promise((resolve) => {
    http.get({ host: "127.0.0.1", port, path }, (res) => {
      res.resume();
      resolve(res.statusCode);
    });
  });
  assert.equal(await get("/healthz"), 200);
  assert.equal(await get("/"), 404);
  assert.equal(await get("/ws"), 404);
});

test("two sides forward text verbatim both ways and hear peer:1 / peer:0", async () => {
  const mac = await open(CH, "mac");
  assert.equal(await next(mac), "peer:0");
  const phone = await open(CH, "phone");
  assert.equal(await next(phone), "peer:1");
  assert.equal(await next(mac), "peer:1");
  const sealed = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=";
  phone.send(sealed);
  assert.equal(await next(mac), sealed);
  mac.send(`reply ${sealed}`);
  assert.equal(await next(phone), `reply ${sealed}`);
  phone.close();
  assert.equal(await next(mac), "peer:0");
  mac.close();
  await closed(mac);
});

test("a frame with nobody on the other side is dropped, not queued", async () => {
  const phone = await open(CH, "phone");
  assert.equal(await next(phone), "peer:0");
  phone.send("into the void");
  const mac = await open(CH, "mac");
  assert.equal(await next(mac), "peer:1");
  await assert.rejects(next(mac, 300), /no frame/);
  phone.close();
  mac.close();
  await Promise.all([closed(phone), closed(mac)]);
});

test("newest wins: a second socket on a held side closes the first with 4001", async () => {
  const first = await open(CH, "phone");
  assert.equal(await next(first), "peer:0");
  const second = await open(CH, "phone");
  const gone = await closed(first);
  assert.equal(gone.code, relay.CLOSE_REPLACED);
  assert.equal(gone.reason, "replaced");
  assert.equal(await next(second), "peer:0");
  const mac = await open(CH, "mac");
  assert.equal(await next(mac), "peer:1");
  second.send("from the newest");
  assert.equal(await next(mac), "from the newest");
  second.close();
  mac.close();
  await Promise.all([closed(second), closed(mac)]);
});

test("a peer: frame from a client is dropped", async () => {
  const mac = await open(CH, "mac");
  await next(mac);
  const phone = await open(CH, "phone");
  await next(phone);
  await next(mac);
  phone.send("peer:1");
  phone.send("peer:0");
  phone.send("real");
  assert.equal(await next(mac), "real");
  mac.close();
  phone.close();
  await Promise.all([closed(mac), closed(phone)]);
});

test("RATE_PER_MINUTE + 1 frames close the sender with 4008", async () => {
  const mac = await open(CH, "mac");
  await next(mac);
  const phone = await open(CH, "phone");
  await next(phone);
  await next(mac);
  for (let i = 0; i <= relay.RATE_PER_MINUTE; i += 1) phone.send(`f${i}`);
  const gone = await closed(phone);
  assert.equal(gone.code, relay.CLOSE_SLOW_DOWN);
  assert.equal(gone.reason, "slow down");
  // The other side is untouched and hears the departure.
  let last = "";
  for (let i = 0; i < relay.RATE_PER_MINUTE + 1; i += 1) {
    last = await next(mac);
    if (last === "peer:0") break;
  }
  assert.equal(last, "peer:0");
  mac.close();
  await closed(mac);
});

test("an oversize frame closes the socket", async () => {
  const mac = await open(CH, "mac");
  await next(mac);
  mac.send("x".repeat(relay.MAX_FRAME_BYTES + 1));
  const gone = await closed(mac);
  assert.equal(gone.code, 1009);
});

test("an empty channel entry is deleted", async () => {
  const other = CH.replace(/0/g, "f");
  const mac = await open(other, "mac");
  await next(mac);
  assert.ok(server.channelCount() >= 1);
  mac.close();
  await closed(mac);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(server.channelCount(), 0);
});

test("one address holds at most MAX_CONNECTIONS_PER_ADDRESS lines; another address still gets in", async () => {
  const crowd = { headers: { "fly-client-ip": "203.0.113.7" } };
  const held = [];
  for (let i = 0; i < relay.MAX_CONNECTIONS_PER_ADDRESS; i += 1) {
    const ch = i.toString(16).padStart(32, "a");
    held.push(await open(ch, "mac", crowd));
  }
  assert.equal(await upgradeStatus(`/ws?ch=${CH}&side=phone`, crowd.headers), 503);
  assert.equal(await upgradeStatus(`/ws?ch=${CH}&side=phone`,
    { "fly-client-ip": "198.51.100.9" }), 101);
  // A line closing frees its place for the same address.
  held[0].close();
  await closed(held[0]);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(await upgradeStatus(`/ws?ch=${CH}&side=phone`, crowd.headers), 101);
  for (const ws of held.slice(1)) ws.terminate();
  await new Promise((resolve) => setTimeout(resolve, 50));
});
