"""The ship run's gate helper, `.claude/skills/ship/gate.sh`, and the briefs
that route their gates through it.

The 21 Sep 2026 audit of four implement runs found the full host suite —
nine to ten minutes single-process under fleet load — run four to six times
per card, a baseline that was never green, every helper retyping the ledger
row and the worktree recipe by hand, and every phone card sent through a
security review by a blanket `ios/BobPhone/` match. These tests pin the
helper's behaviour on a throwaway repository and the words in the briefs
that make the helper the route.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / ".claude/skills/ship/gate.sh"
PYTEST = Path(sys.executable).with_name("pytest")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def _gate(repo: Path, scratch: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    # The helper's pytest boots carry no plugin autoload: the throwaway repo
    # needs none, and each boot was 1.5 s of xdist/asyncio/timeout imports.
    env = dict(os.environ, SCRATCH=str(scratch), SHIP_PYTEST=str(PYTEST),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return subprocess.run(["bash", str(GATE), *args], cwd=cwd or repo,
                          env=env, capture_output=True, text=True)


@pytest.fixture
def run(tmp_path: Path):
    """A committed repository with one test file — one green test, one red —
    and a scratch directory outside it holding the Phase 0 baseline."""
    repo = tmp_path / "repo"
    (repo / "host" / "tests").mkdir(parents=True)
    (repo / "host/tests/test_thing.py").write_text(
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert False\n", encoding="utf-8")
    _git(repo, "init", "-q", ".")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "pre-ship-head.txt").write_text(_git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    (scratch / "pre-ship.patch").write_text("", encoding="utf-8")
    (scratch / "pre-ship-staged.patch").write_text("", encoding="utf-8")
    (scratch / "pre-ship-status.txt").write_text("", encoding="utf-8")
    (scratch / "ship-delta-paths.txt").write_text("host/dark_army_daemon/other.py\n", encoding="utf-8")
    (scratch / "ship-delta.patch").write_text("+one\n+two\n-three\n", encoding="utf-8")
    yield repo, scratch
    _gate(repo, scratch, "baseline", "--remove")


def _ledger(scratch: Path) -> list[dict]:
    return [json.loads(line) for line in (scratch / "ship-attempts.json").read_text(encoding="utf-8").splitlines() if line]


def test_the_helper_refuses_to_run_without_a_scratch_directory(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "SCRATCH"}
    done = subprocess.run(["bash", str(GATE), "lane"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert done.returncode == 2 and "SCRATCH" in done.stderr


def test_dispatch_writes_the_orchestrator_row_the_briefs_describe(run):
    repo, scratch = run
    assert _gate(repo, scratch, "dispatch", "1", "initial").returncode == 0
    (row,) = _ledger(scratch)
    assert row == {"gate": "dispatch", "attempt": None, "failing_ids": [], "delta_digest": "",
                   "exit": None, "dispatch": 1, "reason": "initial"}


def test_run_logs_counts_and_stops_on_the_same_failure_twice(run):
    """Attempt 0 is the first run; the same ids red on the next attempt exit 4
    — the same-failure stop — and a spent budget exits 3 without running."""
    repo, scratch = run
    red = ["sh", "-c", 'echo "FAILED tests/test_thing.py::test_b - boom"; exit 1']
    first = _gate(repo, scratch, "run", "pytest-targeted", "1", "--", *red)
    assert first.returncode == 1
    assert (scratch / "gate-pytest-targeted-d1-a0.log").exists()
    second = _gate(repo, scratch, "run", "pytest-targeted", "1", "--", *red)
    assert second.returncode == 4 and "SAME FAILURE TWICE" in second.stdout
    rows = _ledger(scratch)
    assert [r["attempt"] for r in rows] == [0, 1]
    assert all(r["failing_ids"] == ["tests/test_thing.py::test_b"] and r["exit"] == 1 and r["dispatch"] == 1 for r in rows)
    assert all(len(r["delta_digest"]) == 16 for r in rows)
    third = _gate(repo, scratch, "run", "pytest-targeted", "1", "--", *red)
    assert third.returncode == 4  # attempt 2 of 3 runs, and the ids are the same again
    fourth = _gate(repo, scratch, "run", "pytest-targeted", "1", "--", *red)
    assert fourth.returncode == 3 and "BUDGET SPENT" in fourth.stdout
    assert len(_ledger(scratch)) == 3, "a refused run writes no row"


def test_the_full_suite_has_a_budget_of_two(run):
    repo, scratch = run
    green = ["sh", "-c", "echo ok"]
    assert _gate(repo, scratch, "run", "pytest-full", "2", "--", *green).returncode == 0
    assert _gate(repo, scratch, "run", "pytest-full", "2", "--", *green).returncode == 0
    assert _gate(repo, scratch, "run", "pytest-full", "2", "--", *green).returncode == 3
    assert _gate(repo, scratch, "run", "pytest-full", "3", "--", *green).returncode == 0, "a new dispatch starts at attempt 0"


def test_a_non_pytest_gate_records_its_first_error_line(run):
    repo, scratch = run
    done = _gate(repo, scratch, "run", "swift", "1", "--", "sh", "-c", 'echo "error: thing.swift:3: no"; exit 1')
    assert done.returncode == 1
    (row,) = _ledger(scratch)
    assert row["gate"] == "swift" and row["failing_ids"] == ["error: thing.swift:3: no"]


def test_classify_names_the_three_classes_and_replays_by_id(run):
    """`test_b` was red at the baseline: PRE-EXISTING. `test_a` and a new
    `test_c` are green or absent there, in a file dirty now, clean at the
    baseline and outside the delta: IN-FLIGHT — another run's work. An id
    whose module the delta touches is YOURS without any replay."""
    repo, scratch = run
    (repo / "host/tests/test_thing.py").write_text(
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert False\n\n\ndef test_c():\n    assert False\n",
        encoding="utf-8")
    done = _gate(repo, scratch, "classify",
                 "tests/test_thing.py::test_b", "tests/test_thing.py::test_a",
                 "tests/test_thing.py::test_c", "tests/test_other.py::test_x")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert any(line.startswith("YOURS tests/test_other.py::test_x (the delta touches host/dark_army_daemon/other.py)") for line in lines)
    assert any(line.startswith("PRE-EXISTING tests/test_thing.py::test_b") for line in lines)
    assert any(line.startswith("IN-FLIGHT tests/test_thing.py::test_a") for line in lines)
    assert any(line.startswith("IN-FLIGHT tests/test_thing.py::test_c") for line in lines)
    assert (scratch / "pre-existing.txt").read_text(encoding="utf-8").split() == ["tests/test_thing.py::test_b"]
    assert set((scratch / "in-flight.txt").read_text(encoding="utf-8").split()) == {
        "tests/test_thing.py::test_a", "tests/test_thing.py::test_c"}
    log = (scratch / "baseline-0.log").read_text(encoding="utf-8")
    assert log.count("=== tests/test_thing.py::") == 3, "one pytest run per id, never the whole suite"
    assert (scratch / "baseline" / ".git").exists(), "the worktree lives under the scratch directory"
    assert _git(repo, "status", "--porcelain").rstrip() == " M host/tests/test_thing.py", "the tree itself is untouched"


def test_a_classed_id_stays_out_of_the_ledger_row(run):
    repo, scratch = run
    (scratch / "pre-existing.txt").write_text("tests/test_thing.py::test_b\n", encoding="utf-8")
    red = ["sh", "-c", 'echo "FAILED tests/test_thing.py::test_b - boom"; echo "FAILED tests/test_thing.py::test_z - x"; exit 1']
    _gate(repo, scratch, "run", "pytest-full", "1", "--", *red)
    (row,) = _ledger(scratch)
    assert row["failing_ids"] == ["tests/test_thing.py::test_z"]


def test_an_unbuildable_baseline_reports_unavailable_and_treats_the_id_as_yours(run):
    repo, scratch = run
    (scratch / "pre-ship.patch").write_text("--- a/nope\n+++ b/nope\n@@ -1 +1 @@\n-x\n+y\n", encoding="utf-8")
    done = _gate(repo, scratch, "baseline", "tests/test_thing.py::test_b")
    assert done.stdout.startswith("UNAVAILABLE tests/test_thing.py::test_b")
    classified = _gate(repo, scratch, "classify", "tests/test_thing.py::test_b")
    assert classified.stdout.startswith("YOURS tests/test_thing.py::test_b")


def test_snapshot_omits_binary_artifacts(run):
    """A dirty .vsix is the hunk `git apply` cannot replay. Snapshot drops
    it; a text change next to it still lands in the patch."""
    repo, scratch = run
    vsix = repo / "pack.vsix"
    vsix.write_bytes(b"PK\x03\x04old")
    (repo / "host/tests/note.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bin")
    vsix.write_bytes(b"PK\x03\x04new")
    (repo / "host/tests/note.py").write_text("a = 2\n", encoding="utf-8")
    done = _gate(repo, scratch, "snapshot")
    assert done.returncode == 0, done.stderr
    patch = (scratch / "pre-ship.patch").read_text(encoding="utf-8", errors="replace")
    assert "pack.vsix" not in patch
    assert "Binary files" not in patch
    assert "note.py" in patch and "+a = 2" in patch


def test_a_binary_hunk_does_not_make_the_baseline_unbuildable(run):
    """A leftover vsix hunk in a hand-rolled patch is stripped on apply."""
    repo, scratch = run
    (scratch / "pre-ship.patch").write_text(
        "diff --git a/pack.vsix b/pack.vsix\n"
        "index 111..222\n"
        "Binary files a/pack.vsix and b/pack.vsix differ\n",
        encoding="utf-8")
    done = _gate(repo, scratch, "classify", "tests/test_thing.py::test_b")
    assert done.returncode == 0, done.stderr + done.stdout
    assert done.stdout.startswith("PRE-EXISTING tests/test_thing.py::test_b"), done.stdout


def test_classify_phone_grep_is_in_flight_when_ios_is_dirty(run):
    """Phone source-grep tests read ios/BobPhone/. A dirty Swift file
    outside the delta is another run, even when the test file is clean."""
    repo, scratch = run
    (repo / "host/tests/test_phone_terminal.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    swift = repo / "ios" / "BobPhone" / "AgentDetailView.swift"
    swift.parent.mkdir(parents=True)
    swift.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "phone")
    (scratch / "pre-ship-head.txt").write_text(
        _git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    swift.write_text("new\n", encoding="utf-8")
    done = _gate(repo, scratch, "classify",
                 "tests/test_phone_terminal.py::test_x")
    assert done.returncode == 0, done.stderr + done.stdout
    assert done.stdout.startswith(
        "IN-FLIGHT tests/test_phone_terminal.py::test_x"), done.stdout
    assert "ios/BobPhone/AgentDetailView.swift" in done.stdout


def test_classify_phone_grep_is_yours_when_the_delta_touches_ios(run):
    repo, scratch = run
    (repo / "host/tests/test_phone_terminal.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8")
    swift = repo / "ios" / "BobPhone" / "AgentDetailView.swift"
    swift.parent.mkdir(parents=True)
    swift.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "phone")
    (scratch / "pre-ship-head.txt").write_text(
        _git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    (scratch / "ship-delta-paths.txt").write_text(
        "ios/BobPhone/AgentDetailView.swift\n", encoding="utf-8")
    swift.write_text("new\n", encoding="utf-8")
    done = _gate(repo, scratch, "classify",
                 "tests/test_phone_terminal.py::test_x")
    assert done.returncode == 0, done.stderr + done.stdout
    assert "YOURS tests/test_phone_terminal.py::test_x (the delta touches ios/BobPhone/)" in done.stdout


def test_classify_phone_grep_is_in_flight_when_a_sibling_is_dirty(run):
    """Delta touches one phone file; another phone file is dirty outside
    the delta. A tree-wide grep is IN-FLIGHT on the sibling, not YOURS
    on the prefix — otherwise a concurrent CardDetailView edit makes
    every phone card FAIL verify."""
    repo, scratch = run
    (repo / "host/tests/test_phone_card_write_feedback.py").write_text(
        "def test_dims():\n    assert True\n", encoding="utf-8")
    conv = repo / "ios" / "BobPhone" / "ConversationView.swift"
    card = repo / "ios" / "BobPhone" / "CardDetailView.swift"
    conv.parent.mkdir(parents=True)
    conv.write_text("old-conv\n", encoding="utf-8")
    card.write_text("old-card\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "phone")
    (scratch / "pre-ship-head.txt").write_text(
        _git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    (scratch / "ship-delta-paths.txt").write_text(
        "ios/BobPhone/ConversationView.swift\n", encoding="utf-8")
    conv.write_text("new-conv\n", encoding="utf-8")
    card.write_text("new-card\n", encoding="utf-8")
    done = _gate(repo, scratch, "classify",
                 "tests/test_phone_card_write_feedback.py::test_dims")
    assert done.returncode == 0, done.stderr + done.stdout
    assert done.stdout.startswith(
        "IN-FLIGHT tests/test_phone_card_write_feedback.py::test_dims"), done.stdout
    assert "ios/BobPhone/CardDetailView.swift" in done.stdout


def test_classify_inventory_grep_is_in_flight_when_a_docs_sibling_is_dirty(run):
    """Inventory tests grep docs/. Delta touches one subject document;
    another is dirty outside the delta. A tree-wide load check is
    IN-FLIGHT on the sibling, not YOURS on the prefix — otherwise a
    concurrent context-panel edit makes every docs card FAIL verify."""
    repo, scratch = run
    (repo / "host/tests/test_ship_context.py").write_text(
        "def test_inventory_check_passes_on_this_tree():\n    assert True\n",
        encoding="utf-8")
    host_doc = repo / "docs" / "context-host.md"
    panel_doc = repo / "docs" / "context-panel.md"
    host_doc.parent.mkdir(parents=True)
    host_doc.write_text("old-host\n", encoding="utf-8")
    panel_doc.write_text("old-panel\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs")
    (scratch / "pre-ship-head.txt").write_text(
        _git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    (scratch / "ship-delta-paths.txt").write_text(
        "docs/context-host.md\n", encoding="utf-8")
    host_doc.write_text("new-host\n", encoding="utf-8")
    panel_doc.write_text("new-panel\n", encoding="utf-8")
    done = _gate(repo, scratch, "classify",
                 "tests/test_ship_context.py::test_inventory_check_passes_on_this_tree")
    assert done.returncode == 0, done.stderr + done.stdout
    assert done.stdout.startswith(
        "IN-FLIGHT tests/test_ship_context.py::test_inventory_check_passes_on_this_tree"
    ), done.stdout
    assert "docs/context-panel.md" in done.stdout


def test_classify_inventory_grep_is_yours_when_only_the_delta_docs_moved(run):
    repo, scratch = run
    (repo / "host/tests/test_ship_context.py").write_text(
        "def test_inventory_check_passes_on_this_tree():\n    assert True\n",
        encoding="utf-8")
    host_doc = repo / "docs" / "context-host.md"
    host_doc.parent.mkdir(parents=True)
    host_doc.write_text("old-host\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs")
    (scratch / "pre-ship-head.txt").write_text(
        _git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    (scratch / "ship-delta-paths.txt").write_text(
        "docs/context-host.md\n", encoding="utf-8")
    host_doc.write_text("new-host\n", encoding="utf-8")
    done = _gate(repo, scratch, "classify",
                 "tests/test_ship_context.py::test_inventory_check_passes_on_this_tree")
    assert done.returncode == 0, done.stderr + done.stdout
    assert "YOURS tests/test_ship_context.py::test_inventory_check_passes_on_this_tree (the delta touches docs/)" in done.stdout


def test_a_missing_pytest_is_not_an_attempt(run):
    """Exit 127/4/5 with no parsed ids is cwd/argv, not a test failure."""
    repo, scratch = run
    done = _gate(repo, scratch, "run", "pytest-targeted", "1", "--",
                 "sh", "-c", "exit 127")
    assert done.returncode == 127
    assert "OPERATOR" in done.stdout
    assert not (scratch / "ship-attempts.json").exists()
    red = ["sh", "-c", 'echo "FAILED tests/test_thing.py::test_b - boom"; exit 1']
    second = _gate(repo, scratch, "run", "pytest-targeted", "1", "--", *red)
    assert second.returncode == 1
    (row,) = _ledger(scratch)
    assert row["attempt"] == 0 and row["failing_ids"] == ["tests/test_thing.py::test_b"]


def test_pytest_gates_run_from_host(run):
    repo, scratch = run
    done = _gate(repo, scratch, "run", "pytest-targeted", "1", "--",
                 "sh", "-c", "echo ok")
    assert done.returncode == 0, done.stderr + done.stdout
    log = (scratch / "gate-pytest-targeted-d1-a0.log").read_text(encoding="utf-8")
    assert f"cwd: {repo / 'host'}" in log


def test_unbuildable_baseline_with_dirty_ios_is_in_flight(run):
    repo, scratch = run
    (repo / "host/tests/test_agent_chatter.py").write_text(
        "def test_sites():\n    assert True\n", encoding="utf-8")
    swift = repo / "ios" / "BobPhone" / "ConversationView.swift"
    swift.parent.mkdir(parents=True)
    swift.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "chatter")
    (scratch / "pre-ship-head.txt").write_text(
        _git(repo, "rev-parse", "HEAD"), encoding="utf-8")
    (scratch / "pre-ship.patch").write_text(
        "--- a/nope\n+++ b/nope\n@@ -1 +1 @@\n-x\n+y\n", encoding="utf-8")
    swift.write_text("new\n", encoding="utf-8")
    done = _gate(repo, scratch, "classify",
                 "tests/test_agent_chatter.py::test_sites")
    assert done.returncode == 0, done.stderr + done.stdout
    assert done.stdout.startswith(
        "IN-FLIGHT tests/test_agent_chatter.py::test_sites"), done.stdout


@pytest.mark.parametrize("paths, patch_lines, lane, why", [
    ("host/dark_army_daemon/other.py\n", 3, "fast", "lines: 3"),
    ("host/dark_army_daemon/other.py\n", 151, "full", "lines: 151"),
    ("host/setup.py\n", 3, "full", "integration: host/setup.py"),
    ("panel/Sources/BobPanel/BoardModels.swift\n", 3, "full",
     "integration: panel/Sources/BobPanel/BoardModels.swift"),
    ("panel/Sources/BobPanel/Settings.swift\n", 3, "fast", "integration: no"),
    ("ios/BobPhone/Client.swift\n", 3, "full", "security: ios/BobPhone/Client.swift"),
    ("ios/BobPhone/CardDetailView.swift\n", 3, "fast", "security: no"),
    ("panel/Sources/BobPanel/Foo.swift\n", 3, "full", "content: X-Bob-Token"),
])
def test_the_lane_is_read_off_the_delta_alone(run, paths, patch_lines, lane, why):
    """Fast at or under 150 changed lines with no integration path and no
    security floor matched; a phone screen that draws a card is not a door,
    a header in the diff's content is wherever it lives."""
    repo, scratch = run
    (scratch / "ship-delta-paths.txt").write_text(paths, encoding="utf-8")
    body = "".join(f"+line {i}\n" for i in range(patch_lines))
    if why.startswith("content"):
        body = body.replace("+line 0\n", '+    request.setValue(token, forHTTPHeaderField: "X-Bob-Token")\n')
    (scratch / "ship-delta.patch").write_text(body, encoding="utf-8")
    done = _gate(repo, scratch, "lane")
    assert done.stdout.splitlines()[0] == f"LANE: {lane}", done.stdout
    assert why in done.stdout, done.stdout


def test_a_missing_delta_identity_is_the_full_lane(run):
    repo, scratch = run
    (scratch / "ship-delta.patch").unlink()
    assert _gate(repo, scratch, "lane").stdout.startswith("LANE: full (no delta identity")


# --- the briefs route their gates through the helper -----------------------------

IMPLEMENTER = REPO / ".claude/agents/bc-implementer.md"
VERIFIER = REPO / ".claude/agents/bc-verifier.md"
IMPLEMENT = REPO / ".claude/skills/ship/references/implement.md"
ADAPTER = REPO / ".claude/skills/ship/SKILL.md"
COMMON = REPO / ".claude/skills/ship/references/common.md"


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_the_full_suite_runs_across_workers_everywhere_it_is_named():
    for path in (IMPLEMENTER, VERIFIER, IMPLEMENT):
        text = _flat(path)
        assert ".venv/bin/pytest -q -n auto -p no:cacheprovider" in text, path
        assert ".venv/bin/pytest -q`" not in text, f"{path}: a single-process full suite is still named"
    ini = (REPO / "host/pytest.ini").read_text(encoding="utf-8")
    assert "timeout = 300" in ini and "-n" not in ini.split("timeout")[-1]
    dev = (REPO / "host/requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest-xdist" in dev and "pytest-timeout" in dev


def test_the_briefs_name_the_helper_and_the_three_classes():
    for path in (IMPLEMENTER, VERIFIER):
        text = _flat(path)
        assert "gate.sh classify" in text, path
        for word in ("PRE-EXISTING", "IN-FLIGHT", "YOURS"):
            assert word in text, (path, word)
    implementer = _flat(IMPLEMENTER)
    assert "gate.sh run" in implementer and "Never write a ledger row by hand" in implementer
    assert "IN-FLIGHT: <ids or \"none\">" in implementer
    assert "when that file exists" in implementer
    assert "printf" not in implementer, "the inline ledger row is retired"
    verifier = _flat(VERIFIER)
    assert "A `YOURS` id is a FAIL" in verifier
    assert "you never run it for them" not in verifier
    assert "ios/BobPhone/" in verifier
    assert "docs/" in verifier
    assert "do not run the subset first" in verifier
    assert "docs/" in implementer
    common = _flat(COMMON)
    assert "still working?" in common
    assert "does **not** pre-read the subject documents" in common or "does not pre-read the subject documents" in common
    assert "gate.sh" in _flat(ADAPTER)
    assert "gate.sh snapshot" in _flat(COMMON)
    assert "gate.sh snapshot" in _flat(IMPLEMENT)


def test_the_implement_reference_has_the_lane_and_one_copy_of_the_floors():
    text = _flat(IMPLEMENT)
    assert "### Phase 6.6: the lane" in text
    assert "LANE: fast" in text and "skip Phase 6.7, 6.8 and 6.9" in text
    assert "### Phases 6.7–6.9: one parallel panel" in text
    assert "do not run the subset first" in text
    assert "gate.sh dispatch <n>" in text and "printf" not in text
    assert "grep -qE" not in text, "the review floors live in gate.sh, not inline"
    for name in ("INTEGRATION_PATHS", "SECURITY_PATHS", "SECURITY_CONTENT"):
        assert name in text, name
    gate = GATE.read_text(encoding="utf-8")
    assert "ios/BobPhone/|" not in gate, "the blanket phone match is retired"
    assert "ios/BobPhone/(Client|Pairing|Push|RelayTransport|RemoteAuth" in gate
    assert "the full suite at most once more and only when the fix reached a module" in text
