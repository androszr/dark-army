"""The `dark-army-next: rebuild` marker and the button it offers.

`session_stats` parses the marker beside the `## Work done` report and stamps
`rebuild_marker_at`; `BobDaemon._enrich_agent_stubs` publishes `rebuild_offered`
for an agent working in Dark Army's own checkout. The marker never raises a card:
`marker_at` and `finished_quietly` do not know it exists.
"""
import json

import pytest

from dark_army_daemon import enrollment
from dark_army_daemon import rebuild_state
from dark_army_daemon import session_stats as ss
from dark_army_daemon.daemon import BobDaemon

REPORT = ("Here is what I did.\n\n## Work done\n**Asked:** x\n**Changed:** y\n"
          "**Verified:** z\n**Unchecked:** Nothing - every check above ran.")
MARKER = "<!-- dark-army-next: rebuild -->"
T1 = "2026-10-03T10:00:00.000Z"
T2 = "2026-10-03T10:05:00.000Z"
T3 = "2026-10-03T10:10:00.000Z"
E1 = ss._epoch(ss._parse_ts(T1))
E2 = ss._epoch(ss._parse_ts(T2))


def _assistant(text, ts=T1):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "role": "assistant", "model": "claude-x",
        "content": [{"type": "text", "text": text}]}})


def _tool_only(ts=T2):
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "role": "assistant", "model": "claude-x", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}})


def _user(text, ts=T2):
    return json.dumps({"type": "user", "timestamp": ts, "message": {
        "role": "user", "content": text}})


def _stats(tmp_path, *records):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(records) + "\n")
    return ss.parse_transcript(str(p)) if hasattr(ss, "parse_transcript") \
        else ss.StatsCache().get(str(p))


# --- the parse ---------------------------------------------------------------

def test_a_report_with_the_marker_stamps_the_clock(tmp_path):
    stats = _stats(tmp_path, _assistant(REPORT + "\n" + MARKER))
    assert stats.rebuild_marker_at == E1
    assert "dark-army-next" not in stats.last_text
    assert "dark-army-next" not in stats.last_report


def test_the_marker_alone_is_noise(tmp_path):
    stats = _stats(tmp_path, _assistant("Which one do you want?\n" + MARKER))
    assert stats.rebuild_marker_at == 0.0


def test_an_unknown_value_is_ignored(tmp_path):
    stats = _stats(tmp_path, _assistant(
        REPORT + "\n<!-- dark-army-next: deploy -->"))
    assert stats.rebuild_marker_at == 0.0


def test_a_later_tool_only_turn_keeps_it(tmp_path):
    stats = _stats(tmp_path, _assistant(REPORT + "\n" + MARKER), _tool_only())
    assert stats.rebuild_marker_at == E1


def test_chatter_after_the_report_keeps_it(tmp_path):
    stats = _stats(tmp_path, _assistant(REPORT + "\n" + MARKER),
                   _assistant("Anything else?", T2))
    assert stats.rebuild_marker_at == E1


def test_the_next_person_prompt_clears_it(tmp_path):
    stats = _stats(tmp_path, _assistant(REPORT + "\n" + MARKER),
                   _user("now do y"))
    assert stats.rebuild_marker_at == 0.0


def test_a_marker_in_a_user_message_is_ignored(tmp_path):
    stats = _stats(tmp_path, _user(REPORT + "\n" + MARKER))
    assert stats.rebuild_marker_at == 0.0


def test_a_newer_report_without_the_marker_withdraws_the_offer(tmp_path):
    stats = _stats(tmp_path, _assistant(REPORT + "\n" + MARKER),
                   _assistant(REPORT, T2))
    assert stats.rebuild_marker_at == 0.0


def test_the_marker_never_moves_the_waiting_markers(tmp_path):
    stats = _stats(tmp_path, _assistant(REPORT + "\n" + MARKER))
    assert stats.marker_at == 0.0
    assert stats.last_summary == "" and stats.last_actions == []


def test_a_report_with_the_marker_is_still_a_quiet_finish(tmp_path):
    p = tmp_path / "q.jsonl"
    p.write_text(_assistant(REPORT + "\n" + MARKER) + "\n")
    assert ss.finished_quietly(str(p))


# --- the offer ---------------------------------------------------------------

ROOT = "/Users/me/dark-army"


def _daemon(monkeypatch, tmp_path, *, cwd=ROOT, own=ROOT, available=True,
            stamp=None, records=None):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(records or [
        _assistant(REPORT + "\n" + MARKER)]) + "\n")
    monkeypatch.setattr(ss, "resolve_transcript", lambda sid, *a, **k: str(transcript))
    monkeypatch.setattr(enrollment, "self_root", lambda: own)
    monkeypatch.setattr(enrollment, "root_enrolled",
                        lambda c: ROOT if str(c).startswith(ROOT) else "/elsewhere")
    monkeypatch.setattr(rebuild_state, "cached_stamp", lambda: stamp or {})
    d = BobDaemon()
    d._rebuild = {**rebuild_state.snapshot_defaults(), "available": available}
    stub = {"session_id": "00000000-0000-0000-0000-000000000001",
            "project": "p", "cwd": cwd, "state": "idle", "subagents": 0,
            "subagent_ids": [], "_category": "sleeping"}
    return d._enrich_agent_stubs([stub])["sleeping"][0]


def test_offered_in_our_own_checkout(monkeypatch, tmp_path):
    assert _daemon(monkeypatch, tmp_path)["rebuild_offered"] is True


def test_offered_for_an_agent_that_cd_d_into_a_subfolder(monkeypatch, tmp_path):
    assert _daemon(monkeypatch, tmp_path,
                   cwd=ROOT + "/host")["rebuild_offered"] is True


def test_not_offered_in_another_project(monkeypatch, tmp_path):
    assert _daemon(monkeypatch, tmp_path,
                   cwd="/Users/me/other")["rebuild_offered"] is False


def test_not_offered_in_a_card_worktree(monkeypatch, tmp_path):
    row = _daemon(monkeypatch, tmp_path, own=ROOT + "/main")
    assert row["rebuild_offered"] is False


def test_not_offered_when_this_mac_cannot_rebuild(monkeypatch, tmp_path):
    assert _daemon(monkeypatch, tmp_path,
                   available=False)["rebuild_offered"] is False


def test_offered_clears_after_a_rebuild_that_started_later(monkeypatch, tmp_path):
    row = _daemon(monkeypatch, tmp_path,
                  stamp={"ok": True, "started_at": E1 + 1})
    assert row["rebuild_offered"] is False


def test_offered_stays_when_the_rebuild_started_before_the_report(monkeypatch, tmp_path):
    row = _daemon(monkeypatch, tmp_path,
                  stamp={"ok": True, "started_at": E1 - 1})
    assert row["rebuild_offered"] is True


def test_offered_without_a_marker_is_false(monkeypatch, tmp_path):
    row = _daemon(monkeypatch, tmp_path, records=[_assistant(REPORT)])
    assert row["rebuild_offered"] is False


def test_the_marker_never_raises_a_card_or_a_bucket(monkeypatch, tmp_path):
    row = _daemon(monkeypatch, tmp_path)
    assert row["marker_at"] == 0.0


@pytest.mark.parametrize("marker,stamp,own,row,facts,want", [
    (0.0, {}, ROOT, ROOT, {"available": True}, False),
    (5.0, {}, ROOT, "/other", {"available": True}, False),
    (5.0, {}, "", "", {"available": True}, False),
    (5.0, {"ok": True, "started_at": 9.0}, ROOT, ROOT, {"available": True}, False),
    (5.0, {"ok": True, "started_at": 5.0}, ROOT, ROOT, {"available": True}, False),
    (5.0, {"ok": False, "started_at": 9.0}, ROOT, ROOT, {"available": True}, True),
    (5.0, {"ok": True, "started_at": 1.0}, ROOT, ROOT, {"available": True}, True),
    (5.0, {}, ROOT, ROOT, {"available": False}, False),
    (5.0, {}, ROOT, ROOT, {"available": True}, True),
])
def test_offered_truth_table(marker, stamp, own, row, facts, want):
    assert rebuild_state.offered(marker, stamp, own, row, facts) is want
