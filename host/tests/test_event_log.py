# host/tests/test_event_log.py
"""The daemon's diary (`event_log.py`): the store and the sentence.

Seam: `EventLog(path=tmp_path / "log.jsonl")`. The sentence is composed in
exactly one place, so every `KINDS` member gets one exact-string case here and
nowhere else needs to spell one.
"""

from __future__ import annotations

import json
import os
import time

from dark_army_daemon import event_log
from dark_army_daemon.event_log import (
    EventLog, FORBIDDEN_KEYS, KINDS, MAX_ENTRIES, PRUNE_SLACK, PUBLISHED_KEYS,
    RETENTION_SECONDS, sentence,
)


#: A base every literal `ts` here is offset from; a day-old literal is pruned.
_NOW = time.time() - 60


def _log(tmp_path) -> EventLog:
    log = EventLog(path=tmp_path / "log.jsonl")
    log.open()
    return log


# ── the sentence, one per kind ────────────────────────────────────────────────

def test_every_kind_has_a_sentence_case_here():
    covered = {
        "session_start", "session_end", "session_error", "permission_ask",
        "permission_resolved", "card_dispatched", "card_done", "card_manual",
        "card_dispatch_failed", "card_plan_attached", "card_plan_approved",
        "card_work_recorded", "access_burst",
    }
    assert covered == set(KINDS)
    for kind in KINDS:
        assert sentence(kind, nickname="Vex", title="t", project="bob",
                        tool="Bash", outcome="allow") != ""


def test_session_start():
    assert sentence("session_start", nickname="Vex", project="bob") \
        == "Vex started in bob"


def test_session_end_plain_and_decorated():
    assert sentence("session_end", nickname="Vex", project="bob",
                    detail={"end_reason": "ended"}) == "Vex finished in bob"
    assert sentence("session_end", nickname="Vex", project="bob",
                    detail={"end_reason": "no process",
                            "duration_seconds": 252, "cost": 0.4171}) \
        == "Vex finished in bob after 4m 12s, $0.42 — no process"


def test_session_error():
    assert sentence("session_error", nickname="Hex", project="bob") \
        == "Hex hit an API error in bob"


def test_permission_ask_clips_the_description():
    assert sentence("permission_ask", nickname="Hex",
                    detail={"tool": "Bash", "description": ""}) \
        == "Hex wants to run Bash"
    long = "x" * 200
    text = sentence("permission_ask", nickname="Hex",
                    detail={"tool": "Bash", "description": long})
    assert text.startswith("Hex wants to run Bash: xxx")
    assert text.endswith("…")
    assert len(text) == len("Hex wants to run Bash: ") + event_log.MAX_DESCRIPTION_CHARS


def test_permission_resolved_three_ways():
    base = dict(nickname="Hex", detail={"tool": "Bash"})
    assert sentence("permission_resolved", outcome="allow", **base) \
        == "Hex's Bash was allowed"
    assert sentence("permission_resolved", outcome="deny", **base) \
        == "Hex's Bash was denied"
    assert sentence("permission_resolved", outcome="lapsed",
                    why="the session ended", **base) \
        == "Hex's Bash lapsed — the session ended"


def test_card_dispatched():
    assert sentence("card_dispatched", title="Add the diary", project="bob",
                    detail={"tool": "claude"}) \
        == "Add the diary started with claude in bob"


def test_card_done_by_agent_and_by_hand():
    assert sentence("card_done", title="Add the diary", nickname="Vex",
                    detail={"closed_by": "Vex"}) \
        == "Vex finished the card Add the diary"
    assert sentence("card_done", title="Add the diary",
                    detail={"closed_by": "user"}) \
        == "Add the diary was moved to Done by hand"


def test_card_manual_failed_and_plan():
    assert sentence("card_manual", title="Add the diary") \
        == "Add the diary needs a manual check"
    assert sentence("card_dispatch_failed", title="Add the diary",
                    detail={"error": "no session appeared"}) \
        == "Add the diary could not start — no session appeared"
    assert sentence("card_plan_attached", title="Add the diary",
                    nickname="Mira") == "Mira attached a plan to Add the diary"


def test_who_falls_back_title_then_provider_then_an_agent():
    assert sentence("session_start", title="Fix the strip", project="bob") \
        == "Fix the strip started in bob"
    assert sentence("session_start", detail={"provider": "codex"}, project="bob") \
        == "codex started in bob"
    assert sentence("session_start", project="bob") == "an agent started in bob"
    # A card sentence never uses the card's title as its subject.
    assert sentence("card_plan_attached", title="Add the diary") \
        == "an agent attached a plan to Add the diary"


def test_unknown_kind_is_an_empty_sentence():
    assert sentence("nonsense", nickname="x") == ""


# ── append ────────────────────────────────────────────────────────────────────

def test_append_carries_exactly_the_published_keys(tmp_path):
    log = _log(tmp_path)
    entry = log.append("session_start", nickname="Vex", project="bob",
                       session_id="s1", detail={"provider": "claude"})
    assert entry is not None
    assert set(entry) == set(PUBLISHED_KEYS)
    assert entry["text"] == "Vex started in bob"
    assert len(entry["id"]) == 12
    assert entry["detail"] == {"provider": "claude"}


def test_append_refuses_an_unknown_kind(tmp_path):
    log = _log(tmp_path)
    assert log.append("session_begin", nickname="x") is None
    assert log.recent() == []
    assert not (tmp_path / "log.jsonl").exists()


def test_append_refuses_a_forbidden_key_at_any_depth(tmp_path):
    log = _log(tmp_path)
    assert "claim" in FORBIDDEN_KEYS and "port" in FORBIDDEN_KEYS
    assert log.append("permission_ask", nickname="x", claim="secret") is None
    assert log.append("permission_ask", nickname="x",
                      detail={"tool": "Bash", "claim": "secret"}) is None
    assert log.append("permission_ask", nickname="x",
                      detail={"nested": {"port": 4242}}) is None
    assert log.recent() == []


def test_append_writes_one_json_line_per_event(tmp_path):
    log = _log(tmp_path)
    log.append("session_start", nickname="a", project="p")
    log.append("session_error", nickname="a", project="p")
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["kind"] == "session_error"
    assert oct(os.stat(tmp_path / "log.jsonl").st_mode & 0o777) == "0o600"


# ── prune ─────────────────────────────────────────────────────────────────────

def test_entries_older_than_a_day_are_pruned(tmp_path, monkeypatch):
    log = _log(tmp_path)
    now = 1_800_000_000.0
    monkeypatch.setattr(event_log.time, "time", lambda: now)
    log.append("session_start", nickname="old", ts=now - RETENTION_SECONDS - 5)
    log.append("session_start", nickname="fresh", ts=now - 10)
    kept = [e["nickname"] for e in log.recent()]
    assert kept == ["fresh"]
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert [json.loads(l)["nickname"] for l in lines] == ["fresh"]


def test_recent_applies_the_retention_floor_without_a_write(tmp_path, monkeypatch):
    """A quiet day: the age prune runs only on append, so a read after 24h
    of silence must itself refuse to hand back yesterday."""
    log = _log(tmp_path)
    now = 1_800_000_000.0
    monkeypatch.setattr(event_log.time, "time", lambda: now)
    log.append("session_start", nickname="a", ts=now - 100)
    log.append("session_start", nickname="b", ts=now - 10)
    assert [e["nickname"] for e in log.recent()] == ["b", "a"]
    monkeypatch.setattr(event_log.time, "time",
                        lambda: now + RETENTION_SECONDS + 50)
    assert [e["nickname"] for e in log.recent()] == []
    monkeypatch.setattr(event_log.time, "time",
                        lambda: now + RETENTION_SECONDS - 50)
    assert [e["nickname"] for e in log.recent()] == ["b"]
    # No write happened: the file still holds both lines.
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_the_cap_keeps_the_newest(tmp_path):
    log = _log(tmp_path)
    for i in range(MAX_ENTRIES + 7):
        log.append("session_start", nickname=f"n{i}", ts=_NOW + i)
    assert len(log) == MAX_ENTRIES
    rows = log.recent()
    assert rows[0]["nickname"] == f"n{MAX_ENTRIES + 6}"
    assert rows[-1]["nickname"] == "n7"


def _count_replaces(monkeypatch):
    replaces = []
    real = os.replace

    def counting(src, dst):
        replaces.append(dst)
        return real(src, dst)

    monkeypatch.setattr(event_log.os, "replace", counting)
    return replaces


def test_the_file_is_not_rewritten_while_under_the_cap_plus_slack(
        tmp_path, monkeypatch):
    log = _log(tmp_path)
    replaces = _count_replaces(monkeypatch)
    for i in range(5):
        log.append("session_start", nickname=f"n{i}", ts=_NOW + i)
    assert replaces == []
    for i in range(MAX_ENTRIES):
        log.append("session_start", nickname=f"m{i}", ts=_NOW + 100 + i)
    # 505 lines on disk: over the cap, inside the slack — the deque is
    # trimmed, the file is left alone.
    assert replaces == []
    assert len(log) == MAX_ENTRIES
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == MAX_ENTRIES + 5


def test_at_the_cap_the_file_is_rewritten_once_per_slack_not_per_append(
        tmp_path, monkeypatch):
    log = _log(tmp_path)
    replaces = _count_replaces(monkeypatch)
    total = MAX_ENTRIES + PRUNE_SLACK
    for i in range(total - 1):
        log.append("session_start", nickname=f"n{i}", ts=_NOW + i)
        assert len(log) <= MAX_ENTRIES
    assert replaces == []                     # 549 appends, zero rewrites
    log.append("session_start", nickname="tip", ts=_NOW + total)
    assert len(replaces) == 1                 # the 550th compacts the file
    assert len(log) == MAX_ENTRIES
    lines = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(lines) == MAX_ENTRIES
    assert json.loads(lines[-1])["nickname"] == "tip"
    assert log.recent()[0]["nickname"] == "tip"
    # And the next slack's worth is silent again.
    for i in range(PRUNE_SLACK - 1):
        log.append("session_start", nickname=f"z{i}", ts=_NOW + total + 1 + i)
    assert len(replaces) == 1
    assert len(log) == MAX_ENTRIES


def test_an_age_prune_rewrites_at_once_whatever_the_line_count(
        tmp_path, monkeypatch):
    log = _log(tmp_path)
    replaces = _count_replaces(monkeypatch)
    now = 1_800_000_000.0
    monkeypatch.setattr(event_log.time, "time", lambda: now)
    log.append("session_start", nickname="old", ts=now - RETENTION_SECONDS + 1)
    assert replaces == []
    monkeypatch.setattr(event_log.time, "time", lambda: now + 5)
    log.append("session_start", nickname="fresh", ts=now + 5)
    assert len(replaces) == 1
    assert [e["nickname"] for e in log.recent()] == ["fresh"]


def test_open_compacts_a_file_that_ran_over_the_cap(tmp_path, monkeypatch):
    log = _log(tmp_path)
    for i in range(MAX_ENTRIES + 20):
        log.append("session_start", nickname=f"n{i}", ts=_NOW + i)
    assert len((tmp_path / "log.jsonl").read_text().splitlines()) == MAX_ENTRIES + 20
    log.close()
    replaces = _count_replaces(monkeypatch)
    again = EventLog(path=tmp_path / "log.jsonl")
    again.open()
    assert len(replaces) == 1
    assert len(again) == MAX_ENTRIES
    assert len((tmp_path / "log.jsonl").read_text().splitlines()) == MAX_ENTRIES


def test_last_ts_is_the_newest_of_that_kind_for_that_session(tmp_path):
    log = _log(tmp_path)
    log.append("session_start", session_id="a", ts=_NOW + 1)
    log.append("session_end", session_id="a", ts=_NOW + 2)
    log.append("session_start", session_id="a", ts=_NOW + 3)
    log.append("session_end", session_id="b", ts=_NOW + 4)
    assert log.last_ts("a", "session_end") == _NOW + 2
    assert log.last_ts("a", "session_start") == _NOW + 3
    assert log.last_ts("b", "session_start") is None
    assert log.last_ts("", "session_end") is None


# ── reopen ────────────────────────────────────────────────────────────────────

def test_write_close_reopen_returns_the_same_entries_in_order(tmp_path):
    log = _log(tmp_path)
    a = log.append("session_start", nickname="a", ts=_NOW + 1)
    b = log.append("session_end", nickname="a", ts=_NOW + 2,
                   detail={"end_reason": "ended"})
    log.close()
    assert log.append("session_start", nickname="late") is None
    again = EventLog(path=tmp_path / "log.jsonl")
    again.open()
    assert again.recent() == [b, a]


def test_a_truncated_last_line_is_tolerated(tmp_path):
    log = _log(tmp_path)
    log.append("session_start", nickname="a", ts=_NOW + 1)
    log.append("session_start", nickname="b", ts=_NOW + 2)
    path = tmp_path / "log.jsonl"
    with open(path, "a") as fh:
        fh.write('{"id": "abc", "ts": 1000003, "kind": "session_st')
    again = EventLog(path=path)
    again.open()
    assert [e["nickname"] for e in again.recent()] == ["b", "a"]


def test_unknown_keys_on_disk_are_dropped_on_open(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(json.dumps({"id": "abc", "ts": _NOW, "kind": "session_start",
                                "text": "x started", "future_field": 1}) + "\n")
    log = EventLog(path=path)
    log.open()
    rows = log.recent()
    assert len(rows) == 1
    assert "future_field" not in rows[0]
    assert rows[0]["detail"] == {}


# ── recent ────────────────────────────────────────────────────────────────────

def test_recent_is_newest_first_strictly_after_since_and_capped(tmp_path):
    log = _log(tmp_path)
    for i in range(5):
        log.append("session_start", nickname=f"n{i}", ts=_NOW + i)
    rows = log.recent()
    assert [r["nickname"] for r in rows] == ["n4", "n3", "n2", "n1", "n0"]
    assert [r["nickname"] for r in log.recent(since=_NOW + 2)] == ["n4", "n3"]
    assert [r["nickname"] for r in log.recent(limit=2)] == ["n4", "n3"]
    assert log.recent(since=_NOW + 4) == []


def test_recent_returns_copies(tmp_path):
    log = _log(tmp_path)
    log.append("session_start", nickname="a", detail={"provider": "claude"})
    rows = log.recent()
    rows[0]["detail"]["provider"] = "tampered"
    rows[0]["nickname"] = "tampered"
    assert log.recent()[0]["detail"] == {"provider": "claude"}
    assert log.recent()[0]["nickname"] == "a"


def test_access_burst_names_the_count_the_source_and_the_door():
    """The diary's one line about a burst at the phone doors. `door` is the
    label (`access_log.door_label`) and the sentence supplies the noun; the
    peer is an address or a paired device id, never a key."""
    assert sentence("access_burst", detail={
        "door": "Wi-Fi", "peer": "10.0.0.9", "count": 5,
        "window_seconds": 600,
    }) == ("Dark Army refused 5 connection attempts from 10.0.0.9 "
           "at the Wi-Fi door in 10 minutes")
    assert sentence("access_burst", detail={"window_seconds": 30}) \
        == ("Dark Army refused 0 connection attempts from an unknown source "
            "at the phone door in 1 minute")
