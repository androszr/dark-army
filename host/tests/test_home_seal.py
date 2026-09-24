# host/tests/test_home_seal.py
"""The sealed home path: the pins the plan's answers ask for, in one place.

Every request on the phone door after pairing is a sealed frame under the
device's home key (`relay.HOME`); the plaintext routes the old phone called
answer 426 in words and run nothing. Free ports from held-open sockets and a
temp ledger, exactly as `test_lan_access.py` does.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import attachments, devices, lan_hosts, paths, relay
from dark_army_daemon.api_server import HOME_UPDATE_REFUSAL, ApiServer
from dark_army_daemon.daemon import BobDaemon
from tests.pake_pair import pair_typed
from tests.free_ports import free_ports


def _free_ports(count: int) -> list[int]:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_ports(count)


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture(autouse=True)
def machine(monkeypatch):
    monkeypatch.setattr(lan_hosts, "live_addrs", lambda: {"en0": ["192.168.1.5"]})
    monkeypatch.setattr(lan_hosts, "default_route_addr", lambda: "192.168.1.5")
    monkeypatch.setattr(socket, "gethostname", lambda: "test-mac")


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    devices.reset()
    relay.reset()
    yield tmp_path / "devices.json"
    devices.reset()
    relay.reset()


@pytest.fixture(autouse=True)
def attach_dir(tmp_path, monkeypatch):
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", root)
    return root


@pytest_asyncio.fixture
async def server(tmp_path, monkeypatch):
    loop_port, lan_port = _free_ports(2)
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", tmp_path / "api-token")
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    monkeypatch.setattr(api_mod, "LAN_API_PORT", lan_port)
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=loop_port)
    daemon._api = srv
    daemon.lan_access_enabled = True
    await srv.start()
    await srv.start_lan()
    try:
        yield srv, daemon, lan_port
    finally:
        await srv.stop()


def _blocking_fetch(path, *, port, data=None, headers=None, method=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {},
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def fetch(*args, **kwargs):
    return await asyncio.to_thread(_blocking_fetch, *args, **kwargs)


async def pair_plain(lan_port: int, name: str = "phone") -> dict:
    """A typed-address pair: SRP over the code (`tests/pake_pair.py`),
    the derived key under `key` and no key in any answer."""
    return await pair_typed(fetch, lan_port, name=name)


def _open_reply(key: bytes, body: bytes, last: int = 0):
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, body.decode(),
                                  last, ns=relay.HOME)
    assert err == "", err
    return frame


async def home_post(lan_port: int, key: bytes, kind: str, body: dict,
                    ctr: int, *, chan: str = "", headers: dict | None = None):
    """Seal one frame, POST /api/home, open the sealed answer. Returns the
    opened Mac-to-phone frame."""
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, kind, body,
                            ns=relay.HOME)
    sent = {"X-Bob-Channel": chan or relay.channel_id(key, ns=relay.HOME),
            "Content-Type": "text/plain"}
    sent.update(headers or {})
    status, out = await fetch("/api/home", port=lan_port,
                              data=wire.encode(), headers=sent)
    return status, out


async def inner(lan_port, key, kind, body, ctr):
    status, out = await home_post(lan_port, key, kind, body, ctr)
    assert status == 200, out
    frame = _open_reply(key, out)
    assert frame["kind"] == "reply"
    return int(frame["body"]["status"]), frame["body"]["body"]


# --- plaintext is refused in words, and nothing runs --------------------------


@pytest.mark.asyncio
async def test_plain_state_action_usage_and_upload_are_426_in_words(
        server, monkeypatch):
    srv, _daemon, lan_port = server
    paired = await pair_plain(lan_port)
    ran = []

    async def spy(action, payload, device_id=""):
        ran.append(action)
        return 200, "application/json", b"{}"

    monkeypatch.setattr(srv, "_lan_run", spy)
    for path, data, headers in (
        ("/api/state", None, {}),
        ("/api/usage", None, {}),
        ("/api/action", json.dumps({"action": "dismiss",
                                    "session_id": "s"}).encode(),
         {"Content-Type": "application/json"}),
        # Even with the old header, or the loopback token.
        ("/api/state", None, {"X-Bob-Device": paired["token"]}),
        ("/api/action", json.dumps({"action": "dismiss"}).encode(),
         {"X-Bob-Device": paired["token"]}),
        ("/api/action", json.dumps({"action": "dismiss"}).encode(),
         {"X-Bob-Token": srv.token}),
    ):
        status, out = await fetch(path, port=lan_port, data=data,
                                  headers=headers)
        assert status == 426, (path, status, out)
        assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL
    # A plain upload is a frame-less upload: the old route is gone.
    status, out = await fetch(
        "/api/upload?staging=abcd1234-efgh5678-ijkl9012-mnop34&name=a.png",
        port=lan_port, data=b"\x89PNG",
        headers={"X-Bob-Device": paired["token"],
                 "Content-Type": "application/octet-stream"})
    assert status == 403
    assert json.loads(out)["error"] == "that phone is not paired"
    assert ran == []


# --- one phone cannot act as another -------------------------------------------


@pytest.mark.asyncio
async def test_a_frame_under_one_key_cannot_wear_anothers_channel(server):
    _srv, daemon, lan_port = server
    daemon.remote_access_enabled = True
    a = await pair_plain(lan_port, "a")
    b = await pair_plain(lan_port, "b")
    # A's frame, B's channel header: the seal fails under B's key.
    status, out = await home_post(
        lan_port, a["key"], "action",
        {"action": "register_push_token", "token": "ab" * 32}, 1,
        chan=relay.channel_id(b["key"], ns=relay.HOME))
    assert status == 403
    assert json.loads(out)["error"] == relay.REFUSAL_WORDS["seal"]
    assert relay.push_token(a["device_id"]) is None
    assert relay.push_token(b["device_id"]) is None
    # A's frame under A's own channel lands on A.
    status, body = await inner(
        lan_port, a["key"], "action",
        {"action": "register_push_token", "token": "ab" * 32}, 1)
    assert status == 200, body
    assert relay.push_token(a["device_id"]) == "ab" * 32
    assert relay.push_token(b["device_id"]) is None


@pytest.mark.asyncio
async def test_the_activity_token_verb_lands_on_the_caller_at_home_and_away(server):
    """`register_activity_token` is `register_push_token`'s twin on both
    doors: the caller's own channel alone at home, and the away door's
    `_remote_run` reaches the same helper for the same device."""
    srv, daemon, lan_port = server
    daemon.remote_access_enabled = True
    a = await pair_plain(lan_port, "a")
    b = await pair_plain(lan_port, "b")
    status, out = await home_post(
        lan_port, a["key"], "action",
        {"action": "register_activity_token", "token": "ab" * 32}, 1,
        chan=relay.channel_id(b["key"], ns=relay.HOME))
    assert status == 403
    assert json.loads(out)["error"] == relay.REFUSAL_WORDS["seal"]
    assert relay.activity_token(a["device_id"]) is None
    status, body = await inner(
        lan_port, a["key"], "action",
        {"action": "register_activity_token", "token": "ab" * 32}, 1)
    assert status == 200, body
    assert relay.activity_token(a["device_id"]) == "ab" * 32
    assert relay.activity_shape(a["device_id"]) == 1
    assert relay.activity_token(b["device_id"]) is None
    # Away: the relay door's execution helper, same device, same helper.
    relay.note_lan_proof(b["device_id"])
    status, _ctype, out = await srv._remote_run(
        "action", {"action": "register_activity_token", "token": "cd" * 32,
                   "env": "dev"}, b["device_id"])
    assert status == 200, out
    assert relay.activity_token(b["device_id"]) == "cd" * 32
    assert relay.activity_env(b["device_id"]) == "dev"
    assert relay.activity_token(a["device_id"]) == "ab" * 32


@pytest.mark.asyncio
async def test_the_activity_token_records_the_cards_shape(server):
    """`shape` is optional. Absent stores 1, `"2"` and `2` store 2, and
    anything else is 400 with nothing stored. The away door reaches the
    same helper."""
    srv, daemon, lan_port = server
    daemon.remote_access_enabled = True
    phone = await pair_plain(lan_port, "shape")
    did = phone["device_id"]
    status, body = await inner(
        lan_port, phone["key"], "action",
        {"action": "register_activity_token", "token": "ab" * 32}, 1)
    assert status == 200, body
    assert relay.activity_shape(did) == 1
    status, body = await inner(
        lan_port, phone["key"], "action",
        {"action": "register_activity_token", "token": "cd" * 32, "shape": "2"}, 2)
    assert status == 200, body
    assert relay.activity_shape(did) == 2
    status, body = await inner(
        lan_port, phone["key"], "action",
        {"action": "register_activity_token", "token": "ef" * 32, "shape": 3}, 3)
    assert status == 400, body
    assert json.loads(body)["error"] == "unknown live card shape"
    # The refusal stored nothing new: the previous token is still the one.
    assert relay.activity_token(did) == "cd" * 32
    other = await pair_plain(lan_port, "shape-b")
    status, body = await inner(
        lan_port, other["key"], "action",
        {"action": "register_activity_token", "token": "ab" * 32, "shape": "x"}, 1)
    assert status == 400, body
    assert relay.activity_token(other["device_id"]) is None
    relay.note_lan_proof(other["device_id"])
    status, _ctype, out = await srv._remote_run(
        "action", {"action": "register_activity_token", "token": "ab" * 32,
                   "env": "dev", "shape": 2}, other["device_id"])
    assert status == 200, out
    assert relay.activity_shape(other["device_id"]) == 2
    assert relay.activity_env(other["device_id"]) == "dev"


@pytest.mark.asyncio
async def test_an_away_frame_is_refused_unopened_on_the_home_door(server):
    """Same key material, wrong namespace: a relay frame under the device's
    *relay* key, and a relay-namespace frame under its *home* key, both
    fail the seal on `/api/home`."""
    _srv, daemon, lan_port = server
    daemon.remote_access_enabled = True
    paired = await pair_plain(lan_port)
    relay_key = base64.b64decode(paired["relay_key"])
    wire = relay.seal_frame(relay_key, relay.DIR_PHONE_TO_MAC, 1, "state", {})
    status, out = await fetch(
        "/api/home", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(paired["key"],
                                                   ns=relay.HOME)})
    assert status == 403
    assert json.loads(out)["error"] == relay.REFUSAL_WORDS["seal"]
    wire = relay.seal_frame(paired["key"], relay.DIR_PHONE_TO_MAC, 1,
                            "state", {}, ns=relay.RELAY)
    status, out = await fetch(
        "/api/home", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(paired["key"],
                                                   ns=relay.HOME)})
    assert status == 403
    assert json.loads(out)["error"] == relay.REFUSAL_WORDS["seal"]
    # And a relay channel id is not a home channel id.
    status, out = await fetch(
        "/api/home", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(relay_key)})
    assert status == 403
    assert json.loads(out)["error"] == "that phone is not paired"


# --- the key never surfaces ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_home_key_never_reaches_the_read_surface(server):
    srv, daemon, lan_port = server
    paired = await pair_plain(lan_port)
    published = json.dumps(srv.state(), default=str)
    published += json.dumps(daemon.devices_snapshot(), default=str)
    published += json.dumps(devices.snapshot(), default=str)
    assert paired["home_key"] not in published
    assert paired["key"].hex() not in published
    assert "home_key_b64" not in published
    assert relay.channel_id(paired["key"], ns=relay.HOME) not in published
    row = next(r for r in daemon.devices_snapshot()["devices"]
               if r["id"] == paired["device_id"])
    assert row["home"] is True
    assert daemon.devices_snapshot()["home_sealed"] is True


# --- pairing, sealed and plain -------------------------------------------------


@pytest.mark.asyncio
async def test_a_sealed_pair_request_gets_a_sealed_reply(server, ledger):
    _srv, daemon, lan_port = server
    daemon.remote_access_enabled = True
    code = devices.begin(allow_plain=True)
    key = devices.pairing_home_key()
    assert key is not None and len(key) == 32
    # The panel's QR carries the same key.
    assert base64.b64decode(daemon.begin_pairing()["home_key"]) == \
        devices.pairing_home_key()
    code = devices.begin(allow_plain=True)
    key = devices.pairing_home_key()
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 7, "pair",
                            {"code": code, "name": "scanned"}, ns=relay.HOME)
    status, out = await fetch(
        "/api/pair", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(key, ns=relay.HOME),
                 "Content-Type": "text/plain"})
    assert status == 200, out
    frame = _open_reply(key, out)
    assert frame["kind"] == "reply"
    answer = json.loads(frame["body"]["body"])
    assert answer["token"] and answer["device_id"]
    assert answer["relay_key"]
    assert "home_key" not in answer
    assert "home_key" not in frame["body"]
    # The pair frame's counter is the device's floor: a replay of the pair
    # frame — or anything at or below 7 — is refused.
    entry = next(e for e in json.loads(ledger.read_text())["devices"]
                 if e["id"] == answer["device_id"])
    assert entry["home_recv_ctr"] == 7
    assert devices.home_key(answer["device_id"]) == key
    status, out = await home_post(lan_port, key, "state", {}, 7)
    assert status == 200
    refusal = _open_reply(key, out, last=1)
    assert refusal["kind"] == "err"
    assert refusal["body"]["ctr_expected"] == 8
    status, body = await inner(lan_port, key, "state", {}, 8)
    assert status == 200
    assert "agents" in json.loads(body)


@pytest.mark.asyncio
async def test_a_sealed_pair_refusal_is_sealed_too(server):
    _srv, _daemon, lan_port = server
    code = devices.begin(allow_plain=True)
    key = devices.pairing_home_key()
    chan = relay.channel_id(key, ns=relay.HOME)
    # A wrong channel id learns only the wrong-code sentence.
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "pair",
                            {"code": code, "name": "p"}, ns=relay.HOME)
    status, out = await fetch(
        "/api/pair", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(relay.mint_key(),
                                                   ns=relay.HOME)})
    assert status == 403
    assert json.loads(out)["error"] == "that pairing code is not valid"
    # A wrong code inside a good frame is a sealed err the phone can open.
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "pair",
                            {"code": "NOPE1234", "name": "p"}, ns=relay.HOME)
    status, out = await fetch("/api/pair", port=lan_port, data=wire.encode(),
                              headers={"X-Bob-Channel": chan})
    assert status == 200
    frame = _open_reply(key, out)
    assert frame["kind"] == "err"
    assert frame["body"]["error"] == "that pairing code is not valid"
    # A frame that is not a pair request is refused before redeem.
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "state", {},
                            ns=relay.HOME)
    status, out = await fetch("/api/pair", port=lan_port, data=wire.encode(),
                              headers={"X-Bob-Channel": chan})
    assert status == 403
    # A non-ASCII channel header is refused with the same sentence, not by
    # a dropped connection: headers are latin-1 and `compare_digest` on two
    # `str`s raises on one.
    status, out = await fetch(
        "/api/pair", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": chan[:-1] + "\xe9"})
    assert status == 403
    assert json.loads(out)["error"] == "that pairing code is not valid"
    token, _did, _detail = devices.redeem(code, "still-valid")
    assert token


@pytest.mark.asyncio
async def test_the_plain_pair_reply_carries_the_home_key_and_it_works(server):
    _srv, _daemon, lan_port = server
    paired = await pair_plain(lan_port)
    assert len(paired["key"]) == 32
    assert devices.home_key(paired["device_id"]) == paired["key"]
    status, body = await inner(lan_port, paired["key"], "state", {}, 1)
    assert status == 200
    assert "agents" in json.loads(body)
    status, body = await inner(lan_port, paired["key"], "usage",
                               {"query": "window=session"}, 2)
    assert status == 200
    assert json.loads(body)["window"] == "session"
    # An unknown kind is an inner 404, sealed.
    status, _body = await inner(lan_port, paired["key"], "history", {}, 3)
    assert status == 404


# --- un-pairing deletes the key ------------------------------------------------


@pytest.mark.asyncio
async def test_unpair_deletes_the_home_key_and_the_next_frame_is_403(
        server, ledger):
    _srv, daemon, lan_port = server
    paired = await pair_plain(lan_port)
    status, _ = await inner(lan_port, paired["key"], "state", {}, 1)
    assert status == 200
    ok, _detail = daemon.unpair_device(paired["device_id"])
    assert ok is True
    assert devices.home_key(paired["device_id"]) is None
    assert paired["home_key"] not in ledger.read_text()
    status, out = await home_post(lan_port, paired["key"], "state", {}, 2)
    assert status == 403
    assert json.loads(out)["error"] == "that phone is not paired"


# --- replay ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_replayed_frame_answers_a_sealed_err_with_ctr_expected(server):
    _srv, _daemon, lan_port = server
    paired = await pair_plain(lan_port)
    status, _ = await inner(lan_port, paired["key"], "state", {}, 5)
    assert status == 200
    status, out = await home_post(lan_port, paired["key"], "state", {}, 5)
    assert status == 200
    frame = _open_reply(paired["key"], out, last=1)
    assert frame["kind"] == "err"
    assert frame["body"]["status"] == 409
    assert frame["body"]["error"] == relay.REFUSAL_WORDS["ctr"]
    assert frame["body"]["ctr_expected"] == 6
    # A write's counter persists at once: a fresh process still refuses.
    status, _ = await inner(lan_port, paired["key"], "action",
                            {"action": "dismiss"}, 9)
    devices.reset()
    assert devices.last_home_recv_ctr(paired["device_id"]) == 9


@pytest.mark.asyncio
async def test_a_stale_timestamp_is_a_sealed_err_and_an_origin_a_plain_403(
        server):
    """A skewed clock is a refusal of a frame whose seal *verified*, so it is
    answered sealed with the status inside — a plain 403 would have the
    phone forgetting a valid pairing on every poll. Origin, and anything
    the door could not authenticate, stays a plain 403."""
    _srv, _daemon, lan_port = server
    paired = await pair_plain(lan_port)
    wire = relay.seal_frame(paired["key"], relay.DIR_PHONE_TO_MAC, 1, "state",
                            {}, ts=1.0, ns=relay.HOME)
    status, out = await fetch(
        "/api/home", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(paired["key"],
                                                   ns=relay.HOME)})
    assert status == 200
    frame = _open_reply(paired["key"], out)
    assert frame["kind"] == "err"
    assert frame["body"]["status"] == 409
    assert frame["body"]["error"] == relay.REFUSAL_WORDS["ts"]
    assert "ctr_expected" not in frame["body"]
    # And the pairing stands: the next good frame is answered.
    status, body = await inner(lan_port, paired["key"], "state", {}, 1)
    assert status == 200
    status, out = await home_post(lan_port, paired["key"], "state", {}, 1,
                                  headers={"Origin": "http://evil.example"})
    assert status == 403
    assert json.loads(out)["error"] == "forbidden"
    # No channel header at all: not paired, before any open.
    status, out = await fetch("/api/home", port=lan_port, data=b"x")
    assert status == 403
    assert json.loads(out)["error"] == "that phone is not paired"


# --- the home path is not the away path ----------------------------------------


@pytest.mark.asyncio
async def test_home_actions_are_gated_by_lan_actions_alone(server, monkeypatch):
    """A verb in `LAN_ACTIONS` but not `REMOTE_ACTIONS` runs at home; the
    lease is never consulted; nothing is recorded as remote activity."""
    srv, daemon, lan_port = server
    paired = await pair_plain(lan_port)
    ran = []

    async def spy(action, payload, device_id=""):
        ran.append((action, device_id))
        return 200, "application/json", b'{"ok": true}'

    monkeypatch.setattr(srv, "_lan_run", spy)
    monkeypatch.setattr(relay, "lease_valid", lambda _d: False)
    saved = ApiServer.REMOTE_ACTIONS
    try:
        ApiServer.REMOTE_ACTIONS = ("dismiss",)
        status, _ = await inner(lan_port, paired["key"], "action",
                                {"action": "stop_session"}, 1)
        assert status == 200
        assert ran == [("stop_session", paired["device_id"])]
        status, _ = await inner(lan_port, paired["key"], "action",
                                {"action": "wrap_up"}, 2)
        assert status == 404
    finally:
        ApiServer.REMOTE_ACTIONS = saved
    assert daemon.devices_snapshot()["remote_activity"] == []


@pytest.mark.asyncio
async def test_devices_paired_before_sealed_home_access_have_no_key(ledger):
    """An older ledger entry has no `home_key_b64`: it resolves to no
    channel, its snapshot row says `home: false`, and its counters read 0."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"version": 1, "devices": [
        {"id": "old1", "name": "Old", "digest": "ab" * 32, "paired_at": 1.0},
    ]}))
    devices.invalidate()
    assert devices.home_key("old1") is None
    assert devices.device_for_home_channel("ab" * 16) == ""
    assert devices.device_for_home_channel("") == ""
    assert devices.last_home_recv_ctr("old1") == 0
    assert devices.next_home_send_ctr("old1") == 0
    row = devices.snapshot()["devices"][0]
    assert row["home"] is False
    assert "home_key_b64" not in json.dumps(devices.snapshot())
