"""Real journal-shaped fixtures: measured work, provenance and incremental I/O."""
import json
import os

import pytest

from dark_army_daemon.codex_spenders import CodexSpenders, window_bounds

NOW = 1_800_000_000


def meta(thread="root", created=NOW - 100, **extra):
    return {"type": "session_meta", "payload": {
        "id": thread, "timestamp": created, **extra}}


def model(name):
    return {"type": "turn_context", "payload": {"model": name}}


def count(ts, total, last=None):
    def tokens(pair):
        return {"input_tokens": pair[0], "output_tokens": pair[1]}
    info = {"total_token_usage": tokens(total)}
    if last is not None:
        info["last_token_usage"] = tokens(last)
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "token_count", "info": info}}


def write(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def read(tmp_path, *events, **bounds):
    write(tmp_path / "journal.jsonl", *events)
    reader = CodexSpenders(tmp_path, clock=lambda: NOW, **bounds)
    return reader, reader.snapshot()


def weights(report):
    return {row["model"]: row["tokens"] for row in report["models"]}


def test_model_switches_and_two_windows(tmp_path):
    reader, short = read(tmp_path, meta(created=NOW - 90000), model("old"),
                         count(NOW - 20000, (80, 20), (80, 20)), model("new"),
                         count(NOW - 10, (110, 30), (30, 10)))
    assert weights(short) == {"new": 40}
    assert weights(reader.snapshot("week")) == {"old": 100, "new": 40}
    assert reader.bytes_read == 0


def test_overlapping_cached_and_reasoning_tokens_are_not_added(tmp_path):
    event = count(NOW - 10, (100, 20), (100, 20))
    for key in ("total_token_usage", "last_token_usage"):
        event["payload"]["info"][key].update(cached_input_tokens=90,
                                            reasoning_output_tokens=15)
    _, report = read(tmp_path, meta(), model("m"), event)
    assert weights(report) == {"m": 120}
    assert "cost_usd" not in report["models"][0]


def test_repeated_cumulative_different_timestamps_and_duplicate_files(tmp_path):
    events = [meta(), model("m"), count(NOW - 20, (90, 10), (90, 10)),
              count(NOW - 10, (90, 10), (90, 10))]
    write(tmp_path / "a.jsonl", *events)
    write(tmp_path / "b.jsonl", *events)
    reader = CodexSpenders(tmp_path, clock=lambda: NOW)
    assert weights(reader.snapshot()) == {"m": 100}
    assert weights(reader.snapshot()) == {"m": 100}
    assert reader.bytes_read == 0


def test_lifetime_only_baseline_then_delta_and_reset(tmp_path):
    _, report = read(tmp_path, meta(), model("m"),
                     count(NOW - 40, (1000, 100)),
                     count(NOW - 30, (1010, 102)),
                     count(NOW - 20, (1, 1), (1, 1)),
                     count(NOW - 10, (6, 3)))
    assert weights(report) == {"m": 19}
    assert report["partial"]


@pytest.mark.parametrize("bad", [True, -1, 1.5, "20", None, float("nan"), 2**64])
def test_invalid_counters_do_not_become_usage(tmp_path, bad):
    event = count(NOW - 10, (1, 2), (1, 2))
    event["payload"]["info"]["total_token_usage"]["input_tokens"] = bad
    _, report = read(tmp_path, meta(), model("m"), event)
    assert not report["models"] and report["partial"]


def test_missing_model_and_missing_provenance_are_explicit(tmp_path):
    _, report = read(tmp_path, meta(), count(NOW - 10, (1, 2), (1, 2)))
    assert weights(report) == {"unknown model": 3} and report["partial"]
    _, report = read(tmp_path, model("m"), count(NOW - 10, (1, 2), (1, 2)))
    assert not report["models"] and report["partial"]


@pytest.mark.parametrize("provenance", [{"parent_thread_id": "parent"},
                                        {"forked_from_id": "parent"}])
def test_copied_history_is_counted_only_in_parent_and_child_work_survives(tmp_path, provenance):
    copied = count(NOW - 50, (90, 10), (90, 10))
    write(tmp_path / "parent.jsonl", meta("parent"), model("p"), copied)
    write(tmp_path / "child.jsonl", meta("child", NOW - 20, **provenance),
          model("p"), copied, model("c"), count(NOW - 10, (95, 15), (5, 5)))
    report = CodexSpenders(tmp_path, clock=lambda: NOW).snapshot()
    assert weights(report) == {"p": 100, "c": 10}


def test_partial_final_line_retried_after_append(tmp_path):
    path = write(tmp_path / "a.jsonl", meta(), model("m"))
    event = json.dumps(count(NOW - 10, (1, 2), (1, 2))).encode()
    with path.open("ab") as stream:
        stream.write(event[:30])
    reader = CodexSpenders(tmp_path, clock=lambda: NOW)
    assert reader.snapshot()["partial"]
    with path.open("ab") as stream:
        stream.write(event[30:] + b"\n")
    assert weights(reader.snapshot()) == {"m": 3}
    assert not reader.snapshot()["partial"]


def test_truncation_replacement_and_same_size_rewrite_reset_contribution(tmp_path):
    path = write(tmp_path / "a.jsonl", meta(), model("old"), count(NOW - 10, (90, 10), (90, 10)))
    reader = CodexSpenders(tmp_path, clock=lambda: NOW)
    assert weights(reader.snapshot()) == {"old": 100}
    write(path, meta(), model("new"), count(NOW - 10, (90, 10), (90, 10)))
    assert weights(reader.snapshot()) == {"new": 100}
    replacement = write(tmp_path / "replacement", meta(), model("small"), count(NOW - 5, (1, 1), (1, 1)))
    os.replace(replacement, path)
    assert weights(reader.snapshot()) == {"small": 2}
    write(path, meta())
    assert not reader.snapshot()["models"]


def test_small_budgets_progress_fairly_and_eventually_finish(tmp_path):
    for i in range(5):
        write(tmp_path / f"{i}.jsonl", meta(str(i)), model(str(i)),
              *[{"type": "response_item", "payload": "x" * 100} for _ in range(i * 3)],
              count(NOW - 1, (1, 1), (1, 1)))
    reader = CodexSpenders(tmp_path, clock=lambda: NOW, byte_budget=500,
                           file_budget=100, discovery_budget=2)
    for _ in range(100):
        report = reader.snapshot()
        assert reader.bytes_read <= 500
        if not report["partial"]:
            break
    else:
        pytest.fail("bounded discovery/read failed to finish")
    assert len(report["models"]) == 5
    assert not reader.snapshot()["partial"] and reader.bytes_read == 0


def test_record_and_line_limits_are_explicit_and_recover_to_following_records(tmp_path):
    reader, report = read(tmp_path, meta(), model("m"),
                          {"type": "response_item", "payload": "x" * 1000},
                          count(NOW - 3, (1, 1), (1, 1)),
                          count(NOW - 2, (2, 2), (1, 1)),
                          count(NOW - 1, (3, 3), (1, 1)),
                          line_limit=500, file_budget=100, max_records=2)
    assert report["partial"] and weights(report) == {"m": 4}
    assert "limit" in report["reason"]
    assert reader.snapshot()["partial"]


def test_missing_empty_and_file_discovery_limit(tmp_path):
    assert not CodexSpenders(tmp_path / "missing").snapshot()["available"]
    assert not CodexSpenders(tmp_path).snapshot()["available"]
    for i in range(2):
        write(tmp_path / f"{i}.jsonl", meta(str(i)))
    report = CodexSpenders(tmp_path, max_files=1).snapshot()
    assert report["partial"] and "limit" in report["reason"]


def test_codex_boundaries_are_own_current_windows_and_reset_without_append(tmp_path):
    reader, _ = read(tmp_path, meta(created=NOW - 20000), model("m"),
                     count(NOW - 1000, (1, 2), (1, 2)))
    bars = [{"provider": "claude", "label": "5h", "resets_at": NOW + 17500},
            {"provider": "codex", "label": "5h", "window_minutes": 300,
             "resets_at": NOW + 10000}]
    assert weights(reader.snapshot(bars=bars)) == {"m": 3}
    bars[1]["resets_at"] = NOW + 17900
    assert not reader.snapshot(bars=bars)["models"]
    assert reader.bytes_read == 0
    for reset in (NOW - 1, NOW + 18001, True, float("nan")):
        bars[1]["resets_at"] = reset
        assert window_bounds("session", bars, NOW) == (NOW - 18000, NOW, "rolling 5h")
    assert window_bounds("day", bars, NOW)[0] == NOW - 86400
    bars[1].update(label="7d", window_minutes=10080, resets_at=NOW + 100)
    assert window_bounds("week", bars, NOW)[0] == NOW + 100 - 604800


@pytest.mark.parametrize("minutes", [None, 299, 359, True, "300"])
def test_rounded_label_cannot_prove_exact_window_duration(minutes):
    bars = [{"provider": "codex", "label": "5h", "window_minutes": minutes,
             "resets_at": NOW + 10000}]
    assert window_bounds("session", bars, NOW) == (NOW - 18000, NOW, "rolling 5h")
    bars[0]["window_minutes"] = 300
    assert window_bounds("session", bars, NOW)[0] == NOW - 8000


def test_shrink_above_consumed_offset_resets_partial_file_contribution(tmp_path):
    path = write(tmp_path / "a.jsonl", meta(), model("old"),
                 count(NOW - 10, (1, 2), (1, 2)),
                 {"type": "response_item", "payload": "x" * 3000})
    reader = CodexSpenders(tmp_path, clock=lambda: NOW, byte_budget=600, file_budget=100)
    assert weights(reader.snapshot()) == {"old": 3}
    offset = reader._files[path].offset
    original_size = path.stat().st_size
    write(path, meta(), model("new"), count(NOW - 10, (5, 5), (5, 5)),
          {"type": "response_item", "payload": "x" * 900})
    assert offset < path.stat().st_size < original_size
    assert weights(reader.snapshot()) == {"new": 10}


@pytest.mark.parametrize("operation", ["scandir", "stat"])
def test_unreadable_root_is_not_reported_as_missing_and_recovers(tmp_path, monkeypatch, operation):
    write(tmp_path / "a.jsonl", meta(), model("m"), count(NOW - 10, (1, 2), (1, 2)))
    reader = CodexSpenders(tmp_path, clock=lambda: NOW)
    with monkeypatch.context() as denied:
        if operation == "scandir":
            original = os.scandir
            def scandir(path):
                if os.fspath(path) == os.fspath(tmp_path):
                    raise PermissionError("fixture denied root")
                return original(path)
            denied.setattr(os, "scandir", scandir)
        else:
            original = type(tmp_path).stat
            def stat(path, *args, **kwargs):
                if path == tmp_path:
                    raise PermissionError("fixture denied root")
                return original(path, *args, **kwargs)
            denied.setattr(type(tmp_path), "stat", stat)
        failed = reader.snapshot()
        assert not failed["available"] and failed["partial"]
        assert "could not be read" in failed["reason"]
        assert "No local Codex history" not in failed["reason"]
        assert not failed["models"]
    recovered = reader.snapshot()
    assert recovered["available"] and not recovered["partial"]
    assert weights(recovered) == {"m": 3}


def test_unreadable_subtree_marks_mixed_shares_partial_until_complete_healthy_walk(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    write(blocked / "a.jsonl", meta("blocked"), model("blocked"), count(NOW - 10, (1, 2), (1, 2)))
    write(tmp_path / "open" / "a.jsonl", meta("open"), model("open"), count(NOW - 10, (3, 4), (3, 4)))
    reader = CodexSpenders(tmp_path, clock=lambda: NOW)
    original = os.scandir
    def scandir(path):
        if os.fspath(path) == os.fspath(blocked):
            raise PermissionError("fixture denied subtree")
        return original(path)
    with monkeypatch.context() as denied:
        denied.setattr(os, "scandir", scandir)
        partial = reader.snapshot()
        assert partial["available"] and partial["partial"]
        assert weights(partial) == {"open": 7}
        assert "could not be read" in partial["reason"]
    reader.discovery_budget = 1
    still_partial = reader.snapshot()
    assert still_partial["partial"] and "could not be read" in still_partial["reason"]
    for _ in range(20):
        recovered = reader.snapshot()
        if not recovered["partial"]:
            break
    else:
        pytest.fail("healthy discovery did not clear the transient error")
    assert weights(recovered) == {"open": 7, "blocked": 3}


def test_discovery_stat_failure_marks_coverage_and_retries(tmp_path, monkeypatch):
    blocked = write(tmp_path / "blocked.jsonl", meta("blocked"), model("blocked"),
                    count(NOW - 10, (1, 2), (1, 2)))
    write(tmp_path / "open.jsonl", meta("open"), model("open"), count(NOW - 10, (3, 4), (3, 4)))
    reader = CodexSpenders(tmp_path, clock=lambda: NOW)
    original = type(tmp_path).stat
    def stat(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("fixture cannot stat journal")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as denied:
        denied.setattr(type(tmp_path), "stat", stat)
        partial = reader.snapshot()
        assert partial["available"] and partial["partial"]
        assert weights(partial) == {"open": 7}
        assert "could not be read" in partial["reason"]
    recovered = reader.snapshot()
    assert not recovered["partial"]
    assert weights(recovered) == {"open": 7, "blocked": 3}


@pytest.mark.parametrize("already_cached", [False, True])
@pytest.mark.parametrize("unknown_model", [False, True])
def test_journal_open_recovers_without_erasing_accounting_notes(tmp_path, monkeypatch,
                                                               already_cached, unknown_model):
    events = [meta()]
    if not unknown_model:
        events.append(model("m"))
    path = write(tmp_path / "a.jsonl", *events, count(NOW - 10, (1, 2), (1, 2)))
    original_revision = (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns)
    reader = CodexSpenders(tmp_path, clock=lambda: NOW)
    if already_cached:
        assert reader.snapshot()["models"]
    original = type(path).open
    def denied_open(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("fixture temporarily denies journal")
        return original(candidate, *args, **kwargs)
    with monkeypatch.context() as denied:
        denied.setattr(type(path), "open", denied_open)
        failed = reader.snapshot()
        assert failed["partial"]
        assert "Some local journals are unavailable." in failed["reason"]
    recovered = reader.snapshot()
    assert weights(recovered) == {"unknown model" if unknown_model else "m": 3}
    assert "Some local journals are unavailable." not in recovered["reason"]
    assert recovered["partial"] is unknown_model
    assert ("no model name" in recovered["reason"]) is unknown_model
    assert (path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns) == original_revision
    if already_cached:
        assert reader.bytes_read == 0
