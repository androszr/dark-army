import AppKit
import XCTest
@testable import BobPanel

/// The card screen is its own ordinary window now, not a panel-attached sheet.
/// What a test can see in-process: the window's own properties, that there is
/// only ever **one** of it across retargets, the key-routing predicate the
/// monitor asks, and that every close route lands in `BoardState.closeEditor()`
/// — including the refusal while a create is on the wire. What it cannot see is
/// minimise, the Dock tile and real window ordering; those are the manual
/// checks on the card.
///
/// **Nothing here reaches the screen.** Every controller is built with
/// `ordersOnScreen: false` and force-closed in `tearDown`: a real window
/// ordered front from the `xctest` process is a dead "Card" window over the
/// person's desktop, one per test, holding their keyboard until the run ends
/// — `testPresentingNeverOrdersTheWindowOnScreen` pins the seam.
final class CardWindowTests: XCTestCase {

    private var controllers: [CardWindowController] = []

    @MainActor
    override func tearDown() {
        controllers.forEach { $0.forceClose() }
        controllers.removeAll()
        super.tearDown()
    }

    @MainActor
    private func makeController(ordersOnScreen: Bool = false)
        -> (CardWindowController, BoardState, DaemonClient)
    {
        let client = DaemonClient()
        let state = BoardState()
        let controller = CardWindowController(client: client, state: state,
                                              ordersOnScreen: ordersOnScreen)
        controllers.append(controller)
        return (controller, state, client)
    }

    // MARK: - The window itself

    @MainActor
    func testTheWindowIsTitledClosableMiniaturizableAndResizable() {
        let (controller, state, _) = makeController()
        state.editing = BoardState.newCard
        let win = try! XCTUnwrap(controller.windowForTesting)
        for mask: NSWindow.StyleMask in [.titled, .closable, .miniaturizable, .resizable] {
            XCTAssertTrue(win.styleMask.contains(mask),
                          "the card window must carry \(mask)")
        }
    }

    @MainActor
    func testTheWindowIsAnOrdinaryDarkWindowThatSurvivesItsOwnClose() {
        let (controller, state, _) = makeController()
        state.editing = BoardState.newCard
        let win = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertEqual(win.level, .normal)
        XCTAssertFalse(win.isReleasedWhenClosed,
                       "the object is reused for the next card")
        XCTAssertEqual(win.appearance?.name, .darkAqua)
    }

    @MainActor
    func testTheWindowHasTheSheetsOldFrameAsItsFloor() {
        let (controller, state, _) = makeController()
        state.editing = BoardState.newCard
        let win = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertEqual(win.contentMinSize.width, CardWindowMetrics.minWidth)
        XCTAssertEqual(win.contentMinSize.height, CardWindowMetrics.minHeight)
        XCTAssertEqual(CardWindowMetrics.minWidth, 620)
        XCTAssertEqual(CardWindowMetrics.minHeight, 620)
    }

    @MainActor
    func testPresentingNeverOrdersTheWindowOnScreen() {
        let (controller, state, _) = makeController()
        state.editing = BoardState.newCard
        let win = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertFalse(win.isVisible,
                       "a test's card window must never reach the screen")
        XCTAssertFalse(controller.isVisiblyOnScreen)
        XCTAssertEqual(win.title, "New Card", "the presentation path still runs")
        state.editing = "card-42"
        XCTAssertFalse(win.isVisible)
        XCTAssertEqual(win.title, "Card")
    }

    // MARK: - One window, reused

    @MainActor
    func testRetargetingReusesTheSameWindow() {
        let (controller, state, _) = makeController()
        state.editing = BoardState.newCard
        let first = try! XCTUnwrap(controller.windowForTesting)
        state.editing = "card-42"
        let second = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertTrue(first === second,
                      "opening a second card must swap the content of the one window")
    }

    @MainActor
    func testClosingAndReopeningStillReusesTheSameWindow() {
        let (controller, state, _) = makeController()
        state.editing = BoardState.newCard
        let first = try! XCTUnwrap(controller.windowForTesting)
        state.closeEditor()
        state.editing = "card-7"
        XCTAssertTrue(controller.windowForTesting === first)
    }

    // MARK: - Key routing

    @MainActor
    func testOwnsIsTrueForItsOwnWindowAndFalseForAStrangerOrNil() {
        let (controller, state, _) = makeController()
        state.editing = BoardState.newCard
        let win = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertTrue(controller.owns(win))
        XCTAssertFalse(controller.owns(nil))
        let stranger = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 100, height: 100),
            styleMask: [.titled], backing: .buffered, defer: false)
        XCTAssertFalse(controller.owns(stranger))
    }

    @MainActor
    func testOwnsIsFalseBeforeTheWindowExists() {
        let (controller, _, _) = makeController()
        XCTAssertNil(controller.windowForTesting)
        XCTAssertFalse(controller.owns(nil))
        XCTAssertFalse(controller.isKey)
    }

    // MARK: - The close routes

    @MainActor
    func testTheRedButtonIsRefusedWhileACreateIsOnTheWire() {
        let (controller, state, _) = makeController()
        state.editing = BoardState.newCard
        let win = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertTrue(controller.windowShouldClose(win))
        state.composerSaving = true
        XCTAssertFalse(controller.windowShouldClose(win),
                       "the copies the request names must not be discarded")
    }

    @MainActor
    func testForceCloseKeepsTheDraftWhileACreateIsOnTheWire() {
        let (controller, state, _) = makeController()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        state.draft.title = "half typed"
        let staged = "\(state.stagingId)/shot.png"
        state.stagedAttachments = [staged]
        state.composerSaving = true
        controller.forceClose()
        XCTAssertEqual(state.editing, BoardState.newCard)
        XCTAssertEqual(state.draft.title, "half typed")
        XCTAssertEqual(state.stagedAttachments, [staged])
    }

    @MainActor
    func testForceCloseDiscardsTheDraftOtherwise() {
        let (controller, state, _) = makeController()
        state.openComposer(defaultProject: "p", defaultRoot: "/tmp",
                           offeredTools: [])
        state.draft.title = "half typed"
        state.stagedAttachments = ["\(state.stagingId)/shot.png"]
        controller.forceClose()
        XCTAssertNil(state.editing, "the close route lands in closeEditor()")
        XCTAssertTrue(state.stagedAttachments.isEmpty)
        XCTAssertTrue(state.stagingId.isEmpty)
    }
}
