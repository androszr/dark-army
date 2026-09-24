# host/tests/test_fleet_figures.py
"""The live fleet's cost and tokens, composed once on the Mac.

Seam: snapshot dict literals. The module imports nothing but `run_health`,
`run_figures` and the stdlib — `test_run_figures.py`'s purity shape.
"""

from __future__ import annotations

import re
from pathlib import Path

from dark_army_daemon import fleet_figures, run_figures, run_health

MODULE = Path(__file__).resolve().parents[1] / "dark_army_daemon" / "fleet_figures.py"


def test_an_empty_snapshot_has_measured_nothing():
    out = fleet_figures.compose({})
    assert out == {"cost_usd": None, "cost_measured": 0, "cost_rows": 0,
                   "tokens_k": None}
    assert fleet_figures.compose(None) == out


def test_one_measured_cost_rounds_to_the_cent_and_an_empty_metrics_does_not():
    snap = {"running": [
        {"session_id": "a", "metrics": {"cost_usd": 1.234}},
        {"session_id": "b", "metrics": {}},
    ]}
    out = fleet_figures.compose(snap)
    assert out["cost_usd"] == 1.23
    assert out["cost_measured"] == 1
    assert out["cost_rows"] == 2
    assert out["tokens_k"] is None


def test_a_bool_or_a_string_cost_is_not_measured():
    snap = {"running": [
        {"session_id": "a", "metrics": {"cost_usd": True}},
        {"session_id": "b", "metrics": {"cost_usd": "1.00"}},
        {"session_id": "c", "metrics": {"cost_usd": False}},
    ]}
    out = fleet_figures.compose(snap)
    assert out["cost_usd"] is None
    assert out["cost_measured"] == 0
    assert out["cost_rows"] == 3


def test_a_finished_row_counts_only_while_it_is_still_alive():
    alive = {"session_id": "a", "alive": True, "metrics": {"cost_usd": 2}}
    dead = {"session_id": "b", "alive": False, "metrics": {"cost_usd": 9}}
    unflagged = {"session_id": "c", "metrics": {"cost_usd": 8}}
    out = fleet_figures.compose({"finished": [alive, dead, unflagged]})
    assert out["cost_usd"] == 2.0
    assert out["cost_measured"] == 1
    assert out["cost_rows"] == 1


def test_tokens_are_fresh_input_plus_output_never_cache_reads():
    snap = {"waiting": [{
        "session_id": "w",
        "stats": {"total_input_tokens": 5_000, "output_tokens": 500,
                  "cache_read_tokens": 9_999_999},
    }]}
    out = fleet_figures.compose(snap)
    assert run_health.tokens_of(snap["waiting"][0]["stats"]) == 5_500
    assert out["tokens_k"] == 5


def test_a_large_token_sum_quantises_to_two_figures():
    snap = {"running": [{
        "session_id": "a",
        "stats": {"total_input_tokens": 1_234_567, "output_tokens": 0},
    }]}
    assert fleet_figures.compose(snap)["tokens_k"] == 1200
    assert run_health.quantise_tokens_k(1_234_567) == 1200


def test_a_row_with_no_stats_contributes_no_tokens():
    snap = {"running": [{"session_id": "a", "metrics": {"cost_usd": 1}}]}
    assert fleet_figures.compose(snap)["tokens_k"] is None


def test_the_same_session_in_two_buckets_is_counted_once():
    row = {"session_id": "a", "metrics": {"cost_usd": 1.0}}
    later = {"session_id": "a", "metrics": {"cost_usd": 5.0}}
    out = fleet_figures.compose({"running": [row], "waiting": [later]})
    assert out["cost_usd"] == 1.0
    assert out["cost_measured"] == 1
    assert out["cost_rows"] == 1


def test_a_non_dict_row_is_skipped():
    snap = {"running": ["nope", None, {"session_id": "a", "metrics": {"cost_usd": 2}}]}
    out = fleet_figures.compose(snap)
    assert out["cost_rows"] == 1
    assert out["cost_usd"] == 2.0


def test_a_sleeping_row_is_live_and_an_abandoned_one_is_not():
    snap = {
        "sleeping": [{"session_id": "s", "metrics": {"cost_usd": 0.5}}],
        "abandoned": [{"session_id": "z", "metrics": {"cost_usd": 4}}],
    }
    out = fleet_figures.compose(snap)
    assert out["cost_rows"] == 1
    assert out["cost_usd"] == 0.5


def test_the_unknown_cost_words_are_the_cards_own():
    assert fleet_figures.COST_UNKNOWN == run_figures.COST_UNKNOWN == "cost unknown"


def test_the_module_imports_nothing_but_the_two_figure_modules_and_the_stdlib():
    src = MODULE.read_text()
    imports = [line for line in src.splitlines()
               if line.startswith("import ") or line.startswith("from ")]
    assert imports == [
        "from __future__ import annotations",
        "import threading",
        "from collections import deque",
        "from . import run_figures, run_health",
    ]
    assert not re.search(
        r"\b(samples|session_stats|pricing|sqlite3|daemon|api_server)\b", src)


# ── the burn meter: spend per hour from growth, never total ÷ age ────────────

def _burn_snapshot(**sessions):
    """`running` rows; each value is (cost_usd, tokens)."""
    rows = []
    for sid, (cost, tokens) in sessions.items():
        row = {"session_id": sid, "metrics": {}, "stats": {}}
        if cost is not None:
            row["metrics"]["cost_usd"] = cost
        if tokens is not None:
            row["stats"] = {"total_input_tokens": tokens, "output_tokens": 0}
        rows.append(row)
    return {"running": rows}


def test_burn_meter_says_nothing_until_it_has_watched_ten_minutes():
    meter = fleet_figures.BurnMeter()
    meter.observe(_burn_snapshot(a=(1.0, 1000)), 0.0)
    meter.observe(_burn_snapshot(a=(2.0, 5000)), 300.0)
    assert meter.rates(300.0) == {"cost_usd_hour": None, "tokens_k_hour": None}


def test_a_session_first_seen_is_a_baseline_not_a_jump():
    # A daemon restart must not book a $50 session's lifetime into its
    # first hour.
    meter = fleet_figures.BurnMeter()
    meter.observe(_burn_snapshot(a=(50.0, 9_000_000)), 0.0)
    meter.observe(_burn_snapshot(a=(50.0, 9_000_000)), 3600.0)
    assert meter.rates(3600.0) == {"cost_usd_hour": 0.0, "tokens_k_hour": 0}


def test_growth_over_the_hour_is_the_rate_and_older_growth_leaves_the_window():
    meter = fleet_figures.BurnMeter()
    meter.observe(_burn_snapshot(a=(1.0, 0), b=(0.0, 0)), 0.0)
    meter.observe(_burn_snapshot(a=(3.0, 400_000), b=(1.0, 600_000)), 1800.0)
    meter.observe(_burn_snapshot(a=(3.0, 400_000), b=(1.0, 600_000)), 3600.0)
    assert meter.rates(3600.0) == {"cost_usd_hour": 3.0, "tokens_k_hour": 1000}
    # Half an hour of quiet later, the 1800s growth is still inside the hour;
    # at 5401s it has left.
    assert meter.rates(5400.0)["cost_usd_hour"] == 3.0
    meter.observe(_burn_snapshot(a=(3.0, 400_000), b=(1.0, 600_000)), 5401.0)
    assert meter.rates(5401.0)["cost_usd_hour"] == 0.0


def test_a_short_watch_is_scaled_to_an_hour():
    meter = fleet_figures.BurnMeter()
    meter.observe(_burn_snapshot(a=(0.0, 0)), 0.0)
    meter.observe(_burn_snapshot(a=(1.0, 100_000)), 900.0)
    # Fifteen minutes watched, a dollar spent: four dollars an hour.
    assert meter.rates(900.0) == {"cost_usd_hour": 4.0, "tokens_k_hour": 400}


def test_the_same_snapshot_twice_adds_nothing_and_a_fall_rebaselines():
    meter = fleet_figures.BurnMeter()
    meter.observe(_burn_snapshot(a=(1.0, 1000)), 0.0)
    for _ in range(3):
        meter.observe(_burn_snapshot(a=(2.0, 1000)), 600.0)
    meter.observe(_burn_snapshot(a=(0.5, 1000)), 700.0)   # a reset
    meter.observe(_burn_snapshot(a=(1.5, 1000)), 800.0)
    assert meter.rates(3600.0)["cost_usd_hour"] == 2.0


def test_a_fleet_with_no_measured_cost_says_no_cost_rate():
    meter = fleet_figures.BurnMeter()
    meter.observe(_burn_snapshot(a=(None, 1000)), 0.0)
    meter.observe(_burn_snapshot(a=(None, 50_000)), 3600.0)
    assert meter.rates(3600.0) == {"cost_usd_hour": None, "tokens_k_hour": 49}


def test_compose_with_a_meter_adds_the_two_rates_and_without_one_is_unchanged():
    snap = _burn_snapshot(a=(1.0, 1000))
    assert set(fleet_figures.compose(snap)) == {
        "cost_usd", "cost_measured", "cost_rows", "tokens_k"}
    meter = fleet_figures.BurnMeter()
    figures = fleet_figures.compose(snap, meter, 0.0)
    assert figures["cost_usd_hour"] is None and figures["tokens_k_hour"] is None
    assert figures["cost_usd"] == 1.0
