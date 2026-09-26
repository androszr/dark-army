# host/tests/test_phone_context_trend.py
"""The phone says which way an agent's context is heading.

Source pins over the Swift files, `test_phone_reconnect_display.py`'s idiom —
`ios/` has no test target by explicit decision, and this suite is the one
that actually runs.

The daemon publishes a slope per agent as `trend` (`SampleRing.trend`), whose
contract is that "no answer yet" and "an answer of zero" are tellable apart by
whoever renders it. Everything pinned here keeps the phone on that side of the
line: every rate is optional and decoded through `maybe`, never defaulted;
the steady threshold lives in one constant; the marker is text and the tint
is untouched; the detail line copies `signals.py`'s register; and the widget
gains nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
PROCESS = PHONE / "ProcessTable.swift"
DETAIL = PHONE / "AgentDetailView.swift"
PANEL_MODELS = ROOT / "panel" / "Sources" / "BobPanel" / "Models.swift"
DAEMON = ROOT / "host" / "dark_army_daemon"
SAMPLES = DAEMON / "samples.py"
SIGNALS = DAEMON / "signals.py"
DAEMON_PY = DAEMON / "daemon.py"
WIDGET_SUMMARY = ROOT / "ios" / "Shared" / "FleetSummary.swift"
WIDGET_DIR = ROOT / "ios" / "BobPhoneWidget"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _struct(text: str, name: str) -> str:
    """A top-level `struct <name>` declaration, up to the next top-level one."""
    match = re.search(rf"\bstruct {name}\b", text)
    assert match, f"no struct {name}"
    rest = text[match.start():]
    ends = [i for i in (rest.find("\nstruct ", 1),
                        rest.find("\nfinal class ", 1),
                        rest.find("\nextension ", 1),
                        rest.find("\nenum ", 1)) if i > 0]
    return rest[:min(ends)] if ends else rest


def _member(text: str, signature: str) -> str:
    """A member's body: from its signature to the next member at the same
    four-space indent (a `var`, `func` or `static`)."""
    start = text.index(signature)
    rest = text[start + len(signature):]
    ends = [i for i in (rest.find("\n    var "),
                        rest.find("\n    private var "),
                        rest.find("\n    func "),
                        rest.find("\n    private func "),
                        rest.find("\n    static "),
                        rest.find("\n    enum "),
                        rest.find("\n}")) if i >= 0]
    return rest[:min(ends)] if ends else rest


def _raw_values(struct_text: str) -> set[str]:
    return set(re.findall(r'case \w+ = "([^"]+)"', struct_text))


# --- the wire ------------------------------------------------------------------

def test_the_phone_decodes_trend_with_the_daemon_s_own_keys():
    trend = _struct(_read(MODELS), "Trend")
    samples = _read(SAMPLES)
    for key in ('"ctx_pct_per_min"', '"ctx_runway_seconds"'):
        assert key in trend, f"phone Trend lacks {key}"
        assert key in samples, f"{key} is not a literal the daemon publishes"
    assert "case samples" in trend


def test_the_phone_trend_matches_the_panel_s_wire_keys():
    phone = _raw_values(_struct(_read(MODELS), "Trend"))
    panel = _raw_values(_struct(_read(PANEL_MODELS), "Trend"))
    assert phone, "no raw keys found in the phone's Trend"
    assert phone == panel


def test_the_daemon_publishes_trend_per_entry():
    assert 'entry["trend"] = self._samples.trend(' in _read(DAEMON_PY)


def test_an_older_mac_decodes_to_an_empty_trend():
    agent = _struct(_read(MODELS), "Agent")
    assert "trend = c.value(.trend, Trend())" in agent
    keys = agent[agent.index("enum CodingKeys"):agent.index("init(from decoder")]
    assert re.search(r"\btrend\b", keys), "CodingKeys does not name trend"
    assert "var trend = Trend()" in agent


# --- absent is never zero ------------------------------------------------------

def test_every_rate_is_optional_and_never_defaulted():
    trend = _struct(_read(MODELS), "Trend")
    assert "var ctxPctPerMin: Double?" in trend
    assert "var ctxRunwaySeconds: Double?" in trend
    assert "ctxPctPerMin = c.maybe(.ctxPctPerMin)" in trend
    assert "ctxRunwaySeconds = c.maybe(.ctxRunwaySeconds)" in trend
    forbidden = ("ctxPctPerMin ?? ", "ctxRunwaySeconds ?? ",
                 "c.value(.ctxPctPerMin", "c.value(.ctxRunwaySeconds")
    for path in sorted(PHONE.rglob("*.swift")):
        text = path.read_text()
        for form in forbidden:
            assert form not in text, f"{path.name} defaults a rate: {form!r}"


def test_the_marker_is_guarded_on_the_optional():
    trend = _struct(_read(MODELS), "Trend")
    pace = _member(trend, "var pace: Pace? {")
    assert "guard let rate = ctxPctPerMin else { return nil }" in pace
    row = _member(_read(PROCESS), "private var ctxLabel: String {")
    assert "agent.trend.pace?.marker" in row
    assert "?? 0" not in row


def test_no_zero_rate_is_ever_printed():
    for path in (PROCESS, DETAIL):
        text = _read(path)
        assert "0%/min" not in text, path.name
        assert "0.0%/min" not in text, path.name


# --- one threshold -------------------------------------------------------------

def test_the_steady_band_is_one_constant():
    models = _read(MODELS)
    assert models.count("static let steadyBand") == 1
    assert "0.05" not in _read(PROCESS)
    assert "0.05" not in _read(DETAIL)


# --- text, never colour --------------------------------------------------------

def test_the_marker_is_text_and_the_tint_is_untouched():
    trend = _struct(_read(MODELS), "Trend")
    pace_enum = trend[trend.index("enum Pace"):]
    for glyph in ('"↑"', '"→"', '"↓"'):
        assert glyph in pace_enum
    colour = _member(_read(PROCESS), "private var ctxColor: Color {")
    assert "exceeds200k" in colour
    assert "pct >= 85" in colour
    assert "pct >= 75" in colour
    assert "trend" not in colour


def test_voiceover_gets_the_pace_in_words():
    process = _read(PROCESS)
    assert ".accessibilityLabel(ctxSpoken)" in process
    trend = _struct(_read(MODELS), "Trend")
    spoken = trend[trend.index("var spoken: String"):trend.index("var pace: Pace?")]
    for word in ('"rising"', '"steady"', '"falling"'):
        assert word in spoken


# --- the column does not widen -------------------------------------------------

def test_the_header_and_row_ctx_widths_agree():
    process = _read(PROCESS)
    header = re.search(r'Text\("CTX"\)\.frame\(width: (\d+)', process)
    assert header, "no CTX heading width"
    after_label = process[process.index("Text(ctxLabel)"):]
    row = re.search(r"\.frame\(width: (\d+)", after_label)
    assert row, "no CTX cell width"
    assert int(header.group(1)) == int(row.group(1))
    ctx = int(row.group(1))
    face = int(re.search(r"PixelMark\(.*?size: (\d+)", process, re.S).group(1))
    state = int(re.search(r"Text\(stateLabel\).*?\.frame\(width: (\d+)",
                          process, re.S).group(1))
    age_width = int(re.search(r"static let width: CGFloat = (\d+)", process).group(1))
    arithmetic = re.search(
        r"(\d+) \+ (\d+) \+ (\d+) \+ (\d+) = (\d+)", process)
    assert arithmetic, "the doc comment lost its width arithmetic"
    a, b, age, c, total = (int(g) for g in arithmetic.groups())
    assert (a, b, age, c) == (face, state, age_width, ctx)
    assert a + b + age + c == total
    assert "100%↑" in process, "the doc comment no longer records the measured marker"


# --- the detail line -----------------------------------------------------------

def test_the_detail_line_is_the_signals_register():
    detail = _read(DETAIL)
    assert "full in ~" in detail
    assert "%+.1f%%/min" in detail
    assert "(steady)" in detail
    line = _member(detail, "static func contextLine(ctx: Double, trend: Trend) -> String {")
    assert 'var line = "ctx       \\(' in line
    assert "elapsed(runway)" in line, "the runway must reuse elapsed(_:)"
    assert "full in ~" in _read(SIGNALS)


def test_the_detail_view_reads_the_trend_through_context_line():
    detail = _read(DETAIL)
    assert "Self.contextLine(ctx: ctx, trend: agent.trend)" in detail
    # The old bare line is gone; the only `ctx       ` literal is inside
    # contextLine itself.
    assert detail.count('"ctx       \\(') == 1


# --- the widget gains nothing --------------------------------------------------

def test_the_widget_is_untouched():
    assert "trend" not in _read(WIDGET_SUMMARY)
    files = sorted(WIDGET_DIR.rglob("*.swift"))
    assert files, "no widget sources found"
    for path in files:
        assert "trend" not in path.read_text(), path.name
