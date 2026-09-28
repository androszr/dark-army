# host/tests/test_pair_bot.py
"""A headless device on its own relay channel.

``pair_bot`` is loopback only. It does not spend the QR, it does not ride
either phone door, and the pair reply is the one place the keys leave.
The bot then speaks the phone's sealed frames on ``side=phone``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import time
from collections import defaultdict
from urllib.parse import parse_qs, urlparse

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from dark_army_daemon import devices, paths, relay, relay_bot, relay_ws
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
    monkeypatch.setattr(paths, "BOT_PAIR_PATH", tmp_path / "state" / "grok-bot.json")
    monkeypatch.setattr(paths, "BOT_MCP_TOKEN_PATH",
                        tmp_path / "state" / "grok-bot-mcp-token")
    relay.reset()
    devices.reset()
    yield
    relay.reset()
    devices.reset()


@pytest.fixture
def server():
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    return srv, daemon


def _arm(daemon) -> None:
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    assert relay.set_ws_url("wss://socket.example")[0]


class _LoopbackReq:
    def __init__(self, payload):
        self.method = "POST"
        self.path = "/api/action"
        self.query = ""
        self.headers = {"x-bob-token": "t"}
        self.body = json.dumps(payload).encode()

    def json(self):
        return json.loads(self.body)


def _secrets(text: str) -> None:
    assert "home_key" not in text
    assert "relay_key" not in text
    assert "key_b64" not in text
    assert "token" not in text


# --- the ledger row -----------------------------------------------------------


def test_mint_device_leaves_the_pairing_window_alone():
    code = devices.begin()
    token, device_id, name, detail = devices.mint_device("")
    assert token and device_id and detail == ""
    assert name == devices.DEFAULT_BOT_NAME
    assert devices.pairing_open()
    assert devices._pairing["code"] == code
    assert devices.home_key(device_id) is not None
    row = devices.snapshot()["devices"]
    assert row[0]["name"] == "Grok" and row[0]["id"] == device_id
    dumped = json.dumps(devices.snapshot())
    _secrets(dumped)
    assert token not in dumped
    assert base64.b64encode(devices.home_key(device_id)).decode() not in dumped


def test_mint_device_clamps_the_name_and_stops_at_eight():
    long = "G" * (devices.MAX_DEVICE_NAME_CHARS + 10)
    _token, _did, name, detail = devices.mint_device(long)
    assert detail == "" and len(name) == devices.MAX_DEVICE_NAME_CHARS
    for i in range(devices.MAX_DEVICES - 1):
        token, _did, _name, detail = devices.mint_device(f"n{i}")
        assert token and detail == ""
    token, device_id, name, detail = devices.mint_device("one more")
    assert token == "" and device_id == "" and name == ""
    assert "at most" in detail
    assert len(devices.snapshot()["devices"]) == devices.MAX_DEVICES


# --- the loopback verb --------------------------------------------------------


def test_a_second_bot_refuses_until_the_first_is_unpaired(server):
    """A phone row leaves the bot door open. One bot closes it. Unpairing
    that row is what opens it again, and the phone stays paired."""
    _srv, daemon = server
    _arm(daemon)
    token, phone_id, detail = devices.redeem(devices.begin(), "iPhone")
    assert token and detail == ""
    first = daemon.pair_bot("Grok")
    assert first["ok"] is True
    second = daemon.pair_bot("Another")
    assert second == {"ok": False, "detail": (
        "A bot is already paired. Unpair it in Devices to pair another.")}
    ids = [row["id"] for row in devices.snapshot()["devices"]]
    assert ids == [phone_id, first["device_id"]]
    assert set(relay.channel_ids()) == {first["device_id"]}
    dumped = json.dumps(daemon.devices_snapshot())
    assert "headless" not in dumped
    assert first["relay_key"] not in dumped
    ok, _detail = daemon.unpair_device(first["device_id"])
    assert ok
    third = daemon.pair_bot("Grok")
    assert third["ok"] is True
    assert third["device_id"] != first["device_id"]
    assert relay.channel_key(first["device_id"]) is None
    assert phone_id in [row["id"] for row in devices.snapshot()["devices"]]


def test_pair_bot_refuses_until_the_socket_is_on(server):
    _srv, daemon = server
    for _ in range(3):
        result = daemon.pair_bot("Grok")
        assert result["ok"] is False
        assert devices.snapshot()["devices"] == []
        assert relay.channel_ids() == {}
    assert daemon.pair_bot("Grok")["detail"] == "Away access is off"
    daemon.remote_access_enabled = True
    assert daemon.pair_bot("Grok")["detail"] == "Socket link is off"
    daemon.relay_ws_enabled = True
    assert daemon.pair_bot("Grok")["detail"] == (
        "Dark Army has no socket address set")


def test_pair_bot_mints_a_channel_beside_an_existing_phone(server):
    _srv, daemon = server
    phone_key = relay.create_channel("phone-1")
    code = devices.begin()
    _arm(daemon)
    result = daemon.pair_bot("  Grok bot  ")
    assert result["ok"] is True
    assert result["name"] == "Grok bot"
    assert devices.pairing_open() and devices._pairing["code"] == code
    assert relay.channel_key("phone-1") == base64.b64decode(phone_key)
    assert result["channel_id"] != relay.channel_id(
        base64.b64decode(phone_key))
    assert result["channel_id"] == relay.channel_id(
        base64.b64decode(result["relay_key"]))
    assert base64.b64decode(result["home_key"]) == devices.home_key(
        result["device_id"])
    assert base64.b64decode(result["relay_key"]) == relay.channel_key(
        result["device_id"])
    assert result["relay_ws_url"] == "wss://socket.example"
    # The bot acts on its Write grant, not the day lease, which pairing
    # clamps to 0 days so nothing can re-arm it.
    assert relay.bot_grant_valid(result["device_id"], "write")
    assert relay.lease_days(result["device_id"]) == 0
    snap = json.dumps(daemon.devices_snapshot())
    _secrets(snap)
    assert result["token"] not in snap
    assert result["channel_id"] not in snap
    assert result["relay_key"] not in snap
    assert result["home_key"] not in snap
    ok, _detail = daemon.unpair_device(result["device_id"])
    assert ok
    assert relay.channel_key(result["device_id"]) is None
    assert relay.channel_key("phone-1") is not None


def test_a_failed_relay_mint_rolls_the_row_back(server, monkeypatch):
    _srv, daemon = server
    _arm(daemon)
    monkeypatch.setattr(relay, "create_channel", lambda device_id: "")
    result = daemon.pair_bot("Grok")
    assert result == {"ok": False, "detail": "could not open an away channel"}
    assert devices.snapshot()["devices"] == []
    assert relay.channel_ids() == {}


@pytest.mark.asyncio
async def test_pair_bot_is_loopback_only(server, monkeypatch):
    srv, daemon = server
    assert "pair_bot" not in ApiServer.LAN_ACTIONS
    assert "pair_bot" not in ApiServer.REMOTE_ACTIONS
    _arm(daemon)
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "pair_bot", "name": "Grok"}, "dev-1")
    assert status == 404
    status, _ctype, _body = await srv._sealed_run(
        "action", {"action": "pair_bot", "name": "Grok"}, "dev-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 404
    assert devices.snapshot()["devices"] == []
    monkeypatch.setattr(srv, "_authorised", lambda request: False)
    assert srv._devices_request(_LoopbackReq({"action": "pair_bot"})) is None
    monkeypatch.setattr(srv, "_authorised", lambda request: True)
    parsed = srv._devices_request(
        _LoopbackReq({"action": "pair_bot", "name": "Desk"}))
    assert parsed is not None
    status, _ctype, body = await srv._devices(*parsed)
    assert status == 200
    reply = json.loads(body)
    assert reply["ok"] is True and reply["name"] == "Desk"
    snap = json.dumps(daemon.devices_snapshot())
    assert reply["relay_key"] not in snap
    assert reply["home_key"] not in snap
    assert reply["token"] not in snap
    assert reply["channel_id"] not in snap


def test_a_pair_file_keeps_its_secrets_beside_it():
    """The file a bot is given has no key. The sidecar puts them back for
    a local renew, and a counter write does not copy them into the file."""
    key = base64.b64encode(relay.mint_key()).decode("ascii")
    home = base64.b64encode(relay.mint_key()).decode("ascii")
    record = relay_bot.record_from_reply({
        "ok": True, "name": "Vex", "device_id": "abc", "token": "sekret",
        "home_key": home, "relay_key": key,
        "relay_ws_url": "wss://socket.example",
        "channel_id": relay.channel_id(base64.b64decode(key)),
    })
    public, secret = relay_bot.split_secrets(record)
    relay_bot.write_record(paths.BOT_PAIR_PATH, public)
    relay_bot.write_record(relay_bot.secrets_path(paths.BOT_PAIR_PATH), secret)
    stored = json.loads(paths.BOT_PAIR_PATH.read_text())
    assert "relay_key" not in stored and "home_key" not in stored
    assert "token" not in stored
    loaded = relay_bot._update(paths.BOT_PAIR_PATH, lambda rec: rec.__setitem__(
        "send_ctr", int(rec.get("send_ctr") or 0) + 1))
    assert loaded["relay_key"] == key
    again = json.loads(paths.BOT_PAIR_PATH.read_text())
    assert "relay_key" not in again and again["send_ctr"] == 1


def test_the_pair_file_is_private_and_roundtrips():
    assert paths.BOT_PAIR_NAME in paths._PRIVATE_FILES
    key = base64.b64encode(relay.mint_key()).decode("ascii")
    home = base64.b64encode(relay.mint_key()).decode("ascii")
    record = relay_bot.record_from_reply({
        "ok": True,
        "name": "Grok",
        "device_id": "abc",
        "token": "sekret",
        "home_key": home,
        "relay_key": key,
        "relay_url": "https://mailbox.example",
        "relay_ws_url": "wss://socket.example",
        "channel_id": relay.channel_id(base64.b64decode(key)),
    })
    relay_bot.write_record(paths.BOT_PAIR_PATH, record)
    mode = stat.S_IMODE(paths.BOT_PAIR_PATH.stat().st_mode)
    assert mode == 0o600
    loaded = json.loads(paths.BOT_PAIR_PATH.read_text())
    assert loaded["send_ctr"] == 0 and loaded["channel_id"] == record["channel_id"]
    wire = relay.seal_frame(
        base64.b64decode(loaded["relay_key"]), relay.DIR_PHONE_TO_MAC,
        1, "state", {"done": "review"}, frame_id="req-1")
    frame, err = relay.open_frame(
        base64.b64decode(loaded["relay_key"]), relay.DIR_PHONE_TO_MAC,
        wire, 0)
    assert err == "" and frame["kind"] == "state" and frame["id"] == "req-1"


def test_a_home_check_in_frame_arms_nothing_for_the_bot(server):
    """The frame `renew` seals is a normal home frame and it verifies — but
    the bot is not on the day lease (`docs/transport-contract.md`, *The
    bot's access is two grants*), so its own home frames re-arm nothing:
    `_home_admit` skips `note_lan_proof` for the bot, and pairing already
    clamped its lease to 0 days."""
    srv, daemon = server
    _arm(daemon)
    result = daemon.pair_bot("Grok")
    assert result["ok"] is True
    did = result["device_id"]
    assert relay.lease_days(did) == 0
    assert relay.lease_valid(did) is False
    # Even a bot row carrying an old grant length is not re-armed.
    data = json.loads(paths.RELAY_PATH.read_text())
    data["channels"][did]["lease_days"] = 14
    data["channels"][did]["lease_expires_at"] = time.time() - 60
    paths.RELAY_PATH.write_text(json.dumps(data))
    relay.invalidate()
    home = base64.b64decode(result["home_key"])
    wire = relay.seal_frame(
        home, relay.DIR_PHONE_TO_MAC, 1, "state", {"done": "review"},
        ns=relay.HOME)

    class _Req:
        path = "/api/home"
        peer = "127.0.0.1"
        headers = {"x-bob-channel": relay.channel_id(home, ns=relay.HOME)}

    device_id, _key, frame = srv._home_open(_Req(), wire=wire)
    assert device_id == did
    assert frame["kind"] == "state"
    assert relay.lease_valid(did) is False


def test_a_reply_whose_channel_does_not_match_is_refused():
    key = base64.b64encode(relay.mint_key()).decode("ascii")
    home = base64.b64encode(relay.mint_key()).decode("ascii")
    with pytest.raises(relay_bot.BotError):
        relay_bot.record_from_reply({
            "ok": True, "name": "Grok", "device_id": "abc", "token": "t",
            "home_key": home, "relay_key": key,
            "relay_ws_url": "wss://socket.example",
            "channel_id": "0" * 32,
        })


# --- the socket ---------------------------------------------------------------


class _FakeRelay:
    """One mac side and one phone side per channel, text forwarded."""

    def __init__(self):
        self.channels = defaultdict(lambda: {"mac": None, "phone": None})
        self.url = ""

    async def handler(self, ws):
        parsed = urlparse(ws.request.path)
        qs = parse_qs(parsed.query)
        ch = (qs.get("ch") or [""])[0]
        side = (qs.get("side") or [""])[0]
        if side not in ("mac", "phone") or len(ch) != 32:
            await ws.close(1008, "bad request")
            return
        entry = self.channels[ch]
        other = "phone" if side == "mac" else "mac"
        entry[side] = ws
        peer = entry[other]
        try:
            await ws.send("peer:1" if peer is not None else "peer:0")
            if peer is not None:
                await peer.send("peer:1")
            async for message in ws:
                if isinstance(message, bytes) or str(message).startswith("peer:"):
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


async def _until(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never held")


@pytest.mark.asyncio
async def test_the_bot_arms_its_own_socket_and_reads_state(server, monkeypatch):
    srv, daemon = server
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    fake = _FakeRelay()
    held = await websockets.serve(fake.handler, "127.0.0.1", 0)
    port = held.sockets[0].getsockname()[1]
    fake.url = f"ws://127.0.0.1:{port}"
    # ``set_ws_url`` refuses plain ``ws://``. The connector's own test seam
    # is a stored loopback address, which is what ``pair_bot`` reads back.
    data = relay.load()
    data["ws_url"] = fake.url
    assert relay.save(data)
    try:
        result = daemon.pair_bot("Grok")
        assert result["ok"] is True
        relay_bot.write_record(
            paths.BOT_PAIR_PATH, relay_bot.record_from_reply(result))
        monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
        conn = relay_ws.RelaySocketConnector(srv)
        srv.on_picture = conn.nudge
        conn.start()
        try:
            await _until(lambda: conn.socket_word(result["device_id"])
                         == relay_ws.SOCKET_OPEN)
            async with relay_bot.BotSession() as session:
                frame = await session.request(
                    "state", {"done": "review"})
            assert conn.socket_word(result["device_id"]) == relay_ws.SOCKET_ARMED
        finally:
            await conn.stop()
    finally:
        held.close()
        await held.wait_closed()
    payload = frame["body"]
    assert payload["status"] == 200
    assert payload["re"]
    picture = json.loads(payload["body"])
    assert "agents" in picture
    saved = json.loads(paths.BOT_PAIR_PATH.read_text())
    assert saved["send_ctr"] == 1
    assert saved["recv_ctr"] >= 1
    assert "relay_key" not in json.dumps(picture)


async def _mcp_post(port: int, token: str | None, payload: dict):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps(payload).encode()
    lines = [
        "POST /mcp HTTP/1.1",
        "Host: 127.0.0.1",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
    ]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, rest = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, rest


@pytest.mark.asyncio
async def test_the_connector_lists_tools_and_refuses_a_stranger():
    server, line, token, port = await relay_bot.start_connector()
    try:
        bound = server.sockets[0].getsockname()
        assert bound[0] == "127.0.0.1"
        assert paths.BOT_MCP_TOKEN_NAME in paths._PRIVATE_FILES
        assert stat.S_IMODE(paths.BOT_MCP_TOKEN_PATH.stat().st_mode) == 0o600
        status, body = await _mcp_post(port, None, {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert status == 401
        assert token.encode() not in body
        status, body = await _mcp_post(port, token, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {}})
        assert status == 200
        reply = json.loads(body)
        assert reply["result"]["serverInfo"]["name"] == "Dark Army"
        status, body = await _mcp_post(port, token, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in json.loads(body)["result"]["tools"]]
        assert names == ["picture", "act"]
        status, body = await _mcp_post(port, token, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "act", "arguments": {}}})
        result = json.loads(body)["result"]
        assert result["isError"] is True
    finally:
        server.close()
        await server.wait_closed()
        await line.close()


@pytest.mark.asyncio
async def test_picture_reads_proxys_line(server, monkeypatch):
    srv, daemon = server
    daemon.remote_access_enabled = True
    daemon.relay_ws_enabled = True
    fake = _FakeRelay()
    held = await websockets.serve(fake.handler, "127.0.0.1", 0)
    fake.url = f"ws://127.0.0.1:{held.sockets[0].getsockname()[1]}"
    data = relay.load()
    data["ws_url"] = fake.url
    assert relay.save(data)
    mcp = None
    conn = None
    try:
        result = daemon.pair_bot("Vex")
        assert result["ok"] is True
        relay_bot.write_record(
            paths.BOT_PAIR_PATH, relay_bot.record_from_reply(result))
        monkeypatch.setattr(relay_ws, "WS_PUSH_MIN_INTERVAL", 0.05)
        conn = relay_ws.RelaySocketConnector(srv)
        srv.on_picture = conn.nudge
        conn.start()
        await _until(lambda: conn.socket_word(result["device_id"])
                     == relay_ws.SOCKET_OPEN)
        mcp_server, line, token, port = await relay_bot.start_connector()
        mcp = (mcp_server, line)
        status, body = await _mcp_post(port, token, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "picture", "arguments": {}}})
        assert status == 200
        text = json.loads(body)["result"]["content"][0]["text"]
        assert text.startswith("Proxy's line is open.")
        picture = json.loads(text.split("\n", 1)[1])
        assert "agents" in picture
        assert conn.socket_word(result["device_id"]) == relay_ws.SOCKET_ARMED
        assert token not in text
    finally:
        if mcp is not None:
            mcp[0].close()
            await mcp[0].wait_closed()
            await mcp[1].close()
        if conn is not None:
            await conn.stop()
        held.close()
        await held.wait_closed()


def _saved_record() -> dict:
    key = base64.b64encode(relay.mint_key()).decode("ascii")
    home = base64.b64encode(relay.mint_key()).decode("ascii")
    record = relay_bot.record_from_reply({
        "ok": True, "name": "Grok", "device_id": "abc", "token": "t",
        "home_key": home, "relay_key": key,
        "relay_ws_url": "wss://socket.example",
        "channel_id": relay.channel_id(base64.b64decode(key)),
    })
    relay_bot.write_record(paths.BOT_PAIR_PATH, record)
    return record


def test_a_second_session_refuses_but_counter_writes_run_beside_it():
    """`listen` holds the socket lock for its life; a second session is
    refused, and a `renew`-shaped counter write still goes through."""
    _saved_record()
    held = relay_bot._hold_session(paths.BOT_PAIR_PATH)
    try:
        with pytest.raises(relay_bot.PairBusy):
            relay_bot._hold_session(paths.BOT_PAIR_PATH)
        written = relay_bot._update(
            paths.BOT_PAIR_PATH,
            lambda r: r.__setitem__("home_send_ctr", r["home_send_ctr"] + 1))
        assert written["home_send_ctr"] == 1
    finally:
        os.close(held)
    os.close(relay_bot._hold_session(paths.BOT_PAIR_PATH))


def test_a_session_persist_keeps_home_counters_written_beside_it():
    _saved_record()
    session = relay_bot.BotSession()
    session._lock_fd = relay_bot._hold_session(paths.BOT_PAIR_PATH)
    try:
        session.record = relay_bot._update(paths.BOT_PAIR_PATH)
        relay_bot._update(
            paths.BOT_PAIR_PATH,
            lambda r: r.update(home_send_ctr=4, home_recv_ctr=3))
        assert session._next_send() == 1
        saved = json.loads(paths.BOT_PAIR_PATH.read_text())
        assert saved["send_ctr"] == 1
        assert saved["home_send_ctr"] == 4 and saved["home_recv_ctr"] == 3
        # A lower in-memory counter never rolls the file's back.
        relay_bot._update(paths.BOT_PAIR_PATH,
                          lambda r: r.__setitem__("send_ctr", 9))
        assert session._next_send() == 9
    finally:
        os.close(session._lock_fd)
        session._lock_fd = None


def test_a_failed_change_writes_nothing():
    _saved_record()
    before = paths.BOT_PAIR_PATH.read_text()

    def refuse(record):
        record["send_ctr"] = 99
        raise relay_bot.BotError("no")

    with pytest.raises(relay_bot.BotError):
        relay_bot._update(paths.BOT_PAIR_PATH, refuse)
    assert paths.BOT_PAIR_PATH.read_text() == before


def test_post_loopback_needs_the_desk_token_from_the_environment(monkeypatch):
    """The device verbs are desk verbs: `post_loopback` reads the desk token
    from `DARK_ARMY_DESK_TOKEN` and never the on-disk session token, and
    says where to get it when it is missing — before any request is made."""
    monkeypatch.delenv("DARK_ARMY_DESK_TOKEN", raising=False)
    (paths.STATE_DIR).mkdir(parents=True, exist_ok=True)
    (paths.STATE_DIR / "api-token").write_text("session-on-disk\n")

    def no_request(*_a, **_k):
        raise AssertionError("no request without a desk token")

    monkeypatch.setattr(relay_bot.urllib.request, "urlopen", no_request)
    with pytest.raises(relay_bot.BotError) as err:
        relay_bot.post_loopback({"action": "unpair_device", "device_id": "x"})
    assert "Copy desk key" in str(err.value)
    assert "DARK_ARMY_DESK_TOKEN" in str(err.value)

    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"ok": true}'

    def capture(req, timeout=0):
        seen["token"] = req.get_header("X-bob-token")
        return _Resp()

    monkeypatch.setattr(relay_bot.urllib.request, "urlopen", capture)
    monkeypatch.setenv("DARK_ARMY_DESK_TOKEN", "desk-from-env")
    assert relay_bot.post_loopback({"action": "noop"}) == {"ok": True}
    assert seen["token"] == "desk-from-env"
