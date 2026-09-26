# host/tests/test_start_ask.py
"""Mission Control asking the person to start a card.

The verb's whole security property is that it **starts nothing**: it puts
the card on Needs you as "start asked", the entry opens the card, and only
the person's own press on START (`dispatch_card`) starts it. What is tested
here is that the ask never dispatches, that only Mission Control may ask,
that the ask shows on the card exactly while it could still be answered, and
that a press or a Dismiss ends it.
"""
import pytest

from dark_army_daemon import channel_server as cs
from dark_army_daemon import daemon_board
from dark_army_daemon import inbox_ack
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

MISSION = "mission-session"


def _board_daemon(tmp_path, monkeypatch, caller=MISSION):
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    monkeypatch.setattr(daemon, "mission_snapshot",
                        lambda: {"session_id": MISSION})

    async def fresh(port):
        return caller

    monkeypatch.setattr(daemon, "_board_request_session_fresh", fresh)
    return daemon, store


def _ask(daemon, card_id, port=51000):
    return daemon._handle_board_start_ask_request(
        {"type": "board_start_ask_request", "port": port, "card_id": card_id})


def _decorated(daemon, card_id):
    state = daemon._build_board_state()
    return next(c for c in state["cards"] if c["id"] == card_id)


@pytest.mark.asyncio
async def test_an_ask_puts_the_card_on_needs_you_and_starts_nothing(
        tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch)
    try:
        card, _ = store.create({"title": "build it", "project": "bob",
                                "column_name": "backlog", "tool": "claude"})
        pressed = []

        async def locked(*a, **k):
            pressed.append(a)
            return True, "started"

        monkeypatch.setattr(daemon, "_dispatch_card_locked", locked)
        reply = await _ask(daemon, card["id"])
        assert reply["ok"] is True
        assert reply["title"] == "build it"
        assert pressed == [], "an ask must never dispatch"
        stored = store.get(card["id"])
        assert not stored.get("session_id")
        assert stored.get("link_state") != "dispatching"
        shown = _decorated(daemon, card["id"])
        assert shown["start_ask_id"]
        assert shown["start_asked_by"] == "Mission Control"
        kind, fp = inbox_ack.card_kind_and_fp(shown)
        assert kind == "start_asked"
        assert fp == inbox_ack.fingerprint("start_asked", shown["start_ask_id"])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_only_mission_control_may_ask(tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch, caller="some-worker")
    try:
        card, _ = store.create({"title": "x", "project": "bob",
                                "column_name": "backlog", "tool": "claude"})
        reply = await _ask(daemon, card["id"])
        assert reply["ok"] is False
        assert "Mission Control" in reply["detail"]
        assert "start_ask_id" not in _decorated(daemon, card["id"])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_unattributable_ask_is_refused(tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch, caller="")
    try:
        card, _ = store.create({"title": "x", "project": "bob",
                                "column_name": "backlog", "tool": "claude"})
        reply = await _ask(daemon, card["id"])
        assert reply["ok"] is False
        assert "start_ask_id" not in _decorated(daemon, card["id"])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_done_card_or_a_missing_one_cannot_be_asked_about(
        tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch)
    try:
        done, _ = store.create({"title": "old", "project": "bob",
                                "column_name": "done", "tool": "claude"})
        assert (await _ask(daemon, done["id"]))["ok"] is False
        assert (await _ask(daemon, "0" * 32))["ok"] is False
        assert (await _ask(daemon, ""))["ok"] is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_ask_is_refused_when_starting_is_switched_off(
        tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(daemon, "board_dispatch_enabled", False,
                            raising=False)
        card, _ = store.create({"title": "x", "project": "bob",
                                "column_name": "backlog", "tool": "claude"})
        reply = await _ask(daemon, card["id"])
        assert reply["ok"] is False
        assert "not allowed to start" in reply["detail"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_persons_start_press_ends_the_ask(tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch)
    try:
        card, _ = store.create({"title": "x", "project": "bob",
                                "column_name": "backlog", "tool": "claude"})

        async def locked(*a, **k):
            return True, "started"

        monkeypatch.setattr(daemon, "_dispatch_card_locked", locked)
        await _ask(daemon, card["id"])
        assert card["id"] in daemon._start_ask_map()
        ok, _ = await daemon.dispatch_card(card["id"])
        assert ok
        assert card["id"] not in daemon._start_ask_map()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_refused_press_keeps_the_ask(tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch)
    try:
        card, _ = store.create({"title": "x", "project": "bob",
                                "column_name": "backlog", "tool": "claude"})

        async def locked(*a, **k):
            return False, "no"

        monkeypatch.setattr(daemon, "_dispatch_card_locked", locked)
        await _ask(daemon, card["id"])
        ok, _ = await daemon.dispatch_card(card["id"])
        assert not ok
        assert card["id"] in daemon._start_ask_map()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_dismiss_drops_the_ask(tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch)
    try:
        card, _ = store.create({"title": "x", "project": "bob",
                                "column_name": "backlog", "tool": "claude"})
        await _ask(daemon, card["id"])
        assert await daemon.drop_start_ask(card["id"]) is True
        assert "start_ask_id" not in _decorated(daemon, card["id"])
        assert await daemon.drop_start_ask(card["id"]) is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_stale_ask_or_a_started_card_shows_no_ask(tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch)
    try:
        card, _ = store.create({"title": "x", "project": "bob",
                                "column_name": "backlog", "tool": "claude"})
        await _ask(daemon, card["id"])
        daemon._start_ask_map()[card["id"]]["asked_at"] -= (
            daemon_board.START_ASK_TTL_SECONDS + 1)
        assert "start_ask_id" not in _decorated(daemon, card["id"])

        other, _ = store.create({"title": "y", "project": "bob",
                                 "column_name": "backlog", "tool": "claude"})
        await _ask(daemon, other["id"])
        store.bind_session(other["id"], "worker")
        assert "start_ask_id" not in _decorated(daemon, other["id"])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_asks_are_capped(tmp_path, monkeypatch):
    daemon, store = _board_daemon(tmp_path, monkeypatch)
    try:
        ids = []
        for n in range(daemon_board.MAX_START_ASKS + 1):
            card, _ = store.create({"title": f"c{n}", "project": "bob",
                                    "column_name": "backlog", "tool": "claude"})
            ids.append(card["id"])
        for cid in ids[:-1]:
            assert (await _ask(daemon, cid))["ok"] is True
        assert (await _ask(daemon, ids[-1]))["ok"] is False
        # Asking again about a card already asked is not a new ask.
        assert (await _ask(daemon, ids[0]))["ok"] is True
    finally:
        store.close()


def test_the_tool_is_claude_only_and_takes_just_a_card_id():
    claude = [t["name"] for t in cs.tools_for_host(cs.HOST_CLAUDE)]
    codex = [t["name"] for t in cs.tools_for_host(cs.HOST_CODEX)]
    assert "dark_army_request_start" in claude
    assert "dark_army_request_start" not in codex
    tool = next(t for t in cs.tools_for_host(cs.HOST_CLAUDE)
                if t["name"] == "dark_army_request_start")
    assert set(tool["inputSchema"]["properties"]) == {"card_id"}
    assert "does not start" in tool["description"]


def test_the_tool_forwards_the_card_id_and_never_a_session():
    server = cs.ChannelServer.__new__(cs.ChannelServer)
    sent = []
    server.port = 51000
    server.name = cs.CURRENT_NAME
    server.call_daemon = lambda msg: sent.append(msg) or {
        "ok": True, "title": "build it"}
    result = server._call_request_start({"card_id": "abc", "session_id": "x"})
    assert result["isError"] is False
    assert "has not started" in result["content"][0]["text"]
    assert sent[0]["type"] == "board_start_ask_request"
    assert sent[0]["card_id"] == "abc"
    assert "session_id" not in sent[0]
    assert server._call_request_start({})["isError"] is True
