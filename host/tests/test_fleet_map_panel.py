"""Source pins for the fleet activity map in the Mac panel (the shape of
`test_process_row_chips.py`): the map's clock stops when it should, a press on
a portrait is a press on the row, the choice is stored in the panel's own
file, and the hook handler knows nothing of the map."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"


def _read(name: str) -> str:
    return (PANEL / name).read_text()


def _code(name: str) -> str:
    """The file without `//` comments, so a comment naming a banned word
    does not trip the pin."""
    return re.sub(r"//.*", "", _read(name))


def test_the_map_files_carry_no_geometry_reader_progress_view_or_bare_animation():
    for name in ("FleetMapView.swift", "FleetMap.swift", "FleetMapModels.swift"):
        code = _code(name)
        assert "GeometryReader" not in code, name
        assert "ProgressView" not in code, name
        assert "withAnimation(" not in code, name
        assert ".animation(" not in code, name


def test_the_only_clock_is_a_timeline_paused_by_visibility_and_reduce_motion():
    code = _code("FleetMapView.swift")
    assert len(re.findall(r"TimelineView\(", code)) == 1
    assert "TimelineView(AnimationTimelineSchedule(minimumInterval:" in code
    assert "paused: paused)" in code
    paused = re.search(r"let paused = ([^\n]+)", code)
    assert paused
    assert "visible" in paused.group(1) and "reduceMotion" in paused.group(1)


def test_a_portrait_press_and_a_row_press_share_one_handler():
    view = _read("PanelView.swift")
    assert view.count("tapRow(") >= 3
    assert "func tapRow(_ row: SectionRow)" in view
    assert "onSelect: { tapRow(row) }" in view
    assert "onSelect: { tapRow($0) }" in view
    assert "RailLayout.selectionAfterTap" in view[view.index("func tapRow"):][:900]


def test_the_ring_reads_the_rows_own_ctx_function():
    assert "ProcessRow.ctxColor(agent.metrics)" in _read("FleetMapView.swift")
    table = _read("ProcessTable.swift")
    assert "static func ctxColor(_ metrics: Metrics) -> Color" in table
    assert "private var ctxColor: Color { Self.ctxColor(agent.metrics) }" in table


def test_the_choice_is_stored_in_the_panels_own_position_file():
    placement = _read("Placement.swift")
    assert placement.count("fleet_rail_view") >= 2
    assert "func fleetRailView()" in placement
    assert "func saveFleetRailView(" in placement


def test_the_section_is_decoded_and_carried_like_the_others():
    models = _read("Models.swift")
    assert "case fleetActivity" in models
    assert 'fleetActivity = "fleet_activity"' in models
    assert "carried.insert(.fleetActivity)" in models
    assert "case .fleetActivity" in _read("DaemonClient.swift")


def test_the_map_and_the_feed_are_wired_in_the_rail():
    view = _read("PanelView.swift")
    assert "FleetRailSwitch(" in view
    assert "railView == .map" in view
    assert "FleetMapPane(" in view
    assert "client.snapshot.fleetActivity" in view


def test_the_hook_handler_knows_nothing_of_the_map():
    hooks = ROOT / "host" / "dark_army_menubar" / "hooks.py"
    assert "fleet" not in hooks.read_text()


def test_the_map_neither_rebuilds_its_views_nor_traps_on_a_duplicate_key():
    code = _code("FleetMapView.swift")
    assert ".id(settle)" not in code
    assert "uniqueKeysWithValues" not in code
    assert "uniquingKeysWith" in code
