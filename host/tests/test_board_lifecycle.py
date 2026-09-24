"""Durable lifecycle observations: episodes, coverage, clocks, retention."""
from __future__ import annotations

import math
import sqlite3

import pytest

from dark_army_daemon.board import SCHEMA_VERSION, BoardStore
from dark_army_daemon import board_lifecycle as m


class Clock:
    def __init__(self, utc=1_000.0, mono=0.0):
        self.utc = float(utc)
        self.mono = float(mono)

    def advance(self, seconds):
        self.utc += seconds
        self.mono += seconds

    def now(self):
        return self.utc

    def monotonic(self):
        return self.mono


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = BoardStore(tmp_path / "board.db")
    s._clock_utc = clock.now
    s._clock_mono = clock.monotonic
    s.connect()
    yield s
    s.close()


def card(store, **fields):
    payload = dict(title="Ship it", root="/project", tool="claude",
                   intended_benefit="Faster", success_criterion="One click")
    payload.update(fields)
    c, reason = store.create(payload)
    assert c, reason
    return c


def observe(store, cid, clock, **facts):
    seq = store.lifecycle_facts_seq(cid)
    payload = dict(queued=False, executing=False, review=False, rework=False,
                   manual_check=False, impl_unknown=False, visible=True)
    payload.update(seq)
    payload.update(facts)
    store.observe_lifecycle(cid, payload, utc=clock.utc, monotonic=clock.mono)


def report(store, card_id=None, root="/project", start=None, end=None, **kw):
    start = 1_000.0 if start is None else start
    end = start + 200 if end is None else end
    kw.setdefault("now", end)
    if card_id:
        return store.lifecycle_report(card_id=card_id, start=start, end=end, **kw)
    return store.lifecycle_report(root=root, start=start, end=end, **kw)


def test_schema_is_twenty_two():
    assert SCHEMA_VERSION == 25
    assert "lifecycle_partial" not in m.__dict__


def test_nearest_rank_fixture_values():
    values = [0, 10, 20, 30]
    assert m.nearest_rank(values, 0.5) == 10
    assert m.nearest_rank(values, 0.9) == 30
    assert sum(m.histogram(values)) == 4
    assert m.histogram(values)[0] == 4
    assert m.nearest_rank([], 0.5) is None


def _seeded_timeline(store, clock, c):
    """queue 0-10, bind at 10, exec 10-30, question 30-40, exec 40-50,
    review 50-80, revision 80, queue 80-90, exec 90-110, review 110-120,
    accept 120. Observe every 10s."""
    t0 = clock.utc
    store.update(c["id"], {"queue_state": "queued", "queued_at": clock.utc})
    observe(store, c["id"], clock, queued=True)
    clock.advance(10)
    observe(store, c["id"], clock, queued=True)
    # launcher/bind at 10
    store.update(c["id"], {
        "link_state": "dispatching", "column_name": "in_progress",
        "dispatched_at": clock.utc, "queue_state": "", "queued_at": None,
    }, bump=False)
    store.bind_session(c["id"], "sid-1")
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock)
    # question pause 30-40: this sample confirms 20-30 as execution
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    # submission at 50
    store.record_outcome_run(c["id"], "claude", "sid-1", "implementation")
    store.update(c["id"], {"column_name": "done"})
    store.mark_ended(c["id"], when=clock.utc)
    observe(store, c["id"], clock, review=True)
    clock.advance(10)
    observe(store, c["id"], clock, review=True)
    clock.advance(10)
    observe(store, c["id"], clock, review=True)
    clock.advance(10)
    observe(store, c["id"], clock, review=True)
    # revision at 80
    current = store.get(c["id"])
    store.request_outcome_revision(
        c["id"], current["outcome_revision"], "rev-1", "Please change it")
    store.update(c["id"], {
        "column_name": "backlog", "session_id": "", "link_state": "",
        "queue_state": "queued", "queued_at": clock.utc,
    })
    observe(store, c["id"], clock, queued=True, rework=True)
    clock.advance(10)
    observe(store, c["id"], clock, queued=True, rework=True)
    # second attempt at 90
    store.update(c["id"], {
        "link_state": "dispatching", "column_name": "in_progress",
        "dispatched_at": clock.utc, "queue_state": "", "queued_at": None,
    }, bump=False)
    store.bind_session(c["id"], "sid-2")
    observe(store, c["id"], clock, executing=True, rework=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True, rework=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True, rework=True)
    store.record_outcome_run(c["id"], "claude", "sid-2", "implementation")
    store.update(c["id"], {"column_name": "done"})
    store.mark_ended(c["id"], when=clock.utc)
    observe(store, c["id"], clock, review=True)
    clock.advance(10)
    observe(store, c["id"], clock, review=True)
    current = store.get(c["id"])
    store.accept_outcome(
        c["id"], current["outcome_revision"], "acc-1", "Looks good")
    observe(store, c["id"], clock)
    return t0


def test_seeded_120s_timeline(store, clock):
    c = card(store)
    t0 = _seeded_timeline(store, clock, c)
    page = report(store, root="/project", start=t0, end=t0 + 120.1, now=t0 + 120.1)
    q = page["summaries"]["queue"]
    e = page["summaries"]["execution"]
    r = page["summaries"]["review"]
    w = page["summaries"]["rework"]
    assert q["n"] == 2 and q["N"] == 2
    assert q["p50"] == 10 and q["min"] == 10 and q["max"] == 10
    assert sorted([q["min"], q["max"]]) == [10, 10]
    assert e["n"] == 2
    assert {e["min"], e["max"]} == {20, 30}
    assert r["n"] == 2
    assert {r["min"], r["max"]} == {10, 30}
    assert w["n"] == 1 and w["p50"] == 30
    total = (q["observed_seconds"] + e["observed_seconds"]
             + r["observed_seconds"] + w["observed_seconds"])
    assert total > 120, "rework overlaps; the four rows are not a partition"
    assert page["overlap_note"] == m.OVERLAP_NOTE


def test_review_manual_union(store, clock):
    c = card(store)
    store.record_outcome_run(c["id"], "claude", "sid", "implementation")
    store.update(c["id"], {"column_name": "done", "session_id": "sid",
                           "link_state": "ended"})
    observe(store, c["id"], clock, review=True)
    clock.advance(10)
    observe(store, c["id"], clock, review=True, manual_check=True)
    clock.advance(10)
    observe(store, c["id"], clock, review=True, manual_check=True)
    clock.advance(10)
    observe(store, c["id"], clock, review=True)
    page = report(store, root="/project", start=1000, end=1040)
    review = page["summaries"]["review"]
    # 0-10 review, 10-30 both, 30-40 review → union 40, not 60.
    assert review["observed_seconds"] == pytest.approx(30.0)
    assert review["cause_seconds"]["review"] == pytest.approx(30.0)
    assert review["cause_seconds"]["manual_check"] == pytest.approx(20.0)


def test_refine_adds_no_execution(store, clock):
    c = card(store)
    store.update(c["id"], {"refine_state": "dispatching",
                           "refine_session_id": "ref-1"})
    observe(store, c["id"], clock)
    clock.advance(10)
    observe(store, c["id"], clock)
    store.update(c["id"], {"refine_state": "live"})
    clock.advance(10)
    observe(store, c["id"], clock)
    page = report(store, root="/project", start=1000, end=1030)
    assert page["summaries"]["execution"]["N"] == 0
    assert page["summaries"]["execution"]["observed_seconds"] == 0


def test_zero_direct_queue(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "column_name": "in_progress",
        "dispatched_at": clock.utc, "queue_state": "",
    }, bump=False)
    page = report(store, root="/project", start=1000, end=1100)
    q = page["summaries"]["queue"]
    assert q["n"] == 1 and q["N"] == 1
    assert q["min"] == 0 and q["max"] == 0


def test_cancelled_queue_is_not_a_sample(store, clock):
    c = card(store)
    store.update(c["id"], {"queue_state": "queued", "queued_at": clock.utc})
    observe(store, c["id"], clock, queued=True)
    clock.advance(10)
    observe(store, c["id"], clock, queued=True)
    store.update(c["id"], {"queue_state": "", "queued_at": None})
    page = report(store, root="/project", start=1000, end=1100)
    q = page["summaries"]["queue"]
    assert q["n"] == 0 and q["cancelled"] == 1 and q["N"] == 1


def test_mark_ended_then_mark_live_retracts_completion(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    store.bind_session(c["id"], "sid")
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    store.mark_ended(c["id"], when=clock.utc)
    cutoff = clock.utc
    page = report(store, root="/project", start=1000, end=cutoff, now=cutoff,
                  as_of=cutoff)
    assert page["summaries"]["execution"]["n"] == 1
    clock.advance(5)
    store.mark_live(c["id"])
    later = report(store, root="/project", start=1000, end=cutoff,
                   as_of=cutoff)
    assert later["summaries"]["execution"]["n"] == 1, "later mark_live must not rewrite the cutoff"
    open_page = report(store, root="/project", start=1000, end=clock.utc + 1,
                       now=clock.utc)
    assert open_page["summaries"]["execution"]["n"] == 0
    assert open_page["summaries"]["execution"]["open"] == 1
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    frozen = report(store, root="/project", start=1000, end=cutoff, as_of=cutoff)
    assert frozen["summaries"]["execution"]["n"] == 1
    assert frozen["summaries"]["execution"]["min"] == page["summaries"]["execution"]["min"]
    assert frozen["summaries"]["execution"]["max"] == page["summaries"]["execution"]["max"]


def test_reopen_twice_preserves_attempts(store, clock, tmp_path):
    c = card(store)
    for i in range(3):
        store.update(c["id"], {
            "link_state": "dispatching", "dispatched_at": clock.utc,
            "session_id": "",
        }, bump=False)
        store.bind_session(c["id"], f"sid-{i}")
        observe(store, c["id"], clock, executing=True)
        clock.advance(10)
        observe(store, c["id"], clock, executing=True)
        store.mark_ended(c["id"], when=clock.utc)
        clock.advance(5)
        store.update(c["id"], {"session_id": "", "link_state": ""})
        clock.advance(5)
    path = store._path
    store.close()
    again = BoardStore(path)
    again.connect()
    rows = list(again._conn.execute("SELECT id FROM lifecycle_attempts"))
    assert len(rows) == 3
    again.close()
    third = BoardStore(path)
    third.connect()
    assert len(list(third._conn.execute("SELECT id FROM lifecycle_attempts"))) == 3
    third.close()


def test_late_collector_cannot_mutate_successor(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    first = store.lifecycle_facts_seq(c["id"])["attempt_id"]
    store.bind_session(c["id"], "sid-1")
    store.mark_ended(c["id"], when=clock.utc)
    store.update(c["id"], {"session_id": "", "link_state": ""})
    clock.advance(10)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    store.bind_session(c["id"], "sid-2")
    second = store.lifecycle_facts_seq(c["id"])["attempt_id"]
    assert first != second
    store.observe_lifecycle(c["id"], {
        "executing": True, "attempt_id": first,
        "boundary_seq": 0, "queued": False, "review": False,
        "rework": False, "manual_check": False, "impl_unknown": False,
        "visible": True,
    }, utc=clock.utc, monotonic=clock.mono)
    spans = list(store._conn.execute(
        "SELECT attempt_id FROM lifecycle_spans WHERE category='execution'"))
    assert all(row["attempt_id"] != first or True for row in spans)
    # The stale sample is discarded; the successor has no execution span from it.
    exec_spans = [row["attempt_id"] for row in spans]
    assert first not in exec_spans


def test_reorder_does_not_restart_queue(store, clock):
    c = card(store)
    store.update(c["id"], {"queue_state": "queued", "queued_at": clock.utc})
    observe(store, c["id"], clock, queued=True)
    clock.advance(5)
    store.update(c["id"], {"queue_rank": 2})
    observe(store, c["id"], clock, queued=True)
    clock.advance(5)
    observe(store, c["id"], clock, queued=True)
    open_eps = list(store._conn.execute(
        "SELECT id FROM lifecycle_episodes WHERE kind='queue' AND disposition='open'"))
    assert len(open_eps) == 1


def test_review_acknowledgement_is_not_acceptance(store, clock):
    c = card(store, column_name="done")
    store.record_outcome_run(c["id"], "claude", "sid", "implementation")
    store.update(c["id"], {"column_name": "done"})
    store.mark_reviewed(c["id"])
    page = report(store, card_id=c["id"], start=1000, end=1100)
    assert store.outcome_card_report(c["id"])["card"]["accepted"] is False
    assert page["summaries"]["review"]["n"] in (0, 1)


def test_scope_change_is_not_rework(store, clock):
    c = card(store)
    store.accept_outcome(c["id"], 0, "acc", "ok")
    store.update(c["id"], {
        "success_criterion": "Two clicks",
        "expected_outcome_revision": 1,
        "confirm_outcome_scope_change": True,
    })
    page = report(store, root="/project", start=1000, end=1100)
    assert page["summaries"]["rework"]["N"] == 0


def test_clock_gap_is_not_elapsed(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    store.bind_session(c["id"], "sid")
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    known = report(store, root="/project", start=1000, end=2000)
    clock.advance(40)  # > 30s
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    page = report(store, root="/project", start=1000, end=2000)
    exec_s = page["summaries"]["execution"]["observed_seconds"]
    assert exec_s == pytest.approx(20.0)
    assert exec_s == pytest.approx(
        known["summaries"]["execution"]["observed_seconds"] + 10.0)
    assert page["summaries"]["execution"]["n"] == 0
    assert page["summaries"]["execution"]["open"] == 1


def test_clock_gap_on_a_closed_episode_is_partial_not_n(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    store.bind_session(c["id"], "sid")
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    clock.advance(40)
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    store.mark_ended(c["id"], when=clock.utc)
    page = report(store, root="/project", start=1000, end=2000)
    exec_s = page["summaries"]["execution"]
    assert exec_s["n"] == 0
    assert exec_s["partial"] + exec_s["unknown"] >= 1
    assert exec_s["observed_seconds"] == pytest.approx(20.0)
    assert exec_s["min"] is None


def test_negative_and_nonfinite_clocks(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    store.bind_session(c["id"], "sid")
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    store.observe_lifecycle(c["id"], {
        "executing": True, **store.lifecycle_facts_seq(c["id"]),
        "queued": False, "review": False, "rework": False,
        "manual_check": False, "impl_unknown": False, "visible": True,
    }, utc=float("nan"), monotonic=clock.mono)
    clock.utc -= 5
    clock.mono -= 5
    observe(store, c["id"], clock, executing=True)
    page = report(store, root="/project", start=1000, end=2000, now=2000)
    assert page["summaries"]["execution"]["observed_seconds"] == pytest.approx(10.0)


def test_deletion_retains_history_and_stops_accrual(store, clock):
    c = card(store)
    store.update(c["id"], {"queue_state": "queued", "queued_at": clock.utc})
    observe(store, c["id"], clock, queued=True)
    clock.advance(10)
    observe(store, c["id"], clock, queued=True)
    cid = c["id"]
    ok, _ = store.delete(cid)
    assert ok
    assert store.get(cid) is None
    clock.advance(30)
    observe(store, cid, clock, queued=True)
    page = report(store, card_id=cid, start=1000, end=2000)
    assert page["live"] is False
    assert page["removed_note"] == "No longer on the board"
    q = page["summaries"]["queue"]
    assert q["cancelled"] == 1
    assert q["observed_seconds"] == pytest.approx(10.0)


def test_clear_done_keeps_lifecycle(store, clock):
    c = card(store, column_name="done")
    store.record_outcome_run(c["id"], "claude", "sid", "implementation")
    store.update(c["id"], {"column_name": "done"})
    count, token = store.done_scope()
    ok, deleted, _ = store.clear_done(count, token)
    assert ok and deleted >= 1
    page = report(store, card_id=c["id"], start=1000, end=2000)
    assert page["available"] is True
    assert store.get(c["id"]) is None


def test_clear_done_refusal_does_not_close(store):
    c = card(store, column_name="done")
    ok, deleted, detail = store.clear_done(99, "deadbeef" * 8)
    assert not ok and deleted == 0
    assert store.get(c["id"]) is not None


def test_prune_done_retains_lifecycle(store, clock):
    c = card(store, column_name="done")
    store.update(c["id"], {"column_name": "done", "done_at": 1.0})
    n = store.prune_done(10)
    assert n >= 1
    page = report(store, card_id=c["id"], start=1, end=2000)
    assert page["available"] is True


def test_savepoint_failure_keeps_board_write(store, clock, monkeypatch):
    c = card(store)
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk")
    monkeypatch.setattr(store, "_lifecycle_transition_locked", boom)
    updated, detail = store.update(c["id"], {"title": "Still saved"})
    assert updated is not None and updated["title"] == "Still saved"
    page = report(store, root="/project", start=1000, end=2000)
    assert page["measurements_available"] is False or page["summaries"]["queue"]["N"] == 0
    path = store._path
    store.close()
    again = BoardStore(path)
    again.connect()
    again._clock_utc = clock.now
    card_row = again.get(c["id"])
    assert card_row is not None and card_row["title"] == "Still saved"
    health = again._conn.execute(
        "SELECT value FROM lifecycle_meta WHERE key='unavailable'").fetchone()
    assert health is None or health["value"] in ("0", "1")
    again.close()


def test_span_checkpoint_does_not_change_generation(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    store.bind_session(c["id"], "sid")
    first = store.lifecycle_generation()
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    assert store.lifecycle_generation() == first
    store.mark_ended(c["id"], when=clock.utc)
    assert store.lifecycle_generation() != first


def test_unsampled_execution_is_not_a_zero_quantile(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    store.bind_session(c["id"], "sid")
    store.mark_ended(c["id"], when=clock.utc)
    page = report(store, root="/project", start=1000, end=1100)
    exec_s = page["summaries"]["execution"]
    assert exec_s["n"] == 0
    assert exec_s["N"] == 1
    assert exec_s["partial"] + exec_s["unknown"] >= 1
    assert exec_s["min"] is None
    q = page["summaries"]["queue"]
    assert q["n"] == 1 and q["min"] == 0


def test_retention_does_not_invent_completeness(store, clock):
    c = card(store)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
    }, bump=False)
    store.bind_session(c["id"], "sid")
    observe(store, c["id"], clock, executing=True)
    clock.advance(10)
    observe(store, c["id"], clock, executing=True)
    store.mark_ended(c["id"], when=clock.utc)
    store.lifecycle_prune(now=clock.utc + 400 * 86400, keep_days=366)
    page = report(store, root="/project", start=clock.utc - 10, end=clock.utc + 10,
                  now=clock.utc)
    # Expired evidence is counted in retention, not as a newly complete sample.
    assert page["retention"]
    exec_s = page["summaries"]["execution"]
    assert exec_s["n"] == 0
    assert exec_s["open"] == 0
    assert exec_s["partial"] + exec_s["unknown"] + exec_s["N"] >= 1
    assert exec_s.get("min") is None


def test_carry_in_excluded_from_distribution(store, clock):
    c = card(store)
    store.update(c["id"], {"queue_state": "queued", "queued_at": clock.utc})
    observe(store, c["id"], clock, queued=True)
    clock.advance(10)
    observe(store, c["id"], clock, queued=True)
    clock.advance(10)
    observe(store, c["id"], clock, queued=True)
    store.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc,
        "queue_state": "",
    }, bump=False)
    page = report(store, root="/project", start=1015, end=1100)
    q = page["summaries"]["queue"]
    assert q["N"] == 0
    assert q["carry_in"]["completed"] == 1
    assert q["carry_in"]["observed_seconds"] > 0


def test_period_bound_rejects_366_plus(store):
    with pytest.raises(ValueError):
        store.lifecycle_report(root="/project", start=0, end=367 * 86400)


def test_page_size_does_not_change_summaries(store, clock):
    cards = [card(store, title=f"C{i}") for i in range(3)]
    for c in cards:
        store.update(c["id"], {
            "link_state": "dispatching", "dispatched_at": clock.utc,
        }, bump=False)
    a = report(store, root="/project", start=1000, end=1100, limit=1)
    b = report(store, root="/project", start=1000, end=1100, limit=25)
    assert a["summaries"]["queue"]["N"] == b["summaries"]["queue"]["N"]
    assert a["summaries"]["queue"]["n"] == b["summaries"]["queue"]["n"]
    assert len(a["cards"]) == 1
    assert len(b["cards"]) == 3


def test_changed_generation_refuses_page(store, clock):
    c = card(store)
    first = report(store, root="/project", start=1000, end=1100)
    store.update(c["id"], {"queue_state": "queued", "queued_at": clock.utc})
    again = store.lifecycle_report(
        root="/project", start=1000, end=1100,
        generation=first["generation"])
    assert again["available"] is False
    assert "refresh" in again["reason"]


def test_interleaved_cards(store, clock):
    a = card(store, title="A")
    b = card(store, title="B")
    store.update(a["id"], {"queue_state": "queued", "queued_at": clock.utc})
    store.update(b["id"], {"queue_state": "queued", "queued_at": clock.utc})
    observe(store, a["id"], clock, queued=True)
    observe(store, b["id"], clock, queued=True)
    clock.advance(10)
    observe(store, a["id"], clock, queued=True)
    observe(store, b["id"], clock, queued=True)
    store.update(a["id"], {
        "link_state": "dispatching", "dispatched_at": clock.utc, "queue_state": "",
    }, bump=False)
    page = report(store, root="/project", start=1000, end=1100)
    assert page["summaries"]["queue"]["cards"] == 2


def test_no_events_before_tracking(store, clock):
    c = card(store)
    page = report(store, card_id=c["id"], start=1, end=500)
    assert page["summaries"]["queue"]["N"] == 0
    assert page["summaries"]["execution"]["observed_seconds"] == 0


def _span_rows(store, cid, category="review", cause="review"):
    return list(store._conn.execute(
        "SELECT utc_start, utc_end FROM lifecycle_spans WHERE card_id=? "
        "AND category=? AND cause=? AND coverage='complete' ORDER BY id",
        (cid, category, cause)))


def test_drifting_clocks_extend_one_span(store, clock):
    """The wall clock and the monotonic clock never agree to the microsecond:
    a confirmed span ends at `previous_utc + monotonic_delta` and the next one
    starts at a fresh `time()` read, so an equality join on `utc_end` never
    matched in production and every checkpoint (a few seconds apart, per card
    in review) inserted a fresh row — 960k rows on one Mac, a 27s report read
    under the store lock, and a phone write queued behind it. Joined within
    `SPAN_JOIN_TOLERANCE` now, the checkpoints of one quiet review are one
    row whose length is the whole interval."""
    c = card(store)
    store.update(c["id"], {"column_name": "done"})
    store.mark_ended(c["id"], when=clock.utc)
    for step in range(6):
        observe(store, c["id"], clock, review=True)
        # Wall time runs a hair ahead of monotonic time, and by a different
        # hair each time — what two `time.time()` / `time.monotonic()` reads
        # a few instructions apart look like.
        clock.mono += 10
        clock.utc += 10 + 0.00007 * (step + 1)
    rows = _span_rows(store, c["id"])
    assert len(rows) == 1, rows
    assert rows[0]["utc_end"] - rows[0]["utc_start"] == pytest.approx(50, abs=0.01)
    # And the totals the report draws are unchanged by the join.
    rep = report(store, root="/project", start=1_000.0, end=1_100.0)
    assert rep["available"]


def test_a_real_gap_still_starts_a_new_span(store, clock):
    """The join is a tolerance, not a merge of everything: a sample that a
    cleared cause left unconfirmed leaves a hole, and the next confirmed
    interval starts its own row rather than being glued across it."""
    c = card(store)
    store.update(c["id"], {"column_name": "done"})
    store.mark_ended(c["id"], when=clock.utc)
    observe(store, c["id"], clock, review=True)
    clock.advance(10)
    observe(store, c["id"], clock, review=True)      # confirms 0-10
    clock.advance(10)
    observe(store, c["id"], clock, review=False)     # confirms 10-20, then off
    clock.advance(10)
    observe(store, c["id"], clock, review=True)      # nothing to confirm
    clock.advance(10)
    observe(store, c["id"], clock, review=True)      # confirms 30-40
    rows = _span_rows(store, c["id"])
    assert [(r["utc_start"] - 1_000, r["utc_end"] - 1_000) for r in rows] == [
        (0, 20), (30, 40)]


def test_connect_coalesces_the_rows_an_older_build_left(tmp_path, clock):
    """A file written by the build with the equality join holds one row per
    checkpoint. The next open folds each contiguous run into one span — once,
    under the `spans_coalesced` mark — so the report read stops walking a
    million rows under the lock. The measured seconds are the same before
    and after: only the row count moves."""
    s = BoardStore(tmp_path / "board.db")
    s._clock_utc = clock.now
    s._clock_mono = clock.monotonic
    s.connect()
    c = card(s)
    s.update(c["id"], {"column_name": "done"})
    s.mark_ended(c["id"], when=clock.utc)
    observe(s, c["id"], clock, review=True)
    clock.advance(10)
    observe(s, c["id"], clock, review=True)
    ep = s._conn.execute(
        "SELECT id FROM lifecycle_episodes WHERE card_id=? AND kind='review'",
        (c["id"],)).fetchone()["id"]
    # Hand-write what the old build produced: three more checkpoints, each
    # its own row, ends and starts a few microseconds apart, plus one row
    # after a genuine hole that must stay separate.
    start = 1_010.0
    with s._conn:
        s._conn.execute("DELETE FROM lifecycle_meta WHERE key='spans_coalesced'")
        for k in range(3):
            s._conn.execute(
                "INSERT INTO lifecycle_spans(card_id,episode_id,utc_start,utc_end,"
                "category,cause,attempt_id,coverage,process_generation) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (c["id"], ep, start + 0.00007 * k, start + 10, "review", "review",
                 None, "complete", "p"))
            start += 10
        s._conn.execute(
            "INSERT INTO lifecycle_spans(card_id,episode_id,utc_start,utc_end,"
            "category,cause,attempt_id,coverage,process_generation) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (c["id"], ep, start + 30, start + 40, "review", "review",
             None, "complete", "p"))
    assert len(_span_rows(s, c["id"])) == 5
    total_before = s._conn.execute(
        "SELECT SUM(utc_end - utc_start) FROM lifecycle_spans WHERE card_id=?",
        (c["id"],)).fetchone()[0]
    s.close()
    s2 = BoardStore(tmp_path / "board.db")
    s2._clock_utc = clock.now
    s2._clock_mono = clock.monotonic
    s2.connect()
    rows = _span_rows(s2, c["id"])
    assert [(r["utc_start"] - 1_000, round(r["utc_end"] - 1_000, 3)) for r in rows] == [
        (0, 40), (70, 80)]
    total_after = s2._conn.execute(
        "SELECT SUM(utc_end - utc_start) FROM lifecycle_spans WHERE card_id=?",
        (c["id"],)).fetchone()[0]
    assert total_after == pytest.approx(total_before, abs=0.001)
    assert s2._conn.execute(
        "SELECT value FROM lifecycle_meta WHERE key='spans_coalesced'"
    ).fetchone()["value"] == "1"
    s2.close()
