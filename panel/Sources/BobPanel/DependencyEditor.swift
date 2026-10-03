import SwiftUI

/// The waits-on control: one row per card (title, the daemon's met bit as a
/// word where it is known, ✕) and an **Add…** menu over this project's other
/// cards that are not Done and not already listed. Bound to a `Binding` of
/// newline-joined ids — the store's own shape — so the composer's draft and
/// the saved card's WAITS ON section draw and edit one control and cannot
/// drift. The store still refuses a loop, a self-wait and another project in
/// words whatever this offers.
struct DependencyEditor: View {
    @Binding var selected: String
    let root: String
    let cards: [BoardCard]
    /// The card being edited, so it never offers itself. `""` in the composer.
    var selfId: String = ""
    /// The daemon's resolved rows for a saved card (title, column, met).
    /// Empty in the composer, where rows come from `cards` and carry no word.
    var resolved: [CardDependency] = []
    /// A saved card shows its own "Waits on nothing." line elsewhere.
    var emptyNote = ""
    /// False on a card in Done, which offers no **Add…**.
    var canAdd = true

    /// The ids the rows show, in stored order: the daemon's resolved rows
    /// when given, else the draft's ids that still name a card on the board.
    static func rowIds(selected: String, cards: [BoardCard],
                       resolved: [CardDependency]) -> [String] {
        if !resolved.isEmpty { return resolved.map(\.id) }
        let known = Set(cards.map(\.id))
        return BoardCard.stages(selected).filter { known.contains($0) }
    }

    /// What **Add…** may offer: cards of `root`, not `selfId`, not already
    /// `listed`, none in Done — and nothing once the list holds the store's
    /// eight (`board.MAX_BLOCKERS`, which would otherwise cut a ninth
    /// without a word).
    static func choices(root: String, selfId: String, listed: [String],
                        in cards: [BoardCard]) -> [BoardCard] {
        let held = Set(listed)
        guard held.count < BoardCard.maxDependencies else { return [] }
        return cards.filter {
            $0.root == root && $0.id != selfId
                && !held.contains($0.id) && $0.column != "done"
        }
    }

    /// The ids of this project's cards Prepare's suggestion may land on:
    /// same root, not in Done.
    static func listedIds(root: String, in cards: [BoardCard]) -> Set<String> {
        Set(cards.filter { $0.root == root && $0.column != "done" }.map(\.id))
    }

    /// The daemon's met bit in words — the same three `dependency_line`
    /// uses. `met` is never re-derived here; the column only says *why*.
    static func word(_ dep: CardDependency) -> String {
        if dep.column == "done" { return "done" }
        return dep.met ? "check pending" : "not yet"
    }

    private var ids: [String] {
        Self.rowIds(selected: selected, cards: cards, resolved: resolved)
    }

    private func write(_ list: [String]) {
        selected = list.joined(separator: "\n")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(ids, id: \.self) { id in
                let dep = resolved.first { $0.id == id }
                let title = dep?.title ?? cards.first { $0.id == id }?.title ?? ""
                HStack(spacing: 8) {
                    Text(title.isEmpty ? "untitled" : title)
                        .font(.system(size: 11))
                        .fixedSize(horizontal: false, vertical: true)
                    if let dep {
                        Text(Self.word(dep))
                            .font(.system(size: 10))
                            .foregroundStyle(dep.met ? Theme.phosphor : Theme.dim)
                    }
                    Spacer(minLength: 8)
                    Button("\u{2715}") {
                        write(ids.filter { $0 != id })
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Stop waiting on \(title)")
                    .clickable()
                }
            }
            let options = Self.choices(root: root, selfId: selfId,
                                       listed: ids, in: cards)
            if canAdd, !options.isEmpty {
                Menu("Add\u{2026}") {
                    ForEach(options) { other in
                        Button(other.title.isEmpty ? "untitled" : other.title) {
                            write(ids + [other.id])
                        }
                    }
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .controlSize(.small)
                .accessibilityLabel("Add a card this one waits on")
                .clickable()
            } else if ids.isEmpty, !emptyNote.isEmpty {
                Text(emptyNote)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
    }
}
