"""Card isolation through the daemon: Start prepares a card's own worktree,
then opens its terminal there; Done removes the folder and keeps the branch.

`test_dispatch.py`'s seam: a real `BoardStore` on a temp file,
`dispatch.spawn` stubbed to record where the terminal would open,
`_known_project_roots` monkeypatched, and — where the case needs one — a real
git repository under `tmp_path`, so every `git worktree` call Dark Army makes
is a real one. `docs/card-worktrees.md`,
`plans/2026-09-28-card-worktree-isolation.md`.
"""

import asyncio
import os
import subprocess
import time

import pytest

from dark_army_daemon import (daemon_board, dispatch, trust_marks, work_record,
                               worktrees)
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _git(root, *args) -> str:
    done = subprocess.run(["git", *args], cwd=str(root), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=work_record.git_env())
    assert done.returncode == 0, done.stderr
    return done.stdout.decode()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")
    return os.path.realpath(root)


def _trust(monkeypatch, tmp_path, root, *, claude=True):
    """A fake `~/.claude.json` in which the person has (or has not) trusted
    `root` — the setup script's first gate."""
    import json
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"projects": {
        str(root): {"hasTrustDialogAccepted": bool(claude)}}}))
    monkeypatch.setattr(trust_marks, "CLAUDE_CONFIG_PATH", path)
    monkeypatch.setattr(trust_marks, "CODEX_CONFIG_PATH",
                        tmp_path / "no-codex.toml")
    return path


def _script(root, body: str, mode: int = 0o644) -> str:
    script = os.path.join(root, worktrees.SETUP_SCRIPT)
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w") as handle:
        handle.write(body)
    os.chmod(script, mode)
    return script


def _make(store, root, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": str(root),
              "prompt": "go", "tool": "claude", "column_name": "backlog"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    time.sleep(0.002)
    return card


def _stub(d, monkeypatch, root, opened):
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(root)})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")

    async def accept(spawn_root, argv, name, **kw):
        opened.append({"root": spawn_root, "argv": list(argv), "kw": dict(kw)})
        return True, "opened", None

    monkeypatch.setattr(dispatch, "spawn", accept)


async def _settle(d):
    """Wait for every prepare task, including one a re-entry started."""
    for _ in range(20):
        tasks = [t for t in (getattr(d, "_worktree_tasks", None) or set())
                 if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


async def _released(d):
    """Wait for the detached release task (`_kick_worktree_releases`), and
    for any release it queued again, a bounded number of rounds."""
    for _ in range(20):
        task = getattr(d, "_worktree_release_task", None)
        if task is None or task.done():
            if not (getattr(d, "_worktree_release_queue", None)
                    or getattr(d, "_worktree_orphans", None)):
                return
            await d._kick_worktree_releases()
            task = d._worktree_release_task
        await task


def _git_calls(d, monkeypatch):
    """Record every argv `_run_git` is handed, and still run it."""
    calls: list = []
    real = d._run_git

    async def spy(argv, root, **kw):
        calls.append(list(argv))
        return await real(argv, root, **kw)

    monkeypatch.setattr(d, "_run_git", spy)
    return calls


def _worktree_calls(calls, verb):
    return [c for c in calls if "worktree" in c and verb in c]


# --- the switch and the non-git project: exactly today ----------------------------

@pytest.mark.asyncio
async def test_isolation_off_starts_in_the_root_byte_for_byte(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    d.set_board_isolation_override(repo, False)
    card = _make(store, repo)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert len(opened) == 1
    assert opened[0]["root"] == repo
    assert "cwd" not in opened[0]["kw"]
    # `test_an_accepted_spawn_marks_the_card_dispatching`'s argv exactly.
    assert opened[0]["argv"] == ["/bin/claude", "go"]
    assert store.get(card["id"])["link_state"] == "dispatching"
    assert store.get(card["id"])["worktree_path"] == ""


@pytest.mark.asyncio
async def test_a_root_that_is_not_a_git_checkout_makes_no_worktree(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = os.path.realpath(tmp_path / "plain")
    os.makedirs(root)
    opened: list = []
    _stub(d, monkeypatch, root, opened)
    calls = _git_calls(d, monkeypatch)
    card = _make(store, root)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert "cwd" not in opened[0]["kw"]
    assert not [c for c in calls if "worktree" in c]
    assert d._isolation_state(root) == ""


# --- prepare, then dispatch ------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_first_start_prepares_then_opens_in_the_worktree(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    baselines: list = []
    monkeypatch.setattr(d, "_schedule_work_baseline",
                        lambda cid, root, now: baselines.append((cid, root)))
    card = _make(store, repo, title="Fix the wrap")

    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert (ok, detail) == (True, worktrees.PREPARING_NOTE)
    # Nothing written yet: a preparing card is not `dispatching`.
    got = store.get(card["id"])
    assert got["link_state"] == "" and got["column_name"] == "backlog"
    assert opened == []
    assert d._worktree_note(got) == worktrees.PREPARING_NOTE

    await _settle(d)
    path = worktrees.worktree_dir(repo, card["id"])
    assert len(opened) == 1
    assert opened[0]["root"] == repo
    assert opened[0]["kw"]["cwd"] == path
    got = store.get(card["id"])
    assert got["worktree_path"] == path
    assert got["worktree_branch"] == worktrees.branch_name(card)
    assert got["link_state"] == "dispatching"
    assert baselines == [(card["id"], path)]
    assert os.path.isdir(path)
    assert d._worktree_preparing == {}
    # The main checkout does not see the folder.
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.asyncio
async def test_a_second_press_during_preparation_is_told_to_wait(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    release = asyncio.Event()
    real = d._prepare_worktree

    async def slow(head, root):
        await release.wait()
        return await real(head, root)

    monkeypatch.setattr(d, "_prepare_worktree", slow)
    card = _make(store, repo)
    other = _make(store, repo, title="another card")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE

    before = store.get(card["id"])
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert (ok, detail) == (False, dispatch.WORKTREE_PREPARING_REFUSAL)
    assert store.get(card["id"]) == before
    assert dispatch.is_transient(dispatch.WORKTREE_PREPARING_REFUSAL)

    # A queued replay of the same card holds: still queued, no words.
    store.update(card["id"], {"queue_state": "queued",
                              "queued_at": time.time()}, bump=False)
    ok, detail = await d.dispatch_card(card["id"], queued_replay=True)
    assert not ok
    held = store.get(card["id"])
    assert held["queue_state"] == "queued" and held["dispatch_error"] == ""

    # Another card of the project waits in the busy words — and queues.
    ok, detail = await d.dispatch_card(other["id"], allow_unplanned=True)
    assert store.get(other["id"])["queue_state"] == "queued"
    assert opened == []

    release.set()
    await _settle(d)
    assert len(opened) == 1
    assert opened[0]["kw"]["cwd"] == worktrees.worktree_dir(repo, card["id"])
    assert d._worktree_preparing == {}


@pytest.mark.asyncio
async def test_a_failing_setup_script_keeps_the_card_and_runs_again(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    _trust(monkeypatch, tmp_path, repo)
    count = tmp_path / "runs.txt"
    script = _script(repo, f'echo run >> "{count}"\necho "in $DARK_ARMY_WORKTREE"\n'
                     "exit 3\n")
    card = _make(store, repo)

    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE
    await _settle(d)
    got = store.get(card["id"])
    log = worktrees.setup_log_path(repo, card["id"])
    assert "exit 3" in got["dispatch_error"]
    assert log in got["dispatch_error"]
    assert got["column_name"] == "backlog" and got["link_state"] == ""
    assert got["worktree_path"] == ""
    assert opened == []
    path = worktrees.worktree_dir(repo, card["id"])
    assert os.path.isdir(path), "the worktree is kept for the next press"
    assert "in " + path in open(log).read()

    with open(script, "w") as handle:
        handle.write(f'echo run >> "{count}"\nexit 0\n')
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE
    await _settle(d)
    assert count.read_text().count("run") == 2
    assert len(opened) == 1 and opened[0]["kw"]["cwd"] == path
    assert store.get(card["id"])["worktree_path"] == path


@pytest.mark.asyncio
async def test_a_recorded_worktree_that_still_exists_is_reused(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    card = _make(store, repo)
    path = worktrees.worktree_dir(repo, card["id"])
    branch = worktrees.branch_name(card)
    _git(repo, "worktree", "add", "-q", "-b", branch, path, "main")
    store.record_worktree(card["id"], path, branch)
    calls = _git_calls(d, monkeypatch)

    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok and detail != worktrees.PREPARING_NOTE
    assert opened[0]["kw"]["cwd"] == path
    assert not _worktree_calls(calls, "add")


@pytest.mark.asyncio
async def test_a_window_too_old_for_a_subfolder_is_a_refusal_on_the_card(
        daemon, repo, monkeypatch):
    """The real `dispatch.spawn` over a stubbed `vscode_reveal`: the words
    come back as the spawn's refusal, and the card never starts in the
    main checkout instead."""
    from dark_army_daemon import vscode_reveal
    d, store = daemon
    monkeypatch.setattr(d, "_known_project_roots", lambda: {repo})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")
    monkeypatch.setattr(vscode_reveal, "_bob_ext_locks", lambda: [{
        "port": 1, "authToken": "t", "extensionVersion": "0.1.21",
        "workspaceFolders": [repo]}])

    async def never(*a, **k):
        raise AssertionError("nothing may be posted to an older window")

    monkeypatch.setattr(vscode_reveal, "_post_json", never)
    card = _make(store, repo)
    await d.dispatch_card(card["id"], allow_unplanned=True)
    await _settle(d)
    got = store.get(card["id"])
    assert got["dispatch_error"] == dispatch.WORKTREE_WINDOW_REFUSAL
    assert got["link_state"] == ""


# --- a batch shares the head's folder --------------------------------------------------

@pytest.mark.asyncio
async def test_a_batch_works_in_one_worktree_carried_onto_the_next_card(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    os.makedirs(os.path.join(repo, "plans"))
    cards = []
    for i in (1, 2):
        card = _make(store, repo, title=f"card {i}", column_name="prep")
        plan = os.path.join(repo, "plans", f"card-{i}.md")
        with open(plan, "w") as handle:
            handle.write(f"# card {i}\n")
        card, detail = store.attach_plan(card["id"], plan, "")
        assert card is not None, detail
        cards.append(card)

    ok, detail = await d.start_cards([c["id"] for c in cards])
    assert ok and detail.startswith(worktrees.PREPARING_NOTE)
    await _settle(d)
    head_path = worktrees.worktree_dir(repo, cards[0]["id"])
    assert len(opened) == 1 and opened[0]["kw"]["cwd"] == head_path
    assert store.get(cards[0]["id"])["worktree_path"] == head_path
    assert store.get(cards[1]["id"])["worktree_path"] == ""
    assert store.get(cards[1]["id"])["batch_id"]

    store.bind_session(cards[0]["id"], "s-batch")
    ok, detail, moved = await d.advance_batch_by_session("s-batch")
    assert ok, detail
    second = store.get(cards[1]["id"])
    assert second["session_id"] == "s-batch"
    assert second["worktree_path"] == head_path
    assert second["worktree_branch"] == worktrees.branch_name(cards[0])
    assert len(opened) == 1, "one folder, one terminal, for the whole batch"


# --- release at Done --------------------------------------------------------------------

async def _prepared(d, store, repo, monkeypatch, title="do the thing"):
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    card = _make(store, repo, title=title)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE
    await _settle(d)
    got = store.get(card["id"])
    assert got["worktree_path"]
    return got


def _end(d, store, card_id, sid):
    """The reconcile's `mark_ended` seam, as a session that has gone."""
    d._board_missing_since[card_id] = time.time() - 10_000
    d._reconcile_board({})
    assert store.get(card_id)["link_state"] == "ended"


@pytest.mark.asyncio
async def test_a_done_card_with_a_live_session_keeps_its_folder(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    store.update(card["id"], {"session_id": "s1", "link_state": "live",
                              "column_name": "done"}, bump=False)
    await d._maybe_release_worktree({"id": card["id"]})
    before = dict(card, column_name="in_progress", link_state="live")
    await d._after_board_write(before, store.get(card["id"]))
    assert os.path.isdir(card["worktree_path"])
    assert store.get(card["id"])["worktree_path"] == card["worktree_path"]


@pytest.mark.asyncio
async def test_the_mark_ended_seam_removes_a_done_cards_folder(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    unmarked: list = []
    monkeypatch.setattr(trust_marks, "unmark", unmarked.append)
    store.update(card["id"], {"session_id": "s1", "link_state": "live",
                              "column_name": "done"}, bump=False)
    _end(d, store, card["id"], "s1")
    await d._flush_work_records()
    await _released(d)
    assert not os.path.exists(card["worktree_path"])
    got = store.get(card["id"])
    # The folder is gone and the card keeps the memory of its branch: a
    # finished card's branch is what MERGE lands (`merges.py`).
    assert got["worktree_path"] == ""
    assert got["worktree_branch"] == card["worktree_branch"]
    assert unmarked == [card["worktree_path"]]
    # The branch is there to merge.
    assert card["worktree_branch"] in _git(repo, "branch", "--list", "card/*")


@pytest.mark.asyncio
async def test_a_dirty_folder_is_kept_and_the_card_says_so(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    with open(os.path.join(card["worktree_path"], "unsaved.txt"), "w") as fh:
        fh.write("not committed\n")
    store.update(card["id"], {"session_id": "s1", "link_state": "live",
                              "column_name": "done"}, bump=False)
    _end(d, store, card["id"], "s1")
    await d._flush_work_records()
    await _released(d)
    assert os.path.isdir(card["worktree_path"])
    got = store.get(card["id"])
    assert got["worktree_path"] == card["worktree_path"]
    decorated = d._decorate_card_for_snapshot(got, {got["id"]: got},
                                              active=set())
    assert decorated["worktree_note"] == worktrees.KEPT_NOTE.format(
        card["worktree_path"])
    assert decorated["isolation"] == "on"


@pytest.mark.asyncio
async def test_a_folder_whose_check_was_copied_to_the_main_checkout_is_removed(
        daemon, repo, monkeypatch):
    """A check flagged inside a side folder is copied to the main checkout
    and its original stays: a byte-identical duplicate is not crew output
    that exists nowhere else, so Done removes the folder. A copy that
    differs keeps it."""
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    with open(os.path.join(repo, ".git", "info", "exclude"), "a") as fh:
        fh.write("/manual-check/\n")
    rel = os.path.join("manual-check", "2026-09-28-x", "check.md")
    side = os.path.join(card["worktree_path"], rel)
    main = os.path.join(repo, rel)
    for target in (side, main):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write("1. Open it.\n")
    store.update(card["id"], {"session_id": "s1", "link_state": "live",
                              "column_name": "done"}, bump=False)
    _end(d, store, card["id"], "s1")
    await d._flush_work_records()
    await _released(d)
    assert not os.path.isdir(card["worktree_path"])
    assert os.path.isfile(main)
    assert store.get(card["id"])["worktree_path"] == ""


def test_crew_output_is_copied_only_when_every_file_matches(tmp_path):
    side, home = tmp_path / "side", tmp_path / "home"
    for root, text in ((side, "a"), (home, "a")):
        (root / "scout" / "r").mkdir(parents=True)
        (root / "scout" / "r" / "report.md").write_text(text)
    listing = b"!! scout/\0"
    assert daemon_board._crew_output_is_copied(listing, str(side), str(home))
    (home / "scout" / "r" / "report.md").write_text("b")
    assert not daemon_board._crew_output_is_copied(listing, str(side), str(home))
    (home / "scout" / "r" / "report.md").unlink()
    assert not daemon_board._crew_output_is_copied(listing, str(side), str(home))
    assert not daemon_board._crew_output_is_copied(b"", str(side), str(home))
    assert not daemon_board._crew_output_is_copied(listing, str(side), str(side))
    assert not daemon_board._crew_output_is_copied(b"!! ../x\0", str(side),
                                               str(home))


@pytest.mark.asyncio
async def test_a_folder_holding_an_ignored_scout_report_is_kept(
        daemon, repo, monkeypatch):
    """Git removes ignored files without refusing, and a scout report, a
    plan or a check file is git-ignored: the folder stays, with the note."""
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    with open(os.path.join(repo, ".git", "info", "exclude"), "a") as fh:
        fh.write("/scout/\n")
    report = os.path.join(card["worktree_path"], "scout", "r", "report.md")
    os.makedirs(os.path.dirname(report))
    with open(report, "w") as fh:
        fh.write("# findings\n")
    assert _git(card["worktree_path"], "status", "--porcelain") == ""
    store.update(card["id"], {"session_id": "s1", "link_state": "live",
                              "column_name": "done"}, bump=False)
    _end(d, store, card["id"], "s1")
    await d._flush_work_records()
    await _released(d)
    assert os.path.isfile(report)
    got = store.get(card["id"])
    assert got["worktree_path"] == card["worktree_path"]
    decorated = d._decorate_card_for_snapshot(got, {got["id"]: got},
                                              active=set())
    assert decorated["worktree_note"] == worktrees.KEPT_NOTE.format(
        card["worktree_path"])


def test_crew_output_is_any_entry_git_names():
    assert not worktrees.holds_crew_output(b"")
    assert worktrees.holds_crew_output(b"!! scout/\0")
    assert worktrees.holds_crew_output(b"?? plans/p.md\0")
    assert worktrees.argv_crew_output("/p")[-5:] == [
        "--", "scout", "plans", "manual-check", "docs/research"]


@pytest.mark.asyncio
async def test_a_done_arrival_after_the_session_ended_releases_at_once(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    store.update(card["id"], {"session_id": "s1", "link_state": "ended",
                              "column_name": "in_progress"}, bump=False)
    ok, detail = await d.update_card(card["id"], {"column_name": "done"})
    await _released(d)
    assert ok is not None, detail
    assert not os.path.exists(card["worktree_path"])


@pytest.mark.asyncio
async def test_deleting_a_card_releases_its_folder_after_the_close(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    order: list = []

    async def close(before, shell_pid=None):
        order.append("close")

    monkeypatch.setattr(d, "_close_for_deleted_card", close)
    store.update(card["id"], {"link_state": "ended"}, bump=False)
    ok, detail = await d.delete_card(card["id"])
    await _released(d)
    assert ok, detail
    assert order == ["close"]
    assert not os.path.exists(card["worktree_path"])


@pytest.mark.asyncio
async def test_a_path_outside_the_worktrees_folder_is_never_removed(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    card = _make(store, repo)
    elsewhere = str(tmp_path / "elsewhere")
    os.makedirs(elsewhere)
    store.record_worktree(card["id"], elsewhere, "card/x")
    store.update(card["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    await d._maybe_release_worktree({"id": card["id"]})
    assert os.path.isdir(elsewhere)


# --- the switch on the daemon ------------------------------------------------------------

def test_the_switch_stores_only_off_and_is_replaced_never_mutated(daemon, repo):
    d, _store = daemon
    assert d._isolation_for(repo) is True
    first = d.board_isolation_overrides
    d.set_board_isolation_override(repo, False)
    assert d.board_isolation_overrides == {repo: False}
    assert first == {}, "the old dict is never written through"
    assert d._isolation_for(repo) is False
    d.set_board_isolation_override(repo, True)
    assert d.board_isolation_overrides == {}
    d.set_board_isolation_overrides({repo: False, "/x": True, "": False,
                                     "/y": "false"})
    assert d.board_isolation_overrides == {repo: False}


def test_the_board_publishes_the_overrides_and_each_card_its_state(
        daemon, repo, tmp_path):
    d, store = daemon
    plain = os.path.realpath(tmp_path)
    _make(store, repo)
    _make(store, plain, title="plain", project="other")
    d.set_board_isolation_override(repo, False)
    state = d._build_board_state()
    assert state["isolation_overrides"] == {repo: False}
    by_root = {c["root"]: c for c in state["cards"]}
    assert by_root[repo]["isolation"] == "off"
    assert by_root[plain]["isolation"] == ""


# --- the success criterion -------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_cards_of_one_project_work_in_two_folders_and_leave_two_branches(
        daemon, repo, monkeypatch):
    """Two Backlog cards of one project started side by side each get their
    own branch and their own folder, neither run's files appear in the
    other's folder or in the main checkout, and when both cards are Done the
    branches are there to merge and the folders are gone."""
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    d.set_board_parallel_override(repo, 2)
    one = _make(store, repo, title="first card")
    two = _make(store, repo, title="second card")

    ok, detail = await d.dispatch_card(one["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE
    await _settle(d)
    # The first session binds; the launch slot frees for the second card.
    store.update(one["id"], {"session_id": "s1", "link_state": "live"},
                 bump=False)
    ok, detail = await d.dispatch_card(two["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE, detail
    await _settle(d)
    store.update(two["id"], {"session_id": "s2", "link_state": "live"},
                 bump=False)

    cwds = [o["kw"]["cwd"] for o in opened]
    assert len(cwds) == 2 and cwds[0] != cwds[1]
    for cwd in cwds:
        assert worktrees.inside(repo, cwd)

    # Each run writes and commits its own work, on its own branch.
    for card, cwd in ((one, cwds[0]), (two, cwds[1])):
        name = f"{card['id'][:8]}.txt"
        with open(os.path.join(cwd, name), "w") as handle:
            handle.write("work\n")
        _git(cwd, "add", name)
        _git(cwd, "commit", "-q", "-m", f"work for {card['title']}")
    assert not os.path.exists(os.path.join(cwds[1], f"{one['id'][:8]}.txt"))
    assert not os.path.exists(os.path.join(cwds[0], f"{two['id'][:8]}.txt"))
    assert _git(repo, "status", "--porcelain") == ""

    for card in (one, two):
        store.update(card["id"], {"column_name": "done"}, bump=False)
    d._board_missing_since[one["id"]] = time.time() - 10_000
    d._board_missing_since[two["id"]] = time.time() - 10_000
    d._reconcile_board({})
    await d._flush_work_records()
    await _released(d)

    for cwd in cwds:
        assert not os.path.exists(cwd)
    branches = _git(repo, "branch", "--list", "card/*")
    assert worktrees.branch_name(one) in branches
    assert worktrees.branch_name(two) in branches
    for card in (one, two):
        log = _git(repo, "log", "--oneline", worktrees.branch_name(card))
        assert f"work for {card['title']}" in log
    assert _git(repo, "status", "--porcelain") == ""


# --- dispatch 2: the repairs ---------------------------------------------------------

def _inside_row(path, sid="s-inside"):
    return {"running": [{"session_id": sid, "provider": "claude",
                         "kind": "interactive", "project": "bob",
                         "cwd": path, "started_at": time.time()}]}


@pytest.mark.asyncio
async def test_deleting_a_live_card_whose_close_landed_releases_its_folder(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    store.update(card["id"], {"session_id": "s1", "link_state": "live"},
                 bump=False)

    async def closed(before, shell_pid=None):
        return True

    monkeypatch.setattr(d, "_close_for_deleted_card", closed)
    ok, detail = await d.delete_card(card["id"])
    await _released(d)
    assert ok, detail
    assert not os.path.exists(card["worktree_path"])
    assert card["worktree_branch"] in _git(repo, "branch", "--list", "card/*")


@pytest.mark.asyncio
async def test_a_deleted_cards_folder_waits_for_a_session_still_inside_it(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    store.update(card["id"], {"session_id": "s1", "link_state": "live"},
                 bump=False)

    async def refused(before, shell_pid=None):
        return False

    monkeypatch.setattr(d, "_close_for_deleted_card", refused)
    d._agents_snapshot_cache = _inside_row(card["worktree_path"], "s1")
    ok, _ = await d.delete_card(card["id"])
    await _released(d)
    assert ok
    assert os.path.isdir(card["worktree_path"])
    assert card["id"] in d._worktree_orphans
    await d._flush_worktree_releases()
    assert os.path.isdir(card["worktree_path"]), "still in use: still waits"
    d._agents_snapshot_cache = {}
    await d._flush_worktree_releases()
    assert not os.path.exists(card["worktree_path"])
    assert d._worktree_orphans == {}


@pytest.mark.asyncio
async def test_a_queued_card_whose_preparation_fails_is_dequeued_once(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    _trust(monkeypatch, tmp_path, repo)
    count = tmp_path / "runs.txt"
    _script(repo, f'echo run >> "{count}"\nexit 3\n')
    d.board_autostart_enabled = True
    card = _make(store, repo)
    store.update(card["id"], {"queue_state": "queued",
                              "queued_at": time.time()}, bump=False)
    for _ in range(2):              # two drain passes
        d._reconcile_board({})
        await d._flush_queue_dispatches()
        await _settle(d)
    got = store.get(card["id"])
    assert count.read_text().count("run") == 1
    assert got["queue_state"] == ""
    assert "exit 3" in got["dispatch_error"]
    assert opened == []


@pytest.mark.asyncio
async def test_expiry_then_done_keeps_the_folder_while_a_session_is_inside(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    # The bind window ran out: the link is empty, the terminal is still
    # open in the folder.
    store.update(card["id"], {"link_state": "", "column_name": "in_progress"},
                 bump=False)
    d._agents_snapshot_cache = _inside_row(card["worktree_path"])
    got, detail = await d.update_card(card["id"], {"column_name": "done"})
    assert got is not None, detail
    assert os.path.isdir(card["worktree_path"])
    decorated = d._decorate_card_for_snapshot(
        store.get(card["id"]), {}, active=set())
    assert "worktree_note" not in decorated
    # A spawn receipt Dark Army still holds keeps it too.
    d._agents_snapshot_cache = {}
    d._spawn_shell_pids[card["id"]] = 4242
    await d._flush_worktree_releases()
    assert os.path.isdir(card["worktree_path"])
    d._spawn_shell_pids.pop(card["id"])
    await d._flush_worktree_releases()
    assert not os.path.exists(card["worktree_path"])


@pytest.mark.asyncio
async def test_the_kept_note_is_derived_across_a_restart_a_run_and_a_hand_removal(
        daemon, repo, monkeypatch, tmp_path):
    import shutil
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    path = card["worktree_path"]
    with open(os.path.join(path, "unsaved.txt"), "w") as fh:
        fh.write("not committed\n")
    store.update(card["id"], {"session_id": "s1", "link_state": "ended",
                              "column_name": "done"}, bump=False)

    # A restart: a fresh daemon over the same store, nothing in memory.
    fresh = BobDaemon(sessions_path=tmp_path / "sessions2.json")
    fresh._board = store
    got = store.get(card["id"])
    kept = worktrees.KEPT_NOTE.format(path)
    assert fresh._decorate_card_for_snapshot(got, {}, active=set())[
        "worktree_note"] == kept
    fresh._reconcile_board({})
    await fresh._flush_work_records()
    await _released(fresh)
    assert os.path.isdir(path)
    assert fresh._decorate_card_for_snapshot(
        store.get(card["id"]), {}, active=set())["worktree_note"] == kept
    # Looked at once per process: the next pass queues nothing.
    fresh._reconcile_board({})
    assert fresh._worktree_release_queue == []

    # A new run of the card: no note while it is live.
    store.update(card["id"], {"column_name": "in_progress",
                              "link_state": "live"}, bump=False)
    assert "worktree_note" not in fresh._decorate_card_for_snapshot(
        store.get(card["id"]), {}, active=set())

    # The folder removed by hand: the next process clears the pair.
    store.update(card["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    shutil.rmtree(path)
    again = BobDaemon(sessions_path=tmp_path / "sessions3.json")
    again._board = store
    again._reconcile_board({})
    await again._flush_work_records()
    await _released(again)
    got = store.get(card["id"])
    assert got["worktree_path"] == ""
    assert "worktree_note" not in again._decorate_card_for_snapshot(
        got, {}, active=set())


@pytest.mark.asyncio
async def test_a_queued_replay_refused_by_an_older_window_is_dequeued(
        daemon, repo, monkeypatch):
    from dark_army_daemon import vscode_reveal
    d, store = daemon
    monkeypatch.setattr(d, "_known_project_roots", lambda: {repo})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")
    monkeypatch.setattr(vscode_reveal, "_bob_ext_locks", lambda: [{
        "port": 1, "authToken": "t", "extensionVersion": "0.1.21",
        "workspaceFolders": [repo]}])
    card = _make(store, repo)
    path = worktrees.worktree_dir(repo, card["id"])
    branch = worktrees.branch_name(card)
    _git(repo, "worktree", "add", "-q", "-b", branch, path, "main")
    store.record_worktree(card["id"], path, branch)
    store.update(card["id"], {"queue_state": "queued",
                              "queued_at": time.time()}, bump=False)
    ok, detail = await d.dispatch_card(card["id"], queued_replay=True)
    assert (ok, detail) == (False, dispatch.WORKTREE_WINDOW_REFUSAL)
    got = store.get(card["id"])
    assert got["queue_state"] == ""
    assert got["dispatch_error"] == dispatch.WORKTREE_WINDOW_REFUSAL


def _clone(tmp_path, origin):
    clone = tmp_path / "clone"
    done = subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=work_record.git_env())
    assert done.returncode == 0, done.stderr
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return os.path.realpath(clone)


@pytest.mark.asyncio
async def test_the_base_is_local_main_when_it_already_holds_origin(
        daemon, repo, tmp_path):
    d, _store = daemon
    clone = _clone(tmp_path, repo)
    assert await d._worktree_base(clone) == "main", "level: local main"
    (tmp_path / "clone" / "mine.txt").write_text("unpushed\n")
    _git(clone, "add", "mine.txt")
    _git(clone, "commit", "-q", "-m", "unpushed work")
    assert await d._worktree_base(clone) == "main", "ahead: local main"


@pytest.mark.asyncio
async def test_the_base_is_origin_when_origin_moved_ahead(
        daemon, repo, tmp_path):
    d, _store = daemon
    clone = _clone(tmp_path, repo)
    (tmp_path / "proj" / "theirs.txt").write_text("upstream\n")
    _git(repo, "add", "theirs.txt")
    _git(repo, "commit", "-q", "-m", "upstream work")
    _git(clone, "fetch", "-q", "origin")
    assert await d._worktree_base(clone) == "origin/main"
    assert await d._worktree_base(repo) == "main", "no remote: local main"


@pytest.mark.asyncio
async def test_a_branch_made_from_origin_tracks_nothing(daemon, repo, tmp_path):
    clone = _clone(tmp_path, repo)
    path = worktrees.worktree_dir(clone, "abcd1234")
    done = subprocess.run(
        worktrees.argv_worktree_add(clone, path, "card/abcd1234-x",
                                    "origin/main"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=work_record.git_env())
    assert done.returncode == 0, done.stderr
    upstream = subprocess.run(
        ["git", "-C", clone, "rev-parse", "--abbrev-ref",
         "card/abcd1234-x@{upstream}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert upstream.returncode != 0, upstream.stdout


@pytest.mark.asyncio
async def test_clear_done_releases_every_cleared_cards_folder(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    store.update(card["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    count, token, _view = store.done_tokens()
    ok, cleared, detail = await d.clear_done_cards(count, token)
    await _released(d)
    assert ok, detail
    assert not os.path.exists(card["worktree_path"])


@pytest.mark.asyncio
async def test_a_card_deleted_while_its_folder_is_prepared_leaves_no_folder(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    unmarked: list = []
    monkeypatch.setattr(trust_marks, "unmark", unmarked.append)
    gate = asyncio.Event()
    real = d._prepare_worktree

    async def slow(head, root):
        out = await real(head, root)
        await gate.wait()
        return out

    monkeypatch.setattr(d, "_prepare_worktree", slow)
    card = _make(store, repo)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE
    for _ in range(50):             # let the add land before the delete
        await asyncio.sleep(0.02)
        if os.path.isdir(worktrees.worktree_dir(repo, card["id"])):
            break
    ok, _ = await d.delete_card(card["id"])
    await _released(d)
    assert ok
    gate.set()
    await _settle(d)
    path = worktrees.worktree_dir(repo, card["id"])
    assert not os.path.exists(path)
    # The delete's own release of the unrecorded folder and the prepare
    # task's discard may both take the copies back; `unmark` is idempotent.
    assert set(unmarked) == {path}
    assert opened == []


# --- the setup script's gate -------------------------------------------------------------

async def _start_refused(d, store, repo, monkeypatch):
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    card = _make(store, repo)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE
    await _settle(d)
    assert opened == []
    got = store.get(card["id"])
    assert got["worktree_path"] == "" and got["link_state"] == ""
    return got


@pytest.mark.asyncio
async def test_an_untrusted_project_refuses_the_script_in_words(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo, claude=False)
    ran = tmp_path / "ran"
    _script(repo, f'touch "{ran}"\n')
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNTRUSTED_REFUSAL
    assert not ran.exists()
    assert not os.path.exists(worktrees.worktree_dir(repo, got["id"])), \
        "refused before any folder is made"


@pytest.mark.asyncio
async def test_a_tracked_script_is_refused(daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    script = _script(repo, f'touch "{ran}"\n')
    _git(repo, "add", "-f", worktrees.SETUP_SCRIPT)
    _git(repo, "commit", "-q", "-m", "tracked script")
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_script_others_can_write_is_refused(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    script = _script(repo, f'touch "{ran}"\n', mode=0o664)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_script_owned_by_somebody_else_is_refused(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    script = _script(repo, f'touch "{ran}"\n')
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_symlinked_script_is_refused_not_followed(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    real = tmp_path / "elsewhere.sh"
    real.write_text(f'touch "{ran}"\n')
    script = os.path.join(repo, worktrees.SETUP_SCRIPT)
    os.makedirs(os.path.dirname(script))
    os.symlink(real, script)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_link_at_the_setup_log_is_refused_and_its_target_untouched(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    _script(repo, f'touch "{ran}"\n')
    card = _make(store, repo)
    target = tmp_path / "victim.txt"
    target.write_text("keep me\n")
    os.makedirs(os.path.join(repo, worktrees.WORKTREES_DIR))
    log = worktrees.setup_log_path(repo, card["id"])
    os.symlink(target, log)
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    await d.dispatch_card(card["id"], allow_unplanned=True)
    await _settle(d)
    got = store.get(card["id"])
    assert "could not write its log" in got["dispatch_error"]
    assert target.read_text() == "keep me\n"
    assert not ran.exists()
    assert opened == []


@pytest.mark.asyncio
async def test_a_symlinked_worktrees_folder_is_refused(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    folder = os.path.join(repo, worktrees.WORKTREES_DIR)
    os.symlink(elsewhere, folder)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == \
        worktrees.WORKTREES_SYMLINK_REFUSAL.format(folder)
    assert list(elsewhere.iterdir()) == []


# --- dispatch 3: the last repairs -----------------------------------------------------

@pytest.mark.asyncio
async def test_a_folder_removed_by_hand_but_still_registered_prepares_once(
        daemon, repo, monkeypatch):
    """Registered with git but gone from disk ("prunable"): pruned, added
    again on its own branch, recorded, spawned — once, never a loop."""
    import shutil
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    card = _make(store, repo)
    path = worktrees.worktree_dir(repo, card["id"])
    branch = worktrees.branch_name(card)
    _git(repo, "worktree", "add", "-q", "-b", branch, path, "main")
    store.record_worktree(card["id"], path, branch)
    shutil.rmtree(path)
    calls = _git_calls(d, monkeypatch)

    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert detail == worktrees.PREPARING_NOTE
    await _settle(d)
    assert len(opened) == 1
    assert opened[0]["kw"]["cwd"] == path
    assert os.path.isdir(path)
    assert len([c for c in calls if c[-2:] == ["worktree", "prune"]]) == 1
    assert len(_worktree_calls(calls, "add")) == 1
    assert len(calls) < 25, calls
    assert d._worktree_preparing == {}


def test_a_preparation_pops_only_its_own_entry(daemon):
    d, _store = daemon
    d._worktree_preparing = {"c1": {"project": "bob", "token": "later"}}
    d._pop_preparing("c1", "earlier")
    assert "c1" in d._worktree_preparing
    d._pop_preparing("c1", "later")
    assert d._worktree_preparing == {}


@pytest.mark.asyncio
async def test_a_slow_release_holds_up_no_publish_done_or_delete(
        daemon, repo, monkeypatch):
    d, store = daemon
    done_card = await _prepared(d, store, repo, monkeypatch, title="done one")
    # Out of the project's launch slot, so the second card starts too.
    store.update(done_card["id"], {"link_state": "ended"}, bump=False)
    gone_card = await _prepared(d, store, repo, monkeypatch, title="gone one")
    gate = asyncio.Event()
    started: list = []

    async def slow(card, *, deleting=False):
        started.append(card.get("id"))
        await gate.wait()
        return True

    monkeypatch.setattr(d, "_maybe_release_worktree", slow)
    store.update(done_card["id"], {"session_id": "s1", "link_state": "ended",
                                   "column_name": "in_progress"}, bump=False)
    got, _ = await asyncio.wait_for(
        d.update_card(done_card["id"], {"column_name": "done"}), 2)
    assert got is not None
    store.update(gone_card["id"], {"link_state": "ended"}, bump=False)
    ok, _ = await asyncio.wait_for(d.delete_card(gone_card["id"]), 2)
    assert ok
    await asyncio.wait_for(d._flush_work_records(), 2)
    await asyncio.sleep(0)
    assert started, "the release runs, detached"
    gate.set()
    await _released(d)


@pytest.mark.asyncio
async def test_an_unrecorded_folder_goes_with_its_deleted_card(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    _trust(monkeypatch, tmp_path, repo)
    _script(repo, "exit 3\n")
    card = _make(store, repo)
    await d.dispatch_card(card["id"], allow_unplanned=True)
    await _settle(d)
    path = worktrees.worktree_dir(repo, card["id"])
    assert os.path.isdir(path) and store.get(card["id"])["worktree_path"] == ""
    ok, _ = await d.delete_card(card["id"])
    assert ok
    await _released(d)
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_an_unrecorded_folder_goes_with_clear_done(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    _trust(monkeypatch, tmp_path, repo)
    _script(repo, "exit 3\n")
    card = _make(store, repo)
    await d.dispatch_card(card["id"], allow_unplanned=True)
    await _settle(d)
    path = worktrees.worktree_dir(repo, card["id"])
    store.update(card["id"], {"column_name": "done"}, bump=False)
    count, token, _view = store.done_tokens()
    ok, _n, detail = await d.clear_done_cards(count, token)
    assert ok, detail
    await _released(d)
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_a_folder_kept_for_a_working_batch_sibling_draws_no_kept_note(
        daemon, repo, monkeypatch):
    d, store = daemon
    head = await _prepared(d, store, repo, monkeypatch, title="head")
    member = _make(store, repo, title="member")
    store.record_worktree(member["id"], head["worktree_path"],
                          head["worktree_branch"])
    for cid in (head["id"], member["id"]):
        store.update(cid, {"batch_id": "b1"}, bump=False)
    store.update(head["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    store.update(member["id"], {"column_name": "in_progress",
                                "link_state": "live", "session_id": "s1"},
                 bump=False)
    by_id = {c["id"]: c for c in store.cards()}
    decorated = d._decorate_card_for_snapshot(by_id[head["id"]], by_id,
                                              active=set())
    assert "worktree_note" not in decorated
    # Once the sibling is finished too, a folder still there is "kept".
    store.update(member["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    by_id = {c["id"]: c for c in store.cards()}
    decorated = d._decorate_card_for_snapshot(by_id[head["id"]], by_id,
                                              active=set())
    assert decorated["worktree_note"] == worktrees.KEPT_NOTE.format(
        head["worktree_path"])


@pytest.mark.asyncio
async def test_a_setup_folder_committed_as_a_link_is_refused(
        daemon, repo, monkeypatch, tmp_path):
    """A repository commits `ci/worktree-setup.sh` and `.dark-army` as a link
    to `ci/`: `ls-files` of `.dark-army/worktree-setup.sh` names nothing, and
    `lstat` of the script sees only its last part. The folder rule and the
    realpath rule refuse it."""
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    os.makedirs(os.path.join(repo, "ci"))
    with open(os.path.join(repo, "ci", "worktree-setup.sh"), "w") as fh:
        fh.write(f'touch "{ran}"\n')
    os.symlink("ci", os.path.join(repo, ".dark-army"))
    _git(repo, "add", "-f", "ci", ".dark-army")
    _git(repo, "commit", "-q", "-m", "linked setup folder")
    script = os.path.join(repo, worktrees.SETUP_SCRIPT)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_setup_folder_others_can_write_is_refused(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    script = _script(repo, f'touch "{ran}"\n')
    os.chmod(os.path.dirname(script), 0o777)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_git_that_cannot_answer_refuses_the_script(
        daemon, repo, monkeypatch, tmp_path):
    """Fail closed: a failed or timed-out `git status` is never read as
    "untracked"."""
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    script = _script(repo, f'touch "{ran}"\n')
    real = d._git_blocking

    def broken(argv, root, **kw):
        if "status" in argv:
            return False, b"", work_record.GIT_TIMEOUT_REASON
        return real(argv, root, **kw)

    monkeypatch.setattr(d, "_git_blocking", broken)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


# --- dispatch 4 -------------------------------------------------------------------------

def _case_insensitive(folder) -> bool:
    probe = os.path.join(str(folder), "CaseProbe")
    open(probe, "w").close()
    try:
        return os.path.exists(os.path.join(str(folder), "caseprobe"))
    finally:
        os.unlink(probe)


@pytest.mark.asyncio
async def test_a_case_variant_committed_script_is_refused(
        daemon, repo, monkeypatch, tmp_path):
    """`.DARK-ARMY/Worktree-Setup.sh` committed on the Mac's case-insensitive
    disk: the file rules pass (it *is* `.dark-army/worktree-setup.sh` to the
    disk), `ls-files` of the lowercase name says nothing — and `git status`
    says nothing either, which is not "untracked", so it is refused."""
    if not _case_insensitive(repo):
        pytest.skip("this disk is case-sensitive")
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    os.makedirs(os.path.join(repo, ".DARK-ARMY"))
    with open(os.path.join(repo, ".DARK-ARMY", "Worktree-Setup.sh"), "w") as fh:
        fh.write(f'touch "{ran}"\n')
    _git(repo, "add", "-f", ".DARK-ARMY/Worktree-Setup.sh")
    _git(repo, "commit", "-q", "-m", "case variant")
    script = os.path.join(repo, worktrees.SETUP_SCRIPT)
    assert os.path.isfile(script), "the disk folds the case"
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_setup_folder_that_is_a_submodule_is_refused(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    _git(sub, "config", "user.email", "t@example.com")
    _git(sub, "config", "user.name", "t")
    (sub / "worktree-setup.sh").write_text(f'touch "{ran}"\n')
    _git(sub, "add", "worktree-setup.sh")
    _git(sub, "commit", "-q", "-m", "script")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
         str(sub), ".dark-army")
    _git(repo, "commit", "-q", "-m", "setup folder as a submodule")
    script = os.path.join(repo, worktrees.SETUP_SCRIPT)
    assert os.path.isfile(script)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_setup_folder_ignored_as_enrolment_leaves_it_runs_the_script(
        daemon, repo, monkeypatch, tmp_path):
    """Enrolment writes `.dark-army/` into the project's `.gitignore`; git then
    answers `!! .dark-army/` for the folder — a positive "ignored", which it
    gives only when nothing under the folder is tracked. The script runs."""
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    _script(repo, f'touch "{ran}"\n')
    with open(os.path.join(repo, ".gitignore"), "w") as fh:
        fh.write(".dark-army/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore the key folder")
    card = _make(store, repo)
    await d.dispatch_card(card["id"], allow_unplanned=True)
    await _settle(d)
    assert ran.exists()
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_the_done_archive_draws_no_kept_note_while_a_sibling_works(
        daemon, repo, monkeypatch):
    d, store = daemon
    head = await _prepared(d, store, repo, monkeypatch, title="head")
    member = _make(store, repo, title="member")
    store.record_worktree(member["id"], head["worktree_path"],
                          head["worktree_branch"])
    for cid in (head["id"], member["id"]):
        store.update(cid, {"batch_id": "b1"}, bump=False)
    store.update(head["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    store.update(member["id"], {"column_name": "in_progress",
                                "link_state": "live", "session_id": "s1"},
                 bump=False)
    archive = d.done_archive_cards()
    row = next(c for c in archive["cards"] if c["id"] == head["id"])
    assert "worktree_note" not in row
    store.update(member["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    archive = d.done_archive_cards()
    row = next(c for c in archive["cards"] if c["id"] == head["id"])
    assert row["worktree_note"] == worktrees.KEPT_NOTE.format(
        head["worktree_path"])


@pytest.mark.asyncio
async def test_a_card_deleted_while_its_setup_runs_leaves_no_folder(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    _trust(monkeypatch, tmp_path, repo)
    _script(repo, "exit 3\n")
    gate = asyncio.Event()
    real = d._prepare_worktree

    async def slow(head, root):
        out = await real(head, root)      # the add and the failing script ran
        await gate.wait()
        return out

    monkeypatch.setattr(d, "_prepare_worktree", slow)
    card = _make(store, repo)
    await d.dispatch_card(card["id"], allow_unplanned=True)
    path = worktrees.worktree_dir(repo, card["id"])
    for _ in range(100):
        await asyncio.sleep(0.02)
        if os.path.isdir(path):
            break
    ok, _ = await d.delete_card(card["id"])
    assert ok
    # Still being prepared: the delete queues nothing for it.
    assert card["id"] not in (getattr(d, "_worktree_orphans", None) or {})
    gate.set()
    await _settle(d)
    await _released(d)
    assert not os.path.exists(path)
    assert opened == []


@pytest.mark.asyncio
async def test_a_card_restarted_mid_release_keeps_its_folder(
        daemon, repo, monkeypatch):
    """The destructive verb re-checks at the moment it fires: the card was
    dragged out of Done and started again in the gap before the remove."""
    d, store = daemon
    card = await _prepared(d, store, repo, monkeypatch)
    store.update(card["id"], {"session_id": "s1", "link_state": "ended",
                              "column_name": "done"}, bump=False)
    calls = _git_calls(d, monkeypatch)
    real_shared = d._worktree_shared

    def restart_in_the_gap(current, cards):
        store.update(card["id"], {"column_name": "in_progress",
                                  "link_state": "dispatching"}, bump=False)
        return real_shared(current, cards)

    monkeypatch.setattr(d, "_worktree_shared", restart_in_the_gap)
    await d._maybe_release_worktree({"id": card["id"]})
    assert os.path.isdir(card["worktree_path"])
    assert not _worktree_calls(calls, "remove")
    assert store.get(card["id"])["worktree_path"] == card["worktree_path"]


@pytest.mark.asyncio
async def test_a_release_queued_while_one_runs_is_taken_before_the_task_ends(
        daemon, monkeypatch):
    d, _store = daemon
    seen: list = []

    async def fake(card, *, deleting=False):
        seen.append(card["id"])
        if card["id"] == "a":
            d._worktree_release_queue = list(d._worktree_release_queue) + ["b"]
        if card["id"] == "c":
            d._defer_worktree_release({"id": "c"}, False)
            return False
        return True

    monkeypatch.setattr(d, "_maybe_release_worktree", fake)
    d._worktree_release_queue = ["a", "c"]
    await d._flush_worktree_releases()
    assert seen == ["a", "c", "b"]
    # A release it deferred waits for a later pass rather than spinning.
    assert d._worktree_release_queue == ["c"]


# --- dispatch 5: `.dark-army` must not itself be a repository -------------------------

def _git_ok(root, *args):
    return _git(root, "-c", "protocol.file.allow=always", *args)


def _setup_repo(tmp_path, name, ran):
    """A little repository whose only file is a setup script that leaves
    a mark — somebody else's script."""
    sub = tmp_path / name
    sub.mkdir()
    _git(sub, "init", "-q", "-b", "main")
    _git(sub, "config", "user.email", "t@example.com")
    _git(sub, "config", "user.name", "t")
    (sub / "worktree-setup.sh").write_text(f'touch "{ran}"\n')
    _git(sub, "add", "worktree-setup.sh")
    _git(sub, "commit", "-q", "-m", "script")
    return sub


def _status_allows(root) -> bool:
    out = subprocess.run(
        worktrees.argv_setup_status(root, worktrees.SETUP_SCRIPT),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=work_record.git_env()).stdout
    return worktrees.setup_status_allows(out)


@pytest.mark.asyncio
async def test_a_stale_submodule_left_behind_by_a_pull_is_refused(
        daemon, monkeypatch, tmp_path):
    """The upstream had `.dark-army` as a submodule, then dropped the
    gitlink and ignored the folder; the person's pull leaves the folder
    behind with its `.git` file. Git answers `!! .dark-army/` — it never
    looks inside a nested repository — so only the folder rule refuses."""
    d, store = daemon
    ran = tmp_path / "ran"
    sub = _setup_repo(tmp_path, "sub", ran)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "t")
    (upstream / "a.txt").write_text("one\n")
    _git(upstream, "add", "a.txt")
    _git_ok(upstream, "submodule", "add", "-q", str(sub), ".dark-army")
    _git(upstream, "commit", "-q", "-m", "setup folder as a submodule")
    mine = tmp_path / "mine"
    done = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "clone", "-q",
         "--recurse-submodules", str(upstream), str(mine)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=work_record.git_env())
    assert done.returncode == 0, done.stderr
    _git(mine, "config", "user.email", "t@example.com")
    _git(mine, "config", "user.name", "t")
    # The upstream drops the gitlink and ignores the folder.
    _git(upstream, "rm", "-q", "--cached", ".dark-army")
    _git(upstream, "rm", "-q", "--cached", ".gitmodules")
    (upstream / ".gitignore").write_text(".dark-army/\n")
    _git(upstream, "add", ".gitignore")
    _git(upstream, "commit", "-q", "-m", "drop the submodule")
    _git(mine, "pull", "-q", "--no-rebase", "origin", "main")
    root = os.path.realpath(mine)
    script = os.path.join(root, worktrees.SETUP_SCRIPT)
    assert os.path.isfile(script), "the pull left the folder behind"
    assert os.path.lexists(os.path.join(root, ".dark-army", ".git"))
    assert _status_allows(root), "git's half of the gate would pass it"
    _trust(monkeypatch, tmp_path, root)
    got = await _start_refused(d, store, root, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_repository_cloned_into_an_ignored_setup_folder_is_refused(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    ran = tmp_path / "ran"
    sub = _setup_repo(tmp_path, "sub", ran)
    with open(os.path.join(repo, ".gitignore"), "w") as fh:
        fh.write(".dark-army/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore the key folder")
    done = subprocess.run(["git", "clone", "-q", str(sub),
                           os.path.join(repo, ".dark-army")],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=work_record.git_env())
    assert done.returncode == 0, done.stderr
    script = os.path.join(repo, worktrees.SETUP_SCRIPT)
    assert os.path.isdir(os.path.join(repo, ".dark-army", ".git"))
    assert _status_allows(repo), "git's half of the gate would pass it"
    _trust(monkeypatch, tmp_path, repo)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


def test_the_run_time_check_refuses_a_nested_repository_too(repo, daemon):
    """`_setup_script_file_check` is the run-time re-check as well: a `.git`
    appearing after the Start gate is still refused before the script runs."""
    d, _store = daemon
    script = _script(repo, "exit 0\n")
    assert d._setup_script_file_check(repo) == ""
    with open(os.path.join(os.path.dirname(script), ".git"), "w") as fh:
        fh.write("gitdir: ../elsewhere\n")
    assert d._setup_script_file_check(repo) == \
        worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert d._run_worktree_setup(repo, repo, "abcd1234", "card/x") == \
        worktrees.SETUP_UNSAFE_REFUSAL.format(script)


# --- dispatch 6: the script is compared with the index by inode ------------------------

KELVIN_FOLDER = ".darK-army"          # U+212A KELVIN SIGN


def _upstream_with_script(tmp_path, folder, ran, *, ignore=True):
    """An upstream that commits a setup script under `folder` (a spelling
    of `.dark-army`) and, like an enrolled project, ignores `.dark-army/`."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    _git(up, "config", "user.email", "t@example.com")
    _git(up, "config", "user.name", "t")
    (up / "a.txt").write_text("one\n")
    if ignore:
        (up / ".gitignore").write_text(".dark-army/\n")
    _git(up, "add", "-A")
    _git(up, "commit", "-q", "-m", "start")
    return up


def _commit_script(up, folder, ran):
    os.makedirs(os.path.join(up, folder), exist_ok=True)
    with open(os.path.join(up, folder, "worktree-setup.sh"), "w") as fh:
        fh.write(f'touch "{ran}"\n')
    _git(up, "add", "-f", f"{folder}/worktree-setup.sh")
    _git(up, "commit", "-q", "-m", "somebody else's setup script")


def _clone_into(src, dest):
    done = subprocess.run(["git", "clone", "-q", str(src), str(dest)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=work_record.git_env())
    assert done.returncode == 0, done.stderr
    _git(dest, "config", "user.email", "t@example.com")
    _git(dest, "config", "user.name", "t")
    return os.path.realpath(dest)


def _enrol(root):
    """What enrolment leaves: a private `.dark-army/` holding the key."""
    folder = os.path.join(root, ".dark-army")
    os.makedirs(folder, exist_ok=True)
    os.chmod(folder, 0o700)
    with open(os.path.join(folder, "key"), "w") as fh:
        fh.write("k\n")


@pytest.mark.asyncio
async def test_a_kelvin_spelled_script_pulled_into_an_enrolled_project_is_refused(
        daemon, monkeypatch, tmp_path):
    d, store = daemon
    ran = tmp_path / "ran"
    up = _upstream_with_script(tmp_path, KELVIN_FOLDER, ran)
    root = _clone_into(up, tmp_path / "mine")
    if not _case_insensitive(root):
        pytest.skip("this disk is case-sensitive")
    _enrol(root)
    _commit_script(up, KELVIN_FOLDER, ran)
    _git(root, "pull", "-q", "--no-rebase", "origin", "main")
    script = os.path.join(root, worktrees.SETUP_SCRIPT)
    assert os.path.isfile(script), "the pull landed in the person's folder"
    assert _status_allows(root), "git's status half would pass it"
    assert d._setup_script_file_check(root) == "", "the file rules pass it"
    _trust(monkeypatch, tmp_path, root)
    got = await _start_refused(d, store, root, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_a_kelvin_spelled_script_in_a_fresh_clone_then_enrolled_is_refused(
        daemon, monkeypatch, tmp_path):
    d, store = daemon
    ran = tmp_path / "ran"
    up = _upstream_with_script(tmp_path, KELVIN_FOLDER, ran)
    _commit_script(up, KELVIN_FOLDER, ran)
    root = _clone_into(up, tmp_path / "mine")
    if not _case_insensitive(root):
        pytest.skip("this disk is case-sensitive")
    _enrol(root)
    script = os.path.join(root, worktrees.SETUP_SCRIPT)
    assert os.path.isfile(script)
    assert _status_allows(root)
    _trust(monkeypatch, tmp_path, root)
    got = await _start_refused(d, store, root, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()
    # And the run-time look refuses it on its own, before the script runs.
    assert d._run_worktree_setup(root, root, "abcd1234", "card/x") == \
        worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_an_ascii_variant_with_ignorecase_off_is_refused(
        daemon, monkeypatch, tmp_path):
    d, store = daemon
    ran = tmp_path / "ran"
    up = _upstream_with_script(tmp_path, ".DARK-ARMY", ran)
    _commit_script(up, ".DARK-ARMY", ran)
    root = _clone_into(up, tmp_path / "mine")
    if not _case_insensitive(root):
        pytest.skip("this disk is case-sensitive")
    _git(root, "config", "core.ignorecase", "false")
    _enrol(root)
    script = os.path.join(root, worktrees.SETUP_SCRIPT)
    assert os.path.isfile(script)
    _trust(monkeypatch, tmp_path, root)
    got = await _start_refused(d, store, root, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


@pytest.mark.asyncio
async def test_an_untracked_script_in_an_enrolled_project_still_runs(
        daemon, monkeypatch, tmp_path):
    """The person's own script, in the private folder enrolment made, in a
    project whose other dot-files are tracked: allowed, and it runs."""
    d, store = daemon
    ran = tmp_path / "ran"
    up = _upstream_with_script(tmp_path, KELVIN_FOLDER, ran)
    root = _clone_into(up, tmp_path / "mine")
    _enrol(root)
    with open(os.path.join(root, worktrees.SETUP_SCRIPT), "w") as fh:
        fh.write(f'touch "{ran}"\n')
    opened: list = []
    _stub(d, monkeypatch, root, opened)
    _trust(monkeypatch, tmp_path, root)
    card = _make(store, root)
    await d.dispatch_card(card["id"], allow_unplanned=True)
    await _settle(d)
    assert ran.exists()
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_an_index_listing_that_fails_refuses_the_script(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    script = _script(repo, f'touch "{ran}"\n')
    real = d._git_blocking

    def broken(argv, root, **kw):
        if "ls-files" in argv:
            return False, b"", work_record.GIT_TIMEOUT_REASON
        return real(argv, root, **kw)

    monkeypatch.setattr(d, "_git_blocking", broken)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)
    assert not ran.exists()


# --- dispatch 7: APFS alone, and the hidden-file listing's own cap ----------------------

def _disk(monkeypatch, kinds):
    """Stand in for other disks: `kinds` maps a path to its disk kind; any
    other path answers APFS."""
    seen: list = []

    def fake(path):
        seen.append(str(path))
        return kinds.get(str(path), "apfs")

    monkeypatch.setattr(daemon_board, "setup_volume_type", fake)
    return seen


def test_this_disk_reads_as_apfs_and_a_missing_path_reads_as_nothing(tmp_path):
    assert daemon_board.setup_volume_type(str(tmp_path)) == "apfs"
    assert daemon_board.setup_volume_type(str(tmp_path / "gone")) == ""


def test_an_apfs_project_keeps_the_allowed_path(repo, daemon, monkeypatch):
    d, _store = daemon
    _script(repo, "exit 0\n")
    seen = _disk(monkeypatch, {})
    assert d._setup_script_file_check(repo) == ""
    assert repo in seen
    assert os.path.join(repo, ".dark-army") in seen


def test_no_script_reads_no_disk_kind(repo, daemon, monkeypatch):
    d, _store = daemon
    seen = _disk(monkeypatch, {repo: "hfs"})
    assert d._setup_script_file_check(repo) is None
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hfs", ""])
async def test_a_script_on_a_disk_that_is_not_apfs_is_refused(
        daemon, repo, monkeypatch, tmp_path, kind):
    """Mac OS Extended, and a disk whose kind cannot be read: refused in
    words at the Start, and again by the run-time look, never run."""
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    script = _script(repo, f'touch "{ran}"\n')
    _disk(monkeypatch, {repo: kind})
    words = worktrees.SETUP_VOLUME_REFUSAL.format(script)
    got = await _start_refused(d, store, repo, monkeypatch)
    assert got["dispatch_error"] == words
    assert d._run_worktree_setup(repo, repo, "abcd1234", "card/x") == words
    assert not ran.exists()


def test_a_setup_folder_on_another_disk_is_refused(repo, daemon, monkeypatch):
    d, _store = daemon
    script = _script(repo, "exit 0\n")
    _disk(monkeypatch, {os.path.join(repo, ".dark-army"): "hfs"})
    assert d._setup_script_file_check(repo) == \
        worktrees.SETUP_VOLUME_REFUSAL.format(script)


def _track_many_hidden(root, count: int) -> int:
    """`count` index entries under `.yarn/cache/` sharing one blob, written
    straight into the index (no files on disk), committed. Returns the size
    of `git ls-files -z -- '.*'`."""
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=b"x\n",
        stdout=subprocess.PIPE, env=work_record.git_env(),
        check=True).stdout.decode().strip()
    lines = "".join(
        f"100644 {blob}\t.yarn/cache/package-number-{i:06d}-"
        f"{'x' * 60}.zip\n" for i in range(count))
    subprocess.run(["git", "update-index", "--index-info"], cwd=str(root),
                   input=lines.encode(), env=work_record.git_env(), check=True)
    _git(root, "commit", "-q", "-m", "a yarn cache")
    return len(subprocess.run(
        worktrees.argv_index_dotfiles(root), stdout=subprocess.PIPE,
        env=work_record.git_env(), check=True).stdout)


@pytest.mark.asyncio
async def test_a_yarn_cache_of_hidden_files_does_not_refuse_the_script(
        daemon, repo, monkeypatch, tmp_path):
    """Thousands of tracked hidden files pass the shared git cap; the
    listing's own cap reads them, and the person's script runs."""
    d, store = daemon
    size = _track_many_hidden(repo, 3500)
    assert size > work_record.MAX_GIT_OUTPUT_BYTES
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran"
    script = _script(repo, f'touch "{ran}"\n')
    assert d._setup_script_git_refusal(repo) == "", script
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    card = _make(store, repo)
    await d.dispatch_card(card["id"], allow_unplanned=True)
    await _settle(d)
    assert ran.exists()
    assert len(opened) == 1


def test_a_listing_over_its_own_cap_says_there_are_too_many_hidden_files(
        daemon, repo, monkeypatch):
    d, _store = daemon
    size = _track_many_hidden(repo, 50)
    monkeypatch.setattr(worktrees, "MAX_INDEX_DOTFILES_BYTES", size - 1)
    script = _script(repo, "exit 0\n")
    assert d._setup_script_git_refusal(repo) == \
        worktrees.SETUP_TOO_MANY_HIDDEN_REFUSAL.format(script)



# --- the pack copies (`docs/card-worktrees.md`, *The pack copies*) ----------------------

SETTINGS = ".claude/settings.json"
SKILL = ".claude/skills/shunt/SKILL.md"
LEAD = ".claude/leads/x.md"


def _write(root, rel, text):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as handle:
        handle.write(text)


def _pack_repo(repo, tmp_path, monkeypatch, *, ledger=True):
    """The main checkout holds an uncommitted pack update: settings changed,
    a new skill, a lead removed — and an own change to `a.txt`."""
    from dark_army_daemon import paths
    from dark_army_menubar import pack_ledger
    monkeypatch.setattr(paths, "AGENT_PACK_PATH", tmp_path / "agent-pack.json")
    _write(repo, SETTINGS, '{"old": true}\n')
    _write(repo, LEAD, "lead\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pack")
    if ledger:
        pack_ledger.remember(repo, profile="web")
    _write(repo, SETTINGS, '{"new": true}\n')
    _write(repo, SKILL, "skill\n")
    os.unlink(os.path.join(repo, LEAD))
    _write(repo, "a.txt", "mine\n")


def _read(root, rel):
    with open(os.path.join(root, rel)) as handle:
        return handle.read()


async def _done(d, store, card):
    store.update(card["id"], {"session_id": "s1", "link_state": "live",
                              "column_name": "done"}, bump=False)
    _end(d, store, card["id"], "s1")
    await d._flush_work_records()
    await _released(d)


@pytest.mark.asyncio
async def test_an_uncommitted_pack_update_lands_in_the_worktree_and_stays_off_the_branch(
        daemon, repo, monkeypatch, tmp_path):
    import json
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    before = _git(repo, "status", "--porcelain", "--untracked-files=all")
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    assert _read(wt, SETTINGS) == '{"new": true}\n'
    assert _read(wt, SKILL) == "skill\n"
    assert not os.path.exists(os.path.join(wt, LEAD))
    assert _read(wt, "a.txt") == "one\n"
    assert _git(wt, "status", "--porcelain", "--untracked-files=all") == ""
    flags = {line[2:].strip(): line[0]
             for line in _git(wt, "ls-files", "-v").splitlines()}
    assert flags[SETTINGS] == flags[SKILL] == flags[LEAD] == "S"
    _write(wt, "agent.txt", "work\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "work")
    changed = _git(repo, "diff", "--name-only",
                   f"main..{card['worktree_branch']}").split()
    assert changed == ["agent.txt"]
    assert _git(repo, "status", "--porcelain",
                "--untracked-files=all") == before
    manifest = worktrees.parse_manifest(open(
        worktrees.pack_manifest_path(repo, card["id"])).read())
    assert {r["path"]: r["kind"] for r in manifest} == {
        SETTINGS: "tracked", SKILL: "new", LEAD: "deleted"}
    json.loads(open(worktrees.pack_manifest_path(repo, card["id"])).read())


@pytest.mark.asyncio
async def test_a_project_without_a_pack_ledger_row_carries_no_pack_files(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch, ledger=False)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    assert _read(wt, SETTINGS) == '{"old": true}\n'
    assert not os.path.exists(os.path.join(wt, SKILL))
    assert os.path.exists(os.path.join(wt, LEAD))
    assert not os.path.exists(worktrees.pack_manifest_path(repo, card["id"]))


@pytest.mark.asyncio
async def test_untouched_pack_copies_of_all_three_kinds_let_the_folder_go(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    await _done(d, store, card)
    assert not os.path.exists(card["worktree_path"])
    assert not os.path.exists(worktrees.pack_manifest_path(repo, card["id"]))
    assert card["worktree_branch"] in _git(repo, "branch", "--list", "card/*")


@pytest.mark.asyncio
async def test_an_edited_pack_copy_keeps_the_folder_and_the_card_says_so(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    _write(wt, SETTINGS, '{"agent": true}\n')
    _write(wt, SKILL, "agent skill\n")
    await _done(d, store, card)
    assert os.path.isdir(wt)
    got = store.get(card["id"])
    decorated = d._decorate_card_for_snapshot(got, {got["id"]: got},
                                              active=set())
    assert decorated["worktree_note"] == worktrees.KEPT_NOTE.format(wt)
    status = _git(wt, "status", "--porcelain")
    assert SETTINGS in status and SKILL in status
    assert _read(wt, SETTINGS) == '{"agent": true}\n'


@pytest.mark.asyncio
async def test_a_second_start_refreshes_an_untouched_pack_copy_and_spares_an_edited_one(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    _write(repo, SETTINGS, '{"newest": true}\n')
    _write(repo, SKILL, "main skill v2\n")
    _write(wt, SKILL, "agent edit\n")
    updated, _detail = await d.reset_card(
        card["id"], {"column_name": "backlog"})
    assert updated is not None
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok and detail != worktrees.PREPARING_NOTE, detail
    assert _read(wt, SETTINGS) == '{"newest": true}\n'
    assert _read(wt, SKILL) == "agent edit\n"


@pytest.mark.asyncio
async def test_a_failed_pack_carry_still_opens_the_terminal_and_logs_one_line(
        daemon, repo, monkeypatch, tmp_path, caplog):
    import logging
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    real = d._git_blocking

    def fail_status(argv, root, **kw):
        if "status" in argv and "--no-renames" in argv:
            return False, b"", "failed"
        return real(argv, root, **kw)

    monkeypatch.setattr(d, "_git_blocking", fail_status)
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    card = _make(store, repo)
    with caplog.at_level(logging.INFO):
        ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
        await _settle(d)
    assert opened and worktrees.inside(repo, opened[0]["kw"]["cwd"])
    assert not store.get(card["id"]).get("dispatch_error")
    assert any("could not read the uncommitted pack update" in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_a_pack_folder_committed_as_a_link_is_never_written_through(
        daemon, repo, monkeypatch, tmp_path):
    import shutil
    from dark_army_daemon import paths
    from dark_army_menubar import pack_ledger
    monkeypatch.setattr(paths, "AGENT_PACK_PATH", tmp_path / "agent-pack.json")
    outside = tmp_path / "outside"
    outside.mkdir()
    # The branch commits `.claude/skills` as a link out of the folder ...
    os.makedirs(os.path.join(repo, ".claude"))
    os.symlink(str(outside), os.path.join(repo, ".claude", "skills"))
    _git(repo, "add", "-f", ".claude/skills")
    _git(repo, "commit", "-q", "-m", "link")
    pack_ledger.remember(repo, profile="web")
    # ... while the main checkout holds a real folder with a new skill.
    os.unlink(os.path.join(repo, ".claude", "skills"))
    _write(repo, SKILL, "skill\n")
    d, store = daemon
    opened: list = []
    _stub(d, monkeypatch, repo, opened)
    card = _make(store, repo)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    await _settle(d)
    assert opened
    assert os.listdir(outside) == []
    shutil.rmtree(os.path.join(repo, ".claude"))


@pytest.mark.asyncio
async def test_the_pack_carry_only_reads_the_main_checkout(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    calls: list = []
    real = d._git_blocking

    def spy(argv, root, **kw):
        calls.append((list(argv), os.path.realpath(str(root))))
        return real(argv, root, **kw)

    monkeypatch.setattr(d, "_git_blocking", spy)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = os.path.realpath(card["worktree_path"])
    first = next(i for i, (argv, _r) in enumerate(calls)
                 if "--no-renames" in argv)
    main = [argv for argv, root in calls[first:]
            if root == os.path.realpath(repo)]
    assert main and all(
        "status" in argv or ("worktree" in argv and "list" in argv)
        for argv in main)
    writes = [(argv, root) for argv, root in calls
              if "update-index" in argv or "-N" in argv]
    assert writes and all(root == wt for _argv, root in writes)


@pytest.mark.asyncio
async def test_a_reused_folders_uncommitted_edit_to_an_uncarried_pack_path_survives(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    # The agent changed a pack path no carry recorded; main now changes it too.
    _write(repo, ".gitignore", "main\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    _write(wt, "CLAUDE.md", "agent wrote this\n")
    _write(repo, "CLAUDE.md", "main wrote this\n")
    await d.reset_card(card["id"], {"column_name": "backlog"})
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert _read(wt, "CLAUDE.md") == "agent wrote this\n"
    assert "CLAUDE.md" in _git(wt, "status", "--porcelain",
                               "--untracked-files=all")


@pytest.mark.asyncio
async def test_a_missing_pack_manifest_keeps_a_folder_holding_marked_copies(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    _write(wt, SETTINGS, '{"agent": true}\n')
    os.unlink(worktrees.pack_manifest_path(repo, card["id"]))
    await _done(d, store, card)
    assert os.path.isdir(wt)
    assert _read(wt, SETTINGS) == '{"agent": true}\n'


@pytest.mark.asyncio
async def test_a_failed_pack_unmark_keeps_the_folder(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    _write(wt, SETTINGS, '{"agent": true}\n')
    real = d._git_blocking

    def fail_unmark(argv, root, **kw):
        if "--no-skip-worktree" in argv:
            return False, b"", "failed"
        return real(argv, root, **kw)

    monkeypatch.setattr(d, "_git_blocking", fail_unmark)
    await _done(d, store, card)
    assert os.path.isdir(wt)
    assert _read(wt, SETTINGS) == '{"agent": true}\n'


@pytest.mark.asyncio
async def test_a_hard_link_at_a_carried_pack_path_is_never_written_through(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    outside = tmp_path / "outside.txt"
    outside.write_text("precious\n")
    dest = os.path.join(wt, SETTINGS)
    os.unlink(dest)
    os.link(str(outside), dest)
    _write(repo, SETTINGS, '{"newest": true}\n')
    # The link's bytes differ from the manifest, so refresh must not touch it
    # at all; drop the manifest row's guard by matching the digest.
    import hashlib
    with open(worktrees.pack_manifest_path(repo, card["id"])) as handle:
        rows = worktrees.parse_manifest(handle.read())
    for row in rows:
        if row["path"] == SETTINGS:
            row["sha256"] = hashlib.sha256(b"precious\n").hexdigest()
    with open(worktrees.pack_manifest_path(repo, card["id"]), "w") as handle:
        handle.write(worktrees.manifest_text(rows))
    await d.reset_card(card["id"], {"column_name": "backlog"})
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert outside.read_text() == "precious\n"
    assert _read(wt, SETTINGS) == '{"newest": true}\n'


@pytest.mark.asyncio
async def test_a_failed_pack_intent_to_add_still_marks_the_tracked_copies(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    real = d._git_blocking

    def fail_add(argv, root, **kw):
        if "add" in argv and "-N" in argv:
            return False, b"", "failed"
        return real(argv, root, **kw)

    monkeypatch.setattr(d, "_git_blocking", fail_add)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    assert _git(wt, "status", "--porcelain", "--untracked-files=all") == ""
    assert not os.path.exists(os.path.join(wt, SKILL))
    flags = {line[2:].strip(): line[0]
             for line in _git(wt, "ls-files", "-v").splitlines()}
    assert flags[SETTINGS] == "S"


@pytest.mark.asyncio
async def test_a_failed_pack_mark_puts_tracked_copies_back_to_head(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    real = d._git_blocking

    def fail_mark(argv, root, **kw):
        if "--skip-worktree" in argv:
            return False, b"", "failed"
        return real(argv, root, **kw)

    monkeypatch.setattr(d, "_git_blocking", fail_mark)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    assert _read(wt, SETTINGS) == '{"old": true}\n'
    assert os.path.exists(os.path.join(wt, LEAD))
    assert not os.path.exists(os.path.join(wt, SKILL))
    assert _git(wt, "status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.asyncio
async def test_a_second_cards_start_in_a_shared_folder_keys_the_pack_manifest_by_the_folder(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    first = await _prepared(d, store, repo, monkeypatch)
    wt = first["worktree_path"]
    second = _make(store, repo, title="second card")
    store.record_worktree(second["id"], wt, first["worktree_branch"])
    await d.reset_card(first["id"], {"column_name": "backlog"})
    _write(wt, SKILL, "agent edit\n")
    _write(repo, SKILL, "main skill v2\n")
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(second["id"], allow_unplanned=True)
    assert ok, detail
    assert _read(wt, SKILL) == "agent edit\n"
    assert not os.path.exists(worktrees.pack_manifest_path(repo, second["id"]))
    assert os.path.exists(worktrees.pack_manifest_path(repo, first["id"]))


@pytest.mark.asyncio
async def test_a_folder_removed_by_hand_is_carried_fresh_not_by_its_stale_manifest(
        daemon, repo, monkeypatch, tmp_path):
    import shutil
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    assert os.path.exists(worktrees.pack_manifest_path(repo, card["id"]))
    shutil.rmtree(wt)
    await d.reset_card(card["id"], {"column_name": "backlog"})
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    await _settle(d)
    wt = store.get(card["id"])["worktree_path"]
    assert os.path.isdir(wt)
    assert _read(wt, SETTINGS) == '{"new": true}\n'
    assert os.path.exists(os.path.join(wt, SKILL))
    assert not os.path.exists(os.path.join(wt, LEAD))
    assert _git(wt, "status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.asyncio
async def test_a_lost_pack_manifest_leaves_marked_copies_alone_on_a_second_start(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    _write(wt, SETTINGS, '{"agent": true}\n')
    os.unlink(worktrees.pack_manifest_path(repo, card["id"]))
    await d.reset_card(card["id"], {"column_name": "backlog"})
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert _read(wt, SETTINGS) == '{"agent": true}\n'


@pytest.mark.asyncio
async def test_a_failed_pack_mark_restores_a_tracked_file_larger_than_the_default_cap(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    big = "x" * 600_000 + "\n"
    _write(repo, ".gitignore", big)
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "big")
    _write(repo, ".gitignore", big + "more\n")
    real = d._git_blocking

    def fail_mark(argv, root, **kw):
        if "--skip-worktree" in argv:
            return False, b"", "failed"
        return real(argv, root, **kw)

    monkeypatch.setattr(d, "_git_blocking", fail_mark)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    assert _read(wt, ".gitignore") == big
    assert _git(wt, "status", "--porcelain", "--untracked-files=all") == ""


@pytest.mark.asyncio
async def test_a_stale_pack_temp_file_is_swept_on_the_next_carry(
        daemon, repo, monkeypatch, tmp_path):
    d, store = daemon
    _pack_repo(repo, tmp_path, monkeypatch)
    card = await _prepared(d, store, repo, monkeypatch)
    wt = card["worktree_path"]
    stale = os.path.join(wt, ".claude", ".dark-army-pack-abc123.tmp")
    _write(wt, ".claude/.dark-army-pack-abc123.tmp", "left over\n")
    _write(repo, SETTINGS, '{"v3": true}\n')
    await d.reset_card(card["id"], {"column_name": "backlog"})
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert not os.path.exists(stale)


# --- the stale-registration sweep ---------------------------------------------------

def _enrolled_roots_are(monkeypatch, *roots):
    monkeypatch.setattr(daemon_board.enrollment, "enrolled_roots",
                        lambda: set(roots))


async def _pruned(d):
    """Kick the release task the sweep rides on and wait for it."""
    await d._kick_worktree_releases()
    task = getattr(d, "_worktree_release_task", None)
    if task is not None:
        await task


def _detached(repo, tmp_path, name):
    path = str(tmp_path / name)
    _git(repo, "worktree", "add", "--detach", path, "HEAD")
    return os.path.realpath(path)


def _listed(repo):
    return worktrees.parse_worktree_list(_git(repo, "worktree", "list",
                                              "--porcelain"))


@pytest.mark.asyncio
async def test_prune_forgets_a_dead_registration_and_keeps_the_living(
        daemon, repo, tmp_path, monkeypatch):
    import shutil
    d, _store = daemon
    dead = _detached(repo, tmp_path, "dead")
    live = _detached(repo, tmp_path, "live")
    card = worktrees.worktree_dir(repo, "abcd1234")
    _git(repo, "worktree", "add", "-b", "card/abcd1234-x", card, "main")
    shutil.rmtree(dead)
    _enrolled_roots_are(monkeypatch, repo)
    calls = _git_calls(d, monkeypatch)
    d._reconcile_board({})
    assert d._worktree_prune_queue == [repo]
    await _pruned(d)
    assert [c[-2:] for c in _worktree_calls(calls, "list")] == [
        ["list", "--porcelain"]]
    assert len(_worktree_calls(calls, "prune")) == 1
    listed = _listed(repo)
    assert dead not in listed
    assert live in listed and os.path.realpath(card) in listed
    assert os.path.isdir(live) and os.path.isdir(card)


@pytest.mark.asyncio
async def test_prune_runs_once_per_root_per_process(
        daemon, repo, tmp_path, monkeypatch):
    import shutil
    d, _store = daemon
    shutil.rmtree(_detached(repo, tmp_path, "dead"))
    _enrolled_roots_are(monkeypatch, repo)
    calls = _git_calls(d, monkeypatch)
    d._reconcile_board({})
    await _pruned(d)
    before = len(_worktree_calls(calls, "list")) \
        + len(_worktree_calls(calls, "prune"))
    assert before == 2
    d._reconcile_board({})
    await _pruned(d)
    assert len(_worktree_calls(calls, "list")) \
        + len(_worktree_calls(calls, "prune")) == before


@pytest.mark.asyncio
async def test_prune_makes_no_git_call_for_a_non_git_root(
        daemon, tmp_path, monkeypatch):
    d, _store = daemon
    plain = tmp_path / "plain"
    plain.mkdir()
    _enrolled_roots_are(monkeypatch, str(plain))
    calls = _git_calls(d, monkeypatch)
    d._reconcile_board({})
    await _pruned(d)
    assert calls == []
    assert not getattr(d, "_worktree_prune_queue", None)


@pytest.mark.asyncio
async def test_prune_with_nothing_dead_does_not_prune(
        daemon, repo, tmp_path, monkeypatch):
    d, _store = daemon
    live = _detached(repo, tmp_path, "live")
    _enrolled_roots_are(monkeypatch, repo)
    calls = _git_calls(d, monkeypatch)
    d._reconcile_board({})
    await _pruned(d)
    assert _worktree_calls(calls, "prune") == []
    assert live in _listed(repo)


@pytest.mark.asyncio
async def test_prune_skips_a_root_whose_present_folder_lost_its_marker(
        daemon, repo, tmp_path, monkeypatch, caplog):
    import logging
    import shutil
    d, _store = daemon
    hollow = _detached(repo, tmp_path, "hollow")
    os.remove(os.path.join(hollow, ".git"))
    shutil.rmtree(_detached(repo, tmp_path, "dead"))
    _enrolled_roots_are(monkeypatch, repo)
    calls = _git_calls(d, monkeypatch)
    with caplog.at_level(logging.INFO, logger=daemon_board.logger.name):
        d._reconcile_board({})
        await _pruned(d)
    assert _worktree_calls(calls, "prune") == []
    lines = [r for r in caplog.records if hollow in r.getMessage()]
    assert len(lines) == 1
    assert os.path.isdir(hollow)


@pytest.mark.asyncio
async def test_prune_waits_while_a_card_is_being_prepared_in_the_root(
        daemon, repo, tmp_path, monkeypatch):
    import shutil
    d, _store = daemon
    shutil.rmtree(_detached(repo, tmp_path, "dead"))
    _enrolled_roots_are(monkeypatch, repo)
    calls = _git_calls(d, monkeypatch)
    d._worktree_preparing = {"abcd1234": {"root": repo, "token": "t"}}
    d._reconcile_board({})
    await _pruned(d)
    assert calls == []
    assert d._worktree_prune_queue == [repo]
    d._worktree_preparing = {}
    await _pruned(d)
    assert len(_worktree_calls(calls, "prune")) == 1
    assert not d._worktree_prune_queue
