"""Source pins for the plan fold on the card window and the phone's card screen.

`PlanSplit` decides where a plan's plain half ends — the template's
`## Technical detail` heading — and both clients fold everything below it
behind one `TECHNICAL DETAIL` row. Neither client can be run here, so these
read the Swift as text: the helper is byte-identical on both sides, the marker
is the template's own heading, both `planSection`s fold through it, and the
fold is view state that no store or snapshot carries.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL_HELPER = ROOT / "panel" / "Sources" / "BobPanel" / "PlanDiagram.swift"
PANEL_VIEW = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardSheet.swift"
PHONE_VIEW = ROOT / "ios" / "BobPhone" / "CardDetailView.swift"
TEMPLATE = ROOT / ".claude" / "skills" / "ship" / "templates" / "plan.md"

MARKER = "## Technical detail"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _helper_block(text: str) -> str:
    """The lines from `enum PlanSplit {` to the first following line that is
    exactly `}` — exactly one such block per file."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if line == "enum PlanSplit {"]
    assert len(starts) == 1, f"expected one `enum PlanSplit {{`, found {len(starts)}"
    start = starts[0]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _member(text: str, head: str) -> str:
    """The body of one member: from `head` to the next line at the same
    indent that opens another member (or the type's closing brace)."""
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if head in line)
    indent = len(lines[start]) - len(lines[start].lstrip())
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        this = len(line) - len(line.lstrip())
        if this < indent or (this == indent and not line.lstrip().startswith(
                (".", "}", "//", "///"))):
            return "\n".join(lines[start:j])
    return "\n".join(lines[start:])


def test_the_split_helper_is_the_same_on_both_sides():
    panel = _helper_block(_read(PANEL_HELPER))
    phone = _helper_block(_read(PHONE_VIEW))
    assert "static func split(" in panel
    assert panel == phone


def test_a_doctored_helper_would_not_compare_equal():
    panel = _helper_block(_read(PANEL_HELPER))
    doctored = panel.replace("== marker", ".hasPrefix(marker)", 1)
    assert doctored != panel
    assert doctored != _helper_block(_read(PHONE_VIEW))


def test_the_marker_is_the_templates_heading():
    for path in (PANEL_HELPER, PHONE_VIEW):
        assert _read(path).count(f'"{MARKER}"') == 1, path
    assert MARKER in _read(TEMPLATE).split("\n")


def test_both_plan_sections_fold():
    for path in (PANEL_VIEW, PHONE_VIEW):
        section = _member(_read(path), "private var planSection")
        assert "PlanSplit.split(" in section, path
        assert "planDetailOpen" in section, path
        assert '"TECHNICAL DETAIL"' in section, path
    phone = _member(_read(PHONE_VIEW), "private var planSection")
    assert '.accessibilityLabel("Technical detail")' in phone
    assert ".accessibilityValue(" in phone


def test_the_fold_is_view_state_only():
    for path in (ROOT / "ios" / "BobPhone" / "CardCache.swift",
                 ROOT / "ios" / "BobPhone" / "Models.swift",
                 ROOT / "panel" / "Sources" / "BobPanel" / "Drafts.swift"):
        assert "planDetailOpen" not in _read(path), path
    daemon = ROOT / "host" / "dark_army_daemon"
    for path in daemon.rglob("*.py"):
        assert "planDetailOpen" not in path.read_text(), path


def test_no_new_line_caps():
    assert _read(PHONE_VIEW).count(".lineLimit(") == 0
