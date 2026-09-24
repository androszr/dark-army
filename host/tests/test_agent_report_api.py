# host/tests/test_agent_report_api.py
"""How the agent report is served: `/api/history` and the sealed read.

The sealed half uses `test_home_seal`'s free-port fixtures, so nothing here
binds a fixed port or reaches the running fleet's files.
"""

from __future__ import annotations

import json

import pytest

from dark_army_daemon import devices, relay
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.history import HistoryStore
from tests.test_home_seal import (  # noqa: F401  (fixtures)
    server, ledger, machine, attach_dir, _no_fleet_snapshot,
    pair_plain, inner, fetch,
)


def _history(daemon, tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.connect()
    import time as _time
    store.add_turn("s1", _time.time(), message_id="m1",
                   model="claude-sonnet-4-6", output_tokens=500,
                   input_tokens=1000, cache_read=1000, agent_id="a1",
                   attr_agent="bc-implementer", duration_ms=1200)
    store.upsert_session("s1", project="proj", cost_usd=1.0)
    daemon._history = store
    return store


# --- the loopback report -----------------------------------------------------


@pytest.mark.asyncio
async def test_history_carries_the_agent_sections(server, tmp_path):
    api, daemon, _lan = server
    store = _history(daemon, tmp_path)
    try:
        status, body = await fetch("/api/history?range=30d", port=api._port)
        report = json.loads(body)
        assert status == 200 and report["available"] is True
        assert set(report) >= {"by_agent", "top_dispatches", "effectiveness"}
        assert report["by_agent"][0]["name"] == "bc-implementer"
        assert report["by_agent"][0]["dispatches"] == 1
        assert report["by_agent"][0]["cache_hit_ratio"] == pytest.approx(0.5)
        assert report["top_dispatches"][0]["agent_id"] == "a1"
        # The effectiveness half is present even with no board: absent would
        # be indistinguishable from a daemon that cannot compute it.
        assert report["effectiveness"]["supported"] is True
    finally:
        store.close()
        daemon._history = None


@pytest.mark.asyncio
async def test_history_joins_a_card_to_the_session_that_worked_it(server, tmp_path):
    api, daemon, _lan = server
    store = _history(daemon, tmp_path)
    board = BoardStore(tmp_path / "board.db")
    board.connect()
    daemon._board = board
    try:
        card, error = board.create(dict(title="Ship it", root=str(tmp_path),
                                        tool="claude",
                                        intended_benefit="a benefit",
                                        success_criterion="a criterion"))
        assert error == "created"
        board.record_outcome_run(card["id"], "claude", "s1", "implementation")
        status, body = await fetch(
            f"/api/history?range=30d&root={tmp_path}", port=api._port)
        effectiveness = json.loads(body)["effectiveness"]
        assert status == 200 and effectiveness["available"] is True
        row = [c for c in effectiveness["cards"] if c["card_id"] == card["id"]][0]
        assert row["cost_usd"] > 0, "the session's spend sits on the card's row"
        assert row["partial"] is False
        assert effectiveness["summary"]["joined_sessions"] == 1
        assert effectiveness["outcomes_available"] is True
    finally:
        daemon._board = None
        board.close()
        store.close()
        daemon._history = None


@pytest.mark.asyncio
async def test_history_still_refuses_an_unknown_range(server):
    api, _daemon, _lan = server
    status, body = await fetch("/api/history?range=since-tuesday", port=api._port)
    assert status == 400 and "allowed" in json.loads(body)


@pytest.mark.asyncio
async def test_the_session_branch_does_not_carry_the_aggregate_sections(
        server, tmp_path):
    api, daemon, _lan = server
    store = _history(daemon, tmp_path)
    try:
        status, body = await fetch("/api/history?session=s1", port=api._port)
        report = json.loads(body)
        assert status == 200 and report["session"]["session_id"] == "s1"
        for key in ("by_agent", "top_dispatches", "effectiveness"):
            assert key not in report, key
    finally:
        store.close()
        daemon._history = None


# --- the sealed read ---------------------------------------------------------


@pytest.mark.asyncio
async def test_sealed_agent_report_answers_on_both_doors(server, tmp_path,
                                                         monkeypatch):
    api, daemon, lan_port = server
    store = _history(daemon, tmp_path)
    try:
        phone = await pair_plain(lan_port)
        did = devices.device_for_home_channel(
            relay.channel_id(phone["key"], ns=relay.HOME))
        # A lapsed away window bounds what the phone may *do*, never what it
        # may see: this is a read, so it must still answer.
        monkeypatch.setattr(relay, "lease_valid", lambda _: False)
        before = len(daemon._remote_activity)

        status, body = await inner(lan_port, phone["key"], "agent_report",
                                   {"query": "range=30d"}, 1)
        home = json.loads(body)
        assert status == 200 and home["supported"] is True

        status, _ctype, away = await api._remote_run(
            "agent_report", {"query": "range=30d"}, did)
        assert status == 200
        assert json.loads(away)["agents"] == home["agents"]
        assert len(daemon._remote_activity) == before, "a read records nothing"
    finally:
        store.close()
        daemon._history = None


@pytest.mark.asyncio
async def test_sealed_agent_report_refuses_a_bad_range(server):
    _api, _daemon, lan_port = server
    phone = await pair_plain(lan_port)
    status, body = await inner(lan_port, phone["key"], "agent_report",
                               {"query": "range=fortnight"}, 1)
    assert status == 400 and "error" in json.loads(body)


@pytest.mark.asyncio
async def test_an_unknown_kind_is_still_not_found(server):
    _api, _daemon, lan_port = server
    phone = await pair_plain(lan_port)
    status, out = await inner(lan_port, phone["key"], "agent_reports",
                              {"query": "range=30d"}, 1)
    assert status == 404


# --- the bound ---------------------------------------------------------------


def test_an_oversize_report_is_trimmed_at_one_offset():
    report = {
        "supported": True, "available": True,
        "agents": [{"name": "bc-implementer", "note": "x" * 400}
                   for _ in range(400)],
        "cards": [{"card_id": str(n), "title": "y" * 400} for n in range(400)],
    }
    body = ApiServer._agent_report_page_bytes(report, 0)
    assert len(body) <= 300_000
    page = json.loads(body)
    assert page["next_offset"] > 0
    # Both collections reduced together, or a page skips rows on one of them.
    assert len(page["agents"]) == len(page["cards"]) == page["next_offset"]


def test_the_trim_says_which_collection_it_actually_reached():
    """A shared offset is not a shared cut.

    `cards` is routinely the long collection and the phone never draws it, so
    a client that read a bare `next_offset` as "your helper list was cut"
    would say so about a complete list. Absent means whole.
    """
    report = {
        "supported": True, "available": True,
        "agents": [{"name": f"a{n}"} for n in range(3)],
        "cards": [{"card_id": str(n), "title": "y" * 900} for n in range(600)],
    }
    page = json.loads(ApiServer._agent_report_page_bytes(report, 0))
    assert page["next_offset"] > 0
    assert page["cards_truncated"] is True
    assert "agents_truncated" not in page, "the helper list was never cut"
    assert len(page["agents"]) == 3


def test_a_trimmed_helper_list_says_it_was_trimmed():
    report = {
        "supported": True, "available": True,
        "agents": [{"name": f"a{n}", "note": "x" * 900} for n in range(600)],
        "cards": [],
    }
    page = json.loads(ApiServer._agent_report_page_bytes(report, 0))
    assert page["agents_truncated"] is True


def test_a_report_that_cannot_be_trimmed_says_so():
    report = {"supported": True, "available": True,
              "agents": [{"name": "z" * 400_000}], "cards": []}
    page = json.loads(ApiServer._agent_report_page_bytes(report, 0))
    assert page["available"] is False and page["reason"]


# --- a refusal that refuses -------------------------------------------------


@pytest.mark.asyncio
async def test_an_over_long_root_is_refused_rather_than_answered_machine_wide(
        server, tmp_path):
    """The sealed sibling raises; this door used to blank the field.

    Blanking answers **every project** under the caption of the one that was
    asked for, which is a refusal that fails open. One rule, both doors.
    """
    api, daemon, _lan = server
    store = _history(daemon, tmp_path)
    try:
        status, body = await fetch(
            "/api/history?range=30d&root=" + "x" * 1100, port=api._port)
        assert status == 400
        assert "1024" in json.loads(body)["error"]
    finally:
        store.close()
        daemon._history = None


@pytest.mark.asyncio
async def test_the_sealed_sibling_refuses_the_same_root(server):
    _api, _daemon, lan_port = server
    phone = await pair_plain(lan_port)
    status, _out = await inner(lan_port, phone["key"], "agent_report",
                               {"query": "range=30d&root=" + "x" * 1100}, 1)
    assert status == 400


# --- the version marker ------------------------------------------------------


def test_pipeline_writable_carries_the_version_marker():
    from dark_army_daemon.daemon import BobDaemon

    assert BobDaemon._pipeline_writable(BobDaemon())["agent_report_supported"] is True


def test_pipeline_writable_carries_the_live_activity_marker():
    """`live_activity_supported`: this daemon takes `register_activity_token`
    and pushes Live Activity updates. A literal True under the
    never-a-missing-key rule; the phone decodes absence as false and posts
    no token against an older Mac."""
    from dark_army_daemon.daemon import BobDaemon

    assert BobDaemon._pipeline_writable(BobDaemon())["live_activity_supported"] is True


def test_pipeline_writable_carries_the_fleet_card_marker():
    """`live_activity_fleet`: this daemon publishes the fleet Lock Screen
    card. A literal True; an older Mac sends no key, which the phone
    decodes false and answers with the face-only card."""
    from dark_army_daemon.daemon import BobDaemon

    assert BobDaemon._pipeline_writable(BobDaemon())["live_activity_fleet"] is True


def test_agent_report_is_a_read_and_never_an_action():
    assert "agent_report" not in ApiServer.LAN_ACTIONS
    assert "agent_report" not in ApiServer.REMOTE_ACTIONS


@pytest.mark.asyncio
async def test_a_project_with_no_runs_lists_no_helpers(server, tmp_path):
    """The helper table under a project heading is that project's own work.

    An empty session filter passed on as "no filter" is how every helper on
    the Mac ends up listed under a project that ran none of them — and how
    this report and the machine-wide `by_agent` beside it come to disagree
    for the same range.
    """
    api, daemon, _lan = server
    store = _history(daemon, tmp_path)
    board = BoardStore(tmp_path / "board.db")
    board.connect()
    daemon._board = board
    empty = tmp_path / "quiet-project"
    empty.mkdir()
    try:
        status, body = await fetch(
            f"/api/history?range=30d&root={empty}", port=api._port)
        effectiveness = json.loads(body)["effectiveness"]
        assert status == 200
        assert effectiveness["agent_scope"] == "project"
        assert effectiveness["agents"] == [], "no runs here, so no helpers"
        # …while the machine-wide report still knows about the same turn.
        status, body = await fetch("/api/history?range=30d", port=api._port)
        machine_wide = json.loads(body)["effectiveness"]
        assert machine_wide["agent_scope"] == "machine"
        assert [row["name"] for row in machine_wide["agents"]] == ["bc-implementer"]
    finally:
        daemon._board = None
        board.close()
        store.close()
        daemon._history = None


@pytest.mark.asyncio
async def test_manual_checks_are_counted_inside_the_project_asked_about(
        server, tmp_path):
    """A chore on another project's card is not this project's chore."""
    api, daemon, _lan = server
    store = _history(daemon, tmp_path)
    board = BoardStore(tmp_path / "board.db")
    board.connect()
    daemon._board = board
    other = tmp_path / "elsewhere"
    other.mkdir()
    try:
        mine, _ = board.create(dict(title="Mine", root=str(tmp_path),
                                    tool="claude"))
        theirs, _ = board.create(dict(title="Theirs", root=str(other),
                                      tool="claude"))
        board.record_outcome_run(mine["id"], "claude", "s1", "implementation")
        # `manual_steps` is `flag_manual`'s alone, so each card is flagged by
        # its own bound session — the only way the store lets it be written.
        for card, sid in ((mine, "s1"), (theirs, "s9")):
            board.update(card["id"], {"session_id": sid, "link_state": "live"})
            _row, detail = board.flag_manual(card["id"], sid,
                                             "1. Open the panel.")
            assert _row is not None, detail
        status, body = await fetch(
            f"/api/history?range=30d&root={tmp_path}", port=api._port)
        summary = json.loads(body)["effectiveness"]["summary"]
        assert status == 200
        assert summary["manual_checks_outstanding"] == 1, (
            "the other project's chore is not this project's")
        status, body = await fetch("/api/history?range=30d", port=api._port)
        assert json.loads(body)["effectiveness"]["summary"][
            "manual_checks_outstanding"] == 2
    finally:
        daemon._board = None
        board.close()
        store.close()
        daemon._history = None


@pytest.mark.asyncio
async def test_the_sealed_offset_is_a_real_slice(server, monkeypatch):
    """`offset` moves the page, or a client following `next_offset` loops.

    Validating an offset and then paging from the top for ever is worse than
    not offering one: the wire says "there is more, ask from here" and then
    answers page one to the asking.
    """
    api, daemon, _lan = server

    async def report(days, root=""):
        return {"supported": True, "available": True,
                "agents": [{"name": f"a{n}"} for n in range(4)],
                "cards": [{"card_id": str(n)} for n in range(4)]}

    monkeypatch.setattr(daemon, "agent_efficiency_report", report,
                        raising=False)
    status, _ctype, body = await api._agent_report_for("range=30d&offset=2")
    page = json.loads(body)
    assert status == 200 and page["offset"] == 2
    assert [row["name"] for row in page["agents"]] == ["a2", "a3"]
    assert [row["card_id"] for row in page["cards"]] == ["2", "3"]


@pytest.mark.asyncio
async def test_a_following_page_starts_where_the_last_one_stopped(server,
                                                                  monkeypatch):
    api, daemon, _lan = server
    rows = 400

    async def report(days, root=""):
        return {"supported": True, "available": True,
                "agents": [{"name": f"a{n}", "note": "x" * 400}
                           for n in range(rows)],
                "cards": [{"card_id": str(n), "title": "y" * 400}
                          for n in range(rows)]}

    monkeypatch.setattr(daemon, "agent_efficiency_report", report,
                        raising=False)
    _status, _ctype, body = await api._agent_report_for("range=30d")
    first = json.loads(body)
    assert first["next_offset"] > 0
    _status, _ctype, body = await api._agent_report_for(
        f"range=30d&offset={first['next_offset']}")
    second = json.loads(body)
    assert second["agents"][0]["name"] != first["agents"][0]["name"]
    assert second["agents"][0]["name"] == f"a{first['next_offset']}"
