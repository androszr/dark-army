import AppKit
import XCTest
@testable import BobPanel

/// **The hosted terminal's keyboard, tabled.**
///
/// `plans/2026-09-06-terminal-pane-input-parity.md`. Every rule about who
/// owns a key press while the pane holds the caret is a pure function over
/// `(keyCode, flags, charactersIgnoringModifiers)`, so the whole decision is
/// here — no window, no AppKit event constructed, in `DetailTabTests`' shape.
/// `main.swift`'s monitor performs these verdicts and decides nothing itself.
final class TerminalKeyTests: XCTestCase {

    // Key codes, named so the cases read.
    private let del = TerminalKeys.deleteKey
    private let fwd = TerminalKeys.forwardDeleteKey
    private let left = TerminalKeys.leftKey
    private let right = TerminalKeys.rightKey
    private let up = TerminalKeys.upKey
    private let down = TerminalKeys.downKey
    private let ret = TerminalKeys.returnKey
    private let enter = TerminalKeys.keypadEnterKey
    private let letterA: UInt16 = 0

    private func verdict(_ code: UInt16,
                         _ flags: NSEvent.ModifierFlags = [],
                         _ chars: String? = nil) -> TerminalKeys.Verdict {
        TerminalKeys.verdict(keyCode: code, flags: flags, characters: chars)
    }

    // MARK: the ⌘ line edits — as they were, and still are

    func testTheCommandLineEditsAreTypedAtThePty() {
        XCTAssertEqual(verdict(del, [.command]), .type([0x15]))    // ⌘⌫ ^U
        XCTAssertEqual(verdict(fwd, [.command]), .type([0x0b]))    // ⌘⌦ ^K
        XCTAssertEqual(verdict(left, [.command]), .type([0x01]))   // ⌘← ^A
        XCTAssertEqual(verdict(right, [.command]), .type([0x05]))  // ⌘→ ^E
    }

    /// fn-⌫ carries no modifier at all and lands on `deleteForward:`, which
    /// SwiftTerm has no case for either.
    func testTheBareForwardDeleteIsTypedButCommandForwardDeleteIsItsOwnRow() {
        XCTAssertEqual(verdict(fwd), .type([0x1b, 0x5b, 0x33, 0x7e]))
        XCTAssertEqual(verdict(fwd, [.command]), .type([0x0b]))
        XCTAssertNotEqual(verdict(fwd), verdict(fwd, [.command]))
    }

    /// The ordering edge that matters most: ⌘ rows require `.control`
    /// absent, so a ⌃ combination stays SwiftTerm's own.
    func testControlIsNeverIntercepted() {
        XCTAssertEqual(verdict(left, [.command, .control]), .swiftTerm)
        XCTAssertEqual(verdict(del, [.command, .control]), .swiftTerm)
        XCTAssertEqual(verdict(letterA, [.control], "c"), .swiftTerm)
        XCTAssertEqual(verdict(letterA, [.control], "r"), .swiftTerm)
    }

    // MARK: a new line instead of a send

    func testShiftAndOptionReturnBothAddALine() {
        XCTAssertEqual(verdict(ret, [.shift]), .type([0x1b, 0x0d]))
        XCTAssertEqual(verdict(ret, [.option]), .type([0x1b, 0x0d]))
        XCTAssertEqual(verdict(enter, [.shift]), .type([0x1b, 0x0d]))
    }

    /// A plain Return is still a send: SwiftTerm's own.
    func testPlainReturnIsSwiftTermS() {
        XCTAssertEqual(verdict(ret), .swiftTerm)
        XCTAssertEqual(verdict(enter), .swiftTerm)
        XCTAssertEqual(verdict(ret, [.command]), .swiftTerm)
    }

    // MARK: Option, which is no longer Meta

    func testOptionRestoresTheFourThingsMetaGave() {
        XCTAssertEqual(verdict(del, [.option]), .type([0x1b, 0x7f]))
        XCTAssertEqual(verdict(fwd, [.option]), .type([0x1b, 0x64]))
        XCTAssertEqual(verdict(left, [.option]), .type([0x1b, 0x62]))
        XCTAssertEqual(verdict(right, [.option]), .type([0x1b, 0x66]))
    }

    /// ⌥ with a letter composes: SwiftTerm draws the layout's own character.
    func testOptionWithALetterComposes() {
        XCTAssertEqual(verdict(letterA, [.option], "a"), .swiftTerm)
        XCTAssertEqual(verdict(letterA, [.option], "e"), .swiftTerm)
    }

    /// The ⌥ rows are matched before the bare-key fall-through, and Shift
    /// riding along does not change them.
    func testOptionShiftStillMovesAWord() {
        XCTAssertEqual(verdict(left, [.option, .shift]), .type([0x1b, 0x62]))
        XCTAssertEqual(verdict(right, [.option, .shift]), .type([0x1b, 0x66]))
    }

    // MARK: the ⌘ rows that are not line edits

    func testClearIsTypedSoTheDaemonSEmulatorSeesIt() {
        XCTAssertEqual(verdict(letterA, [.command], "k"), .type([0x0c]))
        XCTAssertEqual(verdict(letterA, [.command], "K"), .type([0x0c]))
    }

    func testCopyPasteAndSelectAllAreSwiftTermSOwnMethods() {
        XCTAssertEqual(verdict(letterA, [.command], "c"), .app(.copy))
        XCTAssertEqual(verdict(letterA, [.command], "v"), .app(.paste))
        XCTAssertEqual(verdict(letterA, [.command], "a"), .app(.selectAll))
    }

    /// ⌘⌥O is SwiftTerm's hidden `optionAsMetaKey` toggle: consumed, so it
    /// can never flip Option back to Meta by accident.
    func testTheHiddenOptionAsMetaToggleIsConsumed() {
        XCTAssertEqual(verdict(letterA, [.command, .option], "o"), .nobody)
        XCTAssertEqual(verdict(left, [.command, .option]), .nobody)
        XCTAssertEqual(verdict(right, [.command, .option]), .nobody)
    }

    /// The window's and the app's: handed back to AppKit.
    func testTheWindowAndAppShortcutsAreHandedBack() {
        for key in ["w", "m", "h", "q", ","] {
            XCTAssertEqual(verdict(letterA, [.command], key), .swiftTerm, key)
        }
    }

    /// Consumed silently rather than reaching `doCommand`'s
    /// "Unhandle selector" print.
    func testTheUnhandledCommandKeysAreDroppedQuietly() {
        XCTAssertEqual(verdict(letterA, [.command], "x"), .nobody)
        XCTAssertEqual(verdict(letterA, [.command], "z"), .nobody)
        XCTAssertEqual(verdict(letterA, [.command, .shift], "z"), .nobody)
        XCTAssertEqual(verdict(up, [.command]), .nobody)
        XCTAssertEqual(verdict(down, [.command]), .nobody)
    }

    // MARK: everything else belongs to the agent

    /// Escape, Tab, Space, the arrows and every letter — the presses the
    /// rail's triage verbs were eating.
    func testTheOrdinaryKeysAllReachSwiftTerm() {
        for code: UInt16 in [53, 48, 49, 123, 124, 125, 126, 1, 2, 13] {
            XCTAssertEqual(verdict(code), .swiftTerm, "key \(code)")
        }
    }

    /// Shift-modified presses are never intercepted — that is what selects
    /// text while an application is tracking the mouse
    /// (`shiftBypassesMouseReporting`).
    func testShiftModifiedPressesAreSwiftTermS() {
        for code: UInt16 in [left, right, up, down, letterA, 53, 48] {
            XCTAssertEqual(verdict(code, [.shift], "a"), .swiftTerm,
                           "shift + \(code)")
        }
    }

    // MARK: the chunker

    func testChunksOnEmptyIsNothingAtAll() {
        XCTAssertEqual(TerminalKeys.chunks(Data(), limit: 8), [])
    }

    func testChunksBelowAndAtTheLimitIsOnePiece() {
        let exact = Data(repeating: 0x41, count: 8)
        XCTAssertEqual(TerminalKeys.chunks(exact, limit: 8), [exact])
        let under = Data(repeating: 0x41, count: 3)
        XCTAssertEqual(TerminalKeys.chunks(under, limit: 8), [under])
    }

    func testChunksOneOverTheLimitSplitsInOrder() {
        let data = Data((0..<9).map { UInt8($0) })
        let pieces = TerminalKeys.chunks(data, limit: 8)
        XCTAssertEqual(pieces.count, 2)
        XCTAssertEqual(pieces.reduce(Data(), +), data)
        for piece in pieces { XCTAssertLessThanOrEqual(piece.count, 8) }
    }

    func testChunksManyTimesTheLimitConcatenateToTheInput() {
        let data = Data((0..<1000).map { UInt8($0 % 251) })
        let pieces = TerminalKeys.chunks(data, limit: 64)
        XCTAssertEqual(pieces.count, 16)
        XCTAssertEqual(pieces.reduce(Data(), +), data)
        for piece in pieces { XCTAssertLessThanOrEqual(piece.count, 64) }
    }

    /// The shipped limit is under the daemon's own refusal
    /// (`TERMINAL_MAX_RAW_BYTES`, 256 KiB), so an ordinary long paste is cut
    /// here rather than refused whole there.
    func testTheShippedLimitIsBelowTheDaemonSRefusal() {
        XCTAssertLessThan(TerminalKeys.frameLimit, 256 * 1024)
        XCTAssertGreaterThan(TerminalKeys.frameLimit, 0)
    }

    // MARK: the caret

    /// All four booleans. The last two clauses are what stop this becoming
    /// the mirror image of the dictation theft it repairs.
    func testClaimsCaretOverEveryCombination() {
        XCTAssertTrue(TerminalKeys.claimsCaret(
            paneAttached: true, windowIsKey: true,
            currentIsTerminal: false, currentIsEditable: false))
        XCTAssertFalse(TerminalKeys.claimsCaret(
            paneAttached: false, windowIsKey: true,
            currentIsTerminal: false, currentIsEditable: false))
        XCTAssertFalse(TerminalKeys.claimsCaret(
            paneAttached: true, windowIsKey: false,
            currentIsTerminal: false, currentIsEditable: false))
        XCTAssertFalse(TerminalKeys.claimsCaret(
            paneAttached: true, windowIsKey: true,
            currentIsTerminal: true, currentIsEditable: false))
        XCTAssertFalse(TerminalKeys.claimsCaret(
            paneAttached: true, windowIsKey: true,
            currentIsTerminal: false, currentIsEditable: true))
    }

    /// Dictation may move the caret anywhere except out of a terminal.
    func testMayPromoteRefusesOnlyTheTerminal() {
        XCTAssertTrue(DictationFocus.mayPromote(
            currentIsTerminal: false, currentIsEditable: false))
        XCTAssertTrue(DictationFocus.mayPromote(
            currentIsTerminal: false, currentIsEditable: true))
        XCTAssertFalse(DictationFocus.mayPromote(
            currentIsTerminal: true, currentIsEditable: false))
        XCTAssertFalse(DictationFocus.mayPromote(
            currentIsTerminal: true, currentIsEditable: true))
    }

    /// And the `.flagsChanged` monitor promotes only for a modifier of the
    /// *recorded* shortcut — never for the Shift that starts a capital
    /// letter. A modifier-only shortcut is matched whole, side included.
    func testAModifierOnlyShortcutIsMatchedWithItsSide() {
        XCTAssertTrue(DictationFocus.isShortcutModifier(keyCode: 54, label: "R⌘"))
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 55, label: "R⌘"))
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 56, label: "R⌘"))
        XCTAssertTrue(DictationFocus.isShortcutModifier(keyCode: 55, label: "⌘"))
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 54, label: "⌘"))
    }

    /// A shortcut with a letter in it ("⌥⌘D", exactly what the recorder
    /// composes) carries no side and matches each of its modifiers on the
    /// glyph — otherwise it would promote nothing, ever, since the letter
    /// itself arrives as a `keyDown` no promote path watches.
    func testACombinationShortcutMatchesEachOfItsModifiers() {
        XCTAssertTrue(DictationFocus.isShortcutModifier(keyCode: 58, label: "⌥⌘D"))
        XCTAssertTrue(DictationFocus.isShortcutModifier(keyCode: 55, label: "⌥⌘D"))
        // Right-hand keys too: the label cannot distinguish a side.
        XCTAssertTrue(DictationFocus.isShortcutModifier(keyCode: 61, label: "⌥⌘D"))
        XCTAssertTrue(DictationFocus.isShortcutModifier(keyCode: 54, label: "⌥⌘D"))
        // And a modifier that is not in it still promotes nothing.
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 56, label: "⌥⌘D"))
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 59, label: "⌥⌘D"))
    }

    /// Nothing recorded promotes nothing at all, and a key that is not a
    /// modifier never matches.
    func testNoShortcutPromotesNothing() {
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 54, label: ""))
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 55, label: ""))
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 57, label: "⌘"))
        XCTAssertFalse(DictationFocus.isShortcutModifier(keyCode: 0, label: "⌥⌘D"))
    }
}
