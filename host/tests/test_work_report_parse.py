"""A finished agent's report, split once into the parts it names.

`work_report.parse` reads `last_report` (the slice `session_stats` cuts from
`## Work done` to the end of the message) into Asked / Changed / Verified /
Unchecked / Card and one headline; both clients draw that shape through
`WorkReport`, a Foundation-only enum byte-pinned Mac/phone from the
`enum WorkReport {` line down (`test_run_figures.py`'s idiom), whose rules
are run under `swiftc` here rather than read (`test_comm_rules.py`'s idiom).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import work_report as wr

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "host" / "dark_army_daemon" / "work_report.py"
PANEL_FILE = REPO / "panel" / "Sources" / "BobPanel" / "WorkReport.swift"
PHONE_FILE = REPO / "ios" / "BobPhone" / "WorkReport.swift"
PROJECT = REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
MARKER = "enum WorkReport {"

# The fixture `test_work_report_surface.py` has always used: the hint's shape,
# nothing left unchecked.
REPORT = (
    "## Work done\n"
    "**Asked:** why refines open in the editor.\n"
    "**Changed:** the spawner now follows the preference.\n"
    "**Verified:** the suite passed.\n"
    "**Unchecked:** Nothing - every check above ran.\n"
)

STEPS = (
    "## Work done\n"
    "**Asked:** make the banner quiet.\n"
    "**Changed:**\n"
    "- notifier.py: no sound for a finished run\n"
    "- alerts.py: the report rule\n"
    "**Verified:**\n"
    "- the alert tests\n"
    "- the full suite\n"
    "**Unchecked:**\n"
    "1. Open any enrolled project and start a session.\n"
    "2. Let it finish and\n"
    "   watch the corner of the screen.\n"
    "3. Expect a banner with no sound.\n"
    "Why not automated: the sound is on a real Mac.\n"
    "**Card:** closed, the check flagged.\n"
)


# --- the parser ----------------------------------------------------------------


def test_the_standing_fixture_parses_to_its_four_parts():
    p = wr.parse(REPORT)
    assert p["labelled"] is True and p["cut"] is False
    assert p["asked"] == "why refines open in the editor."
    assert p["changed"] == ["the spawner now follows the preference."]
    assert p["verified"] == ["the suite passed."]
    assert p["unchecked"] == [] and p["nothing_unchecked"] is True
    assert p["why_not_automated"] == "" and p["card"] == ""


def test_bullets_under_changed_become_items():
    p = wr.parse(STEPS)
    assert p["changed"] == ["notifier.py: no sound for a finished run",
                            "alerts.py: the report rule"]
    assert p["verified"] == ["the alert tests", "the full suite"]


def test_asked_without_bold_parses():
    p = wr.parse("## Work done\nAsked: plain labels.\nChanged: one file.\n")
    assert p["labelled"] is True
    assert p["asked"] == "plain labels."
    assert p["changed"] == ["one file."]


def test_bulleted_labels_parse():
    p = wr.parse("## Work done\n- **Asked:** x\n- **Changed:** y\n")
    assert p["asked"] == "x" and p["changed"] == ["y"]


@pytest.mark.parametrize("line", [
    "Nothing - every check above ran.",
    "Nothing - every check above ran",
    "Nothing — every check above ran.",
    "Nothing – every check above ran.",
    "**Nothing - every check above ran.**",
])
def test_nothing_unchecked_is_recognised_in_every_dash(line):
    p = wr.parse(f"## Work done\n**Changed:** x\n**Unchecked:** {line}\n")
    assert p["nothing_unchecked"] is True
    assert p["unchecked"] == []


def test_numbered_steps_and_the_reason_are_kept_apart():
    p = wr.parse(STEPS)
    assert p["unchecked"] == [
        "Open any enrolled project and start a session.",
        "Let it finish and watch the corner of the screen.",
        "Expect a banner with no sound.",
    ]
    assert p["why_not_automated"] == "Why not automated: the sound is on a real Mac."
    assert p["nothing_unchecked"] is False


def test_the_card_line_lands_in_card():
    assert wr.parse(STEPS)["card"] == "closed, the check flagged."


def test_a_head_cut_report_parses_without_inventing_asked():
    cut = "…a line of the report.\n**Changed:** the tail.\n**Verified:** it ran.\n"
    p = wr.parse(cut)
    assert p["cut"] is True and p["labelled"] is True
    assert p["asked"] == ""
    assert p["changed"] == ["the tail."]
    assert p["headline"].startswith("Changed: the tail")


def test_a_report_with_no_labels_is_honestly_unlabelled():
    p = wr.parse("## Work done\nEverything went fine, see the diff.\n")
    assert p["labelled"] is False
    assert p["headline"] == ""
    assert p["changed"] == [] and p["asked"] == ""
    assert wr.parse("")["labelled"] is False
    assert wr.parse(None)["labelled"] is False          # type: ignore[arg-type]


def test_a_runaway_list_is_clamped():
    long = "## Work done\n**Changed:**\n" + "- a line of the report\n" * 400
    assert len(wr.parse(long)["changed"]) == wr.MAX_ITEMS


def test_a_lead_in_before_a_list_is_not_an_item():
    p = wr.parse("## Work done\n**Changed:** three files:\n- a\n- b\n")
    assert p["changed"] == ["a", "b"]


def test_a_fenced_unchecked_list_is_not_counted_with_its_fence():
    fenced = ("## Work done\n**Changed:** x\n**Unchecked:**\n```\n"
              "1. Open the app.\n2. Press Restart.\n```\n"
              "Why not automated: a real screen.\n")
    p = wr.parse(fenced)
    assert p["unchecked"] == ["Open the app.", "Press Restart."]
    assert p["unchecked_total"] == 2
    assert p["why_not_automated"] == "Why not automated: a real screen."
    assert p["headline"].endswith("2 unchecked")
    inside = ("## Work done\n**Changed:** x\n**Unchecked:**\n~~~\n"
              "1. Open it.\nWhy not automated: needs a phone.\n~~~\n")
    q = wr.parse(inside)
    assert q["unchecked"] == ["Open it."]
    assert q["why_not_automated"] == "Why not automated: needs a phone."


def test_a_clamped_list_keeps_its_true_count():
    long = ("## Work done\n**Changed:**\n" + "- a change\n" * 20
            + "**Verified:**\n" + "- a check\n" * 15
            + "**Unchecked:**\n" + "".join(f"{i}. step\n" for i in range(1, 15)))
    p = wr.parse(long)
    assert len(p["changed"]) == wr.MAX_ITEMS and p["changed_total"] == 20
    assert len(p["verified"]) == wr.MAX_ITEMS and p["verified_total"] == 15
    assert len(p["unchecked"]) == wr.MAX_ITEMS and p["unchecked_total"] == 14
    assert "15 verified" in p["headline"] and "14 unchecked" in p["headline"]


def test_totals_default_to_the_list_length():
    assert wr.headline({"labelled": True, "changed": ["x"], "verified": ["a", "b"]}) \
        == "Changed: x \u00b7 2 verified"
    assert wr.parse(REPORT)["verified_total"] == 1


def test_parse_is_memoised_and_every_caller_gets_its_own_copy():
    first = wr.parse(STEPS)
    first["changed"].append("mutated")
    first["headline"] = "mutated"
    again = wr.parse(STEPS)
    assert "mutated" not in again["changed"] and again["headline"] != "mutated"
    assert wr._parse_cached.cache_info().maxsize == wr.PARSE_CACHE


def test_long_blank_runs_parse_in_linear_time():
    """The label and bullet patterns never put two whitespace quantifiers
    side by side, so a line of thousands of blanks cannot backtrack."""
    import time
    body = ("## Work done\n**Changed:** x\n" + " \t" * 1950 + "\n"
            + "-" + " " * 3900 + "\n")[:4000]
    assert len(body) == 4000
    for text in (body, body.replace("**Changed:**", "- **Changed:**")):
        wr._parse_cached.cache_clear()
        started = time.perf_counter()
        wr.parse(text)
        assert time.perf_counter() - started < 0.05


@pytest.mark.parametrize("item", [
    "Card: the board tile now wraps its title",
    "Verified: the old check still holds",
    "Asked: a question nobody asked",
])
def test_a_plain_bulleted_label_word_under_changed_stays_an_item(item):
    p = wr.parse("## Work done\n**Changed:**\n- the first change\n"
                 f"- {item}\n- the last change\n**Verified:** it ran.\n")
    assert p["changed"] == ["the first change", item, "the last change"]
    assert p["card"] == "" and p["verified"] == ["it ran."]
    assert p["headline"].startswith("Changed: the first change")


def test_a_bold_bulleted_label_still_opens_its_section():
    p = wr.parse("## Work done\n- **Changed:** x\n- **Card:** closed, check flagged.\n")
    assert p["changed"] == ["x"]
    assert p["card"] == "closed, check flagged."


# --- the headline --------------------------------------------------------------


def test_headline_names_the_change_the_checks_and_nothing_unchecked():
    assert wr.parse(REPORT)["headline"] == (
        "Changed: the spawner now follows the preference · 1 verified"
        " · nothing unchecked")


def test_headline_counts_unchecked_steps():
    assert wr.parse(STEPS)["headline"] == (
        "Changed: notifier.py: no sound for a finished run · 2 verified"
        " · 3 unchecked")


def test_headline_leads_with_what_exists():
    assert wr.headline(wr.parse("## Work done\n**Asked:** a thing.\n")) \
        == "Asked: a thing"
    assert wr.headline({"labelled": False, "changed": ["x"]}) == ""


def test_headline_clamps_with_an_ellipsis():
    p = wr.parse("## Work done\n**Changed:** " + "word " * 80 + "\n")
    assert len(p["headline"]) <= wr.HEADLINE_CHARS
    assert p["headline"].endswith("…")


def test_headline_is_plain_text():
    p = wr.parse("## Work done\n**Changed:** `notifier.py` is **quiet**\n")
    assert p["headline"] == "Changed: notifier.py is quiet"


def test_headline_of_reads_the_row():
    assert wr.headline_of({"work_report": wr.parse(REPORT)}).startswith("Changed:")
    assert wr.headline_of({"last_report": REPORT}) == ""
    assert wr.headline_of({"work_report": "text"}) == ""
    assert wr.headline_of({}) == ""


def test_the_module_imports_nothing_from_the_daemon():
    text = MODULE.read_text()
    assert not re.search(r"^from \.", text, re.M)
    assert not re.search(r"^\s*(?:from|import) dark_army_", text, re.M)
    imports = set(re.findall(r"^(?:from|import) (\w+)", text, re.M))
    assert imports <= {"__future__", "functools", "re", "typing"}, imports


# --- the byte-pinned Swift pair ------------------------------------------------


def _shared(path: Path) -> str:
    lines = path.read_text().splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\n") == MARKER]
    assert len(starts) == 1, f"expected one {MARKER!r} line in {path}"
    return "".join(lines[starts[0]:])


def test_the_panel_and_phone_share_one_rule():
    panel, phone = _shared(PANEL_FILE), _shared(PHONE_FILE)
    for name, region in (("panel", panel), ("phone", phone)):
        assert len(region) >= 1500, f"parsed too little from the {name} copy"
        assert "static func rowLead(headline: String, base: String) -> String" in region
        assert "static func uncheckedCaption(count: Int, nothing: Bool) -> String" in region
        assert '"Nothing unchecked — every check ran"' in region
    assert panel == phone, (
        "ios/BobPhone/WorkReport.swift has drifted from the panel's copy — "
        "mirror the edit onto the other side; never re-baseline one side.")
    for path in (PANEL_FILE, PHONE_FILE):
        assert "import Foundation" in path.read_text()
        assert "SwiftUI" not in path.read_text()


def test_the_phone_project_compiles_the_shared_file():
    project = PROJECT.read_text()
    assert "/* WorkReport.swift in Sources */ = {isa = PBXBuildFile;" in project
    assert re.search(r"^\s+\w+ /\* WorkReport\.swift in Sources \*/,$", project, re.M)
    assert "path = WorkReport.swift;" in project


# --- the rules, run ------------------------------------------------------------

DECODING = r'''
extension KeyedDecodingContainer {
    func value<T: Decodable>(_ key: Key, _ fallback: T) -> T {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) ?? fallback
    }
    func maybe<T: Decodable>(_ key: Key) -> T? {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil)
    }
}
'''

HARNESS = r'''
struct In: Decodable {
    let mode: String
    let headline: String?
    let base: String?
    let count: Int?
    let nothing: Bool?
    let steps: [String]?
    let report: WorkReport.Parsed?
}
let input = try! JSONDecoder().decode(In.self, from: FileHandle.standardInput.readDataToEndOfFile())
var out: [String: Any] = [:]
switch input.mode {
case "rowLead":
    out["lead"] = WorkReport.rowLead(headline: input.headline!, base: input.base!)
case "unchecked":
    out["caption"] = WorkReport.uncheckedCaption(count: input.count!, nothing: input.nothing!)
    out["nothing"] = WorkReport.nothingUnchecked
case "numbered":
    out["steps"] = WorkReport.numbered(input.steps!)
case "more":
    out["more"] = WorkReport.more(shown: input.count!, total: input.steps!.count)
    out["cut"] = WorkReport.cutNote
default:
    out["sections"] = WorkReport.sections(input.report!).map { $0.caption }
    out["headline"] = input.report!.headline
    out["totals"] = [WorkReport.total(input.report!, .changed),
                     WorkReport.total(input.report!, .verified),
                     WorkReport.total(input.report!, .unchecked)]
    out["cutFlag"] = input.report!.cut
}
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    tmp = tmp_path_factory.mktemp("work-report-probe")
    path = tmp / "WorkReportProbe.swift"
    path.write_text(PANEL_FILE.read_text() + "\n" + DECODING + "\n" + HARNESS)
    executable = tmp / "WorkReportProbe"
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr

    def run(payload: dict) -> dict:
        ran = subprocess.run([str(executable)], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30)
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)

    return run


def test_rowLead_puts_the_headline_first(probe):
    h = "Changed: x · 1 verified · nothing unchecked"
    assert probe({"mode": "rowLead", "headline": h, "base": "Mobile cards"})["lead"] \
        == f"{h} · Mobile cards"
    assert probe({"mode": "rowLead", "headline": "", "base": "Mobile cards"})["lead"] \
        == "Mobile cards"
    assert probe({"mode": "rowLead", "headline": h, "base": "—"})["lead"] == h


def test_uncheckedCaption_and_nothing_unchecked(probe):
    none = probe({"mode": "unchecked", "count": 0, "nothing": True})
    assert none["caption"] == "UNCHECKED"
    assert none["nothing"] == "Nothing unchecked — every check ran"
    assert probe({"mode": "unchecked", "count": 3, "nothing": False})["caption"] \
        == "UNCHECKED (3)"


def test_numbered_steps(probe):
    assert probe({"mode": "numbered", "steps": ["Open it.", "Press it."]})["steps"] \
        == ["1. Open it.", "2. Press it."]


@pytest.mark.parametrize("report, sections", [
    (REPORT, ["ASKED", "CHANGED", "VERIFIED", "UNCHECKED"]),
    (STEPS, ["ASKED", "CHANGED", "VERIFIED", "UNCHECKED", "CARD"]),
    ("## Work done\nno labels\n", []),
])
def test_the_daemons_shape_decodes_into_the_sections_drawn(probe, report, sections):
    """The dict `parse` publishes is what `WorkReport.Parsed` decodes: the
    two ends agree on every key, run rather than read."""
    parsed = wr.parse(report)
    got = probe({"mode": "sections", "report": parsed})
    assert got["sections"] == sections
    assert got["headline"] == parsed["headline"]
    assert got["totals"] == [parsed["changed_total"], parsed["verified_total"],
                             parsed["unchecked_total"]]


def test_a_clamped_and_cut_report_crosses_the_wire_whole(probe):
    long = ("\u2026tail of Asked\n**Changed:**\n" + "- a change\n" * 20
            + "**Unchecked:**\n" + "".join(f"{i}. step\n" for i in range(1, 15)))
    parsed = wr.parse(long)
    got = probe({"mode": "sections", "report": parsed})
    assert got["totals"] == [20, 0, 14] and got["cutFlag"] is True


def test_more_and_the_cut_note(probe):
    got = probe({"mode": "more", "count": 12, "steps": ["s"] * 20})
    assert got["more"] == "+8 more"
    assert got["cut"] == "(report cut \u2014 start not kept)"
    assert probe({"mode": "more", "count": 3, "steps": ["s"] * 3})["more"] == ""
