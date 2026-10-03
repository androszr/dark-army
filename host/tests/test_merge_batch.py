"""Several finished cards merged one after another (`merge_cards`).

`test_merge_card.py`'s seams, by import: a real `BoardStore` on a temp file, a
real git repository under `tmp_path`, `enrollment.enrolled_roots`
monkeypatched. The batch is the single card's engine run in a loop, so every
landing here is a real merge commit (`docs/card-worktrees.md`, *Several
finished cards merge one after another*).
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from dark_army_daemon import board, enrollment, merges
from .test_merge_card import (_check_script, _check_text, _done_card, _git,
                              _id8, _make, _rev, _settle, _settle_checks,
                              _trust, daemon, repo)  # noqa: F401  (fixtures)

STEPS = "1. Open the menu bar.\nWhy not automated: a real screen."


def _pairs(*cards):
    return [(c["id"], _rev(c["_root"], c["worktree_branch"])) for c in cards]


async def _cards(d, store, repo, *titles):
    out = []
    for n, title in enumerate(titles):
        card = await _done_card(d, store, repo, title, file=f"c{n}.txt")
        card["_root"] = repo
        out.append(card)
    return out


async def _batch(d, pairs):
    ok, detail = await d.merge_cards(pairs)
    if ok:
        await _settle(d)
    return ok, detail


def _log(root):
    return _git(root, "log", "--first-parent", "--format=%s", "main").split(
        "\n")


@pytest.mark.asyncio
async def test_two_cards_land_in_list_order(daemon, repo):
    d, store = daemon
    first, second = await _cards(d, store, repo, "first card", "second card")
    ok, detail = await d.merge_cards(_pairs(first, second))
    assert ok, detail
    assert any(not t.done() for t in d._merge_tasks)
    assert detail.startswith("Merging 2 cards into main, one after another")
    await _settle(d)
    log = _log(repo)
    assert log[0] == f"Merge card/{_id8(second)}: second card"
    assert log[1] == f"Merge card/{_id8(first)}: first card"
    branches = _git(repo, "branch", "--list")
    for card in (first, second):
        assert card["worktree_branch"] not in branches
        assert not os.path.exists(card["worktree_path"])
        assert store.get(card["id"])["merge_state"] == "merged"
    assert d._merge_batch == {}
    assert d._merging == {}
    # Both rows read Merged on the Worktrees page though folder and branch
    # are gone, and neither can be ticked.
    rows = {r["title"]: r for r in d._worktrees_sync()["rows"]}
    for title in ("first card", "second card"):
        assert rows[title]["status"] == "merged"
        assert rows[title]["mergeable"] is False


@pytest.mark.asyncio
async def test_a_conflict_is_recorded_and_the_other_card_still_lands(
        daemon, repo):
    d, store = daemon
    clash = await _done_card(d, store, repo, "clash", file="a.txt",
                             text="branch\n")
    clash["_root"] = repo
    with open(os.path.join(repo, "a.txt"), "w") as handle:
        handle.write("main edit\n")
    _git(repo, "commit", "-q", "-am", "main edit")
    other = await _done_card(d, store, repo, "other", file="o.txt")
    other["_root"] = repo
    ok, _ = await _batch(d, _pairs(clash, other))
    assert ok
    got = store.get(clash["id"])
    assert got["merge_state"] == "conflict"
    assert "a.txt" in got["merge_note"]
    snap = d._decorate_card_for_snapshot(d._trim_card_for_snapshot(got), {})
    assert snap["merge_offered"] is True
    assert merges.PRESS_AGAIN_SUFFIX in snap["merge_line"]
    assert store.get(other["id"])["merge_state"] == "merged"
    assert _log(repo)[0] == f"Merge card/{_id8(other)}: other"


@pytest.mark.asyncio
async def test_a_red_check_is_recorded_and_the_other_card_lands(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    _trust(monkeypatch, tmp_path, repo)
    # Red only where the card's own file is present.
    _check_script(repo, 'test ! -f red.txt\n')
    red = await _done_card(d, store, repo, "red", file="red.txt")
    red["_root"] = repo
    green = await _done_card(d, store, repo, "green", file="g.txt")
    green["_root"] = repo
    ok, _ = await _batch(d, _pairs(red, green))
    assert ok
    assert store.get(red["id"])["merge_state"] == "checks_failed"
    assert store.get(green["id"])["merge_state"] == "merged"


@pytest.mark.asyncio
async def test_a_queued_card_says_so_and_is_held(daemon, repo):
    d, store = daemon
    first, second = await _cards(d, store, repo, "one", "two")
    tips = dict(_pairs(first, second))
    d._merge_batch = {"token": "t", "queue": [first["id"], second["id"]],
                      "tips": tips, "total": 2}
    snap = d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(first["id"])), {})
    assert snap["merge_state"] == "queued"
    assert "1 of 2" in snap["merge_line"]
    assert snap["merge_offered"] is False
    later = d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(second["id"])), {})
    assert "2 of 2" in later["merge_line"]
    ok, detail = await d.merge_card(first["id"])
    assert (ok, detail) == (False, merges.MERGE_QUEUED_REFUSAL)
    assert d._merge_hold(first["id"]) is True
    d._merge_batch = {}
    assert d._merge_hold(first["id"]) is False


@pytest.mark.asyncio
async def test_failing_cards_are_skipped_and_named_at_the_press(
        daemon, repo):
    d, store = daemon
    good, moved, open_check, working = await _cards(
        d, store, repo, "good", "moved", "opened", "working")
    store.update(working["id"], {"column_name": "in_progress"}, bump=False)
    # An open hand-check on `opened`.
    store.update(open_check["id"], {"column_name": "in_progress"}, bump=False)
    store.bind_session(open_check["id"], "s9")
    path = os.path.join(repo, "manual-check", "2026-10-03-open", "check.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(_check_text())
    got, why = store.flag_manual(open_check["id"], "s9", STEPS,
                                 os.path.realpath(path))
    assert got is not None, why
    store.update(open_check["id"], {"column_name": "done",
                                    "link_state": "ended"}, bump=False)
    pairs = _pairs(good, moved, open_check, working)
    pairs[1] = (moved["id"], "0" * 40)
    ok, detail = await _batch(d, pairs)
    assert ok
    assert detail.startswith("Merging 1 card into main, one after another: "
                             "good.")
    for title, words in (("moved", merges.TIP_CHANGED_REFUSAL),
                         ("opened", merges.MANUAL_OPEN_REFUSAL),
                         ("working", merges.NOT_DONE_REFUSAL)):
        assert f"{title} — {words.rstrip('.')}" in detail
    for card in (moved, open_check, working):
        assert store.get(card["id"])["merge_state"] == ""
    assert store.get(good["id"])["merge_state"] == "merged"

    # Every card failing: nothing runs.
    ok, detail = await d.merge_cards([(moved["id"], "0" * 40)])
    assert ok is False and detail.startswith("Nothing was merged: ")
    assert d._merge_batch == {}


@pytest.mark.asyncio
async def test_a_second_batch_while_one_runs_is_refused(daemon, repo):
    d, store = daemon
    first, second = await _cards(d, store, repo, "one", "two")
    ok, _ = await d.merge_cards(_pairs(first))
    assert ok
    ok, detail = await d.merge_cards(_pairs(second))
    assert (ok, detail) == (False, merges.BATCH_RUNNING_REFUSAL)
    await _settle(d)


@pytest.mark.asyncio
async def test_the_bounds_are_refused_in_words(daemon, repo):
    d, store = daemon
    nine = [(f"id{n}", "a" * 40) for n in range(board.MAX_BATCH_CARDS + 1)]
    assert await d.merge_cards(nine) == (False, merges.BATCH_TOO_MANY_REFUSAL)
    assert await d.merge_cards([]) == (False, merges.BATCH_EMPTY_REFUSAL)


@pytest.mark.asyncio
async def test_a_branch_that_moved_after_the_press_is_blocked_at_its_turn(
        daemon, repo):
    d, store = daemon
    first, second = await _cards(d, store, repo, "one", "two")
    ok, _ = await d.merge_cards(_pairs(first, second))
    assert ok
    with open(os.path.join(second["worktree_path"], "late.txt"), "w") as fh:
        fh.write("late\n")
    _git(second["worktree_path"], "add", ".")
    _git(second["worktree_path"], "commit", "-q", "-m", "late")
    await _settle(d)
    assert store.get(first["id"])["merge_state"] == "merged"
    got = store.get(second["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == merges.TIP_CHANGED_REFUSAL


@pytest.mark.asyncio
async def test_a_card_dragged_out_of_done_before_its_turn_writes_nothing(
        daemon, repo):
    d, store = daemon
    first, second, third = await _cards(d, store, repo, "one", "two", "three")
    ok, _ = await d.merge_cards(_pairs(first, second, third))
    assert ok
    store.update(second["id"], {"column_name": "backlog"}, bump=False)
    await _settle(d)
    assert store.get(first["id"])["merge_state"] == "merged"
    assert store.get(second["id"])["merge_state"] == ""
    assert store.get(second["id"])["column_name"] == "backlog"
    assert store.get(third["id"])["merge_state"] == "merged"


def _second_repo(tmp_path, monkeypatch, first):
    root = tmp_path / "proj2"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    real = os.path.realpath(root)
    monkeypatch.setattr(enrollment, "enrolled_roots",
                        lambda: {first, real})
    return real


@pytest.mark.asyncio
async def test_cards_in_two_enrolled_roots_both_land(
        daemon, repo, tmp_path, monkeypatch):
    d, store = daemon
    other_root = _second_repo(tmp_path, monkeypatch, repo)
    one = await _done_card(d, store, repo, "here", file="h.txt")
    one["_root"] = repo
    two = await _done_card(d, store, other_root, "there", file="t.txt")
    two["_root"] = other_root
    ok, _ = await _batch(d, _pairs(one, two))
    assert ok
    assert store.get(one["id"])["merge_state"] == "merged"
    assert store.get(two["id"])["merge_state"] == "merged"
    assert _log(other_root)[0] == f"Merge card/{_id8(two)}: there"


@pytest.mark.asyncio
async def test_the_batch_waits_for_another_merge_of_the_project(
        daemon, repo, monkeypatch):
    d, store = daemon
    (card,) = await _cards(d, store, repo, "patient")
    monkeypatch.setattr(merges, "BATCH_WAIT_SECONDS", 5.0)
    d._merging = {"someone": {"root": repo, "since": time.time(),
                              "token": "t"}}
    d._merge_batch = {"token": "t", "queue": [card["id"]],
                      "tips": dict(_pairs(card)), "total": 1}
    task = asyncio.ensure_future(d._merge_batch_task("t"))
    d._merge_tasks.add(task)
    await asyncio.sleep(0.3)
    assert store.get(card["id"])["merge_state"] == ""
    assert not task.done()
    # Still queued, and its folder held, through the wait.
    assert d._merge_batch["queue"] == [card["id"]]
    assert d._merge_hold(card["id"]) is True
    d._merging = {}
    await _settle(d)
    assert store.get(card["id"])["merge_state"] == "merged"
    assert d._merge_batch == {}


@pytest.mark.asyncio
async def test_a_wait_that_runs_out_records_the_projects_refusal(
        daemon, repo, monkeypatch):
    d, store = daemon
    (card,) = await _cards(d, store, repo, "impatient")
    monkeypatch.setattr(merges, "BATCH_WAIT_SECONDS", 0.0)
    d._merging = {"someone": {"root": repo, "since": time.time(),
                              "token": "t"}}
    d._merge_batch = {"token": "t", "queue": [card["id"]],
                      "tips": dict(_pairs(card)), "total": 1}
    task = asyncio.ensure_future(d._merge_batch_task("t"))
    d._merge_tasks.add(task)
    await task
    got = store.get(card["id"])
    assert got["merge_state"] == "blocked"
    assert got["merge_note"] == merges.PROJECT_MERGING_REFUSAL


def test_batch_report_wording():
    assert merges.batch_report(["A"], []) == \
        "Merging 1 card into main, one after another: A."
    assert merges.batch_report(["A", "B"], [("D", "It is not done."),
                                            ("E", "Hand-check open")]) == (
        "Merging 2 cards into main, one after another: A, B. "
        "Not merged: D — It is not done; E — Hand-check open.")
    assert merges.batch_report([], [("D", "Nope.")]) == \
        "Nothing was merged: D — Nope."
    long = "x" * 200
    assert len(merges.batch_report([long], [])) < 200


@pytest.mark.asyncio
async def test_a_settled_card_at_its_turn_is_not_recorded_as_blocked(
        daemon, repo, monkeypatch):
    d, store = daemon
    (card,) = await _cards(d, store, repo, "settled")
    tip = _pairs(card)[0][1]
    store.record_merge(card["id"], "merged", "Merged into main as abc.")
    events: list = []
    monkeypatch.setattr(d, "_log_card_event",
                        lambda c, kind, **kw: events.append(kind))
    d._merge_batch = {"token": "t", "queue": [card["id"]],
                      "tips": {card["id"]: tip}, "total": 1}
    task = asyncio.ensure_future(d._merge_batch_task("t"))
    d._merge_tasks.add(task)
    await task
    got = store.get(card["id"])
    assert got["merge_state"] == "merged"
    assert got["merge_note"].startswith("Merged into main")
    assert "card_merge_blocked" not in events


@pytest.mark.asyncio
async def test_a_project_merge_that_slips_in_is_waited_for_not_recorded(
        daemon, repo, monkeypatch):
    d, store = daemon
    (card,) = await _cards(d, store, repo, "unlucky")
    monkeypatch.setattr(merges, "BATCH_WAIT_SECONDS", 30.0)
    real = d._merge_start
    calls: list = []

    async def start(cid, tip):
        calls.append(1)
        if len(calls) == 1:
            return False, merges.PROJECT_MERGING_REFUSAL, "", ""
        return await real(cid, tip)

    monkeypatch.setattr(d, "_merge_start", start)
    d._merge_batch = {"token": "t", "queue": [card["id"]],
                      "tips": dict(_pairs(card)), "total": 1}
    task = asyncio.ensure_future(d._merge_batch_task("t"))
    d._merge_tasks.add(task)
    await asyncio.sleep(0.3)
    assert d._merge_batch["queue"] == [card["id"]]
    assert store.get(card["id"])["merge_state"] == ""
    await _settle(d)
    assert len(calls) == 2
    assert store.get(card["id"])["merge_state"] == "merged"
