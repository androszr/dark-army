"""The phone's own gate cannot be quietly deleted or watered down.

`.github/workflows/tests.yml` gained a third job on 6 Sep 2026: `phone`,
running `xcodebuild test` against an iOS Simulator, gated by a cheap
`changes` job on a 1x `ubuntu-latest` runner. Until then nothing in this
repository built the iPhone app, and a phone file that would not compile
passed every gate.

The interesting failures are not "the job is missing" — somebody deleting
forty lines of YAML notices. They are the ways the configuration can be
*wrong* while looking right:

- a workflow-level ``paths:`` key, which is the tempting fix for "only run
  the phone job on phone pushes" and silently stops ``host`` and ``panel``
  running on every other push, disabling the whole suite;
- a ``needs:`` or ``if:`` landing on ``host`` or ``panel``, the same effect
  by another route;
- the phone job's ``if:`` no longer reading the gate — a 10x macOS runner on
  every push;
- the gate's path list losing ``ios/`` or the workflow file's own path, the
  second of which makes the gate unrepairable by a workflow-only push;
- ``-skipPackagePluginValidation`` disappearing, which fails with one line
  naming a plugin nobody on a runner can press Trust for;
- ``-quiet`` or a ``| tail`` appearing, which throws away the middle of the
  log, where a failed ``xcodebuild`` run keeps everything worth reading;
- the host job's ``-n auto`` disappearing, which puts six minutes back on
  every push.

**No YAML parser is available here.** `yaml` imports in neither
`host/.venv` nor `/usr/bin/python3`, and `host/requirements-dev.txt` does not
list PyYAML — so these are text assertions over the file, this suite's usual
style, adding no dependency. Job blocks are sliced on two-space-indented
keys rather than by line number, so an inserted step does not break them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"

# The all-zeros sha GitHub reports for `github.event.before` on a branch's
# first push. The gate must recognise it and run anyway.
ZERO_SHA = "0" * 40


def _text() -> str:
    if not WORKFLOW.is_file():
        pytest.skip("no tests.yml in this checkout")
    return WORKFLOW.read_text(encoding="utf-8")


def _job(name: str) -> str:
    """One job's block: from its two-space-indented key to the next one."""
    text = _text()
    starts = [m.start() for m in re.finditer(r"(?m)^  (\w+):$", text)]
    keys = [re.match(r"  (\w+):", text[s:]).group(1) for s in starts]
    assert name in keys, f"no `{name}:` job in tests.yml; jobs are {keys}"
    index = keys.index(name)
    end = starts[index + 1] if index + 1 < len(starts) else len(text)
    return text[starts[index]:end]


def test_the_gate_and_the_phone_job_both_exist():
    text = _text()
    assert "\n  changes:\n" in text
    assert "\n  phone:\n" in text


def test_the_phone_job_runs_on_the_sdk_the_archive_ships_from():
    # macos-15 still ships the iOS 18 SDK; a gate green against a different
    # SDK than the shipped binary can pass while TestFlight fails.
    assert "runs-on: macos-26" in _job("phone")


def test_the_phone_job_actually_runs_the_tests():
    phone = _job("phone")
    assert "xcodebuild test" in phone
    assert "-scheme BobPhone" in phone
    assert "-project ios/BobPhone.xcodeproj" in phone
    assert "-skipPackagePluginValidation" in phone


def test_the_phone_job_is_gated_on_the_cheap_job():
    phone = _job("phone")
    assert "needs: changes" in phone
    assert "if: needs.changes.outputs.phone == 'true'" in phone


def test_the_gate_names_both_the_phone_tree_and_the_workflow_itself():
    # Without its own path the gate could never be repaired by a push that
    # only edits this file.
    changes = _job("changes")
    assert "^ios/" in changes
    assert "tests.yml" in changes


def test_the_gate_fails_toward_running():
    # A new branch, a force push or a shallow history leaves the diff
    # undecidable; an undecidable diff must run the job, never skip it.
    changes = _job("changes")
    assert ZERO_SHA in changes
    assert "phone=true" in changes


def test_the_trigger_is_still_a_bare_push_with_no_path_filter():
    text = _text()
    assert re.search(r"(?m)^on:\n  push:\s*$", text), "the trigger moved"
    # A workflow-level `paths:` narrows *every* job, `host` and `panel`
    # included. It is the one forbidden fix.
    assert not re.search(r"(?m)^    paths:", text)
    assert not re.search(r"(?m)^      paths:", text)


def test_the_two_always_on_jobs_are_still_ungated():
    for name in ("host", "panel"):
        block = _job(name)
        assert "needs:" not in block, name
        assert "if:" not in block, name
        assert "runs-on: macos-15" in block, name


def test_the_host_job_runs_the_suite_across_workers():
    # Comment lines are dropped first, as in the phone job's pin: the step's
    # explanatory comment must neither satisfy nor break the assertion.
    lines = [line for line in _job("host").splitlines()
             if not line.lstrip().startswith("#")]
    invocation = "python -m pytest -q -n auto -p no:cacheprovider"
    assert "\n".join(lines).count(invocation) == 1, (
        "the host job's Test step no longer runs the suite across workers"
    )
    pytest_lines = [line for line in lines if "pytest" in line]
    assert pytest_lines, "no pytest invocation in the host job"
    for line in pytest_lines:
        # Unpiped: a failed xdist run's `[gwN]` traceback is mid-log.
        assert "|" not in line, line


def test_the_phone_job_does_not_throw_away_its_own_log():
    # Comment lines are dropped first: the job carries a comment saying why
    # neither flag is there, and a test that could not tell the two apart
    # would fail on the explanation.
    #
    # Only the `xcodebuild` invocation is judged: `| tail -1` is how the
    # Xcode-selection step picks the newest toolchain, on the panel job too.
    lines = [line for line in _job("phone").splitlines()
             if not line.lstrip().startswith("#")]
    # The *last* mention, not the first: the job's own name is
    # "Phone (xcodebuild test)".
    start = max(i for i, line in enumerate(lines) if "xcodebuild test" in line)
    invocation = "\n".join(lines[start:])
    assert "-quiet" not in invocation
    assert "| tail" not in invocation
    assert "tail" not in invocation
