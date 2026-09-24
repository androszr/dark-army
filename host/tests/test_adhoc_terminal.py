# host/tests/test_adhoc_terminal.py
"""An assistant started with **no card behind it**.

`+ TERMINAL` on the Agents rail is `dispatch_card`'s sibling without the
paperwork: the same launcher preference, the same enrolment ledger, the same
one-per-project launch bound and the same cooldown, but no card, no board row
and no prompt on the argv at all. The three things worth proving here are that
the argv really is one element, that the launch bound is symmetric with a
card's dispatch in *both* directions, and that the pty a person gets can be
found again by session with no card involved.
"""

import os
import time

import pytest

from dark_army_daemon import dispatch
from dark_army_daemon import enrollment
from dark_army_daemon import ptyhost
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture
def project(tmp_path):
    """A real directory: `adhoc_guard` ends on `os.path.isdir`."""
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    d.board_dispatch_enabled = True
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _enrol(monkeypatch, *roots):
    """Stand in for the ledger. `enrolled_roots` is the offer set *and* the
    guard's set, and the daemon re-reads it under its own lock — so a test
    that widens it here is testing the same read the press makes."""
    normalised = {dispatch.normalise_root(str(r)) for r in roots}
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(normalised))

    def label(cwd: str) -> str:
        cwd = dispatch.normalise_root(str(cwd or ""))
        return os.path.basename(cwd) if cwd in normalised else ""

    monkeypatch.setattr(enrollment, "enrolled_label", label)


def _stub_spawn(monkeypatch, opened, *, ok=True, pid=4242):
    """Record every `spawn_local`, open nothing. The only honest place to
    assert "no terminal opened" is the recorder."""
    async def local(root, argv, name, *, stamp="", **_kw):
        opened.append({"root": root, "argv": list(argv), "name": name,
                       "stamp": stamp})
        return (ok, "bob-terminal" if ok else "no host", pid if ok else None)

    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: f"/usr/local/bin/{tool}")


# --- the argv --------------------------------------------------------------


def test_the_argv_is_exactly_the_executable():
    """Zero card-supplied elements. No `--` separator to get right, no
    `--model` off a store field, and nothing for `prompt_refusal` to judge."""
    assert dispatch.adhoc_argv("claude", "/usr/local/bin/claude") == \
        ["/usr/local/bin/claude"]
    assert dispatch.adhoc_argv("codex", "/usr/local/bin/codex") == \
        ["/usr/local/bin/codex"]
    for tool in ("claude", "codex", "grok"):
        argv = dispatch.adhoc_argv(tool, f"/x/{tool}")
        assert len(argv) == 1
        assert "--" not in argv
        assert "--model" not in argv


@pytest.mark.asyncio
async def test_a_spawn_puts_one_argv_element_on_the_command_line(
        daemon, project, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert ok, detail
    assert len(opened) == 1
    assert opened[0]["argv"] == ["/usr/local/bin/claude"]
    assert opened[0]["root"] == str(project)


@pytest.mark.asyncio
async def test_the_terminal_says_bob_opened_it(daemon, project, monkeypatch):
    """A row nobody can account for is what `origin.py` exists to remove, and
    a terminal opened on a press is a row a person did not type into being.
    This is the one kind that names no card."""
    from dark_army_daemon import origin

    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert ok, detail
    assert origin.parse(opened[0]["stamp"]) == {
        "by": "adhoc", "card_id": "", "stage": ""}
    assert origin.sentence("adhoc") == "Dark Army opened this terminal for you"


# --- the switch ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_launcher_switch_removes_it_entirely(
        daemon, project, monkeypatch):
    """`board_dispatch` is Dark Army-as-launcher's one switch, and off has to mean
    off for every route to it — not only for the button on a card."""
    d, _store = daemon
    d.board_dispatch_enabled = False
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert not ok
    assert "not allowed to start sessions" in detail
    assert opened == [], "a switched-off launcher must open no terminal"


# --- the folder ------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unenrolled_folder_is_refused(daemon, project, tmp_path,
                                               monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)
    stranger = tmp_path / "stranger"
    stranger.mkdir()

    ok, detail = await d.open_adhoc_terminal(str(stranger), "claude")
    assert not ok
    assert detail == "that folder is not one of Dark Army's projects"
    assert opened == []


@pytest.mark.asyncio
async def test_an_enrolled_folder_with_no_editor_window_still_starts(
        daemon, project, monkeypatch):
    """The widening this whole verb leans on. An ad-hoc terminal is always
    Dark Army's own pty, which needs no VS Code window — so the offer set is the
    ledger, not `_known_project_roots()`, exactly as START HERE already is."""
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)
    # No windows, no live sessions: `_known_project_roots()` is empty.
    monkeypatch.setattr(d, "_known_project_roots", lambda: set())

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert ok, detail
    assert len(opened) == 1


# --- the assistant ---------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_assistant_is_refused_in_words(
        daemon, project, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)

    ok, detail = await d.open_adhoc_terminal(str(project), "cursor")
    assert not ok
    assert detail == "cannot start cursor"
    assert opened == []


@pytest.mark.asyncio
async def test_no_assistant_at_all_is_refused(daemon, project, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)

    ok, detail = await d.open_adhoc_terminal(str(project), "")
    assert not ok
    assert detail == "no assistant was chosen"
    assert opened == []


# --- the launch bound, both directions -------------------------------------


@pytest.mark.asyncio
async def test_a_dispatching_card_refuses_the_button(
        daemon, project, monkeypatch):
    """Binding is by elimination, so "the first new session in this project"
    has to name one launch. A card already starting owns that name."""
    d, store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)
    card, detail = store.create({"title": "a card", "project": project.name,
                                 "root": str(project), "prompt": "go",
                                 "tool": "claude", "column_name": "backlog"})
    assert card is not None, detail
    store.update(card["id"], {"link_state": "dispatching",
                              "column_name": "in_progress",
                              "dispatched_at": time.time()})

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert not ok
    assert detail == dispatch.PROJECT_BUSY_REFUSAL
    assert dispatch.is_transient(detail)
    assert opened == []


@pytest.mark.asyncio
async def test_a_stale_dispatching_card_no_longer_holds_the_project(
        daemon, project, monkeypatch):
    """The bind window bounds the *slot*, not only the card.

    The binders' give-up is what normally clears `dispatching`, and it runs
    inside the board reconcile — so a reconcile that stops used to leave one
    card holding its whole project's launch slot for ever, refusing Start,
    Refine and + TERMINAL alike in a sentence that invites a retry. Past the
    window nobody is waiting for that session, so the card names no launch
    here whatever the column still says."""
    d, store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)
    card, detail = store.create({"title": "a card", "project": project.name,
                                 "root": str(project), "prompt": "go",
                                 "tool": "claude", "column_name": "backlog"})
    assert card is not None, detail
    store.update(card["id"], {
        "link_state": "dispatching", "column_name": "in_progress",
        "dispatched_at": time.time() - dispatch.DISPATCH_BIND_WINDOW - 1})

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert ok, detail
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_an_adhoc_launch_queues_a_card_start_in_the_same_project(
        daemon, project, monkeypatch):
    """The other direction, and the one that has to hold on an extension
    older than 0.1.10, where there is no descent receipt at all: the ad-hoc
    pseudo-entry is the entire defence against the two racing for one row."""
    d, store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(project)})

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert ok, detail
    assert len(d._adhoc_launches) == 1

    card, detail = store.create({"title": "a card", "project": project.name,
                                 "root": str(project), "prompt": "go",
                                 "tool": "claude", "column_name": "backlog"})
    assert card is not None, detail
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert not ok
    assert len(opened) == 1, "the card must not have opened a second terminal"
    assert store.get(card["id"])["queue_state"] == "queued"


@pytest.mark.asyncio
async def test_a_second_press_inside_the_cooldown_is_refused(
        daemon, project, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert ok, detail
    # Release the project bound so the cooldown is what answers.
    d._adhoc_launches.clear()
    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert not ok
    assert detail == dispatch.COOLDOWN_REFUSAL
    assert len(opened) == 1


# --- the entry releases ----------------------------------------------------


def test_the_entry_releases_once_its_terminal_is_named(daemon):
    """The sweep lives inside `_launch_inflight` because that is its only
    reader: a leaked entry would park every card start in the project."""
    d, _store = daemon
    _fake_terminal(d, "h1")
    d._adhoc_launches["l1"] = {"project": "bob", "root": "/a/bob",
                               "handle": "h1", "at": time.time()}
    assert [c["id"] for c in d._launch_inflight([])] == ["adhoc:l1"]

    d._pty.get("h1").session_id = "s-new"
    assert d._launch_inflight([]) == []
    assert d._adhoc_launches == {}


def test_the_entry_releases_when_the_bind_window_runs_out(daemon):
    d, _store = daemon
    _fake_terminal(d, "h2")
    d._adhoc_launches["l2"] = {
        "project": "bob", "root": "/a/bob", "handle": "h2",
        "at": time.time() - dispatch.DISPATCH_BIND_WINDOW - 1,
    }
    assert d._launch_inflight([]) == []
    assert d._adhoc_launches == {}


def test_an_entry_whose_terminal_has_gone_releases(daemon):
    d, _store = daemon
    d._adhoc_launches["l3"] = {"project": "bob", "root": "/a/bob",
                               "handle": "nobody", "at": time.time()}
    assert d._launch_inflight([]) == []
    assert d._adhoc_launches == {}


def _fake_terminal(d, handle: str):
    """Put a `PtyTerminal` in the host's map without starting a process.
    The sweep is a dict lookup on the handle, so this is the whole seam."""
    term = ptyhost.PtyTerminal(handle=handle, name="claude", root="/a/bob",
                               pid=0, master=-1, proc=None,
                               screen=ptyhost.Screen(80, 24), cols=80, rows=24)
    d._pty._terms[handle] = term
    return term


# --- no board write --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_spawn_makes_no_board_write_and_moves_no_revision(
        daemon, project, monkeypatch):
    """The success criterion, asserted at the store: a terminal can be
    spawned without selecting or creating a card."""
    d, store = daemon
    opened: list = []
    _enrol(monkeypatch, project)
    _stub_spawn(monkeypatch, opened)
    other = store.create({"title": "untouched", "project": "elsewhere",
                          "root": "/a/elsewhere", "prompt": "go",
                          "tool": "claude", "column_name": "backlog"})[0]
    before = [(c["id"], c["column_name"], c["revision"], c["link_state"])
              for c in store.cards()]
    assert len(before) == 1

    ok, detail = await d.open_adhoc_terminal(str(project), "claude")
    assert ok, detail

    after = [(c["id"], c["column_name"], c["revision"], c["link_state"])
             for c in store.cards()]
    assert after == before, "an ad-hoc terminal writes no board row at all"
    assert store.get(other["id"])["revision"] == 0


# --- the door --------------------------------------------------------------


def test_the_verb_is_on_no_phone_tuple_and_is_not_a_board_write():
    """More capability than `board_dispatch` — it names no card a person
    wrote — so it stays on loopback, where a press is somebody at the Mac."""
    assert "spawn_terminal" not in ApiServer.BOARD_ACTIONS
    assert "spawn_terminal" not in ApiServer.LAN_ACTIONS
    assert "spawn_terminal" not in ApiServer.REMOTE_ACTIONS
    assert "spawn_terminal" not in ApiServer._LAN_BOARD
    # And `REMOTE_ACTIONS ⊆ LAN_ACTIONS` still holds either way.
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_an_unauthenticated_press_never_reaches_the_daemon(monkeypatch):
    """`_spawn_terminal_request` is behind `_authorised` — `X-Bob-Token` and
    the Origin allowlist — and returning None drops the request back down
    `_route`, which is what answers 403."""
    calls: list = []

    class _Daemon:
        async def open_adhoc_terminal(self, root, tool):
            calls.append((root, tool))
            return True, "started"

    server = ApiServer(_Daemon())
    server.token = "the-secret"
    body = b'{"action": "spawn_terminal", "root": "/a/bob", "tool": "claude"}'
    from dark_army_daemon.api_server import _Request

    bad = _Request("POST", "/api/action", {},
                   {"content-type": "application/json"}, body)
    assert server._spawn_terminal_request(bad) is None
    status, _ctype, _out = server._route(bad)
    assert status == 403
    assert calls == []

    good = _Request("POST", "/api/action", {},
                    {"content-type": "application/json",
                     "x-bob-token": "the-secret"}, body)
    assert server._spawn_terminal_request(good) == ("/a/bob", "claude")


@pytest.mark.asyncio
async def test_the_handler_400s_a_request_that_names_nothing():
    class _Daemon:
        async def open_adhoc_terminal(self, root, tool):
            raise AssertionError("must not be reached")

    server = ApiServer(_Daemon())
    status, _ctype, body = await server._spawn_terminal("", "claude")
    assert status == 400
    assert b"which folder" in body
    status, _ctype, body = await server._spawn_terminal("/a/bob", "")
    assert status == 400


@pytest.mark.asyncio
async def test_a_refusal_answers_409_in_the_daemons_own_words():
    class _Daemon:
        async def open_adhoc_terminal(self, root, tool):
            return False, dispatch.PROJECT_BUSY_REFUSAL

    server = ApiServer(_Daemon())
    status, _ctype, body = await server._spawn_terminal("/a/bob", "claude")
    assert status == 409
    assert dispatch.PROJECT_BUSY_REFUSAL.encode() in body


# --- the self-bind, with no card -------------------------------------------


@pytest.mark.asyncio
async def test_a_pty_with_no_card_is_found_again_by_its_session(daemon,
                                                               tmp_path):
    """The claim the whole plan rests on: `_pty_handle_for` names an unbound
    terminal after a session by asking `ptyhost.owns` and
    `_bindable_candidate`, and knows nothing about the board. So an ad-hoc
    terminal binds itself on the next agents snapshot with no card involved,
    and the Terminal tab resolves for it.

    A real `PtyHost` on a real child, which is the whole seam: `persist` is
    already off under pytest, so the broker is never involved."""
    d, _store = daemon
    assert d._pty.persist is False, "pytest runs the host in-process"

    ok, detail, pid = await d._pty.start(
        str(tmp_path), ["/bin/sh", "-c", "sleep 5"], "adhoc")
    assert ok, detail
    try:
        sid = "s-adhoc"
        d._session_states[sid] = {"state": "working", "pid": pid,
                                  "last_event": time.time()}
        stub = {"session_id": sid, "kind": "interactive"}

        handle = d._pty_handle_for(sid, stub)
        assert handle is not None
        assert d._pty.for_session(sid) == handle
        assert d._pty.get(handle).session_id == sid
    finally:
        await d._pty.close(d._pty.for_session(sid) or handle)


# --- the menu bar is untouched ---------------------------------------------


def test_the_menu_bar_app_knows_nothing_about_this():
    """Stated rather than omitted: no AppKit main-thread work, no strip rung,
    no menu row. The status item's contract is not reopened here."""
    from pathlib import Path
    pkg = Path(__file__).resolve().parents[1] / "dark_army_menubar"
    for path in pkg.glob("*.py"):
        assert "adhoc" not in path.read_text(), path.name
