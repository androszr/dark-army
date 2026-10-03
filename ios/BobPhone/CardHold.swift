import Foundation

/// The phone Board tab's hold: a half-second press on a Prep or Backlog card
/// enters that row's select mode with the card already ticked, and a bar
/// above the tab bar carries the count, the row's one verb and the assistant
/// row (`docs/phone-contract.md`, *A held card opens the batch bar*).
///
/// The rule is **the SELECT word's own gate** plus the two things a gesture
/// adds (no other row is selecting; the press is not out), so a hold never
/// opens a bar whose verb could not fire, and it reads
/// `PhoneRowSelection` rather than re-deriving it. Foundation only, so
/// `swiftc` can run it from the host suite
/// (`host/tests/test_phone_card_hold.py`); the XCTest twin is
/// `ios/BobPhoneTests/CardHoldTests.swift`.
enum PhoneCardHold {
    /// How long a finger rests before the card is held.
    static let minimumDuration: TimeInterval = 0.5

    /// How far, in points, a finger may travel and still be a hold. Further
    /// is a scroll or a swipe, and the hold fails.
    static let maximumDistance: CGFloat = 10

    /// The rows a hold may open: the two whose batch verb the Mac takes.
    static let rows = ["prep", "backlog"]

    /// Whether holding `card` in `column` enters select mode: the row is one
    /// of `rows`, the Mac takes the row's batch verb (`supported`), the row's
    /// press is not out, no row is selecting, the card could be ticked, and
    /// at least two of the drawn `cards` could be (the SELECT word's gate).
    static func entersSelect(column: String, card: BoardCard, cards: [BoardCard],
                             supported: Bool, pressOut: Bool, selecting: String?,
                             dispatchEnabled: Bool) -> Bool {
        rows.contains(column)
            && supported
            && !pressOut
            && selecting == nil
            && PhoneRowSelection.tickable(column, card: card,
                                          dispatchEnabled: dispatchEnabled)
            && PhoneRowSelection.offersSelect(column, cards: cards,
                                              dispatchEnabled: dispatchEnabled)
    }

    /// How long after a hold its release may still be swallowed. A release
    /// that never reaches the tile (the tile is swapped for its select-mode
    /// twin mid-touch) must not eat a later, deliberate tap.
    static let releaseWindow: TimeInterval = 1

    /// Whether a press on `tapped` is the release that follows the hold on
    /// `held` (at `heldAt`), which must not untick the card the hold just
    /// ticked: the same card, within `releaseWindow`.
    static func consumesTap(held: String?, tapped: String,
                            heldAt: Date?, now: Date) -> Bool {
        guard let held, let heldAt, held == tapped else { return false }
        let age = now.timeIntervalSince(heldAt)
        return age >= 0 && age <= releaseWindow
    }

    /// The bar's count line.
    static func countLine(count: Int) -> String {
        "\(count) selected"
    }

    /// What a screen reader says for the bar as one group; empty for no row.
    static func barLabel(column: String?, count: Int) -> String {
        switch column {
        case "prep": return "Batch refine, \(count) selected"
        case "backlog": return "Batch start, \(count) selected"
        default: return ""
        }
    }
}
