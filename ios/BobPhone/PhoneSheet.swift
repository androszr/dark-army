import Foundation

/// Presentation decisions independent of SwiftUI, executable by host tests.
/// The table reads the sheet's kind and, for a card, the card's column: the
/// agent and a card in In progress are the two sheets answered from half
/// height, everything else opens large.
enum PhoneSheetKind: String, CaseIterable {
    case agent, card, catchUp, decision, workFile, notification

    /// The one board column whose card is answered from, and the only place
    /// the phone's sheet code spells it.
    static let answeringColumn = "in_progress"

    static func answers(_ kind: PhoneSheetKind, column: String?) -> Bool {
        kind == .agent || (kind == .card && column == answeringColumn)
    }

    static func initialDetent(_ kind: PhoneSheetKind, column: String?, accessibility: Bool) -> SheetDetent {
        accessibility || !answers(kind, column: column) ? .large : .medium
    }

    static func detents(_ kind: PhoneSheetKind, column: String?, accessibility: Bool) -> [SheetDetent] {
        initialDetent(kind, column: column, accessibility: accessibility) == .medium ? [.medium, .large] : [.large]
    }

    /// Growing is one-way: hiding a keyboard must not collapse a typed answer.
    static func onKeyboard(_ current: SheetDetent) -> SheetDetent { .large }

    static func presentsTerminalFullScreen(_ kind: PhoneSheetKind) -> Bool { kind == .agent }
}

enum SheetDetent { case medium, large }
