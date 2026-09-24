import Foundation

/// What a tapped notification shows, decided in two steps that never wait
/// on each other. `open()` is synchronous: it reads the buzz's entry out
/// of the log this phone keeps and composes the held destination from the
/// picture already on the client — the card, the agent, or the buzz's own
/// words — touching neither `refresh` nor `fetch`. `resolve()` is today's
/// live path verbatim over injected closures: one refresh, then the receipt
/// page only when the Mac answered live; otherwise `offline`. The view
/// draws the page when it lands and the held destination until then, so a
/// person with the radios off sees the card at once and the live page
/// takes over without another tap.
///
/// Everything the model needs is handed in as closures, so a test can
/// count the requests (`HeldDestinationTests`: zero at `open()`, one
/// refresh and no fetch at `resolve()` while the stub says not live).
@MainActor
final class NotificationDestinationModel: ObservableObject {
    let route: PendingReceipt
    private let log: NotificationLogStore
    /// The picture on the client — `client.snapshot`, live or held; read at `open()`.
    private let picture: () -> Snapshot
    private let isLive: () -> Bool
    private let accepts: () -> Bool
    private let refresh: () async -> Void
    private let fetch: (String) async -> CatchUpPage?

    /// The live page, once the Mac answered. Settable by the view for the
    /// one case a page is already held on the sheet entry (`page = held`).
    @Published var page: CatchUpPage?
    @Published private(set) var loading = false
    @Published private(set) var offline = false
    @Published private(set) var held: HeldDestination?
    @Published private(set) var entry: NotificationLogEntry?

    init(route: PendingReceipt, log: NotificationLogStore,
         snapshot: @escaping () -> Snapshot,
         isLive: @escaping () -> Bool,
         accepts: @escaping () -> Bool,
         refresh: @escaping () async -> Void,
         fetch: @escaping (String) async -> CatchUpPage?) {
        self.route = route
        self.log = log
        self.picture = snapshot
        self.isLive = isLive
        self.accepts = accepts
        self.refresh = refresh
        self.fetch = fetch
    }

    /// The held-first step: the entry for this receipt, and what the held
    /// picture makes of it. Synchronous, and asks the Mac for nothing.
    func open() {
        entry = log.entry(for: route.receiptId)
        held = HeldDestination.resolve(entry: entry, snapshot: picture())
    }

    /// The live step — `NotificationDestinationView.resolve()`'s body as it
    /// stood, over the closures. Returns the page so the view can retain it
    /// on the sheet entry after its own receipt checks; nil means no page.
    func resolve() async -> CatchUpPage? {
        guard !loading, accepts() else { return nil }
        loading = true
        defer { loading = false }
        await refresh()
        guard !Task.isCancelled, accepts(), isLive() else { offline = true; return nil }
        let result = await fetch("receipt_id=\(route.receiptId)")
        guard !Task.isCancelled, accepts() else { return nil }
        guard let result else { offline = true; return nil }
        page = result
        offline = false
        return result
    }
}
