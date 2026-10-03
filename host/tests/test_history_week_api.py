"""The sealed `history_week` read: the phone's History screen.

One body on three doors — loopback `GET /api/history-week` (token-gated like
`/api/knowledge`), the sealed kind at home and the sealed kind away — cut
from `agent_efficiency_report(7, "")` to a closed key set, so no card title,
person or project folder ever rides the relay. A read: neither action tuple,
no lease, no `remote_activity` record. `docs/sealed-reads-contract.md`,
*`history_week` is a sealed read*.
"""
from __future__ import annotations

import copy
import json

import pytest

from dark_army_daemon import devices, relay
from dark_army_daemon import api_server as api_mod
from dark_army_daemon.agent_report import _SESSION_FIGURES
from dark_army_daemon.api_server import (
    HISTORY_WEEK_BODY_KEYS, HISTORY_WEEK_CARD_KEYS, HISTORY_WEEK_DAY_KEYS,
    HISTORY_WEEK_LIMIT_POINT_KEYS, HISTORY_WEEK_LIMITS_KEYS,
    HISTORY_WEEK_SESSION_KEYS, ApiServer,
)
from dark_army_daemon.daemon import BobDaemon
from tests.test_home_seal import (  # noqa: F401 — fixtures, used by name
    server, ledger, machine, attach_dir, _no_fleet_snapshot,
    pair_plain, inner, fetch,
)

#: Never on the week, at any depth: what a person reads, and what unlocks.
FENCE = {"title", "who", "root", "key", "digest", "claim", "channel", "port",
         "token", "turns", "cost_usd", "model", "manual_steps"}


def _session(sid, **money):
    session = {"provider": "claude", "session_id": sid, "phase": "implementation",
               "bound_at": 1_758_000_000.0, "known": True}
    for key in _SESSION_FIGURES:
        session[key] = None
    session.update({"model": "claude-opus", "output_tokens": 1200,
                    "duration_ms": 90_000, "measured_cost_usd": 3.0})
    session.update(money)
    return session


def _report(cards=None):
    return {
        "supported": True, "available": True, "root": "",
        "range_days": 7, "from": 1_757_400_000.0, "to": 1_758_004_800.0,
        "generated_at": 1_758_004_800.0, "sessions_truncated": False,
        "agent_scope": "machine", "outcomes_available": False,
        "outcomes_reason": "Acceptance is reported per project; choose one to see it.",
        "measurements_available": True, "codex_history_partial": False,
        "agents": [{"agent": "bc-implementer", "cost_usd": 4.0}],
        "summary": {},
        "cards": cards if cards is not None else [
            {"card_id": "c1", "title": "Secret plan", "root": "/Users/me/secret",
             "who": "Ada", "turns": 12, "cost_usd": 9.0, "accepted": None,
             "manual_check_outstanding": False,
             "sessions": [
                 _session("s1", token_cost_usd=4.0, claude_token_cost_usd=4.0,
                          reported_cost_usd=6.0),
                 _session("s2", provider="grok", reported_cost_usd=0.0),
             ]},
        ],
        "other_days": [
            {"day": "2025-09-15", "claude_token_cost_usd": 0.5,
             "grok_token_cost_usd": 0.0, "codex_token_cost_usd": 0.25,
             "claude_reported_cost_usd": 0.0,
             "claude_token_unpriced_sessions": 1,
             "grok_token_unpriced_sessions": 0,
             "codex_token_unpriced_sessions": 0,
             "claude_token_unpriced_session_ids": ["s9"],
             "grok_token_unpriced_session_ids": [],
             "codex_token_unpriced_session_ids": [],
             "claude_measured_cost_usd": 2.0, "estimated_cost_usd": 1.0,
             "claude_output_tokens": 800, "unpriced_sessions": 1},
        ],
    }


@pytest.fixture
def stub(server):
    api, daemon, port = server
    calls = []
    fixture = {"report": _report()}

    async def agent_efficiency_report(days, root=""):
        calls.append((days, root))
        return copy.deepcopy(fixture["report"])

    daemon.agent_efficiency_report = agent_efficiency_report
    return api, daemon, port, calls, fixture


def _keys_at_every_depth(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        for key, inner_value in value.items():
            out.add(key)
            _keys_at_every_depth(inner_value, out)
    elif isinstance(value, list):
        for item in value:
            _keys_at_every_depth(item, out)
    return out


@pytest.mark.asyncio
async def test_three_doors_answer_the_same_bytes(stub, monkeypatch):
    api, daemon, port, calls, _ = stub
    recorded = []
    daemon.record_remote_action = lambda *a: recorded.append(a)
    status, local = await fetch("/api/history-week", port=api._port,
                                headers={"X-Bob-Token": api.token})
    assert status == 200
    phone = await pair_plain(port)
    did = devices.device_for_home_channel(
        relay.channel_id(phone["key"], ns=relay.HOME))
    # A lapsed away window still reads: expiry bounds doing, never seeing.
    monkeypatch.setattr(relay, "lease_valid", lambda _: False)
    status, home = await inner(port, phone["key"], "history_week", {}, 1)
    assert status == 200
    status, _ctype, away = await api._remote_run("history_week", {}, did)
    assert status == 200
    assert local.decode() == home == away.decode()
    assert json.loads(home)["available"] is True
    assert calls and all(call == (7, "") for call in calls)
    assert recorded == []


@pytest.mark.asyncio
async def test_loopback_without_the_token_is_forbidden(stub):
    api, _daemon, _port, calls, _ = stub
    status, out = await fetch("/api/history-week", port=api._port)
    assert status == 403
    assert json.loads(out) == {"error": "forbidden"}
    status, _ = await fetch("/api/history-week", port=api._port,
                            headers={"X-Bob-Token": "wrong"})
    assert status == 403
    assert calls == []


@pytest.mark.asyncio
async def test_the_key_set_is_closed_at_every_depth(stub):
    api = stub[0]
    status, _ctype, body = await api._history_week_for({})
    assert status == 200
    week = json.loads(body)
    assert set(week) <= set(HISTORY_WEEK_BODY_KEYS)
    assert "truncated" not in week
    assert week["range_days"] == 7
    assert week["partial"] is False
    assert week["cards"], "the fixture's card was dropped"
    for card in week["cards"]:
        assert set(card) <= set(HISTORY_WEEK_CARD_KEYS)
        for session in card["sessions"]:
            assert set(session) <= set(HISTORY_WEEK_SESSION_KEYS)
    for day in week["other_days"]:
        assert set(day) <= set(HISTORY_WEEK_DAY_KEYS)
    leaked = _keys_at_every_depth(week) & FENCE
    assert not leaked, leaked
    assert "limits" not in week, "no store is open in this fixture"
    assert "Secret plan" not in body.decode()
    assert "/Users/me/secret" not in body.decode()
    assert "Ada" not in body.decode()


@pytest.mark.asyncio
async def test_absent_stays_absent_and_zero_stays_zero(stub):
    api = stub[0]
    _status, _ctype, body = await api._history_week_for({})
    week = json.loads(body)
    priced, unpriced = week["cards"][0]["sessions"]
    assert priced["token_cost_usd"] == 4.0
    assert priced["reported_cost_usd"] == 6.0
    # A null token cost is absent, never 0 — a 0 prices the day at $0.00.
    assert "token_cost_usd" not in unpriced
    assert "claude_token_cost_usd" not in unpriced
    # A reported 0 is a real report and survives as 0.
    assert unpriced["reported_cost_usd"] == 0.0
    assert unpriced["known"] is True
    day = week["other_days"][0]
    assert day["claude_reported_cost_usd"] == 0.0
    assert "grok_reported_cost_usd" not in day
    assert day["claude_token_unpriced_session_ids"] == ["s9"]


def test_the_read_is_on_neither_action_tuple():
    assert "history_week" not in ApiServer.LAN_ACTIONS
    assert "history_week" not in ApiServer.REMOTE_ACTIONS


@pytest.mark.asyncio
async def test_an_oversized_week_drops_cards_and_says_so(stub):
    api, _daemon, _port, _calls, fixture = stub
    fixture["report"] = _report(cards=[
        {"card_id": f"c{i:04d}", "sessions": [
            _session(f"s{i:04d}-{n}" + "x" * 200, token_cost_usd=1.0)
            for n in range(4)]}
        for i in range(600)])
    _status, _ctype, body = await api._history_week_for({})
    assert len(body) <= 300_000
    week = json.loads(body)
    assert week["available"] is True
    assert week["truncated"] is True
    assert 0 < len(week["cards"]) < 600


@pytest.mark.asyncio
async def test_a_mac_without_the_week_says_so(stub):
    api, daemon, _port, _calls, fixture = stub
    fixture["report"] = {"supported": True, "available": False,
                         "reason": "history database is not open"}
    _status, _ctype, body = await api._history_week_for({})
    assert json.loads(body) == {"supported": True, "available": False,
                                "reason": "history database is not open"}
    daemon.agent_efficiency_report = None
    _status, _ctype, body = await api._history_week_for({})
    week = json.loads(body)
    assert week["available"] is False
    assert week["reason"]


@pytest.mark.asyncio
async def test_the_lan_door_refuses_the_plain_addresses(stub):
    _api, _daemon, port, calls, _ = stub
    status, _ = await fetch("/api/history", port=port)
    assert status == 404
    status, out = await fetch("/api/history-week", port=port)
    assert status == 426
    assert json.loads(out)["error"] == api_mod.HOME_UPDATE_REFUSAL
    assert calls == []


def test_the_marker_rides_the_pipeline_flags():
    assert BobDaemon()._pipeline_writable()["history_week_supported"] is True


# --- Claude's limit pressure -------------------------------------------------


def _store_with_samples(tmp_path):
    import time

    from dark_army_daemon.history import HistoryStore

    store = HistoryStore(tmp_path / "history.db")
    store.connect()
    now = time.time()
    store.record_metrics("c1", {"five_hour_pct": 60.0, "seven_day_pct": 20.0,
                                "five_hour_resets_at": now + 3600},
                         ts=now - 600)
    store.record_metrics("c1", {"seven_day_pct": 21.0}, ts=now - 7200)
    store.record_metrics("g1", {"five_hour_pct": 95.0,
                                "five_hour_resets_at": now + 400_000},
                         ts=now - 600, provider="grok")
    return store, now


@pytest.mark.asyncio
async def test_limits_ride_the_week_closed_and_claude_only(stub, tmp_path):
    api, daemon, _port, _calls, _ = stub
    store, now = _store_with_samples(tmp_path)
    daemon._history = store
    try:
        _status, _ctype, body = await api._history_week_for({})
    finally:
        store.close()
    week = json.loads(body)
    limits = week["limits"]
    assert set(limits) <= set(HISTORY_WEEK_LIMITS_KEYS)
    assert {"from", "to", "series", "resets"} <= set(limits)
    for point in limits["series"]:
        assert set(point) <= set(HISTORY_WEEK_LIMIT_POINT_KEYS)
    # Claude's 60%, never Grok's 95%, and never Grok's far-off reset.
    assert max(p["five_hour_pct"] for p in limits["series"]
               if "five_hour_pct" in p) == 60.0
    assert limits["resets"] == [pytest.approx(now + 3600)]
    assert "current" not in limits and "burn_pct_per_hour" not in limits
    assert not _keys_at_every_depth(week) & FENCE


@pytest.mark.asyncio
async def test_a_null_reading_is_a_missing_key_on_the_wire(stub, tmp_path):
    api, daemon, _port, _calls, _ = stub
    store, _now = _store_with_samples(tmp_path)
    daemon._history = store
    try:
        _status, _ctype, body = await api._history_week_for({})
    finally:
        store.close()
    series = json.loads(body)["limits"]["series"]
    assert any("five_hour_pct" not in p and "seven_day_pct" in p for p in series)
    for point in series:
        assert None not in point.values()


@pytest.mark.asyncio
async def test_no_store_or_no_claude_reading_sends_no_limits(stub, tmp_path):
    import time

    from dark_army_daemon.history import HistoryStore

    api, daemon, _port, _calls, _ = stub
    daemon._history = None
    _status, _ctype, body = await api._history_week_for({})
    assert "limits" not in json.loads(body)
    store = HistoryStore(tmp_path / "grok-only.db")
    store.connect()
    store.record_metrics("g1", {"five_hour_pct": 95.0}, ts=time.time() - 60,
                         provider="grok")
    daemon._history = store
    try:
        _status, _ctype, body = await api._history_week_for({})
    finally:
        store.close()
    assert "limits" not in json.loads(body)


@pytest.mark.asyncio
async def test_a_stored_inf_or_nan_cannot_fail_the_week(stub):
    api, daemon, _port, _calls, _ = stub

    class Odd:
        def limits_report(self, days=None):
            inf, nan = float("inf"), float("nan")
            return {"bucket_seconds": 1800, "from": 1.0, "to": inf,
                    "series": [{"ts": 1.0, "five_hour_pct": inf,
                                "seven_day_pct": 5.0},
                               {"ts": nan, "five_hour_pct": 1.0},
                               {"ts": 2.0, "five_hour_pct": nan,
                                "seven_day_pct": nan}],
                    "resets": [inf, nan, 3.0, True]}

    daemon._history = Odd()
    _status, _ctype, body = await api._history_week_for({})
    limits = json.loads(body)["limits"]
    assert limits["series"] == [{"ts": 1.0, "seven_day_pct": 5.0}]
    assert limits["resets"] == [3.0]
    assert "to" not in limits


@pytest.mark.asyncio
async def test_a_failing_limits_read_leaves_the_key_absent(stub):
    api, daemon, _port, _calls, _ = stub

    class Broken:
        def limits_report(self, days=None):
            raise RuntimeError("database is locked")

    daemon._history = Broken()
    _status, _ctype, body = await api._history_week_for({})
    week = json.loads(body)
    assert week["available"] is True
    assert "limits" not in week
