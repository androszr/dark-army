# host/tests/test_manual_check_api.py
"""The Checks section's read and the outcome verb, at the daemon and both
phone doors.

Seams: `test_knowledge_api.py`'s — a real `BobDaemon` and `BoardStore` on a
temp file, `enrollment.enrolled_roots` patched to temp project roots, the
loopback GET driven through `_handle_client` — plus
`test_phone_review_and_manual_acks.py`'s `_sealed_run` / `_remote_run` over
a ledger on temp files for the lease.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from dark_army_daemon import (board, command_receipts, devices, enrollment,
                              event_log, manual_check, paths, relay)
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

VERB = "board_manual_outcome"
STEPS = "1. Open the menu bar.\nWhy not automated: a real screen."


def _check(card="Fit the strip", check="the strip fits",
           created="2026-09-25T14:32:00+02:00", status="open",
           outcome="none", steps="1. Open the menu bar.") -> str:
    return (f"# {check.capitalize()}\n\n"
            f"- **Card:** {card}\n"
            f"- **Project:** proj\n"
            f"- **Check:** {check}\n"
            f"- **Created:** {created}\n"
            f"- **Status:** {status}\n"
            f"- **Outcome:** {outcome}\n"
            f"- **Checked at:** none\n\n"
            f"## Steps\n\n{steps}\n\n"
            f"## Why not automated\n\nA real screen.\n")


def _put(root: str, folder: str, text: str) -> str:
    target = os.path.join(root, "manual-check", folder)
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, "check.md")
    with open(path, "w") as handle:
        handle.write(text)
    return os.path.realpath(path)


class _Writer:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    """Devices and relay on temp files, for the away door's lease."""
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)
    devices.reset()
    relay.reset()
    yield
    devices.reset()
    relay.reset()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    roots = []
    for name in ("project-a", "project-b"):
        (tmp_path / name).mkdir()
        roots.append(os.path.realpath(tmp_path / name))
    members = set(roots)

    def _normalise(root):
        text = str(root or "").strip()
        return os.path.realpath(text) if text else ""

    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(members))
    monkeypatch.setattr(enrollment, "normalise", _normalise)
    monkeypatch.setattr(
        enrollment, "root_enrolled",
        lambda cwd: _normalise(cwd) if _normalise(cwd) in members else "")
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    api = ApiServer(daemon, port=0)
    api.token = "token"
    api._receipts = command_receipts.CommandReceipts()
    yield api, daemon, store, roots
    store.close()


async def _get(api, query, headers=None):
    hdrs = {"host": "localhost", "x-bob-token": "token"} \
        if headers is None else headers
    lines = [f"GET /api/manual-checks?{query} HTTP/1.1"]
    lines += [f"{k}: {v}" for k, v in hdrs.items()] + ["", ""]
    reader = asyncio.StreamReader()
    reader.feed_data("\r\n".join(lines).encode())
    reader.feed_eof()
    writer = _Writer()
    await api._handle_client(reader, writer)
    head, _, body = bytes(writer.buf).partition(b"\r\n\r\n")
    return int(head.split()[1]), body


def _flagged_card(store, root, path, title="Fit the strip", **extra):
    fields = {"title": title, "project": "proj", "root": root}
    fields.update(extra)
    card, detail = store.create(fields)
    assert card is not None, detail
    store.bind_session(card["id"], "s1")
    got, detail = store.flag_manual(card["id"], "s1", STEPS, path)
    assert got is not None, detail
    return got


# --- the read ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_read_is_token_gated(setup):
    api, *_ = setup
    status, _ = await _get(api, "", {"host": "localhost"})
    assert status == 403
    status, _ = await _get(api, "", {"host": "localhost",
                                     "authorization": "Bearer token"})
    assert status == 403


@pytest.mark.asyncio
async def test_lists_every_enrolled_root_open_first(setup):
    api, _, store, (a, b) = setup
    old_open = _put(a, "2026-09-20-old", _check(
        check="old open", created="2026-09-20T09:00:00+02:00"))
    new_passed = _put(b, "2026-09-25-done", _check(
        check="new passed", status="passed", outcome="fine"))
    new_open = _put(b, "2026-09-24-open", _check(
        check="new open", created="2026-09-24T09:00:00+02:00"))
    card = _flagged_card(store, b, new_open)
    status, body = await _get(api, "")
    assert status == 200, body
    report = json.loads(body)
    assert report["supported"] is True and report["available"] is True
    assert [c["path"] for c in report["checks"]] == [
        new_open, old_open, new_passed]
    first = report["checks"][0]
    assert first["card_id"] == card["id"]
    assert first["status"] == "open" and first["check"] == "new open"
    assert report["checks"][2]["outcome"] == "fine"
    assert report["checks"][1]["card_id"] == ""


@pytest.mark.asyncio
async def test_a_search_word_and_a_status_filter_narrow_the_list(setup):
    api, _, _, (a, b) = setup
    _put(a, "2026-09-25-strip", _check(check="the strip fits"))
    _put(b, "2026-09-25-login", _check(
        card="Sign-in card", check="login works", status="passed", outcome="Looked FINE",
        steps="1. Press Sign in."))
    for query, wanted in (("q=STRIP", ["the strip fits"]),
                          ("q=sign%20in", ["login works"]),
                          ("q=fine", ["login works"]),
                          ("q=zzz", []),
                          ("status=passed", ["login works"]),
                          ("status=open", ["the strip fits"]),
                          ("status=all", ["the strip fits", "login works"])):
        status, body = await _get(api, query)
        assert status == 200, (query, body)
        assert [c["check"] for c in json.loads(body)["checks"]] == wanted, \
            query
    status, body = await _get(api, "status=done")
    assert status == 400 and b"status must be" in body


@pytest.mark.asyncio
async def test_a_named_root_must_be_enrolled(setup, tmp_path):
    api, _, _, (a, _b) = setup
    _put(a, "2026-09-25-strip", _check())
    status, body = await _get(api, "root=" + quote(a, safe=""))
    assert status == 200 and len(json.loads(body)["checks"]) == 1
    status, body = await _get(
        api, "root=" + quote(str(tmp_path / "elsewhere"), safe=""))
    assert status == 200
    report = json.loads(body)
    assert report["available"] is False and report["checks"] == []
    assert "watching" in report["reason"] or "enrolled" in report["reason"]


@pytest.mark.asyncio
async def test_one_file_is_served_only_from_an_enrolled_folder(setup, tmp_path):
    api, _, _, (a, _b) = setup
    path = _put(a, "2026-09-25-strip", _check())
    status, body = await _get(api, "path=" + quote(path, safe=""))
    document = json.loads(body)
    assert status == 200 and document["available"] is True
    assert "## Steps" in document["text"]
    stray = _put(str(tmp_path / "loose"), "x", _check())
    status, body = await _get(api, "path=" + quote(stray, safe=""))
    document = json.loads(body)
    assert status == 200 and document["available"] is False
    assert document["text"] == ""
    assert document["reason"] == board.MANUAL_CHECK_PLACE_REFUSAL


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
async def test_the_sealed_kind_answers_on_both_doors_and_records_nothing(
        setup, door):
    api, daemon, _, (a, _b) = setup
    _put(a, "2026-09-25-strip", _check())
    actions = api.LAN_ACTIONS if door == "home" else api.REMOTE_ACTIONS
    status, _, body = await api._sealed_run(
        "manual_checks", {"q": "strip"}, "device", actions=actions,
        check_lease=door == "away", record=door == "away")
    assert status == 200, body
    assert [c["check"] for c in json.loads(body)["checks"]] == [
        "the strip fits"]
    assert list(getattr(daemon, "_remote_activity", [])) == []


def test_every_board_shape_states_both_markers(setup, monkeypatch):
    _api, daemon, store, _roots = setup
    keys = ("manual_checks_supported", "manual_outcome_writable")
    for key in keys:
        assert daemon._pipeline_writable()[key] is True, key
        assert daemon._build_board_state()[key] is True, key
    daemon._board = None
    for key in keys:
        assert daemon._build_board_state()[key] is True, key
    daemon._board = store


# --- the verb ----------------------------------------------------------------


def _verb(path, status="passed", note=""):
    return {"action": VERB, "path": path, "status": status, "note": note}


def test_the_verb_is_on_every_list_and_remote_stays_within_lan():
    assert ApiServer.BOARD_ACTIONS.count(VERB) == 1
    assert ApiServer.LAN_ACTIONS.count(VERB) == 1
    assert ApiServer.REMOTE_ACTIONS.count(VERB) == 1
    assert VERB in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    assert board.MANUAL_OUTCOME_RECORDED_REFUSAL == manual_check.RECORDED


@pytest.mark.asyncio
async def test_records_passed_and_clears_the_card(setup, monkeypatch):
    api, daemon, store, (a, _b) = setup
    path = _put(a, "2026-09-25-strip", _check())
    card = _flagged_card(store, a, path)
    publish = AsyncMock()
    monkeypatch.setattr(daemon, "_publish_board", publish)
    lines = []
    monkeypatch.setattr(daemon, "_event_log", object())
    monkeypatch.setattr(daemon, "_log_event",
                        lambda kind, **fields: lines.append((kind, fields)))
    before = open(path).read().splitlines()
    status, _, body = await api._sealed_run(
        "action", _verb(path, "passed", "looked fine"), "phone",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    after = open(path).read().splitlines()
    changed = [(x, y) for x, y in zip(before, after) if x != y]
    assert [x for x, _ in changed] == [
        "- **Status:** open", "- **Outcome:** none", "- **Checked at:** none"]
    assert changed[0][1] == "- **Status:** passed"
    assert changed[1][1] == "- **Outcome:** looked fine"
    assert changed[2][1].startswith("- **Checked at:** 20")
    assert store.get(card["id"])["manual_steps"] == ""
    assert publish.await_count == 1
    assert [kind for kind, _ in lines] == ["card_manual_outcome"]
    sentence = event_log.sentence(lines[0][0], **lines[0][1])
    assert sentence == "Fit the strip: manual check passed — looked fine"
    assert path not in json.dumps(lines[0][1])


@pytest.mark.asyncio
async def test_second_press_is_refused(setup):
    api, _, _, (a, _b) = setup
    path = _put(a, "2026-09-25-strip", _check())
    status, _, body = await api._board_action(VERB, _verb(path, "failed"))
    assert status == 200, body
    text = open(path).read()
    status, _, body = await api._board_action(VERB, _verb(path, "passed"))
    assert status == 409
    assert json.loads(body)["detail"] == board.MANUAL_OUTCOME_RECORDED_REFUSAL
    assert open(path).read() == text


@pytest.mark.asyncio
async def test_a_bad_status_or_no_path_is_400_and_writes_nothing(setup):
    api, _, _, (a, _b) = setup
    path = _put(a, "2026-09-25-strip", _check())
    text = open(path).read()
    status, _, _ = await api._board_action(VERB, _verb(path, "done"))
    assert status == 400
    status, _, _ = await api._board_action(VERB, _verb("", "passed"))
    assert status == 400
    assert open(path).read() == text


@pytest.mark.asyncio
async def test_a_stray_file_a_link_and_a_fifo_are_refused(setup, tmp_path):
    api, _, _, (a, _b) = setup
    real = _put(a, "2026-09-25-strip", _check())
    text = open(real).read()
    stray = _put(str(tmp_path / "loose"), "x", _check())
    linked_dir = os.path.join(a, "manual-check", "2026-09-26-link")
    os.makedirs(linked_dir)
    link = os.path.join(linked_dir, "check.md")
    os.symlink(real, link)
    fifo_dir = os.path.join(a, "manual-check", "2026-09-27-fifo")
    os.makedirs(fifo_dir)
    fifo = os.path.join(fifo_dir, "check.md")
    os.mkfifo(fifo)
    for path in (stray, link, fifo, "manual-check/2026-09-25-strip/check.md"):
        status, _, body = await api._board_action(VERB, _verb(path))
        assert status == 409, (path, body)
        assert json.loads(body)["detail"] == board.MANUAL_CHECK_PLACE_REFUSAL
    assert open(real).read() == text
    assert "- **Status:** open" in open(stray).read()


@pytest.mark.asyncio
async def test_away_rides_the_lease(setup, monkeypatch):
    api, _, _, (a, _b) = setup
    path = _put(a, "2026-09-25-strip", _check())
    relay.create_channel("phone")
    relay.note_lan_proof("phone")
    assert relay.lease_valid("phone")
    monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
    status, _, body = await api._remote_run("action", _verb(path), "phone")
    assert status == 403
    assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
    assert "- **Status:** open" in open(path).read()
    monkeypatch.setattr(relay, "lease_valid", lambda _device: True)
    status, _, body = await api._remote_run("action", _verb(path), "phone")
    assert status == 200, body
    assert "- **Status:** passed" in open(path).read()


@pytest.mark.asyncio
async def test_acceptance_follows_the_check(setup, monkeypatch):
    api, daemon, store, (a, _b) = setup
    monkeypatch.setattr(daemon, "_publish_board", AsyncMock())
    path = _put(a, "2026-09-25-strip", _check())
    card = _flagged_card(store, a, path, intended_benefit="Benefit",
                         success_criterion="Criterion")
    accept = {"action": "board_accept_outcome", "card_id": card["id"],
              "expected_outcome_revision": "0", "request_key": "one",
              "evidence": "Observed success"}
    status, _, _ = await api._board_action("board_accept_outcome", accept)
    assert status == 409
    status, _, body = await api._board_action(VERB, _verb(path))
    assert status == 200, body
    accept["request_key"] = "two"
    status, _, body = await api._board_action("board_accept_outcome", accept)
    assert status == 200, body


# --- the card's sealed read --------------------------------------------------


def test_the_card_read_serves_its_check_file(setup):
    api, _, store, (a, _b) = setup
    path = _put(a, "2026-09-25-strip", _check())
    card = _flagged_card(store, a, path)
    report = api._card_collect(card["id"], with_plan=True)
    assert report["manual_check"]["available"] is True
    assert "## Why not automated" in report["manual_check"]["text"]
    plain, _ = store.create({"title": "plain", "project": "proj", "root": a})
    report = api._card_collect(plain["id"], with_plan=True)
    assert report["manual_check"] == {
        "available": False, "path": "", "text": "",
        "reason": "this card has no manual check file"}
