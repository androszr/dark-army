"""The iOS profile's preflight catches the two ways a privacy gate ends up red.

A scaffold that created the app plists without the dictation pair left
fit-app's `check-privacy-strings.py` red from its first commit, and a later
card that froze `ios/Config/` left its builder stuck between the gate and the
plan. I3b refuses the first plan, I3c the second.
"""

import shutil
import subprocess
from pathlib import Path

PACK = Path(__file__).resolve().parents[1] / "dark_army_menubar" / "agent_pack"
PREFLIGHT = PACK / "template" / ".claude" / "skills" / "ship" / "preflight.sh"
IOS = PACK / "profiles" / "ios-swift-testflight"


def _run(tmp_path: Path, plan_text: str, guard_exit: int | None = None) -> str:
    checklist = tmp_path / "checklist"
    checklist.mkdir(exist_ok=True)
    shutil.copy(IOS / "PREFLIGHT-ios.md", checklist / "PREFLIGHT-ios.md")
    (checklist / "PREFLIGHT-CHECKLIST.md").write_text("")
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    if guard_exit is not None:
        (project / "scripts" / "check-privacy-strings.py").write_text(
            f"import sys\nsys.exit({guard_exit})\n")
    plan = tmp_path / "plan.md"
    plan.write_text(plan_text)
    result = subprocess.run(
        ["bash", str(PREFLIGHT), str(plan), str(checklist)],
        cwd=project, capture_output=True, text=True, timeout=60)
    return result.stdout


def test_a_plan_creating_the_plists_without_the_pair_is_blocked(tmp_path):
    out = _run(tmp_path, "Step 1: create ios/Config/Info.plist and Info-Debug.plist.\n")
    assert "BLOCK: a plan creating the app plists" in out


def test_a_plan_creating_the_plists_with_the_pair_is_clean(tmp_path):
    out = _run(tmp_path, "Step 1: create ios/Config/Info.plist with "
               "NSMicrophoneUsageDescription and NSSpeechRecognitionUsageDescription.\n")
    assert "PREFLIGHT: clean" in out


def test_a_plan_editing_an_existing_plist_is_not_taken_for_a_scaffold(tmp_path):
    out = _run(tmp_path, "Step 2: add NSCameraUsageDescription to Info.plist "
               "and Info-Debug.plist.\n")
    assert "PREFLIGHT: clean" in out


def test_freezing_config_over_a_red_guard_is_blocked(tmp_path):
    out = _run(tmp_path, "Draw the cats. Do not modify ios/Config plists.\n", guard_exit=1)
    assert "BLOCK: the plan freezes ios/Config/" in out


def test_freezing_config_over_a_green_guard_is_clean(tmp_path):
    out = _run(tmp_path, "Draw the cats. Do not modify ios/Config plists.\n", guard_exit=0)
    assert "PREFLIGHT: clean" in out


def test_the_planner_rules_carry_both_lessons():
    rules = (IOS / "fragments" / "planner-rules.md").read_text()
    assert "Every app plist starts with the dictation pair" in rules
    assert "Freezing a guarded file checks its guard first" in rules
