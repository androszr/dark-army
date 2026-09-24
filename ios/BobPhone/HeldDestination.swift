import Foundation
import SwiftUI

/// What a tapped buzz opens when the Mac is out of reach: the card or the
/// agent it names, found in the picture the phone already holds — or, when
/// neither is in that picture any more, the buzz's own words.
///
/// `resolve` is pure and synchronous — Foundation only, no client, no
/// fetch, no `await` — so composing the held view costs nothing and asks
/// the Mac for nothing (`HeldDestinationTests` counts the requests at
/// zero). It re-derives nothing: the card is `snapshot.board.cards`' own
/// row, the agent is `PhoneInbox.uniqueAgent`'s answer across the five
/// buckets, category included.
enum HeldDestination: Equatable {
    case card(BoardCard)
    case agent(Agent, Category)
    case words(NotificationLogEntry)

    static func == (lhs: HeldDestination, rhs: HeldDestination) -> Bool {
        switch (lhs, rhs) {
        case (.card(let a), .card(let b)): return a.id == b.id
        case (.agent(let a, let ca), .agent(let b, let cb)): return a.sessionId == b.sessionId && ca == cb
        case (.words(let a), .words(let b)): return a == b
        default: return false
        }
    }

    /// The card the entry names, else the agent, else the words; nil with
    /// no entry at all (a buzz this phone never saw — the live path alone).
    static func resolve(entry: NotificationLogEntry?, snapshot: Snapshot) -> HeldDestination? {
        guard let entry else { return nil }
        if !entry.cardId.isEmpty,
           let card = snapshot.board.cards.first(where: { $0.id == entry.cardId }) {
            return .card(card)
        }
        if let (agent, category) = PhoneInbox.uniqueAgent(session: entry.sessionId, agents: snapshot.agents) {
            return .agent(agent, category)
        }
        return .words(entry)
    }
}

/// The held view itself: the same card and agent screens the live page
/// draws, composed off the held snapshot. Marks nothing read, consumes no
/// receipt, posts nothing of its own — every control on those screens
/// still goes through the queue, which is the person's press, not this
/// view's.
struct HeldDestinationView: View {
    @Environment(\.phoneSheetEntry) private var sheetEntry
    let held: HeldDestination
    @ObservedObject var client: PhoneClient

    var body: some View {
        switch held {
        case .card(let card):
            PhoneCardDetailView(seed: card, client: client,
                                retained: sheetEntry?.cardDraft(for: card.id))
        case .agent(let agent, let category):
            AgentDetailView(seed: agent, category: category, client: client)
        case .words(let entry):
            HeldNotificationWords(entry: entry)
        }
    }
}

/// The fallback: the buzz's own two lines, when it arrived and, only where
/// a fetched page once said it, its kind. Every line wraps; nothing here is
/// a control, so nothing here can be mistaken for the live page's.
struct HeldNotificationWords: View {
    let entry: NotificationLogEntry

    var body: some View {
        List {
            Text(entry.title.isEmpty ? "A notification" : entry.title)
                .fixedSize(horizontal: false, vertical: true)
            if !entry.subtitle.isEmpty {
                Text(entry.subtitle)
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("arrived \(Date(timeIntervalSince1970: entry.seenAt).formatted())")
                .font(Theme.mono(10)).foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            if !entry.kind.isEmpty {
                Text(entry.kind)
                    .font(Theme.mono(10)).foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("The card or agent this was about is no longer in the picture this phone holds. The live page opens when the Mac answers.")
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(Theme.mono(12))
        .scrollContentBackground(.hidden).background(Theme.bg)
        .accessibilityElement(children: .combine)
        .decryptSurface("held notification")
    }
}
