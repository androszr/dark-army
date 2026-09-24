// relay-ws/server.js — the whole socket relay: one live line each way,
// sealed letters passed straight across.
//
// One Node process, one dependency (`ws`), no store. A paired phone and its
// Mac each hold one WebSocket to `/ws?ch=<32 hex>&side=mac|phone`, and every
// text frame one side sends is written verbatim to the other side, or
// dropped when nobody is there. It carries the same ChaCha20-Poly1305
// envelopes the mailbox (`relay/api/box.js`) carries and can read none of
// them: the security boundary is the AEAD on both ends, never this file.
//
// What it holds itself to: it logs nothing it carries (there is no logging
// call in the file), it reads no environment variable and holds no secret
// that could be used against the Mac, it caps how fast either side may send
// (`RATE_PER_MINUTE`, a fixed window, then `4008 slow down`), it drops an
// oversize frame (`maxPayload`, the library closes with 1009), it refuses a
// browser-shaped upgrade (an `Origin` header is 403), it holds at most
// `MAX_CONNECTIONS_PER_ADDRESS` lines from one sending address (Fly's
// `Fly-Client-IP`, then 503), so one stranger cannot fill `MAX_CONNECTIONS`
// and lock the owner out, and a second socket on
// a held side replaces the first (`4001 replaced` — newest wins). A frame
// beginning `peer:` is the service's own word for the other side arriving
// (`peer:1`) or leaving (`peer:0`); a client sending one is dropped.

const http = require("node:http");
const { WebSocketServer } = require("ws");

const MAX_FRAME_BYTES = 1024 * 1024; // over the Mac/phone frame cap, with room
const MAX_CONNECTIONS = 512;
// A Mac and a phone hold two; a household behind one router, a few more.
const MAX_CONNECTIONS_PER_ADDRESS = 16;
const RATE_PER_MINUTE = 240;
const PING_MS = 25000;
const PORT = 8080;
const CLOSE_REPLACED = 4001;
const CLOSE_SLOW_DOWN = 4008;
const CH_SHAPE = /^[0-9a-f]{32}$/;
const SIDES = new Set(["mac", "phone"]);

function otherSide(side) {
  return side === "mac" ? "phone" : "mac";
}

function refuse(socket, status, text) {
  socket.write(`HTTP/1.1 ${status} ${text}\r\nConnection: close\r\n`
    + "Content-Length: 0\r\n\r\n");
  socket.destroy();
}

// The sender. Fly's proxy sets `Fly-Client-IP` on every request it
// forwards; the socket's own address is the proxy, so it is the fallback
// only (a local run, the tests).
function clientAddress(req) {
  const header = req.headers["fly-client-ip"];
  return String(header || req.socket.remoteAddress || "unknown").trim();
}

function isOpen(ws) {
  return ws !== null && ws !== undefined && ws.readyState === ws.OPEN;
}

/** Start the relay on `port` (0 for an ephemeral one). Returns the
 * `http.Server`; `stop()` on the returned object closes every line. */
function start(port = PORT) {
  const channels = new Map(); // ch -> { mac: ws | null, phone: ws | null }
  let connections = 0;
  const perAddress = new Map(); // address -> open lines

  const server = http.createServer((req, res) => {
    if (req.method === "GET" && req.url === "/healthz") {
      res.writeHead(200, { "Content-Type": "text/plain", "Cache-Control": "no-store" });
      res.end("ok");
      return;
    }
    res.writeHead(404, { "Content-Type": "text/plain", "Cache-Control": "no-store" });
    res.end("not found");
  });

  const wss = new WebSocketServer({ noServer: true, maxPayload: MAX_FRAME_BYTES });

  function attach(ws, ch, side, address) {
    connections += 1;
    perAddress.set(address, (perAddress.get(address) || 0) + 1);
    let entry = channels.get(ch);
    if (!entry) {
      entry = { mac: null, phone: null };
      channels.set(ch, entry);
    }
    const other = otherSide(side);
    const previous = entry[side];
    if (previous) previous.close(CLOSE_REPLACED, "replaced");
    entry[side] = ws;
    ws.alive = true;
    ws.sent = 0;
    ws.windowStart = Date.now();
    ws.on("pong", () => { ws.alive = true; });
    ws.on("error", () => {});
    ws.on("message", (data, isBinary) => {
      if (isBinary) return; // text frames only
      const text = data.toString("utf8");
      if (text.startsWith("peer:")) return; // the service's own word
      const now = Date.now();
      if (now - ws.windowStart >= 60000) {
        ws.windowStart = now;
        ws.sent = 0;
      }
      ws.sent += 1;
      if (ws.sent > RATE_PER_MINUTE) {
        ws.close(CLOSE_SLOW_DOWN, "slow down");
        return;
      }
      const peer = entry[other];
      if (isOpen(peer)) peer.send(text);
    });
    ws.on("close", () => {
      connections -= 1;
      const held = (perAddress.get(address) || 1) - 1;
      if (held > 0) perAddress.set(address, held);
      else perAddress.delete(address);
      if (entry[side] !== ws) return; // already replaced by a newer socket
      entry[side] = null;
      const peer = entry[other];
      if (isOpen(peer)) peer.send("peer:0");
      if (!entry.mac && !entry.phone) channels.delete(ch);
    });
    const peer = entry[other];
    if (isOpen(peer)) {
      peer.send("peer:1");
      ws.send("peer:1");
    } else {
      ws.send("peer:0");
    }
  }

  server.on("upgrade", (req, socket, head) => {
    let url;
    try {
      url = new URL(req.url, "http://relay");
    } catch {
      return refuse(socket, 400, "Bad Request");
    }
    const ch = url.searchParams.get("ch") || "";
    const side = url.searchParams.get("side") || "";
    if (url.pathname !== "/ws" || !CH_SHAPE.test(ch) || !SIDES.has(side)) {
      return refuse(socket, 400, "Bad Request");
    }
    if (req.headers.origin !== undefined) {
      return refuse(socket, 403, "Forbidden");
    }
    if (connections >= MAX_CONNECTIONS) {
      return refuse(socket, 503, "Service Unavailable");
    }
    const address = clientAddress(req);
    if ((perAddress.get(address) || 0) >= MAX_CONNECTIONS_PER_ADDRESS) {
      return refuse(socket, 503, "Service Unavailable");
    }
    wss.handleUpgrade(req, socket, head, (ws) => attach(ws, ch, side, address));
  });

  // A missed pong terminates the line; the other side hears `peer:0`.
  const pinger = setInterval(() => {
    for (const ws of wss.clients) {
      if (!ws.alive) {
        ws.terminate();
        continue;
      }
      ws.alive = false;
      ws.ping();
    }
  }, PING_MS);
  pinger.unref();

  server.stop = () => new Promise((resolve) => {
    clearInterval(pinger);
    for (const ws of wss.clients) ws.terminate();
    wss.close();
    server.close(() => resolve());
  });
  server.channelCount = () => channels.size;
  server.listen(port);
  return server;
}

module.exports = {
  start, MAX_FRAME_BYTES, MAX_CONNECTIONS, MAX_CONNECTIONS_PER_ADDRESS,
  RATE_PER_MINUTE, PING_MS,
  CLOSE_REPLACED, CLOSE_SLOW_DOWN, CH_SHAPE,
};

if (require.main === module) start(PORT);
