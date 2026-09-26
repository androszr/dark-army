# host/tests/test_bot_access.py
"""The bot's access is two grants, not the day lease.

The headless device (`devices.is_headless`) reads while its Read grant is on
and acts while its Write grant is on; each is off, timed from the moment it
was chosen, or on with no timer (`relay.bot_grant`). `_sealed_run` checks
both on both doors, a phone keeps its day lease exactly, and the verb that
moves a grant (`set_bot_access`) refuses the bot itself by identity and any
target that is not the bot. `docs/transport-contract.md`, *The bot's access
is two grants, not the day lease*.
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
    monkeypatch.setattr(paths, "BOT_PAIR_PATH",
                        tmp_path / "state" / "grok-bot.json")
    monkeypatch.setattr(paths, "BOT_MCP_TOKEN_PATH",
                        tmp_path / "state" / "grok-bot-mcp-token")
    relay.reset()
    devices.reset()
    yield tmp_path / "relay.json"
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


def _bot(daemon) -> str:
    """Pair the bot the way the desk does and return its device id."""
    _arm(daemon)
    result = daemon.pair_bot("Grok")
    assert result["ok"] is True
    return result["device_id"]


def _lapse(store_path, device_id: str) -> None:
    """Wind one device's day lease into the past, as time passing would."""
    data = json.loads(store_path.read_text())
    data["channels"][device_id]["lease_expires_at"] = time.time() - 60
    store_path.write_text(json.dumps(data))
    relay.invalidate()


def _strip_grants(store_path, device_id: str) -> None:
    """A channel written by a build from before the grants existed."""
    data = json.loads(store_path.read_text())
    entry = data["channels"][device_id]
    for key in ("bot_read_mode", "bot_read_until",
                "bot_write_mode", "bot_write_until"):
        entry.pop(key, None)
    store_path.write_text(json.dumps(data))
    relay.invalidate()


def _set_raw(store_path, device_id: str, **fields) -> None:
    data = json.loads(store_path.read_text())
    data["channels"][device_id].update(fields)
    store_path.write_text(json.dumps(data))
    relay.invalidate()


def _fake_lan_run(srv, monkeypatch) -> list:
    calls = []

    async def fake(action, payload, device_id=""):
        calls.append(action)
        return 200, "application/json", b"{}"

    monkeypatch.setattr(srv, "_lan_run", fake)
    return calls


# --- 1. the store ------------------------------------------------------------


@pytest.mark.parametrize("side", relay.BOT_ACCESS_SIDES)
@pytest.mark.parametrize("mode", relay.BOT_ACCESS_MODES)
def test_every_mode_round_trips_on_every_side(store, side, mode):
    relay.create_channel("bot")
    ok, detail = relay.set_bot_access("bot", side, mode)
    assert ok is True and detail == ""
    got, until = relay.bot_grant("bot", side)
    assert got == mode
    if mode in relay.BOT_ACCESS_SECONDS:
        want = time.time() + relay.BOT_ACCESS_SECONDS[mode]
        assert abs(until - want) < 5
    else:
        assert until == 0.0
    assert relay.bot_grant_valid("bot", side) is (mode != "off")


def test_choosing_the_same_timer_again_is_a_refresh(store):
    relay.create_channel("bot")
    relay.set_bot_access("bot", "write", "1h")
    _set_raw(store, "bot", bot_write_until=time.time() + 60)
    first = relay.bot_grant("bot", "write")[1]
    relay.set_bot_access("bot", "write", "1h")
    second = relay.bot_grant("bot", "write")[1]
    assert second > first
    assert abs(second - (time.time() + 3600)) < 5


def test_the_setter_refuses_what_it_does_not_offer(store):
    assert relay.set_bot_access("nobody", "read", "off") == (
        False, "that device is not paired")
    relay.create_channel("bot")
    assert relay.set_bot_access("bot", "read", "3d") == (
        False, relay.BOT_ACCESS_MODE_REFUSAL)
    assert relay.set_bot_access("bot", "admin", "off") == (
        False, relay.BOT_ACCESS_MODE_REFUSAL)
    assert relay.set_bot_access("bot", "read", True) == (
        False, relay.BOT_ACCESS_MODE_REFUSAL)


def test_a_lapsed_timer_reads_as_off(store):
    relay.create_channel("bot")
    relay.set_bot_access("bot", "read", "6h")
    _set_raw(store, "bot", bot_read_until=time.time() - 1)
    assert relay.bot_grant("bot", "read") == ("off", 0.0)
    assert relay.bot_grant_valid("bot", "read") is False


# --- 2 and 3. absent and unknown mean opposite things ------------------------


def test_a_channel_from_before_the_grants_keeps_todays_behaviour(store):
    relay.create_channel("bot")
    _strip_grants(store, "bot")
    assert relay.bot_grant("bot", "read") == ("forever", 0.0)
    mode, until = relay.bot_grant("bot", "write")
    assert mode == "24h"
    assert until == relay.lease_expires_at("bot")
    _lapse(store, "bot")
    assert relay.bot_grant("bot", "write") == ("off", 0.0)
    # Reading never depended on the day lease, and still does not.
    assert relay.bot_grant("bot", "read") == ("forever", 0.0)


def test_a_newer_builds_word_reads_as_off(store):
    relay.create_channel("bot")
    _set_raw(store, "bot", bot_read_mode="3d",
             bot_read_until=time.time() + 999_999,
             bot_write_mode=7, bot_write_until=time.time() + 999_999)
    assert relay.bot_grant("bot", "read") == ("off", 0.0)
    assert relay.bot_grant("bot", "write") == ("off", 0.0)


def test_no_channel_is_off(store):
    assert relay.bot_grant("nobody", "read") == ("off", 0.0)
    assert relay.bot_grant("nobody", "write") == ("off", 0.0)


# --- 4. defaults at pairing ---------------------------------------------------


def test_a_fresh_bot_reads_without_a_timer_and_acts_for_a_day(server):
    _srv, daemon = server
    bot = _bot(daemon)
    assert devices.is_headless(bot) is True
    assert relay.bot_grant(bot, "read") == ("forever", 0.0)
    mode, until = relay.bot_grant(bot, "write")
    assert mode == "24h"
    assert abs(until - (time.time() + 86400)) < 5


def test_is_headless_is_the_bot_alone(server):
    _srv, daemon = server
    token, phone_id, _detail = devices.redeem(devices.begin(), "iPhone")
    assert token
    bot = _bot(daemon)
    assert devices.is_headless(bot) is True
    assert devices.is_headless(phone_id) is False
    assert devices.is_headless("") is False
    assert devices.is_headless("nobody") is False


# --- 5. the read gate, both doors ---------------------------------------------


@pytest.mark.asyncio
async def test_read_off_refuses_the_picture_on_both_doors(server):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "read", "off")[0]
    status, _ctype, body = await srv._remote_run("state", {}, bot)
    assert status == 403
    assert json.loads(body)["detail"] == relay.BOT_READ_REFUSAL
    status, _ctype, body = await srv._sealed_run(
        "state", {}, bot, actions=ApiServer.LAN_ACTIONS,
        check_lease=False, record=False)
    assert status == 403
    assert json.loads(body)["error"] == relay.BOT_READ_REFUSAL


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["plans", "bearings", "card", "log",
                                  "terminal", "scout_reports", "usage"])
async def test_read_off_refuses_every_read_kind(server, kind):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "read", "off")[0]
    status, _ctype, body = await srv._remote_run(kind, {}, bot)
    assert status == 403
    assert json.loads(body)["detail"] == relay.BOT_READ_REFUSAL


@pytest.mark.asyncio
async def test_read_on_answers_the_picture(server):
    srv, daemon = server
    bot = _bot(daemon)
    status, _ctype, _body = await srv._remote_run("state", {}, bot)
    assert status == 200
    assert relay.set_bot_access(bot, "read", "1h")[0]
    status, _ctype, _body = await srv._remote_run("state", {}, bot)
    assert status == 200


@pytest.mark.asyncio
async def test_read_off_does_not_close_the_write_side(server, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    calls = _fake_lan_run(srv, monkeypatch)
    assert relay.set_bot_access(bot, "read", "off")[0]
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "dismiss", "session_id": "s"}, bot)
    assert status == 200 and calls == ["dismiss"]


# --- 6. the write gate, and the phone untouched ------------------------------


@pytest.mark.asyncio
async def test_write_off_refuses_the_action_even_inside_a_day_lease(
        server, store, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "write", "off")[0]
    # A day lease the old rule would have honoured.
    _set_raw(store, bot, lease_expires_at=time.time() + 3600, lease_days=1)
    assert relay.lease_valid(bot) is True
    calls = _fake_lan_run(srv, monkeypatch)
    for run in (
            lambda p: srv._remote_run("action", p, bot),
            lambda p: srv._sealed_run(
                "action", p, bot, actions=ApiServer.LAN_ACTIONS,
                check_lease=False, record=False)):
        status, _ctype, body = await run(
            {"action": "stop_session", "session_id": "s"})
        assert status == 403
        assert json.loads(body)["detail"] == relay.BOT_WRITE_REFUSAL
    assert calls == []


@pytest.mark.asyncio
async def test_write_on_ignores_a_lapsed_day_lease(server, store, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "write", "1h")[0]
    _lapse(store, bot)
    assert relay.lease_valid(bot) is False
    calls = _fake_lan_run(srv, monkeypatch)
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "stop_session", "session_id": "s"}, bot)
    assert status == 200 and calls == ["stop_session"]


@pytest.mark.asyncio
async def test_write_off_keeps_the_unchosen_verb_a_404(server, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "write", "off")[0]
    calls = _fake_lan_run(srv, monkeypatch)
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "set_away_days", "days": 14}, bot)
    assert status == 404 and calls == []


@pytest.mark.asyncio
async def test_phone_with_a_lapsed_lease_is_still_refused_in_lease_words(
        server, store, monkeypatch):
    srv, daemon = server
    _bot(daemon)
    relay.create_channel("phone-1")
    _lapse(store, "phone-1")
    calls = _fake_lan_run(srv, monkeypatch)
    status, _ctype, body = await srv._remote_run(
        "action", {"action": "stop_session", "session_id": "s"}, "phone-1")
    assert status == 403
    assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
    assert calls == []


@pytest.mark.asyncio
async def test_phone_reads_are_untouched_by_the_bots_grants(server, store):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "read", "off")[0]
    relay.create_channel("phone-1")
    _lapse(store, "phone-1")
    status, _ctype, _body = await srv._remote_run("state", {}, "phone-1")
    assert status == 200
    status, _ctype, _body = await srv._sealed_run(
        "state", {}, "phone-1", actions=ApiServer.LAN_ACTIONS,
        check_lease=False, record=False)
    assert status == 200


@pytest.mark.asyncio
async def test_phone_at_home_still_writes_with_no_lease(
        server, store, monkeypatch):
    srv, daemon = server
    _bot(daemon)
    relay.create_channel("phone-1")
    _lapse(store, "phone-1")
    calls = _fake_lan_run(srv, monkeypatch)
    status, _ctype, _body = await srv._sealed_run(
        "action", {"action": "dismiss", "session_id": "s"}, "phone-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 200 and calls == ["dismiss"]


# --- 7. the verb, on three doors ----------------------------------------------


def test_the_verb_is_on_both_phone_tuples():
    assert "set_bot_access" in ApiServer.LAN_ACTIONS
    assert "set_bot_access" in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_a_phone_away_sets_the_bots_grant_and_it_is_listed(server):
    srv, daemon = server
    bot = _bot(daemon)
    relay.create_channel("phone-1")
    status, _ctype, body = await srv._remote_run("action", {
        "action": "set_bot_access", "device_id": bot,
        "side": "read", "mode": "6h"}, "phone-1")
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert relay.bot_grant(bot, "read")[0] == "6h"
    last = list(daemon._remote_activity)[-1]
    assert last["action"] == "set_bot_access"
    assert last["device_id"] == "phone-1"
    # Filed once, by the away door, never a second time by the method.
    assert len(daemon._remote_activity) == 1


@pytest.mark.asyncio
async def test_a_phone_at_home_sets_the_grant_and_nothing_is_listed(server):
    srv, daemon = server
    bot = _bot(daemon)
    relay.create_channel("phone-1")
    status, _ctype, body = await srv._sealed_run("action", {
        "action": "set_bot_access", "device_id": bot,
        "side": "write", "mode": "forever"}, "phone-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 200, body
    assert relay.bot_grant(bot, "write") == ("forever", 0.0)
    assert list(daemon._remote_activity) == []


@pytest.mark.asyncio
async def test_the_bot_may_not_change_its_own_access(server):
    srv, daemon = server
    bot = _bot(daemon)
    before = relay.bot_grant(bot, "write")
    for run in (
            lambda p: srv._remote_run("action", p, bot),
            lambda p: srv._sealed_run(
                "action", p, bot, actions=ApiServer.LAN_ACTIONS,
                check_lease=False, record=False)):
        status, _ctype, body = await run({
            "action": "set_bot_access", "device_id": bot,
            "side": "write", "mode": "forever"})
        assert status == 409
        assert json.loads(body)["detail"] == relay.BOT_ACCESS_SELF_REFUSAL
    assert relay.bot_grant(bot, "write") == before


def test_the_bot_is_refused_by_identity_whatever_it_aims_at(server):
    _srv, daemon = server
    bot = _bot(daemon)
    relay.create_channel("phone-1")
    assert daemon.set_bot_access(
        "phone-1", "write", "forever", requester=bot) == (
        False, relay.BOT_ACCESS_SELF_REFUSAL)


@pytest.mark.asyncio
async def test_a_phone_may_not_aim_the_verb_at_a_phone(server):
    srv, daemon = server
    _bot(daemon)
    relay.create_channel("phone-1")
    relay.create_channel("phone-2")
    relay.set_lease_days("phone-2", 3)
    status, _ctype, body = await srv._remote_run("action", {
        "action": "set_bot_access", "device_id": "phone-2",
        "side": "write", "mode": "off"}, "phone-1")
    assert status == 409
    assert json.loads(body)["detail"] == relay.BOT_ACCESS_TARGET_REFUSAL
    assert relay.lease_days("phone-2") == 3
    store_entry = relay.load()["channels"]["phone-2"]
    assert "bot_write_mode" not in store_entry


class _LoopbackReq:
    def __init__(self, payload):
        self.method = "POST"
        self.path = "/api/action"
        self.query = ""
        self.headers = {"x-bob-token": "t"}
        self.body = json.dumps(payload).encode()

    def json(self):
        return json.loads(self.body)


@pytest.mark.asyncio
async def test_the_desk_sets_the_grant_on_loopback_and_it_is_listed(
        server, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    monkeypatch.setattr(srv, "_authorised", lambda request: False)
    assert srv._devices_request(_LoopbackReq({
        "action": "set_bot_access", "device_id": bot,
        "side": "read", "mode": "off"})) is None
    monkeypatch.setattr(srv, "_authorised", lambda request: True)
    parsed = srv._devices_request(_LoopbackReq({
        "action": "set_bot_access", "device_id": bot,
        "side": "read", "mode": "off"}))
    assert parsed is not None
    status, _ctype, body = await srv._devices(*parsed)
    assert status == 200 and json.loads(body)["ok"] is True
    assert relay.bot_grant(bot, "read") == ("off", 0.0)
    last = list(daemon._remote_activity)[-1]
    assert last["device_id"] == "desk"
    assert last["action"] == "set_bot_access"


@pytest.mark.asyncio
async def test_a_non_string_mode_is_refused_on_every_door(server):
    srv, daemon = server
    bot = _bot(daemon)
    relay.create_channel("phone-1")
    status, _ctype, body = await srv._devices("set_bot_access", {
        "device_id": bot, "side": "read", "mode": 1})
    assert status == 409
    assert json.loads(body)["detail"] == relay.BOT_ACCESS_MODE_REFUSAL
    status, _ctype, _body = await srv._lan_run("set_bot_access", {
        "device_id": bot, "side": "read", "mode": None},
        device_id="phone-1")
    assert status == 400
    status, _ctype, body = await srv._devices("set_bot_access", {
        "device_id": bot, "side": "read", "mode": "3d"})
    assert status == 409
    assert json.loads(body)["detail"] == relay.BOT_ACCESS_MODE_REFUSAL
    assert relay.bot_grant(bot, "read") == ("forever", 0.0)


# --- 8. the snapshot ----------------------------------------------------------


def test_the_snapshot_carries_the_bots_grants_and_no_secret(server):
    _srv, daemon = server
    token, phone_id, _detail = devices.redeem(devices.begin(), "iPhone")
    assert token
    relay.create_channel(phone_id)
    bot = _bot(daemon)
    relay.set_bot_access(bot, "write", "6h")
    snap = daemon.devices_snapshot()
    rows = {row["id"]: row for row in snap["devices"]}
    access = rows[bot]["bot_access"]
    assert access["read"] == {"mode": "forever", "until": 0.0}
    assert access["write"]["mode"] == "6h"
    assert abs(access["write"]["until"] - (time.time() + 21600)) < 5
    assert "bot_access" not in rows[phone_id]
    text = json.dumps(snap)
    assert "key_b64" not in text and "home_key" not in text
    for did in (bot, phone_id):
        key = relay.channel_key(did)
        assert key is not None
        assert relay.channel_id(key) not in text
    assert "headless" not in text


# --- 9. the downgrade shape ---------------------------------------------------


def test_write_off_ends_the_day_lease_and_home_does_not_reopen_it(server):
    _srv, daemon = server
    bot = _bot(daemon)
    # Pairing already ended the bot's day lease; give it one back, as a row
    # from an older build could carry, to prove Write off ends it again.
    data = json.loads(paths.RELAY_PATH.read_text())
    data["channels"][bot].update(lease_days=3,
                                 lease_expires_at=time.time() + 3600)
    paths.RELAY_PATH.write_text(json.dumps(data))
    relay.invalidate()
    assert relay.lease_valid(bot) is True
    ok, _detail = daemon.set_bot_access(bot, "write", "off")
    assert ok is True
    assert relay.lease_days(bot) == 0
    assert relay.lease_expires_at(bot) == 0.0
    relay.note_lan_proof(bot)
    assert relay.lease_days(bot) == 0
    assert relay.lease_expires_at(bot) == 0.0


def test_the_grant_keys_ride_beside_every_other_channel_key(server):
    _srv, daemon = server
    bot = _bot(daemon)
    key_before = relay.channel_key(bot)
    relay.set_bot_access(bot, "read", "1h")
    entry = relay.load()["channels"][bot]
    assert relay.channel_key(bot) == key_before
    for key in ("key_b64", "created_at", "lease_expires_at", "lease_days",
                "send_ctr", "recv_ctr"):
        assert key in entry
    assert entry["bot_read_mode"] == "1h"


# --- repair round: the doors outside `_sealed_run` (S1) ----------------------


class _Writer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _HomeReq:
    path = "/api/home"
    peer = "127.0.0.1"
    preverified = None
    body = b"frame"

    def __init__(self):
        self.headers = {"x-bob-frame": "frame"}


def _sealed_answers(srv, monkeypatch, device_id, frame):
    """Stand the verifier and the sealer aside: `_home_open` answers with
    this device and frame, and a sealed answer is its plain JSON."""
    monkeypatch.setattr(srv, "_home_open",
                        lambda request, wire, expect_kind=None:
                        (device_id, b"k" * 32, frame))
    monkeypatch.setattr(srv, "_home_answer",
                        lambda did, key, body, kind: json.dumps(
                            dict(body, kind=kind)).encode())


@pytest.mark.asyncio
async def test_read_off_refuses_the_home_terminal_stream(server, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "read", "off")[0]
    _sealed_answers(srv, monkeypatch, bot,
                    {"id": "s1", "body": {"session": "abc"}})
    attached = []
    monkeypatch.setattr(daemon, "terminal_attach",
                        lambda sid: attached.append(sid) or (None, b"", False, "x"))
    sent = []

    async def respond(writer, status, ctype, body):
        sent.append((status, json.loads(body)))

    monkeypatch.setattr(srv, "_respond", respond)
    await srv._serve_lan_terminal_stream(_HomeReq(), None, _Writer())
    assert attached == []
    assert sent[0][1]["status"] == 403
    assert sent[0][1]["error"] == relay.BOT_READ_REFUSAL


@pytest.mark.asyncio
async def test_write_off_refuses_every_keystroke_on_the_stream(
        server, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    typed = []
    monkeypatch.setattr(daemon, "terminal_stream_input",
                        lambda handle, blob: typed.append(blob) or (True, ""))
    on_input = srv._lan_stream_input(bot, "h1")
    assert on_input(b"ls") == (True, "")
    assert relay.set_bot_access(bot, "write", "off")[0]
    assert on_input(b"rm") == (False, relay.BOT_WRITE_REFUSAL)
    assert typed == [b"ls"]
    # A phone's keystrokes are never judged by the bot's grants.
    relay.create_channel("phone-1")
    assert srv._lan_stream_input("phone-1", "h2")(b"q") == (True, "")


@pytest.mark.asyncio
async def test_write_off_refuses_the_home_upload(server, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "write", "off")[0]
    _sealed_answers(srv, monkeypatch, bot,
                    {"id": "u1", "body": {"staging": "s", "name": "a.png"}})
    opened = []
    monkeypatch.setattr(srv, "_open_blob_and_store",
                        lambda *a: opened.append(a) or (True, "a.png", "", 1))
    status, _ctype, body = await srv._lan_upload(_HomeReq())
    answer = json.loads(body)
    assert status == 200 and answer["status"] == 403
    assert json.loads(answer["body"])["detail"] == relay.BOT_WRITE_REFUSAL
    assert opened == []


@pytest.mark.asyncio
async def test_switching_a_grant_off_hangs_up_the_bots_streams(server):
    srv, daemon = server
    bot = _bot(daemon)
    relay.create_channel("phone-1")
    bot_writer, phone_writer = _Writer(), _Writer()
    srv._lan_streams = {bot: [bot_writer], "phone-1": [phone_writer]}
    status, _ctype, _body = await srv._devices("set_bot_access", {
        "device_id": bot, "side": "read", "mode": "1h"})
    assert status == 200 and bot_writer.closed is False
    status, _ctype, _body = await srv._devices("set_bot_access", {
        "device_id": bot, "side": "read", "mode": "off"})
    assert status == 200
    assert bot_writer.closed is True
    assert phone_writer.closed is False


# --- S2: the bot is known even when the ledger is not -------------------------


@pytest.mark.asyncio
async def test_an_unreadable_ledger_fails_closed(server, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "read", "off")[0]
    paths.DEVICES_PATH.write_text("{ not json")
    devices.invalidate()
    assert devices.is_headless(bot) is False
    assert devices.is_bot(bot) is True
    status, _ctype, body = await srv._remote_run("state", {}, bot)
    assert status == 403
    assert json.loads(body)["detail"] == relay.BOT_READ_REFUSAL
    # And the bot still may not change its own grants.
    assert daemon.set_bot_access(bot, "read", "forever", requester=bot) == (
        False, relay.BOT_ACCESS_SELF_REFUSAL)
    calls = _fake_lan_run(srv, monkeypatch)
    assert relay.set_bot_access(bot, "write", "off")[0]
    status, _ctype, _body = await srv._remote_run(
        "action", {"action": "dismiss", "session_id": "s"}, bot)
    assert status == 403 and calls == []


def test_a_phone_channel_carries_no_bot_grants(store):
    relay.create_channel("phone-1")
    assert relay.has_bot_grants("phone-1") is False
    assert devices.is_bot("phone-1") is False
    assert devices.is_bot("") is False


# --- S3 ------------------------------------------------------------------------


def test_a_nan_timer_reads_as_off(store):
    relay.create_channel("bot")
    relay.set_bot_access("bot", "write", "1h")
    _set_raw(store, "bot", bot_write_until=float("nan"),
             bot_read_mode="6h", bot_read_until=float("inf"))
    assert relay.bot_grant("bot", "write") == ("off", 0.0)
    assert relay.bot_grant("bot", "read") == ("off", 0.0)


# --- B1: the bot is off the day lease for good --------------------------------


def test_the_bots_own_home_frames_re_arm_nothing(server, store):
    srv, daemon = server
    bot = _bot(daemon)
    _strip_grants(store, bot)
    _set_raw(store, bot, lease_days=14, lease_expires_at=time.time() - 60)
    srv._home_admit(bot, 1, durable=False)
    assert relay.lease_valid(bot) is False


def test_a_legacy_bot_is_made_explicit_and_never_shows_a_long_24h(server, store):
    _srv, daemon = server
    bot = _bot(daemon)
    _strip_grants(store, bot)
    _set_raw(store, bot, lease_days=14,
             lease_expires_at=time.time() + 13 * 86400)
    snap = daemon.devices_snapshot()
    row = next(r for r in snap["devices"] if r["id"] == bot)
    assert row["bot_access"]["read"] == {"mode": "forever", "until": 0.0}
    write = row["bot_access"]["write"]
    assert write["mode"] == "24h"
    assert write["until"] <= time.time() + 86400 + 1
    assert relay.lease_days(bot) == 0 and relay.lease_expires_at(bot) == 0.0
    # Explicit keys now: a downgrade reads an ended lease, never a wider one.
    entry = relay.load()["channels"][bot]
    assert entry["bot_write_mode"] == "24h" and entry["bot_read_mode"] == "forever"


def test_a_legacy_bot_with_a_short_lease_keeps_what_it_had(server, store):
    _srv, daemon = server
    bot = _bot(daemon)
    _strip_grants(store, bot)
    ends = time.time() + 3600
    _set_raw(store, bot, lease_days=1, lease_expires_at=ends)
    assert relay.materialise_bot_grants(bot) == (True, "")
    mode, until = relay.bot_grant(bot, "write")
    assert mode == "24h" and abs(until - ends) < 1


def test_every_grant_change_clamps_the_lease(server, store):
    _srv, daemon = server
    bot = _bot(daemon)
    assert relay.lease_days(bot) == 0
    _set_raw(store, bot, lease_days=7, lease_expires_at=time.time() + 86400)
    assert relay.set_bot_access(bot, "write", "forever")[0]
    assert relay.lease_days(bot) == 0 and relay.lease_expires_at(bot) == 0.0
    _set_raw(store, bot, lease_days=7, lease_expires_at=time.time() + 86400)
    assert relay.set_bot_access(bot, "read", "6h")[0]
    assert relay.lease_days(bot) == 0


# --- B4: a failed save is not kept in memory ----------------------------------


def test_a_failed_save_leaves_the_grant_as_it_was(server, monkeypatch):
    _srv, daemon = server
    bot = _bot(daemon)
    assert relay.bot_grant(bot, "read") == ("forever", 0.0)
    monkeypatch.setattr(relay, "save", lambda data: False)
    ok, detail = relay.set_bot_access(bot, "read", "off")
    assert ok is False and detail
    assert relay.bot_grant(bot, "read") == ("forever", 0.0)
    assert relay.load()["channels"][bot]["bot_read_mode"] == "forever"


# --- B5: a lapsed timer is redrawn when it lapses -----------------------------


@pytest.mark.asyncio
async def test_the_earliest_running_timer_is_armed_and_redraws(
        server, store, monkeypatch):
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "read", "1h")[0]
    until = relay.bot_grant(bot, "read")[1]
    srv._devices_section()
    assert srv._bot_expiry_at == until
    assert srv._bot_expiry_handle is not None
    frames = []
    monkeypatch.setattr(srv, "_broadcast", lambda: frames.append(1))
    writer = _Writer()
    srv._lan_streams = {bot: [writer]}
    _set_raw(store, bot, bot_read_until=time.time() - 1)
    srv._bot_grant_lapsed()
    assert frames == [1]
    assert writer.closed is True
    # Nothing left running on the read side; the write side's day is.
    srv._devices_section()
    assert srv._bot_expiry_at == relay.bot_grant(bot, "write")[1]
    srv._bot_expiry_handle.cancel()


@pytest.mark.asyncio
async def test_a_grant_change_arms_the_timer_with_nobody_listening(
        server, store, monkeypatch):
    """`_broadcast` builds no picture while no panel or phone listens, so the
    grant change itself has to set the timer, or a timed Read would keep an
    open bot stream painting past its end."""
    srv, daemon = server
    bot = _bot(daemon)
    assert relay.set_bot_access(bot, "read", "1h")[0]
    monkeypatch.setattr(srv, "_broadcast", lambda: None)
    srv._bot_expiry_at = 0.0
    srv._bot_expiry_handle = None
    srv._bot_grants_moved(bot)
    assert srv._bot_expiry_at == relay.bot_grant(bot, "read")[1]
    assert srv._bot_expiry_handle is not None
    srv._bot_expiry_handle.cancel()


# --- B6: `renew` says what the Mac said --------------------------------------


def test_renew_prints_the_inner_refusal():
    from dark_army_daemon import relay_bot
    refused = {"body": {"status": 403, "body": json.dumps(
        {"error": relay.BOT_READ_REFUSAL, "detail": relay.BOT_READ_REFUSAL})}}
    assert relay_bot._inner_refusal(refused) == relay.BOT_READ_REFUSAL
    assert relay_bot._inner_refusal({"body": {"status": 200, "body": "{}"}}) == ""
    assert relay_bot._inner_refusal(
        {"body": {"status": 409, "error": "counter"}}) == "counter"


def test_relay_bot_no_longer_describes_a_day_window():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1]
            / "dark_army_daemon" / "relay_bot.py").read_text()
    assert "Away writes last a day" not in text
    assert "renews the away window" not in text
    assert "Away writes run for this device's window" not in text


# --- B3: the phone never re-sends a failed bot press -------------------------


def _phone(name: str) -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[2]
            / "ios" / "BobPhone" / name).read_text()


def test_the_phone_never_resends_a_failed_bot_press():
    receipts = _phone("Receipts.swift")
    # The press is judged by the Mac's `bot_access` ...
    assert "case botAccess(deviceId: String, side: String, mode: String)" \
        in receipts
    assert "case PhoneActions.setBotAccess:" in receipts
    # ... and the sweep's own drop seam (`evidenceBeforeSending`, which the
    # sweep answers with `receipts.remove`) says yes before any resend.
    evidence = receipts[receipts.index("static func evidenceBeforeSending("):]
    evidence = evidence[:evidence.index("static func landed(")]
    assert "case .botAccess:\n            // Dropped, landed or not" in evidence
    client = _phone("Client.swift")
    sweep = client[client.index("func flushReceipts()"):]
    assert sweep.index("ReceiptLedger.evidenceBeforeSending(receipt.effect,") \
        < sweep.index("receipts.markSending(receipt.id)")


def test_the_profile_screen_never_posts_its_own_reset():
    view = _phone("ProfileView.swift")
    change = view[view.index(".onChange(of: selection.wrappedValue)"):]
    change = change[:change.index("sendBotAccess(")]
    assert "botReseeding.remove(side) != nil { return }" in change
    assert "BotAccessRules.shouldPost(" in change
    # A refusal is drawn inline and the menu goes back to the Mac's *current*
    # position, read after the answer, not one captured before it.
    assert "botRefusal[side] = BotAccessRules.refusalWords(result.detail)" in view
    assert "reseedBot(side, selection, to: publishedBotMode(side))" in view
