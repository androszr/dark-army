#!/usr/bin/env python3
"""The phone's away path, measured on the Mac alone.

Drives the real relay connector (`relay_client.RelayConnector`) against the
test suite's fake mailbox (`test_relay_client._Mailbox`, a stdlib HTTP
server on a loopback port) with real sealed envelopes, a fleet-sized agents
snapshot and a board of cards, and prints typical / slow / worst times for
the four trips a phone makes — a whole `state` read, an `unchanged` `state`
read quoting the digest, a `usage` read and one refused `action` — plus the
plain, deflated and sealed byte size of the two reads. The numbers are the
report's baseline (`docs/2026-09-20-relay-latency-audit.md`), and this is
the command anybody reruns to check them:

    cd host && .venv/bin/python ../tools/relay_latency_bench.py \\
        --sessions 20 --cards 40 --rounds 30

What it measures, per trip, on the phone's hand:

* ``post_to_pickup`` — from the phone's POST landing to the Mac's held GET
  taking the frame (off the fake mailbox's own GET stamps);
* ``mac_dwell`` / ``mac_hold`` / ``mac_open`` / ``mac_run`` — the Mac's own
  legs, echoed on the reply envelope as ``timing``;
* ``total`` — POST to opened reply.

``--socket`` drives the **socket lane** instead (`relay_ws.RelaySocketConnector`
against the test suite's fake socket relay, `test_relay_ws._FakeRelay`):
the same four trips down one held line, plus a fifth, ``push`` — the Mac
nudged by a changed picture and the sealed push received — with the same
columns (the mailbox legs read zero: there is no mailbox). The figures page
is `docs/2026-09-21-relay-socket-mvp-figures.md`.

What it does **not** measure: Vercel's cold starts, the public internet, a
cellular link, iOS backgrounding. The fake mailbox ticks every
``--mailbox-tick`` seconds while holding a poll (0.05 by default, the test
suite's); ``--mailbox-tick 2.0`` reproduces the real mailbox's `POLL_MS`
quantisation, and nothing here can reproduce the rest — that is what the
report's *Field figures* section is for.

Touches no real state directory: `paths._home()` is redirected before the
daemon package is imported (the pytest guard's own switch), and the three
constants the connector reads are pointed under a fresh temporary folder
besides. Runs under `host/.venv`, prints a table (or one JSON object with
``--json``) and exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOST = REPO / "host"

# Isolation first, imports second: every path constant in
# `dark_army_daemon.paths` is derived from `_home()` at import time, and
# the pytest guard is the one switch that moves it off the real home.
os.environ.setdefault("PYTEST_CURRENT_TEST", "tools/relay_latency_bench.py")
_TMP = Path(tempfile.mkdtemp(prefix="relay-latency-bench-"))

sys.path.insert(0, str(HOST))
sys.path.insert(0, str(HOST / "tests"))

from dark_army_daemon import paths  # noqa: E402

paths.RELAY_PATH = _TMP / "relay.json"
paths.DEVICES_PATH = _TMP / "devices.json"
paths.STATE_DIR = _TMP / "state"

from dark_army_daemon import relay, relay_client, relay_ws  # noqa: E402
from dark_army_daemon.daemon import BobDaemon  # noqa: E402
from test_relay_client import (  # noqa: E402
    _Mailbox, _build_connector, _phone_receive, _phone_send,
)
from test_relay_ws import (  # noqa: E402
    _FakeRelay, _Phone, _build_connector as _build_socket_connector, _until,
)
import websockets  # noqa: E402

TRIPS = ("state", "state_unchanged", "usage", "action")
SOCKET_TRIPS = TRIPS + ("push",)
STATS = ("p50", "p90", "max")
LEGS = ("post_to_pickup", "mac_dwell", "mac_hold", "mac_open", "mac_run",
        "total")


def _percentile(values, q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, int(-(-q / 100.0 * len(ordered) // 1)))
    return ordered[min(len(ordered), rank) - 1]


def _agents_snapshot(sessions: int) -> dict:
    """A fleet the size of a real one: every row shaped like an enriched
    stub — id, name, state, project, ~2 KB of last words, stats, a trend."""
    words = ("Reading the plan and the two contracts before touching the "
             "connector; the held poll and the write bucket stay where they "
             "are. ")
    last_text = (words * 40)[:2048]
    rows = []
    for i in range(sessions):
        rows.append({
            "session_id": f"bench-session-{i:04d}",
            "name": f"Session {i}",
            "nickname": ("Cipher", "Vex", "Mira", "Ledger")[i % 4],
            "state": ("working", "idle", "thinking")[i % 3],
            "project": f"project-{i % 5}",
            "branch": "main",
            "cwd": f"/Users/bench/project-{i % 5}",
            "last_text": last_text,
            "last_summary": "Waiting on the verifier's table.",
            "started_at": 1_700_000_000 + i,
            "idle_seconds": 12 + i,
            "quiet_since": 1_700_000_100 + i,
            "stats": {
                "assistant_messages": 40 + i, "total_input_tokens": 120_000 + i,
                "output_tokens": 30_000 + i, "duration_seconds": 3600,
                "tool_counts": {"Read": 30, "Bash": 20, "Edit": 12},
                "files_touched": [f"host/dark_army_daemon/file_{k}.py"
                                  for k in range(6)],
            },
            "metrics": {"ctx_used_pct": 40 + (i % 50), "cost_usd": 1.25 + i},
            "trend": [[1_700_000_000 + k * 10, 40 + k, 1.0 + k / 10, 100 * k]
                      for k in range(90)],
            "channel": True,
            "can_type": False,
            "subagents": [],
        })
    third = max(1, len(rows) // 3)
    return {
        "running": rows[:third],
        "waiting": rows[third:2 * third],
        "sleeping": rows[2 * third:],
        "abandoned": [],
        "finished": [],
    }


def _board_snapshot(cards: int) -> dict:
    out = []
    for i in range(cards):
        out.append({
            "id": f"card-{i:04d}",
            "title": f"Card {i}: measure the phone's two doors end to end",
            "summary": ("A repeatable measurement on the Mac alone, then a "
                        "report ranking what lies beyond the filed cards."),
            "prompt": ("Implement per the plan; every step in order; the "
                       "gates green. " * 6)[:400],
            "column": ("prep", "backlog", "in_progress", "done")[i % 4],
            "project": f"project-{i % 5}",
            "root": f"/Users/bench/project-{i % 5}",
            "tool": "claude",
            "priority": str(i % 100),
            "position": i,
            "created_at": 1_700_000_000 + i,
            "revision": 3,
            "workflow": "bc-implementer,bc-verifier",
            "area": "pocket",
            "link_state": "",
            "session_id": "",
        })
    return {
        "available": True, "cards": out,
        "counts": {"prep": cards // 4, "backlog": cards // 4,
                   "in_progress": cards // 4, "done": cards - 3 * (cards // 4)},
        "projects": [f"project-{k}" for k in range(5)],
        "tools": ["claude", "codex", "grok"],
        "dispatch_enabled": True, "prepare_enabled": True,
        "generated_at": time.time(),
    }


class _TickingMailbox(_Mailbox):
    """The suite's fake mailbox with a settable poll tick and hold."""

    tick = 0.05
    hold_seconds = 1.0

    def do_GET(self):  # noqa: N802 — the stdlib's name
        with _Mailbox.lock:
            _Mailbox.gets.append((time.monotonic(), self.path))
        if self._refused():
            return
        key = self._key()
        if key is None:
            self.send_response(400)
            self.end_headers()
            return
        held = "wait=1" in self.path
        deadline = time.monotonic() + (self.hold_seconds if held else 0.0)
        while True:
            with _Mailbox.lock:
                queue = _Mailbox.boxes[key]
                popped = queue.pop() if queue else None
            if popped is not None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(popped)))
                self.end_headers()
                self.wfile.write(popped)
                return
            if time.monotonic() >= deadline:
                self.send_response(204)
                self.end_headers()
                return
            time.sleep(self.tick)


def _sizes(plain: bytes) -> dict:
    deflated = relay._deflate(plain)
    return {"plain": len(plain), "deflated": len(deflated),
            # base64 of nonce + ciphertext + tag: 4/3 of (12 + n + 16).
            "sealed": 4 * ((12 + len(deflated) + 16 + 2) // 3)}


async def _one_trip(key: bytes, url: str, ctr: int, kind: str, body: dict,
                    chan: str) -> dict:
    """POST, wait for the reply, and read the legs off the mailbox's GET
    stamps and the Mac's echo."""
    before = len(_Mailbox.gets)
    posted = time.monotonic()
    await _phone_send(key, url, ctr, kind, body)
    frame = await _phone_receive(key, url, ctr - 1, timeout=30.0)
    total = time.monotonic() - posted
    if frame is None:
        raise SystemExit(f"no reply to {kind} #{ctr} within 30 s")
    envelope = frame["body"]
    timing = envelope.get("timing") or {}
    # The Mac's pickup is the first to-mac GET the mailbox *answered with a
    # frame* after the POST; the stamp is when the GET arrived, so the
    # pickup moment is that GET's return — approximated as the first to-mac
    # GET stamped after the POST plus the tick it waited. Reading the Mac's
    # own `hold` is the honest figure; this one is the phone's view.
    to_mac = [t for t, path in _Mailbox.gets[before:]
              if f"ch={chan}" in path and "dir=to-mac" in path]
    pickup = (to_mac[0] - posted) if to_mac else 0.0
    return {
        "status": int(envelope.get("status") or 0),
        "body": envelope.get("body") or "",
        "legs": {
            "post_to_pickup": max(0.0, pickup),
            "mac_dwell": float(timing.get("dwell") or 0.0),
            "mac_hold": float(timing.get("hold") or 0.0),
            "mac_open": float(timing.get("open") or 0.0),
            "mac_run": float(timing.get("run") or 0.0),
            "total": total,
        },
    }


async def _bench(args) -> dict:
    _TickingMailbox.tick = float(args.mailbox_tick)
    _TickingMailbox.hold_seconds = float(args.mailbox_hold)
    _Mailbox.boxes.clear()
    _Mailbox.gets = []
    _Mailbox.refuse = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _TickingMailbox)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    relay.reset()
    # The bench asks faster than any phone: thirty rounds of four trips in
    # a few seconds would trip `RELAY_MAX_FRAMES_PER_MINUTE` (60) and
    # `RELAY_MAX_WRITES_PER_MINUTE` (10) and measure the buckets, not the
    # path. Both are lifted for the run — the connector reads them when a
    # device's bucket is first built — and the report says what they are.
    relay.RELAY_MAX_FRAMES_PER_MINUTE = 1_000_000
    relay.RELAY_MAX_WRITES_PER_MINUTE = 1_000_000
    conn, srv, daemon, key = _build_connector(url)
    srv.on_agents_change(_agents_snapshot(args.sessions))
    srv.on_board_change(_board_snapshot(args.cards))
    chan = relay.channel_id(key)
    conn.start()
    results = {trip: {leg: [] for leg in LEGS} for trip in TRIPS}
    sizes: dict = {}
    ctr = 0
    try:
        # Warm the channel: the first frame flips it active (held polls).
        ctr += 1
        await _one_trip(key, url, ctr, "state", {}, chan)
        digest = ""
        for _ in range(args.rounds):
            ctr += 1
            full = await _one_trip(key, url, ctr, "state", {}, chan)
            payload = json.loads(full["body"])
            digest = payload.get("state_digest", "")
            sizes.setdefault("state", _sizes(full["body"].encode()))
            for leg in LEGS:
                results["state"][leg].append(full["legs"][leg])

            ctr += 1
            same = await _one_trip(key, url, ctr, "state", {"digest": digest}, chan)
            if not json.loads(same["body"]).get("unchanged"):
                raise SystemExit("the digest-quoting read was not `unchanged`")
            sizes.setdefault("state_unchanged", _sizes(same["body"].encode()))
            for leg in LEGS:
                results["state_unchanged"][leg].append(same["legs"][leg])

            ctr += 1
            usage = await _one_trip(key, url, ctr, "usage", {}, chan)
            sizes.setdefault("usage", _sizes(usage["body"].encode()))
            for leg in LEGS:
                results["usage"][leg].append(usage["legs"][leg])

            ctr += 1
            # `inbox_ack` with an empty payload is refused in words (400)
            # before anything runs: a write's whole path, nothing dispatched.
            act = await _one_trip(key, url, ctr, "action",
                                  {"action": "inbox_ack"}, chan)
            if act["status"] != 400:
                raise SystemExit(f"the refused action answered {act['status']}")
            sizes.setdefault("action", _sizes(act["body"].encode()))
            for leg in LEGS:
                results["action"][leg].append(act["legs"][leg])
    finally:
        await conn.stop()
        server.shutdown()
        server.server_close()

    trips = {}
    for trip in TRIPS:
        trips[trip] = {}
        for leg in LEGS:
            values = results[trip][leg]
            trips[trip][leg] = {
                "p50": _percentile(values, 50),
                "p90": _percentile(values, 90),
                "max": max(values) if values else 0.0,
                "mean": statistics.fmean(values) if values else 0.0,
            }
    return {
        "sessions": args.sessions, "cards": args.cards, "rounds": args.rounds,
        "mailbox_tick": float(args.mailbox_tick),
        "mailbox_hold": float(args.mailbox_hold),
        "trips": trips,
        "sizes": sizes,
        "sealed_bytes": {trip: sizes.get(trip, {}).get("sealed", 0)
                         for trip in TRIPS},
        "python": sys.version.split()[0],
    }


async def _socket_trip(phone: _Phone, kind: str, body: dict) -> dict:
    """One request down the line: send, wait for the sealed answer, read the
    Mac's legs off the echo. No mailbox, so `post_to_pickup`, `mac_dwell`
    and `mac_hold` read zero."""
    posted = time.monotonic()
    await phone.send(kind, body)
    frame = await phone.receive(timeout=30.0)
    total = time.monotonic() - posted
    if frame is None:
        raise SystemExit(f"no socket reply to {kind} within 30 s")
    envelope = frame["body"]
    timing = envelope.get("timing") or {}
    return {
        "status": int(envelope.get("status") or 0),
        "body": envelope.get("body") or "",
        "legs": {
            "post_to_pickup": 0.0, "mac_dwell": 0.0, "mac_hold": 0.0,
            "mac_open": float(timing.get("open") or 0.0),
            "mac_run": float(timing.get("run") or 0.0),
            "total": total,
        },
    }


async def _socket_bench(args) -> dict:
    """The same measurement down the socket lane, plus the push trip."""
    fake = _FakeRelay()
    server = await websockets.serve(fake.handler, "127.0.0.1", 0,
                                    process_request=fake.process_request)
    url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    relay.reset()
    relay.RELAY_MAX_FRAMES_PER_MINUTE = 1_000_000
    relay.RELAY_MAX_WRITES_PER_MINUTE = 1_000_000
    # The push bucket is lifted like the frame buckets; the coalescing
    # interval is left at the real `WS_PUSH_MIN_INTERVAL`, because it is
    # part of what a push costs and the head line names it.
    relay_ws.WS_PUSH_MAX_PER_MINUTE = 1_000_000
    conn, srv, daemon, key = _build_socket_connector(url)
    srv.on_agents_change(_agents_snapshot(args.sessions))
    srv.on_board_change(_board_snapshot(args.cards))
    conn.start()
    results = {trip: {leg: [] for leg in LEGS} for trip in SOCKET_TRIPS}
    sizes: dict = {}
    phone = None
    try:
        await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN,
                     timeout=10.0, message="the Mac never opened its line")
        phone = await _Phone(key, url).connect()
        await _until(lambda: fake.channels[phone.chan]["phone"] is not None)
        # Arm the line: the first verified frame; the Mac may push from here.
        await _socket_trip(phone, "state", {"done": "review"})
        digest = ""
        for n in range(args.rounds):
            full = await _socket_trip(phone, "state", {})
            payload = json.loads(full["body"])
            digest = payload.get("state_digest", "")
            sizes.setdefault("state", _sizes(full["body"].encode()))
            for leg in LEGS:
                results["state"][leg].append(full["legs"][leg])

            same = await _socket_trip(phone, "state", {"digest": digest})
            if not json.loads(same["body"]).get("unchanged"):
                raise SystemExit("the digest-quoting read was not `unchanged`")
            sizes.setdefault("state_unchanged", _sizes(same["body"].encode()))
            for leg in LEGS:
                results["state_unchanged"][leg].append(same["legs"][leg])

            usage = await _socket_trip(phone, "usage", {})
            sizes.setdefault("usage", _sizes(usage["body"].encode()))
            for leg in LEGS:
                results["usage"][leg].append(usage["legs"][leg])

            act = await _socket_trip(phone, "action", {"action": "inbox_ack"})
            if act["status"] != 400:
                raise SystemExit(f"the refused action answered {act['status']}")
            sizes.setdefault("action", _sizes(act["body"].encode()))
            for leg in LEGS:
                results["action"][leg].append(act["legs"][leg])

            # The push trip: a changed picture nudges the connector, and the
            # sealed push is received. `total` is nudge to opened push;
            # `mac_run` is the Mac's own `state()` build, read off the
            # timing record's shape (the push carries no echo, so it is
            # left at zero here — the access-log `timing` line has it).
            snapshot = _agents_snapshot(args.sessions)
            snapshot["running"][0]["name"] = f"Session changed {n}"
            nudged = time.monotonic()
            srv.on_agents_change(snapshot)
            push = await phone.receive(timeout=30.0)
            total = time.monotonic() - nudged
            if push is None or push.get("kind") != "push":
                raise SystemExit("no push after a changed picture")
            sizes.setdefault("push", _sizes(str(push["body"]["body"]).encode()))
            legs = {"post_to_pickup": 0.0, "mac_dwell": 0.0, "mac_hold": 0.0,
                    "mac_open": 0.0, "mac_run": 0.0, "total": total}
            for leg in LEGS:
                results["push"][leg].append(legs[leg])
    finally:
        if phone is not None:
            await phone.close()
        await conn.stop()
        server.close()
        await server.wait_closed()

    trips = {}
    for trip in SOCKET_TRIPS:
        trips[trip] = {}
        for leg in LEGS:
            values = results[trip][leg]
            trips[trip][leg] = {
                "p50": _percentile(values, 50),
                "p90": _percentile(values, 90),
                "max": max(values) if values else 0.0,
                "mean": statistics.fmean(values) if values else 0.0,
            }
    return {
        "sessions": args.sessions, "cards": args.cards, "rounds": args.rounds,
        "route": "socket",
        "push_min_interval": float(relay_ws.WS_PUSH_MIN_INTERVAL),
        "trips": trips,
        "sizes": sizes,
        "sealed_bytes": {trip: sizes.get(trip, {}).get("sealed", 0)
                         for trip in SOCKET_TRIPS},
        "python": sys.version.split()[0],
    }


def _table(report: dict) -> str:
    socket = report.get("route") == "socket"
    trips_named = SOCKET_TRIPS if socket else TRIPS
    if socket:
        head = (f"relay socket bench — sessions {report['sessions']}, cards "
                f"{report['cards']}, rounds {report['rounds']}, push "
                f"coalescing {report['push_min_interval']} s")
    else:
        head = (f"relay latency bench — sessions {report['sessions']}, cards "
                f"{report['cards']}, rounds {report['rounds']}, mailbox tick "
                f"{report['mailbox_tick']} s, hold {report['mailbox_hold']} s")
    lines = [
        head,
        "",
        "| trip | leg | p50 | p90 | max |",
        "|---|---|---|---|---|",
    ]
    for trip in trips_named:
        for leg in LEGS:
            row = report["trips"][trip][leg]
            lines.append(f"| {trip} | {leg} | {row['p50']:.3f} | "
                         f"{row['p90']:.3f} | {row['max']:.3f} |")
    lines += ["", "| reply | plain bytes | deflated | sealed (base64) |",
              "|---|---|---|---|"]
    for trip in trips_named:
        size = report["sizes"].get(trip, {})
        lines.append(f"| {trip} | {size.get('plain', 0)} | "
                     f"{size.get('deflated', 0)} | {size.get('sealed', 0)} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--cards", type=int, default=40)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--mailbox-tick", type=float, default=0.05,
                        help="the fake mailbox's poll tick while holding "
                             "(2.0 reproduces the real mailbox's POLL_MS)")
    parser.add_argument("--mailbox-hold", type=float, default=1.0,
                        help="how long the fake mailbox holds a wait=1 GET")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON object instead of the table")
    parser.add_argument("--socket", action="store_true",
                        help="drive the socket lane (relay_ws) against the "
                             "suite's fake socket relay instead of the mailbox")
    args = parser.parse_args(argv)
    if args.rounds < 1 or args.sessions < 0 or args.cards < 0:
        parser.error("rounds must be ≥ 1; sessions and cards ≥ 0")
    try:
        report = asyncio.run(_socket_bench(args) if args.socket else _bench(args))
    finally:
        # The sandbox this run wrote its relay.json / devices.json under,
        # and the pytest-guard root (`$TMPDIR/dark-army-tests-<pid>/`)
        # every other path constant was derived from.
        shutil.rmtree(_TMP, ignore_errors=True)
        shutil.rmtree(paths._home(), ignore_errors=True)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
