"""Rebuild & restart's daemon half: the door, the hop, the snapshot and the stamp.

Three entry points start one rebuild; the phone's is `rebuild_app`, chosen on
both phone tuples (it runs `build.sh` from the working tree as it stands, and
away it rides the lease like every write). The menu-bar app owns the one
in-flight gate; the daemon's whole part is to hand the press over and publish
the facts. See `test_menubar.py` for the app's side.
"""
import json

import pytest

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import paths
from dark_army_daemon import rebuild_state
from dark_army_daemon.api_server import ApiServer, DESK_TOKEN_REFUSAL, _Request
from dark_army_daemon.daemon import BobDaemon

FORBIDDEN = ("repo_root", "path", "token", "key")


class _StubDaemon:
    _agents_poller = None

    def __init__(self, answer=(True, rebuild_state.REBUILD_STARTED)):
        self.answer = answer
        self.calls = 0

    def request_rebuild(self, token=""):
        self.calls += 1
        self.tokens = getattr(self, "tokens", []) + [token]
        return self.answer

    def rebuild_snapshot(self):
        return {"available": True, "label": "Rebuild & Reload",
                "rebuilding": False}


def _post(token=None, origin=None):
    headers = {"content-type": "application/json"}
    if token:
        headers["x-bob-token"] = token
    if origin:
        headers["origin"] = origin
    return _Request("POST", "/api/action", {}, headers,
                    b'{"action": "rebuild_app"}')


def _server(daemon):
    server = ApiServer(daemon)
    server.token = "desk-secret"
    server.session_token = "session-secret"
    return server


# --- the tuples ---------------------------------------------------------------

def test_the_verb_is_chosen_on_both_doors():
    assert ApiServer.LAN_ACTIONS.count("rebuild_app") == 1
    assert ApiServer.REMOTE_ACTIONS.count("rebuild_app") == 1
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


# --- the loopback door --------------------------------------------------------

@pytest.mark.asyncio
async def test_the_desk_token_asks_once_and_hears_started():
    daemon = _StubDaemon()
    server = _server(daemon)
    request = _post("desk-secret")
    action = server._app_request(request)
    assert action == "rebuild_app"
    status, _ctype, body = await server._app_action(action)
    assert status == 200
    assert json.loads(body) == {"ok": True, "detail": rebuild_state.REBUILD_STARTED}
    assert daemon.calls == 1


def test_the_session_token_is_turned_away_in_words():
    daemon = _StubDaemon()
    server = _server(daemon)
    request = _post("session-secret")
    assert server._app_request(request) is None
    status, _ctype, body = server._route(request)
    assert status == 403
    assert json.loads(body)["detail"] == DESK_TOKEN_REFUSAL
    assert daemon.calls == 0


def test_a_foreign_origin_is_refused():
    server = _server(_StubDaemon())
    assert server._app_request(_post("desk-secret", "http://evil.example")) is None


def test_no_token_is_refused():
    server = _server(_StubDaemon())
    assert server._app_request(_post()) is None


@pytest.mark.asyncio
async def test_a_refusal_is_a_409_in_the_daemons_words():
    daemon = _StubDaemon((False, rebuild_state.REBUILD_UNAVAILABLE_REFUSAL))
    server = _server(daemon)
    status, _ctype, body = await server._app_action("rebuild_app")
    assert status == 409
    assert json.loads(body)["detail"] == rebuild_state.REBUILD_UNAVAILABLE_REFUSAL


# --- the sealed doors ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_sealed_home_frame_runs_it():
    daemon = _StubDaemon()
    server = _server(daemon)
    status, _ctype, body = await server._sealed_run(
        "action", {"action": "rebuild_app", "root": "/evil"}, "phone-1",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 200 and daemon.calls == 1
    assert json.loads(body)["detail"] == rebuild_state.REBUILD_STARTED


def _away(monkeypatch, *, lease=True, bot=False, bot_write=True):
    from dark_army_daemon import devices, relay
    monkeypatch.setattr(relay, "lease_valid", lambda _d: lease)
    monkeypatch.setattr(devices, "is_bot", lambda _d: bot)
    monkeypatch.setattr(relay, "materialise_bot_grants", lambda _d: None,
                        raising=False)
    monkeypatch.setattr(relay, "bot_grant_valid",
                        lambda _d, _side: bot_write)


@pytest.mark.asyncio
async def test_a_relay_frame_inside_the_lease_asks_once(monkeypatch):
    _away(monkeypatch)
    daemon = _StubDaemon()
    server = _server(daemon)
    status, _ctype, _body = await server._remote_run(
        "action", {"action": "rebuild_app"}, "phone-1")
    assert status == 200
    assert daemon.calls == 1


@pytest.mark.asyncio
async def test_an_away_press_on_a_lapsed_lease_runs_nothing(monkeypatch):
    from dark_army_daemon import relay
    _away(monkeypatch, lease=False)
    daemon = _StubDaemon()
    server = _server(daemon)
    status, _ctype, body = await server._remote_run(
        "action", {"action": "rebuild_app"}, "phone-1")
    assert status == 403
    assert json.loads(body)["detail"] == relay.LEASE_REFUSAL
    assert daemon.calls == 0


@pytest.mark.asyncio
async def test_the_bot_without_write_is_refused_away(monkeypatch):
    from dark_army_daemon import relay
    _away(monkeypatch, bot=True, bot_write=False)
    daemon = _StubDaemon()
    server = _server(daemon)
    status, _ctype, body = await server._remote_run(
        "action", {"action": "rebuild_app"}, "bot-1")
    assert status == 403
    assert json.loads(body)["detail"] == relay.BOT_WRITE_REFUSAL
    assert daemon.calls == 0


@pytest.mark.asyncio
async def test_a_replayed_away_press_runs_once(monkeypatch):
    _away(monkeypatch)
    daemon = _StubDaemon()
    server = _server(daemon)
    payload = {"action": "rebuild_app", "command_token": "0123456789abcdef"}
    first = await server._remote_run("action", dict(payload), "phone-1")
    second = await server._remote_run("action", dict(payload), "phone-1")
    assert first == second
    assert daemon.calls == 1


@pytest.mark.asyncio
async def test_an_away_press_reaches_the_menu_bar_once(monkeypatch):
    _away(monkeypatch)
    d = _daemon(available=True)
    observer = _Observer()
    d.add_observer(observer)
    server = _server(d)
    status, _ctype, body = await server._remote_run(
        "action", {"action": "rebuild_app"}, "phone-1")
    assert status == 200
    assert json.loads(body)["detail"] == rebuild_state.REBUILD_STARTED
    assert observer.requests == 1
    d._rebuild["rebuilding"] = True
    status, _ctype, body = await server._remote_run(
        "action", {"action": "rebuild_app"}, "phone-1")
    assert status == 200
    assert json.loads(body)["detail"] == rebuild_state.REBUILD_ALREADY
    assert observer.requests == 1


@pytest.mark.asyncio
async def test_the_away_press_is_listed_as_remote_activity(monkeypatch):
    _away(monkeypatch)
    daemon = _StubDaemon()
    seen = []
    daemon.record_remote_action = lambda *a: seen.append(a)
    server = _server(daemon)
    await server._remote_run("action", {"action": "rebuild_app"}, "phone-1")
    assert seen == [("phone-1", "rebuild_app", True)]


def test_the_away_marker_is_published():
    from dark_army_daemon.daemon_board import BoardVerbsMixin

    class _Stub(BoardVerbsMixin):
        def _observers_implementing(self, name):
            return False

        def _board_projects(self):
            return []

    assert _Stub()._pipeline_writable()["rebuild_away_supported"] is True


# --- the section --------------------------------------------------------------

def test_the_section_rides_state_and_is_omittable():
    server = _server(_StubDaemon())
    assert server.state()["rebuild"] == _StubDaemon().rebuild_snapshot()
    sections = api_mod._OMITTABLE_SECTIONS
    assert "rebuild" in sections
    assert sections.index("rebuild") > sections.index("power")


def test_a_daemon_without_the_snapshot_publishes_an_empty_section():
    class _Old:
        _agents_poller = None
    assert ApiServer(_Old()).state()["rebuild"] == {}


def test_a_rebuild_change_broadcasts():
    server = _server(_StubDaemon())
    sent = []
    server._broadcast = lambda *a, **k: sent.append(1)
    server.on_rebuild_change({"rebuilding": True})
    assert sent == [1]


# --- the daemon's verb --------------------------------------------------------

class _Observer:
    def __init__(self):
        self.requests = 0
        self.changes = []

    def on_rebuild_request(self):
        self.requests += 1

    def on_rebuild_change(self, facts):
        self.changes.append(facts)


def _daemon(**facts):
    d = BobDaemon()
    d._rebuild = {**rebuild_state.snapshot_defaults(), **facts}
    return d


def test_snapshot_carries_eight_keys_and_no_path():
    snap = _daemon(available=True, label="Rebuild & Reload").rebuild_snapshot()
    assert sorted(snap) == sorted([
        "available", "label", "rebuilding", "restarting", "started_at", "last_outcome",
        "last_finished_at", "last_error"])
    assert not [k for k in snap if any(f in k for f in FORBIDDEN)]


def test_with_no_source_the_verb_says_so():
    d = _daemon(available=False)
    d.add_observer(_Observer())
    assert d.request_rebuild() == (False, rebuild_state.REBUILD_UNAVAILABLE_REFUSAL)


def test_with_nobody_listening_the_verb_says_so():
    d = _daemon(available=True)
    assert d.request_rebuild() == (False, rebuild_state.REBUILD_UNREACHABLE_REFUSAL)


def test_a_press_while_rebuilding_is_a_quiet_yes():
    d = _daemon(available=True, rebuilding=True)
    observer = _Observer()
    d.add_observer(observer)
    assert d.request_rebuild() == (True, rebuild_state.REBUILD_ALREADY)
    assert observer.requests == 0


def test_a_press_hands_one_request_to_the_app():
    d = _daemon(available=True)
    observer = _Observer()
    d.add_observer(observer)
    assert d.request_rebuild() == (True, rebuild_state.REBUILD_STARTED)
    assert observer.requests == 1


def test_set_rebuild_state_merges_and_tells_observers():
    d = _daemon(available=True, label="Rebuild & Reload")
    observer = _Observer()
    d.add_observer(observer)
    d.set_rebuild_state({"rebuilding": True, "started_at": 5.0, "junk": 1,
                         "last_error": "x" * 900})
    snap = d.rebuild_snapshot()
    assert snap["rebuilding"] is True and snap["started_at"] == 5.0
    assert snap["label"] == "Rebuild & Reload"
    assert "junk" not in snap
    assert len(snap["last_error"]) == rebuild_state.ERROR_LIMIT
    assert observer.changes and observer.changes[-1]["rebuilding"] is True


def test_a_fresh_daemon_is_seeded_from_the_stamp():
    rebuild_state.write_stamp(10.0, 20.0, True)
    snap = BobDaemon().rebuild_snapshot()
    assert snap["last_outcome"] == "ok" and snap["last_finished_at"] == 20.0
    assert snap["rebuilding"] is False


# --- the stamp ----------------------------------------------------------------

def test_the_stamp_round_trips_and_keeps_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    assert rebuild_state.read_stamp() == {}
    (tmp_path / "stamp.json").write_text(json.dumps({"future": 7}))
    rebuild_state.write_stamp(1.5, 2.5, True)
    stamp = rebuild_state.read_stamp()
    assert stamp == {"future": 7, "started_at": 1.5, "finished_at": 2.5, "ok": True}


def test_a_corrupt_stamp_reads_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    (tmp_path / "stamp.json").write_text("{not json")
    assert rebuild_state.read_stamp() == {}
    (tmp_path / "stamp.json").write_text("[1]")
    assert rebuild_state.read_stamp() == {}


def test_the_cached_stamp_follows_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    assert rebuild_state.cached_stamp() == {}
    rebuild_state.write_stamp(1.0, 2.0, True)
    assert rebuild_state.cached_stamp()["ok"] is True
    rebuild_state.write_stamp(1.0, 3.0, False)
    assert rebuild_state.cached_stamp()["ok"] is False


# --- a restart under way, a replay after one, and the error's home folder ------

def test_a_press_while_restarting_is_refused_in_words():
    d = _daemon(available=True, restarting=True)
    observer = _Observer()
    d.add_observer(observer)
    assert d.request_rebuild() == (False, rebuild_state.REBUILD_RESTARTING_REFUSAL)
    assert observer.requests == 0


def test_the_same_press_asked_again_after_the_restart_is_not_run_again(
        tmp_path, monkeypatch):
    """A phone press whose reply was lost can be replayed to the fresh daemon,
    whose receipt ledger is empty: the stamp remembers the press's token."""
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    first = _daemon(available=True)
    observer = _Observer()
    first.add_observer(observer)
    assert first.request_rebuild("tok-1") == (True, rebuild_state.REBUILD_STARTED)
    rebuild_state.write_stamp(1.0, 2.0, True)      # the app's success stamp
    fresh = BobDaemon()
    fresh._rebuild = {**rebuild_state.snapshot_defaults(), "available": True}
    again = _Observer()
    fresh.add_observer(again)
    assert fresh.request_rebuild("tok-1") == (True, rebuild_state.REBUILD_REPLAYED)
    assert again.requests == 0


def test_a_different_press_is_never_blocked_after_a_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    rebuild_state.note_press("tok-1")
    rebuild_state.write_stamp(1.0, 2.0, True)
    d = _daemon(available=True, last_outcome="ok", last_finished_at=3.0)
    observer = _Observer()
    d.add_observer(observer)
    assert d.request_rebuild("tok-2") == (True, rebuild_state.REBUILD_STARTED)
    assert d.request_rebuild("") == (True, rebuild_state.REBUILD_STARTED)
    assert observer.requests == 2
    assert rebuild_state.read_stamp()["token"] == "tok-2"


def test_the_stamp_keeps_the_token_through_the_apps_write(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    rebuild_state.note_press("tok-9")
    rebuild_state.write_stamp(1.0, 2.0, True)
    assert rebuild_state.read_stamp()["token"] == "tok-9"
    assert rebuild_state.replayed("tok-9") and not rebuild_state.replayed("tok-8")
    assert not rebuild_state.replayed("")


@pytest.mark.asyncio
async def test_the_sealed_door_hands_the_press_token_to_the_verb():
    daemon = _StubDaemon()
    server = _server(daemon)
    await server._sealed_run(
        "action", {"action": "rebuild_app", "command_token": "0123456789abcdef"},
        "phone-1", actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert daemon.tokens == ["0123456789abcdef"]


def test_a_failed_rebuild_never_blocks_a_retry():
    import time
    d = _daemon(available=True, last_outcome="failed",
                last_finished_at=time.time() - 5)
    d.add_observer(_Observer())
    assert d.request_rebuild() == (True, rebuild_state.REBUILD_STARTED)


def test_the_published_error_never_names_the_home_folder():
    from pathlib import Path
    home = str(Path.home())
    d = _daemon(available=True)
    d.set_rebuild_state({"last_outcome": "failed",
                         "last_error": f"error: {home}/Code/x/File.swift:3: boom"})
    err = d.rebuild_snapshot()["last_error"]
    assert home not in err
    assert "~/Code/x/File.swift" in err
    assert len(rebuild_state.redact_error("x" * 900)) == rebuild_state.ERROR_LIMIT


def test_a_path_cut_by_the_tail_is_redacted_before_the_cut():
    from pathlib import Path
    home = str(Path.home())
    text = ("y" * 400) + f" {home}/a/b"
    assert home not in rebuild_state.redact_error(text)


def test_the_phone_never_replays_a_rebuild_whose_reply_was_lost():
    """A source pin on the phone's sender: `flushReceipts` closes a
    `rebuild_app` receipt instead of re-sending it under its old token."""
    from pathlib import Path
    ios = Path(__file__).resolve().parents[2] / "ios" / "BobPhone"
    client = (ios / "Client.swift").read_text()
    receipts = (ios / "Receipts.swift").read_text()
    assert "ReceiptLedger.neverReplays(receipt)" in client
    assert client.index("ReceiptLedger.neverReplays(receipt)") \
        < client.index("receipts.markSending(receipt.id)")
    # Replay -> never sent (attempts > 0); RETRY -> sent once, because retry()
    # resets attempts to 0 under a fresh token.
    assert "neverReplayed.contains(receipt.action) && receipt.attempts > 0" in receipts
    retry = receipts.split("func retry(", 1)[1][:1500]
    assert "again.attempts = 0" in retry and "again.id = UUID().uuidString" in retry
    assert "neverReplayed: Set<String> = [PhoneActions.rebuildApp]" in receipts
    assert "notReplayedLine,\n    ]" in receipts.split(
        "static let phoneAuthored", 1)[1][:300]
