"""Closed and dead agents leave the live fleet.

A Codex journal with no process, a terminal Dark Army closed, and a live+finished
duplicate must not keep a row in running/waiting. Seams: BobDaemon() as
test_finished.py does, Codex records as test_agents_poll.py does.
"""

import time

import pytest

from dark_army_daemon.agents_poll import AgentRecord
from dark_army_daemon.daemon import BobDaemon, FINISHED_RETENTION_SECONDS
from dark_army_daemon import codex_rollouts

from tests.test_agents_poll import _attach_codex, _codex_root, _CodexProc


def _live_ids(snap):
    return (
        {row["session_id"] for row in snap.get("running") or []}
        | {row["session_id"] for row in snap.get("waiting") or []}
        | {row["session_id"] for row in snap.get("sleeping") or []}
    )


def _finished_ids(snap):
    return {row["session_id"] for row in snap.get("finished") or []}


def test_a_dead_codex_root_leaves_live_buckets_and_tombstones(monkeypatch):
    rec = _codex_root("dead")
    rec.process_seen = False
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [rec])
    d._refresh_codex_records()
    snap = d.detailed_snapshot()
    assert rec.session_id not in _live_ids(snap)
    assert rec.session_id in _finished_ids(snap)
    assert d._finished[rec.session_id]["end_reason"] == "no process"


def test_a_live_codex_root_stays_in_waiting(monkeypatch):
    rec = _codex_root("live")
    rec.process_seen = True
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [rec])
    d._refresh_codex_records()
    snap = d.detailed_snapshot()
    assert rec.session_id in {row["session_id"] for row in snap.get("waiting") or []}
    assert rec.session_id not in _finished_ids(snap)


def test_process_seen_none_is_never_retired(monkeypatch):
    cli = _codex_root("maybe")
    cli.process_seen = None
    app = _codex_root("ide")
    app.originator = "codex-app"
    app.source_kind = "app"
    app.process_seen = None
    d = BobDaemon()
    d._codex_records = {cli.session_id: cli, app.session_id: app}
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [cli, app])
    d._refresh_codex_records()
    assert cli.session_id in d._codex_records
    assert app.session_id in d._codex_records
    assert cli.session_id not in d._finished
    assert app.session_id not in d._finished


def test_a_retired_codex_sid_returns_when_the_process_does(monkeypatch):
    dead = _codex_root("back")
    dead.process_seen = False
    d = BobDaemon()
    d._codex_records[dead.session_id] = dead
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [dead])
    d._refresh_codex_records()
    assert d._finished[dead.session_id]["end_reason"] == "no process"

    alive = _codex_root("back")
    alive.process_seen = True
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [alive])
    d._refresh_codex_records()
    assert alive.session_id in d._codex_records
    assert alive.session_id not in d._finished
    snap = d.detailed_snapshot()
    assert alive.session_id in _live_ids(snap)
    assert alive.session_id not in _finished_ids(snap)


def test_refreshing_a_dead_codex_twice_does_not_restart_finished_at(monkeypatch):
    rec = _codex_root("once")
    rec.process_seen = False
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [rec])
    d._refresh_codex_records()
    first = d._finished[rec.session_id]["finished_at"]
    d._refresh_codex_records()
    assert d._finished[rec.session_id]["finished_at"] == first
    assert d._finished[rec.session_id]["end_reason"] == "no process"


def test_a_closed_id_is_dropped_from_agent_records():
    d = BobDaemon()
    d._note_closed("s1")
    d._on_agent_records([
        AgentRecord("s1", kind="interactive", activity="busy", name="n"),
        AgentRecord("s2", kind="interactive", activity="busy", name="m"),
    ])
    assert "s1" not in d._agent_records
    assert "s2" in d._agent_records


def test_a_hook_event_clears_the_closed_note():
    d = BobDaemon()
    d._note_closed("s1")
    assert d._closed_by_bob("s1")
    d._update_session_state("session_start", "SessionStart", "s1")
    assert not d._closed_by_bob("s1")
    assert "s1" in d._session_states


def test_closed_ids_are_pruned_past_retention():
    d = BobDaemon()
    d._note_closed("s1")
    stamped, pid, created = d._closed_ids["s1"]
    d._closed_ids["s1"] = (
        stamped - FINISHED_RETENTION_SECONDS - 1, pid, created)
    d._prune_finished()
    assert "s1" not in d._closed_ids


def test_a_codex_sid_with_a_record_and_a_tombstone_is_one_row(monkeypatch):
    rec = _codex_root("dup")
    rec.process_seen = True
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    d._record_finished(rec.session_id, {
        "project": rec.project, "cwd": rec.cwd, "pid": rec.pid,
        "state": "idle", "provider": "codex", "last_event": rec.last_event,
    }, "no process")
    monkeypatch.setattr(d, "_refresh_codex_records", lambda: None)
    monkeypatch.setattr(d, "_refresh_grok_records", lambda: None)
    stubs = d._collect_agent_stubs()
    ids = [row["session_id"] for row in stubs]
    assert ids.count(rec.session_id) == 1
    assert len(ids) == len(set(ids))
    match = [row for row in stubs if row["session_id"] == rec.session_id][0]
    assert match["_category"] != "finished"


@pytest.mark.asyncio
async def test_closing_a_roster_only_claude_session_leaves_a_closed_tombstone(
    monkeypatch,
):
    from dark_army_daemon import vscode_reveal as vr

    async def _close(pid, tty):
        return {"matched": True, "closed": True}

    monkeypatch.setattr(vr, "close_terminal", _close)
    d = BobDaemon()
    d._agent_records["s1"] = AgentRecord(
        "s1", kind="interactive", activity="idle", pid=4242, cwd="/code/bob",
    )
    ok, detail = await d._close_session_terminal("s1")
    assert (ok, detail) == (True, "")
    assert "s1" not in d._agent_records
    assert d._closed_by_bob("s1")
    assert "s1" in d._finished
    assert d._finished["s1"]["end_reason"] == "closed"
    monkeypatch.setattr(d, "_refresh_codex_records", lambda: None)
    monkeypatch.setattr(d, "_refresh_grok_records", lambda: None)
    stubs = d._collect_agent_stubs()
    live = [row for row in stubs if row["session_id"] == "s1"
            and row.get("_category") != "finished"]
    assert live == []
    assert any(row["session_id"] == "s1" and row.get("_category") == "finished"
               for row in stubs)


def test_stop_does_not_note_closed_and_a_resumed_process_is_live_again(monkeypatch):
    rec = _codex_root("stop", revision=(10, 20))
    rec.process_seen = True
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [rec])
    d._settle_codex_stop(rec.session_id, "stopped")
    assert not d._closed_by_bob(rec.session_id)

    resumed = _codex_root("stop", revision=(10, 21))
    resumed.process_seen = True
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [resumed])
    d._refresh_codex_records()
    assert resumed.session_id in d._codex_records
    assert not d._closed_by_bob(resumed.session_id)
    snap = d.detailed_snapshot()
    assert resumed.session_id in _live_ids(snap)


def _close_codex_with_proc(monkeypatch, thread_id, proc, revision=(10, 20)):
    rec = _codex_root(thread_id, revision=revision)
    _attach_codex(rec, proc)
    rec.process_seen = True
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [rec])
    d._settle_codex_stop(rec.session_id, "closed")
    assert d._closed_by_bob(rec.session_id)
    return d, rec


def test_a_closed_codex_sid_is_not_readmitted_on_the_dying_process(monkeypatch):
    proc = _CodexProc(pid=7001, thread_id="dying", created=100.0)
    d, rec = _close_codex_with_proc(monkeypatch, "dying", proc)

    dying = _codex_root("dying", revision=(10, 21))
    _attach_codex(dying, proc)
    dying.process_seen = True
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [dying])
    d._refresh_codex_records()
    assert dying.session_id not in d._codex_records
    assert d._closed_by_bob(dying.session_id)
    snap = d.detailed_snapshot()
    assert dying.session_id not in _live_ids(snap)


def test_a_closed_codex_sid_is_not_readmitted_on_a_sibling_cwd_tui(monkeypatch):
    proc = _CodexProc(pid=7001, thread_id="sib", created=100.0)
    d, rec = _close_codex_with_proc(monkeypatch, "sib", proc)

    sibling = _codex_root("sib", revision=(10, 21))
    sibling.process_seen = True
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [sibling])
    d._refresh_codex_records()
    assert sibling.session_id not in d._codex_records
    assert d._closed_by_bob(sibling.session_id)
    snap = d.detailed_snapshot()
    assert sibling.session_id not in _live_ids(snap)
    assert rec.session_id not in _live_ids(snap)


def test_a_closed_codex_sid_returns_on_a_new_pid(monkeypatch):
    proc = _CodexProc(pid=7001, thread_id="back", created=100.0)
    d, rec = _close_codex_with_proc(monkeypatch, "back", proc)

    new_proc = _CodexProc(pid=8001, thread_id="back", created=200.0)
    back = _codex_root("back", revision=(10, 22))
    _attach_codex(back, new_proc)
    back.process_seen = True
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [back])
    d._refresh_codex_records()
    assert back.session_id in d._codex_records
    assert not d._closed_by_bob(back.session_id)
    snap = d.detailed_snapshot()
    assert back.session_id in _live_ids(snap)


def test_a_closed_codex_sid_returns_on_a_new_explicit_resume(monkeypatch):
    rec = _codex_root("resume", revision=(10, 20))
    rec.process_identity = codex_rollouts.CodexProcessIdentity(
        pid=7001, executable="/opt/codex", cwd="/code/bob",
        create_time=100.0, argv=("codex",), match_kind="unique_cwd",
    )
    rec.pid = 7001
    rec.process_seen = True
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [rec])
    d._settle_codex_stop(rec.session_id, "closed")
    assert d._closed_by_bob(rec.session_id)

    back = _codex_root("resume", revision=(10, 22))
    back.process_identity = codex_rollouts.CodexProcessIdentity(
        pid=8001, executable="/opt/codex", cwd="/code/bob",
        create_time=200.0, argv=("codex", "resume", "resume"),
        match_kind="explicit_resume",
    )
    back.pid = 8001
    back.process_seen = True
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [back])
    d._refresh_codex_records()
    assert back.session_id in d._codex_records
    assert not d._closed_by_bob(back.session_id)
    snap = d.detailed_snapshot()
    assert back.session_id in _live_ids(snap)


def test_collect_emits_no_live_stub_for_a_closed_codex_sid_still_on_the_roster(
    monkeypatch,
):
    rec = _codex_root("stay")
    rec.process_seen = True
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    d._note_closed(rec.session_id, pid=7001, create_time=100.0)
    d._record_finished(rec.session_id, {
        "project": rec.project, "cwd": rec.cwd, "pid": rec.pid,
        "state": "idle", "provider": "codex", "last_event": rec.last_event,
    }, "closed")
    monkeypatch.setattr(d, "_refresh_codex_records", lambda: None)
    monkeypatch.setattr(d, "_refresh_grok_records", lambda: None)
    stubs = d._collect_agent_stubs()
    live = [row for row in stubs if row["session_id"] == rec.session_id
            and row.get("_category") != "finished"]
    assert live == []
    assert any(row["session_id"] == rec.session_id
               and row.get("_category") == "finished"
               for row in stubs)


@pytest.mark.asyncio
async def test_confirm_codex_stop_settles_when_roster_emptied_mid_wait(monkeypatch):
    from dark_army_daemon import daemon as daemon_mod
    from tests.test_agents_poll import _CodexProc, _attach_codex, _no_sleeping

    _no_sleeping(monkeypatch)
    proc = _CodexProc(thread_id="race")
    record = _attach_codex(_codex_root("race", revision=(11, 22)), proc)
    identity = record.process_identity
    d = BobDaemon()
    d._codex_records = {}
    monkeypatch.setattr(daemon_mod.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [record])

    def kill_and_go():
        proc.killed = True
        monkeypatch.setattr(
            codex_rollouts.psutil, "process_iter", lambda attrs: [])

        def _gone(pid):
            raise daemon_mod.psutil.NoSuchProcess(pid)

        monkeypatch.setattr(daemon_mod.psutil, "Process", _gone)

    proc.kill = kill_and_go
    await d._confirm_codex_stop(record.session_id, identity, reason="closed")
    assert proc.killed is True
    assert record.session_id not in d._codex_records
    assert record.session_id in d._hidden_codex
    assert d._finished[record.session_id]["end_reason"] == "closed"
    assert d._closed_by_bob(record.session_id)


def test_a_dead_codex_root_keeps_stats_on_the_finished_stub(monkeypatch):
    rec = _codex_root("costly")
    rec.process_seen = False
    rec.stats.input_tokens = 120
    rec.stats.output_tokens = 20
    rec.metrics = {"ctx_used_pct": 50, "five_hour_pct": 42}
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [rec])
    d._refresh_codex_records()
    snap = d.detailed_snapshot()
    finished = [row for row in snap.get("finished") or []
                if row["session_id"] == rec.session_id]
    assert finished
    assert finished[0]["stats"]["input_tokens"] == 120
    assert finished[0]["stats"]["output_tokens"] == 20
    assert finished[0]["metrics"]["ctx_used_pct"] == 50
    assert finished[0]["metrics"]["five_hour_pct"] == 42


def test_a_second_death_after_a_proven_resume_gets_its_own_finished_row(monkeypatch):
    """A kept `closed` tombstone would mask the run that came back and died.

    The retirement branch declines to write while any tombstone stands, so
    *Recently finished* would show the first run's cost on the first run's
    clock — and expire on the first run's retention while the second was still
    worth reading."""
    proc = _CodexProc(pid=7001, thread_id="twice", created=100.0)
    d, rec = _close_codex_with_proc(monkeypatch, "twice", proc)
    assert d._finished[rec.session_id]["end_reason"] == "closed"
    first_at = d._finished[rec.session_id]["finished_at"]

    back = _codex_root("twice", revision=(10, 22))
    _attach_codex(back, _CodexProc(pid=8001, thread_id="twice", created=200.0))
    back.process_seen = True
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [back])
    d._refresh_codex_records()
    assert back.session_id in d._codex_records
    assert back.session_id not in d._finished      # the old tombstone is gone

    dead = _codex_root("twice", revision=(10, 23))
    _attach_codex(dead, _CodexProc(pid=8001, thread_id="twice", created=200.0))
    dead.process_seen = False
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [dead])
    d._refresh_codex_records()
    tomb = d._finished[dead.session_id]
    assert tomb["end_reason"] == "no process"
    assert tomb["finished_at"] >= first_at
    snap = d.detailed_snapshot()
    assert dead.session_id in _finished_ids(snap)
    assert dead.session_id not in _live_ids(snap)


def test_an_unreadable_process_table_does_not_readmit_a_retired_codex_root(
        monkeypatch):
    """One failed psutil tick leaves `process_seen` None for every record.

    Failing open means never *retiring* on no evidence; un-retiring on none
    would put a dead root back under *Needs you* and drop its finished row."""
    dead = _codex_root("blind")
    dead.process_seen = False
    d = BobDaemon()
    d._codex_records[dead.session_id] = dead
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [dead])
    d._refresh_codex_records()
    assert d._finished[dead.session_id]["end_reason"] == "no process"
    retired_at = d._finished[dead.session_id]["finished_at"]

    blind = _codex_root("blind", revision=(10, 21))
    blind.process_seen = None
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [blind])
    d._refresh_codex_records()
    assert blind.session_id not in d._codex_records
    assert d._finished[blind.session_id]["finished_at"] == retired_at
    snap = d.detailed_snapshot()
    assert blind.session_id not in _live_ids(snap)
    assert blind.session_id in _finished_ids(snap)


@pytest.mark.parametrize("working", [False, True])
@pytest.mark.parametrize("ownership", ["resume", "holder"])
def test_external_codex_close_reconciles_real_journals(
        tmp_path, monkeypatch, working, ownership):
    from datetime import datetime, timezone
    from dark_army_daemon.board import BoardStore
    from tests.test_codex_rollouts import _FakeProcess, row, write_rollout

    stamp = datetime.now(timezone.utc).isoformat()
    paths = {}
    for thread in ("closed", "sibling"):
        path = tmp_path / "2026/09/05" / f"rollout-{thread}.jsonl"
        rows = [
            row("session_meta", {"id": thread, "cwd": str(tmp_path),
                "originator": "codex-tui", "source": "cli", "thread_source": "user"}, stamp),
            row("event_msg", {"type": "task_started", "turn_id": "turn"}, stamp),
            row("response_item", {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Preserved reply"}]}, stamp),
            row("event_msg", {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 120, "output_tokens": 20},
                "last_token_usage": {"total_tokens": 500}, "model_context_window": 1000}}, stamp),
        ]
        if not working or thread == "sibling":
            rows.append(row("event_msg", {"type": "task_complete", "turn_id": "turn"}, stamp))
        write_rollout(path, rows)
        paths[thread] = path
    def owner(pid, thread):
        return _FakeProcess(pid, cwd=str(tmp_path), open_files=(paths[thread],),
                            cmdline=("codex", "resume", thread)
                            if ownership == "resume" else ("codex",))
    processes = [owner(77, "closed"), owner(78, "sibling")]
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: processes)
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(d, "_refresh_grok_records", lambda: None)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        card, detail = store.create({"title": "Keep column", "prompt": "go",
            "project": tmp_path.name, "root": str(tmp_path), "tool": "codex",
            "column_name": "in_progress", "session_id": "codex:closed", "link_state": "live"})
        assert card, detail
        card, detail = store.update(card["id"], {"session_id": "codex:closed", "link_state": "live"})
        assert card, detail
        def refresh():
            d._roster_refreshed_at = None
            # Each refresh stands for one past the shared process reading's
            # TTL: inside it the three scans of a push reuse one walk.
            codex_rollouts.invalidate_process_snapshot()
            return d.detailed_snapshot()
        assert _live_ids(refresh()) == {"codex:closed", "codex:sibling"}
        original = d._codex_records["codex:closed"].revision
        processes = None
        assert "codex:closed" in _live_ids(refresh())
        processes = [owner(78, "sibling")]
        snap = refresh()
        assert _live_ids(snap) == {"codex:sibling"}
        assert _finished_ids(snap) == {"codex:closed"}
        finished = snap["finished"][0]
        assert finished["stats"]["input_tokens"] == 120
        assert finished["stats"]["output_tokens"] == 20
        assert finished["metrics"]["ctx_used_pct"] == 50
        assert finished["last_text"] == "Preserved reply"
        assert "codex:closed" not in d._claiming_session_ids()
        assert d._activity_counts()["attention"] == 1
        assert d._activity_counts()["working"] == 0
        first = d._finished["codex:closed"]
        refresh()
        assert d._finished["codex:closed"]["finished_at"] == first["finished_at"]
        processes = None
        assert _live_ids(refresh()) == {"codex:sibling"}
        d._reconcile_board(snap)
        d._board_missing_since[card["id"]] -= d.BOARD_SESSION_GRACE + 1
        d._reconcile_board(snap)
        ended = store.get(card["id"])
        assert ended["link_state"] == "ended"
        assert ended["column_name"] == "in_progress"
        processes = [owner(79, "closed"), owner(78, "sibling")]
        snap = refresh()
        assert _live_ids(snap) == {"codex:closed", "codex:sibling"}
        assert not _finished_ids(snap)
        assert d._codex_records["codex:closed"].revision == original
        if ownership == "holder":
            entry = next(r for bucket in ("running", "waiting") for r in snap[bucket]
                         if r["session_id"] == "codex:closed")
            assert not any(entry.get(key) for key in ("can_jump", "can_stop", "can_close", "can_type"))
        processes = [owner(78, "sibling")]
        assert _finished_ids(refresh()) == {"codex:closed"}
        assert d._finished["codex:closed"] is not first
        assert "codex:closed" not in d._hidden_codex
    finally:
        store.close()


# --- A Codex root whose journal aged out of the bounded scan ---------------
#
# `codex_rollouts.load_recent()` drops a journal untouched for
# LIVE_GRACE_SECONDS, so the commonest Codex ending — turn over, tab open,
# journal quiet — reaches `_refresh_codex_records` as an *absence* rather than
# as a record with `process_seen False`. Before `_retire_quiet_codex_roots`
# that absence stamped nothing at all.


def _quiet_journal(tmp_path, thread_id, age_seconds, text="## Work done\n\nAll of it."):
    import os
    from dark_army_daemon import session_stats as ss

    path = tmp_path / f"rollout-{thread_id}.jsonl"
    path.write_text("{}\n")
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    rec = _codex_root(thread_id, path=path)
    rec.stats = ss.SessionStats()
    rec.stats.last_text = text
    return rec


def test_a_quiet_codex_journal_is_tombstoned_with_its_last_words(monkeypatch, tmp_path):
    rec = _quiet_journal(tmp_path, "quiet",
                         codex_rollouts.LIVE_GRACE_SECONDS + 60)
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    # The scan no longer returns it: that is exactly what ageing out means.
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [])
    d._refresh_codex_records()
    assert rec.session_id not in d._codex_records
    assert d._finished[rec.session_id]["end_reason"] == "evicted"
    snap = d.detailed_snapshot()
    assert rec.session_id in _finished_ids(snap)
    row = next(r for r in snap["finished"] if r["session_id"] == rec.session_id)
    assert "## Work done" in (row.get("last_text") or "")


def test_a_journal_still_inside_the_grace_is_never_retired(monkeypatch, tmp_path):
    """An absent id is not evidence: only the journal's own clock retires it."""
    rec = _quiet_journal(tmp_path, "fresh", 5.0)
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [])
    d._refresh_codex_records()
    assert rec.session_id not in d._finished


def test_an_unreadable_journal_retires_nothing(monkeypatch, tmp_path):
    rec = _codex_root("gone", path=tmp_path / "never-written.jsonl")
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [])
    d._refresh_codex_records()
    assert rec.session_id not in d._finished


def test_a_hidden_codex_root_is_not_resurrected_as_a_tombstone(monkeypatch, tmp_path):
    rec = _quiet_journal(tmp_path, "hidden",
                         codex_rollouts.LIVE_GRACE_SECONDS + 60)
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    d._hidden_codex[rec.session_id] = (rec.path, rec.revision)
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [])
    d._refresh_codex_records()
    assert rec.session_id not in d._finished


def test_a_quiet_root_that_writes_again_drops_its_eviction_tombstone(monkeypatch, tmp_path):
    rec = _quiet_journal(tmp_path, "back",
                         codex_rollouts.LIVE_GRACE_SECONDS + 60)
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [])
    d._refresh_codex_records()
    assert d._finished[rec.session_id]["end_reason"] == "evicted"
    # The thread writes again; the scan returns it with no process evidence.
    rec.process_seen = None
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [rec])
    d._refresh_codex_records()
    assert rec.session_id in d._codex_records
    assert rec.session_id not in d._finished


def test_a_quiet_codex_root_writes_one_session_end_to_the_diary(monkeypatch, tmp_path):
    rec = _quiet_journal(tmp_path, "diary",
                         codex_rollouts.LIVE_GRACE_SECONDS + 60)
    d = BobDaemon()
    d._codex_records[rec.session_id] = rec
    d._logged_starts = {rec.session_id}
    written = []
    monkeypatch.setattr(d, "_log_session_event",
                        lambda sid, kind, **kw: written.append((sid, kind)))
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [])
    d._refresh_codex_records()
    assert written == [(rec.session_id, "session_end")]
