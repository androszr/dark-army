// relay/tests/box-rate.test.mjs — box.js's three rate windows against an
// in-memory stand-in for Upstash's pipeline endpoint. No dependencies: the
// file is loaded into a vm with node's own modules, like push.js's tests.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import crypto from 'node:crypto';
import { Readable } from 'node:stream';

const source = fs.readFileSync(new URL('../api/box.js', import.meta.url), 'utf8');
const RATE = Number(source.match(/const RATE_PER_MINUTE = (\d+)/)[1]);
const PER_ADDRESS = Number(source.match(/const ADDRESS_RATE_PER_MINUTE = (\d+)/)[1]);
const GLOBAL = Number(source.match(/const GLOBAL_RATE_PER_MINUTE = (\d+)/)[1]);

function harness() {
  const store = new Map();
  const fetch = async (_url, init) => {
    const rows = JSON.parse(init.body).map(([cmd, key, ...rest]) => {
      if (cmd === 'INCR') { store.set(key, (store.get(key) || 0) + 1); return { result: store.get(key) }; }
      if (cmd === 'LPUSH') { store.set(key, [rest[0], ...(store.get(key) || [])]); return { result: 1 }; }
      if (cmd === 'RPOP') { const list = store.get(key) || []; return { result: list.length ? list.pop() : null }; }
      return { result: 1 };
    });
    return { ok: true, json: async () => rows };
  };
  const context = {
    module: { exports: null }, require: name => (name === 'node:crypto' ? crypto : {}),
    process: { env: { KV_REST_API_URL: 'https://redis', KV_REST_API_TOKEN: 't' } },
    Buffer, URL, setTimeout, clearTimeout, fetch,
  };
  vm.runInNewContext(source, context);
  return { store, async post(ch, ip) {
    const req = Readable.from([Buffer.from('envelope')]);
    req.method = 'POST'; req.url = `/api/box?ch=${ch}&dir=to-mac`; req.headers = { 'x-real-ip': ip };
    const res = { statusCode: 0, setHeader() {}, end(text) { this.text = text; } };
    await context.module.exports(req, res);
    return res.statusCode;
  } };
}

const channel = i => i.toString(16).padStart(32, '0');

test('one channel is capped at RATE_PER_MINUTE', async () => {
  const h = harness();
  for (let i = 0; i < RATE; i += 1) assert.equal(await h.post(channel(1), '192.0.2.1'), 200);
  assert.equal(await h.post(channel(1), '192.0.2.1'), 429);
});

test('rotating the channel does not dodge the per-address cap', async () => {
  const h = harness();
  for (let i = 0; i < PER_ADDRESS; i += 1) assert.equal(await h.post(channel(i), '192.0.2.1'), 200);
  assert.equal(await h.post(channel(PER_ADDRESS + 1), '192.0.2.1'), 429);
  assert.equal(await h.post(channel(PER_ADDRESS + 1), '192.0.2.2'), 200);
});

test('rotating both channel and address meets the global ceiling', async () => {
  const h = harness();
  for (let i = 0; i < GLOBAL; i += 1) {
    assert.equal(await h.post(channel(i), `10.0.${i >> 8}.${i & 255}`), 200);
  }
  assert.equal(await h.post(channel(GLOBAL + 1), '198.51.100.1'), 429);
});

test('one address over its own cap never spends the global ceiling', async () => {
  const h = harness();
  for (let i = 0; i <= GLOBAL; i += 1) await h.post(channel(i), '192.0.2.1');
  assert.equal(await h.post(channel(GLOBAL + 2), '198.51.100.1'), 200);
});

test('no address is stored, only a truncated hash', async () => {
  const h = harness();
  await h.post(channel(1), '192.0.2.77');
  const keys = [...h.store.keys()].join(' ');
  assert(!keys.includes('192.0.2.77'));
  assert.match(keys, /rate:addr:[0-9a-f]{16}:post/);
});
