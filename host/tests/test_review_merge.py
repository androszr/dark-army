"""Review & merge on a Done card (`docs/card-worktrees.md`, *Review & merge*).

Built on `test_merge_card.py`'s seams: a real `BoardStore` on a temp file, a
real git repository, `dispatch.spawn` stubbed. The terminal close is a stub
that records the session it was asked to close.
"""

from __future__ import annotations

import os
import time

import pytest

from dark_army_daemon import dispatch, merges, origin, terminal_title
from dark_army_daemon.api_server import ApiServer
from tests.test_merge_card import (  # noqa: F401  (fixtures and helpers)
    STEPS, _check_text, _done_card, _git, _rev, _settle,
    _stub_spawn, daemon, repo)

_SHIP = "VERDICT: SHIP\n\nFixed two findings; tests pass."


def _stub_close(d, monkeypatch):
    closed: list = []

    async def close(session_id, **kw):
        closed.append(session_id)
        return True, ""

    monkeypatch.setattr(d, "close_session_terminal", close)
    return closed


async def _open_check_card(d, store, repo, *, status="open"):
    """A Done card whose hand-check is still open (steps flagged)."""
    card = await _done_card(d, store, repo, check=False)
    target = os.path.join(repo, "manual-check", "2026-11-15-open")
    os.makedirs(target, exist_ok=True)
    path = os.path.realpath(os.path.join(target, "check.md"))
    with open(path, "w") as handle:
        handle.write(_check_text(status))
    store.update(card["id"], {"column_name": "in_progress"}, bump=False)
    store.bind_session(card["id"], "s1")
    got, detail = store.flag_manual(card["id"], "s1", STEPS, path)
    assert got is not None, detail
    store.update(card["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    return store.get(card["id"]), path


@pytest.mark.asyncio
async def test_offered_with_open_steps_but_not_for_merge(daemon, repo):
    d, store = daemon
    card, _path = await _open_check_card(d, store, repo)
    assert d._merge_gate_sync(card, busy=False)[0] == merges.MANUAL_OPEN_REFUSAL
    assert d._merge_gate_sync(card, busy=False, open_ok=True)[0] == ""
    snap = d._decorate_card_for_snapshot(d._trim_card_for_snapshot(card), {})
    assert snap["review_merge_offered"] is True
    assert snap["merge_offered"] is False
    assert "review_merge_running" not in snap


@pytest.mark.asyncio
async def test_a_failed_check_file_refuses_even_with_open_steps_allowed(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card, path = await _open_check_card(d, store, repo, status="failed")
    assert d._merge_gate_sync(card, busy=False, open_ok=True)[0] \
        == merges.MANUAL_FAILED_REFUSAL
    ok, detail = await d.review_merge_card(card["id"])
    assert (ok, detail) == (False, merges.MANUAL_FAILED_REFUSAL)
    assert opened == []
    snap = d._decorate_card_for_snapshot(d._trim_card_for_snapshot(card), {})
    assert snap["review_merge_offered"] is False


@pytest.mark.asyncio
async def test_the_press_records_passed_spawns_and_names_the_orders(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card, path = await _open_check_card(d, store, repo)
    tip = _rev(repo, card["worktree_branch"])
    ok, detail = await d.review_merge_card(card["id"])
    assert ok, detail
    with open(path) as handle:
        text = handle.read()
    assert "passed" in text.lower() and merges.REVIEW_MERGE_CHECK_PASSED_NOTE in text
    assert store.get(card["id"])["manual_steps"] == ""
    assert len(opened) == 1
    spawn = opened[0]
    assert spawn["kw"]["cwd"] == card["worktree_path"]
    assert spawn["kw"]["stamp"].startswith("card-review-merge|")
    assert spawn["name"] == "review+merge: do the thing"[:40]
    assert spawn["argv"][0] == "/bin/claude"
    prompt = spawn["argv"][-1]
    assert prompt.startswith(dispatch.REVIEW_MERGE_PREAMBLE.strip())
    assert f"/review {card['worktree_branch']}" in prompt
    assert "BLOCK and FIX" in prompt
    assert "VERDICT: SHIP" in prompt and "VERDICT: STOP" in prompt
    assert dispatch.CONSULT_ANSWER_TOOL in prompt
    assert "never merge into main" in prompt
    assert "never push, tag, install, release or delete a branch" in prompt
    assert prompt.rstrip().endswith("bob-tldr or bob-actions blocks.")
    entry = d._consults[card["id"]]
    assert entry["purpose"] == "review_merge" and entry["tip"] == tip
    snap = d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(card["id"])), {})
    assert snap["review_running"] is True
    assert snap["review_merge_running"] is True
    # Busy rungs: merge, Run review and a second press all wait.
    assert (await d.merge_card(card["id"]))[1] == merges.REVIEW_RUNNING_REFUSAL
    assert (await d.run_card_review(card["id"]))[1] \
        == merges.REVIEW_RUNNING_REFUSAL
    assert (await d.review_merge_card(card["id"]))[1] \
        == merges.REVIEW_RUNNING_REFUSAL
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_with_no_check_file_the_press_clears_the_steps(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card = await _done_card(d, store, repo, check=False)
    store.update(card["id"], {"column_name": "in_progress"}, bump=False)
    store.bind_session(card["id"], "s1")
    store.flag_manual(card["id"], "s1", STEPS, "")
    store.update(card["id"], {"column_name": "done", "link_state": "ended"},
                 bump=False)
    assert store.get(card["id"])["manual_steps"]
    ok, detail = await d.review_merge_card(card["id"])
    assert ok, detail
    assert store.get(card["id"])["manual_steps"] == ""


@pytest.mark.asyncio
async def test_ship_arms_the_pending_merge_with_the_fresh_tip_and_closes(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    closed = _stub_close(d, monkeypatch)
    card = await _done_card(d, store, repo)
    ok, _ = await d.review_merge_card(card["id"])
    assert ok
    old_tip = d._consults[card["id"]]["tip"]
    d._consults[card["id"]]["session_id"] = "rm-1"
    # The helper committed a fix before answering.
    wt = card["worktree_path"]
    with open(os.path.join(wt, "fix.txt"), "w") as fh:
        fh.write("fixed\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "fix a finding")
    new_tip = _rev(repo, card["worktree_branch"])
    assert new_tip != old_tip
    _, detail = await d.answer_card_by_session("rm-1", _SHIP)
    assert detail == "answered"
    row = store.get(card["id"])
    assert row["review_verdict"] == "ship" and row["review_tip"] == new_tip
    pending = d._review_merge_pending[card["id"]]
    assert pending["tip"] == new_tip and pending["session_id"] == "rm-1"
    assert closed == ["rm-1"]
    snap = d._decorate_card_for_snapshot(d._trim_card_for_snapshot(row), {})
    assert snap["review_merge_running"] is True
    assert snap["merge_line"] == merges.REVIEW_MERGE_WAITING_LINE
    # A second press is refused while the merge waits.
    assert (await d.review_merge_card(card["id"]))[1] \
        == merges.REVIEW_MERGE_PENDING_REFUSAL


@pytest.mark.asyncio
async def test_stop_arms_nothing(daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    closed = _stub_close(d, monkeypatch)
    card = await _done_card(d, store, repo)
    ok, _ = await d.review_merge_card(card["id"])
    assert ok
    d._consults[card["id"]]["session_id"] = "rm-2"
    await d.answer_card_by_session("rm-2", "VERDICT: STOP — it is unsafe\n\nx")
    assert d._review_merge_pending == {}
    assert closed == []
    assert store.get(card["id"])["review_verdict"] == "stop"
    snap = d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(card["id"])), {})
    assert "review_merge_running" not in snap


async def _armed(d, store, repo, monkeypatch):
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    _stub_close(d, monkeypatch)
    card = await _done_card(d, store, repo)
    await d.review_merge_card(card["id"])
    d._consults[card["id"]]["session_id"] = "rm-3"
    await d.answer_card_by_session("rm-3", _SHIP)
    assert card["id"] in d._review_merge_pending
    return card


@pytest.mark.asyncio
async def test_the_pending_merge_waits_for_the_terminal_then_fires_once(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _armed(d, store, repo, monkeypatch)
    tip = d._review_merge_pending[card["id"]]["tip"]
    # The reviewer's terminal is still inside the folder: nothing fires.
    monkeypatch.setattr(d, "_session_inside", lambda p: True)
    await d._flush_review_merge_pending()
    assert card["id"] in d._review_merge_pending
    assert store.get(card["id"])["merge_state"] == ""
    # It closes: the merge fires once with the reviewed tip.
    monkeypatch.setattr(d, "_session_inside", lambda p: False)
    calls: list = []
    real = d.merge_card

    async def spy(cid, expected_tip=None):
        calls.append((cid, expected_tip))
        return await real(cid, expected_tip=expected_tip)

    monkeypatch.setattr(d, "merge_card", spy)
    await d._flush_review_merge_pending()
    await _settle(d)
    assert calls == [(card["id"], tip)]
    assert card["id"] not in d._review_merge_pending
    assert store.get(card["id"])["merge_state"] == "merged"
    await d._flush_review_merge_pending()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_pending_merge_drops_when_the_tip_moved(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _armed(d, store, repo, monkeypatch)
    monkeypatch.setattr(d, "_session_inside", lambda p: False)
    wt = card["worktree_path"]
    with open(os.path.join(wt, "late.txt"), "w") as fh:
        fh.write("late\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "late")
    await d._flush_review_merge_pending()
    assert d._review_merge_pending == {}
    row = store.get(card["id"])
    assert row["merge_state"] == "blocked"
    assert row["merge_note"] == merges.REVIEW_MERGE_TIP_MOVED_NOTE


@pytest.mark.asyncio
async def test_the_pending_merge_drops_when_the_card_left_done(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _armed(d, store, repo, monkeypatch)
    store.update(card["id"], {"column_name": "backlog"})
    await d._flush_review_merge_pending()
    assert d._review_merge_pending == {}
    assert store.get(card["id"])["merge_state"] == ""


@pytest.mark.asyncio
async def test_the_pending_merge_times_out_in_words(daemon, repo, monkeypatch):
    d, store = daemon
    card = await _armed(d, store, repo, monkeypatch)
    monkeypatch.setattr(d, "_session_inside", lambda p: True)
    entry = dict(d._review_merge_pending[card["id"]])
    entry["since"] = time.time() - merges.REVIEW_MERGE_WAIT_SECONDS - 5
    d._review_merge_pending = {card["id"]: entry}
    await d._flush_review_merge_pending()
    assert d._review_merge_pending == {}
    row = store.get(card["id"])
    assert row["merge_state"] == "blocked"
    assert row["merge_note"] == merges.REVIEW_MERGE_TIMEOUT_NOTE
    assert "close it and press MERGE" in row["merge_note"]


def test_origin_and_title_know_the_new_press():
    assert "card-review-merge" in origin.KINDS
    assert origin.stamp("card-review-merge", "abc").startswith(
        "card-review-merge|")
    assert terminal_title.ORIGIN_PREFIX["card-review-merge"] \
        == "review+merge: "
    assert "to review and merge" in origin._VERBS["card-review-merge"]


def test_the_action_is_on_every_list_and_routes():
    verb = "board_review_merge"
    assert ApiServer._MERGE_PRESSES["review_merge"] == "review_merge_card"
    assert ApiServer.BOARD_ACTIONS.count(verb) == 1
    assert ApiServer.LAN_ACTIONS.count(verb) == 1
    assert ApiServer.REMOTE_ACTIONS.count(verb) == 1
    assert verb in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def _snap(d, store, cid):
    return d._decorate_card_for_snapshot(
        d._trim_card_for_snapshot(store.get(cid)), {})


@pytest.mark.asyncio
async def test_a_refused_spawn_does_not_record_the_check_passed(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)

    async def refuse(spawn_root, argv, name, **kw):
        return False, "no terminal", None

    monkeypatch.setattr(dispatch, "spawn", refuse)
    card, path = await _open_check_card(d, store, repo)
    ok, detail = await d.review_merge_card(card["id"])
    assert (ok, detail) == (False, "no terminal")
    assert merges.REVIEW_MERGE_CHECK_PASSED_NOTE not in open(path).read()
    assert store.get(card["id"])["manual_steps"] != ""
    assert card["id"] not in d._consults


@pytest.mark.asyncio
async def test_a_settle_refusal_after_the_spawn_keeps_the_check_open_in_words(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card, _path = await _open_check_card(d, store, repo)

    async def refuse(path, status, note=""):
        return False, "The check file changed under you."

    monkeypatch.setattr(d, "record_manual_outcome", refuse)
    ok, detail = await d.review_merge_card(card["id"])
    # The helper is running, so the press is ok; nothing is closed or cleared.
    assert ok and "The check file changed under you." in detail
    assert len(opened) == 1 and card["id"] in d._consults
    row = store.get(card["id"])
    assert row["manual_steps"] != ""
    assert "The check file changed under you." in row["merge_note"]


@pytest.mark.asyncio
async def test_only_the_place_refusal_falls_back_to_clearing_the_steps(
        daemon, repo, monkeypatch):
    from dark_army_daemon import board
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    card, _path = await _open_check_card(d, store, repo)

    async def outside(path, status, note=""):
        return False, board.MANUAL_CHECK_PLACE_REFUSAL

    monkeypatch.setattr(d, "record_manual_outcome", outside)
    ok, detail = await d.review_merge_card(card["id"])
    assert ok, detail
    assert store.get(card["id"])["manual_steps"] == ""


@pytest.mark.asyncio
async def test_the_flush_drops_silently_when_already_merged_or_merging(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _armed(d, store, repo, monkeypatch)
    cid = card["id"]
    monkeypatch.setattr(d, "_session_inside", lambda p: False)
    # A person's own merge is under way.
    d._merging = {cid: {"root": card["root"], "since": time.time(),
                        "token": "t"}}
    await d._flush_review_merge_pending()
    assert d._review_merge_pending == {}
    assert store.get(cid)["merge_state"] == ""
    assert store.get(cid)["merge_note"] == ""
    d._merging = {}
    # Merged meanwhile: nothing is written over it.
    d._review_merge_pending = {cid: {"tip": "a" * 40, "session_id": "",
                                     "since": time.time()}}
    store.record_merge(cid, "merged", "Merged by hand.")
    await d._flush_review_merge_pending()
    assert d._review_merge_pending == {}
    row = store.get(cid)
    assert row["merge_state"] == "merged" and row["merge_note"] == "Merged by hand."


@pytest.mark.asyncio
async def test_stop_never_overwrites_a_merged_state(daemon, repo, monkeypatch):
    d, store = daemon
    card = await _armed(d, store, repo, monkeypatch)
    store.record_merge(card["id"], "merged", "Merged.")
    await d._review_merge_stop(card["id"], "late stop")
    assert d._review_merge_pending == {}
    row = store.get(card["id"])
    assert row["merge_state"] == "merged" and row["merge_note"] == "Merged."


@pytest.mark.asyncio
async def test_merge_and_the_batch_refuse_while_a_review_merge_waits(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _armed(d, store, repo, monkeypatch)
    cid = card["id"]
    monkeypatch.setattr(d, "_session_inside", lambda p: False)
    tip = d._review_merge_pending[cid]["tip"]
    assert (await d.merge_card(cid))[1] == merges.REVIEW_MERGE_PENDING_REFUSAL
    ok, detail = await d.merge_cards([(cid, tip)])
    assert merges.REVIEW_MERGE_PENDING_REFUSAL in detail
    assert store.get(cid)["merge_state"] == ""
    snap = _snap(d, store, cid)
    assert snap["merge_offered"] is False
    assert snap["review_merge_offered"] is False
    assert d._review_merge_pending[cid]["tip"] == tip


@pytest.mark.asyncio
async def test_an_unreadable_tip_keeps_waiting_until_the_limit(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _armed(d, store, repo, monkeypatch)
    cid = card["id"]
    monkeypatch.setattr(d, "_session_inside", lambda p: False)

    async def unreadable(root, ref):
        return ""

    monkeypatch.setattr(d, "_merge_tip", unreadable)
    await d._flush_review_merge_pending()
    assert cid in d._review_merge_pending
    assert store.get(cid)["merge_state"] == ""
    entry = dict(d._review_merge_pending[cid])
    entry["since"] = time.time() - merges.REVIEW_MERGE_WAIT_SECONDS - 5
    d._review_merge_pending = {cid: entry}
    await d._flush_review_merge_pending()
    assert d._review_merge_pending == {}
    row = store.get(cid)
    assert row["merge_note"] == merges.REVIEW_MERGE_TIP_UNREAD_NOTE
    assert row["merge_note"] != merges.REVIEW_MERGE_TIP_MOVED_NOTE


# -- The card's own assistant finished but still sits in its terminal --

def _sitting(d, store, card, bucket="sleeping", sid="own-1"):
    """The card's finished assistant still open in its folder."""
    store.update(card["id"], {"link_state": "live", "session_id": sid},
                 bump=False)
    row = {"session_id": sid, "cwd": card["worktree_path"]}
    if bucket == "finished":
        row["alive"] = True
    d._agents_snapshot_cache = {bucket: [row]}
    return store.get(card["id"])


def _gone(d, store, card):
    store.update(card["id"], {"link_state": "ended"}, bump=False)
    d._agents_snapshot_cache = {}


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket", ["sleeping", "finished"])
async def test_review_merge_closes_a_finished_assistant_then_starts(
        daemon, repo, monkeypatch, bucket):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    closed = _stub_close(d, monkeypatch)
    card = await _done_card(d, store, repo)
    _sitting(d, store, card, bucket)
    ok, detail = await d.review_merge_card(card["id"])
    assert (ok, detail) == (True, merges.CLOSING_FINISHED_NOTE)
    assert closed == ["own-1"] and opened == []
    snap = _snap(d, store, card["id"])
    assert snap["review_merge_running"] is True
    assert snap["merge_line"] == merges.CLOSING_FINISHED_LINE
    assert snap["review_merge_offered"] is False
    # A second press while it closes changes nothing.
    assert (await d.review_merge_card(card["id"]))[1] \
        == merges.CLOSING_FINISHED_NOTE
    assert closed == ["own-1"]
    # Still open: nothing starts.
    await d._flush_card_press_waiting()
    assert opened == [] and card["id"] in d._card_press_waiting
    # Gone: the review starts once.
    _gone(d, store, card)
    await d._flush_card_press_waiting()
    assert len(opened) == 1
    assert d._consults[card["id"]]["purpose"] == "review_merge"
    assert card["id"] not in d._card_press_waiting
    await d._flush_card_press_waiting()
    assert len(opened) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket", ["running", "waiting"])
async def test_a_working_assistant_is_never_closed(
        daemon, repo, monkeypatch, bucket):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    closed = _stub_close(d, monkeypatch)
    card = await _done_card(d, store, repo)
    _sitting(d, store, card, bucket)
    assert await d.review_merge_card(card["id"]) \
        == (False, merges.CARD_LIVE_REFUSAL)
    assert await d.merge_card(card["id"]) \
        == (False, merges.CARD_LIVE_REFUSAL)
    assert closed == [] and opened == []
    assert not d._card_press_waiting


@pytest.mark.asyncio
async def test_a_refused_close_refuses_the_press_in_its_words(
        daemon, repo, monkeypatch):
    d, store = daemon
    card = await _done_card(d, store, repo)
    _sitting(d, store, card)

    async def refuse(session_id, **kw):
        return False, "It is asking a permission question."

    monkeypatch.setattr(d, "close_session_terminal", refuse)
    assert await d.review_merge_card(card["id"]) \
        == (False, "It is asking a permission question.")
    assert not d._card_press_waiting


@pytest.mark.asyncio
async def test_merge_closes_a_finished_assistant_then_merges(
        daemon, repo, monkeypatch):
    d, store = daemon
    closed = _stub_close(d, monkeypatch)
    card = await _done_card(d, store, repo)
    _sitting(d, store, card)
    ok, detail = await d.merge_card(card["id"])
    assert (ok, detail) == (True, merges.CLOSING_FINISHED_NOTE)
    assert closed == ["own-1"]
    snap = _snap(d, store, card["id"])
    assert snap["merge_offered"] is False
    assert "review_merge_running" not in snap
    _gone(d, store, card)
    await d._flush_card_press_waiting()
    await _settle(d)
    assert store.get(card["id"])["merge_state"] == "merged"
    assert not d._card_press_waiting


@pytest.mark.asyncio
async def test_a_terminal_that_never_closes_stops_in_words(
        daemon, repo, monkeypatch):
    d, store = daemon
    _stub_close(d, monkeypatch)
    card = await _done_card(d, store, repo)
    _sitting(d, store, card)
    await d.review_merge_card(card["id"])
    d._card_press_waiting[card["id"]]["since"] = (
        time.time() - merges.CLOSING_FINISHED_WAIT_SECONDS - 1)
    await d._flush_card_press_waiting()
    assert not d._card_press_waiting
    row = store.get(card["id"])
    assert row["merge_state"] == "blocked"
    assert row["merge_note"] == merges.CLOSING_FINISHED_TIMEOUT_NOTE


@pytest.mark.asyncio
async def test_three_presses_in_one_project_each_start_in_turn(
        daemon, repo, monkeypatch):
    """The 15 Nov report: three Review & merge presses, one review ran and
    another stopped on "another card in this project is already starting".
    Each now waits its turn and starts once the one before has bound."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    closed = _stub_close(d, monkeypatch)
    cards = [await _done_card(d, store, repo, title=f"card {n}",
                              file=f"f{n}.txt") for n in range(3)]
    rows = []
    for n, card in enumerate(cards):
        sid = f"own-{n}"
        store.update(card["id"], {"link_state": "live", "session_id": sid},
                     bump=False)
        rows.append({"session_id": sid, "cwd": card["worktree_path"]})
    d._agents_snapshot_cache = {"sleeping": rows}
    for card in cards:
        assert (await d.review_merge_card(card["id"]))[0] is True
    assert sorted(closed) == ["own-0", "own-1", "own-2"]
    for card in cards:
        store.update(card["id"], {"link_state": "ended"}, bump=False)
    d._agents_snapshot_cache = {}
    started: list = []
    for _round in range(3):
        await d._flush_card_press_waiting()
        # The helper that just started binds, freeing the project.
        for cid, entry in d._consults.items():
            if not entry.get("session_id"):
                entry["session_id"] = f"bound-{cid[:4]}"
                started.append(cid)
    assert len(opened) == 3
    assert sorted(started) == sorted(c["id"] for c in cards)
    assert not d._card_press_waiting
    for card in cards:
        assert store.get(card["id"])["merge_state"] in ("", None)


@pytest.mark.asyncio
async def test_a_queued_review_says_so_and_gives_up_after_the_limit(
        daemon, repo, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, repo, opened)
    first = await _done_card(d, store, repo, title="one", file="a.txt")
    second = await _done_card(d, store, repo, title="two", file="b.txt")
    assert (await d.review_merge_card(first["id"]))[0] is True
    ok, detail = await d.review_merge_card(second["id"])
    assert (ok, detail) == (True, merges.REVIEW_MERGE_QUEUED_NOTE)
    snap = _snap(d, store, second["id"])
    assert snap["merge_line"] == merges.REVIEW_MERGE_QUEUED_LINE
    assert snap["review_merge_running"] is True
    d._card_press_waiting[second["id"]]["since"] = (
        time.time() - merges.REVIEW_MERGE_WAIT_SECONDS - 1)
    await d._flush_card_press_waiting()
    assert not d._card_press_waiting
    assert store.get(second["id"])["merge_state"] == "blocked"
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_the_done_view_token_moves_with_merge_work(
        daemon, repo, monkeypatch):
    """The 15 Nov phone report: merged cards still drew the merge icon,
    because the phone's held Done column refetches only when the view token
    moves, and a merge (no revision step) never moved it."""
    d, store = daemon
    _stub_spawn(d, monkeypatch, repo, [])
    card = await _done_card(d, store, repo)
    cid = card["id"]

    def token():
        return d._done_view_now(store.done_tokens()[2])

    t0 = token()
    store.record_merge(cid, "merged", "Merged.")
    t1 = token()
    assert t1 != t0
    store.clear_worktree(cid, branch=True)
    t2 = token()
    assert t2 != t1
    d._card_press_waiting = {cid: {"verb": "review_merge", "why": "queued"}}
    t3 = token()
    assert t3 != t2
    d._card_press_waiting = {}
    assert token() == t2
    # The frame and the archive page stamp the same token, or a phone
    # would refetch the archive on every frame.
    d._card_press_waiting = {cid: {"verb": "merge"}}
    assert d._build_board_state()["done_view_token"] \
        == d.done_archive_cards()["done_view_token"] == token()


@pytest.mark.asyncio
async def test_a_new_review_merge_clears_an_earlier_stop(
        daemon, repo, monkeypatch):
    d, store = daemon
    _stub_spawn(d, monkeypatch, repo, [])
    card = await _done_card(d, store, repo)
    store.record_merge(card["id"], "blocked", "Stopped: something earlier")
    ok, _detail = await d.review_merge_card(card["id"])
    assert ok
    assert store.get(card["id"])["merge_state"] == ""
    assert "merge_line" not in _snap(d, store, card["id"])
