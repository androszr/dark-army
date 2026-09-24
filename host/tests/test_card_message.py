# host/tests/test_card_message.py
"""Send a message to the assistant working a card.

`message_card` is `ask_card`'s sibling and a different act: `ask_card` reaches
a session down its channel or starts a helper, and a board-dispatched session
has no channel. So this one types — `vscode_reveal.send_text` onto the client's
own input line, behind `INPUT_LINE_CLEAR`, the route `/compact`, `/clear` and
`/low-priority` already take.

These tests pin the bytes, the record and its ordering, the eight refusals by
constant identity, and the `can_message` matrix — including the cost gate, that
`can_send_text` is never asked for a row which is not working a live card.
"""

import inspect

import pytest

from dark_army_daemon import board
from dark_army_daemon import daemon as dm
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


LANDED = {"matched": True, "sent": True, "terminalName": "zsh"}


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json", headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _make(store, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": "/tmp",
              "prompt": "go", "tool": "claude",
              "summary": "make the thing work"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _live(d, store, provider="claude", pid=4242, **kw):
    """A card bound live to a session Dark Army has a pid for."""
    card = _make(store, **kw)
    store.update(card["id"], {"session_id": "sess-live",
                              "link_state": "live"})
    d._session_states["sess-live"] = {"provider": provider, "pid": pid,
                                      "last_event": 0}
    return store.get(card["id"])


def _arm_send(monkeypatch, sent, result=LANDED):
    from dark_army_daemon import vscode_reveal as vr

    async def _send(pid, tty, text, newline=True):
        sent.append((pid, tty, text, newline))
        return result

    monkeypatch.setattr(vr, "send_text", _send)


def _forbid_send(monkeypatch):
    from dark_army_daemon import vscode_reveal as vr

    async def _send(*a, **k):
        raise AssertionError("nothing may be typed on a refusal")

    monkeypatch.setattr(vr, "send_text", _send)


# --- the happy path -----------------------------------------------------------

@pytest.mark.asyncio
async def test_it_types_the_kill_line_and_the_text_at_the_cards_pid(
        daemon, monkeypatch):
    d, store = daemon
    card = _live(d, store)
    sent = []
    _arm_send(monkeypatch, sent)
    ok, detail = await d.message_card(card["id"], "also update the readme")
    assert (ok, detail) == (True, "sent")
    # `tty=""` and `newline` left at its default, exactly as `wrap_up_session`
    # and `low_priority_session` call it.
    assert sent == [(4242, "", "\x15also update the readme", True)]
    assert dm.INPUT_LINE_CLEAR == "\x15"


@pytest.mark.asyncio
async def test_the_message_is_recorded_on_the_card_as_typed(daemon,
                                                            monkeypatch):
    d, store = daemon
    card = _live(d, store)
    _arm_send(monkeypatch, [])
    assert (await d.message_card(card["id"], "also update the readme"))[0]
    rows = store.messages(card["id"])
    assert len(rows) == 1
    assert rows[0]["author"] == "user"
    assert rows[0]["kind"] == "question"
    assert rows[0]["via"] == "terminal"
    assert rows[0]["text"] == "also update the readme"


@pytest.mark.asyncio
async def test_a_send_that_does_not_land_records_nothing(daemon, monkeypatch):
    d, store = daemon
    card = _live(d, store)
    _arm_send(monkeypatch, [], result={"matched": False, "sent": False})
    ok, detail = await d.message_card(card["id"], "also update the readme")
    assert ok is False
    assert detail == dm.NO_TYPING_WINDOW_REFUSAL
    assert store.messages(card["id"]) == []


@pytest.mark.asyncio
async def test_a_full_thread_still_reports_the_message_as_sent(daemon,
                                                               monkeypatch):
    """The act happened. `False` here would read as "nothing was sent" and
    invite a second press that typed it twice."""
    d, store = daemon
    card = _live(d, store)
    for i in range(board.MAX_THREAD_MESSAGES):
        row, why = store.add_message(card["id"], "user", f"m{i}", "note")
        assert row is not None, why
    sent = []
    _arm_send(monkeypatch, sent)
    ok, detail = await d.message_card(card["id"], "one more thing")
    assert ok is True
    assert "not recorded" in detail
    assert len(sent) == 1
    assert len(store.messages(card["id"])) == board.MAX_THREAD_MESSAGES


# --- the seven refusals, by constant identity ---------------------------------

@pytest.mark.asyncio
async def test_an_empty_message_is_refused(daemon, monkeypatch):
    d, store = daemon
    card = _live(d, store)
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], "   ") == (
        False, dm.CARD_MESSAGE_EMPTY_REFUSAL)


@pytest.mark.asyncio
async def test_a_leading_slash_is_refused_as_a_command(daemon, monkeypatch):
    d, store = daemon
    card = _live(d, store)
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], "/clear") == (
        False, dm.CARD_MESSAGE_SLASH_REFUSAL)


@pytest.mark.asyncio
@pytest.mark.parametrize("text",
                         ["one\ntwo", "one\rtwo", "one\ttwo", "one\x7ftwo"])
async def test_a_control_character_is_refused_as_more_than_one_line(
        daemon, monkeypatch, text):
    d, store = daemon
    card = _live(d, store)
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], text) == (
        False, dm.CARD_MESSAGE_ONE_LINE_REFUSAL)


@pytest.mark.asyncio
async def test_a_message_over_the_reply_limit_is_refused(daemon, monkeypatch):
    d, store = daemon
    card = _live(d, store)
    _forbid_send(monkeypatch)
    long = "x" * (d.MAX_REPLY_CHARS + 1)
    ok, detail = await d.message_card(card["id"], long)
    assert (ok, detail) == (False, d.CARD_MESSAGE_TOO_LONG_REFUSAL)
    assert str(d.MAX_REPLY_CHARS) in detail


@pytest.mark.asyncio
async def test_a_card_whose_link_has_ended_is_refused(daemon, monkeypatch):
    d, store = daemon
    card = _live(d, store)
    store.update(card["id"], {"link_state": "ended"})
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], "hello") == (
        False, dm.CARD_MESSAGE_NO_SESSION_REFUSAL)


@pytest.mark.asyncio
async def test_a_card_with_no_session_at_all_is_refused(daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], "hello") == (
        False, dm.CARD_MESSAGE_NO_SESSION_REFUSAL)


@pytest.mark.asyncio
async def test_a_codex_session_is_refused_in_its_own_words(daemon,
                                                           monkeypatch):
    d, store = daemon
    card = _live(d, store, provider="codex", tool="codex")
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], "hello") == (
        False, dm.CARD_MESSAGE_CODEX_REFUSAL)


@pytest.mark.asyncio
async def test_an_open_permission_prompt_is_refused_in_the_shared_words(
        daemon, monkeypatch):
    """The newline is the whole safety case: an open dialog reads it as
    confirming the highlighted choice."""
    d, store = daemon
    card = _live(d, store)
    monkeypatch.setattr(d, "_prompts_by_session",
                        lambda: {"sess-live": {"request_id": "r1"}})
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], "hello") == (
        False, dm.PROMPT_BLOCKED_REFUSAL)
    assert store.messages(card["id"]) == []


@pytest.mark.asyncio
async def test_an_open_question_is_refused_in_its_own_words(daemon,
                                                            monkeypatch):
    """An `AskUserQuestion` dialog is deliberately *not* a permission prompt —
    the hook broker refuses to hold one — so it never reaches
    `_prompts_by_session()` and needs its own gate. It owns the input line just
    as hard: Dark Army's own answer burst commits a choice by exactly this route."""
    d, store = daemon
    card = _live(d, store)
    monkeypatch.setattr(d, "_prompts_by_session", dict)
    d._questions["sess-live"] = {"id": "q1", "text": "Postgres or SQLite?",
                                 "options": ["Postgres", "SQLite"]}
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], "hello") == (
        False, dm.CARD_MESSAGE_QUESTION_REFUSAL)
    assert store.messages(card["id"]) == []


@pytest.mark.asyncio
async def test_a_question_only_the_hook_has_seen_yet_is_refused(daemon,
                                                                monkeypatch):
    """The live source, and the one that matters. `self._questions` is written
    only by `_enrich_agent_stubs`, on the agents-push cycle; the `PreToolUse`
    hook that opens the dialog writes `self._pending_questions` at once and,
    when it produces no state change, schedules no push — so the published
    cache can be a whole `SNAPSHOT_REFRESH_SECONDS` behind while the dialog
    owns the input line. A press in that window would commit a choice nobody
    read, which is the entire hazard this gate exists for."""
    d, store = daemon
    card = _live(d, store)
    monkeypatch.setattr(d, "_prompts_by_session", dict)
    d._questions.clear()
    d._pending_questions["sess-live"] = {
        "id": "q1", "text": "Postgres or SQLite?",
        "options": ["Postgres", "SQLite"]}
    _forbid_send(monkeypatch)
    assert not d._questions
    assert await d.message_card(card["id"], "hello") == (
        False, dm.CARD_MESSAGE_QUESTION_REFUSAL)
    assert store.messages(card["id"]) == []


def test_the_question_refusal_is_its_own_sentence():
    """Never a reuse of the prompt one: the two say different things about
    different dialogs, and a shared string would mislead on one of them."""
    assert (dm.CARD_MESSAGE_QUESTION_REFUSAL
            != dm.PROMPT_BLOCKED_REFUSAL)
    assert "question" in dm.CARD_MESSAGE_QUESTION_REFUSAL


@pytest.mark.asyncio
async def test_a_session_with_no_pid_is_refused(daemon, monkeypatch):
    d, store = daemon
    card = _live(d, store, pid=None)
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, st: None)
    monkeypatch.setattr(d, "_roster_pid", lambda sid: None)
    _forbid_send(monkeypatch)
    assert await d.message_card(card["id"], "hello") == (
        False, dm.CARD_MESSAGE_NO_PID_REFUSAL)


@pytest.mark.asyncio
async def test_a_card_id_that_does_not_exist_is_refused(daemon, monkeypatch):
    d, _store = daemon
    _forbid_send(monkeypatch)
    assert await d.message_card("nope", "hello") == (False, "no such card")


def test_the_eight_refusals_are_eight_distinct_sentences():
    words = {dm.CARD_MESSAGE_EMPTY_REFUSAL, dm.CARD_MESSAGE_SLASH_REFUSAL,
             dm.CARD_MESSAGE_ONE_LINE_REFUSAL,
             dm.CARD_MESSAGE_NO_SESSION_REFUSAL,
             dm.CARD_MESSAGE_CODEX_REFUSAL, dm.CARD_MESSAGE_NO_PID_REFUSAL,
             dm.CARD_MESSAGE_QUESTION_REFUSAL,
             BobDaemon.CARD_MESSAGE_TOO_LONG_REFUSAL}
    assert len(words) == 8


# --- the route can never quietly become the channel ---------------------------

def test_the_verb_types_and_never_reaches_for_the_channel():
    src = inspect.getsource(BobDaemon.message_card)
    assert "send_text" in src
    assert "push_channel_event" not in src
    assert "INPUT_LINE_CLEAR" in src


def test_the_hoisted_window_refusal_is_written_once():
    src = inspect.getsource(dm)
    assert src.count("No VS Code window owns") == 1
    assert dm.NO_TYPING_WINDOW_REFUSAL.endswith("type into it.")
    assert dm.NO_CLOSING_WINDOW_REFUSAL.endswith("close it.")


# --- the store ---------------------------------------------------------------

def test_terminal_is_a_known_via_and_a_row_can_carry_it(tmp_path):
    assert "terminal" in board.MESSAGE_VIAS
    store = BoardStore(tmp_path / "b.db")
    store.connect()
    try:
        card = _make(store)
        row, detail = store.add_message(card["id"], "user", "hi",
                                        "question", "terminal")
        assert row is not None, detail
        assert store.messages(card["id"])[0]["via"] == "terminal"
    finally:
        store.close()


# --- the can_message matrix ---------------------------------------------------

def _enrich(monkeypatch, stubs, cards, prompts=None, pending=None):
    """Run `_enrich_agent_stubs` with `can_send_text` recording its probes."""
    from dark_army_daemon import vscode_reveal as vr

    probed = []

    def _can(pid, tty=""):
        probed.append(pid)
        return True

    monkeypatch.setattr(vr, "can_send_text", _can)
    monkeypatch.setattr(vr, "can_close_terminal", lambda pid, tty="": False)
    d = BobDaemon(headless=True)
    d._board_state = {"cards": cards}
    if prompts is not None:
        monkeypatch.setattr(d, "_prompts_by_session", lambda: prompts)
    if pending is not None:
        d._pending_questions.update(pending)
    return d._enrich_agent_stubs(stubs), probed


LIVE_CARD = [{"id": "c1", "session_id": "s1", "link_state": "live"}]


def test_can_message_is_true_for_a_running_row_working_a_live_card(monkeypatch):
    out, probed = _enrich(
        monkeypatch,
        [{"session_id": "s1", "pid": 11, "_category": "running", "cwd": ""}],
        LIVE_CARD)
    assert out["running"][0]["can_message"] is True
    assert probed == [11]


def test_can_message_is_false_and_unprobed_for_a_row_working_no_card(
        monkeypatch):
    """The cost gate: `sid in carded` is the first term, so a fleet that is
    not working cards pays no `ps` at all."""
    out, probed = _enrich(
        monkeypatch,
        [{"session_id": "s2", "pid": 22, "_category": "running", "cwd": ""}],
        LIVE_CARD)
    assert out["running"][0]["can_message"] is False
    assert probed == []


def test_a_card_that_is_not_live_probes_nothing(monkeypatch):
    out, probed = _enrich(
        monkeypatch,
        [{"session_id": "s1", "pid": 11, "_category": "running", "cwd": ""}],
        [{"id": "c1", "session_id": "s1", "link_state": "ended"}])
    assert out["running"][0]["can_message"] is False
    assert probed == []


def test_can_message_is_false_for_codex(monkeypatch):
    out, _probed = _enrich(
        monkeypatch,
        [{"session_id": "s1", "pid": 11, "_category": "running", "cwd": "",
          "provider": "codex"}],
        LIVE_CARD)
    assert out["running"][0]["can_message"] is False


def test_can_message_is_false_for_a_background_agent(monkeypatch):
    out, _probed = _enrich(
        monkeypatch,
        [{"session_id": "s1", "pid": 11, "_category": "running", "cwd": "",
          "kind": "background"}],
        LIVE_CARD)
    assert out["running"][0]["can_message"] is False


def test_can_message_is_false_for_a_row_that_is_not_alive(monkeypatch):
    out, _probed = _enrich(
        monkeypatch,
        [{"session_id": "s1", "pid": 11, "_category": "running", "cwd": "",
          "alive": False}],
        LIVE_CARD)
    for bucket in out.values():
        for row in bucket:
            assert row["can_message"] is False


def test_can_message_is_false_while_a_permission_prompt_is_open(monkeypatch):
    out, _probed = _enrich(
        monkeypatch,
        [{"session_id": "s1", "pid": 11, "_category": "running", "cwd": ""}],
        LIVE_CARD,
        prompts={"s1": {"request_id": "r1"}})
    assert out["running"][0]["can_message"] is False


def test_can_message_is_false_while_a_question_is_open(monkeypatch):
    """The prompt gate cannot cover this one: a question dialog never enters
    `_prompts_by_session()`. The flag reads the same `entry["question"]` the
    row already publishes, so the button is absent rather than drawn and
    refused."""
    out, _probed = _enrich(
        monkeypatch,
        [{"session_id": "s1", "pid": 11, "_category": "running", "cwd": ""}],
        LIVE_CARD,
        pending={"s1": {"id": "q1", "text": "Postgres or SQLite?",
                        "options": ["Postgres", "SQLite"]}})
    row = out["running"][0]
    assert row["question"]          # the row really is stopped on one
    assert row["can_message"] is False


def test_carded_live_sessions_is_the_one_walk(monkeypatch):
    d = BobDaemon(headless=True)
    d._board_state = {"cards": [
        {"session_id": "s1", "link_state": "live"},
        {"session_id": "s2", "link_state": "dispatching"},
        {"session_id": "", "link_state": "live"},
        {"session_id": "s3", "link_state": "ended"},
    ]}
    assert d._carded_live_sessions() == {"s1"}
    # No board published yet is not an error, it is nobody.
    d._board_state = {}
    assert d._carded_live_sessions() == set()
