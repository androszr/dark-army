"""Hermetic door pins for inbox_ack: loopback, LAN, away, stale, permission.

Seam: stub live subjects on a real BobDaemon, fake device, lease.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import devices, lan_hosts, paths, relay
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon
from tests.pake_pair import pair_typed
from dark_army_daemon.inbox_ack import (
    INBOX_ACK_KIND_REFUSAL,
    INBOX_ACK_STALE_REFUSAL,
    fingerprint,
    question_material,
)
from tests.free_ports import free_ports


def _free_ports(count: int) -> list[int]:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_ports(count)


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


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


@pytest.fixture(autouse=True)
def machine(monkeypatch):
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
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    devices.reset()
    relay.reset()
    yield
    devices.reset()
    relay.reset()


@pytest_asyncio.fixture
async def server(token_path, ports, tmp_path):
    loop_port, _lan_port = ports
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
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


async def _pair_plain(lan_port: int, name: str = "phone") -> dict:
    return await pair_typed(fetch, lan_port, name=name)


async def _home_post(lan_port: int, key: bytes, kind: str, body: dict, ctr: int):
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, kind, body,
                            ns=relay.HOME)
    sent = {"X-Bob-Channel": relay.channel_id(key, ns=relay.HOME),
            "Content-Type": "text/plain"}
    status, out = await fetch("/api/home", port=lan_port, data=wire.encode(),
                              headers=sent)
    if status != 200:
        return status, out
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, out.decode(),
                                  0, ns=relay.HOME)
    assert err == "", err
    assert frame["kind"] == "reply", frame
    return int(frame["body"]["status"]), frame["body"]["body"]


def _seed_question(daemon, sid="s1", qid="tool-1", text="ok?"):
    daemon._agents_snapshot_cache = {
        "running": [],
        "sleeping": [],
        "waiting": [{
            "session_id": sid,
            "questions": [{"id": qid, "text": text}],
            "question": {"id": qid, "text": text},
        }],
        "abandoned": [],
        "finished": [],
    }
    material = question_material([{"id": qid, "text": text}])
    return "s:" + sid, "question", fingerprint("question", material)


def _action_body(key, kind, fp):
    return json.dumps({
        "action": "inbox_ack", "key": key, "kind": kind, "fingerprint": fp,
    }).encode()


def test_both_tuples_and_remote_subset_of_lan():
    assert "inbox_ack" in ApiServer.LAN_ACTIONS
    assert "inbox_ack" in ApiServer.REMOTE_ACTIONS
    assert "inbox_ack" not in ApiServer.BOARD_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_lan_actions_inbox_ack_comment_has_no_parenthesis():
    text = open(api_mod.__file__, encoding="utf-8").read()
    lan = text.split("LAN_ACTIONS = (", 1)[1].split("REMOTE_ACTIONS = (", 1)[0]
    block = lan.split('"inbox_ack"', 1)[0].rsplit("#", 1)[-1]
    assert "(" not in block and ")" not in block
    remote = text.split("REMOTE_ACTIONS = (", 1)[1].split("_LAN_BOARD", 1)[0]
    rblock = remote.split('"inbox_ack"', 1)[0].rsplit("#", 1)[-1]
    assert "(" not in rblock and ")" not in rblock


@pytest.mark.asyncio
async def test_loopback_matching_echo_is_200(server):
    srv, daemon, (loop_port, _) = server
    key, kind, fp = _seed_question(daemon)
    status, body = await fetch(
        "/api/action", port=loop_port, data=_action_body(key, kind, fp),
        headers={"X-Bob-Token": srv.token})
    assert status == 200, body
    payload = json.loads(body)
    assert payload["ok"] is True
    records = daemon.inbox_snapshot()["acks"]
    assert records == [{"key": key, "kind": kind, "fp": fp}]


@pytest.mark.asyncio
async def test_stale_fingerprint_409s_and_writes_nothing(server):
    srv, daemon, (loop_port, _) = server
    key, kind, _fp = _seed_question(daemon)
    status, body = await fetch(
        "/api/action", port=loop_port,
        data=_action_body(key, kind, "question:deadbeef"),
        headers={"X-Bob-Token": srv.token})
    assert status == 409, body
    payload = json.loads(body)
    assert payload["ok"] is False
    assert payload["detail"] == INBOX_ACK_STALE_REFUSAL
    assert daemon.inbox_snapshot()["acks"] == []


@pytest.mark.asyncio
async def test_permission_kind_409s(server):
    srv, daemon, (loop_port, _) = server
    _seed_question(daemon)
    status, body = await fetch(
        "/api/action", port=loop_port,
        data=_action_body("s:s1", "permission", "permission:x"),
        headers={"X-Bob-Token": srv.token})
    assert status == 409, body
    assert json.loads(body)["detail"] == INBOX_ACK_KIND_REFUSAL
    assert daemon.inbox_snapshot()["acks"] == []


@pytest.mark.asyncio
async def test_ended_work_is_dismissable_and_retired_kinds_are_not(server):
    """Dismiss is one verb: a card whose assistant has gone can be hidden
    until it changes (the card itself stays In progress). An ack for a kind
    the inbox no longer lists is refused, whichever client sent it."""
    srv, daemon, (loop_port, _) = server
    daemon._agents_snapshot_cache = {
        "running": [], "sleeping": [], "waiting": [],
        "abandoned": [], "finished": []}
    daemon._board_state = {"cards": [
        {"id": "e1", "needs_you": True, "column_name": "in_progress"},
        {"id": "pl", "column_name": "backlog", "plan_path": "p.md",
         "session_id": "", "refine_state": ""},
    ]}
    headers = {"X-Bob-Token": srv.token}
    status, body = await fetch(
        "/api/action", port=loop_port,
        data=_action_body("c:e1", "ended_work", fingerprint("ended_work", "")),
        headers=headers)
    assert status == 200, body
    assert daemon.inbox_snapshot()["acks"] == [
        {"key": "c:e1", "kind": "ended_work", "fp": fingerprint("ended_work", "")}]
    assert daemon._board_state["cards"][0]["column_name"] == "in_progress"
    status, body = await fetch(
        "/api/action", port=loop_port,
        data=_action_body("c:pl", "plan_ready", fingerprint("plan_ready", "p.md")),
        headers=headers)
    assert status == 409, body
    assert json.loads(body)["detail"] == INBOX_ACK_KIND_REFUSAL


@pytest.mark.asyncio
async def test_idempotent_second_press_is_200(server):
    srv, daemon, (loop_port, _) = server
    key, kind, fp = _seed_question(daemon)
    headers = {"X-Bob-Token": srv.token}
    first, _ = await fetch("/api/action", port=loop_port,
                           data=_action_body(key, kind, fp), headers=headers)
    second, body = await fetch("/api/action", port=loop_port,
                               data=_action_body(key, kind, fp), headers=headers)
    assert first == 200 and second == 200, body
    assert len(daemon.inbox_snapshot()["acks"]) == 1


@pytest.mark.asyncio
async def test_state_before_agents_cache_keeps_session_acks(server):
    """SSE attach calls state() before the first agents push."""
    srv, daemon, (loop_port, _) = server
    assert "_agents_snapshot_cache" not in daemon.__dict__
    fp = fingerprint("question", "tool-1")
    daemon._inbox_acks.ack("s:codex1", "question", fp)
    path = daemon._inbox_acks.path
    status, body = await fetch("/api/state", port=loop_port)
    assert status == 200, body
    state = json.loads(body)
    assert any(row["key"] == "s:codex1" for row in state["inbox"]["acks"])
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert any(row["key"] == "s:codex1" for row in disk["acks"])


@pytest.mark.asyncio
async def test_inbox_available_on_state(server):
    srv, daemon, (loop_port, _) = server
    status, body = await fetch("/api/state", port=loop_port)
    assert status == 200
    state = json.loads(body)
    assert state["inbox"]["available"] is True
    assert state["inbox"]["acks"] == []
    acks = state["inbox"]["acks"]
    blob = json.dumps(acks)
    assert "token" not in blob
    assert "claim" not in blob
    assert "channel" not in blob


def test_absent_section_on_stub_without_the_method():
    class Stub:
        _global_signals = []
        _mesh = []
        _agents_poller = type("P", (), {"available": None, "last_error": ""})()

        def _permission_snapshot(self):
            return []

    srv = ApiServer(Stub(), port=0)
    assert srv.state()["inbox"] == {}


@pytest.mark.asyncio
async def test_sealed_lan_200(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    key, kind, fp = _seed_question(daemon)
    status, out = await _home_post(lan_port, paired["key"], "action", {
        "action": "inbox_ack", "key": key, "kind": kind, "fingerprint": fp,
    }, 1)
    assert status == 200, out
    payload = json.loads(out) if isinstance(out, (bytes, str)) else out
    if isinstance(payload, bytes):
        payload = json.loads(payload)
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload.get("ok") is True
    assert daemon.inbox_snapshot()["acks"][0]["key"] == key


@pytest.mark.asyncio
async def test_away_200_inside_a_live_lease(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port))["device_id"]
    relay.note_lan_proof(device_id)
    key, kind, fp = _seed_question(daemon)
    status, _ctype, body = await srv._remote_run("action", {
        "action": "inbox_ack", "key": key, "kind": kind, "fingerprint": fp,
    }, device_id)
    assert status == 200, body
    assert json.loads(body)["ok"] is True


@pytest.mark.asyncio
async def test_away_404_when_dropped_from_remote_actions(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port))["device_id"]
    relay.note_lan_proof(device_id)
    key, kind, fp = _seed_question(daemon)
    called = []
    real = srv._lan_run

    async def wrapped(*args, **kwargs):
        called.append(args[0] if args else kwargs.get("action"))
        return await real(*args, **kwargs)

    srv._lan_run = wrapped
    saved = ApiServer.REMOTE_ACTIONS
    try:
        ApiServer.REMOTE_ACTIONS = tuple(
            a for a in ApiServer.REMOTE_ACTIONS if a != "inbox_ack")
        status, _ctype, _body = await srv._remote_run("action", {
            "action": "inbox_ack", "key": key, "kind": kind, "fingerprint": fp,
        }, device_id)
    finally:
        ApiServer.REMOTE_ACTIONS = saved
        srv._lan_run = real
    assert status == 404
    assert called == []
    assert daemon.inbox_snapshot()["acks"] == []


@pytest.mark.asyncio
async def test_lapsed_lease_403_writes_nothing(server, monkeypatch):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    device_id = (await _pair_plain(lan_port))["device_id"]
    relay.note_lan_proof(device_id)
    key, kind, fp = _seed_question(daemon)
    monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
    status, _ctype, body = await srv._remote_run("action", {
        "action": "inbox_ack", "key": key, "kind": kind, "fingerprint": fp,
    }, device_id)
    assert status == 403, body
    assert relay.LEASE_REFUSAL.encode() in body or b"lapsed" in body
    assert daemon.inbox_snapshot()["acks"] == []


# --- an acknowledged session goes quiet everywhere, not only on the Inbox ---

def _seed_waiting(daemon, sid="s2", state="idle"):
    """A finished turn: a notification card, hook state, no question."""
    daemon._agents_snapshot_cache = {
        "running": [], "sleeping": [],
        "waiting": [{"session_id": sid, "questions": [], "question": {}}],
        "abandoned": [], "finished": [],
    }
    daemon._active_notifications[sid] = {
        "session_id": sid, "hook": "Stop", "message": "done?"}
    daemon._session_states[sid] = {
        "state": state, "last_event": 1.0, "last_event_monotonic": 1.0}
    return "s:" + sid, "waiting", fingerprint("waiting", "")


@pytest.mark.asyncio
async def test_waiting_ack_drops_the_card_so_the_row_stops_needing_you(server):
    """The hide list alone left the card, so the strip, widget, Agents row
    and tab dot still said needs you after a person had said they saw it."""
    srv, daemon, (loop_port, _) = server
    key, kind, fp = _seed_waiting(daemon)
    status, body = await fetch(
        "/api/action", port=loop_port, data=_action_body(key, kind, fp),
        headers={"X-Bob-Token": srv.token})
    assert status == 200, body
    assert "s2" not in daemon._active_notifications
    assert daemon._session_states["s2"]["state"] == "idle"
    assert daemon.inbox_snapshot()["acks"] == [
        {"key": key, "kind": kind, "fp": fp}]


@pytest.mark.asyncio
async def test_waiting_ack_demotes_error_to_idle_like_low_priority(server):
    """`categorize` banks `error` under waiting with or without a card."""
    srv, daemon, (loop_port, _) = server
    key, kind, fp = _seed_waiting(daemon, state="error")
    status, body = await fetch(
        "/api/action", port=loop_port, data=_action_body(key, kind, fp),
        headers={"X-Bob-Token": srv.token})
    assert status == 200, body
    st = daemon._session_states["s2"]
    assert st["state"] == "idle"
    assert st["last_event"] > 1.0 and st["last_event_monotonic"] > 1.0


@pytest.mark.asyncio
async def test_question_ack_drops_the_card_but_keeps_a_dialog_state(server):
    """Dismiss's rule per row; a `waiting` state is a dialog still up."""
    srv, daemon, (loop_port, _) = server
    key, kind, fp = _seed_question(daemon)
    daemon._active_notifications["s1"] = {"session_id": "s1", "hook": "Stop"}
    daemon._session_states["s1"] = {
        "state": "waiting", "last_event": 1.0, "last_event_monotonic": 1.0}
    status, body = await fetch(
        "/api/action", port=loop_port, data=_action_body(key, kind, fp),
        headers={"X-Bob-Token": srv.token})
    assert status == 200, body
    assert "s1" not in daemon._active_notifications
    assert daemon._session_states["s1"]["state"] == "waiting"


@pytest.mark.asyncio
async def test_refused_ack_touches_no_card_and_no_state(server):
    srv, daemon, (loop_port, _) = server
    key, kind, _fp = _seed_waiting(daemon, state="error")
    status, _body = await fetch(
        "/api/action", port=loop_port,
        data=_action_body(key, kind, fingerprint("waiting", "x") + "stale"),
        headers={"X-Bob-Token": srv.token})
    assert status == 409
    assert "s2" in daemon._active_notifications
    assert daemon._session_states["s2"]["state"] == "error"
