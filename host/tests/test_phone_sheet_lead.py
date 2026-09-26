"""The agent sheet's half height is a glance: the rules run under swiftc.

`ios/BobPhone/AgentSheetLead.swift` holds the Foundation-only rules the agent
sheet's lead and conversation read — the still's size per detent, the status
line under the name, the context figure, the question's words, and the fold
that drops tool-result rows and gathers runs of tool calls into one line.
They are compiled here beside `PhoneSheet.swift` and `ConversationModels.swift`
and executed, case by case; the phone job runs the same table
(`AgentSheetLeadTests.swift`). The source pins below hold the views to them.
"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests.test_detail_tabs import _block, _code

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
RULES = PHONE / "AgentSheetLead.swift"
DETAIL = PHONE / "AgentDetailView.swift"
CONVERSATION = PHONE / "ConversationView.swift"
HOST = PHONE / "PhoneSheetHost.swift"
DECRYPT = PHONE / "DecryptFeedback.swift"
PROJECT = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

# `ConversationModels.swift` decodes through the tolerant helper that lives in
# `Models.swift`, which is SwiftUI-free but large; the one extension it needs
# is restated here so the rules compile alone.
SHIM = r'''
import Foundation
extension KeyedDecodingContainer {
    func value<T: Decodable>(_ key: Key, _ fallback: T) -> T {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) ?? fallback
    }
}
'''

MAIN = r'''
import Foundation

func t(_ seq: Int, _ kind: String, _ tool: String = "") -> ConversationTurn {
    ConversationTurn(seq: seq, kind: kind, tool: tool)
}

func shape(_ rows: [ConversationFold.Row]) -> [String] {
    rows.map {
        switch $0 {
        case .turn(let turn): return "turn:\(turn.kind)"
        case .run(let tools): return "run(\(tools.count))"
        }
    }
}

let mixed = [t(0, "user"), t(1, "agent"), t(2, "tool", "Bash"), t(3, "result"),
             t(4, "tool", "Read"), t(5, "result"), t(6, "agent"),
             t(7, "tool", "Grep"), t(8, "user")]

switch CommandLine.arguments[1] {
case "still_sizes":
    precondition(AgentSheetLead.stillSize(detent: .medium) == 40)
    precondition(AgentSheetLead.stillSize(detent: .large) == 96)
    precondition(AgentSheetLead.stillSize(detent: nil) == 96)
    precondition(AgentSheetLead.compactStill < AgentSheetLead.fullStill)
case "status_line_joins":
    precondition(AgentSheetLead.statusLine(head: "Working — running Bash", age: "1h",
                                           ctx: "ctx 20%↑")
                 == "Working — running Bash · 1h · ctx 20%↑")
case "status_line_drops_empties":
    precondition(AgentSheetLead.statusLine(head: "Idle in the editor", age: "",
                                           ctx: "") == "Idle in the editor")
    precondition(AgentSheetLead.statusLine(head: "", age: "5m", ctx: " ") == "5m")
    precondition(AgentSheetLead.statusLine(head: "", age: "", ctx: "") == "")
case "status_line_drops_undated_dash":
    precondition(AgentSheetLead.statusLine(head: "Stopped, waiting for you", age: "—",
                                           ctx: "ctx 3%")
                 == "Stopped, waiting for you · ctx 3%")
case "ctx_text":
    precondition(AgentSheetLead.ctxText(pct: nil, marker: "↑") == "")
    precondition(AgentSheetLead.ctxText(pct: 19.6, marker: "↑") == "ctx 20%↑")
    precondition(AgentSheetLead.ctxText(pct: 0, marker: "") == "ctx 0%")
case "question_trims_and_joins":
    precondition(AgentSheetLead.questionText(["  Which one?\n", "", "   ", "And why?"])
                 == "Which one?\n\nAnd why?")
    precondition(AgentSheetLead.questionText([]) == "")
    precondition(AgentSheetLead.questionText(["only"]) == "only")
case "rows_fold_a_mixed_list":
    let rows = ConversationFold.rows(mixed)
    precondition(shape(rows) == ["turn:user", "turn:agent", "run(2)", "turn:agent",
                                 "turn:tool", "turn:user"], "\(shape(rows))")
    precondition(rows[2].id == 2)
    precondition(rows.map(\.id) == [0, 1, 2, 6, 7, 8])
case "rows_never_draw_a_result":
    let rows = ConversationFold.rows(mixed + [t(9, "result"), t(10, "result")])
    for row in rows {
        if case .turn(let turn) = row { precondition(turn.kind != "result") }
        if case .run(let tools) = row { precondition(tools.allSatisfy { $0.kind == "tool" }) }
    }
    precondition(ConversationFold.rows([t(0, "result")]).isEmpty)
case "rows_min_run":
    let rows = ConversationFold.rows(mixed, minRun: 3)
    precondition(shape(rows) == ["turn:user", "turn:agent", "turn:tool", "turn:tool",
                                 "turn:agent", "turn:tool", "turn:user"], "\(shape(rows))")
    precondition(ConversationFold.minRun == 2)
case "rows_tail_run_keeps_its_id":
    let short = ConversationFold.rows([t(0, "agent"), t(1, "tool", "Bash"), t(2, "tool", "Bash")])
    let longer = ConversationFold.rows([t(0, "agent"), t(1, "tool", "Bash"), t(2, "tool", "Bash"),
                                        t(3, "result"), t(4, "tool", "Read")])
    precondition(short.last?.id == 1 && longer.last?.id == 1)
    precondition(shape(longer) == ["turn:agent", "run(3)"])
case "summary_counts_in_first_seen_order":
    let tools = [t(0, "tool", "Bash"), t(1, "tool", "Read"), t(2, "tool", "Bash"),
                 t(3, "tool", "Grep"), t(4, "tool", "Bash")]
    precondition(ConversationFold.summary(tools) == "⚙ 5 tool calls · Bash ×3, Read, Grep",
                 ConversationFold.summary(tools))
    precondition(ConversationFold.spoken(tools) == "5 tool calls: Bash 3 times, Read, Grep")
case "summary_names_a_nameless_tool":
    precondition(ConversationFold.summary([t(0, "tool", ""), t(1, "tool", " ")])
                 == "⚙ 2 tool calls · tool ×2")
case "status_line_spoken":
    precondition(AgentSheetLead.spokenCtx(pct: 19.6, pace: "rising") == "context 20 percent, rising")
    precondition(AgentSheetLead.spokenCtx(pct: 3, pace: "") == "context 3 percent")
    precondition(AgentSheetLead.spokenCtx(pct: nil, pace: "rising") == "")
    precondition(AgentSheetLead.spokenStatus(head: "Working — running Bash", age: "1 hour",
                                             ctx: "context 20 percent, rising")
                 == "Working — running Bash, 1 hour, context 20 percent, rising")
    precondition(AgentSheetLead.spokenStatus(head: "Idle in the editor", age: "", ctx: "")
                 == "Idle in the editor")
    precondition(!AgentSheetLead.spokenStatus(head: "a", age: "1h", ctx: "context 2 percent, rising")
                 .contains("↑"))
case "status_line_live_bucket":
    precondition(AgentSheetLead.liveBucket([("waiting", false), ("running", true),
                                            ("sleeping", false), ("finished", false),
                                            ("abandoned", false)]) == "running")
    precondition(AgentSheetLead.liveBucket([("waiting", true), ("running", true)]) == "waiting")
    precondition(AgentSheetLead.liveBucket([("waiting", false), ("running", false)]) == nil)
    precondition(AgentSheetLead.liveBucket([]) == nil)
case "rows_run_keeps_an_expanded_call_open":
    let tools = [t(2, "tool", "Bash"), t(4, "tool", "Read")]
    precondition(!ConversationFold.runIsOpen(tools, id: 2, openRuns: [], expanded: []))
    precondition(ConversationFold.runIsOpen(tools, id: 2, openRuns: [2], expanded: []))
    precondition(ConversationFold.runIsOpen(tools, id: 2, openRuns: [], expanded: [2]))
    precondition(ConversationFold.runIsOpen(tools, id: 2, openRuns: [], expanded: [4]))
    precondition(!ConversationFold.runIsOpen(tools, id: 2, openRuns: [9], expanded: [7]))
default:
    fatalError("unknown case \(CommandLine.arguments[1])")
}
print("ok")
'''

CASES = [
    "still_sizes",
    "status_line_joins",
    "status_line_drops_empties",
    "status_line_drops_undated_dash",
    "ctx_text",
    "question_trims_and_joins",
    "rows_fold_a_mixed_list",
    "rows_never_draw_a_result",
    "rows_min_run",
    "rows_tail_run_keeps_its_id",
    "summary_counts_in_first_seen_order",
    "summary_names_a_nameless_tool",
    "status_line_spoken",
    "status_line_live_bucket",
    "rows_run_keeps_an_expanded_call_open",
]


@pytest.fixture(scope="module")
def rules_binary(tmp_path_factory):
    swift = shutil.which("swiftc")
    if swift is None:
        pytest.skip("Swift toolchain unavailable")
    folder = tmp_path_factory.mktemp("sheet-lead")
    (folder / "shim.swift").write_text(SHIM)
    (folder / "main.swift").write_text(MAIN)
    binary = folder / "rules"
    subprocess.run([swift, str(PHONE / "PhoneSheet.swift"),
                    str(PHONE / "ConversationModels.swift"), str(RULES),
                    str(folder / "shim.swift"), str(folder / "main.swift"),
                    "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("case", CASES)
def test_the_rule_table_runs(rules_binary, case):
    out = subprocess.run([str(rules_binary), case], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# --- the source holds the views to the rules ---------------------------------


def test_the_rules_file_is_foundation_only():
    text = RULES.read_text()
    assert "import Foundation" in text
    assert "import SwiftUI" not in text and "PresentationDetent" not in text


def test_both_new_files_are_in_the_project():
    project = PROJECT.read_text()
    assert project.count("AgentSheetLead.swift in Sources") == 1
    assert project.count("AgentSheetLeadTests.swift in Sources") == 1


def test_the_conversation_folds_its_rows_and_scrolls_by_them():
    conv = CONVERSATION.read_text()
    assert conv.count("ConversationFold.rows(") == 1
    assert "ForEach(drawn)" not in conv
    messages = conv.split("var body: some View", 1)[1].split(
        "private var earlierControl", 1)[0]
    assert "turns.last" not in messages, "a scroll anchor may name a result with no row"
    assert "ForEach(rows)" in messages
    assert "ConversationRunRow(" in messages
    assert "ConversationFold.summary(tools)" in conv
    assert ".accessibilityLabel(ConversationFold.spoken(tools))" in conv


def test_the_question_rides_the_conversation_as_ask():
    conv = CONVERSATION.read_text()
    # Both inits take the ask as a defaulted argument, beside the property.
    assert conv.count('ask: String = "",') == 2, "both inits take the ask"
    assert 'var ask: String = ""\n' in conv
    assert "self.ask = ask" in conv and "ask: ask," in conv
    body = conv.split("var body: some View", 1)[1].split(
        "@ViewBuilder private func rowView", 1)[0]
    answer = body.split(".id(Self.answerAnchor)", 1)[0].rsplit("ForEach(rows)", 1)[1]
    assert answer.index("Text(ask)") < answer.index("AnswerBox(")
    detail = _code(_block(DETAIL.read_text(), "var body: some View"))
    assert "ask: AgentSheetLead.questionText(" in detail
    assert "header:" not in detail
    assert detail.index("ask: AgentSheetLead.questionText(") < detail.index("typing: keyboardUp)")


def test_the_sheet_frame_tells_its_content_the_detent_and_hosts_the_caption():
    host = HOST.read_text()
    assert "var phoneSheetDetent: SheetDetent?" in host
    assert ".environment(\\.phoneSheetDetent, detent)" in host
    assert ".environment(\\.decryptCaptionHost, captionHost)" in host
    assert host.count("DecryptCaption(") == 1
    assert "glyphs != nil ? Theme.phosphor : Theme.hair" in host
    detail = DETAIL.read_text()
    assert "@Environment(\\.phoneSheetDetent) private var sheetDetent" in detail
    assert "AgentSheetLead.stillSize(detent: sheetDetent)" in detail


def test_a_surface_under_a_host_mounts_no_strip():
    ui = DECRYPT.read_text()
    surface = ui.split("struct DecryptSurface", 1)[1]
    assert "host == nil" in surface
    assert "host?.surface = identity" in surface
    assert "if host?.surface == identity { host?.surface = nil }" in surface
    assert ".onDisappear { releaseHost()" in surface


def test_the_lead_is_name_status_and_card():
    text = DETAIL.read_text()
    identity = _code(_block(text, "private var identity: some View"))
    assert "AgentSheetLead.statusLine(" in identity
    assert "PhoneAgentFacts.head(agent: agent, category: liveCategory)" in identity
    # VoiceOver hears words, not the pace arrow.
    assert ".accessibilityLabel(AgentSheetLead.spokenStatus(" in identity
    assert "agent.trend.pace?.spoken" in identity
    assert "client.snapshot.generatedAt" in identity
    assert "Date()" not in identity
    assert ".lineLimit(" not in identity
    rest = _code(_block(text, "private var rest: some View"))
    assert "Text(quote)" in rest and "originLead" in rest
    lead = _code(_block(text, "private var mainScreen: some View"))
    assert "originLead" not in lead
    assert "size: leadStill" in lead


def test_the_quiet_verbs_share_one_row():
    text = DETAIL.read_text()
    quiet = _code(_block(text, "private var quietVerbs: some View"))
    assert "AdaptiveStack(" in quiet
    for label in ("closeLabel", "stopLabel", "deleteLabel", "Verbs.hide.label"):
        assert label in quiet, label
    assert "closeBox" in quiet
    ask = _code(_block(text, "private var askVerbs: some View"))
    for needle in ("ForEach(cards)", "ForEach(prompts)", "lowPriorityBox"):
        assert needle in ask, needle
    verbs = _code(_block(text, "private var verbs: some View"))
    assert verbs.index("askVerbs") < verbs.index("quietVerbs")
    close = _code(_block(text, "private var closeBox: some View"))
    assert "if arm.close != nil || closeInPlay" in close
    assert "DecryptButton(" not in close, "the button is in the row, not the sentence"


def test_the_sheet_reads_the_live_bucket_not_the_seed():
    """The seed `category` is fixed at open (a decision page always passes
    `.waiting`); an agent that starts waiting, or is answered, moves bucket
    while its sheet is up. Every reading and verb gate reads `liveCategory`."""
    text = DETAIL.read_text()
    live = _code(_block(text, "private var liveCategory: Category"))
    assert "AgentSheetLead.liveBucket(" in live
    for bucket in ("waiting", "running", "sleeping", "finished", "abandoned"):
        assert f'("{bucket}", a.{bucket}.contains {{ $0.sessionId == id }})' in live, bucket
    assert "?? category" in live
    view = _code(_block(text, "struct AgentDetailView"))
    # The seed is read in one place only: the fallback.
    # Argument labels and the declaration aside, the seed is read once: the
    # fallback.
    assert "let category: Category" in view
    assert len(re.findall(r"\bcategory\b(?!:)", view)) == 1, "the fallback only"


def test_a_run_holding_an_expanded_call_stays_open():
    conv = CONVERSATION.read_text()
    row = conv.split("case .run(let tools):", 1)[1].split("}", 1)[0]
    assert "ConversationFold.runIsOpen(tools, id: row.id" in row
    close = _code(_block(conv, "private func toggleRun("))
    assert "expanded.remove(tool.seq)" in close

