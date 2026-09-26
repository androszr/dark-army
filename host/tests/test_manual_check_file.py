# host/tests/test_manual_check_file.py
"""The manual check's one parser, checker, writer and scanner
(`dark_army_daemon.manual_check`), and its two byte copies.

`test_scout_report.py`'s shape: the module is pure standard library, parses
under Python 3.9 (the checker runs under a project's own `python3`), carries
no double-brace placeholder (the pack renderer would substitute it), and is
byte-identical in the three places it lives.
"""

from __future__ import annotations

import ast
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dark_army_daemon import manual_check as mc

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "host" / "dark_army_daemon" / "manual_check.py"
COPIES = (
    ROOT / ".claude" / "skills" / "ship" / "manual_check.py",
    ROOT / "host" / "dark_army_menubar" / "agent_pack" / "template" / ".claude"
    / "skills" / "ship" / "manual_check.py",
)

GOOD = """# The strip fits

- **Card:** Fit the strip
- **Project:** dark-army
- **Check:** the strip fits beside three sessions
- **Created:** 2026-09-25T14:32:00+02:00
- **Status:** open
- **Outcome:** none
- **Checked at:** none

## Steps

1. Open the menu bar with three sessions running.
2. Expect the strip to still fit inside its budget.

## Why not automated

It is a real menu bar on a real screen.
"""


def _write(folder: Path, text: str, name: str = "check.md") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


# --- parse and check ---------------------------------------------------------


def test_a_complete_check_parses_to_its_header():
    assert mc.parse_header(GOOD) == {
        "card": "Fit the strip",
        "project": "dark-army",
        "check": "the strip fits beside three sessions",
        "created": "2026-09-25T14:32:00+02:00",
        "status": "open",
    }
    assert mc.title(GOOD) == "The strip fits"
    assert mc.check(GOOD) == []
    assert mc.steps_text(GOOD).startswith("1. Open the menu bar")


@pytest.mark.parametrize("key", mc.HEADER_KEYS)
def test_each_missing_key_is_named_exactly(key):
    text = "\n".join(line for line in GOOD.splitlines()
                     if not line.startswith("- **%s:**" % key)) + "\n"
    assert mc.check(text) == [mc.MISSING % key]


def test_a_bad_status_and_a_bad_created_are_named():
    text = GOOD.replace("- **Status:** open", "- **Status:** done")
    assert mc.check(text) == [mc.BAD_STATUS]
    text = GOOD.replace("2026-09-25T14:32:00+02:00", "yesterday")
    assert mc.check(text) == [mc.BAD_CREATED]
    # A trailing Z is UTC, which Python before 3.11 refuses on its own.
    text = GOOD.replace("2026-09-25T14:32:00+02:00", "2026-09-25T12:32:00Z")
    assert mc.check(text) == []


def test_headings_dropped_or_shuffled_are_named():
    dropped = GOOD.replace("## Why not automated", "## Why")
    assert mc.HEADING_MISSING % "Why not automated" in mc.check(dropped)
    head, _, rest = GOOD.partition("## Steps")
    steps, _, reason = rest.partition("## Why not automated")
    shuffled = head + "## Why not automated" + reason + "\n## Steps" + steps
    assert mc.HEADING_OUT_OF_ORDER % "Why not automated" in mc.check(shuffled)


def test_prose_steps_and_an_empty_reason_are_named():
    prose = GOOD.replace("1. Open the menu bar with three sessions running.\n"
                         "2. Expect the strip to still fit inside its budget.",
                         "Look at the menu bar.")
    assert mc.check(prose) == [mc.NO_STEPS]
    empty = GOOD.replace("It is a real menu bar on a real screen.\n", "")
    assert mc.check(empty) == [mc.NO_REASON]


def test_brief_folds_the_missing_keys():
    problems = mc.check("# T\n\n- **Card:** c\n\n## Steps\n")
    line = mc.brief(problems)
    assert line.startswith("missing Project, Check, Created, Status")


# --- write_outcome -------------------------------------------------------------


def test_recording_rewrites_exactly_three_lines(tmp_path):
    path = _write(tmp_path / "manual-check" / "2026-09-25-strip", GOOD)
    os.chmod(path, 0o640)
    before = path.read_text().splitlines()
    assert mc.write_outcome(path, "passed", "looked\nfine") == ""
    after = path.read_text().splitlines()
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert [before[i] for i in changed] == [
        "- **Status:** open", "- **Outcome:** none", "- **Checked at:** none"]
    assert after[changed[0]] == "- **Status:** passed"
    assert after[changed[1]] == "- **Outcome:** looked fine"
    assert after[changed[2]].startswith("- **Checked at:** 20")
    assert mc.check(path.read_text()) == []
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    # No temporary file is left beside the check.
    assert sorted(os.listdir(path.parent)) == ["check.md"]


def test_a_second_recording_is_refused_and_writes_nothing(tmp_path):
    path = _write(tmp_path / "c", GOOD)
    assert mc.write_outcome(path, "failed", "") == ""
    text = path.read_text()
    assert "- **Outcome:** none" in text
    assert "- **Status:** failed" in text
    assert mc.write_outcome(path, "passed", "again") == mc.RECORDED
    assert path.read_text() == text


def test_recording_refuses_a_bad_status_a_malformed_file_and_a_link(tmp_path):
    path = _write(tmp_path / "c", GOOD)
    assert mc.write_outcome(path, "done", "") == mc.BAD_OUTCOME
    assert path.read_text() == GOOD
    broken = _write(tmp_path / "d", GOOD.replace("## Steps", "## Stops"))
    assert mc.write_outcome(broken, "passed", "") == mc.MALFORMED
    link = tmp_path / "link.md"
    link.symlink_to(path)
    assert mc.write_outcome(link, "passed", "") == mc.UNREADABLE
    assert path.read_text() == GOOD


def test_a_status_line_in_the_steps_is_never_rewritten(tmp_path):
    text = GOOD.replace("2. Expect", "- **Status:** open\n2. Expect")
    path = _write(tmp_path / "c", text)
    assert mc.write_outcome(path, "passed", "") == ""
    body = path.read_text().split("## Steps", 1)[1]
    assert "- **Status:** open" in body


# --- scan, sort and match ------------------------------------------------------


def test_scan_lists_the_plain_and_the_malformed_and_skips_a_symlink(tmp_path):
    root = tmp_path / "proj"
    base = root / "manual-check"
    _write(base / "2026-09-24-old", GOOD.replace(
        "2026-09-25T14:32:00+02:00", "2026-09-24T09:00:00+02:00"))
    _write(base / "2026-09-25-broken", GOOD.replace("- **Status:** open\n", ""))
    elsewhere = _write(tmp_path / "elsewhere" / "x", GOOD)
    (base / "2026-09-26-linked").symlink_to(elsewhere.parent)
    entries = mc.scan(root)
    assert [e["folder"] for e in entries] == [
        "2026-09-25-broken", "2026-09-24-old"]
    broken, old = entries
    assert broken["malformed"] is True
    assert broken["problem"] == mc.MISSING % "Status"
    assert old["malformed"] is False and old["problem"] == ""
    assert old["title"] == "The strip fits"
    assert old["status"] == "open" and old["outcome"] == ""
    assert old["steps_preview"].startswith("1. Open the menu bar")
    assert entries == sorted(entries, key=mc.sort_key, reverse=True)
    assert mc.scan(tmp_path / "nothing-here") == []
    assert len(mc.scan(root, 1)) == 1


def test_matches_is_casefolded_over_five_fields():
    entry = {"title": "The Strip", "check": "fits", "steps_preview": "Open X",
             "outcome": "Looked FINE", "card": "Fit the strip card"}
    for needle in ("strip", "FITS", "open x", "fine", "strip card"):
        assert mc.matches(entry, needle), needle
    assert not mc.matches(entry, "zzz")
    assert mc.matches(entry, "")


# --- main ----------------------------------------------------------------------


def test_the_checker_prints_ok_or_its_problems(tmp_path):
    good = _write(tmp_path / "a", GOOD)
    run = subprocess.run([sys.executable, str(COPIES[0]), str(good)],
                         capture_output=True, text=True)
    assert run.returncode == 0 and run.stdout.strip() == "ok"
    bad = _write(tmp_path / "b", "# T\n\n- **Card:** c\n\n## Steps\n")
    run = subprocess.run([sys.executable, str(COPIES[0]), str(bad)],
                         capture_output=True, text=True)
    assert run.returncode == 1
    assert "missing Status" in run.stdout


# --- the three copies and the 3.9 floor ---------------------------------------


def test_the_three_copies_are_byte_identical():
    source = MODULE.read_bytes()
    for copy in COPIES:
        assert copy.read_bytes() == source, copy


def test_the_copies_are_world_readable_and_not_executable():
    for copy in COPIES:
        assert stat.S_IMODE(copy.stat().st_mode) == 0o644, copy


def test_the_module_parses_under_39_and_imports_only_the_standard_library():
    source = MODULE.read_text()
    tree = ast.parse(source, feature_version=(3, 9))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import"
            names.add((node.module or "").split(".")[0])
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    assert names <= stdlib, names - stdlib
    assert not any(n.startswith("dark_army") for n in names)


def test_the_module_carries_no_pack_placeholder():
    assert "{{" not in MODULE.read_text()


# --- the audit's three (25 Sep 2026) -------------------------------------------


@pytest.mark.parametrize("note", ["<ok>", "none", "N/A", "-", "—"])
def test_a_note_the_parser_would_drop_is_quoted_and_survives(tmp_path, note):
    path = _write(tmp_path / "c", GOOD)
    assert mc.write_outcome(path, "passed", note) == ""
    text = path.read_text()
    assert mc.check(text) == []
    assert f'- **Outcome:** "{note}"' in text
    assert mc.parse_header(text)["outcome"] == f'"{note}"'
    assert mc.outcome_text("looked fine") == "looked fine"
    assert mc.outcome_text("  ") == "none"


@pytest.mark.parametrize("created", [
    "2026-09-25", "2026-09-25T14:32:00", "2026-09-25T14:32+02:00",
    "2026-09-25 14:32:00+02:00"])
def test_created_needs_a_full_date_time_with_an_offset(created):
    text = GOOD.replace("2026-09-25T14:32:00+02:00", created)
    assert mc.check(text) == [mc.BAD_CREATED], created


def test_created_accepts_z_and_a_fraction():
    for created in ("2026-09-25T12:32:00Z", "2026-09-25T12:32:00.5+00:00"):
        assert mc.check(GOOD.replace("2026-09-25T14:32:00+02:00", created)) == []


def test_a_symlinked_manual_check_folder_lists_nothing(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "2026-09-25-x", GOOD)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "manual-check").symlink_to(elsewhere)
    assert mc.scan(root) == []
