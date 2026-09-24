"""Loopback and sealed lifecycle reads: identity, paging, authority."""
from __future__ import annotations

import json
import time

import pytest

from dark_army_daemon import devices, relay
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from tests.test_home_seal import (  # noqa: F401
    server, ledger, machine, attach_dir, _no_fleet_snapshot,
    pair_plain, inner, fetch,
)


def _seed(store, n=3):
    cards = []
    for i in range(n):
        c, err = store.create(dict(
            title=f"Card {i}", root="/project", tool="claude",
            intended_benefit="B", success_criterion="C"))
        assert err == "created"
        store.update(c["id"], {
            "link_state": "dispatching", "dispatched_at": 1_000 + i,
            "queue_state": "",
        }, bump=False)
        cards.append(c)
    return cards


@pytest.mark.asyncio
async def test_loopback_home_away_identical_summaries(server, tmp_path, monkeypatch):
    api, daemon, port = server
    store = BoardStore(tmp_path / "life.db")
    store.connect()
    daemon._board = store
    try:
        _seed(store)
        now = time.time()
        query = f"root=/project&from={now - 60}&to={now + 60}"
        status, local = await fetch("/api/lifecycle?" + query, port=api._port)
        assert status == 200
        home_body = json.loads(local)
        assert home_body["available"] is True
        assert home_body["summaries"]["queue"]["n"] == 3

        phone = await pair_plain(port)
        did = devices.device_for_home_channel(
            relay.channel_id(phone["key"], ns=relay.HOME))
        monkeypatch.setattr(relay, "lease_valid", lambda _: False)
        status, sealed = await inner(
            port, phone["key"], "lifecycle", {"query": query}, 1)
        assert status == 200
        home = json.loads(sealed)
        status, ctype, away = await api._remote_run(
            "lifecycle", {"query": query}, did)
        assert status == 200
        assert json.loads(away)["summaries"] == home["summaries"] == home_body["summaries"]
        # Expired lease still reads; no writer-list expansion.
        status, _ = await inner(
            port, phone["key"], "action",
            {"action": "board_accept_outcome", "card_id": "x"}, 2)
        assert status == 404
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "root=/project&limit=101",
    "root=/project&limit=0",
    "root=/project&offset=-1",
    "root=/project&from=NaN",
    "root=/project&to=inf",
    "root=/project&from=4&to=3",
    "root=/project&limit=1&limit=2",
    "root=/project&card=abc",
    "root=/project&from=0&to=" + str(400 * 86400),
    "",
])
async def test_report_bounds(server, query):
    api, _, _ = server
    assert (await api._lifecycle_report_for(query))[0] == 400


@pytest.mark.asyncio
async def test_page_size_invariance_and_generation(server, tmp_path):
    api, daemon, _ = server
    store = BoardStore(tmp_path / "life.db")
    store.connect()
    daemon._board = store
    try:
        _seed(store, 5)
        now = time.time()
        q = f"root=/project&from={now - 60}&to={now + 60}"
        status, _, a = await api._lifecycle_report_for(q + "&limit=2")
        status, _, b = await api._lifecycle_report_for(q + "&limit=25")
        ra, rb = json.loads(a), json.loads(b)
        assert ra["summaries"]["queue"]["n"] == rb["summaries"]["queue"]["n"]
        assert ra["summaries"]["queue"]["N"] == rb["summaries"]["queue"]["N"]
        assert len(ra["cards"]) == 2
        gen = ra["generation"]
        store.update(store.cards()[0]["id"], {
            "queue_state": "queued", "queued_at": now})
        status, _, body = await api._lifecycle_report_for(
            q + f"&generation={gen}")
        assert json.loads(body)["available"] is False
        assert "refresh" in json.loads(body)["reason"]
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_removed_card_detail(server, tmp_path):
    api, daemon, _ = server
    store = BoardStore(tmp_path / "life.db")
    store.connect()
    daemon._board = store
    try:
        cards = _seed(store, 1)
        cid = cards[0]["id"]
        store.delete(cid)
        now = time.time()
        status, _, body = await api._lifecycle_report_for(
            f"card={cid}&from={now - 60}&to={now + 60}")
        report = json.loads(body)
        assert status == 200
        assert report["live"] is False
        assert report["removed_note"] == "No longer on the board"
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_payload_cap_shrinks_collection(server, tmp_path):
    api, daemon, _ = server
    store = BoardStore(tmp_path / "life.db")
    store.connect()
    daemon._board = store
    try:
        c, _ = store.create(dict(
            title="Huge", root="/project", tool="claude",
            intended_benefit="B", success_criterion="C"))
        # Inflate the report with many delayed-card rows via the page helper.
        report = {
            "supported": True, "available": True, "card_id": "",
            "summaries": {"queue": {"n": 1}},
            "cards": [{"card_id": f"c{i}", "title": "x" * 2000} for i in range(400)],
            "episodes": [],
        }
        body = ApiServer._lifecycle_page_bytes(report, 0)
        assert len(body) <= 300_000
        parsed = json.loads(body)
        if parsed.get("available") is False:
            assert "limit" in parsed.get("reason", "").lower() or parsed.get("reason")
        else:
            assert parsed.get("next_offset") is not None or len(parsed["cards"]) < 400
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_no_remote_action_entry(server, tmp_path, monkeypatch):
    api, daemon, port = server
    store = BoardStore(tmp_path / "life.db")
    store.connect()
    daemon._board = store
    recorded = []
    daemon.record_remote_action = lambda *a: recorded.append(a)
    try:
        _seed(store, 1)
        phone = await pair_plain(port)
        await inner(port, phone["key"], "lifecycle",
                    {"query": "root=/project&from=1000&to=2000"}, 1)
        assert recorded == []
        assert "lifecycle" not in api.LAN_ACTIONS
        assert "lifecycle" not in api.REMOTE_ACTIONS
    finally:
        store.close()
        daemon._board = None


@pytest.mark.asyncio
async def test_collector_unavailable_is_stated(server, tmp_path):
    api, daemon, _ = server
    store = BoardStore(tmp_path / "life.db")
    store.connect()
    daemon._board = store
    daemon._lifecycle_unavailable = True
    try:
        _seed(store, 1)
        now = time.time()
        status, _, body = await api._lifecycle_report_for(
            f"root=/project&from={now - 60}&to={now + 60}")
        report = json.loads(body)
        assert status == 200
        assert report["measurements_available"] is False
        assert report["summaries"]["queue"]["n"] == 1
    finally:
        store.close()
        daemon._board = None
