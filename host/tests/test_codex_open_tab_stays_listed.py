"""A finished Codex run whose tab is still open stays on the roster.

The roster's 15-minute grace is how a *closed* Codex session leaves. A turn
that finished with its terminal still open goes just as quiet, and dropping
it lost the record every Close path needs: from then on the tab could only be
hidden, never closed (two fit-app runs on 23 Sep 2026 were left open that
way). A journal a live native Codex process still holds open is kept; one
nobody holds leaves exactly as before.
"""

import json
import os
import time

from dark_army_daemon import codex_rollouts


def _rollout(path, thread):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": "2026-09-23T12:00:00Z", "type": "session_meta",
         "payload": {"type": "session_meta", "id": thread, "cwd": "/code/fit",
                     "originator": "codex-tui", "source": "cli", "thread_source": "user"}},
        {"timestamp": "2026-09-23T12:00:01Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t1"}},
        {"timestamp": "2026-09-23T12:05:00Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "t1"}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    old = time.time() - (codex_rollouts.LIVE_GRACE_SECONDS + 3600)
    os.utime(path, (old, old))


def _stub_processes(monkeypatch, held_paths, snapshot=("proc",)):
    calls = []

    def snap():
        calls.append("snapshot")
        return list(snapshot) if snapshot is not None else None

    monkeypatch.setattr(codex_rollouts, "process_snapshot_shared", snap)
    monkeypatch.setattr(codex_rollouts, "_native_processes",
                        lambda procs: [(p, None) for p in procs])
    monkeypatch.setattr(codex_rollouts, "_open_identities",
                        lambda _p: [codex_rollouts._journal_identity(p) for p in held_paths])
    monkeypatch.setattr(codex_rollouts, "attach_process_ids", lambda _records: None)
    return calls


def test_a_quiet_journal_a_live_codex_holds_open_is_kept(tmp_path, monkeypatch):
    held = tmp_path / "2026/09/23/rollout-held.jsonl"
    gone = tmp_path / "2026/09/23/rollout-gone.jsonl"
    _rollout(held, "held")
    _rollout(gone, "gone")
    _stub_processes(monkeypatch, [held])
    ids = {r.session_id for r in codex_rollouts.load_recent(tmp_path)}
    assert "codex:held" in ids
    assert "codex:gone" not in ids


def test_nothing_is_kept_when_the_process_table_cannot_be_read(tmp_path, monkeypatch):
    held = tmp_path / "2026/09/23/rollout-held.jsonl"
    _rollout(held, "held")
    _stub_processes(monkeypatch, [held], snapshot=None)
    assert codex_rollouts.load_recent(tmp_path) == []


def test_a_fresh_scan_never_walks_the_process_table_for_this(tmp_path, monkeypatch):
    fresh = tmp_path / "2026/09/23/rollout-fresh.jsonl"
    _rollout(fresh, "fresh")
    now = time.time()
    os.utime(fresh, (now, now))
    calls = _stub_processes(monkeypatch, [])
    ids = {r.session_id for r in codex_rollouts.load_recent(tmp_path)}
    assert ids == {"codex:fresh"}
    assert calls == []


def test_the_open_file_walk_runs_once_per_shared_process_scan(tmp_path, monkeypatch):
    """Most of the newest journals are past the grace on a real machine, so
    the walk must not repeat on every two-second roster refresh."""
    held = tmp_path / "2026/09/23/rollout-held.jsonl"
    _rollout(held, "held")
    snapshot = ["proc"]
    walks = []
    monkeypatch.setattr(codex_rollouts, "process_snapshot_shared", lambda: snapshot)
    monkeypatch.setattr(codex_rollouts, "_native_processes",
                        lambda procs: [(p, None) for p in procs])

    def opened(_proc):
        walks.append(1)
        return [codex_rollouts._journal_identity(held)]

    monkeypatch.setattr(codex_rollouts, "_open_identities", opened)
    monkeypatch.setattr(codex_rollouts, "attach_process_ids", lambda _records: None)
    for _ in range(3):
        ids = {r.session_id for r in codex_rollouts.load_recent(tmp_path)}
        assert "codex:held" in ids
    assert walks == [1]
    snapshot = ["proc"]  # a new scan is walked afresh
    codex_rollouts.load_recent(tmp_path)
    assert walks == [1, 1]
