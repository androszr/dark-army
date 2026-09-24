# host/tests/test_first_run_checklist.py
"""The first-run checklist's evidence, the launch line's honesty and the
denied-notification recovery, at their seams.

Three things are pinned here, none of which needs a screen:

- `daemon.checklist_facts` — which enrolled root an open window or a live
  session is evidence *for*, and which it is not (a parent, a sibling, a
  nested enrolment, a dead lock, a helper, an empty cwd).
- `launch_report` and the three installers' result plumbing — attempted is
  never reported as succeeded, and a background result never respawns the
  panel.
- `Notifier.refresh_status` — the explicit settings read that alone may
  say "denied", with one query in flight and every waiter answered.
"""
import json
import os
import types
from unittest.mock import MagicMock

import pytest

from dark_army_daemon import daemon as dmod
from dark_army_daemon import enrollment, workspace
from dark_army_daemon.daemon import BobDaemon


# ── helpers ──────────────────────────────────────────────────────────────────

def _enrol(tmp_path, name="proj"):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    ok, _ = enrollment.enroll(str(root))
    assert ok
    key = (root / enrollment.KEY_RELATIVE).read_text().strip()
    return os.path.realpath(str(root)), key


def _window(*folders, pid=0, name="w"):
    return workspace.Window(name, tuple(folders), pid)


def _row(cwd, state="idle", kind="interactive", sid="s1"):
    return {"session_id": sid, "cwd": cwd, "state": state, "kind": kind}


def _facts(roots, windows, rows):
    """`checklist_facts` with `root_enrolled` computed over the given roots,
    so no ledger is needed for the pure cases."""
    roots = [os.path.realpath(r) for r in roots]

    def resolve(cwd):
        here = os.path.realpath(cwd)
        best = ""
        for r in roots:
            if (here == r or here.startswith(r + os.sep)) and len(r) > len(best):
                best = r
        return best

    return {f["root"]: f for f in dmod.checklist_facts(roots, windows, rows, resolve)}


# ── checklist_facts: the pure table ──────────────────────────────────────────

def test_zero_roots_is_an_empty_tuple(tmp_path):
    assert dmod.checklist_facts([], [_window(str(tmp_path))], [_row(str(tmp_path))],
                                lambda cwd: "") == ()


def test_one_root_open_and_running_ticks_both(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    facts = _facts([str(root)], [_window(str(root), pid=os.getpid())],
                   [_row(str(root / "src"))])
    fact = facts[os.path.realpath(str(root))]
    assert fact["editor_observed"] is True
    assert fact["session_observed"] is True


def test_a_same_basename_sibling_is_no_editor_evidence(tmp_path):
    (tmp_path / "a" / "proj").mkdir(parents=True)
    (tmp_path / "b" / "proj").mkdir(parents=True)
    root = str(tmp_path / "a" / "proj")
    facts = _facts([root], [_window(str(tmp_path / "b" / "proj"))], [])
    assert facts[os.path.realpath(root)]["editor_observed"] is False


def test_a_containing_parent_window_is_no_editor_evidence(tmp_path):
    root = tmp_path / "parent" / "proj"
    root.mkdir(parents=True)
    facts = _facts([str(root)], [_window(str(tmp_path / "parent"))], [])
    assert facts[os.path.realpath(str(root))]["editor_observed"] is False


def test_nested_enrolled_roots_each_need_their_own_window_and_session(tmp_path):
    outer = tmp_path / "proj"
    inner = outer / "host"
    inner.mkdir(parents=True)
    facts = _facts([str(outer), str(inner)],
                   [_window(str(inner))],
                   [_row(str(inner / "tests"))])
    o = facts[os.path.realpath(str(outer))]
    i = facts[os.path.realpath(str(inner))]
    # The inner folder is open and has the session; the outer has neither —
    # a session inside the inner enrolment resolves to the inner root.
    assert (i["editor_observed"], i["session_observed"]) == (True, True)
    assert (o["editor_observed"], o["session_observed"]) == (False, False)


def test_a_multi_root_workspace_counts_each_exact_folder(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    for d in (a, b, c):
        d.mkdir()
    facts = _facts([str(a), str(b), str(c)], [_window(str(a), str(b))], [])
    assert facts[os.path.realpath(str(a))]["editor_observed"] is True
    assert facts[os.path.realpath(str(b))]["editor_observed"] is True
    assert facts[os.path.realpath(str(c))]["editor_observed"] is False


def test_a_session_in_the_wrong_root_ticks_only_its_own(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    facts = _facts([str(a), str(b)], [], [_row(str(b))])
    assert facts[os.path.realpath(str(a))]["session_observed"] is False
    assert facts[os.path.realpath(str(b))]["session_observed"] is True


def test_an_empty_or_unresolvable_cwd_counts_for_nobody(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    facts = _facts([str(a)], [], [_row(""), _row(str(tmp_path / "elsewhere"))])
    assert facts[os.path.realpath(str(a))]["session_observed"] is False


def test_a_dead_lock_is_no_editor_evidence(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    monkeypatch.setattr(workspace, "pid_alive", lambda pid: pid != 4242)
    facts = _facts([str(a)], [_window(str(a), pid=4242)], [])
    assert facts[os.path.realpath(str(a))]["editor_observed"] is False
    facts = _facts([str(a)], [_window(str(a), pid=4243)], [])
    assert facts[os.path.realpath(str(a))]["editor_observed"] is True


def test_a_lock_without_a_pid_is_still_accepted(tmp_path):
    """An extension older than the pid field wrote a lock with none; it is
    not refused for a field it never had."""
    a = tmp_path / "a"
    a.mkdir()
    facts = _facts([str(a)], [_window(str(a))], [])
    assert facts[os.path.realpath(str(a))]["editor_observed"] is True


@pytest.mark.parametrize("state", ["registered", "idle", "thinking", "working",
                                   "confused", "error", None])
def test_every_main_session_state_counts_as_started(tmp_path, state):
    a = tmp_path / "a"
    a.mkdir()
    facts = _facts([str(a)], [], [_row(str(a), state=state)])
    assert facts[os.path.realpath(str(a))]["session_observed"] is True


def test_facts_carry_no_key_digest_session_id_or_pid(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    facts = dmod.checklist_facts([str(a)], [_window(str(a), pid=os.getpid())],
                                 [{"session_id": "secret-sid", "cwd": str(a),
                                   "pid": 777777, "key": "k-secret"}],
                                 lambda cwd: os.path.realpath(str(a)))
    blob = json.dumps(facts)
    for forbidden in ("secret-sid", "777777", "k-secret", '"key"', '"pid"',
                      '"session_id"', '"digest"'):
        assert forbidden not in blob
    assert set(facts[0]) == {"root", "editor_observed", "session_observed",
                             "own_checkout"}


def test_pid_alive_reads_zero_as_alive_and_a_dead_pid_as_dead():
    assert workspace.pid_alive(0) is True
    assert workspace.pid_alive(os.getpid()) is True
    # A pid nobody has: the highest pid macOS hands out is well below this.
    assert workspace.pid_alive(2 ** 22 + 12345) is False


def test_window_carries_the_lock_pid_and_defaults_to_zero():
    assert workspace.Window("w", ("/a",)).pid == 0
    assert workspace._lock_pid({"pid": 12}) == 12
    assert workspace._lock_pid({"extHostPid": 34, "pid": 12}) == 34
    assert workspace._lock_pid({"pid": True}) == 0
    assert workspace._lock_pid({"pid": "12"}) == 0
    assert workspace._lock_pid({}) == 0


# ── the daemon: helpers out, main sessions in, no walk at publication ────────

def test_observe_checklist_takes_main_agents_from_the_live_buckets(
        enforce_enrolment, tmp_path, monkeypatch):
    root, _ = _enrol(tmp_path)
    d = BobDaemon()
    monkeypatch.setattr(dmod.workspace, "windows", lambda force=False: ())
    snapshot = {
        "running": [_row(root, kind="background", sid="bg")],
        "sleeping": [_row(root, state="idle", sid="main")],
        "waiting": [],
        "abandoned": [_row(root, sid="old")],
        "finished": [_row(root, sid="done")],
    }
    facts = d._observe_checklist(snapshot)
    assert facts == ({"root": root, "editor_observed": False,
                      "session_observed": True, "own_checkout": False},)


def test_a_background_or_abandoned_row_alone_is_no_session(
        enforce_enrolment, tmp_path, monkeypatch):
    root, _ = _enrol(tmp_path)
    d = BobDaemon()
    monkeypatch.setattr(dmod.workspace, "windows", lambda force=False: ())
    snapshot = {
        "running": [_row(root, kind="background", sid="bg")],
        "sleeping": [], "waiting": [],
        "abandoned": [_row(root, sid="old")],
        "finished": [_row(root, sid="done")],
    }
    assert d._observe_checklist(snapshot)[0]["session_observed"] is False


def test_a_stray_child_a_parent_claims_is_no_session(
        enforce_enrolment, tmp_path, monkeypatch):
    root, _ = _enrol(tmp_path)
    d = BobDaemon()
    monkeypatch.setattr(dmod.workspace, "windows", lambda force=False: ())
    monkeypatch.setattr(d, "_parent_of_child",
                        lambda sid: "parent" if sid == "child" else "")
    snapshot = {"running": [_row(root, sid="child")], "sleeping": [],
                "waiting": [], "abandoned": [], "finished": []}
    assert d._observe_checklist(snapshot)[0]["session_observed"] is False


def test_enrollment_snapshot_reads_the_stored_facts_and_walks_nothing(
        enforce_enrolment, tmp_path, monkeypatch):
    root, key = _enrol(tmp_path)
    d = BobDaemon()
    monkeypatch.setattr(dmod.workspace, "windows",
                        lambda force=False: (_window(root, pid=os.getpid()),))
    d._checklist_facts = d._observe_checklist(
        {"running": [], "sleeping": [], "waiting": []})
    assert d._checklist_facts[0]["editor_observed"] is True

    # Publication must not start a filesystem walk of its own.
    def boom(force=False):
        raise AssertionError("enrollment_snapshot walked the editor locks")
    monkeypatch.setattr(dmod.workspace, "windows", boom)
    snap = d.enrollment_snapshot()
    assert snap["checklist"] == {
        "available": True,
        "roots": [{"root": root, "editor_observed": True,
                   "session_observed": False, "own_checkout": False}],
    }
    blob = json.dumps(snap)
    assert key not in blob and enrollment.digest(key) not in blob
    assert "digest" not in blob


def test_an_editor_only_change_reaches_the_next_publication(
        enforce_enrolment, tmp_path, monkeypatch):
    """Closing VS Code returns step 2 to waiting on the next cycle: the
    facts are rebuilt from the cached window read, no second poller."""
    root, _ = _enrol(tmp_path)
    d = BobDaemon()
    empty = {"running": [], "sleeping": [], "waiting": []}
    monkeypatch.setattr(dmod.workspace, "windows",
                        lambda force=False: (_window(root, pid=os.getpid()),))
    d._checklist_facts = d._observe_checklist(empty)
    assert d.enrollment_snapshot()["checklist"]["roots"][0]["editor_observed"]
    monkeypatch.setattr(dmod.workspace, "windows", lambda force=False: ())
    d._checklist_facts = d._observe_checklist(empty)
    assert not d.enrollment_snapshot()["checklist"]["roots"][0]["editor_observed"]


def test_a_root_unenrolled_since_the_last_cycle_is_not_published(
        enforce_enrolment, tmp_path, monkeypatch):
    root, _ = _enrol(tmp_path)
    d = BobDaemon()
    monkeypatch.setattr(dmod.workspace, "windows", lambda force=False: ())
    d._checklist_facts = d._observe_checklist(
        {"running": [], "sleeping": [], "waiting": []})
    assert [f["root"] for f in d.enrollment_snapshot()["checklist"]["roots"]] == [root]
    ok, _ = enrollment.unenroll(root)
    assert ok
    enrollment.invalidate()
    assert d.enrollment_snapshot()["checklist"]["roots"] == []


def test_a_fresh_daemon_publishes_an_available_empty_checklist(enforce_enrolment):
    d = BobDaemon()
    assert d.enrollment_snapshot()["checklist"] == {"available": True, "roots": []}


@pytest.mark.asyncio
async def test_the_admission_gate_is_untouched(enforce_enrolment, tmp_path):
    """An unkeyed hook event still creates no session — and therefore never
    ticks step 3."""
    root, _ = _enrol(tmp_path)
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": "ghost",
                             "cwd": root})
    assert "ghost" not in d._session_states


@pytest.mark.asyncio
async def test_the_push_path_stores_the_facts(enforce_enrolment, tmp_path,
                                              monkeypatch):
    """`_push_agents_snapshot` rebuilds the tuple on its executor hop and the
    API reads the stored value. Everything heavier on that path is stubbed."""
    root, key = _enrol(tmp_path)
    d = BobDaemon()
    monkeypatch.setattr(dmod.workspace, "windows",
                        lambda force=False: (_window(root, pid=os.getpid()),))

    class Obs:
        def on_agents_change(self, snapshot):
            pass
    d._observers = [Obs()]
    monkeypatch.setattr(d, "_collect_agent_stubs", lambda: [])
    monkeypatch.setattr(d, "_enrich_agent_stubs", lambda stubs: {
        "running": [], "sleeping": [_row(root, sid="main")], "waiting": [],
        "abandoned": [], "finished": []})
    for name in ("_reconcile_board", "_apply_terminal_titles"):
        monkeypatch.setattr(d, name, lambda *a, **k: None)
    for name in ("_flush_queue_dispatches", "_flush_auto_compacts",
                 "_flush_session_titles", "_flush_card_priorities",
                 "_flush_work_records"):
        if hasattr(d, name):
            async def _noop(*a, **k):
                return None
            monkeypatch.setattr(d, name, _noop)
    await d._push_agents_snapshot()
    facts = d.enrollment_snapshot()["checklist"]["roots"]
    assert facts == [{"root": root, "editor_observed": True,
                      "session_observed": True, "own_checkout": False}]


# ── own_checkout: Dark Army's own checkout is marked, never followed ─────────

@pytest.fixture
def self_memo(monkeypatch):
    """Reset `enrollment.self_root()`'s per-process memo for one case, and put
    the original back afterwards (monkeypatch restores the attribute)."""
    monkeypatch.setattr(enrollment, "_SELF_ROOT", None)
    return monkeypatch


def test_own_checkout_is_true_for_the_exact_self_root_only(tmp_path):
    parent = tmp_path / "code"
    own = parent / "dark-army"
    sibling = parent / "dark-army2"
    for d in (own, sibling):
        d.mkdir(parents=True)
    roots = [os.path.realpath(str(p)) for p in (parent, own, sibling)]
    own_root = os.path.realpath(str(own))
    facts = {f["root"]: f for f in dmod.checklist_facts(
        roots, [], [], lambda cwd: "", own_root=own_root)}
    assert facts[own_root]["own_checkout"] is True
    assert facts[os.path.realpath(str(parent))]["own_checkout"] is False
    assert facts[os.path.realpath(str(sibling))]["own_checkout"] is False


def test_no_root_is_own_checkout_when_there_is_no_self_root(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    for kwargs in ({}, {"own_root": ""}):
        facts = dmod.checklist_facts([os.path.realpath(str(a))], [], [],
                                     lambda cwd: "", **kwargs)
        assert [f["own_checkout"] for f in facts] == [False]


def test_self_root_is_empty_with_no_source_tree(self_memo):
    from dark_army_menubar import dev_build
    self_memo.setattr(dev_build, "find_repo_root", lambda: None)
    assert enrollment.self_root() == ""


def test_self_root_is_empty_when_the_lookup_raises(self_memo):
    from dark_army_menubar import dev_build

    def boom():
        raise OSError("stamp unreadable")
    self_memo.setattr(dev_build, "find_repo_root", boom)
    assert enrollment.self_root() == ""


def test_self_root_is_the_normalised_checkout_and_is_memoised(self_memo, tmp_path):
    from pathlib import Path

    from dark_army_menubar import dev_build
    real = tmp_path / "checkout"
    real.mkdir()
    link = tmp_path / "via-link"
    link.symlink_to(real)
    calls = []

    def find():
        calls.append(1)
        return Path(str(link) + os.sep)
    self_memo.setattr(dev_build, "find_repo_root", find)
    assert enrollment.self_root() == os.path.realpath(str(real))
    assert enrollment.self_root() == os.path.realpath(str(real))
    assert calls == [1]


def test_observe_checklist_passes_the_memoised_self_root(
        enforce_enrolment, tmp_path, monkeypatch):
    own, _ = _enrol(tmp_path, "dark-army")
    mine, _ = _enrol(tmp_path, "mine")
    d = BobDaemon()
    monkeypatch.setattr(dmod.workspace, "windows", lambda force=False: ())
    calls = []

    def fake_self_root():
        calls.append(1)
        return own
    monkeypatch.setattr(dmod.enrollment, "self_root", fake_self_root)
    empty = {"running": [], "sleeping": [], "waiting": []}
    facts = {f["root"]: f for f in d._observe_checklist(empty)}
    assert calls == [1]
    assert facts[own]["own_checkout"] is True
    assert facts[mine]["own_checkout"] is False

    # Published under the enrolment section, still with no key or digest.
    d._checklist_facts = d._observe_checklist(empty)
    assert calls == [1, 1]
    published = d.enrollment_snapshot()["checklist"]["roots"]
    assert {f["root"]: f["own_checkout"] for f in published} == {
        own: True, mine: False}
    blob = json.dumps(published)
    for forbidden in ('"key"', '"digest"', '"pid"', '"session_id"'):
        assert forbidden not in blob


# ── launch_report ────────────────────────────────────────────────────────────

def test_launch_report_starts_pending_and_records_each_field():
    from dark_army_menubar import launch_report as lr
    report = lr.LaunchReport()
    assert report.to_dict() == {
        "hooks": {"status": "pending", "detail": ""},
        "extension": {"status": "pending", "detail": ""},
        "login_item": {"status": "pending", "detail": ""},
    }
    assert report.settled is False
    report.set(lr.HOOKS, lr.UNCHANGED, "hooks current")
    report.set(lr.EXTENSION, lr.CHANGED, "editor extension 0.2.0 installed")
    report.set(lr.LOGIN_ITEM, lr.FAILED, "not registered")
    assert report.settled is True
    assert report.status(lr.LOGIN_ITEM) == "failed"


def test_launch_report_refuses_an_unknown_status_and_field():
    from dark_army_menubar import launch_report as lr
    report = lr.LaunchReport()
    report.set(lr.HOOKS, "succeeded", "x")      # not in the vocabulary
    assert report.status(lr.HOOKS) == "unknown"
    report.set("statusline", lr.CHANGED)         # not a field
    assert set(report.to_dict()) == set(lr.FIELDS)


def test_launch_report_bounds_and_flattens_the_detail():
    from dark_army_menubar import launch_report as lr
    report = lr.LaunchReport()
    report.set(lr.HOOKS, lr.FAILED, "a\nb\t" + "x" * 500)
    detail = report.to_dict()["hooks"]["detail"]
    assert detail.startswith("a b x")
    assert "\n" not in detail
    assert len(detail) == lr.MAX_DETAIL_CHARS


def test_login_outcome_table():
    from dark_army_menubar import launch_report as lr
    from dark_army_menubar.launchd import EnableResult
    assert lr.login_outcome(EnableResult(True, True, "login item enabled")) == \
        ("changed", "login item enabled")
    # The case this whole module exists for: a plist on disk after a
    # refused bootstrap is a failure, never "enabled".
    status, detail = lr.login_outcome(EnableResult(True, False, "bootstrap exited 5"))
    assert status == "failed" and "bootstrap" in detail
    assert lr.login_outcome(EnableResult(False, False, ""))[0] == "failed"
    assert lr.login_outcome(None, already=True) == ("unchanged", "login item already set up")
    assert lr.login_outcome(None) == ("unknown", "login item result not reported")
    assert lr.login_outcome(None, error=OSError("x"))[0] == "failed"


# ── launchd.enable() says what it did ────────────────────────────────────────

@pytest.fixture
def agent(tmp_path, monkeypatch):
    from dark_army_menubar import launchd
    plist = tmp_path / "com.dark-army.menubar.plist"
    monkeypatch.setattr(launchd, "PLIST_PATH", plist)
    calls = []
    returncodes = {}

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        rc = returncodes.get(argv[1], 0)
        return types.SimpleNamespace(returncode=rc, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    return launchd, plist, calls, returncodes


def test_enable_reports_a_registered_agent(agent):
    launchd, plist, _, _ = agent
    result = launchd.enable()
    assert result.written and result.bootstrapped
    assert plist.exists()


def test_enable_reports_a_written_plist_launchd_refused(agent):
    launchd, plist, _, returncodes = agent
    returncodes["bootstrap"] = 5
    result = launchd.enable()
    assert plist.exists()
    assert result.written is True and result.bootstrapped is False
    assert "boom" not in result.detail       # stderr stays in the log
    assert "5" in result.detail


# ── first_run.apply_first_run ────────────────────────────────────────────────

@pytest.fixture
def prefs_file(tmp_path):
    return tmp_path / "preferences.json"


def test_first_run_reports_a_bootstrap_failure_as_failed(prefs_file, monkeypatch):
    from dark_army_menubar import first_run
    from dark_army_menubar.launchd import EnableResult
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: False)
    monkeypatch.setattr(first_run.launchd, "enable",
                        lambda: EnableResult(True, False, "bootstrap exited 5"))
    report = first_run.apply_first_run(prefs_file)
    assert report.applied is True
    assert report.login_status == "failed"
    # The marker is still down: the next launch is an ordinary one.
    assert json.loads(prefs_file.read_text()) == {first_run.FIRST_RUN_KEY: True}


def test_first_run_reports_a_registered_agent_as_changed(prefs_file, monkeypatch):
    from dark_army_menubar import first_run
    from dark_army_menubar.launchd import EnableResult
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: False)
    monkeypatch.setattr(first_run.launchd, "enable",
                        lambda: EnableResult(True, True, "login item enabled"))
    assert first_run.apply_first_run(prefs_file).login_status == "changed"


def test_first_run_with_the_plist_already_there_is_unchanged(prefs_file, monkeypatch):
    from dark_army_menubar import first_run
    calls = []
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: True)
    monkeypatch.setattr(first_run.launchd, "enable", lambda: calls.append(1))
    report = first_run.apply_first_run(prefs_file)
    assert report.applied is True and report.login_status == "unchanged"
    assert calls == []


def test_first_run_with_an_older_stub_returning_none_is_unknown(prefs_file, monkeypatch):
    from dark_army_menubar import first_run
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: False)
    monkeypatch.setattr(first_run.launchd, "enable", lambda: None)
    assert first_run.apply_first_run(prefs_file).login_status == "unknown"


def test_first_run_with_a_raising_enable_is_failed_and_marks(prefs_file, monkeypatch):
    from dark_army_menubar import first_run

    def boom():
        raise OSError("no write access")
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: False)
    monkeypatch.setattr(first_run.launchd, "enable", boom)
    report = first_run.apply_first_run(prefs_file)
    assert report.applied is True and report.login_status == "failed"
    assert first_run.is_first_run(prefs_file) is False


def test_a_prior_run_reports_the_login_item_as_it_stands(prefs_file, monkeypatch):
    from dark_army_menubar import first_run
    prefs_file.write_text(json.dumps({"notification_sound": False}))
    calls = []
    monkeypatch.setattr(first_run.launchd, "enable", lambda: calls.append(1))
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: True)
    report = first_run.apply_first_run(prefs_file)
    assert (report.applied, report.login_status) == (False, "unchanged")
    assert "already" in report.login_detail
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: False)
    report = first_run.apply_first_run(prefs_file)
    assert (report.applied, report.login_status) == (False, "unchanged")
    assert "off" in report.login_detail
    assert calls == []
    # The retained preference is untouched.
    assert json.loads(prefs_file.read_text()) == {"notification_sound": False}


def test_the_boolean_face_is_unchanged(prefs_file, monkeypatch):
    from dark_army_menubar import first_run
    monkeypatch.setattr(first_run.launchd, "is_enabled", lambda: True)
    assert first_run.apply_first_run_defaults(prefs_file) is True
    assert first_run.apply_first_run_defaults(prefs_file) is False
    assert first_run.FIRST_RUN_PREFERENCES == {}


# ── vscode_extension.ensure_installed says what it did ───────────────────────

@pytest.fixture
def ext(monkeypatch):
    from dark_army_menubar import vscode_extension as vx
    state = {"bundled": "0.2.0", "installed": "0.2.0",
             "install": (True, "installed (reload a VS Code window to activate)"),
             "installs": 0}
    monkeypatch.setattr(vx, "reap_stale_code_cli", lambda: 0)
    monkeypatch.setattr(vx, "bundled_version", lambda: state["bundled"])
    monkeypatch.setattr(vx, "installed_version", lambda: state["installed"])

    def install():
        state["installs"] += 1
        return state["install"]
    monkeypatch.setattr(vx, "install_extension", install)
    return vx, state


def test_extension_current_is_unchanged_and_installs_nothing(ext):
    vx, state = ext
    result = vx.ensure_installed()
    assert result.status == "unchanged" and state["installs"] == 0


def test_extension_newer_than_bundled_is_never_downgraded(ext):
    vx, state = ext
    state["installed"] = "0.3.0"
    result = vx.ensure_installed()
    assert result.status == "unchanged" and state["installs"] == 0


def test_extension_missing_package_is_skipped(ext):
    vx, state = ext
    state["bundled"] = None
    result = vx.ensure_installed()
    assert result.status == "skipped" and state["installs"] == 0


def test_extension_missing_or_older_installs_and_says_reload(ext):
    vx, state = ext
    state["installed"] = None
    result = vx.ensure_installed()
    assert result.status == "changed" and state["installs"] == 1
    assert "reload" in result.detail
    state["installed"] = "0.1.0"
    assert vx.ensure_installed().status == "changed"


@pytest.mark.parametrize("detail", ["VS Code CLI not found",
                                    "install failed: timeout"])
def test_extension_cli_and_timeout_failures_are_failed_verbatim(ext, detail):
    vx, state = ext
    state["installed"] = None
    state["install"] = (False, detail)
    result = vx.ensure_installed()
    assert result.status == "failed" and detail in result.detail


def test_extension_raw_cli_output_is_summarised(ext):
    vx, state = ext
    state["installed"] = None
    state["install"] = (False, "Error: EACCES /Users/somebody/.vscode/extensions")
    result = vx.ensure_installed()
    assert result.status == "failed"
    assert "/Users/somebody" not in result.detail
    assert "install command failed" in result.detail


def test_extension_check_raising_is_unknown_not_a_crash(ext, monkeypatch):
    vx, _ = ext

    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(vx, "installed_version", boom)
    assert vx.ensure_installed().status == "unknown"


def test_extension_result_reaches_the_callback_once(ext):
    vx, state = ext
    seen = []
    vx.ensure_installed(report=lambda s, d: seen.append((s, d)))
    assert seen == [("unchanged", "editor extension 0.2.0 current")]


def test_extension_result_callback_updates_the_report_without_respawning(monkeypatch):
    from dark_army_menubar import app as A
    from dark_army_menubar import launch_report as lr
    hops = []
    monkeypatch.setattr(A, "callAfter", lambda fn, *a: hops.append((fn, a)))
    fake = MagicMock()
    monkeypatch.setattr(A, "_RUNNING_APP", fake)
    monkeypatch.setattr(A, "LAUNCH_REPORT", lr.LaunchReport())
    A._report_extension_result("changed", "editor extension 0.2.0 installed")
    assert A.LAUNCH_REPORT.status(lr.EXTENSION) == "changed"
    # One hop, to the context push, with respawn False.
    assert hops == [(fake._push_panel_context, (False,))]


def test_extension_result_before_the_app_exists_is_held(monkeypatch):
    from dark_army_menubar import app as A
    from dark_army_menubar import launch_report as lr
    monkeypatch.setattr(A, "callAfter", lambda *a: pytest.fail("hopped with no app"))
    monkeypatch.setattr(A, "_RUNNING_APP", None)
    monkeypatch.setattr(A, "LAUNCH_REPORT", lr.LaunchReport())
    A._report_extension_result("skipped", "no bundled editor extension")
    assert A.LAUNCH_REPORT.status(lr.EXTENSION) == "skipped"


# ── the stale login item: repaired at launch, and never a reason not to start ─

def _stale_login_item(monkeypatch, repair):
    from dark_army_menubar import app as A
    from dark_army_menubar import launch_report as lr
    monkeypatch.setattr(A, "LAUNCH_REPORT", lr.LaunchReport())
    monkeypatch.setattr(A.launchd, "is_enabled", lambda: True)
    monkeypatch.setattr(A.launchd, "is_stale", lambda: True)
    monkeypatch.setattr(A.launchd, "repair_stale_login_item", repair)
    return A, lr


def test_a_failed_login_item_repair_keeps_the_app_starting(monkeypatch, caplog):
    """Every upgraded Mac takes the repair once on its first launch; a
    refused `launchctl` or a full disk must be a `failed` login item and a
    log line, never an exception out of the app's construction."""
    def boom():
        raise PermissionError("LaunchAgents is not writable")
    A, lr = _stale_login_item(monkeypatch, boom)
    with caplog.at_level("WARNING", logger="dark-army.menubar"):
        A._repair_login_item_if_stale()          # returns; does not raise
    assert A.LAUNCH_REPORT.status(lr.LOGIN_ITEM) == "failed"
    assert "could not repair the login item" in caplog.text


def test_a_login_item_repair_is_a_change_for_the_next_login(monkeypatch):
    from dark_army_menubar.launchd import EnableResult
    A, lr = _stale_login_item(
        monkeypatch,
        lambda: EnableResult(True, False, "login item updated, effective at the next login"))
    A._repair_login_item_if_stale()
    assert A.LAUNCH_REPORT.status(lr.LOGIN_ITEM) == "changed"
    assert A.LAUNCH_REPORT.to_dict()[lr.LOGIN_ITEM]["detail"] == \
        "login item updated, effective at the next login"


def test_a_current_login_item_is_left_alone(monkeypatch):
    A, lr = _stale_login_item(monkeypatch, lambda: pytest.fail("repaired a current item"))
    monkeypatch.setattr(A.launchd, "is_stale", lambda: False)
    before = A.LAUNCH_REPORT.status(lr.LOGIN_ITEM)
    A._repair_login_item_if_stale()
    assert A.LAUNCH_REPORT.status(lr.LOGIN_ITEM) == before


# ── hooks: judged by before/after, never by re-running ───────────────────────

def test_hook_outcome_table(monkeypatch):
    from dark_army_menubar import app as A
    from dark_army_menubar import launch_report as lr
    monkeypatch.setattr(A, "LAUNCH_REPORT", lr.LaunchReport())
    A._record_hook_outcome(True, True)
    assert A.LAUNCH_REPORT.status(lr.HOOKS) == "unchanged"
    A._record_hook_outcome(False, True)
    assert A.LAUNCH_REPORT.status(lr.HOOKS) == "changed"
    # Attempted and still not current is a failure, never "installed".
    A._record_hook_outcome(False, False)
    assert A.LAUNCH_REPORT.status(lr.HOOKS) == "failed"
    A._record_hook_outcome(True, False, error=True)
    assert A.LAUNCH_REPORT.status(lr.HOOKS) == "failed"


def test_hooks_current_needs_both_the_script_and_the_config(monkeypatch):
    from dark_army_menubar import app as A
    monkeypatch.setattr(A.hooks, "script_current", lambda p, t: True)
    monkeypatch.setattr(A.hooks, "are_hooks_installed", lambda: False)
    assert A._hooks_current() is False
    monkeypatch.setattr(A.hooks, "are_hooks_installed", lambda: True)
    assert A._hooks_current() is True
    monkeypatch.setattr(A.hooks, "script_current", lambda p, t: False)
    assert A._hooks_current() is False


# ── the context push carries both facts ──────────────────────────────────────

def test_context_push_ships_the_launch_report_and_notification_status(monkeypatch):
    from dark_army_menubar import app as A
    from dark_army_menubar import launch_report as lr
    report = lr.LaunchReport()
    report.set(lr.HOOKS, lr.UNCHANGED, "hooks current")
    monkeypatch.setattr(A, "LAUNCH_REPORT", report)
    app = object.__new__(A.BobCompanionApp)
    app._panel = MagicMock()
    app._panel.available = True
    app._panel.alive.return_value = True
    app._panel_probes = {"build_info": None}
    app._repo_root = None
    app._settings = {"version": "1"}
    app._vscode_installing = False
    app._rebuilding = False
    app._notification_status = "denied"
    monkeypatch.setattr(A.pack_ledger, "published", lambda: [])
    A.BobCompanionApp._push_panel_context(app, respawn=False)
    settings = app._panel.set_context.call_args.kwargs["settings"]
    assert settings["launch"]["hooks"] == {"status": "unchanged",
                                           "detail": "hooks current"}
    assert settings["launch"]["extension"]["status"] == "pending"
    assert settings["notification_status"] == "denied"


def test_notification_status_hops_and_pushes_without_respawn(monkeypatch):
    from dark_army_menubar import app as A
    hops = []
    monkeypatch.setattr(A, "callAfter", lambda fn, *a: hops.append((fn, a)))
    app = object.__new__(A.BobCompanionApp)
    pushes = []
    app._push_panel_context = lambda respawn=True: pushes.append(respawn)
    A.BobCompanionApp._on_notification_status(app, "denied")
    assert hops == [(app._apply_notification_status, ("denied",))]
    A.BobCompanionApp._apply_notification_status(app, "denied")
    assert app._notification_status == "denied"
    assert pushes == [False]
    # An answer outside the vocabulary is `unknown`, never trusted.
    A.BobCompanionApp._apply_notification_status(app, "yes")
    assert app._notification_status == "unknown"


def test_the_panel_can_ask_for_a_status_read_and_never_a_request(monkeypatch):
    from dark_army_menubar import app as A
    app = object.__new__(A.BobCompanionApp)
    app._notifier = MagicMock()
    A.BobCompanionApp.PANEL_ACTIONS["refresh_notification_status"](app, None)
    app._notifier.refresh_status.assert_called_once_with()
    app._notifier.start.assert_not_called()
    app._notifier.requestAuthorizationWithOptions_completionHandler_.assert_not_called()


def test_a_status_read_request_pushes_no_context_of_its_own(monkeypatch):
    """The answer arrives through `_apply_notification_status`, which pushes
    with `respawn=False`; a second push from the action's own tail would
    carry the default `respawn=True` and could spawn a hidden panel when
    the line was the last thing a quitting panel sent."""
    from dark_army_menubar import app as A
    events = []
    app = object.__new__(A.BobCompanionApp)
    app._notifier = MagicMock()
    app._push_panel_context = lambda *a, **k: events.append("push")
    monkeypatch.setattr(A, "callAfter", lambda fn: fn())
    A.BobCompanionApp._on_panel_action(app, "refresh_notification_status", None)
    app._notifier.refresh_status.assert_called_once_with()
    assert events == []


def test_a_deliberate_panel_open_refreshes_the_status(monkeypatch):
    from dark_army_menubar import app as A
    app = object.__new__(A.BobCompanionApp)
    app._notifier = MagicMock()
    app._panel = MagicMock()
    app._panel.available = True
    app._panel.toggle.return_value = True
    app._push_panel_context = lambda *a, **k: None
    app._status_anchor = lambda: None
    A.BobCompanionApp._on_open_panel(app, None)
    app._notifier.refresh_status.assert_called_once_with()


# ── the notifier's explicit read ─────────────────────────────────────────────

def test_authorization_status_name_table():
    from dark_army_menubar import notifier as N
    assert N.authorization_status_name(0) == "not_determined"
    assert N.authorization_status_name(1) == "denied"
    assert N.authorization_status_name(2) == "authorized"
    assert N.authorization_status_name(3) == "authorized"   # provisional
    assert N.authorization_status_name(4) == "authorized"   # ephemeral
    for odd in (None, True, "1", 9, -1):
        assert N.authorization_status_name(odd) == "error"


class _FakeCenter:
    """Records the settings query and answers it on demand, like the
    framework's callback thread would."""

    def __init__(self):
        self.handlers = []
        self.raise_on_query = False

    def getNotificationSettingsWithCompletionHandler_(self, handler):
        if self.raise_on_query:
            raise RuntimeError("no centre")
        self.handlers.append(handler)

    def answer(self, raw):
        handler = self.handlers.pop(0)
        settings = None if raw is None else types.SimpleNamespace(
            authorizationStatus=lambda: raw)
        handler(settings)


def _notifier(center=None, on_status=None):
    from dark_army_menubar import notifier as N
    n = N.Notifier.__new__(N.Notifier)
    n._on_action = None
    n._on_status = on_status
    n._ns = {} if center is not None else None
    n._center = center
    n._delegate = None
    n._authorized = None
    n._status = N.STATUS_UNKNOWN
    n._status_inflight = False
    n._status_inflight_at = 0.0
    n._status_waiters = []
    return n


@pytest.mark.parametrize("raw,expected", [(0, "not_determined"), (1, "denied"),
                                          (2, "authorized"), (None, "error"),
                                          ("odd", "error")])
def test_refresh_status_maps_the_answer(raw, expected):
    center = _FakeCenter()
    seen = []
    n = _notifier(center)
    assert n.refresh_status(seen.append) is True
    center.answer(raw)
    assert seen == [expected]
    assert n.status == expected


def test_an_unavailable_notifier_answers_unavailable_without_a_query():
    seen = []
    n = _notifier(None)
    assert n.refresh_status(seen.append) is False
    assert seen == ["unavailable"] and n.status == "unavailable"


def test_a_raising_query_is_an_error_not_a_denial():
    center = _FakeCenter()
    center.raise_on_query = True
    seen = []
    n = _notifier(center)
    assert n.refresh_status(seen.append) is False
    assert seen == ["error"]
    assert n._authorized is None      # the post gate is untouched
    assert n._status_inflight is False


def test_duplicate_returns_coalesce_onto_one_query():
    center = _FakeCenter()
    seen = []
    n = _notifier(center)
    n.refresh_status(lambda s: seen.append(("a", s)))
    n.refresh_status(lambda s: seen.append(("b", s)))
    n.refresh_status()
    assert len(center.handlers) == 1
    center.answer(1)
    assert seen == [("a", "denied"), ("b", "denied")]
    # The in-flight flag is released, so a later return asks again.
    n.refresh_status(lambda s: seen.append(("c", s)))
    assert len(center.handlers) == 1
    center.answer(2)
    assert seen[-1] == ("c", "authorized")


def test_denied_then_authorized_on_return_flips_the_post_gate():
    center = _FakeCenter()
    standing = []
    n = _notifier(center, on_status=standing.append)
    n.refresh_status()
    center.answer(1)
    assert n._authorized is False and standing == ["denied"]
    n.refresh_status()
    center.answer(2)
    assert n._authorized is True and standing == ["denied", "authorized"]


def test_the_request_callback_is_followed_by_a_read_not_a_second_request():
    center = _FakeCenter()
    n = _notifier(center)
    n._authorized_cb(False, "some error")
    assert len(center.handlers) == 1        # one settings read
    assert not hasattr(center, "requested")
    center.answer(0)
    # A request error is not a denial: not-determined leaves the gate as
    # the request callback set it and draws no warning.
    assert n.status == "not_determined"


def test_a_failing_status_callback_does_not_stop_the_others():
    center = _FakeCenter()
    seen = []

    def boom(status):
        raise RuntimeError("x")
    n = _notifier(center)
    n.refresh_status(boom)
    n.refresh_status(seen.append)
    center.answer(2)
    assert seen == ["authorized"]


def test_a_completion_that_never_comes_does_not_coalesce_for_ever(monkeypatch):
    """A hung settings read is asked again once it is older than the timeout,
    so a denied warning can still clear on a later return."""
    from dark_army_menubar import notifier as N
    center = _FakeCenter()
    seen = []
    clock = [1000.0]
    monkeypatch.setattr(N.time, "monotonic", lambda: clock[0])
    n = _notifier(center)
    n.refresh_status(lambda s: seen.append(("a", s)))
    # Inside the window: coalesced, one query outstanding.
    clock[0] += N.STATUS_INFLIGHT_TIMEOUT_SECONDS - 1
    n.refresh_status(lambda s: seen.append(("b", s)))
    assert len(center.handlers) == 1
    # Past it: a fresh query is issued; the hung one is left unanswered.
    clock[0] += 2
    n.refresh_status(lambda s: seen.append(("c", s)))
    assert len(center.handlers) == 2
    center.handlers.pop(0)                 # the hung one never calls back
    center.answer(2)
    assert seen == [("a", "authorized"), ("b", "authorized"), ("c", "authorized")]
    assert n._status_inflight is False
