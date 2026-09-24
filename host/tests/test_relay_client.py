# host/tests/test_relay_client.py
"""The connector against a fake mailbox.

A stdlib ``http.server`` on a free port on a thread plays the Vercel
function (the `test_lan_access.py` real-server precedent); the test plays
the phone, sealing and opening frames with ``relay.py``'s own helpers — the
same code path ``RelayTransport.swift`` mirrors. The connector under test is
the real one, wired to a real ``ApiServer._remote_run``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from dark_army_daemon import devices, paths, relay, relay_client
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    relay.reset()
    devices.invalidate()
    yield
    relay.reset()
    devices.invalidate()


class _Mailbox(BaseHTTPRequestHandler):
    """The fake relay: per-(channel, direction) deques, short waits."""

    boxes: dict = defaultdict(deque)
    lock = threading.Lock()
    #: Every GET this mailbox saw: ``(monotonic, full path)``. The idle-pacing
    #: tests read the cadence and the query string straight out of it.
    gets: list = []
    #: When set, every request is answered with this status instead — the
    #: seam for "the mailbox is refusing" (429, 503).
    refuse: int = 0

    def log_message(self, *args):  # quiet
        pass

    def _key(self):
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        if parsed.path != "/api/box":
            return None
        qs = parse_qs(parsed.query)
        ch = (qs.get("ch") or [""])[0]
        direction = (qs.get("dir") or [""])[0]
        if not ch or direction not in ("to-mac", "to-phone"):
            return None
        return (ch, direction)

    def _refused(self) -> bool:
        if not _Mailbox.refuse:
            return False
        self.send_response(_Mailbox.refuse)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_POST(self):
        if self._refused():
            return
        key = self._key()
        if key is None:
            self.send_response(400)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        with _Mailbox.lock:
            _Mailbox.boxes[key].appendleft(body)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        with _Mailbox.lock:
            _Mailbox.gets.append((time.monotonic(), self.path))
        if self._refused():
            return
        key = self._key()
        if key is None:
            self.send_response(400)
            self.end_headers()
            return
        # The real mailbox holds the connection only when asked to. A bare
        # GET is one RPOP and an immediate answer — which is exactly the
        # cheap shape the connector's idle mode is buying.
        held = "wait=1" in self.path
        deadline = time.monotonic() + (1.0 if held else 0.0)
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
            time.sleep(0.05)


@pytest.fixture
def mailbox():
    _Mailbox.boxes = defaultdict(deque)
    _Mailbox.gets = []
    _Mailbox.refuse = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Mailbox)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    yield url
    server.shutdown()
    server.server_close()


@pytest_asyncio.fixture
async def connector(mailbox):
    """A running connector wired to a real `_remote_run`, plus phone hands."""
    data = relay.load()
    data["url"] = mailbox  # loopback http: the stated test seam
    assert relay.save(data)
    key_b64 = relay.create_channel("dev-1")
    assert key_b64
    key = relay.channel_key("dev-1")
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    conn = relay_client.RelayConnector(srv)
    conn.start()
    try:
        yield conn, srv, key, mailbox
    finally:
        await conn.stop()


def _build_connector(mailbox_url: str):
    """The `connector` fixture's body, callable from a test that has to
    monkeypatch the pacing constants *before* the channel task starts."""
    data = relay.load()
    data["url"] = mailbox_url
    assert relay.save(data)
    assert relay.create_channel("dev-1")
    key = relay.channel_key("dev-1")
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    conn = relay_client.RelayConnector(srv)
    return conn, srv, daemon, key


def _make_channel_idle(device_id: str = "dev-1") -> None:
    """Age the channel's liveness record past the active window — the one
    input the idle/active decision reads."""
    data = relay.load()
    channels = dict(data.get("channels") or {})
    entry = dict(channels.get(device_id) or {})
    entry["last_frame_at"] = time.time() - relay_client.ACTIVE_WINDOW_SECONDS - 60
    channels[device_id] = entry
    data["channels"] = channels
    assert relay.save(data)
    relay._frame_mem.pop(device_id, None)


def _post_box(url: str, chan: str, direction: str, wire: str) -> None:
    req = urllib.request.Request(
        f"{url}/api/box?ch={chan}&dir={direction}", data=wire.encode(),
        method="POST")
    urllib.request.urlopen(req, timeout=5).read()


def _pop_box(url: str, chan: str, direction: str) -> str:
    req = urllib.request.Request(
        f"{url}/api/box?ch={chan}&dir={direction}&wait=1")
    resp = urllib.request.urlopen(req, timeout=5)
    if resp.status != 200:
        return ""
    return resp.read().decode()


async def _phone_send(key: bytes, url: str, ctr: int, kind: str, body) -> None:
    chan = relay.channel_id(key)
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, kind, body)
    await asyncio.to_thread(_post_box, url, chan, "to-mac", wire)


async def _phone_receive(key: bytes, url: str, last_ctr: int,
                         timeout: float = 8.0):
    """Poll to-phone until one verifiable frame arrives, or time out."""
    chan = relay.channel_id(key)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wire = await asyncio.to_thread(_pop_box, url, chan, "to-phone")
        if wire:
            frame, err = relay.open_frame(
                key, relay.DIR_MAC_TO_PHONE, wire, last_ctr)
            assert err == "", err
            return frame
        await asyncio.sleep(0.05)
    return None


@pytest.mark.asyncio
async def test_full_loop_for_state(connector):
    _conn, _srv, key, url = connector
    await _phone_send(key, url, 1, "state", {})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None
    assert frame["kind"] == "reply"
    body = frame["body"]
    assert body["status"] == 200
    payload = json.loads(body["body"])
    assert "agents" in payload


@pytest.mark.asyncio
async def test_full_loop_for_one_write(connector, monkeypatch):
    _conn, srv, key, url = connector
    calls = []

    async def fake_lan_run(action, payload, device_id=""):
        calls.append((action, payload.get("session_id")))
        return 200, "application/json", b'{"ok": true}'

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    await _phone_send(key, url, 1, "action",
                      {"action": "dismiss", "session_id": "s1"})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None
    assert frame["body"]["status"] == 200
    assert calls == [("dismiss", "s1")]


@pytest.mark.asyncio
async def test_key_removed_mid_run_goes_silent(connector):
    _conn, _srv, key, url = connector
    # Prove the channel is alive first.
    await _phone_send(key, url, 1, "state", {})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None
    # Un-pair: the key is deleted; the connector re-reads it per frame.
    relay.forget("dev-1")
    await _phone_send(key, url, 2, "state", {})
    silent = await _phone_receive(key, url, frame["ctr"], timeout=2.5)
    assert silent is None


@pytest.mark.asyncio
async def test_malformed_mailbox_body_dropped_connector_lives(connector):
    conn, _srv, key, url = connector
    chan = relay.channel_id(key)
    await asyncio.to_thread(_post_box, url, chan, "to-mac", "not an envelope")
    # The garbage is dropped and counted; the next real frame still answers.
    await _phone_send(key, url, 1, "state", {})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None
    assert frame["body"]["status"] == 200
    assert sum(conn.dropped.values()) >= 1


@pytest.mark.asyncio
async def test_slow_action_does_not_block_polling(connector, monkeypatch):
    """One held write (`prepare_card` keeps the phone waiting 135s) must not
    stop the channel answering its 8s state polls — each verified frame runs
    as its own task, off the polling loop."""
    _conn, srv, key, url = connector
    release = asyncio.Event()

    async def slow_lan_run(action, payload, device_id=""):
        await release.wait()
        return 200, "application/json", b'{"ok": true}'

    monkeypatch.setattr(srv, "_lan_run", slow_lan_run)
    await _phone_send(key, url, 1, "action",
                      {"action": "dismiss", "session_id": "s1"})
    await _phone_send(key, url, 2, "state", {})
    # The state answer arrives while the action is still held.
    frame = await _phone_receive(key, url, 0)
    assert frame is not None
    assert frame["kind"] == "reply"
    assert "agents" in json.loads(frame["body"]["body"])
    release.set()
    reply = await _phone_receive(key, url, frame["ctr"])
    assert reply is not None
    assert reply["body"]["status"] == 200


@pytest.mark.asyncio
async def test_concurrent_answers_reach_the_mailbox_in_ctr_order(
        connector, monkeypatch):
    """Two concurrent actions whose answers race: ctr allocation and the
    mailbox POST must be one step per device. Without `_answer`'s send lock,
    the first answer allocates its ctr and then stalls on the POST, the
    second allocates a *higher* ctr and lands *first* in the FIFO — the
    phone pops it, its `recvCtr` advances, and the earlier answer is then
    hard-refused as a replay: the action ran, the phone reports failure."""
    conn, srv, key, url = connector
    hold = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def held_lan_run(action, payload, device_id=""):
        sid = payload.get("session_id")
        await hold[sid].wait()
        return 200, "application/json", json.dumps({"ran": sid}).encode()

    monkeypatch.setattr(srv, "_lan_run", held_lan_run)

    real_http = conn._http
    slowed = {"done": False}

    async def slow_first_answer_post(method, target, data=None):
        # Only the first to-phone POST stalls — inside the lock, which is
        # exactly where an unlocked implementation lets the race in.
        if method == "POST" and "to-phone" in target and not slowed["done"]:
            slowed["done"] = True
            await asyncio.sleep(0.5)
        return await real_http(method, target, data)

    monkeypatch.setattr(conn, "_http", slow_first_answer_post)

    await _phone_send(key, url, 1, "action",
                      {"action": "dismiss", "session_id": "a"})
    await _phone_send(key, url, 2, "action",
                      {"action": "dismiss", "session_id": "b"})
    # Let the connector poll both frames and start both executions.
    await asyncio.sleep(0.5)
    hold["a"].set()          # a's answer takes the lock, POST stalls 0.5s
    await asyncio.sleep(0.1)
    hold["b"].set()          # b's answer arrives while a's POST is held

    first = await _phone_receive(key, url, 0)
    assert first is not None
    assert json.loads(first["body"]["body"])["ran"] == "a"
    second = await _phone_receive(key, url, first["ctr"])
    assert second is not None
    assert json.loads(second["body"]["body"])["ran"] == "b"
    assert second["ctr"] > first["ctr"]


@pytest.mark.asyncio
async def test_stale_counter_answers_ctr_expected(connector):
    _conn, _srv, key, url = connector
    await _phone_send(key, url, 5, "state", {})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None and frame["kind"] == "reply"
    # A frame at or below the accepted counter is refused — but the refusal
    # is sealed to the phone and names the counter to fast-forward to.
    await _phone_send(key, url, 5, "state", {})
    err = await _phone_receive(key, url, frame["ctr"])
    assert err is not None
    assert err["kind"] == "err"
    assert err["body"]["ctr_expected"] == 6


# --- the away path saying why it stopped ---------------------------------------


@pytest.mark.asyncio
async def test_a_refusing_mailbox_is_logged_once_then_recovers(
        mailbox, monkeypatch, caplog):
    """The bug this replaces: a 429, 503, 500 or 403 fell into `if status not
    in (200, 204): sleep(RETRY); continue` and was retried every five seconds
    **forever with no log line at any level**. The away path died and the
    machine said nothing. What it must do instead is say it once, on the
    transition, and say it again only on a slow heartbeat — a permanently
    dead mailbox cannot be allowed to flood the log either.
    """
    monkeypatch.setattr(relay_client, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(relay_client, "HEALTH_REPORT_SECONDS", 600.0)
    _Mailbox.refuse = 429
    conn, _srv, _daemon, _key = _build_connector(mailbox)
    caplog.set_level("INFO", logger="dark-army")
    conn.start()
    try:
        await asyncio.sleep(1.0)  # ~20 retries at 0.05s
        failing = [r for r in caplog.records
                   if "relay channel failing" in r.getMessage()]
        assert len(failing) == 1, [r.getMessage() for r in caplog.records]
        assert "429" in failing[0].getMessage()
        assert failing[0].levelname == "WARNING"
        assert not [r for r in caplog.records
                    if "still failing" in r.getMessage()]
        # And it recovers, once, when the mailbox does.
        _Mailbox.refuse = 0
        # The channel is active, so its next poll is a held one — the fake
        # mailbox holds it a second before answering 204.
        await asyncio.sleep(2.0)
        recovered = [r for r in caplog.records
                     if "relay channel recovered" in r.getMessage()]
        assert len(recovered) == 1
        assert "429" in recovered[0].getMessage()
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_the_reminder_line_is_time_gated_not_per_retry(
        mailbox, monkeypatch, caplog):
    """With the heartbeat interval short, a long outage says something more
    than once — but on the heartbeat, never on the retry."""
    monkeypatch.setattr(relay_client, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(relay_client, "HEALTH_REPORT_SECONDS", 0.3)
    _Mailbox.refuse = 503
    conn, _srv, _daemon, _key = _build_connector(mailbox)
    caplog.set_level("INFO", logger="dark-army")
    conn.start()
    try:
        await asyncio.sleep(1.0)
        reminders = [r for r in caplog.records
                     if "still failing" in r.getMessage()]
        # ~20 retries in the window; at one line per 0.3s, a handful.
        assert 1 <= len(reminders) <= 5, len(reminders)
        assert all("503" in r.getMessage() for r in reminders)
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_a_quiet_channel_polls_cheaply_and_backs_off(
        mailbox, monkeypatch):
    """The idle burn: a held 20-second poll costs the mailbox ~52 storage
    commands, and the Mac held one continuously whether or not anybody was
    away. A quiet channel must ask *without* the held-poll flag and space its
    asks out, doubling to a ceiling."""
    monkeypatch.setattr(relay_client, "IDLE_GAP_START", 0.1)
    monkeypatch.setattr(relay_client, "IDLE_GAP_MAX", 0.4)
    conn, _srv, _daemon, _key = _build_connector(mailbox)
    _make_channel_idle()
    conn.start()
    try:
        await asyncio.sleep(1.5)
    finally:
        await conn.stop()
    with _Mailbox.lock:
        gets = [g for g in _Mailbox.gets if "dir=to-mac" in g[1]]
    assert len(gets) >= 4, gets
    assert all("wait=1" not in path for _, path in gets), gets
    gaps = [b[0] - a[0] for a, b in zip(gets, gets[1:])]
    # 0.1, 0.2, 0.4, 0.4, … — the escalation is visible in the first three.
    assert gaps[1] > gaps[0], gaps
    assert gaps[2] > gaps[1], gaps
    assert max(gaps) < 0.4 + 0.3, gaps  # the ceiling holds


@pytest.mark.asyncio
async def test_a_carried_frame_snaps_the_channel_back_to_held_polls(
        mailbox, monkeypatch):
    """One frame flips the channel active, and everything after it is fast
    again — the whole cost of the idle backoff is the *first* request."""
    monkeypatch.setattr(relay_client, "IDLE_GAP_START", 0.1)
    monkeypatch.setattr(relay_client, "IDLE_GAP_MAX", 0.2)
    conn, _srv, _daemon, key = _build_connector(mailbox)
    _make_channel_idle()
    before = relay.last_frame_at("dev-1")
    conn.start()
    try:
        await asyncio.sleep(0.5)
        with _Mailbox.lock:
            idle_gets = [g for g in _Mailbox.gets if "dir=to-mac" in g[1]]
        assert idle_gets and all("wait=1" not in p for _, p in idle_gets)
        await _phone_send(key, mailbox, 1, "state", {})
        frame = await _phone_receive(key, mailbox, 0)
        assert frame is not None and frame["kind"] == "reply"
        # (d) the frame is recorded as liveness, which is what flipped it.
        assert relay.last_frame_at("dev-1") > before
        await asyncio.sleep(0.5)
        with _Mailbox.lock:
            polls = [g for g in _Mailbox.gets if "dir=to-mac" in g[1]]
        after = polls[len(idle_gets):]
        assert any("wait=1" in p for _, p in after), after
    finally:
        await conn.stop()


_NOW = 1_700_000_000.0


@pytest.mark.parametrize(
    "now, last_frame_at, last_push_at, idle_gap, expected",
    [
        (_NOW, _NOW - 10, _NOW - 10, 5, (True, 0.0, 5)),
        (_NOW, _NOW - 10, 0.0, 5, (True, 0.0, 5)),
        (_NOW, _NOW - 700, _NOW - 10, 5, (True, 0.0, 5)),
        (_NOW, _NOW - 700, _NOW - 700, 5, (False, 5, 10)),
        (_NOW, 0.0, 0.0, 5, (False, 5, 10)),
        (_NOW, _NOW - 600, 0.0, 5, (True, 0.0, 5)),
        (_NOW, _NOW - 601, 0.0, 5, (False, 5, 10)),
        (_NOW, _NOW - 700, 0.0, 20, (False, 20, 30)),
        (_NOW, _NOW - 700, 0.0, 30, (False, 30, 30)),
        (_NOW, _NOW - 10, 0.0, 30, (True, 0.0, 5)),
        (_NOW, _NOW + 5, 0.0, 5, (True, 0.0, 5)),
        (_NOW, 0.0, _NOW - 1, 20, (True, 0.0, 5)),
        # A buzz arms the shorter push window, never the frame's.
        (_NOW, 0.0, _NOW - 90, 5, (True, 0.0, 5)),
        (_NOW, _NOW - 700, _NOW - 91, 5, (False, 5, 10)),
        (_NOW, 0.0, _NOW - 300, 20, (False, 20, 30)),
        (_NOW, 0.0, _NOW + 5, 5, (True, 0.0, 5)),
    ],
)
def test_next_gap_table(monkeypatch, now, last_frame_at, last_push_at,
                        idle_gap, expected):
    """The connector's whole pacing decision, as a table of moments. A
    frame holds the full window; a buzz only `PUSH_ARM_SECONDS`, because
    the phone's first frame re-arms the window by itself and the push leg
    only has to bridge until a tap lands."""
    monkeypatch.setattr(relay_client, "ACTIVE_WINDOW_SECONDS", 600)
    monkeypatch.setattr(relay_client, "PUSH_ARM_SECONDS", 90)
    monkeypatch.setattr(relay_client, "IDLE_GAP_START", 5)
    monkeypatch.setattr(relay_client, "IDLE_GAP_MAX", 30)
    assert relay_client._next_gap(
        now, last_frame_at, last_push_at, idle_gap) == expected


@pytest.mark.asyncio
async def test_a_push_that_went_through_arms_a_held_poll(mailbox, monkeypatch):
    """A buzz the mailbox took is enough to hold the next poll — the frame
    stamp can stay stale. The stamp is written by `push_alert` before the
    channel task starts, so the first poll it makes is the held one; the
    test waits for that poll to exist, never on a measured gap."""
    monkeypatch.setattr(relay_client, "IDLE_GAP_START", 0.1)
    monkeypatch.setattr(relay_client, "IDLE_GAP_MAX", 0.2)
    conn, _srv, _daemon, _key = _build_connector(mailbox)
    _make_channel_idle()
    before = relay.last_frame_at("dev-1")
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    _push_only(conn, lambda *a: (200, b"ok"))
    stale = relay.last_frame_at("dev-1")
    assert not relay_client._next_gap(time.time(), stale, 0.0, 0.1)[0]
    assert await conn.push_alert("dev-1", {"title": "x", "badge": 1}) is True
    assert relay.last_frame_at("dev-1") == before
    pushed = conn._last_push_ok_at("dev-1")
    assert pushed > 0
    assert relay_client._next_gap(time.time(), stale, pushed, 0.1)[0]
    conn.start()
    try:
        deadline = time.monotonic() + 5.0
        polls = []
        while time.monotonic() < deadline and not polls:
            await asyncio.sleep(0.02)
            with _Mailbox.lock:
                polls = [g for g in _Mailbox.gets if "dir=to-mac" in g[1]]
        assert polls and "wait=1" in polls[0][1], polls
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_the_snapshot_carries_health_and_no_secret(connector):
    """`/api/state` is ungated on loopback, served on the LAN door and
    carried through the relay itself. The health record is statuses and clock
    readings — never the key, never its digest, and never the channel id,
    which is *derived from the key* and would be a free traffic-analysis
    handle for anyone who read one snapshot."""
    conn, _srv, key, url = connector
    daemon = conn._api._daemon
    daemon.remote_access_enabled = True
    daemon._relay_connector = conn
    data = relay.load()
    data["push_secret"] = "buzzword-9000"
    assert relay.save(data)
    relay.invalidate()
    await _phone_send(key, url, 1, "state", {})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None
    snap = daemon.devices_snapshot()
    assert snap["relay_health"]["state"] == "ok"
    assert snap["relay_health"]["last_ok_at"] > 0
    stored = relay.load()["channels"]["dev-1"]
    blob = json.dumps(snap)
    assert stored["key_b64"] not in blob
    assert relay.channel_id(key) not in blob
    import re as _re
    assert not _re.search(r"[0-9a-f]{32}", blob), blob
    # The push secret is for the connector's Bearer header alone — never a
    # snapshot field, on either surface.
    assert "buzzword-9000" not in blob
    assert "buzzword-9000" not in json.dumps(_srv.state())


# --- link timing: the Mac's legs on the reply, the rollup on the log ------------


@pytest.mark.asyncio
async def test_a_reply_carries_the_macs_hops(connector):
    """The reply envelope carries `timing` beside `body`: the mailbox
    dwell, the held GET's wait, the seal's opening and the run, floats at
    or above zero — and the body under it is the same `_remote_run` answer
    it always was."""
    _conn, srv, key, url = connector
    await _phone_send(key, url, 1, "state", {})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None and frame["kind"] == "reply"
    envelope = frame["body"]
    assert set(envelope) == {"re", "status", "content_type", "body", "timing"}
    assert set(envelope["timing"]) == {"dwell", "hold", "open", "run"}
    for leg in envelope["timing"].values():
        assert isinstance(leg, float) and leg >= 0.0
    # The phone stamped `ts` a moment ago, so dwell is small and honest.
    assert envelope["timing"]["dwell"] < 30.0
    payload = json.loads(envelope["body"])
    assert "agents" in payload
    assert "timing" not in payload


@pytest.mark.asyncio
async def test_the_timing_rollup_lands_on_the_access_log_and_never_alerts(
        mailbox, monkeypatch, tmp_path):
    """Two requests inside a window shortened to 0.2 s: the second closes
    the first window, and one `timing` line with `count` ≥ 2 lands on the
    access log — through `note_link_timing` alone. The detector is never
    consulted (a spy on `BurstDetector.note`), no alert opens and the
    line's `hops` is exactly `HOP_KEYS`."""
    from dark_army_daemon import access_log, link_timing
    monkeypatch.setattr(link_timing, "ROLLUP_SECONDS", 0.2)
    conn, srv, daemon, key = _build_connector(mailbox)
    log = access_log.AccessLog(path=tmp_path / "access.jsonl")
    log.open()
    daemon._access_log = log
    noted = []
    monkeypatch.setattr(daemon._burst_detector, "note",
                        lambda *a, **k: noted.append((a, k)))
    conn.start()
    try:
        await _phone_send(key, url := mailbox, 1, "state", {})
        assert await _phone_receive(key, url, 0) is not None
        await asyncio.sleep(0.3)
        await _phone_send(key, url, 2, "usage", {})
        assert await _phone_receive(key, url, 1) is not None
        await _phone_send(key, url, 3, "state", {})
        assert await _phone_receive(key, url, 2) is not None
        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            rows = [r for r in log.recent(include_timing=True)
                    if r["kind"] == "timing"]
            if rows:
                break
            await asyncio.sleep(0.05)
    finally:
        await conn.stop()
    assert len(rows) >= 1, log.recent(include_timing=True)
    line = rows[-1]
    assert line["door"] == "relay"
    assert line["device_id"] == "dev-1"
    assert line["count"] >= 1
    assert tuple(line) == access_log.PUBLISHED_KEYS
    assert set(line["hops"]) == set(access_log.HOP_KEYS)
    assert line["hops"]["n"] >= 1
    assert line["hops"]["size_max"] > 0
    assert line["text"].startswith("relay: ")
    assert noted == []
    assert log.open_alerts() == []
    assert daemon.security_snapshot()["alerts"] == []
    assert daemon._burst_detector.hits("dev-1") == 0
    # The default read hides it; nothing secret rides it.
    assert [r for r in log.recent() if r["kind"] == "timing"] == []
    blob = json.dumps(line)
    assert relay.load()["channels"]["dev-1"]["key_b64"] not in blob
    assert relay.channel_id(key) not in blob
    assert mailbox not in blob


@pytest.mark.asyncio
async def test_the_timing_echo_is_not_in_the_state_digest(connector):
    """Two polls quoting the digest stay `unchanged` with the echo present:
    the legs move on every request, so had they entered the state body
    every away poll would be the whole picture again."""
    _conn, _srv, key, url = connector
    await _phone_send(key, url, 1, "state", {})
    first = await _phone_receive(key, url, 0)
    assert first is not None
    digest = json.loads(first["body"]["body"])["state_digest"]
    assert first["body"]["timing"]
    await _phone_send(key, url, 2, "state", {"digest": digest})
    second = await _phone_receive(key, url, 1)
    assert second is not None
    assert second["body"]["timing"]
    assert second["body"]["timing"] != first["body"]["timing"]
    payload = json.loads(second["body"]["body"])
    assert payload["unchanged"] is True
    assert payload["state_digest"] == digest
    await _phone_send(key, url, 3, "state", {"digest": digest})
    third = await _phone_receive(key, url, 2)
    assert json.loads(third["body"]["body"])["unchanged"] is True


# --- push_alert: the buzz's Mac half -------------------------------------------


def _push_only(conn, answer):
    """Install an ``_http`` that scripts the **push route only** and hands
    every other URL straight back to the real one.

    The `connector` fixture is *running*: a blanket fake fails its poll leg
    too and makes the away link genuinely failing — the exact false positive
    these tests exist to rule out. ``answer`` is called with the request and
    either returns ``(status, body)`` or raises (a transport failure).
    """
    real = conn._http

    async def routed(method, url, data=None, headers=None):
        if "/api/push" in url:
            return answer(method, url, data, headers)
        return await real(method, url, data, headers)

    conn._http = routed
    return routed



@pytest.mark.asyncio
async def test_push_alert_reads_the_token_at_the_moment_of_use(connector):
    """No token, no POST; a token, one POST to the push route with the
    store's own tok/env beside the daemon's composed title and badge; after
    `forget`, silence again — the `channel_key` discipline, so un-pairing
    is the push kill switch too."""
    conn, _srv, key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append((method, url, data))
        return 200, b"ok"

    conn._http = fake_http
    assert await conn.push_alert("dev-1", {"title": "x", "badge": 1}) is False
    assert calls == []

    relay.note_push_token("dev-1", "ab" * 32, "dev")
    ok = await conn.push_alert("dev-1", {"title": "Vex needs you",
                                         "badge": 2})
    assert ok is True
    ((method, url, data),) = calls
    assert method == "POST"
    assert url.endswith(f"/api/push?ch={relay.channel_id(key)}")
    body = json.loads(data)
    assert body == {"tok": "ab" * 32, "env": "dev",
                    "title": "Vex needs you", "badge": 2}

    relay.forget("dev-1")
    calls.clear()
    assert await conn.push_alert("dev-1", {"title": "x", "badge": 0}) is False
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [
    ("finished", "finished"),
    ("permission", "permission"),
    ("loud", None),
    ("", None),
    (None, None),
])
async def test_push_alert_carries_a_kind_only_from_the_closed_set(
        connector, kind, expected):
    """The kind word is the third and last caller field on the wire, and it
    is sent only when it is a member of `PUSH_KINDS`. Anything else is
    omitted rather than forwarded — the mailbox then plays its default
    sound, which is exactly the buzz before kinds existed."""
    conn, _srv, _key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append(json.loads(data))
        return 200, b"ok"

    conn._http = fake_http
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    body = {"title": "Vex finished", "badge": 0}
    if kind is not None:
        body["kind"] = kind
    assert await conn.push_alert("dev-1", body) is True
    (posted,) = calls
    if expected is None:
        assert "kind" not in posted
        assert set(posted) == {"tok", "env", "title", "badge"}
    else:
        assert posted["kind"] == expected
        assert set(posted) == {"tok", "env", "title", "badge", "kind"}


@pytest.mark.asyncio
@pytest.mark.parametrize("work,expected", [
    ("Add options trading", "Add options trading"),
    ("  Add   options\n trading ", "Add options trading"),
    ("", None),
    (None, None),
    ("x" * 200, "x" * relay_client.PUSH_WORK_CHARS),
])
async def test_push_alert_carries_a_clamped_work_line(connector, work, expected):
    """The fourth and last caller field: one short line naming what the agent
    is working on, whitespace-collapsed and clamped to `PUSH_WORK_CHARS`, and
    **absent** rather than empty when there is nothing to say — an empty
    string would render as a blank second line on the phone."""
    conn, _srv, _key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append(json.loads(data))
        return 200, b"ok"

    conn._http = fake_http
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    body = {"title": "Vex needs you", "badge": 1}
    if work is not None:
        body["work"] = work
    assert await conn.push_alert("dev-1", body) is True
    (posted,) = calls
    if expected is None:
        assert "work" not in posted
        assert set(posted) == {"tok", "env", "title", "badge"}
    else:
        assert posted["work"] == expected
        assert len(posted["work"]) <= relay_client.PUSH_WORK_CHARS
        assert set(posted) == {"tok", "env", "title", "badge", "work"}


@pytest.mark.asyncio
@pytest.mark.parametrize("need,expected", [
    ("Approve running Bash", "Approve running Bash"),
    ("  Which   database\n do I use? ", "Which database do I use?"),
    ("", None),
    (None, None),
    ("x" * 300, "x" * relay_client.PUSH_NEED_CHARS),
])
async def test_push_alert_carries_a_clamped_need_line(connector, need, expected):
    """The third line: one sentence saying what is needed, whitespace-
    collapsed and clamped to `PUSH_NEED_CHARS`, and **absent** rather than
    empty when there is nothing to say — an empty string would render as a
    blank line on the phone."""
    conn, _srv, _key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append(json.loads(data))
        return 200, b"ok"

    conn._http = fake_http
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    body = {"title": "Vex wants to run a tool", "badge": 1}
    if need is not None:
        body["need"] = need
    assert await conn.push_alert("dev-1", body) is True
    (posted,) = calls
    if expected is None:
        assert "need" not in posted
        assert set(posted) == {"tok", "env", "title", "badge"}
    else:
        assert posted["need"] == expected
        assert len(posted["need"]) <= relay_client.PUSH_NEED_CHARS
        assert set(posted) == {"tok", "env", "title", "badge", "need"}


@pytest.mark.asyncio
@pytest.mark.parametrize("face,expected", [
    ("ptyś", "ptyś"),
    ("vex", "vex"),
    ("../x", None),
    ("mrrobot", None),
    ("", None),
    (None, None),
])
async def test_push_alert_carries_a_face_only_from_the_roster(
        connector, face, expected):
    """The slug joins the wire only when it is a member of `PUSH_FACE_SLUGS`.
    A miss is dropped, not a failed buzz, and a body without the field is
    unchanged."""
    conn, _srv, _key, _url = connector
    calls = []

    async def fake_http(method, url, data=None, headers=None):
        calls.append(json.loads(data))
        return 200, b"ok"

    conn._http = fake_http
    relay.note_push_token("dev-1", "ab" * 32, "dev")
    body = {"title": "Vex asked you a question", "badge": 1}
    if face is not None:
        body["face"] = face
    assert await conn.push_alert("dev-1", body) is True
    (posted,) = calls
    if expected is None:
        assert "face" not in posted
        assert set(posted) == {"tok", "env", "title", "badge"}
    else:
        assert posted["face"] == expected
        assert set(posted) == {"tok", "env", "title", "badge", "face"}


def test_the_wire_set_matches_the_policy_set():
    from dark_army_daemon import alerts

    assert relay_client.PUSH_KINDS == alerts.KINDS


@pytest.mark.asyncio
async def test_push_alert_failures_feed_the_push_track_not_the_away_link(
        connector):
    """A refused buzz is a buzz problem. It lands in the connector's own
    push record and leaves the away link — the one thing `relay_health`
    speaks for, and the one thing the Devices menu draws — saying "ok"."""
    conn, _srv, key, url = connector
    daemon = conn._api._daemon
    daemon.remote_access_enabled = True
    daemon._relay_connector = conn
    relay.note_push_token("dev-1", "ab" * 32, "prod")
    _push_only(conn, lambda *a: (503, b"unavailable"))

    # One real frame, so the poll leg has actually answered and the away
    # record exists to be judged.
    await _phone_send(key, url, 1, "state", {})
    assert await _phone_receive(key, url, 0) is not None

    assert await conn.push_alert("dev-1", {"title": "x", "badge": 0}) is False
    assert await conn.push_alert("dev-1", {"title": "x", "badge": 0}) is False

    assert conn._push_health["dev-1"].state == "failing"
    assert conn._push_health["dev-1"].failures == 2
    assert conn._health["dev-1"].state == "ok"
    assert conn.health_snapshot()["state"] == "ok"
    snap = daemon.devices_snapshot()
    assert snap["relay_health"]["state"] == "ok"
    # Log-only, by decision: the buzz track is never a snapshot field.
    assert "push_health" not in json.dumps(snap)


@pytest.mark.asyncio
async def test_push_alert_is_bucketed_per_device(connector):
    conn, _srv, _key, _url = connector
    relay.note_push_token("dev-1", "ab" * 32, "prod")
    sent = []

    async def fake_http(method, url, data=None, headers=None):
        sent.append(url)
        return 200, b"ok"

    conn._http = fake_http
    results = []
    for _ in range(relay_client.PUSH_MAX_PER_MINUTE + 3):
        results.append(await conn.push_alert("dev-1", {"title": "x",
                                                       "badge": 0}))
    assert results.count(True) == relay_client.PUSH_MAX_PER_MINUTE
    assert len(sent) == relay_client.PUSH_MAX_PER_MINUTE


@pytest.mark.asyncio
async def test_push_alert_sends_the_stored_push_secret_as_a_bearer(connector):
    """The push route is the one thing the mailbox authenticates — `ch` is
    caller-chosen and proves nothing — so the Mac proves itself with the
    stored `push_secret`. No secret stored means no header, not an empty
    one."""
    conn, _srv, _key, _url = connector
    relay.note_push_token("dev-1", "ab" * 32, "prod")
    seen = []

    async def fake_http(method, url, data=None, headers=None):
        seen.append(dict(headers or {}))
        return 200, b"ok"

    conn._http = fake_http
    assert await conn.push_alert("dev-1", {"title": "x", "badge": 0}) is True
    assert seen[0] == {}

    data = relay.load()
    data["push_secret"] = "s3same"
    assert relay.save(data)
    relay.invalidate()
    assert await conn.push_alert("dev-1", {"title": "x", "badge": 0}) is True
    assert seen[1] == {"Authorization": "Bearer s3same"}


@pytest.mark.asyncio
async def test_a_refused_push_secret_is_said_once_in_words(connector, caplog):
    """A 401 from the push route is a misconfigured or missing secret — say
    so in words, once per failing run, and never print the secret itself."""
    import logging

    conn, _srv, _key, _url = connector
    relay.note_push_token("dev-1", "ab" * 32, "prod")
    data = relay.load()
    data["push_secret"] = "s3same"
    assert relay.save(data)
    relay.invalidate()

    _push_only(conn, lambda *a: (401, b"unauthorized"))
    with caplog.at_level(logging.WARNING):
        assert await conn.push_alert("dev-1",
                                     {"title": "x", "badge": 0}) is False
        assert await conn.push_alert("dev-1",
                                     {"title": "x", "badge": 0}) is False
    health = conn._push_health.get("dev-1")
    assert health is not None and health.state == "failing"
    lines = [r.getMessage() for r in caplog.records]
    assert sum("refused the push secret" in line for line in lines) == 1
    assert all("s3same" not in line for line in lines)
    # One line for the event, not two: the wording *is* the transition line,
    # and it is the buzz that failed, never the away link.
    assert not [line for line in lines if "relay channel failing" in line]
    assert not [line for line in lines if "phone buzz failing" in line]
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_push_track_reports_on_transition_and_heartbeat(connector,
                                                              caplog,
                                                              monkeypatch):
    """The buzz record gets the away link's own discipline in its own words:
    one warning on the transition, a time-gated reminder while it stays
    broken, one quiet note when it recovers — and it never creates or moves
    the channel record."""
    import logging

    conn, _srv, _key, _url = connector
    relay.note_push_token("dev-1", "ab" * 32, "prod")
    monkeypatch.setattr(relay_client, "HEALTH_REPORT_SECONDS", 0.0)

    def blow_up(*_a):
        raise OSError("no route to host")

    _push_only(conn, blow_up)
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert await conn.push_alert("dev-1",
                                         {"title": "x", "badge": 0}) is False
    lines = [r.getMessage() for r in caplog.records]
    assert sum("phone buzz failing (status no answer)" in line
               for line in lines) == 1
    assert sum("phone buzz still failing" in line for line in lines) == 2
    assert not [line for line in lines if "relay channel" in line
                and "failing" in line]

    caplog.clear()
    _push_only(conn, lambda *a: (200, b"ok"))
    with caplog.at_level(logging.INFO):
        assert await conn.push_alert("dev-1",
                                     {"title": "x", "badge": 0}) is True
    lines = [r.getMessage() for r in caplog.records]
    assert sum("phone buzz recovered after 3 failures" in line
               for line in lines) == 1
    assert conn._push_health["dev-1"].state == "ok"

    caplog.clear()
    _push_only(conn, lambda *a: (502, b"apple refused"))
    with caplog.at_level(logging.INFO):
        assert await conn.push_alert("dev-1",
                                     {"title": "x", "badge": 0}) is False
    lines = [r.getMessage() for r in caplog.records]
    assert sum("phone buzz failing (status 502)" in line
               for line in lines) == 1

    # The away record was never touched by any of that: it is either absent
    # or exactly what the healthy poll leg made it.
    away = conn._health.get("dev-1")
    assert away is None or (away.state == "ok" and away.failures == 0)


@pytest.mark.asyncio
async def test_a_missing_push_route_is_one_info_line_and_no_failure(connector,
                                                                    caplog):
    """A mailbox deployed before the push route existed (404), or one whose
    push variables are unset (503 `unconfigured`), is not an outage. Say what
    to do once per run, then say nothing and count nothing."""
    import logging

    conn, _srv, _key, _url = connector
    relay.note_push_token("dev-1", "ab" * 32, "prod")

    answers = [(404, b"<html>NOT_FOUND</html>"),
               (404, b"<html>NOT_FOUND</html>"),
               (503, b"unconfigured"),
               (503, b"unconfigured\n")]
    _push_only(conn, lambda *a: answers.pop(0))
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            assert await conn.push_alert("dev-1",
                                         {"title": "x", "badge": 0}) is False
    notes = [r for r in caplog.records if "no push route yet" in r.getMessage()]
    assert len(notes) == 1
    assert notes[0].levelno == logging.INFO
    assert "vercel deploy --prod" in notes[0].getMessage()
    assert "PUSH_SECRET" in notes[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    buzz = conn._push_health.get("dev-1")
    assert buzz is None or (buzz.state == "ok" and buzz.failures == 0)
    away = conn._health.get("dev-1")
    assert away is None or (away.state == "ok" and away.failures == 0)

    # Once per *run*: a restarted connector may be facing a redeployed
    # mailbox, so it is allowed to say it again.
    caplog.clear()
    await conn.stop()
    conn.start()
    answers.append((404, b"<html>NOT_FOUND</html>"))
    with caplog.at_level(logging.INFO):
        assert await conn.push_alert("dev-1",
                                     {"title": "x", "badge": 0}) is False
    assert len([r for r in caplog.records
                if "no push route yet" in r.getMessage()]) == 1


# --- the diary ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_loop_for_the_log(connector, tmp_path):
    from dark_army_daemon.event_log import EventLog
    _conn, srv, key, url = connector
    log = EventLog(path=tmp_path / "event-log.jsonl")
    log.open()
    srv._daemon._event_log = log
    log.append("card_manual", title="Add the diary", project="bob")
    await _phone_send(key, url, 1, "log", {"query": "since=0"})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None
    assert frame["kind"] == "reply"
    assert frame["body"]["status"] == 200
    page = json.loads(frame["body"]["body"])
    assert page["available"] is True
    assert [e["text"] for e in page["events"]] == ["Add the diary needs a manual check"]


@pytest.mark.asyncio
async def test_an_unknown_read_kind_is_still_404(connector):
    _conn, _srv, key, url = connector
    await _phone_send(key, url, 1, "logs", {"query": "since=0"})
    frame = await _phone_receive(key, url, 0)
    assert frame is not None
    assert frame["body"]["status"] == 404
