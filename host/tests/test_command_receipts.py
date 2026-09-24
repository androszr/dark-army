# host/tests/test_command_receipts.py
"""The command-token ledger: replay, mismatch, bounds, expiry, and where it
sits in `_sealed_run`.

The ordering is the safety case and it is tested rather than asserted in
prose: the ledger sits **below** the action allow-list and **below** the away
lease check, and answers only with a status this same daemon produced. A
lookup above either would let a token minted at home replay out of a lapsed
away window.
"""

from __future__ import annotations

import json

import pytest

from dark_army_daemon import command_receipts, relay
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.command_receipts import (MAX_RECEIPTS,
                                                   RECEIPT_SECONDS, TOKEN_RE,
                                                   CommandReceipts)


# --- the ledger itself --------------------------------------------------------


def test_a_recorded_answer_replays_verbatim():
    ledger = CommandReceipts()
    answer = (200, "application/json", b'{"ok": true}')
    ledger.record("dev", "abcd1234", answer)
    assert ledger.look_up("dev", "abcd1234") == answer


def test_two_devices_do_not_share_a_receipt():
    ledger = CommandReceipts()
    ledger.record("one", "abcd1234", (200, "application/json", b"{}"))
    assert ledger.look_up("two", "abcd1234") is None


def test_an_unknown_token_is_no_dedupe():
    ledger = CommandReceipts()
    assert ledger.look_up("dev", "neverseen1") is None


def test_a_malformed_token_is_neither_recorded_nor_matched():
    """An absent or malformed token means today's behaviour exactly, which is
    what keeps a phone older than this ledger working."""
    ledger = CommandReceipts()
    for bad in ("", "short", "has spaces in it", "x" * 200, None, 7):
        assert CommandReceipts.usable(bad) == ""
        ledger.record("dev", bad, (200, "application/json", b"{}"))
    assert len(ledger) == 0
    assert TOKEN_RE.match("a" * 8)
    assert not TOKEN_RE.match("a" * 7)


def test_the_ledger_evicts_past_its_cap():
    ledger = CommandReceipts(max_receipts=4)
    for n in range(10):
        ledger.record("dev", f"token{n:04d}", (200, "application/json", b"{}"))
    assert len(ledger) == 4
    assert ledger.look_up("dev", "token0000") is None
    assert ledger.look_up("dev", "token0009") is not None


def test_the_ledger_forgets_past_the_ttl(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(command_receipts.time, "time", lambda: clock["now"])
    ledger = CommandReceipts(ttl=100)
    ledger.record("dev", "abcd1234", (200, "application/json", b"{}"))
    clock["now"] += 99
    assert ledger.look_up("dev", "abcd1234") is not None
    clock["now"] += 2
    assert ledger.look_up("dev", "abcd1234") is None


def test_the_bounds_are_what_the_module_states():
    assert MAX_RECEIPTS == 256
    assert RECEIPT_SECONDS == 900


def test_the_module_imports_nothing_from_the_daemon():
    """A data structure, not a capability."""
    src = open(command_receipts.__file__).read()
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("from .") or stripped.startswith("import bob_"):
            raise AssertionError(line)


# --- where it sits in `_sealed_run` ------------------------------------------


class _CountingDaemon:
    """Just enough daemon for `_sealed_run`'s action branch."""

    def __init__(self):
        self.runs = 0

    def record_remote_action(self, *_a, **_kw):
        pass


def _server(monkeypatch, statuses):
    """An `ApiServer` whose `_lan_run` counts calls and answers in turn."""
    daemon = _CountingDaemon()
    srv = ApiServer(daemon, port=0)
    answers = list(statuses)

    async def fake_lan_run(action, payload, device_id=""):
        daemon.runs += 1
        status = answers.pop(0) if answers else 200
        return status, "application/json", json.dumps(
            {"ok": status == 200, "run": daemon.runs}).encode()

    monkeypatch.setattr(srv, "_lan_run", fake_lan_run)
    return srv, daemon


@pytest.mark.asyncio
async def test_a_replayed_token_returns_the_original_and_runs_once(monkeypatch):
    srv, daemon = _server(monkeypatch, [200])
    payload = {"action": "dismiss", "session_id": "s",
               "command_token": "abcd1234"}
    first = await srv._sealed_run("action", payload, "dev",
                                  actions=("dismiss",), check_lease=False,
                                  record=False)
    second = await srv._sealed_run("action", dict(payload), "dev",
                                   actions=("dismiss",), check_lease=False,
                                   record=False)
    assert first == second
    assert daemon.runs == 1
    assert json.loads(first[2])["run"] == 1


@pytest.mark.asyncio
async def test_a_409_is_recorded_and_replayed(monkeypatch):
    """Both are final answers about this press; a person's RETRY mints a new
    token, so recording a refusal freezes nobody out."""
    srv, daemon = _server(monkeypatch, [409])
    payload = {"action": "dismiss", "command_token": "abcd1234"}
    first = await srv._sealed_run("action", payload, "dev",
                                  actions=("dismiss",), check_lease=False,
                                  record=False)
    assert first[0] == 409
    second = await srv._sealed_run("action", dict(payload), "dev",
                                   actions=("dismiss",), check_lease=False,
                                   record=False)
    assert second == first
    assert daemon.runs == 1


@pytest.mark.asyncio
async def test_an_absent_token_runs_every_time(monkeypatch):
    srv, daemon = _server(monkeypatch, [200, 200])
    for _ in range(2):
        await srv._sealed_run("action", {"action": "dismiss"}, "dev",
                              actions=("dismiss",), check_lease=False,
                              record=False)
    assert daemon.runs == 2


@pytest.mark.asyncio
async def test_a_malformed_token_runs_every_time(monkeypatch):
    srv, daemon = _server(monkeypatch, [200, 200])
    for _ in range(2):
        await srv._sealed_run(
            "action", {"action": "dismiss", "command_token": "nope"}, "dev",
            actions=("dismiss",), check_lease=False, record=False)
    assert daemon.runs == 2


@pytest.mark.asyncio
async def test_an_unchosen_action_404s_before_the_ledger(monkeypatch):
    srv, daemon = _server(monkeypatch, [200])
    status, _ctype, _body = await srv._sealed_run(
        "action", {"action": "stop_session", "command_token": "abcd1234"},
        "dev", actions=("dismiss",), check_lease=False, record=False)
    assert status == 404
    assert daemon.runs == 0
    assert srv._receipts.look_up("dev", "abcd1234") is None


@pytest.mark.asyncio
async def test_a_lapsed_lease_refuses_before_the_ledger_is_consulted(
        monkeypatch):
    """A token minted at home must never replay out of a lapsed away window,
    which is the whole reason the lookup sits below the lease check."""
    srv, daemon = _server(monkeypatch, [200])
    payload = {"action": "dismiss", "command_token": "abcd1234"}
    # At home first: the answer is recorded.
    await srv._sealed_run("action", dict(payload), "dev",
                          actions=("dismiss",), check_lease=False, record=False)
    assert srv._receipts.look_up("dev", "abcd1234") is not None

    monkeypatch.setattr(relay, "lease_valid", lambda _d: False)
    status, _ctype, body = await srv._sealed_run(
        "action", dict(payload), "dev", actions=("dismiss",),
        check_lease=True, record=False)
    assert status == 403
    assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
    assert daemon.runs == 1


# --- prepare_card's lost reply -------------------------------------------------


@pytest.mark.asyncio
async def test_a_prepare_still_being_written_is_a_202_the_ledger_skips(monkeypatch):
    """The phone's PREPARE from away (21 Sep 2026): a reply lost on the relay
    is asked for again under the same `command_token`. A replay that lands
    while the helper is still running meets `prepare_card_text`'s
    single-flight refusal; that answer is a 202 — **not recorded** — so the
    next replay reaches the daemon again and is handed the finished draft
    the ledger recorded from the first press."""
    from dark_army_daemon import daemon_board

    daemon = _CountingDaemon()
    srv = ApiServer(daemon, port=0)
    outcomes = [
        (None, daemon_board.PREPARE_BUSY_DETAIL),
        ({"prompt": "Do the thing.", "workflow": ""}, ""),
    ]

    async def fake_prepare(_fields):
        daemon.runs += 1
        return outcomes.pop(0)

    daemon.prepare_card_text = fake_prepare
    payload = {"action": "prepare_card", "summary": "s", "root": "/tmp",
               "command_token": "press-0001-prepare"}
    run = lambda: srv._sealed_run(  # noqa: E731
        "action", dict(payload), "dev", actions=("prepare_card",),
        check_lease=False, record=False)
    busy = await run()
    assert busy[0] == 202
    body = json.loads(busy[2])
    assert body["ok"] is False
    assert body["detail"] == daemon_board.PREPARE_BUSY_DETAIL
    assert len(srv._receipts) == 0
    done = await run()
    assert done[0] == 200
    assert json.loads(done[2])["prompt"] == "Do the thing."
    assert len(srv._receipts) == 1
    replayed = await run()
    assert replayed == done
    assert daemon.runs == 2


@pytest.mark.asyncio
async def test_every_other_prepare_refusal_is_still_a_409():
    daemon = _CountingDaemon()
    srv = ApiServer(daemon, port=0)

    async def fake_prepare(_fields):
        return None, "a card needs a description"

    daemon.prepare_card_text = fake_prepare
    status, _ctype, body = await srv._prepare({"summary": ""})
    assert status == 409
    assert json.loads(body)["detail"] == "a card needs a description"


def test_the_busy_sentence_has_one_spelling():
    """`daemon_board.prepare_card_text` answers it and `api_server._prepare`
    recognises it; a second spelling on either side would turn the 202 back
    into a recorded 409."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "dark_army_daemon"
    board_src = (root / "daemon_board.py").read_text()
    api_src = (root / "api_server.py").read_text()
    assert board_src.count("already writing one") == 1
    assert "already writing one" not in api_src
    assert "detail == daemon_board.PREPARE_BUSY_DETAIL" in api_src

