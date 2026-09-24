"""Live cost and time on the card (`plans/2026-09-13-card-cost-and-time.md`).

Composition (`run_figures.py`), the store read over the two ledgers
(`BoardStore.run_figures`), the decorate's absent-where-no-run rule, the
drift check that buys the frame, the phone's version marker, the module's
import purity and the panel/phone byte-pin of the wording rule.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dark_army_daemon import run_figures
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon

REPO = Path(__file__).resolve().parents[2]
PANEL_FILE = REPO / "panel" / "Sources" / "BobPanel" / "RunFigures.swift"
PHONE_FILE = REPO / "ios" / "BobPhone" / "RunFigures.swift"
MODULE_FILE = REPO / "host" / "dark_army_daemon" / "run_figures.py"
MARKER = "enum RunFigures {"


# --- composition ----------------------------------------------------------

def _cost(usd=1.4, coverage="complete"):
    return {"totals": {"USD": usd} if usd is not None else {}, "coverage": coverage}


def test_compose_floors_seconds_to_the_minute_and_rounds_cost_to_cents():
    out = run_figures.compose(cost=_cost(1.23456), seconds=119.9, ticking=True,
                              ctx=61.7, attempts=1)
    assert out == {"cost_usd": 1.23, "cost_coverage": "complete",
                   "active_seconds": 60, "active_ticking": True,
                   "ctx_percent": 61, "attempts": 1}
    assert run_figures.compose(cost=_cost(), seconds=59, ticking=False,
                               ctx=None, attempts=0)["active_seconds"] == 0
    assert run_figures.compose(cost=_cost(), seconds=3600, ticking=False,
                               ctx=None, attempts=0)["active_seconds"] == 3600


def test_compose_keeps_no_execution_span_apart_from_under_a_minute():
    """A refined-but-never-started card has a run row and no working time:
    the time part is absent, not "under a minute"."""
    out = run_figures.compose(cost=_cost(0.31), seconds=None, ticking=False,
                              ctx=None, attempts=0)
    assert out["active_seconds"] is None
    assert run_figures.compose(cost=_cost(), seconds=0, ticking=False,
                               ctx=None, attempts=0)["active_seconds"] == 0


def test_compose_maps_unknown_coverage_to_no_cost_and_keeps_partial():
    unknown = run_figures.compose(cost=_cost(0.5, "unknown"), seconds=0,
                                  ticking=False, ctx=None, attempts=1)
    assert unknown["cost_usd"] is None
    assert unknown["cost_coverage"] == "unknown"
    # No USD total at all reads as unknown too — never a made-up zero.
    none = run_figures.compose(cost={"totals": {}, "coverage": "complete"},
                               seconds=0, ticking=False, ctx=None, attempts=1)
    assert none["cost_usd"] is None
    assert none["cost_coverage"] == "unknown"
    partial = run_figures.compose(cost=_cost(2.0, "partial"), seconds=0,
                                  ticking=False, ctx=None, attempts=1)
    assert partial == {"cost_usd": 2.0, "cost_coverage": "partial",
                       "active_seconds": 0, "active_ticking": False,
                       "ctx_percent": None, "attempts": 1}
    assert run_figures.COST_UNKNOWN == "cost unknown"
    assert run_figures.ACTIVE_QUANTUM_SECONDS == 60


def test_drift_key_is_stable_across_a_sub_quantum_move():
    """Thirty seconds, three cents and one point of context later, the
    same key: the cent and the percent are drawn, but the frame is bought
    at the minute, the dime and five points — not once an observation pass.
    The pause (`active_ticking`) is not a term at all; nothing draws it."""
    before = run_figures.compose(cost=_cost(1.21), seconds=65, ticking=True,
                                 ctx=61.0, attempts=1)
    after = run_figures.compose(cost=_cost(1.24), seconds=95, ticking=False,
                                ctx=62.0, attempts=1)
    assert before["cost_usd"] != after["cost_usd"]
    assert before["ctx_percent"] != after["ctx_percent"]
    assert run_figures.drift_key(before) == run_figures.drift_key(after)


@pytest.mark.parametrize("field, value", [
    ("seconds", 160),       # the minute moved
    ("cost", 1.35),         # the dime moved
    ("ctx", 66),            # five points of context moved
    ("attempts", 2),        # a second attempt bound
])
def test_drift_key_changes_on_a_quantum_move(field, value):
    base = dict(cost=_cost(1.234), seconds=100, ticking=True, ctx=61.2, attempts=1)
    moved = dict(base)
    moved[field] = _cost(value) if field == "cost" else value
    assert run_figures.drift_key(run_figures.compose(**base)) != \
        run_figures.drift_key(run_figures.compose(**moved))


def test_drift_key_moves_when_the_time_part_first_appears():
    base = dict(cost=_cost(0.31), seconds=None, ticking=False, ctx=None, attempts=0)
    assert run_figures.drift_key(run_figures.compose(**base)) != \
        run_figures.drift_key(run_figures.compose(**dict(base, seconds=5)))


# --- the store read -------------------------------------------------------

@pytest.fixture
def setup(tmp_path, monkeypatch):
    d = BobDaemon(headless=True)
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    d._board = s
    monkeypatch.setattr(d, "_publish_board", AsyncMock())
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {})
    c, reason = s.create(dict(title="Work", root=str(tmp_path), tool="claude"))
    assert reason == "created"
    yield d, s, c
    s.close()


def _clocks(store, start=None):
    """Fake, advancing clocks for the lifecycle ledger so a span has a
    length a test can read rather than the microseconds two adjacent calls
    would give it. Starts at the real clock: `lifecycle_prune` on the next
    `connect()` trims spans older than 366 days, and a span dated 1970 is
    exactly that. `observe_outcome` reads the real clock, which is fine —
    the cost path keys on the reading, not on elapsed time."""
    state = {"t": time.time() if start is None else float(start)}

    def tick(seconds):
        state["t"] += seconds

    store._clock_utc = lambda: state["t"]
    store._clock_mono = lambda: state["t"]
    return tick


def test_a_fresh_card_has_no_figures(setup):
    _, s, c = setup
    assert s.run_figures() == {}


def test_a_measured_cost_is_present(setup):
    _, s, c = setup
    assert s.record_outcome_run(c["id"], "claude", "s1", "implementation")
    s.observe_outcome(c["id"], set(), costs=[dict(
        provider="claude", session_id="s1", amount=1.234, currency="USD",
        source="live_measured", complete=True)])
    parts = s.run_figures()[c["id"]]
    assert parts["cost"] == {"totals": {"USD": 1.234}, "coverage": "complete"}
    assert parts["seconds"] is None
    assert parts["ticking"] is False
    assert parts["attempts"] == 1


def test_a_plain_live_reading_grades_complete_for_the_card(setup):
    """The ledger stores every live and history reading as `partial`
    (`cost_coverage`) because neither proves child inclusion; the card's
    line grades from `late` / `cost_conflict` instead, so a plain measured
    run reads complete and "(partial)" keeps the plan's meaning."""
    _, s, c = setup
    s.record_outcome_run(c["id"], "claude", "s1", "implementation")
    s.observe_outcome(c["id"], set(), costs=[dict(
        provider="claude", session_id="s1", amount=1.0, currency="USD",
        source="live_measured", complete=False)])
    row = s._conn.execute("SELECT cost_coverage FROM outcome_runs").fetchone()
    assert row["cost_coverage"] == "partial"          # the report's own grade
    assert s.run_figures()[c["id"]]["cost"]["coverage"] == "complete"


def test_a_late_bind_grades_partial_for_the_card(setup):
    _, s, c = setup
    s.record_outcome_run(c["id"], "claude", "s1", "implementation", late=True)
    s.observe_outcome(c["id"], set(), costs=[dict(
        provider="claude", session_id="s1", amount=1.0, currency="USD",
        source="live_measured", complete=False)])
    assert s.run_figures()[c["id"]]["cost"]["coverage"] == "partial"


def test_a_refinement_only_card_has_cost_and_no_time(setup):
    """Refined, never started: a run row, no execution span, so the line
    carries the planner's spend and no time part."""
    _, s, c = setup
    s.record_outcome_run(c["id"], "claude", "plan1", "refinement")
    s.observe_outcome(c["id"], set(), costs=[dict(
        provider="claude", session_id="plan1", amount=0.31, currency="USD",
        source="live_measured", complete=False)])
    parts = s.run_figures()[c["id"]]
    assert parts["seconds"] is None
    assert parts["attempts"] == 0
    figures = run_figures.compose(cost=parts["cost"], seconds=parts["seconds"],
                                  ticking=parts["ticking"], ctx=None,
                                  attempts=parts["attempts"])
    assert figures["active_seconds"] is None
    assert figures["cost_usd"] == 0.31


def test_a_lower_reading_keeps_the_known_amount_and_reads_partial(setup):
    """The store's own rule, re-asserted through this read: a decrease
    preserves the known amount and flips the coverage."""
    _, s, c = setup
    s.record_outcome_run(c["id"], "claude", "s1", "implementation")
    reading = dict(provider="claude", session_id="s1", amount=2.0,
                   currency="USD", source="live_measured", complete=True)
    s.observe_outcome(c["id"], set(), costs=[reading])
    s.observe_outcome(c["id"], set(), costs=[dict(reading, amount=1.0)])
    parts = s.run_figures()[c["id"]]
    assert parts["cost"]["totals"] == {"USD": 2.0}
    assert parts["cost"]["coverage"] == "partial"


def test_two_implementation_sessions_are_two_attempts_and_one_sum(setup):
    _, s, c = setup
    s.record_outcome_run(c["id"], "claude", "s1", "implementation")
    s.record_outcome_run(c["id"], "claude", "s2", "implementation")
    for sid, amount in (("s1", 1.0), ("s2", 0.5)):
        s.observe_outcome(c["id"], set(), costs=[dict(
            provider="claude", session_id=sid, amount=amount, currency="USD",
            source="live_measured", complete=True)])
    parts = s.run_figures()[c["id"]]
    assert parts["attempts"] == 2
    assert parts["cost"]["totals"] == {"USD": 1.5}


def test_a_refinement_session_counts_cost_but_not_attempts(setup):
    _, s, c = setup
    s.record_outcome_run(c["id"], "claude", "s1", "implementation")
    s.record_outcome_run(c["id"], "claude", "plan1", "refinement")
    for sid, amount in (("s1", 1.0), ("plan1", 0.25)):
        s.observe_outcome(c["id"], set(), costs=[dict(
            provider="claude", session_id=sid, amount=amount, currency="USD",
            source="live_measured", complete=True)])
    parts = s.run_figures()[c["id"]]
    assert parts["attempts"] == 1
    assert parts["cost"]["totals"] == {"USD": 1.25}
    assert parts["cost"]["coverage"] == "complete"


def test_an_unmeasured_run_beside_a_measured_one_reads_partial(setup):
    """A Codex refinement Dark Army could not price beside a measured Claude
    implementation: part of the card's spend is unknown, so the sum says so."""
    _, s, c = setup
    s.record_outcome_run(c["id"], "codex", "plan1", "refinement")
    s.record_outcome_run(c["id"], "claude", "s1", "implementation")
    s.observe_outcome(c["id"], set(), costs=[dict(
        provider="claude", session_id="s1", amount=1.0, currency="USD",
        source="live_measured", complete=False)])
    assert s.run_figures()[c["id"]]["cost"] == {"totals": {"USD": 1.0},
                                                "coverage": "partial"}


def test_execution_seconds_accrue_while_live_and_working(setup, monkeypatch):
    d, s, c = setup
    tick = _clocks(s)
    s.update(c["id"], {"link_state": "dispatching", "dispatched_at": 1}, bump=False)
    s.bind_session(c["id"], "sid")
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    snapshot = {"running": [{"session_id": "sid", "provider": "claude"}]}
    d._observe_board_outcomes(snapshot, {"sid"})
    tick(10)
    d._observe_board_outcomes(snapshot, {"sid"})
    parts = s.run_figures()[c["id"]]
    assert parts["seconds"] == pytest.approx(10.0)
    assert parts["ticking"] is True
    # A question on the row pauses the count: the checkpoint's memberships
    # lose `execution`, so the figure stops ticking.
    tick(10)
    asked = {"running": [{"session_id": "sid", "provider": "claude",
                          "question": {"text": "Which?"}}]}
    d._observe_board_outcomes(asked, {"sid"})
    parts = s.run_figures()[c["id"]]
    assert parts["ticking"] is False
    assert parts["seconds"] == pytest.approx(20.0)
    tick(10)
    d._observe_board_outcomes(asked, {"sid"})
    assert s.run_figures()[c["id"]]["seconds"] == pytest.approx(20.0)


# --- decorate -------------------------------------------------------------

def _ran(d, s, card_id, sid, *, tick, provider="claude", ctx=61.0):
    s.update(card_id, {"link_state": "dispatching", "dispatched_at": 1}, bump=False)
    s.bind_session(card_id, sid)
    # What `_bind_dispatched_card` does beside the bind
    # (`_record_outcome_binding`, late=False); the observer alone would
    # record the run as a late bind, which is the ledger's "partial".
    s.record_outcome_run(card_id, provider, sid, "implementation")
    row = {"session_id": sid, "provider": provider,
           "metrics": {"cost_usd": 1.234, "ctx_used_pct": ctx}}
    snapshot = {"running": [row], "waiting": [], "sleeping": [], "finished": []}
    d._agents_snapshot_cache = snapshot
    d._observe_board_outcomes(snapshot, {sid})
    tick(10)
    d._observe_board_outcomes(snapshot, {sid})
    return snapshot


def test_decorate_stamps_figures_on_a_run_card_and_nothing_on_an_untouched_one(setup, monkeypatch):
    d, s, c = setup
    tick = _clocks(s)
    other, _ = s.create(dict(title="Untouched", root=c["root"], tool="claude"))
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    _ran(d, s, c["id"], "sid", tick=tick)
    cards = {x["id"]: x for x in d._build_board_state()["cards"]}
    figures = cards[c["id"]]["run_figures"]
    assert set(figures) == {"cost_usd", "cost_coverage", "active_seconds",
                            "active_ticking", "ctx_percent", "attempts"}
    assert figures["cost_usd"] == 1.23
    assert figures["cost_coverage"] == "complete"  # a plain measured reading, nothing went wrong
    assert figures["active_ticking"] is True
    assert figures["ctx_percent"] == 61
    assert figures["attempts"] == 1
    assert "run_figures" not in cards[other["id"]]


def test_a_codex_card_says_cost_unknown_rather_than_zero(setup, monkeypatch):
    d, s, c = setup
    tick = _clocks(s)
    s.update(c["id"], {"tool": "codex"})
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"cx"})
    _ran(d, s, c["id"], "cx", tick=tick, provider="codex")
    figures = next(x for x in d._build_board_state()["cards"]
                   if x["id"] == c["id"])["run_figures"]
    assert figures["cost_usd"] is None
    assert figures["cost_coverage"] == "unknown"
    assert figures["active_ticking"] is True


def test_ctx_percent_rides_only_a_live_link_in_a_live_bucket(setup, monkeypatch):
    d, s, c = setup
    tick = _clocks(s)
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    snapshot = _ran(d, s, c["id"], "sid", tick=tick)

    def figures():
        return next(x for x in d._build_board_state()["cards"]
                    if x["id"] == c["id"])["run_figures"]

    assert figures()["ctx_percent"] == 61
    # A live session quiet past the idle grace sits in `finished` flagged
    # `alive`: still live, still its context percent — no flicker.
    d._agents_snapshot_cache = {"running": [], "waiting": [], "sleeping": [],
                                "finished": [dict(snapshot["running"][0], alive=True)]}
    assert figures()["ctx_percent"] == 61
    # The same row filed under `finished` without the flag is a tombstone,
    # and its last reading is not a live context percent.
    d._agents_snapshot_cache = {"running": [], "waiting": [], "sleeping": [],
                                "finished": snapshot["running"]}
    assert figures()["ctx_percent"] is None
    # A row in a live bucket whose card link has ended: the figure is gone.
    d._agents_snapshot_cache = snapshot
    s.mark_ended(c["id"])
    assert figures()["ctx_percent"] is None
    assert figures()["cost_usd"] == 1.23   # the total stays


def test_done_archive_carries_the_finished_totals(setup, monkeypatch):
    d, s, c = setup
    tick = _clocks(s)
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    _ran(d, s, c["id"], "sid", tick=tick)
    s.mark_ended(c["id"])
    assert s.declare_done(c["id"], "sid", "finished")[0]
    card = next(x for x in d.done_archive_cards()["cards"] if x["id"] == c["id"])
    assert card["run_figures"]["cost_usd"] == 1.23
    assert card["run_figures"]["ctx_percent"] is None


def test_the_marker_rides_pipeline_writable(setup):
    d, _, _ = setup
    assert BobDaemon._pipeline_writable(d)["run_figures_supported"] is True


# --- drift ----------------------------------------------------------------

def test_drift_buys_one_frame_then_none_on_the_same_ledgers(setup, monkeypatch):
    d, s, c = setup
    tick = _clocks(s)
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    _ran(d, s, c["id"], "sid", tick=tick)
    assert d._run_figures_drifted() is True
    assert d._run_figures_drifted() is False
    # Thirty more seconds inside the same minute: still no frame.
    for _ in range(3):
        tick(10)
        d._observe_board_outcomes(d._agents_snapshot_cache, {"sid"})
    assert d._run_figures_drifted() is False
    # Past the minute: one frame.
    for _ in range(3):
        tick(10)
        d._observe_board_outcomes(d._agents_snapshot_cache, {"sid"})
    assert d._run_figures_drifted() is True
    assert d._run_figures_drifted() is False
    # The statusline's own cadence — a cent and a point of context per
    # pass — buys nothing inside the minute; a dime does.
    row = d._agents_snapshot_cache["running"][0]
    row["metrics"] = {"cost_usd": 1.244, "ctx_used_pct": 62.0}
    tick(10)
    d._observe_board_outcomes(d._agents_snapshot_cache, {"sid"})
    assert d._run_figures_drifted() is False
    row["metrics"] = {"cost_usd": 1.35, "ctx_used_pct": 62.0}
    tick(10)
    d._observe_board_outcomes(d._agents_snapshot_cache, {"sid"})
    assert d._run_figures_drifted() is True


def test_run_figures_memo_runs_no_sql_without_a_ledger_write(setup):
    _, s, c = setup
    assert s.record_outcome_run(c["id"], "claude", "s1", "implementation")
    first = s.run_figures()
    statements = []
    s._conn.set_trace_callback(statements.append)
    try:
        again = s.run_figures()
    finally:
        s._conn.set_trace_callback(None)
    assert again == first
    assert statements == []


def test_run_figures_memo_recomputes_after_a_ledger_write(setup):
    _, s, c = setup
    assert s.run_figures() == {}
    assert s.record_outcome_run(c["id"], "claude", "s1", "implementation")
    statements = []
    s._conn.set_trace_callback(statements.append)
    try:
        parts = s.run_figures()
    finally:
        s._conn.set_trace_callback(None)
    assert statements
    assert parts[c["id"]]["attempts"] == 1


def test_drift_check_survives_a_broken_store(setup, monkeypatch):
    d, s, _ = setup
    monkeypatch.setattr(s, "cards", lambda: (_ for _ in ()).throw(OSError("disk")))
    assert d._run_figures_drifted() is False


def test_drift_check_is_in_the_reconcile():
    src = (REPO / "host" / "dark_army_daemon" / "daemon_board.py").read_text()
    body = src.split("def _reconcile_board(", 1)[1].split("\n    def ", 1)[0]
    assert "self._observe_board_outcomes(snapshot, active)" in body
    assert body.index("_observe_board_outcomes") < body.index("_run_figures_drifted")


# --- the finished total survives a restart ---------------------------------

def test_a_restart_keeps_the_finished_total_and_attempts(tmp_path, monkeypatch):
    d = BobDaemon(headless=True)
    s = BoardStore(tmp_path / "board.db")
    s.connect()
    d._board = s
    monkeypatch.setattr(d, "_publish_board", AsyncMock())
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {})
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"sid"})
    c, _ = s.create(dict(title="Work", root=str(tmp_path), tool="claude"))
    tick = _clocks(s)
    _ran(d, s, c["id"], "sid", tick=tick)
    s.mark_ended(c["id"])
    before = s.run_figures()[c["id"]]
    assert before["seconds"] == pytest.approx(10.0)
    s.close()

    reopened = BoardStore(tmp_path / "board.db")
    reopened.connect()
    try:
        after = reopened.run_figures()[c["id"]]
    finally:
        reopened.close()
    assert after["cost"] == before["cost"]
    assert after["seconds"] == pytest.approx(before["seconds"])
    assert after["attempts"] == before["attempts"] == 1
    assert after["ticking"] is False


# --- purity and the byte-pinned pair -------------------------------------

def test_run_figures_module_reads_neither_samples_nor_stats_nor_pricing():
    src = MODULE_FILE.read_text()
    assert not re.search(r"import (samples|session_stats|pricing)|from \. import .*(samples|session_stats|pricing)", src)
    for name in ("samples", "session_stats", "pricing", "sqlite3"):
        assert not re.search(rf"\b{name}\b", src), name


def _shared(path: Path) -> str:
    lines = path.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


def test_the_panel_and_phone_share_one_wording_rule():
    panel, phone = _shared(PANEL_FILE), _shared(PHONE_FILE)
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 1500, f"parsed too little from the {name} copy"
        assert "static func line(" in region
        assert '"cost unknown"' in region
        assert '"(partial)"' in region or '" (partial)"' in region
    assert panel == phone, (
        "ios/BobPhone/RunFigures.swift has drifted from the panel's copy — "
        "mirror the edit onto the other side; never re-baseline one side.")
    assert "import Foundation" in PHONE_FILE.read_text()
    assert "SwiftUI" not in phone


def test_the_phone_project_compiles_the_shared_file():
    project = (REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj").read_text()
    assert "RunFigures.swift in Sources" in project


def test_the_swift_word_for_unknown_cost_is_the_daemon_s():
    assert f'"{run_figures.COST_UNKNOWN}"' in _shared(PANEL_FILE)
