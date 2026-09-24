# host/tests/test_phone_board_project_filter.py
"""The phone's Board tab filters by project with Fleet's own strip.

Source pins in the neighbouring `test_phone_*` idiom, plus the two pure enums
(`FleetProjects`, `BoardProjects`) run under `swiftc` rather than read
(`test_comm_rules.py`'s idiom). What is pinned, each being what the next
reader would delete or duplicate:

* one strip, `PhoneProjectStrip`, declared once and drawn by both tabs —
  the Board never grows a second chip view of its own;
* the Board resolves its choice through Fleet's `FleetProjects.resolve`,
  on read and on write-back, with the offline keep answered by whether the
  Mac published a board;
* the Board keeps its **own** stored choice (`board.project`), with Fleet's
  storage kind, so narrowing one tab never narrows the other;
* a filter that hides cards says so in words, per row and for the board.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
FLEET = PHONE / "FleetView.swift"
BOARD = PHONE / "BoardView.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _code(text: str) -> str:
    return re.sub(r"//[^\n]*", "", text)


def _block(text: str, signature: str) -> str:
    """From `signature` to its matching closing brace."""
    start = text.index(signature)
    brace = text.index("{", start)
    depth = 0
    for i, ch in enumerate(text[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"unclosed block: {signature}")


# --- one strip, two tabs --------------------------------------------------------


def test_the_strip_is_declared_once_and_drawn_by_both_tabs():
    declared = [p.name for p in PHONE.glob("*.swift")
                if "struct PhoneProjectStrip" in _read(p)]
    assert declared == ["FleetView.swift"]
    fleet, board = _code(_read(FLEET)), _code(_read(BOARD))
    assert "PhoneProjectStrip(" in _block(fleet, "private var projectStrip")
    assert "PhoneProjectStrip(" in _block(board, "private var projectStrip")
    # The Board draws no chip of its own: the chip view, its count text and
    # its cap live in the shared strip alone.
    for copied in ("private func chip(", 'Text("\\(count)")', "minHeight: 32",
                   "Circle().fill(Color.red)", ".lineLimit("):
        assert copied not in board, copied


def test_the_strip_owns_no_selection():
    strip = _block(_code(_read(FLEET)), "struct PhoneProjectStrip")
    assert "Storage" not in strip, "the strip must not store a choice"
    assert "let active: String" in strip
    assert "let select: (String) -> Void" in strip
    assert "let on = active == name" in strip
    assert "select(name)" in strip
    assert 'chip(title: "ALL", name: "", dot: false, count: total)' in strip


def test_the_board_keeps_its_own_choice_with_fleet_s_storage():
    fleet, board = _code(_read(FLEET)), _code(_read(BOARD))
    assert fleet.count('@SceneStorage("fleet.project") private var project = ""') == 1
    assert board.count('@SceneStorage("board.project") private var project = ""') == 1
    assert '"fleet.project"' not in board


def test_the_board_resolves_through_fleet_s_step():
    board = _code(_read(BOARD))
    active = _block(board, "private var activeProject")
    assert "FleetProjects.resolve(selected: project, in: projects, heard: board.available)" in active
    change = _block(board, ".onChange(of: projects)")
    assert "FleetProjects.resolve(" in change
    assert "heard: board.available" in change
    assert "project = next" in change
    # ALL stays reachable while a stale choice is up — Fleet's condition.
    assert "if !projects.isEmpty || !activeProject.isEmpty {" in board
    names = _block(board, "private var projects")
    assert "board.projects.map(\\.name)" in names
    assert "allCards.map(\\.project)" in names


def test_the_filter_narrows_every_row_before_the_search():
    board = _code(_read(BOARD))
    project_cards = _block(board, "private func projectCards(in id: String)")
    assert "BoardProjects.matches(project: $0.project, active: activeProject)" in project_cards
    visible = _block(board, "private func visibleCards(in id: String)")
    assert visible.split("{", 1)[1].lstrip().startswith("projectCards(in: id)")
    heading = _block(board, "private func rowHeading(for item:")
    assert "searching || !activeProject.isEmpty" in heading


def test_a_filtered_out_list_says_so_in_words():
    board = _code(_read(BOARD))
    body = _block(board, "private func rowBody(for id: String)")
    # Row genuinely empty first, then emptied by the filter, then by search.
    empty = body.index('CommentLine(text: "nothing here")')
    filtered = body.index("BoardProjects.rowLine(project: activeProject)")
    searched = body.index('CommentLine(text: "no cards match")')
    assert empty < filtered < searched
    columns = _block(board, "private var columnsBody")
    assert "BoardProjects.boardLine(project: activeProject)" in columns
    assert "!activeProject.isEmpty && !anyVisible" in columns


# --- the rules, run -------------------------------------------------------------


def _enum(text: str, name: str) -> str:
    return _block(text, f"enum {name} {{")


HARNESS = r'''
import Foundation
var out: [String: Any] = [:]
out["names"] = BoardProjects.names(cards: ["b", "", "a", "b"], open: ["c", "a"])
out["allMatches"] = BoardProjects.matches(project: "", active: "")
out["otherMatches"] = BoardProjects.matches(project: "a", active: "b")
out["sameMatches"] = BoardProjects.matches(project: "a", active: "a")
out["counts"] = BoardProjects.counts(["a", "b", "a"])
out["open"] = BoardProjects.openColumns
out["rowLine"] = BoardProjects.rowLine(project: "dark-army")
out["boardLine"] = BoardProjects.boardLine(project: "dark-army")
out["keptOffline"] = FleetProjects.resolve(selected: "gone", in: [], heard: false)
out["forgotten"] = FleetProjects.resolve(selected: "gone", in: ["a"], heard: true)
out["kept"] = FleetProjects.resolve(selected: "a", in: ["a"], heard: true)
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    tmp = tmp_path_factory.mktemp("board-projects-probe")
    source = tmp / "main.swift"
    source.write_text("\n".join([
        HARNESS.split("var out", 1)[0],
        _enum(_read(FLEET), "FleetProjects"),
        _enum(_read(BOARD), "BoardProjects"),
        "var out" + HARNESS.split("var out", 1)[1],
    ]))
    exe = tmp / "probe"
    built = subprocess.run([swiftc, str(source), "-o", str(exe)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout)


def test_names_merge_cards_and_open_projects_like_fleet(probe):
    assert probe["names"] == ["a", "b", "c"]


def test_all_matches_everything_and_a_name_only_itself(probe):
    assert probe["allMatches"] is True
    assert probe["otherMatches"] is False
    assert probe["sameMatches"] is True


def test_chips_count_open_cards(probe):
    assert probe["counts"] == {"a": 2, "b": 1}
    assert probe["open"] == ["prep", "backlog", "in_progress"]


def test_the_filter_lines_name_the_project(probe):
    assert probe["rowLine"] == "no dark-army cards in this row"
    assert probe["boardLine"] == "no dark-army cards on the board · ALL shows every project"


def test_the_shared_resolution_step_keeps_and_forgets(probe):
    assert probe["keptOffline"] == "gone"
    assert probe["forgotten"] == ""
    assert probe["kept"] == "a"
