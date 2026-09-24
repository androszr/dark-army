"""Reply by typing — the second delivery route for `reply_to_session`.

Behind `typed_reply_enabled` (preference `typed_reply`, off for the MVP), a
reply is put on the session's own input line through `send_text` — Ctrl-U,
the words, one Enter — with the channel as the fallback. Its whole risk is
the trailing Enter landing on something other than an empty input line, so
every check runs before the first keystroke and a refusal never types. These
tests pin the bytes, the order of the refusals, the fallback, the busy guard
and what `_enrich_agent_stubs` publishes as `channel` / `reply_via`.
"""
import asyncio

import pytest

from dark_army_daemon import daemon as dm
from dark_army_daemon.daemon import BobDaemon


LANDED = {"matched": True, "sent": True, "terminalName": "zsh"}


def _daemon(monkeypatch, sent=None, result=LANDED, can_type=True,
            category="waiting", provider="claude", typed=True,
            channel=False, pushed=None, prompts=None):
    from dark_army_daemon import vscode_reveal as vr

    async def _send(pid, tty, text, newline=True):
        if sent is not None:
            sent.append((pid, text, newline))
        return result

    monkeypatch.setattr(vr, "send_text", _send)
    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": can_type)
    daemon = BobDaemon(headless=True)
    daemon.typed_reply_enabled = typed
    state = {"pid": 4242, "last_event": 0, "state": "idle"}
    if provider is not None:
        state["provider"] = provider
    daemon._session_states["s1"] = state
    cats = {} if category is None else {"s1": category}
    monkeypatch.setattr(daemon, "_reconciled_categories", lambda: cats)
    monkeypatch.setattr(daemon, "_prompts_by_session", lambda: prompts or {})
    monkeypatch.setattr(daemon, "_channel_for_session",
                        lambda sid: object() if channel else None)

    async def _push(sid, text, extra=None):
        if pushed is not None:
            pushed.append((sid, text, extra))
        return True

    monkeypatch.setattr(daemon, "push_channel_event", _push)
    return daemon


def _reply(daemon, text="accept"):
    return asyncio.run(daemon.reply_to_session("s1", text))


# --- the switch ----------------------------------------------------------------

def test_flag_off_is_byte_identical_to_today(monkeypatch):
    """With the preference off the method body is today's: the channel
    refusal in its own words, and nothing typed."""
    sent = []
    daemon = _daemon(monkeypatch, sent, typed=False)
    ok, detail = _reply(daemon)
    assert ok is False
    assert "not started with Dark Army's channel" in detail
    assert sent == []


def test_flag_off_with_a_channel_still_pushes_and_never_types(monkeypatch):
    sent, pushed = [], []
    daemon = _daemon(monkeypatch, sent, typed=False, channel=True, pushed=pushed)
    assert _reply(daemon) == (True, "")
    assert sent == []
    assert pushed == [("s1", "accept", {"kind": "user"})]


def test_the_default_is_off():
    assert BobDaemon(headless=True).typed_reply_enabled is False


# --- the bytes -----------------------------------------------------------------

def test_a_waiting_reachable_row_gets_kill_line_text_and_enter(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent)
    assert _reply(daemon) == (True, "")
    assert sent == [(4242, "\x15accept", True)]


def test_typing_wins_over_a_channel_and_the_channel_is_not_pushed(monkeypatch):
    sent, pushed = [], []
    daemon = _daemon(monkeypatch, sent, channel=True, pushed=pushed)
    assert _reply(daemon) == (True, "")
    assert sent == [(4242, "\x15accept", True)]
    assert pushed == []


# --- refusals, none of which type ----------------------------------------------

def test_a_running_row_is_refused(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent, category="running")
    ok, detail = _reply(daemon)
    assert ok is False and "still working" in detail
    assert sent == []


def test_a_row_bob_has_no_state_for_is_refused(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent, category=None)
    ok, detail = _reply(daemon)
    assert ok is False and "has no state" in detail
    assert sent == []


def test_a_multi_line_reply_is_refused_not_collapsed(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent)
    for text in ("yes\nplease", "yes\r\nplease"):
        ok, detail = _reply(daemon, text)
        assert ok is False and "One line at a time" in detail
    assert sent == []


def test_a_multi_line_reply_falls_back_to_the_channel(monkeypatch):
    sent, pushed = [], []
    daemon = _daemon(monkeypatch, sent, channel=True, pushed=pushed)
    assert _reply(daemon, "yes\nplease") == (True, "")
    assert sent == []
    assert pushed == [("s1", "yes\nplease", {"kind": "user"})]


@pytest.mark.parametrize("first", ["/", "!", "#"])
def test_a_command_prefix_is_refused(monkeypatch, first):
    sent = []
    daemon = _daemon(monkeypatch, sent)
    ok, detail = _reply(daemon, first + "clear")
    assert ok is False and "command" in detail and f"'{first}'" in detail
    assert sent == []
    assert dm.TYPED_REPLY_REFUSED_PREFIXES == "/!#"


def test_an_open_permission_prompt_is_refused(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent, prompts={"s1": {"request_id": "r1"}})
    ok, detail = _reply(daemon)
    assert ok is False and "answer it first" in detail
    assert sent == []


def test_an_open_question_is_refused(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent)
    daemon._questions["s1"] = {"text": "Which?", "options": ["a", "b"]}
    ok, detail = _reply(daemon)
    assert ok is False and "use the option buttons" in detail
    assert sent == []


def test_a_burst_already_typing_is_refused(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent)
    daemon._answering.add("s1")
    ok, detail = _reply(daemon)
    assert ok is False and "Already typing" in detail
    assert sent == []


def test_no_pid_is_refused(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent)
    daemon._session_states["s1"].pop("pid")
    ok, detail = _reply(daemon)
    assert ok is False and "No PID" in detail
    assert sent == []


def test_no_typing_window_is_refused_and_names_vs_code(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent, can_type=False)
    ok, detail = _reply(daemon)
    assert ok is False and "VS Code" in detail
    assert sent == []


def test_a_send_that_does_not_land_is_refused(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent, result=None)
    ok, detail = _reply(daemon)
    assert ok is False and "No VS Code window owns" in detail
    assert len(sent) == 1


@pytest.mark.parametrize("kw", [{"can_type": False}, {"result": None}])
def test_an_unreachable_window_falls_back_to_the_channel(monkeypatch, kw):
    pushed = []
    daemon = _daemon(monkeypatch, channel=True, pushed=pushed, **kw)
    assert _reply(daemon) == (True, "")
    assert pushed == [("s1", "accept", {"kind": "user"})]


def test_codex_is_never_typed_at(monkeypatch):
    sent = []
    daemon = _daemon(monkeypatch, sent, provider="codex")
    ok, detail = _reply(daemon)
    assert ok is False and "Codex" in detail
    assert sent == []


def test_the_busy_guard_is_released_after_success_and_after_a_miss(monkeypatch):
    daemon = _daemon(monkeypatch)
    assert _reply(daemon) == (True, "")
    assert "s1" not in daemon._answering
    daemon = _daemon(monkeypatch, result=None)
    assert _reply(daemon)[0] is False
    assert "s1" not in daemon._answering


def test_the_busy_guard_is_held_across_the_send(monkeypatch):
    from dark_army_daemon import vscode_reveal as vr

    daemon = _daemon(monkeypatch)
    seen = []

    async def _send(pid, tty, text, newline=True):
        seen.append("s1" in daemon._answering)
        return LANDED

    monkeypatch.setattr(vr, "send_text", _send)
    assert _reply(daemon) == (True, "")
    assert seen == [True]


# --- what the row publishes ----------------------------------------------------

WAITING = {"session_id": "s1", "pid": 4242, "_category": "waiting", "cwd": ""}


def _row(monkeypatch, stub=WAITING, typed=True, can_type=True,
         reachable=False, prompts=None, question=None):
    from dark_army_daemon import vscode_reveal as vr

    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": can_type)
    monkeypatch.setattr(vr, "can_close_terminal", lambda pid, tty="": False)
    daemon = BobDaemon(headless=True)
    daemon.typed_reply_enabled = typed
    monkeypatch.setattr(daemon, "_session_reachable", lambda sid: reachable)
    if prompts:
        monkeypatch.setattr(daemon, "_prompts_by_session", lambda: prompts)
    if question:
        daemon._pending_questions["s1"] = dict(question)
    out = daemon._enrich_agent_stubs([dict(stub)])
    rows = [r for bucket in out.values() for r in bucket
            if r.get("session_id") == "s1"]
    assert len(rows) == 1
    return rows[0]


def test_flag_on_and_typable_publishes_channel_true_and_typed(monkeypatch):
    """The ordering pin: `reply_via` reads `can_type`, so it must be computed
    after it — on this row `channel` would otherwise be false."""
    row = _row(monkeypatch)
    assert row["can_type"] is True
    assert row["channel"] is True
    assert row["reply_via"] == "typed"


def test_flag_off_and_unreachable_publishes_neither(monkeypatch):
    row = _row(monkeypatch, typed=False)
    assert row["channel"] is False
    assert row["reply_via"] == ""


def test_flag_on_with_a_channel_still_says_typed(monkeypatch):
    row = _row(monkeypatch, reachable=True)
    assert row["channel"] is True
    assert row["reply_via"] == "typed"


def test_flag_off_with_a_channel_says_channel(monkeypatch):
    row = _row(monkeypatch, typed=False, reachable=True)
    assert row["channel"] is True
    assert row["reply_via"] == "channel"


def test_flag_on_and_running_publishes_none(monkeypatch):
    row = _row(monkeypatch, stub=dict(WAITING, _category="running"))
    assert row["channel"] is False
    assert row["reply_via"] == ""


def test_flag_on_and_an_open_prompt_publishes_none(monkeypatch):
    row = _row(monkeypatch, prompts={"s1": {"request_id": "r1"}})
    assert row["channel"] is False
    assert row["reply_via"] == ""


def test_flag_on_and_an_open_question_falls_back_to_the_channel_word(monkeypatch):
    """The dialog owns the input line, so typing is out; a channel, where one
    exists, is still the route and the row says so."""
    q = {"id": "q1", "text": "Which?", "options": ["a", "b"]}
    row = _row(monkeypatch, question=q)
    assert row["reply_via"] == ""
    row = _row(monkeypatch, question=q, reachable=True)
    assert row["reply_via"] == "channel"


def test_flag_on_but_not_typable_falls_back_to_reachability(monkeypatch):
    row = _row(monkeypatch, can_type=False)
    assert (row["channel"], row["reply_via"]) == (False, "")
    row = _row(monkeypatch, can_type=False, reachable=True)
    assert (row["channel"], row["reply_via"]) == (True, "channel")


def test_session_reachable_is_not_widened(monkeypatch):
    """`_decide_permission_ask`'s live-channel refusal reads it; the typed
    route widens the published `channel`, never this."""
    daemon = BobDaemon(headless=True)
    daemon.typed_reply_enabled = True
    daemon._session_states["s1"] = {"pid": 4242, "last_event": 0,
                                    "provider": "claude"}
    assert daemon._session_reachable("s1") is False
