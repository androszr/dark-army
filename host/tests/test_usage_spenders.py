"""The same additive provider groups through local, sealed home and away reads."""
import json
from types import SimpleNamespace

import pytest

from dark_army_daemon import api_server, codex_spenders
from dark_army_daemon.history import HistoryStore
from tests.test_codex_spenders import NOW, count, meta, model, write


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(api_server.time, "time", lambda: NOW)
    monkeypatch.setattr(api_server.limits, "snapshot", lambda _: {
        "available": True, "bars": [{"kind": "session", "provider": "claude",
          "label": "5h", "resets_at": NOW + 17900}]})
    monkeypatch.setattr(api_server.claude_usage, "get_snapshot", lambda: {})
    monkeypatch.setattr(api_server.claude_usage, "merge_snapshot", lambda snapshot, _: snapshot)
    monkeypatch.setattr(api_server.grok_billing, "get_snapshot", lambda: {})
    root = tmp_path / "journals"
    write(root / "a.jsonl", meta(created=NOW - 30000), model("codex-model"),
          count(NOW - 1000, (90, 10), (90, 10)))
    monkeypatch.setattr(codex_spenders, "reader", codex_spenders.CodexSpenders(root, clock=lambda: NOW))
    daemon = SimpleNamespace(_history=None, codex_usage_snapshot=lambda: {
        "available": True, "bars": [{"kind": "codex_primary", "provider": "codex",
            "label": "5h", "window_minutes": 300, "resets_at": NOW + 10000}]})
    return api_server.ApiServer(daemon)


async def report(server, query=""):
    status, _, body = await server._usage_report_for(query)
    assert status == 200
    return json.loads(body)


@pytest.mark.asyncio
async def test_codex_available_without_history_and_own_reset(server):
    data = await report(server)
    assert data["attribution"]["available"] is False  # old consumer contract
    group, = data["attribution"]["providers"]
    assert group["provider"] == "codex" and group["available"]
    assert group["models"][0]["tokens"] == 100
    assert group["window_start"] == NOW + 10000 - 18000
    assert group["measurement"] == "local_token_share"
    assert "cost_usd" not in group["models"][0]


@pytest.mark.asyncio
async def test_all_three_read_doors_return_same_groups(server):
    local = await server._usage_report(api_server._Request("GET", "/api/usage", "", {}, b""))
    home = await server._sealed_run("usage", {}, "phone", actions=server.LAN_ACTIONS,
                                    check_lease=False, record=False)
    away = await server._sealed_run("usage", {}, "phone", actions=server.REMOTE_ACTIONS,
                                    check_lease=True, record=True)
    assert local == home == away


@pytest.mark.asyncio
async def test_providers_separate_and_grok_never_borrows_claude_window(server, tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.connect()
    try:
        server._daemon._history = store
        for provider in ("claude", "grok"):
            store.add_turn(provider, NOW - 50, message_id=provider,
                           provider=provider, model="claude-opus-4-8", output_tokens=100)
        store.add_turn("grok", NOW - 1000, message_id="old-grok",
                       provider="grok", model="claude-opus-4-8", output_tokens=100)
        data = await report(server)
        groups = {g["provider"]: g for g in data["attribution"]["providers"]}
        assert set(groups) == {"claude", "codex", "grok"}
        assert groups["claude"]["models"][0]["turns"] == 1
        assert groups["grok"]["models"][0]["turns"] == 2
        assert groups["grok"]["window_start"] == NOW - 18000
        assert groups["claude"]["window_start"] == NOW - 100
        assert groups["claude"]["models"][0]["pct"] == 100
        assert data["attribution"]["models"]  # still readable by old clients
    finally:
        store.close()


@pytest.mark.asyncio
async def test_missing_and_failed_journals_leave_limits_usable(server, monkeypatch, tmp_path):
    monkeypatch.setattr(codex_spenders, "reader", codex_spenders.CodexSpenders(tmp_path / "missing"))
    missing = await report(server)
    group, = missing["attribution"]["providers"]
    assert not group["available"] and group["reason"]
    def fail(*args):
        raise OSError("fixture failure")
    monkeypatch.setattr(codex_spenders.reader, "snapshot", fail)
    data = await report(server)
    assert data["limits"] == missing["limits"]
    assert data["attribution"]["providers"][0]["partial"]


@pytest.mark.asyncio
@pytest.mark.parametrize("window,seconds", [("session", 18000), ("week", 604800), ("day", 86400)])
async def test_rolling_window_fallback_and_future_timestamp_filter(server, window, seconds):
    server._daemon.codex_usage_snapshot = lambda: {"bars": []}
    data = await report(server, "window=" + window)
    group, = data["attribution"]["providers"]
    assert group["window_start"] == NOW - seconds
    assert group["window_end"] == NOW
    assert group["window_label"].startswith("rolling")
