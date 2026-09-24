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
        XCTAssertEqual(SettingsWindowMetrics.minWidth, 460)
        XCTAssertEqual(SettingsWindowMetrics.minHeight, 560)
        controller.forceClose()
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
