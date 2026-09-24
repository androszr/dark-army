"""The work-record read: one body, three doors.

`_work_record_report_for` is exercised directly against an `ApiServer` holding
a `BoardStore` on `tmp_path` (the shape `test_lan_access.py` already uses for
board-backed reads), and the sealed `work_record` kind is exercised through
`_sealed_run` on both doors' argument tuples.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from dark_army_daemon import board, enrollment, work_record
from dark_army_daemon.api_server import ApiServer


class _Daemon:
    """Only what the report reaches for. The real daemon's `_run_git` is a
    method of the board mixin; the report calls it through `getattr`, so this
    stands in for it without a subprocess where the test does not want one."""

    def __init__(self, store):
        self._board = store
        self._identities = None
        self.git_calls = []

    async def _run_git(self, argv, root, *, truncate=False):
        self.git_calls.append((list(argv), root, truncate))
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(root), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=work_record.git_env())
        out, _ = await proc.communicate()
        if len(out) > work_record.MAX_GIT_OUTPUT_BYTES and truncate:
            out = out[:work_record.MAX_GIT_OUTPUT_BYTES]
        if proc.returncode:
            return False, out, work_record.GIT_FAILED_REASON
        return True, out, ""


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


@pytest.fixture()
def server(tmp_path, repo, monkeypatch):
    store = board.BoardStore(tmp_path / "board.db")
    store.connect()
    daemon = _Daemon(store)
    # The read re-checks enrolment *at the moment of the read*, never from the
    # stored row — so the ledger is what this fixture points at.
    monkeypatch.setattr(enrollment, "root_enrolled",
                        lambda cwd: str(repo) if cwd == str(repo) else "")
    srv = ApiServer(daemon, port=0)
    yield srv, daemon, store, repo
    store.close()


def _card(store, repo):
    card, _ = store.create({"title": "t", "project": "proj",
                            "root": str(repo), "prompt": "x",
                            "tool": "claude"})
    return card["id"]


def _run(coro):
    return asyncio.run(coro)


def _body(result):
    status, ctype, raw = result
    return status, json.loads(raw)


# --- the record shape --------------------------------------------------------

def test_no_card_is_refused_in_words(server):
    srv = server[0]
    status, out = _body(_run(srv._work_record_report_for("")))
    assert status == 400
    assert "card" in out["error"]


def test_an_unknown_card_is_available_with_no_record(server):
    srv = server[0]
    status, out = _body(_run(srv._work_record_report_for("card=nope")))
    assert status == 200
    # `available` is **stated**: an open board with no record for that card
    # must be distinguishable from a board that is not open at all.
    assert out["available"] is True
    assert out["record"] is None
    assert out["caption"] == work_record.CAPTION


def test_a_run_that_has_not_ended_publishes_no_record(server):
    srv, _daemon, store, repo = server
    cid = _card(store, repo)
    store.open_run(cid, 100.0, str(repo), "abc")
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}")))
    assert status == 200 and out["record"] is None


def test_a_finished_run_comes_back_whole(server):
    srv, _daemon, store, repo = server
    cid = _card(store, repo)
    store.open_run(cid, 100.0, str(repo), "abc")
    store.close_run(cid, 100.0, "s1", "closed", "## Work done", "tldr",
                    [{"path": "a.py", "added": 1, "removed": 0,
                      "binary": False, "new": False}], True)
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}")))
    assert status == 200
    assert out["record"]["report"] == "## Work done"
    assert out["record"]["files"][0]["path"] == "a.py"
    assert out["record"]["files_available"] is True


def test_the_head_carries_the_three_shunt_keys(server):
    """The snapshot head and the on-demand record both carry the helper's
    figures, and the on-demand record carries the daemon's sentence; a run
    that delegated nothing carries zeros, an unknown cost and no sentence."""
    srv, _daemon, store, repo = server
    cid = _card(store, repo)
    store.open_run(cid, 100.0, str(repo), "abc")
    store.close_run(cid, 100.0, "s1", "closed", "## Work done", "", [], True,
                    shunt_delegations=3, shunt_lines_kept_out=2140,
                    shunt_worker_cost_usd=None)
    head = store.run_headlines()[cid]
    for key in ("shunt_delegations", "shunt_lines_kept_out", "shunt_worker_cost_usd"):
        assert key in head
    assert head["shunt_delegations"] == 3 and head["shunt_worker_cost_usd"] is None
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}")))
    assert status == 200
    record = out["record"]
    assert record["shunt_delegations"] == 3
    assert record["shunt_lines_kept_out"] == 2140
    assert record["shunt_worker_cost_usd"] is None
    assert record["shunt_words"] == work_record.shunt_words(record)
    assert "2,140 lines" in record["shunt_words"] and "unknown" in record["shunt_words"]

    other = _card(store, repo)
    store.open_run(other, 100.0, str(repo), "abc")
    store.close_run(other, 100.0, "s2", "quiet", "", "", [], True)
    status, out = _body(_run(srv._work_record_report_for(f"card={other}")))
    assert out["record"]["shunt_delegations"] == 0
    assert out["record"]["shunt_words"] == ""


def test_the_card_fetch_carries_the_sentence_on_the_full_record(server):
    import types
    srv, daemon, store, repo = server
    daemon._identities = types.SimpleNamespace(peek=lambda author: "")
    cid = _card(store, repo)
    store.open_run(cid, 100.0, str(repo), "abc")
    store.close_run(cid, 100.0, "s1", "closed", "## Work done", "", [], True,
                    shunt_delegations=1, shunt_lines_kept_out=400,
                    shunt_worker_cost_usd=0.02)
    card = srv._card_collect(cid, with_plan=False)["cards"][0]
    assert card["work_record_full"]["shunt_words"] == (
        "1 delegation kept 400 lines out of the main model; helper cost $0.02")


def test_a_shut_board_says_so_rather_than_looking_empty(server):
    srv, daemon, _store, _repo = server
    daemon._board = None
    status, out = _body(_run(srv._work_record_report_for("card=x")))
    assert status == 200
    assert out["available"] is False and out["record"] is None


# --- one file's diff ---------------------------------------------------------

def _recorded(store, repo, rows):
    cid = _card(store, repo)
    head = subprocess.run(work_record.argv_baseline(str(repo)),
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    store.open_run(cid, 100.0, str(repo), head)
    store.close_run(cid, 100.0, "s1", "closed", "r", "", rows, True)
    return cid


def test_a_non_integer_file_is_refused(server):
    srv, _daemon, store, repo = server
    cid = _recorded(store, repo, [{"path": "a.py", "added": 1, "removed": 0,
                                   "binary": False, "new": False}])
    status, out = _body(_run(
        srv._work_record_report_for(f"card={cid}&file=../etc/passwd")))
    assert status == 400
    assert "whole number" in out["error"]


def test_an_out_of_range_file_is_a_404_in_words(server):
    srv, _daemon, store, repo = server
    cid = _recorded(store, repo, [{"path": "a.py", "added": 1, "removed": 0,
                                   "binary": False, "new": False}])
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}&file=7")))
    assert status == 404
    assert "not on Dark Army's list" in out["error"]


def test_asking_for_a_file_on_a_card_with_no_record(server):
    srv, _daemon, store, repo = server
    cid = _card(store, repo)
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}&file=0")))
    assert status == 404
    assert "no record" in out["error"]


def test_one_file_comes_back_and_only_that_file(server):
    srv, _daemon, store, repo = server
    (repo / "a.py").write_text("one\ntwo\nthree\n")
    (repo / "c.py").write_text("elsewhere\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    cid = _recorded(store, repo, [
        {"path": "a.py", "added": 1, "removed": 0, "binary": False,
         "new": False},
        {"path": "c.py", "added": 1, "removed": 0, "binary": False,
         "new": False}])
    # The baseline was taken after the second commit, so re-point it at the
    # first by re-opening the run against HEAD~1.
    head = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=str(repo),
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    with store._lock:
        store._conn.execute("UPDATE card_runs SET baseline = ? WHERE card_id = ?",
                            (head, cid))
        store._conn.commit()
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}&file=0")))
    assert status == 200
    assert out["available"] is True and out["path"] == "a.py"
    assert "three" in out["text"]
    assert "elsewhere" not in out["text"]
    assert out["truncated"] is False


def test_an_untracked_file_is_diffed_against_nothing(server):
    srv, _daemon, store, repo = server
    (repo / "brand-new.py").write_text("fresh\n")
    cid = _recorded(store, repo, [{"path": "brand-new.py", "binary": False,
                                   "new": True}])
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}&file=0")))
    assert status == 200
    assert out["new"] is True
    assert "fresh" in out["text"]


def test_a_project_un_enrolled_since_the_run_is_refused(server, monkeypatch):
    srv, _daemon, store, repo = server
    cid = _recorded(store, repo, [{"path": "a.py", "added": 1, "removed": 0,
                                   "binary": False, "new": False}])
    monkeypatch.setattr(enrollment, "root_enrolled", lambda cwd: "")
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}&file=0")))
    assert status == 404
    assert out["error"] == work_record.NOT_ENROLLED_REASON


def test_a_file_outside_the_project_is_refused(server):
    srv, _daemon, store, repo = server
    # A path git could never have produced, forced into the stored list: the
    # containment re-check is belt and braces and it has to hold anyway.
    cid = _recorded(store, repo, [{"path": "../escape.txt", "binary": False,
                                   "new": True}])
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}&file=0")))
    assert status == 404
    assert "outside" in out["error"]


def test_a_long_diff_is_truncated_and_says_so(server):
    srv, _daemon, store, repo = server
    (repo / "big.py").write_text("x\n" * 60_000)
    cid = _recorded(store, repo, [{"path": "big.py", "binary": False,
                                   "new": True}])
    status, out = _body(_run(srv._work_record_report_for(f"card={cid}&file=0")))
    assert status == 200
    assert out["truncated"] is True
    assert len(out["text"]) <= work_record.MAX_DIFF_BYTES


# --- the doors ---------------------------------------------------------------

def test_the_record_is_a_read_not_an_action():
    """`work_record` is a `kind` beside `state`, `usage` and `log`. Neither
    action tuple grew, and the away list still may not exceed home's."""
    assert "work_record" not in ApiServer.LAN_ACTIONS
    assert "work_record" not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_both_sealed_doors_answer_the_kind_with_no_lease_and_no_record(server):
    srv, _daemon, store, repo = server
    cid = _recorded(store, repo, [{"path": "a.py", "added": 1, "removed": 0,
                                   "binary": False, "new": False}])
    for actions, check_lease, record in (
            (ApiServer.LAN_ACTIONS, False, False),
            (ApiServer.REMOTE_ACTIONS, True, True)):
        status, out = _body(_run(srv._sealed_run(
            "work_record", {"query": f"card={cid}"}, "dev-1",
            actions=actions, check_lease=check_lease, record=record)))
        # A read: the away door's lapsed-lease refusal never applies, and no
        # `remote_activity` recorder is called (this daemon has none, so a
        # call would raise rather than pass quietly).
        assert status == 200
        assert out["record"]["verdict"] == "closed"


def test_an_unknown_sealed_kind_is_still_a_404(server):
    srv = server[0]
    status, _ctype, raw = _run(srv._sealed_run(
        "work_records", {}, "dev-1", actions=ApiServer.LAN_ACTIONS,
        check_lease=False, record=False))
    assert status == 404
    assert json.loads(raw)["error"] == "not found"
