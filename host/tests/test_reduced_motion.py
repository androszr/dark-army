"""Motion follows the system's Reduce Motion setting on both apps.

`plans/2026-09-25-usability-accessibility-pass.md`, Phase 3. Every animation
the panel and the phone own goes through `Motion` — `Motion.animate` for a
`withAnimation`, `Motion.animation` for an `.animation(_:value:)` — which is
no animation at all when the owning view reads
`accessibilityReduceMotion` as true. Two files keep their own reduced forms
and are not swept: `DecryptFeedback.swift` (`DecryptMotion.frame(reduced:)`)
and `AgentChatter.swift`, already byte-pinned from its marker down.
`Motion.swift` is a pair too, byte-equal from `enum Motion {` down.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PHONE = ROOT / "ios" / "BobPhone"
WIDGET = ROOT / "ios" / "BobPhoneWidget"
PROJECT = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
PANEL_MOTION = PANEL / "Motion.swift"
PHONE_MOTION = PHONE / "Motion.swift"
MARKER = "enum Motion {"

#: Files that keep their own reduced forms and are not swept.
EXEMPT = {"Motion.swift", "AgentChatter.swift", "DecryptFeedback.swift"}


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def _shared(path: Path) -> str:
    lines = _read(path).splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


def _swept() -> list[Path]:
    out = [p for folder in (PANEL, PHONE) for p in sorted(folder.glob("*.swift"))
           if p.name not in EXEMPT]
    return out


def test_the_two_copies_are_one_rule():
    assert _shared(PANEL_MOTION) == _shared(PHONE_MOTION)


def test_a_doctored_copy_would_not_compare_equal():
    doctored = _shared(PHONE_MOTION).replace("reduced ? nil : animation",
                                             "animation", 1)
    assert doctored != _shared(PANEL_MOTION)


def test_the_sweep_sees_both_apps():
    names = [p.name for p in _swept()]
    assert len(names) >= 150, len(names)
    for expected in ("PanelView.swift", "BoardView.swift", "BoardCardView.swift",
                     "ProjectTabs.swift", "AgentDetailView.swift"):
        assert expected in names, expected


def test_no_bare_animation_outside_motion():
    offenders = []
    for path in _swept():
        code = _code(_read(path))
        if "withAnimation(" in code:
            offenders.append(f"{path.name}: withAnimation(")
        # A modifier `.animation(` whose argument is not `Motion.animation(`;
        # the `(?<!Motion)` keeps the helper's own name out of the count.
        for match in re.finditer(r"(?<!Motion)\.animation\(", code):
            if not code.startswith(".animation(Motion.animation(", match.start()):
                offenders.append(f"{path.name}: .animation(")
    assert offenders == []


def test_every_motion_caller_reads_the_system_setting():
    callers = [p for p in _swept() if "Motion.anim" in _code(_read(p))]
    assert {p.name for p in callers} >= {
        "PanelView.swift", "BoardView.swift", "BoardCardView.swift",
        "ProjectTabs.swift", "AgentDetailView.swift"}
    for path in callers:
        code = _code(_read(path))
        assert "@Environment(\\.accessibilityReduceMotion) private var reduceMotion" in code, path.name
        for call in re.findall(r"Motion\.anim\w+\([^\n]*", code):
            assert "reduced: reduceMotion" in call, (path.name, call)


def test_the_chatter_and_the_decrypt_keep_their_own_forms():
    for folder in (PANEL, PHONE):
        chatter = _read(folder / "AgentChatter.swift")
        assert "Motion.anim" not in chatter
        assert "reduceMotion" in chatter
    assert "reduced:" in _read(PHONE / "DecryptFeedback.swift")


def test_the_widget_is_out_of_scope_and_the_app_target_builds_it():
    for path in sorted(WIDGET.glob("*.swift")):
        assert "Motion." not in _read(path), path.name
    project = _read(PROJECT)
    assert project.count("/* Motion.swift in Sources */") == 2  # the file, the phase
    assert "path = Motion.swift;" in project


def test_motion_is_nothing_under_reduce_motion(tmp_path):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    harness = (
        "import SwiftUI\n" + _shared(PANEL_MOTION) + "\n"
        "print(Motion.animation(.easeOut(duration: 0.2), reduced: true) == nil)\n"
        "print(Motion.animation(.easeOut(duration: 0.2), reduced: false) != nil)\n"
        "print(Motion.animate(.snappy(duration: 0.1), reduced: true) { 41 + 1 })\n"
        "print(Motion.animate(.snappy(duration: 0.1), reduced: false) { \"ran\" })\n")
    path = tmp_path / "MotionProbe.swift"
    executable = tmp_path / "MotionProbe"
    path.write_text(harness)
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.split("\n")[:-1] == ["true", "true", "42", "ran"]
