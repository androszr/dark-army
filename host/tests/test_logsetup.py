# host/tests/test_logsetup.py
"""Tests for log rotation and retention.

The bug these cover: the log was one file appended since install, so the panel's
"Open Log" opened every day at once. The live file must hold today and only
today, the days before it must survive as archives, and the archives must not
accumulate forever either.
"""
import gzip
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dark_army_menubar import logsetup


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    """Never touch ~/Library/Logs during a test run."""
    path = tmp_path / "Logs" / "DarkArmy" / logsetup.LOG_NAME
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(logsetup, "log_file_path", lambda: path)
    logsetup._startup_report.clear()
    return path


def write_log(path: Path, days: list[str], last_write: datetime) -> None:
    """A log whose records start on `days[0]` and whose mtime is `last_write`."""
    path.write_text("".join(f"{d} 10:00:00,000 [x] INFO: line\n" for d in days))
    stamp = last_write.timestamp()
    os.utime(path, (stamp, stamp))


def test_yesterdays_log_is_archived_at_startup(log_dir):
    yesterday = datetime.now() - timedelta(days=1)
    write_log(log_dir, [f"{yesterday:%Y-%m-%d}"], yesterday)

    rolled = logsetup.roll_stale_log()

    assert rolled is not None
    assert rolled.name == f"{logsetup.LOG_NAME}.{yesterday:%Y-%m-%d}.gz"
    assert not log_dir.exists(), "the live file must start empty, not carry yesterday"
    assert "line" in gzip.open(rolled, "rt").read()


def test_todays_log_is_left_alone(log_dir):
    today = datetime.now()
    write_log(log_dir, [f"{today:%Y-%m-%d}"], today)

    assert logsetup.roll_stale_log() is None
    assert log_dir.exists()
    assert logsetup.archive_paths() == []


def test_log_written_today_but_starting_weeks_ago_is_archived(log_dir):
    """The upgrade case: mtime says fresh, the first record says July.

    This is the one an mtime-only check calls current, which is exactly the
    6.5 MB file the complaint was about.
    """
    today = datetime.now()
    start = today - timedelta(days=21)
    write_log(log_dir, [f"{start:%Y-%m-%d}", f"{today:%Y-%m-%d}"], today)

    rolled = logsetup.roll_stale_log()

    assert rolled is not None
    assert rolled.name == f"{logsetup.LOG_NAME}.{start:%Y-%m-%d}--{today:%Y-%m-%d}.gz"
    assert not log_dir.exists()


def test_archive_keeps_the_mtime_of_the_day_it_covers(log_dir):
    yesterday = datetime.now() - timedelta(days=1)
    write_log(log_dir, [f"{yesterday:%Y-%m-%d}"], yesterday)

    rolled = logsetup.roll_stale_log()

    assert abs(rolled.stat().st_mtime - yesterday.timestamp()) < 2, (
        "pruning goes by mtime, so the archive must be dated by its day, "
        "not by the moment it was compressed"
    )


def test_two_rolls_of_the_same_day_do_not_overwrite(log_dir):
    yesterday = datetime.now() - timedelta(days=1)
    write_log(log_dir, [f"{yesterday:%Y-%m-%d}"], yesterday)
    first = logsetup.roll_stale_log()
    write_log(log_dir, [f"{yesterday:%Y-%m-%d}"], yesterday)
    second = logsetup.roll_stale_log()

    assert first != second
    assert first.exists() and second.exists()
    assert second.name.endswith(".gz")


def test_prune_drops_only_what_is_past_retention(log_dir):
    now = datetime.now()
    kept = log_dir.with_name(logsetup.LOG_NAME + ".recent.gz")
    stale = log_dir.with_name(logsetup.LOG_NAME + ".ancient.gz")
    for path, age in ((kept, 2), (stale, logsetup.RETENTION_DAYS + 1)):
        path.write_bytes(b"x")
        stamp = (now - timedelta(days=age)).timestamp()
        os.utime(path, (stamp, stamp))

    removed = logsetup.prune_archives(now=now)

    assert removed == [stale]
    assert kept.exists() and not stale.exists()


def test_prune_never_touches_the_live_file(log_dir):
    now = datetime.now()
    write_log(log_dir, [f"{now:%Y-%m-%d}"], now - timedelta(days=365))

    logsetup.prune_archives(now=now)

    assert log_dir.exists(), "the file being written to is not an archive"


def test_handler_rotates_and_compresses(log_dir):
    handler = logsetup.build_handler()
    try:
        handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "before", (), None))
        handler.doRollover()
        handler.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "after", (), None))
    finally:
        handler.close()

    archives = logsetup.archive_paths()
    assert len(archives) == 1 and archives[0].name.endswith(".gz")
    assert "before" in gzip.open(archives[0], "rt").read()
    assert "after" in log_dir.read_text()
    assert "before" not in log_dir.read_text()


def test_build_handler_survives_a_broken_directory(log_dir, monkeypatch):
    """Tidying is a courtesy; the app must still get a handler."""
    monkeypatch.setattr(logsetup, "roll_stale_log", lambda *a, **k: 1 / 0)

    handler = logsetup.build_handler()
    try:
        assert isinstance(handler, logging.Handler)
    finally:
        handler.close()


def test_startup_report_is_deferred_until_there_is_a_handler(log_dir, caplog):
    yesterday = datetime.now() - timedelta(days=1)
    write_log(log_dir, [f"{yesterday:%Y-%m-%d}"], yesterday)

    handler = logsetup.build_handler()
    handler.close()
    assert logsetup._startup_report, "build_handler runs before logging is configured"

    with caplog.at_level(logging.INFO):
        logsetup.report_startup(logging.getLogger("test"))
    assert "Archived" in caplog.text
    assert not logsetup._startup_report, "drained, so a restart does not repeat it"
