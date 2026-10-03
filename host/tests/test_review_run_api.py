# host/tests/test_review_run_api.py
"""The Review section's daemon verbs and both doors, on `test_adhoc_terminal`'s
seams: temp paths, a real `BoardStore`, the enrolment ledger stood in for,
`dispatch.spawn_local` stubbed, and a fake `PtyHost`.

The whole loop is `test_the_whole_loop_from_start_to_done`: Start, the
findings file, the picks state, Continue, the step ledger, done.
"""

import asyncio
import json
import os
import subprocess
import time

import pytest

from dark_army_daemon import (daemon_review, dispatch, enrollment, inbox_ack,
                              origin, paths, review_run)
from dark_army_daemon.api_server import ApiServer, _Request, _OMITTABLE_SECTIONS
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

FINDINGS = """\
VERDICT: STOP — one thing breaks

BLOCK · the first thing breaks
  Where:          a.py:1
  Fix:            repair the first

FIX · the second thing is rough
  Where:          b.py:2
  Fix:            smooth the second

WARN · the third is a risk
  Where:          c.py:3
  Fix:            note the third

NOTE · the fourth is a remark
  Where:          d.py:4
  Fix:            reword the fourth
"""


class FakeTerm:
    def __init__(self, handle, name, root):
        self.handle = handle
        self.name = name
        self.root = root
        self.session_id = ""
        self.exited = False


class FakePty:
    """`PtyHost`'s surface the review verbs touch."""

    def __init__(self):
        self.terms = {}
        self.written = []
        self.closed = []
        self.connected = True
        self.close_result = True

    def owns(self, pid):
        return None if getattr(self, "owns_nothing", False) else (
            "h-1" if pid else None)

    def get(self, handle):
        return self.terms.get(handle)

    def terminals(self):
        return list(self.terms.values())

    def for_session(self, sid):
        for t in self.terms.values():
            if t.session_id == sid and sid:
                return t.handle
        return None

    def bind(self, handle, sid):
        self.terms[handle].session_id = sid
        return True

    def unbind(self, sid):
        pass

    def write(self, handle, text="", data=None):
        self.written.append((handle, text))
        return True

    async def drain(self, handle, timeout):
        return True

    async def close(self, handle, grace=0):
        self.closed.append(handle)
        if not self.close_result:
            return False
        self.terms[handle].exited = True
        return True


def _git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e.x",
               GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e.x")
    subprocess.run(["git", *args], cwd=str(root), check=True, env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def project(tmp_path):
    """A clone with an upstream: one commit ahead, one untracked file."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", "main")
    root = tmp_path / "proj"
    _git(tmp_path, "clone", "-q", str(remote), str(root))
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    _git(root, "push", "-q", "-u", "origin", "HEAD:main")
    (root / "b.txt").write_text("two\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "second")
    (root / "c.txt").write_text("three\n")
    return root


def _tree(root):
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        out += [os.path.join(dirpath, f) for f in files]
    return sorted(out)


@pytest.fixture
def daemon(tmp_path, project, monkeypatch):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    d.board_dispatch_enabled = True
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    d._pty = FakePty()
    roots = {dispatch.normalise_root(str(project))}
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(roots))
    monkeypatch.setattr(enrollment, "enrolled_label",
                        lambda cwd: os.path.basename(
                            dispatch.normalise_root(str(cwd or "")))
                        if dispatch.normalise_root(str(cwd or "")) in roots
                        else "")
    d.opened = []

    async def local(root, argv, name, *, stamp="", **_kw):
        d.opened.append({"root": root, "argv": list(argv), "name": name,
                         "stamp": stamp})
        d._pty.terms["h-1"] = FakeTerm("h-1", name, root)
        return True, "bob-terminal", 4242

    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: f"/usr/local/bin/{tool}")
    try:
        yield d
    finally:
        store.close()


def _write(run_id, name, text):
    folder = review_run.folder_for(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


def _observe(d):
    return d._observe_review_runs(d._review_compose_facts())


def _run(d, run_id):
    return next(r for r in d.review_snapshot()["runs"] if r["id"] == run_id)


# --- the offer ---------------------------------------------------------------


def test_the_offer_says_what_is_not_on_the_remote(daemon, project):
    offer = daemon.review_offer(str(project))
    assert offer["available"] is True
    assert offer["upstream"] == "origin/main"
    assert offer["ahead"] == 1 and offer["changed"] == 1
    assert offer["scope_line"] == (
        "everything not on the remote branch, 1 commit ahead and 1 changed file")
    assert [s["id"] for s in offer["steps"]] == ["commit", "push"]


def test_a_folder_with_no_remote_branch_reviews_the_working_tree(daemon, tmp_path,
                                                               monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q")
    (plain / "f").write_text("x")
    monkeypatch.setattr(enrollment, "enrolled_roots",
                        lambda: {dispatch.normalise_root(str(plain))})
    offer = daemon.review_offer(str(plain))
    assert offer["scope_line"] == "no remote branch, uncommitted changes only"
    assert [s["id"] for s in offer["steps"]] == ["commit"]


def test_an_unwatched_folder_is_not_offered(daemon, tmp_path):
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    offer = daemon.review_offer(str(stranger))
    assert offer["available"] is False
    assert offer["reason"] == daemon_review.REVIEW_ROOT_REFUSAL


def test_the_pack_ledger_row_supplies_a_projects_own_steps(daemon, project):
    paths.AGENT_PACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"root": dispatch.normalise_root(str(project)), "profile": "web",
           "after_steps": [["lint", "Lint", "run pnpm lint"],
                           ["commit", "Commit", "commit it"]]}
    paths.AGENT_PACK_PATH.write_text(json.dumps({"projects": [row]}))
    try:
        offer = daemon.review_offer(str(project))
        assert [s["id"] for s in offer["steps"]] == ["lint", "commit"]
    finally:
        paths.AGENT_PACK_PATH.unlink()


# --- Start -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_refused_with_the_launcher_off(daemon, project):
    daemon.board_dispatch_enabled = False
    ok, detail = await daemon.start_review(str(project), "claude", [])
    assert not ok
    assert detail == "Dark Army is not allowed to start sessions (see the ⋯ menu)"
    assert daemon.opened == []


@pytest.mark.asyncio
async def test_start_is_refused_for_an_unenrolled_root(daemon, tmp_path):
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    ok, detail = await daemon.start_review(str(stranger), "claude", [])
    assert not ok and detail == daemon_review.REVIEW_ROOT_REFUSAL
    assert daemon.opened == []


@pytest.mark.asyncio
async def test_start_is_refused_for_an_unknown_step(daemon, project):
    ok, detail = await daemon.start_review(str(project), "claude", ["deploy"])
    assert not ok and detail == daemon_review.REVIEW_STEPS_REFUSAL
    ok, detail = await daemon.start_review(str(project), "claude", "commit")
    assert not ok and detail == daemon_review.REVIEW_STEPS_REFUSAL
    assert daemon.opened == []


@pytest.mark.asyncio
async def test_start_is_refused_for_an_unknown_provider(daemon, project):
    ok, detail = await daemon.start_review(str(project), "cursor", [])
    assert not ok and detail == "cannot start cursor"


@pytest.mark.asyncio
async def test_start_is_refused_while_a_card_is_dispatching(daemon, project):
    card, why = daemon._board.create({
        "title": "a card", "project": project.name, "root": str(project),
        "prompt": "go", "tool": "claude", "column_name": "backlog"})
    assert card is not None, why
    daemon._board.update(card["id"], {"link_state": "dispatching",
                                      "column_name": "in_progress",
                                      "dispatched_at": time.time()})
    ok, detail = await daemon.start_review(str(project), "claude", [])
    assert not ok and detail == dispatch.PROJECT_BUSY_REFUSAL
    assert daemon.opened == []


@pytest.mark.asyncio
async def test_a_binding_review_queues_a_card_start_in_the_same_project(
        daemon, project, monkeypatch):
    ok, run_id = await daemon.start_review(str(project), "claude", ["commit"])
    assert ok, run_id
    assert [c["id"] for c in daemon._launch_inflight([])] == [f"review:{run_id}"]
    card, why = daemon._board.create({
        "title": "a card", "project": project.name, "root": str(project),
        "prompt": "go", "tool": "claude", "column_name": "backlog"})
    assert card is not None, why
    monkeypatch.setattr(daemon, "_known_project_roots",
                        lambda: {dispatch.normalise_root(str(project))})
    ok, _detail = await daemon.dispatch_card(card["id"], allow_unplanned=True)
    assert not ok
    assert len(daemon.opened) == 1, "the card must not open a second terminal"
    assert daemon._board.get(card["id"])["queue_state"] == "queued"


@pytest.mark.asyncio
async def test_a_second_start_on_the_same_root_is_busy(daemon, project):
    ok, _ = await daemon.start_review(str(project), "claude", [])
    assert ok
    daemon._review_attempt = 0.0
    ok, detail = await daemon.start_review(str(project), "claude", [])
    assert not ok and detail == daemon_review.REVIEW_BUSY_REFUSAL
    assert len(daemon.opened) == 1


@pytest.mark.asyncio
async def test_start_writes_the_ticks_and_opens_a_stamped_prompt(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", ["commit"])
    assert ok, run_id
    opened = daemon.opened[0]
    assert opened["argv"][-1].startswith("/review remote")
    assert "1. commit" in opened["argv"][-1]
    assert "push" in opened["argv"][-1].split("Not authorised in this run:")[1]
    assert origin.parse(opened["stamp"])["by"] == "review"
    assert opened["name"] == review_run.pty_name(run_id)
    run_json = review_run.folder_for(run_id) / review_run.RUN_NAME
    assert oct(run_json.stat().st_mode & 0o777) == "0o600"
    assert json.loads(run_json.read_text())["steps"] == ["commit"]
    assert _run(daemon, run_id)["state"] == "reviewing"


# --- the whole loop ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_whole_loop_from_start_to_done(daemon, project):
    before = _tree(project)
    ok, run_id = await daemon.start_review(
        str(project), "claude", ["commit", "push"])
    assert ok, run_id
    assert _tree(project) == before

    # The assistant writes its findings and ends the turn.
    daemon._pty.terms["h-1"].session_id = "s-1"
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    assert _observe(daemon) is True
    run = _run(daemon, run_id)
    assert run["state"] == "picks" and run["verdict"] == "STOP"
    assert [f["grade"] for f in run["findings"]] == [
        "BLOCK", "FIX", "WARN", "NOTE"]
    # ...and the run is listed under Needs you, hidden by an ack only for as
    # long as the findings are the same.
    live = daemon._inbox_live(f"r:{run_id}")
    assert live is not None and live[0] == "review_picks"

    ok, detail = await daemon.continue_review(run_id, [2, 3])
    assert ok, detail
    picks = json.loads((review_run.folder_for(run_id) / review_run.PICKS_NAME)
                       .read_text())
    assert picks["fix"] == [2, 3] and picks["skip"] == [1, 4]
    handle, line = daemon._pty.written[-1]
    assert handle == "h-1" and line.endswith("\r")
    assert "picks.json" in line and "no others" in line
    assert _run(daemon, run_id)["state"] == "fixing"

    _write(run_id, review_run.STEPS_NAME,
           "STEP commit: done — made one commit\nSTEP push: done — pushed\nDONE\n")
    assert _observe(daemon) is True
    run = _run(daemon, run_id)
    assert run["state"] == "done"
    assert [ln["id"] for ln in run["ledger"]] == ["commit", "push"]
    assert [s["id"] for s in run["steps"]] == ["commit", "push"]
    assert all(s["status"] == "done" for s in run["steps"])
    assert run["picks"] == [2, 3]
    assert _tree(project) == before, "a project's tree is untouched"


# --- Continue and End ----------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_is_refused_outside_picks(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    ok, detail = await daemon.continue_review(run_id, [])
    assert not ok and detail == daemon_review.REVIEW_NOT_PICKS_REFUSAL
    ok, detail = await daemon.continue_review("nope", [])
    assert not ok and detail == daemon_review.REVIEW_GONE_REFUSAL
    assert daemon._pty.written == []


@pytest.mark.asyncio
async def test_continue_is_refused_for_a_finding_not_on_the_list(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    _observe(daemon)
    for bad in ([9], [0], [True], ["2"]):
        ok, detail = await daemon.continue_review(run_id, bad)
        assert not ok and detail == daemon_review.REVIEW_PICK_REFUSAL, bad
    assert daemon._pty.written == []
    assert _run(daemon, run_id)["state"] == "picks"


@pytest.mark.asyncio
async def test_continue_is_refused_while_a_permission_prompt_is_up(
        daemon, project, monkeypatch):
    from dark_army_daemon.daemon import TERMINAL_PROMPT_REFUSAL
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    daemon._pty.terms["h-1"].session_id = "s-1"
    _observe(daemon)
    assert _run(daemon, run_id)["session_id"] == "s-1"
    monkeypatch.setattr(daemon, "_prompts_by_session",
                        lambda: {"s-1": {"id": "p"}})
    ok, detail = await daemon.continue_review(run_id, [1])
    assert not ok and detail == TERMINAL_PROMPT_REFUSAL
    assert daemon._pty.written == []
    assert _run(daemon, run_id)["state"] == "picks"
    monkeypatch.setattr(daemon, "_prompts_by_session", lambda: {})
    ok, detail = await daemon.continue_review(run_id, [1])
    assert ok, detail


@pytest.mark.asyncio
async def test_end_closes_by_handle_and_a_second_end_is_refused(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    ok, detail = await daemon.end_review(run_id)
    assert ok, detail
    assert daemon._pty.closed == ["h-1"]
    assert _run(daemon, run_id)["state"] == "ended"
    ok, detail = await daemon.end_review(run_id)
    assert not ok and detail == daemon_review.REVIEW_ENDED_REFUSAL
    assert daemon._pty.closed == ["h-1"]


@pytest.mark.asyncio
async def test_end_checks_the_terminal_is_this_runs(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.terms["h-1"].name = "someone else's terminal"
    ok, detail = await daemon.end_review(run_id)
    assert not ok and detail == daemon_review.REVIEW_IDENTITY_REFUSAL
    assert daemon._pty.closed == []


@pytest.mark.asyncio
async def test_end_keeps_the_record_when_the_close_is_unconfirmed(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.close_result = False
    ok, detail = await daemon.end_review(run_id)
    assert not ok and detail == daemon_review.REVIEW_CLOSE_FAILED_REFUSAL
    assert _run(daemon, run_id)["state"] == "reviewing"


# --- the section, the inbox, the doors ---------------------------------------


@pytest.mark.asyncio
async def test_the_section_carries_no_handle_and_no_folder_path(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", ["commit"])
    text = json.dumps(daemon.review_snapshot())
    assert "h-1" not in text
    assert ".dark-army" not in text and str(paths.REVIEW_RUNS_DIR) not in text
    assert "digest" not in text
    assert daemon.review_snapshot()["available"] is True


@pytest.mark.asyncio
async def test_a_dismissed_picks_entry_stays_hidden_until_the_findings_change(
        daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    _observe(daemon)
    kind, fp = daemon._inbox_live(f"r:{run_id}")
    assert (kind, fp) == ("review_picks", inbox_ack.fingerprint(
        "review_picks", inbox_ack.review_picks_material(
            run_id, _run(daemon, run_id)["findings_at"])))
    ok, detail = await daemon.ack_inbox(f"r:{run_id}", kind, fp)
    assert ok, detail
    assert ("r:" + run_id, kind, fp) in inbox_ack.ack_set(
        daemon.inbox_snapshot()["acks"])
    # The same run with a different arrival stamp is a different subject.
    daemon._review_set(run_id, findings_at=_run(daemon, run_id)["findings_at"] + 5)
    _kind, fp2 = daemon._inbox_live(f"r:{run_id}")
    assert fp2 != fp
    assert inbox_ack.valid_key("r:abc") and "review_picks" in inbox_ack.ACK_KINDS


def test_the_three_actions_are_on_both_phone_tuples_and_the_section_is_omittable():
    for name in ("review_start", "review_continue", "review_end"):
        assert name in ApiServer.LAN_ACTIONS
        assert name in ApiServer.REMOTE_ACTIONS
        assert name in ApiServer.REVIEW_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    assert "review" in _OMITTABLE_SECTIONS


def test_an_unauthenticated_review_press_never_reaches_the_daemon():
    calls = []

    class _Daemon:
        async def start_review(self, *a):
            calls.append(a)
            return True, "x"

    server = ApiServer(_Daemon())
    server.token = "the-secret"
    body = b'{"action": "review_start", "root": "/a", "tool": "claude"}'
    bad = _Request("POST", "/api/action", {},
                   {"content-type": "application/json"}, body)
    assert server._review_request(bad) is None
    good = _Request("POST", "/api/action", {},
                    {"content-type": "application/json",
                     "x-bob-token": "the-secret"}, body)
    assert server._review_request(good)[0] == "review_start"
    assert calls == []


@pytest.mark.asyncio
async def test_the_handler_answers_409_in_words_and_400_for_a_bad_list(daemon):
    server = ApiServer(daemon)
    status, _c, body = await server._review_run(
        "review_continue", {"run_id": "nope", "fix": ["1", "2"]})
    assert status == 409 and daemon_review.REVIEW_GONE_REFUSAL.encode() in body
    status, _c, body = await server._review_run(
        "review_continue", {"run_id": "nope", "fix": [True]})
    assert status == 400
    status, _c, body = await server._review_run(
        "review_start", {"root": "/x", "tool": "claude", "steps": 7})
    assert status == 400


@pytest.mark.asyncio
async def test_review_offer_on_loopback_and_on_the_sealed_door(daemon, project):
    server = ApiServer(daemon)
    from urllib.parse import quote
    status, _c, body = await server._review_offer_for(
        "root=" + quote(str(project), safe=""))
    assert status == 200 and json.loads(body)["upstream"] == "origin/main"
    status, _c, body = await server._review_offer_for({"root": str(project)})
    assert status == 200 and json.loads(body)["ahead"] == 1
    status, _c, body = await server._review_offer_for("")
    assert status == 400
    status, _c, body = await server._sealed_run(
        "review_offer", {"root": str(project)}, "dev-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 200 and json.loads(body)["available"] is True


@pytest.mark.asyncio
async def test_a_phone_action_reaches_the_verb_through_the_lan_door(daemon, project):
    server = ApiServer(daemon)
    status, _c, body = await server._lan_run(
        "review_start", {"root": str(project), "tool": "claude",
                         "steps": ["commit"]})
    assert status == 200, body
    run_id = json.loads(body)["detail"]
    status, _c, body = await server._lan_run("review_end", {"run_id": run_id})
    assert status == 200
    status, _c, body = await server._lan_run("review_end", {"run_id": run_id})
    assert status == 409 and daemon_review.REVIEW_ENDED_REFUSAL.encode() in body


def test_state_carries_the_review_section(daemon):
    server = ApiServer(daemon)
    assert server.state()["review"]["available"] is True


# --- restart survival ----------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_reloads_the_records_and_a_held_handle_keeps_fixing(
        daemon, project, tmp_path):
    ok, run_id = await daemon.start_review(str(project), "claude", ["commit"])
    daemon._pty.terms["h-1"].session_id = "s-1"
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    _observe(daemon)
    ok, detail = await daemon.continue_review(run_id, [1])
    assert ok, detail

    again = BobDaemon(sessions_path=tmp_path / "sessions2.json")
    again._pty = daemon._pty
    again._adopt_review_terminals()
    run = next(r for r in again.review_snapshot()["runs"] if r["id"] == run_id)
    assert run["state"] == "fixing"
    assert again._review_handle_if_named("") is None


@pytest.mark.asyncio
async def test_restart_with_a_lost_handle_reads_exited(daemon, project, tmp_path):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    again = BobDaemon(sessions_path=tmp_path / "sessions3.json")
    again._pty = FakePty()
    again._adopt_review_terminals()
    run = next(r for r in again.review_snapshot()["runs"] if r["id"] == run_id)
    assert run["state"] == "exited" and run["error"] == "terminal closed"


@pytest.mark.asyncio
async def test_an_exited_terminal_ends_the_run_after_its_files_are_read(
        daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.terms["h-1"].exited = True
    _observe(daemon)
    assert _run(daemon, run_id)["state"] == "exited"


@pytest.mark.asyncio
async def test_the_session_row_resolves_the_runs_handle_without_binding(
        daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.terms["h-1"].session_id = "s-9"
    _observe(daemon)
    assert daemon._pty_handle_for("s-9") == "h-1"
    daemon._pty.terms["h-1"].session_id = "s-other"
    assert daemon._review_handle_if_named("s-9") is None


# --- repair dispatch 2 ---------------------------------------------------------


@pytest.mark.asyncio
async def test_picks_json_carries_each_picked_findings_grade_and_line(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    daemon._pty.terms["h-1"].session_id = "s-1"
    _observe(daemon)
    ok, detail = await daemon.continue_review(run_id, [2, 3])
    assert ok, detail
    picks = json.loads((review_run.folder_for(run_id) / review_run.PICKS_NAME).read_text())
    assert picks["fixes"] == [
        {"index": 2, "grade": "FIX", "line": "the second thing is rough"},
        {"index": 3, "grade": "WARN", "line": "the third is a risk"}]
    assert picks["fix"] == [2, 3]


@pytest.mark.asyncio
async def test_a_findings_file_with_a_do_next_list_keeps_the_picks_aligned(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    _write(run_id, review_run.FINDINGS_NAME,
           FINDINGS + "\n## Do next\n\n- FIX: the second thing is rough\n")
    _observe(daemon)
    assert [f["index"] for f in _run(daemon, run_id)["findings"]] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_an_unplaced_pid_still_finds_the_terminal_by_its_name(daemon, project):
    daemon._pty.owns_nothing = True
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    assert ok, run_id
    assert daemon._review_find(run_id)["handle"] == "h-1"
    ok, detail = await daemon.end_review(run_id)
    assert ok and daemon._pty.closed == ["h-1"]


@pytest.mark.asyncio
async def test_end_marks_a_run_ended_when_no_live_terminal_exists(daemon, project):
    """The CLI died at spawn and owns(pid) failed: nothing to close, and the
    run must not block its project for ever."""
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.terms["h-1"].exited = True
    daemon._review_set(run_id, handle="")
    ok, detail = await daemon.end_review(run_id)
    assert ok, detail
    assert _run(daemon, run_id)["state"] == "ended"
    assert daemon._pty.closed == []


@pytest.mark.asyncio
async def test_end_refuses_in_words_while_the_terminal_host_is_down(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.connected = False
    ok, detail = await daemon.end_review(run_id)
    assert not ok and detail == daemon_review.REVIEW_HOST_DOWN_REFUSAL
    assert "try End again" in detail
    assert _run(daemon, run_id)["state"] == "reviewing"


@pytest.mark.asyncio
async def test_a_no_handle_run_is_read_exited_after_the_bind_window(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.terms.clear()
    daemon._review_set(run_id, handle="")
    assert _observe(daemon) is False
    daemon._review_set(
        run_id, started_at=time.time() - daemon_review.HANDLE_BIND_SECONDS - 1)
    _observe(daemon)
    assert _run(daemon, run_id)["state"] == "exited"
    ok, _ = await daemon.end_review(run_id)
    assert ok


@pytest.mark.asyncio
async def test_a_name_resolved_handle_is_saved(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._review_set(run_id, handle="")
    daemon._review_resolve_handle(run_id, daemon._review_find(run_id))
    # The save is handed to the executor, off the loop: wait for it.
    for _ in range(100):
        saved = review_run.load_records(paths.REVIEW_RUNS_PATH)
        if next(r for r in saved if r["id"] == run_id).get("handle") == "h-1":
            break
        await asyncio.sleep(0.02)
    assert next(r for r in saved if r["id"] == run_id)["handle"] == "h-1"


@pytest.mark.asyncio
async def test_a_live_run_is_always_published_beyond_the_bound(daemon):
    for i in range(review_run.MAX_PUBLISHED_RUNS + 3):
        rec = review_run.new_record(
            run_id=f"{i:08x}", root="/p", project="p", tool="claude", scope="",
            scope_line="", upstream="", steps=[], now=100.0 + i)
        rec["state"] = "done"
        daemon._review_runs.append(rec)
    live = review_run.new_record(
        run_id="aaaaaaaa", root="/q", project="q", tool="claude", scope="",
        scope_line="", upstream="", steps=[], now=1.0)
    daemon._review_runs.append(live)
    ids = [r["id"] for r in daemon.review_snapshot()["runs"]]
    assert "aaaaaaaa" in ids
    assert len(ids) == review_run.MAX_PUBLISHED_RUNS


@pytest.mark.asyncio
async def test_the_review_pass_runs_with_no_board_and_survives_a_raise(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._board = None
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    daemon._review_pty_facts = daemon._review_compose_facts()
    assert daemon._reconcile_review() is True
    assert _run(daemon, run_id)["state"] == "picks"

    def boom(_facts):
        raise RuntimeError("x")

    daemon._observe_review_runs = boom
    assert daemon._reconcile_review() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,adopted", [("claude", False), ("grok", False),
                                          ("claude", True)])
async def test_continue_will_not_type_into_an_unbound_run(
        daemon, project, monkeypatch, tool, adopted):
    monkeypatch.setattr(dispatch, "_UNSUPPORTED", {}, raising=False)
    ok, run_id = await daemon.start_review(str(project), tool, [])
    assert ok, run_id
    if adopted:
        daemon._review_set(run_id, tool="")
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    _observe(daemon)
    assert daemon._pty.terms["h-1"].session_id == ""
    ok, detail = await daemon.continue_review(run_id, [1])
    assert not ok and detail == daemon_review.REVIEW_NOT_BOUND_REFUSAL
    assert "has not connected yet" in detail and "may have landed" not in detail
    assert daemon._pty.written == []
    assert _run(daemon, run_id)["state"] == "picks"


@pytest.mark.asyncio
async def test_continue_still_types_by_handle_for_an_unbound_codex_run(daemon, project, monkeypatch):
    monkeypatch.setattr(dispatch, "_UNSUPPORTED", {}, raising=False)
    ok, run_id = await daemon.start_review(str(project), "codex", [])
    assert ok, run_id
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    _observe(daemon)
    ok, detail = await daemon.continue_review(run_id, [1])
    assert ok, detail
    assert daemon._pty.written


@pytest.mark.asyncio
async def test_the_handle_is_resolved_only_for_a_terminal_wearing_the_runs_name(daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.terms["h-1"].session_id = "s-9"
    _observe(daemon)
    assert daemon._review_handle_if_named("s-9") == "h-1"
    daemon._pty.terms["h-1"].name = "another terminal"
    assert daemon._review_handle_if_named("s-9") is None


@pytest.mark.asyncio
async def test_away_a_review_that_ticks_rebuild_or_restart_is_refused():
    """`rebuild_app` is on the away door as a person's own press, but a review
    run's Rebuild and Restart steps are not that press: the away door still
    refuses either in words, before the lease or the daemon; Commit alone is
    untouched by the rule."""
    from dark_army_daemon import api_server as api_mod
    calls = []

    class _Daemon:
        async def start_review(self, *a):
            calls.append(a)
            return True, "x"

    server = ApiServer(_Daemon())
    assert "rebuild_app" in ApiServer.REMOTE_ACTIONS
    for steps in (["rebuild"], "commit,restart", ["commit", "rebuild", "push"]):
        status, _c, body = await server._sealed_run(
            "action", {"action": "review_start", "root": "/a",
                       "tool": "claude", "steps": steps}, "dev-1",
            actions=ApiServer.REMOTE_ACTIONS, check_lease=False, record=False)
        assert status == 403
        assert json.loads(body)["detail"] == api_mod.REVIEW_HOME_ONLY_REFUSAL
    assert calls == []
    status, _c, _b = await server._sealed_run(
        "action", {"action": "review_start", "root": "/a", "tool": "claude",
                   "steps": ["rebuild", "restart"]}, "dev-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 200 and len(calls) == 1
    status, _c, _b = await server._sealed_run(
        "action", {"action": "review_start", "root": "/a", "tool": "claude",
                   "steps": ["commit"]}, "dev-1",
        actions=ApiServer.REMOTE_ACTIONS, check_lease=False, record=False)
    assert status == 200 and len(calls) == 2


@pytest.mark.asyncio
async def test_a_third_door_tuple_still_refuses_rebuild_and_restart_steps():
    """The guard fails closed: any tuple but `LAN_ACTIONS` refuses."""
    from dark_army_daemon import api_server as api_mod
    calls = []

    class _Daemon:
        async def start_review(self, *a):
            calls.append(a)
            return True, "x"

    server = ApiServer(_Daemon())
    status, _c, body = await server._sealed_run(
        "action", {"action": "review_start", "root": "/a", "tool": "claude",
                   "steps": ["rebuild"]}, "dev-1",
        actions=tuple(ApiServer.REMOTE_ACTIONS), check_lease=False, record=False)
    assert status == 403
    assert json.loads(body)["detail"] == api_mod.REVIEW_HOME_ONLY_REFUSAL
    status, _c, _b = await server._sealed_run(
        "action", {"action": "review_start", "root": "/a", "tool": "claude",
                   "steps": ["rebuild"]}, "dev-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 200 and len(calls) == 1


@pytest.mark.asyncio
async def test_the_checklist_follows_a_findings_file_that_grows_until_picked(
        daemon, project):
    """An assistant may write the VERDICT line and a few findings, then add
    the rest: until a person decides, the checklist follows the file."""
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    assert ok, run_id
    head = FINDINGS.split("WARN ·")[0]
    _write(run_id, review_run.FINDINGS_NAME, head)
    _observe(daemon)
    first = _run(daemon, run_id)
    assert first["state"] == "picks" and len(first["findings"]) == 2
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    assert _observe(daemon) is True
    grown = _run(daemon, run_id)
    assert grown["state"] == "picks" and len(grown["findings"]) == 4
    assert _observe(daemon) is False, "an unchanged file moves nothing"
    daemon._pty.terms["h-1"].session_id = "s-1"
    ok, detail = await daemon.continue_review(run_id, [4])
    assert ok, detail
    _write(run_id, review_run.FINDINGS_NAME, head)
    _observe(daemon)
    assert len(_run(daemon, run_id)["findings"]) == 4, "a decision freezes it"


@pytest.mark.asyncio
async def test_continue_is_not_a_success_when_the_run_left_picks_meanwhile(
        daemon, project, monkeypatch):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.terms["h-1"].session_id = "s-1"
    _write(run_id, review_run.FINDINGS_NAME, FINDINGS)
    _observe(daemon)

    async def typed_then_exited(handle, line, session_id):
        daemon._review_set(run_id, state="exited")
        return True, ""

    monkeypatch.setattr(daemon, "_terminal_line_input", typed_then_exited)
    ok, detail = await daemon.continue_review(run_id, [1])
    assert not ok and detail == daemon_review.REVIEW_NOT_PICKS_REFUSAL
    assert _run(daemon, run_id)["state"] == "exited"


@pytest.mark.asyncio
async def test_a_done_run_with_its_terminal_open_still_holds_the_project(
        daemon, project):
    ok, run_id = await daemon.start_review(str(project), "claude", [])
    daemon._pty.terms["h-1"].session_id = "s-1"   # bound: no launch pending
    daemon._review_set(run_id, state="done")
    daemon._review_attempt = 0.0
    ok, detail = await daemon.start_review(str(project), "claude", [])
    assert not ok and detail == daemon_review.REVIEW_BUSY_REFUSAL
    daemon._pty.terms["h-1"].exited = True
    daemon._review_attempt = 0.0
    ok, detail = await daemon.start_review(str(project), "claude", [])
    assert ok, detail
