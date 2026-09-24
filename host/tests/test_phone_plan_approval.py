"""Source pins for the phone's APPROVAL section on the card screen.

Approval got a section of its own under the plan: a rule, an `APPROVAL`
heading, one status sentence — not yet approved / approved on a date / plan
changed since approval — and, where a press is still wanted, a full-width
button at touch measure. Nothing in this repo builds or runs the iPhone app
(`swift build` covers `panel/` alone), so these read the Swift as text: the
section sits after the plan's fold, the three sentences are exact literals,
every ink is a `Theme` token, the section is gated on the Mac's version
marker, the button keeps its 44pt floor, the heading is a landmark, and the
old `approveRow` is gone.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE_DIR = ROOT / "ios" / "BobPhone"
PHONE_VIEW = PHONE_DIR / "CardDetailView.swift"

SYSTEM_COLOURS = (".orange", ".secondary", ".primary", ".red", ".green",
                  ".gray", ".white", ".black", ".blue", ".yellow", "Color(")
TOKENS = ("Theme.amber", "Theme.phosphor", "Theme.dim", "Theme.faint",
          "Theme.hair")
MOTION_OR_MEASURE = (".animation(", "withAnimation", ".rotationEffect",
                     "GeometryReader", "UIScreen", ".lineLimit(",
                     ".dynamicTypeSize(")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


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


def _enum_block(text: str) -> str:
    """The lines from `private enum PlanApprovalStatus {` to the first
    following line that is exactly `}` — exactly one such block."""
    lines = text.split("\n")
    head = "private enum PlanApprovalStatus {"
    starts = [i for i, line in enumerate(lines) if line == head]
    assert len(starts) == 1, f"expected one `{head}`, found {len(starts)}"
    start = starts[0]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start:end + 1])


def _section(text: str) -> str:
    return _member(text, "private func approvalSection")


def _no_system_colour(block: str) -> None:
    for needle in SYSTEM_COLOURS:
        assert needle not in block, f"system colour {needle!r} in the section"


def test_the_section_sits_after_the_fold():
    src = _read(PHONE_VIEW)
    plan = _member(src, "private var planSection")
    assert "approvalSection(plan)" in plan
    assert plan.index('"TECHNICAL DETAIL"') < plan.index("approvalSection(plan)")
    assert '"APPROVAL"' in _section(src)


def test_the_three_sentences_are_literals():
    src = _read(PHONE_VIEW)
    for words in ('"not yet approved"', '"plan changed since approval"',
                  '"approved on "'):
        assert src.count(words) == 1, words


def test_no_system_colour_in_the_section():
    src = _read(PHONE_VIEW)
    section, enum = _section(src), _enum_block(src)
    _no_system_colour(section)
    _no_system_colour(enum)
    both = section + enum
    for token in TOKENS:
        assert token in both, token


def test_a_doctored_section_would_fail():
    import pytest
    enum = _enum_block(_read(PHONE_VIEW))
    assert "Theme.amber" in enum
    with pytest.raises(AssertionError):
        _no_system_colour(enum.replace("Theme.amber", ".orange"))


def test_the_section_is_gated_on_the_version_marker():
    section = _section(_read(PHONE_VIEW))
    assert "board.planApprovalSupported" in section
    assert "!plan.digest.isEmpty" in section


def test_the_status_rule_is_the_panels():
    enum = _enum_block(_read(PHONE_VIEW))
    assert "approved.isEmpty" in enum
    assert "approved == digest" in enum
    # `0` is "never stamped", never the first second of 1970.
    assert "approvedAt > 0" in enum


def test_the_button_meets_the_touch_target():
    src = _read(PHONE_VIEW)
    assert "touchFloor: CGFloat = 44" in _enum_block(src)
    section = _section(src)
    assert "minHeight:" in section
    assert "maxWidth: .infinity" in section
    assert "PlanApprovalStatus.touchFloor" in section


def test_the_heading_is_a_landmark_and_the_words_are_spoken():
    section = _section(_read(PHONE_VIEW))
    assert ".accessibilityAddTraits(.isHeader)" in section
    assert section.count(".accessibilityHidden") == 0
    assert ".accessibilityLabel(" in section
    assert ".accessibilityHint(" in section


def test_nothing_moves_or_measures():
    src = _read(PHONE_VIEW)
    for block in (_section(src), _enum_block(src)):
        for needle in MOTION_OR_MEASURE:
            assert needle not in block, needle


def test_the_old_row_is_gone():
    assert _read(PHONE_VIEW).count("approveRow") == 0


def test_no_new_swift_file_named_for_the_feature():
    assert not list(PHONE_DIR.glob("PlanApproval*.swift"))
