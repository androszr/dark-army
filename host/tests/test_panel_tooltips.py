"""The panel's tooltip allowlist: twenty-four keepers, named files, spoken replacements.

Text pins in `test_inbox_surface.py`'s shape: the panel is another language,
so the only cross-language guard available is to read the Swift off disk.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"

EXPECTED = {
    "BoardCardView.swift": 3,
    "DraftsSheet.swift": 2,
    "BoardProjectControls.swift": 2,
    "InboxView.swift": 2,
    "ProcessTable.swift": 3,
    "ProviderSwitch.swift": 2,
    "RowBars.swift": 3,
    "SettingsRows.swift": 4,
    "SettingsSection.swift": 3,
    "SettingsSidebar.swift": 1,
}

HELP = ".help("


def _read(name):
    return (PANEL / name).read_text()


def _help_counts():
    counts = {}
    for path in sorted(PANEL.glob("*.swift")):
        n = path.read_text().count(HELP)
        if n:
            counts[path.name] = n
    return counts


def test_tooltip_allowlist_is_exactly_the_named_survivors():
    counts = _help_counts()
    # 25 since 3 Oct 2026: the brand bar's Rebuild button names its failure
    # (or that the build is older than the source) in its tooltip.
    assert sum(EXPECTED.values()) == 25
    assert sum(counts.values()) == 25, (
        f"whole-tree {HELP} count is {sum(counts.values())}, not 25: {counts}"
    )
    assert counts == EXPECTED, (
        f"{HELP} sites drifted from the allowlist.\n"
        f"expected {EXPECTED}\n"
        f"got      {counts}"
    )


def test_every_file_outside_the_allowlist_has_zero_tooltips():
    for path in sorted(PANEL.glob("*.swift")):
        if path.name in EXPECTED:
            continue
        n = path.read_text().count(HELP)
        assert n == 0, f"{path.name} has {n} {HELP} sites; it is not on the allowlist"


def test_each_keeper_still_carries_its_text():
    row_bars = _read("RowBars.swift")
    assert row_bars.count("Press twice to confirm.") == 2
    assert "sendHelp(replyVia:" in row_bars

    provider = _read("ProviderSwitch.swift")
    assert "helpText(entry.provider)" in provider
    assert "Leave the assistant unchosen" in provider

    card = _read("BoardCardView.swift")
    assert "toolChipLabel" in card
    assert "names one it no longer offers" in card
    assert "on a terminal Dark Army itself opens" in card

    # The settings window's five moved with its rows on 26 Sep 2026: the row
    # shell, a segment, a models pop-up and a fact (`SettingsRows.swift`),
    # and the kill switch in the sidebar's foot (`SettingsSidebar.swift`).
    settings_rows = _read("SettingsRows.swift")
    assert ".help(tooltip)" in settings_rows
    assert ".help(entry.row.tooltip)" in settings_rows
    assert ".help(row.tooltip)" in settings_rows
    assert ".help(row.tooltip)" in _read("SettingsSidebar.swift")
    assert HELP not in _read("SettingsView.swift")

    settings_section = _read("SettingsSection.swift")
    assert "helpText" in settings_section
    assert "client.context.build" in settings_section

    controls = _read("BoardProjectControls.swift")
    assert "How many agents may work at once in this project" in controls
    assert "Start every planned card in this project" in controls

    process = _read("ProcessTable.swift")
    assert "before sending" in process
    assert ".help(agent.areaLine)" in process
    assert 'agent.areaLine.isEmpty ? "" : ", " + agent.areaLine' in process
    # The CMD cell cuts a long title to two lines; the hover gives it whole
    # (25 Sep 2026).
    assert '.help(lines.top.isEmpty ? lines.title : "\\(lines.top) · \\(lines.title)")' in process

    drafts = _read("DraftsSheet.swift")
    assert "Throw away every draft here, and the files they " in drafts
    assert "Throw this draft away, and the files attached to it." in drafts

    inbox = _read("InboxView.swift")
    assert "Hide every entry here until it changes. " in inbox
    assert "Hide this until it changes. Nothing is answered, moved or closed." in inbox


def test_icon_only_controls_keep_a_spoken_label():
    panel_view = _read("PanelView.swift")
    assert 'accessibilityLabel("Clear the filter")' in panel_view

    settings = _read("SettingsRows.swift")
    assert 'accessibilityLabel("Clear the search")' in settings

    card = _read("BoardCardView.swift")
    assert "accessibilityLabel(CrewBand.caption(" in card
    assert "accessibilityLabel(help)" in card

    history = _read("HistoryView.swift")
    assert '"Token cost"' in history
    assert '"Tokens out"' in history
    assert '"On the cards"' in history
    assert "accessibilityLabel(label)" in history
    assert 'accessibilityLabel("Back to the board")' in history
    assert r'accessibilityLabel("Copy `\(resumeCommand)`")' in history
    assert HELP not in history
