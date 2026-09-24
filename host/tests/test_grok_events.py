"""Grok events.jsonl: per-tool counts under Grok's own names."""
import json
import os

from dark_army_daemon.grok_events import EventsCache, parse_events


def _event(kind, tool="read_file", outcome="success", ts="2026-08-15T00:00:00Z"):
    rec = {"ts": ts, "type": kind, "tool_name": tool}
    if kind == "tool_completed":
        rec["outcome"] = outcome
        rec["duration_ms"] = 4
    return rec


def test_counts_are_keyed_by_grok_tool_names(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in [
        _event("tool_started", "write"),
        _event("tool_completed", "write"),
        _event("tool_started", "search_replace"),
        _event("tool_started", "search_replace"),
        _event("tool_completed", "search_replace"),
        _event("tool_completed", "search_replace"),
        _event("tool_started", "read_file"),
        _event("tool_completed", "read_file", outcome="error"),
        {"ts": "2026-08-15T00:00:01Z", "type": "phase_changed"},
    ]))
    events = parse_events(str(path))
    assert events.tool_counts == {"write": 1, "search_replace": 2, "read_file": 1}
    assert events.total_tool_calls == 4
    assert events.tool_failures == 1


def test_missing_file_is_empty():
    events = parse_events("/no/such/events.jsonl")
    assert events.tool_counts == {}
    assert events.tool_failures == 0


def test_incremental_read_only_parses_the_delta(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(_event("tool_started", "write")) + "\n")
    os.utime(path, (1, 1))
    cache = EventsCache()
    first = cache.get(str(path))
    assert first.tool_counts == {"write": 1}
    assert cache.last_folded == 1

    assert cache.get(str(path)).tool_counts == {"write": 1}
    assert cache.last_folded == 0

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_event("tool_started", "grep")) + "\n")
    os.utime(path, (2, 2))
    second = cache.get(str(path))
    assert cache.last_folded == 1
    assert second.tool_counts == {"write": 1, "grep": 1}


def test_truncated_last_line_is_held(tmp_path):
    first = json.dumps(_event("tool_started", "write")) + "\n"
    second = json.dumps(_event("tool_started", "grep")) + "\n"
    path = tmp_path / "events.jsonl"
    path.write_bytes((first + second[:20]).encode())
    os.utime(path, (1, 1))
    cache = EventsCache()
    assert cache.get(str(path)).tool_counts == {"write": 1}

    path.write_bytes((first + second).encode())
    os.utime(path, (2, 2))
    got = cache.get(str(path))
    assert got.tool_counts == {"write": 1, "grep": 1}
    assert cache.last_folded == 1


# --- the permission hook is not a prompt --------------------------------------


def _perm(kind, tool="run_terminal_command", ts="2026-09-21T09:21:31.639Z"):
    rec = {"ts": ts, "type": kind, "tool_name": tool}
    if kind == "permission_resolved":
        rec["decision"] = "allow"
        rec["wait_ms"] = 0
    return rec


def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_a_resolved_request_is_not_pending(tmp_path):
    """Grok's `permission_prompt` Notification fires for every tool call;
    the allowed ones resolve in milliseconds and the log says so."""
    from dark_army_daemon.grok_events import permission_pending
    path = tmp_path / "events.jsonl"
    _write(path, [
        _event("tool_started"),
        _perm("permission_requested"),
        _perm("permission_resolved"),
        {"ts": "2026-09-21T09:21:31.654Z", "type": "phase_changed",
         "phase": "tool_execution"},
    ])
    assert permission_pending(str(path)) is False


def test_an_open_request_is_pending(tmp_path):
    from dark_army_daemon.grok_events import permission_pending
    path = tmp_path / "events.jsonl"
    _write(path, [
        _perm("permission_requested", ts="2026-09-21T09:00:00Z"),
        _perm("permission_resolved", ts="2026-09-21T09:00:00Z"),
        _event("tool_started"),
        _perm("permission_requested"),
        {"ts": "2026-09-21T09:21:31.640Z", "type": "phase_changed",
         "phase": "permission_prompt"},
    ])
    assert permission_pending(str(path)) is True


def test_open_permission_detail_returns_only_fields_grok_wrote(tmp_path):
    from dark_army_daemon.grok_events import permission_request_detail
    path = tmp_path / "events.jsonl"
    _write(path, [{"type": "permission_requested", "tool_name": "bash",
                   "command": "touch /tmp/probe"}])
    assert permission_request_detail(str(path)) == {
        "tool_name": "bash", "command": "touch /tmp/probe"}


def test_permission_detail_refuses_resolved_and_fifo(tmp_path):
    from dark_army_daemon.grok_events import permission_request_detail
    path = tmp_path / "events.jsonl"
    _write(path, [_perm("permission_requested"), _perm("permission_resolved")])
    assert permission_request_detail(str(path)) == {}
    assert permission_request_detail(str(tmp_path / "missing")) == {}
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert permission_request_detail(str(fifo)) == {}


def test_no_request_or_no_file_says_nothing(tmp_path):
    from dark_army_daemon.grok_events import permission_pending
    path = tmp_path / "events.jsonl"
    _write(path, [_event("tool_started"), _event("tool_completed")])
    assert permission_pending(str(path)) is None
    assert permission_pending(str(tmp_path / "missing.jsonl")) is None


def test_only_the_tail_is_read(tmp_path):
    """A long file with the pair at the end answers off the tail alone."""
    from dark_army_daemon.grok_events import permission_pending
    path = tmp_path / "events.jsonl"
    filler = [{"ts": "2026-09-21T09:00:00Z", "type": "phase_changed",
               "phase": "streaming"}] * 5000
    _write(path, filler + [_perm("permission_requested"),
                           _perm("permission_resolved")])
    assert permission_pending(str(path), tail_bytes=4096) is False
