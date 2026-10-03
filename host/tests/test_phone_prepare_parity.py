# host/tests/test_phone_prepare_parity.py
"""The phone's own Prepare route reads and asks exactly as the Mac does.

`ios/BobPhone/PreparerBrief.swift` carries the brief and the idea-mode label
list as two literals that must equal `card_preparer_brief.BRIEF` and
`card_prepare.MODE_HEAD_IDEA` byte for byte. `ios/BobPhone/CardPrepareRules.swift`
is a Foundation-only port of the Mac's readers and refusals; this module
slices it between its marker lines, builds it with `swiftc` (`test_cast.py`'s
pattern), and runs the same answer texts through both sides, insisting on
identical fields. It skips cleanly with no toolchain.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import areas, card_prepare, card_preparer_brief

ROOT = Path(__file__).resolve().parents[2]
IOS = ROOT / "ios" / "BobPhone"
BRIEF_SWIFT = IOS / "PreparerBrief.swift"
RULES_SWIFT = IOS / "CardPrepareRules.swift"

ROSTER = ["bc-implementer", "bc-verifier", "bc-bug-auditor",
          "bc-security-reviewer", "bc-integration-reviewer", "bc-planner"]
ROOTS = ["/Users/x/Code/alpha", "/Users/x/Code/beta", "/Users/x/other/beta"]
AREAS = [(a.name, a.concept) for a in areas.AREAS]
AREA_KEYS = [(a.slug, a.name) for a in areas.AREAS]

GOLDEN_ANSWERS = [
    # fenced, the ordinary shape
    "```\nTITLE: Ship the widget\nSUMMARY: One line.\nBENEFICIARY: Rob\n"
    "BENEFIT: Less walking.\nCRITERION: The tile moves.\nINSTRUCTIONS: Do it.\n"
    "SPECIALISTS:\n- bc-implementer\n- bc-verifier\nFOLDER: /Users/x/Code/alpha\n"
    "AREA: Pocket\n```",
    # list-marker title, quoted summary, NONE objective
    "TITLE: - \"Quoted title\"\nSUMMARY: 'A summary.'\nBENEFICIARY: NONE\n"
    "BENEFIT: none\nCRITERION:\nINSTRUCTIONS: * Start here.\nSPECIALISTS: NONE\n",
    # over-cap objective
    "TITLE: T\nSUMMARY: S\nBENEFICIARY: " + "x" * 201 + "\nBENEFIT: b\n"
    "CRITERION: c\nINSTRUCTIONS: Go.\nSPECIALISTS: NONE\n",
    # missing INSTRUCTIONS → all empty
    "TITLE: T\nSUMMARY: S\nSPECIALISTS: bc-implementer\n",
    # first-line-only title, multi-line summary
    "TITLE: First\nsecond\nSUMMARY: One\nTwo\nINSTRUCTIONS: 1. Numbered start.\n"
    "More.\nSPECIALISTS: NONE",
    # specialists with a trailing parenthetical, an off-roster name, a
    # first-token match and a duplicate
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS:\n"
    "- BC-Implementer (build)\n- bc-nobody\n- bc-verifier checks it\n"
    "- bc-implementer\n* bc-security-reviewer (a (nested) one)\n",
    # FOLDER: basename-unique, exact, ambiguous, off-list, NONE
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\nFOLDER: alpha\n",
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\n"
    "FOLDER: /USERS/X/CODE/BETA\n",
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\nFOLDER: beta\n",
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\nFOLDER: /nope\n",
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\nFOLDER: NONE\n",
    # chatty FOLDER inside INSTRUCTIONS, then the real one last
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Put it in FOLDER: /Users/x/Code/beta "
    "please.\nSPECIALISTS: NONE\nFOLDER: - \"alpha\"\nAREA: gate\n",
    # 15-word title, 81-char title, leading-dash prompt
    "TITLE: " + " ".join(["w"] * 15) + "\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\n",
    "TITLE: " + "t" * 81 + "\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\n",
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: -flag first\nSPECIALISTS: NONE\n",
    # over-long prompt
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: " + "p" * 4001 + "\nSPECIALISTS: NONE\n",
    # CRLF line ends, unicode, and an AREA the table does not know
    "TITLE: Ünïcode tïtle\r\nSUMMARY: Ça va.\r\nINSTRUCTIONS: Fais-le.\r\n"
    "SPECIALISTS: NONE\r\nAREA: kitchen\r\n",
    # empty and whitespace
    "",
    "   \n\n  ",
    # DEPENDS ON: an exact title and a decorated, quoted one, then AREA
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\n"
    "DEPENDS ON:\n- Build the pipeline\n- \"night LIGHTING\"\n* c3\n"
    "AREA: Pocket\n",
    # DEPENDS ON: NONE plus an unknown line, and a duplicate title offered
    # twice (resolves to nothing)
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS: NONE\n"
    "DEPENDS ON:\nNONE\nA card nobody offered\nTwin\nAREA: gate\n",
    # a DEPENDS ON line led by a roster name must not become a specialist
    "TITLE: T\nSUMMARY: S\nINSTRUCTIONS: Do.\nSPECIALISTS:\n- bc-implementer\n"
    "DEPENDS ON:\nbc-verifier misses stale counts\nAREA: gate\n",
]

#: The closed list the DEPENDS ON reader is given in every golden, `[id, title]`.
CANDIDATES = [["c1", "Build the pipeline"], ["c2", "Night lighting"],
              ["c3", "Fix the door"], ["c4", "Twin"], ["c5", "twin"]]

PROMPT_FIXTURES = [
    dict(idea="  Make   the widget\n\tmove  ", tool="claude", project="alpha",
         roster=ROSTER, roots=ROOTS),
    dict(idea="x" * 2500, tool="", project="", roster=[], roots=["/only"]),
    dict(idea="Add the night lighting", tool="claude", project="alpha",
         roster=ROSTER, roots=ROOTS, candidates=CANDIDATES),
]


def _literal(source: str, begin: str, end: str) -> str:
    block = source[source.index(begin):source.index(end)]
    match = re.search(r'"""\n(.*)\n"""', block, re.S)
    assert match, begin
    # Swift keeps every line between the delimiters and drops only the
    # newline that precedes the closing one: the captured text *is* the
    # literal, and the blank line before `"""` is the trailing newline.
    return match.group(1)


def test_the_brief_and_the_mode_head_are_the_macs_byte_for_byte():
    source = BRIEF_SWIFT.read_text()
    assert _literal(source, "// --- brief begin", "// --- brief end") \
        == card_preparer_brief.BRIEF
    assert _literal(source, "// --- mode head begin", "// --- mode head end") \
        == card_prepare.MODE_HEAD_IDEA
    assert f'static let name = "{card_preparer_brief.NAME}"' in source


def test_the_rules_file_is_foundation_only():
    source = RULES_SWIFT.read_text()
    assert source.startswith("// --- rules begin\nimport Foundation\n")
    assert source.rstrip().endswith("// --- rules end")
    assert source.count("import ") == 1
    for stranger in ("Specialists.", "Areas.", "PhoneClient", "SwiftUI", "UIKit"):
        assert stranger not in source, stranger


HARNESS = r'''
struct In: Decodable {
    let mode: String
    let raw: String?
    let roster: [String]
    let roots: [String]
    let areas: [[String]]
    let areaKeys: [[String]]
    let idea: String?
    let tool: String?
    let project: String?
    let modeHead: String?
    let candidates: [[String]]
    let rows: [[String]]?
    let root: String?
}
let input = try! JSONDecoder().decode(In.self, from: FileHandle.standardInput.readDataToEndOfFile())
var out: [String: Any] = [:]
if input.mode == "prompt" {
    out["prompt"] = CardPrepareRules.promptForIdea(
        modeHead: input.modeHead!, idea: input.idea!, tool: input.tool!,
        project: input.project!, roster: input.roster, roots: input.roots,
        candidates: input.candidates.map { $0[1] },
        areas: input.areas.map { (name: $0[0], concept: $0[1]) })
} else if input.mode == "candidates" {
    out["candidates"] = CardPrepareRules.candidates(
        from: input.rows!.map { (id: $0[0], title: $0[1], root: $0[2], column: $0[3]) },
        root: input.root!).map { [$0.id, $0.title] }
} else {
    let raw = input.raw!
    let idea = CardPrepareRules.parseIdea(raw, roster: input.roster)
    let objective = CardPrepareRules.parseObjective(raw)
    out["title"] = idea.title
    out["summary"] = idea.summary
    out["prompt"] = idea.prompt
    out["stages"] = idea.stages
    out["objective"] = objective
    out["folder"] = CardPrepareRules.parseFolder(raw, roots: input.roots)
    out["area"] = CardPrepareRules.parseArea(raw, areas: input.areaKeys.map { (slug: $0[0], name: $0[1]) })
    out["dependencies"] = CardPrepareRules.parseDependencies(
        raw, candidates: input.candidates.map { (id: $0[0], title: $0[1]) })
    out["title_refusal"] = CardPrepareRules.titleRefusal(idea.title) ?? ""
    out["summary_refusal"] = CardPrepareRules.summaryRefusal(idea.summary) ?? ""
    out["objective_refusal"] = CardPrepareRules.objectiveRefusal(objective) ?? ""
    out["prompt_refusal"] = CardPrepareRules.promptRefusal(idea.prompt) ?? ""
}
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    source = RULES_SWIFT.read_text()
    sliced = source[source.index("// --- rules begin"):source.index("// --- rules end")]
    tmp = tmp_path_factory.mktemp("prepare-probe")
    path = tmp / "PrepareProbe.swift"
    path.write_text(sliced + "\n" + HARNESS)
    executable = tmp / "PrepareProbe"
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr

    def run(payload: dict) -> dict:
        base = {"roster": ROSTER, "roots": ROOTS, "areas": AREAS,
                "areaKeys": AREA_KEYS, "candidates": CANDIDATES}
        base.update(payload)
        ran = subprocess.run([str(executable)], input=json.dumps(base),
                             capture_output=True, text=True, timeout=30)
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)

    return run


def _python_reading(raw: str) -> dict:
    roster = [(name, "") for name in ROSTER]
    title, summary, prompt, stages = card_prepare.parse_idea(raw, roster=roster)
    objective = card_prepare.parse_objective(raw)
    return {
        "title": title, "summary": summary, "prompt": prompt, "stages": stages,
        "objective": objective,
        "folder": card_prepare.parse_folder(raw, ROOTS),
        "area": card_prepare.parse_area(raw),
        "dependencies": card_prepare.parse_dependencies(
            raw, [tuple(c) for c in CANDIDATES]),
        "title_refusal": card_prepare.title_refusal(title) or "",
        "summary_refusal": card_prepare.summary_refusal(summary) or "",
        "objective_refusal": card_prepare.objective_refusal(objective) or "",
        # The phone leaves the per-tool subcommand rule to `dispatch.guard`
        # at Start; with no tool named the Mac's `refusal` adds the union
        # rule, which no golden here trips (none is a single word).
        "prompt_refusal": card_prepare.refusal(prompt, "") or "",
    }


@pytest.mark.parametrize("raw", GOLDEN_ANSWERS, ids=range(len(GOLDEN_ANSWERS)))
def test_both_readers_agree_on_every_golden(probe, raw):
    assert probe({"mode": "read", "raw": raw}) == _python_reading(raw)


@pytest.mark.parametrize("fixture", PROMPT_FIXTURES,
                         ids=("plain", "bounds", "candidates"))
def test_the_prompt_is_the_macs_byte_for_byte(probe, fixture):
    candidates = fixture.get("candidates", [])
    expected = card_prepare.prompt_for(
        title="", summary="", tool=fixture["tool"], project=fixture["project"],
        roster=[(n, "") for n in fixture["roster"]], idea=fixture["idea"],
        roots=fixture["roots"], candidates=[tuple(c) for c in candidates])
    got = probe({"mode": "prompt", "modeHead": card_prepare.MODE_HEAD_IDEA,
                 "idea": fixture["idea"], "tool": fixture["tool"],
                 "project": fixture["project"], "roster": fixture["roster"],
                 "roots": fixture["roots"], "candidates": candidates})
    assert got["prompt"] == expected


def test_the_key_never_rides_anything_the_phone_sends():
    """The Anthropic URL appears once, and nothing that talks to the Mac,
    the relay, the widget, the outbox or a saved file names the key store."""
    sources = {p.name: p.read_text() for p in IOS.glob("*.swift")}
    hits = [name for name, text in sources.items()
            if "api.anthropic.com" in text]
    assert hits == ["AnthropicPrepare.swift"]
    readers = [name for name, text in sources.items()
               if "AnthropicKeyStore.load(" in text]
    assert readers == ["AnthropicPrepare.swift"]
    for name in ("Client.swift", "RelayTransport.swift", "HomeTransport.swift",
                 "Outbox.swift", "Receipts.swift", "CardCache.swift",
                 "HeldPicture.swift", "Push.swift", "BackgroundRefresh.swift"):
        assert "AnthropicKeyStore" not in sources[name], name
    widget = (ROOT / "ios" / "BobPhoneWidget").glob("*.swift")
    for path in widget:
        assert "Anthropic" not in path.read_text(), path.name


def test_the_candidate_list_is_built_by_one_rule_on_both_sides(probe):
    rows = [
        {"id": "a", "title": "Still working", "root": "/p", "column": "prep"},
        {"id": "b", "title": "Finished", "root": "/p", "column": "done"},
        {"id": "c", "title": "Elsewhere", "root": "/q", "column": "prep"},
        {"id": "d", "title": "Twin", "root": "/p", "column": "prep"},
        {"id": "e", "title": "twin", "root": "/p", "column": "backlog"},
        {"id": "f", "title": "SUMMARY: reads as a label", "root": "/p",
         "column": "prep"},
        {"id": "g", "title": "L" * 300, "root": "/p", "column": "prep"},
        {"id": "h", "title": "  spaced   out  ", "root": "/p", "column": "prep"},
    ] + [{"id": f"n{i}", "title": f"Card {i:03d}", "root": "/p",
          "column": "prep"} for i in range(60)]
    got = probe({"mode": "candidates", "root": "/p",
                 "rows": [[r["id"], r["title"], r["root"], r["column"]]
                          for r in rows]})
    cards = [{"id": r["id"], "title": r["title"], "root": r["root"],
              "column_name": r["column"]} for r in rows]
    # The daemon's own builder, over the same cards.
    from dark_army_daemon.daemon import BobDaemon
    daemon = BobDaemon.__new__(BobDaemon)
    daemon._board_state = {"cards": cards}
    expected = [list(pair) for pair in daemon._dependency_candidates("/p")]
    assert got["candidates"] == expected
    assert len(expected) == card_prepare.MAX_DEPENDENCY_CHOICES
    assert expected[0] == ["a", "Still working"]
