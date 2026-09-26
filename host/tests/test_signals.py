"""Rules are a table, so their tests are dicts. No daemon, no clock, no I/O."""
import pytest

from dark_army_daemon import signals as sg
from dark_army_daemon.daemon import BobDaemon


def entry(**over) -> dict:
    base = {
        "session_id": "s1",
        "idle_seconds": 0.0,
        "current_tool": "",
        "subagents": 0,
        "metrics": {},
        "stats": {},
    }
    base.update(over)
    return base


def rules(found) -> set:
    return {s.rule for s in found}


def by_rule(found, rule) -> sg.Signal:
    return next(s for s in found if s.rule == rule)


# ── context ──────────────────────────────────────────────────────────────────

def test_context_at_the_ceiling_is_critical():
    found = sg.evaluate_agent(entry(metrics={"ctx_used_pct": 94}), "running")
    signal = by_rule(found, "ctx-full")
    assert signal.severity == sg.CRIT
    assert "94%" in signal.text
    assert signal.action == "reveal"


def test_exceeds_200k_fires_even_without_a_percentage():
    found = sg.evaluate_agent(entry(metrics={"exceeds_200k": True}), "running")
    assert "ctx-full" in rules(found)


def test_context_below_the_ceiling_is_silent():
    assert "ctx-full" not in rules(
        sg.evaluate_agent(entry(metrics={"ctx_used_pct": 62}), "running"))


def test_the_ceiling_is_eighty_five():
    """The Strands Harness default (`docs/harness-token-policy.md`): the line
    sits where a compaction still has room to work. 84 is silent, 85 fires,
    and with no trend to temper it the reading is critical."""
    assert sg.CTX_CRIT_PCT == 85.0
    below = sg.evaluate_agent(entry(metrics={"ctx_used_pct": 84}), "running")
    assert "ctx-full" not in rules(below)
    at = sg.evaluate_agent(entry(metrics={"ctx_used_pct": 85}), "running")
    assert by_rule(at, "ctx-full").severity == sg.CRIT
    calm = sg.evaluate_agent(
        entry(metrics={"ctx_used_pct": 85},
              trend={"ctx_runway_seconds": 3600.0}), "running")
    assert by_rule(calm, "ctx-full").severity == sg.WARN


def test_every_client_draws_the_daemons_context_line():
    """The panel's row and detail, the phone's row and the card's health line
    each draw the context line by hand. They are pinned here to the one figure
    the daemon acts on, so the red a person sees and the compact Dark Army
    types land at the same moment. The usage meters keep their own 90%."""
    from pathlib import Path

    from dark_army_daemon import run_health

    root = Path(__file__).resolve().parents[2]
    line = int(sg.CTX_CRIT_PCT)
    assert run_health.CTX_WORRY_PCT == line

    for rel in ("panel/Sources/BobPanel/ProcessTable.swift",
                "ios/BobPhone/ProcessTable.swift"):
        text = (root / rel).read_text()
        start = text.index("private var ctxColor: Color {")
        body = text[start:text.index("\n    }\n", start)]
        assert f"pct >= {line}" in body, rel
    detail = (root / "panel/Sources/BobPanel/AgentDetailPane.swift").read_text()
    assert f"static let ctxCrit: Double = {line}" in detail


# ── context runway ───────────────────────────────────────────────────────────
#
# The number the context line was always standing in for. At 85% you may have
# forty minutes or two, and only one of those is worth interrupting somebody
# about.


def test_a_session_on_course_to_fill_warns_before_it_is_full():
    found = sg.evaluate_agent(
        entry(metrics={"ctx_used_pct": 68},
              trend={"ctx_runway_seconds": 600.0, "ctx_pct_per_min": 3.2}),
        "running")
    signal = by_rule(found, "ctx-runway")
    assert signal.severity == sg.WARN
    assert "10m" in signal.text and "3.2%/min" in signal.text
    assert "ctx-full" not in rules(found)


def test_minutes_of_runway_is_critical():
    found = sg.evaluate_agent(
        entry(metrics={"ctx_used_pct": 82},
              trend={"ctx_runway_seconds": 120.0, "ctx_pct_per_min": 9.0}),
        "running")
    assert by_rule(found, "ctx-runway").severity == sg.CRIT


def test_a_steep_climb_early_in_a_session_says_nothing():
    """At 12% the estimate is extrapolating furthest and the news is smallest."""
    found = sg.evaluate_agent(
        entry(metrics={"ctx_used_pct": 12},
              trend={"ctx_runway_seconds": 600.0, "ctx_pct_per_min": 8.8}),
        "running")
    assert "ctx-runway" not in rules(found)


def test_a_full_session_with_plenty_of_time_is_not_an_emergency():
    """91% and barely moving is not the same animal as 91% climbing 4%/min, and
    only the second earns a banner — `crit` is what alerts.py fires on."""
    calm = sg.evaluate_agent(
        entry(metrics={"ctx_used_pct": 91},
              trend={"ctx_runway_seconds": 3600.0}), "running")
    assert by_rule(calm, "ctx-full").severity == sg.WARN
    assert "full in ~1h" in by_rule(calm, "ctx-full").text

    racing = sg.evaluate_agent(
        entry(metrics={"ctx_used_pct": 91},
              trend={"ctx_runway_seconds": 200.0}), "running")
    assert by_rule(racing, "ctx-full").severity == sg.CRIT


def test_without_a_trend_the_ceiling_behaves_exactly_as_it_did():
    """Most of the time there is no series to fit, and the old rule is what is
    left. It must not have become quieter for want of one."""
    found = sg.evaluate_agent(entry(metrics={"ctx_used_pct": 94}), "running")
    assert by_rule(found, "ctx-full").severity == sg.CRIT


def test_missing_context_is_not_read_as_zero():
    """The one wrong answer: reporting an unreported session as healthy."""
    found = sg.evaluate_agent(entry(metrics={"ctx_used_pct": None}), "running")
    assert "ctx-full" not in rules(found)


# ── stall ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,idle,expected", [
    ("Bash", 200, None),                 # builds legitimately take minutes
    ("Bash", 320, sg.WARN),
    ("Bash", 800, sg.CRIT),
    ("Read", 130, sg.WARN),              # a read has no excuse
    ("Read", 60, None),
    ("WebFetch", 70, sg.WARN),
])
def test_stall_thresholds_are_per_tool(tool, idle, expected):
    found = sg.evaluate_agent(
        entry(current_tool=tool, idle_seconds=idle), "running")
    got = by_rule(found, "stall").severity if "stall" in rules(found) else None
    assert got == expected


def test_a_parent_waiting_on_its_children_is_not_stalled():
    found = sg.evaluate_agent(
        entry(current_tool="Agent", idle_seconds=3600, subagents=3), "running")
    assert "stall" not in rules(found)


def test_agent_tool_without_live_children_can_still_stall():
    found = sg.evaluate_agent(
        entry(current_tool="Agent", idle_seconds=3600, subagents=0), "running")
    assert "stall" in rules(found)


def test_only_running_agents_stall():
    assert "stall" not in rules(
        sg.evaluate_agent(entry(idle_seconds=9999), "sleeping"))


# ── churn / PR / waiting ─────────────────────────────────────────────────────

def test_churn_needs_edits_per_file_not_edits():
    quiet = sg.evaluate_agent(entry(stats={
        "tool_counts": {"Edit": 30}, "files_touched": 10}), "running")
    loud = sg.evaluate_agent(entry(stats={
        "tool_counts": {"Edit": 20, "Write": 11}, "files_touched": 2}), "running")
    assert "churn" not in rules(quiet)
    assert by_rule(loud, "churn").severity == sg.INFO


def test_churn_never_divides_by_zero_files():
    assert "churn" not in rules(sg.evaluate_agent(
        entry(stats={"tool_counts": {"Edit": 9}, "files_touched": 0}), "running"))


def test_churn_fires_on_grok_edit_names():
    found = sg.evaluate_agent(entry(stats={
        "tool_counts": {"search_replace": 20, "write": 6},
        "files_touched": 2,
    }), "running")
    assert by_rule(found, "churn").severity == sg.INFO


def test_changes_requested_names_the_pr():
    found = sg.evaluate_agent(entry(metrics={
        "pr_review_state": "CHANGES_REQUESTED", "pr_number": 42}), "running")
    signal = by_rule(found, "pr-changes")
    assert "#42" in signal.text and signal.action == "open_pr"


@pytest.mark.parametrize("idle,expected", [
    (600, None), (2000, sg.WARN), (5 * 3600, sg.CRIT)])
def test_waiting_escalates_with_the_hours(idle, expected):
    found = sg.evaluate_agent(entry(idle_seconds=idle), "waiting")
    got = by_rule(found, "waiting").severity if "waiting" in rules(found) else None
    assert got == expected


def test_a_long_session_with_no_statusline_says_so_once():
    found = sg.evaluate_agent(
        entry(stats={"duration_seconds": 1200}), "running")
    assert by_rule(found, "no-metrics").severity == sg.INFO


def test_a_young_session_is_not_nagged_about_the_statusline():
    assert "no-metrics" not in rules(
        sg.evaluate_agent(entry(stats={"duration_seconds": 30}), "running"))


def test_a_session_that_reports_metrics_is_never_flagged():
    assert "no-metrics" not in rules(sg.evaluate_agent(
        entry(stats={"duration_seconds": 9999}, metrics={"cost_usd": 1.0}),
        "running"))


def test_a_grok_row_with_only_a_model_id_is_still_blank():
    """The hole this rule exists to fill: Grok always has model_id."""
    found = sg.evaluate_agent(entry(
        stats={"duration_seconds": 1200},
        metrics={"model_id": "grok-4.6"}), "running")
    assert "no-metrics" in rules(found)


def test_a_context_reading_alone_suppresses_no_metrics():
    assert "no-metrics" not in rules(sg.evaluate_agent(entry(
        stats={"duration_seconds": 1200},
        metrics={"model_id": "grok-4.6", "ctx_used_pct": 22}), "running"))


# ── global ───────────────────────────────────────────────────────────────────

def test_budget_needs_more_than_one_agent():
    hot = {"metrics": {"five_hour_pct": 84}, "subagents": 0, "session_id": "a"}
    alone = sg.evaluate_global({"running": [dict(hot)], "sleeping": []})
    crowd = sg.evaluate_global(
        {"running": [dict(hot), dict(hot, session_id="b")], "sleeping": []})
    assert "budget" not in {s.rule for s in alone}
    assert by_rule(crowd, "budget").severity == sg.WARN


def test_budget_takes_the_worst_reading_not_the_last():
    snapshot = {"running": [
        {"session_id": "a", "metrics": {"five_hour_pct": 30}},
        {"session_id": "b", "metrics": {"five_hour_pct": 93}}]}
    assert by_rule(sg.evaluate_global(snapshot), "budget").severity == sg.CRIT


def test_swarm_counts_subagents_across_every_category():
    snapshot = {
        "running": [{"session_id": "a", "subagents": 4,
                     "metrics": {"five_hour_pct": 70}}],
        "sleeping": [{"session_id": "b", "subagents": 3, "metrics": {}}]}
    assert "swarm" in {s.rule for s in sg.evaluate_global(snapshot)}


def test_budget_is_per_provider():
    snapshot = {"running": [
        {"session_id": "c1", "provider": "claude",
         "metrics": {"five_hour_pct": 95}, "subagents": 0},
        {"session_id": "c2", "provider": "claude",
         "metrics": {"five_hour_pct": 95}, "subagents": 0},
        {"session_id": "g1", "provider": "grok",
         "metrics": {"five_hour_pct": 10, "budget_cycle": "weekly"},
         "subagents": 0},
        {"session_id": "g2", "provider": "grok",
         "metrics": {"five_hour_pct": 10}, "subagents": 0},
    ]}
    found = sg.evaluate_global(snapshot)
    assert {s.rule for s in found} == {"budget"}
    assert "95%" in by_rule(found, "budget").text
    assert "5h" in by_rule(found, "budget").text


def test_grok_budget_names_the_account():
    snapshot = {"running": [
        {"session_id": "g1", "provider": "grok",
         "metrics": {"five_hour_pct": 84, "budget_cycle": "weekly"},
         "subagents": 0},
        {"session_id": "g2", "provider": "grok",
         "metrics": {"five_hour_pct": 84}, "subagents": 0},
    ]}
    found = sg.evaluate_global(snapshot)
    assert "grok-budget" in {s.rule for s in found}
    assert "budget" not in {s.rule for s in found}
    assert "Grok weekly" in by_rule(found, "grok-budget").text


def test_fleet_signals_carry_the_account_they_are_about():
    """The panel marks the strip with the provider's logo, and the text says
    "5h" or "weekly" rather than naming one — so the field has to be there."""
    snapshot = {"running": [
        {"session_id": "c1", "provider": "claude",
         "metrics": {"five_hour_pct": 95}, "subagents": 0},
        {"session_id": "c2", "provider": "claude",
         "metrics": {"five_hour_pct": 95}, "subagents": 0},
        {"session_id": "g1", "provider": "grok",
         "metrics": {"five_hour_pct": 91}, "subagents": 0},
        {"session_id": "g2", "provider": "grok",
         "metrics": {"five_hour_pct": 91}, "subagents": 0},
    ]}
    found = sg.evaluate_global(snapshot)
    assert by_rule(found, "budget").provider == "claude"
    assert by_rule(found, "grok-budget").provider == "grok"


def test_doom_loop_is_a_warning():
    found = sg.evaluate_agent(entry(metrics={"doom_loop_attempts": 2}), "running")
    assert by_rule(found, "doom-loop").severity == sg.WARN


def test_a_single_cancellation_is_not_a_streak():
    assert "cancellations" not in rules(sg.evaluate_agent(
        entry(metrics={"consecutive_cancellations": 1}), "running"))
    found = sg.evaluate_agent(
        entry(metrics={"consecutive_cancellations": 2}), "running")
    assert by_rule(found, "cancellations").severity == sg.WARN


def test_edit_retry_needs_a_high_ratio():
    quiet = sg.evaluate_agent(entry(
        metrics={"edit_and_retry_count": 2},
        stats={"total_tool_calls": 40}), "running")
    loud = sg.evaluate_agent(entry(
        metrics={"edit_and_retry_count": 8},
        stats={"total_tool_calls": 40}), "running")
    assert "edit-retry" not in rules(quiet)
    assert by_rule(loud, "edit-retry").severity == sg.INFO


def test_reverted_is_info():
    found = sg.evaluate_agent(entry(metrics={"has_reverted": True}), "running")
    assert by_rule(found, "reverted").severity == sg.INFO
    assert "reverted" not in rules(
        sg.evaluate_agent(entry(metrics={"has_reverted": False}), "running"))


# ── hysteresis ───────────────────────────────────────────────────────────────

def snapshot_with(**over) -> dict:
    return {"running": [entry(**over)], "sleeping": [], "waiting": [],
            "abandoned": []}


def test_a_signal_must_hold_before_it_is_shown():
    engine = sg.SignalEngine(hold=2)
    stalled = snapshot_with(current_tool="Read", idle_seconds=300)
    assert engine.evaluate(stalled)["by_session"] == {}      # first sighting
    assert "stall" in {s["rule"] for s in
                       engine.evaluate(stalled)["by_session"]["s1"]}


def test_a_signal_must_hold_before_it_is_withdrawn():
    engine = sg.SignalEngine(hold=2)
    stalled = snapshot_with(current_tool="Read", idle_seconds=300)
    healthy = snapshot_with(current_tool="Read", idle_seconds=1)
    engine.evaluate(stalled); engine.evaluate(stalled)
    assert engine.evaluate(healthy)["by_session"].get("s1")   # still shown
    assert not engine.evaluate(healthy)["by_session"].get("s1")


def test_a_single_slow_call_never_flaps_into_view():
    engine = sg.SignalEngine(hold=2)
    engine.evaluate(snapshot_with(current_tool="Read", idle_seconds=300))
    engine.evaluate(snapshot_with(current_tool="Read", idle_seconds=1))
    engine.evaluate(snapshot_with(current_tool="Read", idle_seconds=1))
    assert engine.evaluate(
        snapshot_with(current_tool="Read", idle_seconds=1))["by_session"] == {}


def test_escalation_skips_the_wait():
    """A warning that became critical is news; queueing it defeats the point."""
    engine = sg.SignalEngine(hold=2)
    engine.evaluate(snapshot_with(current_tool="Read", idle_seconds=130))
    engine.evaluate(snapshot_with(current_tool="Read", idle_seconds=130))
    engine.evaluate(snapshot_with(current_tool="Read", idle_seconds=1))
    out = engine.evaluate(snapshot_with(current_tool="Read", idle_seconds=400))
    assert out["by_session"]["s1"][0]["severity"] == sg.CRIT


def test_a_departed_session_is_forgotten_immediately():
    engine = sg.SignalEngine(hold=2)
    stalled = snapshot_with(current_tool="Read", idle_seconds=300)
    engine.evaluate(stalled); engine.evaluate(stalled)
    empty = {"running": [], "sleeping": [], "waiting": [], "abandoned": []}
    assert engine.evaluate(empty)["by_session"] == {}
    assert engine._state == {}


def test_worst_severity_leads_the_row():
    engine = sg.SignalEngine(hold=1)
    out = engine.evaluate(snapshot_with(
        current_tool="Read", idle_seconds=300,
        metrics={"ctx_used_pct": 95, "pr_review_state": "CHANGES_REQUESTED"}))
    severities = [s["severity"] for s in out["by_session"]["s1"]]
    assert severities == sorted(severities, key=lambda s: -sg._RANK[s])
    assert severities[0] == sg.CRIT


# ── the snapshot actually carries them ───────────────────────────────────────

def _stub(**over) -> dict:
    base = {"session_id": "00000000-0000-0000-0000-000000000000",
            "project": "proj", "state": "working", "subagents": 0,
            "subagent_ids": [], "idle_seconds": 0.0, "current_tool": "",
            "_category": "running"}
    base.update(over)
    return base


def test_every_entry_carries_a_signals_list_even_when_quiet():
    """A key that appears only when something is wrong is a key consumers get
    wrong; the empty list is the contract."""
    d = BobDaemon()
    entry = d._enrich_agent_stubs([_stub()])["running"][0]
    assert entry["signals"] == []


def test_a_stalled_session_reaches_the_snapshot_after_the_hold():
    d = BobDaemon()
    stub = _stub(current_tool="Read", idle_seconds=400)
    d._enrich_agent_stubs([dict(stub)])                       # first sighting
    entry = d._enrich_agent_stubs([dict(stub)])["running"][0]
    assert [s["rule"] for s in entry["signals"]] == ["stall"]


def test_global_signals_are_kept_off_the_rows():
    d = BobDaemon()
    # Metrics reach an entry from the statusline store, never from the stub —
    # _enrich_agent_stubs overwrites the key. Seeding the stub instead would test
    # a path the daemon does not have.
    d._session_metrics["a"] = {"five_hour_pct": 95}
    d._session_metrics["b"] = {"five_hour_pct": 95}
    stubs = [_stub(session_id="a"), _stub(session_id="b")]
    for _ in range(2):
        snap = d._enrich_agent_stubs([dict(s) for s in stubs])
    assert all(s["rule"] != "budget"
               for row in snap["running"] for s in row["signals"])
    assert "budget" in {s["rule"] for s in d._global_signals}


def test_signals_serialise_to_plain_dicts():
    engine = sg.SignalEngine(hold=1)
    out = engine.evaluate(snapshot_with(metrics={"ctx_used_pct": 99}))
    signal = out["by_session"]["s1"][0]
    assert set(signal) == {"rule", "severity", "text", "action", "provider"}
    assert all(isinstance(v, str) for v in signal.values())
