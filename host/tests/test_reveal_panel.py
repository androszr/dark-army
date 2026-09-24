# host/tests/test_reveal_panel.py
"""Jump *back*: a VS Code window asking Dark Army's panel to come forward on it.

`reveal_in_vscode` read from the other end. The editor knows which terminal it
is looking at (a shell pid and a tty) and nothing about sessions; the daemon
knows the sessions and nothing about terminals. This is the seam between them,
and everything it decides is **aim** rather than control — the worst a wrong
pair achieves is the wrong row highlighted, which is why nothing here refuses.
"""

import asyncio
import json

import pytest

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import vscode_reveal
from dark_army_daemon.api_server import ApiServer, _Request
from dark_army_daemon.daemon import BobDaemon


# ── the matcher ──────────────────────────────────────────────────────────────

def _ps_fake(ppids: dict, ttys: dict):
    """A stand-in process table: `{pid: ppid}` and `{pid: tty}`."""
    async def ps_field(field: str, pid: int) -> str:
        if field == "ppid":
            return str(ppids.get(pid, 0))
        return ttys.get(pid, "")
    return ps_field


def _match(shell_pid, tty, candidates, ppids=None, ttys=None):
    return asyncio.run(vscode_reveal.session_for_terminal(
        shell_pid, tty, candidates, ps_field=_ps_fake(ppids or {}, ttys or {})))


def test_ancestry_reaches_the_shell_through_a_chain():
    """The session is a grandchild of the terminal's shell — the ordinary case
    for an agent started inside a `zsh` VS Code opened."""
    assert _match(100, "", [("s1", 400, 5.0)],
                  ppids={400: 300, 300: 200, 200: 100}) == "s1"


def test_the_tty_is_the_fallback_when_ancestry_misses():
    """An orphaned session has been reparented away from its shell but still
    holds the terminal device — `findTerminal`'s own second rung."""
    assert _match(100, "/dev/ttys004", [("s1", 400, 5.0)],
                  ppids={400: 1}, ttys={400: "ttys004"}) == "s1"


def test_a_leading_dev_is_not_part_of_the_device_name():
    assert _match(100, "ttys004", [("s1", 400, 5.0)],
                  ppids={400: 1}, ttys={400: "/dev/ttys004"}) == "s1"


def test_two_sessions_in_one_terminal_pick_the_most_recently_active():
    """`/clear` leaves the old row lingering beside its successor in the same
    tab. `terminal_title` breaks that tie the same way: most recent wins."""
    assert _match(100, "", [("old", 400, 5.0), ("new", 401, 9.0)],
                  ppids={400: 100, 401: 100}) == "new"


def test_ancestry_outranks_a_tty_match_by_another_session():
    """A child of this terminal is a stronger answer than a stranger sharing
    the device, however recently that stranger spoke."""
    assert _match(100, "ttys004",
                  [("child", 400, 1.0), ("sharer", 500, 99.0)],
                  ppids={400: 100, 500: 1},
                  ttys={500: "ttys004"}) == "child"


def test_nothing_matches_resolves_to_nothing():
    assert _match(100, "ttys004", [("s1", 400, 5.0)],
                  ppids={400: 1}, ttys={400: "ttys009"}) is None


def test_a_nonsense_shell_pid_resolves_to_nothing():
    assert _match(0, "ttys004", [("s1", 400, 5.0)],
                  ttys={400: "ttys004"}) is None


def test_no_candidates_resolves_to_nothing():
    assert _match(100, "ttys004", []) is None


def test_the_walk_is_bounded():
    """A ppid cycle must not spin the daemon; the cap is the extension's 12."""
    ppids = {n: n + 1 for n in range(400, 460)}
    assert _match(999, "", [("s1", 400, 5.0)], ppids=ppids) is None


# ── the API gate ─────────────────────────────────────────────────────────────

TOKEN = "t" * 32


def _server(daemon=None):
    srv = ApiServer(daemon if daemon is not None else object(), port=0)
    srv.token = TOKEN
    return srv


_TOKENED = {"x-bob-token": TOKEN}


def _request(headers=_TOKENED, payload=None, method="POST", path="/api/action"):
    body = json.dumps(payload if payload is not None
                      else {"action": "reveal_panel", "shell_pid": 42,
                            "tty": "ttys004"}).encode()
    return _Request(method, path, "", dict(headers), body)


def test_an_authorised_request_is_extracted():
    assert _server()._reveal_panel_request(_request()) == (42, "ttys004")


def test_a_request_without_the_token_is_not_extracted_and_routes_to_403():
    srv = _server()
    request = _request(headers={})
    assert srv._reveal_panel_request(request) is None
    assert srv._route(request)[0] == 403


def test_the_wrong_header_spelling_is_not_the_token():
    """`Authorization` reads fine on this API and silently 403s every write —
    the failure mode with no symptom the extension is written against."""
    srv = _server()
    request = _request(headers={"authorization": f"Bearer {TOKEN}"})
    assert srv._reveal_panel_request(request) is None
    assert srv._route(request)[0] == 403


def test_another_action_falls_through():
    assert _server()._reveal_panel_request(
        _request(payload={"action": "reveal_session", "session_id": "s"})) is None


def test_a_garbage_shell_pid_still_asks_for_the_plain_panel():
    """Neither field is validated: they are untrusted aim, and a pair that
    names nothing opens the panel rather than refusing in the editor."""
    assert _server()._reveal_panel_request(
        _request(payload={"action": "reveal_panel",
                          "shell_pid": "not-a-pid"})) == (0, "")


class _StubDaemon:
    def __init__(self, reply):
        self.reply = reply
        self.seen = None

    async def reveal_panel_for_terminal(self, shell_pid, tty):
        self.seen = (shell_pid, tty)
        return self.reply


def test_the_action_answers_200_with_the_daemons_own_words():
    daemon = _StubDaemon((True, "no session matched this terminal"))
    srv = _server(daemon)
    status, _, body = asyncio.run(srv._reveal_panel(42, "ttys004"))
    assert status == 200
    assert daemon.seen == (42, "ttys004")
    assert json.loads(body) == {
        "ok": True, "detail": "no session matched this terminal"}


# ── the observer fan-out ─────────────────────────────────────────────────────

class _Watcher:
    def __init__(self):
        self.seen = []

    def on_panel_reveal(self, session_id: str) -> None:
        self.seen.append(session_id)


class _Deaf:
    """An observer one generation behind. `_notify_observers` skips it."""


def test_a_match_is_named_to_the_observers(monkeypatch):
    daemon = BobDaemon()
    watcher, deaf = _Watcher(), _Deaf()
    daemon.add_observer(deaf)
    daemon.add_observer(watcher)
    daemon._session_states["s1"] = {"last_event": 5.0}
    monkeypatch.setattr(daemon, "_legacy_navigation_pid", lambda facts: 400)

    async def matcher(shell_pid, tty, candidates, **kw):
        assert candidates == [("s1", 400, 5.0)]
        return "s1"

    monkeypatch.setattr(vscode_reveal, "session_for_terminal", matcher)
    ok, detail = asyncio.run(daemon.reveal_panel_for_terminal(100, "ttys004"))
    assert (ok, detail) == (True, "")
    assert watcher.seen == ["s1"]


def test_an_idle_session_evicted_from_hook_state_is_still_found_by_the_roster(monkeypatch):
    """The reported bug: a finished turn with no card goes quiet past the
    staleness timeout and leaves `_session_states`, but Claude's roster keeps
    its row on the panel. Pressing the button on that terminal must still aim."""
    from dark_army_daemon.agents_poll import AgentRecord
    daemon = BobDaemon()
    watcher = _Watcher()
    daemon.add_observer(watcher)
    daemon._agent_records["idle"] = AgentRecord("idle", pid=400)
    monkeypatch.setattr(daemon, "_legacy_navigation_pid",
                        lambda facts: facts[2] if facts[0] == "idle" else None)

    async def matcher(shell_pid, tty, candidates, **kw):
        assert candidates == [("idle", 400, 0.0)]
        return "idle"

    monkeypatch.setattr(vscode_reveal, "session_for_terminal", matcher)
    ok, detail = asyncio.run(daemon.reveal_panel_for_terminal(100, "ttys004"))
    assert (ok, detail) == (True, "")
    assert watcher.seen == ["idle"]


def test_a_miss_still_opens_the_panel_and_says_so():
    """Always `(True, …)`: the press happened in the editor, and a terminal Dark Army
    cannot place is a plain panel with a reason, never a refusal to render."""
    daemon = BobDaemon()
    watcher = _Watcher()
    daemon.add_observer(watcher)
    ok, detail = asyncio.run(daemon.reveal_panel_for_terminal(100, "ttys004"))
    assert ok is True
    assert detail
    assert watcher.seen == [""]


def test_a_wedged_process_table_does_not_hold_the_reply(monkeypatch):
    daemon = BobDaemon()
    watcher = _Watcher()
    daemon.add_observer(watcher)
    daemon._session_states["s1"] = {"last_event": 5.0}
    monkeypatch.setattr(daemon, "_legacy_navigation_pid", lambda facts: 400)

    async def wedged(*a, **kw):
        raise asyncio.TimeoutError

    monkeypatch.setattr(vscode_reveal, "session_for_terminal", wedged)
    ok, _ = asyncio.run(daemon.reveal_panel_for_terminal(100, "ttys004"))
    assert ok is True
    assert watcher.seen == [""]


def test_codex_two_navigation_roots_round_trip_to_their_own_rows(tmp_path, monkeypatch):
    from tests.test_agents_poll import _navigation_daemon
    daemon, roots, processes = _navigation_daemon(tmp_path, monkeypatch)
    watcher = _Watcher()
    daemon.add_observer(watcher)
    original = vscode_reveal.session_for_terminal
    async def matcher(shell_pid, tty, candidates):
        return await original(shell_pid, tty, candidates, ps_field=_ps_fake(
            {701: 601, 601: 501, 702: 602, 602: 502},
            {701: "ttys040", 702: "ttys041"}))
    monkeypatch.setattr(vscode_reveal, "session_for_terminal", matcher)
    for root, shell, tty in zip(roots, (501, 502), ("ttys040", "ttys041")):
        assert asyncio.run(daemon.reveal_panel_for_terminal(shell, tty)) == (True, "")
        assert watcher.seen[-1] == root.session_id
    assert len(watcher.seen) == 2


@pytest.mark.parametrize("fault", ["tty_conflict", "sibling_tty", "ambiguous", "replaced", "timeout", "legacy_competitor", "candidate_timeout"])
def test_codex_reverse_uncertainty_opens_plain_panel(tmp_path, monkeypatch, fault):
    from tests.test_agents_poll import _navigation_daemon
    from dataclasses import replace
    daemon, roots, processes = _navigation_daemon(tmp_path, monkeypatch)
    watcher = _Watcher()
    daemon.add_observer(watcher)
    original = vscode_reveal.session_for_terminal
    if fault == "legacy_competitor":
        daemon._session_states["claude"] = {"last_event": 10}
        monkeypatch.setattr(daemon, "_legacy_navigation_pid", lambda facts: 800)
    if fault == "candidate_timeout":
        async def timeout(_captured):
            raise asyncio.TimeoutError
        monkeypatch.setattr(daemon, "_fresh_navigation_targets", timeout)
    async def matcher(shell_pid, tty, candidates):
        if fault == "timeout":
            raise asyncio.TimeoutError
        if fault == "replaced" and candidates:
            daemon._codex_records[roots[0].session_id] = replace(roots[0])
        return await original(shell_pid, tty, candidates, ps_field=_ps_fake(
            {701: 601, 601: 501, 702: 501 if fault == "ambiguous" else 502, 800: 501},
            {701: "ttys040", 702: "ttys041"}))
    monkeypatch.setattr(vscode_reveal, "session_for_terminal", matcher)
    tty = {"tty_conflict": "ttys099", "sibling_tty": "ttys041"}.get(fault, "")
    ok, detail = asyncio.run(daemon.reveal_panel_for_terminal(501, tty))
    assert ok and detail
    assert watcher.seen == [""]


def test_grok_roster_session_without_hook_state_still_matches(monkeypatch):
    """Grok is in `_grok_records` before hooks speak. The reverse jump
    still has to find it, or the editor button opens a blank panel."""
    from types import SimpleNamespace

    daemon = BobDaemon()
    watcher = _Watcher()
    daemon.add_observer(watcher)
    daemon._grok_records["g1"] = SimpleNamespace(session_id="g1", pid=400)
    monkeypatch.setattr(daemon, "_legacy_navigation_pid",
                        lambda facts: 400 if facts[0] == "g1" else None)

    async def matcher(shell_pid, tty, candidates, **kw):
        assert ("g1", 400, 0.0) in candidates
        return "g1"

    monkeypatch.setattr(vscode_reveal, "session_for_terminal", matcher)
    ok, detail = asyncio.run(daemon.reveal_panel_for_terminal(100, "ttys004"))
    assert (ok, detail) == (True, "")
    assert watcher.seen == ["g1"]


def test_legacy_navigation_worker_cannot_write_loop_owned_pid(monkeypatch):
    import threading
    from types import SimpleNamespace
    from dark_army_daemon import daemon as daemon_module
    daemon = BobDaemon()
    daemon._session_states["claude"] = {"last_event": 1}
    daemon._agent_records["claude"] = SimpleNamespace(pid=400)
    watcher = _Watcher()
    daemon.add_observer(watcher)
    loop_thread = threading.get_ident()
    probes = []
    def process(pid):
        probes.append(threading.get_ident())
        assert daemon._session_states["claude"].get("pid") is None
        return SimpleNamespace(name=lambda: "claude", cmdline=lambda: ["claude"])
    monkeypatch.setattr(daemon_module.psutil, "Process", process)
    async def matcher(shell, tty, candidates):
        assert candidates == [("claude", 400, 1.0)]
        return "claude"
    monkeypatch.setattr(vscode_reveal, "session_for_terminal", matcher)
    assert asyncio.run(daemon.reveal_panel_for_terminal(100, "")) == (True, "")
    assert probes and all(thread != loop_thread for thread in probes)
    assert daemon._session_states["claude"] == {"last_event": 1}
