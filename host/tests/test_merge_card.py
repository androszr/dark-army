"""Review and merge on a Done card, through the daemon.

`test_dispatch_worktree.py`'s seams: a real `BoardStore` on a temp file,
`dispatch.spawn` stubbed to record where a helper would open and what it was
told, `_known_project_roots` monkeypatched, and a real git repository under
`tmp_path`, so every `git` call Dark Army makes is a real one. Each merge case
starts from a Done card with a real worktree and real commits, presses
`merge_card` and awaits the detached task (`docs/card-worktrees.md`, *Review
and merge*).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time

import pytest

from dark_army_daemon import (daemon_board, dispatch, enrollment, merges,
                              trust_marks, work_record, worktrees)
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

STEPS = "1. Open the menu bar.\nWhy not automated: a real screen."


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


def _git(root, *args, check=True) -> str:
    done = subprocess.run(["git", *args], cwd=str(root), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=merges.merge_env())
    if check:
        assert done.returncode == 0, done.stderr
    return done.stdout.decode()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    (root / "b.txt").write_text("keep\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    real = os.path.realpath(root)
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: {real})
    return real


def _trust(monkeypatch, tmp_path, root, *, claude=True):
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"projects": {
        str(root): {"hasTrustDialogAccepted": bool(claude)}}}))
    monkeypatch.setattr(trust_marks, "CLAUDE_CONFIG_PATH", path)
    monkeypatch.setattr(trust_marks, "CODEX_CONFIG_PATH",
                        tmp_path / "no-codex.toml")


def _check_script(root, body: str, mode: int = 0o644) -> str:
    script = os.path.join(root, merges.MERGE_CHECK_SCRIPT)
    os.makedirs(os.path.dirname(script), exist_ok=True)
    with open(script, "w") as handle:
        handle.write(body)
    os.chmod(script, mode)
    return script


def _make(store, root, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": str(root),
              "prompt": "go", "tool": "claude", "column_name": "done"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    time.sleep(0.002)
    return card


def _check_text(status="open") -> str:
    return ("# The strip fits\n\n- **Card:** Fit the strip\n"
            "- **Project:** proj\n- **Check:** the strip fits\n"
            "- **Created:** 2026-09-25T14:32:00+02:00\n"
            f"- **Status:** {status}\n- **Outcome:** none\n"
            "- **Checked at:** none\n\n## Steps\n\n1. Open the menu bar.\n\n"
            "## Why not automated\n\nA real screen.\n")


async def _settle_checks(d, store, card_id, root, *, outcome="passed",
                         folder="2026-10-03-x"):
    """Flag a hand-check on the card and record its outcome through the
    daemon's own verb, on a real file."""
    target = os.path.join(root, "manual-check", folder)
    os.makedirs(target, exist_ok=True)
    path = os.path.realpath(os.path.join(target, "check.md"))
    with open(path, "w") as handle:
        handle.write(_check_text())
    store.update(card_id, {"column_name": "in_progress"}, bump=False)
    store.bind_session(card_id, "s1")
    got, detail = store.flag_manual(card_id, "s1", STEPS, path)
    assert got is not None, detail
    # The run is over: a Done card whose assistant has gone.
    store.update(card_id, {"column_name": "done", "link_state": "ended"},
                 bump=False)
    ok, detail = await d.record_manual_outcome(path, outcome, "")
    assert ok, detail
    return path


async def _done_card(d, store, repo, title="do the thing", *, file="c.txt",
                     text="card\n", tool="claude", check=True):
    """A Done card whose branch has one commit in its own real folder."""
    card = _make(store, repo, title=title, tool=tool)
    path, branch, error = await d._prepare_worktree(card, repo)
    assert not error, error
    store.record_worktree(card["id"], path, branch)
    with open(os.path.join(path, file), "w") as handle:
        handle.write(text)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", f"work on {title}")
    if check:
        await _settle_checks(d, store, card["id"], repo,
                             folder=f"2026-10-03-{card['id'][:8]}")
    return store.get(card["id"])


async def _merge(d, card_id, **kw):
    ok, detail = await d.merge_card(card_id, **kw)
    if ok:
        await _settle(d)
    return ok, detail


async def _settle(d):
    for _ in range(20):
        tasks = [t for t in list(d._merge_tasks) if not t.done()]
        tasks += [t for t in (getattr(d, "_worktree_tasks", None) or set())
                  if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _stub_spawn(d, monkeypatch, root, opened):
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(root)})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: f"/bin/{tool}")

    async def accept(spawn_root, argv, name, **kw):
        opened.append({"root": spawn_root, "argv": list(argv), "name": name,
                       "kw": dict(kw)})
        return True, "opened", None

    monkeypatch.setattr(dispatch, "spawn", accept)


def _git_calls(d, monkeypatch):
    calls: list = []
    real = d._run_git

    async def spy(argv, root, **kw):
        calls.append(list(argv))
        return await real(argv, root, **kw)

    monkeypatch.setattr(d, "_run_git", spy)
    return calls


def _id8(card):
    return worktrees.short_id(card["id"])


def _inside_row(path, sid="s-inside"):
    return {"running": [{"session_id": sid, "provider": "claude",
                         "kind": "interactive", "project": "bob",
                         "cwd": path, "started_at": time.time()}]}


def _rev(root, ref="main"):
    return _git(root, "rev-parse", ref).strip()


# --- the success criterion ---------------------------------------------------------


@pytest.mark.asyncio
async def test_two_done_cards_land_one_at_a_time_over_a_dirty_root(
        daemon, repo, monkeypatch):
    d, store = daemon
    first = await _done_card(d, store, repo, "first card", file="c1.txt")
    second = await _done_card(d, store, repo, "second card", file="c2.txt")
    # Main moves meanwhile, the root is dirty in a file the branches do not
    # touch and holds one untracked file.
    with open(os.path.join(repo, "main.txt"), "w") as handle:
        handle.write("moved\n")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-q", "-m", "main moved")
    old_main = _rev(repo)
    with open(os.path.join(repo, "b.txt"), "w") as handle:
        handle.write("unsaved edit\n")
    with open(os.path.join(repo, "loose.txt"), "w") as handle:
        handle.write("never added\n")
    branch_tip = _rev(repo, first["worktree_branch"])

    ok, detail = await d.merge_card(first["id"])
    assert (ok, detail) == (True, merges.MERGING_NOTE)
    assert first["id"] in d._merging
    await _settle(d)

    subject = f"Merge card/{_id8(first)}: first card"
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() == subject
    assert _git(repo, "rev-list", "--parents", "-1", "main").split()[1:] \
        == [old_main, branch_tip]
    assert open(os.path.join(repo, "b.txt")).read() == "unsaved edit\n"
    assert open(os.path.join(repo, "loose.txt")).read() == "never added\n"
    assert open(os.path.join(repo, "c1.txt")).read() == "card\n"
    assert first["worktree_branch"] not in _git(repo, "branch", "--list")
    assert not os.path.exists(first["worktree_path"])
    got = store.get(first["id"])
    assert got["merge_state"] == "merged"
    assert got["merge_note"].startswith("Merged into main as ")
    assert got["worktree_branch"] == "" and got["worktree_path"] == ""
    assert first["id"] not in d._merging
    # No check script: the card says no checks ran.
    assert merges.MERGED_UNCHECKED_NOTE in got["merge_note"]

    # The second card keeps its branch and no state, and is still offered.
    other = store.get(second["id"])
    assert other["worktree_branch"] == second["worktree_branch"]
    assert other["merge_state"] == ""
    refusal, _r, _b, _p = d._merge_gate_sync(other)
    assert refusal == ""
    ok, _ = await _merge(d, second["id"])
    assert ok
    assert store.get(second["id"])["merge_state"] == "merged"
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() \
        == f"Merge card/{_id8(second)}: second card"
    assert open(os.path.join(repo, "b.txt")).read() == "unsaved edit\n"


@pytest.mark.asyncio
async def test_the_gate_reads_the_check_file(daemon, repo, monkeypatch):
    d, store = daemon
    failed = await _done_card(d, store, repo, "failed", file="f.txt",
                              check=False)
    await _settle_checks(d, store, failed["id"], repo, outcome="failed",
                         folder="2026-10-03-failed")
    ok, detail = await d.merge_card(failed["id"])
    assert (ok, detail) == (False, merges.MANUAL_FAILED_REFUSAL)
    # The Changes page says the same thing, in the same words.
    page = d._card_changes_sync(failed["id"])
    assert page["merge_offered"] is False
    assert page["merge_refusal"] == merges.MANUAL_FAILED_REFUSAL

    # An open check: the steps are set, nothing recorded.
    openc = await _done_card(d, store, repo, "open", file="o.txt", check=False)
    store.update(openc["id"], {"column_name": "in_progress"}, bump=False)
    store.bind_session(openc["id"], "s2")
    path = os.path.join(repo, "manual-check", "2026-10-03-open", "check.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(_check_text())
    got, detail = store.flag_manual(openc["id"], "s2", STEPS,
                                    os.path.realpath(path))
    assert got is not None, detail
    store.update(openc["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    ok, detail = await d.merge_card(openc["id"])
    assert (ok, detail) == (False, merges.MANUAL_OPEN_REFUSAL)
    assert d._card_changes_sync(openc["id"])["merge_offered"] is False

    # Marked checked (no outcome written): settled, and it merges.
    ok, detail = await d.clear_manual_check(openc["id"])
    assert ok, detail
    ok, detail = await _merge(d, openc["id"])
    assert (ok, detail) == (True, merges.MERGING_NOTE)
    assert store.get(openc["id"])["merge_state"] == "merged"
    # The failed card was never touched.
    assert store.get(failed["id"])["merge_state"] == ""
    assert failed["worktree_branch"] in _git(repo, "branch", "--list")


@pytest.mark.asyncio
async def test_the_gate_refuses_in_order(daemon, repo, monkeypatch):
    d, store = daemon
    # Not in Done.
    card = _make(store, repo, column_name="backlog")
    assert (await d.merge_card(card["id"]))[1] == merges.NOT_DONE_REFUSAL
    # Done with no branch on record.
    card = _make(store, repo)
    assert (await d.merge_card(card["id"]))[1] == merges.NO_BRANCH_REFUSAL
    assert (await d.merge_card("gone"))[1] == merges.NO_CARD_REFUSAL
    # Already merged.
    store.record_worktree(card["id"], "", "card/zzz-x")
    store.record_merge(card["id"], "merged", "Merged")
    assert (await d.merge_card(card["id"]))[1] == merges.ALREADY_MERGED_REFUSAL
    # Not enrolled / not a checkout.
    store.record_merge(card["id"], "", "")
    monkeypatch.setattr(enrollment, "root_enrolled", lambda cwd: "")
    assert (await d.merge_card(card["id"]))[1] == merges.NOT_ENROLLED_REFUSAL
    monkeypatch.setattr(enrollment, "root_enrolled", lambda cwd: str(cwd))
    plain = os.path.join(os.path.dirname(repo), "plain")
    os.makedirs(plain)
    other = _make(store, plain)
    store.record_worktree(other["id"], "", "card/yyy-x")
    assert (await d.merge_card(other["id"]))[1] == merges.NOT_A_CHECKOUT_REFUSAL


# --- the root and the trunk ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_root_dirty_in_a_file_the_branch_changes_is_refused(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo, file="a.txt", text="branch\n")
    old = _rev(repo)
    with open(os.path.join(repo, "a.txt"), "w") as handle:
        handle.write("unsaved\n")
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert "a.txt" in got["merge_note"]
    assert got["merge_note"].startswith("The merge would change files you")
    assert _rev(repo) == old
    assert open(os.path.join(repo, "a.txt")).read() == "unsaved\n"
    # The folder is back on its branch and clean; the branch is still there.
    assert _git(got["worktree_path"], "rev-parse", "--abbrev-ref",
                "HEAD").strip() == card["worktree_branch"]
    assert _git(got["worktree_path"], "status", "--porcelain") == ""
    assert card["worktree_branch"] in _git(repo, "branch", "--list")


@pytest.mark.asyncio
async def test_a_root_on_a_feature_branch_moves_only_the_pointer(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    with open(os.path.join(repo, "a.txt"), "w") as handle:
        handle.write("dirty on feature\n")
    old_main = _rev(repo, "main")
    feature = _rev(repo, "feature")
    ok, _ = await _merge(d, card["id"])
    assert ok
    assert _rev(repo, "main") != old_main
    assert _git(repo, "log", "-1", "--format=%s", "main").strip().startswith(
        f"Merge card/{_id8(card)}")
    # The main checkout is byte-identical: same branch, same file, same HEAD.
    assert _rev(repo, "feature") == feature
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature"
    assert open(os.path.join(repo, "a.txt")).read() == "dirty on feature\n"
    assert not os.path.exists(os.path.join(repo, "c.txt"))
    got = store.get(card["id"])
    assert got["merge_state"] == "merged"
    # `branch -d` would refuse here (the checked-out HEAD is not main); the
    # ref is deleted by `update-ref -d`, guarded by the tip that was merged.
    assert merges.BRANCH_KEPT_SUFFIX not in got["merge_note"]
    assert got["worktree_branch"] == ""
    assert card["worktree_branch"] not in _git(repo, "branch", "--list")


@pytest.mark.asyncio
async def test_main_checked_out_in_another_folder_is_refused(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    elsewhere = str(tmp_path / "elsewhere")
    _git(repo, "worktree", "add", "-q", elsewhere, "main")
    old = _rev(repo, "main")
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert "main is checked out in another folder" in got["merge_note"]
    assert _rev(repo, "main") == old


@pytest.mark.asyncio
async def test_a_main_checkout_mid_merge_is_refused(daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    old = _rev(repo)
    git_dir = os.path.join(repo, ".git")
    with open(os.path.join(git_dir, "MERGE_HEAD"), "w") as handle:
        handle.write(old + "\n")
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == merges.ROOT_BUSY_REFUSAL
    assert _rev(repo) == old


@pytest.mark.asyncio
async def test_main_moving_during_the_checks_is_refused(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    _check_script(repo, 'git -C "$DARK_ARMY_ROOT" commit -q --allow-empty '
                        '-m "main moved during the checks"\n')
    card = await _done_card(d, store, repo)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == merges.TRUNK_MOVED_REFUSAL.format("main")
    assert _git(repo, "log", "-1", "--format=%s", "main").strip() \
        == "main moved during the checks"
    # Nothing was moved onto main and the folder is back on the branch.
    assert _git(got["worktree_path"], "rev-parse", "--abbrev-ref",
                "HEAD").strip() == card["worktree_branch"]
    # A second press rebuilds the merge on the new tip and lands it.
    _check_script(repo, "exit 0\n")
    ok, _ = await _merge(d, card["id"])
    assert ok
    assert store.get(card["id"])["merge_state"] == "merged"


# --- conflict, Fix and the third press ----------------------------------------------


@pytest.mark.asyncio
async def test_conflict_offers_fix_in_the_card_folder(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card = await _done_card(d, store, repo, file="a.txt", text="branch\n",
                            tool="codex")
    with open(os.path.join(repo, "a.txt"), "w") as handle:
        handle.write("main edit\n")
    _git(repo, "commit", "-q", "-am", "main edit")
    old_main = _rev(repo)
    root_status = _git(repo, "status", "--porcelain")

    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "conflict"
    assert "a.txt" in got["merge_note"] and "main" in got["merge_note"]
    path = got["worktree_path"]
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() \
        == card["worktree_branch"]
    assert _git(path, "status", "--porcelain") == ""
    assert _rev(repo) == old_main
    assert _git(repo, "status", "--porcelain") == root_status
    # The card's line names the next step.
    snap = d._decorate_card_for_snapshot(d._trim_card_for_snapshot(got), {})
    assert snap["merge_state"] == "conflict"
    assert snap["merge_line"].endswith(merges.PRESS_AGAIN_SUFFIX)
    assert "merge_note" not in snap and "review_tip" not in snap
    assert "worktree_note" not in snap

    # Fix: one spawn, in the card's folder, the card's own tool.
    ok, detail = await d.fix_merge_card(card["id"])
    assert ok, detail
    assert len(opened) == 1
    spawn = opened[0]
    assert spawn["kw"]["cwd"] == path
    assert os.sep + ".worktrees" + os.sep in path
    assert spawn["kw"]["stamp"].startswith("card-merge-fix|")
    assert spawn["argv"][0] == "/bin/codex" and "--" in spawn["argv"]
    prompt = spawn["argv"][-1]
    assert prompt.startswith(dispatch.MERGE_FIX_PREAMBLE.strip())
    for needle in (card["worktree_branch"], path, "main", "a.txt"):
        assert needle in prompt
    assert spawn["name"].startswith("fix: ")
    # An assistant at work in the folder: the line says so.
    snap = d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(card["id"])), {})
    assert snap["merge_line"].endswith(merges.HELPER_AT_WORK_SUFFIX)
    # MERGE is refused while it is there.
    ok, detail = await d.merge_card(card["id"])
    assert (ok, detail) == (False, merges.SESSION_INSIDE_REFUSAL)
    # A second Fix press hits the helper ladder, not a second spawn.
    ok, detail = await d.fix_merge_card(card["id"])
    assert not ok and len(opened) == 1

    # The helper's work: bring main into the branch, resolve, commit.
    _git(path, "merge", "main", check=False)
    with open(os.path.join(path, "a.txt"), "w") as handle:
        handle.write("resolved\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-q", "-m", "resolve")
    d._merge_helpers = {}
    snap = d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(card["id"])), {})
    assert snap["merge_line"].endswith(merges.PRESS_AGAIN_SUFFIX)
    ok, _ = await _merge(d, card["id"])
    assert ok
    assert store.get(card["id"])["merge_state"] == "merged"
    parents = _git(repo, "rev-list", "--parents", "-1", "main").split()[1:]
    assert parents[0] == old_main
    assert open(os.path.join(repo, "a.txt")).read() == "resolved\n"


@pytest.mark.asyncio
async def test_a_session_inside_the_folder_reads_as_at_work(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    store.record_merge(card["id"], "conflict", "Merge needs you: stopped.")
    path = store.get(card["id"])["worktree_path"]
    d._agents_snapshot_cache = _inside_row(path)
    got = store.get(card["id"])
    assert d._merge_line(got).endswith(merges.HELPER_AT_WORK_SUFFIX)
    d._agents_snapshot_cache = {}
    assert d._merge_line(got).endswith(merges.PRESS_AGAIN_SUFFIX)


@pytest.mark.asyncio
async def test_fix_is_refused_without_a_stopped_merge_and_when_dispatch_is_off(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card = await _done_card(d, store, repo)
    ok, detail = await d.fix_merge_card(card["id"])
    assert (ok, detail) == (False, merges.FIX_NOT_NEEDED_REFUSAL)
    store.record_merge(card["id"], "conflict", "Merge needs you: x.")
    d.board_dispatch_enabled = False
    ok, detail = await d.fix_merge_card(card["id"])
    assert (ok, detail) == (False, merges.DISPATCH_OFF_REFUSAL)
    assert opened == []
    d.board_dispatch_enabled = True
    store.record_merge(card["id"], "blocked", "words")
    assert (await d.fix_merge_card(card["id"]))[1] \
        == merges.FIX_NOT_NEEDED_REFUSAL


# --- the checks ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_red_check_script_offers_fix(daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    _trust(monkeypatch, tmp_path, repo)
    _check_script(repo, 'echo "checks ran in $DARK_ARMY_WORKTREE"\nexit 3\n')
    card = await _done_card(d, store, repo)
    old_main = _rev(repo)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "checks_failed"
    log = merges.merge_log_path(repo, card["id"])
    assert "exit 3" in got["merge_note"] and log in got["merge_note"]
    assert "checks ran in" in open(log).read()
    assert _rev(repo) == old_main
    assert _git(got["worktree_path"], "rev-parse", "--abbrev-ref",
                "HEAD").strip() == card["worktree_branch"]
    # Fix opens the card's tool in the folder, naming the log.
    ok, detail = await d.fix_merge_card(card["id"])
    assert ok, detail
    assert opened[0]["kw"]["cwd"] == got["worktree_path"]
    assert opened[0]["kw"]["stamp"].startswith("card-merge-fix|")
    assert log in opened[0]["argv"][-1]
    # The repaired check lets the merge through.
    d._merge_helpers = {}
    _check_script(repo, "exit 0\n")
    ok, _ = await _merge(d, card["id"])
    assert ok
    done = store.get(card["id"])
    assert done["merge_state"] == "merged"
    assert merges.MERGED_UNCHECKED_NOTE not in done["merge_note"]


@pytest.mark.asyncio
async def test_a_passing_check_merges_and_runs_in_the_card_folder(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    ran = tmp_path / "ran.txt"
    _check_script(repo, f'pwd > "{ran}"\ngit log -1 --format=%s >> "{ran}"\n')
    card = await _done_card(d, store, repo, "checked card")
    ok, _ = await _merge(d, card["id"])
    assert ok
    assert store.get(card["id"])["merge_state"] == "merged"
    lines = ran.read_text().splitlines()
    assert lines[0].endswith(f"card-{_id8(card)}")
    # The checks ran at the merge commit.
    assert lines[1] == f"Merge card/{_id8(card)}: checked card"


@pytest.mark.asyncio
async def test_an_untrusted_project_refuses_the_check_script(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    _trust(monkeypatch, tmp_path, repo, claude=False)
    _check_script(repo, "exit 0\n")
    old = _rev(repo)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == worktrees.SETUP_UNTRUSTED_REFUSAL
    assert _rev(repo) == old
    assert _git(got["worktree_path"], "rev-parse", "--abbrev-ref",
                "HEAD").strip() == card["worktree_branch"]


@pytest.mark.asyncio
async def test_a_group_writable_check_script_is_refused(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    _trust(monkeypatch, tmp_path, repo)
    script = _check_script(repo, "exit 0\n", mode=0o664)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == worktrees.SETUP_UNSAFE_REFUSAL.format(script)


# --- tips, duplicates, recreation, concurrency ---------------------------------------


@pytest.mark.asyncio
async def test_expected_tip_guards_the_press_when_present(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    tip = _rev(repo, card["worktree_branch"])
    stale = "0" * 40
    ok, detail = await d.merge_card(card["id"], expected_tip=stale)
    assert (ok, detail) == (False, merges.TIP_CHANGED_REFUSAL)
    ok, detail = await _merge(d, card["id"], expected_tip=tip)
    assert (ok, detail) == (True, merges.MERGING_NOTE)
    assert store.get(card["id"])["merge_state"] == "merged"
    # Absent means no guard.
    other = await _done_card(d, store, repo, "other", file="o.txt")
    ok, _ = await _merge(d, other["id"])
    assert ok and store.get(other["id"])["merge_state"] == "merged"


@pytest.mark.asyncio
async def test_a_branch_already_in_main_is_cleaned_up_without_a_new_commit(
        daemon, repo, monkeypatch):
    d, store = daemon
    first = await _done_card(d, store, repo, "first", file="c1.txt")
    # A second card shares the first card's commits: its branch is the
    # first's tip and the first lands first.
    second = _make(store, repo, title="second")
    path, branch, error = await d._prepare_worktree(second, repo)
    assert not error
    store.record_worktree(second["id"], path, branch)
    _git(path, "merge", "-q", "--ff-only", first["worktree_branch"])
    await _settle_checks(d, store, second["id"], repo, folder="2026-10-03-dup")
    ok, _ = await _merge(d, first["id"])
    assert ok and store.get(first["id"])["merge_state"] == "merged"
    count = int(_git(repo, "rev-list", "--count", "main").strip())
    ok, _ = await _merge(d, second["id"])
    assert ok
    got = store.get(second["id"])
    assert got["merge_state"] == "merged"
    assert got["merge_note"].startswith(merges.ALREADY_MERGED_NOTE)
    assert int(_git(repo, "rev-list", "--count", "main").strip()) == count
    assert branch not in _git(repo, "branch", "--list")
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_a_released_folder_is_recreated_for_the_merge(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    # The release: folder gone, the branch name kept on the card.
    await d._maybe_release_worktree({"id": card["id"]})
    got = store.get(card["id"])
    assert got["worktree_path"] == "" and not os.path.exists(path)
    assert got["worktree_branch"] == card["worktree_branch"]
    calls = _git_calls(d, monkeypatch)
    ok, _ = await _merge(d, card["id"])
    assert ok
    adds = [c for c in calls if "worktree" in c and "add" in c]
    assert adds and adds[0][-2:] == [path, card["worktree_branch"]]
    assert store.get(card["id"])["merge_state"] == "merged"
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_an_uncommitted_file_in_the_folder_is_refused(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    with open(os.path.join(card["worktree_path"], "stray.txt"), "w") as fh:
        fh.write("unsaved\n")
    old = _rev(repo)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == merges.DIRTY_FOLDER_REFUSAL
    assert _rev(repo) == old


@pytest.mark.asyncio
async def test_a_second_press_and_a_second_card_of_the_project_wait(
        daemon, repo, monkeypatch):
    d, store = daemon
    first = await _done_card(d, store, repo, "one", file="c1.txt")
    second = await _done_card(d, store, repo, "two", file="c2.txt")
    ok, _ = await d.merge_card(first["id"])
    assert ok
    ok, detail = await d.merge_card(first["id"])
    assert (ok, detail) == (False, merges.MERGE_RUNNING_REFUSAL)
    ok, detail = await d.merge_card(second["id"])
    assert (ok, detail) == (False, merges.PROJECT_MERGING_REFUSAL)
    assert d._card_changes_sync(first["id"])["merge_refusal"] \
        == merges.MERGE_RUNNING_REFUSAL
    # The snapshot says so while it runs.
    snap = d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(first["id"])), {})
    assert snap["merge_state"] == "merging"
    assert snap["merge_line"] == merges.MERGING_NOTE
    await _settle(d)
    assert d._merging == {}
    ok, _ = await _merge(d, second["id"])
    assert ok


@pytest.mark.asyncio
async def test_two_presses_at_once_start_one_merge(daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    results = await asyncio.gather(d.merge_card(card["id"]),
                                   d.merge_card(card["id"]))
    assert sorted(ok for ok, _ in results) == [False, True]
    await _settle(d)
    assert store.get(card["id"])["merge_state"] == "merged"


@pytest.mark.asyncio
async def test_a_project_on_another_root_merges_meanwhile(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    d._merging = {"someone": {"root": "/elsewhere", "since": time.time(),
                              "token": "t"}}
    ok, _ = await _merge(d, card["id"])
    assert ok
    d._merging = {}
    assert store.get(card["id"])["merge_state"] == "merged"


@pytest.mark.asyncio
async def test_busy_rungs_refuse_in_words(daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    # A live session inside the folder.
    d._agents_snapshot_cache = _inside_row(path)
    assert (await d.merge_card(card["id"]))[1] == merges.SESSION_INSIDE_REFUSAL
    d._agents_snapshot_cache = {}
    # A release queued.
    d._worktree_release_queue = [card["id"]]
    assert (await d.merge_card(card["id"]))[1] == merges.RELEASE_PENDING_REFUSAL
    d._worktree_release_queue = []
    # A review running.
    d._consults[card["id"]] = {"purpose": "review", "session_id": "",
                               "started": time.time()}
    assert (await d.merge_card(card["id"]))[1] == merges.REVIEW_RUNNING_REFUSAL
    d._consults.pop(card["id"])
    # A live link.
    store.update(card["id"], {"link_state": "live", "session_id": "s7"},
                 bump=False)
    assert (await d.merge_card(card["id"]))[1] == merges.CARD_LIVE_REFUSAL
    store.update(card["id"], {"link_state": "ended"}, bump=False)
    # The changes page leaves the busy rungs out.
    d._agents_snapshot_cache = _inside_row(path)
    assert d._card_changes_sync(card["id"])["merge_offered"] is True


@pytest.mark.asyncio
async def test_a_release_while_merging_is_deferred(daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    d._merging = {card["id"]: {"root": repo, "since": time.time(),
                               "token": "t"}}
    assert await d._maybe_release_worktree({"id": card["id"]}) is False
    assert os.path.isdir(card["worktree_path"])
    assert card["id"] in d._worktree_release_queue
    d._merging = {}
    d._worktree_release_queue = []
    assert await d._maybe_release_worktree({"id": card["id"]}) is True
    assert not os.path.exists(card["worktree_path"])
    # The branch stays on the card.
    assert store.get(card["id"])["worktree_branch"] == card["worktree_branch"]


@pytest.mark.asyncio
async def test_a_card_dragged_out_of_done_during_the_checks_is_not_merged(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    card = await _done_card(d, store, repo)
    old = _rev(repo)
    # The script reopens the card, standing in for a hand on the board.
    def reopen(*_a, **_k):
        store.update(card["id"], {"column_name": "backlog"}, bump=False)
        return "", "", "log"
    monkeypatch.setattr(d, "_exec_setup_script", reopen)
    _check_script(repo, "exit 0\n")
    ok, _ = await _merge(d, card["id"])
    assert ok
    assert _rev(repo) == old
    got = store.get(card["id"])
    assert got["column_name"] == "backlog"
    assert got["merge_state"] == ""  # leaving Done emptied the pair


# --- the branch backfill ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_backfill_records_an_existing_branch_once(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = _make(store, repo, title="Old finished card")
    branch = worktrees.branch_name(card)
    _git(repo, "branch", branch)
    wt = os.path.join(repo, ".worktrees", "tmp-old")
    _git(repo, "worktree", "add", "-q", wt, branch)
    with open(os.path.join(wt, "old.txt"), "w") as fh:
        fh.write("old\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "old work")
    _git(repo, "worktree", "remove", wt)
    contained = _make(store, repo, title="Merged by hand")
    cbranch = worktrees.branch_name(contained)
    _git(repo, "branch", cbranch)
    absent = _make(store, repo, title="Never had one")
    calls = _git_calls(d, monkeypatch)

    d._reconcile_board({})
    d._reconcile_board({})
    assert len(d._branch_backfill_queue) == 3
    await d._flush_branch_backfill()
    await d._branch_backfill_task
    got = store.get(card["id"])
    assert got["worktree_branch"] == branch and got["worktree_path"] == ""
    assert got["merge_state"] == ""
    done = store.get(contained["id"])
    assert done["merge_state"] == "merged"
    assert done["merge_note"] == merges.ALREADY_MERGED_NOTE
    none = store.get(absent["id"])
    assert none["worktree_branch"] == "" and none["merge_state"] == ""
    asked = [c for c in calls if "--verify" in c and absent["id"][:8] in c[-1]]
    assert len(asked) == 1
    # Once per process: a later pass queues nothing.
    d._reconcile_board({})
    assert d._branch_backfill_queue == []
    # And a backfilled card is offered MERGE (its check settled).
    await _settle_checks(d, store, card["id"], repo, folder="2026-10-03-old")
    assert d._merge_gate_sync(store.get(card["id"]))[0] == ""


# --- Run review ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_review_spawns_claude_in_the_folder_and_records_the_verdict(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card = await _done_card(d, store, repo, tool="codex")
    tip = _rev(repo, card["worktree_branch"])
    ok, detail = await d.run_card_review(card["id"])
    assert ok, detail
    assert len(opened) == 1
    spawn = opened[0]
    assert spawn["kw"]["cwd"] == card["worktree_path"]
    assert spawn["kw"]["stamp"].startswith("card-review|")
    assert spawn["argv"][0] == "/bin/claude"
    prompt = spawn["argv"][-1]
    assert prompt.startswith(dispatch.REVIEW_PREAMBLE.strip())
    assert f"/review {card['worktree_branch']}" in prompt
    assert dispatch.CONSULT_ANSWER_TOOL in prompt
    entry = d._consults[card["id"]]
    assert entry["purpose"] == "review" and entry["tip"] == tip
    assert d._review_running(card["id"])
    snap = d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(card["id"])), {})
    assert snap["review_running"] is True
    # A second press and a merge wait for it.
    assert (await d.run_card_review(card["id"]))[1] \
        == merges.REVIEW_RUNNING_REFUSAL
    assert (await d.merge_card(card["id"]))[1] == merges.REVIEW_RUNNING_REFUSAL
    assert len(opened) == 1

    # The helper answers; the first line is the verdict.
    d._consults[card["id"]]["session_id"] = "rev-1"
    got, detail = await d.answer_card_by_session(
        "rev-1", "VERDICT: STOP — it deletes the data\n\nDetails.")
    assert detail == "answered"
    row = store.get(card["id"])
    assert row["review_verdict"] == "stop" and row["review_tip"] == tip
    assert card["id"] not in d._consults
    # The full review is on the card's thread, as the consultant's answer.
    thread = store.messages(card["id"])
    assert thread and thread[-1]["text"].startswith("VERDICT: STOP")
    assert thread[-1]["via"] == "consultant"
    snap = d._decorate_card_for_snapshot(d._trim_card_for_snapshot(row), {})
    assert snap["review_verdict"] == "stop"
    assert "review_running" not in snap and "review_tip" not in snap
    # The version judged ages with a later commit.
    page = d._card_changes_sync(card["id"])
    assert page["review"] == {"verdict": "stop", "tip": tip[:8],
                              "current": True}
    with open(os.path.join(card["worktree_path"], "later.txt"), "w") as fh:
        fh.write("later\n")
    _git(card["worktree_path"], "add", ".")
    _git(card["worktree_path"], "commit", "-q", "-m", "later")
    assert d._card_changes_sync(card["id"])["review"]["current"] is False


@pytest.mark.asyncio
async def test_an_answer_with_no_verdict_line_leaves_the_columns_empty(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card = await _done_card(d, store, repo)
    ok, _ = await d.run_card_review(card["id"])
    assert ok
    d._consults[card["id"]]["session_id"] = "rev-2"
    await d.answer_card_by_session("rev-2", "Looks fine to me.\nVERDICT: SHIP")
    row = store.get(card["id"])
    assert row["review_verdict"] == "" and row["review_tip"] == ""
    assert card["id"] not in d._consults


@pytest.mark.asyncio
async def test_run_review_is_gated_like_merge(daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    failed = await _done_card(d, store, repo, check=False)
    await _settle_checks(d, store, failed["id"], repo, outcome="failed",
                         folder="2026-10-03-bad")
    assert (await d.run_card_review(failed["id"]))[1] \
        == merges.MANUAL_FAILED_REFUSAL
    backlog = _make(store, repo, column_name="backlog")
    assert (await d.run_card_review(backlog["id"]))[1] \
        == merges.NOT_DONE_REFUSAL
    ok_card = await _done_card(d, store, repo, "fine", file="z.txt")
    d.board_dispatch_enabled = False
    assert (await d.run_card_review(ok_card["id"]))[1] \
        == merges.DISPATCH_OFF_REFUSAL
    assert opened == []


# --- leaving Done and the snapshot -----------------------------------------------------


@pytest.mark.asyncio
async def test_dragged_out_of_done_empties_the_merge_pair_and_keeps_the_review(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    store.record_merge(card["id"], "blocked", "words")
    store.record_review_verdict(card["id"], "ship", "a" * 40)
    store.update(card["id"], {"column_name": "backlog"})
    got = store.get(card["id"])
    assert got["merge_state"] == "" and got["merge_note"] == ""
    assert got["review_verdict"] == "ship"


@pytest.mark.asyncio
async def test_a_plain_card_publishes_none_of_the_four_keys(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = _make(store, repo, column_name="backlog")
    snap = d._decorate_card_for_snapshot(d._trim_card_for_snapshot(card), {})
    for key in ("merge_state", "merge_line", "review_verdict",
                "review_running", "merge_note", "review_tip"):
        assert key not in snap, key


def test_the_writable_markers_are_published(daemon):
    d, _store = daemon
    writable = d._pipeline_writable()
    for key in ("card_changes_supported", "merge_writable",
                "review_run_writable"):
        assert writable[key] is True


def test_the_helper_guard_is_consult_guards_generalisation():
    card = {"id": "c1", "project": "p", "root": "/tmp"}
    kw = dict(roots=["/tmp"], in_flight=[{"id": "merge-fix:c1", "project": "p"}],
              now=time.time())
    ok, detail = dispatch.helper_guard(card, key="merge-fix", tool="codex",
                                       prompt="You are fixing.", **kw)
    assert not ok and detail == "a helper is already looking at this card"
    # The same entry under another key does not block this one.
    ok, detail = dispatch.helper_guard(card, key="consult", tool="claude",
                                       prompt="You are asking.", **kw)
    assert not ok and detail == dispatch.PROJECT_BUSY_REFUSAL
    ok, detail = dispatch.helper_guard(card, key="consult", tool="claude",
                                       prompt="-leading dash", roots=["/tmp"],
                                       in_flight=[], now=time.time())
    assert not ok and "cannot start with '-'" in detail
    ok, detail = dispatch.consult_guard(card, roots=["/tmp"], in_flight=[],
                                        now=time.time(), question="why?")
    assert ok, detail


def test_the_two_prompts_lead_with_their_preambles_and_never_a_dash():
    card = {"title": "-sneaky title", "id": "c1"}
    fix = dispatch.merge_fix_prompt(card, branch="card/x", worktree="/w",
                                    trunk="main", state="checks_failed",
                                    detail="note", log="/w.log")
    assert fix.startswith(dispatch.MERGE_FIX_PREAMBLE.strip())
    assert not fix.startswith("-") and "/w.log" in fix
    assert "## Merge ready" in fix and "never push" in fix
    review = dispatch.review_prompt(card, branch="card/x", trunk="main",
                                    worktree="/w")
    assert review.startswith(dispatch.REVIEW_PREAMBLE.strip())
    assert "VERDICT: SHIP" in review and "VERDICT: STOP" in review
    assert dispatch.prompt_refusal("claude", fix) is None
    assert dispatch.prompt_refusal("codex", review) is None


def test_every_git_call_in_the_daemon_goes_through_the_three_pure_modules():
    root = os.path.dirname(os.path.dirname(daemon_board.__file__))
    pkg = os.path.join(root, "dark_army_daemon")
    allowed = {"work_record.py", "worktrees.py", "merges.py"}
    offenders = []
    for name in sorted(os.listdir(pkg)):
        if not name.endswith(".py") or name in allowed:
            continue
        with open(os.path.join(pkg, name)) as handle:
            for number, line in enumerate(handle, 1):
                if '"git"' in line:
                    offenders.append(f"{name}:{number}")
    assert offenders == []
    assert work_record is not None


# --- the audit's gaps (dispatch 2) --------------------------------------------------


def _write(root, rel, text):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as handle:
        handle.write(text)
    return full


def _carry(d, monkeypatch, repo, path):
    """The real carry (`_sync_pack_copies`): the main checkout's uncommitted
    pack update copied into the folder, marked and recorded."""
    from dark_army_menubar import pack_ledger
    monkeypatch.setattr(pack_ledger, "entry", lambda root: {"files": {}})
    note = d._sync_pack_copies(repo, path, d._pack_card_id(path))
    assert note == "", note
    return worktrees.pack_manifest_path(repo, d._pack_card_id(path))


def _tags(path, rel):
    return _git(path, "ls-files", "-t", "--", rel).split()[0:1]


NEW = ".claude/agents/zz-new.md"
TRACKED = ".claude/agents/zz-tracked.md"


@pytest.mark.asyncio
async def test_a_carried_new_pack_file_does_not_stop_the_merge(
        daemon, repo, monkeypatch):
    """A folder that carried a new agent-pack file holds it as an
    intent-to-add + skip-worktree entry and git refuses to merge over one
    (`Entry … not uptodate`, exit 128). The merge releases every carried
    file first and the carry comes back by the Start routine."""
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _write(repo, NEW, "carried\n")
    manifest = _carry(d, monkeypatch, repo, path)
    assert os.path.exists(manifest)
    assert _git(path, "status", "--porcelain") == ""
    assert _tags(path, NEW) == ["S"]
    _write(repo, "main.txt", "moved\n")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-q", "-m", "main moved")
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "merged", got["merge_note"]
    assert NEW not in _git(repo, "ls-tree", "-r", "--name-only", "main")
    assert not os.path.exists(path)
    # The main checkout's own copy is untouched.
    assert open(os.path.join(repo, NEW)).read() == "carried\n"


@pytest.mark.asyncio
async def test_the_carry_is_applied_again_after_a_stop(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    _check_script(repo, "exit 3\n")
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _write(repo, NEW, "carried\n")
    _carry(d, monkeypatch, repo, path)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "checks_failed", got["merge_note"]
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() \
        == card["worktree_branch"]
    assert _git(path, "status", "--porcelain") == ""
    assert open(os.path.join(path, NEW)).read() == "carried\n"
    assert _tags(path, NEW) == ["S"]


@pytest.mark.asyncio
async def test_a_carried_tracked_file_the_trunk_changed_does_not_stop_the_merge(
        daemon, repo, monkeypatch):
    """(a) a carried TRACKED file whose trunk version moved since the fork:
    `checkout --detach <trunk>` fails 'local changes would be overwritten'
    over a skip-worktree entry, even with identical bytes."""
    d, store = daemon
    _write(repo, TRACKED, "v1\n")
    _git(repo, "add", TRACKED)
    _git(repo, "commit", "-q", "-m", "pack file")
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _write(repo, TRACKED, "v2 uncommitted\n")
    _carry(d, monkeypatch, repo, path)
    assert open(os.path.join(path, TRACKED)).read() == "v2 uncommitted\n"
    assert _tags(path, TRACKED) == ["S"]
    # The trunk lands a new version of the same file.
    _write(repo, TRACKED, "v3\n")
    _git(repo, "commit", "-q", "-am", "pack file v3")
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "merged", got["merge_note"]
    assert _git(repo, "show", f"main:{TRACKED}") == "v3\n"
    assert not os.path.exists(card["worktree_path"])


@pytest.mark.asyncio
async def test_a_carried_new_file_the_trunk_now_tracks_does_not_stop_the_merge(
        daemon, repo, monkeypatch):
    """(b) the trunk committed the very file the folder carried as new: the
    detach would fail 'untracked working tree files would be overwritten'."""
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _write(repo, NEW, "same bytes\n")
    _carry(d, monkeypatch, repo, path)
    _git(repo, "add", NEW)
    _git(repo, "commit", "-q", "-m", "trunk now tracks the pack file")
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "merged", got["merge_note"]
    assert _git(repo, "show", f"main:{NEW}") == "same bytes\n"


@pytest.mark.asyncio
async def test_a_manifest_path_missing_from_the_index_does_not_undo_the_release(
        daemon, repo, monkeypatch):
    """(c) one manifest `new` path no longer in the folder's index must not
    make the batch `update-index --no-skip-worktree` fail for all the others
    and leave their marks standing."""
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    other = ".claude/agents/zz-other.md"
    _write(repo, NEW, "one\n")
    _write(repo, other, "two\n")
    _carry(d, monkeypatch, repo, path)
    # `other` leaves the index and the disk behind the carry's back.
    _git(path, "update-index", "--no-skip-worktree", "--", other)
    _git(path, "rm", "--cached", "-q", "--", other)
    os.unlink(os.path.join(path, other))
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "merged", got["merge_note"]
    assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_an_edited_carried_copy_refuses_and_nothing_is_touched(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _write(repo, NEW, "carried\n")
    manifest = _carry(d, monkeypatch, repo, path)
    _write(path, NEW, "edited by the agent\n")
    old = _rev(repo)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == merges.DIRTY_FOLDER_REFUSAL
    assert _rev(repo) == old
    assert open(os.path.join(path, NEW)).read() == "edited by the agent\n"
    assert os.path.exists(manifest)
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() \
        == card["worktree_branch"]


@pytest.mark.asyncio
async def test_releasing_the_carry_never_stages_a_deletion_of_a_head_path(
        daemon, repo, monkeypatch):
    """A manifest row marked new whose path this HEAD tracks is a tracked
    path: it goes back to HEAD, it is never `rm --cached`."""
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _write(path, NEW, "on the branch\n")
    _git(path, "add", NEW)
    _git(path, "commit", "-q", "-m", "branch tracks it")
    with open(worktrees.pack_manifest_path(repo, d._pack_card_id(path)),
              "w") as handle:
        handle.write(worktrees.manifest_text([{
            "path": NEW, "kind": "new",
            "sha256": hashlib.sha256(b"on the branch\n").hexdigest()}]))
    assert d._merge_uncarry_sync(repo, path) == ""
    assert _git(path, "diff", "--cached", "--name-status") == ""
    assert _git(path, "status", "--porcelain") == ""
    assert open(os.path.join(path, NEW)).read() == "on the branch\n"


@pytest.mark.asyncio
async def test_a_fix_helper_opens_on_a_folder_with_the_carry_released(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _write(repo, NEW, "carried\n")
    _carry(d, monkeypatch, repo, path)
    store.record_merge(card["id"], "conflict", "Merge needs you: x.")
    ok, detail = await d.fix_merge_card(card["id"])
    assert ok, detail
    assert _git(path, "ls-files", "-t", "--", NEW) == ""
    assert not os.path.exists(os.path.join(path, NEW))


@pytest.mark.asyncio
async def test_a_merging_card_cannot_be_started_or_moved_out_of_done(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card = await _done_card(d, store, repo)
    d._merging = {card["id"]: {"root": repo, "since": time.time(),
                               "token": "t"}}
    got, detail = await d.update_card(card["id"], {"column_name": "backlog"})
    assert got is None and detail == merges.MOVE_WHILE_MERGING_REFUSAL
    got, detail = await d.reorder_card(card["id"], "in_progress", "")
    assert got is None and detail == merges.MOVE_WHILE_MERGING_REFUSAL
    got, detail = await d.reset_card(card["id"], {"column_name": "backlog"})
    assert got is None and detail == merges.MOVE_WHILE_MERGING_REFUSAL
    assert store.get(card["id"])["column_name"] == "done"
    # A field save that does not leave Done is not a move.
    got, _ = await d.update_card(card["id"], {"title": "renamed"})
    assert got is not None
    # And Start on its folder is refused in words.
    store.update(card["id"], {"column_name": "backlog", "session_id": "",
                              "link_state": ""}, bump=False)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert (ok, detail) == (False, merges.START_WHILE_MERGING_REFUSAL)
    assert opened == []
    d._merging = {}


@pytest.mark.asyncio
async def test_a_folder_left_detached_and_clean_is_put_back_on_its_branch(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _git(path, "checkout", "-q", "--detach")
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert await d._reusable_worktree(store.get(card["id"])) == path
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() \
        == card["worktree_branch"]
    # A folder a merge is working in is left alone.
    _git(path, "checkout", "-q", "--detach")
    d._merging = {card["id"]: {"root": repo, "since": time.time(),
                               "token": "t"}}
    assert await d._reusable_worktree(store.get(card["id"])) == path
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    d._merging = {}
    # And so is a detached folder with uncommitted work.
    with open(os.path.join(path, "stray.txt"), "w") as handle:
        handle.write("x\n")
    assert await d._reusable_worktree(store.get(card["id"])) == path
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"


@pytest.mark.asyncio
async def test_the_main_checkout_answers_must_agree_or_nothing_moves(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    old = _rev(repo)
    real = d._run_git

    async def flaky(argv, root, **kw):
        if "symbolic-ref" in argv and "HEAD" in argv:
            return False, b"", "git failed"
        return await real(argv, root, **kw)

    monkeypatch.setattr(d, "_run_git", flaky)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == merges.ROOT_UNSURE_REFUSAL
    assert _rev(repo) == old
    # A genuinely detached root agrees with the list and takes update-ref.
    monkeypatch.setattr(d, "_run_git", real)
    _git(repo, "checkout", "-q", "--detach")
    ok, _ = await _merge(d, card["id"])
    assert ok
    assert store.get(card["id"])["merge_state"] == "merged"
    assert _rev(repo, "main") != old


@pytest.mark.asyncio
async def test_merge_offered_is_published_from_the_gate_without_the_busy_rungs(
        daemon, repo, monkeypatch):
    d, store = daemon

    def offered(card_id):
        snap = d._decorate_card_for_snapshot(
            d._trim_card_for_snapshot(store.get(card_id)), {})
        return snap.get("merge_offered")

    ok_card = await _done_card(d, store, repo, "fine", file="a1.txt")
    assert offered(ok_card["id"]) is True
    failed = await _done_card(d, store, repo, "failed", file="a2.txt",
                              check=False)
    await _settle_checks(d, store, failed["id"], repo, outcome="failed",
                         folder="2026-10-03-bad")
    assert offered(failed["id"]) is False
    # Cached by the file's mtime: a rewritten file is read again.
    path = store.get(failed["id"])["manual_check_path"]
    with open(path) as handle:
        text = handle.read()
    with open(path, "w") as handle:
        handle.write(text.replace("Status:** failed", "Status:** passed")
                     + "\n<!-- edited -->\n")
    assert offered(failed["id"]) is True
    openc = await _done_card(d, store, repo, "open", file="a3.txt", check=False)
    store.update(openc["id"], {"column_name": "in_progress"}, bump=False)
    store.bind_session(openc["id"], "s3")
    cp = os.path.join(repo, "manual-check", "2026-10-03-o", "check.md")
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    with open(cp, "w") as handle:
        handle.write(_check_text())
    store.flag_manual(openc["id"], "s3", STEPS, os.path.realpath(cp))
    store.update(openc["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    assert offered(openc["id"]) is False
    # Busy rungs do not unpublish it; a merge running does.
    d._agents_snapshot_cache = _inside_row(ok_card["worktree_path"])
    assert offered(ok_card["id"]) is True
    d._merging = {ok_card["id"]: {"root": repo, "since": time.time(),
                                  "token": "t"}}
    assert offered(ok_card["id"]) is False
    d._merging = {}
    # Only a Done card with a branch carries the key at all.
    plain = _make(store, repo, column_name="backlog")
    assert offered(plain["id"]) is None


@pytest.mark.asyncio
async def test_a_folder_that_cannot_go_back_says_detached_and_offers_no_fix(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo, file="a.txt", text="branch\n")
    with open(os.path.join(repo, "a.txt"), "w") as handle:
        handle.write("main edit\n")
    _git(repo, "commit", "-q", "-am", "main edit")

    async def refuse(path, branch):
        return False

    monkeypatch.setattr(d, "_merge_back_to_branch", refuse)
    ok, _ = await _merge(d, card["id"])
    assert ok
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"].startswith(
        "The merge stopped, and Dark Army could not put this card's folder")
    assert not merges.FIX_STATES.count(got["merge_state"])


@pytest.mark.asyncio
async def test_expected_tip_is_checked_again_when_the_task_runs(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    old = _rev(repo)
    await d._merge_run(card["id"], repo, "0" * 40)
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == merges.TIP_CHANGED_REFUSAL
    assert _rev(repo) == old
    # The right tip goes through the same door.
    tip = _rev(repo, card["worktree_branch"])
    await d._merge_run(card["id"], repo, tip)
    assert store.get(card["id"])["merge_state"] == "merged"


@pytest.mark.asyncio
async def test_a_present_expected_tip_must_be_a_whole_hash(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    for bad in ("", "abc", "A" * 40, "not a tip", 7):
        ok, detail = await d.merge_card(card["id"], expected_tip=bad)
        assert (ok, detail) == (False, merges.TIP_CHANGED_REFUSAL), bad
    assert d._merging == {}
    ok, detail = await d.merge_card(card["id"], expected_tip=None)
    assert (ok, detail) == (True, merges.MERGING_NOTE)
    await _settle(d)


@pytest.mark.asyncio
async def test_a_branch_checked_out_elsewhere_is_never_deleted(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    _git(repo, "worktree", "add", "-q", "--detach", str(tmp_path / "other"))
    # Keep the card's folder (a stray file) so the branch stays in use there.
    with open(os.path.join(card["worktree_path"], "stray.txt"), "w") as fh:
        fh.write("x\n")
    ok = await d._merge_delete_branch(
        repo, card["worktree_branch"], _rev(repo, card["worktree_branch"]),
        "main")
    assert ok is False
    assert card["worktree_branch"] in _git(repo, "branch", "--list")


@pytest.mark.asyncio
async def test_a_detached_folder_mid_rebase_or_merge_is_left_alone(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    path = card["worktree_path"]
    _git(path, "checkout", "-q", "--detach")
    git_dir = _git(path, "rev-parse", "--git-dir").strip()
    marker = os.path.join(path, git_dir, "rebase-merge")
    os.makedirs(marker)
    assert await d._reusable_worktree(store.get(card["id"])) == path
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    os.rmdir(marker)
    assert await d._reusable_worktree(store.get(card["id"])) == path
    assert _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() \
        == card["worktree_branch"]
