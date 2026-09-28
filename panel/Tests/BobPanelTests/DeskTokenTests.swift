import AppKit
import XCTest
@testable import BobPanel

/// The desk token arrives on the menu bar's context push and nowhere else
/// (`docs/transport-contract.md`, *The loopback door has two tokens*). These
/// pin the panel's half: the empty default, and the Copy desk key row that
/// is drawn only while a key is held.
@MainActor
final class DeskTokenTests: XCTestCase {
    private func rows(context: DaemonClient.PanelContext,
                      deskTokenArmed: Bool = false) -> [SettingsRow] {
        SettingsMenuModel.rows(
            settings: DaemonClient.Settings(), context: context,
            enrollment: Enrollment(), recordingShortcut: false,
            unenrolArmed: nil, deskTokenArmed: deskTokenArmed)
    }

    private func deskRow(_ tree: [SettingsRow]) -> SettingsRow? {
        SettingsMenuModel.flattened(tree)
            .first { $0.id == SettingsMenuModel.deskTokenRowId }
    }

    /// No key until the first push: the panel never reads one off disk.
    func testContextStartsWithNoDeskToken() {
        XCTAssertEqual(DaemonClient.PanelContext().deskToken, "")
    }

    /// The row is absent while the panel holds no key, and present under
    /// Advanced once it does — so a press can never copy an empty string.
    func testCopyDeskKeyRowOnlyWithAToken() {
        XCTAssertNil(deskRow(rows(context: DaemonClient.PanelContext())))

        var ctx = DaemonClient.PanelContext()
        ctx.deskToken = "desk-secret"
        let tree = rows(context: ctx)
        let row = deskRow(tree)
        XCTAssertNotNil(row)
        XCTAssertEqual(row?.title, "Copy desk key")
        if case .custom(let action) = row?.kind {
            XCTAssertEqual(action, .copyDeskToken)
        } else {
            XCTFail("Copy desk key is not a custom row")
        }
        // Under Advanced, beside Open log.
        let advanced = tree.first { $0.id == "submenu:advanced" }
        if case .submenu(_, let inner) = advanced?.kind {
            XCTAssertTrue(inner.contains { $0.id == SettingsMenuModel.deskTokenRowId })
        } else {
            XCTFail("no Advanced submenu")
        }
        // The token itself is never drawn in a title or a tooltip.
        for r in SettingsMenuModel.flattened(tree) {
            XCTAssertFalse(r.title.contains("desk-secret"))
            XCTAssertFalse(r.tooltip.contains("desk-secret"))
        }
    }

    /// Armed, the title says what the second press does; the id stays put.
    func testArmedTitle() {
        var ctx = DaemonClient.PanelContext()
        ctx.deskToken = "desk-secret"
        let armed = deskRow(rows(context: ctx, deskTokenArmed: true))
        XCTAssertEqual(armed?.title, "Press again: copy the desk key")
        XCTAssertEqual(armed?.id, "custom:copyDeskToken")
        XCTAssertNotEqual(SettingsMenuModel.deskTokenTitle,
                          SettingsMenuModel.deskTokenArmedTitle)
    }

    /// Clipboard-history managers skip the key: concealed and transient.
    func testClipboardTypesMarkTheKeyConcealedAndTransient() {
        XCTAssertEqual(DeskTokenClipboard.types.map(\.rawValue), [
            NSPasteboard.PasteboardType.string.rawValue,
            "org.nspasteboard.ConcealedType",
            "org.nspasteboard.TransientType"])
        XCTAssertEqual(DeskTokenClipboard.clearAfter, 60)
    }

    /// A private pasteboard, never the person's own: the copy lands, the
    /// clear only bites while nothing else was copied since.
    func testCopyThenClearOnlyWhileUnchanged() {
        let board = NSPasteboard.withUniqueName()
        defer { board.releaseGlobally() }
        DeskTokenClipboard.copy("desk-secret", to: board)
        XCTAssertEqual(board.string(forType: .string), "desk-secret")
        XCTAssertNotNil(board.types?.first { $0 == DeskTokenClipboard.concealed })
        let stamp = board.changeCount
        board.clearContents()
        board.setString("something else", forType: .string)
        XCTAssertFalse(DeskTokenClipboard.clearIfUnchanged(board, since: stamp))
        XCTAssertEqual(board.string(forType: .string), "something else")
        XCTAssertTrue(DeskTokenClipboard.clearIfUnchanged(board, since: board.changeCount))
        XCTAssertNil(board.string(forType: .string))
    }
}
