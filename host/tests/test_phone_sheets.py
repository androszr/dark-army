"""Phone sheet policy executes under swiftc; source pins guard its UI wiring."""
from collections import Counter
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_detail_tabs import _block, _code

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
CONVERTED = ("NeedsYouView", "FleetView", "RecentlyView", "BoardView",
             "PipelineView", "CardDetailView", "AgentDetailView",
             "WorkRecordView", "CatchUpView")


@pytest.fixture(scope="module")
def rules_binary(tmp_path_factory):
    swift = shutil.which("swiftc")
    if swift is None:
        pytest.skip("Swift toolchain unavailable")
    folder = tmp_path_factory.mktemp("phone-sheets")
    main = folder / "main.swift"
    main.write_text('''
import Foundation
let kind = PhoneSheetKind(rawValue: CommandLine.arguments[1])!
let accessibility = CommandLine.arguments[2] == "true"
let expected: SheetDetent = accessibility || kind != .agent ? .large : .medium
precondition(PhoneSheetKind.initialDetent(kind, accessibility: accessibility) == expected)
precondition(PhoneSheetKind.detents(kind, accessibility: accessibility) ==
             (expected == .medium ? [.medium, .large] : [.large]))
precondition(PhoneSheetKind.onKeyboard(.medium) == .large)
precondition(PhoneSheetKind.onKeyboard(.large) == .large)
precondition(PhoneSheetKind.presentsTerminalFullScreen(kind) == (kind == .agent))
precondition(PhoneSheetKind.allCases.count == 6)
''')
    binary = folder / "rules"
    subprocess.run([swift, str(PHONE / "PhoneSheet.swift"), str(main), "-o", str(binary)],
                   check=True, capture_output=True, text=True)
    return binary


@pytest.mark.parametrize("kind", ["agent", "card", "catchUp", "decision", "workFile", "notification"])
@pytest.mark.parametrize("accessibility", ["false", "true"])
def test_executed_detent_and_terminal_policy(rules_binary, kind, accessibility):
    subprocess.run([str(rules_binary), kind, accessibility], check=True)


def test_one_sheet_and_one_full_screen_terminal():
    sheets, covers, links = [], [], []
    for path in PHONE.glob("*.swift"):
        code = _code(path.read_text())
        sheets.extend([path.name] * code.count(".sheet("))
        covers.extend([path.name] * code.count(".fullScreenCover("))
        links.extend([path.name] * code.count("NavigationLink"))
    assert sheets == ["BobPhoneApp.swift"]
    # Two covers, both the terminal: the agent sheet's and the Comm tab's
    # (Mission Control's live screen; the tab is a root, not a sheet, so
    # `terminalPresented` is untouched by it).
    assert sorted(covers) == ["AgentDetailView.swift", "CommView.swift"]
    # Profile links to the knowledge notes and to the phone doors' access log.
    # The composer links to Profile ("Add a key under Profile").
    assert Counter(links) == Counter({"BoardView.swift": 1, "UsageView.swift": 2,
                                      "ProfileView.swift": 2,
                                      "ComposerView.swift": 1})


@pytest.mark.parametrize("name", CONVERTED)
def test_every_converted_surface_uses_shared_router(name):
    code = _code((PHONE / f"{name}.swift").read_text())
    assert "@EnvironmentObject private var sheets: PhoneSheetRouter" in code
    assert "sheets.show(" in code
    assert "DecryptButton(action:" in code


def test_pure_rules_and_project_registration():
    pure = _code((PHONE / "PhoneSheet.swift").read_text())
    assert "import Foundation" in pure
    assert "SwiftUI" not in pure
    assert pure.count("static func ") == 4
    project = (ROOT / "ios/BobPhone.xcodeproj/project.pbxproj").read_text()
    for name in ["PhoneSheet", "PhoneSheetHost", "SheetPresentationTests"]:
        assert project.count(f"{name}.swift in Sources") == 1
    for name in ["PhoneSheet", "PhoneSheetHost"]:
        assert "DetailTab" not in (PHONE / f"{name}.swift").read_text()


def test_frame_reaims_and_grows_without_collapsing_on_keyboard_hide():
    frame = _code((PHONE / "PhoneSheetHost.swift").read_text())
    for needle in [".safeAreaInset(edge: .top", ".presentationDetents(",
                   ".presentationDragIndicator(.visible)", ".presentationBackground(Theme.bg)",
                   ".overlay(ScanlineOverlay())", ".environment(\\.decryptActive, true)",
                   ".onChange(of: router.topState?.id)", ".onChange(of: dynamicTypeSize)",
                   ".id(router.topState?.id)", "PhoneSheet.initialDetent(", "PhoneSheet.detents(",
                   "detent = PhoneSheet.onKeyboard(detent)", "Back to ", "Text(\"Close\")"]:
        assert needle in frame, needle
    assert "guard !router.terminalPresented else { return }" in frame
    assert "offeredDetents" in frame
    assert "PhoneSheet.detents(" in frame
    assert "@Published var terminalPresented" not in frame
    assert "var terminalPresented = false" in frame
    assert frame.count("UIResponder.keyboardWillShowNotification") == 1
    assert "keyboardWillHideNotification" not in frame
    assert ".navigationTitle(" not in frame
    assert ".lineLimit(" not in frame


def test_presenter_identity_is_independent_of_the_subject():
    app = _code((PHONE / "BobPhoneApp.swift").read_text())
    router = _code((PHONE / "PhoneSheetHost.swift").read_text())
    assert "@StateObject private var sheets = PhoneSheetRouter()" in app
    assert "get: { sheets.presentation }" in app
    assert "if $0 == nil { sheets.close() }" in app
    assert "sheets.route(route)" in app
    assert "notificationRoute" not in app
    assert "private let presentationIdentity = Presentation()" in router
    assert "top == nil ? nil : presentationIdentity" in router
    assert "@Published private(set) var stack: [PhoneSheet] = []" in router
    assert "static let MAX_DEPTH = 3" in router
    assert "stack = [.notification(receipt)]" in router


def test_question_and_actions_precede_full_detail():
    src = (PHONE / "AgentDetailView.swift").read_text()
    body = _code(_block(src, "var body: some View"))
    assert body.index("cardLead") < body.index("originLead")
    assert body.index("identity") < body.index("cardLead")
    assert body.index("originLead") < body.index("PhoneAgentScreenBar(")
    assert body.index("PhoneAgentScreenBar(") < body.index("ConversationScreen(")
    details = _code(_block(src, "private var detailsScreen: some View"))
    assert "identity" not in details
    assert details.index("questionLead") < details.index("actions") < details.index("rest")
    doc = _code(_block(src, "private func body(for agent: Agent)"))
    assert "questionList" not in doc
    assert "lastSummary" in doc and "lastText" in doc
    question = _code(_block(src, "private var questionLead: some View"))
    assert "agent.questionList" in question
    rest = _code(_block(src, "private var rest: some View"))
    for needle in ["header", "facts", 'Text("cmd', "elsewhereTerminal", "body(for: agent)"]:
        assert needle in rest


def test_notification_retains_generation_guards_and_dismissal_policy():
    src = _code((PHONE / "CatchUpView.swift").read_text())
    for needle in ["router.consume(route)", "router.accepts(route)",
                   ".interactiveDismissDisabled(page == nil || page?.available == false)",
                   "onAvailability(available == true)"]:
        assert needle in src
    assert "NavigationStack" not in src
    assert ".toolbar" not in src


def test_terminal_return_does_not_reinitialize_the_sheet_height():
    frame = _code((PHONE / "PhoneSheetHost.swift").read_text())
    assert "@State private var didAimInitialDetent = false" in frame
    arrival = _block(frame, ".onAppear {")
    assert arrival.index("guard !didAimInitialDetent else { return }") < arrival.index("aimDetent()")
    assert "didAimInitialDetent = true" in arrival
    # New subjects and text sizes still apply their own initial-height policy.
    assert ".onChange(of: router.topState?.id) { _, _ in aimDetent() }" in frame
    assert ".onChange(of: dynamicTypeSize) { _, _ in aimDetent() }" in frame


def test_keyed_views_bind_to_their_retained_entry_instead_of_resaving_on_departure():
    host = _code((PHONE / "PhoneSheetHost.swift").read_text())
    card = _code((PHONE / "CardDetailView.swift").read_text())
    answer = _code((PHONE / "AnswerBox.swift").read_text())
    agent = _code((PHONE / "AgentDetailView.swift").read_text())
    decision = _code((PHONE / "CatchUpView.swift").read_text())
    assert ".id(router.topState?.id)" in host
    assert ".environment(\\.phoneSheetEntry, router.topState)" in host
    assert "retained: router.topState?.cardDraft(for: card.id)" in host
    assert "retained: sheetEntry?.cardDraft(for: card.id)" in decision
    assert "StateObject(wrappedValue: retained ?? PhoneCardDraftState())" in card
    for field in ["draftTitle", "draftSummary", "draftPrompt", "draftPriority", "messageText"]:
        assert f"$retained.{field}" in card
        assert f"nonmutating set {{ retained.{field} = newValue }}" in card
    assert "if draftFor != id {" in card
    assert "if cardFull != nil && !force { return }" in card
    assert "guard let row = answer?.card, row.id == id, !draftTouched" in card
    assert "retainedReply: sheetEntry?.replyDraft(for: agent.sessionId)" in agent
    assert "StateObject(wrappedValue: retainedReply ?? PhoneReplyDraft())" in answer
    assert "text: $replyDraft.text" in answer
    assert "nonmutating set { replyDraft.text = newValue }" in answer
    assert "arm.disarm()" in agent and "dictation.stop()" in agent


def test_notification_restores_only_previously_authorized_pages_within_unlock_scope():
    source = _code((PHONE / "CatchUpView.swift").read_text())
    view = _block(source, "struct NotificationDestinationView")
    assert "sheetEntry?.resolvedNotification(" in view
    assert "unlocked: router.unlocked" in view
    assert "generation: router.generation, requestGeneration: router.requestGeneration" in view
    resolve = _block(view, "private func resolve()")
    assert resolve.count("router.accepts(route)") >= 3
    assert "authorized: router.accepts(route)" in resolve
    assert resolve.index("router.accepts(route)") < resolve.index("sheetEntry?.rememberNotification(")
    assert "page = held" in view


def test_mount_and_availability_follow_actual_entry_identity_not_subject_identity():
    source = _code((PHONE / "PhoneSheetHost.swift").read_text())
    state = _block(source, "final class PhoneSheetEntryState")
    assert "let id = UUID()" in state
    assert ".id(router.topState?.id)" in source
    assert ".id(router.top?.id)" not in source
    assert ".onChange(of: router.topState?.id)" in source
    assert "resolvedNotification != router.topState?.id" in source
    assert "guard router.topState?.id == entryIdentity else { return }" in source


def test_the_agent_screens_card_row_says_it_is_a_link():
    """21 Sep 2026: the bound card's title opened the card's sheet on a
    tap, but read as a heading, so nobody tapped it. The row now carries a
    chip naming the verb and the column (`OPEN CARD · BACKLOG`) with a
    chevron, the sheet stacks over the agent screen (`sheets.show`, never
    `close`), and VoiceOver hears where Back leads."""
    text = (PHONE / "AgentDetailView.swift").read_text()
    lead = text.split("private var cardLead: some View")[1].split(
        "static func cardChip(")[0]
    assert "sheets.show(.card(card))" in lead and "sheets.close()" not in lead
    assert "Self.cardChip(column: card.column)" in lead
    assert 'Image(systemName: "chevron.right")' in lead
    assert "Back returns here" in lead
    chip = text.split("static func cardChip(column: String) -> String")[1].split("\n    }")[0]
    for column, word in (("prep", "PREP"), ("backlog", "BACKLOG"),
                         ("in_progress", "IN PROGRESS"), ("done", "DONE")):
        assert f'"{column}": "{word}"' in chip
    assert 'return "OPEN CARD"' in chip and '"OPEN CARD · " + name' in chip
