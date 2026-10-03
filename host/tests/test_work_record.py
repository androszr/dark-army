"""What Dark Army observed a run do — the pure module and the store's four verbs.

The git half needs no stub and no manual step: `git init` in `tmp_path` is a
real repository, and the parsers are fed real `git diff --numstat` output.
"""

from __future__ import annotations

import subprocess

import pytest

from pathlib import Path

from dark_army_daemon import board, work_record


# --- the parsers -------------------------------------------------------------

def test_numstat_reads_counts_and_leaves_binary_uncounted():
    rows = work_record.parse_numstat(
        "12\t3\thost/a.py\n"
        "-\t-\tassets/cast/frog.png\n"
        "0\t0\tREADME.md\n")
    assert [r["path"] for r in rows] == [
        "host/a.py", "assets/cast/frog.png", "README.md"]
    assert rows[0]["added"] == 12 and rows[0]["removed"] == 3
    # A binary file's counts are **absent**, never zero: git said `-`, and a
    # zero there reads as a file that changed by nothing.
    assert rows[1]["binary"] is True
    assert "added" not in rows[1] and "removed" not in rows[1]
    # And a real zero-line change is still a counted row.
    assert rows[2]["binary"] is False and rows[2]["added"] == 0


def test_numstat_resolves_a_rename_to_the_new_path():
    rows = work_record.parse_numstat(
        "1\t1\thost/{old.py => new.py}\n"
        "2\t0\tdocs/a.md => docs/b.md\n")
    assert [r["path"] for r in rows] == ["host/new.py", "docs/b.md"]


def test_numstat_drops_a_path_git_still_quoted():
    rows = work_record.parse_numstat('1\t1\t"weird\\nname.py"\n1\t1\tok.py\n')
    assert [r["path"] for r in rows] == ["ok.py"]


def test_untracked_splits_on_nul_and_marks_new():
    rows = work_record.parse_untracked(b"a.py\0dir/b with space.md\0")
    assert [r["path"] for r in rows] == ["a.py", "dir/b with space.md"]
    assert all(r["new"] is True for r in rows)


def test_clamp_drops_an_over_long_path_but_still_counts_it():
    rows = [{"path": "a.py"}, {"path": "x" * (work_record.MAX_PATH_CHARS + 1)}]
    kept, truncated, total = work_record.clamp_files(rows)
    assert [r["path"] for r in kept] == ["a.py"]
    assert truncated is True
    assert total == 2


def test_clamp_bounds_the_list_and_states_the_real_total():
    rows = [{"path": f"f{i}.py"} for i in range(work_record.MAX_FILES + 10)]
    kept, truncated, total = work_record.clamp_files(rows)
    assert len(kept) == work_record.MAX_FILES
    assert truncated is True
    assert total == work_record.MAX_FILES + 10


def test_totals_ignore_a_binary_row():
    added, removed = work_record.totals([
        {"path": "a", "added": 3, "removed": 1},
        {"path": "b", "binary": True},
    ])
    assert (added, removed) == (3, 1)


# --- the verdict -------------------------------------------------------------

def test_verdict_over_all_four_card_shapes():
    closed = {"column_name": "done", "closed_by": "s1"}
    assert work_record.verdict_for(closed, "s1") == work_record.VERDICT_CLOSED
    flagged = {"column_name": "in_progress", "manual_steps": "1. look"}
    assert work_record.verdict_for(flagged, "s1") == work_record.VERDICT_MANUAL
    # A hand-dragged Done card names nobody, so it is quiet on purpose: the
    # column already says a person moved it.
    dragged = {"column_name": "done", "closed_by": ""}
    assert work_record.verdict_for(dragged, "s1") == work_record.VERDICT_QUIET
    quiet = {"column_name": "in_progress"}
    assert work_record.verdict_for(quiet, "s1") == work_record.VERDICT_QUIET


def test_every_verdict_has_words_and_an_unknown_one_has_none():
    for verdict in work_record.VERDICTS:
        assert work_record.verdict_words(verdict)
    assert work_record.verdict_words("something-newer") == ""


def test_the_caption_says_it_is_the_apps_own_observation():
    text = work_record.CAPTION.lower()
    assert "dark army" in text and "claim" in text


# --- the argv ----------------------------------------------------------------

def test_every_git_call_keeps_its_hands_off_the_index_lock():
    for argv in (work_record.argv_baseline("/r"),
                 work_record.argv_numstat("/r", "abc"),
                 work_record.argv_untracked("/r"),
                 work_record.argv_file_diff("/r", "abc", "a.py"),
                 work_record.argv_new_file_diff("/r", "a.py"),
                 work_record.argv_upstream("/r"),
                 work_record.argv_ahead("/r", "origin/main"),
                 work_record.argv_status("/r")):
        assert argv[0] == "git"
        assert "--no-optional-locks" in argv
        assert argv[argv.index("-C") + 1] == "/r"
    env = work_record.git_env({"PATH": "/bin", "PYTHONHOME": "/bundle"})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "PYTHONHOME" not in env


# --- against a real repository ----------------------------------------------

def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.py").write_text("one\ntwo\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    return root


def test_the_parsers_read_a_real_repository(repo):
    head = subprocess.run(work_record.argv_baseline(str(repo)),
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    (repo / "a.py").write_text("one\ntwo\nthree\n")
    (repo / "b.py").write_text("new\n")
    numstat = subprocess.run(work_record.argv_numstat(str(repo), head),
                             capture_output=True, text=True,
                             check=True).stdout
    rows = work_record.parse_numstat(numstat)
    assert [r["path"] for r in rows] == ["a.py"]
    assert rows[0]["added"] == 1 and rows[0]["removed"] == 0
    others = subprocess.run(work_record.argv_untracked(str(repo)),
                            capture_output=True, check=True).stdout
    new_rows = work_record.parse_untracked(others)
    assert [r["path"] for r in new_rows] == ["b.py"]


# --- the store ---------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    s = board.BoardStore(tmp_path / "board.db")
    s.connect()
    yield s
    s.close()


def _card(store, root="/r"):
    card, _ = store.create({"title": "t", "project": "p", "root": root,
                            "prompt": "do it", "tool": "claude"})
    return card["id"]


def _rows():
    return [{"path": "a.py", "added": 3, "removed": 1,
             "binary": False, "new": False}]


def test_open_then_close_round_trips(store):
    cid = _card(store)
    assert store.open_run(cid, 100.0, "/r", "abc")[0] is True
    # An open run publishes no headline: a record of work still in flight
    # would read as a finding.
    assert store.run_headlines() == {}
    record, detail = store.close_run(cid, 100.0, "s1", "closed",
                                     "## Work done\nall of it", "tldr",
                                     _rows(), True, "", 1)
    assert detail == "recorded"
    assert record["files"] == _rows()
    assert record["lines_added"] == 3 and record["lines_removed"] == 1
    assert record["verdict"] == "closed"
    head = store.run_headlines()[cid]
    assert head["files"] == 1 and head["report"] is True
    assert head["files_available"] is True


def test_a_second_start_replaces_the_record(store):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    store.close_run(cid, 100.0, "s1", "closed", "first", "", _rows(), True)
    store.open_run(cid, 200.0, "/r", "def")
    # The replacement is open, so nothing is published for it yet — and the
    # previous run's record is gone rather than stacked beside it.
    assert store.run_headlines() == {}
    assert store.run_for(cid)["report"] == ""
    assert store.run_for(cid)["baseline"] == "def"


def test_a_late_collector_cannot_stamp_the_newer_run(store):
    cid = _card(store)
    store.open_run(cid, 200.0, "/r", "def")
    record, detail = store.close_run(cid, 100.0, "s1", "closed", "stale", "",
                                     _rows(), True)
    assert record is None
    assert detail == "that card was started again"
    assert store.run_for(cid)["report"] == ""


def test_a_run_with_no_baseline_is_still_recorded(store):
    cid = _card(store)
    record, detail = store.close_run(
        cid, 100.0, "s1", "quiet", "went quiet", "", [], False,
        work_record.NO_BASELINE_REASON)
    assert detail == "recorded"
    assert record["baseline"] == ""
    assert record["files_available"] is False
    assert record["files_reason"] == work_record.NO_BASELINE_REASON


def test_a_malformed_file_list_is_refused(store):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    record, detail = store.close_run(cid, 100.0, "s1", "closed", "x", "",
                                     [{"nope": 1}], True)
    assert record is None and "shape" in detail
    assert store.run_for(cid)["recorded_at"] is None


def test_the_report_is_clamped_not_refused(store):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    record, detail = store.close_run(
        cid, 100.0, "s1", "closed", "x" * (work_record.MAX_REPORT_CHARS + 50),
        "y" * (work_record.MAX_SUMMARY_CHARS + 50), [], True)
    assert detail == "recorded"
    assert len(record["report"]) == work_record.MAX_REPORT_CHARS
    assert len(record["summary"]) == work_record.MAX_SUMMARY_CHARS


def test_the_list_is_bounded_and_the_count_stays_honest(store):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    record, _ = store.close_run(cid, 100.0, "s1", "closed", "x", "",
                                _rows(), True, "", 431)
    assert record["files_changed"] == 1
    assert record["files_total"] == 431


def test_deleting_the_card_drops_the_run(store):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    store.close_run(cid, 100.0, "s1", "closed", "x", "", _rows(), True)
    store.delete(cid)
    assert store.run_for(cid) is None


def test_clearing_done_drops_the_run(store):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    store.close_run(cid, 100.0, "s1", "closed", "x", "", _rows(), True)
    store.update(cid, {"column_name": "done"})
    count, token = store.done_scope()
    assert store.clear_done(count, token)[0] is True
    assert store.run_for(cid) is None


def test_pruning_done_drops_the_run(store):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    store.close_run(cid, 100.0, "s1", "closed", "x", "", _rows(), True)
    store.update(cid, {"column_name": "done", "done_at": 1.0})
    assert store.prune_done(older_than=2.0) == 1
    assert store.run_for(cid) is None


def test_the_orphan_sweep_removes_a_row_whose_card_is_gone(store, tmp_path):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    store.close_run(cid, 100.0, "s1", "closed", "x", "", _rows(), True)
    # A build that predates this table destroys the card and leaves the row.
    with store._lock:
        store._conn.execute("DELETE FROM cards WHERE id = ?", (cid,))
        store._conn.commit()
    store.close()
    again = board.BoardStore(tmp_path / "board.db")
    again.connect()
    assert again.run_for(cid) is None
    again.close()


def test_no_surface_can_write_the_run(store):
    # `card_runs` names no member of the writable field set, so no panel,
    # phone, channel tool or LAN door can write a word of it.
    for name in work_record.RECORD_KEYS:
        if name in ("root", "session_id", "summary"):
            continue          # real `cards` columns with their own rings
        assert name not in board.BoardStore._WRITABLE, name


def test_the_schema_carries_the_table_without_a_cards_column():
    # `card_runs` is its own table: no `cards` column names it. Its own
    # ADD COLUMN entries (v24) carry a DEFAULT each, the forward-compatible
    # rule every entry here lives under.
    assert "card_runs" not in str(board.BoardStore._ADDED_COLUMNS["cards"])
    for _name, decl in board.BoardStore._ADDED_COLUMNS["card_runs"]:
        assert "DEFAULT" in decl, decl
    assert board.SCHEMA_VERSION >= 24


# --- the shunt ledger --------------------------------------------------------

FIXTURE_LEDGER = (Path(__file__).resolve().parent / "fixtures" / "ship_efficiency"
                  / "shunt-ledger-sample.jsonl")


def test_read_shunt_ledger_skips_a_bad_line_counts_a_duplicate_once_and_keeps_cost_unknown(tmp_path):
    ledger = tmp_path / "s1.jsonl"
    ledger.write_text(
        '{"delegation_id": "a", "lines_kept_out": 100, "worker_cost_usd": 0.02}\n'
        'not json\n'
        '{"delegation_id": "a", "lines_kept_out": 100, "worker_cost_usd": 0.02}\n'
        '{"delegation_id": "b", "lines_kept_out": "40", "worker_cost_usd": null}\n'
        '{"lines_kept_out": 999}\n'
        '[1, 2]\n')
    assert work_record.read_shunt_ledger(ledger) == (2, 140, None)


def test_read_shunt_ledger_sums_the_cost_only_when_every_delegation_measured_it(tmp_path):
    ledger = tmp_path / "s1.jsonl"
    ledger.write_text(
        '{"delegation_id": "a", "lines_kept_out": 10, "worker_cost_usd": 0.02}\n'
        '{"delegation_id": "b", "lines_kept_out": 5, "worker_cost_usd": 0.015}\n')
    assert work_record.read_shunt_ledger(ledger) == (2, 15, 0.035)


def test_a_missing_ledger_is_zero_delegations_and_cost_unknown(tmp_path):
    assert work_record.read_shunt_ledger(tmp_path / "nope.jsonl") == (0, 0, None)
    assert work_record.read_shunt_ledger(None) == (0, 0, None)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert work_record.read_shunt_ledger(empty) == (0, 0, None)
    assert work_record.shunt_words({"shunt_delegations": 0}) == ""
    assert work_record.shunt_words({}) == ""


def test_shunt_words_name_both_numbers_and_say_unknown_in_words():
    assert work_record.shunt_words({"shunt_delegations": 3, "shunt_lines_kept_out": 2140,
                                    "shunt_worker_cost_usd": 0.04}) == (
        "3 delegations kept 2,140 lines out of the main model; helper cost $0.04")
    assert work_record.shunt_words({"shunt_delegations": 1, "shunt_lines_kept_out": 1,
                                    "shunt_worker_cost_usd": None}) == (
        "1 delegation kept 1 line out of the main model; helper cost unknown")
    assert "$0.00" not in work_record.shunt_words(
        {"shunt_delegations": 2, "shunt_lines_kept_out": 9, "shunt_worker_cost_usd": None})


def test_a_measured_sub_cent_cost_is_under_a_cent_never_free():
    words = work_record.shunt_words(
        {"shunt_delegations": 2, "shunt_lines_kept_out": 9, "shunt_worker_cost_usd": 0.0012})
    assert words.endswith("helper cost under $0.01")
    assert "$0.00" not in words
    # A measured zero is a zero, half a cent rounds up, a cent is a cent.
    assert work_record.shunt_words(
        {"shunt_delegations": 1, "shunt_lines_kept_out": 9, "shunt_worker_cost_usd": 0.0}
    ).endswith("helper cost $0.00")
    assert work_record.shunt_words(
        {"shunt_delegations": 1, "shunt_lines_kept_out": 9, "shunt_worker_cost_usd": 0.005}
    ).endswith("helper cost $0.01")
    assert work_record.shunt_words(
        {"shunt_delegations": 1, "shunt_lines_kept_out": 9, "shunt_worker_cost_usd": 0.01}
    ).endswith("helper cost $0.01")


def test_a_ledger_folds_into_the_record(store):
    """The six-line synthetic ledger (one duplicate id) through the store:
    the record carries 5 delegations, the ledger's line sum over the records
    that succeeded and both numbers in the sentence."""
    delegations, kept, cost = work_record.read_shunt_ledger(FIXTURE_LEDGER)
    # 1200 + 640 + 180 + 90; the failed d-0005 record's 30 lines never count.
    assert (delegations, kept, cost) == (5, 2110, None)
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    record, detail = store.close_run(
        cid, 100.0, "s1", "closed", "## Work done", "", _rows(), True,
        shunt_delegations=delegations, shunt_lines_kept_out=kept,
        shunt_worker_cost_usd=cost)
    assert detail == "recorded"
    assert record["shunt_delegations"] == 5
    assert record["shunt_lines_kept_out"] == 2110
    assert record["shunt_worker_cost_usd"] is None
    words = work_record.shunt_words(record)
    assert "5 delegations" in words and "2,110 lines" in words
    assert "helper cost unknown" in words
    head = store.run_headlines()[cid]
    assert head["shunt_delegations"] == 5 and head["shunt_lines_kept_out"] == 2110
    assert head["shunt_worker_cost_usd"] is None
    # And a measured cost is stored as a number, read back as one.
    store.open_run(cid, 200.0, "/r", "def")
    record, _ = store.close_run(cid, 200.0, "s2", "quiet", "", "", [], False,
                                shunt_delegations=1, shunt_lines_kept_out=9,
                                shunt_worker_cost_usd=0.04)
    assert record["shunt_worker_cost_usd"] == 0.04
    assert work_record.shunt_words(record).endswith("helper cost $0.04")


def test_close_run_without_the_keywords_stores_no_delegations(store):
    cid = _card(store)
    store.open_run(cid, 100.0, "/r", "abc")
    record, _ = store.close_run(cid, 100.0, "s1", "closed", "r", "", _rows(), True)
    assert record["shunt_delegations"] == 0
    assert record["shunt_lines_kept_out"] == 0
    assert record["shunt_worker_cost_usd"] is None


def test_a_v23_card_runs_table_gains_the_three_columns_with_defaults(tmp_path):
    import sqlite3
    path = tmp_path / "board.db"
    s = board.BoardStore(path)
    s.connect()
    cid = _card(s)
    s.open_run(cid, 100.0, "/r", "abc")
    s.close_run(cid, 100.0, "s1", "closed", "r", "", [], False)
    s.close()
    conn = sqlite3.connect(path)
    for column in ("shunt_delegations", "shunt_lines_kept_out", "shunt_worker_cost_usd"):
        conn.execute(f"ALTER TABLE card_runs DROP COLUMN {column}")
    conn.execute("UPDATE schema_meta SET value = '23' WHERE key = 'version'")
    conn.commit()
    conn.close()
    again = board.BoardStore(path)
    again.connect()
    record = again.run_for(cid)
    assert record["shunt_delegations"] == 0
    assert record["shunt_worker_cost_usd"] is None
    assert again.run_headlines()[cid]["shunt_delegations"] == 0
    again.close()


# --- the whole path, against a real repository -------------------------------

@pytest.mark.asyncio
async def test_a_dispatched_run_ends_with_a_record_of_what_it_changed(
        tmp_path, monkeypatch):
    """Start to record, with nothing stubbed but the launcher itself: a real
    `git init` repository, a real baseline reading, real edits, the reconcile's
    own `mark_ended` seam and the loop's collector."""
    from dark_army_daemon import dispatch, enrollment
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon

    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "a.py").write_text("one\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")

    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    monkeypatch.setattr(daemon, "_known_project_roots",
                        lambda: {str(root.resolve())})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/" + tool)

    async def accept(_root, _argv, _name, **_kw):
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    monkeypatch.setattr(enrollment, "root_enrolled", lambda cwd: str(root))
    # This is the main-checkout record, end to end: card isolation (on by
    # default for a git project, `docs/card-worktrees.md`) is switched off
    # for this root, so the run works — and is measured — in the root itself.
    # `test_dispatch_worktree.py` drives the isolated path.
    daemon.set_board_isolation_override(str(root.resolve()), False)
    try:
        card, _ = store.create({"title": "the work", "project": "proj",
                                "root": str(root), "tool": "claude",
                                "prompt": "go", "column_name": "backlog"})
        ok, detail = await daemon.dispatch_card(card["id"],
                                                allow_unplanned=True)
        assert ok, detail
        # The baseline is taken detached; nothing awaits it in production.
        for task in list(daemon._work_open_tasks):
            await task
        assert store.run_for(card["id"])["baseline"]

        store.bind_session(card["id"], "s1")
        (root / "a.py").write_text("one\ntwo\nthree\n")
        (root / "b.py").write_text("brand new\n")
        store.declare_done(card["id"], "s1", "did it")
        # The session's delegation ledger, as the shunt wrappers write it.
        from dark_army_daemon import paths
        paths.SHUNT_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        (paths.SHUNT_LEDGER_DIR / "s1.jsonl").write_text(
            FIXTURE_LEDGER.read_text(encoding="utf-8"), encoding="utf-8")

        daemon._board_missing_since[card["id"]] = 0.0
        daemon._reconcile_board({"running": [], "finished": [
            {"session_id": "s1", "last_text": "## Work done\nall of it",
             "last_summary": "done"}]})
        assert len(daemon._work_record_queue) == 1

        async def quiet():
            return None

        monkeypatch.setattr(daemon, "_publish_board", quiet)
        await daemon._flush_work_records()
        await daemon._work_record_task

        record = store.run_for(card["id"])
        assert record["verdict"] == work_record.VERDICT_CLOSED
        assert record["report"] == "## Work done\nall of it"
        assert record["files_available"] is True
        by_path = {f["path"]: f for f in record["files"]}
        assert by_path["a.py"]["added"] == 2
        assert by_path["b.py"]["new"] is True
        assert record["lines_added"] == 2

        assert record["shunt_delegations"] == 5
        assert record["shunt_lines_kept_out"] == 2110
        assert record["shunt_worker_cost_usd"] is None
        head = daemon._build_board_state()["cards"][0]["work_record"]
        assert head["verdict"] == "closed" and head["files"] == 2
        assert head["report"] is True
        assert head["shunt_delegations"] == 5
        # And it survives the daemon: a fresh store on the same file still
        # has it, which is the whole point of the table.
        store.close()
        again = BoardStore(tmp_path / "board.db")
        again.connect()
        assert again.run_for(card["id"])["report"] == "## Work done\nall of it"
        again.close()
    finally:
        try:
            store.close()
        except Exception:
            pass


def test_a_failed_delegation_counts_but_its_lines_never_reach_the_record(tmp_path):
    # A helper that failed kept nothing out of the main model: the caller
    # read the files another way. The attempt is still a delegation.
    ledger = tmp_path / "s.jsonl"
    ledger.write_text(
        '{"delegation_id": "a", "lines_kept_out": 100, "worker_cost_usd": 0.01, "ok": true}\n'
        '{"delegation_id": "b", "lines_kept_out": 900, "worker_cost_usd": 0.01, "ok": false}\n'
        '{"delegation_id": "c", "lines_kept_out": 5, "worker_cost_usd": 0.01}\n')
    assert work_record.read_shunt_ledger(ledger) == (3, 105, 0.03)


# --- the ledger's file name ---------------------------------------------------

SHUNT_SKILL = (Path(__file__).resolve().parents[2]
               / "host/dark_army_menubar/agent_pack/template/.claude/skills/shunt")


def _wrapper_module(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location("shunt_" + name, SHUNT_SKILL / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("wrapper", ["bulk_read", "code_write"])
@pytest.mark.parametrize("session_id", [
    "abc-123", "a/b c", "../../etc/passwd", "sess:1|x", "ünïcode-ok", "a.b",
])
def test_ledger_name_is_the_wrappers_own_map_byte_for_byte(tmp_path, wrapper, session_id):
    """The wrappers write under a sanitised name; the daemon used to read
    under the raw id, so an id with any other character read as 0
    delegations and one shaped like a path read an arbitrary file."""
    module = _wrapper_module(wrapper)
    home = tmp_path / "home"
    home.mkdir()
    module.append_ledger({"HOME": str(home)}, module.ledger_record(
        "claude", "haiku", "bulk-read", ["x.py"], 12, None, 1.0, True, session_id))
    (written,) = list((home / ".dark-army" / "shunt").iterdir())
    assert written.name == work_record.ledger_name(session_id)
    assert "/" not in written.name
    assert work_record.read_shunt_ledger(written) == (1, 12, None)


def test_ledger_name_refuses_an_empty_or_dot_id():
    assert work_record.ledger_name("") is None
    assert work_record.ledger_name(None) is None
    assert work_record.ledger_name(".") is None
    assert work_record.ledger_name("..") is None
    assert work_record.ledger_name("/") == "_.jsonl"
    assert work_record.ledger_name("../x") == ".._x.jsonl"


@pytest.mark.asyncio
async def test_the_daemon_reads_the_ledger_under_the_wrappers_name_and_never_off_a_raw_id(
        tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch
    from dark_army_daemon import enrollment, paths
    from dark_army_daemon.daemon import BobDaemon

    ledgers = tmp_path / "shunt"
    ledgers.mkdir()
    monkeypatch.setattr(paths, "SHUNT_LEDGER_DIR", ledgers)
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = board.BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        async def quiet_git(argv, root, *, truncate=False):
            return True, b"", ""
        daemon._run_git = quiet_git
        daemon._publish_board = AsyncMock()
        read_paths = []
        original = work_record.read_shunt_ledger

        def counting(path):
            read_paths.append(Path(path))
            return original(path)

        monkeypatch.setattr(work_record, "read_shunt_ledger", counting)
        # An id with a slash and a space: the wrapper wrote `a_b_c.jsonl`.
        (ledgers / "a_b_c.jsonl").write_text(
            '{"delegation_id": "d1", "lines_kept_out": 40, "ok": true}\n')
        card, _ = store.create({"title": "t", "project": "p", "root": "/tmp",
                                "tool": "claude"})
        store.open_run(card["id"], 100.0, "/tmp", "abc")
        with patch.object(enrollment, "root_enrolled", lambda cwd: "/tmp"):
            await daemon._collect_work_record(
                card["id"], "a/b c", "/tmp", 100.0, "quiet", "", "")
        assert read_paths == [ledgers / "a_b_c.jsonl"]
        record = store.run_for(card["id"])
        assert record["shunt_delegations"] == 1 and record["shunt_lines_kept_out"] == 40
        # An id with no safe name reads no file at all.
        read_paths.clear()
        store.open_run(card["id"], 200.0, "/tmp", "abc")
        with patch.object(enrollment, "root_enrolled", lambda cwd: "/tmp"):
            await daemon._collect_work_record(
                card["id"], "..", "/tmp", 200.0, "quiet", "", "")
        assert read_paths == []
        record = store.run_for(card["id"])
        assert record["shunt_delegations"] == 0
        assert record["shunt_worker_cost_usd"] is None
    finally:
        store.close()
