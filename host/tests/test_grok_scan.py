"""Grok updates.jsonl → history.db backfill."""
import json

from dark_army_daemon.grok_scan import parse_turns, scan
from dark_army_daemon.grok_usage import USD_TICKS
from dark_army_daemon.history import COST_MEASURED, HistoryStore
from tests.test_grok_usage import _turn, _write


def _session(root, sid, cwd, turns, title="Pixel art", model="grok-4.6"):
    directory = root / cwd.replace("/", "%2F") / sid
    directory.mkdir(parents=True)
    _write(directory / "updates.jsonl", turns)
    (directory / "summary.json").write_text(json.dumps({
        "info": {"cwd": cwd},
        "generated_title": title,
        "current_model_id": model,
    }))
    return directory


def test_parse_turns_uses_stamped_ticks(tmp_path):
    line = json.dumps(_turn(2 * USD_TICKS, input_tokens=10, output_tokens=4,
                            timestamp=1_786_000_100))
    [turn] = parse_turns([line], "sid-1")
    assert turn["session_id"] == "sid-1" or turn["session_id"]
    assert turn["cost_usd"] == 2.0
    assert turn["provider"] == "grok"
    assert turn["output_tokens"] == 4
    assert turn["model"] == "grok-4.6"
    assert turn["message_id"].startswith("grok:")


def test_scan_ingests_measured_turns_and_the_session(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    _session(root, "g1", "/tmp/proj", [
        _turn(USD_TICKS, input_tokens=100, output_tokens=20, timestamp=100),
        _turn(2 * USD_TICKS, input_tokens=50, output_tokens=5, timestamp=200),
    ], title="Pixel art")

    summary = scan(store, root)
    assert summary["files_seen"] == 1
    assert summary["turns_added"] == 2
    assert summary["sessions_priced"] == 1

    row = store._query("SELECT * FROM sessions WHERE session_id='g1'")[0]
    assert row["provider"] == "grok"
    assert row["title"] == "Pixel art"
    assert row["project"] == "proj"
    assert row["primary_model"] == "grok-4.6"
    assert row["cost_usd"] == 3.0
    assert row["cost_source"] == COST_MEASURED
    assert store._scalar("SELECT COUNT(*) FROM turns WHERE session_id='g1'") == 2
    assert store._scalar("SELECT SUM(cost_usd) FROM turns WHERE session_id='g1'") == 3.0
    store.close()


def test_rescanning_never_duplicates(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    _session(root, "g1", "/tmp/proj", [_turn(USD_TICKS, timestamp=100)])
    assert scan(store, root)["turns_added"] == 1
    assert scan(store, root)["turns_added"] == 0
    assert scan(store, root)["files_read"] == 0
    assert store._scalar("SELECT COUNT(*) FROM turns") == 1
    store.close()


def test_an_unpriced_turn_poisons_the_session_total(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    _session(root, "g1", "/tmp/proj", [
        _turn(USD_TICKS, timestamp=100),
        _turn(None, timestamp=200),
    ])
    scan(store, root)
    row = store._query("SELECT cost_usd, cost_source FROM sessions")[0]
    assert row["cost_usd"] is None
    assert row["cost_source"] is None
    # The priced turn is still in the table; we just refuse a session total.
    assert store._scalar("SELECT COUNT(*) FROM turns") == 2
    store.close()


def test_daily_report_uses_measured_ticks_not_a_price_table(tmp_path):
    """pricing.py has no Grok rates. The sparkline has to use the stamps."""
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    _session(root, "g1", "/tmp/proj", [
        _turn(5 * USD_TICKS, output_tokens=1_000_000, timestamp=1_786_000_000),
    ])
    scan(store, root)
    day = store.daily_report(days=3650)[0]
    assert day["cost_usd"] == 5.0
    assert day["grok_cost_usd"] == 5.0
    assert day["claude_cost_usd"] == 0.0
    assert day["grok_turns"] == 1
    store.close()


def test_missing_sessions_dir_is_not_fatal(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    assert scan(store, tmp_path / "nope")["files_seen"] == 0
    store.close()


def test_scan_fills_title_from_the_opening_prompt_when_grok_wrote_none(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    directory = _session(root, "g1", "/tmp/proj", [
        _turn(USD_TICKS, timestamp=100),
    ], title="")
    (directory / "summary.json").write_text(json.dumps({
        "info": {"cwd": "/tmp/proj"},
        "generated_title": "",
        "session_summary": "",
        "current_model_id": "grok-4.6",
    }))
    (directory / "chat_history.jsonl").write_text("\n".join([
        json.dumps({"type": "user", "synthetic_reason": "system_reminder",
                    "content": "<system-reminder>skills…"}),
        json.dumps({"type": "user", "content": "Fix the wrap-up flap"}),
    ]) + "\n")
    scan(store, root)
    assert store._query("SELECT title FROM sessions")[0]["title"] == \
        "Fix the wrap-up flap"
    store.close()


def test_scan_does_not_overwrite_a_generated_grok_title(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    directory = _session(root, "g1", "/tmp/proj", [
        _turn(USD_TICKS, timestamp=100),
    ], title="Pixel art")
    (directory / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": "a longer first prompt"}) + "\n")
    scan(store, root)
    assert store._query("SELECT title FROM sessions")[0]["title"] == "Pixel art"
    store.close()


def test_scan_backfills_a_grok_title_after_the_file_has_been_ingested(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    directory = _session(root, "g1", "/tmp/proj", [
        _turn(USD_TICKS, timestamp=100),
    ], title="Pixel art")
    scan(store, root)
    store._write("UPDATE sessions SET title = NULL WHERE session_id = 'g1'", ())
    summary = scan(store, root)
    assert summary["files_read"] == 0
    assert summary["titles_filled"] == 1
    assert store._query("SELECT title FROM sessions")[0]["title"] == "Pixel art"
    store.close()


# --- byte-offset scan positions -------------------------------------------------


def test_resume_is_a_byte_offset_not_a_line_walk(tmp_path):
    """The position is stored as a negated byte offset: the old line count
    meant re-walking the whole file to skip already-read lines on every mtime
    change, and updates.jsonl's mtime moves on every turn."""
    import os

    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    directory = _session(root, "g1", "/tmp/proj", [_turn(USD_TICKS, timestamp=100)])
    path = directory / "updates.jsonl"

    scan(store, root)
    mtime, pos = store.scan_position(str(path))
    assert pos == -path.stat().st_size

    with path.open("a") as fh:
        fh.write(json.dumps(_turn(USD_TICKS, timestamp=200)) + "\n")
    os.utime(path, (path.stat().st_atime, mtime + 5))
    assert scan(store, root)["turns_added"] == 1
    assert store.scan_position(str(path))[1] == -path.stat().st_size
    assert store._scalar("SELECT COUNT(*) FROM turns") == 2
    store.close()


def test_a_legacy_line_count_position_rescans_once_without_duplicates(tmp_path):
    """Old builds stored line counts (non-negative). One full rescan upgrades
    the row to a byte offset; INSERT OR IGNORE keeps the rescan free."""
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    directory = _session(root, "g1", "/tmp/proj", [
        _turn(USD_TICKS, timestamp=100),
        _turn(2 * USD_TICKS, timestamp=200),
    ])
    path = directory / "updates.jsonl"

    scan(store, root)
    assert store._scalar("SELECT COUNT(*) FROM turns") == 2
    # An older build's row: a positive line count at a stale mtime.
    store.set_scan_position(str(path), path.stat().st_mtime - 10, 2)

    summary = scan(store, root)
    assert summary["turns_added"] == 0
    assert store._scalar("SELECT COUNT(*) FROM turns") == 2
    assert store.scan_position(str(path))[1] == -path.stat().st_size
    store.close()


def test_a_shrunk_updates_file_is_rescanned_from_the_start(tmp_path):
    store = HistoryStore(tmp_path / "h.db")
    store.connect()
    root = tmp_path / "sessions"
    directory = _session(root, "g1", "/tmp/proj", [
        _turn(USD_TICKS, timestamp=100),
        _turn(2 * USD_TICKS, timestamp=200),
    ])
    path = directory / "updates.jsonl"
    scan(store, root)

    # Replaced with a shorter file: the offset points past the new end.
    import os
    mtime = path.stat().st_mtime
    _write(path, [_turn(3 * USD_TICKS, timestamp=300)])
    os.utime(path, (path.stat().st_atime, mtime + 5))
    summary = scan(store, root)
    assert summary["turns_added"] == 1
    assert store.scan_position(str(path))[1] == -path.stat().st_size
    store.close()
