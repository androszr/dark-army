# host/tests/test_relay_ws.py
"""The socket connector against a fake socket relay.

``websockets.serve`` on a loopback port plays ``relay-ws/server.js`` (one
coroutine pairing the ``mac`` and ``phone`` sides per channel, forwarding
text verbatim, the ``peer:`` control frames, newest-wins with ``4001`` and a
``refuse`` seam — `test_relay_client.py`'s pattern); the test plays the
phone with ``websockets.connect``, sealing and opening frames with
``relay.py``'s own helpers. The connector under test is the real one, wired
to a real ``ApiServer._remote_run`` and hung on ``on_picture`` exactly as the
daemon hangs it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from http import HTTPStatus
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio
import websockets
from websockets.exceptions import ConnectionClosed

from dark_army_daemon import access_log, devices, link_timing, paths, relay
from dark_army_daemon import api_server, relay_ws
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon

CH_SHAPE = re.compile(r"^[0-9a-f]{32}$")


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
    yield tmp_path / "relay.json"
    relay.reset()
    devices.invalidate()


class _FakeRelay:
    """The socket relay: per channel a `mac` and a `phone` side, text
    forwarded verbatim, `peer:1` / `peer:0` on attach and detach, newest
    wins with `4001`, and a `refuse` seam that turns every upgrade away
    with one HTTP status."""

    def __init__(self):
        self.channels: dict = defaultdict(lambda: {"mac": None, "phone": None})
        self.refuse = 0
        self.url = ""
        self.attaches: list = []

    def process_request(self, connection, request):
        if self.refuse:
            return connection.respond(HTTPStatus(self.refuse), "refused\n")
        return None

    async def handler(self, ws):
        parsed = urlparse(ws.request.path)
        qs = parse_qs(parsed.query)
        ch = (qs.get("ch") or [""])[0]
        side = (qs.get("side") or [""])[0]
        if parsed.path != "/ws" or side not in ("mac", "phone") \
                or not CH_SHAPE.match(ch):
            await ws.close(1008, "bad request")
            return
        entry = self.channels[ch]
        other = "phone" if side == "mac" else "mac"
        previous = entry[side]
        if previous is not None:
            await previous.close(4001, "replaced")
        entry[side] = ws
        self.attaches.append(side)
        peer = entry[other]
        try:
            if peer is not None:
                await peer.send("peer:1")
                await ws.send("peer:1")
            else:
                await ws.send("peer:0")
            async for message in ws:
                if isinstance(message, bytes) or message.startswith("peer:"):
                    continue
                peer = entry[other]
                if peer is not None:
                    try:
                        await peer.send(message)
                    except ConnectionClosed:
                        pass
        except ConnectionClosed:
            pass
        finally:
            if entry[side] is ws:
                entry[side] = None
                peer = entry[other]
                if peer is not None:
                    try:
                        await peer.send("peer:0")
                    except ConnectionClosed:
                        pass


@pytest_asyncio.fixture
async def fake():
    relay_ = _FakeRelay()
    server = await websockets.serve(
        relay_.handler, "127.0.0.1", 0,
        process_request=relay_.process_request)
    port = server.sockets[0].getsockname()[1]
    relay_.url = f"ws://127.0.0.1:{port}"
    yield relay_
    server.close()
    await server.wait_closed()


def _build_connector(url: str, device_id: str = "dev-1"):
    # Written straight into the store: `set_ws_url` refuses anything but
    # `wss://`, and loopback `ws://` is the connector's stated test seam.
    data = relay.load()
    data["ws_url"] = url
    assert relay.save(data)
    assert relay.create_channel(device_id)
    key = relay.channel_key(device_id)
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    conn = relay_ws.RelaySocketConnector(srv)
    srv.on_picture = conn.nudge
    return conn, srv, daemon, key


@pytest_asyncio.fixture
async def connector(fake, monkeypatch):
    monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
    conn, srv, daemon, key = _build_connector(fake.url)
    conn.start()
    try:
        yield conn, srv, daemon, key, fake
    finally:
        await conn.stop()


async def _until(predicate, timeout: float = 5.0, message: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(message or "condition never held")


class _Phone:
    """The test's phone: its own socket to the fake relay, sealing with the
    channel's key and opening every frame the Mac sends down."""

    def __init__(self, key: bytes, url: str):
        self.key = key
        self.chan = relay.channel_id(key)
        self.url = url
        self.ws = None
        self.send_ctr = 0
        self.recv_ctr = 0
        self.refused: list = []
        # Frames already opened but handed back for the next `receive`.
        self.held: list = []

    async def connect(self):
        self.ws = await websockets.connect(
            f"{self.url}/ws?ch={self.chan}&side=phone")
        return self

    async def close(self):
        if self.ws is not None:
            await self.ws.close()

    async def send(self, kind: str, body, ctr: int | None = None,
                   frame_id: str = "") -> str:
        if ctr is None:
            self.send_ctr += 1
            ctr = self.send_ctr
        frame_id = frame_id or f"req-{ctr}"
        wire = relay.seal_frame(self.key, relay.DIR_PHONE_TO_MAC, ctr, kind,
                                body, frame_id=frame_id)
        await self.ws.send(wire)
        return frame_id

    async def receive(self, timeout: float = 5.0):
        """The next frame that opens under the key, or None on timeout;
        `peer:` words are skipped and a frame that fails to open is
        remembered under `refused`."""
        if self.held:
            return self.held.pop(0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(
                    self.ws.recv(), timeout=max(0.01, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return None
            if isinstance(message, bytes) or message.startswith("peer:"):
                continue
            frame, err = relay.open_frame(self.key, relay.DIR_MAC_TO_PHONE,
                                          message, self.recv_ctr)
            if err:
                self.refused.append(err)
                continue
            self.recv_ctr = int(frame["ctr"])
            return frame
        return None


def _picture(n: int) -> dict:
    return {"running": [{"session_id": f"s{n}", "name": f"S{n}",
                         "state": "working"}],
            "sleeping": [], "waiting": [], "abandoned": [], "finished": []}


async def _armed_phone(conn, srv, key, fake, kind: str = "usage",
                       device_id: str = "dev-1",
                       take_owed_push: bool = False) -> _Phone:
    """A phone connected and armed by one verified request of `kind`.

    Arming broadcasts, so a `usage`-armed phone is owed one push,
    `WS_PUSH_MIN_INTERVAL` later. Unloaded it lands after the reply; under
    load (the suite across workers) it can overtake the reply, and is then
    handed back for the test's next `receive`. `take_owed_push` takes it
    here, for a test whose next frame must be a reply or must be nothing."""
    await _until(lambda: conn.socket_word(device_id) == relay_ws.SOCKET_OPEN,
                 message="the Mac never opened its line")
    phone = await _Phone(key, fake.url).connect()
    await _until(lambda: fake.channels[phone.chan]["phone"] is not None)
    await phone.send(kind, {})
    reply = await phone.receive()
    owed = None
    if reply is not None and reply["kind"] == "push":
        owed, reply = reply, await phone.receive()
    assert reply is not None and reply["kind"] == "reply"
    assert reply["body"]["status"] == 200
    assert conn.socket_word(device_id) == relay_ws.SOCKET_ARMED
    if take_owed_push:
        owed = owed or await phone.receive()
        assert owed is not None and owed["kind"] == "push"
    elif owed is not None:
        phone.held.append(owed)
    return phone


# --- the request path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_state_request_over_the_socket_is_answered_sealed_with_timing(connector):
    conn, srv, _daemon, key, fake = connector
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
    phone = await _Phone(key, fake.url).connect()
    frame_id = await phone.send("state", {})
    reply = await phone.receive()
    assert reply is not None
    assert reply["kind"] == "reply"
    envelope = reply["body"]
    assert envelope["re"] == frame_id
    assert envelope["status"] == 200
    assert set(envelope) == {"re", "status", "content_type", "body", "timing"}
    assert set(envelope["timing"]) == {"open", "run"}
    for leg in envelope["timing"].values():
        assert isinstance(leg, float) and leg >= 0.0
    payload = json.loads(envelope["body"])
    assert "agents" in payload and "state_digest" in payload
    assert "timing" not in payload
    await phone.close()


@pytest.mark.asyncio
async def test_an_unverified_socket_gets_no_push_while_broadcast_fires(connector):
    """A stranger holding the channel id connects and gets nothing: two
    broadcasts, two changed pictures, not one byte down an unarmed line."""
    conn, srv, _daemon, key, fake = connector
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
    stranger = await _Phone(key, fake.url).connect()
    await _until(lambda: fake.channels[stranger.chan]["phone"] is not None)
    srv.on_agents_change(_picture(1))
    await asyncio.sleep(0.2)
    srv.on_agents_change(_picture(2))
    srv._broadcast()
    assert await stranger.receive(timeout=0.6) is None
    assert stranger.refused == []
    assert conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN
    await stranger.close()


@pytest.mark.asyncio
async def test_the_first_verified_frame_arms_the_socket_and_the_next_broadcast_pushes(connector):
    conn, srv, _daemon, key, fake = connector
    phone = await _armed_phone(conn, srv, key, fake, kind="usage")
    srv._broadcast()
    push = await phone.receive()
    assert push is not None and push["kind"] == "push"
    body = push["body"]
    assert set(body) == {"status", "content_type", "body"}
    assert body["status"] == 200
    picture = json.loads(body["body"])
    assert "agents" in picture
    assert picture["state_digest"]
    assert conn._pushed_digest["dev-1"] == picture["state_digest"]
    await phone.close()


@pytest.mark.asyncio
async def test_a_displaced_phone_gets_no_push_until_it_re_arms(connector):
    """`peer:` frames only narrow, never arm. A phone that armed the line
    and is then displaced by a second `side=phone` socket (newest wins,
    the Mac hears `peer:1`) — or that simply leaves (`peer:0`) — is not
    who the Mac verified: the arming goes, two changed pictures push not
    a byte, and only a fresh verified request re-arms the line."""
    conn, srv, _daemon, key, fake = connector
    # The arming push is taken first: under load it could still be in
    # flight when the second socket takes the line and land on it — a frame
    # sent while the line was armed, not after the displacement.
    first = await _armed_phone(conn, srv, key, fake, kind="usage",
                               take_owed_push=True)
    second = _Phone(key, fake.url)
    # The same phone reconnecting: its persisted counters come with it.
    second.send_ctr, second.recv_ctr = first.send_ctr, first.recv_ctr
    await second.connect()
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN,
                 message="the displacing socket did not narrow the arming")
    assert "dev-1" not in conn._armed
    srv.on_agents_change(_picture(1))
    await asyncio.sleep(0.2)
    srv.on_agents_change(_picture(2))
    srv._broadcast()
    assert await second.receive(timeout=0.6) is None
    assert second.refused == []
    assert conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN
    # A fresh verified `state` request re-arms, and the next change pushes.
    await second.send("state", {"done": "review", "with_usage": True})
    reply = await second.receive()
    assert reply is not None and reply["kind"] == "reply"
    assert reply["body"]["status"] == 200
    assert conn.socket_word("dev-1") == relay_ws.SOCKET_ARMED
    srv.on_agents_change(_picture(3))
    srv._broadcast()
    push = await second.receive()
    assert push is not None and push["kind"] == "push"
    # Leaving narrows too: `peer:0` disarms, and the word says so.
    await second.close()
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN,
                 message="the departing phone left the line armed")
    await first.close()


@pytest.mark.asyncio
async def test_a_broadcast_with_nothing_changed_pushes_nothing(connector):
    conn, srv, _daemon, key, fake = connector
    phone = await _armed_phone(conn, srv, key, fake, kind="usage")
    srv._broadcast()
    assert (await phone.receive())["kind"] == "push"
    srv._broadcast()
    srv._broadcast()
    assert await phone.receive(timeout=0.5) is None
    await phone.close()


@pytest.mark.asyncio
async def test_a_changed_picture_after_a_push_delivers_exactly_one_more(connector):
    conn, srv, _daemon, key, fake = connector
    phone = await _armed_phone(conn, srv, key, fake, kind="usage")
    srv._broadcast()
    first = await phone.receive()
    assert first["kind"] == "push"
    digest = json.loads(first["body"]["body"])["state_digest"]
    srv.on_agents_change(_picture(7))
    second = await phone.receive()
    assert second is not None and second["kind"] == "push"
    assert json.loads(second["body"]["body"])["state_digest"] != digest
    assert await phone.receive(timeout=0.5) is None
    await phone.close()


@pytest.mark.asyncio
async def test_a_phone_state_answer_sets_the_push_digest(connector):
    """The phone's own `state` answer down the line is what the next push
    compares against: a broadcast right after it has nothing new to say."""
    conn, srv, _daemon, key, fake = connector
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
    phone = await _Phone(key, fake.url).connect()
    await _until(lambda: fake.channels[phone.chan]["phone"] is not None)
    # The phone's check-in body: the review-only board, the push's picture.
    await phone.send("state", {"done": "review", "with_usage": True})
    reply = await phone.receive()
    assert reply["kind"] == "reply" and reply["body"]["status"] == 200
    digest = json.loads(reply["body"]["body"])["state_digest"]
    assert conn._pushed_digest.get("dev-1") == digest
    srv._broadcast()
    assert await phone.receive(timeout=0.5) is None
    # And a real change after it is pushed once.
    srv.on_agents_change(_picture(3))
    push = await phone.receive()
    assert push is not None and push["kind"] == "push"
    assert json.loads(push["body"]["body"])["state_digest"] != digest
    await phone.close()


@pytest.mark.asyncio
async def test_pushes_coalesce_inside_the_min_interval(connector):
    conn, srv, _daemon, key, fake = connector
    phone = await _armed_phone(conn, srv, key, fake, kind="usage")
    for _ in range(5):
        srv._broadcast()
    assert conn._push_handle is not None
    first = await phone.receive()
    assert first is not None and first["kind"] == "push"
    assert await phone.receive(timeout=0.5) is None
    await phone.close()


@pytest.mark.asyncio
async def test_one_push_builds_the_picture_once_for_every_armed_phone(connector,
                                                                     monkeypatch):
    """Two armed phones, one push: the picture and its digests are built
    once, both phones are answered off it, and the answer is the one the
    phone's own `state` read would fingerprint. A second push over an
    unchanged picture builds once and calls `_remote_run` for neither —
    both are skipped before their bucket is taken."""
    conn, srv, _daemon, _key, _fake = connector
    await conn.stop()                  # the lines below are the test's own
    assert relay.create_channel("dev-2")
    conn._sockets = {"dev-1": object(), "dev-2": object()}
    conn._armed = {"dev-1", "dev-2"}
    conn._pushed_digest = {}
    real_state = srv.state
    builds: list = []
    monkeypatch.setattr(srv, "state",
                        lambda **k: builds.append(k) or real_state(**k))
    real_run = srv._remote_run
    runs: list = []

    async def run(kind, payload, device_id, *, prebuilt=None):
        runs.append((device_id, prebuilt is not None))
        return await real_run(kind, payload, device_id, prebuilt=prebuilt)

    monkeypatch.setattr(srv, "_remote_run", run)
    sent: list = []

    async def answer(device_id, key, body, *, kind):
        sent.append((device_id, kind, json.loads(body["body"])))
        return True

    monkeypatch.setattr(conn, "_answer", answer)
    monkeypatch.setattr(conn, "_note_timing", lambda *a, **k: None)

    await conn._push_all()
    assert builds == [{"done_review": True}], "one picture per push"
    assert sorted(runs) == [("dev-1", True), ("dev-2", True)]
    assert sorted(d for d, _, _ in sent) == ["dev-1", "dev-2"]
    whole, sections = api_server._state_digests(real_state(done_review=True))
    for _device, kind, body in sent:
        assert kind == "push"
        assert body["state_digest"] == whole
        assert body["section_digests"] == sections
        assert "agents" in body and "board" in body
    assert conn._pushed_digest == {"dev-1": whole, "dev-2": whole}

    builds.clear(), runs.clear(), sent.clear()
    await conn._push_all()
    assert builds == [{"done_review": True}]
    assert runs == [] and sent == [], "an unchanged picture runs nothing"


@pytest.mark.asyncio
async def test_the_push_bucket_bounds_a_flood(fake, monkeypatch):
    monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.02)
    monkeypatch.setattr(relay_ws, "WS_PUSH_MAX_PER_MINUTE", 2)
    conn, srv, _daemon, key = _build_connector(fake.url)
    conn.start()
    try:
        phone = await _armed_phone(conn, srv, key, fake, kind="usage")
        for n in range(6):
            srv.on_agents_change(_picture(n))
            await asyncio.sleep(0.1)
        pushes = 0
        while (frame := await phone.receive(timeout=0.4)) is not None:
            assert frame["kind"] == "push"
            pushes += 1
        assert pushes == 2
        await phone.close()
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_an_action_on_remote_actions_runs_and_is_recorded(connector, monkeypatch):
    conn, srv, daemon, key, fake = connector
    calls = []

    async def fake_lan_run(action, payload, device_id=""):
        calls.append((action, payload.get("session_id"), device_id))
        return 200, "application/json", b'{"ok": true}'

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    phone = await _armed_phone(conn, srv, key, fake, kind="usage",
                               take_owed_push=True)
    frame_id = await phone.send("action", {"action": "dismiss",
                                           "session_id": "s1"})
    reply = await phone.receive()
    assert reply["kind"] == "reply" and reply["body"]["re"] == frame_id
    assert reply["body"]["status"] == 200
    assert calls == [("dismiss", "s1", "dev-1")]
    assert [(r["device_id"], r["action"], r["ok"])
            for r in daemon._remote_activity] == [("dev-1", "dismiss", True)]
    await phone.close()


@pytest.mark.asyncio
async def test_an_unchosen_action_is_a_sealed_404(connector, monkeypatch):
    conn, srv, _daemon, key, fake = connector
    calls = []

    async def fake_lan_run(action, payload, device_id=""):
        calls.append(action)
        return 200, "application/json", b'{"ok": true}'

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    phone = await _armed_phone(conn, srv, key, fake, kind="usage",
                               take_owed_push=True)
    assert "set_relay_ws" not in ApiServer.REMOTE_ACTIONS
    await phone.send("action", {"action": "set_relay_ws", "url": "wss://x"})
    reply = await phone.receive()
    assert reply["kind"] == "reply"
    assert reply["body"]["status"] == 404
    assert calls == []
    assert relay.get_ws_url() == fake.url
    await phone.close()


@pytest.mark.asyncio
async def test_a_lapsed_lease_refuses_in_the_lease_words_and_runs_nothing(
        connector, monkeypatch, store):
    conn, srv, _daemon, key, fake = connector
    calls = []

    async def fake_lan_run(action, payload, device_id=""):
        calls.append(action)
        return 200, "application/json", b'{"ok": true}'

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    phone = await _armed_phone(conn, srv, key, fake, kind="usage",
                               take_owed_push=True)
    data = json.loads(store.read_text())
    data["channels"]["dev-1"]["lease_expires_at"] = time.time() - 60
    store.write_text(json.dumps(data))
    relay.invalidate()
    await phone.send("action", {"action": "dismiss", "session_id": "s1"})
    reply = await phone.receive()
    assert reply["kind"] == "reply"
    assert reply["body"]["status"] == 403
    assert json.loads(reply["body"]["body"])["error"] == relay.LEASE_REFUSAL
    assert calls == []
    # Reads keep working past expiry.
    await phone.send("state", {})
    assert (await phone.receive())["body"]["status"] == 200
    await phone.close()


@pytest.mark.asyncio
async def test_a_replayed_frame_is_dropped_and_answered_with_ctr_expected(connector):
    conn, srv, _daemon, key, fake = connector
    phone = await _armed_phone(conn, srv, key, fake, kind="state")
    await phone.send("state", {}, ctr=1)  # the counter already accepted
    err = await phone.receive()
    assert err is not None and err["kind"] == "err"
    assert err["body"]["status"] == 409
    assert err["body"]["ctr_expected"] == 2
    assert err["body"]["re"] == ""
    assert conn.dropped == {"ctr": 1}
    await phone.close()


@pytest.mark.asyncio
async def test_a_replayed_command_token_is_answered_from_the_receipt_ledger(
        connector, monkeypatch):
    conn, srv, _daemon, key, fake = connector
    calls = []

    async def fake_lan_run(action, payload, device_id=""):
        calls.append(action)
        return 200, "application/json", b'{"ok": true, "n": 1}'

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    phone = await _armed_phone(conn, srv, key, fake, kind="usage",
                               take_owed_push=True)
    press = {"action": "dismiss", "session_id": "s1",
             "command_token": "press-0001-abcdef"}
    await phone.send("action", press)
    first = await phone.receive()
    await phone.send("action", press)
    second = await phone.receive()
    assert first["body"]["status"] == second["body"]["status"] == 200
    assert first["body"]["body"] == second["body"]["body"]
    assert calls == ["dismiss"]
    await phone.close()


# --- the ladder ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reconnect_ladder_grows_and_caps_against_a_refusing_relay(
        fake, monkeypatch, caplog):
    """A relay refusing the upgrade is retried on a doubling ladder that
    caps at `WS_RECONNECT_MAX`, never a hot loop, and the transition is one
    warning line; a reminder only on the heartbeat."""
    waits: list = []

    async def counting_sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0.005)

    monkeypatch.setattr(relay_ws, "_sleep", counting_sleep)
    monkeypatch.setattr(relay_ws, "HEALTH_REPORT_SECONDS", 600.0)
    fake.refuse = 503
    conn, _srv, _daemon, _key = _build_connector(fake.url)
    caplog.set_level("INFO", logger="dark-army")
    conn.start()
    try:
        await _until(lambda: len(waits) >= 9, timeout=10.0,
                     message=f"only {len(waits)} attempts")
        health = conn.health_snapshot()
    finally:
        await conn.stop()
    assert health["state"] == "failing"
    assert health["status"] == 503
    assert health["failures"] >= 9
    assert conn.health_snapshot() == {}  # reset with stop, so it can say so again
    assert waits[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert all(w == relay_ws.WS_RECONNECT_MAX for w in waits[6:])
    failing = [r for r in caplog.records
               if "relay socket failing" in r.getMessage()]
    assert len(failing) == 1, [r.getMessage() for r in caplog.records]
    assert "503" in failing[0].getMessage()
    assert failing[0].levelname == "WARNING"
    assert "mailbox carries the phone" in failing[0].getMessage()
    assert not [r for r in caplog.records if "still failing" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_replaced_close_waits_the_maximum(fake, monkeypatch):
    waits: list = []

    async def counting_sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0.005)

    monkeypatch.setattr(relay_ws, "_sleep", counting_sleep)
    conn, _srv, _daemon, key = _build_connector(fake.url)
    conn.start()
    try:
        await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
        chan = relay.channel_id(key)
        # Another Mac takes the channel: the relay closes ours with 4001.
        await fake.channels[chan]["mac"].close(4001, "replaced")
        await _until(lambda: len(waits) >= 1)
        assert waits[0] == relay_ws.WS_RECONNECT_MAX
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_a_relay_that_accepts_and_drops_at_once_climbs_the_ladder(
        fake, monkeypatch):
    """Accepted then closed inside `WS_STABLE_SECONDS` is a failure for the
    ladder; the backoff resets only after a stable spell."""
    waits: list = []

    async def counting_sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0.005)

    monkeypatch.setattr(relay_ws, "_sleep", counting_sleep)
    conn, _srv, _daemon, key = _build_connector(fake.url)
    chan = relay.channel_id(key)
    conn.start()
    seen: list = []
    try:
        for i in range(3):
            await _until(lambda: fake.channels[chan]["mac"] is not None
                         and fake.channels[chan]["mac"] not in seen)
            ws = fake.channels[chan]["mac"]
            seen.append(ws)
            await ws.close(1000, "bye")
            await _until(lambda: len(waits) >= i + 1)
        assert waits[:3] == [1.0, 2.0, 4.0]
    finally:
        await conn.stop()


def test_a_ws_address_that_is_not_loopback_is_refused_and_wss_accepted(store):
    assert relay_ws._ws_url_ok("wss://relay.example/") is True
    assert relay_ws._ws_url_ok("ws://127.0.0.1:8080") is True
    assert relay_ws._ws_url_ok("ws://localhost:8080") is True
    assert relay_ws._ws_url_ok("ws://relay.example") is False
    assert relay_ws._ws_url_ok("https://relay.example") is False
    ok, detail = relay.set_ws_url("ws://relay.example")
    assert ok is False and detail == relay.WS_URL_REFUSAL
    assert detail == "the socket address must start with wss://"
    ok, detail = relay.set_ws_url("https://relay.example")
    assert ok is False and detail == relay.WS_URL_REFUSAL
    assert relay.get_ws_url() == ""
    ok, _ = relay.set_ws_url("wss://relay.example/")
    assert ok is True
    assert relay.get_ws_url() == "wss://relay.example"
    # Clearing works, and the mailbox address is untouched either way.
    relay.set_url("https://mail.example")
    assert relay.set_ws_url("") == (True, "")
    assert relay.get_ws_url() == ""
    assert relay.get_url() == "https://mail.example"
    assert "ws_url" in json.loads(store.read_text())


# --- the snapshot, the words and the timing -------------------------------------


@pytest.mark.asyncio
async def test_the_snapshot_carries_socket_health_and_the_word_and_no_secret(
        fake, monkeypatch):
    monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
    # A real paired device, so the row the word rides exists.
    code = devices.begin(allow_plain=True)
    token, device_id, detail = devices.redeem(code, "phone")
    assert token, detail
    conn, srv, daemon, key = _build_connector(fake.url, device_id)
    conn.start()
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    daemon._relay_socket = conn
    phone = await _armed_phone(conn, srv, key, fake, kind="usage",
                               device_id=device_id)
    snap = daemon.devices_snapshot()
    assert snap["relay_ws_enabled"] is True
    assert snap["relay_ws_url"] == fake.url
    assert snap["relay_ws_health"]["state"] == "ok"
    assert snap["relay_ws_health"]["last_ok_at"] > 0
    assert set(snap["relay_ws_health"]) == {"state", "status", "failures",
                                            "failing_for", "last_ok_at"}
    row = next(r for r in snap["devices"] if r["id"] == device_id)
    assert row["socket"] == "armed"
    assert row["socket"] in relay_ws.SOCKET_WORDS
    stored = relay.load()["channels"][device_id]
    blob = json.dumps(snap)
    assert stored["key_b64"] not in blob
    assert relay.channel_id(key) not in blob
    assert not re.search(r"[0-9a-f]{32}", blob), blob
    assert "recv_ctr" not in blob and "send_ctr" not in blob
    assert "state_digest" not in blob
    # Both switches off: the standing and the word go quiet, the address stays.
    daemon.relay_ws_enabled = False
    quiet = daemon.devices_snapshot()
    assert "relay_ws_health" not in quiet
    assert quiet["relay_ws_enabled"] is False
    assert next(r for r in quiet["devices"]
                if r["id"] == device_id)["socket"] == "off"
    await phone.close()
    await conn.stop()


@pytest.mark.asyncio
async def test_the_socket_word_follows_the_line(fake, monkeypatch):
    conn, _srv, _daemon, key = _build_connector(fake.url)
    assert conn.socket_word("dev-1") == "off"
    fake.refuse = 503
    monkeypatch.setattr(relay_ws, "_sleep", lambda s: asyncio.sleep(0.05))
    conn.start()
    try:
        await _until(lambda: conn.socket_word("dev-1") == "connecting")
        fake.refuse = 0
        await _until(lambda: conn.socket_word("dev-1") == "open")
        phone = await _Phone(key, fake.url).connect()
        await phone.send("usage", {})
        assert (await phone.receive()) is not None
        assert conn.socket_word("dev-1") == "armed"
        await phone.close()
        # The phone leaving does not disarm the Mac's line by itself — the
        # arming is per socket, cleared when *this* socket closes.
        chan = relay.channel_id(key)
        await fake.channels[chan]["mac"].close(1000, "bye")
        await _until(lambda: conn.socket_word("dev-1") != "armed")
    finally:
        await conn.stop()
    assert conn.socket_word("dev-1") == "off"


@pytest.mark.asyncio
async def test_socket_frames_do_not_stamp_the_mailboxs_liveness(connector):
    """The mailbox connector keeps its own pacing: a frame down the socket
    is not `relay.last_frame_at`'s to notice, so the mailbox idles down its
    ladder while the phone lives on the socket."""
    conn, srv, _daemon, key, fake = connector
    before = relay.last_frame_at("dev-1")
    phone = await _armed_phone(conn, srv, key, fake, kind="state")
    await phone.send("state", {})
    assert (await phone.receive()) is not None
    assert relay.last_frame_at("dev-1") == before
    await phone.close()


@pytest.mark.asyncio
async def test_a_socket_trip_is_timed_on_the_ws_door(fake, monkeypatch, tmp_path):
    """The success criterion's Mac half: a trip down the socket files a
    `timing` record under door `ws`, the ten-minute line lands on the
    access log beside the relay's, and its sentence begins `socket:`."""
    monkeypatch.setattr(link_timing, "ROLLUP_SECONDS", 0.2)
    monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
    conn, srv, daemon, key = _build_connector(fake.url)
    log = access_log.AccessLog(path=tmp_path / "access.jsonl")
    log.open()
    daemon._access_log = log
    noted = []
    monkeypatch.setattr(daemon._burst_detector, "note",
                        lambda *a, **k: noted.append((a, k)))
    conn.start()
    try:
        phone = await _armed_phone(conn, srv, key, fake, kind="usage")
        srv._broadcast()  # one push, timed under kind `push`
        assert (await phone.receive())["kind"] == "push"
        await asyncio.sleep(0.3)
        await phone.send("state", {})
        assert (await phone.receive()) is not None
        await phone.send("state", {})
        assert (await phone.receive()) is not None
        rows = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = [r for r in log.recent(include_timing=True)
                    if r["kind"] == "timing"]
            if rows:
                break
            await asyncio.sleep(0.05)
        await phone.close()
    finally:
        await conn.stop()
    assert rows, log.recent(include_timing=True)
    line = rows[-1]
    assert line["door"] == "ws"
    assert line["device_id"] == "dev-1"
    assert line["count"] >= 2
    assert tuple(line) == access_log.PUBLISHED_KEYS
    assert set(line["hops"]) == set(access_log.HOP_KEYS)
    assert line["hops"]["n"] >= 2
    assert line["hops"]["size_max"] > 0
    # No mailbox on this door: dwell and hold read zero.
    assert line["hops"]["dwell_max"] == 0.0 and line["hops"]["hold_max"] == 0.0
    assert line["text"].startswith("socket: ")
    assert " requests in " in line["text"]
    assert "opened in" in line["text"] and "answered in" in line["text"]
    assert access_log.sentence("timing", door="ws", count=3, window_seconds=600,
                               hops=line["hops"]).startswith("socket: 3 requests")
    assert noted == []
    assert log.open_alerts() == []
    blob = json.dumps(line)
    assert relay.load()["channels"]["dev-1"]["key_b64"] not in blob
    assert relay.channel_id(key) not in blob
    assert fake.url not in blob


@pytest.mark.asyncio
async def test_a_refused_frame_lands_on_the_access_log_under_the_ws_door(
        connector, tmp_path):
    conn, srv, daemon, key, fake = connector
    log = access_log.AccessLog(path=tmp_path / "access.jsonl")
    log.open()
    daemon._access_log = log
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
    phone = await _Phone(key, fake.url).connect()
    await _until(lambda: fake.channels[phone.chan]["phone"] is not None)
    await phone.ws.send("not an envelope")
    await _until(lambda: conn.dropped.get("seal") == 1, timeout=3.0)
    await _until(lambda: any(r["door"] == "ws" for r in log.recent()), timeout=3.0)
    row = next(r for r in log.recent() if r["door"] == "ws")
    assert row["kind"] == "refusal" and row["reason"] == "seal"
    assert row["text"].startswith("the relay socket refused dev-1")
    # The garbage armed nothing; the line still answers a real frame.
    assert conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN
    await phone.send("state", {})
    assert (await phone.receive())["body"]["status"] == 200
    await phone.close()


# --- lifecycle --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_every_task_and_closes_the_socket(fake, monkeypatch):
    monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
    conn, srv, _daemon, key = _build_connector(fake.url)
    conn.start()
    phone = await _armed_phone(conn, srv, key, fake, kind="usage")
    chan = relay.channel_id(key)
    srv._broadcast()
    assert conn._push_handle is not None
    await conn.stop()
    assert conn._tasks == {} and conn._handlers == set()
    assert conn._supervisor is None and conn._push_handle is None
    assert conn._sockets == {} and conn._armed == set()
    assert conn.socket_word("dev-1") == "off"
    assert conn.health_snapshot() == {}
    await _until(lambda: fake.channels[chan]["mac"] is None,
                 message="the Mac's line stayed open after stop()")
    # The phone hears the departure and nothing more is pushed.
    message = await asyncio.wait_for(phone.ws.recv(), timeout=2.0)
    while message != "peer:0":
        message = await asyncio.wait_for(phone.ws.recv(), timeout=2.0)
    assert message == "peer:0"
    await phone.close()


@pytest.mark.asyncio
async def test_a_start_racing_a_stop_leaves_the_connector_armed_capable(
        fake, monkeypatch):
    """A quick off→on: `start()` lands while `stop()` is still awaiting
    the cancelled tasks (the daemon schedules both onto the loop in turn).
    The start is deferred until the stop returns rather than interleaved
    with it, so the restarted connector's fresh line is never wiped from
    the containers — a verified frame arms it and a push arrives."""
    monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
    conn, srv, _daemon, key = _build_connector(fake.url)
    conn.start()
    phone = await _armed_phone(conn, srv, key, fake, kind="usage",
                               take_owed_push=True)
    stopping = asyncio.ensure_future(conn.stop())
    await asyncio.sleep(0)  # the stop has begun and awaits the old tasks
    assert conn._stopping is not None and not conn._running
    conn.start()            # no yield between: the quick on
    assert not conn._running and conn._restart_wanted
    await stopping
    try:
        assert conn._running and conn._supervisor is not None
        assert conn._stopping is None and not conn._restart_wanted
        await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN,
                     message="the restarted connector never opened its line")
        # The phone's socket outlived the Mac's; it re-arms the new line.
        await phone.send("state", {"done": "review", "with_usage": True})
        reply = await phone.receive()
        assert reply is not None and reply["kind"] == "reply"
        assert conn.socket_word("dev-1") == relay_ws.SOCKET_ARMED
        srv.on_agents_change(_picture(7))
        srv._broadcast()
        push = await phone.receive()
        assert push is not None and push["kind"] == "push"
        # Two stops in a row: the second waits for the first, and a start
        # between them is cancelled by the second.
        first = asyncio.ensure_future(conn.stop())
        await asyncio.sleep(0)
        conn.start()
        second = asyncio.ensure_future(conn.stop())
        await asyncio.gather(first, second)
        assert not conn._running and not conn._restart_wanted
        assert conn._sockets == {} and conn._stopping is None
    finally:
        await phone.close()
        await conn.stop()


@pytest.mark.asyncio
async def test_no_socket_address_is_a_standing_of_its_own_with_one_warning(
        fake, monkeypatch, caplog):
    """Socket link on with no address stored publishes a standing rather
    than nothing: `state` `no-address`, `relay_health`'s five key names,
    one warning line on the transition and none per retry; the first
    address to arrive ends the spell."""
    waits: list = []

    async def counting_sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0.005)

    monkeypatch.setattr(relay_ws, "_sleep", counting_sleep)
    conn, _srv, daemon, key = _build_connector("")
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    daemon._relay_socket = conn
    caplog.set_level("INFO", logger="dark-army")
    conn.start()
    try:
        await _until(lambda: len(waits) >= 4, message="no retries")
        assert all(w == relay_ws.WS_URL_RETRY_SECONDS for w in waits)
        health = conn.health_snapshot()
        assert health["state"] == relay_ws.HEALTH_NO_ADDRESS
        assert health["status"] == 0 and health["failures"] == 0
        assert health["failing_for"] >= 0.0
        assert set(health) == {"state", "status", "failures", "failing_for",
                               "last_ok_at"}
        assert conn.socket_word("dev-1") == relay_ws.SOCKET_OFF
        snap = daemon.devices_snapshot()
        assert snap["relay_ws_health"]["state"] == relay_ws.HEALTH_NO_ADDRESS
        assert snap["relay_ws_url"] == ""
        assert not re.search(r"[0-9a-f]{32}", json.dumps(snap))
        warned = [r for r in caplog.records
                  if "no socket address set" in r.getMessage()]
        assert len(warned) == 1, [r.getMessage() for r in caplog.records]
        assert warned[0].levelname == "WARNING"
        assert "mailbox carries the phone" in warned[0].getMessage()
        assert not [r for r in caplog.records
                    if "relay socket failing" in r.getMessage()]
        # An address arriving ends the spell: the line opens, the standing
        # is ok, and the next spell without one warns once more.
        data = relay.load()
        data["ws_url"] = fake.url
        assert relay.save(data)
        await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
        assert conn.health_snapshot()["state"] == "ok"
        assert relay.channel_id(key) not in json.dumps(daemon.devices_snapshot())
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_an_address_arriving_to_a_down_relay_says_failing_at_once(
        fake, monkeypatch, caplog):
    """The spell without an address ends when one arrives — and if the
    relay behind it is down, the standing is `failing` with the status
    and one WARNING at once, never `no-address` with status 0 until the
    reminder: `_note_failure` resets the record so the ok→failing
    transition happens."""
    waits: list = []

    async def counting_sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0.005)

    monkeypatch.setattr(relay_ws, "_sleep", counting_sleep)
    monkeypatch.setattr(relay_ws, "HEALTH_REPORT_SECONDS", 600.0)
    conn, _srv, daemon, key = _build_connector("")
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    daemon._relay_socket = conn
    caplog.set_level("INFO", logger="dark-army")
    conn.start()
    try:
        await _until(lambda: len(waits) >= 2, message="no retries")
        assert conn.health_snapshot()["state"] == relay_ws.HEALTH_NO_ADDRESS
        fake.refuse = 503
        data = relay.load()
        data["ws_url"] = fake.url
        assert relay.save(data)
        await _until(lambda: conn.health_snapshot().get("failures", 0) >= 3,
                     message="the relay was never tried")
        health = conn.health_snapshot()
        assert health["state"] == "failing"
        assert health["status"] == 503
        assert health["failing_for"] >= 0.0
        snap = daemon.devices_snapshot()
        assert snap["relay_ws_health"]["state"] == "failing"
        assert snap["relay_ws_health"]["status"] == 503
        assert not re.search(r"[0-9a-f]{32}", json.dumps(snap))
        failing = [r for r in caplog.records
                   if "relay socket failing" in r.getMessage()]
        assert len(failing) == 1, [r.getMessage() for r in caplog.records]
        assert failing[0].levelname == "WARNING"
        assert "503" in failing[0].getMessage()
        assert not [r for r in caplog.records
                    if "still failing" in r.getMessage()]
        assert not [r for r in caplog.records
                    if "recovered" in r.getMessage()]
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_a_refused_stored_address_is_one_warning(
        fake, monkeypatch, caplog):
    """A stored address the connector refuses (only reachable by editing
    `relay.json` by hand) is `bad-address` and exactly **one** WARNING for
    the whole spell — the refusal's reason folded into that line, never a
    second line beside it, and none per retry."""
    waits: list = []

    async def counting_sleep(seconds):
        waits.append(seconds)
        await asyncio.sleep(0.005)

    monkeypatch.setattr(relay_ws, "_sleep", counting_sleep)
    conn, _srv, daemon, _key = _build_connector("https://relay.example")
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    daemon._relay_socket = conn
    caplog.set_level("INFO", logger="dark-army")
    conn.start()
    try:
        await _until(lambda: len(waits) >= 4, message="no retries")
        assert all(w == relay_ws.WS_URL_RETRY_SECONDS for w in waits)
        health = conn.health_snapshot()
        assert health["state"] == relay_ws.HEALTH_BAD_ADDRESS
        assert health["status"] == 0 and health["failures"] == 0
        assert daemon.devices_snapshot()["relay_ws_health"]["state"] == \
            relay_ws.HEALTH_BAD_ADDRESS
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
        assert "must start with wss://" in warnings[0].getMessage()
        assert "relay socket" in warnings[0].getMessage()
        assert "mailbox carries the phone" in warnings[0].getMessage()
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_a_quick_away_off_then_on_leaves_both_connectors_running(
        fake, monkeypatch):
    """`set_remote_access(False)`, then `(True)` while the off-apply is
    parked at its first await (the socket's stop) — the AppKit thread's
    quick off→on. The on-apply starts the mailbox connector meanwhile; the
    latest stash wins, so the resumed off-apply never stops what the
    on-apply started. End state: away on, mailbox connector running,
    socket lane open. (Two stashes with no yield at all are both read as
    "on" when the applies run, so that order never raced.)"""
    monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
    conn, srv, daemon, _key = _build_connector(fake.url)
    conn.start()
    daemon._loop = asyncio.get_running_loop()
    daemon._relay_socket = conn
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
    try:
        daemon.set_remote_access(False)
        await asyncio.sleep(0)           # the off-apply reaches its await
        assert conn._stopping is not None, "the off-apply is not parked"
        daemon.set_remote_access(True)
        await _until(lambda: conn._running)
        await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
        await asyncio.sleep(0.1)         # the resumed off-apply has run
        assert daemon.remote_access_enabled is True
        assert daemon._relay_connector is not None
        assert daemon._relay_connector._running is True
        assert conn._running is True
        assert srv.on_picture == conn.nudge
        assert conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN
    finally:
        await daemon._stop_relay_connector()
        await conn.stop()


@pytest.mark.asyncio
async def test_set_remote_access_off_stops_the_socket_connector(fake, monkeypatch):
    monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
    conn, srv, daemon, key = _build_connector(fake.url)
    conn.start()
    daemon._loop = asyncio.get_running_loop()
    daemon._relay_socket = conn
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
    daemon.set_remote_access(False)
    await _until(lambda: not conn._running)
    await asyncio.sleep(0.1)
    assert srv.on_picture is None
    assert conn._tasks == {}
    # Away back on with the socket switch still on restarts the lane.
    daemon.set_remote_access(True)
    await _until(lambda: conn._running)
    assert srv.on_picture == conn.nudge
    await _until(lambda: conn.socket_word("dev-1") == relay_ws.SOCKET_OPEN)
    # The socket switch alone stops it too, and the mailbox is untouched.
    daemon.set_relay_ws(False)
    await _until(lambda: not conn._running)
    assert daemon.remote_access_enabled is True
    await daemon._stop_relay_connector()


def test_the_socket_is_started_only_while_both_switches_are_on():
    daemon = BobDaemon()
    started = []
    daemon._start_relay_socket = lambda: started.append("start")
    stopped = []

    async def _stop():
        stopped.append("stop")

    daemon._stop_relay_socket = _stop
    daemon._start_relay_connector = lambda: None
    loop = asyncio.new_event_loop()
    try:
        daemon._loop = loop
        daemon._api = type("Api", (), {"_broadcast": lambda self: None})()

        async def drive():
            daemon.set_relay_ws(True)          # away off: nothing starts
            await asyncio.sleep(0)
            assert started == [] and stopped == ["stop"]
            daemon.set_remote_access(True)     # both on: starts
            await asyncio.sleep(0)
            assert started == ["start"]
            daemon.set_relay_ws(False)         # socket off: stops
            await asyncio.sleep(0)
            assert stopped == ["stop", "stop"]

        loop.run_until_complete(drive())
    finally:
        loop.close()
