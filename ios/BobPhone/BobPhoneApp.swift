import CryptoKit
import LocalAuthentication
import SwiftUI
import UIKit

@main
struct BobPhoneApp: App {
    /// The APNs delegate. SwiftUI has no other seat for
    /// `didRegisterForRemoteNotificationsWithDeviceToken`.
    @UIApplicationDelegateAdaptor(PushDelegate.self) private var pushDelegate
    @StateObject private var lock = LockGate()
    // Reference lifetime without a root subscription: only chrome observes ticks.
    @State private var decryptFeedback = DecryptFeedback()
    @StateObject private var pairing = PairingStore()
    @StateObject private var client = PhoneClient()
    @StateObject private var outbox = OutboxStore()
    /// Where the person was — tab, section, sheet trail, typed text. A
    /// shared instance (`PhoneRouter.shared`'s pattern), so observed.
    @ObservedObject private var place = PhonePlaceStore.shared
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // The pull-to-refresh indicator is a `UIRefreshControl`, and it
        // reads its colour from UIKit's appearance proxy, not from SwiftUI's
        // `.tint` — the `TabView` below already carries `Theme.phosphor`
        // and the control stayed grey under it. This line is what actually
        // colours it; the per-screen `.tint(Theme.phosphor)` on each
        // refreshable container is the plan's pin, kept for the grep.
        UIRefreshControl.appearance().tintColor = UIColor(Theme.phosphor)
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if !lock.unlocked {
                    LockedView(busy: lock.busy, error: lock.error) {
                        Task { await lock.unlock() }
                    }
                } else if pairing.record == nil {
                    PairingView(store: pairing)
                } else {
                    ContentView(client: client, pairing: pairing, outbox: outbox)
                }
            }
            // Dark-only, twice over. This covers SwiftUI; `UIUserInterfaceStyle`
            // in Info.plist covers UIKit-hosted chrome (alerts, the scanner).
            // A view designed against these tokens does not survive being
            // handed a light palette — the panel's `.darkAqua` lesson.
            .environment(\.decryptFeedback, decryptFeedback)
            .preferredColorScheme(.dark)
            .background(Theme.bg)
            // A widget tap or any `bobphone://<tab>` link parks the tab in
            // the router's slot; `ContentView` applies it post-unlock, so
            // the Face-ID gate stays in front of everything a link can aim.
            .onOpenURL { url in PhoneRouter.shared.open(url) }
            .onChange(of: scenePhase) { _, phase in
                if phase != .active { decryptFeedback.cancel() }
                switch phase {
                case .active:
                    // Adopt a Lock Screen card the last run left up before
                    // the first poll can request a second one.
                    LiveActivityController.shared.adoptExisting()
                    // The line comes up while Face ID runs; nothing is
                    // sent on it until the unlock's `wake()`.
                    client.prewarm()
                    Task {
                        await lock.unlock()
                        if !lock.unlocked { client.abandonPrewarm() }
                    }
                    // Opening the app supersedes the badge — the screen is
                    // now the truth — and is also the re-registration
                    // nudge: iOS rotates push tokens whenever it likes, and
                    // a >24h-old send is retried here.
                    PushRegistrar.shared.clearBadge()
                    PushRegistrar.shared.ensure(record: pairing.record)
                case .inactive:
                    // Above the carve-outs on purpose: the flush is
                    // idempotent, and a consent alert that later turns into
                    // a real backgrounding must not be the one path where
                    // the half-typed card was never written.
                    outbox.flushDraftNow()
                    // And where the person was, for the same reason.
                    place.flushNow()
                    // The app-switcher snapshot is taken at `.inactive`, so
                    // leaving the fleet on screen there is a Face ID bypass.
                    // Skip only while the app's own unlock sheet is up
                    // (`busy`) or the pairing camera is showing — neither is
                    // fleet/board data. A system consent alert (microphone,
                    // speech — and the first notification-permission ask,
                    // which PushRegistrar flags) also makes the scene
                    // inactive; locking there would unmount the composer and
                    // destroy a half-typed card, or demand a second Face ID
                    // right after Allow. The fourth is the Face ID / passcode
                    // sheet in front of a write from away
                    // (`RemoteAuth.prompting`): it covers the screen for its
                    // own lifetime, and locking under it unmounted the card
                    // or agent the press was made on and then asked a second
                    // time on `.active`. It also means the lock — and so
                    // `RemoteAuth.reset()` — does not run during the prompt,
                    // so the 300-second grace that prompt just granted
                    // survives it. Intended. The residual is the same one the
                    // consent alerts already carry: the snapshot taken at
                    // `.inactive` can hold fleet or board content behind the
                    // system sheet. It is bounded because `.background` below
                    // locks unconditionally, with no carve-outs at all.
                    // The screen locks; the poller does **not** stop. A
                    // Control Centre pull, a banner tap or the Face ID sheet
                    // itself all pass through `.inactive`, and stopping here
                    // made every glance cost a re-prompt *and* a reconnect
                    // (4 s at home, up to 30 s through the mailbox). What
                    // the snapshot bypass needs is the lock screen in front
                    // of the fleet, which the lock alone provides; the radio
                    // stays on until `.background` below.
                    let scannerUp = lock.unlocked && pairing.record == nil
                    let consenting = DictationEngine.shared.requestingConsent
                        || PushRegistrar.shared.requestingConsent
                        || RemoteAuth.shared.prompting
                    if !lock.busy && !scannerUp && !consenting {
                        lock.lock()
                    }
                case .background:
                    // Strictly before the lock below, which tears the whole
                    // hierarchy down and takes the composer's `@State` with
                    // it. This ordering is the fix; the debounce is comfort.
                    outbox.flushDraftNow()
                    place.flushNow()
                    DictationEngine.shared.stop()
                    lock.lock()
                    // The lock is unconditional, as before. The poller is
                    // paused, not stopped: `suspend()` stamps the departure
                    // and lands the counters, the loop polls nothing while
                    // departed and stops itself past `BackgroundGrace.window`
                    // (five minutes), and the unlock gate below decides
                    // whether the return keeps it or starts fresh.
                    client.suspend()
                    // After the final summary publish: restart the widget's
                    // timeline so its dim clock counts from this departure,
                    // not from the last throttled reload.
                    client.flushWidgetReload()
                    // And book the tile's next check-in without us.
                    if pairing.record != nil { BackgroundRefresh.schedule() }
                @unknown default:
                    break
                }
            }
            .onAppear { [client, pairing, outbox] in
                // The registrar borrows the client's transport (the quiet
                // LAN-then-relay route) for every send; wired once, here.
                PushRegistrar.shared.client = client
                // The one trigger the queue rides. Remember what the board
                // offers so a card can be written out of reach, then take
                // whatever is waiting — silently, pressing nothing.
                client.onLive = {
                    outbox.remember(board: client.snapshot.board)
                    // The cache stamps what the board just listed and drops
                    // what it no longer carries; the delta sweep then asks
                    // only for the cards whose change number moved. An
                    // unchanged board sends nothing at all.
                    client.cardCache.remember(board: client.snapshot.board)
                    Task { await outbox.sync(using: client) }
                    // The cache's own sweep and the receipt flush ride the
                    // same trigger in their own task, so neither can hold
                    // the outbox's send behind it.
                    Task {
                        await client.cardCache.sync(using: client)
                        // Presses still in `sent` when the app came back,
                        // replayed once each under their original token.
                        await client.flushReceipts()
                    }
                }
                // A poll answered on one of the spare addresses is kept, so
                // the phone heals across a Wi-Fi or VPN change without being
                // paired again. `promote` publishes `record`, which the
                // `onChange` below restarts the client on; its own
                // same-address guard is what stops that becoming a loop.
                client.onPromote = { host in pairing.promote(host: host) }
                // And where home moved to: the Mac's live addresses ride the
                // snapshot, which the relay carries too — so a phone that can
                // only hear the relay learns its way back onto the LAN. Additive,
                // and silent when it has nothing new, so this is no loop either.
                client.onHosts = { hosts in pairing.learnHosts(hosts) }
                // The sealed-frame counters land in the Keychain through
                // this pair: the client throttles (once a minute, plus a
                // flush on stop), the store writes without publishing, and
                // the channel is rebuilt from the Keychain's own numbers so
                // a relaunch never rewinds a counter it already spent. The
                // token rides along so the store can refuse counters from a
                // pairing that is no longer the record's own.
                client.onCounters = { token, send, recv in
                    pairing.persistCounters(token: token, send: send, recv: recv)
                }
                client.storedCounters = { pairing.storedCounters() }
                // The home channel's own pair, same discipline.
                client.onHomeCounters = { token, send, recv in
                    pairing.persistHomeCounters(token: token, send: send,
                                                recv: recv)
                }
                client.storedHomeCounters = { pairing.storedHomeCounters() }
            }
            .onChange(of: lock.unlocked) { _, unlocked in
                decryptFeedback.cancel()
                if unlocked {
                    // Keychain read happens only after the gate succeeds, and
                    // so is the outbox's own read: it is a protected file
                    // holding the person's own words.
                    pairing.load()
                    outbox.load()
                    // The two new stores are protected files too — the
                    // person's own words, and a record of their presses —
                    // so they are read behind the same gate, beside the
                    // outbox and never before it.
                    client.cardCache.load()
                    client.conversationCache.load()
                    client.receipts.load()
                    client.heldPicture.load()
                    client.notificationLog.load()
                    place.load()
                    // The draft is on disk again; if it was on screen when
                    // the app went away, ask for it back.
                    outbox.armResumeIfNeeded()
                    if let record = pairing.record {
                        PhoneRouter.shared.pair(token: record.token)
                        // The saved place is stamped like the router's slot;
                        // adopted before it is armed, so another Mac's
                        // place is never applied, even once.
                        place.adopt(identity: pairingIdentity(token: record.token))
                        place.arm()
                        // After `pair`, so the token the buzzes are filed
                        // under is the current pairing's; after `load`, so
                        // what was banked before the gate joins the file
                        // rather than replacing it.
                        client.absorbBankedNotifications(token: record.token)
                        // Back from `.inactive` (no departure stamped) or
                        // from a short `.background`: keep the running
                        // poller — `start` would cancel the check-in in
                        // flight, drop the digest and begin again at attempt
                        // zero. Back later, or with nothing running: a fresh
                        // session, as before.
                        switch BackgroundGrace.verdict(departedAt: client.departedAt,
                                                       now: Date(), alive: client.isPolling) {
                        case .keep: client.wake()
                        case .restart: client.start(record: record)
                        }
                        // The unlock is also a registration nudge, kept for
                        // the cold launch: there `.active` fired before the
                        // face check with no record on the client, so its
                        // `ensure` had no transport — this one does. After a
                        // short background the record survives and the
                        // `.active` `ensure` may already have a transport,
                        // which is harmless: `ensure` is throttled by its
                        // own >24h stamp, so neither is a send per unlock.
                        PushRegistrar.shared.ensure(record: record)
                    } else {
                        // Provably unpaired, behind the gate: whatever the
                        // delegate or a tray read banked since the un-pair
                        // (an old Mac keeps pushing until its own un-pair;
                        // the 403 paths unregister nothing) is dropped
                        // here, or the next pairing's first drain would
                        // file it under the new token. Not a guard inside
                        // `bank`: the cold launch's `.active` runs before
                        // `pairing.load()`, so the registrar has no record
                        // yet when the tray is read, and a refusal there
                        // would lose the tray itself.
                        PushRegistrar.shared.forgetBanked()
                        place.forget()
                    }
                }
                // Locking stops nothing here: `.background` pauses the
                // poller and the poller stops itself past the grace.
            }
            .onChange(of: pairing.record) { old, record in
                if record == nil { decryptFeedback.cancel() }
                if let record, lock.unlocked {
                    PhoneRouter.shared.pair(token: record.token)
                    // A pairing made or changed while unlocked: places are
                    // stamped with it from now on, another Mac's dropped.
                    place.adopt(identity: pairingIdentity(token: record.token))
                    // The unlock gate's `load()` re-publishes the Keychain's
                    // copy, whose counters `suspend()` moved at
                    // `.background`; a record that differs from the last
                    // one in its counters alone is the same pairing, and a
                    // `start` on it would undo the gate's `wake()`. A
                    // changed address or key still restarts, as before.
                    if !BackgroundGrace.keepsPoller(old: old?.identity,
                                                    new: record.identity,
                                                    polling: client.isPolling) {
                        client.start(record: record)
                    }
                } else {
                    client.stop()
                }
                // The moment pairing lands is the first moment a permission
                // dialog makes sense — there is now somebody to buzz.
                PushRegistrar.shared.ensure(record: record)
            }
        }
    }
}

/// The pairing a saved place belongs to: SHA-256 hex of the token, the same
/// identity `PhoneRouter.pair(token:)` stamps its slot with. Never the token.
private func pairingIdentity(token: String) -> String {
    SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined()
}

extension PairingRecord {
    /// Everything but the four counters — what `.onChange(of: pairing.record)`
    /// compares through `BackgroundGrace.keepsPoller`. The one bridge from
    /// the record to the Foundation-only rule.
    var identity: BackgroundGrace.PairingIdentity {
        BackgroundGrace.PairingIdentity(
            token: token, host: host, hosts: hosts, port: port,
            deviceId: deviceId, relayKey: relayKey, relayURL: relayURL,
            homeKey: homeKey, homeKeyCrossedInClear: homeKeyCrossedInClear,
            relayWSURL: relayWSURL)
    }
}

@MainActor
final class LockGate: ObservableObject {
    @Published var unlocked = false
    @Published var busy = false
    @Published var error = ""
    /// Stepped by every `lock()`. An unlock captures it before its sheet
    /// and commits only if no lock came in between: a Face ID that resolves
    /// after the app went to the background must not unlock it, nor grant
    /// the away writes (security review, 25 Sep 2026).
    private var epoch = 0

    func lock() {
        epoch += 1
        PhoneRouter.shared.lock()
        unlocked = false
        error = ""
        // A locked phone's next opening asks again, and the writes' grant
        // must not outlive the person putting the phone down.
        RemoteAuth.shared.reset()
    }

    func unlock() async {
        if unlocked { return }
        busy = true
        error = ""
        defer { busy = false }
        let context = LAContext()
        var reasonError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &reasonError) else {
            error = reasonError?.localizedDescription ?? "No passcode is set."
            unlocked = false
            return
        }
        let started = epoch
        do {
            let answered = try await context.evaluatePolicy(
                .deviceOwnerAuthentication,
                localizedReason: "Unlock Dark Army to see your Mac.")
            // Locked meanwhile, or already behind the lock screen: the
            // answer is stale, and the next opening asks again.
            let ok = answered && epoch == started
                && UIApplication.shared.applicationState != .background
            unlocked = ok
            // The opening's face covers the away writes too: one Face ID
            // per opening, until `lock()` resets it.
            if ok { RemoteAuth.shared.grantForSession() }
            if !ok { error = "Unlock cancelled." }
        } catch {
            unlocked = false
            self.error = "Unlock cancelled."
        }
    }
}

struct LockedView: View {
    let busy: Bool
    let error: String
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            BrandMark(size: 48)
            PromptLine(path: "~", command: "locked", size: 13)
            if !error.isEmpty {
                Text(error)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.alarm)
                    .multilineTextAlignment(.center)
            }
            DecryptButton(busy ? "WAITING…" : "UNLOCK") { retry() }
                .disabled(busy)
                .buttonStyle(AlarmOutline())
                // Mid-press the words on it are "WAITING…" alone.
                .accessibilityLabel(busy ? "Waiting for Face ID"
                                         : "Unlock with Face ID")
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        .decryptSurface("locked")
    }
}

/// The four tabs, named so a banked draft can say which one it was written
/// on. Raw values are persisted in `draft.json`; a value an older build does
/// not know reads back as `.needs`.
enum PhoneTab: String, CaseIterable, Hashable {
    case needs, fleet, board, menu

    /// A tab as a draft or a deep link spells it. Comm and Usage were tabs
    /// of their own until the Menu gathered them, so a draft stamped `comm`
    /// and the widget's `bobphone://usage` still land — on the Menu — and
    /// any other menu section's name does too. Anything else is `nil`.
    init?(stored raw: String) {
        if let tab = PhoneTab(rawValue: raw) {
            self = tab
        } else if MenuSection(rawValue: raw) != nil {
            self = .menu
        } else {
            return nil
        }
    }
}

struct ContentView: View {
    @ObservedObject var client: PhoneClient
    @ObservedObject var pairing: PairingStore
    @ObservedObject var outbox: OutboxStore
    /// A notification tap or widget link's pending tab. This view only
    /// exists behind the Face-ID gate, so consuming the slot here is what
    /// keeps the gate in front of every deep link.
    @ObservedObject private var router = PhoneRouter.shared
    /// Where the person was; applied once per unlock, behind the gate.
    @ObservedObject private var place = PhonePlaceStore.shared

    /// Bound so a resumed draft can bring its own tab to the front.
    @State private var selectedTab: PhoneTab = .needs
    /// The section open on the Menu tab, `nil` for the grid itself. Held
    /// here so a deep link can open one after bringing the tab forward.
    @State private var menuOpen: MenuSection?
    @StateObject private var sheets = PhoneSheetRouter()
    /// A restored sheet trail waiting for a picture to match it against —
    /// the held one or the first live one. Resolving against an empty
    /// snapshot would call every subject gone.
    @State private var pendingTrail: [PhonePlace.Entry]?
    /// The tab and section the saved place put on screen, while its trail
    /// still waits: the person moving off them first drops the trail.
    @State private var placeLanding: PlaceLanding?

    private struct PlaceLanding: Equatable {
        var tab: PhoneTab
        var section: MenuSection?
    }

    var body: some View {
        TabView(selection: $selectedTab) {
            // First, and badged: the whole point of the app is the walk to
            // the desk it saves, and this is the tab that saves it.
            NavigationStack {
                PhoneTabRoot(path: "~/needs", tab: .needs, client: client, pairing: pairing,
                             outbox: outbox, onForget: forget) {
                    NeedsYouView(client: client)
                }
            }
            .tabItem { Label("Needs you", systemImage: "exclamationmark.bubble") }
            .badge(client.snapshot.needsYouCount)
            .tag(PhoneTab.needs)
            .environment(\.decryptActive, selectedTab == .needs && sheets.top == nil)

            NavigationStack {
                PhoneTabRoot(path: "~/fleet", tab: .fleet, client: client, pairing: pairing,
                             outbox: outbox, onForget: forget) {
                    FleetView(client: client)
                }
            }
            .tabItem { Label("Fleet", systemImage: "list.bullet") }
            .tag(PhoneTab.fleet)
            .environment(\.decryptActive, selectedTab == .fleet && sheets.top == nil)

            NavigationStack {
                PhoneTabRoot(path: "~/board", tab: .board, client: client, pairing: pairing,
                             outbox: outbox, onForget: forget) {
                    BoardView(client: client, outbox: outbox)
                }
            }
            .tabItem { Label("Board", systemImage: "square.grid.2x2") }
            .tag(PhoneTab.board)
            .environment(\.decryptActive, selectedTab == .board && sheets.top == nil)

            // Usage, Comm (Mission Control, `CommView.swift`), Scouting and
            // Manual checks, as a grid; a tile pushes its section onto this
            // tab's own stack and the back button returns to the grid.
            NavigationStack {
                PhoneTabRoot(path: "~/menu", tab: .menu, client: client, pairing: pairing,
                             outbox: outbox, onForget: forget) {
                    MenuView(client: client, open: $menuOpen,
                             selected: selectedTab == .menu)
                }
            }
            .tabItem { Label("Menu", systemImage: "square.grid.3x3") }
            .tag(PhoneTab.menu)
            .environment(\.decryptActive, selectedTab == .menu && sheets.top == nil)
        }
        .tint(Theme.phosphor)
        .toolbarBackground(Theme.bar, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        // One arrival, in order: the saved place first (it steps aside when
        // anything below is waiting), then the draft's own tab — whose root
        // answers the same signal by pushing the composer — then the tab a
        // notification tap or widget link asked for. All post-unlock, never
        // by the tap itself.
        .onAppear {
            place.compose = { currentPlace() }
            applyPlace()
            applyResumeTab()
            applyPendingTab()
        }
        .onDisappear { place.compose = nil }
        .onChange(of: place.armSignal) { applyPlace() }
        .onChange(of: client.snapshot.generatedAt) { applyPlaceTrailIfReady() }
        .onChange(of: outbox.resumeSignal) { applyResumeTab() }
        .onChange(of: router.signal) { applyPendingTab() }
        // The place moves: kept in memory at once, on disk after a pause,
        // and flushed whole at the lock.
        .onChange(of: selectedTab) { dropWaitingTrailIfMoved(); stashPlace() }
        .onChange(of: menuOpen) { dropWaitingTrailIfMoved(); stashPlace() }
        .onChange(of: sheets.stack) { stashPlace() }
        .sheet(item: Binding(get: { sheets.presentation }, set: { if $0 == nil { sheets.close() } })) { _ in
            PhoneSheetFrame(client: client, outbox: outbox)
                .environmentObject(sheets)
        }
        .environmentObject(sheets)
    }

    /// The saved place, taken once: the tab and the Menu section at once,
    /// the sheet trail when there is a picture to match it against.
    private func applyPlace() {
        guard !PhonePlaceRules.wins(pendingTab: router.pendingTab != nil,
                                    pendingReceipt: router.pendingReceipt != nil,
                                    composerResume: outbox.resumeTab != nil) else {
            place.setAside()
            return
        }
        guard let saved = place.take() else { return }
        selectedTab = PhoneTab(stored: saved.tab) ?? .needs
        menuOpen = MenuSection(rawValue: saved.menuSection)
        pendingTrail = saved.trail.isEmpty ? nil : saved.trail
        placeLanding = PlaceLanding(tab: selectedTab, section: menuOpen)
        applyPlaceTrailIfReady()
    }

    /// A trail still waiting for a picture never pops open over a tab or a
    /// section the person has moved to meanwhile; its typed text is kept.
    private func dropWaitingTrailIfMoved() {
        guard let trail = pendingTrail, let landing = placeLanding,
              landing != PlaceLanding(tab: selectedTab, section: menuOpen) else { return }
        place.keepDrafts(of: trail)
        pendingTrail = nil
        placeLanding = nil
    }

    /// Reopen the saved trail up to the first rung whose subject the picture
    /// no longer lists. A trail the person already replaced by opening a
    /// sheet keeps only its typed text.
    private func applyPlaceTrailIfReady() {
        guard let trail = pendingTrail, client.snapshot.generatedAt != 0 else { return }
        pendingTrail = nil
        placeLanding = nil
        guard sheets.stack.isEmpty else {
            place.keepDrafts(of: trail)
            return
        }
        let snapshot = client.snapshot
        let cut = PhonePlaceRules.cut(trail,
                                      agents: Set(snapshot.agents.all.map(\.sessionId)),
                                      cards: Set(snapshot.board.cards.map(\.id)))
        var rungs: [(PhoneSheet, PhonePlace.Entry)] = []
        var dropped = cut.dropped
        for (index, entry) in cut.kept.enumerated() {
            guard let sheet = PhoneSheet.fromPlace(entry, snapshot: snapshot) else {
                dropped = cut.kept[index...].map { ($0, "not resolved") } + dropped
                break
            }
            rungs.append((sheet, entry))
        }
        place.noteDropped(dropped)
        place.keepDrafts(of: dropped.map(\.0))
        if !rungs.isEmpty { sheets.restore(rungs) }
    }

    /// Where the person is now, for the store's `compose` and its stash.
    private func currentPlace() -> PhonePlace {
        PhonePlace(pairing: place.identity, tab: selectedTab.rawValue,
                   menuSection: menuOpen?.rawValue ?? "",
                   push: place.profileShowing(on: selectedTab.rawValue) ? "profile" : "",
                   trail: pendingTrail ?? sheets.placeTrail())
    }

    private func stashPlace() {
        place.stash(currentPlace())
    }

    private func applyResumeTab() {
        if let tab = outbox.resumeTab {
            // A banked draft that was on screen wins over the saved place.
            place.setAside()
            place.keepDrafts(of: pendingTrail ?? [])
            pendingTrail = nil
            selectedTab = tab
        }
    }

    private func applyPendingTab() {
        if router.pendingTab != nil || router.pendingReceipt != nil {
            // A tap or a link wins over the saved place — even one already
            // restored, as `sheets.close()` / `sheets.route` below make it.
            place.setAside()
            place.keepDrafts(of: pendingTrail ?? [])
            pendingTrail = nil
        }
        if let tab = router.take() { selectedTab = tab; sheets.close() }
        if let section = router.takeSection() { menuOpen = section }
        if let route = router.pendingReceipt { sheets.route(route) }
        // The widget's face: open that agent where the fleet still lists
        // it. A session the fleet has forgotten opens nothing — the tab
        // is already in front, which is the honest fallback.
        let session = router.takeSession()
        if !session.isEmpty,
           let (agent, category) = PhoneInbox.uniqueAgent(
               session: session, agents: client.snapshot.agents) {
            sheets.show(.agent(agent, category))
        }
    }

    /// Stop the poller **before** clearing the record, so a pull-to-refresh
    /// on a screen being torn down cannot reach the Mac.
    private func forget() {
        // Capture the record and channel **before** `stop()` tears them
        // down: the record authenticates the unregister and the channel is
        // its away rung. The send runs behind the teardown, best-effort —
        // a forget pressed away from home is never held up, and the
        // Mac-side un-pair remains the authoritative kill for the buzzes.
        let teardown = client.pushTeardown()
        // The Lock Screen card goes with the pairing, and its token is
        // cleared on the same captured record, before `stop()` below.
        LiveActivityController.shared.endAll(unregistering: true)
        // Everything pairing-scoped goes with the pairing: the snapshot, the
        // bars, the card cache and the command receipts. Without this the
        // deliberate un-pair dropped less than an involuntary 403 did, and a
        // re-pair against another Mac read the old Mac's cards.
        client.forgetPairedState()
        client.stop()
        router.forget()
        pairing.clear()
        // The write grace dies with the pairing it was granted under.
        RemoteAuth.shared.reset()
        if let teardown {
            Task { [client] in
                await PushRegistrar.shared.unregister(
                    record: teardown.record, channel: teardown.channel,
                    home: teardown.home, using: client)
            }
        }
    }
}

/// One tab's chrome: the identity bar on top, the freshness banner under it,
/// the connection line at the bottom, scanlines over the lot.
///
/// A struct instantiated once per tab rather than a method on `ContentView`,
/// because the two navigation booleans must be **per-tab**: state held on
/// `ContentView` would be one switch shared by every stack, and setting it
/// would push the destination in every tab at once. A consequence, accepted:
/// `TabView` keeps inactive tabs' hierarchies alive, so two tabs can each
/// hold a mounted composer. The banked draft, by contrast, is **one slot for
/// the whole phone**: it records whichever composer stashed last, so
/// resuming into a second tab while the first is still mounted means two
/// views sharing one staging id. Accepted residual, last-writer-wins — and
/// strictly better than the old behaviour, where all four died at the first
/// lock.
struct PhoneTabRoot<Content: View>: View {
    let path: String
    /// Which tab this root is, so a banked draft can be reopened where it
    /// was written and so the composer can stamp its own tab on every stash.
    let tab: PhoneTab
    @ObservedObject var client: PhoneClient
    @ObservedObject var pairing: PairingStore
    @ObservedObject var outbox: OutboxStore
    let onForget: () -> Void
    @ViewBuilder let content: () -> Content

    /// Three missed polls. Below that a single slow response would cry wolf.
    private static var staleAfter: TimeInterval { 12 }

    @State private var composing = false
    @State private var showingProfile = false
    /// The saved place's profile push is reapplied through here.
    @ObservedObject private var place = PhonePlaceStore.shared

    var body: some View {
        Group {
            if connecting {
                ConnectingView(
                    link: client.link,
                    sentence: client.lastError.isEmpty
                        ? "Could not reach the Mac." : client.lastError,
                    phase: client.phase)
            } else {
                content()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        // The identity bar replaces the system one on every tab root. The
        // pushed detail keeps its own, for the back button.
        .toolbar(.hidden, for: .navigationBar)
        // Programmatic navigation, registered on the tab's root content —
        // never inside a lazy container, which is the documented failure
        // mode of this API. The bar's buttons only flip the booleans.
        .navigationDestination(isPresented: $composing) {
            ComposerView(client: client, outbox: outbox, tab: tab,
                         pairing: pairing, onForget: onForget)
        }
        .navigationDestination(isPresented: $showingProfile) {
            ProfileView(client: client, pairing: pairing,
                        receipts: client.receipts, onForget: onForget)
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            VStack(spacing: 0) {
                PhoneBrandBar(
                    path: path,
                    connected: client.status == .live,
                    needsYouCount: client.snapshot.needsYouCount,
                    onCompose: { composing = true },
                    onProfile: { showingProfile = true }
                )
                if let heard = staleSince {
                    StaleBanner(lastHeard: heard)
                }
                // A picture drawn while the Mac is out of reach says how old
                // it is; `StaleBanner` and `ReconnectBar` are untouched and
                // this line sits beside them, never in their place.
                if let asOf = heldSince {
                    HeldPictureBanner(asOf: asOf,
                                      reaching: client.status != .unreachable)
                }
            }
        }
        .safeAreaInset(edge: .bottom) { statusBar }
        .decryptSurface("tab")
        // Both hooks are load-bearing: `TabView` mounts a never-visited tab
        // lazily, so a root selected *by* the resume does not exist to hear
        // the signal and its first chance to ask is `.onAppear`.
        .onAppear { applyResume() }
        .onChange(of: outbox.resumeSignal) { applyResume() }
        .onChange(of: place.pushSignal) { applyResume() }
        .onChange(of: showingProfile) { _, showing in
            place.noteProfile(showing, on: tab.rawValue)
            if let now = place.compose?() { place.stash(now) }
        }
    }

    private func applyResume() {
        if outbox.takeResume(for: tab) { composing = true }
        if place.takePush(for: tab.rawValue) == "profile" { showingProfile = true }
    }

    /// A first connect, not a lost one: the status is still settling *and*
    /// no snapshot has ever landed.
    private var connecting: Bool {
        (client.status == .connecting || client.status == .idle)
            && client.snapshot.generatedAt == 0
    }

    /// The Mac has gone quiet. Measured on this phone's own clock — clock skew
    /// between two machines must never fake staleness.
    private var staleSince: Date? {
        guard client.status == .live, let heard = client.lastHeard else { return nil }
        return Date().timeIntervalSince(heard) > Self.staleAfter ? heard : nil
    }

    /// The picture on screen is a held one: the Mac is not answering and
    /// something has been decoded — a restored picture on a cold launch, or
    /// the last live one after the link dropped.
    private var heldSince: Date? {
        guard client.status != .live, client.status != .unpaired,
              client.snapshot.generatedAt != 0 else { return nil }
        return client.pictureAsOf
    }

    @ViewBuilder
    private var statusBar: some View {
        if client.status == .unpaired {
            // The client's own sentence where it has one — `pairAgain` for a
            // record with no home key — else today's literal.
            Text(client.lastError.isEmpty ? "no longer paired" : client.lastError)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.alarm)
                .padding(8)
                .frame(maxWidth: .infinity)
                .background(Color.red.opacity(0.12))
        } else if client.status == .unreachable {
            // The sentence is the Mac's or the relay's own, verbatim; the
            // bar adds the standing of the retry beside it and invents
            // nothing — every figure is stamped by the poll loop.
            ReconnectBar(
                sentence: client.lastError.isEmpty
                    ? "Could not reach the Mac." : client.lastError,
                link: client.link,
                phase: client.phase)
        }
    }
}
