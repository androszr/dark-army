"""The pack's generic gate helper — `template/.claude/skills/ship/gate.sh`.

The local `gate.sh` (`test_ship_gate.py`) knows pytest and this repo's
layout; the pack's copy must not: the Identity table's command is passed to
it, failing ids are read from whichever runner printed them, the baseline
replay is `SHIP_BASELINE_CMD` with `{id}` substituted, and the lane's reviewer
regexes are arguments. Each subcommand is exercised against a throwaway git
repo with a fake test runner, exactly as an enrolled project would run it.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "host/dark_army_menubar/agent_pack/template/.claude/skills/ship/gate.sh"

RUNNER = """#!/bin/bash
# A fake test runner: every id named in `$FAIL_IDS` (space separated) fails.
code=0
for id in "$@"; do
    case " ${FAIL_IDS:-} " in
        *" $id "*) echo "FAILED $id - assert"; code=1 ;;
        *) echo "PASSED $id" ;;
    esac
done
exit $code
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def run(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests/runner.sh").write_text(RUNNER)
    (repo / "tests/runner.sh").chmod(0o755)
    (repo / "tests/thing.test").write_text("test_a\ntest_b\n")
    (repo / "src").mkdir()
    (repo / "src/other.js").write_text("x\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "pre-ship.patch").write_text("")
    (scratch / "pre-ship-staged.patch").write_text("")
    (scratch / "pre-ship-status.txt").write_text("")
    (scratch / "pre-ship-head.txt").write_text(_git(repo, "rev-parse", "HEAD"))

    def _run(*args: str, env: dict | None = None):
        full = {**os.environ, "SCRATCH": str(scratch), **(env or {})}
        return subprocess.run(["bash", str(GATE), *args], cwd=repo, env=full,
                              capture_output=True, text=True)

    _run.repo = repo
    _run.scratch = scratch
    return _run


def _rows(scratch: Path) -> list[dict]:
    text = (scratch / "ship-attempts.json").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_the_helper_is_executable_and_refuses_without_scratch():
    assert os.access(GATE, os.X_OK)
    env = {k: v for k, v in os.environ.items() if k != "SCRATCH"}
    out = subprocess.run(["bash", str(GATE), "digest"], env=env, capture_output=True, text=True)
    assert out.returncode == 2 and "SCRATCH" in out.stderr


def test_dispatch_row_matches_the_briefs_shape(run):
    assert run("dispatch", "1", "initial").returncode == 0
    (row,) = _rows(run.scratch)
    assert row == {"gate": "dispatch", "attempt": None, "failing_ids": [],
                   "delta_digest": "", "exit": None, "dispatch": 1, "reason": "initial"}


def test_run_counts_attempts_and_stops_on_the_same_failure_twice(run):
    cmd = ["--", "tests/runner.sh", "test_a", "test_b"]
    first = run("run", "test_gate-targeted", "1", *cmd, env={"FAIL_IDS": "test_b"})
    assert first.returncode == 1
    second = run("run", "test_gate-targeted", "1", *cmd, env={"FAIL_IDS": "test_b"})
    assert second.returncode == 4 and "SAME FAILURE TWICE" in second.stdout
    third = run("run", "test_gate-targeted", "1", *cmd, env={"FAIL_IDS": "test_a"})
    assert third.returncode == 1
    fourth = run("run", "test_gate-targeted", "1", *cmd)
    assert fourth.returncode == 3 and "BUDGET SPENT" in fourth.stdout
    rows = _rows(run.scratch)
    assert [r["attempt"] for r in rows] == [0, 1, 2]
    assert rows[0]["failing_ids"] == ["test_b"] and rows[2]["failing_ids"] == ["test_a"]
    assert all(r["gate"] == "test_gate-targeted" and r["dispatch"] == 1 for r in rows)


def test_a_full_gate_has_a_budget_of_two(run):
    for _ in range(2):
        run("run", "ios_test_gate-full", "2", "--", "tests/runner.sh", "test_a", env={"FAIL_IDS": "test_a"})
    out = run("run", "ios_test_gate-full", "2", "--", "tests/runner.sh", "test_a")
    assert out.returncode == 3
    assert len(_rows(run.scratch)) == 2


@pytest.mark.parametrize("line,expected", [
    ("FAILED tests/test_x.py::test_a - boom", "tests/test_x.py::test_a"),
    ("Test Case '-[AppTests.LoginTests testEmpty]' failed (0.12 seconds).", "-[AppTests.LoginTests testEmpty]"),
    ("  ✕ renders the header (12 ms)", "renders the header"),
    (" FAIL  src/app.test.ts > App > mounts", "src/app.test.ts > App > mounts"),
])
def test_failing_ids_are_read_from_any_runner(run, line, expected):
    printer = run.repo / "tests/print.sh"
    printer.write_text(f"#!/bin/bash\necho \"{line}\"\nexit 1\n")
    printer.chmod(0o755)
    out = run("run", "test_gate-targeted", "3", "--", "tests/print.sh")
    assert out.returncode == 1
    assert _rows(run.scratch)[-1]["failing_ids"] == [expected]


def test_a_non_test_gate_records_its_first_error_line(run):
    linter = run.repo / "lint.sh"
    linter.write_text("#!/bin/bash\necho 'checking'\necho 'src/other.js:1:1 error no-undef'\nexit 1\n")
    linter.chmod(0o755)
    out = run("run", "lint_gate", "1", "--", "./lint.sh")
    assert out.returncode == 1
    assert _rows(run.scratch)[-1]["failing_ids"] == ["src/other.js:1:1 error no-undef"]


def test_delta_is_the_tree_minus_the_baseline(run):
    (run.scratch / "pre-ship-status.txt").write_text(" M src/other.js\n")
    (run.repo / "src/other.js").write_text("x\ny\n")
    (run.scratch / "pre-ship.patch").write_text(_git(run.repo, "diff", "HEAD"))
    (run.repo / "src/new.js").write_text("a\nb\nc\n")
    (run.repo / "tests/thing.test").write_text("test_a\ntest_b\ntest_c\n")
    assert run("delta").returncode == 0
    paths = (run.scratch / "ship-delta-paths.txt").read_text().split()
    assert paths == ["src/new.js", "tests/thing.test"]
    patch = (run.scratch / "ship-delta.patch").read_text()
    assert "+test_c" in patch and "+a\n+b\n+c\n" in patch and "+y" not in patch
    # A baseline-dirty file edited again since is the run's, toward reviewing.
    (run.repo / "src/other.js").write_text("x\ny\nz\n")
    run("delta")
    assert "src/other.js" in (run.scratch / "ship-delta-paths.txt").read_text().split()


def test_classify_names_the_three_classes(run):
    # test_b is red at the baseline too; test_a's file is another run's edit;
    # test_x lives in a file this run's delta touched.
    (run.scratch / "pre-ship-status.txt").write_text("")
    (run.repo / "tests/thing.test").write_text("test_a\ntest_b\nedited\n")  # dirty now, clean at baseline
    (run.repo / "tests/mine.test").write_text("test_x\n")
    (run.scratch / "ship-delta-paths.txt").write_text("tests/mine.test\n")
    (run.scratch / "ship-delta.patch").write_text("")
    env = {"SHIP_BASELINE_CMD": "FAIL_IDS=tests/thing.test::test_b tests/runner.sh {id}"}
    out = run("classify", "tests/thing.test::test_a", "tests/thing.test::test_b",
              "tests/mine.test::test_x", env=env)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert any(l.startswith("YOURS tests/mine.test::test_x (the delta touches tests/mine.test)") for l in lines)
    assert any(l.startswith("PRE-EXISTING tests/thing.test::test_b") for l in lines)
    assert any(l.startswith("IN-FLIGHT tests/thing.test::test_a") for l in lines)
    assert (run.scratch / "pre-existing.txt").read_text().split() == ["tests/thing.test::test_b"]
    assert (run.scratch / "in-flight.txt").read_text().split() == ["tests/thing.test::test_a"]
    # The worktree is a second tree: the checkout itself is untouched.
    assert _git(run.repo, "status", "--porcelain").rstrip().splitlines() == [
        " M tests/thing.test", "?? tests/mine.test"]
    assert run("baseline", "--remove").stdout.strip() == "baseline worktree removed"
    # A classed id never lands in a later ledger row.
    out = run("run", "test_gate-full", "1", "--", "tests/runner.sh", "tests/thing.test::test_b",
              env={"FAIL_IDS": "tests/thing.test::test_b"})
    assert out.returncode == 1 and _rows(run.scratch)[-1]["failing_ids"] == []


def test_no_baseline_command_means_yours(run):
    (run.scratch / "ship-delta-paths.txt").write_text("")
    (run.scratch / "ship-delta.patch").write_text("")
    out = run("classify", "tests/thing.test::test_b")
    assert out.stdout.startswith("YOURS tests/thing.test::test_b (SHIP_BASELINE_CMD is not set")


@pytest.mark.parametrize("lines,path,regexes,lane", [
    (3, "src/new.js", ["ios/.*Keychain"], "fast"),
    (3, "src/Auth/Keychain.swift", ["ios/.*Keychain", "src/Auth/"], "full"),
    (200, "src/new.js", [], "full"),
])
def test_lane_reads_lines_and_the_reviewer_regexes(run, lines, path, regexes, lane):
    target = run.repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"l{i}\n" for i in range(lines)))
    out = run("lane", *regexes)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[0] == f"LANE: {lane}"
    assert f"lines: {lines} (fast at or under 150)" in out.stdout


def test_the_generic_briefs_route_every_gate_through_the_helper():
    template = ROOT / "host/dark_army_menubar/agent_pack/template"
    implementer = (template / ".claude/agents/{{P}}-implementer.md").read_text()
    verifier = (template / ".claude/agents/{{P}}-verifier.md").read_text()
    implement = (template / ".claude/skills/ship/references/implement.md").read_text()
    adapter = (template / ".claude/skills/ship/SKILL.md").read_text()
    settings = (template / ".claude/settings.json").read_text()
    for text in (implementer, verifier):
        assert "gate.sh classify" in text
        assert "PRE-EXISTING" in text and "IN-FLIGHT" in text and "YOURS" in text
        assert "SHIP_BASELINE_CMD" in text
    assert "gate.sh run" in implementer
    assert "Never write a ledger row by hand" in implementer
    assert 'IN-FLIGHT: <ids or "none">' in implementer
    assert "printf" not in implementer
    assert "A `YOURS` id is a FAIL" in verifier
    assert "### Phase 6.6: the lane" in implement
    assert "gate.sh lane" in implement and "LANE: fast" in implement
    assert "skip Phase 6.7 and 6.8" in implement
    assert "gate.sh dispatch <n>" in implement
    assert "printf" not in implement and "grep -qE" not in implement
    assert "gate.sh" in adapter
    assert '"Bash(bash .claude/skills/ship/gate.sh:*)"' in settings
