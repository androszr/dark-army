"""Command/evidence accounting and the report validators of
`tools/ship_efficiency.py`.

What is pinned: an identical command inside one role is a duplicate and may
be cited, while a different owner, cwd, environment, toolchain, input digest
(tracked, staged or untracked bytes), a missing log, a failure exit or an
interrupted run never is; one full suite per verifier pass with the
implementer's and the verifier's evidence never shared; usage records
aggregated once per provider/session/turn, cumulative versus per-turn kept
straight, a parent total known to include its children never added to them,
a missing cache field unknown rather than zero, incomplete sessions excluded
from the paired median; and the two document validators — a claim the
document makes must be evidenced, a run it did not make must be declared.

The tool launches nothing and executes nothing; every input here is a dict
or a synthetic fixture under `host/tests/fixtures/ship_efficiency/`.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "ship_efficiency.py"
_spec = importlib.util.spec_from_file_location("ship_efficiency", TOOL)
se = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(se)

FIXTURES = ROOT / "host" / "tests" / "fixtures" / "ship_efficiency"
FULL = [".venv/bin/pytest", "-q"]


def _cmd(**kw):
    base = {"owner": "bc-verifier", "argv": FULL, "cwd": "host", "exit": 0,
            "log": "scratch/pytest-full.log", "input_digest": "abc123",
            "env_id": "venv-3.12", "toolchain_id": "py3.12.4"}
    base.update(kw)
    return base


# --- reusable ------------------------------------------------------------------


def test_an_identical_execution_inside_one_role_is_reusable():
    ok, why = se.reusable(_cmd(), _cmd())
    assert ok and "same owner" in why


@pytest.mark.parametrize("change,field", [
    ({"owner": "bc-implementer"}, "different owner"),
    ({"cwd": "panel"}, "cwd"),
    ({"env_id": "venv-3.11"}, "env_id"),
    ({"toolchain_id": "py3.11"}, "toolchain_id"),
    ({"input_digest": "changed-test-bytes"}, "input_digest"),
    ({"input_digest": "staged-or-untracked-bytes-changed"}, "input_digest"),
    ({"argv": FULL + ["tests/test_x.py"]}, "argv"),
])
def test_any_difference_forces_a_rerun(change, field):
    ok, why = se.reusable(_cmd(), _cmd(**change))
    assert not ok and field in why


@pytest.mark.parametrize("earlier,why", [
    (_cmd(exit=1), "did not exit 0"),
    (_cmd(exit=None), "interrupted"),
    (_cmd(log=""), "kept no log"),
    (_cmd(input_digest=""), "no provable input digest"),
])
def test_a_failure_an_interruption_or_a_missing_log_is_never_a_pass(earlier, why):
    ok, reason = se.reusable(earlier, _cmd())
    assert not ok and why in reason


def test_evidence_is_never_shared_between_implementer_and_verifier():
    assert not se.reusable(_cmd(owner="bc-implementer"), _cmd(owner="bc-verifier"))[0]
    assert not se.reusable(_cmd(owner="bc-verifier"), _cmd(owner="bc-implementer"))[0]


# --- command accounting ----------------------------------------------------------


def test_one_full_suite_per_verifier_pass_and_a_citation_counts_as_reuse():
    out = se.command_accounting([
        _cmd(owner="bc-implementer"),
        _cmd(),
        _cmd(cited=True),
    ])
    assert out["executed"] == 2 and out["reused"] == 1
    assert out["full_suites"] == {"bc-implementer": 1, "bc-verifier": 1}
    assert out["duplicates"] == []


def test_running_the_same_suite_twice_in_one_role_is_flagged():
    out = se.command_accounting([_cmd(), _cmd()])
    assert out["full_suites"] == {"bc-verifier": 2}
    assert out["duplicates"] and "repeated inside one role" in out["duplicates"][0]["problem"]


def test_citing_across_roles_or_after_a_change_is_flagged():
    out = se.command_accounting([_cmd(owner="bc-implementer"), _cmd(cited=True)])
    assert out["duplicates"] and "may not reuse" in out["duplicates"][0]["problem"]
    out = se.command_accounting([_cmd(), _cmd(cited=True, input_digest="moved")])
    assert out["duplicates"] and "input_digest" in out["duplicates"][0]["problem"]
    out = se.command_accounting([_cmd(exit=2), _cmd(cited=True)])
    assert out["duplicates"] and "did not exit 0" in out["duplicates"][0]["problem"]


def test_a_targeted_run_is_not_a_full_suite():
    out = se.command_accounting([_cmd(argv=FULL + ["tests/test_a.py"])])
    assert out["full_suites"] == {}


# --- usage -----------------------------------------------------------------------


def _rec(turn, i, o, session="s1", **kw):
    d = {"provider": "claude", "session": session, "turn": turn,
         "input_tokens": i, "output_tokens": o,
         "cache_read_tokens": None, "cache_write_tokens": None}
    d.update(kw)
    return d


def test_each_provider_session_turn_counts_once():
    total = se.usage_totals([_rec(1, 100, 10), _rec(1, 100, 10), _rec(2, 50, 5)])
    assert total["input"] == 150 and total["output"] == 15
    assert total["records_counted"] == 2


def test_cumulative_records_replace_and_per_turn_records_add():
    total = se.usage_totals([
        _rec(1, 100, 10, kind="cumulative"),
        _rec(2, 300, 30, kind="cumulative"),
        _rec(1, 40, 4, session="s2"),
        _rec(2, 40, 4, session="s2"),
    ])
    assert total["input"] == 300 + 80
    assert total["output"] == 30 + 8


def test_a_parent_total_that_includes_children_is_kept_apart():
    total = se.usage_totals([_rec(1, 100, 10), _rec(9, 1000, 200, includes_children=True)])
    assert total["input"] == 100
    assert total["end_to_end"] == {"input": 1000, "output": 200}


def test_a_missing_cache_field_is_unknown_never_zero():
    total = se.usage_totals([_rec(1, 100, 10, cache_read_tokens=40), _rec(2, 100, 10)])
    assert total["cache_read"] is None and total["cache_write"] is None
    known = se.usage_totals([_rec(1, 100, 10, cache_read_tokens=40, cache_write_tokens=0),
                             _rec(2, 100, 10, cache_read_tokens=10, cache_write_tokens=0)])
    assert known["cache_read"] == 50 and known["cache_write"] == 0


def test_a_missing_input_or_output_figure_is_unknown_and_marks_the_role_incomplete():
    """A record with only ``output_tokens`` used to count as zero input, so a
    candidate with no input figure reported a 100% reduction and met the
    target. It is unknown, and unknown usage never enters the median."""
    only_output = {"provider": "claude", "session": "s1", "turn": 1, "output_tokens": 10,
                   "cache_read_tokens": 0, "cache_write_tokens": 0}
    total = se.usage_totals([_rec(1, 100, 10, session="s0", cache_read_tokens=0, cache_write_tokens=0), only_output])
    assert total["input"] is None and total["incomplete"] is True
    assert total["output"] == 20 and total["cache_read"] == 0
    null_input = se.usage_totals([_rec(1, None, 10)])
    assert null_input["input"] is None and null_input["incomplete"] is True
    null_output = se.usage_totals([_rec(1, 100, None)])
    assert null_output["output"] is None and null_output["incomplete"] is True
    assert se.usage_totals([_rec(1, 100, 10)])["incomplete"] is False


def test_a_candidate_without_an_input_figure_never_meets_the_target():
    base = _run(1, 1000)
    cand = _run(1, 500)
    cand["roles"][0]["records"][0].pop("input_tokens")
    summary = se.run_summary(cand)
    assert summary["end_to_end"]["input"] is None and summary["complete"] is False
    paired = se.paired_reduction([se.run_summary(base)], [summary])
    assert paired["complete_pairs"] == 0 and paired["target_met"] is False
    assert paired["excluded"] == [{"pair": [1, "cold"], "why": "incomplete usage"}]
    zero = se.run_summary(_run(1, 0))
    paired = se.paired_reduction([se.run_summary(base)], [zero])
    assert paired["excluded"] == [{"pair": [1, "cold"], "why": "no input figure"}]
    assert paired["median_input_reduction"] is None


def test_a_candidate_without_an_input_figure_fails_validate_report_met(doc, tmp_path):
    base = tmp_path / "b.json"
    cand = tmp_path / "c.json"
    only_output = {"provider": "claude", "session": "p1", "turn": 1, "output_tokens": 10,
                   "cache_read_tokens": None, "cache_write_tokens": None}
    base.write_text(json.dumps({"schema": 1, "runs": [_run(1, 1000)]}), encoding="utf-8")
    run = _run(1, 1)
    run["roles"][0]["records"] = [only_output]
    cand.write_text(json.dumps({"schema": 1, "runs": [run]}), encoding="utf-8")
    out = tmp_path / "results.json"
    proc = subprocess.run([sys.executable, str(TOOL), "report", "--baseline", str(base),
                           "--candidate", str(cand), "--output", str(out)],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["paired"]["cold"]["target_met"] is False
    assert data["candidate"]["runs"][0]["roles"]["parent"]["input"] is None
    met = doc.replace("claim key=runtime_input_reduction value=unmeasured",
                      "claim key=runtime_input_reduction value=met")
    met = met.replace("claim key=cost_saving value=none",
                      "claim key=cost_saving value=none -->\n<!-- ship-efficiency: claim key=results value=results.json")
    assert any("complete cold pairs" in p for p in se.validate_report(met, tmp_path))


def test_provider_token_units_are_copied_not_converted():
    total = se.usage_totals([_rec(1, 7, 3, provider="codex"), _rec(1, 7, 3, provider="claude")])
    assert total["input"] == 14, "two providers, two records, no unit arithmetic"


def test_an_incomplete_session_marks_the_role_incomplete():
    assert se.usage_totals([_rec(1, 1, 1, incomplete=True)])["incomplete"] is True


def test_duplicate_loads_are_per_role_and_per_path():
    dup = se.duplicate_loads([
        {"role": "parent", "path": "CLAUDE.md", "bytes": 1},
        {"role": "parent", "path": "CLAUDE.md", "bytes": 1},
        {"role": "bc-verifier", "path": "CLAUDE.md", "bytes": 1},
    ])
    assert dup == [{"role": "parent", "path": "CLAUDE.md", "times": 2}]


# --- pairing and the report --------------------------------------------------------


def _run(scenario, inp, complete=True, cache="cold"):
    return {"run_id": f"r{scenario}", "scenario": scenario, "complete": complete,
            "cache_state": cache, "roles": [
                {"role": "parent", "records": [_rec(1, inp, 10, session=f"p{scenario}")]}],
            "reads": [], "commands": [], "repairs": 0}


def test_the_median_is_over_complete_pairs_only_and_exclusions_are_listed():
    base = [se.run_summary(_run(1, 1000)), se.run_summary(_run(2, 1000)),
            se.run_summary(_run(3, 1000, complete=False)), se.run_summary(_run(4, 1000))]
    cand = [se.run_summary(_run(1, 800)), se.run_summary(_run(2, 500)),
            se.run_summary(_run(3, 100))]
    paired = se.paired_reduction(base, cand)
    assert paired["complete_pairs"] == 2
    assert paired["median_input_reduction"] == pytest.approx(0.35)
    reasons = {tuple(e["pair"]): e["why"] for e in paired["excluded"]}
    assert reasons[(3, "cold")] == "incomplete usage"
    assert reasons[(4, "cold")] == "unpaired"
    assert paired["target_met"] is True


def test_a_second_run_on_one_side_is_a_duplicate_arm_never_averaged_or_replaced():
    """Two baseline runs of one scenario used to overwrite each other silently,
    so whichever came last set the figure. The pair is excluded and says why."""
    base = [se.run_summary(_run(1, 1000)), se.run_summary(_run(1, 100)), se.run_summary(_run(2, 1000))]
    cand = [se.run_summary(_run(1, 500)), se.run_summary(_run(2, 500))]
    paired = se.paired_reduction(base, cand)
    assert paired["complete_pairs"] == 1
    assert paired["median_input_reduction"] == pytest.approx(0.5)
    assert paired["excluded"] == [{"pair": [1, "cold"], "why": "duplicate arm"}]
    twice = se.paired_reduction([se.run_summary(_run(3, 1000))],
                                [se.run_summary(_run(3, 900)), se.run_summary(_run(3, 10))])
    assert twice["excluded"] == [{"pair": [3, "cold"], "why": "duplicate arm"}]
    assert twice["median_input_reduction"] is None and twice["target_met"] is False


def test_no_complete_pair_means_no_figure_and_no_claim():
    paired = se.paired_reduction([se.run_summary(_run(1, 100, complete=False))],
                                 [se.run_summary(_run(1, 50))])
    assert paired["median_input_reduction"] is None and paired["target_met"] is False


def test_cold_and_warm_records_are_reported_apart():
    report = se.report(
        {"schema": 1, "runs": [_run(1, 1000), _run(1, 1000, cache="warm")]},
        {"schema": 1, "runs": [_run(1, 500), _run(1, 950, cache="warm")]})
    assert report["paired"]["cold"]["median_input_reduction"] == pytest.approx(0.5)
    assert report["paired"]["warm"]["median_input_reduction"] == pytest.approx(0.05)


def test_cost_appears_only_with_an_attributable_amount():
    with_cost = se.run_summary({**_run(1, 10), "cost": {"amount": 1.5, "currency": "USD", "provenance": "invoice"}})
    assert with_cost["cost"]["amount"] == 1.5
    without = se.run_summary({**_run(1, 10), "cost": {"amount": "unknown"}})
    assert without["cost"]["unavailable"] is True
    assert se.run_summary(_run(1, 10))["cost"] is None


def test_the_synthetic_fixtures_import_and_the_report_is_written(tmp_path):
    out = tmp_path / "report.json"
    proc = subprocess.run(
        [sys.executable, str(TOOL), "report",
         "--baseline", str(FIXTURES / "runs-baseline.json"),
         "--candidate", str(FIXTURES / "runs-candidate.json"),
         "--output", str(out)],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["paired"]["cold"]["complete_pairs"] == 2
    b1 = data["baseline"]["runs"][0]
    assert b1["duplicate_loads"] == [{"role": "parent", "path": "CLAUDE.md", "times": 2}]
    assert b1["commands"]["full_suites"] == {"bc-implementer": 1, "bc-verifier": 2}
    assert b1["roles"]["parent"]["cache_read"] is None
    for label in ("baseline", "candidate"):
        for run in data[label]["runs"]:
            assert run["cost"] is None or "amount" in run["cost"] or run["cost"]["unavailable"]


def test_a_run_file_of_the_wrong_schema_is_refused(tmp_path):
    bad = tmp_path / "runs.json"
    bad.write_text(json.dumps({"schema": 0, "runs": []}), encoding="utf-8")
    with pytest.raises(se.ShipEfficiencyError):
        se.load_runs(bad)


# --- the document validators -------------------------------------------------------


@pytest.fixture(scope="module")
def doc():
    return (ROOT / se.REPORT_DOC).read_text(encoding="utf-8")


def test_the_shipped_document_fails_the_evaluation_while_the_runs_are_absent(doc):
    """The six paired scenarios have not run, so the validator must say so
    and refuse — a missing run is never a pass, and the reduced default is
    declared pending, not enabled."""
    assert not any((FIXTURES / f"scenario-{n}-{arm}.json").exists()
                   for n in range(1, 7) for arm in ("baseline", "candidate")), \
        "run evidence appeared at the shipped path; the document must be re-read before it claims anything"
    problems = se.validate_evaluation(doc, FIXTURES)
    assert problems, "validate-evaluation passed with no run evidence"
    for n in range(1, 7):
        assert any(p.startswith(f"scenario {n}: not run") for p in problems), n
    assert any("incomplete" in p and "6 missing run(s)" in p for p in problems)
    assert "pending" in doc.lower()
    assert not any("reduced_default=pending" in p for p in problems), problems


def test_a_complete_synthetic_evidence_set_passes_and_a_hole_in_it_fails(doc, tmp_path):
    """The validator's pass path, proved on the synthetic set under
    ``evaluation-synthetic/`` — twelve records the shipped check never points
    at — and its refusal the moment one record is taken away."""
    synthetic = FIXTURES / "evaluation-synthetic"
    records = sorted(p.name for p in synthetic.glob("scenario-*.json"))
    assert records == sorted(f"scenario-{n}-{arm}.json" for n in range(1, 7)
                             for arm in ("baseline", "candidate"))
    for name in records:
        rec = json.loads((synthetic / name).read_text(encoding="utf-8"))
        assert rec["synthetic"] is True and rec["source"].startswith("synthetic://")
    ran = doc
    for n in range(1, 7):
        ran = ran.replace(f"evaluation scenario={n} status=not-run claims=none",
                          f"evaluation scenario={n} status=ran")
    assert se.validate_evaluation(ran, synthetic, allow_synthetic=True) == []
    assert se.validate_evaluation(ran, FIXTURES) != [], "the shipped path holds no evidence"
    for name in records:
        (tmp_path / name).write_bytes((synthetic / name).read_bytes())
    (tmp_path / "scenario-5-candidate.json").unlink()
    problems = se.validate_evaluation(ran, tmp_path, allow_synthetic=True)
    assert any("scenario 5" in p and "scenario-5-candidate.json is missing" in p for p in problems)
    assert any("1 missing run(s)" in p for p in problems)
    on = ran.replace("claim key=reduced_default value=pending", "claim key=reduced_default value=on")
    assert any("reduced_default=pending" in p for p in se.validate_evaluation(on, tmp_path, allow_synthetic=True))
    assert se.validate_evaluation(on, synthetic, allow_synthetic=True) == []


def test_synthetic_evidence_never_passes_without_the_test_only_allowance(doc, tmp_path):
    """The synthetic set is refused by the validator as shipped — every one
    of its twelve records is named as synthetic — and the command line has no
    switch to allow it, so the shipped check cannot pass on shapes."""
    synthetic = FIXTURES / "evaluation-synthetic"
    ran = doc
    for n in range(1, 7):
        ran = ran.replace(f"evaluation scenario={n} status=not-run claims=none",
                          f"evaluation scenario={n} status=ran")
    problems = se.validate_evaluation(ran, synthetic)
    for n in range(1, 7):
        for arm in ("baseline", "candidate"):
            assert any(p.startswith(f"scenario {n} {arm}: evidence is marked synthetic") for p in problems), (n, arm)
            assert any(p.startswith(f"scenario {n} {arm}: evidence names a synthetic source") for p in problems), (n, arm)
    (tmp_path / "doc.md").write_text(ran, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(TOOL), "validate-evaluation", str(tmp_path / "doc.md"),
                           "--fixtures", str(synthetic)], capture_output=True, text=True, check=False)
    assert proc.returncode == 1 and "marked synthetic" in proc.stdout
    proc = subprocess.run([sys.executable, str(TOOL), "validate-evaluation", str(tmp_path / "doc.md"),
                           "--fixtures", str(synthetic), "--allow-synthetic"],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2, "no command-line allowance exists"
    # A record that only drops the marker but keeps the synthetic source is still refused.
    for name in synthetic.glob("scenario-*.json"):
        rec = json.loads(name.read_text(encoding="utf-8"))
        del rec["synthetic"]
        (tmp_path / name.name).write_text(json.dumps(rec), encoding="utf-8")
    problems = se.validate_evaluation(ran, tmp_path)
    assert any("names a synthetic source" in p for p in problems)
    assert not any("marked synthetic" in p for p in problems)


def _ran_doc(doc, n):
    return doc.replace(f"evaluation scenario={n} status=not-run claims=none",
                       f"evaluation scenario={n} status=ran")


def _evidence(n, arm, **kw):
    rec = {"scenario": n, "arm": arm, "source": f"runs/{n}-{arm[0]}", "redacted": True,
           "verdict": "ITERATE" if n >= 3 else "SHIP",
           "seeded_faults": [f"F{n}"] if n >= 3 else [],
           "caught": [f"F{n}"] if n >= 3 else [],
           "clean_verdict_preserved": True, "gates_run": ["pytest-full", "compileall"],
           "models": {"claude": "opus"}, "reasoning": "high",
           "cross_role_pass_reuse": False, "verifier_report_leaked": False}
    rec.update(kw)
    return rec


def _write_pair(folder, n, baseline, candidate):
    (folder / f"scenario-{n}-baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
    (folder / f"scenario-{n}-candidate.json").write_text(json.dumps(candidate), encoding="utf-8")


def test_a_complete_honest_pair_has_no_problem_of_its_own(doc, tmp_path):
    _write_pair(tmp_path, 4, _evidence(4, "baseline"), _evidence(4, "candidate"))
    problems = se.validate_evaluation(_ran_doc(doc, 4), tmp_path)
    assert not any("scenario 4" in p for p in problems), problems
    _write_pair(tmp_path, 1, _evidence(1, "baseline"), _evidence(1, "candidate"))
    problems = se.validate_evaluation(_ran_doc(doc, 1), tmp_path)
    assert not any("scenario 1" in p for p in problems), problems


@pytest.mark.parametrize("n,change,phrase", [
    (4, {"seeded_faults": []}, "names no seeded fault"),
    (4, {"seeded_faults": None}, "names no seeded fault"),
    (4, {"gates_run": []}, "no gate ran"),
    (1, {"gates_run": []}, "no gate ran"),
    (4, {"models": None}, "models are not stated"),
    (4, {"models": {}}, "models are not stated"),
    (4, {"reasoning": None}, "reasoning is not stated"),
    (4, {"reasoning": ""}, "reasoning is not stated"),
    (4, {"synthetic": True}, "marked synthetic"),
    (1, {"synthetic": True}, "marked synthetic"),
    (4, {"verdict": "MAYBE"}, "verdict must be one of"),
    (4, {"verdict": None}, "verdict must be one of"),
    (4, {"verdict": "SHIP"}, "planted fault did not stop the work"),
    (4, {"verdict": "PASS"}, "planted fault did not stop the work"),
    (3, {"verdict": "SHIP"}, "planted fault did not stop the work"),
    (6, {"verdict": "PASS"}, "planted fault did not stop the work"),
    (1, {"verdict": "ITERATE", "clean_verdict_preserved": False}, "a problem was invented"),
    (2, {"verdict": "FAIL", "clean_verdict_preserved": False}, "a problem was invented"),
    (2, {"verdict": "BLOCK", "clean_verdict_preserved": True}, "a problem was invented"),
    (1, {"verdict": "ITERATE", "clean_verdict_preserved": True}, "the flag must restate the verdict"),
    (2, {"verdict": "SHIP", "clean_verdict_preserved": False}, "the flag must restate the verdict"),
    (1, {"verdict": "PASS", "clean_verdict_preserved": None}, "the flag must restate the verdict"),
])
def test_vacuous_or_contradictory_evidence_is_refused_on_either_arm(doc, tmp_path, n, change, phrase):
    """Twelve records of empty lists, nulls and a flipped marker used to pass;
    every rung is now named, on the baseline arm and on the candidate arm.
    A clean scenario (1–2) is judged on its recorded verdict, never on the
    ``clean_verdict_preserved`` flag alone: an ITERATE that calls itself
    preserved, and a SHIP that disowns itself, are both refused."""
    for arm in ("baseline", "candidate"):
        other = "candidate" if arm == "baseline" else "baseline"
        _write_pair(tmp_path, n, _evidence(n, "baseline"), _evidence(n, "candidate"))
        (tmp_path / f"scenario-{n}-{arm}.json").write_text(
            json.dumps(_evidence(n, arm, **change)), encoding="utf-8")
        problems = se.validate_evaluation(_ran_doc(doc, n), tmp_path)
        assert any(p.startswith(f"scenario {n} {arm}: ") and phrase in p for p in problems), (arm, problems)
        assert not any(p.startswith(f"scenario {n} {other}: ") and phrase in p for p in problems), (other, problems)


def test_a_clean_scenario_may_end_ship_and_needs_no_seeded_fault(doc, tmp_path):
    _write_pair(tmp_path, 2, _evidence(2, "baseline", verdict="PASS"), _evidence(2, "candidate", verdict="SHIP"))
    problems = se.validate_evaluation(_ran_doc(doc, 2), tmp_path)
    assert not any("scenario 2" in p for p in problems), problems


def test_the_candidate_must_catch_its_own_seeded_faults_too(doc, tmp_path):
    cand = _evidence(5, "candidate", seeded_faults=["F5", "F5-extra"], caught=["F5"])
    _write_pair(tmp_path, 5, _evidence(5, "baseline"), cand)
    assert any("scenario 5: the candidate missed a seeded fault" in p
               for p in se.validate_evaluation(_ran_doc(doc, 5), tmp_path))


def test_a_marker_with_a_non_numeric_scenario_is_a_named_problem_not_a_crash(doc, tmp_path):
    text = doc.replace("evaluation scenario=3 status=not-run claims=none",
                       "evaluation scenario=x status=not-run claims=none")
    problems = se.validate_evaluation(text, FIXTURES)
    assert any("names no whole-number scenario: 'x'" in p for p in problems)
    assert any("must declare all six scenarios once; found [1, 2, 4, 5, 6]" in p for p in problems)
    (tmp_path / "doc.md").write_text(text, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(TOOL), "validate-evaluation", str(tmp_path / "doc.md"),
                           "--fixtures", str(FIXTURES)], capture_output=True, text=True, check=False)
    assert proc.returncode == 1 and "Traceback" not in proc.stderr
    assert "whole-number scenario" in proc.stdout


def test_one_not_run_scenario_among_five_ran_still_fails():
    """A document that ran five scenarios and declared the sixth not-run is
    incomplete, whatever the five say."""
    synthetic = FIXTURES / "evaluation-synthetic"
    text = "\n".join(f"<!-- ship-efficiency: evaluation scenario={n} status=ran -->" for n in range(1, 6))
    text += "\n<!-- ship-efficiency: evaluation scenario=6 status=not-run claims=none -->"
    text += "\n<!-- ship-efficiency: claim key=reduced_default value=pending -->\n"
    text += "Scenario 6 was not run; the reduced default is pending it.\n"
    problems = se.validate_evaluation(text, synthetic, allow_synthetic=True)
    assert any(p.startswith("scenario 6: not run") for p in problems)
    assert not any(p.startswith("scenario 5") for p in problems)


def test_validate_report_exits_zero_and_validate_evaluation_exits_one_on_the_shipped_document():
    proc = subprocess.run([sys.executable, str(TOOL), "validate-report", str(ROOT / se.REPORT_DOC)],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc = subprocess.run([sys.executable, str(TOOL), "validate-evaluation", str(ROOT / se.REPORT_DOC),
                           "--fixtures", str(FIXTURES)], capture_output=True, text=True, check=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    for n in range(1, 7):
        assert f"scenario {n}: not run" in proc.stdout, proc.stdout
    assert "incomplete" in proc.stdout


def test_a_claimed_run_without_evidence_fails_the_evaluation(doc, tmp_path):
    claimed = doc.replace("evaluation scenario=3 status=not-run claims=none",
                          "evaluation scenario=3 status=ran")
    problems = se.validate_evaluation(claimed, tmp_path)
    assert any("scenario 3" in p and "missing" in p for p in problems)


def test_a_not_run_scenario_that_claims_something_fails(doc):
    claimed = doc.replace("evaluation scenario=3 status=not-run claims=none",
                          "evaluation scenario=3 status=not-run claims=caught-all")
    assert any("may claim nothing" in p for p in se.validate_evaluation(doc.replace(doc, claimed), FIXTURES))


def test_a_declared_run_needs_the_candidate_to_catch_what_the_baseline_caught(doc, tmp_path):
    claimed = doc.replace("evaluation scenario=4 status=not-run claims=none",
                          "evaluation scenario=4 status=ran")
    base = {"scenario": 4, "arm": "baseline", "source": "runs/4-b", "redacted": True,
            "verdict": "ITERATE", "seeded_faults": ["F1"], "caught": ["F1", "W1"],
            "clean_verdict_preserved": True, "gates_run": ["pytest-full", "compileall"],
            "models": {"claude": "opus"}, "reasoning": "high",
            "cross_role_pass_reuse": False, "verifier_report_leaked": False}
    cand = dict(base, arm="candidate", source="runs/4-c", caught=["F1"])
    (tmp_path / "scenario-4-baseline.json").write_text(json.dumps(base), encoding="utf-8")
    (tmp_path / "scenario-4-candidate.json").write_text(json.dumps(cand), encoding="utf-8")
    problems = se.validate_evaluation(claimed, tmp_path)
    assert any("missed a finding the baseline caught" in p for p in problems)
    cand["caught"] = ["F1", "W1"]
    cand["gates_run"] = ["compileall"]
    (tmp_path / "scenario-4-candidate.json").write_text(json.dumps(cand), encoding="utf-8")
    assert any("skipped a required gate" in p for p in se.validate_evaluation(claimed, tmp_path))
    cand["gates_run"] = base["gates_run"]
    cand["verifier_report_leaked"] = True
    (tmp_path / "scenario-4-candidate.json").write_text(json.dumps(cand), encoding="utf-8")
    assert any("saw the implementer's report" in p for p in se.validate_evaluation(claimed, tmp_path))
    cand["verifier_report_leaked"] = False
    cand["models"] = {"claude": "haiku"}
    (tmp_path / "scenario-4-candidate.json").write_text(json.dumps(cand), encoding="utf-8")
    assert any("models or reasoning differ" in p for p in se.validate_evaluation(claimed, tmp_path))
    cand["models"] = base["models"]
    cand["cross_role_pass_reuse"] = True
    (tmp_path / "scenario-4-candidate.json").write_text(json.dumps(cand), encoding="utf-8")
    assert any("reused across roles" in p for p in se.validate_evaluation(claimed, tmp_path))
    cand["cross_role_pass_reuse"] = False
    (tmp_path / "scenario-4-candidate.json").write_text(json.dumps(cand), encoding="utf-8")
    problems = se.validate_evaluation(claimed, tmp_path)
    assert not any("scenario 4" in p for p in problems), problems
    assert any("5 missing run(s)" in p for p in problems), "the other five are still not run"


def test_a_met_runtime_claim_needs_a_results_file_over_complete_pairs(doc, tmp_path):
    met = doc.replace("claim key=runtime_input_reduction value=unmeasured",
                      "claim key=runtime_input_reduction value=met")
    problems = se.validate_report(met, ROOT)
    assert any("results file" in p for p in problems)
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"paired": {"cold": {"complete_pairs": 0, "target_met": False}}}), encoding="utf-8")
    met2 = met.replace("claim key=cost_saving value=none",
                       f"claim key=cost_saving value=none -->\n<!-- ship-efficiency: claim key=results value={results.name}")
    problems = se.validate_report(met2, tmp_path)
    assert any("complete cold pairs" in p for p in problems)


def test_an_unmeasured_target_may_not_claim_a_cost_saving(doc):
    greedy = doc.replace("claim key=cost_saving value=none", "claim key=cost_saving value=yes")
    assert any("no cost saving may be claimed" in p for p in se.validate_report(greedy, ROOT))
    sneaky = doc + "\nThis change saves $40 a month.\n"
    assert any("claims no cost saving yet says" in p for p in se.validate_report(sneaky, ROOT))


def test_a_report_missing_a_section_or_a_claim_is_refused(doc):
    assert any("## Money" in p for p in se.validate_report(doc.replace("## Money", "## Cash"), ROOT))
    assert any("usage_missing" in p for p in se.validate_report(
        doc.replace("claim key=usage_missing value=yes", ""), ROOT))


# --- the delegation ledgers ---------------------------------------------------------


def test_the_shunt_report_dedups_by_id_and_never_invents_a_cost():
    report = se.shunt_report(FIXTURES)
    # Six lines, one note without an id, one duplicate id → five.
    assert report["delegations"] == 5
    # 1200 + 640 + 180 + 90: the failed d-0005 record's 30 lines never count.
    assert report["lines_kept_out"] == 2110
    assert report["worker_cost"] == "unavailable"
    assert report["failed"] == 1
    assert report["per_mode"]["bulk-read"]["delegations"] == 3
    assert report["per_mode"]["code-write"]["delegations"] == 2
    assert report["per_provider"]["claude"]["delegations"] == 3
    assert report["per_provider"]["codex"]["worker_cost"] == "unavailable"
    assert "not_measured" in report and any("percentage" in n for n in report["not_measured"])


def test_a_bucket_where_every_record_measured_its_cost_reports_the_sum(tmp_path):
    (tmp_path / "a.jsonl").write_text(
        '{"delegation_id": "1", "provider": "claude", "mode": "bulk-read", "lines_kept_out": 10, "worker_cost_usd": 0.02}\n'
        '{"delegation_id": "2", "provider": "claude", "mode": "bulk-read", "lines_kept_out": 5, "worker_cost_usd": 0.015}\n'
        'garbage\n'
        '{"delegation_id": "2", "provider": "claude", "mode": "bulk-read", "lines_kept_out": 5, "worker_cost_usd": 0.015}\n')
    (tmp_path / "b.jsonl").write_text(
        '{"delegation_id": "3", "provider": "grok", "mode": "code-write", "lines_kept_out": 7, "worker_cost_usd": null}\n')
    report = se.shunt_report(tmp_path)
    assert report["ledgers"] == 2 and report["delegations"] == 3
    assert report["per_provider"]["claude"]["worker_cost"] == {"usd": 0.035}
    assert report["per_provider"]["grok"]["worker_cost"] == "unavailable"
    # One unknown among the whole makes the total unavailable, never a floor.
    assert report["worker_cost"] == "unavailable"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert se.shunt_report(empty)["delegations"] == 0
    assert se.shunt_report(empty)["worker_cost"] == "unavailable"


def test_a_failed_record_is_a_delegation_whose_lines_are_not_kept_out(tmp_path):
    (tmp_path / "a.jsonl").write_text(
        '{"delegation_id": "1", "provider": "claude", "mode": "bulk-read", "lines_kept_out": 10, "worker_cost_usd": 0.02, "ok": true}\n'
        '{"delegation_id": "2", "provider": "claude", "mode": "bulk-read", "lines_kept_out": 700, "worker_cost_usd": 0.02, "ok": false}\n')
    report = se.shunt_report(tmp_path)
    assert report["delegations"] == 2 and report["failed"] == 1
    assert report["lines_kept_out"] == 10
    assert report["per_provider"]["claude"]["lines_kept_out"] == 10
    assert report["per_provider"]["claude"]["worker_cost"] == {"usd": 0.04}


def test_the_shunt_subcommand_writes_the_report_and_refuses_a_non_directory(tmp_path):
    out = tmp_path / "shunt.json"
    proc = subprocess.run([sys.executable, str(TOOL), "shunt", "--ledgers", str(FIXTURES),
                           "--output", str(out)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text())
    assert data["delegations"] == 5 and data["worker_cost"] == "unavailable"
    assert "5 delegation(s)" in proc.stdout and "unavailable" in proc.stdout
    proc = subprocess.run([sys.executable, str(TOOL), "shunt", "--ledgers", str(tmp_path / "nope")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2 and "not a directory" in proc.stderr


def test_the_report_requires_the_delegation_section_and_an_honest_shunt_claim(doc):
    assert se.validate_report(doc, ROOT) == []
    assert doc.index("## Observed usage") < doc.index("## Delegation") < doc.index("## Bounded agent evaluation")
    assert any("## Delegation" in p for p in se.validate_report(doc.replace("## Delegation", "## Handoff"), ROOT))
    assert any("shunt" in p for p in se.validate_report(
        doc.replace("claim key=shunt value=unmeasured", ""), ROOT))
    claimed = doc.replace("claim key=shunt value=unmeasured", "claim key=shunt value=measured")
    assert any("results file" in p for p in se.validate_report(claimed, ROOT))
    odd = doc.replace("claim key=shunt value=unmeasured", "claim key=shunt value=ninety-percent")
    assert any("measured or unmeasured" in p for p in se.validate_report(odd, ROOT))
    # The article's figure is not claimed, in words.
    assert "not claimed" in doc and "unavailable" in doc.split("## Delegation")[1]


# --- input of the wrong shape ------------------------------------------------------


def test_a_baseline_inventory_without_aggregate_bytes_is_a_named_problem_not_a_crash(tmp_path):
    inventory = se.inventory(ROOT)
    problems = se.check_inventory(ROOT, inventory, {"rule": "legacy"})
    assert any("no whole-number aggregate_bytes" in p for p in problems)
    problems = se.check_inventory(ROOT, inventory, {"aggregate_bytes": "lots"})
    assert any("no whole-number aggregate_bytes" in p for p in problems)
    bad = tmp_path / "baseline.json"
    bad.write_text(json.dumps({"rule": "legacy"}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(TOOL), "inventory", "--root", str(ROOT), "--check",
                           "--baseline", str(bad)], capture_output=True, text=True, check=False)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr and "no whole-number aggregate_bytes" in proc.stdout
    bad.write_text(json.dumps([1, 2]), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(TOOL), "inventory", "--root", str(ROOT), "--check",
                           "--baseline", str(bad)], capture_output=True, text=True, check=False)
    assert proc.returncode == 2 and proc.stderr.startswith("error:") and "Traceback" not in proc.stderr


def test_main_turns_a_wrong_shaped_run_file_into_an_error_exit(tmp_path):
    """A run record with a non-numeric token count is a ``TypeError`` deep
    inside the aggregation; ``main`` names it and exits 2, no traceback."""
    runs = tmp_path / "runs.json"
    run = _run(1, 1000)
    run["roles"][0]["records"][0]["input_tokens"] = "many"
    runs.write_text(json.dumps({"schema": 1, "runs": [run]}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(TOOL), "report", "--baseline", str(runs),
                           "--candidate", str(runs), "--output", str(tmp_path / "out.json")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stderr.startswith("error: input of the wrong shape (TypeError") and "Traceback" not in proc.stderr
    runs.write_text(json.dumps({"schema": 1, "runs": [{"roles": [{"records": []}]}]}), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(TOOL), "report", "--baseline", str(runs),
                           "--candidate", str(runs), "--output", str(tmp_path / "out.json")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2 and proc.stderr.startswith("error: input of the wrong shape (KeyError")


# --- paragraphs ----------------------------------------------------------------------


def test_paragraphs_keep_fences_whole_split_bullets_and_drop_headings():
    text = "# Title\n\nA rule.\n\n```\ncode\n\nmore\n```\n\n- one\n  wrapped\n- two\n"
    blocks = se.paragraphs(text)
    assert blocks == ["A rule.", "```\ncode\n\nmore\n```", "- one\n  wrapped", "- two"]
    assert se.digest("A  rule.\n") == se.digest("A rule.")
