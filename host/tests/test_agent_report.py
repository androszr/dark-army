# host/tests/test_agent_report.py
"""The agent dimension and the join to outcomes.

Two halves, both hermetic: `HistoryStore` on a temp file for the efficiency
rollups, hand-built dicts for `agent_report.join` — which is pure by
construction and so needs no store at all.
"""

import time

import pytest

from dark_army_daemon import agent_report, crew
from dark_army_daemon.history import HistoryStore


@pytest.fixture
def store(tmp_path):
    s = HistoryStore(tmp_path / "history.db")
    s.connect()
    yield s
    s.close()


def _turn(store, session, *, agent=None, agent_id=None, model="claude-sonnet-4-6",
          ts=None, **fields):
    store.add_turn(session, ts if ts is not None else time.time(),
                   message_id=f"m{store._scalar('SELECT COUNT(*) FROM turns')}",
                   model=model, attr_agent=agent, agent_id=agent_id, **fields)


# --- by_agent ---------------------------------------------------------------


def test_by_agent_folds_one_row_per_helper_kind(store):
    _turn(store, "s1", agent="bc-implementer", agent_id="a1",
          input_tokens=1000, output_tokens=200, cache_read=500, duration_ms=1200,
          tool_name="Bash")
    _turn(store, "s1", agent="bc-implementer", agent_id="a1",
          input_tokens=1000, output_tokens=100, cache_read=500, duration_ms=800)
    _turn(store, "s2", agent="bc-verifier", agent_id="a2",
          input_tokens=10, output_tokens=10)

    rows = {row["name"]: row for row in store.by_agent(30)}
    assert set(rows) == {"bc-implementer", "bc-verifier"}
    impl = rows["bc-implementer"]
    assert impl["turns"] == 2
    assert impl["dispatches"] == 1
    assert impl["output_tokens"] == 300
    assert impl["duration_ms"] == 2000
    assert impl["tool_calls"] == 1
    assert impl["cost_usd"] > 0


def test_by_agent_counts_dispatches_distinctly_across_models(store):
    """`COUNT(DISTINCT agent_id)` must not be taken inside the pricing grain."""
    _turn(store, "s1", agent="bc-verifier", agent_id="a1", model="claude-sonnet-4-6",
          output_tokens=10)
    _turn(store, "s1", agent="bc-verifier", agent_id="a1", model="claude-haiku-4-5",
          output_tokens=10)
    _turn(store, "s1", agent="bc-verifier", agent_id="a2", model="claude-haiku-4-5",
          output_tokens=10)

    row = store.by_agent(30)[0]
    assert row["dispatches"] == 2


def test_by_agent_reports_no_cost_rather_than_zero_when_nothing_priced(store):
    _turn(store, "s1", agent="bc-planner", agent_id="a1", model="an-unknown-model",
          input_tokens=100, output_tokens=100)

    row = store.by_agent(30)[0]
    assert row["cost_usd"] is None
    assert row["unpriced_turns"] == 1


def test_by_agent_keeps_a_genuine_zero_cache_ratio(store):
    _turn(store, "s1", agent="bc-planner", agent_id="a1",
          input_tokens=500, cache_read=0, output_tokens=1)

    assert store.by_agent(30)[0]["cache_hit_ratio"] == 0.0


def test_by_agent_has_no_cache_ratio_without_a_denominator(store):
    _turn(store, "s1", agent="bc-planner", agent_id="a1",
          input_tokens=0, cache_read=0, output_tokens=1)

    assert store.by_agent(30)[0]["cache_hit_ratio"] is None


def test_by_agent_cache_ratio_is_read_over_read_plus_input(store):
    _turn(store, "s1", agent="bc-planner", agent_id="a1",
          input_tokens=250, cache_read=750, output_tokens=1)

    assert store.by_agent(30)[0]["cache_hit_ratio"] == pytest.approx(0.75)


def test_by_agent_chunking_returns_the_same_fold_as_one_query(store):
    ids = [f"s{n}" for n in range(1200)]
    for sid in ids:
        _turn(store, sid, agent="bc-implementer", agent_id=f"a-{sid}",
              input_tokens=10, output_tokens=5)

    whole = store.by_agent(30)[0]
    chunked = store.by_agent(30, session_ids=ids)[0]
    assert store.SESSION_ID_CHUNK < len(ids)
    for key in ("turns", "dispatches", "input_tokens", "output_tokens",
                "sessions"):
        assert chunked[key] == whole[key], key
    assert chunked["cost_usd"] == pytest.approx(whole["cost_usd"])


def test_by_agent_session_filter_excludes_other_sessions(store):
    _turn(store, "s1", agent="bc-implementer", agent_id="a1", output_tokens=10)
    _turn(store, "s2", agent="bc-implementer", agent_id="a2", output_tokens=10)

    row = store.by_agent(30, session_ids=["s1"])[0]
    assert row["turns"] == 1 and row["dispatches"] == 1


def test_by_agent_ignores_turns_with_no_helper(store):
    _turn(store, "s1", output_tokens=10)
    assert store.by_agent(30) == []


def test_top_dispatches_are_ranked_and_bounded(store):
    for n, tokens in enumerate([10, 5000, 200]):
        _turn(store, "s1", agent="bc-implementer", agent_id=f"a{n}",
              output_tokens=tokens, duration_ms=100 * (n + 1), tool_name="Read")

    rows = store.top_dispatches(30, limit=2)
    assert len(rows) == 2
    assert rows[0]["agent_id"] == "a1"
    assert rows[0]["name"] == "bc-implementer"
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["tool_calls"] == 1


def test_session_efficiency_is_absent_rather_than_zero_for_an_unknown_session(store):
    _turn(store, "s1", output_tokens=10, input_tokens=10)

    facts = store.session_efficiency(["s1", "gone"])
    assert "gone" not in facts
    assert facts["s1"]["turns"] == 1


# --- role_of ----------------------------------------------------------------


def test_role_of_names_every_declared_role():
    for role in crew.ROLES:
        assert agent_report.role_of(role) == role
        assert agent_report.role_of(role.upper()) == role


def test_role_of_is_empty_for_a_name_off_the_table():
    assert agent_report.role_of("some-other-helper") == ""
    assert agent_report.role_of(None) == ""


# --- join -------------------------------------------------------------------


def _runs(*pairs):
    return [{"card_id": card, "provider": "claude", "session_id": sid,
             "phase": "implementation", "root": "/p", "bound_at": 1.0}
            for card, sid in pairs]


def test_join_puts_cost_and_acceptance_on_one_row():
    report = agent_report.join(
        _runs(("c1", "s1")),
        [{"name": "bc-implementer", "cost_usd": 2.0, "turns": 5}],
        {"s1": {"cost_usd": 1.25, "turns": 5, "duration_ms": 900,
                "unpriced_turns": 0}},
        {"accepted_outcomes": 1, "rework_rate": None},
        {"c1": {"title": "A card", "accepted": True, "rework_count": 0,
                "manual_steps": ""}},
    )
    card = report["cards"][0]
    assert card["cost_usd"] == pytest.approx(1.25)
    assert card["accepted"] is True
    assert card["partial"] is False
    assert report["agents"][0]["role"] == "bc-implementer"
    assert report["summary"]["accepted_outcomes"] == 1


def test_join_keeps_a_card_whose_session_has_no_history_row():
    report = agent_report.join(
        _runs(("c1", "s1")), [], {},
        {}, {"c1": {"title": "A card"}})
    card = report["cards"][0]
    assert card["card_id"] == "c1"
    assert card["partial"] is True
    assert card["cost_usd"] is None
    assert report["coverage"]["sessions_with_history"] == 0
    assert report["coverage"]["partial"] is True


def test_join_carries_an_empty_denominator_through_as_none():
    report = agent_report.join(
        [], [], {}, {"rework_rate": None, "submitted_cards": 0}, {})
    assert report["summary"]["rework_rate"] is None
    assert report["summary"]["submitted_cards"] == 0
    assert report["cards"] == []


def test_join_counts_outstanding_manual_checks_and_names_them_as_such():
    report = agent_report.join(
        _runs(("c1", "s1")), [], {"s1": {"cost_usd": 1.0}}, {},
        {"c1": {"manual_steps": "1. Open the panel."},
         "c2": {"manual_steps": ""}})
    assert report["summary"]["manual_checks_outstanding"] == 1
    assert report["cards"][0]["manual_check_outstanding"] is True
    # A rate would need history the store does not keep; only the count is
    # published, and its name says so.
    assert "manual_check_rate" not in report["summary"]


def test_join_folds_two_sessions_onto_one_card():
    report = agent_report.join(
        _runs(("c1", "s1"), ("c1", "s2")), [],
        {"s1": {"cost_usd": 1.0, "turns": 2}, "s2": {"cost_usd": 0.5, "turns": 3}},
        {}, {})
    card = report["cards"][0]
    assert card["cost_usd"] == pytest.approx(1.5)
    assert card["turns"] == 5
    assert len(card["sessions"]) == 2


def test_join_copies_tokens_and_leaves_them_null_without_turns():
    """A known session carries its tokens. A session with no turns does not
    become a zero, and the card's own cost_usd is the mixed figure it was."""
    report = agent_report.join(
        _runs(("c1", "s1"), ("c1", "s2")), [],
        {"s1": {"cost_usd": 1.25, "turns": 4, "duration_ms": 50,
                "output_tokens": 40, "measured_cost_usd": 1.25,
                "model": "claude-opus-4-8"}},
        {}, {"c1": {"who": "Mira"}},
    )
    card = report["cards"][0]
    by_id = {s["session_id"]: s for s in card["sessions"]}
    assert by_id["s1"]["output_tokens"] == 40
    assert by_id["s1"]["measured_cost_usd"] == pytest.approx(1.25)
    assert by_id["s1"]["known"] is True
    assert by_id["s2"]["output_tokens"] is None
    assert by_id["s2"]["measured_cost_usd"] is None
    assert by_id["s2"]["estimated_cost_usd"] is None
    assert by_id["s2"]["duration_ms"] is None
    assert by_id["s2"]["known"] is False
    assert card["cost_usd"] == pytest.approx(1.25)
    assert card["who"] == "Mira"


def test_ledger_who_prefers_the_implementer():
    assert agent_report.ledger_who({
        "bc-planner": "Cipher", "bc-implementer": "Mira",
    }) == "Mira"
    assert agent_report.ledger_who({
        "bc-planner": "Cipher", "bc-verifier": "Vex",
    }) == "Cipher"
    assert agent_report.ledger_who({
        "bc-verifier": "Vex", "bc-bug-auditor": "Ledger",
    }) == "Vex"
    assert agent_report.ledger_who({}) == ""
    assert agent_report.ledger_who(None) == ""


def test_join_leaves_acceptance_absent_where_nobody_asked():
    """`None`, never `False`, where acceptance was never computed.

    A report over every project carries no acceptance at all — the board's
    arithmetic is per canonical root — and a `False` on the row would be read
    as "somebody rejected it", which is a different fact entirely.
    """
    report = agent_report.join(
        _runs(("c1", "s1")), [], {"s1": {"cost_usd": 1.0}}, {},
        {"c1": {"title": "A card"}})
    card = report["cards"][0]
    assert card["accepted"] is None
    assert card["rework_count"] is None
    # A genuine rejection is still distinguishable from silence.
    rejected = agent_report.join(
        _runs(("c1", "s1")), [], {"s1": {"cost_usd": 1.0}}, {},
        {"c1": {"accepted": False, "rework_count": 2}})["cards"][0]
    assert rejected["accepted"] is False and rejected["rework_count"] == 2


def test_session_efficiency_counts_only_turns_inside_the_period(store):
    """A card's spend is the spend inside the period the report states.

    A session bound to a card three days ago may have started three weeks
    ago; folding its whole lifetime into a row printed under `from`/`to` is a
    figure that does not describe the period it sits in.
    """
    now = time.time()
    _turn(store, "s1", ts=now - 40 * 86400, output_tokens=1000)
    _turn(store, "s1", ts=now - 86400, output_tokens=10)
    whole = store.session_efficiency(["s1"])["s1"]
    recent = store.session_efficiency(["s1"], days=7)["s1"]
    assert whole["turns"] == 2
    assert recent["turns"] == 1
    assert recent["output_tokens"] == 10


def test_session_efficiency_of_no_sessions_is_empty(store):
    _turn(store, "s1")
    assert store.session_efficiency([]) == {}


def test_by_agent_filtered_to_no_sessions_returns_no_rows(store):
    """An empty filter means *no rows*, never "no filter".

    Passing `[]` on as `None` would fall through to the machine-wide scan and
    list every helper on the Mac under a project that ran none of them.
    """
    _turn(store, "s1", agent="bc-implementer", agent_id="a1")
    assert store.by_agent(30, session_ids=[]) == []
    assert store.by_agent(30, session_ids=None), "None is still the whole machine"
