import json
import os
import time
import unicodedata
from pathlib import Path

import pytest

from dark_army_daemon import codex_rollouts
from dark_army_daemon.daemon import BobDaemon


def write_rollout(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def row(kind, payload, timestamp="2026-08-18T18:00:00Z"):
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def test_parse_live_codex_rollout(tmp_path):
    path = tmp_path / "2026/08/18/rollout-one.jsonl"
    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "abc", "cwd": "/code/bob",
                             "originator": "codex-tui", "source": "cli",
                             "thread_source": "user"}),
        row("turn_context", {"type": "turn_context", "cwd": "/code/bob", "model": "gpt-5.6-sol"}),
        row("response_item", {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /code/bob"}]}),
        row("response_item", {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "Add Codex support"}]}),
        row("event_msg", {"type": "task_started", "turn_id": "t1"}),
        row("response_item", {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "{}"}),
        row("event_msg", {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 120, "cached_input_tokens": 80,
            "cache_write_input_tokens": 3, "output_tokens": 20},
            "last_token_usage": {"total_tokens": 500}, "model_context_window": 1000},
            "rate_limits": {"primary": {"used_percent": 42,
                            "window_minutes": 10080, "resets_at": 2000}}}),
    ])
    found = codex_rollouts.parse_rollout(path)
    assert found is not None
    assert found.session_id == "codex:abc"
    assert found.project == "bob"
    assert (found.originator, found.source_kind, found.thread_source) == (
        "codex-tui", "cli", "user")
    assert found.activity == "working"
    assert found.current_tool == "exec"
    assert found.title == "Add Codex support"
    assert found.stats.model == "gpt-5.6-sol"
    assert found.stats.input_tokens == 120
    assert found.stats.cache_read_tokens == 80
    assert found.metrics["ctx_used_pct"] == 50
    assert found.metrics["five_hour_pct"] == 42
    assert found.metrics["budget_cycle"] == "7d"
    assert found.limit_bars == [{
        "kind": "codex_primary", "group": "weekly", "provider": "codex",
        "title": "Codex, 7d", "label": "7d", "percent": 42.0,
        "resets_at": 2000.0, "stale": False,
    }]


def test_usage_snapshot_keeps_account_bars_after_live_grace(tmp_path, monkeypatch):
    """A quiet Codex terminal is not a closed weekly window.

    LIVE_GRACE is the roster's liveness cut. The chips read account budgets,
    which outlive the last process by days, and a 15-minute cut blanked them.
    """
    path = tmp_path / "2026/08/29/rollout-quiet.jsonl"
    write_rollout(path, [row("session_meta", {"type": "session_meta", "id": "abc"})])
    older = time.time() - (codex_rollouts.LIVE_GRACE_SECONDS + 3600)
    os.utime(path, (older, older))
    record = codex_rollouts.CodexRecord(
        session_id="codex:abc", thread_id="abc", path=path, last_event=older,
        limit_bars=[{"kind": "codex_secondary", "percent": 29, "label": "7d"}],
    )
    monkeypatch.setattr(codex_rollouts, "_USAGE_CACHE", {})
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(codex_rollouts, "parse_rollout", lambda _p: record)

    snap = codex_rollouts.usage_snapshot()
    assert snap["available"] is True
    assert snap["bars"][0]["percent"] == 29


def test_usage_snapshot_uses_freshest_rate_limit_and_marks_expired_reset():
    older = codex_rollouts.CodexRecord(
        session_id="codex:old", thread_id="old", path=Path("old.jsonl"),
        last_event=100, limit_bars=[{"kind": "codex_primary", "percent": 10,
                                    "resets_at": 500}],
    )
    newer = codex_rollouts.CodexRecord(
        session_id="codex:new", thread_id="new", path=Path("new.jsonl"),
        last_event=200, limit_bars=[{"kind": "codex_primary", "percent": 45,
                                    "resets_at": 250}],
    )
    snap = codex_rollouts.usage_snapshot([older, newer], now=300)
    assert snap["available"] is True
    assert snap["bars"][0]["percent"] == 45
    assert snap["bars"][0]["stale"] is True


def test_usage_snapshot_does_not_mutate_cache_or_inspect_processes(
    tmp_path, monkeypatch,
):
    path = tmp_path / "2026/08/18/rollout-usage.jsonl"
    write_rollout(path, [row("session_meta", {"type": "session_meta", "id": "abc"})])
    record = codex_rollouts.CodexRecord(
        session_id="codex:abc", thread_id="abc", path=path, last_event=200,
        limit_bars=[{"kind": "codex_primary", "percent": 42}],
    )
    marker = Path("loop-owned-cache-entry.jsonl")
    parse_calls = 0

    def parse(_found):
        nonlocal parse_calls
        parse_calls += 1
        return record

    monkeypatch.setattr(codex_rollouts, "_CACHE", {marker: (1, 2, record)})
    monkeypatch.setattr(codex_rollouts, "_USAGE_CACHE", {})
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(codex_rollouts, "parse_rollout", parse)
    monkeypatch.setattr(
        codex_rollouts, "_process_snapshot",
        lambda: pytest.fail("usage read inspected processes"),
    )

    first = codex_rollouts.usage_snapshot(now=300)
    second = codex_rollouts.usage_snapshot(now=300)
    path.write_text(path.read_text() + "\n")
    third = codex_rollouts.usage_snapshot(now=300)

    assert first == second
    assert third == first
    assert first["bars"][0]["percent"] == 42
    assert parse_calls == 2
    assert codex_rollouts._CACHE == {marker: (1, 2, record)}


def test_usage_cache_serializes_concurrent_worker_reads(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    path = tmp_path / "2026/08/18/rollout-usage.jsonl"
    write_rollout(path, [row("session_meta", {"type": "session_meta", "id": "abc"})])
    record = codex_rollouts.CodexRecord(
        session_id="codex:abc", thread_id="abc", path=path, last_event=200,
        limit_bars=[{"kind": "codex_primary", "percent": 42}],
    )
    parse_calls = 0
    calls_lock = threading.Lock()
    simultaneous_parse = threading.Barrier(2)

    def parse(_found):
        nonlocal parse_calls
        with calls_lock:
            parse_calls += 1
        try:
            # Without the usage lock both workers enter and release this barrier;
            # with the lock the first times out, caches, and the second reuses it.
            simultaneous_parse.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return record

    monkeypatch.setattr(codex_rollouts, "_USAGE_CACHE", {})
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(codex_rollouts, "parse_rollout", parse)

    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(lambda _item: codex_rollouts.usage_snapshot(), range(2)))

    assert snapshots[0] == snapshots[1]
    assert parse_calls == 1


def test_usage_keeps_older_same_thread_limits_when_replacement_has_none(
    tmp_path, monkeypatch,
):
    older = tmp_path / "2026/08/18/rollout-old.jsonl"
    newer = tmp_path / "2026/08/19/rollout-new.jsonl"
    for path in (older, newer):
        write_rollout(path, [row("session_meta", {"type": "session_meta", "id": "abc"})])
    os.utime(older, (1999, 1999))
    os.utime(newer, (2000, 2000))
    records = {
        older: codex_rollouts.CodexRecord(
            session_id="codex:abc", thread_id="abc", path=older,
            last_event=1999, limit_bars=[{"kind": "codex_primary", "percent": 37}],
        ),
        newer: codex_rollouts.CodexRecord(
            session_id="codex:abc", thread_id="abc", path=newer,
            last_event=2000, limit_bars=[],
        ),
    }
    monkeypatch.setattr(codex_rollouts, "_USAGE_CACHE", {})
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(codex_rollouts, "parse_rollout", records.__getitem__)

    snapshot = codex_rollouts.usage_snapshot(now=2000)

    assert snapshot["available"] is True
    assert snapshot["bars"][0]["percent"] == 37


class _FakeProcess:
    def __init__(self, pid, name="codex", cmdline=("codex",), cwd="/code/bob",
                 exe="/opt/codex", create_time=100.0, denied=(),
                 open_files=(), open_file_reads=None, on_open_files=None):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "cmdline": list(cmdline),
                     "cwd": cwd, "exe": exe, "create_time": create_time}
        self.fresh = dict(self.info)
        self._open_file_reads = (
            [tuple(open_files)] if open_file_reads is None
            else list(open_file_reads)
        )
        self._open_file_calls = 0
        self._on_open_files = on_open_files
        for key in denied:
            self.info.pop(key, None)
            setattr(self, key, self._denied)

    @staticmethod
    def _denied():
        raise PermissionError("denied")

    def exe(self):
        return self.fresh["exe"]

    def cwd(self):
        return self.fresh["cwd"]

    def create_time(self):
        return self.fresh["create_time"]

    def cmdline(self):
        return list(self.fresh["cmdline"])

    def terminal(self):
        return self.fresh.get("terminal")

    def open_files(self):
        index = self._open_file_calls
        self._open_file_calls += 1
        if self._on_open_files is not None:
            self._on_open_files(self, index)
        if not self._open_file_reads:
            return []
        value = self._open_file_reads[min(index, len(self._open_file_reads) - 1)]
        if isinstance(value, BaseException):
            raise value
        return list(value)


def _root(thread_id="abc", cwd="/code/bob", **changes):
    values = {
        "session_id": f"codex:{thread_id}", "thread_id": thread_id,
        "path": Path(f"{thread_id}.jsonl"), "cwd": cwd,
        "originator": "codex-tui", "source_kind": "cli",
        "thread_source": "user",
    }
    values.update(changes)
    return codex_rollouts.CodexRecord(**values)


def _with_tty(process, tty="ttys004"):
    process.info["terminal"] = tty
    process.fresh["terminal"] = tty
    return process


def _title_roots(records):
    return codex_rollouts.project_title_roots(records)


def _title_map(records, processes):
    return codex_rollouts.resolve_title_ttys(_title_roots(records), processes)


def _native_holder(pid, path, tty="ttys004", *, cwd="/code/bob", **changes):
    process = _FakeProcess(pid, cwd=cwd, open_files=(path,), **changes)
    return _with_tty(process, tty)


def test_vscode_attribution_links_one_recent_native_terminal_without_control():
    record = _root(source_kind="vscode", started_at=100.799)
    process = _FakeProcess(60576, create_time=100.0)

    codex_rollouts.attach_process_ids([record], [process])

    assert record.pid == 60576
    assert record.process_identity is None
    assert record.process_seen is None
    assert codex_rollouts._safe_cli_root(record) is False


@pytest.mark.parametrize("changes", [
    {"parent_thread_id": "parent"},
    {"originator": "codex-app"},
    {"source_kind": "app"},
    {"thread_source": "subagent"},
    {"cwd": ""},
    {"thread_id": ""},
    {"started_at": None},
    {"started_at": 0},
    {"started_at": float("inf")},
    {"started_at": float("nan")},
])
def test_vscode_attribution_refuses_ineligible_roots(changes):
    fields = {"source_kind": "vscode", "started_at": 100.799}
    fields.update(changes)
    record = _root(**fields)
    codex_rollouts.attach_process_ids([record], [_FakeProcess(60576)])
    assert record.pid is None
    assert record.process_identity is None
    assert record.process_seen is None


@pytest.mark.parametrize("other,processes", [
    ([_root("other", source_kind="vscode")], [_FakeProcess(60576)]),
    ([_root("other")], [_FakeProcess(60576)]),
    ([], [_FakeProcess(60576), _FakeProcess(60577)]),
    ([], [_FakeProcess(60576, cwd="/elsewhere")]),
    ([], [_FakeProcess(60576, cmdline=("codex", "resume", "foreign"))]),
    ([], [_FakeProcess(60576, exe="/Applications/Codex.app/Contents/MacOS/codex")]),
    ([], [_FakeProcess(60576, exe="/opt/node", cmdline=("node", "codex"))]),
    ([], [_FakeProcess(60576, cmdline=("codex", "app-server"))]),
    ([], [_FakeProcess(60576, denied=("exe",))]),
    ([], [_FakeProcess(60576), _FakeProcess(60577, denied=("exe", "cwd"))]),
    ([], [_FakeProcess(60576), _FakeProcess(60577, denied=("exe",))]),
])
def test_vscode_attribution_refuses_competing_or_unreadable_processes(other, processes):
    record = _root(source_kind="vscode", started_at=100.799)
    codex_rollouts.attach_process_ids([record, *other], processes)
    assert record.pid is None
    assert record.process_identity is None
    assert record.process_seen is None


@pytest.mark.parametrize("started,created", [
    (99.999, 100.0),
    (401.0, 100.0),
    (100.799, 0),
    (100.799, float("inf")),
    (100.799, float("nan")),
])
def test_vscode_attribution_requires_finite_recent_preceding_start(started, created):
    record = _root(source_kind="vscode", started_at=started)
    codex_rollouts.attach_process_ids([record], [_FakeProcess(60576, create_time=created)])
    assert record.pid is None
    assert record.process_identity is None
    assert record.process_seen is None


def test_vscode_attribution_ignores_a_codex_left_open_for_hours_in_the_folder():
    # A Refine's new Codex beside one open since the morning: the old
    # process began long before this rollout, so it is no competitor.
    record = _root(source_kind="vscode", started_at=20_000.5)
    old = _FakeProcess(60576, create_time=100.0)
    new = _FakeProcess(60577, create_time=20_000.0)

    codex_rollouts.attach_process_ids([record], [old, new])

    assert record.pid == 60577


def test_vscode_attribution_two_new_processes_in_the_window_pair_neither():
    record = _root(source_kind="vscode", started_at=20_000.5)
    codex_rollouts.attach_process_ids([record], [
        _FakeProcess(60576, create_time=19_990.0),
        _FakeProcess(60577, create_time=20_000.0)])
    assert record.pid is None


def test_vscode_attribution_rechecks_identity_and_clears_stale_pid():
    record = _root(source_kind="vscode", started_at=100.799)
    process = _FakeProcess(60576)
    codex_rollouts.attach_process_ids([record], [process])
    assert record.pid == 60576
    process.fresh["create_time"] = 101.0
    codex_rollouts.attach_process_ids([record], [process])
    assert record.pid is None
    process.fresh["create_time"] = 100.0
    codex_rollouts.attach_process_ids([record], [])
    assert record.pid is None
    assert record.process_seen is None


def test_vscode_attribution_unavailable_scan_clears_stale_pid():
    record = _root(source_kind="vscode", started_at=100.799, pid=60576)

    def unavailable():
        raise PermissionError("process table denied")
        yield

    codex_rollouts.attach_process_ids([record], unavailable())
    assert record.pid is None
    assert record.process_identity is None
    assert record.process_seen is None


def test_vscode_attribution_cannot_reuse_a_cli_process():
    cli = _root("cli", started_at=100.799)
    vscode = _root("vscode", source_kind="vscode", started_at=100.799)
    process = _FakeProcess(60576, cmdline=("codex", "resume", "cli"))
    codex_rollouts.attach_process_ids([cli, vscode], [process])
    assert cli.pid == 60576
    assert vscode.pid is None


def test_process_attachment_prefers_explicit_thread_id():
    record = _root()
    codex_rollouts.attach_process_ids([
        record], [_FakeProcess(77, cmdline=("codex", "resume", "abc"))])
    assert record.pid == 77
    assert record.process_identity.match_kind == "explicit_resume"


def test_explicit_resume_prompt_tokens_are_not_classified_as_subcommands(tmp_path):
    path = tmp_path / "abc.jsonl"
    path.write_text("root")
    record = _root(path=path)
    process = _with_tty(
        _FakeProcess(77, cmdline=("codex", "resume", "abc", "review"))
    )
    codex_rollouts.attach_process_ids([record], [process])
    title_ttys = _title_map([record], [process])
    assert record.pid == 77
    assert record.process_identity.match_kind == "explicit_resume"
    assert title_ttys == {record.session_id: "/dev/ttys004"}


def test_process_seen_is_true_when_a_native_process_shares_the_cwd(tmp_path):
    """A cwd match alone now proves no ownership."""
    path = tmp_path / "abc.jsonl"
    path.write_text("root")
    record = _root(path=path)
    codex_rollouts.attach_process_ids([record], [_FakeProcess(88)])
    assert record.process_seen is False


def test_process_seen_is_true_when_a_node_launcher_shares_the_cwd():
    record = _root()
    wrapper = _with_tty(_FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/opt/homebrew/bin/codex", "resume", "abc"),
    ))
    codex_rollouts.attach_process_ids([record], [wrapper])
    assert record.process_seen is False
    assert record.pid is None
    assert record.process_identity is None


def test_process_seen_is_false_when_no_native_process_shares_the_cwd():
    record = _root()
    codex_rollouts.attach_process_ids(
        [record], [_FakeProcess(88, cwd="/other/project")])
    assert record.process_seen is False
    assert record.pid is None


def test_process_seen_is_false_when_no_native_and_no_title_process_shares_the_cwd():
    record = _root()
    other = _FakeProcess(
        88, name="python", exe="/usr/bin/python3",
        cmdline=("python3", "-m", "http.server"),
    )
    codex_rollouts.attach_process_ids([record], [other])
    assert record.process_seen is False
    assert record.pid is None


def test_process_seen_stays_none_for_a_non_cli_root():
    record = _root(originator="codex-app", source_kind="app")
    codex_rollouts.attach_process_ids([record], [_FakeProcess(88)])
    assert record.process_seen is None
    assert record.pid is None


def test_process_seen_stays_none_for_an_empty_process_list():
    record = _root()
    codex_rollouts.attach_process_ids([record], [])
    assert record.process_seen is False


def test_process_attachment_uses_unambiguous_cwd_only():
    record = _root()
    codex_rollouts.attach_process_ids(
        [record], [_FakeProcess(88)])
    assert record.pid == 88
    assert record.process_identity.match_kind == "unique_cwd"

    another = _root("def")
    record.pid = None
    record.process_identity = None
    codex_rollouts.attach_process_ids(
        [record, another], [_FakeProcess(88)])
    assert record.pid is None and another.pid is None


def test_duplicate_thread_paths_keep_newest_before_process_attachment(
    tmp_path, monkeypatch,
):
    older = tmp_path / "2026/08/18/rollout-old.jsonl"
    newer = tmp_path / "2026/08/19/rollout-new.jsonl"
    for path in (older, newer):
        write_rollout(path, [row("session_meta", {
            "type": "session_meta", "id": "abc", "cwd": "/code/bob",
            "originator": "codex-tui", "source": "cli",
            "thread_source": "user",
        })])
    os.utime(older, (1999, 1999))
    os.utime(newer, (2000, 2000))
    proc = _FakeProcess(77, cmdline=("codex", "resume", "abc"))
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", lambda attrs: [proc])
    codex_rollouts._CACHE.clear()

    records = codex_rollouts.load_recent(tmp_path, now=2000, grace=60)

    assert len(records) == 1
    assert records[0].path == newer
    assert records[0].process_identity.match_kind == "explicit_resume"


def test_thread_index_uses_latest_clean_nonempty_name(tmp_path):
    index = tmp_path / "session_index.jsonl"
    index.write_bytes(b"".join([
        json.dumps({"id": "abc", "thread_name": "Provisional name"}).encode() + b"\n",
        b"not-json\n",
        json.dumps({
            "id": "abc", "thread_name": "  Generated\x07\n\u202e\u200b name  ",
        }).encode() + b"\n",
        json.dumps({"id": "abc", "thread_name": "   "}).encode() + b"\n",
        json.dumps({"id": "def", "thread_name": 7}).encode() + b"\n",
    ]))

    assert codex_rollouts._thread_names(index) == {"abc": "Generated name"}


def test_thread_name_removes_unicode_format_and_bidi_controls():
    cleaned = codex_rollouts._clean_thread_name("Safe\u200b\u202e\u2066 name")

    assert cleaned == "Safe name"
    assert all(unicodedata.category(char)[0] != "C" for char in cleaned)


def test_thread_index_discards_a_partial_first_tail_line(tmp_path, monkeypatch):
    index = tmp_path / "session_index.jsonl"
    old = json.dumps({"id": "old", "thread_name": "Outside tail"}) + "\n"
    recent = json.dumps({"id": "new", "thread_name": "Inside tail"}) + "\n"
    index.write_text(old + ("x" * 80) + "\n" + recent, encoding="utf-8")
    monkeypatch.setattr(codex_rollouts, "SESSION_INDEX_TAIL_BYTES", len(recent) + 20)

    assert codex_rollouts._thread_names(index) == {"new": "Inside tail"}


def test_thread_index_keeps_first_row_when_tail_starts_on_line_boundary(
    tmp_path, monkeypatch,
):
    index = tmp_path / "session_index.jsonl"
    older = json.dumps({"id": "old", "thread_name": "Outside tail"}) + "\n"
    boundary = json.dumps({"id": "new", "thread_name": "Exact boundary"}) + "\n"
    index.write_text(older + boundary, encoding="utf-8")
    monkeypatch.setattr(codex_rollouts, "SESSION_INDEX_TAIL_BYTES", len(boundary.encode()))

    assert codex_rollouts._thread_names(index) == {"new": "Exact boundary"}


def test_generated_name_overlays_a_cached_rollout_without_mutating_cache(
    tmp_path, monkeypatch,
):
    rollout = tmp_path / "2026/08/18/rollout-one.jsonl"
    write_rollout(rollout, [
        row("session_meta", {"type": "session_meta", "id": "abc", "cwd": "/code/bob"}),
        row("response_item", {"type": "message", "role": "user", "content": [{
            "type": "input_text", "text": "Opening request used as fallback",
        }]}),
    ])
    index = tmp_path / "session-index.jsonl"
    index.write_text(json.dumps({"id": "abc", "thread_name": "Provisional"}) + "\n")
    monkeypatch.setattr(codex_rollouts, "SESSION_INDEX_PATH", index)
    monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: [])
    codex_rollouts._CACHE.clear()

    first = codex_rollouts.load_recent(tmp_path, now=rollout.stat().st_mtime, grace=60)
    index.write_text(
        index.read_text() + json.dumps({"id": "abc", "thread_name": "Concise generated name"}) + "\n"
    )
    second = codex_rollouts.load_recent(tmp_path, now=rollout.stat().st_mtime, grace=60)

    assert first[0].title == "Provisional"
    assert second[0].title == "Concise generated name"
    assert codex_rollouts._CACHE[rollout][2].title == "Opening request used as fallback"


def test_missing_or_malformed_thread_index_keeps_prompt_fallback(tmp_path, monkeypatch):
    rollout = tmp_path / "2026/08/18/rollout-one.jsonl"
    write_rollout(rollout, [
        row("session_meta", {"type": "session_meta", "id": "abc", "cwd": "/code/bob"}),
        row("response_item", {"type": "message", "role": "user", "content": [{
            "type": "input_text", "text": "Honest fallback",
        }]}),
    ])
    index = tmp_path / "session-index.jsonl"
    index.write_text('{"id":"abc","thread_name":', encoding="utf-8")
    monkeypatch.setattr(codex_rollouts, "SESSION_INDEX_PATH", index)
    monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: [])
    codex_rollouts._CACHE.clear()

    records = codex_rollouts.load_recent(
        tmp_path, now=rollout.stat().st_mtime, grace=60,
    )

    assert records[0].title == "Honest fallback"


def test_plugin_recommendations_are_synthetic_only_when_standalone(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "abc"}),
        row("response_item", {"type": "message", "role": "user", "content": [{
            "type": "input_text",
            "text": "<recommended_plugins>GitHub</recommended_plugins>\n",
        }]}),
        row("response_item", {"type": "message", "role": "user", "content": [{
            "type": "input_text", "text": "Build the safe controls",
        }]}),
    ])
    assert codex_rollouts.parse_rollout(path).title == "Build the safe controls"

    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "abc"}),
        row("response_item", {"type": "message", "role": "user", "content": [{
            "type": "input_text",
            "text": "<recommended_plugins>GitHub</recommended_plugins> Keep this request",
        }]}),
    ])
    assert "Keep this request" in codex_rollouts.parse_rollout(path).title


@pytest.mark.parametrize("injected", [
    "# AGENTS.md instructions for /code/bob",
    "<INSTRUCTIONS>repository setup</INSTRUCTIONS>",
    "<environment_context><cwd>/code/bob</cwd></environment_context>",
    "<recommended_plugins>GitHub</recommended_plugins>",
])
def test_standalone_injections_never_become_titles(tmp_path, injected):
    path = tmp_path / "rollout.jsonl"
    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "abc"}),
        row("response_item", {"type": "message", "role": "user", "content": [{
            "type": "input_text", "text": injected,
        }]}),
        row("response_item", {"type": "message", "role": "user", "content": [{
            "type": "input_text", "text": "The real request",
        }]}),
    ])
    assert codex_rollouts.parse_rollout(path).title == "The real request"


def test_native_child_wins_over_node_wrapper_and_requires_exact_resume():
    record = _root()
    wrapper = _FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/opt/homebrew/bin/codex", "resume", "abc"),
    )
    native = _FakeProcess(71, cmdline=("codex", "resume", "abc"))
    codex_rollouts.attach_process_ids([record], [wrapper, native])
    assert record.pid == 71

    record = _root()
    codex_rollouts.attach_process_ids(
        [record], [_FakeProcess(72, cmdline=("codex", "--thread", "abc"))])
    assert record.process_identity.match_kind == "unique_cwd"


def test_node_wrapper_gets_a_title_tty_without_control_identity():
    record = _root()
    wrapper = _with_tty(_FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=(
            "node",
            "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js",
            "resume", "abc",
        ),
    ))

    title_ttys = _title_map([record], [wrapper])

    assert title_ttys == {record.session_id: "/dev/ttys004"}
    assert record.pid is None
    assert record.process_identity is None


def test_node_wrapper_classifies_command_before_trailing_prompt_tokens():
    record = _root()
    wrapper = _with_tty(_FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/opt/homebrew/bin/codex", "resume", "abc", "review"),
    ))

    title_ttys = _title_map([record], [wrapper])

    assert title_ttys == {record.session_id: "/dev/ttys004"}


def test_native_and_wrapper_nodes_on_one_tty_collapse_to_one_title_target(tmp_path):
    path = tmp_path / "abc.jsonl"
    path.write_text("root")
    record = _root(path=path)
    wrapper = _with_tty(_FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/opt/homebrew/bin/codex", "resume", "abc"),
    ))
    native = _with_tty(
        _FakeProcess(71, cmdline=("codex", "resume", "abc")),
    )

    codex_rollouts.attach_process_ids([record], [wrapper, native])
    title_ttys = _title_map([record], [wrapper, native])

    assert title_ttys == {record.session_id: "/dev/ttys004"}
    assert record.pid == 71
    assert record.process_identity.match_kind == "explicit_resume"


def test_title_ttys_prefer_exact_threads_in_one_cwd():
    first = _root("abc")
    second = _root("def")
    processes = [
        _with_tty(_FakeProcess(
            70, name="node", exe="/opt/homebrew/bin/node",
            cmdline=("node", "/opt/homebrew/bin/codex", "resume", "abc"),
        ), "ttys004"),
        _with_tty(_FakeProcess(
            71, name="node", exe="/opt/homebrew/bin/node",
            cmdline=("node", "/opt/homebrew/bin/codex", "resume", "def"),
        ), "/dev/ttys005"),
    ]

    title_ttys = _title_map([first, second], processes)

    assert title_ttys == {
        first.session_id: "/dev/ttys004",
        second.session_id: "/dev/ttys005",
    }


def test_exact_clear_leftovers_competing_for_one_tty_fail_closed():
    first = _root("abc")
    second = _root("def")
    processes = [
        _with_tty(_FakeProcess(
            70, name="node", exe="/opt/homebrew/bin/node",
            cmdline=("node", "/opt/homebrew/bin/codex", "resume", "abc"),
        )),
        _with_tty(_FakeProcess(
            71, name="node", exe="/opt/homebrew/bin/node",
            cmdline=("node", "/opt/homebrew/bin/codex", "resume", "def"),
        )),
    ]
    assert _title_map([first, second], processes) == {}


def test_title_tty_fallback_counts_distinct_ttys_not_process_nodes(tmp_path):
    path = tmp_path / "abc.jsonl"
    path.write_text("root")
    record = _root(path=path)
    wrapper = _with_tty(_FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/opt/homebrew/bin/codex"),
    ))
    native = _with_tty(_FakeProcess(71, denied=("open_files",)))

    title_ttys = _title_map([record], [wrapper, native])

    assert title_ttys == {record.session_id: "/dev/ttys004"}


def test_title_tty_ambiguity_fails_closed():
    one_root_two_ttys = _root("abc")
    assert _title_map([one_root_two_ttys], [
        _with_tty(_FakeProcess(70), "ttys004"),
        _with_tty(_FakeProcess(71), "ttys005"),
    ]) == {}

    first = _root("abc")
    second = _root("def")
    assert _title_map(
        [first, second], [_with_tty(_FakeProcess(72), "ttys004")]
    ) == {}


def test_title_projection_is_frozen_safe_and_decoration_only(tmp_path):
    safe = _root(path=tmp_path / "safe.jsonl", revision=(7, 8))
    safe.metrics["mutable"] = True
    child = _root("child", path=tmp_path / "child.jsonl", parent_thread_id="abc")
    app = _root("app", path=tmp_path / "app.jsonl", originator="codex-app")

    projected = codex_rollouts.project_title_roots([safe, child, app])

    assert projected == (codex_rollouts.CodexTitleRoot(
        session_id="codex:abc", thread_id="abc",
        path=tmp_path / "safe.jsonl", cwd="/code/bob",
    ),)
    assert projected[0].__dataclass_params__.frozen is True
    assert not hasattr(projected[0], "pid")
    assert not hasattr(projected[0], "process_identity")
    assert not hasattr(projected[0], "revision")
    assert not hasattr(projected[0], "metrics")


def test_three_bare_roots_in_one_cwd_map_by_open_journal(tmp_path):
    records = []
    processes = []
    expected = {}
    for index, (thread_id, tty) in enumerate((
        ("abc", "ttys004"), ("def", "ttys005"), ("ghi", "ttys006"),
    ), start=70):
        path = tmp_path / f"{thread_id}.jsonl"
        path.write_text(thread_id)
        record = _root(thread_id, path=path)
        records.append(record)
        processes.extend([
            _native_holder(index, path, tty),
            _with_tty(_FakeProcess(
                index + 100, name="node", exe="/opt/homebrew/bin/node",
                cmdline=("node", "/opt/homebrew/bin/codex"),
            ), tty),
        ])
        expected[record.session_id] = f"/dev/{tty}"

    assert _title_map(records, processes) == expected


def test_open_child_journal_never_becomes_a_title_target(tmp_path):
    root_path = tmp_path / "root.jsonl"
    child_path = tmp_path / "child.jsonl"
    root_path.write_text("root")
    child_path.write_text("child")
    root = _root("abc", path=root_path)
    child = _root("child", path=child_path, parent_thread_id="abc")
    process = _native_holder(70, root_path)
    process._open_file_reads = [(root_path, child_path)]

    assert _title_map([root, child], [process]) == {
        root.session_id: "/dev/ttys004",
    }


def test_delayed_root_metadata_maps_on_the_next_projection(tmp_path):
    path = tmp_path / "abc.jsonl"
    path.write_text("root")
    record = _root("abc", path=path, originator="")
    process = _native_holder(70, path)

    assert _title_map([record], [process]) == {}
    record.originator = "codex-tui"
    assert _title_map([record], [process]) == {
        record.session_id: "/dev/ttys004",
    }


def test_replacement_journal_is_resolved_without_persisted_state(tmp_path):
    old_path = tmp_path / "old.jsonl"
    new_path = tmp_path / "new.jsonl"
    old_path.write_text("old")
    new_path.write_text("new")
    old = _root("abc", path=old_path)
    new = _root("abc", path=new_path)

    assert _title_map([old], [_native_holder(70, old_path)]) == {
        old.session_id: "/dev/ttys004",
    }
    assert _title_map([new], [_native_holder(71, new_path, "ttys005")]) == {
        new.session_id: "/dev/ttys005",
    }


def test_canonical_symlink_works_but_hard_link_alias_fails_closed(tmp_path):
    path = tmp_path / "root.jsonl"
    symlink = tmp_path / "root-link.jsonl"
    hard_link = tmp_path / "root-hard.jsonl"
    path.write_text("root")
    symlink.symlink_to(path)
    os.link(path, hard_link)
    record = _root(path=path)

    assert _title_map([record], [_native_holder(70, symlink)]) == {
        record.session_id: "/dev/ttys004",
    }
    assert _title_map([record], [_native_holder(71, hard_link)]) == {}


def test_journal_denial_keeps_only_conservative_fallbacks(tmp_path):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    record = _root(path=path)
    denied = _with_tty(_FakeProcess(
        70, cmdline=("codex", "resume", "abc"), denied=("open_files",),
    ))

    assert _title_map([record], [denied]) == {
        record.session_id: "/dev/ttys004",
    }
    assert _title_map([record, _root("def", path=tmp_path / "def.jsonl")], [
        _with_tty(_FakeProcess(71, denied=("open_files",))),
    ]) == {}


def test_accessible_zero_or_multiple_holders_never_guess_by_cwd(tmp_path):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    other = tmp_path / "other.jsonl"
    other.write_text("other")
    record = _root(path=path)

    assert _title_map([record], [_native_holder(70, other)]) == {}
    assert _title_map([record], [
        _native_holder(71, path, "ttys004"),
        _native_holder(72, path, "ttys005"),
    ]) == {}


def test_conflicting_journal_and_exact_resume_evidence_fails_closed(tmp_path):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    record = _root(path=path)
    holder = _native_holder(70, path, "ttys004")
    conflicting = _with_tty(_FakeProcess(
        71, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/opt/homebrew/bin/codex", "resume", "abc"),
    ), "ttys005")

    assert _title_map([record], [holder, conflicting]) == {}


def test_two_roots_competing_for_one_journal_tty_fail_closed(tmp_path):
    first_path = tmp_path / "abc.jsonl"
    second_path = tmp_path / "def.jsonl"
    first_path.write_text("first")
    second_path.write_text("second")
    process = _native_holder(70, first_path)
    process._open_file_reads = [(first_path, second_path)]

    assert _title_map([
        _root("abc", path=first_path), _root("def", path=second_path),
    ], [process]) == {}


def test_process_recycle_file_close_and_inode_swap_fail_closed(tmp_path):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    record = _root(path=path)

    def recycle(process, call):
        if call == 0:
            process.fresh["create_time"] = 999.0

    recycled = _native_holder(70, path, on_open_files=recycle)
    closed = _native_holder(71, path, open_file_reads=[(path,), ()])

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("replacement")

    def swap(_process, call):
        if call == 0:
            path.unlink()
            replacement.replace(path)

    swapped = _native_holder(72, path, on_open_files=swap)

    assert _title_map([record], [recycled]) == {}
    assert _title_map([record], [closed]) == {}
    assert _title_map([record], [swapped]) == {}


@pytest.mark.parametrize("record_changes,process", [
    ({"parent_thread_id": "parent"}, _with_tty(_FakeProcess(70))),
    ({"originator": "codex-app"}, _with_tty(_FakeProcess(70))),
    ({"source_kind": "ide"}, _with_tty(_FakeProcess(70))),
    ({"thread_source": "picker"}, _with_tty(_FakeProcess(70))),
    ({}, _with_tty(_FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/code/bob/server.js"),
    ))),
    ({}, _with_tty(_FakeProcess(
        70, exe="/Applications/Codex.app/Contents/MacOS/codex",
    ))),
    ({}, _with_tty(_FakeProcess(
        70, cmdline=("codex", "app-server"),
    ))),
    ({}, _with_tty(_FakeProcess(
        70, cmdline=("codex", "mcp-server"),
    ))),
    ({}, _with_tty(_FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/opt/homebrew/bin/codex", "exec-server"),
    ))),
    ({}, _with_tty(_FakeProcess(70, cwd="/code/other"))),
    ({}, _FakeProcess(70)),
    ({}, _FakeProcess(70, denied=("terminal",))),
    ({}, _with_tty(_FakeProcess(70, denied=("exe",)))),
])
def test_unsafe_roots_and_processes_never_get_title_ttys(record_changes, process):
    record = _root(**record_changes)
    assert _title_map([record], [process]) == {}


@pytest.mark.parametrize("record_changes,process_changes", [
    ({"parent_thread_id": "parent"}, {}),
    ({"originator": "codex-app"}, {}),
    ({"source_kind": "ide"}, {}),
    ({"thread_source": "picker"}, {}),
    ({}, {"cwd": "/code/other"}),
    ({}, {"exe": "/Applications/Codex.app/Contents/MacOS/codex"}),
    ({}, {"cmdline": ("codex", "app-server", "resume", "abc")}),
    ({}, {"cmdline": ("codex", "code-mode-host", "resume", "abc")}),
    ({}, {"cmdline": ("codex", "exec-server")}),
    ({}, {"cmdline": ("codex", "--model", "gpt-5", "exec-server")}),
    ({}, {"cmdline": ("codex", "mcp-server")}),
    ({}, {"cmdline": ("codex", "remote-control")}),
    ({}, {"cmdline": ("codex", "mcp", "serve")}),
    ({}, {"denied": ("exe",)}),
])
def test_unsafe_roots_and_processes_never_attach(record_changes, process_changes):
    record = _root(**record_changes)
    values = {"cmdline": ("codex", "resume", "abc"), **process_changes}
    process = _FakeProcess(77, **values)
    codex_rollouts.attach_process_ids([record], [process])
    assert record.pid is None
    assert record.process_identity is None


def test_two_native_explicit_matches_are_ambiguous():
    record = _root()
    codex_rollouts.attach_process_ids([record], [
        _FakeProcess(77, cmdline=("codex", "resume", "abc")),
        _FakeProcess(78, cmdline=("codex", "resume", "abc")),
    ])
    assert record.pid is None


def test_identity_validator_replays_every_destructive_fact():
    from dataclasses import replace

    record = _root()
    proc = _FakeProcess(77, cmdline=("codex", "resume", "abc"))
    codex_rollouts.attach_process_ids([record], [proc])
    identity = record.process_identity
    assert codex_rollouts.validate_process_identity(
        record, identity, destructive=True, processes=[proc], records=[record])

    changes = [
        {"create_time": 101.0}, {"exe": "/other/codex"},
        {"cwd": "/code/other"}, {"cmdline": ("codex", "resume", "other")},
    ]
    for change in changes:
        candidate = _FakeProcess(
            77, **{"cmdline": ("codex", "resume", "abc"), **change})
        assert not codex_rollouts.validate_process_identity(
            record, identity, destructive=True, processes=[candidate], records=[record])
    assert not codex_rollouts.validate_process_identity(
        record, identity, destructive=True,
        processes=[_FakeProcess(78, cmdline=("codex", "resume", "abc"))],
        records=[record],
    )
    assert not codex_rollouts.validate_process_identity(
        record, replace(identity, match_kind="unique_cwd"), destructive=True,
        processes=[proc], records=[record],
    )

    fallback = _root()
    codex_rollouts.attach_process_ids([fallback], [_FakeProcess(88)])
    assert not codex_rollouts.validate_process_identity(
        fallback, fallback.process_identity, destructive=True,
        processes=[_FakeProcess(88)], records=[fallback])


def test_completed_turn_is_waiting_and_keeps_last_message(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "abc", "cwd": "/code/bob"}),
        row("event_msg", {"type": "task_started"}),
        row("response_item", {"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": "Done."}]}),
        row("event_msg", {"type": "task_complete"}),
    ])
    found = codex_rollouts.parse_rollout(path)
    assert found.activity == "waiting"
    assert found.stats.last_text == "Done."


def test_collaboration_item_tracks_only_live_agents(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "parent", "cwd": "/code/bob"}),
        row("event_msg", {"type": "item_started", "item": {
            "type": "collabAgentToolCall", "receiverThreadIds": ["child"],
            "prompt": "Inspect the Swift panel", "model": "gpt-5.6-sol",
            "agentsStates": {"child": {"status": "running"}},
        }}),
    ])
    found = codex_rollouts.parse_rollout(path)
    assert found.stats.agents["child"].description == "Inspect the Swift panel"
    assert found.stats.agents["child"].activity == "running"

    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "parent", "cwd": "/code/bob"}),
        row("event_msg", {"type": "item_completed", "item": {
            "type": "collabAgentToolCall", "receiverThreadIds": ["child"],
            "agentsStates": {"child": {"status": "completed"}},
        }}),
    ])
    assert codex_rollouts.parse_rollout(path).stats.agents == {}


def test_child_rollout_is_attached_to_parent_without_polluting_cache(tmp_path):
    parent = tmp_path / "2026/08/18/rollout-parent.jsonl"
    child = tmp_path / "2026/08/18/rollout-child.jsonl"
    write_rollout(parent, [
        row("session_meta", {"type": "session_meta", "id": "parent", "cwd": "/code/bob"}),
    ])
    write_rollout(child, [
        row("session_meta", {"type": "session_meta", "id": "child", "cwd": "/code/bob",
            "source": {"subAgent": {"thread_spawn": {
                "parent_thread_id": "parent", "depth": 1,
                "agent_nickname": "Scout", "agent_role": "explorer",
            }}}}),
        row("response_item", {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Inspect the panel"}]}),
        row("event_msg", {"type": "task_started"}),
    ])
    now = max(parent.stat().st_mtime, child.stat().st_mtime)
    codex_rollouts._CACHE.clear()
    records = codex_rollouts.load_recent(tmp_path, now=now)
    by_id = {item.thread_id: item for item in records}
    assert by_id["child"].parent_thread_id == "parent"
    agent = by_id["parent"].stats.agents["child"]
    assert agent.subagent_type == "explorer"
    assert agent.description == "Inspect the panel"
    assert agent.activity == "working"

    # The parent copy in the parse cache stays raw; attachment is rebuilt on
    # every load and cannot leave a vanished child behind.
    assert codex_rollouts._CACHE[parent][2].stats.agents == {}


def test_lowercase_child_metadata_stays_rollout_owner_when_parent_is_copied(tmp_path):
    parent = tmp_path / "2026/08/18/rollout-parent.jsonl"
    child = tmp_path / "2026/08/18/rollout-child.jsonl"
    write_rollout(parent, [
        row("session_meta", {
            "type": "session_meta", "id": "parent", "cwd": "/code/bob",
            "originator": "codex-tui", "source": "cli", "thread_source": "user",
        }),
    ])
    write_rollout(child, [
        row("session_meta", {"type": "session_meta"}),
        row("session_meta", {
            "type": "session_meta", "id": "child", "cwd": "/code/bob",
            "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": "parent", "depth": 1,
                "agent_nickname": "Scout", "agent_role": "explorer",
            }}},
        }),
        row("session_meta", {
            "type": "session_meta", "id": "parent", "cwd": "/code/bob",
            "originator": "codex-tui", "source": "cli", "thread_source": "user",
        }),
        row("response_item", {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Inspect the panel"}]}),
        row("event_msg", {"type": "task_started"}),
    ])
    os.utime(parent, (1000, 1000))
    os.utime(child, (1001, 1001))

    parsed = codex_rollouts.parse_rollout(child)
    assert parsed is not None
    assert (parsed.thread_id, parsed.session_id) == ("child", "codex:child")
    assert parsed.source_kind == "subagent"
    assert parsed.kind == "background"
    assert (
        parsed.parent_thread_id,
        parsed.agent_nickname,
        parsed.agent_role,
        parsed.agent_depth,
    ) == ("parent", "Scout", "explorer", 1)

    codex_rollouts._CACHE.clear()
    records = codex_rollouts.load_recent(tmp_path, now=1001)
    by_id = {item.thread_id: item for item in records}
    assert set(by_id) == {"parent", "child"}
    assert by_id["parent"].stats.agents["child"].subagent_type == "explorer"


def test_load_recent_ignores_stale_and_half_written_files(tmp_path):
    fresh = tmp_path / "2026/08/18/rollout-fresh.jsonl"
    stale = tmp_path / "2026/08/17/rollout-stale.jsonl"
    rows = [row("session_meta", {"type": "session_meta", "id": "fresh", "cwd": "/code/bob"})]
    write_rollout(fresh, rows)
    write_rollout(stale, rows[:-1] + [row("session_meta", {"type": "session_meta", "id": "stale"})])
    os.utime(fresh, (1000, 1000))
    os.utime(stale, (100, 100))
    fresh.write_text(fresh.read_text() + "{unfinished")
    os.utime(fresh, (1000, 1000))
    found = codex_rollouts.load_recent(tmp_path, now=1100, grace=200)
    assert [item.session_id for item in found] == ["codex:fresh"]


def test_unchanged_rollout_is_not_reparsed(tmp_path, monkeypatch):
    path = tmp_path / "2026/08/18/rollout-one.jsonl"
    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "abc", "cwd": "/code/bob"})
    ])
    now = path.stat().st_mtime
    real = codex_rollouts.parse_rollout
    calls = []

    def counted(candidate):
        calls.append(candidate)
        return real(candidate)

    codex_rollouts._CACHE.clear()
    monkeypatch.setattr(codex_rollouts, "parse_rollout", counted)
    first = codex_rollouts.load_recent(tmp_path, now=now)
    second = codex_rollouts.load_recent(tmp_path, now=now)
    assert calls == [path]
    stat_result = path.stat()
    assert first[0].revision == (stat_result.st_mtime_ns, stat_result.st_size)
    first[0].metrics["mutated"] = True
    assert "mutated" not in second[0].metrics
    assert "mutated" not in codex_rollouts._CACHE[path][2].metrics


def test_load_recent_keeps_title_resolution_transient(tmp_path, monkeypatch):
    path = tmp_path / "2026/08/18/rollout-one.jsonl"
    write_rollout(path, [
        row("session_meta", {
            "type": "session_meta", "id": "abc", "cwd": "/code/bob",
            "originator": "codex-tui", "source": "cli",
            "thread_source": "user",
        }),
    ])
    wrapper = _with_tty(_FakeProcess(
        70, name="node", exe="/opt/homebrew/bin/node",
        cmdline=("node", "/opt/homebrew/bin/codex", "resume", "abc"),
    ))
    monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: [wrapper])
    codex_rollouts._CACHE.clear()

    record = codex_rollouts.load_recent(
        tmp_path, now=path.stat().st_mtime
    )[0]

    assert not hasattr(record, "title_tty")
    assert _title_map([record], [wrapper]) == {
        record.session_id: "/dev/ttys004",
    }
    assert record.pid is None and record.process_identity is None


def test_daemon_snapshot_includes_codex_as_read_only_provider(monkeypatch):
    stats = codex_rollouts.SessionStats(
        model="gpt-5.6-sol", models=["gpt-5.6-sol"], last_text="Ready for review",
        cwd="/code/bob",
    )
    record = codex_rollouts.CodexRecord(
        session_id="codex:abc", thread_id="abc", path=Path("rollout.jsonl"),
        cwd="/code/bob", title="Add Codex support", started_at=100,
        last_event=200, activity="waiting", stats=stats,
    )
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [record])
    daemon = BobDaemon()
    snapshot = daemon.detailed_snapshot()
    entry = snapshot["waiting"][0]
    assert entry["session_id"] == "codex:abc"
    assert entry["provider"] == "codex"
    assert entry["name"] == "Add Codex support"
    assert entry["stats"]["model"] == "gpt-5.6-sol"
    assert entry["last_text"] == "Ready for review"
    assert entry["channel"] is False
    assert entry["can_type"] is False
    assert {key: entry[key] for key in
            ("can_stop", "can_jump", "can_hide", "can_resume")} == {
        "can_stop": False, "can_jump": False,
        "can_hide": True, "can_resume": False,
    }


def test_daemon_nests_codex_child_and_counts_it_once(monkeypatch):
    parent_stats = codex_rollouts.SessionStats()
    parent_stats.agents["child"] = codex_rollouts.AgentInfo(
        agent_id="child", description="Inspect the panel",
        subagent_type="Scout", activity="working",
    )
    parent = codex_rollouts.CodexRecord(
        session_id="codex:parent", thread_id="parent", path=Path("parent.jsonl"),
        cwd="/code/bob", title="Integrate Codex", last_event=200,
        activity="waiting", stats=parent_stats,
    )
    child = codex_rollouts.CodexRecord(
        session_id="codex:child", thread_id="child", path=Path("child.jsonl"),
        cwd="/code/bob", title="Inspect the panel", last_event=200,
        activity="working", parent_thread_id="parent",
    )
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: [parent, child])
    daemon = BobDaemon()
    snapshot = daemon.detailed_snapshot()
    rows = [row for group in ("running", "sleeping", "waiting")
            for row in snapshot[group]]
    assert not any(row["session_id"] == "codex:child" for row in rows)
    root = next(row for row in rows if row["session_id"] == "codex:parent")
    assert root["subagent_rows"] == [{
        "agent_id": "child", "subagent_type": "Scout",
        "description": "Inspect the panel", "model": "",
        "activity": "working", "depth": 0, "parent_agent_id": "",
    }]
    assert daemon._activity_counts()["subagents"] >= 1


def test_non_numeric_usage_value_does_not_abort_the_parse(tmp_path):
    """One `"input_tokens": "lots"` in a token_count line used to raise
    ValueError out of parse_rollout and load_recent, sticking the whole Codex
    roster on that journal. A bad figure reads as 0; the others survive."""
    path = tmp_path / "2026/08/18/rollout-bad-usage.jsonl"
    write_rollout(path, [
        row("session_meta", {"type": "session_meta", "id": "bad", "cwd": "/code/bob",
                             "originator": "codex-tui", "source": "cli",
                             "thread_source": "user"}),
        row("event_msg", {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": "lots", "cached_input_tokens": 80,
            "cache_write_input_tokens": None, "output_tokens": 20}}}),
    ])
    found = codex_rollouts.parse_rollout(path)
    assert found is not None
    assert found.stats.input_tokens == 0
    assert found.stats.cache_read_tokens == 80
    assert found.stats.output_tokens == 20
    assert found.stats.cache_creation_tokens == 0


def _parity_rollout(tmp_path, events):
    path = tmp_path / '2026/09/05/rollout-parity.jsonl'
    write_rollout(path, [row('session_meta', {'id': 'parity', 'cwd': '/code/bob'}),
                         row('event_msg', {'type': 'task_started'}), *events])
    return codex_rollouts.parse_rollout(path)


def _parity_call(name, call_id='ask', **payload):
    return row('response_item', {'type': 'function_call', 'name': name,
                                 'call_id': call_id, **payload})


def _parity_message(text):
    return row('response_item', {'type': 'message', 'role': 'assistant',
                               'content': [{'type': 'output_text', 'text': text}]})


@pytest.mark.parametrize('key', ['input', 'arguments'])
@pytest.mark.parametrize('encoded', [False, True])
def test_codex_pending_question_payload_and_snapshot(tmp_path, monkeypatch, key, encoded):
    payload = {'questions': [{'question': 'Choose a route?', 'header': 'Route',
                             'options': [{'label': 'First'}, {'label': 'Second'}]},
                            {'question': 'Choose a colour?', 'options': [{'label': 'Blue'}]}]}
    record = _parity_rollout(tmp_path, [
        _parity_call('request_user_input', **{key: json.dumps(payload) if encoded else payload}),
        row('response_item', {'type': 'function_call_output', 'call_id': 'unrelated', 'output': '{}'}),
    ])
    assert record.activity == 'waiting'
    assert record.stats.question['id'] == 'ask'
    assert record.stats.question['options'] == ['First', 'Second']
    assert len(record.stats.questions) == 2
    daemon = BobDaemon(headless=True)
    daemon._codex_records[record.session_id] = record
    monkeypatch.setattr(daemon, '_refresh_codex_records', lambda: None)
    monkeypatch.setattr(daemon, '_session_reachable', lambda sid: False)
    snapshot = daemon._enrich_agent_stubs(daemon._collect_agent_stubs())
    entry = next(e for bucket in snapshot.values() for e in bucket if e['session_id'] == record.session_id)
    assert entry['question']['text'] == 'Choose a route?'
    assert entry['questions'][1]['index'] == 1
    assert not entry['can_type'] and not entry['channel']
    assert 'original Codex session' in entry['interaction_note']


@pytest.mark.parametrize('ending', ['turn_aborted', 'task_complete', 'task_completed'])
@pytest.mark.parametrize('with_text', [False, True])
def test_codex_terminal_turn_clears_tools_and_questions(tmp_path, ending, with_text):
    events = [_parity_call('request_user_input', arguments={'questions': [{'question': 'Continue?'}]})]
    if with_text:
        events.append(_parity_message('Some output'))
    events.append(row('event_msg', {'type': ending}))
    record = _parity_rollout(tmp_path, events)
    assert record.activity == 'waiting'
    assert record.current_tool == ''
    assert record.stats.question == {} and record.stats.questions == []
    recovered = _parity_rollout(tmp_path, [*events, row('event_msg', {'type': 'task_started'})])
    assert recovered.activity == 'working'
    assert recovered.stats.question == {}


@pytest.mark.parametrize('ending', [
    row('response_item', {'type': 'function_call_output', 'call_id': 'ask', 'output': '{}'}),
    row('event_msg', {'type': 'task_started'}),
])
def test_codex_matching_question_result_and_new_turn_clear(tmp_path, ending):
    record = _parity_rollout(tmp_path, [
        _parity_call('request_user_input', arguments={'questions': [{'question': 'Continue?'}]}), ending])
    assert record.stats.question == {} and record.stats.questions == []
    assert record.activity == 'working'


@pytest.mark.parametrize('value', ['broken', '[]', [], None, 42, {'questions': [{'question': 'Q', 'options': 5}]}])
def test_codex_malformed_question_is_not_a_wait(tmp_path, value):
    record = _parity_rollout(tmp_path, [_parity_call('request_user_input', arguments=value)])
    assert record.activity == 'working'
    assert record.stats.question == {}


def test_codex_markers_are_bounded_stripped_and_clear(tmp_path):
    marked = 'Output. <!-- bob-tldr:  A short summary  --> <!-- bob-actions: Yes | Yes | No | Extra -->'
    record = _parity_rollout(tmp_path, [_parity_message(marked)])
    assert record.stats.last_summary == 'A short summary'
    assert record.stats.last_actions == ['Yes', 'No', 'Extra']
    assert 'bob-' not in record.stats.last_text
    later = _parity_rollout(tmp_path, [_parity_message(marked), _parity_message('Later output.')])
    assert later.stats.last_summary == '' and later.stats.last_actions == []


@pytest.mark.parametrize('output,expected', [
    ('{"agent_id":"helper-real"}', ['helper-real']),
    ({'agent_id': 'helper-real'}, ['helper-real']),
    ('{"error":"failed"}', []), ('broken', []),
])
def test_codex_spawn_identity_comes_from_matching_result(tmp_path, output, expected):
    events = [_parity_call('spawn_agent', 'spawn-call', arguments=json.dumps({
        'message': 'Review the implementation', 'task_name': 'reviewer', 'model': 'test-model'}))]
    pending = _parity_rollout(tmp_path, events)
    assert pending.stats.agents == {}
    events.append(row('response_item', {'type': 'function_call_output', 'call_id': 'spawn-call', 'output': output}))
    record = _parity_rollout(tmp_path, events)
    assert list(record.stats.agents) == expected
    if expected:
        helper = record.stats.agents['helper-real']
        assert helper.description == 'Review the implementation'
        assert helper.subagent_type == 'Codex agent'  # task name is not a role
        assert helper.model == 'test-model'


@pytest.mark.parametrize('ending', ['turn_aborted', 'task_complete'])
def test_codex_late_end_for_old_turn_keeps_current_question(tmp_path, ending):
    record = _parity_rollout(tmp_path, [
        row('event_msg', {'type': 'task_started', 'turn_id': 'new'}),
        _parity_call('request_user_input', arguments={'questions': [{'question': 'Current?'}]}),
        row('event_msg', {'type': ending, 'turn_id': 'old'}),
    ])
    assert record.stats.question['text'] == 'Current?'
    assert record.activity == 'waiting'


def test_codex_question_beats_live_helpers_in_shared_category(tmp_path, monkeypatch):
    record = _parity_rollout(tmp_path, [
        _parity_call('spawn_agent', 'spawn', arguments={'message': 'Inspect'}),
        row('response_item', {'type': 'function_call_output', 'call_id': 'spawn',
                              'output': '{"agent_id":"child"}'}),
        _parity_call('request_user_input', arguments={'questions': [{'question': 'Continue?'}]}),
    ])
    daemon = BobDaemon(headless=True)
    daemon._codex_records[record.session_id] = record
    monkeypatch.setattr(daemon, '_refresh_codex_records', lambda: None)
    assert daemon._reconciled_categories()[record.session_id] == 'waiting'
    assert daemon._activity_counts()['attention'] == 1


def test_codex_spawn_reconciles_child_journal_without_call_id_phantom(tmp_path):
    record = _parity_rollout(tmp_path, [
        _parity_call('spawn_agent', 'spawn', arguments={'message': 'Review'}),
        row('response_item', {'type': 'function_call_output', 'call_id': 'spawn',
                              'output': '{"agent_id":"child"}'}),
    ])
    child = record.path.with_name('rollout-child.jsonl')
    write_rollout(child, [row('session_meta', {'id': 'child', 'cwd': '/code/bob',
        'source': {'subagent': {'thread_spawn': {'parent_thread_id': 'parity', 'agent_nickname': 'Reviewer'}}}}),
        row('response_item', {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': 'Review the panel'}]})])
    records = codex_rollouts.load_recent(tmp_path)
    parent = next(r for r in records if r.thread_id == 'parity')
    assert list(parent.stats.agents) == ['child']
    assert parent.stats.agents['child'].description == 'Review the panel'
    assert parent.stats.agents['child'].subagent_type == 'Codex agent'  # nickname is not a role


def test_codex_completed_spawn_leaves_no_phantom_helper(tmp_path):
    record = _parity_rollout(tmp_path, [
        _parity_call('spawn_agent', 'spawn', arguments={'message': 'Review'}),
        row('response_item', {'type': 'function_call_output', 'call_id': 'unrelated', 'output': '{}'}),
        row('response_item', {'type': 'function_call_output', 'call_id': 'spawn',
                              'output': '{"agent_id":"child"}'}),
        row('event_msg', {'type': 'item_completed', 'item': {'type': 'collabAgentToolCall',
                             'receiverThreadIds': ['child'], 'agentsStates': {'child': {'status': 'completed'}}}}),
    ])
    assert record.stats.agents == {}


def test_codex_question_and_marker_bounds_reuse_shared_limits(tmp_path):
    from dark_army_daemon import session_stats as ss
    record = _parity_rollout(tmp_path, [
        _parity_message('Output <!-- bob-tldr: ' + 'a' * 500 + ' --> <!-- bob-actions: ' + 'b' * 50 + ' | Yes -->'),
        _parity_call('request_user_input', arguments={'questions': [{
            'question': 'q' * 2000, 'options': [{'label': 'o' * 500}]}]}),
    ])
    assert len(record.stats.last_summary) == ss.MAX_SUMMARY_CHARS
    assert record.stats.last_actions == []  # one overlong label refuses the whole offer
    assert len(record.stats.question['text']) <= ss.MAX_QUESTION_CHARS + 1
    assert len(record.stats.question['options'][0]) <= ss.MAX_OPTION_CHARS + 1


@pytest.mark.parametrize("ownership", ["resume", "holder"])
def test_exact_sibling_ownership_cannot_keep_closed_root_live(tmp_path, ownership):
    closed, sibling = [_root(name, path=tmp_path / f"{name}.jsonl")
                       for name in ("closed", "sibling")]
    for record in (closed, sibling):
        record.path.write_text(record.thread_id)
    process = _FakeProcess(77, open_files=(sibling.path,),
                           cmdline=("codex", "resume", "sibling")
                           if ownership == "resume" else ("codex",))
    codex_rollouts.attach_process_ids([closed, sibling], [process])
    assert closed.process_seen is False
    assert sibling.process_seen is True
    if ownership == "holder":
        assert all(record.pid is None and record.process_identity is None
                   for record in (closed, sibling))


@pytest.mark.parametrize("denied", ["exe", "cwd", "cmdline", "create_time", "open_files"])
def test_unreadable_relevant_candidate_is_unknown(tmp_path, denied):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    record = _root(path=path)
    codex_rollouts.attach_process_ids([record], [_FakeProcess(77, denied=(denied,))])
    assert record.process_seen is None


@pytest.mark.parametrize("race", ["pid", "cwd", "argv", "journal", "fd"])
def test_changing_owner_is_unknown(tmp_path, race):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    record = _root(path=path)
    def change(proc, index):
        if index:
            return
        if race == "pid":
            proc.fresh["create_time"] += 1
        elif race == "cwd":
            proc.fresh["cwd"] = "/elsewhere"
        elif race == "argv":
            proc.fresh["cmdline"] = ["codex", "resume", "other"]
        elif race == "journal":
            replacement = tmp_path / "replacement"
            replacement.write_text("new inode")
            replacement.replace(path)
    process = _FakeProcess(77, open_files=(path,), on_open_files=change,
                           open_file_reads=[(path,), ()] if race == "fd" else None)
    codex_rollouts.attach_process_ids([record], [process])
    assert record.process_seen is None


def test_process_scan_health_and_single_enumeration(tmp_path, monkeypatch):
    path = tmp_path / "2026/09/05/rollout-root.jsonl"
    write_rollout(path, [row("session_meta", {"id": "abc", "cwd": "/code/bob",
        "originator": "codex-tui", "source": "cli", "thread_source": "user"})])
    calls = []
    def empty(attrs):
        calls.append(attrs)
        return iter(())
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", empty)
    assert codex_rollouts.load_recent(tmp_path)[0].process_seen is False
    assert len(calls) == 1
    def failed(attrs):
        raise PermissionError("process table unavailable")
    monkeypatch.setattr(codex_rollouts.psutil, "process_iter", failed)
    # Past the shared reading's TTL: the next load scans again.
    codex_rollouts.invalidate_process_snapshot()
    assert codex_rollouts._process_snapshot() is None
    assert codex_rollouts.load_recent(tmp_path)[0].process_seen is None
    assert codex_rollouts._title_processes() == []
    assert codex_rollouts._CACHE[path][2].process_seen is None


def test_liveness_open_file_reads_are_shared_and_titles_are_irrelevant(tmp_path):
    records = [_root(str(i), path=tmp_path / f"{i}.jsonl") for i in range(8)]
    for record in records:
        record.path.write_text(record.thread_id)
    holder = _FakeProcess(77, open_files=(records[0].path,), denied=("terminal",))
    wrapper = _FakeProcess(78, name="node", exe="/opt/node",
                           cmdline=("node", "/opt/bin/codex"))
    unrelated = _FakeProcess(79, name="launchd", exe="/sbin/launchd", denied=("cwd",))
    codex_rollouts.attach_process_ids(records, iter([wrapper, holder, unrelated]))
    assert [r.process_seen for r in records] == [True] + [False] * 7
    assert holder._open_file_calls == 2
    assert all(r.process_identity is None for r in records)


@pytest.mark.parametrize("changes", [{"parent_thread_id": "parent"},
    {"source_kind": "ide"}, {"originator": "codex-app"}, {"thread_source": "picker"}])
def test_non_safe_roots_have_unknown_liveness_even_with_empty_scan(changes):
    record = _root(**changes)
    codex_rollouts.attach_process_ids([record], [])
    assert record.process_seen is None


def test_child_journal_and_app_server_do_not_prove_root_presence(tmp_path):
    parent_path, child_path = tmp_path / "root.jsonl", tmp_path / "child.jsonl"
    parent_path.write_text("root")
    child_path.write_text("child")
    root = _root(path=parent_path)
    child = _root("child", path=child_path, parent_thread_id="abc")
    codex_rollouts.attach_process_ids([root, child], [
        _FakeProcess(77, open_files=(child_path,)),
        _FakeProcess(78, cmdline=("codex", "app-server"), open_files=(parent_path,))])
    assert root.process_seen is False
    assert child.process_seen is None


def test_conflicting_resume_and_holder_identity_is_unknown(tmp_path):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    record = _root(path=path)
    codex_rollouts.attach_process_ids([record], [
        _FakeProcess(77, cmdline=("codex", "resume", "other"), open_files=(path,))])
    assert record.process_seen is None


def test_exact_resume_is_positive_without_a_journal_fd(tmp_path):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    record = _root(path=path)
    sibling = _root("other", path=tmp_path / "other.jsonl")
    sibling.path.write_text("other")
    proc = _FakeProcess(77, cmdline=("codex", "resume", "abc"))
    codex_rollouts.attach_process_ids([record, sibling], [proc])
    assert record.process_seen is True
    assert sibling.process_seen is False
    proc.fresh["create_time"] += 1
    codex_rollouts.attach_process_ids([record, sibling], [proc])
    assert record.process_seen is None


def test_transient_stat_failure_of_held_root_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / "root.jsonl"
    path.write_text("root")
    record = _root(path=path)
    process = _FakeProcess(77, open_files=(path,))
    original = codex_rollouts._journal_identity
    calls = []
    def intermittent(candidate):
        calls.append(candidate)
        return None if len(calls) == 2 else original(candidate)
    monkeypatch.setattr(codex_rollouts, "_journal_identity", intermittent)
    codex_rollouts.attach_process_ids([record], [process])
    assert record.process_seen is None


def test_fresh_liveness_identity_reopens_psutil_cached_process(monkeypatch):
    process = codex_rollouts.psutil.Process(os.getpid())
    created = process.create_time()
    monkeypatch.setattr(type(process._proc), "create_time", lambda self, **kwargs: created + 100)
    assert process.create_time() == created  # psutil caches this on the instance
    assert codex_rollouts._fresh_liveness_identity(process).create_time == created + 100


def _navigation_fixture(tmp_path, monkeypatch, count=2):
    """Concurrent native TUIs with exact journals, never real processes."""
    roots, processes = [], []
    for i in range(count):
        path = tmp_path / f"nav-{i}.jsonl"
        path.write_text("root")
        roots.append(_root(f"nav-{i}", path=path))
        processes.append(_native_holder(701 + i, path, f"ttys{40 + i:03}"))
    monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: processes)
    codex_rollouts.attach_process_ids(roots, processes)
    return roots, processes


@pytest.mark.parametrize("count", [2, 8])
def test_navigation_exact_holders_do_not_promote_control(tmp_path, monkeypatch, count):
    roots, processes = _navigation_fixture(tmp_path, monkeypatch, count)
    proofs = codex_rollouts.resolve_navigation_proofs(_title_roots(roots), processes)
    assert {sid: p.pid for sid, p in proofs.items()} == {r.session_id: p.pid for r, p in zip(roots, processes)}
    assert all(r.pid is None and r.process_identity is None for r in roots)
    targets = codex_rollouts.navigation_targets(codex_rollouts.project_navigation_roots(roots), proofs)
    assert set(targets) == set(proofs)


@pytest.mark.parametrize("fault", ["same_pid", "same_tty", "two_holders", "two_roots", "duplicate_root", "denied", "wrong_path", "hardlink", "resume", "competing_resume", "argv", "cwd", "created", "tty", "exe", "replace", "empty", "failed", "child", "app", "node"])
def test_navigation_uncertainty_withdraws_exact_proof(tmp_path, monkeypatch, fault):
    roots, processes = _navigation_fixture(tmp_path, monkeypatch)
    first = roots[0]
    if fault == "same_pid":
        processes[1].pid = processes[1].info["pid"] = 701
    elif fault == "same_tty":
        _with_tty(processes[1], "ttys040")
    elif fault == "two_holders":
        processes.append(_native_holder(799, first.path, "ttys099"))
    elif fault == "two_roots":
        processes[0]._open_file_reads = [(first.path, roots[1].path)]
        processes.pop()
    elif fault == "duplicate_root":
        roots.append(first)
    elif fault == "denied":
        processes.append(_native_holder(799, first.path, "ttys099", denied=("create_time",)))
    elif fault in ("wrong_path", "hardlink"):
        alias = tmp_path / "alias.jsonl"
        if fault == "hardlink":
            os.link(first.path, alias)
        else:
            alias.write_text("unrelated")
        processes[0]._open_file_reads = [(alias,)]
    elif fault == "resume":
        processes[0].info["cmdline"] = processes[0].fresh["cmdline"] = ["codex", "resume", "other"]
    elif fault == "competing_resume":
        processes.append(_native_holder(799, roots[1].path, "ttys099", cmdline=("codex", "resume", first.thread_id)))
    elif fault in ("argv", "cwd", "created", "tty", "exe"):
        key, value = {"argv": ("cmdline", ["codex", "--new"]), "cwd": ("cwd", "/else"),
                      "created": ("create_time", 999), "tty": ("terminal", "ttys099"), "exe": ("exe", "/other/codex")}[fault]
        processes[0].fresh[key] = value
    elif fault == "replace":
        def replace(proc, _index):
            other = tmp_path / "replacement"
            other.write_text("new")
            other.replace(first.path)
        processes[0]._on_open_files = replace
    elif fault == "empty":
        processes.clear()
    elif fault == "failed":
        monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: None)
    elif fault == "child":
        first.parent_thread_id = "parent"
    elif fault == "app":
        processes[0].info["cmdline"] = processes[0].fresh["cmdline"] = ["codex", "app-server"]
    elif fault == "node":
        processes[0].info["exe"] = processes[0].fresh["exe"] = "/opt/node"
        processes[0].info["cmdline"] = processes[0].fresh["cmdline"] = ["node", "/opt/codex.js"]
    assert first.session_id not in codex_rollouts.resolve_navigation_proofs(_title_roots(roots))


def test_navigation_root_and_child_journal_only_select_root(tmp_path, monkeypatch):
    roots, processes = _navigation_fixture(tmp_path, monkeypatch)
    child_path = tmp_path / "child.jsonl"
    child_path.write_text("child")
    child = _root("child", path=child_path, parent_thread_id=roots[0].thread_id)
    processes[0]._open_file_reads = [(roots[0].path, child_path)]
    proofs = codex_rollouts.resolve_navigation_proofs(_title_roots([*roots, child]))
    assert set(proofs) == {r.session_id for r in roots}


def test_navigation_ignores_known_kernel_process_with_denied_executable(tmp_path, monkeypatch):
    roots, processes = _navigation_fixture(tmp_path, monkeypatch)
    processes.append(_FakeProcess(999, name="kernel_task", denied=("exe", "cwd")))
    assert set(codex_rollouts.resolve_navigation_proofs(_title_roots(roots))) == {r.session_id for r in roots}


def test_navigation_fresh_process_reopens_cached_psutil_identity(monkeypatch):
    process = codex_rollouts.psutil.Process(os.getpid())
    created = process.create_time()
    monkeypatch.setattr(type(process._proc), "create_time", lambda self, **kwargs: created + 100)
    monkeypatch.setattr(type(process), "exe", lambda self: "/opt/codex")
    monkeypatch.setattr(type(process), "cmdline", lambda self: ["codex"])
    monkeypatch.setattr(type(process), "terminal", lambda self: "ttys040")
    assert process.create_time() == created
    assert codex_rollouts._fresh_title_process(process).create_time == created + 100


@pytest.mark.parametrize('complete', ['task_complete', 'task_completed', 'turn_aborted'])
def test_private_turn_scope_survives_completion_and_changes_on_new_turn(tmp_path, complete):
    path = tmp_path / 'turn-scope.jsonl'
    rows = [row('session_meta', {'id': 'abc', 'cwd': '/code/bob', 'originator': 'codex-tui',
                               'source': 'cli', 'thread_source': 'user'}),
            row('event_msg', {'type': 'task_started', 'turn_id': 'first'}),
            row('event_msg', {'type': complete, 'turn_id': 'first'})]
    write_rollout(path, rows)
    record = codex_rollouts.parse_rollout(path)
    assert record.turn_id == 'first' and record.turn_active is False
    rows.append(row('event_msg', {'type': 'task_started', 'turn_id': 'second'}))
    write_rollout(path, rows)
    record = codex_rollouts.parse_rollout(path)
    assert record.turn_id == 'second' and record.turn_active is True


@pytest.mark.parametrize('fault', ['question', 'changed_turn', 'missing_turn', 'wrong_inode', 'two_holders'])
def test_refinement_observation_refuses_fresh_journal_or_holder_change(tmp_path, monkeypatch, fault):
    path = tmp_path / 'close-root.jsonl'
    rows = [row('session_meta', {'id': 'abc', 'cwd': '/code/bob', 'originator': 'codex-tui',
                               'source': 'cli', 'thread_source': 'user'}),
            row('event_msg', {'type': 'task_started', 'turn_id': 'first'})]
    write_rollout(path, rows)
    record = codex_rollouts.parse_rollout(path)
    root = _title_roots([record])[0]
    processes = [_native_holder(701, path)]
    monkeypatch.setattr(codex_rollouts, '_process_snapshot', lambda: processes)
    journal = codex_rollouts._journal_identity(path)
    assert codex_rollouts.refinement_close_observation((root,), root, journal, 'first')
    if fault == 'question':
        rows.append(row('response_item', {'type': 'function_call', 'name': 'request_user_input',
         'call_id': 'q1', 'arguments': {'questions': [{'id': 'choice', 'header': 'Pick',
           'question': 'Which?', 'options': [{'label': 'A', 'description': 'A'}]}]}}))
    elif fault in ('changed_turn', 'missing_turn'):
        rows.append(row('event_msg', {'type': 'task_started',
                     'turn_id': 'next' if fault == 'changed_turn' else ''}))
    elif fault == 'wrong_inode': journal = (journal[0], journal[1], journal[2] + 1)
    else: processes.append(_native_holder(702, path, 'ttys005'))
    write_rollout(path, rows)
    assert codex_rollouts.refinement_close_observation((root,), root, journal, 'first') is None


def test_process_attachment_pairs_by_nearest_preceding_start():
    """A folder used twice inside the live window still names both pids.

    `unique_cwd` cannot fire with two rollouts and two processes in one
    directory, which is the ordinary state of a project somebody dispatches
    Codex cards into — and a pid-less record is one Dark Army cannot attribute a
    board verb to, nor prove came out of the terminal it opened.
    """
    first = _root("abc", started_at=110.0)
    second = _root("def", started_at=310.0)
    codex_rollouts.attach_process_ids(
        [first, second],
        [_FakeProcess(88, create_time=100.0),
         _FakeProcess(99, create_time=300.0)])
    assert (first.pid, second.pid) == (88, 99)
    assert first.process_identity.match_kind == "nearest_start"
    assert second.process_identity.match_kind == "nearest_start"


def test_nearest_start_grants_no_process_control():
    """A paired pid is attribution data, never a button. The control helper
    admits two match kinds and this is not one of them."""
    record = _root("abc", started_at=110.0)
    process = _FakeProcess(88, create_time=100.0)
    codex_rollouts.attach_process_ids([record], [process])
    assert record.process_identity.match_kind == "unique_cwd"

    other = _root("def", started_at=310.0)
    for item in (record, other):
        item.pid = None
        item.process_identity = None
    later = _FakeProcess(99, create_time=300.0)
    codex_rollouts.attach_process_ids([record, other], [process, later])
    assert other.process_identity.match_kind == "nearest_start"
    assert codex_rollouts.matching_process_identity(
        other, other.process_identity, destructive=False,
        processes=[process, later], records=[record, other]) is None


def test_nearest_start_refuses_two_rollouts_behind_one_process():
    """Somebody started a second thread inside one Codex window: neither
    rollout can claim that process, so neither is paired."""
    first = _root("abc", started_at=110.0)
    second = _root("def", started_at=150.0)
    codex_rollouts.attach_process_ids(
        [first, second], [_FakeProcess(88, create_time=100.0)])
    assert first.pid is None and second.pid is None


def test_nearest_start_refuses_a_process_that_started_after_the_rollout():
    """Ordering is the rule: a process younger than the journal did not
    write it, and one far older is a different session entirely."""
    late = _root("abc", started_at=90.0)
    codex_rollouts.attach_process_ids([late], [
        _FakeProcess(88, create_time=100.0),
        _FakeProcess(99, cwd="/code/bob", create_time=100.0)])
    assert late.pid is None

    stale = _root("def", started_at=100.0 + codex_rollouts.NEAREST_START_SECONDS + 1)
    codex_rollouts.attach_process_ids([stale], [
        _FakeProcess(88, create_time=100.0),
        _FakeProcess(99, create_time=100.0)])
    assert stale.pid is None


# ── the close-out "left open" rung ──────────────────────────────────────────
#
# A Codex plan or scout run that ends on close-out.sh's "left open" line is
# finished and waiting on nobody: it sleeps instead of banking under Needs
# you. Narrow on purpose — a `## Work done` report alone still asks, since
# Needs you is the only flag an unchecked Codex build gets.

_LEFT_OPEN = ('Filed the card; the plan is attached.\n\n'
              'close-out: terminal left open; close it in Dark Army when you have read it.')


def _parity_user(text):
    return row('response_item', {'type': 'message', 'role': 'user',
                                 'content': [{'type': 'input_text', 'text': text}]})


def _category_daemon(record, monkeypatch):
    daemon = BobDaemon(headless=True)
    daemon._codex_records[record.session_id] = record
    monkeypatch.setattr(daemon, '_refresh_codex_records', lambda: None)
    return daemon


def test_codex_left_open_line_sleeps_not_waits(tmp_path, monkeypatch):
    record = _parity_rollout(tmp_path, [
        _parity_message(_LEFT_OPEN),
        row('event_msg', {'type': 'task_complete'}),
    ])
    assert record.left_open is True
    daemon = _category_daemon(record, monkeypatch)
    assert daemon._reconciled_categories()[record.session_id] == 'sleeping'
    assert daemon._activity_counts()['attention'] == 0
    stub = next(s for s in daemon._collect_agent_stubs()
                if s['session_id'] == record.session_id)
    assert stub['state'] == 'idle'
    assert 'left_open' not in stub


def test_codex_next_prompt_clears_left_open(tmp_path, monkeypatch):
    record = _parity_rollout(tmp_path, [
        _parity_message(_LEFT_OPEN),
        row('event_msg', {'type': 'task_complete'}),
        _parity_user('Now also rename the card.'),
    ])
    assert record.left_open is False
    daemon = _category_daemon(record, monkeypatch)
    assert daemon._reconciled_categories()[record.session_id] != 'sleeping'


def test_codex_left_open_with_a_question_still_waits(tmp_path, monkeypatch):
    record = _parity_rollout(tmp_path, [
        _parity_message(_LEFT_OPEN),
        _parity_call('request_user_input', arguments={'questions': [{'question': 'Close it?'}]}),
    ])
    assert record.left_open is True and record.stats.question
    daemon = _category_daemon(record, monkeypatch)
    assert daemon._reconciled_categories()[record.session_id] == 'waiting'
    assert daemon._activity_counts()['attention'] == 1


def test_codex_work_report_alone_still_waits(tmp_path, monkeypatch):
    record = _parity_rollout(tmp_path, [
        _parity_message('Built it.\n\n## Work done\n**Unchecked:** 1. Open the app.'),
        row('event_msg', {'type': 'task_complete'}),
    ])
    assert record.left_open is False
    daemon = _category_daemon(record, monkeypatch)
    assert daemon._reconciled_categories()[record.session_id] == 'waiting'
    assert daemon._activity_counts()['attention'] == 1


# ── one shared process reading per PROCESS_SNAPSHOT_TTL_SECONDS ─────────────

def _counting_scan(monkeypatch, result):
    calls = []

    def scan():
        calls.append(1)
        return result() if callable(result) else result
    monkeypatch.setattr(codex_rollouts, "_process_snapshot", scan)
    return calls


def test_shared_scan_serves_all_three_callers_inside_the_ttl(tmp_path, monkeypatch):
    path = tmp_path / "shared.jsonl"
    path.write_text("root")
    root = _root("shared", path=path)
    calls = _counting_scan(monkeypatch, lambda: [_native_holder(701, path)])
    codex_rollouts.resolve_navigation_proofs(_title_roots([root]))
    codex_rollouts.resolve_title_ttys(_title_roots([root]))
    codex_rollouts.attach_process_ids([root])
    assert len(calls) == 1
    assert root.process_seen is True


def test_shared_scan_walks_again_once_the_ttl_lapses(monkeypatch):
    calls = _counting_scan(monkeypatch, lambda: [])
    assert codex_rollouts.process_snapshot_shared() == []
    assert codex_rollouts.process_snapshot_shared() == []
    assert len(calls) == 1
    stamp, observed = codex_rollouts._PROCESS_SNAPSHOT
    monkeypatch.setattr(codex_rollouts, "_PROCESS_SNAPSHOT", (
        stamp - codex_rollouts.PROCESS_SNAPSHOT_TTL_SECONDS, observed))
    codex_rollouts.process_snapshot_shared()
    assert len(calls) == 2


def test_load_recent_invalidates_the_shared_scan_when_a_root_appears(
        tmp_path, monkeypatch):
    """A new Codex session is never judged off a reading taken before its
    process existed: a changed root set forces a fresh walk."""
    def journal(thread):
        write_rollout(tmp_path / "2026/09/05" / f"rollout-{thread}.jsonl", [
            row("session_meta", {"id": thread, "cwd": "/code/bob",
                "originator": "codex-tui", "source": "cli", "thread_source": "user"})])
    journal("first")
    calls = _counting_scan(monkeypatch, lambda: [])
    now = time.time()
    codex_rollouts.load_recent(tmp_path, now=now, grace=10 ** 9)
    codex_rollouts.load_recent(tmp_path, now=now, grace=10 ** 9)
    assert len(calls) == 1
    journal("second")
    records = codex_rollouts.load_recent(tmp_path, now=now, grace=10 ** 9)
    assert {r.thread_id for r in records} == {"first", "second"}
    assert len(calls) == 2


def test_vscode_attribution_load_recent_attaches_without_cli_liveness(
        tmp_path, monkeypatch):
    now = int(time.time())
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    path = tmp_path / "2026/10/03/rollout-vscode.jsonl"
    processes = []
    calls = _counting_scan(monkeypatch, lambda: processes)
    monkeypatch.setattr(codex_rollouts, "_CACHE", {})
    monkeypatch.setattr(codex_rollouts, "_PROCESS_SNAPSHOT", None)
    monkeypatch.setattr(codex_rollouts, "_LAST_ROOT_IDS", frozenset())
    monkeypatch.setattr(codex_rollouts, "_thread_names", lambda: {})

    # The old empty reading predates both the rollout and its process.
    assert codex_rollouts.process_snapshot_shared() == []
    write_rollout(path, [row("session_meta", {
        "id": "vscode", "cwd": "/code/bob", "originator": "codex-tui",
        "source": "vscode", "thread_source": "user",
    }, timestamp=stamp)])
    processes.append(_FakeProcess(60576, create_time=now - 0.799))

    records = codex_rollouts.load_recent(tmp_path, now=now, grace=10 ** 9)
    assert len(records) == 1
    assert records[0].pid == 60576
    assert records[0].process_identity is None
    assert records[0].process_seen is None
    assert codex_rollouts._safe_cli_root(records[0]) is False
    assert len(calls) == 2

    again = codex_rollouts.load_recent(tmp_path, now=now, grace=10 ** 9)
    assert again[0].pid == 60576
    assert again[0].process_seen is None
    assert len(calls) == 2


def test_shared_scan_never_caches_an_unavailable_reading(monkeypatch):
    results = [None, [], []]
    calls = _counting_scan(monkeypatch, lambda: results[len(calls) - 1])
    assert codex_rollouts.process_snapshot_shared() is None
    assert codex_rollouts._PROCESS_SNAPSHOT is None
    # The next caller retries at once rather than trusting a failed walk.
    assert codex_rollouts.process_snapshot_shared() == []
    assert codex_rollouts.process_snapshot_shared() == []
    assert len(calls) == 2


def test_navigation_targets_scan_fresh_despite_the_shared_reading(
        tmp_path, monkeypatch):
    """Jump revalidates against a fresh walk, never the shared memo."""
    roots, processes = _navigation_fixture(tmp_path, monkeypatch)
    calls = _counting_scan(monkeypatch, lambda: processes)
    proofs = codex_rollouts.resolve_navigation_proofs(_title_roots(roots))
    assert len(calls) == 1
    nav = codex_rollouts.project_navigation_roots(roots)
    assert set(codex_rollouts.navigation_targets(nav, proofs)) == set(proofs)
    assert set(codex_rollouts.navigation_targets(nav, proofs)) == set(proofs)
    assert len(calls) == 3


def test_close_observation_invalidates_the_shared_reading(tmp_path, monkeypatch):
    """A close is authorised off a fresh walk: a holder that left after the
    shared reading was taken refuses the close inside the TTL."""
    path = tmp_path / 'close-fresh.jsonl'
    write_rollout(path, [row('session_meta', {'id': 'abc', 'cwd': '/code/bob',
        'originator': 'codex-tui', 'source': 'cli', 'thread_source': 'user'}),
        row('event_msg', {'type': 'task_started', 'turn_id': 'first'})])
    root = _title_roots([codex_rollouts.parse_rollout(path)])[0]
    journal = codex_rollouts._journal_identity(path)
    live = [_native_holder(701, path)]
    monkeypatch.setattr(codex_rollouts, '_process_snapshot', lambda: live)
    assert codex_rollouts.process_snapshot_shared() == live      # memo primed
    monkeypatch.setattr(codex_rollouts, '_process_snapshot', lambda: [])
    assert codex_rollouts.refinement_close_observation((root,), root, journal, 'first') is None
    monkeypatch.setattr(codex_rollouts, '_process_snapshot', lambda: None)
    assert codex_rollouts.refinement_close_observation((root,), root, journal, 'first') is None


# ── the shared reading is classified once per kind ──────────────────────────

def _counting_classifiers(monkeypatch):
    counts = {"title": 0, "native": 0, "unreadable": 0, "realpath": 0}
    for kind, name in (("title", "_classify_title"), ("native", "_classify_native"),
                       ("unreadable", "_classify_unreadable")):
        real = getattr(codex_rollouts, name)

        def counted(process_list, realpaths, real=real, kind=kind):
            counts[kind] += 1
            return real(process_list, realpaths)
        monkeypatch.setattr(codex_rollouts, name, counted)
    real_realpath = os.path.realpath

    def realpath(path, *a, **k):
        if str(path) == "/bin/zsh":
            counts["realpath"] += 1
        return real_realpath(path, *a, **k)
    monkeypatch.setattr(codex_rollouts.os.path, "realpath", realpath)
    return counts


def test_shared_scan_is_classified_once_per_kind_across_every_caller(tmp_path, monkeypatch):
    """The live profile's largest cost: titles, navigation proofs and the
    roster's liveness pass each re-filtered ~600 processes (a realpath and a
    live `terminal()` read apiece) on every call. One scan is one pass."""
    path = tmp_path / "classified.jsonl"
    path.write_text("root")
    root = _root("classified", path=path)
    others = [_FakeProcess(900 + n, name="zsh", cmdline=("-zsh",), exe="/bin/zsh")
              for n in range(50)]
    calls = _counting_scan(monkeypatch, lambda: [_native_holder(701, path), *others])
    counts = _counting_classifiers(monkeypatch)
    for _ in range(5):
        codex_rollouts.resolve_navigation_proofs(_title_roots([root]))
        codex_rollouts.resolve_title_ttys(_title_roots([root]))
        codex_rollouts.attach_process_ids([root])
    assert len(calls) == 1
    assert counts["title"] == 1 and counts["native"] == 1 and counts["unreadable"] == 1
    assert root.process_seen is True
    # One resolve per distinct executable per scan, shared by every kind —
    # not one per process per call (fifty zsh, fifteen calls: 750 before).
    assert counts["realpath"] == 1
    # A new scan is classified again.
    stamp, observed = codex_rollouts._PROCESS_SNAPSHOT
    monkeypatch.setattr(codex_rollouts, "_PROCESS_SNAPSHOT", (
        stamp - codex_rollouts.PROCESS_SNAPSHOT_TTL_SECONDS, observed))
    codex_rollouts.attach_process_ids([root])
    assert len(calls) == 2 and counts["native"] == 2


def test_a_callers_own_process_list_is_never_served_from_the_memo(tmp_path, monkeypatch):
    """Jump's fresh walk, Stop's proof and a test's stand-ins are classified
    every time: only the shared reading is memoised."""
    path = tmp_path / "own.jsonl"
    path.write_text("root")
    root = _root("own", path=path)
    _counting_scan(monkeypatch, lambda: [_native_holder(701, path)])
    codex_rollouts.process_snapshot_shared()
    counts = _counting_classifiers(monkeypatch)
    own = [_native_holder(702, path)]
    codex_rollouts.attach_process_ids([root], own)
    codex_rollouts.attach_process_ids([root], own)
    codex_rollouts.resolve_title_ttys(_title_roots([root]), own)
    codex_rollouts.resolve_title_ttys(_title_roots([root]), own)
    assert counts["native"] == 2 and counts["title"] == 2


def test_a_member_replaced_in_the_shared_list_is_classified_afresh(tmp_path, monkeypatch):
    path = tmp_path / "swap.jsonl"
    path.write_text("root")
    root = _root("swap", path=path)
    shared = [_FakeProcess(900, name="zsh", cmdline=("-zsh",), exe="/bin/zsh")]
    _counting_scan(monkeypatch, lambda: shared)
    codex_rollouts.attach_process_ids([root])
    assert root.process_seen is False
    shared[0] = _native_holder(701, path)
    codex_rollouts.attach_process_ids([root])
    assert root.process_seen is True


def test_a_new_scan_reusing_the_same_process_objects_is_classified_afresh(tmp_path, monkeypatch):
    """psutil's `process_iter` keeps one `Process` per pid and hands the same
    objects back in every scan, rewriting only `.info`. A new scan with an
    unchanged pid set must still be classified again: a shell that exec'd
    into codex is codex now."""
    path = tmp_path / "exec.jsonl"
    path.write_text("root")
    root = _root("exec", path=path)
    reused = _native_holder(701, path, name="zsh", cmdline=("-zsh",), exe="/bin/zsh")
    calls = _counting_scan(monkeypatch, lambda: [reused])
    codex_rollouts.attach_process_ids([root])
    assert root.process_seen is False
    # The same object, its fields rewritten, in a new scan's new list.
    for fields in (reused.info, reused.fresh):
        fields.update(name="codex", cmdline=["codex"], exe="/opt/codex")
    stamp, observed = codex_rollouts._PROCESS_SNAPSHOT
    monkeypatch.setattr(codex_rollouts, "_PROCESS_SNAPSHOT", (
        stamp - codex_rollouts.PROCESS_SNAPSHOT_TTL_SECONDS, observed))
    codex_rollouts.attach_process_ids([root])
    assert len(calls) == 2
    assert codex_rollouts._PROCESS_SNAPSHOT[1] is not observed
    assert root.process_seen is True
