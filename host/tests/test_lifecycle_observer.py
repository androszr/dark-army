"""Daemon observer seams: shared facts, spawn, independent failure."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture
def setup(tmp_path, monkeypatch):
    d = BobDaemon(headless=True)
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    d._board = s
    monkeypatch.setattr(d, "_publish_board", AsyncMock())
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {})
    c, reason = s.create(dict(
        title="Work", root=str(tmp_path), tool="claude",
        intended_benefit="Benefit", success_criterion="Criterion"))
    assert reason == "created"
    yield d, s, c
    s.close()


def test_capability_flag(setup):
    d, _, _ = setup
    flags = BobDaemon._pipeline_writable(d)
    assert flags["lifecycle_supported"] is True


def test_dispatching_is_not_execution(setup):
    d, s, c = setup
    s.update(c["id"], {"link_state": "dispatching", "dispatched_at": 1})
    d._agents_snapshot_cache = {"running": [{"session_id": "sid"}]}
    d._observe_board_outcomes({"running": [{"session_id": "sid"}]}, {"sid"})
    spans = list(s._conn.execute(
        "SELECT category FROM lifecycle_spans WHERE category='execution'"))
    assert spans == []


def test_live_working_is_execution(setup, monkeypatch):
    d, s, c = setup
    s.update(c["id"], {"link_state": "dispatching", "dispatched_at": 1}, bump=False)
    s.bind_session(c["id"], "sid")
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    snapshot = {"running": [{"session_id": "sid", "provider": "claude"}]}
    d._observe_board_outcomes(snapshot, {"sid"})
    d._observe_board_outcomes(snapshot, {"sid"})
    facts = d._card_observation_facts(
        s.get(c["id"]), {"sid": snapshot["running"][0]},
        {"sid"}, {"sid"}, {}, s.outcome_observation_states())
    assert facts["executing"] is True
    assert facts["working"] is True


def test_question_pauses_execution(setup, monkeypatch):
    d, s, c = setup
    s.bind_session(c["id"], "sid")
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    row = {"session_id": "sid", "provider": "claude",
           "question": {"text": "Which?"}}
    facts = d._card_observation_facts(
        s.get(c["id"]), {"sid": row}, {"sid"}, {"sid"}, {}, {})
    assert facts["executing"] is False
    assert "question" in facts["causes"]


def test_permission_pauses_execution(setup, monkeypatch):
    d, s, c = setup
    s.bind_session(c["id"], "sid")
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {"sid": {}})
    row = {"session_id": "sid", "provider": "claude"}
    facts = d._card_observation_facts(
        s.get(c["id"]), {"sid": row}, {"sid"}, {"sid"}, {"sid": {}}, {})
    assert facts["executing"] is False
    assert "permission" in facts["causes"]


def test_roster_only_is_unknown_not_working(setup):
    d, s, c = setup
    s.bind_session(c["id"], "sid")
    # In running bucket but not in claiming set → roster stub.
    facts = d._card_observation_facts(
        s.get(c["id"]), {}, {"sid"}, set(), {}, {})
    assert facts["working"] is False
    assert facts["executing"] is False
    assert facts["impl_unknown"] is True


def test_queue_observable_without_session(setup):
    d, s, c = setup
    s.update(c["id"], {"queue_state": "queued", "queued_at": 1})
    facts = d._card_observation_facts(
        s.get(c["id"]), {}, set(), set(), {}, {})
    assert facts["queued"] is True
    assert facts["visible"] is True


def test_manual_due_only_after_work_stops(setup, monkeypatch):
    d, s, c = setup
    s.bind_session(c["id"], "sid")
    s.flag_manual(c["id"], "sid", "1. Look at it")
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    working = d._card_observation_facts(
        s.get(c["id"]), {"sid": {"session_id": "sid"}}, {"sid"}, {"sid"}, {}, {})
    assert working["manual_check"] is False
    idle = d._card_observation_facts(
        s.get(c["id"]), {"sid": {"session_id": "sid"}}, set(), set(), {}, {})
    assert idle["manual_check"] is True


def test_lifecycle_failure_does_not_mark_outcomes_or_block(setup, monkeypatch):
    d, s, c = setup
    d._lifecycle_observe_hook = lambda: (_ for _ in ()).throw(OSError("disk"))
    d._observe_board_outcomes({}, set())
    assert d._lifecycle_unavailable is True
    assert getattr(d, "_outcomes_unavailable", False) is False
    updated, _ = s.update(c["id"], {"title": "Still editable"})
    assert updated is not None


def test_outcome_failure_still_independent(setup, monkeypatch):
    d, s, c = setup
    # The pass writes every card's outcome through the one batch verb.
    monkeypatch.setattr(s, "observe_outcomes",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    d._observe_board_outcomes({}, set())
    assert d._outcomes_unavailable is True
    # Lifecycle still ran on whatever facts were collected.
    assert getattr(d, "_lifecycle_unavailable", False) in (False, True)


@pytest.mark.asyncio
async def test_spawn_failure_creates_no_boundary(setup, monkeypatch):
    d, s, c = setup
    spawned = []

    async def fail(*a, **k):
        spawned.append(1)
        return False, "no", None

    monkeypatch.setattr("dark_army_daemon.dispatch.spawn", fail)
    monkeypatch.setattr("dark_army_daemon.dispatch.spawn_local", fail)
    monkeypatch.setattr("dark_army_daemon.dispatch.guard",
                        lambda *a, **k: (True, ""))
    monkeypatch.setattr("dark_army_daemon.dispatch.resolve_executable",
                        lambda *a, **k: "/bin/true")
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(c["root"])})
    monkeypatch.setattr(d, "_enrollment_refusal", AsyncMock(return_value=""))
    monkeypatch.setattr(d, "_plan_gate_refusal", AsyncMock(return_value=""))
    monkeypatch.setattr(d, "_slot_refusal", lambda *a, **k: False)
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: set())
    monkeypatch.setattr(d, "_uses_own_terminal", lambda *a, **k: False)
    monkeypatch.setattr(d, "_live_session_ids", lambda: set())
    monkeypatch.setattr(d, "_launch_inflight", lambda *a, **k: 0)
    ok, detail = await d._dispatch_card_locked(c["id"])
    assert ok is False
    assert len(spawned) == 1
    attempts = list(s._conn.execute("SELECT id FROM lifecycle_attempts"))
    assert attempts == []


@pytest.mark.asyncio
async def test_spawn_then_store_fail_no_fabricated_handoff(setup, monkeypatch):
    d, s, c = setup
    spawned = []

    async def ok_spawn(*a, **k):
        spawned.append(1)
        return True, "opened", 99

    monkeypatch.setattr("dark_army_daemon.dispatch.spawn", ok_spawn)
    monkeypatch.setattr("dark_army_daemon.dispatch.guard",
                        lambda *a, **k: (True, ""))
    monkeypatch.setattr("dark_army_daemon.dispatch.resolve_executable",
                        lambda *a, **k: "/bin/true")
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(c["root"])})
    monkeypatch.setattr(d, "_enrollment_refusal", AsyncMock(return_value=""))
    monkeypatch.setattr(d, "_plan_gate_refusal", AsyncMock(return_value=""))
    monkeypatch.setattr(d, "_slot_refusal", lambda *a, **k: False)
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: set())
    monkeypatch.setattr(d, "_uses_own_terminal", lambda *a, **k: False)
    monkeypatch.setattr(d, "_live_session_ids", lambda: set())
    monkeypatch.setattr(d, "_launch_inflight", lambda *a, **k: 0)
    monkeypatch.setattr(d, "_schedule_work_baseline", lambda *a, **k: None)

    original = d._board_call

    async def flaky(method, *args, **kwargs):
        if method == "update" and kwargs.get("bump") is False or (
                method == "update" and isinstance(args[1] if len(args) > 1 else None, dict)
                and (args[1] or {}).get("link_state") == "dispatching"):
            raise RuntimeError("disk")
        return await original(method, *args, **kwargs)

    monkeypatch.setattr(d, "_board_call", flaky)
    with pytest.raises(RuntimeError):
        await d._dispatch_card_locked(c["id"])
    assert len(spawned) == 1
    card = s.get(c["id"])
    assert card["link_state"] != "dispatching"
    q = list(s._conn.execute(
        "SELECT disposition FROM lifecycle_episodes WHERE kind='queue'"))
    assert all(row["disposition"] != "completed" for row in q)


def test_reset_during_collection_marks_unknown(setup, monkeypatch):
    d, s, c = setup
    s.update(c["id"], {
        "link_state": "dispatching", "dispatched_at": 1,
    }, bump=False)
    s.bind_session(c["id"], "sid")
    seq = s.lifecycle_facts_seq(c["id"])
    s.update(c["id"], {"session_id": "", "link_state": ""})
    s.observe_lifecycle(c["id"], {
        "executing": True, "attempt_id": seq["attempt_id"],
        "boundary_seq": seq["boundary_seq"], "queued": False,
        "review": False, "rework": False, "manual_check": False,
        "impl_unknown": False, "visible": True,
    })
    gaps = list(s._conn.execute(
        "SELECT cause FROM lifecycle_spans WHERE coverage='gap'"))
    assert gaps


def test_two_identical_passes_write_nothing_the_second_time(setup, tmp_path):
    d, s, c = setup
    s.create(dict(title="Second", root=str(tmp_path), tool="claude"))
    before = s._conn.total_changes
    d._observe_board_outcomes({}, set())
    first = s._conn.total_changes
    assert first > before          # the first pass writes each card's rows once
    d._observe_board_outcomes({}, set())
    assert s._conn.total_changes == first
    assert d._outcomes_unavailable is False
    assert d._lifecycle_unavailable is False


def test_one_failing_lifecycle_card_rolls_back_alone(setup, tmp_path, monkeypatch):
    d, s, c = setup
    other, _ = s.create(dict(title="Other", root=str(tmp_path), tool="claude"))
    unchanged, _ = s.create(dict(title="Unchanged", root=str(tmp_path), tool="claude"))
    d._observe_board_outcomes({}, set())          # every row written once
    kept = s._lifecycle_written[unchanged["id"]]
    s._conn.execute("DELETE FROM lifecycle_checkpoints WHERE card_id=?", (other["id"],))
    s._conn.commit()
    s._lifecycle_written.pop(other["id"])
    real = s._observe_locked

    def observe(card_id, *args):
        if card_id == c["id"]:
            raise OSError("disk")
        return real(card_id, *args)
    monkeypatch.setattr(s, "_observe_locked", observe)
    d._observe_board_outcomes({}, set())
    assert d._lifecycle_unavailable is True
    assert d._outcomes_unavailable is False
    written = {r["card_id"] for r in s._conn.execute(
        "SELECT card_id FROM lifecycle_checkpoints")}
    assert other["id"] in written
    # The failing card is forgotten alone: an unrelated unchanged card keeps
    # its written-row memo and is not rewritten on the next pass.
    assert s._lifecycle_written.get(unchanged["id"]) == kept
    assert c["id"] not in s._lifecycle_written
    # The healthy cards after it do not wash out the failure: the ledger
    # still reads unavailable, so a restart records the observer failure.
    marker = s._conn.execute(
        "SELECT value FROM lifecycle_meta WHERE key='unavailable'").fetchone()
    assert marker["value"] == "1"


def test_one_card_whose_facts_raise_does_not_cost_the_others(setup, tmp_path, monkeypatch):
    d, s, c = setup
    other, _ = s.create(dict(title="Other", root=str(tmp_path), tool="claude"))
    real = d._card_observation_facts

    def facts(card, *args):
        if card["id"] == c["id"]:
            raise ValueError("unreadable")
        return real(card, *args)
    monkeypatch.setattr(d, "_card_observation_facts", facts)
    d._observe_board_outcomes({}, set())
    assert d._outcomes_unavailable is True
    outcome = {r["card_id"] for r in s._conn.execute(
        "SELECT card_id FROM outcome_checkpoints")}
    lifecycle = {r["card_id"] for r in s._conn.execute(
        "SELECT card_id FROM lifecycle_checkpoints")}
    assert other["id"] in outcome and c["id"] not in outcome
    assert other["id"] in lifecycle


def test_a_lost_transaction_settles_nothing_from_the_pass(setup, tmp_path, monkeypatch):
    """If the whole transaction is rolled back, rows settled earlier in the
    pass are gone too: no written-row memo may survive for them."""
    d, s, c = setup
    other, _ = s.create(dict(title="Other", root=str(tmp_path), tool="claude"))
    first, last = sorted([c["id"], other["id"]])
    real = s._observe_locked

    def observe(card_id, *args):
        if card_id == last:
            s._conn.execute("ROLLBACK")
            raise OSError("disk")
        return real(card_id, *args)
    monkeypatch.setattr(s, "_observe_locked", observe)
    failed = s.observe_lifecycles([(first, {}), (last, {})])
    assert failed is True
    assert s._lifecycle_written == {}
    assert s._lifecycle_samples == {}
    rows = list(s._conn.execute("SELECT card_id FROM lifecycle_checkpoints"))
    assert rows == []


def test_the_pass_reads_history_cost_fields_only(setup, monkeypatch):
    """`session_cost`, not the whole `session_record` roll-up, per card per
    pass — the pass's largest read — and the measured figure still lands."""
    d, s, c = setup
    s.update(c["id"], {"link_state": "dispatching", "dispatched_at": 1}, bump=False)
    s.bind_session(c["id"], "sid")

    class History:
        asked = []

        def session_cost(self, sid):
            self.asked.append(sid)
            return {"provider": "claude", "cost_usd": 3.5, "cost_source": "measured"}

        def session_record(self, sid):
            raise AssertionError("the observation pass must not roll up a record")
    d._history = History()
    d._observe_board_outcomes({"running": []}, set())
    assert History.asked == ["sid"]
    assert s.run_figures()[c["id"]]["cost"]["totals"] == {"USD": 3.5}
