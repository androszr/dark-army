# host/tests/test_project_selection_fallback.py
"""A project selection that has gone away must land somewhere real.

Source pins over both clients, `test_phone_reconnect_display.py`'s idiom:
these run in the always-on `host` job, while the phone's `xcodebuild test` is
path-gated on `ios/**` and `swift test` covers the panel alone.

Three things are pinned, and each is what the next reader would delete:

* the phone gained a resolution step (`FleetProjects`) between the stored
  project and the lists, and reads it rather than the stored name;
* that step keeps a choice when it has heard nothing — the `heard` guard is
  the whole defence against a lost signal erasing somebody's project, and it
  asks whether any row landed rather than whether any name did;
* the Mac did not lose the guard it already had (`fallbackTab` still wired to
  `resolvedTab`), and a tab press deselects so it is not mistaken for a prune.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FLEET = ROOT / "ios" / "BobPhone" / "FleetView.swift"
PANEL_VIEW = ROOT / "panel" / "Sources" / "BobPanel" / "PanelView.swift"
PROJECT_TABS = ROOT / "panel" / "Sources" / "BobPanel" / "ProjectTabs.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _swift_body(text: str, signature: str, indent: str) -> str:
    """The slice from a declaration to the closing brace at `indent`."""
    start = text.index(signature)
    end = text.index("\n" + indent + "}", start)
    return text[start:end]


def test_the_phone_declares_the_resolution_step() -> None:
    text = _read(FLEET)
    assert "enum FleetProjects {" in text
    body = _swift_body(text, "enum FleetProjects {", "")
    assert "static func names(" in body
    assert "static func resolve(" in body


def test_the_offline_keep_is_written_as_a_guard() -> None:
    """Having heard nothing is no opinion: a poll that returned nothing, a
    transport switch or the first launch must never rewrite the stored
    project. It is a `heard` argument the caller supplies, deliberately not
    `names.isEmpty` — an empty name list is also a real published state (every
    row carrying an empty `project`), where a stale choice must still be
    forgotten."""
    text = _read(FLEET)
    body = _swift_body(text, "static func resolve(", "    ")
    assert "heard: Bool" in body, "resolve no longer takes the offline keep"
    assert "if !heard { return selected }" in body, "the offline keep is gone"
    assert "names.isEmpty" not in body, (
        "an empty name list is not the same question as having heard nothing"
    )
    # And the caller answers it from evidence that means what it says.
    heard = _swift_body(text, "private var heardSomething:", "    ")
    assert "allRows.isEmpty" in heard


def test_the_phone_reads_the_resolved_project_not_the_stored_one() -> None:
    text = _read(FLEET)
    assert "project == name" not in text, "a chip still lights off the stored name"
    filtered = _swift_body(text, "private func filtered(", "    ")
    assert "activeProject" in filtered


def test_all_stays_reachable_while_a_project_is_selected() -> None:
    """The strip is the only route back to every agent, so it may not be
    hidden by the very state that needs it."""
    text = _read(FLEET)
    assert "if !projects.isEmpty || !activeProject.isEmpty {" in text


def test_the_phone_forgets_a_project_that_has_gone() -> None:
    text = _read(FLEET)
    assert ".onChange(of: projects)" in text
    change = _swift_body(text, ".onChange(of: projects)", "        ")
    assert "FleetProjects.resolve" in change
    assert "project = next" in change


def test_a_tab_press_on_the_mac_is_not_a_prune() -> None:
    body = _swift_body(_read(PANEL_VIEW), "private func selectTab(", "    ")
    assert "deselect()" in body
    # The guard compares the press against the tab actually **drawn**, never
    # the stored one. `selectedTab` is nil until the first press while
    # `resolvedTab` falls back to `tabs.first`, so a guard on `selectedTab`
    # makes the first click on the already-highlighted tab close the detail
    # the person is reading -- a press that visibly does nothing except throw
    # away what they were looking at.
    assert "if resolvedTab != tab" in body
    assert "if selectedTab != tab" not in body


def test_the_mac_still_resolves_a_vanished_tab() -> None:
    tabs = _read(PROJECT_TABS)
    assert "func fallbackTab(" in tabs
    resolved = _swift_body(_read(PANEL_VIEW), "private var resolvedTab", "    ")
    assert "fallbackTab(" in resolved
