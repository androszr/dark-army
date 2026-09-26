# host/tests/test_phone_mac_power.py
"""The phone half of the host Mac's battery line on the Fleet tab.

`MacPower.line` (in `ios/BobPhone/FleetView.swift`, for `FleetProjects`'
reason) is run under `swiftc` rather than read — `test_comm_rules.py`'s and
`test_cast.py`'s idiom — over every shape the Mac's `power` section can take.
The rest is pinned by text: `PowerSection` decodes tolerantly, `Snapshot`
reads it with a default, and the Fleet row is a label placed between the
refresh notice and **Catch up across projects**.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import power_source

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
FLEET = PHONE / "FleetView.swift"
MODELS = PHONE / "Models.swift"


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
                return text[i:j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


def _code(text: str) -> str:
    """`text` with `//` comments removed, so a pin reads code, not prose."""
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


# --- the rule, run -------------------------------------------------------------


HARNESS = r'''
struct Case: Decodable {
    let available: Bool
    let present: Bool
    let percent: Int?
    let source: String
    let charge: String
}
let cases = try! JSONDecoder().decode([Case].self, from: FileHandle.standardInput.readDataToEndOfFile())
let lines: [Any] = cases.map {
    MacPower.line(available: $0.available, present: $0.present, percent: $0.percent,
                  source: $0.source, charge: $0.charge) ?? NSNull()
}
let data = try! JSONSerialization.data(withJSONObject: lines)
FileHandle.standardOutput.write(data)
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    tmp = tmp_path_factory.mktemp("mac-power-probe")
    path = tmp / "MacPowerProbe.swift"
    rule = _block(_read(FLEET), "enum MacPower {")
    path.write_text("import Foundation\n\n" + rule + "\n" + HARNESS)
    executable = tmp / "MacPowerProbe"
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr

    def run(cases: list[tuple]) -> list:
        payload = [dict(zip(("available", "present", "percent", "source",
                             "charge"), c)) for c in cases]
        ran = subprocess.run([str(executable)], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30)
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)

    return run


CASES = [
    ((True, True, 73, "ac", "charging"), "mac battery 73% · charging"),
    ((True, True, 41, "battery", "discharging"), "mac battery 41% · on battery"),
    ((True, True, 100, "ac", "charged"), "mac battery 100% · charged"),
    ((True, True, 80, "ac", "not_charging"),
     "mac battery 80% · on power, not charging"),
    ((True, True, 99, "ac", "finishing"), "mac battery 99% · finishing charge"),
    ((True, True, 50, "ac", ""), "mac battery 50% · on power"),
    ((True, True, 50, "battery", ""), "mac battery 50% · on battery"),
    ((True, True, 50, "", ""), "mac battery 50%"),
    ((True, True, 50, "", "something new"), "mac battery 50%"),
    ((False, True, 73, "ac", "charging"), None),
    ((True, False, 73, "ac", "charging"), None),
    ((True, False, None, "ac", ""), None),
    ((True, True, None, "ac", "charging"), None),
]


def test_the_words_over_every_shape(probe):
    got = probe([c for c, _ in CASES])
    assert got == [want for _, want in CASES]


def test_every_charge_word_the_mac_can_send_has_words(probe):
    """A `charge` the daemon's parser can answer never falls through to the
    bare percent: `power_source.CHARGES` and the switch agree."""
    cases = [(True, True, 60, "", charge) for charge in power_source.CHARGES]
    for line in probe(cases):
        assert line.startswith("mac battery 60% · "), line


def test_every_source_word_the_mac_can_send_has_words(probe):
    cases = [(True, True, 60, source, "") for source in power_source.SOURCES]
    for line in probe(cases):
        assert line.startswith("mac battery 60% · "), line


# --- the model -----------------------------------------------------------------


def test_the_power_section_decodes_every_key_tolerantly():
    section = _block(_read(MODELS), "struct PowerSection: Decodable")
    decode = _code(_block(section, "init(from decoder: Decoder)"))
    for key in ("available", "present", "percent", "source", "charge"):
        assert (f"c.value(.{key}," in decode) or (f"c.maybe(.{key})" in decode), key
    assert "c.decode(" not in decode
    assert "decodeIfPresent" not in decode
    assert "var available = false" in section
    assert "var percent: Int? = nil" in section


def test_the_snapshot_reads_power_with_a_default():
    snapshot = _block(_read(MODELS), "struct Snapshot: Decodable")
    assert "var power = PowerSection()" in snapshot
    assert "power = c.value(.power, PowerSection())" in snapshot
    assert "case .power: power = held.power" in snapshot


def test_the_section_carries_no_clock():
    section = _code(_block(_read(MODELS), "struct PowerSection: Decodable"))
    keys = _block(section, "enum CodingKeys")
    for clock in ("_at", "remaining", "seconds", "time"):
        assert clock not in keys


# --- the Fleet row -------------------------------------------------------------


def _body() -> str:
    view = _block(_read(FLEET), "struct FleetView: View")
    return _code(_block(view, "var body: some View"))


def test_the_rule_is_called_once_inside_the_body():
    text = _code(_read(FLEET))
    assert text.count("MacPower.line(") == 1
    assert _body().count("MacPower.line(") == 1


def test_the_row_sits_under_the_refresh_notice_and_above_catch_up():
    body = _body()
    refresh = body.index('.id("refresh")')
    rule = body.index("MacPower.line(")
    row = body.index("CommentLine(text: line)")
    catch_up = body.index("Catch up across projects")
    assert refresh < rule < row < catch_up


def test_the_row_is_a_label_not_a_button():
    body = _body()
    row = _block(body, "if let line = MacPower.line(")
    assert "Button(" not in row
    assert "onTapGesture" not in row
    assert "sheets.show" not in row
    assert ".accessibilityLabel(line)" in row
    assert '.id("power")' in row
    for modifier in (".listRowInsets(EdgeInsets())", ".listRowSeparator(.hidden)",
                     ".listRowBackground(Theme.bg)"):
        assert modifier in row, modifier


def test_the_fleet_file_grew_no_control():
    """The three inventoried sheet routes are still the only buttons
    (`test_phone_action_feedback.py` pins which)."""
    text = _code(_read(FLEET))
    assert text.count("Button(") == 3
    assert text.count("DecryptButton(action: { sheets.show(") == 3
