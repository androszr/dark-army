import Foundation

/// What the Worktrees screen ticks, orders and says — the pure half.
///
/// Phone-only: the Mac draws no such list, so there is no byte pin
/// (`Plans.swift`'s precedent). **Foundation only**, so `host/tests/
/// test_phone_worktrees.py` compiles it under `swiftc` with `CardMerge.swift`
/// and tables the rule, and `ios/BobPhoneTests/WorktreeRowsTests.swift` runs
/// the same table under XCTest. The words on each row (status, line) are the
/// daemon's and are drawn verbatim; only the tick rule and the button's
/// words live here.
///
/// A row may be ticked only where the daemon says it is mergeable
/// (`mergeable`: finished, hand-check settled, clean folder, a tip to echo)
/// **and** the card screen's own MERGE rule holds over the live snapshot
/// card (`CardMerge.offered`), so the list and the card cannot disagree.
enum WorktreeRows {
    /// The most cards one press may carry (`board.MAX_BATCH_CARDS`).
    static let maximum = 8

    /// The live card's facts the tick rule reads: the same four `CardMerge`
    /// takes on the card screen.
    struct CardFacts: Equatable {
        var column: String
        var branch: String
        var mergeState: String
        var manualDue: Bool
        var mergeOffered: Bool
    }

    /// One listed row's facts for ordering and the press.
    struct Entry: Equatable {
        var id: String
        var branchTip: String
        var mergeable: Bool
    }

    /// Whether a row can be ticked: the Mac honours the press, the daemon
    /// calls the row mergeable with a tip to echo, the card is still on the
    /// board and `CardMerge.offered` says MERGE would show.
    ///
    /// A row the Mac calls `nothing` / `merged` / `gone` / `no_card` is never
    /// ticked, whatever else it says: its branch has nothing ahead of main
    /// (an older Mac offered those as mergeable).
    static func tickable(rowMergeable: Bool, branchTip: String,
                         card: CardFacts?, mergeWritable: Bool,
                         status: String = "") -> Bool {
        guard mergeWritable, rowMergeable, !branchTip.isEmpty,
              !finishedStatuses.contains(status),
              let card else { return false }
        return CardMerge.offered(column: card.column, branch: card.branch,
                                 mergeState: card.mergeState,
                                 manualDue: card.manualDue,
                                 daemonOffers: card.mergeOffered)
    }

    /// The statuses with nothing left to merge: the folded group.
    static let finishedStatuses: Set<String> = ["merged", "gone", "no_card", "nothing"]

    /// What a person can do with a row, which is how the screen groups it.
    enum Group: CaseIterable {
        case ready, attention, notReady, finished
    }

    /// The one grouping rule. A row with nothing ahead of main (`nothing`)
    /// is finished, whatever its `mergeable` says, so it can never sit
    /// under READY TO MERGE.
    static func group(status: String, tickable: Bool) -> Group {
        if finishedStatuses.contains(status) { return .finished }
        if tickable { return .ready }
        switch status {
        case "conflict", "checks_failed", "blocked", "unreadable": return .attention
        default: return .notReady
        }
    }

    /// The project chips: the rows' own project labels, unique, non-empty,
    /// sorted (`FleetProjects.names`' rule).
    static func projectNames(_ projects: [String]) -> [String] {
        Array(Set(projects.filter { !$0.isEmpty })).sorted()
    }

    /// `""` is ALL and matches every row, an unnamed project included.
    static func matches(project: String, active: String) -> Bool {
        active.isEmpty || project == active
    }

    /// Rows per project label, for the chips' numbers.
    static func counts(_ projects: [String]) -> [String: Int] {
        projects.reduce(into: [:]) { $0[$1, default: 0] += 1 }
    }

    /// Every row hidden by the filter.
    static func filterLine(project: String) -> String {
        "no \(project) worktrees · ALL shows every project"
    }

    /// Whether DELETE is drawn: the Mac honours the press, says this row's
    /// folder or branch may go (`removable`) and the status is one with
    /// nothing left to merge.
    static func removable(rowRemovable: Bool, status: String, writable: Bool) -> Bool {
        writable && rowRemovable && finishedStatuses.contains(status)
    }

    /// DELETE's words, and the confirming question once armed.
    static func removeVerb(armed: Bool) -> String {
        armed ? "Delete this folder?" : "DELETE"
    }

    /// The ticked ids in the list's own order — the order the Mac merges in.
    static func orderedIds(ticked: Set<String>, listed: [String]) -> [String] {
        listed.filter { ticked.contains($0) }
    }

    /// The branch tip of each id, in the same order, for `expected_tips`.
    static func tips(for ids: [String], entries: [Entry]) -> [String] {
        ids.map { id in entries.first(where: { $0.id == id })?.branchTip ?? "" }
    }

    /// The MERGE button's words.
    static func verb(count: Int) -> String { "MERGE \(count)" }

    /// The confirming question once armed.
    static func armedVerb(count: Int) -> String { "Merge \(count) into main?" }

    /// What the button says past the bound.
    static let tooManyLine = "At most \(maximum) at a time"

    /// The key the screen re-reads on: each listed card's id, column, state and line
    /// off the live snapshot, so the list is read again exactly when a merge
    /// on it moves.
    static func fetchKey(cards: [(id: String, column: String, mergeState: String, mergeLine: String)],
                         listed: [String]) -> String {
        listed.map { id in
            guard let card = cards.first(where: { $0.id == id }) else {
                return id + ":::"
            }
            return id + ":" + card.column + ":" + card.mergeState + ":" + card.mergeLine
        }.joined(separator: "|")
    }
}
