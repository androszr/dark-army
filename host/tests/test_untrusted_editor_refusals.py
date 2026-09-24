# host/tests/test_untrusted_editor_refusals.py
"""The editor extension's Workspace Trust refusal reaches the person in words.

In a folder VS Code has not trusted, the 0.1.21 bridge still activates but
refuses to type into a terminal, replying ``{"matched": false, "sent": false,
"error": "this folder is not trusted in VS Code — …"}``. These pin that the
daemon carries that sentence to the person instead of a generic "no window"
refusal from every verb that types — the typed reply, wrap-up, Low priority,
a question's first keystroke and a card message — and that a refused Codex native reply — nothing typed — releases the
one-reply-per-turn claim so the person can try again once the folder is
trusted. The bridge side is pinned in `test_vscode_extension.py`.
"""
import asyncio

import pytest

from dark_army_daemon import daemon as dmod
from dark_army_daemon import session_io, vscode_reveal
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
# Shared pytest fixtures: the stopped native Codex session the reply targets.
from tests.test_board_refine import refinement_handoff  # noqa: F401 — fixture dependency
from tests.test_codex_human_close import stopped_native  # noqa: F401 — fixture dependency
from tests.test_codex_native_reply import native_reply  # noqa: F401 — the fixture used below

UNTRUSTED = ("this folder is not trusted in VS Code — choose Trust in the "
             "Workspace Trust banner, then try again")


def _untrusted_reply():
    return {"matched": False, "sent": False, "error": UNTRUSTED}


# ── vscode_reveal.send_text hands an owning window's refusal up ──────────────

def _two_locks(monkeypatch, replies):
    locks = [{"port": 7001, "authToken": "a", "extensionVersion": "0.1.21"},
             {"port": 7002, "authToken": "b", "extensionVersion": "0.1.21"}]
    monkeypatch.setattr(vscode_reveal, "_send_capable_locks", lambda: locks)

    async def post(port, token, body, timeout=3.0):
        return replies[port]
    monkeypatch.setattr(vscode_reveal, "_post_json", post)


def test_an_owning_windows_trust_refusal_is_handed_up_not_sent(monkeypatch):
    _two_locks(monkeypatch, {7001: {"matched": False, "sent": False},
                             7002: _untrusted_reply()})
    reply = asyncio.run(vscode_reveal.send_text(4242, "ttys001", "/compact"))
    assert reply == {"matched": False, "sent": False, "error": UNTRUSTED}


def test_a_matched_window_still_wins_over_a_refusal(monkeypatch):
    typed = {"matched": True, "sent": True, "terminalName": "claude"}
    _two_locks(monkeypatch, {7001: _untrusted_reply(), 7002: typed})
    assert asyncio.run(vscode_reveal.send_text(4242, "ttys001", "hi")) == typed


@pytest.mark.parametrize("reply", [
    {"error": "unknown op: send_text"},            # an old window: no `sent`
    {"matched": False, "sent": False},             # a silent miss
    {"matched": False, "sent": False, "error": "   "},
])
def test_anything_but_an_explicit_refusal_is_still_no_window(monkeypatch, reply):
    _two_locks(monkeypatch, {7001: reply, 7002: {"matched": False, "sent": False}})
    assert asyncio.run(vscode_reveal.send_text(4242, "ttys001", "hi")) is None


# ── the typed reply and wrap-up say the editor's words ───────────────────────

@pytest.fixture
def typing_daemon(monkeypatch):
    d = BobDaemon()
    monkeypatch.setattr(d, "_session_provider", lambda sid: "claude")
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {})
    monkeypatch.setattr(d, "_reconciled_categories", lambda: {"s1": "sleeping"})
    monkeypatch.setattr(d, "_roster_pid", lambda sid: 4242)
    monkeypatch.setattr(session_io, "can_send_text", lambda pid: True)
    return d


@pytest.mark.parametrize("reply,words", [
    (_untrusted_reply(), UNTRUSTED),
    (None, dmod.NO_TYPING_WINDOW_REFUSAL),
    ({"matched": False, "sent": False}, dmod.NO_TYPING_WINDOW_REFUSAL),
])
def test_a_typed_reply_refused_by_the_editor_says_why(typing_daemon, monkeypatch,
                                                      reply, words):
    async def send(pid, tty, text, newline=True):
        return reply
    monkeypatch.setattr(session_io, "send_text", send)
    assert asyncio.run(typing_daemon._reply_by_typing("s1", "Continue")) == (False, words)


@pytest.mark.parametrize("reply,words", [
    (_untrusted_reply(), UNTRUSTED),
    (None, dmod.NO_TYPING_WINDOW_REFUSAL),
])
def test_a_wrap_up_refused_by_the_editor_says_why(typing_daemon, monkeypatch,
                                                  reply, words):
    dismissed = []

    async def send(pid, tty, text, newline=True):
        return reply

    async def dismiss(sid):
        dismissed.append(sid)
    monkeypatch.setattr(session_io, "send_text", send)
    monkeypatch.setattr(typing_daemon, "dismiss_notification", dismiss)
    assert asyncio.run(typing_daemon.wrap_up_session("s1")) == (False, words)
    assert dismissed == []


# ── the other three typing verbs say the editor's words ───────────────────────
# Each recipe is rebuilt from its own suite (`test_low_priority._daemon`,
# `test_multi_select_question._asked_daemon`, `test_card_message._live`)
# rather than imported, so their module-level fixtures stay where they are.

_REFUSALS = [
    (_untrusted_reply(), UNTRUSTED),
    (None, dmod.NO_TYPING_WINDOW_REFUSAL),
    ({"matched": False, "sent": False}, dmod.NO_TYPING_WINDOW_REFUSAL),
]


def _stub_editor(monkeypatch, replies):
    """`vscode_reveal.send_text` answering each call from `replies` in turn,
    the last one repeating."""
    calls = []

    async def send(pid, tty, text, newline=True):
        calls.append(text)
        return replies[min(len(calls), len(replies)) - 1]
    monkeypatch.setattr(vscode_reveal, "send_text", send)
    return calls


@pytest.mark.parametrize("reply,words", _REFUSALS)
def test_low_priority_refused_by_the_editor_says_why(monkeypatch, reply, words):
    _stub_editor(monkeypatch, [reply])
    d = BobDaemon(headless=True)
    d._session_states["s1"] = {"pid": 4242, "last_event": 0, "state": "error",
                               "provider": "claude"}
    d._active_notifications["s1"] = {"session_id": "s1", "hook": "StopFailure",
                                     "error_kind": "rate_limit",
                                     "message": "You've hit your limit"}
    assert asyncio.run(d.low_priority_session("s1")) == (False, words)
    assert "s1" in d._active_notifications
    assert d._session_states["s1"]["state"] == "error"
    assert "s1" not in d._low_priority_sent


def _pick_one(n):
    return {"text": f"Q{n}?", "options": ["A", "B"], "header": "",
            "id": "tu_1", "index": n}


@pytest.fixture
def no_key_gap(monkeypatch):
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        await real_sleep(0)
    monkeypatch.setattr("asyncio.sleep", sleep)


@pytest.mark.parametrize("reply,words", _REFUSALS)
def test_a_question_answer_refused_by_the_editor_on_its_first_key_says_why(
        monkeypatch, no_key_gap, reply, words):
    calls = _stub_editor(monkeypatch, [reply])
    d = BobDaemon()
    result = asyncio.run(d._type_answer_burst("s1", 4242, [_pick_one(0)], [[0]]))
    assert result == (False, words)
    assert len(calls) == 1
    assert "s1" not in d._answering


def test_a_question_answer_refused_mid_burst_keeps_the_count(monkeypatch,
                                                             no_key_gap):
    """Only the first keystroke takes the editor's words: once one has
    landed, the person is sent to the terminal to finish."""
    _stub_editor(monkeypatch, [{"matched": True, "sent": True,
                                "terminalName": "zsh"}, _untrusted_reply()])
    d = BobDaemon()
    ok, detail = asyncio.run(d._type_answer_burst(
        "s1", 4242, [_pick_one(0), _pick_one(1)], [[0], [1]]))
    assert ok is False
    assert detail.startswith("Answered 1 of 2")
    assert "finish the rest in the terminal" in detail
    assert UNTRUSTED not in detail
    assert "s1" not in d._answering


@pytest.mark.parametrize("reply,words", _REFUSALS)
def test_a_card_message_refused_by_the_editor_says_why(tmp_path, monkeypatch,
                                                       reply, words):
    _stub_editor(monkeypatch, [reply])
    d = BobDaemon(sessions_path=tmp_path / "sessions.json", headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        card, detail = store.create({"title": "do the thing", "project": "bob",
                                     "root": "/tmp", "prompt": "go",
                                     "tool": "claude",
                                     "summary": "make the thing work"})
        assert card is not None, detail
        store.update(card["id"], {"session_id": "sess-live",
                                  "link_state": "live"})
        d._session_states["sess-live"] = {"provider": "claude", "pid": 4242,
                                          "last_event": 0}
        assert asyncio.run(d.message_card(card["id"], "hi")) == (False, words)
        assert store.messages(card["id"]) == []
    finally:
        store.close()


# ── a refused Codex native reply releases the turn's claim ───────────────────

@pytest.mark.asyncio
async def test_an_untrusted_native_reply_says_so_and_can_be_retried(native_reply,
                                                                    monkeypatch):
    d, _, _, _, records, _, posts = native_reply
    sid = records[0].session_id
    answers = [_untrusted_reply(), {"matched": True, "sent": True}]

    async def post(port, token, body, **kwargs):
        # The claim is taken at the moment of the write, as in production.
        if not await kwargs["before_write"]():
            return None
        posts.append(body)
        return answers.pop(0)
    monkeypatch.setattr(vscode_reveal, "_post_json", post)

    assert await d.reply_to_session(sid, "Continue with the fix") == (False, UNTRUSTED)
    assert sid not in d._codex_reply_attempts
    # Trusted now: the same turn takes the reply, once.
    assert await d.reply_to_session(sid, "Continue with the fix") == (True, "")
    assert len(posts) == 2
    assert not (await d.reply_to_session(sid, "Again"))[0]
    assert len(posts) == 2


@pytest.mark.asyncio
async def test_a_silent_native_miss_keeps_the_claim(native_reply, monkeypatch):
    """No words, no release: the editor may have typed, so no second try."""
    d, _, _, _, records, _, posts = native_reply
    sid = records[0].session_id

    async def post(port, token, body, **kwargs):
        if not await kwargs["before_write"]():
            return None
        posts.append(body)
        return {"matched": False, "sent": False}
    monkeypatch.setattr(vscode_reveal, "_post_json", post)

    ok, words = await d.reply_to_session(sid, "Continue with the fix")
    assert not ok and "did not confirm submission" in words
    assert sid in d._codex_reply_attempts
    assert "already submitted" in (await d.reply_to_session(sid, "Again"))[1]
    assert len(posts) == 1
