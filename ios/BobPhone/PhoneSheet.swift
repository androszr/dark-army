import Foundation

/// Presentation decisions independent of SwiftUI, executable by host tests.
enum PhoneSheetKind: String, CaseIterable {
    case agent, card, catchUp, decision, workFile, notification

    static func initialDetent(_ kind: PhoneSheetKind, accessibility: Bool) -> SheetDetent {
        accessibility || kind != .agent ? .large : .medium
    }

    static func detents(_ kind: PhoneSheetKind, accessibility: Bool) -> [SheetDetent] {
        initialDetent(kind, accessibility: accessibility) == .medium ? [.medium, .large] : [.large]
    }

    /// Growing is one-way: hiding a keyboard must not collapse a typed answer.
    static func onKeyboard(_ current: SheetDetent) -> SheetDetent { .large }

    static func presentsTerminalFullScreen(_ kind: PhoneSheetKind) -> Bool { kind == .agent }
}

enum SheetDetent { case medium, large }
