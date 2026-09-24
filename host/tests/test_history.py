# host/tests/test_history.py
"""Durable history: schema, dedup, local-day bucketing, retention, waiting spans."""

import sqlite3
import threading
import time
from datetime import datetime

import pytest

from dark_army_daemon.history import (
    COST_ESTIMATED,
    COST_MEASURED,
    SCHEMA_VERSION,
    HistoryStore,
    local_day,
)


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(tmp_path / "history.db")
    s.connect()
    yield s
    s.close()


# --- schema ------------------------------------------------------------------


def test_connect_is_idempotent(tmp_path):
    s = HistoryStore(tmp_path / "history.db")
    s.connect()
    s.connect()
    assert s.session_count() == 0
    s.close()


def test_reopening_an_existing_database_keeps_its_rows(tmp_path):
    path = tmp_path / "history.db"
    first = HistoryStore(path)
    first.connect()
    first.upsert_session("s1", project="p")
    first.close()

    second = HistoryStore(path)
    second.connect()
    assert second.session_count() == 1
    second.close()


def test_schema_version_is_recorded(store):
    row = store._query("SELECT value FROM schema_meta WHERE key='version'")[0]
    assert int(row["value"]) == SCHEMA_VERSION


def test_wal_is_enabled(store):
    assert store._query("PRAGMA journal_mode")[0][0].lower() == "wal"


# --- the digest-rebuilding upgrade -------------------------------------------
#
# `prune()` keeps a digest for as long as its session is inside retention: it is
# the only surviving description of work whose turns have already aged out — the
# pair only leaves together, past the horizon. An upgrade that rebuilds
# them must therefore not *delete* them, because `digest_candidates` only ever
# revisits sessions inside DEFAULT_RETENTION_DAYS — anything older would be
# dropped and never written back.

def _downgrade_to(path, version):
    """Stamp an existing database back to `version`, as an older build would."""
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'version'",
                 (str(version),))
    conn.commit()
    conn.close()


def _seed_digest(path, session_id, last_seen):
    s = HistoryStore(path)
    s.connect()
    s.upsert_session(session_id, project="p", last_seen=last_seen)
    s.upsert_digest(session_id, title="t", project="p", last_seen=last_seen,
                    turns=3, tools='{"Bash": 1}', source_mtime=last_seen)
    s.close()


def test_an_older_build_does_not_stamp_the_version_down(tmp_path):
    """Both the .app and a dev checkout open the same file. Stamping down would
    re-run every upgrade step the next time the newer build opened it."""
    path = tmp_path / "history.db"
    s = HistoryStore(path)
    s.connect()
    s.close()

    import dark_army_daemon.history as h
    real = h.SCHEMA_VERSION
    h.SCHEMA_VERSION = real - 1          # pose as the older build
    try:
        older = HistoryStore(path)
        older.connect()
        older.close()
    finally:
        h.SCHEMA_VERSION = real

    conn = sqlite3.connect(path)
    found = int(conn.execute(
        "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0])
    conn.close()
    assert found == real


# --- local day ---------------------------------------------------------------


def test_day_is_local_not_utc():
    """Bucketing by UTC while a human filters by their own calendar puts late
    evening work on tomorrow. Each row stores the day its own clock was showing."""
    ts = time.time()
    assert local_day(ts) == datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def test_late_evening_and_next_morning_land_on_different_days(store):
    evening = datetime(2026, 7, 26, 23, 50).timestamp()
    morning = datetime(2026, 7, 27, 0, 10).timestamp()
    store.record_state("s1", "idle", "working", ts=evening)
    store.record_state("s1", "working", "idle", ts=morning)
    days = [r["day"] for r in store._query("SELECT day FROM state_events ORDER BY ts")]
    assert days == ["2026-07-26", "2026-07-27"]


# --- turns and dedup ---------------------------------------------------------


def test_turn_is_recorded(store):
    assert store.add_turn("s1", time.time(), message_id="m1", model="claude-opus-5",
                          input_tokens=10, output_tokens=20) is True
    row = store._query("SELECT * FROM turns")[0]
    assert row["model"] == "claude-opus-5" and row["output_tokens"] == 20


def test_the_same_message_id_is_never_counted_twice(store):
    """Claude Code writes several records per API response, all sharing message.id,
    and only the last carries final usage. Summing them multiplies the bill."""
    ts = time.time()
    assert store.add_turn("s1", ts, message_id="m1", output_tokens=100) is True
    assert store.add_turn("s1", ts, message_id="m1", output_tokens=100) is False
    total = store._scalar("SELECT SUM(output_tokens) FROM turns")
    assert total == 100


def test_turns_without_a_message_id_are_all_kept(store):
    """The unique index is conditional: rows with no id cannot be deduplicated and
    must not collapse into one."""
    ts = time.time()
    store.add_turn("s1", ts, output_tokens=5)
    store.add_turn("s1", ts, output_tokens=7)
    assert store._scalar("SELECT COUNT(*) FROM turns") == 2


def test_turn_needs_a_session(store):
    assert store.add_turn("", time.time(), message_id="m1") is False


# --- sessions ----------------------------------------------------------------


def test_upsert_creates_then_updates(store):
    store.upsert_session("s1", project="proj", title="First")
    store.upsert_session("s1", title="Renamed")
    row = store._query("SELECT * FROM sessions")[0]
    assert row["title"] == "Renamed" and row["project"] == "proj"


def test_fill_title_only_writes_when_the_row_is_blank(store):
    store.upsert_session("s1", project="p")
    assert store.fill_title("s1", "From the transcript") is True
    assert store._query("SELECT title FROM sessions")[0]["title"] == \
        "From the transcript"
    assert store.fill_title("s1", "A later guess") is False
    assert store._query("SELECT title FROM sessions")[0]["title"] == \
        "From the transcript"
    assert store.fill_title("s1", "   ") is False
    assert store.fill_title("", "x") is False


def test_set_title_overwrites_without_moving_last_seen(store):
    store.upsert_session("s1", title="Opening prompt", last_seen=1000.0)
    assert store.set_title("s1", "Named by Dark Army") is True
    row = store._query("SELECT title, last_seen FROM sessions")[0]
    assert row["title"] == "Named by Dark Army"
    assert row["last_seen"] == 1000.0


def test_untitled_session_ids_are_the_blank_ones(store):
    store.upsert_session("named", title="Has a name")
    store.upsert_session("blank")
    store.upsert_session("empty", title="")
    assert set(store.untitled_session_ids()) == {"blank", "empty"}


def test_a_later_write_cannot_blank_an_earlier_field(store):
    """The live writer learns title, kind and cost at different moments and in no
    fixed order; a partial update must not erase what is already known."""
    store.upsert_session("s1", project="proj", kind="interactive", title="T")
    store.upsert_session("s1", last_seen=time.time())
    row = store._query("SELECT * FROM sessions")[0]
    assert (row["project"], row["kind"], row["title"]) == ("proj", "interactive", "T")


def test_first_seen_never_moves(store):
    store.upsert_session("s1", first_seen=1000.0)
    store.upsert_session("s1", first_seen=9999.0)
    assert store._query("SELECT first_seen FROM sessions")[0]["first_seen"] == 1000.0


def test_cost_carries_its_provenance(store):
    store.upsert_session("s1", cost_usd=1.25, cost_source=COST_MEASURED)
    store.upsert_session("s2", cost_usd=9.99, cost_source=COST_ESTIMATED)
    rows = {r["session_id"]: r["cost_source"]
            for r in store._query("SELECT session_id, cost_source FROM sessions")}
    assert rows == {"s1": COST_MEASURED, "s2": COST_ESTIMATED}


# --- state events ------------------------------------------------------------


def test_repeated_state_is_not_recorded(store):
    """The interesting quantity is how long a state lasted; a repeated `working`
    would cut that span into meaningless pieces."""
    store.record_state("s1", "idle", "working")
    store.record_state("s1", "working", "working")
    assert store._scalar("SELECT COUNT(*) FROM state_events") == 1


def test_reason_is_kept(store):
    store.record_state("s1", "working", "waiting", reason="permission:Bash")
    assert store._query("SELECT reason FROM state_events")[0]["reason"] == "permission:Bash"


# --- waiting spans: the metric this whole layer exists for --------------------


def test_waiting_span_is_measured_between_transitions(store):
    base = time.time() - 1000
    store.record_state("s1", "working", "waiting", reason="AskUserQuestion", ts=base)
    store.record_state("s1", "waiting", "thinking", ts=base + 300)

    spans = store.waiting_spans()
    assert len(spans) == 1
    assert spans[0]["seconds"] == pytest.approx(300, abs=1)
    assert spans[0]["reason"] == "AskUserQuestion"
    assert spans[0]["ended_at"] is not None


def test_an_open_waiting_span_is_measured_to_now(store):
    """A session still blocked has no closing transition — and is precisely the one
    worth reporting."""
    started = time.time() - 120
    store.record_state("s1", "working", "waiting", ts=started)
    span = store.waiting_spans()[0]
    assert span["ended_at"] is None
    assert span["seconds"] == pytest.approx(120, abs=2)


def test_spans_do_not_bleed_between_sessions(store):
    base = time.time() - 500
    store.record_state("s1", "working", "waiting", ts=base)
    store.record_state("s2", "working", "thinking", ts=base + 10)
    store.record_state("s1", "waiting", "idle", ts=base + 60)

    spans = store.waiting_spans()
    assert [s["session_id"] for s in spans] == ["s1"]
    assert spans[0]["seconds"] == pytest.approx(60, abs=1)


def test_multiple_waits_in_one_session_are_separate_spans(store):
    base = time.time() - 1000
    for offset, state in [(0, "waiting"), (30, "working"), (60, "waiting"), (90, "idle")]:
        store.record_state("s1", None, state, ts=base + offset)
    spans = store.waiting_spans()
    assert len(spans) == 2
    assert all(s["seconds"] == pytest.approx(30, abs=1) for s in spans)


# --- metrics -----------------------------------------------------------------


def test_metric_sample_is_stored(store):
    store.record_metrics("s1", {
        "cost_usd": 15.9, "ctx_used_pct": 76, "five_hour_pct": 41,
        "five_hour_resets_at": 1785065400, "seven_day_pct": 12,
        "lines_added": 440, "lines_removed": 131,
    })
    row = store._query("SELECT * FROM metric_samples")[0]
    assert row["cost_usd"] == pytest.approx(15.9)
    assert row["ctx_pct"] == 76
    assert row["five_hour_resets_at"] == 1785065400


def test_absent_metrics_stay_null(store):
    """Not-reported and zero are different, and a $0.00 shown for a session that
    simply has not reported is a lie the UI would repeat."""
    store.record_metrics("s1", {"ctx_used_pct": 10})
    row = store._query("SELECT * FROM metric_samples")[0]
    assert row["cost_usd"] is None and row["five_hour_pct"] is None


def test_empty_metrics_are_not_stored(store):
    store.record_metrics("s1", {})
    assert store._scalar("SELECT COUNT(*) FROM metric_samples") == 0


# --- retention ---------------------------------------------------------------


def test_prune_ages_every_ts_table_at_one_horizon(store):
    """Turns, samples, state events and compactions all age at `days`;
    state_events and compactions used to be kept forever and grew unbounded."""
    now = time.time()
    old = now - 200 * 86400
    store.add_turn("s1", old, message_id="old", output_tokens=1)
    store.record_metrics("s1", {"ctx_used_pct": 1}, ts=old)
    store.record_state("s1", "idle", "working", ts=old)
    store.add_compaction("s1", old, uuid="u-old")
    store.upsert_session("s1", first_seen=old, last_seen=now)

    store.add_turn("s1", now, message_id="new", output_tokens=1)
    store.record_state("s1", "working", "idle", ts=now)
    store.add_compaction("s1", now, uuid="u-new")

    removed = store.prune(days=90)
    assert removed == {"turns": 1, "metric_samples": 1, "state_events": 1,
                       "compactions": 1, "scan_state": 0, "sessions": 0,
                       "session_digests": 0, "themes_retired": 0}
    assert store._scalar("SELECT COUNT(*) FROM turns") == 1
    assert store._scalar("SELECT COUNT(*) FROM state_events") == 1
    assert store._scalar("SELECT COUNT(*) FROM compactions") == 1
    assert store.session_count() == 1


def test_prune_keeps_a_session_whose_turns_are_still_inside_retention(store):
    """The narrative-keep rule at the horizon: an old `last_seen` alone does
    not delete a session — its remaining turns do the talking."""
    old = time.time() - 200 * 86400
    store.upsert_session("s1", first_seen=old, last_seen=old)
    store.add_turn("s1", time.time(), message_id="fresh", output_tokens=1)
    removed = store.prune(days=90)
    assert removed["sessions"] == 0
    assert store.session_count() == 1


def test_prune_drops_old_sessions_and_digests_with_no_remaining_turns(store):
    old = time.time() - 200 * 86400
    store.add_turn("gone", old, message_id="m-old", output_tokens=1)
    store.upsert_session("gone", first_seen=old, last_seen=old)
    store._write("INSERT INTO session_digests(session_id, title, last_seen)"
                 " VALUES(?,?,?)", ("gone", "old work", old))
    # A session with no last_seen at all is never aged: it may have only just
    # registered and simply not reported yet.
    store.upsert_session("undated")

    removed = store.prune(days=90)
    assert removed["sessions"] == 1
    assert removed["session_digests"] == 1
    assert store._query("SELECT session_id FROM sessions")[0]["session_id"] == \
        "undated"
    assert store._scalar("SELECT COUNT(*) FROM session_digests") == 0


def test_prune_drops_scan_positions_for_transcripts_that_are_gone(store, tmp_path):
    """A transcript Claude Code pruned can never be asked about again; an
    unchanged old one still needs its position or the next sweep re-reads it."""
    alive = tmp_path / "alive.jsonl"
    alive.write_text("{}\n")
    store.set_scan_position(str(alive), 1.0, 1)
    store.set_scan_position(str(tmp_path / "deleted.jsonl"), 1.0, 1)

    removed = store.prune(days=90)
    assert removed["scan_state"] == 1
    assert store.scan_position(str(alive)) == (1.0, 1)
    assert store.scan_position(str(tmp_path / "deleted.jsonl")) == (0.0, 0)


def test_prune_disabled_by_zero(store):
    store.add_turn("s1", time.time() - 999 * 86400, message_id="ancient")
    assert store.prune(days=0) == {"turns": 0, "metric_samples": 0,
                                   "state_events": 0, "compactions": 0,
                                   "scan_state": 0, "sessions": 0,
                                   "session_digests": 0, "themes_retired": 0}
    assert store._scalar("SELECT COUNT(*) FROM turns") == 1


# --- robustness --------------------------------------------------------------


def test_writes_before_connect_are_dropped_not_raised(tmp_path):
    s = HistoryStore(tmp_path / "history.db")
    s.record_state("s1", "idle", "working")          # must not raise
    s.record_metrics("s1", {"ctx_used_pct": 1})
    assert s.add_turn("s1", time.time()) is False


def test_a_failing_write_does_not_escape(store, monkeypatch):
    """History observes the system; it is never a gate on it. A broken write costs
    a log line, not an event."""
    class BrokenConnection:
        # sqlite3.Connection.execute is a read-only C attribute, so the whole
        # connection is swapped rather than patched.
        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("disk I/O error")

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

    store._conn = BrokenConnection()
    store.record_state("s1", "idle", "working")      # must not raise
    assert store.add_turn("s1", time.time()) is False
    assert store._query("SELECT 1") == []            # reads degrade too
    assert store.prune(days=1) is not None


def test_concurrent_writers_do_not_corrupt(store):
    """Writes arrive from the executor pool, so more than one thread touches the
    connection."""
    def hammer(n):
        for i in range(50):
            store.record_state(f"s{n}", None, f"state{i}", ts=time.time())

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store._scalar("SELECT COUNT(*) FROM state_events") == 200


def test_daily_tokens_groups_by_day_and_model(store):
    day1 = datetime(2026, 7, 20, 12, 0).timestamp()
    day2 = datetime(2026, 7, 21, 12, 0).timestamp()
    store.add_turn("s1", day1, message_id="a", model="opus", output_tokens=10)
    store.add_turn("s1", day1, message_id="b", model="opus", output_tokens=5)
    store.add_turn("s1", day2, message_id="c", model="haiku", output_tokens=1)

    rows = store.daily_tokens(days=3650)
    assert len(rows) == 2
    by_day = {r["day"]: r for r in rows}
    assert by_day["2026-07-20"]["output_tokens"] == 15
    assert by_day["2026-07-20"]["turns"] == 2
    assert by_day["2026-07-21"]["model"] == "haiku"


# --- Live writer: the daemon's side ------------------------------------------

import asyncio  # noqa: E402

from dark_army_daemon.daemon import BobDaemon  # noqa: E402


@pytest.fixture
def daemon_with_history(tmp_path):
    d = BobDaemon()
    d._history = HistoryStore(tmp_path / "history.db")
    d._history.connect()
    yield d
    d._history.close()


async def _settle():
    """Writes are fire-and-forget into the executor; let them land."""
    for _ in range(40):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_state_transition_is_recorded(daemon_with_history):
    d = daemon_with_history
    await d._handle_message({"event": "session_start", "session_id": "s1",
                             "project": "proj"})
    await d._handle_message({"event": "tool_use", "session_id": "s1",
                             "tool_name": "Bash"})
    await _settle()

    rows = d._history._query("SELECT * FROM state_events ORDER BY id")
    assert [r["to_state"] for r in rows] == ["registered", "working"]
    assert rows[1]["from_state"] == "registered"


@pytest.mark.asyncio
async def test_a_permission_block_records_which_tool(daemon_with_history):
    """"waiting" alone cannot tell you whether an agent is holding a permission
    dialog for `rm -rf` or asking a multiple-choice question."""
    d = daemon_with_history
    await d._handle_message({"event": "session_start", "session_id": "s1"})
    await d._handle_message({"event": "permission", "session_id": "s1",
                             "tool_name": "Bash"})
    await _settle()

    row = d._history._query(
        "SELECT * FROM state_events WHERE to_state='waiting'")[0]
    assert row["reason"] == "permission:Bash"


@pytest.mark.asyncio
async def test_an_api_error_is_labelled(daemon_with_history):
    d = daemon_with_history
    await d._handle_message({"event": "session_start", "session_id": "s1"})
    await d._handle_message({"event": "add", "hook": "StopFailure",
                             "session_id": "s1", "project": "p",
                             "message": "overloaded"})
    await _settle()
    row = d._history._query("SELECT * FROM state_events WHERE to_state='error'")[0]
    assert row["reason"] == "api_error"


@pytest.mark.asyncio
async def test_session_end_closes_the_story(daemon_with_history):
    d = daemon_with_history
    await d._handle_message({"event": "session_start", "session_id": "s1",
                             "project": "proj"})
    await d._handle_message({"event": "dismiss", "hook": "SessionEnd",
                             "session_id": "s1", "reason": "exit"})
    await _settle()

    assert d._history._query(
        "SELECT * FROM state_events WHERE to_state='ended'")
    row = d._history._query("SELECT * FROM sessions WHERE session_id='s1'")[0]
    assert row["ended_at"] is not None and row["end_reason"] == "SessionEnd"


@pytest.mark.asyncio
async def test_waiting_span_survives_into_the_history(daemon_with_history):
    """End to end for the metric the layer exists for."""
    d = daemon_with_history
    await d._handle_message({"event": "session_start", "session_id": "s1"})
    await d._handle_message({"event": "permission", "session_id": "s1",
                             "tool_name": "Bash"})
    await d._handle_message({"event": "dismiss", "hook": "UserPromptSubmit",
                             "session_id": "s1"})
    await _settle()

    spans = d._history.waiting_spans()
    assert len(spans) == 1
    assert spans[0]["reason"] == "permission:Bash"
    assert spans[0]["ended_at"] is not None


@pytest.mark.asyncio
async def test_statusline_records_measured_cost(daemon_with_history):
    d = daemon_with_history
    await d._handle_message({
        "event": "statusline", "session_id": "s1",
        "data": {"session_id": "s1", "cost": {"total_cost_usd": 15.9},
                 "context_window": {"used_percentage": 76},
                 "rate_limits": {"five_hour": {"used_percentage": 41}},
                 "session_name": "Real name"},
    })
    await _settle()

    sample = d._history._query("SELECT * FROM metric_samples")[0]
    assert sample["cost_usd"] == pytest.approx(15.9)
    assert sample["ctx_pct"] == 76 and sample["five_hour_pct"] == 41

    session = d._history._query("SELECT * FROM sessions")[0]
    assert session["cost_source"] == COST_MEASURED, "Claude Code's own figure"
    assert session["title"] == "Real name"


@pytest.mark.asyncio
async def test_metric_sampling_is_throttled(daemon_with_history):
    """The source ticks on every assistant message; at eight agents an unthrottled
    writer is a row a second, forever."""
    d = daemon_with_history
    msg = {"event": "statusline", "session_id": "s1",
           "data": {"session_id": "s1", "context_window": {"used_percentage": 10}}}
    for _ in range(6):
        await d._handle_message(dict(msg))
    await _settle()
    assert d._history._scalar("SELECT COUNT(*) FROM metric_samples") == 1

    d._last_metric_sample["s1"] -= 999      # pretend the interval elapsed
    await d._handle_message(dict(msg))
    await _settle()
    assert d._history._scalar("SELECT COUNT(*) FROM metric_samples") == 2


@pytest.mark.asyncio
async def test_a_daemon_without_history_still_works():
    """History is an observer of the system, never a gate on it."""
    d = BobDaemon()
    assert d._history is None
    await d._handle_message({"event": "session_start", "session_id": "s1"})
    await d._handle_message({"event": "statusline", "session_id": "s1",
                             "data": {"session_id": "s1"}})
    assert "s1" in d._session_states


# --- reports -----------------------------------------------------------------


def test_daily_report_prices_each_model_separately(store):
    """A day that used two models cannot be priced at one rate."""
    day = datetime(2026, 7, 20, 12, 0).timestamp()
    store.add_turn("s1", day, message_id="a", model="claude-opus-4-8",
                   output_tokens=1_000_000)
    store.add_turn("s1", day, message_id="b", model="claude-haiku-4-5",
                   output_tokens=1_000_000)

    report = store.daily_report(days=3650)
    assert len(report) == 1
    assert report[0]["cost_usd"] == pytest.approx(25.0 + 5.0)
    assert report[0]["turns"] == 2
    assert report[0]["unpriced_turns"] == 0


def test_daily_report_counts_unpriced_turns_instead_of_hiding_them(store):
    """An unpriced group must not read as free — the count is what lets the UI
    say "N turns unpriced" rather than showing a total that excludes them."""
    day = datetime(2026, 7, 20, 12, 0).timestamp()
    store.add_turn("s1", day, message_id="a", model="claude-opus-4-8",
                   output_tokens=1_000_000)
    store.add_turn("s1", day, message_id="b", model="some-local-llm",
                   output_tokens=9_000_000)

    row = store.daily_report(days=3650)[0]
    assert row["cost_usd"] == pytest.approx(25.0)
    assert row["unpriced_turns"] == 1


def test_daily_report_uses_the_promotional_rate_for_the_right_day(store):
    store.add_turn("s1", datetime(2026, 7, 20, 12, 0).timestamp(), message_id="a",
                   model="claude-sonnet-5", input_tokens=1_000_000)
    store.add_turn("s1", datetime(2026, 9, 20, 12, 0).timestamp(), message_id="b",
                   model="claude-sonnet-5", input_tokens=1_000_000)
    by_day = {r["day"]: r["cost_usd"] for r in store.daily_report(days=3650)}
    assert by_day["2026-07-20"] == pytest.approx(2.0)
    assert by_day["2026-09-20"] == pytest.approx(3.0)


def test_by_project_ranks_by_cost(store):
    now = time.time()
    store.upsert_session("a", project="cheap", cost_usd=1.0, last_seen=now)
    store.upsert_session("b", project="dear", cost_usd=90.0, last_seen=now)
    store.upsert_session("c", project="dear", cost_usd=10.0, last_seen=now)

    rows = store.by_project()
    assert [r["project"] for r in rows] == ["dear", "cheap"]
    assert rows[0]["sessions"] == 2 and rows[0]["cost_usd"] == pytest.approx(100.0)


def test_by_project_reports_last_active_as_the_max_last_seen(store):
    store.upsert_session("old", project="p", last_seen=100.0)
    store.upsert_session("new", project="p", last_seen=250.0)
    rows = store.by_project()
    assert len(rows) == 1
    assert rows[0]["last_active"] == pytest.approx(250.0)


def test_by_project_spans_all_time_by_default(store):
    """days=None → `_since` 0.0, so a session older than 90 days still counts."""
    old = time.time() - 200 * 86400
    store.upsert_session("ancient", project="p", last_seen=old)
    assert store.by_project(days=30) == []
    rows = store.by_project()
    assert len(rows) == 1 and rows[0]["sessions"] == 1
    assert rows[0]["last_active"] == pytest.approx(old)


def test_daily_report_keeps_measured_and_estimated_apart_by_provider(store):
    day = datetime(2026, 7, 20, 12, 0).timestamp()
    store.add_turn("c1", day, message_id="a", model="claude-opus-4-8",
                   output_tokens=1_000_000, provider="claude")
    store.add_turn("g1", day, message_id="b", model="grok-4.6",
                   output_tokens=9, cost_usd=1.5, provider="grok")
    row = store.daily_report(days=3650)[0]
    assert row["claude_cost_usd"] == pytest.approx(25.0)
    assert row["grok_cost_usd"] == pytest.approx(1.5)
    assert row["cost_usd"] == pytest.approx(26.5)
    assert row["claude_turns"] == 1 and row["grok_turns"] == 1


def test_other_daily_keeps_measured_grok_and_estimates_only_the_leftover(store):
    """A named card's session is excluded. The leftover keeps its own price.

    The measured Grok turn stays measured. The unmeasured Claude turn is
    estimated only while that session is still in the leftover set, and a
    turn with no stored cost and no price-list hit adds no dollars and
    counts as one session, not one turn.
    """
    day = datetime(2026, 7, 20, 12, 0).timestamp()
    store.add_turn("c1", day, message_id="a", model="claude-opus-4-8",
                   output_tokens=1_000_000, provider="claude")
    store.add_turn("g1", day, message_id="b", model="grok-4.6",
                   output_tokens=9, cost_usd=1.5, provider="grok")
    store.add_turn("u1", day, message_id="c", model="not-a-priced-model",
                   output_tokens=4, provider="claude")
    next_day = day + 86400
    store.add_turn("u1", next_day, message_id="d", model="not-a-priced-model",
                   output_tokens=4, provider="claude")
    store.add_turn("u1", next_day + 1, message_id="d2",
                   model="not-a-priced-model", output_tokens=4, provider="claude")
    store.add_turn("x1", day, message_id="e", model="claude-opus-4-8",
                   output_tokens=1_000, cost_usd=3.0, provider="codex")

    held = {row["day"]: row for row in store.other_daily(3650, exclude=["c1"])}
    row = held["2026-07-20"]
    assert row["grok_measured_cost_usd"] == pytest.approx(1.5)
    assert row["grok_output_tokens"] == 9
    assert row["claude_measured_cost_usd"] == 0
    assert row["estimated_cost_usd"] == 0
    assert row["unpriced_sessions"] == 1
    assert row["claude_output_tokens"] == 4
    # A measured Codex turn is not folded into Claude's dollars.
    assert (row["claude_measured_cost_usd"] + row["grok_measured_cost_usd"]
            + row["estimated_cost_usd"]) == pytest.approx(1.5)

    leftover = {row["day"]: row
                for row in store.other_daily(3650, exclude=[])}
    assert leftover["2026-07-20"]["estimated_cost_usd"] == pytest.approx(25.0)
    assert leftover["2026-07-20"]["grok_measured_cost_usd"] == pytest.approx(1.5)
    # Two unpriced turns of one session on the next day are one session.
    assert leftover["2026-07-21"]["unpriced_sessions"] == 1
    assert leftover["2026-07-21"]["estimated_cost_usd"] == 0
    assert leftover["2026-07-21"]["claude_measured_cost_usd"] == 0


def test_other_daily_reads_every_chunk(store):
    """Two batches both count. A cap would keep an arbitrary prefix."""
    store.SESSION_ID_CHUNK = 1
    day = time.time()
    store.add_turn("a", day, message_id="a", model="grok-4.6",
                   output_tokens=1, cost_usd=1.0, provider="grok")
    store.add_turn("b", day, message_id="b", model="grok-4.6",
                   output_tokens=1, cost_usd=2.0, provider="grok")
    rows = store.other_daily(1, exclude=[])
    assert len(rows) == 1
    assert rows[0]["grok_measured_cost_usd"] == pytest.approx(3.0)


def test_other_daily_splits_estimates_and_unpriced_by_provider(store):
    day = datetime(2026, 7, 20, 12, 0).timestamp()
    store.add_turn("c", day, message_id="c", model="claude-opus-4-8",
                   output_tokens=1_000_000, provider="claude")
    store.add_turn("g", day, message_id="g", model="claude-opus-4-8",
                   output_tokens=1_000_000, provider="grok")
    store.add_turn("uc", day, message_id="uc", model="not-a-priced-model",
                   output_tokens=1, provider="claude")
    store.add_turn("ug", day, message_id="ug", model="not-a-priced-model",
                   output_tokens=1, provider="grok")
    row = store.other_daily(3650, exclude=[])[0]
    assert row["claude_estimated_cost_usd"] == pytest.approx(25.0)
    assert row["grok_estimated_cost_usd"] == pytest.approx(25.0)
    assert row["estimated_cost_usd"] == pytest.approx(50.0)
    assert row["claude_unpriced_sessions"] == 1
    assert row["grok_unpriced_sessions"] == 1
    assert row["unpriced_sessions"] == 2
    assert row["claude_measured_cost_usd"] == 0
    assert row["grok_measured_cost_usd"] == 0


def test_session_efficiency_splits_measured_and_estimated_without_moving_cost(store):
    day = time.time()
    store.add_turn("measured", day, message_id="m", model="grok-4.6",
                   output_tokens=9, cost_usd=1.5, provider="grok")
    store.add_turn("estimated", day, message_id="e", model="claude-opus-4-8",
                   output_tokens=1_000_000, provider="claude")
    facts = store.session_efficiency(["measured", "estimated", "gone"])
    assert "gone" not in facts
    measured = facts["measured"]
    assert measured["measured_cost_usd"] == pytest.approx(1.5)
    assert "estimated_cost_usd" not in measured
    assert measured["cost_usd"] == pytest.approx(1.5)
    assert measured["model"] == "grok-4.6"
    estimated = facts["estimated"]
    assert estimated["estimated_cost_usd"] == pytest.approx(25.0)
    assert "measured_cost_usd" not in estimated
    # The old field still includes the estimate. The phone reads that.
    assert estimated["cost_usd"] == pytest.approx(25.0)
    assert estimated["model"] == "claude-opus-4-8"


def test_totals_split_cost_and_counts_by_provider(store):
    now = time.time()
    store.upsert_session("c1", project="p", cost_usd=10, cost_source=COST_ESTIMATED,
                         provider="claude", last_seen=now)
    store.upsert_session("g1", project="p", cost_usd=2, cost_source=COST_MEASURED,
                         provider="grok", last_seen=now)
    store.add_turn("c1", now, message_id="a", model="claude-opus-4-8",
                   output_tokens=100, provider="claude")
    store.add_turn("g1", now, message_id="b", model="grok-4.6",
                   output_tokens=20, cost_usd=2, provider="grok")
    tot = store.totals(days=30)
    assert tot["claude_sessions"] == 1 and tot["grok_sessions"] == 1
    assert tot["claude_cost_usd"] == pytest.approx(10)
    assert tot["grok_cost_usd"] == pytest.approx(2)
    assert tot["claude_turns"] == 1 and tot["grok_turns"] == 1
    assert tot["grok_output_tokens"] == 20


def test_recent_sessions_carry_provider(store):
    store.upsert_session("g1", project="p", provider="grok", last_seen=time.time())
    assert store.recent_sessions(days=30)[0]["provider"] == "grok"


def test_upgrading_to_v6_backfills_provider_and_keeps_scan_positions(tmp_path):
    path = tmp_path / "history.db"
    first = HistoryStore(path)
    first.connect()
    first.upsert_session("g1", project="p", primary_model="grok-4.6")
    first.upsert_session("c1", project="p", primary_model="claude-opus-5")
    first.add_turn("c1", time.time(), message_id="m1", model="claude-opus-5")
    first.set_scan_position("/x/a.jsonl", 123.0, 10)
    first._conn.execute("UPDATE schema_meta SET value = '5' WHERE key = 'version'")
    first._conn.execute("UPDATE sessions SET provider = NULL")
    first._conn.execute("UPDATE turns SET provider = NULL")
    first._conn.commit()
    first.close()

    second = HistoryStore(path)
    second.connect()
    try:
        by_id = {r["session_id"]: r["provider"]
                 for r in second._query("SELECT session_id, provider FROM sessions")}
        assert by_id["g1"] == "grok"
        assert by_id["c1"] == "claude"
        assert second._query("SELECT provider FROM turns")[0]["provider"] == "claude"
        # v6 added columns, not a new transcript field — do not rescan Claude.
        assert second.scan_position("/x/a.jsonl") == (123.0, 10)
    finally:
        second.close()


def test_by_project_reports_how_much_is_measured(store):
    now = time.time()
    store.upsert_session("a", project="p", cost_usd=1.0,
                         cost_source=COST_MEASURED, last_seen=now)
    store.upsert_session("b", project="p", cost_usd=2.0,
                         cost_source=COST_ESTIMATED, last_seen=now)
    assert store.by_project()[0]["measured_sessions"] == 1


def test_by_model_ranks_by_output(store):
    now = time.time()
    store.add_turn("s1", now, message_id="a", model="big", output_tokens=100)
    store.add_turn("s1", now, message_id="b", model="small", output_tokens=1)
    assert [r["model"] for r in store.by_model()] == ["big", "small"]


def test_range_filters_by_time(store):
    old = time.time() - 200 * 86400
    store.add_turn("s1", old, message_id="old", output_tokens=1, model="m")
    store.add_turn("s1", time.time(), message_id="new", output_tokens=1, model="m")
    assert store.totals(days=30)["turns"] == 1
    assert store.totals(days=None)["turns"] == 2


def test_waiting_summary_reports_a_median_not_just_a_total(store):
    """One forgotten overnight block would dominate a mean and hide the everyday
    pattern."""
    base = time.time() - 10_000
    for i, length in enumerate([60, 60, 60, 7200]):
        store.record_state(f"s{i}", "working", "waiting", ts=base + i)
        store.record_state(f"s{i}", "waiting", "idle", ts=base + i + length)

    summary = store.waiting_summary()
    assert summary["spans"] == 4
    assert summary["median_seconds"] == pytest.approx(60, abs=1)
    assert summary["longest_seconds"] == pytest.approx(7200, abs=1)
    assert summary["total_seconds"] == pytest.approx(7380, abs=2)


def test_waiting_summary_counts_open_spans(store):
    store.record_state("s1", "working", "waiting", ts=time.time() - 100)
    assert store.waiting_summary()["open_now"] == 1


def test_waiting_summary_groups_by_reason(store):
    base = time.time() - 500
    store.record_state("s1", "working", "waiting", reason="permission:Bash", ts=base)
    store.record_state("s1", "waiting", "idle", ts=base + 10)
    store.record_state("s2", "working", "waiting", reason="permission:Bash", ts=base)
    store.record_state("s2", "waiting", "idle", ts=base + 10)
    store.record_state("s3", "working", "waiting", reason="AskUserQuestion", ts=base)

    reasons = store.waiting_summary()["by_reason"]
    assert reasons["permission:Bash"] == 2
    assert reasons["AskUserQuestion"] == 1


def test_totals_are_zero_not_null_on_an_empty_database(store):
    totals = store.totals(days=30)
    assert totals["turns"] == 0 and totals["sessions"] == 0
    assert totals["cost_usd"] == 0


# --- cost provenance ---------------------------------------------------------


def test_totals_keep_measured_and_estimated_cost_apart(store):
    """Adding them would produce a total that is true of nothing: one is what the
    work cost, the other what it would have cost at list prices."""
    now = time.time()
    store.upsert_session("a", cost_usd=10.0, cost_source=COST_MEASURED, last_seen=now)
    store.upsert_session("b", cost_usd=900.0, cost_source=COST_ESTIMATED, last_seen=now)

    totals = store.totals(days=30)
    assert totals["measured_cost_usd"] == pytest.approx(10.0)
    assert totals["estimated_cost_usd"] == pytest.approx(900.0)
    assert totals["measured_sessions"] == 1


def test_a_session_with_no_cost_source_counts_as_estimated(store):
    """`cost_source` is NULL until something claims the number. Treating that as
    measured would launder a guess into an authoritative figure."""
    store.upsert_session("a", cost_usd=5.0, last_seen=time.time())
    totals = store.totals(days=30)
    assert totals["measured_cost_usd"] == 0
    assert totals["estimated_cost_usd"] == pytest.approx(5.0)


# --- compactions -------------------------------------------------------------


def test_compaction_is_recorded(store):
    store.add_compaction("s1", time.time(), uuid="u1", trigger="auto",
                         pre_tokens=167_436)
    row = store._query("SELECT * FROM compactions")[0]
    assert row["trigger"] == "auto" and row["pre_tokens"] == 167_436


def test_the_same_compaction_uuid_is_never_counted_twice(store):
    """Re-scanning a transcript must not double the count."""
    now = time.time()
    assert store.add_compaction("s1", now, uuid="u1") is True
    assert store.add_compaction("s1", now, uuid="u1") is False
    assert store._scalar("SELECT COUNT(*) FROM compactions") == 1


def test_compactions_without_a_uuid_are_all_kept(store):
    now = time.time()
    store.add_compaction("s1", now)
    store.add_compaction("s1", now)
    assert store._scalar("SELECT COUNT(*) FROM compactions") == 2


def test_compactions_survive_the_turns_they_outlive_inside_retention(store):
    """Inside the window they are still the only surviving record that a
    context filled up — the transcript is pruned by Claude Code and the
    metric samples age out here. Past the window they age out too."""
    recent = time.time() - 10 * 86400
    old = time.time() - 200 * 86400
    store.add_compaction("s1", recent, uuid="u-recent")
    store.add_compaction("s1", old, uuid="u-old")
    store.add_turn("s1", old, message_id="m1", output_tokens=1)
    store.prune(days=90)
    assert store._scalar("SELECT COUNT(*) FROM compactions") == 1
    assert store._query("SELECT uuid FROM compactions")[0]["uuid"] == "u-recent"
    assert store._scalar("SELECT COUNT(*) FROM turns") == 0


# --- context pressure --------------------------------------------------------


def test_context_pressure_counts_sessions_not_samples(store):
    """A session that sat at 95% for an hour is one session over the line, not
    sixty samples over it."""
    now = time.time()
    store.upsert_session("s1", project="hot", last_seen=now)
    for offset in range(3):
        store.record_metrics("s1", {"ctx_used_pct": 95}, ts=now - offset)

    report = store.context_pressure(days=30)
    assert report["sessions_over_90"] == 1
    assert report["by_project"][0]["sessions"] == 1
    assert report["by_project"][0]["peak_pct"] == 95


def test_context_pressure_merges_peaks_and_compactions_per_project(store):
    now = time.time()
    store.upsert_session("s1", project="repo", last_seen=now)
    store.record_metrics("s1", {"ctx_used_pct": 80}, ts=now)
    store.add_compaction("s1", now, uuid="u1", trigger="manual", pre_tokens=160_000)

    row = store.context_pressure(days=30)["by_project"][0]
    assert row["project"] == "repo"
    assert row["over_75"] == 1 and row["over_90"] == 0
    assert row["compactions"] == 1 and row["manual"] == 1
    assert row["avg_pre_tokens"] == pytest.approx(160_000)


def test_a_project_with_only_compactions_still_appears(store):
    """The two sources cover different populations: compactions reach back before
    Dark Army was installed, where no metric sample can. A project that only has them
    must not vanish from the report."""
    now = time.time()
    store.upsert_session("s1", project="ancient", last_seen=now)
    store.add_compaction("s1", now, uuid="u1", trigger="auto")

    row = store.context_pressure(days=30)["by_project"][0]
    assert row["project"] == "ancient"
    assert row["compactions"] == 1
    assert row["peak_pct"] is None and row["sessions"] == 0


# --- rate-limit budget -------------------------------------------------------


def test_limits_series_reports_the_peak_of_each_bucket(store):
    """A budget's worst moment is the one that matters; a mean would smooth away
    the spike you opened the chart to find."""
    now = time.time()
    for pct in (10, 80, 20):
        store.record_metrics("s1", {"five_hour_pct": pct}, ts=now - 60)

    series = store.limits_report(days=1)["series"]
    assert len(series) == 1
    assert series[0]["five_hour_pct"] == 80


def test_limits_report_lists_the_reset_boundaries(store):
    now = time.time()
    store.record_metrics("s1", {"five_hour_pct": 10,
                                "five_hour_resets_at": now + 100}, ts=now - 10)
    store.record_metrics("s1", {"five_hour_pct": 20,
                                "five_hour_resets_at": now + 18_100}, ts=now - 5)
    assert store.limits_report(days=1)["resets"] == [now + 100, now + 18_100]


def test_burn_rate_is_measured_from_the_start_of_the_window(store):
    """Not between the last two samples: the budget starts each window at zero, so
    the average pace is both simpler and immune to the flat stretch while a human
    reads a diff."""
    now = time.time()
    store.record_metrics("s1", {"five_hour_pct": 40,
                                "five_hour_resets_at": now + 3600}, ts=now)
    # Window opened 4h ago (reset is 1h away, windows are 5h) → 40% over 4h.
    assert store.limits_report(days=1)["burn_pct_per_hour"] == pytest.approx(10, abs=0.1)


def test_a_projection_past_the_reset_is_not_reported(store):
    """The window refills first, so a timestamp there would warn about something
    that cannot happen."""
    now = time.time()
    store.record_metrics("s1", {"five_hour_pct": 40,
                                "five_hour_resets_at": now + 3600}, ts=now)
    assert store.limits_report(days=1)["projected_full_at"] is None


def test_a_projection_inside_the_window_is_reported(store):
    now = time.time()
    store.record_metrics("s1", {"five_hour_pct": 90,
                                "five_hour_resets_at": now + 3600}, ts=now)
    report = store.limits_report(days=1)
    assert report["projected_full_at"] is not None
    assert report["projected_full_at"] < now + 3600


def test_burn_rate_needs_a_reset_time(store):
    """Without one there is no window start, and a pace measured from an unknown
    origin is a made-up number."""
    store.record_metrics("s1", {"five_hour_pct": 50}, ts=time.time())
    report = store.limits_report(days=1)
    assert report["burn_pct_per_hour"] is None
    assert report["projected_full_at"] is None


# --- hourly ------------------------------------------------------------------


def test_hourly_report_buckets_by_the_local_hour(store):
    store.add_turn("s1", datetime(2026, 7, 20, 14, 30).timestamp(),
                   message_id="a", output_tokens=10)
    rows = {r["hour"]: r for r in store.hourly_report(days=3650)}
    assert rows[14]["turns"] == 1
    assert rows[13]["turns"] == 0


def test_hourly_report_covers_every_hour(store):
    """A histogram with gaps reads as missing data, not as an hour nobody works."""
    store.add_turn("s1", time.time(), message_id="a", output_tokens=1)
    assert [r["hour"] for r in store.hourly_report(days=30)] == list(range(24))


def test_hourly_report_counts_the_days_it_saw(store):
    """So a caller can show a daily average instead of a total that grows with the
    range."""
    for day in (20, 21):
        store.add_turn("s1", datetime(2026, 7, day, 9, 0).timestamp(),
                       message_id=f"m{day}", output_tokens=1)
    rows = {r["hour"]: r for r in store.hourly_report(days=3650)}
    assert rows[9]["turns"] == 2 and rows[9]["days"] == 2


# --- recent sessions ---------------------------------------------------------


def test_recent_sessions_are_newest_first_with_their_own_totals(store):
    now = time.time()
    store.upsert_session("old", project="p", first_seen=now - 500, last_seen=now - 400)
    store.upsert_session("new", project="p", first_seen=now - 100, last_seen=now)
    store.add_turn("new", now, message_id="m1", output_tokens=7, input_tokens=3)

    rows = store.recent_sessions(days=30)
    assert [r["session_id"] for r in rows] == ["new", "old"]
    assert rows[0]["turns"] == 1 and rows[0]["tokens"] == 10


def test_recent_sessions_respects_the_limit(store):
    now = time.time()
    for i in range(5):
        store.upsert_session(f"s{i}", last_seen=now - i)
    assert len(store.recent_sessions(days=30, limit=2)) == 2


# --- waiting, sliced ---------------------------------------------------------


def test_waiting_is_grouped_by_the_day_the_block_started(store):
    """A block that crosses midnight is one event that began when it began —
    splitting it would be more precise about the clock and less true about what
    happened."""
    start = datetime(2026, 7, 20, 23, 50).timestamp()
    store.record_state("s1", "working", "waiting", ts=start)
    store.record_state("s1", "waiting", "idle", ts=start + 1800)   # 00:20 next day

    by_day = store.waiting_summary(days=3650)["by_day"]
    assert [d["day"] for d in by_day] == ["2026-07-20"]
    assert by_day[0]["total_seconds"] == pytest.approx(1800)


def test_waiting_is_grouped_by_project(store):
    now = time.time() - 500
    store.upsert_session("s1", project="slow", last_seen=time.time())
    store.upsert_session("s2", project="quick", last_seen=time.time())
    store.record_state("s1", "working", "waiting", ts=now)
    store.record_state("s1", "waiting", "idle", ts=now + 300)
    store.record_state("s2", "working", "waiting", ts=now)
    store.record_state("s2", "waiting", "idle", ts=now + 10)

    rows = store.waiting_summary(days=30)["by_project"]
    assert [r["project"] for r in rows] == ["slow", "quick"]
    assert rows[0]["total_seconds"] == pytest.approx(300, abs=1)


def test_the_longest_blocks_are_reported_with_their_project(store):
    now = time.time() - 500
    store.upsert_session("s1", project="repo", last_seen=time.time())
    store.record_state("s1", "working", "waiting", reason="permission:Bash", ts=now)
    store.record_state("s1", "waiting", "idle", ts=now + 120)

    longest = store.waiting_summary(days=30)["longest"]
    assert longest[0]["project"] == "repo"
    assert longest[0]["reason"] == "permission:Bash"


def test_waiting_on_a_session_with_no_row_is_still_counted(store):
    """The project is unknown, which is not a reason to lose the block."""
    now = time.time() - 100
    store.record_state("ghost", "working", "waiting", ts=now)
    store.record_state("ghost", "waiting", "idle", ts=now + 60)
    rows = store.waiting_summary(days=30)["by_project"]
    assert rows[0]["project"] == "?" and rows[0]["spans"] == 1


# --- migration ---------------------------------------------------------------


def test_upgrading_forgets_the_scan_positions(tmp_path):
    """A schema bump means the scanner learned to read a record type it used to
    walk past. Every transcript is already at its recorded mtime, so without this
    the new columns would stay empty for the life of the database."""
    path = tmp_path / "history.db"
    first = HistoryStore(path)
    first.connect()
    first.set_scan_position("/x/a.jsonl", 123.0, 10)
    first.close()

    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_meta SET value = '1' WHERE key = 'version'")
    conn.commit()
    conn.close()

    second = HistoryStore(path)
    second.connect()
    try:
        assert second.scan_position("/x/a.jsonl") == (0.0, 0)
    finally:
        second.close()


def test_reopening_at_the_current_version_keeps_the_scan_positions(tmp_path):
    """The rescan is a one-off cost of the upgrade, not of every start."""
    path = tmp_path / "history.db"
    first = HistoryStore(path)
    first.connect()
    first.set_scan_position("/x/a.jsonl", 123.0, 10)
    first.close()

    second = HistoryStore(path)
    second.connect()
    try:
        assert second.scan_position("/x/a.jsonl") == (123.0, 10)
    finally:
        second.close()


# --- usage attribution -------------------------------------------------------
#
# Cost-weighted, so the fixtures below pin a model with a known rate rather than
# leaning on whatever the price list happens to hold for a family name.


def _turn(store, ts=None, model="claude-opus-4-8", output_tokens=1000, **fields):
    store.add_turn("s1", ts if ts is not None else time.time(),
                   message_id=fields.pop("message_id", None) or f"m{time.time_ns()}",
                   model=model, output_tokens=output_tokens, **fields)


def test_an_upgraded_database_gains_the_attribution_columns(tmp_path):
    """A v2 file has a v2 `turns`, and CREATE TABLE IF NOT EXISTS will not widen
    it — so without the ALTER every attribution query fails on a real install
    while passing on a fresh one."""
    path = tmp_path / "history.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE turns (message_id TEXT, session_id TEXT NOT NULL,
                            ts REAL NOT NULL, day TEXT NOT NULL, model TEXT,
                            input_tokens INTEGER NOT NULL DEFAULT 0,
                            output_tokens INTEGER NOT NULL DEFAULT 0,
                            cache_read INTEGER NOT NULL DEFAULT 0,
                            cache_creation INTEGER NOT NULL DEFAULT 0,
                            tool_name TEXT, is_sidechain INTEGER NOT NULL DEFAULT 0,
                            agent_id TEXT, duration_ms INTEGER);
        INSERT INTO schema_meta VALUES('version', '2');
    """)
    conn.commit()
    conn.close()

    store = HistoryStore(path)
    store.connect()
    columns = {r["name"] for r in store._query("SELECT name FROM pragma_table_info('turns')")}
    assert {"attr_skill", "attr_agent", "attr_plugin",
            "attr_mcp_server", "attr_mcp_tool"} <= columns
    # And the report runs rather than raising "no such column".
    assert store.usage_attribution(days=1)["skills"] == []
    store.close()


def test_upgrading_forgets_scan_positions_so_the_backfill_reruns(tmp_path):
    """The columns are useless while every transcript is still recorded at its
    current mtime: the scanner would skip them all and they would stay NULL for
    the life of the database."""
    path = tmp_path / "history.db"
    store = HistoryStore(path)
    store.connect()
    store.set_scan_position("/some/transcript.jsonl", 123.0, 10)
    store._conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'version'")
    store._conn.commit()
    store.close()

    reopened = HistoryStore(path)
    reopened.connect()
    # (0.0, 0) is what "never scanned" looks like — and 0.0 never equals a real
    # mtime, so the next scan reads the file from the start.
    assert reopened.scan_position("/some/transcript.jsonl") == (0.0, 0)
    reopened.close()


def test_shares_are_of_the_whole_window_not_of_the_attributed_rows(store):
    """One of two equal turns carries a skill, so the skill is 50% — not 100%.
    A skill's share is of everything spent, which is what makes the tables
    comparable across dimensions."""
    _turn(store, attr_skill="dataviz")
    _turn(store)
    [row] = store.usage_attribution(days=1)["skills"]
    assert (row["name"], row["pct"]) == ("dataviz", 50)


def test_one_turn_can_be_counted_under_several_dimensions(store):
    """An MCP call inside a skill inside a subagent is all three, and /usage says
    so: independent characteristics, not a breakdown."""
    _turn(store, attr_skill="s", attr_agent="a", attr_mcp_server="m")
    report = store.usage_attribution(days=1)
    assert [report[k][0]["pct"] for k in ("skills", "agents", "mcp_servers")] == [100, 100, 100]


def test_rows_below_the_floor_are_dropped(store):
    """A tail of sub-1% rows is noise wearing the clothes of a finding."""
    _turn(store, attr_skill="loud", output_tokens=100_000)
    _turn(store, attr_skill="quiet", output_tokens=100)
    assert [r["name"] for r in store.usage_attribution(days=1)["skills"]] == ["loud"]


def test_an_empty_window_reports_no_shares_rather_than_zeroes(store):
    """Every share would be a division by zero, and 0% everywhere reads as a
    measurement rather than as nothing to measure."""
    report = store.usage_attribution(days=1)
    assert report["window_cost_usd"] == 0 and report["skills"] == []
    assert report["behaviours"] == []


def test_turns_outside_the_window_are_not_counted(store):
    _turn(store, ts=time.time() - 3 * 86400, attr_skill="old")
    _turn(store, attr_skill="new")
    assert [r["name"] for r in store.usage_attribution(days=1)["skills"]] == ["new"]
    assert {r["name"] for r in store.usage_attribution(days=7)["skills"]} == {"old", "new"}


def test_a_long_context_turn_is_reported_as_a_behaviour(store):
    _turn(store, cache_read=200_000)
    keys = {b["key"] for b in store.usage_attribution(days=1)["behaviours"]}
    assert "long_context" in keys


def test_a_behaviour_below_five_percent_is_not_worth_a_sentence(store):
    """The table floor is 1%; a headline claims more, so it has to earn more.

    The bulk turn is deliberately far larger than the marked one: cache writes are
    charged at a multiple of the *input* rate, so at 200k they still cost a few
    dollars, and a smaller denominator lands this on the 5% boundary rather than
    under it."""
    _turn(store, output_tokens=4_000_000)              # the bulk, unremarkable
    _turn(store, output_tokens=1_000, cache_creation=200_000)   # a big write, tiny cost
    keys = {b["key"] for b in store.usage_attribution(days=1)["behaviours"]}
    assert "cache_miss" not in keys


def test_four_concurrent_sessions_are_reported_as_a_pile_up(store):
    """Concurrency is not a column: it is derived from turns landing in the same
    five-minute slot from four different sessions."""
    now = time.time()
    # Stay inside one five-minute slot: `ts+0..+3` at a slot edge otherwise
    # splits across buckets and the HAVING count never reaches four.
    ts = now - (now % 300) + 10
    for i in range(4):
        store.add_turn(f"p{i}", ts + i, message_id=f"par{i}",
                       model="claude-opus-4-8", output_tokens=1000)
    keys = {b["key"] for b in store.usage_attribution(days=1)["behaviours"]}
    assert "parallel" in keys


def test_three_concurrent_sessions_are_not(store):
    ts = time.time()
    for i in range(3):
        store.add_turn(f"p{i}", ts + i, message_id=f"par{i}",
                       model="claude-opus-4-8", output_tokens=1000)
    keys = {b["key"] for b in store.usage_attribution(days=1)["behaviours"]}
    assert "parallel" not in keys


def test_an_eight_hour_session_is_reported_as_a_long_one(store):
    now = time.time()
    _turn(store, ts=now - 9 * 3600, message_id="early")
    _turn(store, ts=now, message_id="late")
    keys = {b["key"] for b in store.usage_attribution(days=1)["behaviours"]}
    assert "long_sessions" in keys


# --- one-session record (the card's "What ran") ------------------------------


def test_session_record_rolls_up_one_session(store):
    ts = 1_700_000_000.0
    store.upsert_session(
        "s1", project="proj", title="Rewrite the pipeline",
        kind="interactive", provider="claude",
        primary_model="claude-opus-4-1",
        first_seen=ts - 120, last_seen=ts, ended_at=ts,
        end_reason="complete", cost_usd=1.25, cost_source=COST_MEASURED,
    )
    store.add_turn("s1", ts - 100, message_id="m1", agent_id="",
                   tool_name="Bash", input_tokens=10, output_tokens=20,
                   attr_agent="reviewer")
    store.add_turn("s1", ts - 90, message_id="m2", agent_id="",
                   tool_name="Bash", input_tokens=5, output_tokens=5)
    store.add_turn("s1", ts - 80, message_id="m3", agent_id="",
                   tool_name="Read", input_tokens=1, output_tokens=1)
    store.add_turn("s1", ts - 70, message_id="m4", agent_id="a1",
                   tool_name="Write", input_tokens=2, output_tokens=2)
    store.add_turn("s1", ts - 60, message_id="m5", agent_id="a1",
                   tool_name="Bash", input_tokens=2, output_tokens=2)
    store.add_turn("s1", ts - 50, message_id="m6", agent_id="a2",
                   tool_name="Bash", input_tokens=2, output_tokens=2)
    store.add_compaction("s1", ts - 40, uuid="c1", trigger="auto")

    rec = store.session_record("s1")
    assert rec is not None
    assert rec["session_id"] == "s1"
    assert rec["project"] == "proj"
    assert rec["title"] == "Rewrite the pipeline"
    assert rec["kind"] == "interactive"
    assert rec["provider"] == "claude"
    assert rec["primary_model"] == "claude-opus-4-1"
    assert rec["first_seen"] == ts - 120
    assert rec["last_seen"] == ts
    assert rec["ended_at"] == ts
    assert rec["end_reason"] == "complete"
    assert rec["cost_usd"] == pytest.approx(1.25)
    assert rec["cost_source"] == COST_MEASURED
    assert rec["turns"] == 6
    assert rec["tokens"] == 54
    assert rec["subagents"] == 2
    assert rec["tools"] == [["Bash", 2], ["Read", 1]]
    assert rec["attribution"] == ["reviewer"]
    assert rec["compactions"] == 1


def test_session_record_own_turns_are_not_subagents(store):
    """Own turns are stamped `agent_id=''`, never NULL. Counting NULL would
    miss every own turn and, worse, `IS NULL` on the subagent side matches
    nothing — the trap `session_tools` documents."""
    store.upsert_session("s1")
    store.add_turn("s1", time.time(), message_id="m1", agent_id="",
                   tool_name="Bash")
    rec = store.session_record("s1")
    assert rec["subagents"] == 0
    assert rec["turns"] == 1
    assert rec["tools"] == [["Bash", 1]]


def test_session_record_unknown_id_is_none(store):
    store.upsert_session("s1")
    assert store.session_record("nope") is None
    assert store.session_record("") is None


def test_session_cost_is_the_record_s_cost_fields_in_one_query(store):
    """The board's observation pass reads these three fields for every card
    with a session on every pass; `session_record` answered them with eight
    queries apiece."""
    store.upsert_session("s1", cost_usd=1.25, cost_source=COST_MEASURED)
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        cost = store.session_cost("s1")
    finally:
        store._conn.set_trace_callback(None)
    assert len(statements) == 1
    record = store.session_record("s1")
    assert cost == {k: record[k] for k in ("provider", "cost_usd", "cost_source")}
    assert store.session_cost("nope") is None
    assert store.session_cost("") is None


def test_session_record_zero_turns_leaves_tokens_none(store):
    """No turn rows is a gap, not a quiet zero — the same contract
    `recent_sessions` documents for a session past retention."""
    store.upsert_session("s1", project="p")
    rec = store.session_record("s1")
    assert rec["turns"] == 0
    assert rec["tokens"] is None
    assert rec["tools"] == []
    assert rec["subagents"] == 0


def test_session_record_survives_close_and_reopen(tmp_path):
    path = tmp_path / "history.db"
    first = HistoryStore(path)
    first.connect()
    first.upsert_session("s1", project="p", title="T", cost_usd=2.0,
                         cost_source=COST_MEASURED)
    first.add_turn("s1", time.time(), message_id="m1", agent_id="",
                   tool_name="Bash", input_tokens=3, output_tokens=4)
    original = first.session_record("s1")
    first.close()

    second = HistoryStore(path)
    second.connect()
    assert second.session_record("s1") == original
    second.close()


def test_usage_provider_groups_preserve_mixed_legacy_models_and_provider_identity(store):
    now = time.time()
    # Deliberately identical model strings: identity must come from the column.
    for provider, output in (("claude", 100), ("grok", 300)):
        store.add_turn(provider, now - 10, message_id=provider,
                       provider=provider, model="claude-opus-4-8", output_tokens=output)
    legacy = store.usage_attribution(since=now - 100)
    assert len(legacy["models"]) == 1 and legacy["models"][0]["turns"] == 2
    groups = store.usage_provider_groups(now - 100, now)
    assert [g["provider"] for g in groups] == ["claude", "grok"]
    assert [g["models"][0]["output_tokens"] for g in groups] == [100, 300]
    assert all(g["models"][0]["pct"] == 100 for g in groups)
    assert all(g["measurement"] == "estimated_cost_share" for g in groups)
    assert store.usage_attribution(since=now - 100) == legacy


def test_provider_unpriced_rows_and_time_bounds(store):
    now = time.time()
    _turn(store, ts=now - 10, model="no-known-price", provider="grok")
    _turn(store, ts=now + 10, provider="claude")
    _turn(store, ts=now - 1000, provider="claude")
    group, = store.usage_provider_groups(now - 100, now)
    assert group["provider"] == "grok"
    assert group["measurement"] == "unpriced" and group["partial"]
    assert group["models"][0]["pct"] is None
    assert group["models"][0]["cost_usd"] is None
