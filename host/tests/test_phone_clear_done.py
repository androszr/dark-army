# host/tests/test_phone_clear_done.py
"""Pins for the phone's store-wide Clear Done sweep.

The Mac already had the verb; this file pins that a paired phone now has it
too — both tuples, `_LAN_BOARD` routing, the version marker on every snapshot
shape, a sealed 200, a stale 409 that deletes nothing, and the phone source
that draws the one-arm confirm.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from dark_army_daemon import command_receipts, relay
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
DAEMON_BOARD = ROOT / "host" / "dark_army_daemon" / "daemon_board.py"
VERB = "board_clear_done"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


class _StubDaemon:
    def __init__(self):
        self.cleared = []

    async def clear_done_cards(self, expected_count, expected_token):
        self.cleared.append((expected_count, expected_token))
        return True, expected_count, "deleted"


def _server(daemon):
    srv = object.__new__(ApiServer)
    srv._daemon = daemon
    srv._receipts = command_receipts.CommandReceipts()
    return srv


def _run(srv, action, payload):
    return asyncio.run(srv._lan_run(action, payload))


def _card(store, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": "/tmp/bob"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


# --- tuples / routing --------------------------------------------------------


def test_both_phone_tuples_contain_the_verb_exactly_once():
    assert ApiServer.LAN_ACTIONS.count(VERB) == 1
    assert ApiServer.REMOTE_ACTIONS.count(VERB) == 1
    assert VERB in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_the_verb_is_still_on_the_loopback_board_list():
    assert ApiServer.BOARD_ACTIONS.count(VERB) == 1


def test_a_sealed_home_frame_reaches_clear_done_cards():
    stub = _StubDaemon()
    token = "a" * 64
    status, _, body = asyncio.run(_server(stub)._sealed_run(
        "action",
        {"action": VERB, "expected_count": "2",
         "expected_done_token": token},
        "device",
        actions=ApiServer.LAN_ACTIONS,
        check_lease=False,
        record=False,
    ))
    assert status == 200, body
    assert stub.cleared == [(2, token)]
    assert json.loads(body)["ok"] is True


def test_a_bool_expected_count_is_400_and_never_calls_the_daemon():
    stub = _StubDaemon()
    status, _, body = _run(_server(stub), VERB, {
        "expected_count": True,
        "expected_done_token": "a" * 64,
    })
    assert status == 400
    assert stub.cleared == []
    assert b"expected_count" in body


def test_a_stale_equal_count_token_is_409_and_deletes_nothing(tmp_path):
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    daemon._board = store
    try:
        first = _card(store, title="first", column_name="done")
        second = _card(store, title="second", column_name="done")
        incoming = _card(store, title="incoming", column_name="backlog")
        expected_count, expected_token = store.done_scope()
        store.move(first["id"], "backlog")
        store.move(incoming["id"], "done")
        actual_count, actual_token = store.done_scope()
        assert actual_count == expected_count
        assert actual_token != expected_token

        status, _, body = _run(_server(daemon), VERB, {
            "expected_count": str(expected_count),
            "expected_done_token": expected_token,
        })
        payload = json.loads(body)
        assert status == 409
        assert payload["deleted_count"] == 0
        assert payload["ok"] is False
        remaining = {c["id"] for c in store.cards(["done"])}
        assert remaining == {second["id"], incoming["id"]}
        assert store.get(first["id"])["column_name"] == "backlog"
    finally:
        store.close()
        daemon._board = None


def test_removing_the_name_from_remote_only_404s_away(monkeypatch):
    stub = _StubDaemon()
    srv = _server(stub)
    monkeypatch.setattr(relay, "lease_valid", lambda _device: True)
    saved = ApiServer.REMOTE_ACTIONS
    try:
        ApiServer.REMOTE_ACTIONS = tuple(
            name for name in saved if name != VERB)
        status, _, _ = asyncio.run(srv._sealed_run(
            "action",
            {"action": VERB, "expected_count": "1",
             "expected_done_token": "a" * 64},
            "device",
            actions=ApiServer.REMOTE_ACTIONS,
            check_lease=True,
            record=False,
        ))
        assert status == 404
        assert stub.cleared == []
        assert VERB in ApiServer.LAN_ACTIONS
    finally:
        ApiServer.REMOTE_ACTIONS = saved


# --- version marker ----------------------------------------------------------


def test_every_board_shape_states_the_new_marker(tmp_path, monkeypatch):
    """`_pipeline_writable` is spread into all three shapes — open, shut and
    read-failed — under this file's never-a-missing-key rule."""
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        assert d._pipeline_writable()["clear_done_writable"] is True
        assert d._build_board_state()["clear_done_writable"] is True
        d._board = None
        assert d._build_board_state()["clear_done_writable"] is True
        d._board = store
        monkeypatch.setattr(
            store, "cards",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
        assert d._build_board_state()["clear_done_writable"] is True
    finally:
        store.close()
        d._board = None


# --- phone source pins -------------------------------------------------------


def test_actions_swift_names_the_verb():
    src = _read(PHONE / "Actions.swift")
    assert 'static let boardClearDone = "board_clear_done"' in src


def test_models_decodes_the_token_and_the_writable_flag():
    src = _read(PHONE / "Models.swift")
    assert 'case doneClearToken = "done_clear_token"' in src
    assert "doneClearToken = c.value(.doneClearToken, \"\")" in src
    assert 'case clearDoneWritable = "clear_done_writable"' in src
    assert "clearDoneWritable = c.value(.clearDoneWritable, false)" in src
    assert "var doneCount: Int { max(0, counts[\"done\"] ?? 0) }" in src
    assert "var hasSafeDoneClearToken: Bool" in src


def test_arm_has_the_clear_done_slot():
    src = _read(PHONE / "Arm.swift")
    assert "case clearDone" in src
    assert "var clearDone: String?" in src


def test_the_done_page_posts_the_verb_with_id_and_token():
    src = _read(PHONE / "BoardView.swift")
    assert "PhoneActions.boardClearDone" in src
    assert 'arm.confirm(.clearDone, id:' in src
    assert '"expected_done_token"' in src
    assert "hasSafeDoneClearToken" in src
    assert "clearDoneWritable" in src
    assert "AgentChatterView(.caret, wait: .clearingDone" in src
    start = src.index("private var clearDoneChrome")
    chrome = src[start:src.index("private func armClearDone")]
    assert chrome.count(".frame(minWidth: 44, minHeight: 44)") == 2
    assert "board_delete" not in chrome
    assert "delete_card" not in chrome
    assert ".lineLimit(" not in chrome


def test_clear_done_cards_still_does_not_wrap_up_a_session():
    src = _read(DAEMON_BOARD)
    start = src.index("    async def clear_done_cards(")
    body = src[start:src.index("\n    async def ", start + 1)]
    assert "_after_board_write" not in body
    assert src.count("    async def clear_done_cards(") == 1
