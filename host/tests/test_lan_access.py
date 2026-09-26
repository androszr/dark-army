# host/tests/test_lan_access.py
"""LAN phone access: pairing, the sealed home door, revocation, and loopback
isolation.

Every request after pairing is a sealed frame under the device's home key
(`relay.HOME`), posted to `/api/home` with `X-Bob-Channel`; the three plain
routes the old phone called answer 426 in words. `_home_post` seals, posts and
opens the answer; `_pair_plain` is the typed-address pair — SRP-6a over the
code through `tests/pake_pair.py`, so the home key crosses the Wi-Fi on no
route.

Ports are chosen at fixture time from held-open sockets — a fixed port in this
file would recreate the full-suite hang already paid for. The device ledger is
pointed at a temp dir, so nothing here reads or writes the live
``~/.dark-army/devices.json``.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import socket
import stat
import time
import urllib.error
import urllib.request
from unittest import mock

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import devices, lan_hosts, paths, relay
from dark_army_daemon.api_server import HOME_UPDATE_REFUSAL, ApiServer
from dark_army_daemon.daemon import BobDaemon
from tests.pake_pair import pair_typed, typed_exchange
from tests.free_ports import free_ports

#: The real reader, captured before the autouse `machine` fixture stubs
#: it out — the two tests below are about the reader itself.
_REAL_LIVE_ADDRS = lan_hosts.live_addrs


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
    """A deterministic machine behind `lan_hosts`' two impure readers.

    Autouse, so no test in this file depends on the routing table or the VPN
    state of whatever machine it runs on — `begin_pairing` used to pass here
    only because the real Mac happened to have a route. Mutate the dict to
    describe a different machine.
    """
    state = {
        "addrs": {"en0": ["192.168.1.5"]},
        "route": "192.168.1.5",
        "hostname": "test-mac",
    }
    monkeypatch.setattr(lan_hosts, "live_addrs", lambda: dict(state["addrs"]))
    monkeypatch.setattr(lan_hosts, "default_route_addr", lambda: state["route"])
    monkeypatch.setattr(socket, "gethostname", lambda: state["hostname"])
    return state


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "devices.json"
    monkeypatch.setattr(paths, "DEVICES_PATH", path)
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    devices.reset()
    relay.reset()
    yield path
    devices.reset()
    relay.reset()


@pytest.fixture
def token_path(tmp_path, monkeypatch):
    path = tmp_path / "api-token"
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", path)
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    return path


@pytest.fixture
def ports(monkeypatch):
    loop_port, lan_port = _free_ports(2)
    monkeypatch.setattr(api_mod, "LAN_API_PORT", lan_port)
    return loop_port, lan_port


@pytest_asyncio.fixture
async def server(token_path, ports):
    loop_port, _lan_port = ports
    daemon = BobDaemon()
    # BobDaemon builds its own ApiServer in __init__; replace it so
    # `devices_snapshot` / `begin_pairing` observe the listener we actually
    # start, the same single-server wiring production uses.
    srv = ApiServer(daemon, port=loop_port)
    daemon._api = srv
    await srv.start()
    try:
        yield srv, daemon, ports
    finally:
        await srv.stop()


def _fetch(path, *, port, data=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {},
        method="POST" if data is not None else "GET",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def fetch(*args, **kwargs):
    import asyncio
    return await asyncio.to_thread(_fetch, *args, **kwargs)


async def _raw(request: bytes, port: int) -> bytes:
    import asyncio
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=5)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _connect_ok(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


async def _pair_plain(lan_port: int, name: str = "phone") -> dict:
    """begin() then the typed-address route — SRP start → finish over
    the socket, the sealed reply opened under the derived key. Returns the
    reply dict plus the raw key under ``"key"``."""
    return await pair_typed(fetch, lan_port, name=name)


async def _home_post(lan_port: int, key: bytes, kind: str, body: dict,
                     ctr: int, *, headers: dict | None = None):
    """Seal one phone-to-Mac frame, POST it to `/api/home`, and open the
    sealed answer. Returns the inner ``(status, body_text)`` of a `reply`,
    or ``(http_status, raw_body)`` when the door refused plainly."""
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, kind, body,
                            ns=relay.HOME)
    sent = {"X-Bob-Channel": relay.channel_id(key, ns=relay.HOME),
            "Content-Type": "text/plain"}
    sent.update(headers or {})
    status, out = await fetch("/api/home", port=lan_port, data=wire.encode(),
                              headers=sent)
    if status != 200:
        return status, out
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, out.decode(),
                                  0, ns=relay.HOME)
    assert err == "", err
    assert frame["kind"] == "reply", frame
    return int(frame["body"]["status"]), frame["body"]["body"]


# --- default off --------------------------------------------------------------


@pytest.mark.asyncio
async def test_lan_off_by_default_binds_no_socket(server):
    srv, daemon, (_loop_port, lan_port) = server
    assert daemon.lan_access_enabled is False
    assert srv.lan_listening is False
    assert srv.state()["devices"]["lan_enabled"] is False
    assert _connect_ok(lan_port) is False


# --- loopback contract, with LAN on ------------------------------------------


@pytest.mark.asyncio
async def test_loopback_state_stays_ungated_when_lan_is_on(server):
    srv, daemon, (loop_port, _lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    status, body = await fetch("/api/state", port=loop_port)
    assert status == 200
    payload = json.loads(body)
    assert "agents" in payload
    assert payload["devices"]["lan_enabled"] is True


@pytest.mark.asyncio
async def test_loopback_rebinding_host_still_403_when_lan_is_on(server):
    srv, daemon, (loop_port, _lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    head = await _raw(
        b"GET /api/state HTTP/1.1\r\nHost: evil.com\r\n\r\n", port=loop_port)
    assert b"403" in head


# --- LAN read gate ------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_state_is_426_in_words(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    status, out = await fetch("/api/state", port=lan_port)
    assert status == 426
    assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL


@pytest.mark.asyncio
async def test_a_frame_under_another_key_is_403(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    # The device's own channel header, a frame sealed under some other key.
    wire = relay.seal_frame(relay.mint_key(), relay.DIR_PHONE_TO_MAC, 1,
                            "state", {}, ns=relay.HOME)
    status, out = await fetch(
        "/api/home", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(paired["key"],
                                                   ns=relay.HOME)})
    assert status == 403
    assert json.loads(out)["error"] == relay.REFUSAL_WORDS["seal"]


@pytest.mark.asyncio
async def test_a_sealed_state_frame_answers_a_sealed_state(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, body = await _home_post(lan_port, paired["key"], "state", {}, 1)
    assert status == 200
    payload = json.loads(body)
    assert "agents" in payload
    assert "board" in payload
    assert payload["devices"] == json.loads(
        json.dumps(srv.state()["devices"]))


@pytest.mark.asyncio
async def test_the_home_reply_carries_the_macs_legs_beside_an_unchanged_body(server):
    """The home door's link timing rides the reply *envelope* — `timing`
    beside `body` with the seal's `open` and the run's `run` — and the
    phone-facing body under it is byte-identical to the plain
    `_sealed_run` answer, so `_state_digest` and every decoder are
    untouched. The record itself is fire-and-forget through
    `note_link_timing` and lands on the daemon's timer."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    key = paired["key"]
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, 1, "state", {},
                            ns=relay.HOME)
    status, out = await fetch(
        "/api/home", port=lan_port, data=wire.encode(),
        headers={"X-Bob-Channel": relay.channel_id(key, ns=relay.HOME),
                 "Content-Type": "text/plain"})
    assert status == 200
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, out.decode(),
                                  0, ns=relay.HOME)
    assert err == "", err
    envelope = frame["body"]
    assert set(envelope) == {"re", "status", "content_type", "body", "timing"}
    assert set(envelope["timing"]) == {"open", "run"}
    for leg in envelope["timing"].values():
        assert isinstance(leg, float) and leg >= 0.0
    payload = json.loads(envelope["body"])
    assert "timing" not in payload
    assert "agents" in payload and "state_digest" in payload
    # The body is what `_sealed_run` answers, byte for byte.
    plain_status, _ctype, plain = await srv._sealed_run(
        "state", {}, paired["device_id"], actions=srv.LAN_ACTIONS,
        check_lease=False, record=False)
    assert plain_status == 200
    assert json.loads(plain)["state_digest"] == payload["state_digest"]
    # And the timer saw one LAN sample with all three legs.
    for _ in range(3):
        await asyncio.sleep(0)
    rollup = daemon._link_timer.rollup("lan", paired["device_id"], time.time())
    assert rollup is not None and rollup["count"] == 1
    assert rollup["hops"]["seal_max"] >= 0.0
    # The size hop is the plain reply's byte count — a whole picture, not
    # the short `unchanged` line (the clock inside moves the exact figure).
    assert abs(rollup["hops"]["size_max"] - len(plain)) < 64


@pytest.mark.asyncio
async def test_lan_pair_with_origin_is_403_and_does_not_spend_the_code(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    code = devices.begin(allow_plain=True)
    body = json.dumps({"pake": "start", "A": "00" * 256}).encode()
    status, _ = await fetch(
        "/api/pair", port=lan_port, data=body,
        headers={"Origin": "http://evil.example"})
    assert status == 403
    paired = await pair_typed(fetch, lan_port, code=code, name="browser")
    assert paired["token"]


@pytest.mark.asyncio
async def test_stop_lan_burns_the_active_pairing_code(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    code = devices.begin(allow_plain=True)
    await srv.stop_lan()
    token, _did, detail = devices.redeem(code, "phone")
    assert token == ""
    assert "valid" in detail


@pytest.mark.asyncio
async def test_lan_state_with_origin_is_403(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, out = await _home_post(
        lan_port, paired["key"], "state", {}, 1,
        headers={"Origin": "http://evil.example"})
    assert status == 403
    assert json.loads(out)["error"] == "forbidden"


@pytest.mark.asyncio
async def test_lan_events_is_404_even_with_valid_token(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    chan = {"X-Bob-Channel": relay.channel_id(paired["key"], ns=relay.HOME)}
    status, _ = await fetch("/api/events", port=lan_port, headers=chan)
    assert status == 404
    status, _ = await fetch("/api/board", port=lan_port, headers=chan)
    assert status == 404
    status, _ = await fetch("/api/action", port=lan_port, headers=chan)
    assert status == 404
    status, _ = await fetch("/api/home", port=lan_port, headers=chan)
    assert status == 404


@pytest.mark.asyncio
async def test_lan_chosen_action_is_not_404(server, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    monkeypatch.setattr(
        BobDaemon, "stop_session",
        lambda self, sid: (False, "no such session"))
    status, out = await _home_post(
        lan_port, paired["key"], "action",
        {"action": "stop_session", "session_id": "s1"}, 1)
    assert status in (200, 409), out
    assert status != 404


@pytest.mark.asyncio
async def test_lan_unchosen_actions_are_404_with_a_valid_token(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    for ctr, action in enumerate(("enroll_project", "reveal_session",
                                  "wrap_up"), start=1):
        status, _ = await _home_post(
            lan_port, paired["key"], "action",
            {"action": action, "session_id": "s1", "card_id": "c1",
             "root": "/tmp"}, ctr)
        assert status == 404, action


@pytest.mark.asyncio
async def test_lan_write_with_origin_is_403(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, _ = await _home_post(
        lan_port, paired["key"], "action",
        {"action": "stop_session", "session_id": "s1"}, 1,
        headers={"Origin": "http://evil.example"})
    assert status == 403


@pytest.mark.asyncio
async def test_plain_action_is_426_in_words(server, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    monkeypatch.setattr(
        BobDaemon, "stop_session",
        lambda self, sid: (_ for _ in ()).throw(AssertionError("ran")))
    body = json.dumps({"action": "stop_session", "session_id": "s1"}).encode()
    for headers in ({}, {"X-Bob-Device": ""},
                    {"X-Bob-Device": paired["token"]},
                    {"X-Bob-Channel": relay.channel_id(paired["key"],
                                                       ns=relay.HOME)}):
        status, out = await fetch("/api/action", port=lan_port, data=body,
                                  headers=headers)
        assert status == 426, headers
        assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL


@pytest.mark.asyncio
async def test_lan_write_with_loopback_token_and_no_device_is_refused(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    body = json.dumps({"action": "stop_session", "session_id": "s1"}).encode()
    status, out = await fetch(
        "/api/action", port=lan_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 426
    assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL
    # On the sealed route the loopback token is not a channel: not paired.
    status, out = await fetch(
        "/api/home", port=lan_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 403
    assert json.loads(out)["error"] == "that phone is not paired"


@pytest.mark.asyncio
async def test_loopback_write_still_uses_the_loopback_token(server, monkeypatch):
    srv, daemon, (loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    monkeypatch.setattr(
        BobDaemon, "stop_session",
        lambda self, sid: (False, "no such session"))
    body = json.dumps({"action": "stop_session", "session_id": "s1"}).encode()
    status, _ = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status in (200, 409)
    status, _ = await fetch("/api/action", port=loop_port, data=body)
    assert status == 403


@pytest.mark.asyncio
async def test_unpair_bites_on_the_next_write(server, monkeypatch):
    srv, daemon, (loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    monkeypatch.setattr(
        BobDaemon, "stop_session",
        lambda self, sid: (False, "no such session"))
    body = json.dumps({"action": "unpair_device",
                       "device_id": paired["device_id"]}).encode()
    status, out = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 200 and json.loads(out)["ok"] is True
    status, out = await _home_post(
        lan_port, paired["key"], "action",
        {"action": "stop_session", "session_id": "s1"}, 1)
    assert status == 403
    assert json.loads(out)["error"] == "that phone is not paired"


def test_lan_actions_is_a_chosen_tuple_not_a_prefix():
    names = ApiServer.LAN_ACTIONS
    assert isinstance(names, (tuple, frozenset))
    chosen = (
        "board_create", "board_update", "board_reset", "board_delete",
        "board_dispatch", "board_refine", "board_clear_done",
        "reply", "permission_verdict", "dismiss", "close_terminal",
        "low_priority",
        "stop_session", "hide_session", "delete_agent", "prepare_card",
        # The queue's order and the two pipeline dials, chosen 5 Sep 2026.
        # Both queue verbs are clear-never-set; neither dial is applied by
        # the daemon (see `BobDaemon.request_preference`).
        "board_unqueue", "board_queue_move",
        "set_board_autostart", "set_board_parallel_root",
        # Refine on several Prep cards in one press, chosen 25 Sep 2026:
        # every Refine guard per card, one planning session.
        "board_refine_batch",
    )
    for name in chosen:
        assert name in names
    for name in ("wrap_up", "enroll_project",
                 "reveal_session",
                 "board_reorder",
                 # The machine-wide dial and the launcher stay at the desk:
                 # one changes every project at once, the other is a
                 # capability grant.
                 "set_board_parallel", "set_board_dispatch"):
        assert name not in names
    assert "board_clear_done".startswith("board_")
    assert "board_clear_done" in names


@pytest.mark.asyncio
async def test_lan_board_create_and_clear_done_both_reach_the_daemon(
        server, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    created = []

    async def fake_create(self, fields):
        created.append(fields)
        return {"id": "c1"}, ""

    cleared = []

    async def fake_clear(self, expected_count, expected_token):
        cleared.append((expected_count, expected_token))
        return True, 0, ""

    monkeypatch.setattr(BobDaemon, "create_card", fake_create)
    monkeypatch.setattr(BobDaemon, "clear_done_cards", fake_clear)
    status, out = await _home_post(lan_port, paired["key"], "action", {
        "action": "board_create", "title": "from phone", "tool": "claude",
        "column_name": "prep",
    }, 1)
    assert status == 200, out
    assert created and created[0]["title"] == "from phone"
    token = "a" * 64
    status, out = await _home_post(lan_port, paired["key"], "action", {
        "action": "board_clear_done", "expected_count": "1",
        "expected_done_token": token,
    }, 2)
    assert status == 200, out
    assert cleared == [(1, token)]


@pytest.mark.asyncio
async def test_lan_board_update_carries_tool_and_model(server, monkeypatch):
    """The LAN allow-list passes a retool and a model pin straight through.

    The phone's two card-detail pickers each send one of these alone; if
    `_board_fields` dropped either, the door would answer 400 ("no writable
    card fields") and the picker would look inert.
    """
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    seen = []

    async def fake_update(self, card_id, fields, allow_unplanned=False):
        seen.append((card_id, fields))
        return {"id": card_id}, ""

    monkeypatch.setattr(BobDaemon, "update_card", fake_update)
    status, out = await _home_post(lan_port, paired["key"], "action", {
        "action": "board_update", "card_id": "c1", "tool": "claude",
        "model": "opus",
    }, 1)
    assert status == 200, out
    assert seen and seen[0][0] == "c1"
    assert seen[0][1]["tool"] == "claude"
    assert seen[0][1]["model"] == "opus"


@pytest.mark.asyncio
async def test_lan_board_create_carries_model(server, monkeypatch):
    """The composer's model choice survives into the create payload."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    created = []

    async def fake_create(self, fields):
        created.append(fields)
        return {"id": "c9"}, ""

    monkeypatch.setattr(BobDaemon, "create_card", fake_create)
    status, out = await _home_post(lan_port, paired["key"], "action", {
        "action": "board_create", "title": "from phone", "tool": "claude",
        "model": "opus", "column_name": "prep",
    }, 1)
    assert status == 200, out
    assert created and created[0]["model"] == "opus"


@pytest.mark.asyncio
async def test_lan_board_create_carries_create_token(server, monkeypatch):
    """Home sealed `board_create` carries `create_token` into `create_card`."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    created = []

    async def fake_create(self, fields):
        created.append(fields)
        return {"id": "c9"}, ""

    monkeypatch.setattr(BobDaemon, "create_card", fake_create)
    status, out = await _home_post(lan_port, paired["key"], "action", {
        "action": "board_create", "title": "from phone", "tool": "claude",
        "create_token": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "column_name": "prep",
    }, 1)
    assert status == 200, out
    assert created and created[0]["create_token"] == \
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.mark.asyncio
async def test_remote_board_create_carries_create_token(server, monkeypatch):
    """Away sealed `board_create` does the same behind `REMOTE_ACTIONS`."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port, name="phone"))["device_id"]
    relay.note_lan_proof(device_id)
    created = []

    async def fake_create(self, fields):
        created.append(fields)
        return {"id": "c9"}, "created"

    monkeypatch.setattr(BobDaemon, "create_card", fake_create)
    status, _ctype, _body = await srv._remote_run("action", {
        "action": "board_create", "title": "from away", "tool": "claude",
        "create_token": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
    }, device_id)
    assert status == 200
    assert created and created[0]["create_token"] == \
        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    assert "board_create" in ApiServer.REMOTE_ACTIONS
    assert ApiServer.LAN_ACTIONS.count("board_create") == 1
    assert ApiServer.REMOTE_ACTIONS.count("board_create") == 1


@pytest.mark.asyncio
async def test_lan_two_creates_with_the_same_token_are_one_row(
        server, tmp_path):
    from dark_army_daemon.board import BoardStore
    srv, daemon, (_loop_port, lan_port) = server
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        daemon.lan_access_enabled = True
        await srv.start_lan()
        paired = await _pair_plain(lan_port)
        token = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        for ctr, title in ((1, "first"), (2, "second")):
            status, out = await _home_post(lan_port, paired["key"], "action", {
                "action": "board_create", "title": title,
                "create_token": token, "column_name": "prep",
            }, ctr)
            assert status == 200, out
        assert len(daemon._board.cards()) == 1
        assert daemon._board.cards()[0]["title"] == "first"
    finally:
        store.close()
        daemon._board = None


# --- LAN usage door -----------------------------------------------------------


@pytest.mark.asyncio
async def test_lan_sealed_usage_is_200(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, body = await _home_post(lan_port, paired["key"], "usage",
                                    {"query": ""}, 1)
    assert status == 200
    payload = json.loads(body)
    bars = payload["limits"]["bars"]
    assert isinstance(bars, list)


@pytest.mark.asyncio
async def test_plain_usage_is_426_in_words(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    status, out = await fetch("/api/usage", port=lan_port)
    assert status == 426
    assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL


@pytest.mark.asyncio
async def test_lan_usage_with_origin_is_403(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, _ = await _home_post(
        lan_port, paired["key"], "usage", {"query": ""}, 1,
        headers={"Origin": "http://evil.example"})
    assert status == 403


@pytest.mark.asyncio
async def test_lan_usage_post_is_404_even_with_valid_token(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, _ = await fetch(
        "/api/usage", port=lan_port, data=b"{}",
        headers={"X-Bob-Channel": relay.channel_id(paired["key"],
                                                   ns=relay.HOME)})
    assert status == 404


@pytest.mark.asyncio
async def test_lan_history_stays_404_with_valid_token(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, _ = await fetch(
        "/api/history", port=lan_port,
        headers={"X-Bob-Channel": relay.channel_id(paired["key"],
                                                   ns=relay.HOME)})
    assert status == 404


@pytest.mark.asyncio
async def test_loopback_usage_stays_ungated_when_lan_is_on(server):
    srv, daemon, (loop_port, _lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    status, body = await fetch("/api/usage", port=loop_port)
    assert status == 200
    payload = json.loads(body)
    bars = payload["limits"]["bars"]
    assert isinstance(bars, list)


# --- pairing ------------------------------------------------------------------


def test_pairing_begin_redeem_resolves():
    code = devices.begin(allow_plain=True)
    token, device_id, detail = devices.redeem(code, "Kitchen iPhone")
    assert token and device_id and detail == ""
    assert devices.resolve(token) == device_id
    assert devices.resolve(devices.mint()) == ""


def test_pairing_code_refuses_a_second_redeem():
    code = devices.begin(allow_plain=True)
    token, _did, _ = devices.redeem(code, "one")
    assert token
    token2, did2, detail = devices.redeem(code, "two")
    assert token2 == "" and did2 == ""
    assert "used" in detail


def test_pairing_code_expires(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr(devices.time, "time", lambda: now["t"])
    code = devices.begin(allow_plain=True)
    now["t"] = 1_000.0 + devices.PAIRING_CODE_SECONDS + 1
    token, _did, detail = devices.redeem(code, "late")
    assert token == ""
    assert "expired" in detail


def test_max_devices_refuses_the_ninth():
    for i in range(devices.MAX_DEVICES):
        code = devices.begin(allow_plain=True)
        token, _did, detail = devices.redeem(code, f"phone-{i}")
        assert token, detail
    code = devices.begin(allow_plain=True)
    token, _did, detail = devices.redeem(code, "ninth")
    assert token == ""
    assert str(devices.MAX_DEVICES) in detail


def _other_code(real: str) -> str:
    """An 8-char code from the pairing alphabet that is not ``real``."""
    other = devices._CODE_ALPHABET[:devices._CODE_LEN]
    return other if other != real else devices._CODE_ALPHABET[1:devices._CODE_LEN + 1]


def test_five_wrong_codes_burn_the_live_window():
    code = devices.begin(allow_plain=True)
    wrong = _other_code(code)
    for _ in range(devices.PAIRING_FAIL_LIMIT - 1):
        token, _did, detail = devices.redeem(wrong, "x")
        assert token == ""
        assert "valid" in detail
    assert devices.pairing_open()
    token, did, detail = devices.redeem(code, "phone")
    assert token and did and detail == ""

    code = devices.begin(allow_plain=True)
    wrong = _other_code(code)
    for _ in range(devices.PAIRING_FAIL_LIMIT):
        token, _did, detail = devices.redeem(wrong, "x")
        assert token == ""
        assert "valid" in detail
    assert devices.pairing_open() is False
    token, _did, detail = devices.redeem(code, "phone")
    assert token == ""
    assert "valid" in detail
    token, _did, detail = devices.redeem(wrong, "x")
    assert token == ""
    assert "valid" in detail


def test_wrong_length_counts_and_does_not_compare_unequal_lengths():
    devices.begin(allow_plain=True)
    for _ in range(devices.PAIRING_FAIL_LIMIT):
        token, _did, detail = devices.redeem("A", "x")
        assert token == ""
        assert "valid" in detail
    assert devices.pairing_open() is False
    redeem_src = inspect.getsource(devices.redeem)
    assert "hmac.compare_digest" in redeem_src
    length_at = redeem_src.find("len(got)")
    compare_at = redeem_src.find("hmac.compare_digest")
    assert length_at != -1 and compare_at != -1 and length_at < compare_at
    sealed_src = inspect.getsource(ApiServer._lan_pair_sealed)
    assert "hmac.compare_digest" in sealed_src
    devices_src = inspect.getsource(devices)
    assert devices_src.count("hmac.compare_digest") >= 2
    assert devices.PAIRING_FAIL_LIMIT == 5


def test_a_correct_code_after_four_misses_still_pairs(caplog):
    caplog.set_level(logging.WARNING, logger="dark-army")
    code = devices.begin(allow_plain=True)
    wrong = _other_code(code)
    for _ in range(4):
        token, _did, _ = devices.redeem(wrong, "x")
        assert token == ""
    token, did, detail = devices.redeem(code, "phone")
    assert token and did and detail == ""
    assert devices.pairing_open() is False
    assert devices._pairing is not None
    assert devices._pairing.get("used") is True
    assert "pairing closed after" not in caplog.text


def test_success_is_not_rate_limited():
    redeem_src = inspect.getsource(devices.redeem)
    note_src = inspect.getsource(devices._note_fail)
    assert "time.sleep" not in redeem_src
    assert "asyncio.sleep" not in redeem_src
    assert "time.sleep" not in note_src
    assert "asyncio.sleep" not in note_src
    code = devices.begin(allow_plain=True)
    token, _did, detail = devices.redeem(code, "phone")
    assert token and detail == ""


def test_used_and_expired_do_not_count(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="dark-army")
    code = devices.begin(allow_plain=True)
    token, _did, _ = devices.redeem(code, "one")
    assert token
    assert devices.pairing_open() is False
    for _ in range(devices.PAIRING_FAIL_LIMIT + 1):
        token2, _did2, detail = devices.redeem(_other_code(code), "x")
        assert token2 == ""
        assert "valid" in detail
    assert "pairing closed after" not in caplog.text
    assert devices._pairing is not None

    now = {"t": 1_000.0}
    monkeypatch.setattr(devices.time, "time", lambda: now["t"])
    code = devices.begin(allow_plain=True)
    now["t"] = 1_000.0 + devices.PAIRING_CODE_SECONDS + 1
    for _ in range(devices.PAIRING_FAIL_LIMIT + 1):
        token, _did, detail = devices.redeem(_other_code(code), "x")
        assert token == ""
    assert "pairing closed after" not in caplog.text
    assert devices._pairing is not None
    token, _did, detail = devices.redeem(code, "late")
    assert token == ""
    assert "expired" in detail


def test_guess_close_logs_once(caplog):
    caplog.set_level(logging.WARNING, logger="dark-army")
    code = devices.begin(allow_plain=True)
    wrong = _other_code(code)
    for _ in range(4):
        devices.redeem(wrong, "x")
    assert "pairing closed after" not in caplog.text
    devices.redeem(wrong, "x")
    closed = [r for r in caplog.records
              if r.getMessage() == "pairing closed after 5 failed codes"]
    assert len(closed) == 1
    assert closed[0].levelno == logging.WARNING
    devices.redeem(wrong, "x")
    closed = [r for r in caplog.records
              if r.getMessage() == "pairing closed after 5 failed codes"]
    assert len(closed) == 1
    assert code not in caplog.text
    assert wrong not in caplog.text


def test_begin_resets_the_counter():
    code = devices.begin(allow_plain=True)
    wrong = _other_code(code)
    for _ in range(4):
        devices.redeem(wrong, "x")
    code = devices.begin(allow_plain=True)
    wrong = _other_code(code)
    for _ in range(4):
        token, _did, _ = devices.redeem(wrong, "x")
        assert token == ""
    token, _did, detail = devices.redeem(code, "phone")
    assert token and detail == ""


@pytest.mark.asyncio
async def test_origin_does_not_count(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    code = devices.begin(allow_plain=True)
    body_wrong = json.dumps({"pake": "start", "A": "00" * 256}).encode()
    for _ in range(5):
        status, _ = await fetch(
            "/api/pair", port=lan_port, data=body_wrong,
            headers={"Origin": "http://evil.example"})
        assert status == 403
    assert devices.pairing_open()
    assert devices._pairing["fails"] == 0
    paired = await pair_typed(fetch, lan_port, code=code)
    assert paired["token"]


def _drain_sse(queue: asyncio.Queue) -> list:
    frames = []
    while True:
        try:
            frames.append(json.loads(queue.get_nowait()))
        except asyncio.QueueEmpty:
            return frames


@pytest.mark.asyncio
async def test_begin_pairing_flushes_open_before_guess_close_can_coalesce(server):
    """Five fast misses must not hide pairing_open true from the square.

    `_broadcast` defers inside BROADCAST_MIN_INTERVAL; a burst of guesses
    would then flush only the already-closed state. The mint flushes now
    so `_last_news` records true before the 200 returns.
    """
    srv, daemon, (loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    queue = asyncio.Queue()
    srv._clients[queue] = False
    if srv._flush_handle is not None:
        srv._flush_handle.cancel()
        srv._flush_handle = None
    closed = srv.state()
    assert closed["devices"].get("pairing_open") is False
    srv._last_news = json.dumps(api_mod._news(closed), sort_keys=True)
    srv._last_sent = time.monotonic()
    # Armed for typing: the guesses below go through the typed branch,
    # which is refused unread (`pair_plain`) on an unarmed pairing.
    body = json.dumps({"action": "begin_pairing", "allow_typed": True}).encode()
    status, out = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 200, out
    payload = json.loads(out)
    assert payload["ok"] is True
    news = json.loads(srv._last_news)
    assert news["devices"]["pairing_open"] is True
    frames = _drain_sse(queue)
    assert any(f.get("devices", {}).get("pairing_open") is True for f in frames)
    wrong = _other_code(payload["code"])
    for _ in range(devices.PAIRING_FAIL_LIMIT):
        record = await typed_exchange(fetch, lan_port, wrong, name="x")
        assert record["finish"][0] == 403
    assert devices.pairing_open() is False
    if srv._flush_handle is not None:
        srv._flush()
    frames.extend(_drain_sse(queue))
    assert any(f.get("devices", {}).get("pairing_open") is False for f in frames)


@pytest.mark.asyncio
async def test_a_wrong_channel_does_not_count(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    code = devices.begin(allow_plain=True)
    body = json.dumps({"code": _other_code(code), "name": "x"}).encode()
    for _ in range(5):
        status, _ = await fetch(
            "/api/pair", port=lan_port, data=body,
            headers={"X-Bob-Channel": "nope"})
        assert status == 403
    assert devices.pairing_open()
    paired = await pair_typed(fetch, lan_port, code=code)
    assert paired["token"]


def test_snapshot_pairing_open_and_no_secrets():
    code = devices.begin(allow_plain=True)
    home = devices.pairing_home_key()
    snap = devices.snapshot()
    assert snap["pairing_open"] is True
    dumped = json.dumps(snap)
    assert code not in dumped
    assert "home_key_b64" not in dumped
    assert "fails" not in dumped
    if home:
        assert base64.b64encode(home).decode("ascii") not in dumped
        assert home.hex() not in dumped
    wrong = _other_code(code)
    for _ in range(devices.PAIRING_FAIL_LIMIT):
        devices.redeem(wrong, "x")
    assert devices.snapshot()["pairing_open"] is False
    devices.reset_pairing()
    assert devices.snapshot()["pairing_open"] is False
    ledger = json.dumps(devices.load())
    assert "fails" not in ledger
    names = "".join(paths._PRIVATE_FILES)
    assert "pairing-fail" not in names
    assert "fails.json" not in names


def test_max_devices_is_not_a_guess():
    for i in range(devices.MAX_DEVICES):
        code = devices.begin(allow_plain=True)
        token, _did, detail = devices.redeem(code, f"phone-{i}")
        assert token, detail
    code = devices.begin(allow_plain=True)
    assert devices.pairing_open()
    token, _did, detail = devices.redeem(code, "ninth")
    assert token == ""
    assert str(devices.MAX_DEVICES) in detail
    assert devices.pairing_open()
    token, _did, detail = devices.redeem(code, "ninth")
    assert token == ""
    assert str(devices.MAX_DEVICES) in detail
    assert devices.pairing_open()


@pytest.mark.asyncio
async def test_begin_pairing_refuses_while_lan_is_off(server):
    srv, daemon, (loop_port, _lan_port) = server
    body = json.dumps({"action": "begin_pairing"}).encode()
    status, out = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 409
    assert "off" in json.loads(out)["detail"].lower()


@pytest.mark.asyncio
async def test_begin_pairing_returns_code_host_port(server):
    srv, daemon, (loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    body = json.dumps({"action": "begin_pairing"}).encode()
    status, out = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 200
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["code"]
    assert payload["port"] == lan_port
    assert "expires_at" in payload
    # The QR carries the whole ordered list, and the old single-address key
    # stays as its head so a phone build from before this change still pairs.
    assert payload["hosts"] == ["192.168.1.5", "test-mac.local"]
    assert payload["host"] == payload["hosts"][0]
    # The home key rides the QR: 32 bytes, base64, the active pairing's own.
    assert len(base64.b64decode(payload["home_key"])) == 32
    assert base64.b64decode(payload["home_key"]) == devices.pairing_home_key()


# --- revocation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpair_bites_on_the_next_request(server):
    srv, daemon, (loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, _ = await _home_post(lan_port, paired["key"], "state", {}, 1)
    assert status == 200
    body = json.dumps({"action": "unpair_device",
                       "device_id": paired["device_id"]}).encode()
    status, out = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 200 and json.loads(out)["ok"] is True
    status, out = await _home_post(lan_port, paired["key"], "state", {}, 2)
    assert status == 403
    assert json.loads(out)["error"] == "that phone is not paired"
    assert devices.home_key(paired["device_id"]) is None


# --- secrets never surface ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_device_secret_never_reaches_the_read_surface(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port, name="secret-phone")
    token, device_id = paired["token"], paired["device_id"]
    published = json.dumps(srv.state(), default=str)
    published += json.dumps(daemon.devices_snapshot(), default=str)
    assert token not in published
    digest = devices.digest(token)
    assert digest not in published
    # The home key, in every spelling the ledger or the wire uses.
    assert paired["home_key"] not in published
    assert paired["key"].hex() not in published
    assert "home_key_b64" not in published
    assert device_id in json.dumps(daemon.devices_snapshot())


@pytest.mark.asyncio
async def test_the_relay_key_never_reaches_the_read_surface(server):
    """The pin above, extended to the away channel's shared secret: neither
    the base64 key nor its raw bytes' hex appears anywhere published."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    payload = await pair_typed(fetch, lan_port)
    key_b64 = payload["relay_key"]
    assert key_b64
    import base64
    key_hex = base64.b64decode(key_b64).hex()
    published = json.dumps(srv.state(), default=str)
    published += json.dumps(daemon.devices_snapshot(), default=str)
    assert key_b64 not in published
    assert key_hex not in published


@pytest.mark.asyncio
async def test_pair_returns_relay_key_exactly_once_per_redeem(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    code = devices.begin(allow_plain=True)
    payload = await pair_typed(fetch, lan_port, code=code)
    assert payload["relay_key"]
    assert "relay_url" in payload
    # The socket relay's address rides the pair reply beside the mailbox's
    # — never the QR, never a snapshot. None set here, so it is empty.
    assert payload["relay_ws_url"] == ""
    assert len(base64.b64decode(payload["home_key"])) == 32
    # The same code a second time is refused — both keys were handed out once.
    record = await typed_exchange(fetch, lan_port, code)
    status, out = record["start"]
    assert status == 403
    assert "relay_key" not in json.loads(out)
    assert "home_key" not in json.loads(out)
    assert json.loads(out)["error"] == "that pairing code has already been used"


@pytest.mark.asyncio
async def test_the_pair_reply_carries_the_socket_address_only_while_socket_link_is_on(
        server):
    """`relay_ws_url` rides the pair reply only while **both** Away access
    and Socket link are on: a phone paired while the trial lane is off
    carries no socket address (and gains one by pairing again once it is
    on), and away off withholds it whatever the socket switch says."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    ok, detail = relay.set_ws_url("wss://sock.example/ws/")
    assert ok, detail
    daemon.relay_ws_enabled = False
    payload = await _pair_plain(lan_port, name="off")
    assert payload["relay_key"]
    assert payload["relay_ws_url"] == ""
    daemon.relay_ws_enabled = True
    payload = await _pair_plain(lan_port, name="on")
    assert payload["relay_key"]
    assert payload["relay_ws_url"] == "wss://sock.example"
    daemon.remote_access_enabled = False
    assert srv._mint_relay("dev-x") == ("", "", "")
    # A daemon that does not know the switch at all hands out no address.
    bare = type("Daemon", (), {"remote_access_enabled": True})()
    with mock.patch.object(srv, "_daemon", bare):
        assert srv._mint_relay("dev-y")[2] == ""


@pytest.mark.asyncio
async def test_devices_snapshot_states_the_away_facts(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    # Paired with the away switch on, because the key is minted only then —
    # that is what gives this device a channel and a lease to state.
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port))["device_id"]
    snap = daemon.devices_snapshot()
    assert snap["remote_enabled"] is True
    assert snap["relay_url"] == ""
    assert snap["remote_activity"] == []
    # The socket lane's three facts beside the mailbox's: the switch (off
    # here), the address (none set) and no standing while the connector
    # does not exist — never a key, a digest or a channel id.
    assert snap["relay_ws_enabled"] is False
    assert snap["relay_ws_url"] == ""
    assert "relay_ws_health" not in snap
    row = next(r for r in snap["devices"] if r["id"] == device_id)
    assert row["relay"] is True
    assert row["lease_expires_at"] > 0
    assert row["socket"] == "off"


@pytest.mark.asyncio
async def test_devices_snapshot_publishes_where_home_is_now(server):
    """The pairing QR's addresses are a photograph taken once. A Mac that
    moves afterwards would be unreachable at home for ever, with the relay
    still answering — a phone sitting in AWAY on its own sofa. The live list
    rides the snapshot, which the relay carries too, so that phone can learn
    its way back."""
    _srv, daemon, _ports = server
    daemon._lan_hosts_cache = None
    with mock.patch.object(daemon, "_pairing_hosts",
                           return_value=(["10.0.0.7", "mac.local"], "")):
        assert daemon.devices_snapshot()["hosts"] == ["10.0.0.7", "mac.local"]


@pytest.mark.asyncio
async def test_the_address_reading_is_taken_on_a_clock_not_per_frame(server):
    """`state()` is built several times a second on a busy fleet and
    `live_addrs()` enumerates every interface."""
    _srv, daemon, _ports = server
    daemon._lan_hosts_cache = None
    calls = []

    def reading():
        calls.append(1)
        return (["10.0.0.7"], "")

    with mock.patch.object(daemon, "_pairing_hosts", side_effect=reading):
        for _ in range(5):
            daemon.devices_snapshot()
        assert len(calls) == 1
        # Past the shelf life, it is read again — a DHCP move is noticed
        # well inside the phone's own minute between full home walks.
        daemon._lan_hosts_cache = (
            daemon._lan_hosts_cache[0] - daemon.PAIRING_HOSTS_TTL - 1,
            daemon._lan_hosts_cache[1])
        daemon.devices_snapshot()
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_an_unreadable_interface_list_keeps_the_last_addresses(server):
    """Never a snapshot that says "home is nowhere" because one reading
    raised: the phone would learn nothing, and an empty list must never be
    mistaken for a Mac that has no address."""
    _srv, daemon, _ports = server
    daemon._lan_hosts_cache = None
    with mock.patch.object(daemon, "_pairing_hosts",
                           return_value=(["10.0.0.7"], "")):
        assert daemon.devices_snapshot()["hosts"] == ["10.0.0.7"]
    daemon._lan_hosts_cache = (daemon._lan_hosts_cache[0]
                               - daemon.PAIRING_HOSTS_TTL - 1,
                               daemon._lan_hosts_cache[1])
    with mock.patch.object(daemon, "_pairing_hosts", side_effect=OSError("no")):
        assert daemon.devices_snapshot()["hosts"] == ["10.0.0.7"]


@pytest.mark.asyncio
async def test_loopback_still_404s_the_mailbox_route(server):
    _srv, _daemon, (loop_port, _lan_port) = server
    status, _ = await fetch("/api/box?ch=00&dir=to-mac", port=loop_port)
    assert status == 404


# --- ledger hygiene -----------------------------------------------------------


def test_devices_json_created_0600(ledger):
    code = devices.begin(allow_plain=True)
    token, _did, detail = devices.redeem(code, "phone")
    assert token, detail
    assert ledger.is_file()
    mode = stat.S_IMODE(ledger.stat().st_mode)
    assert mode == 0o600


def test_devices_json_is_in_private_files():
    assert "devices.json" in paths._PRIVATE_FILES


def test_resolve_empty_token_and_empty_ledger_refuse():
    assert devices.resolve("") == ""
    assert devices.resolve("   ") == ""
    assert devices.resolve(None) == ""
    # Empty ledger: nothing paired yet.
    assert devices.resolve(devices.mint()) == ""


@pytest.mark.asyncio
async def test_failed_lan_bind_leaves_loopback_up(token_path, ports, monkeypatch):
    """A collision on the phone port must not take the panel down."""
    loop_port, lan_port = ports
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        blocker.bind(("0.0.0.0", lan_port))
        blocker.listen(1)
        daemon = BobDaemon()
        daemon.lan_access_enabled = True
        srv = ApiServer(daemon, port=loop_port)
        daemon._api = srv
        await srv.start()
        try:
            await srv.start_lan()
            assert srv.lan_listening is False
            assert daemon.devices_snapshot()["lan_enabled"] is False
            status, _ = await fetch("/api/state", port=loop_port)
            assert status == 200
        finally:
            await srv.stop()
    finally:
        blocker.close()


# --- which addresses a phone could reach --------------------------------------


def test_candidates_drop_tunnels():
    """The door closes a knock that lands on a tunnel address unanswered, so
    offering one would only send the phone somewhere it is turned away."""
    hosts = lan_hosts.candidates(
        {"utun4": ["10.8.0.3"], "en0": ["192.168.1.5"], "ipsec0": ["10.9.0.1"],
         "ppp0": ["10.10.0.1"]},
        "", "")
    assert hosts == ["192.168.1.5"]


def test_candidates_drop_loopback_and_link_local():
    hosts = lan_hosts.candidates(
        {"lo0": ["127.0.0.1"], "en0": ["192.168.1.5"], "en1": ["169.254.3.4"]},
        "", "")
    assert hosts == ["192.168.1.5"]


def test_candidates_promote_the_default_route():
    hosts = lan_hosts.candidates(
        {"en0": ["192.168.1.5"], "en1": ["10.0.0.7"]}, "10.0.0.7", "")
    assert hosts == ["10.0.0.7", "192.168.1.5"]


def test_candidates_ignore_a_default_route_that_names_a_tunnel():
    """The whole bug: the routing table answers with the VPN. That must not
    promote the tunnel above the LAN address."""
    hosts = lan_hosts.candidates(
        {"en0": ["192.168.1.5"], "utun4": ["10.8.0.3"]}, "10.8.0.3", "")
    assert hosts == ["192.168.1.5"]


@pytest.mark.parametrize("raw", ["Studio-Mac", "Studio-Mac.local",
                                 "studio-mac.local.", "  Studio-Mac  "])
def test_bonjour_name_is_normalised(raw):
    hosts = lan_hosts.candidates({"en0": ["192.168.1.5"]}, "", raw)
    assert hosts == ["192.168.1.5", "studio-mac.local"]


def test_bonjour_rung_sits_after_the_lan_and_no_tunnel_follows():
    hosts = lan_hosts.candidates(
        {"en0": ["192.168.1.5"], "utun4": ["10.8.0.3"]}, "", "mac")
    assert hosts == ["192.168.1.5", "mac.local"]


def test_empty_hostname_omits_the_bonjour_rung():
    assert lan_hosts.candidates({"en0": ["192.168.1.5"]}, "", "  ") == \
        ["192.168.1.5"]
    assert lan_hosts.candidates({"en0": ["192.168.1.5"]}, "", ".") == \
        ["192.168.1.5"]


def test_candidates_collapse_duplicates_in_order():
    hosts = lan_hosts.candidates(
        {"en0": ["192.168.1.5", "192.168.1.5"], "bridge0": ["192.168.1.5"]},
        "192.168.1.5", "")
    assert hosts == ["192.168.1.5"]


def test_candidates_of_an_empty_machine_are_empty():
    assert lan_hosts.candidates({}, "", "") == []
    assert lan_hosts.candidates(None, "", "mac") == ["mac.local"]


def test_refusal_is_silent_when_a_lan_address_exists():
    assert lan_hosts.refusal({"en0": ["192.168.1.5"], "utun4": ["10.8.0.3"]}) == ""


def test_refusal_names_the_vpn_address_when_that_is_all_there_is():
    text = lan_hosts.refusal({"utun4": ["10.8.0.3"], "lo0": ["127.0.0.1"]})
    assert "only a VPN address" in text
    assert "10.8.0.3" in text
    assert "Wi-Fi" in text


def test_refusal_says_so_when_there_is_nothing_at_all():
    assert "no network address" in lan_hosts.refusal({})
    assert "no network address" in lan_hosts.refusal(
        {"lo0": ["127.0.0.1"], "en0": ["169.254.9.9"]})


def test_live_addrs_survives_a_broken_reader(monkeypatch):
    def boom():
        raise OSError("no")
    monkeypatch.setattr(lan_hosts, "psutil_net_if_addrs", boom)
    assert _REAL_LIVE_ADDRS() == {}


def test_live_addrs_keeps_ipv4_only(monkeypatch):
    class Entry:
        def __init__(self, family, address):
            self.family = family
            self.address = address
    monkeypatch.setattr(lan_hosts, "psutil_net_if_addrs", lambda: {
        "en0": [Entry(socket.AF_INET6, "fe80::1"),
                Entry(socket.AF_INET, "192.168.1.5")],
        "lo0": [Entry(socket.AF_INET, "127.0.0.1")],
    })
    assert _REAL_LIVE_ADDRS() == {"en0": ["192.168.1.5"],
                                  "lo0": ["127.0.0.1"]}


@pytest.mark.asyncio
async def test_begin_pairing_carries_every_address_best_first(server, machine):
    srv, daemon, (loop_port, lan_port) = server
    machine["addrs"] = {"en0": ["192.168.1.5"], "utun4": ["10.8.0.3"]}
    machine["route"] = "10.8.0.3"
    machine["hostname"] = "Studio-Mac"
    daemon.lan_access_enabled = True
    await srv.start_lan()
    body = json.dumps({"action": "begin_pairing"}).encode()
    status, out = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 200
    payload = json.loads(out)
    assert payload["hosts"] == ["192.168.1.5", "studio-mac.local"]
    assert payload["host"] == payload["hosts"][0]


@pytest.mark.asyncio
async def test_begin_pairing_refuses_a_vpn_only_mac_without_spending_a_code(
        server, machine):
    srv, daemon, (loop_port, lan_port) = server
    machine["addrs"] = {"utun4": ["10.8.0.3"]}
    machine["route"] = "10.8.0.3"
    daemon.lan_access_enabled = True
    await srv.start_lan()
    # A code minted by hand before the refused press must survive it: a
    # refusal may not burn the two-minute window.
    code = devices.begin(allow_plain=True)
    body = json.dumps({"action": "begin_pairing"}).encode()
    status, out = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 409
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "10.8.0.3" in payload["detail"]
    assert "only a VPN address" in payload["detail"]
    token, _did, detail = devices.redeem(code, "phone")
    assert token, detail


@pytest.mark.asyncio
async def test_begin_pairing_refuses_a_mac_with_no_address(server, machine):
    srv, daemon, (loop_port, _lan_port) = server
    machine["addrs"] = {}
    machine["route"] = ""
    machine["hostname"] = ""
    daemon.lan_access_enabled = True
    await srv.start_lan()
    body = json.dumps({"action": "begin_pairing"}).encode()
    status, out = await fetch(
        "/api/action", port=loop_port, data=body,
        headers={"X-Bob-Token": srv.token})
    assert status == 409
    assert "no network address" in json.loads(out)["detail"]


@pytest.mark.asyncio
async def test_answer_question_is_a_chosen_lan_action():
    assert "answer_question" in ApiServer.LAN_ACTIONS


@pytest.mark.asyncio
async def test_lan_answer_question_reaches_the_daemon(server, monkeypatch):
    """The chosen verb is routed, not 404'd — the guards are the daemon's."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    seen = []

    async def fake(self, session_id, question_id, option_index):
        seen.append((session_id, question_id, option_index))
        return False, "That session is no longer waiting on a question."

    monkeypatch.setattr(BobDaemon, "answer_question", fake)
    status, out = await _home_post(lan_port, paired["key"], "action", {
        "action": "answer_question", "session_id": "s1",
        "question_id": "toolu_1", "option_index": "1",
    }, 1)
    assert status == 409, out
    assert seen == [("s1", "toolu_1", 1)]
    assert "no longer waiting" in out


@pytest.mark.asyncio
async def test_lan_answer_question_with_a_bad_index_is_400(server, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)

    async def fake(self, session_id, question_id, option_index):
        raise AssertionError("a malformed answer must never reach the daemon")

    monkeypatch.setattr(BobDaemon, "answer_question", fake)
    for ctr, payload in enumerate((
        {"action": "answer_question", "session_id": "s1", "option_index": "two"},
        {"action": "answer_question", "session_id": "s1", "option_index": "-1"},
        {"action": "answer_question", "session_id": "s1"},
        {"action": "answer_question", "option_index": "0"},
    ), start=1):
        status, _ = await _home_post(lan_port, paired["key"], "action",
                                     payload, ctr)
        assert status == 400, payload


@pytest.mark.asyncio
async def test_lan_answer_question_in_the_clear_is_426(server, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    await _pair_plain(lan_port)

    async def fake(self, session_id, question_id, option_index):
        raise AssertionError("an unauthorised answer must never reach the daemon")

    monkeypatch.setattr(BobDaemon, "answer_question", fake)
    body = json.dumps({
        "action": "answer_question", "session_id": "s1",
        "question_id": "toolu_1", "option_index": "0",
    }).encode()
    status, out = await fetch("/api/action", port=lan_port, data=body)
    assert status == 426
    assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL


@pytest.mark.asyncio
async def test_pairing_mints_no_away_key_while_the_switch_is_off(server):
    """The away switch is a consent gate, so it has to gate the *minting*.

    Minting regardless was inert on the day and wrong the moment somebody
    flipped the switch on: every phone paired while it was off would have
    become able to act from anywhere with no re-pair and no prompt. A
    keyless record simply has no away path, and re-pairing at home is the
    documented way to add one.
    """
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = False
    await srv.start_lan()
    payload = await pair_typed(fetch, lan_port)
    # Pairing still works and still carries write on the LAN — only the away
    # channel is withheld.
    assert payload["token"]
    assert payload["relay_key"] == ""
    assert payload["relay_url"] == ""
    assert payload["relay_ws_url"] == ""
    # The home key is a different key with a different reason to exist,
    # and is minted regardless of the away switch.
    assert len(base64.b64decode(payload["home_key"])) == 32
    device_id = payload["device_id"]
    assert relay.channel_key(device_id) is None
    assert devices.home_key(device_id) is not None
    row = next(r for r in daemon.devices_snapshot()["devices"]
               if r["id"] == device_id)
    assert row["relay"] is False


@pytest.mark.asyncio
async def test_the_away_path_has_its_own_action_list(server):
    """`REMOTE_ACTIONS` is a second list, not a reuse of `LAN_ACTIONS`.

    Sharing one tuple made "the phone may do X at home" and "the phone may
    do X from anywhere" the same decision; `answer_questions` reached the
    internet through that door, added for the phone at home by a plan with
    no reason to think about the relay. A name absent from the away list is
    refused over the relay even inside a live lease, and refused *before*
    anything executes.
    """
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port, name="phone"))["device_id"]
    relay.note_lan_proof(device_id)
    assert relay.lease_valid(device_id)

    # The away list may never exceed the home list: a verb the phone cannot
    # use on the Wi-Fi must not be reachable from the internet.
    assert set(srv.REMOTE_ACTIONS) <= set(srv.LAN_ACTIONS)

    ran = []
    original = srv._lan_run

    async def _spy(action, payload):
        ran.append(action)
        return await original(action, payload)

    srv._lan_run = _spy
    saved = ApiServer.REMOTE_ACTIONS
    try:
        ApiServer.REMOTE_ACTIONS = ("dismiss",)
        status, _ctype, _body = await srv._remote_run(
            "action", {"action": "stop_session"}, device_id)
        assert status == 404
        assert ran == []
        # Narrowing the away list left the home list alone — the two are
        # separate decisions, which is the whole point of the second tuple.
        assert "stop_session" in srv.LAN_ACTIONS
    finally:
        ApiServer.REMOTE_ACTIONS = saved
        srv._lan_run = original


@pytest.mark.asyncio
async def test_lan_board_create_honours_refine_while_board_refine_is_on_lan(
        server, monkeypatch):
    """At home the `refine` flag on `board_create` reaches the one-press
    verb, because `LAN_ACTIONS` allows `board_refine`."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    both, plain = [], []

    async def fake_both(self, fields):
        both.append(fields)
        return {"id": "c1"}, "created", True, "started"

    async def fake_create(self, fields):
        plain.append(fields)
        return {"id": "c1"}, "created"

    monkeypatch.setattr(BobDaemon, "create_card_and_refine", fake_both)
    monkeypatch.setattr(BobDaemon, "create_card", fake_create)
    status, out = await _home_post(lan_port, paired["key"], "action", {
        "action": "board_create", "title": "from phone", "tool": "claude",
        "refine": "true",
    }, 1)
    assert status == 200, out
    assert len(both) == 1 and plain == []
    assert "refine" not in both[0]


async def _away_create_with_refine(srv, daemon, lan_port, monkeypatch):
    """Pair, arm the lease, and send an away `board_create` carrying the
    flag through `_remote_run`. Returns ``(status, both, plain)``."""
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port, name="phone"))["device_id"]
    relay.note_lan_proof(device_id)
    assert relay.lease_valid(device_id)
    both, plain = [], []

    async def fake_both(self, fields):
        both.append(fields)
        return {"id": "c1"}, "created", True, "started"

    async def fake_create(self, fields):
        plain.append(fields)
        return {"id": "c1"}, "created"

    monkeypatch.setattr(BobDaemon, "create_card_and_refine", fake_both)
    monkeypatch.setattr(BobDaemon, "create_card", fake_create)
    status, _ctype, _body = await srv._remote_run("action", {
        "action": "board_create", "title": "from away", "tool": "claude",
        "refine": "true",
    }, device_id)
    return status, both, plain


@pytest.mark.asyncio
async def test_remote_board_create_drops_refine_when_board_refine_is_not_remote(
        server, monkeypatch):
    """The flag is a `board_refine` in disguise. On a door that does not
    allow that verb it is **dropped, not refused**: the card is still
    created (the create half is allowed) and no refinement starts."""
    srv, daemon, (_loop_port, lan_port) = server
    saved = ApiServer.REMOTE_ACTIONS
    try:
        ApiServer.REMOTE_ACTIONS = ("board_create",)
        status, both, plain = await _away_create_with_refine(
            srv, daemon, lan_port, monkeypatch)
    finally:
        ApiServer.REMOTE_ACTIONS = saved
    assert status == 200
    assert both == [] and len(plain) == 1
    assert "refine" not in plain[0]


@pytest.mark.asyncio
async def test_remote_board_create_honours_refine_while_board_refine_is_remote(
        server, monkeypatch):
    """With `board_refine` on the away list (today's shipped tuple), the
    flag reaches the one-press verb from away too."""
    srv, daemon, (_loop_port, lan_port) = server
    assert "board_refine" in ApiServer.REMOTE_ACTIONS
    status, both, plain = await _away_create_with_refine(
        srv, daemon, lan_port, monkeypatch)
    assert status == 200
    assert len(both) == 1 and plain == []


@pytest.mark.asyncio
async def test_prepare_card_is_chosen_at_home_and_away():
    """PREPARE is on **both** lists. The away composer's slowness is
    latency, never a second refusal hiding behind it: a verb missing from
    `REMOTE_ACTIONS` would 404 before anything ran, and the phone would show
    "not found" rather than the notes. The standing bound holds too."""
    assert "prepare_card" in ApiServer.LAN_ACTIONS
    assert "prepare_card" in ApiServer.REMOTE_ACTIONS
    # Two declarations, narrowable one at a time — the property that matters.
    # (Not an identity check: CPython folds two identical tuple literals in
    # one class body to one object, so `is not` would fail on a codebase
    # that is behaving exactly as designed.)
    assert {"LAN_ACTIONS", "REMOTE_ACTIONS"} <= set(vars(ApiServer))
    saved = ApiServer.REMOTE_ACTIONS
    try:
        ApiServer.REMOTE_ACTIONS = ("dismiss",)
        assert "prepare_card" in ApiServer.LAN_ACTIONS
    finally:
        ApiServer.REMOTE_ACTIONS = saved
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_remote_prepare_reaches_the_prepare_helper_and_respects_the_lease(
        server, monkeypatch):
    """Inside a live lease an away PREPARE reaches `_prepare` — the
    *execution* helper, never `_prepare_request`, which checks the loopback
    token the relay never carries. Lapsed, it is refused in the daemon's own
    words and the helper is never called: the Mac does no work for a phone
    whose away window has run down.
    """
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port, name="phone"))["device_id"]
    relay.note_lan_proof(device_id)
    assert relay.lease_valid(device_id)

    calls = []

    async def _spy(payload):
        calls.append(payload)
        return 200, "application/json", b'{"ok": true}'

    monkeypatch.setattr(srv, "_prepare", _spy)

    body = {"action": "prepare_card", "title": "a card", "summary": "",
            "tool": "claude", "project": "p", "root": "/tmp", "attachments": ""}
    status, _ctype, out = await srv._remote_run("action", dict(body), device_id)
    assert status == 200, out
    assert len(calls) == 1
    assert calls[0]["title"] == "a card"

    # The lease lapses: the same request executes nothing at all.
    monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
    status, _ctype, out = await srv._remote_run("action", dict(body), device_id)
    assert status == 403
    refusal = json.loads(out)
    assert refusal["error"] == relay.LEASE_REFUSAL
    assert refusal["detail"] == relay.LEASE_REFUSAL
    assert len(calls) == 1


# --- register_push_token: the phone's APNs address, on the caller's channel ---


async def _register(lan_port, paired: dict, body: dict):
    """One sealed `register_push_token`. The counter rides on the paired
    dict so a test can call this more than once."""
    paired["ctr"] = int(paired.get("ctr", 0)) + 1
    return await _home_post(lan_port, paired["key"], "action",
                            {"action": "register_push_token", **body},
                            paired["ctr"])


@pytest.mark.asyncio
async def test_register_push_token_is_chosen_at_home_and_away():
    """In **both** tuples — the phone must be able to re-register from away
    (a token rotates whenever iOS feels like it) — and the standing bound
    holds: the away list never exceeds the home list."""
    assert "register_push_token" in ApiServer.LAN_ACTIONS
    assert "register_push_token" in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_register_push_token_lands_on_the_callers_channel(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    apns = "ab" * 32
    status, out = await _register(lan_port, paired,
                                  {"token": apns, "env": "dev"})
    assert status == 200, out
    assert relay.push_token(device_id) == apns
    assert relay.push_env(device_id) == "dev"


@pytest.mark.asyncio
async def test_register_push_token_refuses_malformed_tokens(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    for bad in ("zz" * 32,          # not hex
                "ab" * 8,           # too short
                "ab" * 128,         # too long
                "ab" * 16 + "!"):   # punctuation
        status, _ = await _register(lan_port, paired, {"token": bad})
        assert status == 400, bad
    status, _ = await _register(
        lan_port, paired, {"token": "ab" * 32, "env": "staging"})
    assert status == 400
    assert relay.push_token(device_id) is None


@pytest.mark.asyncio
async def test_empty_token_unregisters(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    status, _ = await _register(lan_port, paired, {"token": "cd" * 32})
    assert status == 200
    assert relay.push_token(device_id) is not None
    status, _ = await _register(lan_port, paired, {"token": ""})
    assert status == 200
    assert relay.push_token(device_id) is None


@pytest.mark.asyncio
async def test_register_without_a_channel_is_refused_in_words(server):
    """A phone paired while away access was off has no channel — nothing to
    route a buzz through — and the refusal says what fixes it."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = False       # pairing mints no key
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    assert relay.channel_key(device_id) is None
    status, out = await _register(lan_port, paired, {"token": "ef" * 32})
    assert status == 409
    assert "re-pair" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_the_push_token_never_reaches_the_read_surface(server):
    """The relay-key pin, extended: an APNs token is an address somebody
    could spam, so the snapshot states presence (`push` per device) and
    nothing else."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    row = next(r for r in daemon.devices_snapshot()["devices"]
               if r["id"] == device_id)
    assert row["push"] is False
    apns = "0123456789abcdef" * 4
    status, _ = await _register(lan_port, paired, {"token": apns})
    assert status == 200
    published = json.dumps(srv.state(), default=str)
    published += json.dumps(daemon.devices_snapshot(), default=str)
    assert apns not in published
    row = next(r for r in daemon.devices_snapshot()["devices"]
               if r["id"] == device_id)
    assert row["push"] is True



# --- register_activity_token: the live card's token, the push token's twin ----


async def _register_activity(lan_port, paired: dict, body: dict):
    paired["ctr"] = int(paired.get("ctr", 0)) + 1
    return await _home_post(lan_port, paired["key"], "action",
                            {"action": "register_activity_token", **body},
                            paired["ctr"])


@pytest.mark.asyncio
async def test_register_activity_token_is_chosen_at_home_and_away():
    assert "register_activity_token" in ApiServer.LAN_ACTIONS
    assert "register_activity_token" in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_register_activity_token_lands_on_the_callers_channel(server):
    """The device is the verified caller: a `device` field in the payload
    is ignored, so one phone cannot point another's live card anywhere."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    token = "ab" * 32
    status, out = await _register_activity(
        lan_port, paired, {"token": token, "env": "dev", "device": "other-phone"})
    assert status == 200, out
    assert relay.activity_token(device_id) == token
    assert relay.activity_env(device_id) == "dev"
    assert relay.activity_shape(device_id) == 1
    assert relay.activity_token("other-phone") is None
    # The push token is untouched: two tokens, two verbs.
    assert relay.push_token(device_id) is None


@pytest.mark.asyncio
async def test_register_activity_token_refuses_malformed_tokens(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    for bad in ("zz" * 32, "ab" * 8, "ab" * 128, "ab" * 16 + "!"):
        status, _ = await _register_activity(lan_port, paired, {"token": bad})
        assert status == 400, bad
    status, _ = await _register_activity(
        lan_port, paired, {"token": "ab" * 32, "env": "staging"})
    assert status == 400
    assert relay.activity_token(device_id) is None


@pytest.mark.asyncio
async def test_empty_activity_token_unregisters_and_forgets_the_end_mark(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    status, _ = await _register_activity(lan_port, paired, {"token": "cd" * 32})
    assert status == 200
    assert relay.activity_token(device_id) is not None
    daemon._live_activity_last[device_id] = "ended"
    status, _ = await _register_activity(lan_port, paired, {"token": "ef" * 32})
    assert status == 200
    # A fresh token is a fresh activity: the daemon's end mark is gone.
    assert device_id not in daemon._live_activity_last
    daemon._live_activity_last[device_id] = {"kind": "attention"}
    status, _ = await _register_activity(lan_port, paired, {"token": ""})
    assert status == 200
    assert relay.activity_token(device_id) is None
    assert device_id not in daemon._live_activity_last


@pytest.mark.asyncio
async def test_register_activity_without_a_channel_is_refused_in_words(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = False
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    assert relay.channel_key(paired["device_id"]) is None
    status, out = await _register_activity(lan_port, paired, {"token": "ef" * 32})
    assert status == 409
    assert "re-pair" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_register_activity_token_without_a_device_is_forbidden(server):
    srv, _daemon, _ports = server
    status, _ctype, _out = await srv._lan_run(
        "register_activity_token", {"token": "ab" * 32}, "")
    assert status == 403


@pytest.mark.asyncio
async def test_the_activity_token_never_reaches_the_read_surface(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    row = next(r for r in daemon.devices_snapshot()["devices"]
               if r["id"] == device_id)
    assert row["live_activity"] is False
    token = "fedcba9876543210" * 4
    status, _ = await _register_activity(lan_port, paired, {"token": token})
    assert status == 200
    published = json.dumps(srv.state(), default=str)
    published += json.dumps(daemon.devices_snapshot(), default=str)
    assert token not in published
    row = next(r for r in daemon.devices_snapshot()["devices"]
               if r["id"] == device_id)
    assert row["live_activity"] is True
    assert row["push"] is False
    assert "activity_shape" not in published


@pytest.mark.asyncio
async def test_register_activity_token_stores_shape_2_and_refuses_a_third(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired["device_id"]
    status, out = await _register_activity(
        lan_port, paired, {"token": "ab" * 32, "shape": 2})
    assert status == 200, out
    assert relay.activity_shape(device_id) == 2
    status, out = await _register_activity(
        lan_port, paired, {"token": "cd" * 32, "shape": "2"})
    assert status == 200, out
    assert relay.activity_shape(device_id) == 2
    status, out = await _register_activity(
        lan_port, paired, {"token": "ef" * 32, "shape": 3})
    assert status == 400
    assert json.loads(out)["error"] == "unknown live card shape"
    assert relay.activity_token(device_id) == "cd" * 32
    bare = await _pair_plain(lan_port)
    status, _out = await _register_activity(
        lan_port, bare, {"token": "ab" * 32, "shape": "x"})
    assert status == 400
    assert relay.activity_token(bare["device_id"]) is None


# --- the diary ------------------------------------------------------------------


def _diary(daemon, tmp_path):
    from dark_army_daemon.event_log import EventLog
    log = EventLog(path=tmp_path / "event-log.jsonl")
    log.open()
    daemon._event_log = log
    return log


@pytest.mark.asyncio
async def test_plain_log_is_426_and_a_sealed_log_frame_answers(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    _diary(daemon, tmp_path).append("session_start", nickname="a", project="p")
    # Unpaired and plaintext: the same refusal the other three reads give.
    status, out = await fetch("/api/log", port=lan_port)
    assert status == 426
    assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL
    paired = await _pair_plain(lan_port)
    status, body = await _home_post(lan_port, paired["key"], "log",
                                    {"query": "since=0"}, 1)
    assert status == 200
    page = json.loads(body)
    assert page["available"] is True
    assert [e["text"] for e in page["events"]] == ["a started in p"]
    status, _body = await _home_post(lan_port, paired["key"], "log",
                                     {"query": "limit=0"}, 2)
    assert status == 400


def test_the_diary_is_a_read_not_an_action():
    """`log` is a `kind` beside `state` and `usage`; neither action list
    grew, and the two are still equal (the away list may never exceed home)."""
    assert "log" not in ApiServer.LAN_ACTIONS
    assert "log" not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def _hide_record(tmp_path, daemon, *, child=False):
    from dark_army_daemon.codex_rollouts import CodexRecord
    path = tmp_path / 'rollout-hide.jsonl'
    path.write_text('synthetic journal\n')
    stat = path.stat()
    record = CodexRecord(session_id='codex:hide', thread_id='hide', path=path,
                         revision=(stat.st_mtime_ns, stat.st_size), last_event=time.time(), cwd='/code/bob',
                         parent_thread_id='parent' if child else '')
    daemon._codex_records[record.session_id] = record
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize('away', [False, True])
async def test_sealed_hide_only_suppresses_current_revision(server, tmp_path, monkeypatch, away):
    from dark_army_daemon import codex_rollouts
    srv, daemon, (_, lan_port) = server
    daemon.lan_access_enabled = daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    relay.note_lan_proof(paired['device_id'])
    record = _hide_record(tmp_path, daemon)
    original = record.path.read_bytes()
    signals = []
    monkeypatch.setattr('os.kill', lambda *args: signals.append(args))
    monkeypatch.setattr(daemon, '_notify_activity', lambda: None)
    body = {'action': 'hide_session', 'session_id': record.session_id}
    if away:
        status, _, output = await srv._remote_run('action', body, paired['device_id'])
    else:
        status, output = await _home_post(lan_port, paired['key'], 'action', body, 1)
    assert status == 200 and json.loads(output)['ok']
    assert record.session_id not in daemon._codex_records
    assert signals == [] and record.path.read_bytes() == original
    # An unchanged scan stays hidden; only a new journal revision reappears.
    monkeypatch.setattr(codex_rollouts, 'load_recent', lambda: [record])
    daemon._refresh_codex_records()
    assert record.session_id not in daemon._codex_records
    assert daemon.hide_codex_session(record.session_id)[0]  # idempotent
    record.revision = (record.revision[0] + 1, record.revision[1] + 1)
    daemon._refresh_codex_records()
    assert record.session_id in daemon._codex_records
    assert signals == [] and record.path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize('away', [False, True])
async def test_hide_doors_refuse_children_and_malformed_input(server, tmp_path, monkeypatch, away):
    srv, daemon, (_, lan_port) = server
    daemon.lan_access_enabled = daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    relay.note_lan_proof(paired['device_id'])
    record = _hide_record(tmp_path, daemon, child=True)
    for ctr, (sid, expected) in enumerate([(record.session_id, 409), ('claude', 409),
                                          ('', 400), ('  ', 400), (None, 400), ([], 400), (5, 400)], 1):
        body = {'action': 'hide_session', 'session_id': sid}
        if away:
            status, _, _ = await srv._remote_run('action', body, paired['device_id'])
        else:
            status, _ = await _home_post(lan_port, paired['key'], 'action', body, ctr)
        assert status == expected
    assert record.session_id in daemon._codex_records and record.path.exists()


@pytest.mark.asyncio
async def test_hide_home_origin_replay_revocation_and_independent_allowlist(server, monkeypatch):
    srv, daemon, (_, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    calls = []
    monkeypatch.setattr(daemon, 'hide_codex_session', lambda sid: (calls.append(sid) or True, 'hidden'))
    body = {'action': 'hide_session', 'session_id': 'codex:hide'}
    status, _ = await _home_post(lan_port, paired['key'], 'action', body, 1,
                                headers={'Origin': 'https://example.invalid'})
    assert status == 403 and calls == []
    monkeypatch.setattr(ApiServer, 'REMOTE_ACTIONS', tuple(x for x in ApiServer.REMOTE_ACTIONS if x != 'hide_session'))
    status, _ = await _home_post(lan_port, paired['key'], 'action', body, 1)
    assert status == 200 and calls == ['codex:hide']
    # Replay uses the raw helper since home answers stale counters as `err`.
    wire = relay.seal_frame(paired['key'], relay.DIR_PHONE_TO_MAC, 1, 'action', body, ns=relay.HOME)
    await fetch('/api/home', port=lan_port, data=wire.encode(), headers={
        'X-Bob-Channel': relay.channel_id(paired['key'], ns=relay.HOME)})
    assert calls == ['codex:hide']
    monkeypatch.setattr(ApiServer, 'LAN_ACTIONS', tuple(x for x in ApiServer.LAN_ACTIONS if x != 'hide_session'))
    status, _ = await _home_post(lan_port, paired['key'], 'action', body, 2)
    assert status == 404 and len(calls) == 1
    daemon.unpair_device(paired['device_id'])
    status, _ = await _home_post(lan_port, paired['key'], 'action', body, 3)
    assert status == 403 and len(calls) == 1


@pytest.mark.asyncio
async def test_hide_away_allowlist_lease_and_revocation(server, monkeypatch):
    srv, daemon, (_, lan_port) = server
    daemon.lan_access_enabled = daemon.remote_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    device_id = paired['device_id']
    relay.note_lan_proof(device_id)
    calls = []
    monkeypatch.setattr(daemon, 'hide_codex_session', lambda sid: (calls.append(sid) or True, 'hidden'))
    body = {'action': 'hide_session', 'session_id': 'codex:hide'}
    saved = ApiServer.REMOTE_ACTIONS
    monkeypatch.setattr(ApiServer, 'REMOTE_ACTIONS', tuple(x for x in saved if x != 'hide_session'))
    assert 'hide_session' in ApiServer.LAN_ACTIONS
    assert (await srv._remote_run('action', body, device_id))[0] == 404
    assert calls == []
    monkeypatch.setattr(ApiServer, 'REMOTE_ACTIONS', saved)
    assert (await srv._remote_run('action', body, device_id))[0] == 200
    assert len(calls) == 1
    real_lease_valid = relay.lease_valid
    monkeypatch.setattr(relay, 'lease_valid', lambda _: False)
    status, _, output = await srv._remote_run('action', body, device_id)
    assert status == 403 and json.loads(output)['detail'] == relay.LEASE_REFUSAL
    monkeypatch.setattr(relay, 'lease_valid', real_lease_valid)
    assert relay.lease_valid(device_id)
    daemon.unpair_device(device_id)
    assert (await srv._remote_run('action', body, device_id))[0] == 403
    assert len(calls) == 1


# --- The pipeline dials, on both doors -------------------------------------


@pytest.mark.asyncio
async def test_home_set_board_parallel_root_executes(server, monkeypatch):
    """A sealed home frame reaches `request_preference` with a root and an
    int, and gets the daemon's own answer back."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    asked = []

    monkeypatch.setattr(BobDaemon, "request_preference",
                        lambda self, key, value: (asked.append((key, value))
                                                  or (True, "asked")))
    status, _out = await _home_post(lan_port, paired["key"], "action", {
        "action": "set_board_parallel_root", "root": "/x", "limit": "2",
    }, 1)
    assert status == 200
    assert asked == [("board_parallel_root", {"root": "/x", "limit": 2})]


@pytest.mark.asyncio
async def test_away_set_board_parallel_root_404s_when_off_the_away_list(
        server, monkeypatch):
    """Home and away are two decisions. With the verb pulled from the away
    tuple it 404s inside the sealed reply even under a live lease."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port))["device_id"]
    relay.note_lan_proof(device_id)
    monkeypatch.setattr(BobDaemon, "request_preference",
                        lambda self, key, value: (True, "asked"))

    payload = {"action": "set_board_parallel_root", "root": "/x", "limit": "2"}
    status, _ctype, _body = await srv._remote_run("action", payload, device_id)
    assert status == 200

    saved = ApiServer.REMOTE_ACTIONS
    try:
        ApiServer.REMOTE_ACTIONS = ("dismiss",)
        status, _ctype, body = await srv._remote_run("action", payload, device_id)
    finally:
        ApiServer.REMOTE_ACTIONS = saved
    assert status == 404


@pytest.mark.asyncio
async def test_away_pipeline_writes_refuse_under_a_lapsed_lease(
        server, monkeypatch):
    """Expiry bounds what the phone may *do*, never what it may see."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port))["device_id"]
    relay.note_lan_proof(device_id)
    monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
    called = []
    monkeypatch.setattr(BobDaemon, "request_preference",
                        lambda self, key, value: (called.append(key)
                                                  or (True, "asked")))
    status, _ctype, body = await srv._remote_run("action", {
        "action": "set_board_autostart", "enabled": "off"}, device_id)
    assert status == 403
    assert relay.LEASE_REFUSAL.split()[0].encode() in body or b"lapsed" in body
    assert called == []

    status, _ctype, _body = await srv._remote_run("state", {}, device_id)
    assert status == 200


# --- the card read, and the approval ------------------------------------------
#
# `card` is a *read* on both doors — beside `state`, `usage` and `log` — so it
# widens no action tuple and checks no away lease. `board_approve_plan` is the
# write that goes with it, and it is on both tuples: reading a plan away from
# the desk and then not being able to say "yes, that one" would leave the
# feature half-built.


def _plan_card(daemon, tmp_path):
    """A Backlog card with a real plan file inside its own root."""
    from dark_army_daemon.board import BoardStore
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    plan = root / "plans" / "p.md"
    plan.write_text("# A plan\n\nDo the thing.\n")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    card, detail = store.create({
        "title": "t", "project": "proj", "root": str(root),
        "prompt": "x" * 900, "tool": "claude", "summary": "s"})
    assert card is not None, detail
    got, detail = store.attach_plan(card["id"], str(plan), "sess-1")
    assert got is not None, detail
    return store, got, plan


@pytest.mark.asyncio
async def test_the_card_read_answers_on_both_doors(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    store, card, plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card",
            {"query": f"card={card['id']}"}, 1)
        assert status == 200
        report = json.loads(body)
        assert report["available"] is True
        # The whole prompt, not the frame's preview.
        assert report["cards"][0]["prompt"] == "x" * 900
        assert "messages" not in report["cards"][0]
        assert report["plan"]["available"] is True
        assert report["plan"]["text"] == plan.read_text()
        assert len(report["plan"]["digest"]) == 64
        # And away, on the same read.
        relay.note_lan_proof(paired["device_id"])
        status, _ctype, out = await srv._remote_run(
            "card", {"query": f"card={card['id']}"}, paired["device_id"])
        assert status == 200
        assert json.loads(out)["plan"]["digest"] == report["plan"]["digest"]
        # A malformed query 400s inside the sealed reply.
        status, _body = await _home_post(lan_port, paired["key"], "card",
                                         {"query": ""}, 2)
        assert status == 400
    finally:
        store.close()
        daemon._board = None


def test_card_sync_is_a_read_and_not_on_either_action_tuple():
    """`log`'s rule, restated for the delta read: a read is not what the away
    lease bounds, and putting it on an action tuple would make a read look
    like a write on both doors."""
    assert "card_sync" not in ApiServer.LAN_ACTIONS
    assert "card_sync" not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_card_sync_is_an_accepted_kind_on_the_home_door(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"ids={card['id']}"}, 1)
        assert status == 200
        assert json.loads(body)["cards"][0]["id"] == card["id"]
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_a_missing_plan_says_why(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, plan = _plan_card(daemon, tmp_path)
    try:
        plan.unlink()
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card",
            {"query": f"card={card['id']}"}, 1)
        assert status == 200
        report = json.loads(body)
        assert report["plan"]["available"] is False
        assert report["plan"]["reason"] == "there is no file at that path"
        assert report["plan"]["digest"] == ""
    finally:
        store.close()
        daemon._board = None


def test_the_card_read_is_a_read_not_an_action():
    assert "card" not in ApiServer.LAN_ACTIONS
    assert "card" not in ApiServer.REMOTE_ACTIONS
    assert "board_approve_plan" in ApiServer.LAN_ACTIONS
    assert "board_approve_plan" in ApiServer.REMOTE_ACTIONS
    assert "board_approve_plan" in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_a_lapsed_lease_still_reads_a_card_and_refuses_the_approval(
        server, tmp_path, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        relay.note_lan_proof(paired["device_id"])
        monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
        status, _ctype, out = await srv._remote_run(
            "card", {"query": f"card={card['id']}"}, paired["device_id"])
        assert status == 200
        assert json.loads(out)["plan"]["available"] is True
        status, _ctype, body = await srv._remote_run("action", {
            "action": "board_approve_plan", "card_id": card["id"],
            "plan_path": card["plan_path"], "plan_digest": "0" * 64},
            paired["device_id"])
        assert status == 403
        assert relay.LEASE_REFUSAL.split()[0].encode() in body \
            or b"lapsed" in body
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_the_done_read_answers_on_both_doors(server, tmp_path):
    """The tenth sealed read, and `log`'s rule: a **read** — no lease, neither
    action tuple — so a phone away from the house still sees its own finished
    column."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path)
    try:
        store.update(card["id"], {"session_id": "sess-9",
                                  "column_name": "in_progress"})
        closed, detail = store.declare_done(card["id"], "sess-9", "finished")
        assert closed is not None, detail
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "done", {"offset": 0}, 1)
        assert status == 200, body
        report = json.loads(body)
        assert report["available"] is True
        assert [c["id"] for c in report["cards"]] == [card["id"]]
        assert len(report["done_view_token"]) == 64
        assert len(report["done_clear_token"]) == 64
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_a_lapsed_lease_still_reads_the_done_column(
        server, tmp_path, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        relay.note_lan_proof(paired["device_id"])
        monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
        status, _ctype, out = await srv._remote_run(
            "done", {"offset": 0}, paired["device_id"])
        assert status == 200
        assert json.loads(out)["available"] is True
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_the_done_read_is_on_neither_action_tuple():
    """A read widens nothing. `done` must never appear where a write is
    allow-listed."""
    assert "done" not in ApiServer.LAN_ACTIONS
    assert "done" not in ApiServer.REMOTE_ACTIONS


@pytest.mark.asyncio
async def test_the_approval_lands_from_the_home_door(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card",
            {"query": f"card={card['id']}"}, 1)
        digest = json.loads(body)["plan"]["digest"]
        status, out = await _home_post(lan_port, paired["key"], "action", {
            "action": "board_approve_plan", "card_id": card["id"],
            "plan_path": card["plan_path"], "plan_digest": digest}, 2)
        assert status == 200, out
        assert store.get(card["id"])["plan_approved"] == digest
        # A digest that is not what is on disk is refused in words.
        status, out = await _home_post(lan_port, paired["key"], "action", {
            "action": "board_approve_plan", "card_id": card["id"],
            "plan_path": card["plan_path"], "plan_digest": "0" * 64}, 3)
        assert status == 409
        assert "changed" in json.loads(out)["detail"]
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_board_message_is_on_every_list_the_doors_read():
    """The Send-a-message verb, chosen four times over.

    `BOARD_ACTIONS` is the loopback gate, `_LAN_BOARD` is what `_lan_run`
    hands straight to `_board_action`, and the two phone tuples are two
    separate decisions — at home and from anywhere in the world. Away the
    argument is that the reach is already licensed (`reply` and
    `answer_question` both carry free text into a session Dark Army is running) and
    that this one reaches a strictly narrower set of sessions; the two things
    a terminal can do that a channel cannot — run a slash command, confirm a
    permission dialog — are each refused by a named constant in the daemon.
    """
    assert "board_message" in ApiServer.BOARD_ACTIONS
    assert "board_message" in ApiServer._LAN_BOARD
    assert "board_message" in ApiServer.LAN_ACTIONS
    assert "board_message" in ApiServer.REMOTE_ACTIONS
    # The standing bound, restated where it could newly have been broken.
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_lan_board_message_in_the_clear_is_426(server, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    await _pair_plain(lan_port)

    async def fake(self, card_id, text):
        raise AssertionError("an unsealed message must never reach the daemon")

    monkeypatch.setattr(BobDaemon, "message_card", fake)
    body = json.dumps({
        "action": "board_message", "card_id": "c1", "text": "hello",
    }).encode()
    status, out = await fetch("/api/action", port=lan_port, data=body)
    assert status == 426
    assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL


def test_an_unauthorised_loopback_board_message_is_not_routed():
    """`_board_request` returns None without a token, and `_route` answers
    the 403 — the gate is in one place and this verb did not open a second."""
    from dark_army_daemon import api_server

    srv = api_server.ApiServer(object())
    srv.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "", {},
        b'{"action": "board_message", "card_id": "c1", "text": "hi"}')
    assert srv._board_request(request) is None
    ok = api_server._Request(
        "POST", "/api/action", "", {"x-bob-token": "t"},
        b'{"action": "board_message", "card_id": "c1", "text": "hi"}')
    assert srv._board_request(ok)[0] == "board_message"
