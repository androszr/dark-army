"""A rate-limited Claude session offers a Low priority button.

`low_priority_session` is `wrap_up_session`'s sibling: `/low-priority` typed
onto the session's own input line through VS Code's `sendText`, then the card
dropped and the row moved out of Needs you. Its whole risk is the same as
wrap-up's — half of it can land — plus one of its own: the command is a
toggle, so a second press switches low priority back off. These tests pin
the bytes, the order, the four refusals, the cooldown, the reach flag
(`can_low_priority`), the API intercept and both phone doors.
"""
import asyncio
import time
import inspect

import pytest

from dark_army_daemon import daemon as dm
from dark_army_daemon.daemon import BobDaemon


RATE_LIMIT_CARD = {"session_id": "s1", "hook": "StopFailure",
                   "error_kind": "rate_limit",
                   "message": "You've hit your limit"}


def _daemon(monkeypatch, sent=None, result=None, provider="claude",
            card=RATE_LIMIT_CARD):
    from dark_army_daemon import vscode_reveal as vr

    async def _send(pid, tty, text, newline=True):
        if sent is not None:
            sent.append((pid, text))
        return result

    monkeypatch.setattr(vr, "send_text", _send)
    daemon = BobDaemon(headless=True)
    state = {"pid": 4242, "last_event": 0, "state": "error"}
    if provider is not None:
        state["provider"] = provider
    daemon._session_states["s1"] = state
    if card is not None:
        daemon._active_notifications["s1"] = dict(card)
    return daemon


LANDED = {"matched": True, "sent": True, "terminalName": "zsh"}


# --- the verb -----------------------------------------------------------------

def test_the_command_is_low_priority_behind_a_kill_line(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent, LANDED)
    ok, detail = asyncio.run(daemon.low_priority_session("s1"))
    assert (ok, detail) == (True, "")
    assert sent == [(4242, "\x15/low-priority")]
    assert dm.LOW_PRIORITY_COMMAND == "/low-priority"


def test_the_card_is_dropped_and_the_state_written_only_after_it_lands(monkeypatch):
    daemon = _daemon(monkeypatch, result=LANDED)
    assert asyncio.run(daemon.low_priority_session("s1"))[0] is True
    assert "s1" not in daemon._active_notifications
    st = daemon._session_states["s1"]
    assert st["state"] == "idle"
    assert st["last_event"] > 0
    assert "last_event_monotonic" in st
    # The row stays: Claude will carry on in low-priority mode.
    assert "s1" in daemon._session_states


def test_a_send_that_does_not_land_leaves_card_and_state_alone(monkeypatch):
    daemon = _daemon(monkeypatch, result=None)
    ok, detail = asyncio.run(daemon.low_priority_session("s1"))
    assert ok is False and "VS Code" in detail
    assert "s1" in daemon._active_notifications
    assert daemon._session_states["s1"]["state"] == "error"
    assert "s1" not in daemon._low_priority_sent


def test_an_unmatched_terminal_is_a_refusal_not_a_success(monkeypatch):
    daemon = _daemon(monkeypatch, result={"matched": True, "sent": False})
    ok, detail = asyncio.run(daemon.low_priority_session("s1"))
    assert ok is False and "VS Code" in detail
    assert daemon._session_states["s1"]["state"] == "error"
    assert "s1" in daemon._active_notifications


def test_a_session_with_no_pid_refuses_in_words(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent, LANDED)
    daemon._session_states["s1"].pop("pid")
    ok, detail = asyncio.run(daemon.low_priority_session("s1"))
    assert ok is False and "PID" in detail
    assert sent == []


def test_a_permission_prompt_blocks_the_command(monkeypatch):
    """`sendText` ends in a newline, which an open tool-approval dialog reads
    as confirm — the same trap wrap-up and auto-compact document."""
    sent = []
    daemon = _daemon(monkeypatch, sent, LANDED)
    monkeypatch.setattr(daemon, "_prompts_by_session",
                        lambda: {"s1": {"request_id": "r1"}})
    ok, detail = asyncio.run(daemon.low_priority_session("s1"))
    assert (ok, detail) == (False, dm.PROMPT_BLOCKED_REFUSAL)
    assert sent == []


@pytest.mark.parametrize("provider", ["grok", "codex", None])
def test_only_a_claude_session_is_switched(monkeypatch, provider):
    sent = []
    daemon = _daemon(monkeypatch, sent, LANDED, provider=provider)
    ok, detail = asyncio.run(daemon.low_priority_session("s1"))
    assert (ok, detail) == (False, dm.LOW_PRIORITY_NOT_CLAUDE_REFUSAL)
    assert sent == []
    assert "s1" in daemon._active_notifications


@pytest.mark.parametrize("card", [
    {"hook": "StopFailure", "error_kind": "billing_error", "message": "x"},
    {"hook": "Stop", "error_kind": "rate_limit", "message": "x"},
    {"hook": "Stop", "message": "Waiting for input"},
    None,
])
def test_only_a_rate_limit_stop_failure_card_qualifies(monkeypatch, card):
    sent = []
    daemon = _daemon(monkeypatch, sent, LANDED, card=card)
    ok, detail = asyncio.run(daemon.low_priority_session("s1"))
    assert (ok, detail) == (False, dm.LOW_PRIORITY_NOT_LIMITED_REFUSAL)
    assert sent == []


def test_a_second_press_within_the_cooldown_is_refused_and_types_nothing(monkeypatch):
    """`/low-priority` is a toggle: typed twice it switches back off."""
    sent = []
    daemon = _daemon(monkeypatch, sent, LANDED)
    assert asyncio.run(daemon.low_priority_session("s1"))[0] is True
    # The card comes back (another StopFailure) — the memory still refuses.
    daemon._active_notifications["s1"] = dict(RATE_LIMIT_CARD)
    ok, detail = asyncio.run(daemon.low_priority_session("s1"))
    assert (ok, detail) == (False, dm.LOW_PRIORITY_ALREADY_REFUSAL)
    assert sent == [(4242, "\x15/low-priority")]
    assert "s1" in daemon._active_notifications


def test_the_memory_expires_after_the_cooldown(monkeypatch):
    daemon = _daemon(monkeypatch, result=LANDED)
    assert asyncio.run(daemon.low_priority_session("s1"))[0] is True
    assert daemon._low_priority_recent("s1") is True
    daemon._low_priority_sent["s1"] -= dm.LOW_PRIORITY_COOLDOWN_SECONDS + 1
    assert daemon._low_priority_recent("s1") is False
    # The read is pure (the executor calls it); the prune is the loop's.
    assert "s1" in daemon._low_priority_sent
    daemon._prune_low_priority_sent()
    assert "s1" not in daemon._low_priority_sent
    assert dm.LOW_PRIORITY_COOLDOWN_SECONDS == 600.0


def test_the_predicate_never_writes(monkeypatch):
    """`_low_priority_recent` runs on the executor inside the enrich pass;
    a prune there could pop a stamp the loop had just rewritten."""
    import inspect
    src = inspect.getsource(dm.BobDaemon._low_priority_recent)
    assert ".pop(" not in src and "del " not in src
    daemon = _daemon(monkeypatch, result=LANDED)
    daemon._low_priority_sent["s1"] = time.monotonic() - (
        dm.LOW_PRIORITY_COOLDOWN_SECONDS + 1)
    assert daemon._low_priority_recent("s1") is False
    assert "s1" in daemon._low_priority_sent


def test_forgetting_the_session_clears_the_memory(monkeypatch):
    daemon = _daemon(monkeypatch, result=LANDED)
    assert asyncio.run(daemon.low_priority_session("s1"))[0] is True
    assert "s1" in daemon._low_priority_sent
    daemon._forget_session("s1", "ended")
    assert "s1" not in daemon._low_priority_sent


def test_the_verb_types_and_does_not_reach_for_the_channel_or_the_close():
    src = inspect.getsource(BobDaemon.low_priority_session)
    assert "send_text" in src
    assert "push_channel_event" not in src
    assert "close_terminal" not in src
    assert "LOW_PRIORITY_COMMAND" in src


def test_the_refusals_are_distinct_sentences():
    words = {dm.PROMPT_BLOCKED_REFUSAL, dm.LOW_PRIORITY_NOT_CLAUDE_REFUSAL,
             dm.LOW_PRIORITY_NOT_LIMITED_REFUSAL, dm.LOW_PRIORITY_ALREADY_REFUSAL}
    assert len(words) == 4
    assert "switch it off" in dm.LOW_PRIORITY_ALREADY_REFUSAL


# --- the reach flag -----------------------------------------------------------

def _reach(monkeypatch, stub, card=RATE_LIMIT_CARD, prompts=None,
           recent=False):
    from dark_army_daemon import vscode_reveal as vr

    asked = []

    def _can(pid, tty=""):
        asked.append(pid)
        return True

    monkeypatch.setattr(vr, "can_send_text", _can)
    monkeypatch.setattr(vr, "can_close_terminal", lambda pid, tty="": True)
    daemon = BobDaemon(headless=True)
    sid = stub["session_id"]
    if card is not None:
        daemon._active_notifications[sid] = dict(card, session_id=sid)
    if prompts:
        monkeypatch.setattr(daemon, "_prompts_by_session", lambda: prompts)
    if recent:
        import time
        daemon._low_priority_sent[sid] = time.monotonic()
    out = daemon._enrich_agent_stubs([dict(stub)])
    rows = [r for bucket in out.values() for r in bucket
            if r.get("session_id") == sid]
    assert len(rows) == 1
    return rows[0]["can_low_priority"], asked


WAITING = {"session_id": "s1", "pid": 4242, "_category": "waiting", "cwd": ""}


def test_can_low_priority_is_true_for_a_waiting_claude_row_with_the_card(monkeypatch):
    flag, asked = _reach(monkeypatch, WAITING)
    assert flag is True
    assert asked == [4242]


def test_can_low_priority_is_false_for_a_running_row_and_ps_is_not_asked(monkeypatch):
    flag, asked = _reach(monkeypatch, dict(WAITING, _category="running"))
    assert flag is False
    assert asked == []


@pytest.mark.parametrize("provider", ["grok", "codex"])
def test_can_low_priority_is_false_for_other_providers(monkeypatch, provider):
    flag, _ = _reach(monkeypatch, dict(WAITING, provider=provider))
    assert flag is False


def test_can_low_priority_is_false_for_a_background_row(monkeypatch):
    flag, _ = _reach(monkeypatch, dict(WAITING, kind="background"))
    assert flag is False


def test_can_low_priority_is_false_while_a_prompt_is_open(monkeypatch):
    flag, _ = _reach(monkeypatch, WAITING,
                     prompts={"s1": {"request_id": "r1"}})
    assert flag is False


@pytest.mark.parametrize("card", [
    {"hook": "StopFailure", "error_kind": "billing_error", "message": "x"},
    {"hook": "Stop", "message": "Waiting for input"},
    None,
])
def test_can_low_priority_is_false_without_a_rate_limit_card(monkeypatch, card):
    flag, _ = _reach(monkeypatch, WAITING, card=card)
    assert flag is False


def test_can_low_priority_is_false_within_the_cooldown(monkeypatch):
    flag, _ = _reach(monkeypatch, WAITING, recent=True)
    assert flag is False


def test_the_flag_is_published_on_every_row(monkeypatch):
    """An absent key would decode false on both surfaces anyway, but the
    daemon states it rather than leaving it to a default."""
    from dark_army_daemon import vscode_reveal as vr

    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": False)
    monkeypatch.setattr(vr, "can_close_terminal", lambda pid, tty="": False)
    daemon = BobDaemon(headless=True)
    out = daemon._enrich_agent_stubs([
        dict(WAITING), dict(WAITING, session_id="s2", _category="running"),
    ])
    for bucket in ("waiting", "running"):
        for row in out[bucket]:
            assert row["can_low_priority"] is False


# --- the API and the doors ----------------------------------------------------

class _StubDaemon:
    def __init__(self):
        self.calls = []

    async def low_priority_session(self, session_id):
        self.calls.append(session_id)
        return False, dm.LOW_PRIORITY_NOT_LIMITED_REFUSAL


@pytest.mark.asyncio
async def test_the_api_exposes_low_priority_and_reports_its_refusal():
    from dark_army_daemon import api_server

    daemon = _StubDaemon()
    server = api_server.ApiServer(daemon)
    server.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "", {"x-bob-token": "t"},
        b'{"action": "low_priority", "session_id": "s1"}')
    assert server._low_priority_request(request) == "s1"
    status, _ctype, body = await server._low_priority("s1")
    assert status == 409
    assert dm.LOW_PRIORITY_NOT_LIMITED_REFUSAL.encode("utf-8") in body
    assert daemon.calls == ["s1"]


def test_an_unauthorised_low_priority_is_not_routed():
    from dark_army_daemon import api_server

    server = api_server.ApiServer(object())
    server.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "", {},
        b'{"action": "low_priority", "session_id": "s1"}')
    assert server._low_priority_request(request) is None


def test_a_close_terminal_press_is_not_mistaken_for_low_priority():
    from dark_army_daemon import api_server

    server = api_server.ApiServer(object())
    server.token = "t"
    request = api_server._Request(
        "POST", "/api/action", "", {"x-bob-token": "t"},
        b'{"action": "close_terminal", "session_id": "s1"}')
    assert server._low_priority_request(request) is None


@pytest.mark.asyncio
async def test_the_lan_door_reaches_the_verb_and_400s_an_empty_session():
    from dark_army_daemon import api_server

    daemon = _StubDaemon()
    server = api_server.ApiServer(daemon)
    status, _ctype, body = await server._lan_run(
        "low_priority", {"session_id": "s1"}, "dev")
    assert status == 409
    assert dm.LOW_PRIORITY_NOT_LIMITED_REFUSAL.encode("utf-8") in body
    assert daemon.calls == ["s1"]
    status, _ctype, _body = await server._lan_run("low_priority", {}, "dev")
    assert status == 400
    assert daemon.calls == ["s1"]


def test_low_priority_is_on_both_doors_and_remote_stays_within_lan():
    from dark_army_daemon.api_server import ApiServer

    assert "low_priority" in ApiServer.LAN_ACTIONS
    assert "low_priority" in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
