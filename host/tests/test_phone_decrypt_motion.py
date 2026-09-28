"""Run the real finite Swift model and pin intentional interaction entry points."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios/BobPhone"


def test_actual_swift_projection_and_reducer(tmp_path):
    swift = shutil.which("swiftc")
    if not swift:
        pytest.skip("Swift toolchain unavailable")
    main = tmp_path / "main.swift"
    main.write_text(r'''
import Foundation
for kind in [DecryptMotion.Kind.screen, .button, .refresh] {
    let e = DecryptMotion.Episode(kind: kind, surface: "a", began: 0, generation: 4)
    precondition(e.frame(at: -1) == e.frame(at: 0))
    precondition(e.frame(at: 0.3) == e.frame(at: 0.3))
    precondition(e.frame(at: kind.duration - 0.19) == kind.caption)
    precondition(e.frame(at: kind.duration) == nil)
    precondition(e.frame(at: 0, reduced: true) == kind.caption)
}
precondition(DecryptMotion.buttonDuration == 1.2)
precondition(DecryptMotion.screenDuration == 1.5 && DecryptMotion.refreshDuration == 1.5)
precondition(DecryptMotion.frame("", elapsed: 0, duration: 1.5, seed: 1) == "")
precondition(DecryptMotion.frame(String(repeating: "A", count: 100), elapsed: 2, duration: 1.5, seed: 1).count == 24)
var state = DecryptMotion.State()
state.automatic(.snapshot); state.automatic(.poll); state.automatic(.receipt)
precondition(state.generation == 0)
state.arrive("a", at: 0); state.arrive("a", at: 0.01)
precondition(state.generation == 1)
state.begin(.button, surface: "a", at: 0)
state.begin(.button, surface: "a", at: 0.1)
precondition(state.button?.generation == 3)
state.arrive("b", at: 0)
precondition(state.button == nil)
state.cancel(surface: "a")
precondition(state.surface == "b")
precondition(state.nextTick(at: 0, reduced: true) == 1.5)
var now = 0.0, count = 0
while let next = state.nextTick(at: now, reduced: false) {
    precondition(next - now <= 0.050001)
    now = next; count += 1; state.tick(at: now)
}
precondition(count <= 31 && now == 1.5)
state.cancel(); precondition(state.surface == nil)
state.arrive("composer", at: 10)
state.returnedFromPresentation(at: 12)
precondition(state.screen?.began == 12)
let returned = state.generation
state.arrive("composer", at: 12.01)
precondition(state.generation == returned)
state.cancel()
state.returnedFromPresentation(at: 14)
precondition(state.screen == nil)
state.arrive("composer", at: 14)
let appeared = state.generation
state.returnedFromPresentation(at: 14.01)
precondition(state.generation == appeared)
print("projection/reducer passed")
''')
    binary = tmp_path / "decrypt"
    subprocess.run([swift, str(PHONE / "DecryptMotion.swift"), str(main), "-o", str(binary)], check=True, capture_output=True, text=True)
    assert subprocess.check_output([str(binary)], text=True).strip() == "projection/reducer passed"


def _code(text):
    return re.sub(r"//[^\n]*", "", text)


def test_every_app_owned_button_uses_semantic_wrapper():
    for path in PHONE.glob("*.swift"):
        if path.name == "DecryptFeedback.swift":
            continue
        assert not re.search(r"\bButton\s*[(\{]", _code(path.read_text())), path.name
    wrapper = (PHONE / "DecryptFeedback.swift").read_text()
    assert "Button(role: role)" in wrapper
    assert "feedback.activate(enabled: enabled, action: action)" in wrapper
    assert "simultaneousGesture" not in wrapper and "highPriorityGesture" not in wrapper


def test_explicit_refresh_inventory_and_automatic_silence():
    expected = {"NeedsYouView.swift": 1, "FleetView.swift": 1, "BoardView.swift": 2,
                "UsageView.swift": 1, "PipelineView.swift": 1, "CatchUpView.swift": 1,
                "AgentReportView.swift": 1, "LifecycleReportView.swift": 1}
    actual = {}
    begins = 0
    for path in PHONE.glob("*.swift"):
        text = path.read_text()
        count = text.count(".refreshable {")
        if count:
            actual[path.name] = count
        begins += text.count("begin(kind: .refresh)")
    assert actual == expected
    assert begins == 0
    for name in ["Client.swift", "BackgroundRefresh.swift", "Outbox.swift", "TerminalStream.swift"]:
        assert "Decrypt" not in (PHONE / name).read_text()
    widget = ROOT / "ios/BobPhoneWidget"
    for path in widget.glob("*.swift"):
        assert "Decrypt" not in path.read_text(), path.name


def test_destinations_and_control_bindings_inventory():
    for name in ["BobPhoneApp", "AgentDetailView", "CardDetailView", "ComposerView", "ProfileView",
                 "PipelineView", "WorkRecordView", "CatchUpView", "Pairing",
                 "AgentReportView", "LifecycleReportView", "KnowledgeView",
                 "AccessLogView", "MenuView"]:
        assert ".decryptSurface(" in (PHONE / f"{name}.swift").read_text(), name
    app = (PHONE / "BobPhoneApp.swift").read_text()
    for tab in ["needs", "fleet", "board", "menu"]:
        assert f"selectedTab == .{tab} && sheets.top == nil" in app
    for name in ["CatchUpView", "OutcomeScreens", "ProfileView",
                 "AgentReportView", "LifecycleReportView", "KnowledgeView"]:
        text = (PHONE / f"{name}.swift").read_text()
        for line in text.splitlines():
            if re.search(r"\b(Picker|DisclosureGroup)\(", line):
                assert ".decrypting(decryptFeedback)" in line
    assert "decryptFeedback?.begin(kind: .screen)" in (PHONE / "AgentDetailView.swift").read_text()


def test_sheet_stills_and_arrivals_only_chrome():
    theme = (PHONE / "Theme.swift").read_text()
    mark = theme.split("struct PixelMark", 1)[1].split("struct BrandMark", 1)[0]
    assert mark.count("var size: CGFloat = 20") == 1
    assert "static let sheetSize: CGFloat = 160" in mark
    agent = (PHONE / "AgentDetailView.swift").read_text()
    card = (PHONE / "CardDetailView.swift").read_text()
    # The agent sheet's still is smaller and sits beside its text; its size
    # follows the sheet's height (`AgentSheetLead.stillSize`).
    assert "static let leadPhotoSize: CGFloat = 96" in agent
    assert "size: leadStill" in agent
    assert "PixelMark.sheetSize" in card
    assert agent.count("size: 40") == 1
    identity = agent.split("private var identity: some View", 1)[1].split(
        "private var hostedIdentity", 1)[0]
    assert "PixelMark(" not in identity and "size: 40" not in identity
    assert "photoTucked.toggle()" in identity
    # The lead's second line is the status line, not the quote.
    assert "AgentSheetLead.statusLine(" in identity
    assert "Text(quote)" not in identity
    # The quote is secondary and one line on Details: smaller than the
    # facts, shrinking to fit rather than wrapping.
    rest = agent.split("private var rest: some View", 1)[1].split(
        "static let hostedStripMaxHeight", 1)[0]
    quote = rest.split("Text(quote)", 1)[1].split("}", 1)[0]
    assert "Theme.mono(Self.quoteSize)" in quote and "Theme.dim" in quote
    assert ".lineLimit(1)" in quote
    assert ".minimumScaleFactor(Self.quoteMinScale)" in quote
    assert "fixedSize" not in quote
    assert "static let quoteSize: CGFloat = 10" in agent
    assert "static let quoteMinScale: CGFloat = 0.7" in agent
    assert "private var conversationLead" not in agent
    main = agent.split("private var mainScreen: some View", 1)[1].split(
        "private var titleLine", 1)[0]
    assert "if !photoTucked" in main and "size: leadStill" in main
    assert "TucksPhotoOnScroll" not in agent, "the lead scrolls away with Main's page"
    hosted = agent.split("private var hostedIdentity: some View", 1)[1].split(
        "private var terminalCover", 1)[0]
    assert "size: 40" in hosted
    terminal = agent.split("private var terminalScreen: some View", 1)[1]
    assert "hostedIdentity" in terminal.split("PhoneTerminalPane", 1)[0]
    # The conversation is one scrolling page — status, header, turns, then
    # the ask and its answer buttons — over a message composer pinned
    # beneath it as a stack sibling (26 Sep 2026), never an overlay, so no
    # part can paint over another. It opens at the foot and a new turn or
    # the keyboard scrolls back to the answer buttons.
    conv = (PHONE / "ConversationView.swift").read_text()
    conv_body = conv.split("var body: some View", 1)[1].split(
        "@ViewBuilder private func rowView", 1)[0]
    assert conv_body.count("ScrollView {") == 1
    assert "ConversationColumn" not in conv and "BoundedAnswer" not in conv
    assert conv_body.index("statusLines") < conv_body.index("header")
    assert conv_body.index("header") < conv_body.index("ForEach(rows)")
    assert conv_body.index("ForEach(rows)") < conv_body.index("part: .choices")
    assert conv_body.index("page\n") < conv_body.index("part: .composer")
    assert ".defaultScrollAnchor(.bottom)" in conv_body
    # Open, new turn at the foot, keyboard up, and one `jump` for a message
    # just sent or "↓ n new" pressed (26 Sep 2026).
    assert conv_body.count("scrollTo(Self.answerAnchor, anchor: .bottom)") == 4
    assert "messageWindow" not in conv
    assert conv.count("AnswerBox(") == 2
    ui = (PHONE / "DecryptFeedback.swift").read_text()
    activate = ui.split("func activate(", 1)[1].split("func cancel(", 1)[0]
    assert "begin(kind: .button)" not in activate
    assert "action()" in activate
    chrome = ui.split("struct DecryptChrome", 1)[1].split("struct DecryptAppearance", 1)[0]
    assert '"OPEN"' in chrome
    assert '"REFRESH"' not in chrome
    assert '"INPUT"' not in chrome
    # The strip sits *above* the content, never over it: mounted as a top
    # safe-area inset (an overlay painted the line across the first row of
    # buttons, 21 Sep 2026), and always laid out — no `if let glyphs` around
    # the chrome — so the reserved height never changes and the content does
    # not jump when a scramble starts or ends; the rule dims instead.
    surface = ui.split("struct DecryptSurface", 1)[1]
    assert ".safeAreaInset(edge: .top, spacing: 0)" in surface
    # A sheet draws the caption in its header (`DecryptCaptionHost`), so the
    # strip is mounted only where no host is present: a tab root.
    assert "if let feedback, host == nil" in surface
    assert ".overlay(alignment: .top)" not in surface
    assert "if let glyphs" not in chrome
    assert "glyphs != nil ? Theme.phosphor : Theme.rule" in chrome


def test_decoration_has_no_sensitive_input_or_rendering_engine():
    motion = (PHONE / "DecryptMotion.swift").read_text()
    ui = (PHONE / "DecryptFeedback.swift").read_text()
    for forbidden in ["Canvas", "CADisplayLink", "UIAccessibility.post", "Timer.publish", "snapshot", "generatedAt", "terminalBytes"]:
        assert forbidden not in ui
    assert ".accessibilityHidden(true)" in ui and ".allowsHitTesting(false)" in ui
    assert "viewDidAppear" in ui and "viewDidDisappear" in ui
    assert 'return "OPEN"' in motion and 'return "INPUT"' in motion and 'return "REFRESH"' in motion
    project = (ROOT / "ios/BobPhone.xcodeproj/project.pbxproj").read_text()
    for name in ["DecryptMotion", "DecryptFeedback", "DecryptMotionTests", "DecryptFeedbackTests"]:
        assert f"{name}.swift in Sources" in project


def test_every_navigation_link_has_an_inventoried_destination():
    # ProfileView: the composer's "Add a key under Profile" link.
    allowed = {"PipelineView", "PhoneAgentReport", "PhoneLifecycleReport",
               "KnowledgeView", "AccessLogView", "ProfileView"}
    count = 0
    for path in PHONE.glob("*.swift"):
        code = _code(path.read_text())
        links = list(re.finditer(r"\bNavigationLink(?:\([^\n]*?\))?\s*\{\s*(\w+)\(", code))
        assert len(links) == len(re.findall(r"\bNavigationLink\b", code)), path.name
        for link in links:
            assert link.group(1) in allowed, (path.name, link.group(1))
            count += 1
    assert count == 6


def test_native_picker_launch_and_cancel_return_are_explicit():
    text = (PHONE / "ComposerView.swift").read_text()
    assert "DecryptButton { showingPhotos = true }" in text
    assert ".photosPicker(isPresented: $showingPhotos, selection: $picked," in text
    assert "if !presented { decryptFeedback?.returnedFromPresentation() }" in text
    assert ".onChange(of: picked)" in text and "Task { await sendPicked(items) }" in text


def test_board_rows_fold_on_an_ordinary_button_press_and_no_pager_remains():
    """`plans/2026-09-12-board-rows-instead-of-columns.md`: the paged
    `TabView` and its chip strip are gone, so there is no column selection
    to feed a screen episode. A row's heading is an ordinary `DecryptButton`
    whose only action is the fold, spelled through `BoardRowFold` so the
    phone and the Mac keep one rule."""
    text = (PHONE / "BoardView.swift").read_text()
    assert "TabView(" not in text
    assert "columnSelectionChanged" not in text
    assert '"board.column"' not in text
    heading = text.split("private func rowHeading(for item:", 1)[1].split(
        "private func rowBody(for id: String)", 1)[0]
    assert "DecryptButton {" in heading
    assert ("rowFlipsJoined = BoardRowFold.encode(joined: "
            "BoardRowFold.toggled(item.id, flipped: rowFlips))") in heading
    assert "feedback?.begin(kind: .screen)" not in text

def test_the_answer_box_can_never_paint_over_the_tabs():
    """An interview's option cards once took their whole ideal height and
    painted over the lead, the verbs and the tabs (22 Sep 2026). The
    answer buttons sit under the turns, inside the scroll, and the tabs are
    above the scroll view entirely. The message composer is pinned beneath
    the scroll as a stack sibling (26 Sep 2026): it shortens the page and
    cannot paint over it."""
    from pathlib import Path
    conv = (Path(__file__).resolve().parents[2]
            / "ios" / "BobPhone" / "ConversationView.swift").read_text()
    body = conv.split("var body: some View", 1)[1].split(
        "@ViewBuilder private func rowView", 1)[0]
    head, scroll = body.split("ScrollView {", 1)
    assert "part: .choices" in scroll and "part: .choices" not in head
    assert "part: .composer" in head and "part: .composer" not in scroll
    assert "VStack(spacing: 0) {\n            page" in head
    assert "offset(" not in body and "overlay" not in body
    assert conv.count("AnswerBox(") == 2
