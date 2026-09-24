# host/tests/test_relay_lease.py
"""The 24-hour away window: armed at home, checked per write, never at the
relay.

The lease is a number in ``relay.json`` on the Mac. ``note_lan_proof`` — the
one arming site, inside ``_home_open`` after a home frame verifies — extends
it; ``_remote_run`` checks it per write frame; nothing on the remote path may
touch it. Reads
survive expiry, writes stop in ``LEASE_REFUSAL``'s words, and un-pairing
deletes the channel outright.
"""

from __future__ import annotations

import json
import time

import pytest

from dark_army_daemon import devices, paths, relay
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
    devices.reset_pairing()
    yield tmp_path / "relay.json"
    relay.reset()
    devices.invalidate()
    devices.reset_pairing()


@pytest.fixture
def server():
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    return srv, daemon


def _lapse(store_path, device_id: str) -> None:
    """Wind one device's lease into the past, as time passing would."""
    data = json.loads(store_path.read_text())
    data["channels"][device_id]["lease_expires_at"] = time.time() - 60
    store_path.write_text(json.dumps(data))
    relay.invalidate()


# --- arming -------------------------------------------------------------------


def test_note_lan_proof_arms_a_day(store):
    relay.create_channel("dev-1")
    _lapse(store, "dev-1")
    assert relay.lease_valid("dev-1") is False
    relay.note_lan_proof("dev-1")
    expires = relay.lease_expires_at("dev-1")
    assert abs(expires - (time.time() + relay.RELAY_LEASE_SECONDS)) < 5
    assert relay.lease_valid("dev-1") is True


def test_lease_refresh_coalesces_disk_writes(store):
    relay.create_channel("dev-1")
    before = store.read_bytes()
    # The phone polls every 4 seconds at home; a freshly armed lease gains
    # nothing worth a disk write from another proof moments later.
    relay.note_lan_proof("dev-1")
    relay.note_lan_proof("dev-1")
    assert store.read_bytes() == before
    # Once the stored value lags by more than the refresh floor, the next
    # proof persists.
    data = json.loads(store.read_text())
    data["channels"]["dev-1"]["lease_expires_at"] -= (
        relay.RELAY_LEASE_REFRESH_SECONDS * 2)
    store.write_text(json.dumps(data))
    relay.invalidate()
    lagged = relay.lease_expires_at("dev-1")
    relay.note_lan_proof("dev-1")
    assert relay.lease_expires_at("dev-1") > lagged


def test_no_channel_no_arming(store):
    relay.note_lan_proof("nobody")
    assert relay.lease_valid("nobody") is False
    assert not store.exists()


def test_pairing_itself_arms_the_lease(store):
    relay.create_channel("dev-1")
    assert relay.lease_valid("dev-1") is True


# --- the write gate -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lapsed_lease_refuses_the_write_and_runs_nothing(
        server, store, monkeypatch):
    srv, _daemon = server
    relay.create_channel("dev-1")
    _lapse(store, "dev-1")
    calls = []

    async def fake_lan_run(action, payload, device_id=""):
        calls.append(action)
        return 200, "application/json", b"{}"

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    status, _ctype, body = await srv._remote_run(
        "action", {"action": "stop_session", "session_id": "s"}, "dev-1")
    assert status == 403
    assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
    assert calls == []


@pytest.mark.asyncio
async def test_valid_lease_reaches_lan_run(server, monkeypatch):
    srv, _daemon = server
    relay.create_channel("dev-1")
    calls = []

    async def fake_lan_run(action, payload, device_id=""):
        calls.append(action)
        return 200, "application/json", b"{}"

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "dismiss", "session_id": "s"}, "dev-1")
    assert status == 200
    assert calls == ["dismiss"]


@pytest.mark.asyncio
async def test_unchosen_action_is_404_even_with_a_lease(server, monkeypatch):
    srv, _daemon = server
    relay.create_channel("dev-1")

    async def fake_lan_run(action, payload, device_id=""):  # pragma: no cover - must not run
        raise AssertionError("unchosen action reached _lan_run")

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "wrap_up", "session_id": "s"}, "dev-1")
    assert status == 404


# --- reads survive expiry -----------------------------------------------------


@pytest.mark.asyncio
async def test_state_still_served_lapsed(server, store):
    srv, _daemon = server
    relay.create_channel("dev-1")
    _lapse(store, "dev-1")
    status, ctype, body = await srv._remote_run("state", {}, "dev-1")
    assert status == 200
    payload = json.loads(body)
    assert "agents" in payload


@pytest.mark.asyncio
async def test_usage_still_served_lapsed(server, store):
    srv, _daemon = server
    relay.create_channel("dev-1")
    _lapse(store, "dev-1")
    status, _ctype, body = await srv._remote_run(
        "usage", {"query": "window=session"}, "dev-1")
    assert status == 200
    payload = json.loads(body)
    assert payload["window"] == "session"


# --- the remote path never arms -----------------------------------------------


@pytest.mark.asyncio
async def test_a_remote_frame_never_extends_a_lease(server, monkeypatch):
    srv, _daemon = server
    relay.create_channel("dev-1")
    data = json.loads(paths.RELAY_PATH.read_text())
    data["channels"]["dev-1"]["lease_expires_at"] = time.time() + 120
    paths.RELAY_PATH.write_text(json.dumps(data))
    relay.invalidate()
    held = relay.lease_expires_at("dev-1")

    async def fake_lan_run(action, payload, device_id=""):
        return 200, "application/json", b"{}"

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    await srv._remote_run("state", {}, "dev-1")
    await srv._remote_run("action", {"action": "dismiss"}, "dev-1")
    assert relay.lease_expires_at("dev-1") == held


# --- a verified home frame arms; a refused one does not -----------------------


class _Req:
    def __init__(self, headers, body=b""):
        self.method = "POST"
        self.path = "/api/home"
        self.query = ""
        self.headers = headers
        self.body = body


def _home_request(key: bytes, kind: str, body: dict, ctr: int) -> _Req:
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, kind, body,
                            ns=relay.HOME)
    return _Req({"x-bob-channel": relay.channel_id(key, ns=relay.HOME)},
                wire.encode("ascii"))


def _paired_with_home_key():
    code = devices.begin(allow_plain=True)
    token, device_id, detail = devices.redeem(code, "phone")
    assert token, detail
    relay.create_channel(device_id)
    key = devices.home_key(device_id)
    assert key is not None
    return device_id, key


@pytest.mark.asyncio
async def test_a_verified_home_frame_arms_the_lease(server, store):
    srv, _daemon = server
    device_id, key = _paired_with_home_key()
    _lapse(store, device_id)
    assert relay.lease_valid(device_id) is False
    status, ctype, body = await srv._lan_home(
        _home_request(key, "state", {}, 1))
    assert status == 200 and ctype == "text/plain"
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE,
                                  body.decode(), 0, ns=relay.HOME)
    assert err == "" and frame["body"]["status"] == 200
    assert relay.lease_valid(device_id) is True


@pytest.mark.asyncio
async def test_a_refused_home_frame_arms_nothing(server, store):
    """`note_lan_proof` sits after the successful open, never before."""
    srv, _daemon = server
    device_id, key = _paired_with_home_key()
    _lapse(store, device_id)
    # The right channel header, the wrong key: the seal fails.
    wire = relay.seal_frame(relay.mint_key(), relay.DIR_PHONE_TO_MAC, 1,
                            "state", {}, ns=relay.HOME)
    req = _Req({"x-bob-channel": relay.channel_id(key, ns=relay.HOME)},
               wire.encode("ascii"))
    status, _ctype, body = await srv._lan_home(req)
    assert status == 403
    assert json.loads(body)["error"] == relay.REFUSAL_WORDS["seal"]
    assert relay.lease_valid(device_id) is False
    # A replayed counter is refused sealed, and arms nothing either.
    await srv._lan_home(_home_request(key, "state", {}, 3))
    _lapse(store, device_id)
    status, ctype, body = await srv._lan_home(
        _home_request(key, "state", {}, 3))
    assert status == 200 and ctype == "text/plain"
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE,
                                  body.decode(), 0, ns=relay.HOME)
    assert err == "" and frame["kind"] == "err"
    assert frame["body"]["ctr_expected"] == 4
    assert relay.lease_valid(device_id) is False


# --- revocation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpair_deletes_channel_and_next_frame_fails(server, store):
    _srv, daemon = server
    code = devices.begin(allow_plain=True)
    token, device_id, detail = devices.redeem(code, "phone")
    assert token, detail
    relay.create_channel(device_id)
    key = relay.channel_key(device_id)
    assert key is not None
    ok, _detail = daemon.unpair_device(device_id)
    assert ok is True
    # The connector re-reads the key per frame; a deleted key is the next
    # frame refused, with nothing to even open it against.
    assert relay.channel_key(device_id) is None
    assert relay.lease_valid(device_id) is False
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "state", {})
    fresh = relay.channel_key(device_id)
    assert fresh is None  # fails closed before open_frame is even possible
    assert "channels" in json.loads(store.read_text())


# --- the push token: rides the channel entry, dies with it ---------------------


def test_note_push_token_round_trips(store):
    relay.create_channel("phone-1")
    assert relay.push_token("phone-1") is None
    assert relay.push_env("phone-1") == "prod"
    assert relay.note_push_token("phone-1", "ab" * 32, "dev") is True
    assert relay.push_token("phone-1") == "ab" * 32
    assert relay.push_env("phone-1") == "dev"
    # And it survives a cache drop — it is on disk, not in memory.
    relay.invalidate()
    assert relay.push_token("phone-1") == "ab" * 32


def test_an_empty_token_deletes_all_three_keys(store):
    relay.create_channel("phone-1")
    relay.note_push_token("phone-1", "ab" * 32, "prod")
    assert relay.note_push_token("phone-1", "", "") is True
    assert relay.push_token("phone-1") is None
    entry = json.loads(store.read_text())["channels"]["phone-1"]
    for key in ("push_token", "push_env", "push_updated_at"):
        assert key not in entry


def test_no_channel_refuses_the_token(store):
    """A phone paired before the away path existed has no channel entry —
    there is nothing to hang a buzz on, and no orphan entry is minted."""
    assert relay.note_push_token("phone-9", "ab" * 32, "prod") is False
    if store.exists():
        assert json.loads(store.read_text()).get("channels", {}) == {}


def test_forget_takes_the_push_token_with_it(store):
    """Un-pairing is the push kill switch too: the token lives on the channel
    entry, and `forget` deletes the entry whole."""
    relay.create_channel("phone-1")
    relay.note_push_token("phone-1", "cd" * 32, "prod")
    relay.forget("phone-1")
    assert relay.push_token("phone-1") is None
    assert relay.channel_key("phone-1") is None
    assert "phone-1" not in json.loads(store.read_text())["channels"]


def test_a_bad_env_reads_back_as_prod(store):
    """The store carries what it was handed (forward compatibility), but the
    reader never answers with a host name the connector cannot use."""
    relay.create_channel("phone-1")
    relay.note_push_token("phone-1", "ab" * 32, "sandbox2")
    assert relay.push_env("phone-1") == "prod"


# --- the activity token: the push token's twin, same entry, same rules ---------


def test_activity_shape_round_trips_and_an_older_entry_reads_one(store):
    """Shape rides the channel entry beside the token. An entry that never
    stored it — an older build — reads 1, an off-list value reads 1, and
    an empty token pops the key with the other three."""
    relay.create_channel("phone-1")
    assert relay.activity_shape("phone-1") == 1
    assert relay.note_activity_token("phone-1", "ab" * 32, "dev", 2) is True
    assert relay.activity_shape("phone-1") == 2
    relay.invalidate()
    assert relay.activity_shape("phone-1") == 2
    data = json.loads(store.read_text())
    data["channels"]["phone-1"].pop("activity_shape")
    store.write_text(json.dumps(data))
    relay.invalidate()
    assert relay.activity_shape("phone-1") == 1
    data = json.loads(store.read_text())
    data["channels"]["phone-1"]["activity_shape"] = 9
    store.write_text(json.dumps(data))
    relay.invalidate()
    assert relay.activity_shape("phone-1") == 1
    assert relay.note_activity_token("phone-1", "", "") is True
    entry = json.loads(store.read_text())["channels"]["phone-1"]
    assert "activity_shape" not in entry
    assert "activity_token" not in entry


def test_note_activity_token_round_trips(store):
    relay.create_channel("phone-1")
    assert relay.activity_token("phone-1") is None
    assert relay.activity_env("phone-1") == "prod"
    assert relay.note_activity_token("phone-1", "ab" * 32, "dev") is True
    assert relay.activity_token("phone-1") == "ab" * 32
    assert relay.activity_env("phone-1") == "dev"
    relay.invalidate()
    assert relay.activity_token("phone-1") == "ab" * 32
    # Its own keys: registering an activity token records no push token.
    assert relay.push_token("phone-1") is None


def test_an_empty_activity_token_deletes_all_three_keys(store):
    relay.create_channel("phone-1")
    relay.note_activity_token("phone-1", "ab" * 32, "prod")
    assert relay.note_activity_token("phone-1", "", "") is True
    assert relay.activity_token("phone-1") is None
    entry = json.loads(store.read_text())["channels"]["phone-1"]
    for key in ("activity_token", "activity_env", "activity_updated_at"):
        assert key not in entry


def test_no_channel_refuses_the_activity_token(store):
    assert relay.note_activity_token("phone-9", "ab" * 32, "prod") is False
    if store.exists():
        assert json.loads(store.read_text()).get("channels", {}) == {}


def test_forget_takes_the_activity_token_with_it(store):
    relay.create_channel("phone-1")
    relay.note_activity_token("phone-1", "cd" * 32, "prod")
    relay.forget("phone-1")
    assert relay.activity_token("phone-1") is None
    assert "phone-1" not in json.loads(store.read_text())["channels"]


def test_a_bad_activity_env_reads_back_as_prod(store):
    relay.create_channel("phone-1")
    relay.note_activity_token("phone-1", "ab" * 32, "sandbox2")
    assert relay.activity_env("phone-1") == "prod"


def test_the_devices_snapshot_never_carries_the_activity_token(store):
    """`devices.snapshot()` reads its own ledger and never the relay's; the
    daemon's `devices_snapshot` publishes a bool (`test_live_activity.py`)."""
    from dark_army_daemon import devices
    relay.create_channel("phone-1")
    relay.note_activity_token("phone-1", "ef" * 32, "prod")
    assert "ef" * 32 not in json.dumps(devices.snapshot(), default=str)


# --- the grant: how long one check-in at home buys -----------------------------


def test_a_fresh_pairing_starts_on_the_default_grant(store):
    """Today's behaviour, pinned: one day, armed at pairing."""
    relay.create_channel("dev-1")
    assert relay.lease_days("dev-1") == relay.LEASE_DEFAULT_DAYS
    assert abs(relay.lease_expires_at("dev-1")
               - (time.time() + relay.RELAY_LEASE_SECONDS)) < 5


def test_a_granted_length_is_what_coming_home_arms(store):
    relay.create_channel("dev-1")
    ok, detail = relay.set_lease_days("dev-1", 7)
    assert ok, detail
    _lapse(store, "dev-1")
    relay.note_lan_proof("dev-1")
    expires = relay.lease_expires_at("dev-1")
    assert abs(expires - (time.time() + 7 * relay.RELAY_LEASE_SECONDS)) < 5


def test_setting_a_longer_grant_never_extends_the_running_window(store):
    """The whole safety case: the setter may only clamp down. Lengthening
    waits for `note_lan_proof`, which is the one site that may move an
    expiry forward."""
    relay.create_channel("dev-1")
    held = relay.lease_expires_at("dev-1")
    assert relay.set_lease_days("dev-1", 14)[0] is True
    assert relay.lease_expires_at("dev-1") == held
    # Coming home is what applies it.
    _lapse(store, "dev-1")
    relay.note_lan_proof("dev-1")
    assert abs(relay.lease_expires_at("dev-1")
               - (time.time() + 14 * relay.RELAY_LEASE_SECONDS)) < 5


def test_setting_a_shorter_grant_bites_at_once(store):
    relay.create_channel("dev-1")
    relay.set_lease_days("dev-1", 14)
    _lapse(store, "dev-1")
    relay.note_lan_proof("dev-1")
    assert relay.lease_expires_at("dev-1") > time.time() + 13 * 86400
    assert relay.set_lease_days("dev-1", 1)[0] is True
    assert relay.lease_expires_at("dev-1") <= time.time() + relay.RELAY_LEASE_SECONDS
    assert relay.lease_valid("dev-1") is True


def test_ending_away_access_closes_the_window_and_home_does_not_reopen_it(store):
    relay.create_channel("dev-1")
    assert relay.set_lease_days("dev-1", 0)[0] is True
    assert relay.lease_expires_at("dev-1") == 0.0
    assert relay.lease_valid("dev-1") is False
    assert relay.lease_days("dev-1") == 0
    relay.note_lan_proof("dev-1")
    assert relay.lease_expires_at("dev-1") == 0.0
    assert relay.lease_valid("dev-1") is False


@pytest.mark.parametrize("days", [2, -1, 15, "7", True, 1.0, None])
def test_an_off_list_length_is_refused_in_words(store, days):
    relay.create_channel("dev-1")
    held = relay.lease_expires_at("dev-1")
    ok, detail = relay.set_lease_days("dev-1", days)
    assert ok is False and detail
    assert relay.lease_days("dev-1") == relay.LEASE_DEFAULT_DAYS
    assert relay.lease_expires_at("dev-1") == held


def test_an_unpaired_device_is_refused(store):
    ok, detail = relay.set_lease_days("nobody", 7)
    assert ok is False and detail


def test_a_channel_written_before_grants_reads_as_the_default(store):
    """The upgrade case: no key must mean today's day, never "off"."""
    relay.create_channel("dev-1")
    data = json.loads(store.read_text())
    del data["channels"]["dev-1"]["lease_days"]
    store.write_text(json.dumps(data))
    relay.invalidate()
    assert relay.lease_days("dev-1") == relay.LEASE_DEFAULT_DAYS
    _lapse(store, "dev-1")
    relay.note_lan_proof("dev-1")
    assert abs(relay.lease_expires_at("dev-1")
               - (time.time() + relay.RELAY_LEASE_SECONDS)) < 5


def test_forget_takes_the_grant_with_it(store):
    relay.create_channel("dev-1")
    relay.set_lease_days("dev-1", 7)
    relay.forget("dev-1")
    assert relay.lease_days("dev-1") == 0
    assert relay.set_lease_days("dev-1", 7)[0] is False


@pytest.mark.asyncio
async def test_an_ended_grant_still_serves_reads_and_refuses_writes(
        server, monkeypatch):
    """Expiry bounds doing, never seeing — at zero days as at any other."""
    srv, _daemon = server
    relay.create_channel("dev-1")
    relay.set_lease_days("dev-1", 0)

    async def fake_lan_run(action, payload, device_id=""):  # pragma: no cover
        raise AssertionError("a write ran on an ended grant")

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    status, _ctype, body = await srv._remote_run(
        "action", {"action": "dismiss", "session_id": "s"}, "dev-1")
    assert status == 403
    assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
    status, _ctype, body = await srv._remote_run("state", {}, "dev-1")
    assert status == 200 and "agents" in json.loads(body)


# --- the verb: loopback only --------------------------------------------------


class _LoopbackReq:
    def __init__(self, payload, token):
        self.method = "POST"
        self.path = "/api/action"
        self.query = ""
        self.headers = {"x-bob-token": token}
        self.body = json.dumps(payload).encode()

    def json(self):
        return json.loads(self.body)


@pytest.mark.asyncio
async def test_the_loopback_verb_changes_the_grant(server, monkeypatch):
    srv, _daemon = server
    relay.create_channel("dev-1")
    monkeypatch.setattr(srv, "_authorised", lambda request: True)
    parsed = srv._devices_request(
        _LoopbackReq({"action": "set_away_days", "device_id": "dev-1",
                      "days": 7}, "t"))
    assert parsed is not None
    status, _ctype, body = await srv._devices(*parsed)
    assert status == 200 and json.loads(body)["ok"] is True
    assert relay.lease_days("dev-1") == 7


@pytest.mark.asyncio
async def test_the_loopback_verb_refuses_a_bool(server, monkeypatch):
    """`isinstance(True, int)` is True, and `{"days": true}` meant something
    other than a one-day grant."""
    srv, _daemon = server
    relay.create_channel("dev-1")
    relay.set_lease_days("dev-1", 7)
    monkeypatch.setattr(srv, "_authorised", lambda request: True)
    parsed = srv._devices_request(
        _LoopbackReq({"action": "set_away_days", "device_id": "dev-1",
                      "days": True}, "t"))
    status, _ctype, body = await srv._devices(*parsed)
    assert status == 409 and json.loads(body)["ok"] is False
    assert relay.lease_days("dev-1") == 7


@pytest.mark.asyncio
async def test_neither_door_carries_the_verb(server, monkeypatch):
    srv, _daemon = server
    assert "set_away_days" not in ApiServer.LAN_ACTIONS
    assert "set_away_days" not in ApiServer.REMOTE_ACTIONS
    relay.create_channel("dev-1")
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "set_away_days", "device_id": "dev-1",
                   "days": 14}, "dev-1")
    assert status == 404
    status, _ctype, _body = await srv._sealed_run(
        "action", {"action": "set_away_days", "device_id": "dev-1",
                   "days": 14}, "dev-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 404
    assert relay.lease_days("dev-1") == relay.LEASE_DEFAULT_DAYS


@pytest.mark.asyncio
async def test_set_relay_ws_is_loopback_only_and_refuses_a_plain_address(
        server, monkeypatch):
    """`set_relay_ws` — the socket relay's address — is `set_relay`'s
    sibling on `_devices_request`'s loopback allow-list: on neither
    `LAN_ACTIONS` nor `REMOTE_ACTIONS`, and a non-`wss://` address is
    refused in `WS_URL_REFUSAL`'s words, shown verbatim by the sheet."""
    srv, _daemon = server
    assert "set_relay_ws" not in ApiServer.LAN_ACTIONS
    assert "set_relay_ws" not in ApiServer.REMOTE_ACTIONS
    relay.create_channel("dev-1")
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "set_relay_ws", "url": "wss://r.example"}, "dev-1")
    assert status == 404
    status, _ctype, _body = await srv._sealed_run(
        "action", {"action": "set_relay_ws", "url": "wss://r.example"}, "dev-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 404
    assert relay.get_ws_url() == ""
    monkeypatch.setattr(srv, "_authorised", lambda request: True)
    parsed = srv._devices_request(
        _LoopbackReq({"action": "set_relay_ws", "url": "ws://r.example"}, "t"))
    assert parsed is not None
    status, _ctype, body = await srv._devices(*parsed)
    assert status == 409
    assert json.loads(body) == {"ok": False, "detail": relay.WS_URL_REFUSAL}
    assert relay.get_ws_url() == ""
    parsed = srv._devices_request(
        _LoopbackReq({"action": "set_relay_ws", "url": "wss://r.example/"}, "t"))
    status, _ctype, body = await srv._devices(*parsed)
    assert status == 200 and json.loads(body)["ok"] is True
    assert relay.get_ws_url() == "wss://r.example"
    # The mailbox address is its own key and untouched.
    assert relay.get_url() == ""


@pytest.mark.asyncio
async def test_a_socket_address_pasted_with_the_endpoint_stores_without_it(
        server, monkeypatch):
    """The connector appends `/ws` itself; an address pasted with the
    endpoint on it would have the Mac dial `/ws/ws` (a relay 400) while
    the phone tolerates either. Normalised once, at the store."""
    srv, _daemon = server
    for pasted, stored in (
            ("wss://host/ws/", "wss://host"),
            ("wss://host/ws", "wss://host"),
            ("wss://host/relay/ws/", "wss://host/relay"),
            ("wss://host:8443/ws", "wss://host:8443"),
            # A host that happens to be named `ws` is not a path segment.
            ("wss://ws", "wss://ws"),
            ("wss://host/wsx", "wss://host/wsx")):
        ok, detail = relay.set_ws_url(pasted)
        assert ok, detail
        assert relay.get_ws_url() == stored, pasted
    monkeypatch.setattr(srv, "_authorised", lambda request: True)
    parsed = srv._devices_request(
        _LoopbackReq({"action": "set_relay_ws", "url": "wss://r.example/ws/"},
                     "t"))
    status, _ctype, body = await srv._devices(*parsed)
    assert status == 200 and json.loads(body)["ok"] is True
    assert relay.get_ws_url() == "wss://r.example"
    assert relay.set_ws_url("")[0] and relay.get_ws_url() == ""


def test_the_snapshot_carries_the_grant_and_no_secret(server):
    _srv, daemon = server
    relay.create_channel("dev-1")
    relay.set_lease_days("dev-1", 7)
    snap = daemon.devices_snapshot()
    text = json.dumps(snap)
    assert "key_b64" not in text and "home_key" not in text
    key = relay.channel_key("dev-1")
    assert key is not None
    assert relay.channel_id(key) not in text


# --- the arming stays one site ------------------------------------------------


def test_exactly_one_arming_site():
    import pathlib
    api = pathlib.Path(__file__).resolve().parents[1] / (
        "dark_army_daemon/api_server.py")
    assert api.read_text().count("note_lan_proof(") == 1
    src = pathlib.Path(__file__).resolve().parents[1] / (
        "dark_army_daemon/relay.py")
    body = src.read_text()
    # Two writers of the expiry outside `create_channel`: the setter, which
    # may only clamp down, and `note_lan_proof`, which is the only thing that
    # may move it forward.
    assert body.count('["lease_expires_at"] =') == 3
