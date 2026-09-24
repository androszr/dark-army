import Foundation

/// The panel's held copy of the finished column.
///
/// Three fifths of every board frame used to be finished cards nobody was
/// looking at — measured 2026-09-06: 88 KB of a 173 KB `/api/state`, re-sent
/// every few seconds. The daemon now withholds them from a client that asks
/// with `?done=review`, keeping only the ones that still want a person, and
/// this is the other half of that bargain: fetch the whole column once from
/// `GET /api/board?column=done` and splice it back into every applied
/// snapshot, so nothing downstream — the board's columns, the inbox, the
/// pipeline band, the Clear Done gate — learns that the wire changed shape.
///
/// The two pure statics below are the whole of the rule and are pinned by
/// `DoneArchiveTests`; the class around them is holding and timing.
@MainActor
final class DoneArchive: ObservableObject {
    /// What a screen may say about the column while it is not yet complete.
    /// Never "there is nothing here": an empty finished column under a
    /// non-zero heading count is the one thing this must not draw.
    nonisolated enum State: Equatable {
        case loading
        case ready
        case failed
    }

    /// What a fresh pair of tokens says to do about the copy being held.
    nonisolated enum Action: Equatable {
        /// Both tokens match: what is held still describes the store.
        case keep
        /// Only the view token moved — a card was edited or reviewed, but the
        /// membership is the same. Keep drawing what is held while the
        /// refetch runs, so the column does not flash.
        case refresh
        /// The membership token moved: the held copy may name cards that no
        /// longer exist. Drop it and say it is loading.
        case dropAndRefresh
    }

    /// The archive's cards, in the daemon's order. The views sort for
    /// themselves; this is only the pile.
    @Published private(set) var cards: [BoardCard] = []
    @Published private(set) var state: State = .loading
    /// The membership and view tokens the held copy was stamped with. Empty
    /// until the first fetch lands, which is also what makes the first
    /// snapshot ask for one.
    private(set) var clearStamp = ""
    private(set) var viewStamp = ""

    /// How long a moved token waits before the refetch goes out. A bulk
    /// review moves `done_view_token` once per card, and thirty-eight
    /// fetches for one gesture is what this trailing coalesce exists to
    /// prevent. Not optional.
    static let coalesce: TimeInterval = 2

    private var pending: Task<Void, Never>?
    private var inFlight = false

    /// Fold the held archive into a snapshot's card list.
    ///
    /// The snapshot's own copy always wins on an id collision: it is the one
    /// the daemon decorated against this frame's readings, and the archive's
    /// is as old as the last fetch. Everything the snapshot does not already
    /// carry is appended. Idempotent, so applying it twice to one list — the
    /// frame path and the fetch-completion path both end here — changes
    /// nothing.
    nonisolated static func splice(_ cards: [BoardCard], archive: [BoardCard]) -> [BoardCard] {
        guard !archive.isEmpty else { return cards }
        var seen = Set(cards.map(\.id))
        var out = cards
        for card in archive where !seen.contains(card.id) {
            seen.insert(card.id)
            out.append(card)
        }
        return out
    }

    /// What to do with the held copy, given the frame's two tokens.
    ///
    /// An empty pair from the daemon (an older one, or a board that is not
    /// open) is `.keep`: there is nothing to compare against, and dropping a
    /// held column because a token went missing would blank the screen for a
    /// daemon that never withheld anything in the first place.
    nonisolated static func decide(clear: String, view: String,
                       heldClear: String, heldView: String) -> Action {
        if clear.isEmpty && view.isEmpty { return .keep }
        if heldClear.isEmpty && heldView.isEmpty { return .refresh }
        if clear != heldClear { return .dropAndRefresh }
        if view != heldView { return .refresh }
        return .keep
    }

    /// Apply a decision. `fetch` is the client's own request; it is called at
    /// most once per coalescing window and never while one is already out.
    func consider(clear: String, view: String,
                  fetch: @escaping () async -> Void) {
        switch Self.decide(clear: clear, view: view,
                           heldClear: clearStamp, heldView: viewStamp) {
        case .keep:
            return
        case .dropAndRefresh:
            cards = []
            clearStamp = ""
            viewStamp = ""
            state = .loading
            schedule(fetch)
        case .refresh:
            schedule(fetch)
        }
    }

    /// The trailing coalesce, with one exception: a **cold** archive is
    /// fetched at once. The delay exists so a bulk review does not become
    /// thirty-eight fetches; making the very first fill wait two seconds
    /// would leave the column saying it is reading when it could already have
    /// read.
    private func schedule(_ fetch: @escaping () async -> Void) {
        guard !inFlight else { return }
        let cold = clearStamp.isEmpty && viewStamp.isEmpty && cards.isEmpty
        pending?.cancel()
        pending = Task { [weak self] in
            if !cold {
                try? await Task.sleep(nanoseconds:
                    UInt64(Self.coalesce * 1_000_000_000))
            }
            guard !Task.isCancelled, let self else { return }
            self.inFlight = true
            await fetch()
            self.inFlight = false
        }
    }

    /// A fetch that landed. The tokens stamped here are the ones the *report*
    /// carried, not the frame's: the daemon reads them before the cards, so a
    /// card written mid-read leaves this copy stamped older than it is and
    /// costs one extra refetch — the other order would leave it stamped fresh
    /// and stale for ever.
    func adopt(_ report: BoardReport) {
        cards = report.cards
        clearStamp = report.doneClearToken
        viewStamp = report.doneViewToken
        state = .ready
    }

    /// A fetch that did not land. What is held stays held — a failed refresh
    /// of a good copy is not a reason to blank the column — and the state only
    /// says so when there is nothing to draw.
    func failed() {
        if cards.isEmpty { state = .failed }
    }

    /// Ask again after a failure, from the button that offers it.
    func retry(fetch: @escaping () async -> Void) {
        state = cards.isEmpty ? .loading : state
        pending?.cancel()
        pending = nil
        guard !inFlight else { return }
        Task { [weak self] in
            guard let self else { return }
            self.inFlight = true
            await fetch()
            self.inFlight = false
        }
    }
}
