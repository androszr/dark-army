import json

import pytest

from dark_army_daemon import devices, enrollment, relay
from dark_army_daemon.decision_capture import DecisionCapture
from dark_army_daemon.decision_store import DecisionStore
from tests.test_home_seal import (server, ledger, machine, attach_dir, _no_fleet_snapshot,
                            pair_plain, inner, fetch)


async def capture(daemon, tmp_path):
    result = DecisionCapture(DecisionStore(tmp_path / "decisions.db"))
    await result.call("open")
    daemon._decisions = result
    await result.call("observe", "q", dict(root="/a", project="same", session_id="s", question_text="Choose?"))
    return result


@pytest.mark.asyncio
async def test_actual_sealed_home_away_loopback_parity_and_expired_lease(server, tmp_path, monkeypatch):
    srv, daemon, port = server
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: {"/a"})
    history = await capture(daemon, tmp_path)
    try:
        phone = await pair_plain(port)
        did = devices.device_for_home_channel(relay.channel_id(phone["key"], ns=relay.HOME))
        status, body = await inner(port, phone["key"], "catch_up", {"query": "limit=100"}, 1)
        assert status == 200
        home = json.loads(body)
        monkeypatch.setattr(relay, "lease_valid", lambda _: False)
        status, _, body = await srv._remote_run("catch_up", {"query": "limit=100"}, did)
        assert status == 200 and json.loads(body) == home
        status, body = await fetch("/api/catch-up?limit=100", port=srv._port)
        assert status == 200 and json.loads(body) == home
        status, _, _ = await srv._remote_run("action", {"action": "reply"}, did)
        assert status == 403
        status, _ = await fetch("/api/catch-up", port=port)
        assert status in (404, 426)
    finally:
        await history.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["cursor=-1", "cursor=NaN", "limit=0", "limit=101", "receipt_id=bad", "receipt_id=00000000-0000-4000-8000-000000000001&cursor=0", "cursor=1&cursor=2", "since=inf", "since=5&until=4", "alien=1"])
async def test_strict_query_validation(server, query):
    srv, _, _ = server
    status, _, _ = await srv._catch_up_report(query)
    assert status == 400


@pytest.mark.asyncio
async def test_receipt_ownership_revocation_and_enrollment(server, tmp_path, monkeypatch):
    srv, daemon, port = server
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: {"/a"})
    history = await capture(daemon, tmp_path)
    try:
        one = await pair_plain(port, "one"); two = await pair_plain(port, "two")
        did = devices.device_for_home_channel(relay.channel_id(one["key"], ns=relay.HOME))
        item = (await history.call("query", roots={"/a"}))["items"][0]
        receipt = await history.call("receipt", did, [item["id"]])
        query = {"query": "receipt_id=" + receipt}
        _, body = await inner(port, one["key"], "catch_up", query, 1)
        assert json.loads(body)["available"]
        _, body = await inner(port, two["key"], "catch_up", query, 1)
        assert not json.loads(body)["available"]
        monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set())
        _, body = await inner(port, one["key"], "catch_up", query, 2)
        assert json.loads(body)["items"] == []
        devices.unpair(did)
        status, _, _ = await srv._catch_up_report(query["query"], did)
        assert status == 403
    finally:
        await history.close()


@pytest.mark.asyncio
async def test_missing_store_named_unavailability(server):
    srv, _, _ = server
    status, _, body = await srv._catch_up_report("")
    assert status == 200
    assert not json.loads(body)["available"]
    assert json.loads(body)["reason"]


@pytest.mark.asyncio
async def test_enrollment_removed_while_history_query_waits_is_not_exposed(server, tmp_path, monkeypatch):
    srv, daemon, _ = server
    roots = {"/a"}
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(roots))
    history = await capture(daemon, tmp_path)
    original = history.call
    async def query_then_revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        roots.clear()
        return result
    monkeypatch.setattr(history, "call", query_then_revoke)
    try:
        status, _, body = await srv._catch_up_report("")
        result = json.loads(body)
        assert status == 200 and not result["available"] and result["items"] == []
        assert "enrollment changed" in result["reason"]
    finally:
        await history.close()
