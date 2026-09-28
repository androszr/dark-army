# host/tests/test_mission_control.py
"""Mission Control: the one standing chief-of-staff session — it answers and acts.

`open_adhoc_terminal`'s sibling, strictly narrower — one executable, one
folder (Dark Army's own checkout), one fixed brief, at most one alive — and
the reason it may be a phone verb. The four things worth proving here are
that the argv is fixed and the prompt last, that a second open while one is
alive spawns nothing, that a dead process is re-spawned on the next open and
that only `end_mission` closes the terminal — plus that a quiet, evicted row
can still be typed at by its last id through the record's rung.
"""

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import daemon_board
from dark_army_daemon import dispatch
from dark_army_daemon import enrollment
from dark_army_daemon import mission
from dark_army_daemon import origin
from dark_army_daemon import paths
from dark_army_daemon import ptyhost
from dark_army_daemon.api_server import ApiServer, _Request
from dark_army_daemon import api_server as api_mod
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_menubar import dev_build

REPO = Path(__file__).resolve().parents[2]


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path):
    """A stand-in for Dark Army's own checkout, holding the brief."""
    root = tmp_path / "dark-army"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "mission-control-brief.md").write_text(
        (REPO / "docs" / "mission-control-brief.md").read_text())
    return root


@pytest.fixture
def daemon(tmp_path, monkeypatch, checkout):
    monkeypatch.setattr(paths, "MISSION_PATH", tmp_path / "mission.json")
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: checkout)
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
    normalised = {dispatch.normalise_root(str(r)) for r in roots}
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(normalised))

    def label(cwd: str) -> str:
        cwd = dispatch.normalise_root(str(cwd or ""))
        return os.path.basename(cwd) if cwd in normalised else ""

    monkeypatch.setattr(enrollment, "enrolled_label", label)


def _stub_spawn(monkeypatch, opened, *, ok=True, pid=4242):
    async def local(root, argv, name, *, stamp="", **_kw):
        opened.append({"root": root, "argv": list(argv), "name": name,
                       "stamp": stamp})
        return (ok, "bob-terminal" if ok else "no host", pid if ok else None)

    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: f"/usr/local/bin/{tool}")


def _real_spawn(monkeypatch, d, opened, *, seconds=30):
    """`spawn_local` backed by the daemon's own in-process `PtyHost` on a
    `/bin/sh` child, so `owns(pid)` and the terminal's liveness are real."""
    async def local(root, argv, name, *, stamp="", **_kw):
        opened.append({"root": root, "argv": list(argv), "name": name,
                       "stamp": stamp})
        return await d._pty.start(
            root, ["/bin/sh", "-c", f"sleep {seconds}"], name)

    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: f"/usr/local/bin/{tool}")


async def _close_all(d):
    for term in list(d._pty.terminals()):
        await d._pty.close(term.handle)


# --- the argv ----------------------------------------------------------------


def test_mission_argv_is_fixed_and_the_prompt_is_last():
    """The three flags `card_prepare.argv` already measures, every rule its
    own element, and the prompt the one positional **after** `--agent` —
    `--allowed-tools` is variadic on the installed CLI and would swallow a
    trailing prompt."""
    rules = mission.allowed_tools("/Users/x/dark-army")
    argv = dispatch.mission_argv("/x/claude", '{"a": 1}', rules,
                                 mission.AGENT_NAME, mission.OPENING_PROMPT,
                                 display_name=mission.NAME)
    assert argv == ["/x/claude", "--name", "Mission Control",
                    "--agents", '{"a": 1}',
                    "--allowed-tools", *rules,
                    "--agent", "mission-control", mission.OPENING_PROMPT]
    assert argv[-1] == mission.OPENING_PROMPT
    assert argv.index("--agent") > argv.index("--allowed-tools")
    for flag in ("--no-session-persistence", "--setting-sources",
                 "--strict-mcp-config", "--dangerously-load-development-channels",
                 "--model", "-p"):
        assert flag not in argv, flag


def test_the_grant_is_scoped_to_the_checkout_the_scratch_file_and_loopback():
    """Security: no bare `Read` (that pre-approves `~/.dark-army/api-token`
    and every other user-readable file) and no `Bash(curl:*)` (egress to any
    host). The rule shapes are the ones measured on CLI 2.1.278 — the
    wildcard `Bash(… /*)`, not the `:*` prefix form, which does not match a
    URL — and a root with a space rides as one element, never split."""
    rules = mission.allowed_tools("/Users/x/dark army/")
    assert "Read" not in rules and "Bash" not in rules
    assert "Bash(curl:*)" not in rules
    assert not any(r.startswith("Bash(") and r != mission.CURL_RULE for r in rules)
    assert mission.CURL_RULE == "Bash(curl -s http://127.0.0.1:19874/*)"
    assert not mission.CURL_RULE.endswith(":*)")
    assert "Read(//Users/x/dark army/**)" in rules
    assert "Read(//tmp/darkarmy-state.json)" in rules
    assert "Read(//private/tmp/darkarmy-state.json)" in rules
    assert "Grep" in rules and "Glob" in rules
    for r in rules:
        assert not r.startswith("Read(///"), r
        assert ".bob-companion" not in r and ".dark-army" not in r, r
    home_rule = f"Read(//{str(Path.home()).lstrip('/')}/**)"
    assert home_rule not in mission.allowed_tools(str(Path.home()) + "/dark-army")


def test_the_api_port_is_the_api_door_not_the_hook_socket():
    """`DARK_ARMY_HOOK_SOCKET` (or, from an older broker, the bridge port
    `BOB_COMPANION_PORT`, `HOOK_IPC_PORT`) in the child's environment names
    the hook socket and answers no HTTP; the brief and the curl rule name
    `api_server.API_PORT`'s default instead."""
    from dark_army_daemon import api_server, socket_server
    assert api_server.API_PORT == mission.API_PORT == 19874
    assert mission.API_PORT != socket_server.HOOK_IPC_PORT
    assert mission.API_URL == "http://127.0.0.1:19874"
    assert mission.CURL_RULE.startswith(f"Bash(curl -s {mission.API_URL}/")


@pytest.mark.asyncio
async def test_open_grants_reads_under_the_resolved_checkout_only(
        daemon, checkout, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _stub_spawn(monkeypatch, opened)
    monkeypatch.setattr(d._pty, "owns", lambda pid: "h-mission")
    ok, _detail = await d.open_mission()
    assert ok
    argv = opened[0]["argv"]
    rules = argv[argv.index("--allowed-tools") + 1:argv.index("--agent")]
    canonical = dispatch.normalise_root(str(checkout))
    assert tuple(rules) == mission.allowed_tools(canonical)
    assert f"Read(//{canonical.lstrip('/')}/**)" in rules
    assert "Read" not in rules and "Bash(curl:*)" not in rules


# --- open --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_spawns_once_in_the_checkout_with_the_mission_stamp(
        daemon, checkout, monkeypatch, tmp_path):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _stub_spawn(monkeypatch, opened)
    monkeypatch.setattr(d._pty, "owns", lambda pid: "h-mission")

    ok, detail = await d.open_mission()
    assert ok, detail
    assert len(opened) == 1
    assert opened[0]["root"] == dispatch.normalise_root(str(checkout))
    assert opened[0]["name"] == mission.PTY_NAME
    assert opened[0]["name"] != "Mission Control", \
        "the pty name is never the label a card title could wear"
    assert opened[0]["stamp"] == origin.stamp("mission")
    assert opened[0]["argv"][0] == "/usr/local/bin/claude"
    assert opened[0]["argv"][-1] == mission.OPENING_PROMPT
    agents = json.loads(opened[0]["argv"][opened[0]["argv"].index("--agents") + 1])
    assert "mission-control" in agents
    assert "never" in agents["mission-control"]["prompt"].lower()
    assert detail == "h-mission", "the loopback reply carries the handle"
    record = tmp_path / "mission.json"
    assert record.is_file()
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    data = json.loads(record.read_text())
    assert set(data) >= {"handle", "root", "session_id", "opened_at"}
    assert data["handle"] == "h-mission"
    assert len(d._adhoc_launches) == 1, "the bind race is held like an ad-hoc launch"


@pytest.mark.asyncio
async def test_a_second_open_while_alive_spawns_nothing(
        daemon, checkout, monkeypatch):
    """The phone opens on every tab visit, so this is the property the whole
    tab leans on. A real `PtyHost` on a real child, so `owns(pid)` and the
    liveness read are real."""
    d, _store = daemon
    assert d._pty.persist is False
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened)
    try:
        ok, handle = await d.open_mission()
        assert ok, handle
        assert d._pty.get(handle) is not None
        d._mission_attempt = 0.0  # past the cooldown; the alive check answers
        ok, detail = await d.open_mission()
        assert ok
        assert detail == mission.ALREADY_RUNNING
        assert len(opened) == 1
        assert d._mission["handle"] == handle
    finally:
        await _close_all(d)


@pytest.mark.asyncio
async def test_a_dead_terminal_is_respawned_on_the_next_open(
        daemon, checkout, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened, seconds=0)
    try:
        ok, first = await d.open_mission()
        assert ok, first
        term = d._pty.get(first)
        # `sleep 0` exits at once; wait for the host to notice.
        for _ in range(100):
            if term.exited:
                break
            await __import__("asyncio").sleep(0.05)
        assert term.exited
        d._mission_attempt = 0.0
        d._adhoc_launches.clear()
        ok, second = await d.open_mission()
        assert ok, second
        assert second != first
        assert len(opened) == 2
        assert d._mission["handle"] == second
    finally:
        await _close_all(d)


@pytest.mark.asyncio
async def test_a_terminal_spawned_on_an_older_brief_is_replaced_on_the_next_open(
        daemon, checkout, monkeypatch):
    """The brief rides `--agents` at spawn and nothing re-reads it, so the
    Mission Control spawned on the read-only brief kept saying "I cannot
    add a card" after the brief that made it act landed on disk. The
    record keeps the brief's digest; a mismatch closes the old terminal and
    spawns the new brief. A record with no digest is stale by definition."""
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened)
    try:
        ok, first = await d.open_mission()
        assert ok, first
        assert d._mission["brief_digest"] == mission.brief_digest(
            mission.read_brief(checkout)[0])
        d._mission_attempt = 0.0
        d._adhoc_launches.clear()
        # Same brief: nothing happens.
        ok, detail = await d.open_mission()
        assert ok and detail == mission.ALREADY_RUNNING
        assert len(opened) == 1
        # The brief changes on disk: the next open replaces the terminal.
        brief = checkout / "docs" / "mission-control-brief.md"
        brief.write_text(brief.read_text() + "\n\nAct when asked.\n")
        d._mission_attempt = 0.0
        d._adhoc_launches.clear()
        ok, second = await d.open_mission()
        assert ok, second
        assert second != first
        assert len(opened) == 2
        assert d._mission["handle"] == second
        assert d._mission["brief_digest"] == mission.brief_digest(
            mission.read_brief(checkout)[0])
        old = d._pty.get(first)
        assert old is None or old.exited
        # The brief in the new argv is the edited one.
        agents = json.loads(opened[1]["argv"][opened[1]["argv"].index("--agents") + 1])
        assert "Act when asked." in agents["mission-control"]["prompt"]
        # A record written before the key existed is stale too.
        d._mission.pop("brief_digest")
        d._mission_attempt = 0.0
        d._adhoc_launches.clear()
        ok, third = await d.open_mission()
        assert ok, third
        assert third != second
        assert len(opened) == 3
    finally:
        await _close_all(d)


def _side_folder(checkout: Path) -> Path:
    """A card's side folder as git leaves one: `.git` is a file naming the
    worktree's git folder, whose `commondir` names the main `.git`."""
    (checkout / "host").mkdir(exist_ok=True)
    (checkout / "host" / "build.sh").write_text("#!/bin/sh\n")
    gitdir = checkout / ".git" / "worktrees" / "card-x"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    side = checkout / ".worktrees" / "card-x"
    (side / "host").mkdir(parents=True)
    (side / "host" / "build.sh").write_text("#!/bin/sh\n")
    (side / ".git").write_text(f"gitdir: {gitdir}\n")
    return side


def test_a_side_folder_reads_as_the_main_checkout(daemon, checkout,
                                                  monkeypatch):
    """An app run from a card's side folder finds that folder by the ancestor
    walk; Mission Control must still open in the main checkout."""
    side = _side_folder(checkout)
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: side)
    assert daemon_board._find_own_checkout() == str(checkout)


@pytest.mark.asyncio
async def test_a_terminal_in_another_folder_is_replaced_not_kept_or_adopted(
        daemon, checkout, monkeypatch, tmp_path):
    """Mission Control opened while the checkout read as a card's side folder
    stayed there across the fix, and Done took that folder's key back: every
    board call was refused as not enrolled. The next open replaces it — on
    the alive rung and on the adopt-by-name rung alike."""
    d, _store = daemon
    opened: list = []
    elsewhere = tmp_path / "side"
    (elsewhere / "docs").mkdir(parents=True)
    (elsewhere / "docs" / "mission-control-brief.md").write_text(
        (checkout / "docs" / "mission-control-brief.md").read_text())
    _enrol(monkeypatch, checkout, elsewhere)
    _real_spawn(monkeypatch, d, opened)
    try:
        for lose_record in (False, True):
            monkeypatch.setattr(dev_build, "find_repo_root", lambda: elsewhere)
            d._mission_attempt = 0.0
            d._adhoc_launches.clear()
            ok, first = await d.open_mission()
            assert ok, first
            assert opened[-1]["root"] == dispatch.normalise_root(str(elsewhere))
            monkeypatch.setattr(dev_build, "find_repo_root", lambda: checkout)
            if lose_record:
                d._mission = {}
            d._mission_attempt = 0.0
            d._adhoc_launches.clear()
            ok, second = await d.open_mission()
            assert ok, second
            assert second != first and second != mission.ALREADY_RUNNING
            assert opened[-1]["root"] == dispatch.normalise_root(str(checkout))
            old = d._pty.get(first)
            assert old is None or old.exited
            await _close_all(d)
            d._mission = {}
    finally:
        await _close_all(d)


def test_the_brief_carries_no_waiting_marker():
    """Mission Control answers nobody: a `bob-tldr` or `bob-actions` marker
    in its brief would teach it to end turns under Needs you."""
    text = (REPO / "docs" / "mission-control-brief.md").read_text()
    assert "bob-tldr" not in text and "bob-actions" not in text


def test_load_record_defaults_the_brief_digest(tmp_path):
    path = tmp_path / "mission.json"
    path.write_text(json.dumps({"handle": "h"}))
    assert mission.load_record(path)["brief_digest"] == ""
    assert mission.brief_digest("a") != mission.brief_digest("b")
    assert len(mission.brief_digest("x")) == 16


@pytest.mark.asyncio
async def test_a_live_terminal_wearing_the_name_is_adopted_not_doubled(
        daemon, checkout, monkeypatch):
    """A lost record after a restart: the broker still holds the process."""
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened)
    try:
        ok, handle = await d.open_mission()
        assert ok, handle
        d._mission = {}
        d._mission_attempt = 0.0
        ok, detail = await d.open_mission()
        assert ok and detail == mission.ALREADY_RUNNING
        assert d._mission["handle"] == handle
        assert len(opened) == 1
    finally:
        await _close_all(d)


# --- the refusals ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_launcher_switch_refuses(daemon, checkout, monkeypatch):
    d, _store = daemon
    d.board_dispatch_enabled = False
    opened: list = []
    _enrol(monkeypatch, checkout)
    _stub_spawn(monkeypatch, opened)
    ok, detail = await d.open_mission()
    assert not ok and "not allowed to start sessions" in detail
    assert opened == []


@pytest.mark.asyncio
async def test_no_checkout_refuses_in_words(daemon, checkout, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _stub_spawn(monkeypatch, opened)
    monkeypatch.setattr(dev_build, "find_repo_root", lambda: None)
    ok, detail = await d.open_mission()
    assert not ok
    assert detail == daemon_board.MISSION_NO_CHECKOUT_REFUSAL
    assert "Dark Army's own checkout" in detail
    assert opened == []


@pytest.mark.asyncio
async def test_an_unenrolled_checkout_refuses(daemon, checkout, monkeypatch,
                                              tmp_path):
    d, _store = daemon
    opened: list = []
    other = tmp_path / "other"
    other.mkdir()
    _enrol(monkeypatch, other)
    _stub_spawn(monkeypatch, opened)
    ok, detail = await d.open_mission()
    assert not ok
    assert detail == daemon_board.MISSION_NOT_ENROLLED_REFUSAL
    assert opened == []


@pytest.mark.asyncio
async def test_a_missing_brief_refuses_in_words(daemon, checkout, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _stub_spawn(monkeypatch, opened)
    (checkout / "docs" / "mission-control-brief.md").unlink()
    ok, detail = await d.open_mission()
    assert not ok
    assert "brief is missing" in detail
    assert opened == []


@pytest.mark.asyncio
async def test_a_disconnected_pty_host_refuses_transiently(
        daemon, checkout, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _stub_spawn(monkeypatch, opened)
    monkeypatch.setattr(type(d._pty), "connected", property(lambda self: False))
    ok, detail = await d.open_mission()
    assert not ok
    assert detail == daemon_board.MISSION_HOST_RECONNECTING_REFUSAL
    assert opened == []


@pytest.mark.asyncio
async def test_a_dispatching_card_in_the_checkout_refuses_project_busy(
        daemon, checkout, monkeypatch):
    d, store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _stub_spawn(monkeypatch, opened)
    card, detail = store.create({"title": "a card", "project": checkout.name,
                                 "root": str(checkout), "prompt": "go",
                                 "tool": "claude", "column_name": "backlog"})
    assert card is not None, detail
    store.update(card["id"], {"link_state": "dispatching",
                              "column_name": "in_progress",
                              "dispatched_at": time.time()})
    ok, detail = await d.open_mission()
    assert not ok
    assert detail == dispatch.PROJECT_BUSY_REFUSAL
    assert dispatch.is_transient(detail)
    assert opened == []


@pytest.mark.asyncio
async def test_a_card_start_during_the_bind_window_queues_and_the_entry_releases(
        daemon, checkout, monkeypatch):
    """The other direction of the one-per-project bound, and its release
    once the terminal is named after its session."""
    d, store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _stub_spawn(monkeypatch, opened)
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(checkout)})
    # The terminal the stubbed spawn "opened": in the map under the handle
    # `owns` answers, unnamed, and not yet wearing Mission Control's name
    # (the adoption rung would otherwise find it before the spawn).
    term = _fake_terminal(d, "h-mission")
    term.name = "starting"
    monkeypatch.setattr(d._pty, "owns", lambda pid: "h-mission")

    ok, detail = await d.open_mission()
    assert ok, detail
    assert detail == "h-mission"
    assert len(d._adhoc_launches) == 1

    card, detail = store.create({"title": "a card", "project": checkout.name,
                                 "root": str(checkout), "prompt": "go",
                                 "tool": "claude", "column_name": "backlog"})
    assert card is not None, detail
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert not ok
    assert len(opened) == 1
    assert store.get(card["id"])["queue_state"] == "queued"

    term.session_id = "s-mission"
    assert d._launch_inflight([]) == []
    assert d._adhoc_launches == {}


# --- end ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_mission_closes_the_recorded_terminal_and_persists(
        daemon, checkout, monkeypatch, tmp_path):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened)
    try:
        ok, handle = await d.open_mission()
        assert ok, handle
        ok, detail = await d.end_mission()
        assert ok, detail
        assert d._pty.get(handle) is None or d._pty.get(handle).exited
        assert d._mission["handle"] == ""
        assert d._mission["session_id"] == ""
        assert d._mission["root"] == dispatch.normalise_root(str(checkout))
        assert d._mission["ended"] is True
        assert d._mission["ended_at"] > 0
        data = json.loads((tmp_path / "mission.json").read_text())
        assert data["handle"] == "" and data["ended"] is True
        # The broker has forgotten the terminal; the snapshot still says
        # `ended`, off the record, rather than `off`.
        snap = d.mission_snapshot()
        assert snap["alive"] is False and snap["exited"] is True
        assert not any(k in snap for k in FORBIDDEN)
    finally:
        await _close_all(d)


@pytest.mark.asyncio
async def test_the_next_open_after_end_clears_the_ended_mark(
        daemon, checkout, monkeypatch):
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened)
    try:
        ok, _handle = await d.open_mission()
        assert ok
        ok, _detail = await d.end_mission()
        assert ok
        assert d.mission_snapshot()["exited"] is True
        d._mission_attempt = 0.0
        d._adhoc_launches.clear()
        ok, second = await d.open_mission()
        assert ok, second
        assert d._mission["ended"] is False and d._mission["ended_at"] == 0.0
        snap = d.mission_snapshot()
        assert snap["alive"] is True and snap["exited"] is False
        assert len(opened) == 2
    finally:
        await _close_all(d)


@pytest.mark.asyncio
async def test_an_unconfirmed_close_keeps_the_record_and_says_so(
        daemon, checkout, monkeypatch):
    """`PtyHost.close` answers False on a broker timeout or a lost link.
    The process may be alive: the record must stay, the reply must not say
    "not running", and the next open must not start a second one."""
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened)
    try:
        ok, handle = await d.open_mission()
        assert ok, handle

        async def unconfirmed(h, grace=None):
            return False
        monkeypatch.setattr(d._pty, "close", unconfirmed)
        ok, detail = await d.end_mission()
        assert not ok
        assert detail == daemon_board.MISSION_CLOSE_FAILED_REFUSAL
        assert detail != daemon_board.MISSION_NOT_RUNNING_REFUSAL
        assert d._mission["handle"] == handle, "the record is kept"
        assert d._mission.get("ended") is not True
        d._mission_attempt = 0.0
        ok, detail = await d.open_mission()
        assert ok and detail == mission.ALREADY_RUNNING
        assert len(opened) == 1, "no second Mission Control"
    finally:
        monkeypatch.undo()
        await _close_all(d)


@pytest.mark.asyncio
async def test_end_mission_refuses_a_terminal_not_wearing_the_name(daemon):
    """The identity guard, re-checked at the moment it fires."""
    d, _store = daemon
    term = _fake_terminal(d, "h-x")
    term.name = "claude · bob"
    d._mission = {"handle": "h-x", "root": "/a", "session_id": "", "opened_at": 1.0}
    ok, detail = await d.end_mission()
    assert not ok
    assert detail == daemon_board.MISSION_IDENTITY_REFUSAL
    assert d._pty.get("h-x") is not None, "nothing was closed"


@pytest.mark.asyncio
async def test_a_card_terminal_titled_mission_control_is_neither_adopted_nor_ended(
        daemon, monkeypatch):
    """A card's terminal is named `card["title"][:40]`, so a card titled
    "Mission Control" wears the label. `mission.PTY_NAME` is longer than any
    such slice, and the End guard and the adopt rung look for that."""
    assert len(mission.PTY_NAME) > 40
    assert "Mission Control"[:40] != mission.PTY_NAME
    d, _store = daemon
    card_term = _fake_terminal(d, "h-card")
    card_term.name = "Mission Control"[:40]
    card_term.session_id = "s-card"
    d._pty._by_session["s-card"] = "h-card"
    d._session_states["s-card"] = {
        "state": "working", "last_event": time.time(),
        "origin": origin.stamp("card-start", "abc123")}
    # The adopt rung, with no record at all.
    d._mission = {}
    assert d._adopt_named_mission_terminal() is None
    # The End guard, with a stale record naming the card's terminal.
    d._mission = {"handle": "h-card", "root": "/a", "session_id": "s-card",
                  "opened_at": 1.0}
    ok, detail = await d.end_mission()
    assert not ok and detail == daemon_board.MISSION_IDENTITY_REFUSAL
    assert d._pty.get("h-card") is not None, "nothing was closed"


@pytest.mark.asyncio
async def test_a_bound_session_must_carry_the_mission_stamp(daemon):
    """The pty name alone is not identity once a session is bound: a
    terminal wearing `PTY_NAME` whose session was started for a card is
    refused by both rungs; one stamped `mission` passes; one whose stamp
    lives only on the tombstone still passes; one with no stamp anywhere
    (the tombstone pruned after eviction) still passes — an absent stamp
    is unknown, not foreign, and the pty name already excludes every card
    terminal."""
    d, _store = daemon
    term = _fake_terminal(d, "h-m")
    term.session_id = "s-x"
    d._pty._by_session["s-x"] = "h-m"
    d._session_states["s-x"] = {"state": "idle", "last_event": time.time(),
                                "origin": origin.stamp("card-start", "abc")}
    d._mission = {}
    assert d._adopt_named_mission_terminal() is None
    d._mission = {"handle": "h-m", "root": "/a", "session_id": "s-x",
                  "opened_at": 1.0}
    ok, detail = await d.end_mission()
    assert not ok and detail == daemon_board.MISSION_IDENTITY_REFUSAL
    # Stamped mission: both rungs pass.
    d._session_states["s-x"]["origin"] = origin.stamp("mission")
    d._mission = {}
    assert d._adopt_named_mission_terminal() is term
    # The stamp on the tombstone alone (the row was evicted) is enough.
    d._session_states["s-x"]["hosted"] = True
    d._forget_session("s-x", "evicted")
    term.session_id = "s-x"  # the broker still names it; only the map was unbound
    assert d._finished["s-x"]["stub"]["origin"] == origin.stamp("mission")
    assert d._adopt_named_mission_terminal() is term
    # Bound to a session nobody has a stamp for (the tombstone pruned):
    # still Mission Control — unknown is not foreign.
    d._finished.pop("s-x")
    assert d._adopt_named_mission_terminal() is term
    # A known foreign stamp on the tombstone alone is still refused.
    d._finished["s-x"] = {"stub": {"origin": origin.stamp("card-start", "abc")},
                          "finished_mono": time.monotonic()}
    assert d._adopt_named_mission_terminal() is None


@pytest.mark.asyncio
async def test_end_closes_after_eviction_pruned_the_tombstone(daemon, checkout,
                                                              monkeypatch):
    """Mission Control's resting state: quiet past 300s evicts the row, the
    roster re-binds the same id at the next enrich, and 30 minutes later
    `_prune_finished` drops the tombstone, so no origin stamp survives
    anywhere. End still closes; open is still idempotent."""
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened)
    try:
        ok, handle = await d.open_mission()
        assert ok
        sid = "s-mission"
        assert d._pty.bind(handle, sid)
        d._session_states[sid] = {"state": "idle", "last_event": time.time() - 400,
                                  "hosted": True, "origin": origin.stamp("mission")}
        d._forget_session(sid, "evicted")
        assert sid not in d._session_states and sid in d._finished
        assert d._pty.bind(handle, sid)
        d._finished[sid]["finished_mono"] -= 31 * 60
        d._prune_finished()
        assert sid not in d._finished
        ok, detail = await d.open_mission()
        assert ok and detail == daemon_board.MISSION_ALREADY_RUNNING
        assert d._pty_handle_for(sid) == handle
        ok, detail = await d.end_mission()
        assert ok, detail
        assert d.mission_snapshot()["exited"]
    finally:
        await _close_all(d)


@pytest.mark.asyncio
async def test_restart_while_evicted_still_ends_and_adopts(daemon, monkeypatch):
    """A daemon restart while the row is evicted: the broker hands the
    terminal back already bound to its session id, `sessions.json` never
    held the evicted row and `_finished` is not persisted. End closes on
    the record; with the record lost, the adopt rung adopts rather than
    letting the next open spawn a second Mission Control."""
    d, _store = daemon
    term = _fake_terminal(d, "h-m")
    term.session_id = "s-mission"
    d._pty._by_session["s-mission"] = "h-m"
    assert "s-mission" not in d._session_states
    assert "s-mission" not in d._finished
    d._mission = {}
    assert d._adopt_named_mission_terminal() is term
    d._mission = {"handle": "h-m", "root": "/a", "session_id": "s-mission",
                  "opened_at": 1.0, "ended": False, "ended_at": 0.0}
    assert d.mission_snapshot()["alive"]
    closed: list = []

    async def _close(handle, grace=None):
        closed.append(handle)
        d._pty._terms.pop(handle, None)
        return True

    monkeypatch.setattr(d._pty, "close", _close)
    ok, detail = await d.end_mission()
    assert ok, detail
    assert closed == ["h-m"]
    assert d._mission["ended"] and d._mission["handle"] == ""


@pytest.mark.asyncio
async def test_end_mission_says_not_running_with_no_terminal(daemon):
    d, _store = daemon
    ok, detail = await d.end_mission()
    assert not ok and detail == daemon_board.MISSION_NOT_RUNNING_REFUSAL
    d._mission = {"handle": "gone", "root": "/a", "session_id": "", "opened_at": 1.0}
    ok, detail = await d.end_mission()
    assert not ok and detail == daemon_board.MISSION_NOT_RUNNING_REFUSAL


@pytest.mark.asyncio
async def test_after_end_the_hosted_row_is_retired_as_terminal_closed(
        daemon, checkout, monkeypatch):
    """End closes the terminal and nothing else; the row's eviction is the
    ordinary hosted-terminal-gone rung."""
    d, _store = daemon
    opened: list = []
    _enrol(monkeypatch, checkout)
    _real_spawn(monkeypatch, d, opened)
    try:
        ok, handle = await d.open_mission()
        assert ok, handle
        sid = "s-mission"
        d._pty.bind(handle, sid)
        d._session_states[sid] = {"state": "idle", "hosted": True,
                                  "last_event": time.time(),
                                  "origin": origin.stamp("mission")}
        ok, detail = await d.end_mission()
        assert ok, detail
        gone = d._retire_hostless_sessions()
        assert gone == [sid]
        assert sid not in d._session_states
        assert d._finished[sid]["end_reason"] == daemon_mod.TERMINAL_CLOSED_REASON
    finally:
        await _close_all(d)


# --- the snapshot ------------------------------------------------------------


FORBIDDEN = ("handle", "key", "digest", "claim", "token", "port")


def test_snapshot_with_no_record_is_available_and_not_alive(daemon):
    d, _store = daemon
    snap = d.mission_snapshot()
    assert snap["available"] is True
    assert snap["alive"] is False
    assert snap["exited"] is False
    assert snap["session_id"] == ""
    assert snap["name"] == "Mission Control"
    assert not any(k in snap for k in FORBIDDEN)


def test_snapshot_says_ended_off_the_record_once_the_terminal_is_gone(daemon):
    d, _store = daemon
    d._mission = {"handle": "", "root": "/a", "session_id": "",
                  "opened_at": 5.0, "ended": True, "ended_at": 9.0}
    snap = d.mission_snapshot()
    assert snap["alive"] is False and snap["exited"] is True
    assert not any(k in snap for k in FORBIDDEN)
    assert "ended_at" not in snap and "ended" not in snap


def test_snapshot_follows_the_terminal_and_falls_back_to_the_record(daemon):
    d, _store = daemon
    term = _fake_terminal(d, "h-m")
    term.name = mission.PTY_NAME
    d._mission = {"handle": "h-m", "root": "/a", "session_id": "s-old",
                  "opened_at": 5.0}
    snap = d.mission_snapshot()
    assert snap["alive"] is True
    assert snap["session_id"] == "s-old", "the record's id while the terminal is unnamed"
    term.session_id = "s-new"
    snap = d.mission_snapshot()
    assert snap["session_id"] == "s-new"
    assert d._mission["session_id"] == "s-new", "a /clear-shaped rebind updates the record"
    assert not any(k in snap for k in FORBIDDEN)
    term.exited_at = time.time()
    snap = d.mission_snapshot()
    assert snap["alive"] is False and snap["exited"] is True


def test_the_section_rides_state_and_is_omittable():
    class _Daemon:
        _agents_poller = None

        def mission_snapshot(self):
            return {"available": True, "alive": False}
    server = ApiServer(_Daemon())
    state = server.state()
    assert state["mission"] == {"available": True, "alive": False}
    assert "mission" in api_mod._OMITTABLE_SECTIONS
    assert api_mod._OMITTABLE_SECTIONS.index("mission") \
        > api_mod._OMITTABLE_SECTIONS.index("security")


# --- the quiet gap -----------------------------------------------------------


def test_a_quiet_evicted_row_is_still_typed_at_by_its_last_id(daemon):
    """`_forget_session("evicted")` pops the state and unbinds the name;
    the record's rung still answers the handle for the row-less callers."""
    d, _store = daemon
    sid = "s-mission"
    term = _fake_terminal(d, "h-m")
    term.name = mission.PTY_NAME
    d._pty.bind("h-m", sid)
    d._session_states[sid] = {"state": "idle", "last_event": time.time()}
    d._mission = {"handle": "h-m", "root": "/a", "session_id": sid,
                  "opened_at": 1.0}
    assert d._pty_handle_for(sid) == "h-m"
    d._forget_session(sid, "evicted")
    assert sid not in d._session_states
    assert d._pty.for_session(sid) is None
    assert d._pty_handle_for(sid) == "h-m"
    assert d._pty.for_session(sid) is None, "resolved only, never bound here"


def test_the_rung_answers_none_for_a_terminal_named_for_another_session(daemon):
    d, _store = daemon
    term = _fake_terminal(d, "h-m")
    term.name = mission.PTY_NAME
    term.session_id = "s-other"
    d._pty._by_session["s-other"] = "h-m"
    d._mission = {"handle": "h-m", "root": "/a", "session_id": "s-mission",
                  "opened_at": 1.0}
    assert d._pty_handle_for("s-mission") is None
    assert d._pty_handle_for("s-unknown") is None


def _fake_terminal(d, handle: str):
    term = ptyhost.PtyTerminal(handle=handle, name=mission.PTY_NAME, root="/a",
                               pid=0, master=-1, proc=None,
                               screen=ptyhost.Screen(80, 24), cols=80, rows=24)
    d._pty._terms[handle] = term
    return term


# --- a reply is quiet ------------------------------------------------------


def _stop(sid, hook="Stop", stamp=""):
    msg = {"event": "add", "hook": hook, "session_id": sid,
           "message": "Claude is waiting for your input"}
    if stamp:
        msg["origin"] = stamp
    return msg


@pytest.mark.asyncio
async def test_a_stop_on_a_mission_session_raises_no_card_and_sleeps(daemon):
    """Every Mission Control reply used to raise the stock card: Needs you,
    the strip's attention figure, the chime, the push, the Live Activity —
    and the card exempted the row from staleness eviction."""
    from dark_army_daemon import alerts as alerting
    d, _store = daemon
    sid = "s-mission"
    d._session_states[sid] = {"state": "working", "last_event": time.time(),
                              "subagents": set(),
                              "origin": origin.stamp("mission")}
    await d._handle_message(_stop(sid))
    assert sid not in d._active_notifications
    assert sid not in d._pending_sound_sessions
    state = d._session_states[sid]
    assert state["state"] == "idle"
    assert d._reconciled_categories()[sid] == "sleeping"
    assert not d._waiting_on_human(sid, state), "ordinary 300s eviction"
    policy = alerting.AlertPolicy()
    snapshot = {"sleeping": [{"session_id": sid, "nickname": "x",
                              "state": "idle", "project": "p"}]}
    assert policy.evaluate(snapshot, dict(d._active_notifications),
                           time.time()) == []
    # Claude's own idle-prompt Notification a minute later: still asleep.
    await d._handle_message(_stop(sid, hook="Notification"))
    assert sid not in d._active_notifications
    assert d._session_states[sid]["state"] == "idle"


@pytest.mark.asyncio
async def test_the_first_stop_reads_the_stamp_off_the_message(daemon):
    """The state may not carry the stamp yet on the very first Stop; the
    message does, and first-writer-wins records it after the card path."""
    d, _store = daemon
    sid = "s-mission"
    d._session_states[sid] = {"state": "working", "last_event": time.time(),
                              "subagents": set()}
    await d._handle_message(_stop(sid, stamp=origin.stamp("mission")))
    assert sid not in d._active_notifications
    assert d._session_states[sid]["state"] == "idle"
    assert d._session_states[sid]["origin"] == origin.stamp("mission")


@pytest.mark.asyncio
async def test_a_card_session_and_a_stop_failure_still_ask(daemon):
    """The rung is specific to the `mission` kind and to the parked hooks."""
    d, _store = daemon
    d._session_states["s-card"] = {"state": "working", "last_event": time.time(),
                                   "subagents": set(),
                                   "origin": origin.stamp("card-start", "abc")}
    await d._handle_message(_stop("s-card"))
    assert "s-card" in d._active_notifications
    d._session_states["s-m"] = {"state": "working", "last_event": time.time(),
                                "subagents": set(),
                                "origin": origin.stamp("mission")}
    await d._handle_message({"event": "add", "hook": "StopFailure",
                             "session_id": "s-m", "message": "rate limited",
                             "error": "rate_limit"})
    assert "s-m" in d._active_notifications
    assert d._session_states["s-m"]["state"] == "error"


# --- the record --------------------------------------------------------------


def test_load_record_keeps_unknown_keys_defaults_missing_and_survives_corruption(
        tmp_path):
    path = tmp_path / "mission.json"
    assert mission.load_record(path) == {}
    path.write_text('{"handle": "h", "future": 1}')
    rec = mission.load_record(path)
    assert rec["handle"] == "h" and rec["future"] == 1
    assert rec["root"] == "" and rec["session_id"] == "" and rec["opened_at"] == 0.0
    assert rec["ended"] is False and rec["ended_at"] == 0.0, \
        "a record written before End's marks reads them as defaults"
    path.write_text('{"handle": "", "ended": "yes", "ended_at": "soon"}')
    rec = mission.load_record(path)
    assert rec["ended"] is False and rec["ended_at"] == 0.0
    path.write_text('{"handle": "", "ended": true, "ended_at": 12.5}')
    rec = mission.load_record(path)
    assert rec["ended"] is True and rec["ended_at"] == 12.5
    path.write_text("{not json")
    assert mission.load_record(path) == {}
    path.write_text("[1, 2]")
    assert mission.load_record(path) == {}


def test_save_record_writes_0600(tmp_path, monkeypatch):
    path = tmp_path / "mission.json"
    mission.save_record(path, {"handle": "h", "root": "/a"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["handle"] == "h"


def test_read_brief_refuses_missing_empty_and_oversize(tmp_path):
    assert mission.read_brief(tmp_path)[0] == ""
    assert "missing" in mission.read_brief(tmp_path)[1]
    (tmp_path / "docs").mkdir()
    brief = tmp_path / "docs" / "mission-control-brief.md"
    brief.write_text("   \n")
    assert "empty" in mission.read_brief(tmp_path)[1]
    brief.write_bytes(b"x" * (mission.MAX_BRIEF_BYTES + 1))
    assert "too long" in mission.read_brief(tmp_path)[1]
    brief.write_text("You are Mission Control.")
    assert mission.read_brief(tmp_path) == ("You are Mission Control.", "")


# --- the doors ---------------------------------------------------------------


def test_both_verbs_are_chosen_on_both_phone_tuples_once():
    for verb in ("mission_open", "mission_end"):
        assert ApiServer.LAN_ACTIONS.count(verb) == 1
        assert ApiServer.REMOTE_ACTIONS.count(verb) == 1
        assert verb not in ApiServer.BOARD_ACTIONS
        assert verb not in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    api = (REPO / "host" / "dark_army_daemon" / "api_server.py").read_text()
    chosen = api.split("LAN_ACTIONS = (", 1)[1].split(")", 1)[0]
    assert '"mission_open"' in chosen and '"mission_end"' in chosen
    # `REMOTE_ACTIONS`' own comments carry a parenthesis before the tuple
    # ends, so its membership is the tuple read above, never a slice.


@pytest.mark.asyncio
async def test_lan_run_reaches_the_daemon_and_answers_409_in_its_words():
    class _Daemon:
        open_mission = AsyncMock(return_value=(False, "Mission Control needs Dark Army's own checkout"))
        end_mission = AsyncMock(return_value=(False, "Mission Control is not running"))
    server = ApiServer(_Daemon())
    status, _ctype, body = await server._lan_run("mission_open", {"root": "/evil"})
    assert status == 409
    assert b"own checkout" in body
    _Daemon.open_mission.assert_awaited_once_with()
    status, _ctype, body = await server._lan_run("mission_end", {})
    assert status == 409 and b"not running" in body


@pytest.mark.asyncio
async def test_the_phone_door_never_sees_the_handle():
    class _Daemon:
        async def open_mission(self):
            return True, "h-secret-handle"
    server = ApiServer(_Daemon())
    status, _ctype, body = await server._lan_run("mission_open", {})
    assert status == 200
    assert b"h-secret-handle" not in body
    assert json.loads(body)["detail"] == mission.STARTED


@pytest.mark.asyncio
async def test_the_loopback_reply_carries_the_handle_and_the_token_gates_it():
    calls: list = []

    class _Daemon:
        async def open_mission(self):
            calls.append("open")
            return True, "h-1"

        async def end_mission(self):
            calls.append("end")
            return True, "ended"

    server = ApiServer(_Daemon())
    server.token = "the-secret"
    body = b'{"action": "mission_open"}'
    bad = _Request("POST", "/api/action", {},
                   {"content-type": "application/json"}, body)
    assert server._mission_request(bad) is None
    status, _ctype, _out = server._route(bad)
    assert status == 403
    assert calls == []
    good = _Request("POST", "/api/action", {},
                    {"content-type": "application/json",
                     "x-bob-token": "the-secret"}, body)
    assert server._mission_request(good) == "mission_open"
    status, _ctype, out = await server._mission("mission_open")
    assert status == 200 and json.loads(out)["detail"] == "h-1"
    status, _ctype, out = await server._mission("mission_end")
    assert status == 200 and calls == ["open", "end"]


# --- the origin, the menu bar and the brief ----------------------------------


def test_the_menu_bar_app_knows_nothing_about_this():
    """Stated rather than omitted: no AppKit main-thread work, no strip
    rung, no menu row. The needle is word-bounded: `hooks.py` has always
    said `permission_broker`, whose tail spells `mission_`."""
    import re
    pkg = REPO / "host" / "dark_army_menubar"
    for path in pkg.glob("*.py"):
        text = path.read_text()
        assert not re.search(r"\bmission_|\bmission\.|MISSION", text), path.name


def test_the_brief_curls_the_api_port_never_the_hook_socket():
    """Integration: the hook socket (`BOB_COMPANION_PORT`, 19873) answers no
    HTTP — `curl :19873/api/state` is an empty reply — so every curl in the
    brief names the API door as the exact spelling `mission.CURL_RULE`
    pre-approves, and the brief never tells the agent to curl the variable.
    `?` is a zsh glob and a quoted URL misses the rule, so neither appears
    in a curl."""
    import re
    text = (REPO / "docs" / "mission-control-brief.md").read_text()
    assert str(mission.API_PORT) in text and "19874" in text
    assert "19873" not in text
    curls = re.findall(r"curl -s [^`\n]+", text)
    assert curls, "the brief shows the curl spelling"
    for cmd in curls:
        assert cmd.startswith(f"curl -s {mission.API_URL}/"), cmd
        assert "BOB_COMPANION_PORT" not in cmd, cmd
        assert "?" not in cmd and "'" not in cmd and '"' not in cmd, cmd
    assert not re.search(r"curl[^\n]*\$BOB_COMPANION_PORT", text)
    assert mission.STATE_SCRATCH in text
    assert "hook socket" in text


def test_the_pretty_state_route_is_the_same_picture_in_lines(daemon, checkout):
    """Integration (CLI 2.1.278): `/api/state` is one ~100 KB line, which the
    CLI's `Read` refuses whole (a token ceiling, not a permission) and whose
    only `Grep` hit is "[Omitted long matching line]", so the brief's one
    route to the live picture was unreadable. `GET /api/state/pretty` is the
    same `state()` indented: newline-bearing, short lines, one path (the
    curl rule can carry no `?`), served by `_route` alone so the LAN door
    never sees it, and the SSE frame stays one line."""
    d, store = daemon
    for n in range(40):
        card, detail = store.create({"title": f"card {n}", "project": checkout.name,
                                     "root": str(checkout), "prompt": "p " * 400,
                                     "tool": "claude", "column_name": "backlog"})
        assert card is not None, detail
    d._board_state = d._build_board_state()
    server = ApiServer(d)
    plain = _Request("GET", "/api/state", "", {}, b"")
    pretty = _Request("GET", "/api/state/pretty", "", {}, b"")
    status, ctype, flat = server._route(plain)
    assert status == 200 and b"\n" not in flat
    status, ctype, body = server._route(pretty)
    assert status == 200 and ctype == "application/json"
    text = body.decode()
    assert text.count("\n") > 100
    a, b = json.loads(text), json.loads(flat)
    a.pop("generated_at"), b.pop("generated_at")   # each call stamps its own clock
    assert a == b and len(a["board"]["cards"]) == 40
    # A window of 300 lines (`Read` with `offset` / `limit`) stays well
    # under the CLI's 25 000-token ceiling at roughly four bytes a token;
    # no single line is anywhere near it.
    lines = text.split("\n")
    assert max(len(line) for line in lines) < 4000
    for start in range(0, len(lines), 300):
        assert sum(len(line) + 1 for line in lines[start:start + 300]) < 25000 * 4
    # The path is loopback-only: the LAN handler's routing table does not
    # know it, so it falls to `not_found` there, never to a body.
    src = (REPO / "host" / "dark_army_daemon" / "api_server.py").read_text()
    assert src.count('"/api/state/pretty"') == 1
    assert "indent=2" not in src[src.index("async def _serve_events"):
                                 src.index("async def _serve_events") + 6000]


def test_no_platform_branch_in_the_new_module():
    for name in ("mission.py", "daemon_board.py"):
        src = (REPO / "host" / "dark_army_daemon" / name).read_text()
        assert "sys.platform" not in src, name


# --- always called Mission Control ------------------------------------------


def test_the_argv_carries_no_name_flag_when_none_is_given():
    argv = dispatch.mission_argv("/x/claude", "{}", ["Grep"], "a", "p")
    assert "--name" not in argv


@pytest.mark.asyncio
async def test_open_names_the_session_mission_control(daemon, checkout,
                                                      monkeypatch):
    """The CLI's display name — the Claude app's Remote Control title and
    the terminal title — is fixed at spawn, not drawn from the chat."""
    d, _store = daemon
    _enrol(monkeypatch, checkout)
    opened = []
    _stub_spawn(monkeypatch, opened)
    ok, _detail = await d.open_mission()
    assert ok
    argv = opened[0]["argv"]
    assert argv[argv.index("--name") + 1] == "Mission Control"


def _row_named(d, sid):
    for rows in d.detailed_snapshot().values():
        if isinstance(rows, list):
            for row in rows:
                if row.get("session_id") == sid:
                    return row
    raise AssertionError(f"no row for {sid}")


def test_a_mission_row_is_always_called_mission_control():
    """Neither a generated title nor the first message after a `/clear`
    renames it; a session without the stamp keeps its own name."""
    d = BobDaemon()
    d._session_states["s-m"] = {"state": "idle", "last_event": 1e12, "pid": 1,
                                "origin": origin.stamp("mission")}
    d._session_states["s-x"] = {"state": "idle", "last_event": 1e12, "pid": 2}
    d._session_metrics["s-m"] = {"session_name": "Phone helper tab strip UI"}
    d._session_metrics["s-x"] = {"session_name": "Phone helper tab strip UI"}
    assert _row_named(d, "s-m")["name"] == "Mission Control"
    assert _row_named(d, "s-x")["name"] == "Phone helper tab strip UI"


# --- a /clear after an eviction ---------------------------------------------


def _cleared_mission(d, monkeypatch):
    """The live shape of 25 Sep 2026: the evicted id on the record, the
    terminal unbound, and the `/clear` successor running in it."""
    term = _fake_terminal(d, "h-m")
    term.name = mission.PTY_NAME
    d._mission = {"handle": "h-m", "root": "/a", "session_id": "s-evicted",
                  "opened_at": 1.0}
    monkeypatch.setattr(d._pty, "owns",
                        lambda pid: "h-m" if pid in (777, 778) else None)
    d._session_states["s-new"] = {"state": "working", "pid": 777,
                                  "origin": origin.stamp("mission"),
                                  "last_event_monotonic": 10.0}
    return term


def test_the_snapshot_follows_a_clear_after_an_eviction(daemon, monkeypatch):
    d, _store = daemon
    _cleared_mission(d, monkeypatch)
    assert d.mission_snapshot()["session_id"] == "s-new"
    assert d._mission["session_id"] == "s-new"


def test_the_successor_must_carry_the_stamp_and_run_in_the_terminal(
        daemon, monkeypatch):
    d, _store = daemon
    _cleared_mission(d, monkeypatch)
    d._session_states.pop("s-new")
    # A card session inside the same process tree, and a mission-stamped
    # session in some other terminal: neither is Mission Control.
    d._session_states["s-card"] = {
        "pid": 778, "origin": origin.stamp("card-start", "c1"),
        "last_event_monotonic": 99.0}
    d._session_states["s-far"] = {
        "pid": 900, "origin": origin.stamp("mission"),
        "last_event_monotonic": 99.0}
    assert d.mission_snapshot()["session_id"] == "s-evicted"


@pytest.mark.asyncio
async def test_a_cleared_mission_control_may_still_ask_for_a_start(
        daemon, monkeypatch):
    d, _store = daemon
    _cleared_mission(d, monkeypatch)
    _card, detail = await d.ask_start("s-new", "nope")
    assert detail == "no card has that id", "past the identity check"
    _card, detail = await d.ask_start("s-card", "nope")
    assert detail == "only Mission Control can ask Dark Army to start a card"


def test_a_name_the_broker_kept_for_a_forgotten_id_is_repointed(
        daemon, monkeypatch):
    """After a restart the broker re-adopts the terminal still wearing the
    evicted id; the stamped successor running in it takes over, and the
    stale name is dropped so the next enrich can bind the successor."""
    d, _store = daemon
    _cleared_mission(d, monkeypatch)
    d._pty.bind("h-m", "s-evicted")
    assert d.mission_snapshot()["session_id"] == "s-new"
    assert d._pty.get("h-m").session_id == ""
    assert d._pty.for_session("s-evicted") is None


def test_a_live_bound_session_is_never_repointed(daemon, monkeypatch):
    d, _store = daemon
    _cleared_mission(d, monkeypatch)
    d._pty.bind("h-m", "s-evicted")
    d._session_states["s-evicted"] = {"pid": 777,
                                      "origin": origin.stamp("mission")}
    assert d.mission_snapshot()["session_id"] == "s-evicted"
    assert d._pty.get("h-m").session_id == "s-evicted"
