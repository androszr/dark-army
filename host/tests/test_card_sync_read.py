# host/tests/test_card_sync_read.py
"""The `card_sync` delta read: the kind on both doors, its paging, its plan
containment and its bounds.

It is a **kind**, not an action, for the reason `log` is one: reads are not
what the away lease bounds, and putting it on an action tuple would make a
read look like a write on both doors. The two write tuples do not grow.

Its per-card body is `ApiServer._card_collect` — the same reader the `card`
kind uses — deliberately, so there is exactly one place a card and its plan
are read and exactly one containment rule
(`BoardVerbsMixin._plan_path_refusal`).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from dark_army_daemon import board_workflow, relay
from dark_army_daemon.api_server import (CARD_SYNC_MAX_BYTES,
                                             CARD_SYNC_MAX_IDS, ApiServer)

# The sealed-door fixtures already exist; reuse them rather than a second copy.
from tests.test_lan_access import (  # noqa: F401  (fixtures)
    _home_post, _pair_plain, ledger, machine, ports, server, token_path,
    _no_fleet_snapshot)


def _plan_card(daemon, tmp_path, *, name="proj", text="# A plan\n\nDo it.\n",
               attach=True, store=None):
    from dark_army_daemon.board import BoardStore
    root = tmp_path / name
    (root / "plans").mkdir(parents=True, exist_ok=True)
    plan = root / "plans" / "p.md"
    plan.write_text(text)
    if store is None:
        store = BoardStore(tmp_path / "board.db")
        store.connect()
        daemon._board = store
    card, detail = store.create({
        "title": "t", "project": name, "root": str(root),
        "prompt": "x" * 900, "tool": "claude", "summary": "s"})
    assert card is not None, detail
    if attach:
        card, detail = store.attach_plan(card["id"], str(plan), "sess-1")
        assert card is not None, detail
    return store, card, plan


def _digest(path) -> str:
    return hashlib.sha256(
        board_workflow.read_plan_text(path).encode("utf-8")).hexdigest()


# --- the door ---------------------------------------------------------------


def test_card_sync_is_on_neither_action_tuple():
    """A read, so neither write ring grows — and `REMOTE_ACTIONS` still does
    not exceed `LAN_ACTIONS`."""
    assert "card_sync" not in ApiServer.LAN_ACTIONS
    assert "card_sync" not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_the_plan_read_adds_no_second_containment_rule():
    """Every plan byte served to the phone goes through the daemon's own
    `_plan_path_refusal` and `board_workflow.read_plan_text` and through
    nothing else. A second rule written in the HTTP layer is how they drift.
    """
    import inspect
    src = inspect.getsource(ApiServer._cards_sync_page)
    # The docstring names both rules; the code must call neither.
    body = src.split('"""')[2]
    assert "realpath" not in body
    assert "read_plan_text" not in body
    assert "open(" not in body
    assert "_card_collect" in body


@pytest.mark.asyncio
async def test_the_kind_answers_at_home_and_away(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    store, card, plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"ids={card['id']}"}, 1)
        assert status == 200
        page = json.loads(body)
        assert page["available"] is True
        assert page["cards"][0]["prompt"] == "x" * 900
        assert page["cards"][0]["revision"] == card["revision"]
        assert page["plans"][0]["available"] is True
        assert page["plans"][0]["text"] == plan.read_text()
        assert page["more"] is False
        assert page["unserved"] == []

        # And away, on the same read, with no lease armed at all: expiry
        # bounds what the phone may *do*, never what it may see.
        status, _ctype, out = await srv._remote_run(
            "card_sync", {"query": f"ids={card['id']}"}, paired["device_id"])
        assert status == 200
        assert json.loads(out)["cards"][0]["id"] == card["id"]
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_a_lapsed_lease_still_serves_the_read(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    daemon.remote_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        # End the away window outright: a *write* would refuse in
        # `LEASE_REFUSAL`'s words from here, and the read must not.
        relay.set_lease_days(paired["device_id"], 0)
        assert relay.lease_valid(paired["device_id"]) is False
        status, _ctype, out = await srv._remote_run(
            "card_sync", {"query": f"ids={card['id']}"}, paired["device_id"])
        assert status == 200
        assert relay.LEASE_REFUSAL not in out.decode()
    finally:
        store.close()
        daemon._board = None


# --- the query --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_id_is_dropped_rather_than_fatal(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"ids={card['id']},nosuchcard"}, 1)
        assert status == 200
        page = json.loads(body)
        assert [c["id"] for c in page["cards"]] == [card["id"]]
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_too_many_ids_is_a_400_in_words(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    ids = ",".join(f"id{n}" for n in range(CARD_SYNC_MAX_IDS + 1))
    status, body = await _home_post(
        lan_port, paired["key"], "card_sync", {"query": f"ids={ids}"}, 1)
    assert status == 400
    assert str(CARD_SYNC_MAX_IDS) in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_a_repeated_key_and_an_overlong_id_are_400s(server):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    paired = await _pair_plain(lan_port)
    status, _body = await _home_post(
        lan_port, paired["key"], "card_sync", {"query": "ids=a&ids=b"}, 1)
    assert status == 400
    status, _body = await _home_post(
        lan_port, paired["key"], "card_sync",
        {"query": "ids=" + "z" * 201}, 2)
    assert status == 400


@pytest.mark.asyncio
async def test_a_malformed_plan_stamp_is_dropped_never_fatal(server, tmp_path):
    """One bad entry must not cost the phone a whole page."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"ids={card['id']}&plans=junk,also:notadigest"}, 1)
        assert status == 200
        assert json.loads(body)["cards"][0]["id"] == card["id"]
    finally:
        store.close()
        daemon._board = None


# --- the plan ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_matching_digest_omits_the_plan_and_a_differing_one_sends_it(
        server, tmp_path):
    """The whole cheapness of the re-check: 50 unchanged plans cost 50 stats
    and zero bytes on the wire."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        held = _digest(plan)
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"ids={card['id']}&plans={card['id']}:{held}"}, 1)
        assert status == 200
        item = json.loads(body)["plans"][0]
        assert item["unchanged"] is True
        assert item["text"] == ""
        assert item["available"] is True

        other = "b" * 64
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"ids={card['id']}&plans={card['id']}:{other}"}, 2)
        item = json.loads(body)["plans"][0]
        assert item.get("unchanged", False) is False
        assert item["text"] == plan.read_text()
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_a_plan_stamp_alone_re_checks_without_asking_for_the_card(
        server, tmp_path):
    """A plan file edited under a card whose fields never moved is invisible
    any other way — the Mac does not watch those files."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, plan = _plan_card(daemon, tmp_path)
    try:
        paired = await _pair_plain(lan_port)
        held = _digest(plan)
        plan.write_text("# A plan\n\nSomething else entirely.\n")
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"plans={card['id']}:{held}"}, 1)
        assert status == 200
        page = json.loads(body)
        assert page["cards"] == []
        assert page["plans"][0]["text"] == plan.read_text()
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
@pytest.mark.parametrize("break_it,expected", [
    ("missing", "there is no file at that path"),
    ("outside", "the plan must be a file inside the card's own project"),
    ("not_md", "a plan is a Markdown (.md) file"),
    ("oversize", "that file is too large to be a plan"),
])
async def test_an_unservable_plan_says_why_in_the_daemons_words(
        server, tmp_path, break_it, expected):
    """Stated, never inferred from empty text — and the words are the
    containment rule's own, not a second sentence written here."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, plan = _plan_card(daemon, tmp_path)
    try:
        if break_it == "missing":
            plan.unlink()
        elif break_it == "outside":
            outside = tmp_path / "elsewhere.md"
            outside.write_text("# not yours\n")
            store._conn.execute("UPDATE cards SET plan_path = ? WHERE id = ?",
                                (str(outside), card["id"]))
            store._conn.commit()
        elif break_it == "not_md":
            other = plan.parent / "p.txt"
            other.write_text("# nope\n")
            store._conn.execute("UPDATE cards SET plan_path = ? WHERE id = ?",
                                (str(other), card["id"]))
            store._conn.commit()
        else:
            plan.write_text("x" * (board_workflow.MAX_PLAN_BYTES + 10))
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"ids={card['id']}"}, 1)
        assert status == 200
        item = json.loads(body)["plans"][0]
        assert item["available"] is False
        assert item["reason"] == expected
        assert item["text"] == ""
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_a_card_with_no_plan_says_so(server, tmp_path):
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    store, card, _plan = _plan_card(daemon, tmp_path, attach=False)
    try:
        paired = await _pair_plain(lan_port)
        status, body = await _home_post(
            lan_port, paired["key"], "card_sync",
            {"query": f"ids={card['id']}"}, 1)
        assert status == 200
        item = json.loads(body)["plans"][0]
        assert item["available"] is False
        assert item["reason"] == "this card has no plan"
    finally:
        store.close()
        daemon._board = None


# --- the budget -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_page_past_the_budget_sets_more_and_lists_unserved(
        server, tmp_path):
    """`_outcome_page_bytes`' 300 KB, for its reason. A single item always
    fits — a plan is at most 64 KiB and a prompt at most 8 000 characters —
    so there is no degenerate 'cannot serve one row' case."""
    srv, daemon, (_loop_port, lan_port) = server
    daemon.lan_access_enabled = True
    await srv.start_lan()
    from dark_army_daemon.board import BoardStore
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        ids = []
        for n in range(8):
            _s, card, _plan = _plan_card(
                daemon, tmp_path, name=f"proj{n}",
                text="y" * (board_workflow.MAX_PLAN_BYTES - 10), store=store)
            ids.append(card["id"])
        page = srv._cards_sync_page(ids, {}, CARD_SYNC_MAX_BYTES)
        assert page["more"] is True
        assert page["unserved"]
        assert len(page["cards"]) < len(ids)
        assert len(json.dumps(page).encode()) <= CARD_SYNC_MAX_BYTES + 70_000
        # Nothing is lost: served plus unserved is every id asked for.
        served = [c["id"] for c in page["cards"]]
        assert set(served) | set(page["unserved"]) == set(ids)
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_unserved_names_each_id_once_and_never_a_served_one(
        server, tmp_path):
    """The paging contract: an id asked for in both `ids` and `plans` is
    finished by the first loop, so the second may neither serve its plan
    behind an unserved card row nor list it twice."""
    srv, daemon, (_loop_port, _lan_port) = server
    from dark_army_daemon.board import BoardStore
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        ids = []
        for n in range(8):
            _s, card, _plan = _plan_card(
                daemon, tmp_path, name=f"proj{n}",
                text="y" * (board_workflow.MAX_PLAN_BYTES - 10), store=store)
            ids.append(card["id"])
        stamps = {cid: "c" * 64 for cid in ids}
        page = srv._cards_sync_page(ids, stamps, CARD_SYNC_MAX_BYTES)
        assert page["more"] is True
        assert len(page["unserved"]) == len(set(page["unserved"]))
        served = {c["id"] for c in page["cards"]}
        assert served.isdisjoint(page["unserved"])
        assert {p["card_id"] for p in page["plans"]} <= served
        assert served | set(page["unserved"]) == set(ids)
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_one_full_sized_plan_always_fits(server, tmp_path):
    srv, daemon, (_loop_port, _lan_port) = server
    store, card, _plan = _plan_card(
        daemon, tmp_path, text="z" * (board_workflow.MAX_PLAN_BYTES - 10))
    try:
        page = srv._cards_sync_page([card["id"]], {}, CARD_SYNC_MAX_BYTES)
        assert page["more"] is False
        assert len(page["cards"]) == 1
        assert page["plans"][0]["available"] is True
        assert len(json.dumps(page).encode()) < CARD_SYNC_MAX_BYTES
    finally:
        store.close()
        daemon._board = None
