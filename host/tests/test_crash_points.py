"""The crash-at-every-step harness for the action journal
(`docs/action-journal.md`).

Each journalled action is driven through its real code path with its effects
stubbed. `board_journal_store.CRASH_HOOK` raises `SimulatedCrash` at the Nth
commit (an intent: the instant before the side effect; a result: the instant
after it). The board file is then snapshotted with `sqlite3`'s backup (what a
relaunch would find on disk), reopened under a fresh `BobDaemon`, and recovered
twice. The asserts are the card's success criterion: the card shows the
terminal that opened or sits back where it was with the note, nothing is
launched, signalled or typed again, no card reaches Done, and the second
recovery changes nothing.
"""
import asyncio
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from dark_army_daemon import action_journal as aj
from dark_army_daemon import autocompact, board_journal_store, dispatch
from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import session_io
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


class SimulatedCrash(BaseException):
    """A process death. Not an `Exception`: no `except Exception` in the code
    under test may swallow it."""


@pytest.fixture(autouse=True)
def _reset_hook():
    board_journal_store.CRASH_HOOK = None
    yield
    board_journal_store.CRASH_HOOK = None


class World:
    """One daemon on one store, every effect stubbed and counted."""

    def __init__(self, tmp_path, name, mp):
        self.dir = tmp_path / name
        self.dir.mkdir()
        self.mp = mp
        self.d = BobDaemon(sessions_path=self.dir / "sessions.json")
        self.store = BoardStore(self.dir / "board.db")
        self.store.connect()
        self.d._board = self.store
        self.spawned = 0
        self.typed = 0
        self.terminated = 0
        self.killed = 0
        self.gits = 0
        self.alive = True
        self.events = []
        self.d._event_log = SimpleNamespace()
        self.d._log_event = lambda kind, **f: self.events.append((kind, f))
        self.d._pty = SimpleNamespace(owns=lambda pid: "handle", bind=lambda *a: None)
        world = self

        async def spawn(*a, **k):
            world.spawned += 1
            return True, "bob", 4242

        async def send_text(*a, **k):
            world.typed += 1
            return {"sent": True}

        async def run_git(*a, **k):
            world.gits += 1
            return False, b"", "no git in a crash test"

        class Proc:
            def __init__(self, pid):
                pass

            def terminate(self):
                world.terminated += 1

            def kill(self):
                world.killed += 1

            def name(self):
                return "claude"

            def cmdline(self):
                return ["claude"]

        mp.setattr(dispatch, "spawn", spawn)
        mp.setattr(dispatch, "spawn_local", spawn)
        mp.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
        mp.setattr(session_io, "send_text", send_text)
        mp.setattr(daemon_mod.psutil, "Process", Proc)
        mp.setattr(daemon_mod.psutil, "pid_exists", lambda pid: world.alive)
        mp.setattr(daemon_mod, "QUESTION_KEY_GAP_SECONDS", 0)
        self.d._run_git = run_git
        self.d._known_project_roots = lambda: {"/private/tmp", "/tmp",
                                               str(self.dir)}

    def card(self, **kw):
        fields = {"title": "do the thing", "project": "bob", "root": "/tmp",
                  "prompt": "go", "tool": "claude", "column_name": "backlog"}
        fields.update(kw)
        card, detail = self.store.create(fields)
        assert card is not None, detail
        return card

    def counts(self):
        return (self.spawned, self.typed, self.terminated, self.killed, self.gits)


# --- the five scenarios: kind -> coroutine driving the real path -----------------

async def _card_start(w: World, variant: str):
    card = w.card()
    w.cid = card["id"]
    own = variant == "pty"
    if variant == "queued":
        w.store.update(card["id"], {"queue_state": "queued",
                                    "queued_at": time.time()})
    await w.d.dispatch_card(card["id"], allow_unplanned=True,
                            queued_replay=variant == "queued",
                            own_terminal=True if own else None)


async def _worktree_prepare(w: World, variant: str):
    root = w.dir / "repo"
    root.mkdir()
    card = w.card(root=str(root))
    w.cid = card["id"]
    from dark_army_daemon import worktrees
    path = worktrees.worktree_dir(dispatch.normalise_root(str(root)), card["id"])
    w.path = path
    reused = variant == "reused"
    if reused:
        os.makedirs(path)

    async def run_git(argv, cwd, timeout=None):
        text = " ".join(argv)
        if "worktree list" in text:
            out = f"worktree {os.path.realpath(path)}\n\n" if reused else ""
            return True, out.encode(), ""
        if "worktree add" in text:
            w.mp.setattr(os, "makedirs", os.makedirs)
            os.makedirs(path, exist_ok=True)
            return True, b"", ""
        return False, b"", ""

    async def base(_root):
        return "HEAD"

    async def redispatch(*a, **k):
        return True, ""

    w.d._run_git = run_git
    w.d._worktree_base = base
    w.d._finish_worktree = lambda *a, **k: ""
    w.d.dispatch_card = redispatch
    w.d._worktree_preparing = {card["id"]: {"token": "t"}}
    replay = {"card_id": card["id"], "queued_replay": variant == "queued"}
    await w.d._prepare_worktree_then_dispatch(
        w.store.get(card["id"]), str(root), replay, token="t")


async def _stop_session(w: World, variant: str):
    w.d._agent_records["sess-1"] = SimpleNamespace(pid=4242)
    w.d._session_provider = lambda sid: "claude"
    real_confirm = w.d._confirm_stop

    async def nothing(*a, **k):
        return None

    async def instantly(_delay):
        return None

    w.d._confirm_stop = nothing
    await w.d.stop_session_recorded("sess-1")
    if variant == "both":
        w.mp.setattr(daemon_mod.asyncio, "sleep", instantly)
        await real_confirm("sess-1", 4242)


async def _autocompact(w: World, variant: str):
    w.d._session_provider = lambda sid: "claude"
    w.d._compact_queue = [("sess-1", 4242, "claude")]
    await w.d._flush_auto_compacts()


async def _answer_burst(w: World, variant: str):
    multi = variant == "multi"
    questions = [{"multi_select": multi}]
    groups = [[0, 1] if multi else [0]]
    await w.d._type_answer_burst("sess-1", 4242, questions, groups)


SCENARIOS = {
    "card_start": _card_start,
    "worktree_prepare": _worktree_prepare,
    "stop_session": _stop_session,
    "autocompact": _autocompact,
    "answer_burst": _answer_burst,
}

#: (kind, variant) -> how many commits the scenario makes. A step added to an
#: action without a line here fails `test_the_dry_run_counts_every_commit`.
POINTS = {
    ("card_start", "plain"): 4,
    ("card_start", "queued"): 4,
    ("card_start", "pty"): 4,
    ("worktree_prepare", "new"): 4,
    ("worktree_prepare", "reused"): 4,
    ("stop_session", "both"): 4,
    ("stop_session", "terminate"): 2,
    ("autocompact", "once"): 2,
    ("answer_burst", "single"): 2,
    ("answer_burst", "multi"): 2,
}

CASES = [(kind, variant, point)
         for (kind, variant), n in POINTS.items() for point in range(1, n + 1)]


async def _drive(tmp_path, mp, kind, variant, point):
    """Run the scenario, crashing at commit `point` (None: never). Returns the
    world and whether it crashed."""
    w = World(tmp_path, f"{kind}-{variant}-{point}", mp)
    seen = []

    def hook(phase, k, step, ident):
        seen.append((phase, k, step))
        if point is not None and len(seen) == point:
            raise SimulatedCrash()

    board_journal_store.CRASH_HOOK = hook
    crashed = False
    try:
        await SCENARIOS[kind](w, variant)
    except SimulatedCrash:
        crashed = True
    finally:
        board_journal_store.CRASH_HOOK = None
    w.commits = seen
    return w, crashed


def _relaunch(w: World, mp):
    """What a relaunch finds on disk: the snapshot, under a fresh daemon."""
    snap = w.dir / "relaunch.db"
    src = sqlite3.connect(w.dir / "board.db")
    dst = sqlite3.connect(snap)
    src.backup(dst)
    src.close()
    dst.close()
    w.store.close()
    fresh = World.__new__(World)
    fresh.dir, fresh.mp = w.dir, mp
    fresh.d = BobDaemon(sessions_path=w.dir / "sessions-2.json")
    fresh.store = BoardStore(snap)
    fresh.store.connect()
    fresh.d._board = fresh.store
    fresh.events = []
    fresh.d._event_log = SimpleNamespace()
    fresh.d._log_event = lambda kind, **f: fresh.events.append((kind, f))
    fresh.d._pty = SimpleNamespace(owns=lambda pid: "handle")
    fresh.alive = True

    async def no_git(*a, **k):
        raise AssertionError("recovery ran a git command")

    fresh.d._run_git = no_git
    return fresh


def _snapshot(world):
    return (world.store.cards(), world.store.journal_counts(),
            len(world.events))


@pytest.mark.parametrize("kind,variant", sorted(POINTS))
@pytest.mark.asyncio
async def test_the_dry_run_counts_every_commit(tmp_path, monkeypatch, kind, variant):
    w, crashed = await _drive(tmp_path, monkeypatch, kind, variant, None)
    try:
        assert not crashed
        assert len(w.commits) == POINTS[(kind, variant)], w.commits
        assert w.store.journal_open() == []
    finally:
        w.store.close()


@pytest.mark.parametrize("kind,variant,point", CASES)
@pytest.mark.asyncio
async def test_a_crash_at_every_commit_recovers_cleanly(
        tmp_path, monkeypatch, kind, variant, point):
    w, crashed = await _drive(tmp_path, monkeypatch, kind, variant, point)
    assert crashed, "the scenario never reached the crash point"
    # What the dead process had already done before it died.
    before = w.counts()
    phase, _k, step = w.commits[-1]
    cid = getattr(w, "cid", "")
    fresh = _relaunch(w, monkeypatch)
    try:
        # A launcher, a signal or a keystroke on the fresh daemon would show
        # here: the stubs are shared, so the count must not move.
        first = await fresh.d.recover_actions()
        assert fresh.store.journal_open() == []
        assert w.counts() == before, "recovery repeated a side effect"
        after_first = _snapshot(fresh)
        second = await fresh.d.recover_actions()
        assert (second["open"], second["replayed"], second["interrupted"]) \
            == (0, 0, 0)
        assert _snapshot(fresh) == after_first, "the second recovery changed something"
        assert all(c["column_name"] != "done" for c in fresh.store.cards())
        assert first["mismatches"]["open_after"] == []
        _kind_invariants(fresh, w, kind, variant, point, phase, step, cid)
    finally:
        fresh.store.close()


def _diary(fresh, name):
    return [f for k, f in fresh.events if k == name]


def _kind_invariants(fresh, w, kind, variant, point, phase, step, cid):
    store = fresh.store
    if kind == "card_start":
        card = store.get(cid)
        if point == 1:
            # The terminal never opened: back where it was, with the note.
            assert card["column_name"] == "backlog"
            assert card["link_state"] == ""
            assert card["dispatch_error"] == aj.RESTART_DURING_START_NOTE
            assert card["queue_state"] == ""
            assert cid not in fresh.d._spawn_shell_pids
            assert cid not in fresh.d._spawn_pty_pids
            return
        # The terminal opened: the card shows it, bound the way a live Start
        # would have bound it.
        assert card["column_name"] == "in_progress"
        assert card["link_state"] == "dispatching"
        assert card["dispatched_at"] is not None
        assert card["dispatch_error"] == ""
        assert card["queue_state"] == ""
        assert fresh.d._dispatch_baseline[cid] == set()
        receipt = (fresh.d._spawn_pty_pids if variant == "pty"
                   else fresh.d._spawn_shell_pids)
        assert receipt[cid] == 4242
    elif kind == "worktree_prepare":
        card = store.get(cid)
        assert card["column_name"] == "backlog"
        if point == 1:
            assert card["worktree_path"] in ("", None)
            assert card["dispatch_error"] == aj.RESTART_DURING_PREPARE_NOTE
        elif point in (2, 3):
            assert card["worktree_path"] == w.path
            assert card["dispatch_error"] == aj.RESTART_DURING_PREPARE_NOTE
        else:
            # Recorded and resolved; the press's re-entry alone was lost.
            assert card["worktree_path"] == w.path
        assert card["queue_state"] == ""
    elif kind == "stop_session":
        interrupted = _diary(fresh, "action_interrupted")
        if phase == "intent":
            assert [f["detail"]["step"] for f in interrupted] == [step]
        else:
            assert interrupted == []
    elif kind == "autocompact":
        policy = fresh.d.__dict__["_autocompact"]
        assert policy.pending("sess-1")
        signal = {"rule": "ctx-full", "severity": "crit", "text": "x — y"}
        assert policy.consider("sess-1", [signal], 91.0, True,
                               time.time()) == autocompact.HOLD
        assert len(_diary(fresh, "action_interrupted")) == (
            1 if phase == "intent" else 0)
    elif kind == "answer_burst":
        assert len(_diary(fresh, "action_interrupted")) == (
            1 if phase == "intent" else 0)
        assert not fresh.d._answering


def test_every_declared_action_has_a_crash_scenario():
    assert set(aj.ACTIONS) == set(SCENARIOS)
    assert {kind for kind, _v in POINTS} == set(SCENARIOS)


@pytest.mark.asyncio
async def test_a_merge_style_preparation_leaves_the_journal_alone(tmp_path, monkeypatch):
    """`_merge_folder` and `_merge_run` call `_prepare_worktree` outside a
    Start: no action, no attempt, nothing open at the next launch."""
    from dark_army_daemon import worktrees
    w = World(tmp_path, "merge", monkeypatch)
    try:
        root = w.dir / "repo"
        root.mkdir()
        card = w.card(root=str(root))
        path = worktrees.worktree_dir(dispatch.normalise_root(str(root)), card["id"])

        async def run_git(argv, cwd, timeout=None):
            if "worktree add" in " ".join(argv):
                os.makedirs(path, exist_ok=True)
                return True, b"", ""
            return (True, b"", "") if "worktree list" in " ".join(argv) else (False, b"", "")

        async def base(_root):
            return "HEAD"

        w.d._run_git = run_git
        w.d._worktree_base = base
        w.d._finish_worktree = lambda *a, **k: ""
        _p, _b, error = await w.d._prepare_worktree(w.store.get(card["id"]), str(root))
        assert not error
        assert w.store.journal_open() == []
        assert w.store.journal_counts() == {"intents": 0, "results": 0, "attempts": 0}
    finally:
        w.store.close()


@pytest.mark.asyncio
async def test_a_raising_preparation_leaves_no_journal_id_behind(tmp_path, monkeypatch):
    w = World(tmp_path, "raise", monkeypatch)
    try:
        card = w.card()
        w.d._worktree_preparing = {card["id"]: {"token": "t"}}
        w.d._prepare_journal = {card["id"]: "stale"}

        async def boom(*a, **k):
            raise RuntimeError("git exploded")

        w.d._prepare_worktree = boom
        await w.d._prepare_worktree_then_dispatch(
            w.store.get(card["id"]), "/tmp", {"card_id": card["id"]}, token="t")
        assert card["id"] not in w.d._prepare_journal
    finally:
        w.store.close()
