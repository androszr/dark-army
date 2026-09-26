# host/tests/test_phone_review_and_manual_acks.py
"""Pins for Mark checked and Mark reviewed from a paired phone.

The Mac already had both verbs (`board_manual_clear`, `board_review`); this
file pins that a paired phone now has them too, each chosen on its own line
of `LAN_ACTIONS` and again of `REMOTE_ACTIONS`, routed by `_LAN_BOARD`, and
guarded by a **current-state echo** the store ANDs into its own one-shot
WHERE: a matching press lands once, a stale or second press is refused in
words with nothing written, a lapsed or revoked away grant refuses before
the store is reached, and the two outcome verbs stay off both tuples —
reading a result on the phone never accepts it.

Seams, named per test: the class attributes; `_sealed_run` / `_remote_run`
over a real `BoardStore` on a temp file; `CommandReceipts`; the sealed home
door end to end for the unpaired case; `_pipeline_writable`; the phone
sources as text.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import command_receipts, devices, lan_hosts, paths, relay
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import (
    MANUAL_CHECK_CHANGED_REFUSAL,
    REVIEW_CHANGED_REFUSAL,
    REVIEW_HALF_ECHO_REFUSAL,
    BoardStore,
)
from dark_army_daemon.daemon import BobDaemon
from tests.pake_pair import pair_typed
from tests.free_ports import free_ports

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PHONE_TESTS = ROOT / "ios" / "BobPhoneTests"
API = ROOT / "host" / "dark_army_daemon" / "api_server.py"
DAEMON_BOARD = ROOT / "host" / "dark_army_daemon" / "daemon_board.py"
PANEL_CLIENT = ROOT / "panel" / "Sources" / "BobPanel" / "BoardClient.swift"

MANUAL = "board_manual_clear"
REVIEW = "board_review"
OUTCOME = ("board_accept_outcome", "board_request_revision")
STEPS = "1. Open the app.\n2. Press Restart.\n3. Expect it back within 2s."


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    """`test_lan_access`'s ledger: devices and relay on temp files."""
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    devices.reset()
    relay.reset()
    yield
    devices.reset()
    relay.reset()


@pytest.fixture
def store(tmp_path):
    board = BoardStore(tmp_path / "board.db")
    board.connect()
    try:
        yield board
    finally:
        board.close()


@pytest.fixture
def daemon(tmp_path, store):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    d._board = store
    d._refresh_board_state()
    try:
        yield d
    finally:
        d._board = None


def _server(daemon) -> ApiServer:
    """An `ApiServer` with no sockets: `_sealed_run` and `_remote_run` are
    plain coroutines over `_daemon` and `_receipts`."""
    srv = object.__new__(ApiServer)
    srv._daemon = daemon
    srv._receipts = command_receipts.CommandReceipts()
    return srv


def _manual_card(store) -> dict:
    card, detail = store.create({"title": "check me", "project": "bob",
                                 "root": "/tmp/bob"})
    assert card is not None, detail
    store.bind_session(card["id"], "s1")
    got, detail = store.flag_manual(card["id"], "s1", STEPS)
    assert got is not None, detail
    return got


def _closed_card(store, note: str = "tests pass") -> dict:
    card, detail = store.create({"title": "review me", "project": "bob",
                                 "root": "/tmp/bob",
                                 "column_name": "in_progress"})
    assert card is not None, detail
    store.bind_session(card["id"], "s1")
    got, detail = store.declare_done(card["id"], "s1", note)
    assert got is not None, detail
    return got


def _home(srv, payload, device_id="phone"):
    """Seam: `_sealed_run` behind `LAN_ACTIONS`, no lease — the home door
    after `_home_open` has verified the seal."""
    return asyncio.run(srv._sealed_run(
        "action", payload, device_id, actions=ApiServer.LAN_ACTIONS,
        check_lease=False, record=False))


def _away(srv, payload, device_id="phone"):
    """Seam: `_remote_run` — `REMOTE_ACTIONS`, lease checked, recorded."""
    return asyncio.run(srv._remote_run("action", payload, device_id))


def _arm_lease(device_id="phone") -> None:
    """A device with an away channel that was just seen at home: the one
    route that opens an away window (`relay.note_lan_proof`)."""
    relay.create_channel(device_id)
    relay.note_lan_proof(device_id)
    assert relay.lease_valid(device_id)


def _manual_payload(card: dict, **extra) -> dict:
    out = {"action": MANUAL, "card_id": card["id"],
           "expected_manual_steps": card["manual_steps"]}
    out.update(extra)
    return out


def _review_payload(card: dict, **extra) -> dict:
    out = {"action": REVIEW, "card_id": card["id"],
           "expected_closed_by": card["closed_by"],
           "expected_close_note": card["close_note"]}
    out.update(extra)
    return out


# --- tuples / routing --------------------------------------------------------


def test_both_verbs_are_chosen_once_on_every_list_and_remote_stays_within_lan():
    """Seam: the class attributes."""
    for verb in (MANUAL, REVIEW):
        assert ApiServer.LAN_ACTIONS.count(verb) == 1, verb
        assert ApiServer.REMOTE_ACTIONS.count(verb) == 1, verb
        assert verb in ApiServer._LAN_BOARD, verb
        assert ApiServer.BOARD_ACTIONS.count(verb) == 1, verb
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_the_manual_outcome_verb_is_on_both_tuples_and_the_lan_board():
    """Passed / Failed on a check file: chosen once on each phone tuple,
    routed by `_LAN_BOARD`, and `REMOTE_ACTIONS` still within `LAN_ACTIONS`.
    Mark checked steps aside on a card flagged with a file."""
    verb = "board_manual_outcome"
    assert ApiServer.LAN_ACTIONS.count(verb) == 1
    assert ApiServer.REMOTE_ACTIONS.count(verb) == 1
    assert verb in ApiServer._LAN_BOARD
    assert ApiServer.BOARD_ACTIONS.count(verb) == 1
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    src = _read(API)
    sliced = src.split("LAN_ACTIONS = (", 1)[1].split(")", 1)[0]
    assert f'"{verb}"' in sliced
    ack = _read(PHONE / "Actions.swift").split("enum PhoneCardAck {", 1)[1]
    clear = ack.split("static func showsManualClear", 1)[1].split("}", 1)[0]
    assert "card.manualCheckPath.isEmpty" in clear


def test_outcome_accept_and_revision_stay_off_both_phone_tuples():
    """Acknowledgment never implies outcome acceptance: the two decision
    verbs are loopback-only and reachable from no phone door."""
    for verb in OUTCOME:
        assert verb in ApiServer.BOARD_ACTIONS, verb
        assert verb not in ApiServer.LAN_ACTIONS, verb
        assert verb not in ApiServer.REMOTE_ACTIONS, verb
        assert verb not in ApiServer._LAN_BOARD, verb
    actions = _read(PHONE / "Actions.swift")
    for verb in OUTCOME:
        assert verb not in actions, verb


def test_the_lan_actions_comment_block_holds_no_parenthesis():
    """`test_phone_needs_you` and `test_phone_inbox` slice `LAN_ACTIONS = (`
    at the first `)`; a parenthesis inside the tuple's comments would make
    the two verbs look un-added to every pin that reads it that way."""
    src = _read(API)
    body = src.split("LAN_ACTIONS = (", 1)[1].split("\n    )\n", 1)[0]
    assert f'"{MANUAL}"' in body and f'"{REVIEW}"' in body
    assert "(" not in body, "a '(' inside LAN_ACTIONS truncates the slice"
    sliced = src.split("LAN_ACTIONS = (", 1)[1].split(")", 1)[0]
    assert f'"{MANUAL}"' in sliced and f'"{REVIEW}"' in sliced


def test_the_echo_keys_are_envelope_not_card_fields():
    """A payload naming only the echo must not be judged 'named fields, all
    dropped', and none of the three may ever become a writable field."""
    for key in ("expected_manual_steps", "expected_closed_by",
                "expected_close_note"):
        assert key in ApiServer._BOARD_ENVELOPE
        assert key not in ApiServer._BOARD_FIELDS
    for field in ("manual_steps", "closed_by", "close_note", "reviewed_at"):
        assert field not in ApiServer._BOARD_FIELDS


# --- loopback: absent echo is today's statement --------------------------


def test_the_mac_press_without_an_echo_still_lands(daemon, store):
    """Seam: `_board_action` with `{action, card_id}` alone — what the panel
    posts. Absent means no guard, `expected_revision`'s rule."""
    srv = _server(daemon)
    manual = _manual_card(store)
    status, _, body = asyncio.run(srv._board_action(
        MANUAL, {"action": MANUAL, "card_id": manual["id"]}))
    assert status == 200, body
    assert store.get(manual["id"])["manual_steps"] == ""
    closed = _closed_card(store)
    status, _, body = asyncio.run(srv._board_action(
        REVIEW, {"action": REVIEW, "card_id": closed["id"]}))
    assert status == 200, body
    assert store.get(closed["id"])["reviewed_at"] is not None


# --- sealed home door --------------------------------------------------------


def test_a_matching_manual_echo_lands_once_and_a_second_press_is_refused(
        daemon, store):
    srv = _server(daemon)
    card = _manual_card(store)
    status, _, body = _home(srv, _manual_payload(card))
    assert status == 200, body
    assert json.loads(body)["ok"] is True
    assert store.get(card["id"])["manual_steps"] == ""
    # The card did not move.
    assert store.get(card["id"])["column_name"] == card["column_name"]
    # A second press with the same echo: the store's own 409.
    status, _, body = _home(srv, _manual_payload(card))
    assert status == 409
    assert "no manual check" in json.loads(body)["detail"]


def test_a_stale_manual_echo_is_refused_in_words_and_writes_nothing(
        daemon, store):
    srv = _server(daemon)
    card = _manual_card(store)
    # The session re-flagged since the phone's picture was taken.
    store.clear_manual(card["id"])
    fresh, detail = store.flag_manual(card["id"], "s1", "1. Something else.")
    assert fresh is not None, detail
    revision = fresh["revision"]
    status, _, body = _home(srv, _manual_payload(card))
    assert status == 409, body
    assert json.loads(body)["detail"] == MANUAL_CHECK_CHANGED_REFUSAL
    after = store.get(card["id"])
    assert after["manual_steps"] == "1. Something else."
    assert after["revision"] == revision
    # Present but empty is a refusable echo, never "no guard".
    status, _, body = _home(srv, _manual_payload(card, expected_manual_steps=""))
    assert status == 409
    assert json.loads(body)["detail"] == MANUAL_CHECK_CHANGED_REFUSAL
    assert store.get(card["id"])["manual_steps"] == "1. Something else."


def test_a_matching_review_echo_lands_once_and_a_second_press_is_refused(
        daemon, store):
    srv = _server(daemon)
    card = _closed_card(store)
    status, _, body = _home(srv, _review_payload(card))
    assert status == 200, body
    got = store.get(card["id"])
    assert got["reviewed_at"] is not None
    # The close itself is unchanged and no outcome was accepted.
    assert got["closed_by"] == "s1" and got["close_note"] == "tests pass"
    assert got["column_name"] == "done"
    status, _, body = _home(srv, _review_payload(card))
    assert status == 409
    assert json.loads(body)["detail"] == REVIEW_CHANGED_REFUSAL


def test_a_stale_or_half_review_echo_is_refused_and_writes_nothing(
        daemon, store):
    srv = _server(daemon)
    card = _closed_card(store)
    status, _, body = _home(srv, _review_payload(
        card, expected_close_note="yesterday's close"))
    assert status == 409, body
    assert json.loads(body)["detail"] == REVIEW_CHANGED_REFUSAL
    assert store.get(card["id"])["reviewed_at"] is None
    status, _, body = _home(srv, _review_payload(
        card, expected_closed_by="someone-else"))
    assert status == 409
    assert json.loads(body)["detail"] == REVIEW_CHANGED_REFUSAL
    assert store.get(card["id"])["reviewed_at"] is None
    # Half a pair is refused before any UPDATE.
    half = {"action": REVIEW, "card_id": card["id"],
            "expected_closed_by": card["closed_by"]}
    status, _, body = _home(srv, half)
    assert status == 409
    assert json.loads(body)["detail"] == REVIEW_HALF_ECHO_REFUSAL
    assert store.get(card["id"])["reviewed_at"] is None


# --- receipts --------------------------------------------------------------


def test_a_replayed_token_returns_the_frozen_answer_without_a_second_write(
        daemon, store, monkeypatch):
    """Seam: `CommandReceipts` inside `_sealed_run`. The same token replays
    the 200 and the store is not asked again; a person's RETRY mints a new
    token and must then meet the store guard, not silently succeed."""
    srv = _server(daemon)
    card = _manual_card(store)
    calls = []
    real = store.clear_manual

    def counting(card_id, **kw):
        calls.append(card_id)
        return real(card_id, **kw)

    monkeypatch.setattr(store, "clear_manual", counting)
    token = "press-0001-abcdef"
    first = _home(srv, _manual_payload(card, command_token=token))
    assert first[0] == 200, first[2]
    assert calls == [card["id"]]
    again = _home(srv, _manual_payload(card, command_token=token))
    assert again == first
    assert calls == [card["id"]]
    retry = _home(srv, _manual_payload(card, command_token="press-0002-abcdef"))
    assert retry[0] == 409
    assert "no manual check" in json.loads(retry[2])["detail"]
    assert calls == [card["id"], card["id"]]


# --- away door --------------------------------------------------------------


def test_away_inside_a_live_lease_lands_a_matching_echo(daemon, store):
    """Seam: `_remote_run` after `relay.note_lan_proof`."""
    srv = _server(daemon)
    _arm_lease()
    manual = _manual_card(store)
    status, _, body = _away(srv, _manual_payload(manual))
    assert status == 200, body
    assert store.get(manual["id"])["manual_steps"] == ""
    closed = _closed_card(store)
    status, _, body = _away(srv, _review_payload(closed))
    assert status == 200, body
    assert store.get(closed["id"])["reviewed_at"] is not None
    # Both writes are on the Mac's "done remotely" list.
    actions = [r["action"] for r in daemon._remote_activity]
    assert actions[-2:] == [MANUAL, REVIEW]


def test_dropping_a_name_from_remote_alone_404s_away_before_anything_runs(
        daemon, store, monkeypatch):
    """`test_the_away_path_has_its_own_action_list`'s spy: a name absent
    from `REMOTE_ACTIONS` is 404 over the relay even inside a live lease,
    `_lan_run` never sees it, and the home list is untouched."""
    srv = _server(daemon)
    _arm_lease()
    ran = []
    original = srv._lan_run

    async def _spy(action, payload, device_id=""):
        ran.append(action)
        return await original(action, payload, device_id=device_id)

    srv._lan_run = _spy
    saved = ApiServer.REMOTE_ACTIONS
    monkeypatch.setattr(ApiServer, "REMOTE_ACTIONS", tuple(
        name for name in saved if name not in (MANUAL, REVIEW)))
    manual = _manual_card(store)
    closed = _closed_card(store)
    for payload in (_manual_payload(manual), _review_payload(closed)):
        status, _, _ = _away(srv, payload)
        assert status == 404
    assert ran == []
    assert MANUAL in ApiServer.LAN_ACTIONS and REVIEW in ApiServer.LAN_ACTIONS
    assert store.get(manual["id"])["manual_steps"] == STEPS
    assert store.get(closed["id"])["reviewed_at"] is None


def test_a_lapsed_lease_refuses_both_and_writes_nothing(
        daemon, store, monkeypatch):
    """`test_a_lapsed_lease_still_reads_a_card_and_refuses_the_approval`'s
    shape: `lease_valid` False is 403 in `LEASE_REFUSAL`'s words, and the
    store is never reached."""
    srv = _server(daemon)
    _arm_lease()
    monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
    manual = _manual_card(store)
    closed = _closed_card(store)
    for payload in (_manual_payload(manual), _review_payload(closed)):
        status, _, body = _away(srv, payload)
        assert status == 403, body
        assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
        assert b"lapsed" in body
    assert store.get(manual["id"])["manual_steps"] == STEPS
    assert store.get(closed["id"])["reviewed_at"] is None


def test_a_revoked_away_channel_refuses_both_and_writes_nothing(daemon, store):
    """`relay.forget` — End away access on the Mac — kills the lease, so the
    next press is refused in the same words and nothing is written."""
    srv = _server(daemon)
    _arm_lease()
    relay.forget("phone")
    assert not relay.lease_valid("phone")
    manual = _manual_card(store)
    closed = _closed_card(store)
    for payload in (_manual_payload(manual), _review_payload(closed)):
        status, _, body = _away(srv, payload)
        assert status == 403, body
        assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
    assert store.get(manual["id"])["manual_steps"] == STEPS
    assert store.get(closed["id"])["reviewed_at"] is None


# --- the home door end to end: unpaired -----------------------------------


def _free_ports(count: int) -> list[int]:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_ports(count)


@pytest.fixture
def _machine(monkeypatch):
    monkeypatch.setattr(lan_hosts, "live_addrs",
                        lambda: {"en0": ["192.168.1.5"]})
    monkeypatch.setattr(lan_hosts, "default_route_addr", lambda: "192.168.1.5")
    monkeypatch.setattr(socket, "gethostname", lambda: "test-mac")


@pytest_asyncio.fixture
async def lan_server(tmp_path, monkeypatch, _machine, daemon):
    loop_port, lan_port = _free_ports(2)
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", tmp_path / "api-token")
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    monkeypatch.setattr(api_mod, "LAN_API_PORT", lan_port)
    srv = ApiServer(daemon, port=loop_port)
    daemon._api = srv
    await srv.start()
    daemon.lan_access_enabled = True
    await srv.start_lan()
    try:
        yield srv, daemon, lan_port
    finally:
        await srv.stop()


def _fetch(path, *, port, data=None, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {},
        method="POST" if data is not None else "GET")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def _fetch_async(*args, **kwargs):
    return await asyncio.to_thread(_fetch, *args, **kwargs)


async def _pair_plain(lan_port: int) -> dict:
    return await pair_typed(_fetch_async, lan_port)


async def _home_post(lan_port: int, key: bytes, body: dict, ctr: int):
    wire = relay.seal_frame(key, relay.DIR_PHONE_TO_MAC, ctr, "action", body,
                            ns=relay.HOME)
    sent = {"X-Bob-Channel": relay.channel_id(key, ns=relay.HOME),
            "Content-Type": "text/plain"}
    status, out = await asyncio.to_thread(
        _fetch, "/api/home", port=lan_port, data=wire.encode(), headers=sent)
    if status != 200:
        return status, out
    frame, err = relay.open_frame(key, relay.DIR_MAC_TO_PHONE, out.decode(),
                                  0, ns=relay.HOME)
    assert err == "", err
    return int(frame["body"]["status"]), frame["body"]["body"]


@pytest.mark.asyncio
async def test_the_sealed_home_door_lands_once_and_an_unpaired_phone_is_refused(
        lan_server, store):
    """The whole home door: pair, seal a matching Mark checked, land it;
    then unpair and see the next sealed frame refused by `_home_open` in
    its existing words — a plain 403, nothing written."""
    srv, daemon, lan_port = lan_server
    paired = await _pair_plain(lan_port)
    manual = _manual_card(store)
    status, body = await _home_post(lan_port, paired["key"],
                                    _manual_payload(manual), 1)
    assert status == 200, body
    assert store.get(manual["id"])["manual_steps"] == ""
    closed = _closed_card(store)
    daemon.unpair_device(paired["device_id"])
    status, body = await _home_post(lan_port, paired["key"],
                                    _review_payload(closed), 2)
    assert status == 403, body
    assert json.loads(body)["error"] == "that phone is not paired"
    assert store.get(closed["id"])["reviewed_at"] is None


# --- version markers ---------------------------------------------------------


def test_both_markers_are_true_on_open_shut_and_headless_shapes(
        tmp_path, monkeypatch):
    """`test_phone_clear_done`'s pattern: `_pipeline_writable` is spread into
    every board shape under the never-a-missing-key rule, and the two
    markers are independent keys."""
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    board = BoardStore(tmp_path / "board.db")
    board.connect()
    d._board = board
    try:
        for key in ("manual_clear_writable", "review_writable"):
            assert d._pipeline_writable()[key] is True, key
            assert d._build_board_state()[key] is True, key
        d._board = None
        for key in ("manual_clear_writable", "review_writable"):
            assert d._build_board_state()[key] is True, key
        d._board = board
        monkeypatch.setattr(
            board, "cards",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
        for key in ("manual_clear_writable", "review_writable"):
            assert d._build_board_state()[key] is True, key
    finally:
        board.close()
        d._board = None


def test_no_secret_rides_the_snapshot_for_these_verbs(daemon, store):
    """The echo is a confirmation, not a capability: nothing new — no
    digest, claim or token — is published to make it 'harder'."""
    _manual_card(store)
    _closed_card(store)
    state = json.dumps(daemon._build_board_state())
    assert "expected_manual_steps" not in state
    assert "expected_closed_by" not in state
    assert "expected_close_note" not in state


# --- phone and panel source pins ---------------------------------------------


def test_actions_swift_names_both_verbs_and_the_pure_ack_helpers():
    src = _read(PHONE / "Actions.swift")
    assert f'static let boardManualClear = "{MANUAL}"' in src
    assert f'static let boardReview = "{REVIEW}"' in src
    ack = src.split("enum PhoneCardAck {", 1)[1]
    assert "static func showsManualClear(card: BoardCard, board: Board)" in ack
    assert "static func showsReview(card: BoardCard, board: Board)" in ack
    assert "board.manualClearWritable && card.manualCheckDue" in ack
    assert "board.reviewWritable && card.awaitsReview" in ack
    assert '"expected_manual_steps": card.manualSteps' in ack
    assert '"expected_closed_by": card.closedBy' in ack
    assert '"expected_close_note": card.closeNote' in ack
    assert "CardCache" not in ack


def test_models_decodes_both_markers_false_by_default():
    src = _read(PHONE / "Models.swift")
    assert 'case manualClearWritable = "manual_clear_writable"' in src
    assert 'case reviewWritable = "review_writable"' in src
    assert "manualClearWritable = c.value(.manualClearWritable, false)" in src
    assert "reviewWritable = c.value(.reviewWritable, false)" in src


def test_the_card_screen_arms_both_buttons_off_the_live_card():
    src = _read(PHONE / "CardDetailView.swift")
    assert "board_manual_clear is not a phone verb" not in src
    for label in ('"Mark checked"', '"Mark checked?"',
                  '"Mark reviewed"', '"Mark reviewed?"'):
        assert label in src, label
    assert "PhoneCardAck.showsManualClear(card: card, board: board)" in src
    assert "PhoneCardAck.showsReview(card: card, board: board)" in src
    # The echo is captured before `confirm`, off `card` (the live snapshot
    # over the seed), never the cache.
    manual = src.split("private func pressManualClear()", 1)[1].split("}\n    }", 1)[0]
    assert manual.index("PhoneCardAck.manualClearFields(card)") \
        < manual.index("arm.confirm(.manualClear")
    assert "cached" not in manual
    review = src.split("private func pressReview()", 1)[1].split("}\n    }", 1)[0]
    assert review.index("PhoneCardAck.reviewFields(card)") \
        < review.index("arm.confirm(.review")
    assert "cached" not in review
    assert "send(PhoneActions.boardManualClear, fields" in src
    assert "send(PhoneActions.boardReview, fields" in src
    arm = _read(PHONE / "Arm.swift")
    assert "case manualClear" in arm and "case review" in arm
    # Not in `settlingActions`: the fleet row is not leaving.
    client = _read(PHONE / "Client.swift")
    settling = client.split("settlingActions", 1)[1].split("]", 1)[0]
    assert MANUAL not in settling and REVIEW not in settling


def test_inbox_copy_no_longer_points_at_the_mac():
    """The list writes no sentence of its own any more: Dark Army's refusal or
    queue reason, or nothing. The two acknowledgements are the card
    screen's, named nowhere in the reducer or the list."""
    src = _read(PHONE / "Inbox.swift")
    assert "on the Mac" not in src
    assert "mark it checked" not in src
    assert "mark it reviewed" not in src
    assert "nextAction" not in src
    assert MANUAL not in src and REVIEW not in src
    tests = _read(PHONE_TESTS / "PhoneInboxTests.swift")
    assert "on the Mac." not in tests
    assert "testCardAckButtonsAreGatedOnTheirOwnMarkers" in tests
    assert "testCardAckPayloadsEchoWhatWasOnScreen" in tests
    decode = _read(PHONE_TESTS / "DecodeToleranceTests.swift")
    assert "testAnAbsentAckMarkerDecodesFalseAndAPresentOneTrue" in decode


def test_the_panel_still_posts_card_id_alone():
    """The Mac's own buttons are unchanged: no echo, no arm."""
    src = _read(PANEL_CLIENT)
    for name in ("boardManualClear", "boardReview"):
        fn = src.split(f"func {name}(", 1)[1].split("\n    }\n", 1)[0]
        assert '"card_id"' in fn, name
        for key in ("expected_manual_steps", "expected_closed_by",
                    "expected_close_note"):
            assert key not in fn, (name, key)


def test_the_two_refusals_are_distinct_and_greppable():
    """Both surfaces show the store's words verbatim; the manual and the
    review sentences may never become prefixes of one another."""
    assert MANUAL_CHECK_CHANGED_REFUSAL != REVIEW_CHANGED_REFUSAL
    assert not REVIEW_CHANGED_REFUSAL.startswith(MANUAL_CHECK_CHANGED_REFUSAL)
    assert not MANUAL_CHECK_CHANGED_REFUSAL.startswith(REVIEW_CHANGED_REFUSAL)
    assert re.search(r"already cleared", MANUAL_CHECK_CHANGED_REFUSAL)
    assert re.search(r"already reviewed", REVIEW_CHANGED_REFUSAL)
    board_src = _read(DAEMON_BOARD)
    assert '"manual_clear_writable": True' in board_src
    assert '"review_writable": True' in board_src
