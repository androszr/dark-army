"""A row's `started_at` is a fixed point — on a tombstone too.

`api_server._CLOCK_FIELDS` leaves `started_at` out on purpose: a start is
news once and then never. So a start that is re-derived from the clock on
every build turns every frame into news, the one-frame-a-second quiet floor
never engages, and the panel decodes the whole state at the 0.2 s floor for
as long as the row is retained. Measured on 21 Sep 2026: three finished Grok
rows, 7 full frames in 10 s, the panel at 15 % CPU.
"""
import time

import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon.daemon import BobDaemon


class _Clock:
    """`time` as `daemon.py` sees it, with `time()` stepped forward and
    nothing else touched — the loop and the fixtures keep the real one."""

    def __init__(self, ahead: float):
        self._ahead = ahead

    def time(self):
        return time.time() + self._ahead

    def __getattr__(self, name):
        return getattr(time, name)


def _finished_rows(snap):
    return {row["session_id"]: row for row in snap.get("finished") or []}


def _tombstone(d, sid, provider, **stub):
    now = time.time()
    d._finished[sid] = {
        "finished_at": now - 60.0,
        "finished_mono": time.monotonic() - 60.0,
        "end_reason": "ended",
        "stub": {
            "session_id": sid, "project": "", "cwd": "", "state": "idle",
            "subagents": 0, "subagent_ids": [], "pid": None,
            "current_tool": "", "kind": "interactive", "agent_activity": "",
            "cli_name": "", "provider": provider, **stub,
        },
    }


@pytest.mark.parametrize("provider", ["grok", "claude", "codex"])
def test_a_tombstone_without_a_recorded_start_keeps_one_start_across_builds(monkeypatch, provider):
    d = BobDaemon()
    _tombstone(d, "t1", provider)
    first = _finished_rows(d.detailed_snapshot())["t1"]["started_at"]
    monkeypatch.setattr(daemon_mod, "time", _Clock(5.0))
    second = _finished_rows(d.detailed_snapshot())["t1"]["started_at"]
    monkeypatch.setattr(daemon_mod, "time", _Clock(9.0))
    third = _finished_rows(d.detailed_snapshot())["t1"]["started_at"]
    assert first == second == third, (first, second, third)
    # And it never claims to have started after it finished.
    assert first <= d._finished["t1"]["finished_at"]


def test_a_grok_span_is_anchored_to_the_signals_file_not_the_clock(monkeypatch, tmp_path):
    """`sessionDurationSeconds` is Grok's own wall time; the synthesised span
    ends when the signals file was written, so a finished session's start
    reads the same on every build however long it stays on the list."""
    from dark_army_daemon import grok_roster
    signals = {"sessionDurationSeconds": 120}
    anchored = grok_roster.stats_from(signals, {}, ended_at=1_000_000.0)
    assert anchored.first_ts.timestamp() == 1_000_000.0 - 120
    assert anchored.duration_seconds == 120
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 30.0)
    again = grok_roster.stats_from(signals, {}, ended_at=1_000_000.0)
    assert again.first_ts == anchored.first_ts
    # Through `enrich`, the anchor is the file's mtime.
    folder = tmp_path / "sessions" / "cwd" / "g1"
    folder.mkdir(parents=True)
    (folder / "signals.json").write_text('{"sessionDurationSeconds": 120}')
    (folder / "summary.json").write_text('{"cwd": "/tmp/x"}')
    stamp = 1_700_000_000.0
    import os
    os.utime(folder / "signals.json", (stamp, stamp))
    stats, _metrics = grok_roster.enrich("g1", "/tmp/x", tmp_path / "sessions")
    assert stats.first_ts.timestamp() == pytest.approx(stamp - 120)
    stats2, _ = grok_roster.enrich("g1", "/tmp/x", tmp_path / "sessions")
    assert stats2.first_ts == stats.first_ts
