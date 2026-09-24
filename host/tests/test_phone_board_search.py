"""The Board tab is searched from a line at its top.

A `>` field on the phone's Board tab narrows the snapshot the phone already
holds — title, summary, project — without a request to the Mac. The rule is
Foundation-only and is run here under `swiftc` over the same table
`ios/BobPhoneTests/BoardSearchTests.swift` holds; the wiring is pinned by
source greps. Contract: `docs/phone-contract.md`, *The Board tab is searched
from a line at its top*.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
SEARCH = PHONE / "BoardSearch.swift"
BOARD = PHONE / "BoardView.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
XCTEST = ROOT / "ios" / "BobPhoneTests" / "BoardSearchTests.swift"
CONTRACT = ROOT / "docs" / "phone-contract.md"

HARNESS = r'''
import Foundation

struct Case: Decodable {
    var query: String
    var title: String
    var summary: String
    var project: String
}

let data = FileHandle.standardInput.readDataToEndOfFile()
let cases = try! JSONDecoder().decode([Case].self, from: data)
for c in cases {
    let hit = BoardSearch.matches(query: c.query, title: c.title,
                                  summary: c.summary, project: c.project)
    print("MATCH \(hit ? "true" : "false")")
}
print("LINE \(BoardSearch.noMatchLine(query: "  zzqx  "))")
print("PLACEHOLDER \(BoardSearch.placeholder)")
print("SEARCHING_EMPTY \(BoardSearch.isSearching("") ? "true" : "false")")
print("SEARCHING_BLANK \(BoardSearch.isSearching("   ") ? "true" : "false")")
print("SEARCHING_WORD \(BoardSearch.isSearching("vex") ? "true" : "false")")
'''


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines()
                     if not line.strip().startswith("//"))


@pytest.fixture(scope="module")
def search_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-board-search")
    main = folder / "main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-board-search"
    built = subprocess.run(
        [swiftc, str(SEARCH), str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    return binary


def _run(binary: pathlib.Path, cases: list[dict]) -> tuple[list[bool], dict[str, str]]:
    proc = subprocess.run([str(binary)], input=json.dumps(cases),
                          capture_output=True, text=True, timeout=30,
                          check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    hits: list[bool] = []
    extras: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if line.startswith("MATCH "):
            hits.append(line.split()[1] == "true")
        else:
            key, _, rest = line.partition(" ")
            extras[key] = rest
    return hits, extras


# (query, title, summary, project, expected)
CASES = [
    ("refine", "Refine the board", "", "", True),
    ("one-line", "", "a one-line summary", "", True),
    ("dark-army", "", "", "dark-army", True),
    ("zOsIa", "Zosia's card", "", "", True),
    ("cafe", "café", "", "", True),
    ("  vex ", "Vex", "", "", True),
    ("", "anything", "x", "y", True),
    ("   ", "anything", "x", "y", True),
    ("zzqx", "title", "summary", "project", False),
    ("refine", "other", "elsewhere", "desk", False),
]


def test_a_title_fragment_matches_and_an_unrelated_one_does_not(search_bin):
    """Typing part of a card's title narrows the board to that card."""
    payload = [
        {"query": q, "title": t, "summary": s, "project": p}
        for q, t, s, p, _ in CASES
    ]
    hits, extras = _run(search_bin, payload)
    assert hits == [want for *_, want in CASES]
    assert extras["SEARCHING_EMPTY"] == "false"
    assert extras["SEARCHING_BLANK"] == "false"
    assert extras["SEARCHING_WORD"] == "true"


def test_matching_ignores_case_and_diacritics(search_bin):
    payload = [
        {"query": "zOsIa", "title": "Zosia's card", "summary": "", "project": ""},
        {"query": "cafe", "title": "café", "summary": "", "project": ""},
    ]
    hits, _ = _run(search_bin, payload)
    assert hits == [True, True]


def test_whitespace_around_the_query_is_trimmed(search_bin):
    hits, extras = _run(search_bin, [
        {"query": "  vex ", "title": "Vex", "summary": "", "project": ""},
    ])
    assert hits == [True]
    assert extras["LINE"] == "“zzqx” is not in a title, a summary or a project."


def test_an_empty_or_blank_query_matches_everything_and_is_not_a_search(search_bin):
    """Clearing the field brings the whole board back."""
    payload = [
        {"query": "", "title": "anything", "summary": "x", "project": "y"},
        {"query": "   ", "title": "anything", "summary": "x", "project": "y"},
    ]
    hits, extras = _run(search_bin, payload)
    assert hits == [True, True]
    assert extras["SEARCHING_EMPTY"] == "false"
    assert extras["SEARCHING_BLANK"] == "false"


def test_no_field_carrying_the_words_is_no_match(search_bin):
    hits, _ = _run(search_bin, [
        {"query": "zzqx", "title": "title", "summary": "summary", "project": "project"},
    ])
    assert hits == [False]


def test_the_no_match_line_quotes_the_trimmed_query(search_bin):
    _, extras = _run(search_bin, [])
    assert extras["LINE"] == "“zzqx” is not in a title, a summary or a project."


def test_the_placeholder_names_the_three_fields(search_bin):
    _, extras = _run(search_bin, [])
    assert extras["PLACEHOLDER"] == "grep title, summary, project…"


def test_board_search_is_foundation_only():
    text = _read(SEARCH)
    imports = [line for line in text.splitlines() if line.startswith("import ")]
    assert imports == ["import Foundation"]
    for forbidden in ("BoardCard", "SwiftUI", "PhoneClient", "URLSession"):
        assert forbidden not in text, forbidden
    assert 'is not in a title, a summary or a project' in text


def test_the_board_view_wiring_passes_searching():
    text = _read(BOARD)
    code = _code(text)
    assert code.count("searching: false") == 0
    assert code.count("searching: searching") == 2
    assert text.count("BoardRowFold.drawnFolded(item.id, flipped: rowFlips,") == 2
    assert text.count('@State private var query = ""') == 1
    assert ("BoardSearch.matches(query: query, title: $0.title, "
            "summary: $0.summary, project: $0.project)") in text
    assert 'TextField("", text: $query' in text
    assert text.count("client.post(") == 1
    assert "URLSession" not in text
    assert ".searchable" not in text
    assert ".lineLimit(" not in text
    assert text.count("AgentChatterView(") == 6
    assert "## The Board tab is searched from a line at its top" in _read(CONTRACT)


def test_the_fold_override_opens_every_row_without_writing_flips():
    text = _read(BOARD)
    heading = text.split("private func rowHeading(for item:", 1)[1].split(
        "private func rowBody(for id: String)", 1)[0]
    assert text.count("rowFlipsJoined = BoardRowFold.encode") == 1
    assert "rowFlipsJoined = BoardRowFold.encode" in heading
    search_field = text.split("private var searchField", 1)[1].split(
        "private func rowHeading(for item:", 1)[0]
    assert "rowFlipsJoined =" not in search_field


def test_both_swift_files_are_registered_in_the_project():
    pbx = _read(PBXPROJ)
    assert sum("BoardSearch.swift" in line for line in pbx.splitlines()) == 4
    assert sum("BoardSearchTests.swift" in line for line in pbx.splitlines()) == 2
    assert pbx.count("E5A5C0DE0000000000000505") == 3
    assert pbx.count("E5A5C0DE0000000000000506") == 2
    assert pbx.count("E5A5C0DE0000000000000507") == 3
    assert pbx.count("E5A5C0DE0000000000000508") == 2
    assert XCTEST.exists()


def test_xctest_names_the_seven_cases():
    xctest = _read(XCTEST)
    for case in (
        "testTitleSummaryAndProjectEachMatch",
        "testMatchingIgnoresCaseAndDiacritics",
        "testWhitespaceAroundTheQueryIsTrimmed",
        "testAnEmptyOrBlankQueryMatchesEverythingAndIsNotASearch",
        "testNoFieldCarryingTheWordsIsNoMatch",
        "testTheNoMatchLineQuotesTheTrimmedQuery",
        "testThePlaceholderNamesTheThreeFields",
    ):
        assert f"func {case}()" in xctest, case


def test_no_new_mac_request_was_added():
    text = _read(BOARD)
    assert text.count("client.post(") == 1
    assert re.search(r"URLSession|\.searchable", text) is None


def test_the_search_has_three_ways_out():
    """Reported 21 Sep 2026: a search opened over an empty query had no
    exit — the Search key did nothing, the CLEAR button only existed once
    something was typed, and the rows did not put the keyboard away. Three
    ways out now: the Search key drops focus and keeps the query, the one
    button reads DONE over an empty query and CLEAR over a typed one (both
    drop focus), and a drag on the rows dismisses the keyboard."""
    text = _read(BOARD)
    search_field = text.split("private var searchField", 1)[1].split(
        "private func rowHeading(for item:", 1)[0]
    assert ".onSubmit { searchFocused = false }" in search_field
    assert "if searchFocused || !query.isEmpty {" in search_field
    assert 'DecryptButton(query.isEmpty ? "× DONE" : "× CLEAR")' in search_field
    assert 'accessibilityLabel(query.isEmpty ? "Hide the keyboard"' in search_field
    columns = text.split("private var columnsBody", 1)[1].split(
        "private var searchField", 1)[0]
    assert ".scrollDismissesKeyboard(.interactively)" in columns
