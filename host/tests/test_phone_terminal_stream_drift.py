# host/tests/test_phone_terminal_stream_drift.py
"""The phone's `TerminalStream.swift` carries the panel's frame codec and
head parser byte for byte, and SwiftTerm is wired into exactly the one
target that draws a terminal.

`test_phone_theme_drift.py`'s rule: the phone is an Xcode target and the
panel a SwiftPM module, so the shared code is a hand copy below a marker
line, and this test is what was bought instead of drift. The region runs
from the marker line through the closing brace of `TerminalStreamHead`;
only the header above the marker may differ (each side names the other),
and `SealedTerminalStream` beneath is the phone's own.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PANEL_STREAM = ROOT / "panel" / "Sources" / "BobPanel" / "TerminalStream.swift"
PHONE_STREAM = ROOT / "ios" / "BobPhone" / "TerminalStream.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
PHONE = ROOT / "ios" / "BobPhone"
WIDGET = ROOT / "ios" / "BobPhoneWidget"
TESTS = ROOT / "ios" / "BobPhoneTests"
WIDGET_TESTS = ROOT / "ios" / "BobPhoneWidgetTests"

MARKER = "// MARK: - shared with the panel (byte-pinned)"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _shared(path: Path) -> str:
    """The pinned region: the marker line through `TerminalStreamHead`'s
    closing brace, the first bare `}` after its opening line."""
    lines = _read(path).splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one marker line in {path}, got {len(starts)}"
    heads = [i for i, line in enumerate(lines)
             if line.startswith("enum TerminalStreamHead {")]
    assert len(heads) == 1, f"expected one TerminalStreamHead in {path}"
    end = next(i for i in range(heads[0], len(lines)) if lines[i].rstrip("\n") == "}")
    return "".join(lines[starts[0]:end + 1])


def test_the_frame_codec_and_the_head_parser_are_the_same_on_both_sides():
    panel = _shared(PANEL_STREAM)
    phone = _shared(PHONE_STREAM)
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 2500, f"parsed too little from the {name} stream"
        assert "enum TerminalFrameCodec {" in region, name
        assert "struct Parser" in region, name
        assert "static func parse(_ buffer: Data) -> Parsed?" in region, name
    assert panel == phone, (
        "the phone's TerminalStream.swift has drifted from the panel's below "
        "the marker line. Mirror the edit onto the other side in this same "
        "commit — never re-baseline one side and never loosen this comparison.")


def test_a_doctored_codec_would_not_compare_equal():
    panel = _shared(PANEL_STREAM)
    assert "static let headSize = 5" in panel
    doctored = panel.replace("static let headSize = 5", "static let headSize = 6", 1)
    assert doctored != panel
    assert doctored != _shared(PHONE_STREAM)


def test_the_phone_side_is_the_panel_side_plus_the_sealed_stream():
    phone = _read(PHONE_STREAM)
    assert "final class SealedTerminalStream" in phone
    assert "final class TerminalStreamConnection" not in phone
    panel = _read(PANEL_STREAM)
    assert "SealedTerminalStream" not in panel
    assert "final class TerminalStreamConnection" in panel


def test_swiftterm_is_one_remote_package_on_the_app_target_alone():
    proj = _read(PBXPROJ)
    assert proj.count('XCRemoteSwiftPackageReference "SwiftTerm"') == 1
    assert 'repositoryURL = "https://github.com/migueldeicaza/SwiftTerm.git"' in proj
    assert "kind = upToNextMajorVersion;" in proj
    assert "minimumVersion = 1.20.0;" in proj
    assert proj.count("isa = XCSwiftPackageProductDependency;") == 1
    assert proj.count("productName = SwiftTerm;") == 1
    # The product rides the BobPhone target's own dependency list, and the
    # widget and the test bundle carry none.
    app = proj[proj.index("/* BobPhone */ = {\n\t\t\tisa = PBXNativeTarget;"):
               proj.index("/* BobPhoneWidget */ = {\n\t\t\tisa = PBXNativeTarget;")]
    assert "packageProductDependencies = (" in app
    assert "/* SwiftTerm */" in app
    widget = proj[proj.index("/* BobPhoneWidget */ = {\n\t\t\tisa = PBXNativeTarget;"):
                  proj.index("/* BobPhoneTests */ = {")]
    assert "packageProductDependencies" not in widget
    tests = proj[proj.index("/* BobPhoneTests */ = {"):
                 proj.index("/* End PBXNativeTarget section */")]
    assert "packageProductDependencies" not in tests
    assert proj.count("packageProductDependencies = (") == 1
    assert "packageReferences = (" in proj
    # The panel pins the same version.
    manifest = _read(ROOT / "panel" / "Package.swift")
    assert 'from: "1.20.0"' in manifest


def test_import_swiftterm_appears_in_exactly_one_phone_file():
    importers = []
    for folder in (PHONE, WIDGET, TESTS, WIDGET_TESTS):
        for path in sorted(folder.glob("*.swift")):
            if "import SwiftTerm" in path.read_text():
                importers.append(path.relative_to(ROOT).as_posix())
    assert importers == ["ios/BobPhone/TerminalPane.swift"], importers


def test_nothing_on_the_phone_spins():
    """`docs/agent-chatter.md`'s rule, restated for the new file: no
    `ProgressView` anywhere under `ios/BobPhone`."""
    for path in sorted(PHONE.glob("*.swift")):
        assert "ProgressView" not in path.read_text(), path.name
