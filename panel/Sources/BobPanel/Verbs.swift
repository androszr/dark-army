import Foundation

// The words for the verbs a person presses on an agent — Stop, Delete,
// Hide, Dismiss, Close terminal, Low priority — the plain label, the
// armed "really?" label and the spoken form. Foundation only, and
// **byte-pinned to the phone** from the marker line down
// (`ios/BobPhone/Verbs.swift`, `host/tests/test_verbs_shared.py`): edit
// both copies together, never one. The hover tooltips stay literal
// where they are drawn (`test_panel_tooltips.py`).
enum Verbs {
    /// One verb a person presses on an agent, spelled the same way on the
    /// Mac and on the phone. What the press *does* is the caller's; this
    /// table only owns the words.
    enum Verb: String, CaseIterable {
        case stop, delete, hide, dismiss, closeTerminal, lowPriority

        /// The plain label, before any press.
        var label: String {
            switch self {
            case .stop: return "Stop"
            case .delete: return "Delete"
            case .hide: return "Hide"
            case .dismiss: return "Dismiss"
            case .closeTerminal: return "Close terminal"
            case .lowPriority: return "Low priority"
            }
        }

        /// The "really?" label an armed verb wears until the second press.
        /// A verb that is never armed (`arms` false) answers its own label.
        var armedLabel: String {
            switch self {
            case .stop: return "Really stop?"
            case .delete: return "Really delete?"
            case .closeTerminal: return "Really close the terminal?"
            case .lowPriority: return "Really switch to low priority?"
            case .hide, .dismiss: return label
            }
        }

        /// Whether the verb is armed before it fires. Hide and Dismiss only
        /// hide something until it changes, so they fire on the first press.
        var arms: Bool { armedLabel != label }

        /// What VoiceOver reads for the button, plain or armed.
        func spoken(armed: Bool) -> String {
            let armed = armed && arms
            switch self {
            case .stop: return armed ? "Confirm stop" : "Stop this session"
            case .delete: return armed ? "Confirm delete" : "Delete this agent's record"
            case .hide: return "Hide until it changes"
            case .dismiss: return "Dismiss until it changes"
            case .closeTerminal: return armed ? "Confirm close terminal" : "Close terminal"
            case .lowPriority:
                return armed ? "Confirm switch to low priority" : "Switch to low priority"
            }
        }

        /// The sentence that says the press cannot be taken back, where
        /// that is true of the verb itself. Nil where it is not.
        var noUndo: String? {
            switch self {
            case .closeTerminal: return "Closes the tab and ends this agent. There is no undo."
            case .stop, .delete, .hide, .dismiss, .lowPriority: return nil
            }
        }
    }

    static let stop = Verb.stop
    static let delete = Verb.delete
    static let hide = Verb.hide
    static let dismiss = Verb.dismiss
    static let closeTerminal = Verb.closeTerminal
    static let lowPriority = Verb.lowPriority
}
