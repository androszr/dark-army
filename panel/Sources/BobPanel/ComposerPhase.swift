import Foundation

/// Whether the new-card form has opened its second half, as one pure rule.
///
/// Phase one is the idea, Prepare, the assistant and the attachments.
/// Phase two is everything else, revealed after Prepare answers or the
/// person presses fill in myself. Foundation only: the phone carries a
/// byte-equal copy at the bottom of `ComposerView.swift`
/// (`host/tests/test_composer_phase.py` pins the two equal).
enum ComposerPhase {
    /// Why the second half of the form was opened; picks the caption.
    enum Reveal { case prepared, manual, resumed }
    /// Phase two is drawn when the flag was set — by Prepare or the link —
    /// or when any phase-two field already holds text (an older draft has
    /// no flag). Whitespace-only text counts as empty.
    static func expanded(flag: Bool, fields: [String]) -> Bool {
        flag || fields.contains {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }
    /// The caption above the opened form, `nil` where none is drawn.
    static func caption(for reveal: Reveal) -> String? {
        switch reveal {
        case .prepared:
            return "Dark Army drafted these from your idea — every box is yours to correct"
        case .manual:
            return "Every box is yours to fill — Prepare can still draft them from your idea"
        case .resumed:
            return nil
        }
    }
    static let fillMyselfLabel = "fill in myself"
    /// The words beside a held Save while the form is short.
    static let holdReason = "Prepare first, or fill it in yourself"
}
