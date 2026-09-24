import Foundation

/// The `?cards=delta` merge: rebuild the board's card list from a delta
/// frame and the board the panel already holds.
///
/// The daemon sends a present board carrying `cards_delta: true`, the cards
/// whose news moved, and `card_order` — every card id of the board in the
/// daemon's own order (`CARD_ORDER_SQL`). The list is rebuilt from that
/// order, taking each id from the frame's changed cards first and the held
/// list second, so the result is the daemon's cards verbatim: a card the
/// order no longer names is dropped, and nothing is re-sorted here.
///
/// An id the order names that neither list holds is reported in `missing`
/// — the held board is not the one the daemon assumed — and the caller asks
/// for a whole snapshot. Pure and Foundation-only, so it is tabled in
/// `BoardCardDeltaTests`.
enum BoardCardDelta {
    static func merge(order: [String], changed: [BoardCard],
                      held: [BoardCard]) -> (cards: [BoardCard], missing: [String]) {
        var byId: [String: BoardCard] = [:]
        byId.reserveCapacity(changed.count + held.count)
        for card in held where byId[card.id] == nil {
            byId[card.id] = card
        }
        // The frame's copy wins over the held one.
        for card in changed {
            byId[card.id] = card
        }
        var cards: [BoardCard] = []
        cards.reserveCapacity(order.count)
        var missing: [String] = []
        for id in order {
            if let card = byId[id] {
                cards.append(card)
            } else {
                missing.append(id)
            }
        }
        return (cards, missing)
    }
}
