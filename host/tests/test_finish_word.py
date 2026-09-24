"""The finished list's word: "done" only where a report says so.

Two sessions cut off mid-build when their editor windows went away
(SessionEnd `other`, hook state still `working`) read "done" on the phone's
FINISHED list beside cards nobody had accepted (21 Sep 2026). The daemon
now composes the word (`session_stats.finish_word`) on finished rows and
both clients draw it verbatim, falling back to "done" without the key.
"""
from pathlib import Path

from dark_army_daemon import session_stats as ss

ROOT = Path(__file__).resolve().parents[2]


def test_a_report_is_done_whatever_else_happened():
    assert ss.finish_word("working", "ended", "## Work done\n...") == "done"
    assert ss.finish_word("idle", "evicted", "report") == "done"


def test_cut_off_mid_work_is_not_done():
    assert ss.finish_word("working", "ended", "") == "cut"
    assert ss.finish_word("thinking", "closed", None) == "cut"
    assert ss.finish_word("working", "closed", "") == "cut"


def test_lost_sight_of_wins_over_a_mid_turn_state():
    """A row evicted for silence mid-build still has its process; "cut"
    would say the terminal went away when nothing did."""
    assert ss.finish_word("working", "evicted", "") == "lost"
    assert ss.finish_word("thinking", "no process", None) == "lost"


def test_lost_sight_of_and_a_plain_end():
    assert ss.finish_word("idle", "evicted", "") == "lost"
    assert ss.finish_word("idle", "no process", "") == "lost"
    assert ss.finish_word("idle", "ended", "") == "end"
    assert ss.finish_word(None, None, None) == "end"
    assert ss.finish_word("idle", "cleared", "  ") == "end"


def test_every_word_fits_the_state_column():
    for word in ss.FINISH_WORDS:
        assert 1 <= len(word) <= 4


def test_the_daemon_publishes_it_on_finished_rows_only():
    import time

    from dark_army_daemon.daemon import BobDaemon

    d = BobDaemon()
    now = time.time()
    for sid, end_reason, state in (("t-end", "ended", "idle"),
                                   ("t-lost", "evicted", "working")):
        d._finished[sid] = {
            "finished_at": now - 60.0,
            "finished_mono": time.monotonic() - 60.0,
            "end_reason": end_reason,
            "stub": {
                "session_id": sid, "project": "", "cwd": "", "state": state,
                "subagents": 0, "subagent_ids": [], "pid": None,
                "current_tool": "", "kind": "interactive",
                "agent_activity": "", "cli_name": "", "provider": "claude",
            },
        }
    snap = d.detailed_snapshot()
    finished = {row["session_id"]: row for row in snap["finished"]}
    assert finished["t-end"]["finish_word"] == "end"
    assert finished["t-lost"]["finish_word"] == "lost"
    for bucket in ("running", "sleeping", "waiting", "abandoned"):
        assert all("finish_word" not in row for row in snap.get(bucket) or [])


def test_both_clients_decode_and_draw_it_the_same_way():
    for models, table in (
        (ROOT / "ios" / "BobPhone" / "Models.swift",
         ROOT / "ios" / "BobPhone" / "ProcessTable.swift"),
        (ROOT / "panel" / "Sources" / "BobPanel" / "Models.swift",
         ROOT / "panel" / "Sources" / "BobPanel" / "ProcessTable.swift"),
    ):
        m = models.read_text()
        assert 'case finishWord = "finish_word"' in m, models
        assert 'finishWord = c.value(.finishWord, "")' in m, models
        t = table.read_text()
        assert ('case .finished: return agent.finishWord.isEmpty '
                '? "done" : agent.finishWord') in t, table
