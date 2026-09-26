import Combine
import Foundation
import WidgetKit

/// Polls the Mac every 4s while foregrounded: one sealed `state` frame to
/// `/api/home` under the pairing's home key (`HomeChannel`), then a sealed
/// `usage` frame on the same host. A missed usage fetch keeps the last bars
/// and does not mark the Mac unreachable. No device token travels — the seal
/// is the proof, and a copied header buys nothing.
/// No SSE: iOS suspends sockets when the app backgrounds, so a held stream
/// dies invisibly and a stale screen looks live.
/// The away standing, published once for every surface that draws it.
///
/// A singleton mirror of `PhoneClient.via` / `leaseExpiresAt` rather than
/// two more parameters threaded through every tab scaffold: exactly one
/// object produces this state, the brand bar on every tab consumes it, and
/// the scaffolds in between have nothing to say about it.
@MainActor
final class AwayState: ObservableObject {
    static let shared = AwayState()

    @Published var via: PhoneClient.Via = .lan
    @Published var leaseExpiresAt: Double = 0
    /// The socket lane's standing, one word (`connecting` / `open`) or
    /// empty when there is no line — the profile's row. A word, never an
    /// address and never a health record.
    @Published var socket: String = ""

    /// Whether an acting control should be offered. At home, always; away,
    /// only inside the lease window. The Mac still re-checks per write.
    var canAct: Bool {
        via == .lan || leaseExpiresAt > Date().timeIntervalSince1970
    }
}

/// The reconnection standing: what the poll loop is doing between polls.
/// Every field is stamped by `PhoneClient.noteAttempt` and by nothing
/// else — a view that counted its own attempts or ran its own clock would
/// drift from the loop the moment iOS suspended the app.
struct LinkAttempt: Equatable {
    /// Consecutive polls that did not land. Zero means the last one did.
    var failures: Int = 0
    /// When the loop's own sleep ends — `Date()` plus the pause it took.
    var nextAttemptAt: Date?
    /// That pause, in whole seconds. The progress element's denominator.
    var waitSeconds: Int = 0
    /// The way the next attempt will go, from the same `via` read that
    /// chose the pause. Never re-derived in a view.
    var route: PhoneClient.Via = .lan
}

@MainActor
final class PhoneClient: ObservableObject {
    @Published var snapshot = Snapshot()
    @Published var usage: [UsageBar] = []
    /// The Mac's diary (`GET /api/log`, the sealed `log` kind), newest first,
    /// merged by id and pruned locally to a day / 500. Fetched behind a state
    /// 200 every `logInterval`, and on demand by the Fleet tab.
    @Published private(set) var log: [LogEntry] = []
    private var lastLogFetch = Date.distantPast
    /// True for the whole of `backgroundRefresh`: the diary is never fetched
    /// with nobody watching, and the widget reads nothing new.
    private var backgroundRun = false
    @Published var attribution = UsageAttribution()
    @Published var status: Status = .idle
    @Published var lastError = ""
    /// How the reconnection is going, for the screen to read. Written only
    /// by `noteAttempt` and cleared by `stop()`.
    @Published private(set) var link = LinkAttempt()
    /// What the running check-in is doing right now, in words, or "" between
    /// check-ins. Stamped by `notePhase` at each rung of `pollOnce` /
    /// `pollDirect` / `pollViaRelay` and cleared when `poll` finishes; the
    /// screen draws it and composes nothing.
    @Published private(set) var phase = ""

    /// The check-in's rungs, in the words the screen shows. Written here,
    /// stamped by `notePhase` alone, held by no view.
    enum Phase {
        static let homeOnFile = "Trying the Mac at its usual home address…"
        static func homeOther(_ n: Int, of total: Int) -> String {
            "Trying another home address (\(n) of \(total))…"
        }
        static let relayAsking = "Asking the relay to reach the Mac…"
        static let relayWaiting =
            "The relay has the request — waiting for the Mac to pick it up "
            + "(up to a minute)…"
        static let homeProbe = "Checking whether the Mac is on home Wi-Fi…"
    }
    /// When this phone last got a 200 — **its own clock**, never the
    /// snapshot's `generated_at`. Two machines' clocks drift, and clock skew
    /// must not fake staleness on a Mac that is answering perfectly.
    @Published var lastHeard: Date?
    /// The multi-question dialog picks, shared by every `AnswerBox` on
    /// screen. Owned here so the "Needs you" row and the agent detail read
    /// one copy; pruned beside `pruneSettling`, at poll cadence.
    let answerDrafts = AnswerDrafts()

    enum Status: Equatable {
        case idle
        case connecting
        case live
        case unpaired
        case unreachable
    }

    /// How the last successful poll travelled. The screen must never dress
    /// a relay answer up as home Wi-Fi — the AWAY badge reads this.
    enum Via: Equatable {
        case lan
        case relay
    }

    /// Persisted so a launch — which has no poll history — can go the way
    /// the last check-in went instead of knocking on silent home addresses
    /// first. Read by `backgroundRefresh` and by `seedRouteFromLastPoll`.
    static let lastKnownAwayKey = "lastKnownAway"

    @Published var via: Via = .lan {
        didSet {
            AwayState.shared.via = via
            UserDefaults.standard.set(via == .relay, forKey: Self.lastKnownAwayKey)
            // The socket lane follows the route: wanted while away and on
            // screen, closed at home. `suspend()` / `wake()` own the
            // background half, so a check-in landing while departed does
            // not reopen the line behind the lock.
            channel?.socket?.wanted = via == .relay && departedAt == nil
        }
    }
    /// This phone's own away-window expiry, straight from the snapshot's
    /// devices section. Display and local gating only — the Mac re-checks
    /// the lease per write and its refusal is the authority.
    @Published var leaseExpiresAt: Double = 0 {
        didSet {
            AwayState.shared.leaseExpiresAt = leaseExpiresAt
            // The local "away access ends soon" reminder follows the
            // published expiry; a moved expiry re-books it, none clears it.
            if leaseExpiresAt != oldValue {
                LeaseReminder.sync(expiresAt: leaseExpiresAt)
            }
        }
    }

    /// Whether an acting button should be offered at all. One rule, held by
    /// `AwayState` — this is a convenience read, never a second derivation.
    var canAct: Bool { AwayState.shared.canAct }

    /// Whether this phone knows it is not at home — the last successful
    /// poll travelled by relay, and there is a channel to travel by. It is
    /// the same field `BrandBar` draws as the AWAY badge (`via == .relay`),
    /// deliberately: the screen and the route can never disagree.
    ///
    /// A cold launch goes the way the last poll went: `seedRouteFromLastPoll`
    /// reads `lastKnownAwayKey` when `start()` builds a relay channel, so a
    /// phone that was away last time asks the relay first. A phone never
    /// away starts `.lan` and probes the home addresses cheaply first. A
    /// wrong guess either way self-corrects on the very first poll, and no
    /// write can be pressed before that poll lands (PREPARE is held by
    /// `offline`, and the Needs-you list is empty until a snapshot decodes).
    var knowsItIsAway: Bool { via == .relay && channel != nil }

    /// Told when the sealed-frame counters move, so the pairing store can
    /// persist them. Throttled here: at most once a minute, plus a flush on
    /// `stop()` (background, un-pair, lock). Assigned in `BobPhoneApp`
    /// beside `storedCounters` — unassigned, every relaunch would rewind
    /// the counters to the record's own and reopen the replay window.
    /// The token names which pairing the counters belong to, stamped when
    /// the channel was built: the store must refuse a mismatch, or a dead
    /// pairing's late counters would wind a fresh record's forward and turn
    /// every new answer into a refused replay.
    var onCounters: ((String, Int, Int) -> Void)?
    /// Reads the freshest *persisted* counters when a channel is built.
    /// The Keychain's, not the published record's: `persistCounters` writes
    /// without publishing (a counter bump is not news any view draws), so
    /// the record `start` receives lags on purpose.
    var storedCounters: (() -> (send: Int, recv: Int)?)?
    private var channel: RelayChannel?
    /// The home pair, mirroring the relay's: the sealed home channel's
    /// counters land in the Keychain through `onHomeCounters`, and the
    /// channel is rebuilt from `storedHomeCounters` so a relaunch never
    /// rewinds a counter it already spent. Same token guard.
    var onHomeCounters: ((String, Int, Int) -> Void)?
    var storedHomeCounters: (() -> (send: Int, recv: Int)?)?
    private var homeChannel: HomeChannel?
    private var pendingHomeCounters: (token: String, send: Int, recv: Int)?
    private var lastCounterPersist = Date.distantPast
    /// Staged counters, keyed to the pairing they were counted under. The
    /// token is the generation guard: a late answer from a channel that
    /// outlived its pairing stages here, and `flushCounters` drops it.
    private var pendingCounters: (token: String, send: Int, recv: Int)?

    /// Told when a poll was answered by an address other than the one on
    /// file, so the store can keep the winner. A closure rather than a call
    /// into `PairingStore`: the client knows which address answered and
    /// nothing about the Keychain.
    var onPromote: ((String) -> Void)?
    /// Addresses the Mac published in its snapshot, handed to the pairing
    /// store to learn. Fires only when at least one of them is not already
    /// on file, so a quiet poll writes no Keychain and restarts no client.
    var onHosts: (([String]) -> Void)?

    /// Told when a poll landed — the Mac is in reach right now. The one
    /// trigger the outbox's sweep rides: the phone is poll-only by design, so
    /// there is no push and no background socket to hang a queue off.
    /// `refreshNow` rides the same `poll`, so pull-to-refresh sweeps too.
    var onLive: (() -> Void)?

    /// What happened to every press — `Receipts.swift`. Its own object so
    /// a view can observe the PENDING list without observing the whole
    /// client; the client is what opens and closes the records.
    let receipts = ReceiptLedger()
    /// The ledger's changes, forwarded into this object's: every screen
    /// reads its queue mark and note through `queueMark(for:)` /
    /// `queueNote(for:)` on the client it already observes, so no view
    /// signature changed for the queue.
    private var receiptsForwarding: AnyCancellable?
    private var conversationForwarding: AnyCancellable?
    /// The phone's own complete copy of every card and plan it has seen.
    /// Held here rather than beside `outbox` in the app so `forgetPairing`
    /// can drop it in one place with everything else pairing-scoped.
    let cardCache = CardCacheStore()
    let conversationCache = ConversationCacheStore()
    /// Pictures a person opened, newest last, so reopening one draws at
    /// once without asking the Mac again. **Memory only** — a picture is
    /// never written to disk by the phone — a few entries, and none older
    /// than a day (`ImageMemo.lifetime`), pruned on every applied state.
    var imageMemo = ImageMemo()
    /// The last full picture, on disk, for a cold launch with the Mac out
    /// of reach. Filled at the end of `applyState`'s applied branch and
    /// restored once by `restoreHeldPicture()`; pairing-scoped.
    let heldPicture = HeldPictureStore()
    /// The buzzes this phone has seen (`NotificationLog.swift`): title,
    /// second line, when, and the opaque subject ids the push carried, so a
    /// tap with the Mac out of reach opens the card or agent from the held
    /// picture. Loaded behind the gate, fed by `absorbBankedNotifications`,
    /// enriched by a fetched receipt page; pairing-scoped like the picture.
    let notificationLog = NotificationLogStore()
    /// The phone's own side of every trip's timing (`LinkTiming.swift`),
    /// fed by both channels' `onTiming` and drawn on the access-log
    /// screen. Pairing-scoped: dropped on un-pair, kept across `stop()` —
    /// a day's samples are the point.
    let linkTiming = LinkTimingStore()
    /// When the picture on screen was decoded — a live poll's `Date()`, or
    /// the held picture's own stamp. What the "as of" line counts from.
    @Published private(set) var pictureAsOf: Date?

    private var task: Task<Void, Never>?
    /// The moment the app went to the background, or nil while it is
    /// in front (an `.inactive` glance stamps nothing). Written by
    /// `suspend()`, cleared by `wake()` and `stop()`; read by the loop
    /// and by the unlock gate through `BackgroundGrace`. Memory only: a
    /// process iOS killed starts fresh.
    private(set) var departedAt: Date?
    private let catchUpRequests = CatchUpRequests()
    private var historyGeneration = 0
    private var record: PairingRecord?
    /// The write currently out, per scope — key: the scope `post` resolved
    /// (the session being written to, else the card, else `verb:<action>`),
    /// value: the action name. Published so a view can dim every acting
    /// control for that scope and swap the pressed one's label to SENDING…
    /// while the request is out; a private set here was the reported bug — a
    /// swallowed tap with nothing on screen to say so.
    @Published private(set) var writesInFlight: [String: String] = [:]
    /// Sessions whose confirmed Stop / Close / Delete the Mac accepted and
    /// that are expected to leave the list — the panel's `stopping` rule at
    /// poll cadence. Cleared by the next decoded snapshot no longer listing
    /// the session (`pruneSettling`), never by the HTTP 200, with
    /// `settlingTimeout` as the backstop.
    @Published private(set) var settling: Set<String> = []
    /// Sessions whose answer or reply the Mac accepted, each with the
    /// `question.id` the snapshot showed for that row at send time (`""`
    /// for a reply on a row with no question). An answered question stays
    /// listed until the transcript carries its `tool_result` — the daemon's
    /// `answer_question` deliberately touches nothing on success — so the
    /// HTTP 200 proves nothing about the row, and the refresh inside `post`
    /// polls a snapshot that still lists the question. The hold ends on
    /// the snapshot (`pruneSettlingAnswers`: the row out of `waiting`, or
    /// on a different question), `settlingTimeout` as the backstop.
    @Published private(set) var settlingAnswers: [String: String] = [:]
    /// Cards whose confirmed Delete the Mac accepted and that are expected
    /// to leave the board — `settling`'s shape, keyed by card id rather
    /// than session id, because `board_delete` carries no session. Cleared
    /// by the next decoded board no longer listing the card
    /// (`pruneSettlingCards`), never by the HTTP 200.
    @Published private(set) var settlingCards: Set<String> = []
    /// One phone-authored sentence per card, drawn on the board list and on
    /// the card's own screen. Written only by the settling backstop below,
    /// so the two surfaces cannot disagree.
    @Published private(set) var boardNotices: [String: String] = [:]
    /// Pipeline preferences the Mac accepted and that its board has not yet
    /// reported back. Keyed `"board_autostart"` and `"parallel:<root>"`,
    /// holding the value asked for, so the switch and the picker keep
    /// showing the press while the Mac stores it.
    ///
    /// Never cleared by the HTTP 200, and that is the whole point: the
    /// daemon hands the request to the menu-bar app and answers at once, so
    /// `refreshAfterWrite()`'s snapshot routinely still carries the old
    /// value. `settlingCards`' shape exactly, cleared by a decoded board
    /// reporting the asked-for value.
    @Published private(set) var settlingPreferences: [String: String] = [:]
    /// One phone-authored sentence per preference key, on the same keys.
    /// Written only by the settling backstop below.
    @Published private(set) var pipelineNotices: [String: String] = [:]
    /// What the lapsed backstop says. A card hidden for ever is worse than
    /// one that comes back and admits it does not know.
    static let deleteUnsettledNotice =
        "The Mac accepted the delete, but its board still lists this card."
    /// What a card being deleted says while it waits. The word is required,
    /// not decorative: greying alone would be colour-alone, which this
    /// codebase refuses.
    static let cardLeavingLine = "Deleting…"
    /// The phone's own words for a write that landed. Three sentences,
    /// spelled exactly once each, drawn in the quiet tone: the Mac's own
    /// refusals are shown verbatim in the warning tone and never mixed into
    /// this slot, which is what keeps a phone-authored line from ever
    /// reading as something the Mac said.
    static let cardMarkedDoneLine = "Marked done."
    static let cardSentBackLine = "Sent back."
    static let catchUpMarkedLine = "Caught up to here."
    /// The preference half of the sentence above, for its reason: a switch
    /// frozen on a press the Mac never applied is worse than one that comes
    /// back and admits it does not know.
    static let preferenceUnsettledNotice =
        "The Mac accepted the change, but its board still reports the old setting."
    /// The two settling-key shapes, named once so the screen and the prune
    /// cannot spell them differently.
    static let autostartSettlingKey = "board_autostart"
    static func parallelSettlingKey(_ root: String) -> String {
        "parallel:\(root)"
    }
    /// `Triage.stoppingTimeout`'s figure, for its reason: a row frozen
    /// forever is worse than one that admits it does not know.
    static let settlingTimeout: TimeInterval = 12
    /// The connect probe for a home address that is *not* the one on file.
    /// Named once and used everywhere the phone walks the spare addresses:
    /// a host that is silent rather than refusing costs exactly this, and
    /// with three to five addresses on file the walk is what an away action
    /// used to spend its whole patience on before the relay was even tried.
    /// The first candidate keeps its own, longer, patience — that one is
    /// load-bearing at home, where `timeoutInterval` is an inactivity
    /// timeout and a slow helper answers on a quiet socket.
    static let directProbe: TimeInterval = 4
    /// The home address's patience on the check-in a person is waiting on —
    /// a launch or a return (`wakeCheckIn`). At home the Mac answers in
    /// milliseconds; away, the address is silent and the old eight seconds
    /// were the wake. The socket is coming up alongside, so a miss here
    /// costs about this much before the relay answers, and the loop's next
    /// poll probes home again with the usual patience.
    static let wakeProbe: TimeInterval = 1
    /// The away deadline for PREPARE, and it is arithmetic rather than a
    /// guess: `card_prepare.TIMEOUT_WITH_ATTACHMENTS_SECONDS` (120) +
    /// `relay_client.IDLE_GAP_MAX` (30, the connector's idle pickup) +
    /// two mailbox ticks (2 × `box.js` `POLL_MS` 2000) + margin. Under it,
    /// a prepare the Mac actually completed comes back after the phone has
    /// already called it silent, and the late answer is popped and thrown
    /// away by the next poll.
    static let prepareRelayTimeout: TimeInterval = 165
    /// After the deadline above passes with no answer, PREPARE does not
    /// give up: it asks the Mac again under the **same** `command_token`,
    /// and the Mac's receipt ledger hands back the draft it already wrote
    /// (or a 202 while it is still writing, which is asked again). Each
    /// replay waits this long — one relay round trip, not a whole helper —
    /// and there are `prepareReplayAttempts` of them, `prepareReplayGap`
    /// apart. Why this exists: a reply posted to the to-phone mailbox
    /// while the phone was asleep, or popped by a poll that was not
    /// waiting for it, used to be gone for good; WRITING… then sat until
    /// the deadline and the person paid for a second draft with a second
    /// press. The Mac's ledger keeps an answer for fifteen minutes, so the
    /// whole recovery window (165 + 4 × 35 s) sits well inside it.
    static let prepareReplayTimeout: TimeInterval = 30
    static let prepareReplayAttempts = 4
    static let prepareReplayGap: TimeInterval = 5
    /// How often an away phone re-walks *every* home address, in case the
    /// Mac's address changed while it was out. Between those walks it
    /// probes only the address on file, so coming home costs one short
    /// probe per poll and turns the AWAY badge off within a minute.
    static let homeRediscoveryInterval: TimeInterval = 60
    /// When the last full home walk ran. Main-actor only, like everything
    /// else on this client.
    private var lastFullDirectWalk = Date.distantPast
    /// The Mac's fingerprint of the picture this phone is holding, quoted
    /// back so a quiet Mac can answer "nothing changed" in one line.
    /// Memory-only and deliberately so: a cold launch holds no snapshot, so
    /// it must hold no digest — nothing here goes to `UserDefaults` or the
    /// Keychain.
    private var heldStateDigest = ""
    /// The Mac's fingerprint of each section of the picture this phone is
    /// holding, quoted back as `sections` so a busy Mac can leave out the
    /// sections that did not move and name them in `sections_unchanged`.
    /// Memory-only for `heldStateDigest`'s reason — nothing here goes to
    /// `UserDefaults` or the Keychain. Empty against an older Mac.
    private var heldSectionDigests: [String: String] = [:]
    /// The last decoded picture exactly as it came off the wire — before the
    /// held Done archive is spliced into its board. A kept section is carried
    /// from here, never from `snapshot`: `spliceDone` only adds, so carrying
    /// the on-screen board would keep a Done card the Mac has since cleared
    /// until the next whole picture. Memory-only, cleared beside
    /// `heldSectionDigests`.
    private var heldWire = Snapshot()
    /// When the whole picture last landed. See `fullStateFloor`.
    private var lastFullState: Date?
    /// The resync floor: however well the two sides agree, ask for the whole
    /// picture from scratch this often anyway, so the worst case of a
    /// disagreement about what counts as a change is a screen a minute
    /// behind rather than a screen stuck for good. This is a payload rule,
    /// not a timing one — the 4s/8s check-in rhythm is untouched.
    static let fullStateFloor: TimeInterval = 60
    /// The check-in currently running, if any. See `poll`.
    private var polling: Task<Void, Never>?
    /// A pull-to-refresh is out. Raised by the pull's own refresh alone —
    /// the one a *person* pulled for — and never by `refreshAfterWrite()`, whose
    /// presses already wear their own SENDING… labels; raising it there would
    /// draw the refresh line on every screen for every action. Read by the
    /// screens a person can pull down on, which draw one typed line at the
    /// top of their scrolled content while it is true.
    @Published private(set) var refreshing = false
    /// How many pulls are inside `poll` right now. Counted,
    /// not a bool, so two overlapping pulls (a tab's own plus the person's)
    /// cannot clear the flag while one is still waiting.
    private var refreshesOut = 0
    private var settlingTimeouts: [String: Task<Void, Never>] = [:]
    private var settlingCardTimeouts: [String: Task<Void, Never>] = [:]
    private var settlingPreferenceTimeouts: [String: Task<Void, Never>] = [:]
    private var settlingAnswerTimeouts: [String: Task<Void, Never>] = [:]
    /// Check-ins minted and check-ins finished. Only `poll`'s mint path
    /// counts — a join is not a new read. `refreshAfterWrite` compares them
    /// so a write's refresh is a read made *after* the write.
    private var pollsStarted = 0
    private var pollsCompleted = 0
    /// The verbs whose success means the agent should shortly leave the
    /// list — the writes that settle against the *list*. The answer verbs
    /// settle too, but against the row's question rather than its
    /// presence: see `answerHoldActions`.
    private static let settlingActions: Set<String> = [
        PhoneActions.stopSession, PhoneActions.closeTerminal,
        PhoneActions.deleteAgent, PhoneActions.hideSession,
    ]
    /// The verbs whose success means the row's *question* should shortly
    /// go — a separate set from `settlingActions` because the session
    /// stays live, so `pruneSettling`'s test would never fire for it.
    private static let answerHoldActions: Set<String> = [
        PhoneActions.answerQuestion, PhoneActions.answerQuestions,
        PhoneActions.reply,
    ]
    /// The dedupe guard's refusal, spelled exactly once.
    /// `OutboxStore.transportSentences` carries this same constant: the
    /// collision is transport-shaped — the outbox sweep hitting a verb a
    /// live composer still holds — so the outbox must retry later rather
    /// than paint it on an entry as a refusal the Mac never uttered.
    /// `nonisolated` so the outbox's static set can read it.
    nonisolated static let stillSendingRefusal =
        "Still sending your last tap — give it a moment."
    /// The queue's own dedupe, in words: the same press — same subject,
    /// same verb — is already written down and will go in turn. In
    /// `OutboxStore.transportSentences` for `stillSendingRefusal`'s reason.
    nonisolated static let queuedTwiceRefusal =
        "That press is already in the queue — it will go in turn."
    /// Whether the single ordered sender is inside `flushReceipts`, and
    /// whether a press was queued while it was — so a kick that lands
    /// mid-sweep makes the sweep go round once more rather than starting a
    /// second one.
    private var senderRunning = false
    private var senderKicked = false
    /// Token whose bars `usage` currently holds. Empty means no bars, or
    /// bars that must not be shown for this pairing.
    private var usageToken = ""
    /// The widget's reload throttle: the figures behind the last
    /// `reloadTimelines`, and when it was asked for. The summary *file* is
    /// written on every check-in (the tile's age line must say when the app
    /// last heard); the reload is asked for when the figures changed (at
    /// most once a minute) — and, changed or not, once the last reload is
    /// nine minutes old while check-ins continue. The second rule is the
    /// tile's honesty: its timeline dims itself at ten minutes *after the
    /// last reload*, and a warm app polling every 4s behind an unchanged
    /// fleet must replace that timeline before its own tile starts admitting
    /// an age it does not have. Both throttle slots survive `stop()`, so two
    /// more rungs cover the warm return: `start()` forgets the figures
    /// (a fresh session's first check-in counts as changed), and
    /// `.background` asks for one final reload (`flushWidgetReload`) so the
    /// tile's dim clock starts from the moment the app actually left rather
    /// than from whenever the throttle last let a reload through. Worst
    /// case is still ~7 reloads an hour warm, well inside WidgetKit's
    /// budget; burning the budget on identical numbers every poll is how a
    /// tile goes stale for real.
    private var lastReloadFigures: FleetSummary?
    private var lastWidgetReload = Date.distantPast
    /// What the last summary write said: `"ok <epoch>"` or the failure in
    /// words, for the profile screen's WIDGET rows. Empty until the first
    /// check-in of this process.
    @Published private(set) var widgetWriteReport = ""
    /// Whether the widget has been reloaded at all since this process
    /// started: a fresh process reads what the tile holds out of
    /// `UserDefaults` (`WidgetReloadBudget.lastReload*Key`) on its first
    /// publish, so a `BGAppRefreshTask` run — one client per run — knows
    /// the figures the tile already draws instead of reloading blind.
    private var reloadSlotsLoaded = false
    /// Changed figures reload at most this often.
    private static let reloadMinSeconds: TimeInterval = 60
    /// Unchanged figures still reload this often — under the widget's own
    /// 600s dim horizon, so a fresh note always outruns the dim entry.
    private static let reloadFreshnessSeconds: TimeInterval = 540

    /// This Mac answered the `card_sync` kind with a 404 — it predates the
    /// delta read entirely. `CardCacheStore.sync` stops asking rather than
    /// re-requesting a board that can never land. Cleared on a fresh
    /// `start(record:)`, so an upgraded Mac is asked again on the next
    /// launch, and lifted at once by a board frame carrying a real
    /// `revision`, which only a Mac that has the kind can send.
    private(set) var cardSyncUnsupported = false
    /// The Mac has said 404 to the `terminal` kind: an older Dark Army. Asked
    /// once, `cardSyncUnsupported`'s pattern exactly, and cleared on a fresh
    /// `start(record:)` so an upgraded Mac is asked again next launch.
    private(set) var terminalUnsupported = false
    /// The Mac has said 404 to the `conversation` kind: an older Dark Army.
    /// `terminalUnsupported`'s twin, cleared where it is.
    private(set) var conversationUnsupported = false
    /// The session whose conversation is on screen. The cache holds the
    /// cursor; this only names which id the poll should ask about.
    @Published private(set) var conversationWatching: String?
    /// Last `available: false` reason per session, the status line's words.
    @Published private(set) var conversationUnavailable: [String: String] = [:]
    static let homeCatchUpHops = 40
    static let awayCatchUpHops = 3
    /// The session whose Dark Army-owned terminal is on screen, set by the agent
    /// screen on appear and cleared on disappear. The away feed runs
    /// **only** while this is set — a phone left on a terminal screen away
    /// from home is the worst relay case this app has — and never on the
    /// background check-in or in the widget.
    @Published private(set) var terminalWatching: String?
    /// The away feed's raw-byte cursor: the `bytes_read` the last frame
    /// quoted, `-1` asking the Mac for a whole paint. Reset by
    /// `watchTerminal` and `restartTerminalFeed`.
    private(set) var terminalCursor = -1
    /// Where the away feed's bytes go: the terminal pane's coordinator,
    /// which owns the emulator. Set while a pane is on screen and away;
    /// nil otherwise, and the bytes are simply not asked for.
    var onTerminalBytes: ((TerminalBytesFrame) -> Void)?

    /// The check-in loop is running. Read by the unlock path so a lock that
    /// left the poller alive is not followed by a restart.
    var isPolling: Bool { task != nil }

    init() {
        receiptsForwarding = receipts.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
        conversationForwarding = conversationCache.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
    }

    func start(record: PairingRecord) {
        if record.token != usageToken {
            forgetUsage()
        }
        // A new pairing drops what was banked under the one before: on
        // the pairing screen no gate re-runs and `bank` keeps accepting,
        // so the first drain here would file an old Mac's buzz under the
        // new token. Compared ahead of the teardown that nils the record;
        // the unlock block drains before it calls `start`, so nothing of
        // the cold launch or a `.restart` is lost.
        if self.record?.token != record.token {
            PushRegistrar.shared.forgetBanked()
        }
        stop()
        // Forget the throttle's figures, not its clock: the first check-in
        // of a fresh session counts as changed, so a warm return does not
        // sit behind a dimmed tile because the numbers happen to match.
        lastReloadFigures = nil
        cardSyncUnsupported = false
        terminalUnsupported = false
        conversationUnsupported = false
        self.record = record
        // No home key means this record predates sealed home access: the
        // Mac refuses its every request, so say so at once and poll nothing.
        guard let home = buildHomeChannel(record) else {
            forgetPairing()
            lastError = PhoneActions.pairAgain
            return
        }
        // The one place a pairing becomes the current one, so it is the one
        // place the two pairing-scoped stores are rebased. `adopt` drops any
        // cached card stamped with a *different* Mac, document and all, and
        // `rebase` moves any press minted under one straight to `stuck`
        // rather than ever replaying it here.
        cardCache.adopt(record.token)
        conversationCache.adopt(record.token)
        receipts.rebase(to: record.token)
        heldPicture.adopt(record.token)
        notificationLog.adopt(record.token)
        linkTiming.adopt(record.token)
        restoreHeldPicture(record.token)
        homeChannel = home
        channel = buildChannel(record, socket: true)
        // The token is captured *here*, at build time: a late fire from a
        // channel that outlives this pairing still names the pairing it
        // counted under, whatever `self.record` says by then.
        channel?.onCounters = { [weak self, token = record.token] send, recv in
            self?.noteCounters(token: token, send: send, recv: recv)
        }
        home.onCounters = { [weak self, token = record.token] send, recv in
            self?.noteHomeCounters(token: token, send: send, recv: recv)
        }
        // Every completed trip, either way, lands in the timing ring.
        // `backgroundRefresh` and `lockScreenWrite` wire none of this:
        // a check-in nobody is watching is not a trip anyone reads.
        channel?.onTiming = { [weak self] sample in
            self?.linkTiming.record(sample)
        }
        home.onTiming = { [weak self] sample in
            self?.linkTiming.record(sample)
        }
        // The socket lane, where the record carries an address: a pushed
        // picture lands through the one `applyState` site, and the line's
        // standing becomes the profile's word. Opened only by `via`'s own
        // `didSet` (away, on screen) and one check-in is made the moment
        // it opens, which arms it on the Mac and fetches the picture.
        channel?.onPush = { [weak self] payload in
            self?.tookPush(payload, record: record)
        }
        channel?.onSocketState = { [weak self] state in
            self?.socketStateChanged(state, record: record)
        }
        // The mailbox took the frame: "asking the relay" becomes "waiting
        // for the Mac". Worded here; the channel only says when.
        // Only while a check-in is running: every relay request fires this,
        // and a write pressed while offline must not leave "waiting for
        // the Mac" on screen with nothing in flight.
        channel?.onSent = { [weak self] in
            guard let self, self.polling != nil else { return }
            self.notePhase(Phase.relayWaiting)
        }
        seedRouteFromLastPoll()
        // The first check-in is the one a person is waiting on
        // (`wakeCheckIn`). Away last time, the socket comes up now so it can
        // carry it; home last time, it comes up only if the quick home probe
        // misses (`pollOnce`) — a home opening costs the relay nothing.
        wakeCheckIn = true
        openLineIfLastAway()
        status = .connecting
        task = Task { [weak self] in
            while !Task.isCancelled {
                // Past the grace with nobody back: shut down, so a process
                // iOS resumed for a refresh or a push never polls the
                // mailbox twice. `stop()` cancels this very task; the
                // `return` right after means nothing runs on it cancelled.
                if BackgroundGrace.expired(departedAt: self?.departedAt, now: Date()) {
                    self?.stop()
                    return
                }
                // Departed and inside the grace: hold the picture, poll
                // nothing, look again in a second. `wake()` is what resumes
                // check-ins; this tick is no attempt and notes none.
                if self?.departedAt != nil {
                    try? await Task.sleep(for: .seconds(1))
                    continue
                }
                await self?.poll(record)
                // The pause and the note that publishes it are one call, so
                // the countdown on screen is the sleep actually taken. The
                // fallback is unreachable in practice — a nil `self` ends
                // the loop on the next `isCancelled` — and exists because
                // `self` is weak.
                let pause = await self?.noteAttempt() ?? 4_000_000_000
                try? await Task.sleep(nanoseconds: pause)
            }
        }
    }

    /// The relay line comes up at once only when the last poll went away;
    /// after a home poll `pollOnce` opens it if the quick home probe
    /// misses. One helper, so the start loop itself never reads `via`.
    private func openLineIfLastAway() {
        if via == .relay { channel?.socket?.wanted = true }
    }

    /// The app went to the background. Nothing is torn down: the loop, the
    /// channels, the record and the digest stay for a return inside
    /// `BackgroundGrace.window`. Only the moment is written down and the
    /// counters land in the Keychain, as `stop()` lands them.
    func suspend() {
        guard task != nil else { return }
        departedAt = Date()
        // iOS suspends background sockets: the line is closed here and
        // reopened by `wake()`, never left to die invisibly.
        channel?.socket?.wanted = false
        flushCounters()
        flushHomeCounters()
    }

    /// Back inside the grace. A check-in the system cut mid-flight finishes
    /// first (it fails fast on resume); then exactly one check-in now, so
    /// the picture is fresh in seconds rather than at the loop's next tick.
    /// An `.inactive` glance never stamped a departure and wakes nothing.
    /// `probeHome: false` for the pull-to-refresh's reason: the person is
    /// waiting on this one; the loop's own polls keep probing home.
    func wake() {
        guard departedAt != nil else { return }
        departedAt = nil
        // Up now when the last poll was away; after a home poll it comes up
        // only when the quick home probe misses (`pollOnce`), so a home wake
        // neither opens the relay line nor spends the Mac's wake budget.
        openLineIfLastAway()
        wakeCheckIn = true
        guard let record else { return }
        Task { [weak self] in
            guard let self else { return }
            // Waited out, not joined: `poll` joins a running check-in and
            // would return without a fresh one. The slot is cleared by the
            // loop's own continuation of `poll`, which may run *after* this
            // waiter resumes from the task's value — so wait for the slot
            // itself, yielding so that continuation gets its turn, or the
            // check-in below would join the finished task and make none.
            while let running = self.polling {
                await running.value
                await Task.yield()
            }
            guard self.departedAt == nil, record.token == self.record?.token
            else { return }
            await self.poll(record, probeHome: false)
        }
    }

    /// The app came to the front with the Face ID sheet up: open the socket
    /// now so it is ready when the unlock's `wake()` asks. Nothing is sent —
    /// no frame leaves before the unlock — and the Mac, hearing the relay's
    /// `peer:1`, stops idling its mailbox. `suspend()` closes it again.
    func prewarm() {
        guard departedAt != nil, task != nil, via == .relay else { return }
        channel?.socket?.wanted = true
    }

    /// The unlock that `prewarm()` opened the line for failed or was
    /// cancelled: close it again rather than leave it up behind the lock
    /// until the background grace runs out. A no-op once `wake()` ran.
    func abandonPrewarm() {
        guard departedAt != nil else { return }
        channel?.socket?.wanted = false
    }

    /// Set by `start()` and `wake()`, spent by the next `pollOnce`: the
    /// check-in a person is waiting on tries the home address with
    /// `wakeProbe`'s patience rather than the walk's.
    private var wakeCheckIn = false

    /// A cold launch has no poll history. The last successful poll's route
    /// is on disk (`lastKnownAwayKey`), so a phone that was away last time
    /// starts by asking the relay — `pollOnce`'s own `knowsItIsAway` branch,
    /// reached through the same `via` field the AWAY badge reads. Only ever
    /// seeds `.relay`: a `.lan` guess costs nothing, and a warm `start()`
    /// already holds the truth in `via`.
    private func seedRouteFromLastPoll() {
        guard channel != nil,
              UserDefaults.standard.bool(forKey: Self.lastKnownAwayKey)
        else { return }
        via = .relay
    }

    /// The one writer of `phase`.
    private func notePhase(_ text: String) { phase = text }

    /// The loop's own pause — 8s through the mailbox, 4s at home, exactly as
    /// before — **and** the stamp that publishes it. The two are one function
    /// so the countdown on screen and the sleep the loop actually takes are
    /// the same number by construction, and so `via` is read once rather than
    /// twice. Called from `start()`'s loop and nowhere else; the loop's
    /// departed tick is not an attempt and does not call it.
    private func noteAttempt() -> UInt64 {
        // Remote polling is deliberately slower: 8s through the
        // mailbox against 4s at home.
        let pause: UInt64 = (via == .relay)
            ? 8_000_000_000 : 4_000_000_000
        switch status {
        case .live: link.failures = 0
        case .unreachable: link.failures += 1
        // `.unpaired` is not a connectivity failure and `.connecting` /
        // `.idle` have not answered the question yet: neither moves the count.
        case .unpaired, .connecting, .idle: break
        }
        let seconds = Double(pause) / 1_000_000_000
        link.waitSeconds = Int(seconds)
        link.nextAttemptAt = Date().addingTimeInterval(seconds)
        link.route = via
        return pause
    }

    func stop() {
        catchUpRequests.cancel()
        historyGeneration += 1
        task?.cancel()
        task = nil
        departedAt = nil
        // The check-in in flight goes with the timer, and it goes *cancelled*
        // rather than orphaned: it is holding a waiter on the relay mailbox,
        // and a pop there is a removal.
        polling?.cancel()
        polling = nil
        // The held picture stops being something to quote back the moment
        // this client stops reading.
        heldStateDigest = ""
        heldSectionDigests = [:]
        heldWire = Snapshot()
        lastFullState = nil
        // Background is one of the two moments the counters must land in
        // the Keychain (the other is the once-a-minute throttle) — flushed
        // while `record` still names the pairing they belong to.
        flushCounters()
        flushHomeCounters()
        // Sever the dying channels' callbacks: the cancelled poll task keeps
        // awaiting whatever request was in flight, and its late answer would
        // otherwise stage a dead pairing's counters into the next `start`.
        channel?.onCounters = nil
        channel?.onSent = nil
        channel?.onTiming = nil
        channel?.onPush = nil
        channel?.onSocketState = nil
        channel?.socket?.wanted = false
        channel = nil
        AwayState.shared.socket = ""
        homeChannel?.onCounters = nil
        homeChannel?.onTiming = nil
        homeChannel = nil
        // Cleared with the timer, so a pull-to-refresh on a screen that is
        // being torn down (unpair, an expired grace) cannot reach the Mac.
        record = nil
        // A queued press does not follow a torn-down pairing: the running
        // transmit finishes on its own (`post` tolerates a nil record) and
        // the sweep is not re-kicked. `start()` → `onLive` takes it up again.
        senderKicked = false
        // No stale countdown behind a stopped or un-paired app;
        // `start()` calls `stop()` first, so a fresh session begins at
        // attempt zero for free.
        link = LinkAttempt()
        phase = ""
    }

    /// The away channel, if this record carries one. A record paired before
    /// the away path has no key — no channel, no remote, stated in the UI.
    /// Counters start from the Keychain's own where it is ahead of the
    /// record handed in, so a restart resumes where the persisted counters
    /// left off rather than replaying from the published copy's.
    /// `socket` attaches the socket lane where the record carries an
    /// address — `start()` alone asks for it; `backgroundRefresh` and
    /// `lockScreenWrite` never open a line.
    private func buildChannel(_ record: PairingRecord,
                              socket: Bool = false) -> RelayChannel? {
        guard !record.relayKey.isEmpty, !record.relayURL.isEmpty,
              let key = Data(base64Encoded: record.relayKey) else {
            return nil
        }
        let stored = storedCounters?()
        let built = RelayChannel(key: key, base: record.relayURL,
                                 sendCtr: max(record.sendCtr, stored?.send ?? 0),
                                 recvCtr: max(record.recvCtr, stored?.recv ?? 0))
        if socket, !record.relayWSURL.isEmpty {
            built.socket = RelaySocket(base: record.relayWSURL,
                                       channel: RelayTransport.channelId(key: key))
        }
        return built
    }

    /// A picture the Mac pushed down the socket: applied through the one
    /// `applyState` site exactly as a check-in's answer is, the bars taken
    /// from it, the tile refreshed, and one `push` sample recorded with how
    /// old the picture was on arrival. Ignored while departed or at home,
    /// and for any pairing but the current one.
    private func tookPush(_ payload: [String: Any], record: PairingRecord) {
        guard departedAt == nil, knowsItIsAway,
              record.token == self.record?.token else { return }
        guard (payload["status"] as? Int ?? 0) == 200,
              let text = payload["body"] as? String else { return }
        let body = Data(text.utf8)
        guard applyState(body, record: record, route: .relay) != .unreadable else {
            return
        }
        _ = takeUsage(from: body, record: record)
        publishWidgetSummary()
        lastHeard = Date()
        let stamped = (payload["ts"] as? Double)
            ?? (payload["ts"] as? NSNumber)?.doubleValue ?? 0
        let now = Date().timeIntervalSince1970
        linkTiming.record(LinkTimingSample(
            at: stamped > 0 ? stamped : now, route: LinkTimingRoute.socket,
            kind: "push", status: 200,
            totalSeconds: stamped > 0 ? max(0, now - stamped) : 0,
            postSeconds: 0))
    }

    /// The socket's word for the profile, and one check-in the moment the
    /// line opens — which arms it on the Mac and fetches the picture —
    /// unless one is already running.
    private func socketStateChanged(_ state: RelaySocket.State,
                                    record: PairingRecord) {
        AwayState.shared.socket = state.word
        guard state == .open, departedAt == nil, polling == nil,
              record.token == self.record?.token else { return }
        Task { [weak self] in
            await self?.poll(record, probeHome: false)
        }
    }

    /// The sealed home channel — nil for a record with no home key, which
    /// `start` turns into `pairAgain`. Counters from the Keychain where it
    /// is ahead of the record handed in, the relay channel's rule.
    private func buildHomeChannel(_ record: PairingRecord) -> HomeChannel? {
        guard !record.homeKey.isEmpty,
              let key = Data(base64Encoded: record.homeKey),
              key.count == 32 else {
            return nil
        }
        let stored = storedHomeCounters?()
        return HomeChannel(key: key,
                           sendCtr: max(record.homeSendCtr, stored?.send ?? 0),
                           recvCtr: max(record.homeRecvCtr, stored?.recv ?? 0))
    }

    private func noteHomeCounters(token: String, send: Int, recv: Int) {
        pendingHomeCounters = (token, send, recv)
        guard Date().timeIntervalSince(lastCounterPersist) >= 60 else { return }
        flushHomeCounters()
    }

    private func flushHomeCounters() {
        guard let pending = pendingHomeCounters else { return }
        pendingHomeCounters = nil
        guard pending.token == record?.token else { return }
        lastCounterPersist = Date()
        onHomeCounters?(pending.token, pending.send, pending.recv)
    }

    private func noteCounters(token: String, send: Int, recv: Int) {
        pendingCounters = (token, send, recv)
        guard Date().timeIntervalSince(lastCounterPersist) >= 60 else { return }
        flushCounters()
    }

    private func flushCounters() {
        guard let pending = pendingCounters else { return }
        pendingCounters = nil
        // The generation guard: counters staged under one pairing must never
        // flush under another. Belt to `stop()`'s severing braces — if a
        // future path forgets to sever, the mismatch is still dropped here
        // and again at the store.
        guard pending.token == record?.token else { return }
        lastCounterPersist = Date()
        onCounters?(pending.token, pending.send, pending.recv)
    }

    /// Pull-to-refresh: one extra poll, right now.
    ///
    /// It does **not** touch the 4s timer. Both paths run on the main actor so
    /// their writes serialise, and `poll` always writes the newest response it
    /// got — last-writer-wins is correct here. Do not add locking and do not
    /// cancel the timer for a refresh.
    /// `probeHome: false` because this is the refresh a *person* is waiting
    /// on — the pull-to-refresh spinner, or the state re-read a tapped
    /// button is dimmed behind. Away, the homecoming probe is a 4s wait on
    /// an address that is silent by definition, and paying it here put four
    /// seconds between every away tap and its own confirmation. The timer's
    /// own polls keep probing, so coming home is still noticed within the
    /// same minute.
    func refreshNow() async {
        guard let record else { return }
        refreshesOut += 1
        refreshing = refreshesOut > 0
        defer {
            refreshesOut -= 1
            refreshing = refreshesOut > 0
        }
        await poll(record, probeHome: false)
    }

    /// The refresh a write is dimmed behind. `refreshNow` joins whatever
    /// check-in is running, and one that started before the write still
    /// lists what the write removed; this waits for that one and then makes
    /// exactly one more. Never two at once — `poll` still serialises, and
    /// the `extra < 2` bound plus the `record` guard stop the loop after
    /// `stop()` has torn the pairing off.
    func refreshAfterWrite() async {
        let wanted = pollsStarted + 1
        var extra = 0
        while pollsCompleted < wanted, extra < 2 {
            guard let record else { return }
            await poll(record, probeHome: false)
            extra += 1
        }
    }

    /// The action out for this scope, or nil — the session being written
    /// to, else the card. What the views' `busy` / `sending` and SENDING…
    /// label swaps read: one seam, so the lock and the label can never
    /// disagree about which write is out.
    func writeInFlight(for sessionId: String) -> String? {
        guard !sessionId.isEmpty else { return nil }
        return writesInFlight[sessionId]
    }

    /// True from the instant a confirmed Delete for this card goes out until
    /// the board stops listing it. Two windows, contiguous by construction:
    /// `writesInFlight` is written *before* the request and cleared by
    /// `post`'s `defer` after `refreshAfterWrite()`, and `beginSettlingCard`
    /// fires inside that window on the 200 — so there is no instant where
    /// neither holds. Delete-specific on purpose: a Save or a Start is
    /// card-scoped too, and greying the row for those would say the card is
    /// leaving when it is not.
    func cardLeaving(_ id: String) -> Bool {
        writeInFlight(for: id) == PhoneActions.boardDelete
            || settlingCards.contains(id)
    }

    /// One chosen write. Host walk matches `poll` so a VPN flip cannot make
    /// writes fail while reads still work. Every leg is a sealed frame.
    /// One in-flight POST per *scope* — the session being written to, else
    /// the card, else the bare verb — so a double-tap on a confirmed button
    /// cannot fire twice, and Dismiss on one agent no longer silently blocks
    /// Dismiss on every other. The window deliberately spans the state
    /// refresh after a 200/409: shortening it would reopen the reported gap,
    /// an un-dimmed button whose press the dedupe swallows.
    ///
    /// **Every press is written down before it goes.** `token` is the
    /// `command_token` the Mac dedupes on — minted here and **nowhere else**
    /// — so a reply lost on a flaky connection replays as the same press
    /// rather than as a second one. A non-empty `token` is a replay of a
    /// receipt already on file (`flushReceipts`), which is the one case that
    /// does not open a new record.
    ///
    /// `refreshAfter` is the one-field toggle's exemption. The dedupe window
    /// deliberately spans the state re-read, because a control that stops
    /// dimming before the board has answered is a control whose next press
    /// the dedupe swallows in silence. A toggle whose new value is already on
    /// screen has no such gap: it acknowledges on send, the next ordinary
    /// check-in carries the Mac's own answer, and holding every other button
    /// on the card dim for a whole poll cycle to learn something already
    /// drawn is the delay this exists to remove. A 409 still re-reads —
    /// that is a refusal whose current values the screen must be told about.
    ///
    /// `authorised` is the sender's word that Face ID already passed at the
    /// press (`Receipt.authorised`); the relay rung then asks for nothing.
    /// A first press passes the default and is asked here, as before.
    func post(action: String, fields: [String: String] = [:],
              scope: String = "", token: String = "",
              refreshAfter: Bool = true,
              authorised: Bool = false) async -> PhoneActionResult {
        guard let record else {
            return PhoneActionResult(ok: false, detail: "no longer paired")
        }
        let scopeKey = Self.scopeKey(action: action, fields: fields, scope: scope)
        if writesInFlight[scopeKey] != nil {
            return PhoneActionResult(ok: false, detail: Self.stillSendingRefusal)
        }
        writesInFlight[scopeKey] = action
        defer { writesInFlight[scopeKey] = nil }

        // The one-time mark, minted in exactly one place. A replay carries
        // the token it was minted under; a fresh press opens its record
        // *before* the request goes, so a press made and then interrupted is
        // still on file when the app comes back.
        // A **first press** was made from a screen that draws the Mac's
        // refusal inline; a **resend** (`token` non-empty) had nobody
        // watching. That is what decides whether a refusal is listed in
        // PENDING or is finished business, and it is known only here.
        let firstPress = token.isEmpty
        let mark = token.isEmpty
            ? receipts.open(action: action, scope: scopeKey, fields: fields,
                            effect: ReceiptLedger.effect(for: action,
                                                         fields: fields,
                                                         scope: scopeKey),
                            pairingToken: record.token).id
            : token

        var body = fields
        body["action"] = action
        body["command_token"] = mark
        let payload = try? JSONSerialization.data(withJSONObject: body)

        // The relay rung, behind the owner's face: acting from away asks for
        // Face ID or the passcode first. Written once and called from both
        // sides of the walk — away it runs *before* the home addresses are
        // knocked on at all, at home it is the fallback it has always been.
        func viaRelay(_ channel: RelayChannel) async -> PhoneActionResult {
            // `authorised` is the sender's word that the face was checked
            // at the press; a first press is asked here.
            let admitted = authorised ? true : await RemoteAuth.shared.authorize()
            guard admitted else {
                // The press never left the phone. A **first press** has the
                // person at the sheet: there is nothing pending, so the
                // record goes rather than sitting in `sent` and re-raising
                // this same sheet on every later sweep. A **queued or
                // replayed** press (`token` non-empty) — one queued at home
                // that fell back to the relay, or a `.sent` one re-taken
                // outside the grace — is closed `.stuck` carrying the
                // sentence instead: a press that vanished with no mark, no
                // note and no row was the reported bug, and a `.stuck` row
                // offers RETRY, which asks again.
                if firstPress {
                    receipts.remove(mark)
                } else {
                    receipts.refuse(mark, detail: ReceiptLedger.faceNeededLine,
                                    surfaced: false)
                }
                return PhoneActionResult(ok: false,
                                         detail: ReceiptLedger.faceNeededLine)
            }
            let answer = await channel.request(kind: "action", body: body)
            guard answer.failure.isEmpty else {
                // Transport-shaped: the press may have landed with its
                // reply lost, so the record stays `sent` — behind a
                // doubling backoff, and nothing is painted on it.
                receipts.holdOff(mark)
                return PhoneActionResult(ok: false, detail: answer.failure)
            }
            let parsed = PhoneActionResult.parse(data: answer.body,
                                                 code: answer.status)
            if answer.status == 200 {
                receipts.accept(mark, revision: parsed.revision)
                beginSettling(action: action,
                              sessionId: fields["session_id"] ?? scope)
                if action == PhoneActions.boardDelete,
                   let cid = fields["card_id"], !cid.isEmpty {
                    beginSettlingCard(cid)
                }
                if refreshAfter { await refreshAfterWrite() }
                // The Mac's report rides back from away exactly as it does
                // at home: a blank here was the batch Start's lost report.
                return PhoneActionResult(ok: true, detail: parsed.detail,
                                         revision: parsed.revision,
                                         current: parsed.current)
            }
            let refusal = answer.detail.isEmpty
                ? "The Mac refused (HTTP \(answer.status))."
                : answer.detail
            // **Every** answer that is not a 200 is a final answer about
            // this press, not only the 409 — a 403 for a lapsed lease, a
            // 404 for a verb this door does not carry, a 400 for a payload
            // it will not parse. Leaving those in `sent` had the sweep
            // re-send them on every poll for ever: an unprompted Face ID
            // sheet per relay attempt, a PENDING line contradicting the
            // refusal already on screen, and the press finally executing
            // hours later when the lease was renewed at home.
            receipts.refuse(mark, detail: refusal, surfaced: firstPress)
            // Away keys the Mac refused for pace alone are the terminal
            // pane's to retry (`AwayKeys`), in order and unasked: they are
            // not a refused press, so no REFUSED line and no note on the
            // agent screen for keys that land a few seconds later.
            if action == PhoneActions.terminalInput, fields["bytes"] != nil,
               refusal == AwayKeys.slowDown {
                receipts.remove(mark)
            }
            if answer.status == 409 {
                await refreshAfterWrite()
            }
            // The Mac's own words come back verbatim — the lease refusal
            // included, which views recognise by `leaseRefusalPrefix`.
            return PhoneActionResult(ok: false, detail: refusal,
                                     revision: parsed.revision,
                                     current: parsed.current)
        }

        // Away, the relay is the only route that can work: the home
        // addresses are silent rather than refusing from a foreign network,
        // so knocking on them first cost 8 + 4 × (n−1) seconds before the
        // leg that answers was even tried.
        if knowsItIsAway, let channel {
            return await viaRelay(channel)
        }

        var attempted = false
        if let home = homeChannel {
            for (index, host) in hostCandidates(record).enumerated() {
                guard case .ok = PhoneActions.homeURL(host: host, port: record.port)
                else { continue }
                attempted = true
                // nil is a transport error: nothing there, the walk moves on.
                guard let answer = await home.request(
                    kind: "action", body: body, host: host, port: record.port,
                    timeout: index == 0 ? 8 : Self.directProbe) else { continue }
                if answer.failure == RelayChannel.Trouble.cutShort {
                    receipts.holdOff(mark)
                    return PhoneActionResult(ok: false, detail: "")
                }
                // A 421 is a tunnel address the door does not serve: skip
                // the host, keep the pairing, promote nothing.
                if answer.misdirected { continue }
                if index > 0 { onPromote?(host) }
                // Only the door's own 403 — an unknown channel — means this
                // phone is no longer paired; an inner 403 is one verb's own
                // refusal and is shown, never acted on.
                if answer.refusedAtTheDoor, answer.status == 403 {
                    forgetPairing()
                    return PhoneActionResult(ok: false, detail: "no longer paired")
                }
                guard answer.failure.isEmpty else {
                    receipts.holdOff(mark)
                    return PhoneActionResult(ok: false, detail: answer.failure)
                }
                if answer.status == 404 {
                    // A door that does not carry this verb is a final
                    // answer, `viaRelay`'s rule: re-sending it for ever
                    // would never turn into a different one.
                    receipts.refuse(mark, detail: PhoneActions.olderMac,
                                    surfaced: firstPress)
                    return PhoneActionResult(ok: false, detail: PhoneActions.olderMac)
                }
                let parsed = PhoneActionResult.parse(data: answer.body,
                                                     code: answer.status)
                if answer.status == 200 {
                    receipts.accept(mark, revision: parsed.revision)
                    beginSettling(action: action,
                                  sessionId: fields["session_id"] ?? scope)
                    if action == PhoneActions.boardDelete,
                       let cid = fields["card_id"], !cid.isEmpty {
                        beginSettlingCard(cid)
                    }
                } else {
                    // `viaRelay`'s rule on this leg: any non-200 is final.
                    receipts.refuse(mark, detail: parsed.detail.isEmpty
                        ? "The Mac refused (HTTP \(answer.status))."
                        : parsed.detail, surfaced: firstPress)
                }
                if answer.status == 409 || (answer.status == 200 && refreshAfter) {
                    await refreshAfterWrite()
                }
                return parsed
            }
        }
        // The home walk found nothing — the same relay rung, as the fallback.
        if let channel {
            return await viaRelay(channel)
        }
        let detail = attempted ? "Could not reach the Mac." : "Bad address."
        // A transport failure leaves the receipt in `sent`: the press may
        // have landed with its reply lost, and only a token replay can tell.
        // Nothing is painted on the record — `transportSentences`' rule.
        receipts.holdOff(mark)
        if status != .unpaired {
            status = .unreachable
            lastError = detail
        }
        return PhoneActionResult(ok: false, detail: detail)
    }

    /// The scope a press locks and queues under: the session being written
    /// to, else the card, else the bare verb. One function for `post` and
    /// `enqueue`, so the lock and the queue can never disagree about which
    /// subject a press belongs to.
    private static func scopeKey(action: String, fields: [String: String],
                                 scope: String) -> String {
        if !scope.isEmpty { return scope }
        if let sid = fields["session_id"], !sid.isEmpty { return sid }
        if let cid = fields["card_id"], !cid.isEmpty { return cid }
        return "verb:\(action)"
    }

    /// **A press is queued, not awaited.** The receipt is written down at
    /// the tap in `.queued` and the control is the person's again at once;
    /// the single ordered sender (`flushReceipts`) takes it in turn, one
    /// scope's presses in the order they were made. No `writesInFlight`,
    /// no `refreshAfterWrite()`, nothing awaited on the transport: the
    /// snapshot the ordinary poll delivers is what says it landed.
    ///
    /// Two refusals are the phone's own and come back at once: the same
    /// subject and verb already in play (`queuedTwiceRefusal`), and, from
    /// away, a declined Face ID sheet — asked **before** the record exists,
    /// so a declined sheet leaves nothing queued, and remembered on the
    /// record so the sender never raises it again.
    func enqueue(action: String, fields: [String: String] = [:],
                 scope: String = "") async -> PhoneActionResult {
        guard let record else {
            return PhoneActionResult(ok: false, detail: "no longer paired")
        }
        let scopeKey = Self.scopeKey(action: action, fields: fields, scope: scope)
        // Same subject, same verb, **same payload**: a double-tap. A
        // different press on the same subject queues behind the first.
        if ReceiptLedger.duplicate(action: action, scope: scopeKey,
                                   fields: fields, in: receipts.receipts) {
            return PhoneActionResult(ok: false, detail: Self.queuedTwiceRefusal)
        }
        if knowsItIsAway {
            guard await RemoteAuth.shared.authorize() else {
                return PhoneActionResult(ok: false,
                                         detail: ReceiptLedger.faceNeededLine)
            }
        }
        var effect = ReceiptLedger.effect(for: action, fields: fields,
                                          scope: scopeKey)
        // A reply on a waiting row takes the answer hold: judged by the row
        // leaving `waiting` or standing on a different question, so the
        // mark stays SENT and a second copy is a duplicate until then —
        // `.replyHold`, not `.questionGone`, because the reply carries the
        // person's words and is never dropped unsent as goal-achieved, and
        // a row that keeps waiting closes it `.done` after `replyHold`.
        if action == PhoneActions.reply, effect == .none,
           let waiting = snapshot.agents.waiting.first(
               where: { $0.sessionId == scopeKey }) {
            effect = .replyHold(sessionId: scopeKey,
                                questionId: waiting.question.id)
        }
        // A row verb pressed on a row that is already out of the live
        // buckets (a Hide or Close offered on a finished row) is judged by
        // the row leaving every list: `.rowGone` would read as landed on
        // the very snapshot that offered the button, and the press would
        // be dropped unsent.
        if case .rowGone(let sid) = effect {
            let a = snapshot.agents
            let live = (a.waiting + a.running + a.sleeping)
                .contains { $0.sessionId == sid }
            if !live { effect = .rowGoneAnywhere(sessionId: sid) }
        }
        receipts.enqueue(action: action, scope: scopeKey, fields: fields,
                         effect: effect,
                         pairingToken: record.token,
                         authorised: knowsItIsAway)
        kickSender()
        return PhoneActionResult(ok: true, detail: "")
    }

    /// The word a scope's pressed control wears — `QUEUED`, `SENDING…`,
    /// `SENT` — or nil. The ledger's own arithmetic over its records.
    func queueMark(for scope: String) -> String? {
        guard !scope.isEmpty else { return nil }
        return ReceiptLedger.mark(for: scope, in: receipts.receipts)
    }

    /// The verb of the press a scope's controls are wearing, so a screen
    /// with several buttons on one subject can swap the right label.
    func queuedAction(for scope: String) -> String? {
        guard !scope.isEmpty else { return nil }
        return ReceiptLedger.marked(for: scope, in: receipts.receipts)?.action
    }

    /// The Mac's own words about a scope's last press, or nil — never a
    /// refusal older than `ReceiptLedger.refusedShown`, and never one of
    /// the phone's own `phoneAuthored` stops.
    func queueNote(for scope: String) -> QueueNote? {
        guard !scope.isEmpty else { return nil }
        return ReceiptLedger.note(for: scope, in: receipts.receipts,
                                  now: Date().timeIntervalSince1970)
    }

    /// The state of the newest press of one verb on a scope, or nil when
    /// no record of it remains (aged off, or never made). A screen whose
    /// confirmation is about one verb — the card's message box — reads
    /// its line off this rather than storing a word that never moves.
    func queuedState(action: String, for scope: String) -> Receipt.State? {
        guard !scope.isEmpty else { return nil }
        return receipts.receipts
            .filter { $0.scope == scope && $0.action == action }
            .max { $0.createdAt < $1.createdAt }?.state
    }

    /// The Mac's words about a scope's last press have been **drawn on the
    /// screen it was pressed from**: the refusal is read. Called by every
    /// screen that draws a `queueNote` — arms the confirmation, sets the
    /// note — so the record goes `.refused` (REFUSED on the QUEUE list for
    /// `refusedShown`, then aged like any finished business) rather than a
    /// `.stuck` row for ever. The record is **kept**, never removed: the
    /// note stays the subject's newest receipt, so an older refusal on the
    /// same scope can never become "the newest note" and swap its stale
    /// words in under a confirmation still armed.
    func readQueueNote(for scope: String) {
        guard let note = queueNote(for: scope) else { return }
        receipts.read(note.id)
    }

    /// Start the sender if it is not running; a running one goes round
    /// again. One `Task`, owned here, never two.
    private func kickSender() {
        senderKicked = true
        guard !senderRunning else { return }
        Task { await flushReceipts() }
    }

    /// The push registrar's wire: `post`'s exact walk — the LAN hosts
    /// first, then the sealed relay rung — but **never a Face ID prompt**.
    /// `register_push_token` only names where this phone's own buzzes go;
    /// the Mac still checks the device token, the away lease and
    /// `REMOTE_ACTIONS` per request, so skipping the local prompt widens
    /// nothing. The record and channel ride as parameters because the
    /// forget path runs this *after* `stop()` has torn both off the client.
    /// True only when the Mac answered 200.
    func quietPost(record: PairingRecord, channel: RelayChannel?,
                   home: HomeChannel?,
                   action: String, fields: [String: String]) async -> Bool {
        var body = fields
        body["action"] = action
        // The same sealed rung `post` rides, minus the RemoteAuth prompt
        // (the whole reason this method exists) — and, away, run before the
        // home walk rather than after it.
        func viaRelay(_ channel: RelayChannel) async -> Bool {
            let answer = await channel.request(kind: "action", body: body)
            return answer.failure.isEmpty && answer.status == 200
        }
        if knowsItIsAway, let channel {
            return await viaRelay(channel)
        }
        if let home {
            for (index, host) in hostCandidates(record).enumerated() {
                guard case .ok = PhoneActions.homeURL(host: host, port: record.port)
                else { continue }
                guard let answer = await home.request(
                    kind: "action", body: body, host: host, port: record.port,
                    timeout: index == 0 ? 8 : Self.directProbe) else { continue }
                // A tunnel host the door does not serve: skip it (the
                // four other walks' rule).
                if answer.misdirected { continue }
                // Reached is reached: any answer is the Mac, so the walk
                // stops here whatever the verdict was.
                return answer.failure.isEmpty && answer.status == 200
            }
        }
        // The home walk found nothing — the same sealed rung, as the fallback.
        guard let channel else { return false }
        return await viaRelay(channel)
    }

    /// One write from a lock-screen button, on a fresh client with nobody
    /// in front of it (`LockScreenActions`): the record from the Keychain,
    /// channels built for this send, the direction the last poll went, and
    /// `quietPost`'s walk — no Face ID sheet (iOS asked for the unlock on
    /// the action itself), no receipt, no outbox. The Mac still checks the
    /// pairing, the lease and the verb.
    func lockScreenWrite(record: PairingRecord, action: String,
                         fields: [String: String]) async -> Bool {
        stop()
        self.record = record
        guard let home = buildHomeChannel(record) else { return false }
        homeChannel = home
        channel = buildChannel(record)
        channel?.onCounters = { [weak self, token = record.token] send, recv in
            self?.noteCounters(token: token, send: send, recv: recv)
        }
        home.onCounters = { [weak self, token = record.token] send, recv in
            self?.noteHomeCounters(token: token, send: send, recv: recv)
        }
        seedRouteFromLastPoll()
        let ok = await quietPost(record: record, channel: channel, home: home,
                                 action: action, fields: fields)
        flushCounters()
        flushHomeCounters()
        return ok
    }

    /// The registration send, on the client's own live record and channels.
    /// False (never a prompt, never an error surface) when unpaired.
    func registerPush(fields: [String: String]) async -> Bool {
        guard let record else { return false }
        return await quietPost(record: record, channel: channel,
                               home: homeChannel,
                               action: PhoneActions.registerPushToken,
                               fields: fields)
    }

    /// `registerPush`'s twin for the Live Activity's update token: the same
    /// quiet route, the `register_activity_token` verb. False when unpaired.
    func registerActivityToken(fields: [String: String]) async -> Bool {
        guard let record else { return false }
        return await quietPost(record: record, channel: channel,
                               home: homeChannel,
                               action: PhoneActions.registerActivityToken,
                               fields: fields)
    }

    /// The forget path's capture: the record and channels the last
    /// unregister will ride, taken **before** `stop()` tears them down.
    /// The captured channels keep their own counters, so that final sealed
    /// send never replays a number the live channel already spent.
    func pushTeardown() -> (record: PairingRecord, channel: RelayChannel?,
                            home: HomeChannel?)? {
        guard let record else { return nil }
        return (record, channel, homeChannel)
    }

    /// A confirmed Stop / Close / Delete the Mac accepted: hold the session
    /// dimmed until the snapshot stops listing it, `Triage.beginStopping`'s
    /// shape. The timeout is the backstop, not the mechanism — the prune on
    /// the next decoded snapshot is what normally ends it.
    private func beginSettling(action: String, sessionId: String) {
        if Self.answerHoldActions.contains(action) {
            beginSettlingAnswer(sessionId)
            return
        }
        guard Self.settlingActions.contains(action), !sessionId.isEmpty else {
            return
        }
        settling.insert(sessionId)
        settlingTimeouts[sessionId]?.cancel()
        settlingTimeouts[sessionId] = Task { [weak self] in
            let nanos = UInt64(Self.settlingTimeout * 1_000_000_000)
            try? await Task.sleep(nanoseconds: nanos)
            if Task.isCancelled { return }
            self?.settling.remove(sessionId)
            self?.settlingTimeouts[sessionId] = nil
        }
    }

    /// An answer or reply the Mac accepted: hold the row's answer controls
    /// dimmed until the snapshot shows the question gone. Runs inside
    /// `post` after the 200 and *before* `refreshAfterWrite()`, which is
    /// what keeps `busy` true across the gap when `writesInFlight` clears.
    /// A row that is not waiting has no question state to revert to, so no
    /// hold is taken — today's behaviour for a reply on a running row.
    private func beginSettlingAnswer(_ sessionId: String) {
        guard !sessionId.isEmpty,
              let agent = snapshot.agents.waiting.first(
                where: { $0.sessionId == sessionId }) else {
            return
        }
        settlingAnswers[sessionId] = agent.question.id
        settlingAnswerTimeouts[sessionId]?.cancel()
        settlingAnswerTimeouts[sessionId] = Task { [weak self] in
            let nanos = UInt64(Self.settlingTimeout * 1_000_000_000)
            try? await Task.sleep(nanoseconds: nanos)
            if Task.isCancelled { return }
            self?.settlingAnswers[sessionId] = nil
            self?.settlingAnswerTimeouts[sessionId] = nil
        }
    }

    /// A confirmed Delete the Mac accepted: hold the card off the board
    /// until the snapshot stops listing it. `beginSettling`'s shape, and
    /// deliberately not a member of `settlingActions` — that set is
    /// session-keyed, so `pruneSettling` would never clear a card id.
    private func beginSettlingCard(_ id: String) {
        guard !id.isEmpty else { return }
        settlingCards.insert(id)
        settlingCardTimeouts[id]?.cancel()
        settlingCardTimeouts[id] = Task { [weak self] in
            let nanos = UInt64(Self.settlingTimeout * 1_000_000_000)
            try? await Task.sleep(nanoseconds: nanos)
            if Task.isCancelled { return }
            guard let self, self.settlingCards.contains(id) else { return }
            self.settlingCards.remove(id)
            self.settlingCardTimeouts[id] = nil
            self.boardNotices[id] = Self.deleteUnsettledNotice
        }
    }

    /// The card half of the rule below: the board the Mac serves *next* is
    /// the proof the delete landed, and a card it no longer lists is gone.
    /// A board with `available == false` has no cards, so this clears on it
    /// too — correct, the card is not listed. Notices for cards the Mac has
    /// since dropped go with them.
    private func pruneSettlingCards() {
        let listed = Set(snapshot.board.cards.map(\.id))
        let done = settlingCards.subtracting(listed)
        for id in done {
            settlingCardTimeouts[id]?.cancel()
            settlingCardTimeouts[id] = nil
        }
        settlingCards.subtract(done)
        for key in boardNotices.keys where !listed.contains(key) {
            boardNotices[key] = nil
        }
    }

    /// A pipeline dial the Mac accepted: keep showing the value asked for
    /// until a board reports it. `beginSettlingCard`'s shape, and called by
    /// the pipeline screen rather than by `post` — the two verbs carry no
    /// card and no session, so `post`'s own scope rules cannot name them.
    ///
    /// `want` is the wire value: `"on"` / `"off"` for the switch, the
    /// limit's digits for a dial.
    func beginSettlingPreference(_ key: String, want: String) {
        guard !key.isEmpty else { return }
        settlingPreferences[key] = want
        pipelineNotices[key] = nil
        settlingPreferenceTimeouts[key]?.cancel()
        settlingPreferenceTimeouts[key] = Task { [weak self] in
            let nanos = UInt64(Self.settlingTimeout * 1_000_000_000)
            // The press is queued, not awaited: the backstop counts from
            // the moment the press stops being in play, not from the tap.
            repeat {
                try? await Task.sleep(nanoseconds: nanos)
                if Task.isCancelled { return }
            } while self?.queueMark(for: key) != nil
            guard let self, self.settlingPreferences[key] != nil else { return }
            // A press the Mac refused was never applied: the dial goes back
            // to the board's value with the Mac's words under it
            // (`queueNote`), not the phone's "accepted, but…" sentence.
            if self.queueNote(for: key) != nil {
                self.endSettlingPreference(key)
                return
            }
            self.settlingPreferences[key] = nil
            self.settlingPreferenceTimeouts[key] = nil
            self.pipelineNotices[key] = Self.preferenceUnsettledNotice
        }
    }

    /// The Mac refused a dial press in words: the held value ends at once,
    /// with no notice of the phone's own — the queue note carries the
    /// Mac's. Called by the sender the moment a queued preference press
    /// closes refused, and by the backstop as the belt.
    private func endSettlingPreference(_ key: String) {
        guard settlingPreferences[key] != nil else { return }
        settlingPreferenceTimeouts[key]?.cancel()
        settlingPreferenceTimeouts[key] = nil
        settlingPreferences[key] = nil
        pipelineNotices[key] = nil
    }

    /// The Mac's own refusal about one pipeline control, shown on that
    /// control. Written from the screen rather than inside `post`, because
    /// these two verbs carry neither a session nor a card — `post`'s own
    /// scope rules cannot name them — and cleared by the next press.
    func notePipeline(_ key: String, _ text: String) {
        guard !key.isEmpty else { return }
        pipelineNotices[key] = text
    }

    /// The preference half of the rule below: the board the Mac serves
    /// *next* is the proof the change landed, and a board already reporting
    /// the asked-for value ends the hold. The **board**, never the status
    /// code — the app stores and republishes on its own thread well after
    /// the 200.
    private func prunePreferenceSettling() {
        guard !settlingPreferences.isEmpty else { return }
        let board = snapshot.board
        for (key, want) in settlingPreferences {
            let landed: Bool
            if key == Self.autostartSettlingKey {
                landed = (board.autostartEnabled ? "on" : "off") == want
            } else if key.hasPrefix("parallel:") {
                let root = String(key.dropFirst("parallel:".count))
                let now = board.parallelOverrides[root] ?? 0
                landed = String(now) == want
            } else {
                landed = false
            }
            guard landed else { continue }
            settlingPreferenceTimeouts[key]?.cancel()
            settlingPreferenceTimeouts[key] = nil
            settlingPreferences[key] = nil
            pipelineNotices[key] = nil
        }
    }

    /// Cleared by the snapshot, never by the HTTP 200 —
    /// `Triage.pruneStopping`'s reasoning verbatim, at poll cadence: the
    /// hold ends when the Mac stops listing the session, which is the only
    /// proof the work actually stopped.
    private func pruneSettling() {
        let a = snapshot.agents
        let stillLive = Set((a.waiting + a.running + a.sleeping)
            .map { $0.sessionId })
        let done = settling.subtracting(stillLive)
        guard !done.isEmpty else { return }
        settling.subtract(done)
        for id in done {
            settlingTimeouts[id]?.cancel()
            settlingTimeouts[id] = nil
        }
    }

    /// The answer half of the rule above, at the same cadence. It reads
    /// `waiting` alone — never `running` or `sleeping` — because an
    /// answered session stays live and `pruneSettling`'s test would never
    /// fire: the proof the answer landed is the row leaving `waiting`, or
    /// standing on a different question than the one it was answered on.
    private func pruneSettlingAnswers() {
        guard !settlingAnswers.isEmpty else { return }
        var current: [String: String] = [:]
        for agent in snapshot.agents.waiting {
            current[agent.sessionId] = agent.question.id
        }
        let done = settlingAnswers.filter { sid, qid in
            current[sid] != qid
        }.keys
        for sid in done {
            settlingAnswers[sid] = nil
            settlingAnswerTimeouts[sid]?.cancel()
            settlingAnswerTimeouts[sid] = nil
        }
    }

    /// Drop dialog picks for questions the Mac no longer shows — the same
    /// snapshot-driven cadence as `pruneSettling`. The keys kept are exactly
    /// the ones a live `AnswerBox` could be reading: each listed agent's
    /// current flat question id, which is what the box keys on.
    private func pruneAnswerDrafts() {
        let a = snapshot.agents
        var keys = Set<String>()
        for agent in a.waiting + a.running + a.sleeping {
            keys.insert(AnswerDrafts.key(sessionId: agent.sessionId,
                                         questionId: agent.question.id))
        }
        answerDrafts.prune(keeping: keys)
    }

    /// The Mac said where it is. Anything this record does not already hold
    /// goes to the pairing store, and the next check-in walks **every**
    /// address rather than only the one on file — the newly learned one is at
    /// the tail of the list, and waiting a full `homeRediscoveryInterval` to
    /// try it is a minute of AWAY the phone no longer has to serve.
    ///
    /// The comparison is against `record.hosts` — the list this client was
    /// started with — so learning is idempotent: the restart `learnHosts`
    /// causes hands the next poll a record that already holds them.
    private func noteOfferedHosts(_ offered: [String], record: PairingRecord) {
        guard !offered.isEmpty else { return }
        let known = Set(record.hosts)
        guard offered.contains(where: {
            let host = $0.trimmingCharacters(in: .whitespacesAndNewlines)
            return !host.isEmpty && !known.contains(host)
        }) else { return }
        lastFullDirectWalk = .distantPast
        onHosts?(offered)
    }

    /// The addresses to try, in order: the one on file, then whatever else
    /// the Mac offered when it was paired. `post` and `poll` walk the same
    /// list, and so do the two methods below — a VPN flip must not make one
    /// route work while another fails.
    private func hostCandidates(_ record: PairingRecord) -> [String] {
        var candidates = [record.host]
        for host in record.hosts where host != record.host && !host.isEmpty {
            candidates.append(host)
        }
        return candidates
    }

    /// One staged attachment, sealed, to `POST /api/upload` — a header
    /// frame naming the file and the blob as the body (`HomeChannel.upload`).
    /// Answers with the **stored** relative path in `detail` — post-dedupe,
    /// which is what the card must record; the name the phone asked for is
    /// not it.
    ///
    /// Its own request rather than a ride on `post`: this is not JSON and not
    /// an action, and a photo over kitchen Wi-Fi outlives `post`'s 8s.
    func upload(stagingId: String, name: String, data: Data) async -> PhoneActionResult {
        guard let record, let home = homeChannel else {
            return PhoneActionResult(ok: false, detail: "no longer paired")
        }
        // Photo upload is deliberately LAN-only — `/api/upload` is not on
        // `REMOTE_ACTIONS` and never will be. Away, say so at once rather
        // than after 60 + 30 × (n−1) seconds of knocking on silent
        // addresses to arrive at the same sentence.
        if knowsItIsAway {
            return PhoneActionResult(ok: false,
                                     detail: PhoneActions.photosNeedHome)
        }
        var attempted = false
        for (index, host) in hostCandidates(record).enumerated() {
            guard case .ok = PhoneActions.uploadURL(host: host, port: record.port)
            else { continue }
            attempted = true
            guard let answer = await home.upload(
                stagingId: stagingId, name: name, data: data, host: host,
                port: record.port,
                timeout: index == 0 ? 60 : Self.directProbe) else { continue }
            if answer.failure == RelayChannel.Trouble.cutShort {
                return PhoneActionResult(ok: false, detail: "")
            }
            if answer.misdirected { continue }
            if index > 0 { onPromote?(host) }
            if answer.refusedAtTheDoor, answer.status == 403 {
                forgetPairing()
                return PhoneActionResult(ok: false, detail: "no longer paired")
            }
            guard answer.failure.isEmpty else {
                return PhoneActionResult(ok: false, detail: answer.failure)
            }
            if answer.status == 404 {
                return PhoneActionResult(ok: false, detail: PhoneActions.olderMac)
            }
            let obj = (try? JSONSerialization.jsonObject(with: answer.body)) as? [String: Any]
            if answer.status == 200, let path = obj?["path"] as? String, !path.isEmpty {
                return PhoneActionResult(ok: true, detail: path)
            }
            let detail = (obj?["detail"] as? String) ?? (obj?["error"] as? String) ?? ""
            return PhoneActionResult(
                ok: false,
                detail: detail.isEmpty ? "The Mac refused that file." : detail)
        }
        // Photo upload is deliberately LAN-only — no relay rung here, and
        // the refusal says why in one sentence rather than failing quietly.
        if channel != nil {
            return PhoneActionResult(ok: false,
                                     detail: PhoneActions.photosNeedHome)
        }
        let detail = attempted ? "Could not reach the Mac." : "Bad address."
        if status != .unpaired {
            status = .unreachable
            lastError = detail
        }
        return PhoneActionResult(ok: false, detail: detail)
    }

    /// Ask the Mac to write the card's instructions. Its own request because
    /// `prepare_card_text` spawns a helper that may take 60s, or 120s with
    /// attachments — `timeoutInterval = 135` matches the desktop panel's own
    /// prepare request exactly, and `post`'s 8s would orphan every one of them.
    /// It does not retry: a second press is refused in the daemon's own words.
    func prepareCard(title: String, summary: String, tool: String,
                     project: String, root: String,
                     attachments: String,
                     idea: String = "") async -> PhonePrepareResult {
        guard let record else {
            return PhonePrepareResult(ok: false, detail: "no longer paired",
                                      prompt: "", workflow: "")
        }
        let body: [String: String] = [
            "action": PhoneActions.prepareCard,
            "title": title, "summary": summary, "tool": tool,
            "project": project, "root": root, "attachments": attachments,
            // Additive: an older Mac ignores the key and answers as today.
            // The same body rides the relay leg and every direct candidate.
            "idea": idea,
            // The one-time mark the Mac's receipt ledger files this press
            // under, so a reply lost on the relay can be asked for again as
            // *this* press rather than paid for twice. Minted here and not
            // through `receipts.open`: this verb's whole point is the body,
            // which a resend through `post` would throw away, and a draft
            // nobody is waiting for is not a pending press to list. An
            // older Mac ignores the key, as `post`'s does.
            "command_token": Self.prepareMark(),
        ]
        // Face ID or the passcode, asked once for whichever relay leg runs —
        // the away-first one or the home fallback. One prompt site, so the
        // count of faces this file asks for is exactly `post`'s and this one.
        func face() async -> Bool { await RemoteAuth.shared.authorize() }
        let faceRefusal = PhonePrepareResult(
            ok: false,
            detail: ReceiptLedger.faceNeededLine,
            prompt: "", workflow: "")

        // Away: the relay first, so the Face ID prompt appears at the press
        // rather than after 135 + 30 × (n−1) seconds of silent home
        // addresses. That walk is what "PREPARE waits endlessly" was.
        if knowsItIsAway, let channel {
            guard await face() else { return faceRefusal }
            let answer = await channel.request(kind: "action", body: body,
                                               timeout: Self.prepareRelayTimeout)
            return await recoverPrepare(answer, body: body, over: channel)
        }

        var attempted = false
        if let home = homeChannel {
            for (index, host) in hostCandidates(record).enumerated() {
                guard case .ok = PhoneActions.homeURL(host: host, port: record.port)
                else { continue }
                attempted = true
                guard let answer = await home.request(
                    kind: "action", body: body, host: host, port: record.port,
                    timeout: index == 0 ? 135 : Self.directProbe) else { continue }
                if answer.failure == RelayChannel.Trouble.cutShort {
                    return PhonePrepareResult(ok: false, detail: "",
                                              prompt: "", workflow: "")
                }
                if answer.misdirected { continue }
                if index > 0 { onPromote?(host) }
                if answer.refusedAtTheDoor, answer.status == 403 {
                    forgetPairing()
                    return PhonePrepareResult(ok: false, detail: "no longer paired",
                                              prompt: "", workflow: "")
                }
                guard answer.failure.isEmpty else {
                    return PhonePrepareResult(ok: false, detail: answer.failure,
                                              prompt: "", workflow: "")
                }
                if answer.status == 404 {
                    return PhonePrepareResult(ok: false, detail: PhoneActions.olderMac,
                                              prompt: "", workflow: "")
                }
                return PhonePrepareResult.parse(data: answer.body,
                                                code: answer.status)
            }
        }
        // Prepare is a chosen LAN action, so it rides the relay too — with
        // the deadline the mailbox's own worst pickup needs on top of the
        // helper's patience.
        if let channel {
            guard await face() else { return faceRefusal }
            let answer = await channel.request(kind: "action", body: body,
                                               timeout: Self.prepareRelayTimeout)
            return await recoverPrepare(answer, body: body, over: channel)
        }
        let detail = attempted ? "Could not reach the Mac." : "Bad address."
        if status != .unpaired {
            status = .unreachable
            lastError = detail
        }
        return PhonePrepareResult(ok: false, detail: detail,
                                  prompt: "", workflow: "")
    }

    /// The mark PREPARE rides under — `command_receipts.TOKEN_RE`'s shape,
    /// eight to a hundred of `[A-Za-z0-9._-]`, which a UUID is.
    static func prepareMark() -> String { "prepare-" + UUID().uuidString.lowercased() }

    /// The relay leg's answer, or the draft asked for again. A clean answer
    /// is parsed as before. A missing one — the deadline passed, the relay
    /// refused, the Mac went silent — is not the end: the same body, same
    /// `command_token`, goes again up to `prepareReplayAttempts` times, and
    /// the Mac's ledger answers with the draft it already wrote. A 202 is
    /// the Mac still writing, so that one is asked again too; a 409 is a
    /// real refusal and is shown. Cancellation (the composer went away)
    /// stops the loop between hops, never mid-request.
    private func recoverPrepare(_ first: RelayChannel.Answer,
                                body: [String: String],
                                over channel: RelayChannel) async -> PhonePrepareResult {
        if first.failure.isEmpty, first.status != 202 {
            return PhonePrepareResult.parse(data: first.body, code: first.status)
        }
        var lastFailure = first.failure
        // The last 202's own words, kept so a Mac that was still writing
        // when the attempts ran out is reported as busy, never as silent.
        var stillWriting: PhonePrepareResult? =
            first.failure.isEmpty ? PhonePrepareResult.parse(data: first.body, code: first.status) : nil
        for _ in 0..<Self.prepareReplayAttempts {
            try? await Task.sleep(nanoseconds: UInt64(Self.prepareReplayGap * 1_000_000_000))
            if Task.isCancelled { break }
            let again = await channel.request(kind: "action", body: body,
                                              timeout: Self.prepareReplayTimeout)
            if !again.failure.isEmpty { lastFailure = again.failure; continue }
            let parsed = PhonePrepareResult.parse(data: again.body, code: again.status)
            if again.status == 202 { stillWriting = parsed; continue }
            return parsed
        }
        if let busy = stillWriting, !busy.detail.isEmpty { return busy }
        return PhonePrepareResult(ok: false,
                                  detail: lastFailure.isEmpty ? RelayChannel.Trouble.macSilent : lastFailure,
                                  prompt: "", workflow: "")
    }

    /// One check-in, and never two at once. A pull-to-refresh **joins** the
    /// poll already running rather than starting a second: two polls mean
    /// two waiters on the same `to-phone` mailbox, where a pop is a removal
    /// and the loser of the race has already thrown the winner's answer
    /// away. It also bounds the spinner — a refresh that queued behind a
    /// running poll's relay leg and then ran its own waited both deadlines
    /// end to end, which is what "it refreshes for ever" was.
    private func poll(_ record: PairingRecord, probeHome: Bool = true) async {
        if let running = polling {
            await running.value
            return
        }
        let work = Task { [weak self] in
            guard let self else { return }
            await self.pollOnce(record, probeHome: probeHome)
        }
        pollsStarted += 1
        polling = work
        await work.value
        pollsCompleted += 1
        // A check-in that finished in the seconds after `.background`
        // bumped a counter after `suspend()`'s flush; landing it again
        // keeps the Keychain current if iOS then kills the app.
        if departedAt != nil {
            flushCounters()
            flushHomeCounters()
        }
        // Between check-ins there is no rung to name — but a check-in that
        // `stop()` cancelled finishes late, and must not blank the sentence
        // its replacement just stamped.
        if polling == work {
            notePhase("")
            polling = nil
        }
    }

    private func pollOnce(_ record: PairingRecord, probeHome: Bool = true) async {
        let waking = wakeCheckIn
        wakeCheckIn = false
        // Away, the relay answers and the home addresses do not, so the
        // relay goes first and the home walk becomes a cheap probe behind
        // it — that probe is the whole homecoming mechanism: a 200 from it
        // sets `via = .lan` and the AWAY badge goes out with no relaunch
        // and no re-pairing. Every address is re-tried once a minute in
        // case the Mac's own address changed while this phone was out;
        // between those, only the address on file is probed.
        let candidates = hostCandidates(record)
        if knowsItIsAway, let channel {
            await pollViaRelay(channel, record: record)
            guard probeHome else { return }
            var probes = [record.host]
            if Date().timeIntervalSince(lastFullDirectWalk)
                >= Self.homeRediscoveryInterval {
                lastFullDirectWalk = Date()
                probes = candidates
            }
            notePhase(Phase.homeProbe)
            _ = await pollDirect(record, candidates: probes,
                                 patientFirst: false)
            return
        }
        // At home (and on a cold launch that was not away last time): the
        // address on file first with its patient timeout, then whatever
        // else the Mac offered when it was paired, then the relay. On the
        // check-in a person is waiting on, with a relay to fall back to,
        // only the address on file and only for `wakeProbe`: the socket is
        // coming up meanwhile, and the loop's next poll walks the rest.
        let quick = waking && channel != nil
        if await pollDirect(record, candidates: quick ? [record.host] : candidates,
                            patientFirst: true,
                            firstTimeout: quick ? Self.wakeProbe : nil) {
            // Home answered. Whatever it said — a refusal or an unreadable
            // body leaves `via` unassigned — the line `start()` / `wake()`
            // opened has no work here, so it closes.
            if via == .lan { channel?.socket?.wanted = false }
            return
        }
        // The home walk found nothing. One relay rung before giving up:
        // the same request, sealed, through the mailbox — never plaintext,
        // never with the device header, and never dressed up as home.
        if let channel {
            // The quick probe missed on the check-in a person waits on: the
            // line comes up now, and the request below waits the moment it
            // takes (`RelaySocket.comingUp`) rather than going the mailbox
            // way.
            if quick, departedAt == nil { channel.socket?.wanted = true }
            await pollViaRelay(channel, record: record)
            // A quick probe that missed is not proof of being away: a Mac
            // slow to answer or a radio waking from power save takes longer
            // than `wakeProbe`. The screen is already filled by the relay;
            // walk every home address now at the ordinary probe length, and
            // a 200 sets `via = .lan` before the AWAY badge can settle.
            // Gated on `quick` alone: `wake()` polls with `probeHome: false`,
            // and a wake is exactly where this walk is needed.
            if quick {
                lastFullDirectWalk = Date()
                notePhase(Phase.homeProbe)
                if await pollDirect(record, candidates: candidates,
                                    patientFirst: false), via == .lan {
                    channel.socket?.wanted = false
                }
            }
            return
        }
        // Nothing answered anywhere. Only now is the Mac unreachable.
        let attempted = candidates.contains {
            if case .ok = PhoneActions.homeURL(host: $0, port: record.port) {
                return true
            }
            return false
        }
        if status != .unpaired {
            status = .unreachable
            lastError = attempted ? "Could not reach the Mac." : "Bad address."
        }
    }

    /// What a sealed state read asks for: nothing when this phone holds no
    /// picture (or its picture is older than `fullStateFloor`), and otherwise
    /// the digest of what it is holding, so a quiet Mac may answer "keep what
    /// you have". An older Mac ignores the key and sends the whole thing.
    private func stateRequestBody() -> [String: Any] {
        // `"done": "review"` rides **unconditionally**, including on the full
        // poll that quotes no digest: the Mac builds the board it is about to
        // send *before* fingerprinting it, so a phone that asked on one poll
        // and not the next would quote a digest of a picture it never got.
        // An older Mac ignores the key and sends the whole finished column,
        // which the splice then finds already present and leaves alone.
        // `with_usage` asks the Mac to fold the usage bars into this
        // answer (`_state_with_usage`), so an away check-in is one mailbox
        // trip rather than two in series. An older Mac ignores the key,
        // sends no `usage`, and `pollUsage` / `pollUsageViaRelay` make the
        // second trip as before (`usageRodeState`).
        // The per-section quotes ride only beside `digest`, so the floor
        // bounds them exactly as it bounds the whole one: once a
        // minute the phone asks for everything from scratch.
        guard !heldStateDigest.isEmpty, let last = lastFullState,
              Date().timeIntervalSince(last) < Self.fullStateFloor else {
            return ["done": "review", "with_usage": true]
        }
        return ["digest": heldStateDigest, "done": "review", "with_usage": true]
            .merging(heldSectionDigests.isEmpty ? [:] : ["sections": heldSectionDigests]) { held, _ in held }
    }

    /// Whether the last state answer carried the usage bars — set by
    /// `takeUsage(from:record:)`, read by the two usage legs to skip
    /// their trip. Never persisted.
    private var usageRodeState = false

    /// The bars off a state answer's `usage` key, applied exactly as the
    /// separate `usage` read applies them. Returns whether any rode.
    @discardableResult
    private func takeUsage(from body: Data, record: PairingRecord,
                           frame: StateFrame? = nil) -> Bool {
        // A poll leg hands in the carrier it already read off the main
        // actor (`StateFrame`); the socket's push still reads it here.
        guard record.token == self.record?.token,
              let carried = frame.map({ $0.usage })
                ?? (try? JSONDecoder().decode(StateUsageCarrier.self, from: body)),
              let report = carried.usage else {
            usageRodeState = false
            return false
        }
        usage = report.bars
        usageToken = record.token
        attribution = report.attribution ?? UsageAttribution()
        usageRodeState = true
        return true
    }

    /// The finished column, fetched once and held.
    ///
    /// The board that arrives on the poll carries only the finished cards that
    /// still want a person; this is the rest. Held in memory only — a restart
    /// refetches — and spliced back into every snapshot before anything else
    /// sees it, so `Receipts.landed`, `Outbox.alreadyLanded` and
    /// `CardCacheStore.remember(board:)` keep reading a complete Done column.
    private(set) var heldDoneArchive: [BoardCard] = []
    private var doneArchiveClearStamp = ""
    private var doneArchiveViewStamp = ""
    /// What a screen may say about the finished column while it is short.
    /// Never "there is nothing here": an empty page under a non-zero count is
    /// the one thing this must not draw.
    enum DoneArchiveState { case loading, ready, failed }
    @Published private(set) var doneArchiveState: DoneArchiveState = .loading
    private var doneArchiveInFlight = false
    private var doneArchiveAskedAt: Date?
    /// A bulk review moves the view token once per card. Thirty-eight fetches
    /// for one gesture is what this trailing floor exists to prevent.
    static let doneArchiveCoalesce: TimeInterval = 2

    /// Fold the held finished cards into a card list. The snapshot's own copy
    /// always wins on an id collision — it is the one the Mac decorated
    /// against this frame — and everything else is appended. Idempotent.
    ///
    /// Byte-for-byte the panel's `DoneArchive.splice`; two surfaces, one rule.
    static func spliceDone(_ cards: [BoardCard],
                           archive: [BoardCard]) -> [BoardCard] {
        guard !archive.isEmpty else { return cards }
        var seen = Set(cards.map(\.id))
        var out = cards
        for card in archive where !seen.contains(card.id) {
            seen.insert(card.id)
            out.append(card)
        }
        return out
    }

    /// What a fresh pair of tokens says about the copy being held.
    enum DoneArchiveAction { case keep, refresh, dropAndRefresh }

    /// The panel's `DoneArchive.decide`, spelled the same way. An empty pair
    /// is `.keep`: an older Mac withholds nothing, and dropping a held column
    /// because a field went missing would blank a page for no reason.
    static func decideDone(clear: String, view: String,
                           heldClear: String, heldView: String)
        -> DoneArchiveAction {
        if clear.isEmpty && view.isEmpty { return .keep }
        if heldClear.isEmpty && heldView.isEmpty { return .refresh }
        if clear != heldClear { return .dropAndRefresh }
        if view != heldView { return .refresh }
        return .keep
    }

    /// Splice the held archive into a snapshot, and decide whether to refetch.
    /// Called from `applyState` on **both** its exits, before `snapshot` is
    /// assigned or left standing.
    @discardableResult
    private func mergeDoneArchive(into board: inout Board) -> Bool {
        let before = board.cards.count
        board.cards = Self.spliceDone(board.cards, archive: heldDoneArchive)
        // Whether the assignment is worth making. On the `unchanged` branch
        // the board on screen already holds the spliced cards, so re-assigning
        // it every four seconds would publish a change on exactly the quiet
        // poll the conditional read exists to make free.
        let changed = board.cards.count != before
        switch Self.decideDone(clear: board.doneClearToken,
                               view: board.doneViewToken,
                               heldClear: doneArchiveClearStamp,
                               heldView: doneArchiveViewStamp) {
        case .keep:
            return changed
        case .dropAndRefresh:
            heldDoneArchive = []
            doneArchiveClearStamp = ""
            doneArchiveViewStamp = ""
            doneArchiveState = .loading
            askForDoneArchive()
        case .refresh:
            askForDoneArchive()
        }
        return changed
    }

    private func askForDoneArchive() {
        guard !doneArchiveInFlight, !backgroundRun else { return }
        // The trailing floor, with one exception: a **cold** archive is
        // fetched at once. The floor exists so a bulk review does not become
        // thirty-eight fetches; making the first fill wait two seconds would
        // leave the page saying it is reading when it could already have read.
        let cold = heldDoneArchive.isEmpty && doneArchiveClearStamp.isEmpty
        if !cold, let asked = doneArchiveAskedAt,
           Date().timeIntervalSince(asked) < Self.doneArchiveCoalesce { return }
        doneArchiveAskedAt = Date()
        doneArchiveInFlight = true
        Task { [weak self] in
            await self?.loadDoneArchive()
            self?.doneArchiveInFlight = false
        }
    }

    /// Fetch the finished column and put it on screen.
    ///
    /// `fetchCard`'s ladder exactly — relay first once the last poll went by
    /// relay, home otherwise — and `fetchCard`'s rule about failure: a fetch
    /// that misses leaves whatever is held standing rather than blanking a
    /// page.
    func loadDoneArchive() async {
        guard let record, !backgroundRun else { return }
        let body: [String: Any] = ["offset": 0]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "done", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(
                kind: "done", body: body, host: record.host,
                port: record.port, timeout: 8)
        } else {
            return
        }
        guard record.token == self.record?.token else { return }
        guard let answer, answer.failure.isEmpty, answer.status == 200,
              let page = try? JSONDecoder().decode(
                DoneArchivePage.self, from: answer.body), page.available
        else {
            if heldDoneArchive.isEmpty { doneArchiveState = .failed }
            return
        }
        heldDoneArchive = page.cards
        doneArchiveClearStamp = page.doneClearToken
        doneArchiveViewStamp = page.doneViewToken
        doneArchiveState = .ready
        var board = snapshot.board
        board.cards = Self.spliceDone(board.cards, archive: heldDoneArchive)
        snapshot.board = board
    }

    /// Ask again after a failed fetch — the Retry button's whole action.
    func retryDoneArchive() {
        if heldDoneArchive.isEmpty { doneArchiveState = .loading }
        doneArchiveAskedAt = nil
        askForDoneArchive()
    }

    /// What happened to a `state` 200.
    enum StateOutcome { case applied, unchanged, unreadable }

    /// The one place a state body becomes the screen — both legs route their
    /// 200 through here, so the snapshot is assigned in exactly one place.
    ///
    /// `StateAnswer` is decoded **first**, and that ordering is the whole
    /// defence: `Snapshot` decodes every key tolerantly, so reading it first
    /// would turn an unchanged body into a valid *empty* snapshot and blank
    /// the fleet, the board and the needs-you count on every quiet poll.
    private func applyState(_ body: Data, record: PairingRecord,
                            route: Via, frame: StateFrame? = nil) -> StateOutcome {
        // A picture held past its day goes on the next state, opened or not.
        if imageMemo.count > 0 { imageMemo.prune() }
        // Decoded once and read twice: the `unchanged` marker here, the
        // `sections_unchanged` list after the `Snapshot` decode below.
        // A poll leg hands in `frame`, read off the main actor in the same
        // order (`StateFrame`); the socket's push decodes here.
        let answer = frame?.answer
            ?? (try? JSONDecoder().decode(StateAnswer.self, from: body))
            ?? StateAnswer()
        if answer.unchanged {
            guard answer.stateDigest == heldStateDigest else {
                // Only reachable through a bug on either side. Apply nothing
                // and drop the digest: the next poll asks for the whole
                // picture.
                heldStateDigest = ""
                lastFullState = nil
                return .unchanged
            }
            // The keep branch. `snapshot`, `leaseExpiresAt` and the digest
            // can only change when the digest changes, so all three stand;
            // everything else runs exactly as after a full answer, because
            // an unchanged reply suppresses the state payload and nothing
            // else.
            // The held finished cards go back before anything reads the
            // snapshot: `Receipts.landed`, `Outbox.alreadyLanded` and
            // `CardCacheStore.remember(board:)` all resolve against
            // `snapshot.board.cards`, and a card a person dragged to Done
            // writes no `closed_by` — so it is withheld the instant it
            // lands, and its receipt would never confirm.
            var keptBoard = snapshot.board
            if mergeDoneArchive(into: &keptBoard) {
                snapshot.board = keptBoard
            }
            pruneSettling()
            pruneSettlingAnswers()
            pruneSettlingCards()
            prunePreferenceSettling()
            // A poll went through: the transport is back, and a `.sent`
            // press's backoff ends so a fresh press behind it on the same
            // scope is not held up to `backoffCap` for trouble that is over.
            receipts.releaseHolds()
            receipts.settled(against: snapshot)
            pruneAnswerDrafts()
            status = .live
            lastError = ""
            lastHeard = Date()
            pictureAsOf = Date()
            via = route
            // A check-in that lands while departed (in flight at
            // `.background`, finishing in the seconds before iOS suspends
            // the process) still applies the picture, the digest, the
            // counters and `lastHeard` — but fires no `onLive`: the outbox
            // drain and the receipt replay it kicks ask for Face ID from
            // away, and `LAContext` cannot present behind the lock with
            // nobody in front of the phone. They wait for `wake()`'s own
            // check-in, which runs after the person unlocked.
            if departedAt == nil { onLive?() }
            return .unchanged
        }
        // `frame` present means the picture was already read (nil there is
        // unreadable, never a second try); absent, it is read here.
        guard var decoded = frame.map({ $0.snapshot }) ?? (try? JSONDecoder().decode(
            Snapshot.self, from: body)) else { return .unreadable }
        // A delta answer: the sections the Mac left out are named, never
        // silently absent, and each is taken from `heldWire` — the last
        // answer as it came off the wire, never the screen, whose board has
        // the Done archive spliced in — **before** the assignment below — `decoded` holds an empty default
        // for every one of them, and the board blanking for one frame is
        // what `Receipts.landed`, `Outbox.alreadyLanded` and
        // `CardCacheStore.remember(board:)` would read. A section is kept
        // only on a digest that matches the one this phone quoted; anything
        // else applies nothing (the unchanged branch's mismatch rule) and
        // the next poll asks for the whole picture.
        for name in answer.sectionsUnchanged {
            guard let mine = heldSectionDigests[name], !mine.isEmpty,
                  answer.sectionDigests[name] == mine,
                  heldWire.generatedAt != 0 else {
                heldStateDigest = ""
                heldSectionDigests = [:]
                heldWire = Snapshot()
                lastFullState = nil
                return .unchanged
            }
            _ = decoded.carry(name, from: heldWire)
        }
        // The wire copy is taken before the Done archive is spliced in, so
        // the next delta carries the Mac's own section and not ours.
        heldWire = decoded
        // Before the assignment, for the reason stated on the keep branch.
        var freshBoard = decoded.board
        mergeDoneArchive(into: &freshBoard)
        decoded.board = freshBoard
        snapshot = decoded
        pruneSettling()
        pruneSettlingAnswers()
        pruneSettlingCards()
        prunePreferenceSettling()
        // The transport is back (the keep branch's rule, above).
        receipts.releaseHolds()
        // The honest half: a press is *done* when something a person can see
        // says so, and `stuck` when nothing ever did. `settlingTimeout`
        // still decides only whether a control stops dimming.
        receipts.settled(against: snapshot)
        pruneAnswerDrafts()
        status = .live
        lastError = ""
        lastHeard = Date()
        via = route
        leaseExpiresAt = decoded.devices.leaseExpiresAt(
            deviceId: record.deviceId)
        heldStateDigest = decoded.stateDigest
        heldSectionDigests = answer.sectionDigests
        // A delta is not a whole picture: the floor keeps counting from the
        // last whole one, so a minute of deltas still ends in a full read.
        if answer.sectionsUnchanged.isEmpty {
            lastFullState = Date()
        }
        pictureAsOf = Date()
        noteOfferedHosts(decoded.devices.hosts, record: record)
        // A banner about a row the desk has dealt with is a stale alert;
        // the Mac's own count is the reading that retires it.
        if !backgroundRun {
            PushRegistrar.shared.clearDeliveredIfQuiet(
                needsYou: snapshot.needsYouCount)
            // The Lock Screen card follows the same list: started, updated
            // or ended here, and handed to the Mac to carry on with the
            // app closed. Foreground only, `Activity.request`'s own rule.
            LiveActivityController.shared.reconcile(snapshot: snapshot,
                                                    client: self)
            // The bytes just decoded are the picture a cold launch will
            // draw. Not under a background run: `.completeFileProtection`
            // files are unwritable with the screen locked.
            // A delta answer is never remembered: the held picture is
            // always a whole one.
            if answer.sectionsUnchanged.isEmpty {
                heldPicture.remember(body: body, digest: decoded.stateDigest,
                                     token: record.token)
            }
        }
        // Departed: no `onLive` — the keep branch's reason, above.
        if departedAt == nil { onLive?() }
        return .applied
    }

    /// Draw the held picture, once, on a launch that has decoded nothing
    /// yet. The second permitted assignment of `snapshot`, and deliberately
    /// nothing else: no `status`, no `lastHeard`, no `heldStateDigest` (the
    /// first poll asks for the whole picture), no `onLive`, no receipts, no
    /// widget — a restored picture is not evidence of anything.
    private func restoreHeldPicture(_ token: String) {
        guard snapshot.generatedAt == 0,
              let (body, savedAt) = heldPicture.restore(for: token),
              let held = try? JSONDecoder().decode(Snapshot.self, from: body),
              held.generatedAt != 0 else { return }
        snapshot = held
        pictureAsOf = savedAt
    }

    /// The home walk, on the addresses handed in. True when one of them gave
    /// an HTTP answer at all — whatever the verdict was — which is also when
    /// it has already applied the snapshot, set `via = .lan`, promoted the
    /// winning address and fetched the usage bars. False means nothing
    /// connected, and the caller decides what that is worth.
    ///
    /// `patientFirst` is the home rule: the address on file gets the full 8s
    /// there, because it is the one that ought to answer. Away it is just
    /// another probe, and `Self.directProbe` is what a silent host may cost.
    private func pollDirect(_ record: PairingRecord, candidates: [String],
                            patientFirst: Bool,
                            firstTimeout: TimeInterval? = nil) async -> Bool {
        guard let home = homeChannel else { return false }
        for (index, host) in candidates.enumerated() {
            guard case .ok = PhoneActions.homeURL(host: host, port: record.port)
            else { continue }
            // The home walk says which address it is on; the away probe
            // behind a relay answer was already worded by `pollOnce`.
            if patientFirst {
                notePhase(index == 0
                    ? Phase.homeOnFile
                    : Phase.homeOther(index + 1, of: candidates.count))
            }
            // The address on file keeps the patient timeout; the probes
            // behind it are short, since the whole walk runs inside one poll.
            let timeout: TimeInterval = (patientFirst && index == 0)
                ? (firstTimeout ?? 8) : Self.directProbe
            // nil is a transport error: nothing there. Only a failure to
            // *connect* moves the walk on — everything below is this Mac
            // answering.
            guard let answer = await home.request(
                kind: "state", body: stateRequestBody(), host: host,
                port: record.port, timeout: timeout) else { continue }
            // Read off the main actor, straight after the transport's own
            // await (`StateFrame`): a whole picture parsed here stalled the
            // screen mid-scroll.
            let frame = await StateFrame.offMain(answer.body)
            // The pairing is read again after both awaits, as the relay
            // path does: an unpair or re-pair while this answer was on the
            // wire or being read must not be drawn over.
            guard record.token == self.record?.token else { return true }
            if answer.failure == RelayChannel.Trouble.cutShort { return true }
            // One exception first: a 421 is the Mac saying this address is
            // a tunnel it does not serve — skip it, keep the pairing, and
            // never promote it over the address that works.
            if answer.misdirected { continue }
            // Reached is reached: any answer is the Mac, so the walk stops
            // here and the address is worth keeping. Treating a 403 as "try
            // the next one" would draw a real unpairing as unreachability
            // and knock on every address every poll.
            if index > 0 { onPromote?(host) }
            if answer.refusedAtTheDoor, answer.status == 403 {
                forgetPairing()
                return true
            }
            // A sealed refusal (a skewed clock, a replay the Mac could not
            // fast-forward) is worded, not acted on: the pairing stands.
            guard answer.failure.isEmpty else {
                status = .unreachable
                lastError = answer.failure
                return true
            }
            guard answer.status == 200 else {
                status = .unreachable
                lastError = answer.detail.isEmpty
                    ? "The Mac refused (HTTP \(answer.status))."
                    : answer.detail
                return true
            }
            guard applyState(answer.body, record: record,
                             route: .lan, frame: frame) != .unreadable else {
                status = .unreachable
                lastError = "The Mac sent something this app could not read."
                return true
            }
            if !takeUsage(from: answer.body, record: record, frame: frame) {
                await pollUsage(host: host, record: record, timeout: timeout)
            }
            // After both the snapshot and the usage landed (or the usage
            // kept its last bars) — the tile's note is one check-in, whole.
            publishWidgetSummary()
            await fetchLog(host: host)
            await fetchConversation(host: host)
            return true
        }
        return false
    }

    /// One state poll through the mailbox. Every failure is worded —
    /// "Could not reach the relay.", "An envelope failed verification." —
    /// and a failed poll never leaves a stale screen drawn live.
    ///
    /// `deadline` is the whole check-in's, not one leg's: `nil` is the
    /// foreground, where each leg simply gets the channel's 45s: and a date
    /// is `backgroundRefresh`'s budget, from which each leg takes what is
    /// **left at the moment it starts** rather than a slice reserved up
    /// front. A fixed slice is how the state leg came to be handed less
    /// time than the Mac's own idle wake-up, so it was abandoned every run
    /// — and an abandoned relay leg is not free. True when a snapshot
    /// landed.
    @discardableResult
    private func pollViaRelay(_ channel: RelayChannel,
                              record: PairingRecord,
                              deadline: Date? = nil) async -> Bool {
        let stateLeg = Self.relayLeg(deadline)
        guard stateLeg > 0 else { return false }
        notePhase(Phase.relayAsking)
        let answer = await channel.request(kind: "state",
                                           body: stateRequestBody(),
                                           timeout: stateLeg)
        // Off the main actor, and before the pairing check below, so the
        // check still reads the moment the answer is applied (`StateFrame`).
        let frame = await StateFrame.offMain(answer.body)
        guard record.token == self.record?.token else { return false }
        guard answer.failure.isEmpty else {
            if status != .unpaired {
                status = .unreachable
                lastError = answer.failure
            }
            return false
        }
        guard answer.status == 200,
              applyState(answer.body, record: record,
                         route: .relay, frame: frame) != .unreadable else {
            status = .unreachable
            lastError = answer.detail.isEmpty
                ? "The Mac sent something this app could not read."
                : answer.detail
            return false
        }
        // The bars are the leg worth dropping when a budget runs thin: a
        // miss keeps the last ones, and the tile's counts have landed.
        let usageLeg = Self.relayLeg(deadline)
        if !takeUsage(from: answer.body, record: record, frame: frame),
           usageLeg >= Self.usageLegFloor {
            await pollUsageViaRelay(channel, record: record, timeout: usageLeg)
        }
        // The relay path feeds the tile too: away from home is exactly when
        // the home-screen numbers are the ones being read.
        publishWidgetSummary()
        await fetchLog()
        await fetchTerminalBytes()
        await fetchConversation()
        return true
    }

    /// What one relay leg may spend: everything left before `deadline`,
    /// never more than the channel's own default. `nil` is the foreground,
    /// which has no budget to divide.
    private static func relayLeg(_ deadline: Date?) -> TimeInterval {
        guard let deadline else { return relayLegCap }
        return min(relayLegCap, deadline.timeIntervalSinceNow)
    }
    private static let relayLegCap: TimeInterval = 45
    /// Under this, the bars are not worth the round trip.
    private static let usageLegFloor: TimeInterval = 4

    /// The usage report, same rules as `pollUsage`: a miss keeps the last
    /// bars and never marks the Mac unreachable.
    private func pollUsageViaRelay(_ channel: RelayChannel,
                                   record: PairingRecord,
                                   timeout: TimeInterval = 45) async {
        let answer = await channel.request(kind: "usage", body: ["query": ""],
                                           timeout: timeout)
        guard record.token == self.record?.token else { return }
        guard answer.failure.isEmpty, answer.status == 200 else { return }
        guard let report = try? JSONDecoder().decode(
            UsageReport.self, from: answer.body) else { return }
        usage = report.bars
        usageToken = record.token
        attribution = report.attribution ?? UsageAttribution()
    }

    /// Same host, key and timeout as the state 200 that just landed — a
    /// sealed `usage` frame, never a POST. 404 / 5xx / timeout / decode fail
    /// keep the last bars. Only 403 un-pairs.
    private func pollUsage(host: String, record: PairingRecord,
                           timeout: TimeInterval) async {
        guard let home = homeChannel else { return }
        guard let answer = await home.request(
            kind: "usage", body: ["query": ""], host: host, port: record.port,
            timeout: timeout) else { return }
        // The walk that started this fetch may have been replaced by a
        // new pairing while we awaited. Do not publish Mac A's bars
        // under Mac B's token.
        guard record.token == self.record?.token else { return }
        if answer.refusedAtTheDoor, answer.status == 403 {
            forgetPairing()
            return
        }
        guard answer.failure.isEmpty, answer.status == 200 else { return }
        guard let report = try? JSONDecoder().decode(
            UsageReport.self, from: answer.body) else { return }
        usage = report.bars
        usageToken = record.token
        attribution = report.attribution ?? UsageAttribution()
    }

    /// How often the diary is asked for behind the regular poll; the away
    /// poll is every 8 s and one mailbox round trip per fetch is the cost.
    static let logInterval: TimeInterval = 30
    static let logRetention: TimeInterval = 86400
    static let logCap = 500

    /// The diary, on the leg the last state poll used — relay when the phone
    /// knows it is away, else the home address just answered (`host`), else
    /// the one on file. `since=` the newest entry held keeps the delta small;
    /// the page is merged by id, sorted newest first and pruned. Only a 403
    /// un-pairs, exactly as `pollUsage` does; every other failure keeps the
    /// last list. Never called from `backgroundRefresh`.
    func fetchLog(force: Bool = false, host: String? = nil) async {
        guard let record, !backgroundRun else { return }
        guard force || Date().timeIntervalSince(lastLogFetch) >= Self.logInterval
        else { return }
        lastLogFetch = Date()
        let since = log.first?.ts ?? 0
        let body: [String: Any] = ["query": since > 0 ? "since=\(since)" : ""]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "log", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(
                kind: "log", body: body, host: host ?? record.host,
                port: record.port, timeout: 8)
        } else {
            return
        }
        guard record.token == self.record?.token else { return }
        guard let answer else { return }
        if answer.status == 403 {
            forgetPairing()
            return
        }
        guard answer.failure.isEmpty, answer.status == 200,
              let page = try? JSONDecoder().decode(LogPage.self, from: answer.body)
        else { return }
        mergeLog(page.events)
    }

    /// The agent screen saying which Dark Army-owned terminal it is showing, or
    /// none. A change resets the away feed's cursor, so the first frame for
    /// a new session is a whole paint under its own name.
    func watchTerminal(_ sessionId: String?) {
        let next = (sessionId?.isEmpty ?? true) ? nil : sessionId
        guard next != terminalWatching else { return }
        terminalWatching = next
        terminalCursor = -1
    }

    /// The conversation screen saying which session it is showing, or none.
    /// The cache holds the cursor; this only names the watched id.
    func watchConversation(_ sessionId: String?) {
        let next = (sessionId?.isEmpty ?? true) ? nil : sessionId
        guard next != conversationWatching else { return }
        conversationWatching = next
    }

    /// The same, from a screen that may no longer be the watched one: a
    /// sheet closing over another agent's screen, whose `onDisappear` can
    /// land after the screen underneath re-asserted its own watch.
    func watchConversation(_ sessionId: String?, ifWatching own: String) {
        guard conversationWatching == own else { return }
        watchConversation(sessionId)
    }

    /// One page of the watched conversation, home or away. Returns the
    /// page's `more`. A 403 un-pairs; a 404 latches `conversationUnsupported`.
    @discardableResult
    func fetchConversation(host: String? = nil) async -> Bool {
        guard let record, !backgroundRun, !conversationUnsupported,
              let sid = conversationWatching, !sid.isEmpty else { return false }
        let since = conversationCache.nextSeq(sid)
        let key = conversationCache.key(sid)
        var body: [String: Any] = [:]
        // A helper's key (`<session>#<agent>`) adds `agent`; a session's
        // query is unchanged (`ConversationSubject.query`).
        body["query"] = ConversationSubject.query(key: sid, since: since, cursorKey: key)
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "conversation", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(
                kind: "conversation", body: body, host: host ?? record.host,
                port: record.port, timeout: 8)
        } else {
            return false
        }
        guard record.token == self.record?.token else { return false }
        guard let answer else { return false }
        if answer.status == 403 {
            forgetPairing()
            return false
        }
        if answer.status == 404 {
            // Two Macs say 404 here. One that has never heard of the
            // `conversation` kind — the snapshot carries no
            // `conversation_supported` — is an older Mac, and the latch
            // is right. One that carries the marker knows the kind and is
            // saying it is not watching *this* session (finished,
            // restarted, evicted): that is this session's own reason,
            // never a reason to stop asking for every other agent until
            // the next pairing. Latching here on a per-session 404 blanked
            // the Conversation screen for the whole fleet.
            if !snapshot.board.conversationSupported {
                conversationUnsupported = true
                return false
            }
            let reason = answer.detail.isEmpty
                ? "Dark Army is not watching that session" : answer.detail
            if conversationUnavailable[sid] != reason {
                conversationUnavailable[sid] = reason
            }
            return false
        }
        guard answer.failure.isEmpty, answer.status == 200,
              let page = try? JSONDecoder().decode(ConversationPage.self,
                                                   from: answer.body)
        else { return false }
        guard conversationWatching == sid else { return false }
        if page.available {
            // Written only on a change: a check-in that brought nothing
            // must not publish a screen of thousands of rows.
            if conversationUnavailable[sid] != nil { conversationUnavailable[sid] = nil }
            conversationCache.apply(page, session: sid)
        } else if conversationUnavailable[sid] != page.reason {
            conversationUnavailable[sid] = page.reason
        }
        return page.more
    }

    /// Page until `more` is false, bounded by the hop caps, stopping when
    /// the watch moves. Tail-first: after a page (it carries `total`), a
    /// present further off than the hops left can reach makes the cursor
    /// jump to the last page (`ConversationCatchUp.jump`). Whatever stops
    /// the loop, the cache writes what it holds once, not per page.
    func catchUpConversation(_ sessionId: String) async {
        let hops = knowsItIsAway ? Self.awayCatchUpHops : Self.homeCatchUpHops
        for hop in 1...hops {
            guard conversationWatching == sessionId else { break }
            let more = await fetchConversation()
            if !more { break }
            if let held = conversationCache.conversations[sessionId],
               let jump = ConversationCatchUp.jump(
                   nextSeq: held.nextSeq, total: held.total, hopsLeft: hops - hop) {
                conversationCache.skipAhead(to: jump, session: sessionId)
            }
        }
        conversationCache.flush()
    }

    /// The pane asking for a fresh paint on the next check-in — a
    /// re-attach away (the scene came back, or the route changed).
    func restartTerminalFeed() {
        terminalCursor = -1
    }

    /// The away pane's own grid, quoted on every check-in so the Mac sizes
    /// the pty to the phone's screen (`terminal_phone_resize`, which yields
    /// to the Mac's own pane). Nil until the pane has laid out; the poll
    /// then asks with no size and the Mac leaves the width alone. Set by
    /// the pane alone, and only while it polls — at home the size rides
    /// the stream's own frame.
    var terminalPhoneSize: (cols: Int, rows: Int)?

    /// The held-open home stream for a hosted session, or nil where this
    /// phone has no sealed home channel. `HomeTransport.swift` seals the
    /// opening frame and builds the address; this only asks.
    func terminalStream(session: String) -> SealedTerminalStream? {
        guard let record, let home = homeChannel else { return nil }
        return home.openTerminalStream(session: session, host: record.host,
                                       port: record.port)
    }

    /// The stream came back a plain 403: not paired here, the same verdict
    /// every other route draws from that code.
    func noteTerminalStreamRefused() {
        forgetPairing()
    }

    /// The away feed: the bytes the watched terminal printed since the
    /// cursor, on the relay leg only — at home the pane holds its own
    /// stream. `fetchLog`'s shape, and for `fetchLog`'s reason: a **read**
    /// on the sealed door, so it widens no action tuple and checks no away
    /// lease. `grid=0` because the phone feeds an emulator and wants no
    /// rows; **one hop per poll, never a `more` loop** — a cursor that
    /// falls behind self-heals into a paint on the Mac's side. A 404 is an
    /// older Mac that has never heard of the kind: latched, so it is asked
    /// once.
    func fetchTerminalBytes() async {
        guard let record, !backgroundRun, !terminalUnsupported, knowsItIsAway,
              let sessionId = terminalWatching, onTerminalBytes != nil,
              snapshot.board.terminalStreamSupported, let channel else { return }
        var query = "session=\(sessionId)&grid=0&since_bytes=\(terminalCursor)"
        // The phone's own screen, so the Mac lays the output out for it
        // rather than for the Mac's window — a `painted` frame comes back
        // when the size took, and the emulator starts over at that width.
        if let size = terminalPhoneSize, size.cols > 0, size.rows > 0 {
            query += "&cols=\(size.cols)&rows=\(size.rows)"
        }
        let body: [String: Any] = ["query": query]
        let answer = await channel.request(kind: "terminal", body: body,
                                           timeout: Self.relayLegCap)
        guard record.token == self.record?.token else { return }
        if answer.status == 403 {
            forgetPairing()
            return
        }
        if answer.status == 404 { terminalUnsupported = true }
        guard answer.failure.isEmpty, answer.status == 200,
              let frame = try? JSONDecoder().decode(TerminalBytesFrame.self,
                                                    from: answer.body)
        else { return }
        guard terminalWatching == sessionId else { return }
        if frame.available, frame.bytesRead >= 0 { terminalCursor = frame.bytesRead }
        onTerminalBytes?(frame)
    }

    /// One card, in full, with the plan document it points at.
    ///
    /// `fetchLog`'s shape exactly, and for `fetchLog`'s reason: a read on the
    /// sealed doors, so it widens no action tuple and checks no away lease.
    /// Called from the card screen's own `.task` and from the agent sheet's
    /// journey rail (`loadJourney`, only when its card has moved on since
    /// the held report) and from **nowhere else** — never from `poll`, never from
    /// `backgroundRefresh` — because a screen nobody is looking at has no
    /// card to read.
    ///
    /// A 404 is an older Mac that has never heard of the `card` kind: nil,
    /// and not an error. The card screen falls back to the snapshot's preview
    /// and holds Save.
    func fetchCard(_ cardId: String) async -> CardFull? {
        guard let record, !backgroundRun, !cardId.isEmpty else { return nil }
        let body: [String: Any] = ["query": "card=\(cardId)"]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "card", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(
                kind: "card", body: body, host: record.host,
                port: record.port, timeout: 8)
        } else {
            return nil
        }
        guard record.token == self.record?.token else { return nil }
        guard let answer else { return nil }
        if answer.status == 403 {
            forgetPairing()
            return nil
        }
        guard answer.failure.isEmpty, answer.status == 200,
              let page = try? JSONDecoder().decode(CardFull.self, from: answer.body)
        else { return nil }
        return page
    }

    /// Many cards in full, plus the plan documents that moved — the delta
    /// read `CardCacheStore.sync` is the only caller of.
    ///
    /// `fetchLog`'s shape exactly, and for `fetchLog`'s reason: a read on the
    /// sealed doors, so it widens no action tuple and checks no away lease.
    /// **Never from `poll` and never under `backgroundRun`** — a 24 s
    /// background budget must not be spent filling a cache nobody is
    /// looking at.
    ///
    /// A 404 is an older Mac that has never heard of the `card_sync` kind:
    /// nil, and not an error. The cache simply stays exactly as it is.
    func fetchCardSync(ids: [String], plans: [String: String]) async -> CardSyncPage? {
        guard let record, !backgroundRun else { return nil }
        guard !ids.isEmpty || !plans.isEmpty else { return nil }
        var query = ""
        if !ids.isEmpty { query += "ids=\(ids.joined(separator: ","))" }
        if !plans.isEmpty {
            if !query.isEmpty { query += "&" }
            let stamps = plans.map { "\($0.key):\($0.value)" }.sorted()
            query += "plans=\(stamps.joined(separator: ","))"
        }
        let body: [String: Any] = ["query": query]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "card_sync", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(
                kind: "card_sync", body: body, host: record.host,
                port: record.port, timeout: 8)
        } else {
            return nil
        }
        guard record.token == self.record?.token else { return nil }
        guard let answer else { return nil }
        if answer.status == 403 {
            forgetPairing()
            return nil
        }
        // A 404 is a Mac that has never heard of this kind, and it will keep
        // saying so. Remember it: nothing lands in the cache, so every later
        // pass would find the whole board stale and ask again, for ever.
        if answer.status == 404 { cardSyncUnsupported = true }
        guard answer.failure.isEmpty, answer.status == 200,
              let page = try? JSONDecoder().decode(CardSyncPage.self,
                                                   from: answer.body)
        else { return nil }
        return page
    }

    /// **The single ordered sender.** Every queued press and every press
    /// still in `sent` goes from here, in creation order, one scope's
    /// presses one behind the other (`ReceiptLedger.nextSendable`), each
    /// under its original token — which is what the Mac's own ledger and,
    /// for `board_create`, `create_token` make safe. Receipts in `accepted`
    /// are never re-sent: the Mac already answered, and what they wait on
    /// is the effect. Kicked by a press (`enqueue`) and by the same `onLive`
    /// the outbox sweep rides — that second trigger is the retry tick for a
    /// held-off receipt, at poll cadence, with no new timer.
    func flushReceipts() async {
        guard let record, !backgroundRun else { return }
        if senderRunning {
            senderKicked = true
            return
        }
        senderRunning = true
        defer { senderRunning = false }
        receipts.rebase(to: record.token)
        // Presses that have run out of tries stop being sent and say so,
        // rather than being replayed on every poll until somebody notices.
        for spent in receipts.exhausted(pairingToken: record.token) {
            receipts.gaveUp(spent.id)
        }
        repeat {
            senderKicked = false
            // A scope whose synchronous press (`post`'s own lock: a Save)
            // is out waits this sweep: no attempt spent, no backoff doubled.
            var holding = Set<String>()
            while let receipt = ReceiptLedger.nextSendable(
                in: receipts.receipts, pairingToken: record.token,
                now: Date().timeIntervalSince1970, holding: holding) {
                guard self.record?.token == record.token else { return }
                if writesInFlight[receipt.scope] != nil {
                    // Taken on the next `onLive`; never a spin on the lock.
                    holding.insert(receipt.scope)
                    continue
                }
                // **Never resend what you can check.** The Mac's own ledger is
                // memory-only and TTL'd (`RECEIPT_SECONDS`), so past that window
                // — or across a daemon restart — a replay would *execute again*.
                // `OutboxStore.alreadyLanded`'s check, one rung on.
                if ReceiptLedger.evidenceBeforeSending(receipt.effect,
                                                       in: snapshot) {
                    receipts.remove(receipt.id)
                    continue
                }
                // And what cannot be checked is bounded by the clock instead: a
                // `.none` press past the Mac's own replay window would not be
                // deduped there, so re-sending it is a second message pushed
                // into a session. A `.queued` press is stamped at the tap, so a
                // queue that sat fifteen minutes drops its un-checkable presses
                // the same way a relaunch does. The answer verbs keep the rule
                // with their hold: a dialog that moved on proves nothing.
                let past = Date().timeIntervalSince1970 - receipt.createdAt
                    > ReceiptLedger.replayWindow
                if past, receipt.effect == .none
                    || ReceiptLedger.heldWhileLanding.contains(receipt.action) {
                    receipts.gaveUp(receipt.id, why: ReceiptLedger.tooOldLine)
                    continue
                }
                // A face pass covers its press for the same window: past it
                // the record closes STUCK on `faceNeededLine` (fails closed,
                // no sheet here) and RETRY, which drops the pass, asks.
                if receipt.authorised, past {
                    receipts.gaveUp(receipt.id, why: ReceiptLedger.faceNeededLine)
                    continue
                }
                // Counts the attempt; `post` closes the record — `accept`,
                // `refuse(surfaced: false)` or `holdOff` — exactly as a
                // replay always has, so a queued press's refusal is `.stuck`
                // carrying the Mac's words, which the row reads as REFUSED.
                receipts.markSending(receipt.id)
                _ = await post(action: receipt.action, fields: receipt.fields,
                               scope: receipt.scope, token: receipt.id,
                               refreshAfter: false,
                               authorised: receipt.authorised)
                // `post` answered without touching the record — its own
                // per-scope lock was held by a synchronous press, or the
                // pairing went — so the transmit never happened: back to
                // `sent`, behind the backoff, for the next tick.
                let after = receipts.receipts.first(where: { $0.id == receipt.id })?.state
                if after == .sending {
                    receipts.holdOff(receipt.id)
                }
                // A dial press the Mac refused: the held value ends now,
                // and the Mac's words are the note, not the backstop's.
                if after == .stuck || after == .refused {
                    endSettlingPreference(receipt.scope)
                }
            }
        } while senderKicked
    }

    /// A person's RETRY on the profile screen: a fresh token, the backoff
    /// reset, and one send now.
    func retryReceipt(_ id: String) async {
        receipts.retry(id)
        await flushReceipts()
    }

    func fetchCatchUp(query: String) async -> CatchUpPage? {
        await catchUpRequests.run { [weak self] in
            await self?.fetchCatchUpPage(query: query)
        }
    }

    private func fetchCatchUpPage(query: String) async -> CatchUpPage? {
        guard let record, !backgroundRun else { return nil }
        let generation = historyGeneration
        var answer: RelayChannel.Answer?
        let body: [String: Any] = ["query": query]
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "catch_up", body: body, timeout: Self.relayLegCap)
        } else {
            if let home = homeChannel {
                for (index, host) in hostCandidates(record).enumerated() {
                    guard !Task.isCancelled, historyGeneration == generation else { return nil }
                    answer = await home.request(kind: "catch_up", body: body, host: host, port: record.port,
                                                timeout: index == 0 ? 8 : Self.directProbe)
                    if answer != nil { break }
                }
            }
            if answer == nil, !Task.isCancelled, historyGeneration == generation, let channel {
                answer = await channel.request(kind: "catch_up", body: body, timeout: Self.relayLegCap)
            }
        }
        guard !Task.isCancelled, historyGeneration == generation, record.token == self.record?.token,
              let answer else { return nil }
        if answer.status == 403 { forgetPairing(); return nil }
        if answer.status == 404 {
            var page = CatchUpPage()
            page.reason = "This Mac needs an update before decision history can be opened."
            return page
        }
        guard answer.failure.isEmpty, answer.status == 200 else { return nil }
        let page = try? JSONDecoder().decode(CatchUpPage.self, from: answer.body)
        // A receipt's page names the buzz in the Mac's own words: fill the
        // log entry's kind, title and ids where the payload left them empty.
        let receiptPrefix = "receipt_id="
        if let page, query.hasPrefix(receiptPrefix) {
            notificationLog.enrich(receiptId: String(query.dropFirst(receiptPrefix.count)), page: page)
        }
        return page
    }

    /// Drain what the push delegate and the tray reads banked before the
    /// gate into the protected log, under the pairing handed in. Called from
    /// the unlock block after `PhoneRouter.pair(token:)` with that same
    /// token — the Keychain record's, not `self.record`'s, which a cold
    /// launch has not set yet at that point; with no pairing the banked
    /// entries stay banked.
    func absorbBankedNotifications(token: String) {
        guard !token.isEmpty else { return }
        notificationLog.absorb(PushRegistrar.shared.takeBanked(), token: token)
    }

    /// The same drain from a bank made while the gate is already open — a
    /// buzz that arrived on screen (`willPresent`) or one the Mac's quiet
    /// word cleared. Refused until `start(record:)` has run (the record is
    /// set there and nowhere else, behind the unlock) **and** the log has
    /// been read, so the file is never written before the process's first
    /// unlock and a bank never lands over the entries the file already
    /// holds. (After a re-lock — `.inactive`, or a `.background` inside
    /// `BackgroundGrace` — the record and the poller survive, so a bank
    /// made with the Face ID sheet up does write: the footing
    /// `heldPicture.remember` already stands on.)
    func absorbBankedNotificationsIfOpen() {
        guard let record, notificationLog.loaded else { return }
        absorbBankedNotifications(token: record.token)
    }

    private func mergeLog(_ fresh: [LogEntry]) {
        var byId: [String: LogEntry] = [:]
        for entry in log where !entry.id.isEmpty { byId[entry.id] = entry }
        for entry in fresh where !entry.id.isEmpty { byId[entry.id] = entry }
        let floor = Date().timeIntervalSince1970 - Self.logRetention
        log = Array(byId.values
            .filter { $0.ts >= floor }
            .sorted { $0.ts > $1.ts }
            .prefix(Self.logCap))
    }

    /// The explicit un-pair press, from the profile screen. The 403 path
    /// below is the *involuntary* one and does the same work; this is the
    /// deliberate one, and both have to drop the two pairing-scoped stores
    /// or a re-pair against another Mac reads its cards and replays its
    /// presses. Public because `ContentView.forget()` is outside this class.
    func forgetPairedState() {
        forgetPairing()
    }

    /// Un-pair: the fleet *and* the bars. A 403 that blanks only the
    /// snapshot leaves Mac A's percentages on the Usage tab.
    private func forgetPairing() {
        catchUpRequests.cancel()
        historyGeneration += 1
        PhoneRouter.shared.forget()
        status = .unpaired
        lastError = "no longer paired"
        snapshot = Snapshot()
        // The digest outlives nothing: a stale one is never dangerous (it
        // simply fails to match), but one outliving its Mac is confusing.
        heldStateDigest = ""
        heldSectionDigests = [:]
        heldWire = Snapshot()
        lastFullState = nil
        log = []
        lastLogFetch = .distantPast
        answerDrafts.prune(keeping: [])
        // Pictures belong to the Mac they came from.
        imageMemo = ImageMemo()
        // Both new stores are pairing-scoped and go with it: a cache filled
        // from another Mac must never be read as this one's, and a press
        // made against it must never be replayed here.
        cardCache.forget()
        conversationCache.forget()
        receipts.forget()
        heldPicture.forget()
        notificationLog.forget()
        // The buzzes banked in memory and not yet filed go with the log:
        // left there, the next unlock would file the old Mac's under the
        // new pairing's token.
        PushRegistrar.shared.forgetBanked()
        linkTiming.forget()
        pictureAsOf = nil
        // No pairing, no Lock Screen card (the deliberate path unregistered).
        LiveActivityController.shared.endAll(unregistering: false)
        // No pairing, no window, no reminder about one.
        leaseExpiresAt = 0
        forgetUsage()
    }

    /// Drop bars that belong to another pairing. Same-token `start` after
    /// a background `stop` keeps them, so the tab does not flash empty.
    private func forgetUsage() {
        usage = []
        attribution = UsageAttribution()
        usageToken = ""
    }

    /// One check-in with nobody watching — `BackgroundRefresh`'s whole use
    /// of this class. No timer, no outbox, and the whole thing inside
    /// `budget` seconds: the direction is whichever way the last poll went
    /// (`lastKnownAwayKey`), so an away phone asks the relay first and a
    /// home phone probes two addresses and only then the relay. Every leg
    /// that lands runs `publishWidgetSummary` on the way through, which is
    /// the point. True when a snapshot landed by any route.
    func backgroundRefresh(record: PairingRecord, budget: TimeInterval) async -> Bool {
        stop()
        backgroundRun = true
        defer { backgroundRun = false }
        self.record = record
        homeChannel = buildHomeChannel(record)
        homeChannel?.onCounters = { [weak self, token = record.token] send, recv in
            self?.noteHomeCounters(token: token, send: send, recv: recv)
        }
        channel = buildChannel(record)
        channel?.onCounters = { [weak self, token = record.token] send, recv in
            self?.noteCounters(token: token, send: send, recv: recv)
        }
        defer { stop() }
        let away = UserDefaults.standard.bool(forKey: Self.lastKnownAwayKey)
        // One deadline for the whole run, and every leg reads the clock
        // itself. The Mac's connector backs off to a 30-second gap once a
        // channel has been quiet for ten minutes — and a background floor of
        // fifteen minutes means this run always meets the quiet one — so the
        // state leg is handed everything the budget has bar one home probe.
        // It may still time out; that is a tile keeping last time's numbers,
        // which is the design, and the abandoned request now stops rather
        // than outliving the run.
        let deadline = Date().addingTimeInterval(budget)
        if away, let channel {
            if await pollViaRelay(
                channel, record: record,
                deadline: deadline.addingTimeInterval(-Self.directProbe)) {
                return true
            }
            return await pollDirect(record, candidates: [record.host],
                                    patientFirst: false) && status == .live
        }
        let probes = Array(hostCandidates(record).prefix(2))
        if await pollDirect(record, candidates: probes, patientFirst: false) {
            return status == .live
        }
        guard let channel else { return false }
        return await pollViaRelay(channel, record: record, deadline: deadline)
    }

    /// Who the tile draws, in the order the daemon published them.
    ///
    /// **Waiting alone, never waiting-then-working**: with one blocked agent
    /// among five busy ones a merged list would show the person who needs you
    /// for one hour in six, which is the opposite of what the tile is for. So
    /// the buckets are a ladder, not a concatenation — and where neither has
    /// anybody, the whole cast stands by, which is the one place a face with
    /// no session behind it is the honest thing to draw.
    ///
    /// No sort here: the daemon's order is the order, the same doctrine the
    /// bars follow. Identity is `Cast.character(for:)` — the app's one table
    /// — because the widget target compiles no cast list and must not grow a
    /// fourth copy of the roster.
    private func widgetFaces() -> [FleetSummary.Face] {
        func faces(_ agents: [Agent], doing: String,
                   bucket: String) -> [FleetSummary.Face] {
            let rule = Cast.state(forBucket: bucket).rawValue
            return agents.map { agent in
                FleetSummary.Face(
                    slug: Cast.character(for: agent),
                    name: String(agent.nickname.prefix(FleetSummary.maxNameChars)),
                    doing: doing,
                    rule: rule,
                    sessionId: agent.sessionId)
            }
        }
        if !snapshot.agents.waiting.isEmpty {
            return Array(faces(snapshot.agents.waiting, doing: "needs you",
                               bucket: "waiting")
                .prefix(FleetSummary.maxFaces))
        }
        if !snapshot.agents.running.isEmpty {
            return Array(faces(snapshot.agents.running, doing: "working",
                               bucket: "running")
                .prefix(FleetSummary.maxFaces))
        }
        // Nothing running: the cast stands by. `Cast.artOnly` is included
        // **here and only here** — `overwatch` is art-only precisely because no
        // session wears it, so it can never arrive through the two branches
        // above, and the planner's face is the right one for a quiet machine.
        let idle = Cast.names.map { $0.lowercased() } + Cast.artOnly
        return Array(idle.map { slug in
            FleetSummary.Face(slug: slug,
                              name: String(slug.prefix(1).uppercased()
                                           + slug.dropFirst()),
                              doing: "standing by",
                              rule: Cast.State.sleep.rawValue)
        }.prefix(FleetSummary.maxFaces))
    }

    /// The home-screen tile's note: written after every check-in (LAN and
    /// relay alike), read by the widget's provider, which does no network.
    /// Counts are the daemon's own — `counts.attention` / `counts.working`
    /// straight off the snapshot, to-do as Prep + Backlog each read on its
    /// own (the menu bar's recipe; one junk value must not zero the other).
    /// Bars reduce to one per provider: that provider's **headline window**,
    /// which is the first one the daemon lists for it.
    private func publishWidgetSummary() {
        // One bar per provider, and it is a *chosen window* rather than a
        // chosen number: the first bar the daemon lists for that provider.
        // `limits.cached_bars` orders every provider's block "session first,
        // then the weekly windows", and `UsageView` already leans on that
        // same order, so the head of the block is Claude's 5h, Codex's 5h
        // and Grok's week — the figures the menu-bar strip draws.
        //
        // Picking the *highest percent* instead was the reported bug. It is
        // not a window at all: the tile hopped between the 5h and the weekly
        // meter as the two numbers crossed, so it settled on whichever moves
        // least and sat there looking frozen while the app's own 5h figure
        // climbed all day. It also let a fresh long window stand in for a
        // stale short one — Codex's 7d shown in place of its 5h — which is a
        // different account of a different span, published as if it were the
        // same meter.
        //
        // A stale headline stays stale, drawn as the widget's "–". That is
        // `menu_format.limit_percent`'s doctrine and the reason substitution
        // is wrong here: stale means the window this figure measured has
        // already reset, so the number is not old but *wrong*, and no other
        // window is an answer to the question it was asking.
        var order: [String] = []
        var headline: [String: UsageBar] = [:]
        for bar in usage where headline[bar.provider] == nil {
            headline[bar.provider] = bar
            order.append(bar.provider)
        }
        let bars = order.compactMap { provider -> FleetSummary.Bar? in
            guard let bar = headline[provider] else { return nil }
            // No reading is the same admission as a reset window: a missing
            // percent must never be published as a confident 0%.
            return FleetSummary.Bar(
                provider: provider,
                // The Usage tab's own shortener, not a second rule: a tile
                // row is narrower than a tab row, and "Current week (Fable)"
                // was being published where "7d·Fable" belongs.
                label: UsageChipText.label(kind: bar.kind,
                                           shortLabel: bar.shortLabel,
                                           title: bar.title),
                percent: bar.percent ?? 0,
                stale: bar.stale || bar.percent == nil)
        }
        let counts = snapshot.board.counts
        let summary = FleetSummary(
            needsYou: snapshot.counts.attention,
            working: snapshot.counts.working,
            todo: (counts["prep"] ?? 0) + (counts["backlog"] ?? 0),
            generatedAt: Date().timeIntervalSince1970,
            dimAfter: BackgroundRefresh.dimAfterSeconds,
            bars: bars,
            faces: widgetFaces())
        let writeError = summary.store()
        widgetWriteReport = writeError.isEmpty
            ? "ok \(Int(summary.generatedAt))" : writeError
        loadReloadSlots()
        let changed = lastReloadFigures.map { $0.figures != summary.figures }
            ?? true
        let since = Date().timeIntervalSince(lastWidgetReload)
        let due = changed ? Self.reloadMinSeconds
                          : Self.reloadFreshnessSeconds
        guard since >= due else { return }
        // With nobody in front of the app the reload counts against
        // WidgetKit's daily budget (`WidgetReloadBudget`): it is asked for
        // only when it would change the tile, and only while the ledger
        // has room. The foreground's reloads are exempt and unchanged.
        let counted = backgroundRun || departedAt != nil
        if counted {
            guard WidgetReloadBudget.worthIt(
                changed: changed,
                noteAge: lastReloadFigures.map {
                    Date().timeIntervalSince1970 - $0.generatedAt
                },
                dimAfter: summary.dimAfter) else { return }
            guard spendCountedReload() else { return }
        }
        recordReload(of: summary)
        WidgetCenter.shared.reloadTimelines(ofKind: "BobFleetTile")
    }

    /// The last reload's note and time, read once per process from
    /// `UserDefaults`, where every reload of any kind records them.
    private func loadReloadSlots() {
        guard !reloadSlotsLoaded else { return }
        reloadSlotsLoaded = true
        let defaults = UserDefaults.standard
        let at = defaults.double(forKey: WidgetReloadBudget.lastReloadAtKey)
        guard at > 0 else { return }
        lastWidgetReload = max(lastWidgetReload,
                               Date(timeIntervalSince1970: at))
        if lastReloadFigures == nil,
           let data = defaults.data(forKey: WidgetReloadBudget.lastReloadNoteKey),
           let note = try? JSONDecoder().decode(FleetSummary.self, from: data) {
            lastReloadFigures = note
        }
    }

    private func recordReload(of summary: FleetSummary) {
        lastReloadFigures = summary
        lastWidgetReload = Date()
        let defaults = UserDefaults.standard
        defaults.set(lastWidgetReload.timeIntervalSince1970,
                     forKey: WidgetReloadBudget.lastReloadAtKey)
        if let data = try? JSONEncoder().encode(summary) {
            defaults.set(data, forKey: WidgetReloadBudget.lastReloadNoteKey)
        }
    }

    /// One counted reload off the ledger, or false when the window is
    /// spent.
    private func spendCountedReload() -> Bool {
        let defaults = UserDefaults.standard
        let stamps = defaults.array(forKey: WidgetReloadBudget.stampsKey)
            as? [Double] ?? []
        let now = Date()
        guard WidgetReloadBudget.allows(stamps: stamps, now: now) else {
            return false
        }
        defaults.set(WidgetReloadBudget.spend(stamps: stamps, now: now),
                     forKey: WidgetReloadBudget.stampsKey)
        return true
    }

    /// The app is leaving (`.background`). One unthrottled reload after the
    /// final summary publish, so the widget's timeline — and its
    /// +10-minute dim entry — restarts from the last note written rather
    /// than from whenever the throttle last let a reload through.
    func flushWidgetReload() {
        // Never heard from a Mac this session: nothing new on disk, and a
        // reload would only re-draw whatever an earlier session left.
        guard snapshot.generatedAt != 0 else { return }
        // A departure's reload counts against WidgetKit's budget, so it is
        // asked for only when the tile is behind — the throttle held back
        // a changed note, or the note the tile holds is about to dim —
        // and only while the ledger has room; the tile's dim clock counts
        // from its note's own `generatedAt`, so an up-to-date tile needs
        // nothing here.
        loadReloadSlots()
        guard let note = FleetSummary.load() else { return }
        let changed = lastReloadFigures.map { $0.figures != note.figures }
            ?? true
        guard WidgetReloadBudget.worthIt(
            changed: changed,
            noteAge: lastReloadFigures.map {
                Date().timeIntervalSince1970 - $0.generatedAt
            },
            dimAfter: note.dimAfter) else { return }
        guard spendCountedReload() else { return }
        recordReload(of: note)
        WidgetCenter.shared.reloadTimelines(ofKind: "BobFleetTile")
    }
}

extension PhoneClient {
    /// What Dark Army observed one card's last run do — the whole record, or one
    /// file's changes.
    ///
    /// `outcomeReport`'s body: relay leg when the phone already knows it is
    /// away, else the home channel, guarded on `record.token` and never run
    /// from a background check-in. A **read** on the sealed route, so no
    /// lease is spent and nothing is written.
    ///
    /// A 404 means **this Mac's Dark Army is too old to keep work records** and is
    /// returned as that state (`.unsupported`) rather than as "no record" —
    /// an empty record would tell somebody the run changed nothing.
    enum WorkRecordAnswer {
        case ok(WorkRecordReport)
        case unsupported
        case unreachable
    }

    func workRecord(cardId: String, file: Int? = nil) async -> WorkRecordAnswer {
        guard let record, !backgroundRun else { return .unreachable }
        let value = cardId.addingPercentEncoding(
            withAllowedCharacters: .alphanumerics) ?? ""
        var query = "card=\(value)"
        if let file { query += "&file=\(file)" }
        let body: [String: Any] = ["query": query]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "work_record", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "work_record", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return .unreachable }
        guard self.record?.token == record.token, let answer,
              answer.failure.isEmpty else { return .unreachable }
        if answer.status == 404 { return .unsupported }
        guard answer.status == 200,
              let report = try? JSONDecoder().decode(WorkRecordReport.self,
                                                     from: answer.body)
        else { return .unreachable }
        return .ok(report)
    }

    /// One file's changes, by its index in the record's own list. The phone
    /// never supplies a path.
    func workRecordDiff(cardId: String, file: Int) async -> WorkRecordDiff? {
        guard let record, !backgroundRun else { return nil }
        let value = cardId.addingPercentEncoding(
            withAllowedCharacters: .alphanumerics) ?? ""
        let body: [String: Any] = ["query": "card=\(value)&file=\(file)"]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "work_record", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "work_record", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard self.record?.token == record.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(WorkRecordDiff.self, from: answer.body)
    }

    /// Read-only report on the existing sealed home/away route. No new action.
    func outcomeReport(cardId: String? = nil, root: String? = nil, offset: Int = 0) async -> OutcomeReport? {
        guard let record, !backgroundRun else { return nil }
        let value = (cardId ?? root ?? "").addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
        let body: [String: Any] = ["query": "\(cardId == nil ? "root" : "card")=\(value)&offset=\(offset)"]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "outcomes", body: body, timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "outcomes", body: body, host: record.host,
                                        port: record.port, timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(OutcomeReport.self, from: answer.body)
    }

    /// One enrolled project's notes. `log`'s rule: a **read** on the existing
    /// sealed home/away route. `root` in the JSON body, never a query string.
    func knowledgeReport(root: String) async -> KnowledgeReport? {
        guard let record, !backgroundRun else { return nil }
        let body: [String: Any] = ["root": root]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "knowledge", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "knowledge", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(KnowledgeReport.self, from: answer.body)
    }

    /// The Mac's Checks section — every enrolled project's manual checks,
    /// open first then newest first — on `knowledgeReport(root:)`'s shape:
    /// a sealed **read** on both doors, relay first when away, never on the
    /// poll or `backgroundRefresh`. Asked when the screen appears and after
    /// a press. `q` and `status` ride the JSON body.
    func manualChecks(root: String = "", query: String = "",
                      status: String = "") async -> ManualChecksReport? {
        guard let record, !backgroundRun else { return nil }
        var body: [String: Any] = [:]
        if !root.isEmpty { body["root"] = root }
        if !query.isEmpty { body["q"] = query }
        if !status.isEmpty { body["status"] = status }
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "manual_checks", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "manual_checks", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(ManualChecksReport.self, from: answer.body)
    }

    /// One check file's text, fetched when it is opened. The path rides the
    /// sealed JSON body, never a query string, and the Mac re-checks it.
    func manualCheckText(path: String) async -> ManualCheckDocument? {
        guard let record, !backgroundRun, !path.isEmpty else { return nil }
        let body: [String: Any] = ["path": path]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "manual_checks", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "manual_checks", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(ManualCheckDocument.self, from: answer.body)
    }

    /// The Mac's scout-report list, newest first, no bodies — on
    /// `knowledgeReport(root:)`'s shape and `log`'s rule: a **read** on
    /// both doors, relay first when away, never on the poll or
    /// `backgroundRefresh`. Asked when the Scouting screen appears — and,
    /// with a `query`, after a pause in its search line, for the reports
    /// whose text holds it (`q` in the sealed body, `manualChecks`' shape;
    /// only against a Mac publishing `scout_reports_body_search_supported`).
    func scoutReportsIndex(query: String = "") async -> ScoutReportIndex? {
        guard let record, !backgroundRun else { return nil }
        var body: [String: Any] = [:]
        if !query.isEmpty { body["q"] = query }
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "scout_reports", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "scout_reports", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(ScoutReportIndex.self, from: answer.body)
    }

    /// One scout report's text, fetched when it is opened and at no other
    /// time. The path rides the sealed JSON body — a path on the relay's
    /// logs is the thing this avoids — and the Mac re-checks it against the
    /// set it lists.
    func scoutReportBody(path: String) async -> ScoutReportBody? {
        guard let record, !backgroundRun, !path.isEmpty else { return nil }
        let body: [String: Any] = ["path": path]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "scout_report", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "scout_report", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(ScoutReportBody.self, from: answer.body)
    }

    /// Every watched project's plans, newest first, no bodies —
    /// `scoutReportsIndex`' shape and rule: a **read** on the sealed
    /// home/away route (relay first when away), asked only when the Plans
    /// screen appears, never on the poll, the background refresh or the
    /// widget.
    func plansIndex() async -> PlanIndex? {
        guard let record, !backgroundRun else { return nil }
        let body: [String: Any] = [:]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "plans", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "plans", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(PlanIndex.self, from: answer.body)
    }

    /// One plan's text, fetched when it is opened and at no other time.
    /// The path rides the sealed JSON body, never a query string, and the
    /// Mac re-checks it against the set it lists.
    func planBody(path: String) async -> PlanBody? {
        guard let record, !backgroundRun, !path.isEmpty else { return nil }
        let body: [String: Any] = ["path": path]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "plan", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "plan", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(PlanBody.self, from: answer.body)
    }

    func cachedImage(session: String, path: String) -> ImagePreview? {
        imageMemo.get(session: session, path: path)
    }

    /// One picture from a session's project, asked when the picture sheet
    /// shows it and at no other time. `planBody`'s route and rule: a
    /// **read** on the sealed home/away route; the session and the path
    /// ride the sealed JSON body, never a query string, and the Mac
    /// confines the path to that session's project.
    func imagePreview(session: String, path: String) async -> ImagePreview? {
        guard let record, !backgroundRun, !session.isEmpty, !path.isEmpty else { return nil }
        let body: [String: Any] = ["session": session, "path": path]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "image", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "image", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 15)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200,
              let preview = try? JSONDecoder().decode(ImagePreview.self, from: answer.body)
        else { return nil }
        imageMemo.put(preview, session: session, path: path)
        return preview
    }

    /// The Mac's access log — every refused knock on the phone doors — on
    /// `knowledgeReport(root:)`'s shape and `log`'s rule: a **read** on the
    /// sealed home/away route, so it adds no action, takes no lease check
    /// and writes no `remote_activity` record on the Mac.
    func accessLogReport(since: Double? = nil, limit: Int = 500) async -> AccessLogReport? {
        guard let record, !backgroundRun else { return nil }
        var query = "limit=\(max(1, limit))"
        if let since, since >= 0 { query += "&since=\(since)" }
        // The Mac's ten-minute link-timing rollups are an opt-in, asked
        // for only where the Mac states the marker: an older Mac would
        // 400 the unknown key and the whole screen would go blank.
        if snapshot.board.linkTimingSupported { query += "&timing=1" }
        let body: [String: Any] = ["query": query]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "access_log", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "access_log", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(AccessLogReport.self, from: answer.body)
    }

    /// The agent efficiency report: what each kind of helper cost, and — for
    /// a named project — whether its work was accepted.
    ///
    /// `outcomeReport`'s shape verbatim, and `log`'s rule: a **read** on the
    /// existing sealed home/away route, so it adds no action, takes no lease
    /// check and writes no `remote_activity` record on the Mac.
    func agentReport(range: String = "30d", root: String = "") async -> AgentReport? {
        guard let record, !backgroundRun else { return nil }
        let encoded = root.addingPercentEncoding(
            withAllowedCharacters: .alphanumerics) ?? ""
        let body: [String: Any] = [
            "query": "range=\(range)" + (encoded.isEmpty ? "" : "&root=\(encoded)"),
        ]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "agent_report", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "agent_report", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(AgentReport.self, from: answer.body)
    }

    /// Sealed `lifecycle` read: no lease, not on either action tuple.
    func lifecycleReport(root: String? = nil, cardId: String? = nil,
                         from: Double, to: Double, sort: String = "queue",
                         offset: Int = 0, generation: String = "") async -> LifecycleReport? {
        guard let record, !backgroundRun else { return nil }
        var parts = ["from=\(from)", "to=\(to)", "sort=\(sort)", "offset=\(offset)", "limit=25"]
        if let cardId, !cardId.isEmpty {
            let value = cardId.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
            parts.append("card=\(value)")
        } else if let root {
            let value = root.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? ""
            parts.append("root=\(value)")
        } else { return nil }
        if !generation.isEmpty {
            parts.append("generation=\(generation.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? generation)")
        }
        let body: [String: Any] = ["query": parts.joined(separator: "&")]
        let answer: RelayChannel.Answer?
        if knowsItIsAway, let channel {
            answer = await channel.request(kind: "lifecycle", body: body,
                                           timeout: Self.relayLegCap)
        } else if let home = homeChannel {
            answer = await home.request(kind: "lifecycle", body: body,
                                        host: record.host, port: record.port,
                                        timeout: 10)
        } else { return nil }
        guard record.token == self.record?.token, let answer,
              answer.failure.isEmpty, answer.status == 200 else { return nil }
        return try? JSONDecoder().decode(LifecycleReport.self, from: answer.body)
    }
}
