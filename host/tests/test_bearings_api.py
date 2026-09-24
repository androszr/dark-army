# host/tests/test_bearings_api.py
"""How the Bearings digest is served: loopback GET and the sealed read.

The sealed half uses `test_home_seal`'s free-port fixtures, so nothing here
binds a fixed port or reaches the running fleet's files.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request

import pytest

from dark_army_daemon import devices, relay
from dark_army_daemon.api_server import HOME_UPDATE_REFUSAL, ApiServer
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.event_log import EventLog, _has_forbidden
from tests.test_home_seal import (  # noqa: F401  (fixtures)
    server, ledger, machine, attach_dir, _no_fleet_snapshot,
    pair_plain, inner, fetch,
)


def _row(sid, nickname="", idle=0.0, **extra):
    row = {"session_id": sid, "nickname": nickname, "idle_seconds": idle}
    row.update(extra)
    return row


def _prompt_fleet(daemon, monkeypatch):
    monkeypatch.setattr(daemon, "_prompts_by_session", lambda: {
        "s1": {"tool_name": "Bash", "description": "ls",
               "port": 9999, "request_id": "r1", "claim": "cl41m"},
    })
    daemon._agents_snapshot_cache = {
        "running": [_row("s1", "Cipher", 1, project="alpha")],
    }


# --- the loopback report -----------------------------------------------------


@pytest.mark.asyncio
async def test_loopback_bearings_is_the_snapshot_sorted(server):
    api, daemon, _lan = server
    status, body = await fetch("/api/bearings", port=api._port)
    report = json.loads(body)
    assert status == 200
    assert len(report["sections"]) == 4
    assert [section["key"] for section in report["sections"]] == [
        "needs_your_call", "recently_landed", "underway", "coming_next"]
    assert BobDaemon._pipeline_writable(daemon)["bearings_supported"] is True

    def _text():
        req = urllib.request.Request(
            f"http://127.0.0.1:{api._port}/api/bearings/text")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()

    status, ctype, text = await asyncio.to_thread(_text)
    assert status == 200
    assert ctype == "text/plain; charset=utf-8"
    assert text.decode() == report["text"]


@pytest.mark.asyncio
async def test_since_is_validated_like_the_diary(server, tmp_path):
    api, daemon, _lan = server
    log = EventLog(path=tmp_path / "event-log.jsonl")
    log.open()
    log.append("card_done", title="Landed", nickname="Vex")
    daemon._event_log = log
    try:
        for raw in ("-1", "abc", "inf", "nan"):
            status, body = await fetch(f"/api/bearings?since={raw}",
                                       port=api._port)
            assert status == 400, raw
            assert json.loads(body)["error"] == (
                "since must be a number of seconds ≥ 0")
        status, body = await fetch("/api/bearings?since=0", port=api._port)
        assert status == 200
        unbounded = json.loads(body)
        assert unbounded["sections"][1]["items"]

        status, body = await fetch(
            f"/api/bearings?since={time.time() + 10}", port=api._port)
        assert status == 200
        bounded = json.loads(body)
        assert bounded["sections"][1]["items"] == []
        assert bounded["sections"][1]["empty"] == "Nothing has landed lately."
        for index in (0, 2, 3):
            assert bounded["sections"][index] == unbounded["sections"][index]
    finally:
        log.close()
        daemon._event_log = None


def test_bearings_is_a_read_not_an_action():
    assert "bearings" not in ApiServer.LAN_ACTIONS
    assert "bearings" not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


@pytest.mark.asyncio
async def test_the_sealed_read_answers_at_home_and_away_without_a_lease(
        server, monkeypatch):
    api, daemon, lan_port = server
    phone = await pair_plain(lan_port)
    did = devices.device_for_home_channel(
        relay.channel_id(phone["key"], ns=relay.HOME))
    monkeypatch.setattr(relay, "lease_valid", lambda _: False)
    before = len(daemon._remote_activity)

    status, body = await inner(lan_port, phone["key"], "bearings",
                               {"query": ""}, 1)
    home = json.loads(body)
    assert status == 200
    assert len(home["sections"]) == 4

    status, _ctype, away = await api._remote_run(
        "bearings", {"query": ""}, did)
    assert status == 200
    assert json.loads(away)["sections"] == home["sections"]
    assert len(daemon._remote_activity) == before, "a read records nothing"


@pytest.mark.asyncio
async def test_plaintext_lan_bearings_is_426(server):
    _api, _daemon, lan_port = server
    for path in ("/api/bearings", "/api/bearings/text"):
        status, out = await fetch(path, port=lan_port)
        assert status == 426, path
        assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL


@pytest.mark.asyncio
async def test_no_secret_rides_the_wire(server, monkeypatch):
    api, daemon, lan_port = server
    _prompt_fleet(daemon, monkeypatch)
    phone = await pair_plain(lan_port)
    did = devices.device_for_home_channel(
        relay.channel_id(phone["key"], ns=relay.HOME))
    home_key = phone["key"].hex()
    channel = relay.channel_id(phone["key"], ns=relay.HOME)

    status, loop_body = await fetch("/api/bearings", port=api._port)
    loop = json.loads(loop_body)
    assert status == 200
    status, home_body = await inner(lan_port, phone["key"], "bearings",
                                    {"query": ""}, 1)
    home = json.loads(home_body)
    assert status == 200
    status, _ctype, away_body = await api._remote_run(
        "bearings", {"query": ""}, did)
    away = json.loads(away_body)
    assert status == 200

    for payload in (loop, home, away):
        dumped = json.dumps(payload)
        for section in payload["sections"]:
            assert _has_forbidden(section["items"]) is None
        assert "port" not in dumped
        assert "9999" not in dumped
        assert "claim" not in dumped
        assert "cl41m" not in dumped
        assert home_key not in dumped
        assert channel not in dumped
        assert "state_digest" not in dumped
    assert "Cipher wants to run Bash: ls" in loop["text"]


@pytest.mark.asyncio
async def test_a_daemon_without_a_diary_says_so(server):
    api, daemon, _lan = server
    daemon._event_log = None
    status, body = await fetch("/api/bearings", port=api._port)
    report = json.loads(body)
    assert status == 200
    assert report["diary_available"] is False
    landed = report["sections"][1]
    assert landed["key"] == "recently_landed"
    assert landed["items"] == []
    assert landed["empty"] == "Nothing has landed lately."
