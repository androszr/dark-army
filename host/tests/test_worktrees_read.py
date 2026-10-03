"""The Worktrees read: every card's side folder with a plain status.

`test_card_changes_api.py`'s seams: a real `BobDaemon` and `BoardStore` on a
temp file, `enrollment` patched to a temp project, the loopback GET driven
through `_handle_client`, the sealed kind through `_sealed_run` on both doors,
over a real git repository (`docs/card-worktrees.md`, *Several finished cards
merge one after another*).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re

import pytest

from dark_army_daemon import command_receipts, merges, worktree_list
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon
from .test_card_changes_api import (_Writer, _git, _ledger,  # noqa: F401
                                    setup)
from .test_merge_card import _check_text

STEPS = "1. Open the menu bar.\nWhy not automated: a real screen."


def _card(store, root, title, *, column="done", folder=False, commits=1,
          dirty=0):
    card, detail = store.create({"title": title, "project": "proj",
                                 "root": root, "column_name": column})
    assert card is not None, detail
    branch = f"card/{card['id'][:8]}-x"
    path = os.path.join(root, ".worktrees", f"card-{card['id'][:8]}")
    _git(root, "branch", branch)
    if folder or commits:
        _git(root, "worktree", "add", "-q", path, branch)
        for n in range(commits):
            with open(os.path.join(path, f"f{n}.txt"), "w") as handle:
                handle.write(f"{n}\n")
            _git(path, "add", ".")
            _git(path, "commit", "-q", "-m", f"c{n}")
    for n in range(dirty):
        with open(os.path.join(path, f"loose{n}.txt"), "w") as handle:
            handle.write("x\n")
    if not folder:
        if commits:
            _git(root, "worktree", "remove", "--force", path)
        store.record_worktree(card["id"], "", branch)
    else:
        store.record_worktree(card["id"], path, branch)
    return store.get(card["id"])


def _by_title(page):
    return {r["title"]: r for r in page["rows"]}


async def _get(api, headers=None):
    hdrs = {"host": "localhost", "x-bob-token": "token"} \
        if headers is None else headers
    lines = ["GET /api/worktrees HTTP/1.1"]
    lines += [f"{k}: {v}" for k, v in hdrs.items()] + ["", ""]
    reader = asyncio.StreamReader()
    reader.feed_data("\r\n".join(lines).encode())
    reader.feed_eof()
    writer = _Writer()
    await api._handle_client(reader, writer)
    head, _, body = bytes(writer.buf).partition(b"\r\n\r\n")
    return int(head.split()[1]), body


def test_status_rows_one_per_status(setup):
    api, daemon, store, root = setup
    _card(store, root, "running", column="in_progress", folder=True)
    _card(store, root, "dirty", folder=True, dirty=2)
    ready = _card(store, root, "ready")
    _card(store, root, "same", commits=0)
    clash = _card(store, root, "clash")
    store.record_merge(clash["id"], "conflict", "Conflict in a.txt.")
    held = _card(store, root, "held")
    path = os.path.join(root, "manual-check", "2026-10-03-held", "check.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(_check_text())
    store.update(held["id"], {"column_name": "in_progress"}, bump=False)
    store.bind_session(held["id"], "s1")
    got, why = store.flag_manual(held["id"], "s1", STEPS,
                                 os.path.realpath(path))
    assert got is not None, why
    store.update(held["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    os.makedirs(os.path.join(root, ".worktrees", "loose"))
    os.makedirs(os.path.join(root, "elsewhere"))
    os.symlink(os.path.join(root, "elsewhere"),
               os.path.join(root, ".worktrees", "link"))

    page = daemon._worktrees_sync()
    assert page["available"] is True and page["truncated"] is False
    rows = _by_title(page)
    assert rows["running"]["status"] == "working"
    assert rows["running"]["mergeable"] is False
    assert rows["dirty"]["status"] == "working"
    assert rows["dirty"]["uncommitted"] == 2
    assert rows["dirty"]["line"] == "2 files not committed in its folder."
    assert rows["dirty"]["mergeable"] is False
    done = rows["ready"]
    assert done["status"] == "done" and done["ahead"] == 1
    assert re.fullmatch(r"[0-9a-f]{40}", done["branch_tip"])
    assert done["mergeable"] is True and done["word"] == "Work done"
    assert rows["same"]["status"] == "nothing"
    assert rows["clash"]["status"] == "conflict"
    assert rows["clash"]["line"].startswith("Conflict in a.txt.")
    assert rows["clash"]["mergeable"] is True
    assert rows["held"]["status"] == "waiting"
    assert rows["held"]["line"] == merges.MANUAL_OPEN_REFUSAL
    assert rows["held"]["mergeable"] is False
    orphans = [r for r in page["rows"] if r["status"] == "no_card"]
    assert [r["folder"] for r in orphans] == [".worktrees/loose"]
    assert orphans[0]["mergeable"] is False
    assert not any(r["folder"].endswith("link") for r in page["rows"])
    assert ready["id"] == done["card_id"]


def test_a_merged_card_reads_merged_and_a_queued_one_queued(setup):
    api, daemon, store, root = setup
    done = _card(store, root, "landed")
    store.record_merge(done["id"], "merged", "Merged into main as abc.")
    waiting = _card(store, root, "waiting")
    daemon._merge_batch = {"token": "t", "queue": [waiting["id"]],
                           "tips": {}, "total": 1}
    daemon._merging = {done["id"]: {"root": root, "since": 0.0,
                                    "token": "m"}}
    rows = _by_title(daemon._worktrees_sync())
    assert rows["landed"]["status"] == "merging"
    daemon._merging = {}
    rows = _by_title(daemon._worktrees_sync())
    assert rows["landed"]["status"] == "merged"
    assert rows["waiting"]["status"] == "queued"
    assert "1 of 1" in rows["waiting"]["line"]
    assert rows["waiting"]["mergeable"] is False


def test_an_unenrolled_root_and_a_plain_folder_list_nothing(
        setup, tmp_path, monkeypatch):
    api, daemon, store, root = setup
    plain = tmp_path / "plain"
    plain.mkdir()
    from dark_army_daemon import enrollment
    monkeypatch.setattr(enrollment, "enrolled_roots",
                        lambda: {str(plain)})
    page = daemon._worktrees_sync()
    assert page["available"] is False and page["rows"] == []
    assert page["reason"]
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set())
    assert daemon._worktrees_sync()["available"] is False


def test_the_closed_key_set_and_no_absolute_path(setup):
    api, daemon, store, root = setup
    _card(store, root, "one", folder=True)
    _card(store, root, "two")
    os.makedirs(os.path.join(root, ".worktrees", "stray"))
    page = daemon._worktrees_sync()
    assert page["rows"]
    for row in page["rows"]:
        assert tuple(row) == worktree_list.ROW_KEYS
        assert not row["folder"].startswith("/")
    assert root not in json.dumps(page)
    assert os.path.dirname(root) not in json.dumps(page)


def test_over_the_row_bound_the_page_is_truncated(setup, monkeypatch):
    api, daemon, store, root = setup
    for n in range(4):
        _card(store, root, f"c{n}", commits=0)
    monkeypatch.setattr(worktree_list, "MAX_ROWS", 2)
    page = daemon._worktrees_sync()
    assert len(page["rows"]) == 2 and page["truncated"] is True


@pytest.mark.asyncio
async def test_the_snapshot_path_makes_no_git_call(setup, monkeypatch):
    api, daemon, store, root = setup
    _card(store, root, "one", folder=True)
    _card(store, root, "two")
    calls: list = []
    real_blocking = daemon._git_blocking
    real_run = daemon._run_git

    def blocking(argv, *a, **kw):
        calls.append(argv)
        return real_blocking(argv, *a, **kw)

    async def run(argv, *a, **kw):
        calls.append(argv)
        return await real_run(argv, *a, **kw)

    monkeypatch.setattr(daemon, "_git_blocking", blocking)
    monkeypatch.setattr(daemon, "_run_git", run)
    daemon._refresh_board_state()
    await daemon._publish_board()
    assert daemon._build_board_state()["cards"] is not None
    assert calls == []
    daemon._worktrees_sync()
    assert calls


@pytest.mark.asyncio
async def test_loopback_get_wants_the_desk_token(setup):
    api, daemon, store, root = setup
    _card(store, root, "one")
    status, _ = await _get(api, {"host": "localhost"})
    assert status == 403
    status, _ = await _get(api, {"host": "localhost",
                                 "x-bob-token": "wrong"})
    assert status == 403
    status, body = await _get(api)
    assert status == 200 and json.loads(body)["rows"]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
async def test_the_sealed_kind_answers_on_both_doors_with_no_lease(
        setup, door):
    api, daemon, store, root = setup
    _card(store, root, "one")
    actions = api.LAN_ACTIONS if door == "home" else api.REMOTE_ACTIONS
    before = (tuple(ApiServer.LAN_ACTIONS), tuple(ApiServer.REMOTE_ACTIONS))
    status, _, body = await api._sealed_run(
        "worktrees", {}, "device", actions=actions,
        check_lease=False, record=False)
    assert status == 200, body
    assert json.loads(body)["available"] is True
    assert before == (tuple(ApiServer.LAN_ACTIONS),
                      tuple(ApiServer.REMOTE_ACTIONS))
    src = inspect.getsource(ApiServer._sealed_run)
    assert src.index('kind == "worktrees"') < src.index('kind == "action"')
    assert '"worktrees"' in inspect.getsource(ApiServer._lan_home)
    from dark_army_daemon import api_server
    assert "/api/worktrees" not in api_server.SESSION_READS


def test_the_markers_are_published():
    got = BobDaemon(headless=True)._pipeline_writable()
    assert got["worktrees_supported"] is True
    assert got["merge_batch_writable"] is True


def test_board_merge_batch_is_on_every_tuple():
    verb = "board_merge_batch"
    assert ApiServer.BOARD_ACTIONS.count(verb) == 1
    assert ApiServer.LAN_ACTIONS.count(verb) == 1
    assert ApiServer.REMOTE_ACTIONS.count(verb) == 1
    assert verb in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    src = inspect.getsource(ApiServer)
    for name in ("LAN_ACTIONS = (", "REMOTE_ACTIONS = ("):
        body = src.split(name, 1)[1].split("\n    )\n", 1)[0]
        mine = body[body.index('"set_bot_access",'):]
        assert "(" not in mine, name


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"card_ids": "a,b", "expected_tips": "x"},
    {"card_ids": "a,a", "expected_tips": "x,y"},
    {"card_ids": "a,,b", "expected_tips": "x,y,z"},
    {"card_ids": ["a"], "expected_tips": "x"},
    {"card_ids": "a", "expected_tips": 5},
    {"card_ids": "a"},
])
async def test_merge_pairs_it_cannot_parse_are_a_400(setup, fields):
    api, daemon, store, root = setup
    status, _, body = await api._board_action("board_merge_batch", fields)
    assert status == 400, body
    assert ApiServer._merge_pairs(
        {"card_ids": " a , b ", "expected_tips": "x,y"}) == [("a", "x"),
                                                             ("b", "y")]


def test_no_trunk_is_a_failed_row_never_nothing_to_merge(setup):
    api, daemon, store, root = setup
    _card(store, root, "ready")
    _git(root, "branch", "-m", "main", "trunk")
    page = daemon._worktrees_sync()
    row = _by_title(page)["ready"]
    assert row["line"] == merges.NO_TRUNK_REFUSAL
    assert row["mergeable"] is False
    assert row["status"] == "unreadable" and row["word"] == "Could not read"
    assert row["status"] != "nothing" and row["ahead"] == -1


def test_a_merged_card_with_no_folder_or_branch_stays_listed_as_merged(setup):
    import time
    api, daemon, store, root = setup
    card = _card(store, root, "landed")
    store.clear_worktree(card["id"], branch=True)
    store.record_merge(card["id"], "merged", "Merged into main as abc.")
    rows = _by_title(daemon._worktrees_sync())
    assert rows["landed"]["status"] == "merged"
    assert rows["landed"]["mergeable"] is False
    assert rows["landed"]["branch"] == "" and rows["landed"]["folder"] == ""
    old = store.get(card["id"])
    with store._lock:
        store._conn.execute("UPDATE cards SET updated_at = ? WHERE id = ?",
                            (time.time() - 2 * 86400, card["id"]))
        store._conn.commit()
    assert "landed" not in _by_title(daemon._worktrees_sync())
    assert old


@pytest.mark.asyncio
async def test_a_caller_arriving_mid_read_gets_a_read_that_started_after_it(
        setup, monkeypatch):
    import time
    api, daemon, store, root = setup
    starts: list = []
    real = daemon._worktrees_sync

    def slow():
        starts.append(time.monotonic())
        n = len(starts)
        time.sleep(0.25)
        page = real()
        page["read_number"] = n
        return page

    monkeypatch.setattr(daemon, "_worktrees_sync", slow)
    first = asyncio.ensure_future(daemon.worktrees_report())
    await asyncio.sleep(0.05)
    arrived = time.monotonic()
    later = await asyncio.gather(daemon.worktrees_report(),
                                 daemon.worktrees_report())
    assert (await first)["read_number"] == 1
    assert [p["read_number"] for p in later] == [2, 2]
    assert len(starts) == 2 and starts[1] > arrived
