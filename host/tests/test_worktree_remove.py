"""Worktrees: an already-merged branch is never offered, and DELETE removes
one leftover folder (`docs/card-worktrees.md`, *Several finished cards merge
one after another*).

`test_worktrees_read.py`'s seams: a real `BobDaemon` and `BoardStore` on a
temp file over a real git repository.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from dark_army_daemon import merges, worktree_list
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon
from .test_card_changes_api import _git, setup  # noqa: F401
from .test_worktrees_read import _by_title, _card


def _rel(card) -> str:
    return worktree_list.relative_folder(
        os.path.basename(card["worktree_path"]))


def _land(root, card) -> None:
    """Merge the card's branch into main, as a person would by hand."""
    _git(root, "merge", "-q", "--no-edit", card["worktree_branch"])


def _branch_exists(root, branch) -> bool:
    done = subprocess.run(
        ["git", "-C", root, "rev-parse", "--verify", "--quiet",
         f"refs/heads/{branch}"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)
    return done.returncode == 0


# --- the status rule ---------------------------------------------------------

def test_nothing_ahead_is_never_mergeable_in_the_row():
    row = worktree_list.row(card_id="c", status="nothing", mergeable=True,
                            branch="card/x", branch_tip="a" * 40)
    assert row["mergeable"] is False
    done = worktree_list.row(card_id="c", status="done", mergeable=True,
                             branch="card/x", branch_tip="a" * 40)
    assert done["mergeable"] is True
    assert "nothing" not in worktree_list.MERGEABLE


def test_removable_needs_a_removable_status_and_something_to_remove():
    for status in ("merged", "gone", "nothing", "no_card"):
        assert worktree_list.row(status=status, removable=True,
                                 folder=".worktrees/x")["removable"] is True
    assert worktree_list.row(status="done", removable=True,
                             folder=".worktrees/x")["removable"] is False
    assert worktree_list.row(status="merged", removable=True)[
        "removable"] is False
    assert worktree_list.row(status="merged", removable=False,
                             folder=".worktrees/x")["removable"] is False


def test_a_branch_with_nothing_ahead_is_not_ready_to_merge(setup):
    api, daemon, store, root = setup
    _card(store, root, "same", commits=0)
    _card(store, root, "ahead", commits=1)
    page = daemon._worktrees_sync()
    rows = _by_title(page)
    assert rows["same"]["status"] == "nothing"
    assert rows["same"]["mergeable"] is False
    assert rows["same"]["line"] == worktree_list.NOTHING_LINE
    assert rows["same"]["removable"] is True
    assert rows["ahead"]["status"] == "done"
    assert rows["ahead"]["mergeable"] is True
    assert rows["ahead"]["removable"] is False


@pytest.mark.asyncio
async def test_the_read_records_a_contained_card_as_merged(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "same", commits=0)
    running = _card(store, root, "running", commits=0, column="in_progress")
    await daemon._worktrees_read()
    got = store.get(card["id"])
    assert got["merge_state"] == "merged"
    assert got["merge_note"] == merges.ALREADY_MERGED_NOTE
    # A card not in Done is left as it was.
    assert store.get(running["id"])["merge_state"] == ""
    page = await daemon._worktrees_read()
    assert _by_title(page)["same"]["status"] == "merged"
    assert _by_title(page)["same"]["mergeable"] is False


@pytest.mark.asyncio
async def test_a_branch_that_moved_since_the_read_is_not_recorded(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "same", commits=0)
    daemon._worktrees_sync()
    path = os.path.join(root, ".worktrees", "moved")
    _git(root, "worktree", "add", "-q", path, card["worktree_branch"])
    with open(os.path.join(path, "n.txt"), "w") as handle:
        handle.write("n\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "late")
    await daemon._record_nothing_to_merge()
    assert store.get(card["id"])["merge_state"] == ""


@pytest.mark.asyncio
async def test_the_batch_gate_refuses_a_contained_card_in_words(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "same", commits=0)
    tip = _git(root, "rev-parse", card["worktree_branch"]).strip()
    ok, detail = await daemon.merge_cards([(card["id"], tip)])
    assert ok is False
    assert merges.NOTHING_TO_MERGE_REFUSAL in detail
    assert store.get(card["id"])["merge_state"] == "merged"


# --- DELETE ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_merged_cards_folder_and_branch_are_deleted(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "landed", folder=True, commits=1)
    _land(root, card)
    ok, detail = await daemon.remove_worktree(card["id"], _rel(card), "")
    assert ok is True, detail
    assert merges.REMOVED_BRANCH_SUFFIX in detail
    assert not os.path.exists(card["worktree_path"])
    assert not _branch_exists(root, card["worktree_branch"])
    got = store.get(card["id"])
    assert got["worktree_path"] == "" and got["worktree_branch"] == ""
    assert got["merge_state"] == "merged"


@pytest.mark.asyncio
async def test_a_branch_with_work_main_lacks_is_never_deleted(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "unmerged", folder=True, commits=1)
    ok, detail = await daemon.remove_worktree(card["id"], _rel(card), "")
    assert ok is False and detail == merges.REMOVE_UNMERGED_REFUSAL
    assert os.path.isdir(card["worktree_path"])
    assert _branch_exists(root, card["worktree_branch"])


@pytest.mark.asyncio
async def test_git_refusing_uncommitted_files_keeps_the_folder_in_words(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "dirty", folder=True, commits=1)
    _land(root, card)
    with open(os.path.join(card["worktree_path"], "loose.txt"), "w") as fh:
        fh.write("x\n")
    ok, detail = await daemon.remove_worktree(card["id"], _rel(card), "")
    assert ok is False and detail == merges.REMOVE_KEPT_REFUSAL
    assert os.path.isfile(os.path.join(card["worktree_path"], "loose.txt"))
    assert _branch_exists(root, card["worktree_branch"])
    got = store.get(card["id"])
    assert got["worktree_path"] == card["worktree_path"]
    assert got["worktree_branch"] == card["worktree_branch"]


@pytest.mark.asyncio
async def test_a_card_that_is_not_in_done_is_refused(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "open", folder=True, commits=0,
                 column="in_progress")
    ok, detail = await daemon.remove_worktree(card["id"], _rel(card), "")
    assert (ok, detail) == (False, merges.REMOVE_OPEN_REFUSAL)
    assert os.path.isdir(card["worktree_path"])
    store.update(card["id"], {"column_name": "done"}, bump=False)
    store.bind_session(card["id"], "s1")
    live = store.get(card["id"])
    assert live["link_state"] in ("live", "dispatching")
    ok, detail = await daemon.remove_worktree(card["id"], _rel(card), "")
    assert (ok, detail) == (False, merges.REMOVE_OPEN_REFUSAL)


@pytest.mark.asyncio
async def test_a_live_session_a_merge_and_a_helper_hold_the_folder(
        setup, monkeypatch):
    api, daemon, store, root = setup
    card = _card(store, root, "held", folder=True, commits=0)
    rel = _rel(card)
    with monkeypatch.context() as local:
        local.setattr(daemon, "_session_inside", lambda path: True)
        assert await daemon.remove_worktree(card["id"], rel, "") == (
            False, merges.REMOVE_INUSE_REFUSAL)
    daemon._merging = {card["id"]: {"root": root, "since": 0, "token": "t"}}
    assert await daemon.remove_worktree(card["id"], rel, "") == (
        False, merges.REMOVE_INUSE_REFUSAL)
    daemon._merging = {}
    daemon._merge_helpers = {card["id"]: {"since": __import__("time").time()}}
    assert await daemon.remove_worktree(card["id"], rel, "") == (
        False, merges.REMOVE_INUSE_REFUSAL)
    daemon._merge_helpers = {}
    daemon._worktree_preparing = {card["id"]: object()}
    assert await daemon.remove_worktree(card["id"], rel, "") == (
        False, merges.REMOVE_INUSE_REFUSAL)
    daemon._worktree_preparing = {}
    assert os.path.isdir(card["worktree_path"])
    ok, detail = await daemon.remove_worktree(card["id"], rel, "")
    assert ok is True, detail


@pytest.mark.asyncio
async def test_a_batch_sibling_still_open_keeps_the_shared_folder(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "head", folder=True, commits=0)
    sibling, _why = store.create({"title": "sibling", "project": "proj",
                                  "root": root, "column_name": "in_progress"})
    store.record_worktree(sibling["id"], card["worktree_path"],
                          card["worktree_branch"])
    ok, detail = await daemon.remove_worktree(card["id"], _rel(card), "")
    assert (ok, detail) == (False, merges.REMOVE_SHARED_REFUSAL)
    assert os.path.isdir(card["worktree_path"])


@pytest.mark.asyncio
async def test_a_path_outside_the_worktrees_folder_is_never_touched(
        setup, tmp_path):
    api, daemon, store, root = setup
    card = _card(store, root, "stray", commits=0)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("x")
    store.record_worktree(card["id"], str(outside), card["worktree_branch"])
    ok, detail = await daemon.remove_worktree(card["id"], "", "")
    assert (ok, detail) == (False, merges.REMOVE_OUTSIDE_REFUSAL)
    assert (outside / "keep.txt").exists()


@pytest.mark.asyncio
async def test_a_symbolic_link_is_never_removed(setup, tmp_path):
    api, daemon, store, root = setup
    target = tmp_path / "real"
    target.mkdir()
    os.makedirs(os.path.join(root, ".worktrees"), exist_ok=True)
    os.symlink(str(target), os.path.join(root, ".worktrees", "link"))
    ok, detail = await daemon.remove_worktree(
        "", ".worktrees/link", os.path.basename(root))
    assert (ok, detail) == (False, merges.REMOVE_LINK_REFUSAL)
    assert target.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("folder", [
    "", ".worktrees", ".worktrees/", ".worktrees/..", ".worktrees/a/b",
    "../x", "x", "/etc", ".worktrees/../x"])
async def test_a_folder_name_that_is_not_one_leaf_is_refused(setup, folder):
    api, daemon, store, root = setup
    ok, detail = await daemon.remove_worktree(
        "", folder, os.path.basename(root))
    assert ok is False and detail == merges.REMOVE_NOT_FOUND_REFUSAL


@pytest.mark.asyncio
async def test_an_orphan_work_tree_is_removed_and_its_branch_kept(setup):
    api, daemon, store, root = setup
    path = os.path.join(root, ".worktrees", "orphan")
    _git(root, "worktree", "add", "-q", "-b", "side", path)
    label = os.path.basename(root)
    ok, detail = await daemon.remove_worktree(
        "", ".worktrees/orphan", label)
    assert ok is True, detail
    assert not os.path.exists(path)
    assert _branch_exists(root, "side")


@pytest.mark.asyncio
async def test_an_orphan_with_uncommitted_files_stays(setup):
    api, daemon, store, root = setup
    path = os.path.join(root, ".worktrees", "orphan")
    _git(root, "worktree", "add", "-q", "-b", "side", path)
    with open(os.path.join(path, "loose.txt"), "w") as handle:
        handle.write("x\n")
    ok, detail = await daemon.remove_worktree(
        "", ".worktrees/orphan", os.path.basename(root))
    assert (ok, detail) == (False, merges.REMOVE_KEPT_REFUSAL)
    assert os.path.isfile(os.path.join(path, "loose.txt"))


@pytest.mark.asyncio
async def test_a_detached_orphan_with_unlanded_commits_stays(setup):
    api, daemon, store, root = setup
    path = os.path.join(root, ".worktrees", "orphan")
    _git(root, "worktree", "add", "-q", "--detach", path)
    with open(os.path.join(path, "n.txt"), "w") as handle:
        handle.write("n\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "only here")
    ok, detail = await daemon.remove_worktree(
        "", ".worktrees/orphan", os.path.basename(root))
    assert (ok, detail) == (False, merges.REMOVE_UNMERGED_REFUSAL)
    assert os.path.isdir(path)


@pytest.mark.asyncio
async def test_a_plain_folder_git_does_not_know_is_left_in_words(setup):
    api, daemon, store, root = setup
    os.makedirs(os.path.join(root, ".worktrees", "plain"))
    ok, detail = await daemon.remove_worktree(
        "", ".worktrees/plain", os.path.basename(root))
    assert (ok, detail) == (False, merges.REMOVE_NOT_A_TREE_REFUSAL)
    assert os.path.isdir(os.path.join(root, ".worktrees", "plain"))


@pytest.mark.asyncio
async def test_a_folder_a_card_still_names_is_not_an_orphan(setup):
    api, daemon, store, root = setup
    card = _card(store, root, "named", folder=True, commits=0)
    ok, detail = await daemon.remove_worktree(
        "", _rel(card), os.path.basename(root))
    assert (ok, detail) == (False, merges.REMOVE_CARD_FOLDER_REFUSAL)
    assert os.path.isdir(card["worktree_path"])


@pytest.mark.asyncio
async def test_a_card_with_no_folder_and_a_contained_branch_loses_the_branch(
        setup):
    api, daemon, store, root = setup
    card = _card(store, root, "branch-only", commits=0)
    ok, detail = await daemon.remove_worktree(card["id"], "", "")
    assert ok is True, detail
    assert not _branch_exists(root, card["worktree_branch"])
    got = store.get(card["id"])
    assert got["worktree_branch"] == "" and got["merge_state"] == "merged"
    ok, detail = await daemon.remove_worktree(card["id"], "", "")
    assert (ok, detail) == (False, merges.REMOVE_NOTHING_REFUSAL)


@pytest.mark.asyncio
async def test_a_missing_card_and_a_closed_board_are_words(setup):
    api, daemon, store, root = setup
    assert await daemon.remove_worktree("nope", "", "") == (
        False, merges.NO_CARD_REFUSAL)
    daemon._board = None
    assert await daemon.remove_worktree("x", "", "") == (
        False, merges.BOARD_CLOSED_REFUSAL)


# --- the verb on the doors ---------------------------------------------------

def test_the_verb_is_on_every_list_a_board_merge_batch_is():
    verb = "board_worktree_remove"
    assert ApiServer.BOARD_ACTIONS.count(verb) == 1
    assert ApiServer.LAN_ACTIONS.count(verb) == 1
    assert ApiServer.REMOTE_ACTIONS.count(verb) == 1
    assert verb in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_the_marker_is_published():
    assert BobDaemon(headless=True)._pipeline_writable()[
        "worktree_remove_writable"] is True


@pytest.mark.asyncio
async def test_the_action_routes_to_the_daemon_and_speaks_its_words(
        setup, monkeypatch):
    api, daemon, store, root = setup
    seen = []

    async def fake(card_id, folder, project):
        seen.append((card_id, folder, project))
        return (card_id == "ok"), "words from the daemon"

    monkeypatch.setattr(daemon, "remove_worktree", fake)
    status, _, body = await api._board_action("board_worktree_remove", {})
    assert status == 400 and not seen
    status, _, body = await api._board_action(
        "board_worktree_remove",
        {"card_id": "ok", "folder": ".worktrees/x", "project": "proj"})
    assert status == 200 and b"words from the daemon" in body
    status, _, body = await api._board_action(
        "board_worktree_remove",
        {"folder": ".worktrees/x", "project": "proj"})
    assert status == 409 and b"words from the daemon" in body
    assert seen == [("ok", ".worktrees/x", "proj"),
                    ("", ".worktrees/x", "proj")]
