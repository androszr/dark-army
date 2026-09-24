# host/tests/test_agent_provider_on_rail.py
"""The Mac panel's rail shows a provider mark; the detail spells the name.

`plans/2026-09-06-agent-provider-on-rail.md`. Display-name mapping is
table-tested in `panel/Tests/BobPanelTests/ProviderMarkTests.swift`; this
file pins the wiring those tests cannot see: every process row draws the
existing `ProviderMark`, the header reserves an untitled slot, the detail
header spells `ProviderMark.displayName` and draws no mark, the mapping
lives on the mark, and there is still one `ProviderMark` type.

Since 2026-09-12 the mark is on **every** list of agents on both clients:
the Mac's pipeline rows read it off the card's own `tool` (a queued card
has no face yet but already names its assistant), and the phone's process
row, pipeline row and agent detail draw `PhoneProviderMark` — the phone's
one mark type, whose empty provider is Claude, the wire default.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
PROCESS = PANEL / "ProcessTable.swift"
DETAIL = PANEL / "AgentDetailPane.swift"
ROW_BARS = PANEL / "RowBars.swift"
SWITCH = PANEL / "ProviderSwitch.swift"
PHONE = ROOT / "ios" / "BobPhone" / "ProcessTable.swift"
PHONE_PIPELINE = ROOT / "ios" / "BobPhone" / "PipelineView.swift"
PHONE_DETAIL = ROOT / "ios" / "BobPhone" / "AgentDetailView.swift"
PHONE_MARK = ROOT / "ios" / "BobPhone" / "UsageView.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


def test_every_process_row_draws_the_existing_mark():
    row = _block(_read(PROCESS), "struct ProcessRow")
    assert row.count("ProviderMark(") == 1
    assert "provider: agent.provider" in row
    assert "providerMarkSize" in row
    assert ".accessibilityHidden(true)" in row
    assert "ProviderMark.displayName" in row
    assert "Text(ProviderMark." not in row
    assert "switch agent.provider" not in row
    assert "switch provider" not in row


def test_the_header_reserves_the_slot_and_names_no_column():
    header = _block(_read(PROCESS), "struct ProcessTableHeader")
    assert "ProcessRow.providerMarkSize" in header
    assert 'Text("AI")' not in header
    assert 'Text("PROV")' not in header
    assert 'Text("PROVIDER")' not in header
    assert 'Text("NAME").frame(width: 56' in header


def test_the_detail_header_spells_the_name_and_draws_no_mark():
    header = _block(_read(DETAIL), "struct AgentDetailHeader")
    assert "ProviderMark.displayName" in header
    assert "ProviderMark(" not in header
    assert "switch agent.provider" not in header
    assert "switch provider" not in header
    line = _block(header, "HStack(spacing: 6)")
    assert "ProviderMark.displayName" in line
    assert "category.rawValue" in line


def test_the_mapping_lives_on_the_mark():
    bars = _read(ROW_BARS)
    assert bars.count("static func displayName") == 1
    switch = _read(SWITCH)
    assert "ProviderMark.displayName" in switch
    assert 'case "grok": return "Grok"' not in switch


def test_the_phone_process_row_draws_the_mark_in_both_layouts():
    """Columns and the stacked accessibility layout both carry it, and the
    row's one spoken sentence names the provider in words."""
    row = _block(_read(PHONE), "struct PhoneProcessRow")
    assert row.count("PhoneProviderMark(provider: agent.provider") == 2
    spoken = _block(row, "private var spoken: String")
    assert "PhoneProviderMark.label(for: agent.provider)" in spoken


def test_the_phone_pipeline_row_wears_the_cards_assistant():
    view = _read(PHONE_PIPELINE)
    row = _block(view, "private func row(_ card: BoardCard")
    assert "PhoneProviderMark(provider: card.tool" in row
    # Not folded into one sentence, so the mark stays in the reader's path
    # with its own label.
    mark_line = next(l for l in row.splitlines()
                     if "PhoneProviderMark(provider: card.tool" in l)
    assert "accessibilityHidden" not in mark_line


def test_the_phone_detail_spells_the_name_beside_the_mark():
    detail = _read(PHONE_DETAIL)
    identity = _block(detail, "private var identity: some View")
    assert "PhoneProviderMark(provider: agent.provider" in identity
    assert "PhoneProviderMark.label(for: agent.provider)" in identity


def test_the_phone_mark_reads_empty_as_claude():
    """An `Agent.provider` arrives `""` for Claude's own rows; a mark that
    drew nothing there would show Grok and Codex and leave Claude blank."""
    mark = _block(_read(PHONE_MARK), "struct PhoneProviderMark")
    assert 'provider.isEmpty ? "claude" : provider' in mark
    assert "switch brand" in mark
    assert 'case "claude", "grok", "codex":' in mark


def test_the_phone_has_one_mark_type():
    phone = ROOT / "ios" / "BobPhone"
    count = sum(p.read_text().count("struct PhoneProviderMark")
                for p in phone.rglob("*.swift"))
    assert count == 1


def test_no_second_mark_type():
    count = sum(p.read_text().count("struct ProviderMark")
                for p in PANEL.rglob("*.swift"))
    assert count == 1
