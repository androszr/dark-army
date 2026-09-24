"""Pins for the phone's Usage tab.

`ios/` has no test target by explicit decision, so this is a Python lint over
the Swift source — `test_phone_theme_drift.py`'s pattern. It pins the third
tab, tolerant decode, short-name parity with the panel enum, the 75/90 colour
cuts against `menu_format.py`, wire-key parity, and the read-only door.
"""
import re
from pathlib import Path

from dark_army_menubar.menu_format import (
    USAGE_CRIT_PERCENT,
    USAGE_WARN_PERCENT,
)

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
DAEMON = ROOT / "host" / "dark_army_daemon"
PANEL_SETTINGS = ROOT / "panel" / "Sources" / "BobPanel" / "SettingsSection.swift"

APP = PHONE / "BobPhoneApp.swift"
CLIENT = PHONE / "Client.swift"
MODELS = PHONE / "Models.swift"
USAGE_VIEW = PHONE / "UsageView.swift"
TABLE = PHONE / "ProcessTable.swift"
API_SERVER = DAEMON / "api_server.py"
LIMITS = DAEMON / "limits.py"

WIRE_KEYS = (
    "kind",
    "label",
    "title",
    "percent",
    "resets_at",
    "stale",
    "provider",
    # When the figure was read. Every failure path in the live scoped fetch
    # lands on a *held* reading, so both clients have to be able to say how old
    # one is rather than drawing it as current.
    "as_of",
    # Which of the three readings a bar is. The age note is gated on it: a
    # statusline figure is old because nobody took a turn, not because a
    # refresh failed.
    "source",
    # Window family (`session` / `weekly`). Codex slots are not windows;
    # the composer rule picks 7d by this, with the label suffix as fallback.
    "group",
)

FUNCS = ("label", "modelSuffix", "figure", "ageNote")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _brace_chunk(text: str, start: int) -> str:
    brace = text.find("{", start)
    assert brace >= 0, f"no opening brace after {start}"
    depth = 0
    for i, ch in enumerate(text[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("unclosed brace")


def _enum_chunk(text: str) -> str:
    start = text.find("enum UsageChipText")
    assert start >= 0, "no enum UsageChipText"
    return _brace_chunk(text, start)


def _function_body(enum_src: str, name: str) -> str:
    needle = f"static func {name}"
    start = enum_src.find(needle)
    assert start >= 0, f"no static func {name}"
    return _brace_chunk(enum_src, start)


def _struct_chunk(text: str, name: str) -> str:
    needle = f"struct {name}"
    start = text.find(needle)
    assert start >= 0, f"no struct {name}"
    return _brace_chunk(text, start)


def test_the_sources_are_there():
    assert APP.is_file()
    assert CLIENT.is_file()
    assert MODELS.is_file()
    assert USAGE_VIEW.is_file()
    assert PANEL_SETTINGS.is_file()
    assert API_SERVER.is_file()
    assert LIMITS.is_file()
    assert list(PHONE.glob("*.swift")), "no Swift sources under ios/BobPhone"


def test_the_usage_tab_is_present_once():
    app = _read(APP)
    assert app.count('Label("Usage"') == 1
    assert app.count('"~/usage"') == 1
    assert 'Label("Fleet"' in app
    assert 'Label("Board"' in app
    assert "TabView" in app


def test_usage_types_decode_tolerantly():
    models = _read(MODELS)
    banned = "try c.decode("
    for name in ("UsageBar", "UsageReport", "UsageAttribution"):
        chunk = _struct_chunk(models, name)
        assert banned not in chunk, f"{name} uses synthesized-style decode"
        assert "c.value(" in chunk or "c.maybe(" in chunk, (
            f"{name} has no tolerant helper")
    snapshot = models.split("struct Snapshot")[1].split("struct Counts")[0]
    assert "usage" not in snapshot.lower()
    assert "case generatedAt = \"generated_at\"" in models


def test_short_names_match_the_panel_enum():
    panel = _enum_chunk(_read(PANEL_SETTINGS))
    phone = _enum_chunk(_read(USAGE_VIEW))
    for name, region in (("panel", panel), ("phone", phone)):
        assert "weekly_scoped" in region, f"no weekly_scoped in the {name} enum"
        assert "5h" in region, f"no 5h in the {name} enum"
    for name in FUNCS:
        assert _function_body(panel, name) == _function_body(phone, name), (
            f"{name} drifted between panel SettingsSection.swift and "
            "phone UsageView.swift")


def test_colour_cuts_match_menu_format():
    text = _read(USAGE_VIEW)
    warn = int(USAGE_WARN_PERCENT)
    crit = int(USAGE_CRIT_PERCENT)
    assert f">= {warn}" in text
    assert f">= {crit}" in text
    assert warn == int(USAGE_WARN_PERCENT)
    assert crit == int(USAGE_CRIT_PERCENT)


def test_the_phone_reads_only_keys_the_daemon_publishes():
    assert WIRE_KEYS, "wire-key list must not be empty"
    daemon = _read(API_SERVER) + _read(LIMITS)
    phone = _read(MODELS)
    for key in WIRE_KEYS:
        assert key in daemon, (
            f"wire key {key!r} is not in api_server.py/limits.py "
            "— the phone must not invent fields the Mac does not publish")
        assert key in phone, (
            f"wire key {key!r} is missing from Models.swift")


def test_bars_are_forgotten_with_the_pairing():
    client = _read(CLIENT)
    assert "func forgetUsage" in client
    assert "func forgetPairing" in client
    assert "forgetUsage()" in client.split("func forgetPairing", 1)[1]
    assert "record.token != usageToken" in client


def test_provider_mark_is_named_for_voiceover():
    view = _read(USAGE_VIEW)
    # The silhouette *inside* the mark is decoration and is hidden (see
    # `test_phone_accessibility.py`); the mark itself must stay in the
    # reader's path, carrying the provider's name. So the assertion is over
    # the mark's own trailing chain, not over the whole file — a hidden
    # canvas elsewhere in this file is not this mark going silent.
    mark = _struct_chunk(view, "PhoneProviderMark")
    tail = mark[mark.index(".frame(width: size, height: size)"):]
    assert ".accessibilityHidden(true)" not in tail
    assert "accessibilityLabel(Self.label(for: provider))" in view
    assert 'case "grok": return "Grok"' in view
    assert 'case "codex": return "Codex"' in view
    assert 'return "Claude"' in view


def test_usage_fetch_is_a_get():
    """The Usage tab only looks. Writes already live in Client.post; this
    pin is that /api/usage is never POSTed."""
    client = _read(CLIENT)
    assert 'kind: "usage"' in client
    usage_chunk = client.split("func pollUsage", 1)[1]
    assert 'httpMethod = "POST"' not in usage_chunk
    # A sealed `usage` frame on the home channel, nothing else.
    assert 'kind: "usage"' in usage_chunk
    assert "X-Bob-Device" not in usage_chunk
    view = _read(USAGE_VIEW)
    assert "/api/action" not in view
    assert 'httpMethod = "POST"' not in view


def test_lan_docstring_is_no_longer_two_routes():
    text = _read(API_SERVER)
    start = text.find("async def _handle_lan_client")
    assert start >= 0
    chunk = text[start:start + 1200]
    assert "two routes" not in chunk
    assert "/api/usage" in chunk


def test_usage_bars_are_grouped_under_named_provider_headers():
    """The rows sit under one heading per provider, and the per-row mark that
    heading replaced is gone for good."""
    view = _read(USAGE_VIEW)
    assert "opensRun" not in view, "the per-run mark logic came back"
    row = _struct_chunk(view, "UsageWindowRow")
    assert "PhoneProviderMark" not in row, "a row is drawing its own mark again"
    assert "Color.clear" not in row, "the row's mark spacer came back"
    assert "provider.uppercased()" in view, "no name for an unknown provider"
    assert "displayName" in view
    assert view.count("PhoneSectionHeader(") >= 2, (
        "the groups and the spenders list must both be headed")
    assert "ForEach(attribution.spenderGroups)" in view
    assert "title: group.heading, provider: group.provider" in view
    assert 'title: "CLAUDE · SPENDERS"' not in view


def test_section_header_can_carry_a_provider_mark():
    header = _struct_chunk(_read(TABLE), "PhoneSectionHeader")
    assert "PhoneProviderMark" in header
    assert "provider" in header


def test_a_usage_bar_is_identified_by_its_title_too():
    """Every per-model weekly window is `kind == "weekly_scoped"` — only the
    title varies — so a two-part `provider:kind` id collides the moment the
    endpoint reports two of them, and `ForEach(id: \\.id)` over a colliding pair
    is undefined. One rule for every kind, in both files: a split id rule is how
    the collision comes back."""
    stale = '"\\(provider):\\(kind)"'
    for path in (MODELS, ROOT / "panel" / "Sources" / "BobPanel" / "ActionModels.swift"):
        chunk = _struct_chunk(_read(path), "UsageBar")
        line = next(l for l in chunk.splitlines() if "var id: String" in l)
        assert "title" in line, f"{path.name}: UsageBar.id does not include the title"
        assert stale not in chunk, (
            f"{path.name}: the two-part id literal is back")


def test_both_surfaces_define_and_draw_the_age_note():
    """`ageNote` is pinned identical between the two enums by
    `test_short_names_match_the_panel_enum`; this pins that each surface also
    *uses* it, which textual parity of the definition cannot see."""
    for path in (USAGE_VIEW, PANEL_SETTINGS):
        text = _read(path)
        assert text.count("ageNote") >= 2, (
            f"{path.name}: ageNote is defined but never drawn")
        assert "UsageChipText.ageNote(asOf: bar.asOf, source: bar.source)" in text
    # ...and the field it reads is decoded, tolerantly, in both models.
    for path in (MODELS, ROOT / "panel" / "Sources" / "BobPanel" / "ActionModels.swift"):
        chunk = _struct_chunk(_read(path), "UsageBar")
        assert 'case asOf = "as_of"' in chunk
        assert "asOf = c.maybe(.asOf)" in chunk, (
            f"{path.name}: as_of must decode tolerantly — an older daemon "
            "publishes none, and a throw there blanks the whole tab")


def test_the_age_note_is_not_the_stale_flag():
    """Two different claims: `stale` says the window has reset and the figure is
    now wrong; the note says nobody has refreshed it lately. A surface that
    conflated them would go quiet in exactly the case the note exists for."""
    for path in (USAGE_VIEW, PANEL_SETTINGS):
        body = _function_body(_enum_chunk(_read(path)), "ageNote")
        assert "stale" not in body.replace("staleReadSeconds", "")


def test_the_age_note_is_silent_for_a_self_reported_figure():
    """The note claims a refresh did not get through. For the two account-wide
    windows that is never true — Claude Code reports them on every turn, so an
    old one means the Mac was quiet. Drawn there, it fired on both meters after
    every half-hour lull, which is how a warning stops being read before the
    one real case arrives. Pinned in both surfaces and in the source name the
    daemon actually writes."""
    assert '"statusline"' in _read(LIMITS), (
        "limits.py no longer names the statusline source the gate keys on")
    for path in (USAGE_VIEW, PANEL_SETTINGS):
        chunk = _enum_chunk(_read(path))
        assert 'selfReportedSource = "statusline"' in chunk, (
            f"{path.name}: the gated source is not named")
        body = _function_body(chunk, "ageNote")
        assert "source != selfReportedSource" in body, (
            f"{path.name}: ageNote does not gate on the source")


def test_an_unnamed_source_keeps_the_age_note():
    """Codex and Grok bars name no source and have no local fallback, so an old
    one really is a refresh that did not get through. The gate must therefore
    exclude the one self-reported name rather than admit a list, and the
    parameter's default must be the unnamed source."""
    for path in (USAGE_VIEW, PANEL_SETTINGS):
        body = _function_body(_enum_chunk(_read(path)), "ageNote")
        assert 'source: String = ""' in body, (
            f"{path.name}: the default source must be the unnamed one")
        assert "source ==" not in body, (
            f"{path.name}: the gate must exclude one source, not admit a list")


def test_spenders_have_per_provider_cap_measurement_coverage_and_replace_on_both_legs():
    models, view, client = _read(MODELS), _read(USAGE_VIEW), _read(CLIENT)
    group = _struct_chunk(models, "ProviderShare")
    assert "try c.decode(" not in group
    assert "share of locally recorded tokens" in group
    assert '"window_start"' in group and '"window_end"' in group
    assert "group.models.prefix(5)" in view
    assert "group.period" in view and "group.reason" in view
    assert "Partial local history" in view
    for method in ("pollUsageViaRelay", "pollUsage(host"):
        chunk = _brace_chunk(client, client.index("private func " + method))
        assert "attribution = report.attribution ?? UsageAttribution()" in chunk
        assert chunk.index("guard answer.failure.isEmpty") < chunk.index("attribution =")
        assert "record.token == self.record?.token" in chunk
    reset = _brace_chunk(client, client.index("private func forgetUsage"))
    assert "attribution = UsageAttribution()" in reset


def test_real_swift_spenders_decoder_handles_legacy_empty_malformed_and_groups(tmp_path):
    """Compile the production Foundation types rather than reimplement decoding."""
    import subprocess
    models = _read(MODELS)
    helper = _brace_chunk(models, models.index("extension KeyedDecodingContainer"))
    types = _struct_chunk(models, "UsageAttribution")
    program = 'import Foundation\n' + helper + '\n' + types + r'''
func decode(_ text: String) throws -> UsageAttribution {
    try JSONDecoder().decode(UsageAttribution.self, from: Data(text.utf8))
}
let legacy = try decode(#"{"available":true,"models":[{"model":"grok-model","pct":44}]}"#)
assert(legacy.spenderGroups.count == 1)
assert(legacy.spenderGroups[0].heading == "SPENDERS")
assert(legacy.spenderGroups[0].provider.isEmpty)
let empty = try decode(#"{"available":true,"models":[{"model":"old"}],"providers":[]}"#)
assert(empty.spenderGroups.isEmpty)
let malformed = try decode(#"{"available":"bad","providers":[{"provider":"codex","partial":"bad","models":null,"window_start":"bad"}]}"#)
assert(malformed.spenderGroups.count == 1)
assert(malformed.spenderGroups[0].models.isEmpty)
assert(malformed.spenderGroups[0].windowStart == nil)
let grouped = try decode(#"{"providers":[{"provider":"codex","available":true,"measurement":"local_token_share","models":[{"model":"gpt","pct":75}]}]}"#)
assert(grouped.spenderGroups[0].models[0].pct == 75)
assert(grouped.spenderGroups[0].caption.contains("share of locally recorded tokens"))
assert(try decode("{}").spenderGroups.isEmpty)
'''
    # Swift's assert autoclosure does not accept a throwing expression.
    program = program.replace('assert(try decode("{}").spenderGroups.isEmpty)',
                              'let absent = try decode("{}"); assert(absent.spenderGroups.isEmpty)')
    source = tmp_path / "main.swift"
    source.write_text(program)
    built = subprocess.run(["swiftc", str(source), "-o", str(tmp_path / "decode")],
                           capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(tmp_path / "decode")], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr


def test_phone_tab_stays_five_named_cases():
    app = _read(APP)
    match = re.search(r"enum PhoneTab.*?\n\}", app, re.S)
    assert match, "no enum PhoneTab"
    chunk = match.group(0)
    assert chunk.count("case ") == 1
    assert "case needs, fleet, board, comm, usage" in chunk
    assert app.count('Label("Usage"') == 1


def test_usage_report_rows_are_navigation_links_not_inline_sections():
    view = _read(USAGE_VIEW)
    assert view.count("NavigationLink") == 2
    code = re.sub(r"//[^\n]*", "", view)
    links = list(re.finditer(
        r"\bNavigationLink(?:\([^\n]*?\))?\s*\{\s*(\w+)\(", code))
    assert [m.group(1) for m in links] == [
        "PhoneAgentReport", "PhoneLifecycleReport"]
    assert view.count("PhoneAgentReport(client:") == 1
    assert view.count("PhoneLifecycleReport(client:") == 1


def test_looking_back_reports_are_not_folded():
    for name in ("AgentReportView.swift", "LifecycleReportView.swift"):
        text = (PHONE / name).read_text()
        assert "DisclosureGroup" not in text, name


def test_opening_usage_does_not_fetch_either_report():
    view = _read(USAGE_VIEW)
    assert "agentReport(" not in view
    assert "lifecycleReport(" not in view


def test_report_rows_are_gated_on_support_flags():
    view = _read(USAGE_VIEW)
    assert "agentReportSupported" in view
    assert "lifecycleSupported" in view


def test_usage_report_row_labels_wrap():
    """List sits the label next to a chevron; without fixedSize the long
    phrases this plan kept off the bar clip on the row instead."""
    view = _read(USAGE_VIEW)
    for label in ("Agents · last 30 days", "Where time went"):
        idx = view.index(f'Text("{label}")')
        chunk = view[idx:idx + 400]
        assert ".fixedSize(horizontal: false, vertical: true)" in chunk, label
        assert ".lineLimit(" not in chunk, label
