import AppKit
import XCTest
@testable import BobPanel

final class DictationTests: XCTestCase {

    func testSidedLabelsNameTheRightHandKeys() {
        XCTAssertEqual(DictationKey.sidedLabel[54], "R⌘")
        XCTAssertEqual(DictationKey.sidedLabel[55], "⌘")
        XCTAssertEqual(DictationKey.sidedLabel[60], "R⇧")
        XCTAssertEqual(DictationKey.sidedLabel[61], "R⌥")
        XCTAssertEqual(DictationKey.sidedLabel[62], "R⌃")
    }

    func testEverySidedLabelFitsTheEightCharacterCeiling() {
        for (code, label) in DictationKey.sidedLabel {
            XCTAssertLessThanOrEqual(
                label.count, 8,
                "sidedLabel[\(code)] = \(label) exceeds 8 characters")
        }
    }

    func testModifierFlagCoversExactlyTheEightPushToTalkCodes() {
        let codes = Set(DictationKey.modifierFlag.keys)
        XCTAssertEqual(codes, [54, 55, 56, 58, 59, 60, 61, 62])
        XCTAssertNil(DictationKey.modifierFlag[57])  // Caps Lock
        XCTAssertNil(DictationKey.modifierFlag[63])  // Fn
        XCTAssertEqual(Set(DictationKey.sidedLabel.keys), codes)
    }

    // DictateButton's AppKit mouseDown/mouseUp path is not exercised here:
    // XCTest has no hosting window. available(_:) is the gate that is testable.

    func testPromoteWithoutAWindowIsANoOp() {
        // No hosting window in XCTest; must not trap.
        DictationFocus.promote(reason: "test")
        DictationFocus.logGate(whisper: true, trusted: true, label: "R⌘")
        DictationFocus.logGate(whisper: false, trusted: false, label: "")
    }

    func testAvailableRequiresAllThreeGates() {
        func settings(whisper: Bool, trusted: Bool, label: String)
            -> DaemonClient.Settings {
            var s = DaemonClient.Settings()
            s.macwhisperInstalled = whisper
            s.accessibilityTrusted = trusted
            s.dictationShortcutLabel = label
            return s
        }
        XCTAssertTrue(DictateButton.available(
            settings(whisper: true, trusted: true, label: "R⌘")))
        XCTAssertFalse(DictateButton.available(
            settings(whisper: false, trusted: true, label: "R⌘")))
        XCTAssertFalse(DictateButton.available(
            settings(whisper: true, trusted: false, label: "R⌘")))
        XCTAssertFalse(DictateButton.available(
            settings(whisper: true, trusted: true, label: "")))
        XCTAssertFalse(DictateButton.available(
            settings(whisper: false, trusted: false, label: "")))
    }

    // MARK: - The caret an outside dictation app has to find

    func testEditingTextViewIgnoresANilWindow() {
        XCTAssertNil(DictationFocus.editingTextView(in: nil))
        XCTAssertFalse(DictationFocus.isEditing(nil))
        XCTAssertFalse(DictationFocus.syncAXFocus(window: nil))
    }

    /// A window with a caret in an editable field answers yes, and the field
    /// comes back decorated as a text area — which is the whole of what an
    /// AX-driven dictation app looks for.
    func testEditingTextViewFindsAnEditableFirstResponder() {
        let window = Self.makeWindow()
        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 200, height: 40))
        tv.isEditable = true
        window.contentView?.addSubview(tv)
        XCTAssertTrue(window.makeFirstResponder(tv))

        XCTAssertTrue(DictationFocus.editingTextView(in: window) === tv)
        XCTAssertTrue(DictationFocus.isEditing(window))
        XCTAssertTrue(DictationFocus.syncAXFocus(window: window))
        XCTAssertEqual(tv.accessibilityRole(), .textArea)
        // `isAccessibilityFocused()` is deliberately not asserted: it reads
        // back the real focus, which an offscreen window in XCTest never has
        // — the setter is still what a live key window needs.
    }

    /// Read-only text is not a place a transcript may land, and a hidden one
    /// must not hold the keyboard: both are the letter verbs' floor.
    func testEditingTextViewRefusesReadOnlyAndHiddenFields() {
        let window = Self.makeWindow()
        let tv = NSTextView(frame: NSRect(x: 0, y: 0, width: 200, height: 40))
        tv.isEditable = false
        window.contentView?.addSubview(tv)
        window.makeFirstResponder(tv)
        XCTAssertNil(DictationFocus.editingTextView(in: window))

        tv.isEditable = true
        window.makeFirstResponder(tv)
        XCTAssertNotNil(DictationFocus.editingTextView(in: window))
        tv.isHidden = true
        XCTAssertNil(DictationFocus.editingTextView(in: window))
        XCTAssertFalse(DictationFocus.isEditing(window))
    }

    /// A window with nobody typing answers no, so triage keeps the alphabet.
    func testEditingTextViewSaysNoWithoutACaret() {
        let window = Self.makeWindow()
        window.makeFirstResponder(nil)
        XCTAssertNil(DictationFocus.editingTextView(in: window))
        XCTAssertFalse(DictationFocus.isEditing(window))
    }

    private static func makeWindow() -> NSWindow {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 300, height: 200),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false)
        window.contentView = NSView(frame: window.contentLayoutRect)
        return window
    }
}
