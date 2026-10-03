"""Launch-time recovery of the journalled actions (`docs/action-journal.md`).

Every branch runs on test_dispatch.py's `daemon` shape: a real `BobDaemon` on
a real `BoardStore` over a temp file, with the launcher, the keystroke sender
and the process table stubbed and counted. Recovery must never reach any of
them.
"""
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from dark_army_daemon import action_journal as aj
from dark_army_daemon import autocompact, daemon_recovery, dispatch, session_io
from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


class Effects:
    """Everything recovery must never do, counted."""

    def __init__(self):
        self.spawn = 0
        self.typed = 0
        self.terminate = 0
        self.kill = 0


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    effects = Effects()
    events = []
    d._event_log = SimpleNamespace()
    d._log_event = lambda kind, **f: events.append((kind, f))
    d.effects, d.events = effects, events

    async def spawn(*a, **k):
        effects.spawn += 1
        return True, "bob", 4242

    async def send_text(*a, **k):
        effects.typed += 1
        return {"sent": True}

    monkeypatch.setattr(dispatch, "spawn", spawn)
    monkeypatch.setattr(dispatch, "spawn_local", spawn)
    monkeypatch.setattr(session_io, "send_text", send_text)

    class Proc:
        def __init__(self, pid):
            pass

        def terminate(self):
            effects.terminate += 1

        def kill(self):
            effects.kill += 1

        def name(self):
            return "claude"

        def cmdline(self):
            return ["claude"]

    monkeypatch.setattr(daemon_mod.psutil, "Process", Proc)
    try:
        yield d, store
    finally:
        store.close()


def _make(store, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": "/tmp",
              "prompt": "go", "tool": "claude", "column_name": "backlog"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _start(store, cid, *, spawn="open", record=None, now=None, shell=4242,
           pty_pid=0, batch=False):
    """Write what a Start would have written up to a crash. `spawn` is `open`
    or the outcome of the result; `record` is None (never written), `open`
    or an outcome."""
    now = time.time() - 5 if now is None else now
    action = store.journal_begin("card_start", cid)
    s = store.journal_intent(action, "card_start", cid, "spawn", {
        "card_id": cid, "root": "/tmp", "cwd": "", "tool": "claude",
        "use_own": False, "queued_replay": False, "pre_start_column": "backlog",
        "batch": batch})
    if spawn != "open":
        store.journal_result(s, spawn, "", {"shell_pid": shell, "pty_pid": pty_pid,
                                            "now": now, "use_own": False})
    r = None
    if record is not None:
        r = store.journal_intent(action, "card_start", cid, "record",
                                 {"card_id": cid, "now": now, "cwd": "",
                                  "root": "/tmp", "batch": batch})
        if record != "open":
            store.journal_result(r, record)
    return action, s, r, now


def _results(store):
    return {r["intent_id"]: r["outcome"] for r in store._conn.execute(
        "SELECT intent_id, outcome FROM action_results")}


def _kinds(d):
    return [k for k, _f in d.events]


# --- card start --------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawn_open_leaves_the_card_in_place_with_the_note(daemon):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"queue_state": "queued", "queued_at": time.time()})
    _a, spawn, _r, _n = _start(store, card["id"])
    report = await d.recover_actions()
    got = store.get(card["id"])
    assert got["column_name"] == "backlog"
    assert got["dispatch_error"] == aj.RESTART_DURING_START_NOTE
    assert got["queue_state"] == ""
    assert d.effects.spawn == 0
    assert _results(store)[spawn] == aj.INTERRUPTED
    assert report["interrupted"] == 1 and report["open"] == 1
    failed = [f for k, f in d.events if k == "card_dispatch_failed"]
    assert failed and failed[0]["detail"]["error"] == aj.RESTART_DURING_START_NOTE


@pytest.mark.asyncio
async def test_spawn_open_on_a_card_that_moved_on_is_left_alone(daemon):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"link_state": "dispatching",
                              "column_name": "in_progress",
                              "dispatched_at": time.time()})
    _start(store, card["id"])
    await d.recover_actions()
    assert store.get(card["id"])["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_record_open_inside_the_window_finishes_the_write(daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    _a, _s, rec, now = _start(store, card["id"], spawn="spawned", record="open",
                              shell=4242)
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    await d.recover_actions()
    got = store.get(card["id"])
    assert got["link_state"] == "dispatching"
    assert got["column_name"] == "in_progress"
    assert got["dispatched_at"] == pytest.approx(now)
    assert d._spawn_shell_pids[card["id"]] == 4242
    assert d._dispatch_baseline[card["id"]] == set()
    assert d.effects.spawn == 0
    assert _results(store)[rec] == aj.REPLAYED
    assert "card_dispatched" in _kinds(d)


@pytest.mark.asyncio
async def test_a_receipt_for_a_pid_that_is_gone_is_not_restored(daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    _start(store, card["id"], spawn="spawned", record="open")
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: False)
    await d.recover_actions()
    assert card["id"] not in d._spawn_shell_pids
    assert d._dispatch_baseline[card["id"]] == set()


@pytest.mark.asyncio
async def test_a_pty_receipt_needs_the_broker_to_hold_it(daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    _start(store, card["id"], spawn="spawned", record="open", shell=0,
           pty_pid=777)
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    d._pty = SimpleNamespace(owns=lambda pid: None)
    await d.recover_actions()
    assert card["id"] not in d._spawn_pty_pids
    other = _make(store, title="other")
    _start(store, other["id"], spawn="spawned", record="open", shell=0,
           pty_pid=778)
    store.journal_prune()
    d._pty = SimpleNamespace(owns=lambda pid: "handle")
    await d.recover_actions()
    assert d._spawn_pty_pids[other["id"]] == 778


@pytest.mark.asyncio
async def test_record_open_outside_the_window_replays_and_restores_nothing(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    long_ago = time.time() - dispatch.DISPATCH_BIND_WINDOW - 60
    _start(store, card["id"], spawn="spawned", record="open", now=long_ago)
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    await d.recover_actions()
    assert store.get(card["id"])["link_state"] == "dispatching"
    assert card["id"] not in d._spawn_shell_pids
    assert card["id"] not in d._dispatch_baseline
    # The bind's own expiry does what it does today.
    d._reconcile_board({"running": [], "waiting": [], "sleeping": []})
    got = store.get(card["id"])
    assert got["link_state"] == ""
    assert got["dispatch_error"]
    assert got["column_name"] in ("backlog", "prep")


@pytest.mark.asyncio
async def test_record_open_on_a_card_bound_meanwhile_is_interrupted(daemon):
    d, store = daemon
    card = _make(store)
    _a, _s, rec, _n = _start(store, card["id"], spawn="spawned", record="open")
    store.update(card["id"], {"link_state": "live", "session_id": "sess-1",
                              "column_name": "in_progress"})
    before = store.get(card["id"])
    await d.recover_actions()
    assert store.get(card["id"]) == before
    assert _results(store)[rec] == aj.INTERRUPTED


@pytest.mark.asyncio
async def test_a_record_whose_write_landed_is_only_completed(daemon):
    d, store = daemon
    card = _make(store)
    _a, _s, rec, now = _start(store, card["id"], spawn="spawned", record="open")
    store.update(card["id"], {"link_state": "dispatching",
                              "column_name": "in_progress", "dispatched_at": now})
    await d.recover_actions()
    assert _results(store)[rec] == aj.COMPLETED
    assert d.events == []


@pytest.mark.asyncio
async def test_a_crash_between_the_spawn_result_and_the_record_intent(daemon):
    d, store = daemon
    card = _make(store)
    _a, _s, _r, now = _start(store, card["id"], spawn="spawned", record=None)
    await d.recover_actions()
    got = store.get(card["id"])
    assert got["link_state"] == "dispatching"
    assert got["dispatched_at"] == pytest.approx(now)
    assert store.journal_open() == []


@pytest.mark.asyncio
async def test_attempts_above_the_cap_are_interrupted_with_the_note(daemon):
    d, store = daemon
    card = _make(store)
    _a, _s, rec, _n = _start(store, card["id"], spawn="spawned", record="open")
    store._conn.execute("UPDATE action_intents SET attempt=? WHERE id=?",
                        (aj.MAX_SAFE_REPLAYS + 1, rec))
    store._conn.commit()
    await d.recover_actions()
    got = store.get(card["id"])
    assert got["link_state"] == ""
    assert got["column_name"] == "backlog"
    assert got["dispatch_error"] == aj.RESTART_DURING_START_NOTE
    assert _results(store)[rec] == aj.INTERRUPTED


@pytest.mark.asyncio
async def test_a_changed_declaration_is_never_replayed(daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    _a, _s, rec, _n = _start(store, card["id"], spawn="spawned", record="open")
    monkeypatch.setitem(aj.ACTIONS["card_start"], "record", aj.NEVER)
    await d.recover_actions()
    assert store.get(card["id"])["link_state"] == ""
    assert _results(store)[rec] == aj.INTERRUPTED


@pytest.mark.asyncio
async def test_a_batch_head_is_not_replayed_without_its_batch_marks(daemon):
    d, store = daemon
    card = _make(store)
    _a, _s, rec, _n = _start(store, card["id"], spawn="spawned", record="open",
                             batch=True)
    await d.recover_actions()
    assert store.get(card["id"])["link_state"] == ""
    assert _results(store)[rec] == aj.INTERRUPTED


@pytest.mark.asyncio
async def test_a_refused_spawn_with_an_open_record_closes_it(daemon):
    d, store = daemon
    card = _make(store)
    _a, _s, rec, _n = _start(store, card["id"], spawn="refused", record="open")
    await d.recover_actions()
    assert _results(store)[rec] == aj.INTERRUPTED
    assert store.get(card["id"])["column_name"] == "backlog"


@pytest.mark.asyncio
async def test_recovery_never_moves_a_card_to_done(daemon):
    d, store = daemon
    for spawn, record in (("open", None), ("spawned", "open"),
                          ("spawned", None)):
        card = _make(store)
        _start(store, card["id"], spawn=spawn, record=record)
    await d.recover_actions()
    assert all(c["column_name"] != "done" for c in store.cards())


# --- worktree preparation ------------------------------------------------------

def _prepare(store, cid, path, *, add="open", record=None, start=True,
             queued=False):
    action = store.journal_begin("worktree_prepare", cid)
    a = store.journal_intent(action, "worktree_prepare", cid, "add", {
        "card_id": cid, "root": "/tmp", "path": path, "branch": "card/x",
        "start": start, "queued_replay": queued})
    if add != "open":
        store.journal_result(a, add)
    r = None
    if record is not None:
        r = store.journal_intent(action, "worktree_prepare", cid, "record", {
            "card_id": cid, "root": "/tmp", "path": path, "branch": "card/x"})
        if record != "open":
            store.journal_result(r, record)
    return action, a, r


@pytest.mark.asyncio
async def test_add_open_writes_the_note_and_leaves_the_folder(daemon, tmp_path,
                                                              monkeypatch):
    d, store = daemon
    card = _make(store)
    folder = tmp_path / "half-made"
    folder.mkdir()
    (folder / "file.txt").write_text("x")

    def no_git(*a, **k):
        raise AssertionError("recovery ran a git command")

    monkeypatch.setattr(subprocess, "run", no_git)
    _prepare(store, card["id"], str(folder))
    await d.recover_actions()
    got = store.get(card["id"])
    assert got["dispatch_error"] == aj.RESTART_DURING_PREPARE_NOTE
    assert got["column_name"] == "backlog"
    assert (folder / "file.txt").read_text() == "x"
    assert d.effects.spawn == 0


@pytest.mark.asyncio
async def test_a_queued_press_is_dequeued_by_the_prepare_note(daemon, tmp_path):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"queue_state": "queued", "queued_at": time.time()})
    _prepare(store, card["id"], str(tmp_path / "w"), queued=True)
    await d.recover_actions()
    assert store.get(card["id"])["queue_state"] == ""


@pytest.mark.asyncio
async def test_a_merge_s_folder_leaves_no_note(daemon, tmp_path):
    d, store = daemon
    card = _make(store)
    _prepare(store, card["id"], str(tmp_path / "w"), start=False)
    await d.recover_actions()
    assert store.get(card["id"])["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_record_open_with_the_folder_present_records_it(daemon, tmp_path):
    d, store = daemon
    card = _make(store)
    folder = tmp_path / "w"
    folder.mkdir()
    _a, _add, rec = _prepare(store, card["id"], str(folder), add="added",
                             record="open")
    await d.recover_actions()
    got = store.get(card["id"])
    assert got["worktree_path"] == str(folder)
    assert got["dispatch_error"] == aj.RESTART_DURING_PREPARE_NOTE
    assert _results(store)[rec] == aj.REPLAYED
    assert d.effects.spawn == 0


@pytest.mark.asyncio
async def test_record_open_with_the_folder_gone_is_interrupted(daemon, tmp_path):
    d, store = daemon
    card = _make(store)
    _a, _add, rec = _prepare(store, card["id"], str(tmp_path / "gone"),
                             add="added", record="open")
    await d.recover_actions()
    assert store.get(card["id"])["worktree_path"] in ("", None)
    assert _results(store)[rec] == aj.INTERRUPTED


@pytest.mark.asyncio
async def test_a_finished_folder_step_with_words_is_not_a_lost_press(daemon, tmp_path):
    d, store = daemon
    card = _make(store)
    _a, _add, rec = _prepare(store, card["id"], str(tmp_path / "w"),
                             add="added", record="skipped")
    await d.recover_actions()
    assert store.get(card["id"])["dispatch_error"] == ""


# --- stop, auto-compact, typed answer -------------------------------------------

def _single(store, kind, step, subject, payload=None, outcome=None, age=0.0):
    action = store.journal_begin(kind, subject)
    iid = store.journal_intent(action, kind, subject, step, payload or {})
    if outcome:
        store.journal_result(iid, outcome)
    if age:
        store._conn.execute("UPDATE action_intents SET created_at=? WHERE id=?",
                            (time.time() - age, iid))
        store._conn.commit()
    return iid


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["terminate", "kill"])
async def test_a_stop_open_is_interrupted_and_signals_nothing(daemon, step):
    d, store = daemon
    iid = _single(store, "stop_session", step, "sess-1", {"pid": 4242})
    await d.recover_actions()
    assert _results(store)[iid] == aj.INTERRUPTED
    assert (d.effects.terminate, d.effects.kill) == (0, 0)
    assert _kinds(d) == ["action_interrupted"]
    assert d.events[0][1]["detail"] == {"kind": "stop_session", "step": step}


@pytest.mark.asyncio
async def test_an_autocompact_within_the_settle_window_holds(daemon):
    d, store = daemon
    iid = _single(store, "autocompact", "type", "sess-1", age=30)
    await d.recover_actions()
    assert _results(store)[iid] == aj.INTERRUPTED
    policy = d.__dict__["_autocompact"]
    signal = {"rule": "ctx-full", "severity": "crit", "text": "x — y"}
    verdict = policy.consider("sess-1", [signal], 91.0, True, time.time())
    assert verdict == autocompact.HOLD
    assert d.effects.typed == 0


@pytest.mark.asyncio
async def test_a_sent_autocompact_within_the_window_is_remembered_too(daemon):
    d, store = daemon
    _single(store, "autocompact", "type", "sess-1", outcome="sent", age=10)
    await d.recover_actions()
    assert d.__dict__["_autocompact"].pending("sess-1")


@pytest.mark.asyncio
async def test_an_old_or_unlanded_autocompact_seeds_nothing(daemon):
    d, store = daemon
    _single(store, "autocompact", "type", "old", age=autocompact.SETTLE_SECONDS + 60)
    _single(store, "autocompact", "type", "nope", outcome="not_landed", age=5)
    await d.recover_actions()
    policy = d.__dict__.get("_autocompact")
    assert policy is None or (not policy.pending("old")
                              and not policy.pending("nope"))


@pytest.mark.asyncio
async def test_an_answer_burst_open_types_nothing(daemon):
    d, store = daemon
    iid = _single(store, "answer_burst", "type", "sess-1", {"questions": 2})
    await d.recover_actions()
    assert _results(store)[iid] == aj.INTERRUPTED
    assert d.effects.typed == 0
    assert not d._answering
    assert _kinds(d) == ["action_interrupted"]


@pytest.mark.asyncio
async def test_stop_session_recorded_journals_around_the_signal(daemon):
    d, store = daemon
    d._agent_records["sess-1"] = SimpleNamespace(pid=4242)
    d._session_provider = lambda sid: "claude"
    ok, detail = await d.stop_session_recorded("sess-1")
    assert (ok, detail) == (True, "stopping")
    assert d.effects.terminate == 1
    row = store._conn.execute(
        "SELECT i.step, r.outcome FROM action_intents i JOIN action_results r"
        " ON r.intent_id=i.id").fetchall()
    assert [(r["step"], r["outcome"]) for r in row] == [("terminate", "signalled")]


@pytest.mark.asyncio
async def test_stop_session_recorded_says_refused_for_a_stranger(daemon,
                                                                 monkeypatch):
    d, store = daemon
    d._agent_records["sess-1"] = SimpleNamespace(pid=4242)
    d._session_provider = lambda sid: "claude"
    monkeypatch.setattr(daemon_mod.pid_resolver, "looks_like_session",
                        lambda *a, **k: False)
    ok, _detail = await d.stop_session_recorded("sess-1")
    assert ok is False
    assert d.effects.terminate == 0
    outcome = store._conn.execute("SELECT outcome FROM action_results").fetchone()
    assert outcome["outcome"] == "refused"


@pytest.mark.asyncio
async def test_stop_session_recorded_with_no_pid_writes_no_row(daemon):
    d, store = daemon
    ok, _detail = await d.stop_session_recorded("sess-nobody")
    assert store.journal_counts()["intents"] == 0


# --- the whole -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_twice_changes_nothing_the_second_time(daemon, tmp_path,
                                                              monkeypatch):
    d, store = daemon
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    a, b, c = _make(store), _make(store), _make(store)
    _start(store, a["id"])
    _start(store, b["id"], spawn="spawned", record="open")
    _start(store, c["id"], spawn="spawned", record=None)
    _single(store, "stop_session", "terminate", "s1")
    _single(store, "autocompact", "type", "s2", age=10)
    _single(store, "answer_burst", "type", "s3")
    _prepare(store, _make(store)["id"], str(tmp_path / "w"))
    first = await d.recover_actions()
    assert first["open"] == 7
    assert store.journal_open() == []
    cards = store.cards()
    counts = store.journal_counts()
    events = len(d.events)
    second = await d.recover_actions()
    assert (second["open"], second["replayed"], second["interrupted"]) == (0, 0, 0)
    assert store.cards() == cards
    assert store.journal_counts() == counts
    assert len(d.events) == events


@pytest.mark.asyncio
async def test_a_shut_board_recovers_to_an_empty_report():
    d = BobDaemon()
    d._board = None
    report = await d.recover_actions()
    assert (report["open"], report["replayed"], report["interrupted"]) == (0, 0, 0)


@pytest.mark.asyncio
async def test_an_unknown_kind_is_interrupted_never_replayed(daemon):
    d, store = daemon
    action = store.journal_begin("teleport", "x")
    iid = store.journal_intent(action, "teleport", "x", "beam", {})
    await d.recover_actions()
    assert _results(store)[iid] == aj.INTERRUPTED


def test_the_summary_line_is_logged(daemon, caplog):
    import asyncio
    d, _store = daemon
    with caplog.at_level("INFO", logger="dark-army.journal"):
        asyncio.run(d.recover_actions())
    assert any(r.getMessage().startswith("action journal: 0 open, 0 replayed, "
                                         "0 interrupted, 0 mismatches")
               for r in caplog.records)


# --- the self-check ------------------------------------------------------------

def _card_dict(**kw):
    card = {"id": "c1", "column_name": "in_progress", "link_state": "",
            "dispatched_at": None, "worktree_path": ""}
    card.update(kw)
    return card


def _record_action(cid, now, outcome="completed", at=None):
    return {"action_id": "a-" + cid, "kind": "card_start", "subject": cid,
            "steps": [{"step": "record", "payload": {"now": now},
                       "result": {"outcome": outcome,
                                  "created_at": at if at is not None else now}}]}


def test_self_check_names_a_dispatching_card_with_no_receipt():
    card = _card_dict(link_state="dispatching", dispatched_at=100.0)
    report = daemon_recovery.journal_self_check([card], [], [], now=200.0)
    assert report["dispatching_without_receipt"] == ["c1"]
    ok = daemon_recovery.journal_self_check(
        [card], [], [_record_action("c1", 102.0)], now=200.0)
    assert ok["dispatching_without_receipt"] == []
    far = daemon_recovery.journal_self_check(
        [card], [], [_record_action("c1", 130.0)], now=200.0)
    assert far["dispatching_without_receipt"] == ["c1"]


def test_self_check_names_a_finished_record_whose_card_has_no_link():
    now = 1000.0
    card = _card_dict(link_state="")
    report = daemon_recovery.journal_self_check(
        [card], [], [_record_action("c1", now - 10)], now=now)
    assert report["record_without_card_link"] == ["c1"]
    for fine in (_card_dict(link_state="live"), _card_dict(column_name="done")):
        assert daemon_recovery.journal_self_check(
            [fine], [], [_record_action("c1", now - 10)], now=now
        )["record_without_card_link"] == []
    old = daemon_recovery.journal_self_check(
        [card], [], [_record_action("c1", now - 500)], now=now)
    assert old["record_without_card_link"] == []


def test_self_check_names_a_worktree_that_is_not_a_folder(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    cards = [_card_dict(id="gone", worktree_path=str(tmp_path / "nope")),
             _card_dict(id="fine", worktree_path=str(real)),
             _card_dict(id="done", column_name="done",
                        worktree_path=str(tmp_path / "nope"))]
    report = daemon_recovery.journal_self_check(cards, [])
    assert report["worktree_recorded_but_absent"] == ["gone"]


def test_self_check_carries_ids_only_and_open_after_is_listed():
    report = daemon_recovery.journal_self_check(
        [], [{"action_id": "a1", "payload": {"path": "/secret"}}])
    assert report["open_after"] == ["a1"]
    assert "/secret" not in repr(report)


@pytest.mark.asyncio
async def test_an_upgraded_dispatching_card_is_counted_and_never_touched(daemon):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"link_state": "dispatching",
                              "column_name": "in_progress",
                              "dispatched_at": time.time() - 10})
    before = store.get(card["id"])
    report = await d.recover_actions()
    assert report["mismatches"]["dispatching_without_receipt"] == [card["id"]]
    assert store.get(card["id"]) == before


def test_recovery_module_never_launches_or_finishes_a_card():
    text = open(daemon_recovery.__file__, encoding="utf-8").read()
    for banned in ("dispatch.spawn", "spawn_local", "dispatch_card(",
                   "start_cards(", '"column_name": "done"'):
        assert banned not in text


@pytest.mark.asyncio
async def test_the_diary_line_without_a_nickname_does_not_say_a_session_twice(daemon):
    from dark_army_daemon import event_log
    d, store = daemon
    _single(store, "autocompact", "type", "sess-1", age=30)
    await d.recover_actions()
    kind, fields = d.events[0]
    text = event_log.sentence(kind, **fields)
    assert text == "Asking a session to compact was interrupted by a Dark Army restart"
    d._nicknames_shown["sess-2"] = "Vex"
    _single(store, "answer_burst", "type", "sess-2")
    await d.recover_actions()
    assert event_log.sentence(*[d.events[-1][0]], **d.events[-1][1]) \
        == "Vex: typing an answer interrupted by a Dark Army restart"


@pytest.mark.asyncio
async def test_a_spawner_that_raises_resolves_its_intent(daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def boom(*a, **k):
        raise RuntimeError("window host blew up")

    monkeypatch.setattr(dispatch, "spawn", boom)
    with pytest.raises(RuntimeError):
        await d.dispatch_card(card["id"], allow_unplanned=True)
    assert store.journal_open() == []
    assert list(_results(store).values()) == ["refused"]
    await d.recover_actions()
    assert store.get(card["id"])["dispatch_error"] == ""
