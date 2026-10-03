"""Pins for the phone's Menu tab.

Usage and Comm moved off the tab bar into a Menu grid, four across, beside
History, Scouting, Manual checks, Plans and the bundled Design system (built
by `plans/2026-09-25-phone-history-screen.md`,
`plans/2026-09-25-scout-reports-section.md`,
`plans/2026-09-25-manual-check-folder.md` and
`plans/2026-09-25-phone-plans-library.md`). The old tab names stay
reachable: a draft stamped `comm` or the widget's `bobphone://usage` lands on
the Menu, and the link opens its section.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
APP = PHONE / "BobPhoneApp.swift"
MENU = PHONE / "MenuView.swift"
ROUTER = PHONE / "Router.swift"
OUTBOX = PHONE / "Outbox.swift"
WIDGET = ROOT / "ios" / "BobPhoneWidget" / "WidgetViews.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _enum(text: str, name: str) -> str:
    match = re.search(rf"enum {name}\b.*?\n\}}\n", text, re.S)
    assert match, f"no enum {name}"
    return match.group(0)


def test_the_grid_holds_sections_in_order():
    menu = _enum(_read(MENU), "MenuSection")
    assert "case usage, history, comm, scouting, checks, plans, rebuild, designSystem" in menu
    for title in ('"Usage"', '"History"', '"Comm"', '"Scouting"', '"Manual checks"',
                  '"Plans"', '"Rebuild & restart"', '"Design system"', '"Review"'):
        assert f"return {title}" in menu, title
    # Review is appended after the sections already there, never inserted.
    assert "designSystem, review" in menu


def test_the_grid_is_four_columns_and_two_at_accessibility_sizes():
    view = _read(MENU)
    assert "dynamicTypeSize.isAccessibilitySize ? 2 : 4" in view
    assert "LazyVGrid(columns: columns" in view


def test_every_tile_has_an_icon_and_a_name():
    view = _read(MENU)
    assert "Image(systemName: section.symbol)" in view
    assert "Text(section.title)" in view
    assert ".accessibilityLabel(section.title)" in view


def test_a_tile_pushes_onto_the_tab_stack_with_a_back_button():
    view = _read(MENU)
    code = re.sub(r"//[^\n]*", "", view)
    # Registered on the scroll view, never inside the lazy grid.
    grid_at = code.index("LazyVGrid(")
    dest_at = code.index(".navigationDestination(item: $open)")
    grid_end = code.index(".decryptSurface(\"MenuView\")")
    assert grid_at < grid_end < dest_at
    assert ".toolbar(.visible, for: .navigationBar)" in code


def test_the_unbuilt_sections_say_so():
    view = _read(MENU)
    assert "case .scouting, .checks, .plans, .history, .rebuild: return false" in view
    # Review is built: it lights through its own marker, never `built`'s false.
    assert ".review" not in view.split("case .scouting, .checks, .plans, .history, .rebuild: return false")[0].split("var built: Bool")[1].split("return true")[1]
    assert "MenuNotYet(" in view
    assert '"Not built yet"' in view


def test_the_menu_tab_replaces_usage_and_comm():
    app = _read(APP)
    assert 'Label("Menu", systemImage: "square.grid.3x3")' in app
    assert 'PhoneTabRoot(path: "~/menu", tab: .menu' in app
    assert "MenuView(client: client, open: $menuOpen," in app
    assert "if let section = router.takeSection() { menuOpen = section }" in app


def test_old_names_are_read_through_the_stored_initialiser():
    assert "PhoneTab(stored: d.tab) ?? .needs" in _read(OUTBOX)
    router = _read(ROUTER)
    assert 'PhoneTab(stored: url.host ?? "")' in router
    assert 'pendingSection = MenuSection(rawValue: url.host ?? "")' in router
    assert "PhoneTab(rawValue:" not in router
    # The widget's meter still links by the old word.
    assert 'URL(string: "bobphone://usage")' in _read(WIDGET)


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="needs swiftc")
def test_old_tab_names_land_on_the_menu(tmp_path):
    """Compile the two enums as written and ask them."""
    source = "\n".join([
        "import Foundation",
        _enum(_read(MENU), "MenuSection"),
        _enum(_read(APP), "PhoneTab"),
        """
func check(_ ok: Bool, _ what: String) { if !ok { print(what); exit(1) } }
check(PhoneTab(stored: "usage") == .menu, "usage")
check(PhoneTab(stored: "comm") == .menu, "comm")
check(PhoneTab(stored: "scouting") == .menu, "scouting")
check(PhoneTab(stored: "menu") == .menu, "menu")
check(PhoneTab(stored: "board") == .board, "board")
check(PhoneTab(stored: "needs") == .needs, "needs")
check(PhoneTab(stored: "nonsense") == nil, "nonsense")
check(PhoneTab(stored: "") == nil, "empty")
check(MenuSection(rawValue: "usage") == .usage, "section usage")
check(MenuSection(rawValue: "plans") == .plans, "section plans")
check(MenuSection(rawValue: "history") == .history, "section history")
check(MenuSection(rawValue: "rebuild") == .rebuild, "section rebuild")
check(PhoneTab(stored: "history") == .menu, "history")
check(PhoneTab(stored: "plans") == .menu, "plans")
check(MenuSection(rawValue: "board") == nil, "section board")
check(PhoneTab.allCases.count == 4, "four tabs")
""",
    ])
    path = tmp_path / "main.swift"
    path.write_text(source)
    built = subprocess.run(["swiftc", str(path), "-o", str(tmp_path / "menu")],
                           capture_output=True, text=True, timeout=120)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(tmp_path / "menu")], capture_output=True, text=True,
                         timeout=10)
    assert ran.returncode == 0, ran.stdout + ran.stderr
