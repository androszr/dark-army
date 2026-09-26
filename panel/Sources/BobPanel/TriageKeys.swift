import Foundation

// The triage letters, once. The key monitor (`KeyMonitor.swift`) looks a
// typed character up here, and the **keys** legend under the brand bar is
// composed from the same list, so a key the legend names is a key the
// monitor performs and the two cannot disagree. Foundation only: the
// table is run under `swiftc` by `host/tests/test_panel_keyboard_reach.py`.
// The words come from `Verbs`, the word list the phone shares.

/// The letters the monitor performs by character, and the arrows and
/// chords it reads by key code, named for the legend.
enum TriageKeys {
    /// Every key the monitor matches by its character, lowercased, with the
    /// intent it sends and the word the legend prints beside it.
    static let table: [(key: String, intent: TriageIntent, word: String)] = [
        (key: " ", intent: .open, word: "unfold"),
        (key: "d", intent: .dismiss, word: Verbs.dismiss.label.lowercased()),
        (key: "s", intent: .stop, word: Verbs.stop.label.lowercased()),
        (key: "r", intent: .retire, word: Verbs.delete.label.lowercased()),
        (key: "w", intent: .wrapUp, word: Verbs.closeTerminal.label.lowercased()),
        (key: "/", intent: .focusFilter, word: "filter"),
        (key: "?", intent: .toggleKeys, word: "keys"),
    ]

    /// Keys the monitor reads by key code rather than by character. Listed
    /// so the legend names them too; the monitor's own `switch` performs
    /// them.
    static let leading: [(glyph: String, word: String)] = [
        (glyph: "↑↓", word: "select"),
        (glyph: "⏎", word: "jump"),
    ]
    static let trailing: [(glyph: String, word: String)] = [
        (glyph: "⌃⇥", word: "rail tabs"),
        (glyph: "⎋", word: "back"),
    ]

    /// The intent a typed character sends, or nil where it is not a triage
    /// key — nil is the monitor handing the press back to AppKit.
    static func intent(for character: String) -> TriageIntent? {
        table.first { $0.key == character }?.intent
    }

    /// Return, keypad Enter and Space, the keys that press a focused control.
    static let pressKeyCodes: Set<UInt16> = [36, 76]
    static let pressCharacter = " "

    /// Whether this press belongs to the control holding the keyboard focus
    /// rather than to triage. With Full Keyboard Access a person Tabs onto a
    /// button, an inbox entry or a card tile; Return and Space must then
    /// press *that*, not jump or unfold the selected row. Every other key —
    /// the arrows, the letters — stays triage, and with nothing focused
    /// Return and Space are triage exactly as before. The monitor performs
    /// this answer and decides nothing (`TerminalKeys.verdict`'s shape).
    static func handsToControl(keyCode: UInt16, characters: String?,
                               controlFocused: Bool) -> Bool {
        guard controlFocused else { return false }
        return pressKeyCodes.contains(keyCode) || characters == pressCharacter
    }

    /// The arrows, which move the triage selection.
    static let selectionKeyCodes: Set<UInt16> = [123, 124, 125, 126]

    /// Whether this press moves the selection and so must end the focused
    /// control's claim first. The selection and the keyboard focus are never
    /// two cursors: after an arrow, Return and Space act on the row the
    /// arrow chose, never on a control focused before it — which might be a
    /// verb on the board nobody can see.
    static func endsControlFocus(keyCode: UInt16) -> Bool {
        selectionKeyCodes.contains(keyCode)
    }

    /// How a key is drawn in the legend: Space as its symbol, a letter in
    /// capitals as it sits on the keycap.
    static func glyph(for key: String) -> String {
        key == " " ? "⎵" : key.uppercased()
    }
}

/// The one-line shortcut legend the **keys** chip opens.
enum TriageLegend {
    static var line: String {
        let parts = TriageKeys.leading.map { "\($0.glyph) \($0.word)" }
            + TriageKeys.table.map { "\(TriageKeys.glyph(for: $0.key)) \($0.word)" }
            + TriageKeys.trailing.map { "\($0.glyph) \($0.word)" }
        return parts.joined(separator: " · ")
    }
}
