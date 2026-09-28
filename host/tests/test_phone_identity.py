"""The phone app's signing identity is set in one file.

A fork builds the phone app with its own Apple team by editing the two
lines of ios/Config/Identity.xcconfig. Every other place that needs the
bundle id, the App Group, the background task id or the team spells it
through the `DARK_ARMY_BUNDLE_ID` / `DEVELOPMENT_TEAM` build settings. The
author's values are pinned too: `com.robertandrosz.bobphone*` is on the
closed list of identifiers an installed phone depends on.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IOS = ROOT / "ios"
IDENTITY = IOS / "Config" / "Identity.xcconfig"
BASE = IOS / "Config" / "Base.xcconfig"
PBXPROJ = IOS / "BobPhone.xcodeproj" / "project.pbxproj"

AUTHOR_BUNDLE_ID = "com.robertandrosz.bobphone"
AUTHOR_TEAM = "KURWX5TYSZ"


def _settings(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Z_]+)\s*=\s*(.*?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_identity_holds_the_authors_values_and_base_includes_it():
    settings = _settings(IDENTITY.read_text())
    assert settings == {"DARK_ARMY_BUNDLE_ID": AUTHOR_BUNDLE_ID,
                        "DEVELOPMENT_TEAM": AUTHOR_TEAM}
    base = BASE.read_text()
    assert '#include "Identity.xcconfig"' in base
    assert "DEVELOPMENT_TEAM" not in _settings(base)


def test_no_other_file_under_ios_spells_the_identity():
    offenders = []
    for path in IOS.rglob("*"):
        if not path.is_file() or path == IDENTITY or "xcuserdata" in path.parts:
            continue
        if path.suffix in {".png", ".jpg", ".car", ".xcassets"}:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if "robertandrosz" in text or AUTHOR_TEAM in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_every_bundle_id_derives_from_the_one_setting():
    text = PBXPROJ.read_text()
    ids = re.findall(r"PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);", text)
    assert len(ids) == 10
    assert set(ids) == {
        '"$(DARK_ARMY_BUNDLE_ID)"',
        '"$(DARK_ARMY_BUNDLE_ID).widget"',
        '"$(DARK_ARMY_BUNDLE_ID).notification"',
        '"$(DARK_ARMY_BUNDLE_ID).tests"',
        '"$(DARK_ARMY_BUNDLE_ID).widgettests"',
    }
    assert "DEVELOPMENT_TEAM" not in text


def test_the_project_level_configurations_inherit_the_xcconfig():
    # The two unit-test targets name no xcconfig of their own; they resolve
    # DARK_ARMY_BUNDLE_ID through the project-level configurations.
    text = PBXPROJ.read_text()
    for cid, name in (("50", "Debug"), ("51", "Release")):
        head = (f"7B0B0E1A00000000000000{cid} /* {name} */ = {{\n"
                "\t\t\tisa = XCBuildConfiguration;\n"
                "\t\t\tbaseConfigurationReference = ")
        assert head in text, name


def test_the_export_takes_the_archives_team():
    assert "<key>teamID</key>" not in (IOS / "Config" / "ExportOptions.plist").read_text()


def test_the_testflight_check_reads_the_group_from_the_identity_file():
    wf = (ROOT / ".github" / "workflows" / "testflight.yml").read_text()
    assert "robertandrosz" not in wf
    assert wf.count("ios/Config/Identity.xcconfig") >= 2


def test_testflight_ships_from_the_public_repository():
    # TestFlight moved from the private androszr/bob-companion to the public
    # androszr/dark-army on 28 Sep 2026. The key lives in the public repo's
    # `testflight` environment, so the job must name it, and the build number
    # must clear the private repo's last build (95), since the new repo's run
    # counter restarted at 1 and App Store Connect refuses a reused number.
    wf = (ROOT / ".github" / "workflows" / "testflight.yml").read_text()
    assert "if: github.repository == 'androszr/dark-army'" in wf
    assert "androszr/bob-companion'" not in wf
    assert "\n    environment: testflight\n" in wf
    assert "CURRENT_PROJECT_VERSION=$(( ${{ github.run_number }} + 100 ))" in wf
    assert "CURRENT_PROJECT_VERSION=${{ github.run_number }}" not in wf


def test_testflight_signs_with_rendered_entitlements_and_team():
    # The archive is unsigned and the workflow signs it with `codesign`, which
    # does not expand build settings: the raw files would embed the literal
    # `group.$(DARK_ARMY_BUNDLE_ID)`. Every `--entitlements` must name a
    # rendered copy, and the export options must carry the team from the
    # identity file, since the unsigned archive records none.
    wf = (ROOT / ".github" / "workflows" / "testflight.yml").read_text()
    render = wf.split("- name: Render the identity", 1)[1].split("- name:", 1)[0]
    assert "ios/Config/Identity.xcconfig" in render
    assert "plutil -insert teamID" in render
    assert "grep -q '\\$('" in render
    for line in wf.splitlines():
        if "--entitlements" in line and "codesign -d" not in line:
            assert "$RUNNER_TEMP/ent/" in line or '"$ent/' in line, line
    assert "-exportOptionsPlist ios/Config/" not in wf
    assert wf.index("- name: Render the identity") < wf.index("- name: Embed the entitlements")
