import AppKit

/// **Who owns a key press while the hosted terminal holds the caret.**
///
/// One pure table, no AppKit event in the signature — a key code, the
/// modifier flags and `charactersIgnoringModifiers` — so every row is a test
/// case without a window. `main.swift`'s local `NSEvent` monitor is the only
/// caller and it performs the verdict; it decides nothing itself.
///
/// Three verdicts and a silence:
///
/// * `.type` — bytes AppKit will not carry to the pty by itself. A key
///   carrying ⌘ never reaches SwiftTerm's own handling: `keyDown` hands it to
///   `interpretKeyEvents`, which turns it into a standard editing selector,
///   and `doCommand(by:)` answers one it has no case for by printing
///   "Unhandle selector" and sending nothing. Return is not a kitty
///   functional key either, so ⇧⏎ arrives with its modifiers already
///   discarded and was a plain CR.
/// * `.app` — a thing SwiftTerm itself does, called directly rather than
///   left to the menu: copy, paste, select all.
/// * `.swiftTerm` — handed back to AppKit, which is correct for Escape, Tab,
///   Return, Space, every letter, every ⌃ combination and every ⌥ character
///   composition.
/// * `.nobody` — consumed and dropped, so a stray shortcut cannot fire.
///
/// **Ordering inside `verdict` is load-bearing.** ⇧⏎ / ⌥⏎ are matched first,
/// because Shift alone would otherwise fall into the unmodified rung. Then
/// the ⌥ rows — before the bare-key fall-through — so the word moves and
/// deletions Option used to provide as Meta are still there now that
/// `optionAsMetaKey` is false. Then the ⌘ rows, which require `.command`
/// present and `.control` absent, exactly as the old `TerminalFocus.keyBytes`
/// did, so ⌘⌃← is SwiftTerm's and not ⌘←'s. Everything else is SwiftTerm's.
enum TerminalKeys {

    /// A thing SwiftTerm does to itself, named rather than typed.
    enum Action: Equatable {
        case copy
        case paste
        case selectAll
    }

    enum Verdict: Equatable {
        case type([UInt8])
        case app(Action)
        case swiftTerm
        case nobody
    }

    // Virtual key codes, named once.
    static let deleteKey: UInt16 = 51
    static let forwardDeleteKey: UInt16 = 117
    static let leftKey: UInt16 = 123
    static let rightKey: UInt16 = 124
    static let upKey: UInt16 = 126
    static let downKey: UInt16 = 125
    static let returnKey: UInt16 = 36
    static let keypadEnterKey: UInt16 = 76

    /// `[0x1b, 0x0d]` — ESC CR, the newline-without-send the assistants
    /// already accept. One constant, two rows: ⇧⏎ and ⌥⏎.
    ///
    /// The kitty keyboard protocol would be the modern answer, but nothing on
    /// Dark Army's pty ever replies to `CSI ? u`: `vtgrid._csi` drops every private
    /// CSI whose mark is not `?`, and `Screen.paint()` replays DEC private
    /// modes alone, so `keyboardEnhancementFlags` is empty on both sides for
    /// the life of every hosted session.
    static let newlineWithoutSend: [UInt8] = [0x1b, 0x0d]

    /// The whole table. `characters` is `charactersIgnoringModifiers`.
    static func verdict(keyCode: UInt16,
                        flags: NSEvent.ModifierFlags,
                        characters: String?) -> Verdict {
        let command = flags.contains(.command)
        let control = flags.contains(.control)
        let option = flags.contains(.option)
        let shift = flags.contains(.shift)
        let letter = (characters ?? "").lowercased()
        let isReturn = keyCode == returnKey || keyCode == keypadEnterKey

        // ⇧⏎ / ⌥⏎ — a line, not a send: both `.type([0x1b, 0x0d])`. First,
        // because Shift alone would otherwise land in the unmodified rung
        // below, and because Option is no longer Meta ⌥⏎ has to be stated.
        if isReturn, !command, !control, shift || option {
            return .type(newlineWithoutSend)
        }

        // ⌥ rows. Option is no longer Meta (`TerminalLook.apply(to:)` sets
        // `optionAsMetaKey = false` so a Polish or German layout composes its
        // own characters), so the four things Meta gave are restored here by
        // name. Matched before the bare-key fall-through, and skipped
        // entirely while ⌘ or ⌃ is down.
        if option, !command, !control {
            switch keyCode {
            case deleteKey:        return .type([0x1b, 0x7f])  // ⌥⌫ word back
            case forwardDeleteKey: return .type([0x1b, 0x64])  // ⌥⌦ ESC d
            case leftKey:          return .type([0x1b, 0x62])  // ⌥← ESC b
            case rightKey:         return .type([0x1b, 0x66])  // ⌥→ ESC f
            default:               return .swiftTerm           // compose
            }
        }

        // ⌘ rows. `.control` absent is part of the match, so ⌘⌃← stays
        // SwiftTerm's.
        if command, !control {
            if option {
                // ⌘⌥O is SwiftTerm's own hidden `optionAsMetaKey` toggle and
                // is exactly the accident this plan removes; ⌘⌥← / ⌘⌥→ reach
                // `doCommand`'s unhandled-selector print.
                if letter == "o" { return .nobody }
                if keyCode == leftKey || keyCode == rightKey { return .nobody }
                return .swiftTerm
            }
            switch keyCode {
            case deleteKey:        return .type([0x15])  // ⌘⌫ → ^U, clear the line
            case forwardDeleteKey: return .type([0x0b])  // ⌘⌦ → ^K, clear to the end
            case leftKey:          return .type([0x01])  // ⌘← → ^A, start of the line
            case rightKey:         return .type([0x05])  // ⌘→ → ^E, end of the line
            case upKey, downKey:   return .nobody
            default:               break
            }
            switch letter {
            // ^L is the clear a shell or a CLI performs itself, so the
            // daemon's emulator sees the real clear and it survives a
            // reattach — a local clear would be undone by the next paint.
            case "k": return .type([0x0c])
            case "c": return .app(.copy)
            case "v": return .app(.paste)
            case "a": return .app(.selectAll)
            // The window's and the app's: closing the panel hides it and the
            // pty belongs to the broker, so nothing is destroyed.
            case "w", "m", "h", "q", ",": return .swiftTerm
            // Consumed silently rather than reaching `doCommand`'s
            // "Unhandle selector" print.
            case "x", "z": return .nobody
            default: return .swiftTerm
            }
        }

        // Unmodified: fn-⌫ lands on `deleteForward:`, which SwiftTerm has no
        // case for either.
        if !command, !control, !option {
            return keyCode == forwardDeleteKey
                ? .type([0x1b, 0x5b, 0x33, 0x7e])
                : .swiftTerm
        }

        return .swiftTerm
    }

    /// **Whether the pane may take the caret.**
    ///
    /// Claim only where the pane is drawn, the window is key, the terminal
    /// does not already hold it, and nothing editable does — that last clause
    /// is what stops this becoming the mirror image of the dictation theft it
    /// repairs: a reply field or the board's search box keeps the caret a
    /// person put there.
    static func claimsCaret(paneAttached: Bool,
                            windowIsKey: Bool,
                            currentIsTerminal: Bool,
                            currentIsEditable: Bool) -> Bool {
        paneAttached && windowIsKey && !currentIsTerminal && !currentIsEditable
    }

    /// The largest slice one `I` frame carries.
    ///
    /// The daemon refuses a raw write over `TERMINAL_MAX_RAW_BYTES`
    /// (256 KiB) whole, and a refused paste lands as an orange note instead
    /// of as text. A multi-line paste is one `insertText` and so one frame,
    /// so a long one has to be cut here; the stream serialises every write on
    /// its own queue, which is what keeps the pieces in order.
    static let frameLimit = 128 * 1024

    /// Split `data` into pieces no larger than `limit`, in order. The
    /// concatenation is the input; an empty input is no pieces at all.
    static func chunks(_ data: Data, limit: Int = frameLimit) -> [Data] {
        guard limit > 0 else { return data.isEmpty ? [] : [data] }
        guard data.count > limit else { return data.isEmpty ? [] : [data] }
        var out: [Data] = []
        var start = data.startIndex
        while start < data.endIndex {
            let end = data.index(start, offsetBy: limit, limitedBy: data.endIndex)
                ?? data.endIndex
            out.append(Data(data[start..<end]))
            start = end
        }
        return out
    }
}
