"""Last-known Grok / Codex figures survive a quiet source and a restart."""

import json
import os

from dark_army_daemon import codex_rollouts, grok_billing, usage_hold

LIVE = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-08-14T21:57:10.999844+00:00",
            "end": "2026-08-21T21:57:10.999844+00:00",
        },
        "creditUsagePercent": 36.0,
        "productUsage": [{"product": "GrokBuild", "usagePercent": 36.0}],
    }
}


def _write_rollout(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_grok_snapshot_survives_a_restart_when_the_fetch_fails():
    grok_billing.reset_cache()
    grok_billing.get_snapshot(
        now=1.0, fetch=lambda: grok_billing.parse_billing(LIVE, now=1.0),
    )
    grok_billing.reset_cache()
    held = grok_billing.get_snapshot(
        now=1.0 + grok_billing.TTL_SECONDS + 1, fetch=lambda: {})
    assert held["percent"] == 36.0


def test_codex_usage_serves_the_held_bars_when_journals_go_quiet(
    tmp_path, monkeypatch,
):
    path = tmp_path / "2026/08/29/rollout-once.jsonl"
    _write_rollout(path, [{"timestamp": "2026-08-18T18:00:00Z",
                           "type": "session_meta",
                           "payload": {"type": "session_meta", "id": "abc"}}])
    record = codex_rollouts.CodexRecord(
        session_id="codex:abc", thread_id="abc", path=path, last_event=200,
        limit_bars=[{"kind": "codex_secondary", "percent": 29,
                     "label": "7d", "resets_at": 5000}],
    )
    monkeypatch.setattr(codex_rollouts, "_USAGE_CACHE", {})
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(codex_rollouts, "parse_rollout", lambda _p: record)

    first = codex_rollouts.usage_snapshot(now=300)
    assert first["available"] is True
    assert first["bars"][0]["percent"] == 29

    os.unlink(path)
    monkeypatch.setattr(codex_rollouts, "_USAGE_CACHE", {})
    second = codex_rollouts.usage_snapshot(now=400)
    assert second["available"] is True
    assert second["bars"][0]["percent"] == 29
    assert second["bars"][0]["stale"] is False


def test_codex_held_bar_is_stale_after_its_reset(tmp_path, monkeypatch):
    usage_hold.save("codex", {
        "available": True,
        "provider": "codex",
        "bars": [{"kind": "codex_secondary", "percent": 29, "resets_at": 100}],
        "fetched_at": 50,
    })
    monkeypatch.setattr(codex_rollouts, "_USAGE_CACHE", {})
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    snap = codex_rollouts.usage_snapshot(now=200)
    assert snap["available"] is True
    assert snap["bars"][0]["stale"] is True


def test_usage_hold_ignores_an_empty_save(tmp_path):
    usage_hold.save("grok", {})
    assert usage_hold.load("grok") == {}
