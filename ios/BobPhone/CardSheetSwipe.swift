import Foundation

/// The Board tab's row order as a rule, with no view in it. Read by
/// `BoardView.cardsStack` to draw the row and by `PhoneSheetFrame` to find the
/// neighbour of an In progress card's sheet, so the two cannot disagree. The
/// search query is deliberately not an input: it is transient view state on
/// the tab, and a card opened from Needs you has no search in play.
///
/// Foundation only, so the host suite compiles and runs it under `swiftc`
/// (`host/tests/test_phone_card_sheet_swipe.py`) and the phone job runs the
/// same table (`CardSheetSwipeTests.swift`).
enum CardSheetSwipe {
    /// One drawn run of tiles. `name` is the header the row draws (`""` for
    /// the flat single group, the project name, or `ungroupedName`), so it is
    /// also a stable `ForEach` id.
    struct Group<Card> {
        let name: String
        let cards: [Card]
    }

    /// The header drawn over cards that name no project.
    static let ungroupedName = "—"

    /// Cards under project headings only when they span more than one named
    /// project, projects sorted, the unnamed cards last; otherwise exactly one
    /// flat group in the order given (the snapshot's order).
    static func drawnGroups<Card>(_ cards: [Card], project: (Card) -> String) -> [Group<Card>] {
        let names = Set(cards.map(project).filter { !$0.isEmpty })
        guard names.count > 1 else { return [Group(name: "", cards: cards)] }
        var groups = names.sorted().map { name in
            Group(name: name, cards: cards.filter { project($0) == name })
        }
        let ungrouped = cards.filter { project($0).isEmpty }
        if !ungrouped.isEmpty { groups.append(Group(name: ungroupedName, cards: ungrouped)) }
        return groups
    }

    /// The ids in drawn order, a repeated id dropped keeping its first place:
    /// the list `AgentSheetSwipe.neighbour(of:in:_:)` walks.
    static func order<Card>(_ cards: [Card], project: (Card) -> String, id: (Card) -> String) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for group in drawnGroups(cards, project: project) {
            for card in group.cards {
                let key = id(card)
                if seen.insert(key).inserted { out.append(key) }
            }
        }
        return out
    }
}
