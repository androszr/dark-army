"""Pin both real clients to one pure evidence/navigation contract."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def test_pure_semantics_and_success_fixtures_are_byte_pinned():
    panel = ROOT / "panel/Sources/BobPanel/Collaboration.swift"
    phone = ROOT / "ios/BobPhone/Collaboration.swift"
    assert panel.read_bytes() == phone.read_bytes()
    panel_tests = (ROOT / "panel/Tests/BobPanelTests/CollaborationTests.swift").read_text()
    phone_tests = (ROOT / "ios/BobPhoneTests/CollaborationTests.swift").read_text()
    fixture = lambda source: source.split('static let fixture = """', 1)[1].split('"""', 1)[0]
    assert fixture(panel_tests) == fixture(phone_tests)


def test_map_has_navigation_only_and_no_refresh_schedule_or_injection():
    for directory in ("panel/Sources/BobPanel", "ios/BobPhone"):
        source = (ROOT / directory / "CollaborationView.swift").read_text()
        for forbidden in ("client.post(", "client.reply(", "Timer(", "TimelineView(", "URLSession", "send_text", "openURL", 'Button("Send'):
            assert forbidden not in source
        assert "CollaborationRoute.agent" in source and "CollaborationRoute.card" in source
        assert "current:" in source
        assert "Text(verbatim:" in source
    phone = (ROOT / "ios/BobPhone/CollaborationView.swift").read_text()
    assert "sheets.show(.agent" in phone and "sheets.show(.card" in phone
    assert ".lineLimit(" not in phone


def test_new_phone_sources_belong_to_actual_build_targets():
    project = (ROOT / "ios/BobPhone.xcodeproj/project.pbxproj").read_text()
    for name in ("Collaboration.swift", "CollaborationView.swift", "CollaborationTests.swift"):
        match = re.search(r'([A-F0-9]+) /\* ' + re.escape(name) + r' in Sources \*/ = \{isa = PBXBuildFile;', project)
        assert match, name
        assert project.count(match.group(1)) >= 2, name
