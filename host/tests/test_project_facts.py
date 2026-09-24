# host/tests/test_project_facts.py
"""The per-project facts block that rides the agents snapshot."""

import time
from unittest.mock import patch

from dark_army_daemon.api_server import _news
from dark_army_daemon.daemon import BobDaemon, PROJECT_FACTS_INTERVAL
from dark_army_daemon.history import HistoryStore


def _daemon_with_history(tmp_path, *sessions):
    store = HistoryStore(tmp_path / "history.db")
    store.connect()
    for item in sessions:
        sid, project, last_seen, *rest = item
        cwd = rest[0] if rest else None
        fields = {"project": project, "last_seen": last_seen}
        if cwd is not None:
            fields["cwd"] = cwd
        store.upsert_session(sid, **fields)
    daemon = BobDaemon()
    daemon._history = store
    return daemon, store


def _wrap_facts(store):
    calls = {"n": 0}
    real = store.project_cwd_facts

    def wrapped(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    store.project_cwd_facts = wrapped
    return calls


def _live(daemon, sid, project):
    daemon._session_states[sid] = {
        "state": "working",
        "last_event": time.time(),
        "project": project,
    }


def test_news_drops_last_active_and_keeps_sessions():
    stripped = _news({
        "project_facts": [
            {"project": "x", "sessions": 3, "last_active": 60},
        ],
    })
    assert stripped == {"project_facts": [{"project": "x", "sessions": 3}]}


def test_detailed_snapshot_lists_a_seeded_live_project(tmp_path):
    daemon, store = _daemon_with_history(
        tmp_path, ("s1", "dark-army", 1_700_000_000.0))
    _live(daemon, "s1", "dark-army")
    snap = daemon.detailed_snapshot()
    by_name = {row["project"]: row for row in snap["project_facts"]}
    assert "dark-army" in by_name
    assert by_name["dark-army"]["sessions"] == 1
    store.close()


def test_absent_and_empty_projects_are_not_listed(tmp_path):
    daemon, store = _daemon_with_history(
        tmp_path,
        ("ghost", "gone", 1_700_000_000.0),
        ("other", "", 1_700_000_000.0),
        ("live", "dark-army", 1_700_000_000.0),
    )
    _live(daemon, "s1", "dark-army")
    _live(daemon, "s-empty", "")
    facts = daemon.detailed_snapshot()["project_facts"]
    names = [row["project"] for row in facts]
    assert "dark-army" in names
    assert "gone" not in names
    assert "" not in names
    store.close()


def test_same_name_set_inside_the_interval_hits_the_store_once(tmp_path):
    daemon, store = _daemon_with_history(
        tmp_path, ("s1", "p", 1_700_000_000.0))
    calls = _wrap_facts(store)
    with patch("dark_army_daemon.daemon.time.monotonic", return_value=100.0):
        first = daemon._project_facts_snapshot({"p"})
        second = daemon._project_facts_snapshot({"p"})
    assert calls["n"] == 1
    assert first == second
    store.close()


def test_a_changed_name_set_refreshes_immediately(tmp_path):
    daemon, store = _daemon_with_history(
        tmp_path,
        ("s1", "a", 1_700_000_000.0),
        ("s2", "b", 1_700_000_000.0),
    )
    calls = _wrap_facts(store)
    with patch("dark_army_daemon.daemon.time.monotonic", return_value=100.0):
        daemon._project_facts_snapshot({"a"})
        daemon._project_facts_snapshot({"a", "b"})
    assert calls["n"] == 2
    store.close()


def test_no_history_yields_empty_facts_and_does_not_raise():
    daemon = BobDaemon()
    assert daemon._history is None
    assert daemon._project_facts_snapshot({"p"}) == []
    snap = daemon.detailed_snapshot()
    assert snap["project_facts"] == []


def test_last_active_is_minute_rounded(tmp_path):
    daemon, store = _daemon_with_history(tmp_path, ("s1", "p", 125.0))
    facts = daemon._project_facts_snapshot({"p"})
    assert facts == [{"project": "p", "sessions": 1, "last_active": 120}]
    store.close()


def test_interval_constant_is_one_minute():
    assert PROJECT_FACTS_INTERVAL == 60.0


def test_facts_use_the_workspace_label_not_the_stored_basename(tmp_path):
    daemon, store = _daemon_with_history(
        tmp_path,
        ("s1", "host", 1_700_000_000.0,
         "/Users/x/Code/dark-army/host"),
    )

    def fake_label(cwd, metrics=None, fallback=""):
        if cwd.endswith("/dark-army/host"):
            return "dark-army"
        return fallback

    with patch("dark_army_daemon.daemon.workspace.project_label",
               side_effect=fake_label):
        facts = daemon._project_facts_snapshot({"dark-army"})
    assert [row["project"] for row in facts] == ["dark-army"]
    assert facts[0]["sessions"] == 1
    store.close()


def test_mixed_roots_sum_under_the_workspace_label(tmp_path):
    daemon, store = _daemon_with_history(
        tmp_path,
        ("root", "dark-army", 100.0),
        ("sub", "host", 250.0, "/Users/x/Code/dark-army/host"),
    )

    def fake_label(cwd, metrics=None, fallback=""):
        if cwd and cwd.endswith("/dark-army/host"):
            return "dark-army"
        return fallback

    with patch("dark_army_daemon.daemon.workspace.project_label",
               side_effect=fake_label):
        facts = daemon._project_facts_snapshot({"dark-army"})
    assert len(facts) == 1
    assert facts[0]["project"] == "dark-army"
    assert facts[0]["sessions"] == 2
    assert facts[0]["last_active"] == 240
    store.close()


def test_empty_cwd_keeps_the_stored_project_string(tmp_path):
    daemon, store = _daemon_with_history(
        tmp_path, ("s1", "host", 1_700_000_000.0))

    def boom(*_a, **_k):
        raise AssertionError("project_label must not run on an empty cwd")

    with patch("dark_army_daemon.daemon.workspace.project_label",
               side_effect=boom):
        facts = daemon._project_facts_snapshot({"host"})
    assert [row["project"] for row in facts] == ["host"]
    assert facts[0]["sessions"] == 1
    store.close()


def test_a_history_row_folds_under_its_enrolled_folders_name(tmp_path):
    """The all-time count under a tab follows the rename, because the facts are
    already re-grouped through the same labelling ladder. No patching here —
    this is the real `project_label` over a seeded ledger."""
    import json
    import os

    from dark_army_daemon import enrollment, paths, workspace

    root = os.path.realpath(str(tmp_path / "finance-demo"))
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    paths.ENROLLMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.ENROLLMENT_PATH.write_text(json.dumps({
        "version": 1,
        "projects": [{"root": root, "digest": "x", "label": "IGNORED"}],
    }))
    enrollment.invalidate()
    workspace.invalidate()

    daemon, store = _daemon_with_history(
        tmp_path,
        ("s1", "Ledgerly", 1_700_000_000.0, os.path.join(root, "sub")),
        ("s2", "Ledgerly", 1_700_000_100.0, root),
    )
    facts = daemon._project_facts_snapshot({"finance-demo"})
    assert [row["project"] for row in facts] == ["finance-demo"]
    assert facts[0]["sessions"] == 2
    store.close()
