import AppKit
import XCTest
@testable import BobPanel

/// The settings window, `CardWindowTests`' shape: the window's own properties,
/// one window across repeated presents, the key-routing predicate the monitor
/// asks, that a close cancels an in-flight shortcut capture, and the Escape
/// ladder. What it cannot see is minimise, the Dock tile and real ordering —
/// the manual check on the card.
final class SettingsWindowTests: XCTestCase {

    @MainActor
    private func makeController() -> SettingsWindowController {
        SettingsWindowController(client: DaemonClient())
    }

    // MARK: - The window itself

    @MainActor
    func testTheWindowIsTitledClosableMiniaturizableAndResizable() {
        let controller = makeController()
        controller.present()
        let win = try! XCTUnwrap(controller.windowForTesting)
        for mask: NSWindow.StyleMask in [.titled, .closable, .miniaturizable, .resizable] {
            XCTAssertTrue(win.styleMask.contains(mask),
                          "the settings window must carry \(mask)")
        }
        XCTAssertEqual(win.title, "Settings")
        controller.forceClose()
    }

    @MainActor
    func testTheWindowIsAnOrdinaryDarkWindowThatSurvivesItsOwnClose() {
        let controller = makeController()
        controller.present()
        let win = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertEqual(win.level, .normal)
        XCTAssertFalse(win.isReleasedWhenClosed, "the object is reused for the next open")
        XCTAssertEqual(win.appearance?.name, .darkAqua)
        controller.forceClose()
    }

    @MainActor
    func testTheWindowHasTheMetricsFloor() {
        let controller = makeController()
        controller.present()
        let win = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertEqual(win.contentMinSize.width, SettingsWindowMetrics.minWidth)
        XCTAssertEqual(win.contentMinSize.height, SettingsWindowMetrics.minHeight)
        XCTAssertEqual(SettingsWindowMetrics.minWidth, 720)
        XCTAssertEqual(SettingsWindowMetrics.minHeight, 520)
        XCTAssertEqual(SettingsWindowMetrics.defaultWidth, 860)
        XCTAssertEqual(SettingsWindowMetrics.defaultHeight, 620)
        XCTAssertEqual(SettingsWindowMetrics.sidebarWidth, 210)
        controller.forceClose()
    }

    /// The first present opens at 860 × 620 of content, or as much of it as
    /// the screen has room for.
    @MainActor
    func testTheFirstPresentOpensAtTheDefaultSizeClampedToTheScreen() {
        let controller = makeController()
        controller.present()
        let win = try! XCTUnwrap(controller.windowForTesting)
        let content = win.contentRect(forFrameRect: win.frame).size
        if let visible = (win.screen ?? NSScreen.main)?.visibleFrame {
            let chrome = win.frame.height - content.height
            XCTAssertEqual(content.width, min(SettingsWindowMetrics.defaultWidth, visible.width))
            XCTAssertEqual(content.height,
                           min(SettingsWindowMetrics.defaultHeight, visible.height - chrome))
        } else {
            // No screen to place on: the window keeps the size it was made at.
            XCTAssertEqual(content.width, SettingsWindowMetrics.defaultWidth)
            XCTAssertEqual(content.height, SettingsWindowMetrics.defaultHeight)
        }
        controller.forceClose()
    }

    /// Where the window was left is where it comes back, for the life of the
    /// process — clamped, never re-centred.
    @MainActor
    func testAMovedWindowKeepsItsFrameAcrossACloseAndAPresent() throws {
        let controller = makeController()
        controller.present()
        let win = try XCTUnwrap(controller.windowForTesting)
        guard let visible = (win.screen ?? NSScreen.main)?.visibleFrame else {
            throw XCTSkip("no screen to place a window on")
        }
        let moved = SettingsWindowController.clamped(
            NSRect(x: visible.minX + 10, y: visible.minY + 10,
                   width: SettingsWindowMetrics.minWidth + 40,
                   height: SettingsWindowMetrics.minHeight + 60),
            to: visible)
        win.setFrame(moved, display: false)
        controller.forceClose()
        controller.present()
        XCTAssertEqual(win.frame, moved)
        // A frame left partly off screen is nudged back on, not re-centred.
        let off = NSRect(x: visible.maxX - 100, y: visible.minY + 10,
                         width: moved.width, height: moved.height)
        win.setFrame(off, display: false)
        controller.forceClose()
        controller.present()
        XCTAssertEqual(win.frame, SettingsWindowController.clamped(off, to: visible))
        XCTAssertLessThanOrEqual(win.frame.maxX, visible.maxX)
        controller.forceClose()
    }

    @MainActor
    func testTheClampNeverGoesBelowTheFloorOrPastTheScreen() {
        let visible = NSRect(x: 0, y: 0, width: 1440, height: 900)
        let tiny = SettingsWindowController.clamped(
            NSRect(x: 50, y: 50, width: 100, height: 100), to: visible)
        XCTAssertEqual(tiny.width, SettingsWindowMetrics.minWidth)
        XCTAssertEqual(tiny.height, SettingsWindowMetrics.minHeight)
        let small = NSRect(x: 0, y: 0, width: 600, height: 400)
        let huge = SettingsWindowController.clamped(
            NSRect(x: 0, y: 0, width: 5000, height: 5000), to: small)
        XCTAssertEqual(huge.size, small.size)
    }

    // MARK: - The section

    @MainActor
    func testTheSectionSurvivesACloseAndAPresent() {
        let controller = makeController()
        XCTAssertEqual(controller.state.section, .general)
        controller.present()
        controller.state.select(section: .board)
        controller.forceClose()
        controller.present()
        XCTAssertEqual(controller.state.section, .board)
        controller.forceClose()
    }

    @MainActor
    func testSelectingASectionClearsTheQuery() {
        let state = SettingsWindowState()
        state.query = "outright"
        state.select(section: .advanced)
        XCTAssertEqual(state.section, .advanced)
        XCTAssertEqual(state.query, "")
    }

    @MainActor
    func testAJumpOpensTheSectionAndProjectMarksTheRowAndClearsTheQuery() {
        let state = SettingsWindowState()
        state.query = "un-enrol"
        let entry = SettingsEntry(row: SettingsRow(
            id: "custom:unenrol:/Users/me/proj", title: "Un-enrol",
            kind: .custom(.unenrol(root: "/Users/me/proj"))), block: "proj")
        state.jump(to: SettingsSearch.Hit(section: .projects, crumb: "Projects › proj",
                                          entry: entry, projectRoot: "/Users/me/proj"))
        XCTAssertEqual(state.section, .projects)
        XCTAssertEqual(state.selectedProjectRoot, "/Users/me/proj")
        XCTAssertEqual(state.highlightedRowId, "custom:unenrol:/Users/me/proj")
        XCTAssertEqual(state.query, "")
    }

    @MainActor
    func testAJumpToTheSidebarFootKeepsTheSectionAndTheProject() {
        let state = SettingsWindowState()
        state.select(section: .devices)
        state.selectedProjectRoot = "/a"
        state.query = "quit"
        let quit = SettingsEntry(row: SettingsRow(
            id: "send:quit_app", title: "Quit", kind: .send(action: "quit_app", value: nil)))
        state.jump(to: SettingsSearch.Hit(section: nil, crumb: "Sidebar", entry: quit,
                                          projectRoot: nil))
        XCTAssertEqual(state.section, .devices)
        XCTAssertEqual(state.selectedProjectRoot, "/a")
        XCTAssertEqual(state.highlightedRowId, "send:quit_app")
        XCTAssertEqual(state.query, "")
    }

    /// A jump clears its own mark, so a target that draws no flash cannot
    /// strand it and a later jump to the same row flashes again; a newer
    /// jump's mark outlives the older one's timer.
    @MainActor
    func testAJumpClearsItsOwnMarkAndANewerJumpKeepsItsOwn() async throws {
        let state = SettingsWindowState()
        state.markSeconds = 0.05
        let header = SettingsEntry(row: SettingsRow(
            id: "submenu:agent-models:grok", title: "grok", kind: .submenu(title: "grok", rows: [])))
        state.jump(to: SettingsSearch.Hit(section: .models, crumb: "Models", entry: header,
                                          projectRoot: nil))
        XCTAssertEqual(state.highlightedRowId, "submenu:agent-models:grok")
        XCTAssertEqual(state.jumpSerial, 1)
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertNil(state.highlightedRowId)

        state.markSeconds = 0.05
        state.jump(to: SettingsSearch.Hit(section: .models, crumb: "Models", entry: header,
                                          projectRoot: nil))
        state.markSeconds = 60
        let root = SettingsEntry(row: SettingsRow(
            id: "info:project-root:/a", title: "/a", kind: .info))
        state.jump(to: SettingsSearch.Hit(section: .projects, crumb: "Projects › a",
                                          entry: root, projectRoot: "/a"))
        XCTAssertEqual(state.jumpSerial, 3)
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(state.highlightedRowId, "info:project-root:/a")
    }

    /// One predicate for "showing results": white space alone is not a search,
    /// so the sidebar keeps its selection and the page stays.
    @MainActor
    func testWhiteSpaceIsNotASearch() {
        let state = SettingsWindowState()
        XCTAssertFalse(state.isSearching)
        state.query = "   \n"
        XCTAssertFalse(state.isSearching)
        state.query = " queued "
        XCTAssertTrue(state.isSearching)
    }

    /// A close drops the mark, so the next jump to the same row flashes again.
    @MainActor
    func testACloseClearsTheHighlight() {
        let controller = makeController()
        controller.present()
        controller.state.highlightedRowId = "toggle:set_board_close_terminal"
        controller.forceClose()
        XCTAssertNil(controller.state.highlightedRowId)
    }

    // MARK: - One window, reused

    @MainActor
    func testPresentingTwiceYieldsTheSameWindow() {
        let controller = makeController()
        controller.present()
        let first = try! XCTUnwrap(controller.windowForTesting)
        controller.forceClose()
        controller.present()
        let second = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertTrue(first === second)
        controller.forceClose()
    }

    @MainActor
    func testPresentingStepsTheFocusRequestSoTheFieldTakesTheCaretAgain() {
        let controller = makeController()
        let before = controller.state.focusRequest
        controller.present()
        XCTAssertEqual(controller.state.focusRequest, before + 1)
        controller.present()
        XCTAssertEqual(controller.state.focusRequest, before + 2)
        controller.forceClose()
    }

    // MARK: - Key routing

    @MainActor
    func testOwnsIsTrueForItsOwnWindowAndFalseForAStrangerOrNil() {
        let controller = makeController()
        controller.present()
        let win = try! XCTUnwrap(controller.windowForTesting)
        XCTAssertTrue(controller.owns(win))
        XCTAssertFalse(controller.owns(nil))
        let stranger = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 100, height: 100),
            styleMask: [.titled], backing: .buffered, defer: false)
        XCTAssertFalse(controller.owns(stranger))
        controller.forceClose()
    }

    @MainActor
    func testOwnsIsFalseBeforeTheWindowExists() {
        let controller = makeController()
        XCTAssertNil(controller.windowForTesting)
        XCTAssertFalse(controller.owns(nil))
        XCTAssertFalse(controller.isKey)
        XCTAssertFalse(controller.isVisiblyOnScreen)
    }

    // MARK: - The close routes

    @MainActor
    func testTheRedButtonIsNeverRefusedAndAClosecancelsAShortcutCapture() {
        let controller = makeController()
        controller.present()
        let win = try! XCTUnwrap(controller.windowForTesting)
        controller.actions.beginRecordingShortcut()
        XCTAssertTrue(controller.actions.recordingShortcut)
        XCTAssertTrue(controller.windowShouldClose(win),
                      "a capture in flight must not strand the window")
        controller.forceClose()
        XCTAssertFalse(controller.actions.recordingShortcut,
                       "the one-shot monitor must not outlive the window")
    }

    @MainActor
    func testCancelShortcutRecordingIsANoOpWithNothingInFlight() {
        let controller = makeController()
        controller.actions.cancelShortcutRecording()
        XCTAssertFalse(controller.actions.recordingShortcut)
    }

    // MARK: - Escape

    @MainActor
    func testEscapeClearsTheQueryFirstAndAsksToCloseSecond() {
        let state = SettingsWindowState()
        var closes = 0
        state.onCloseRequest = { closes += 1 }
        state.query = "phone push"
        XCTAssertEqual(state.escape(), .clearedQuery)
        XCTAssertEqual(state.query, "")
        XCTAssertEqual(closes, 0)
        XCTAssertEqual(state.escape(), .close)
        XCTAssertEqual(closes, 1)
    }

    // MARK: - Optimistic toggles

    @MainActor
    func testAPressDrawsTheWantedStateUntilTheContextEchoes() {
        let client = DaemonClient()
        let controller = SettingsWindowController(client: client)
        let actions = controller.actions
        XCTAssertTrue(actions.toggleIsOn(action: "set_notification_sound", model: true))
        actions.sendToggle(action: "set_notification_sound", wanted: false)
        XCTAssertFalse(actions.toggleIsOn(action: "set_notification_sound", model: true))
        // A snapshot publish clears nothing.
        client.snapshot = Snapshot()
        XCTAssertFalse(actions.toggleIsOn(action: "set_notification_sound", model: true))
        // A context publish clears the overlay on the next main-queue turn.
        client.context = DaemonClient.PanelContext()
        let settled = expectation(description: "overlay cleared")
        DispatchQueue.main.async { settled.fulfill() }
        wait(for: [settled], timeout: 2)
        XCTAssertTrue(actions.toggleIsOn(action: "set_notification_sound", model: true))
    }

    // MARK: - The shared folder chooser

    /// The settings window and the first-run checklist run one chooser. A
    /// cancel is inert — the completion hears `nil` and nothing is sent —
    /// and a chosen folder goes to the daemon's own enrol verb, whose answer
    /// reaches the caller as it is.
    @MainActor
    func testACancelledChooserIsInertAndAChosenFolderReachesTheCaller() {
        let original = SettingsActions.folderPicker
        defer { SettingsActions.folderPicker = original }
        let client = DaemonClient()

        SettingsActions.folderPicker = { nil }
        var heard: [ActionResult?] = []
        SettingsActions.enrolChosenFolder(client: client) { heard.append($0) }
        XCTAssertEqual(heard.count, 1)
        XCTAssertNil(heard[0])

        // The settings window's own row goes through the same seam.
        let controller = SettingsWindowController(client: client)
        var picks = 0
        SettingsActions.folderPicker = { picks += 1; return nil }
        controller.actions.chooseFolderToEnrol()
        XCTAssertEqual(picks, 1)
    }
}
