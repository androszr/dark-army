import Combine
import Foundation

/// Arm-then-confirm. Separate slots so Start cannot re-aim Delete.
///
/// Setting one slot does not write into another. `arm` clears the rest first
/// so only one verb is waiting; `confirm` is true only when that slot is
/// already armed. Navigating away calls `disarm()`.
@MainActor
final class Arm: ObservableObject {
    /// 30 seconds: long enough to actually read the armed copy — "Really
    /// close the terminal?" plus its warning line — without the arm quietly
    /// lapsing mid-read (8s was shorter than the reading). The expiry itself
    /// stays: a destructive verb armed on a phone in hand must not sit live
    /// indefinitely, and `onDisappear` still disarms on navigation.
    static let timeoutSeconds: TimeInterval = 30

    enum Slot: Equatable {
        case start
        case startHere
        case refine
        case stop
        case deleteCard
        case deleteAgent
        case close
        case permission
        case done
        case lowPriority
        case clearDone
        /// Mark checked / Mark reviewed on the card screen. Their own slots
        /// so Start cannot re-aim an acknowledgement, and vice versa.
        case manualClear
        /// Passed / Failed on a check file — its own slot, so a Mark
        /// checked arm can never confirm an outcome.
        case manualOutcome
        case review
        /// MERGE and Fix on a Done card (`CardMerge`). Their own slots, so
        /// an arm for one can never confirm the other or any verb above.
        case merge
        case mergeFix
        /// End on the Comm tab: closes Mission Control's terminal.
        case missionEnd
        /// The Prep row's batch Refine on the Board tab. Its own slot, so a
        /// Refine armed on a card screen can never confirm a batch, nor the
        /// reverse; armed on the joined id list, so a tick changed between
        /// the two presses re-arms rather than fires.
        case refineBatch
        /// The Backlog row's batch Start on the Board tab. Its own slot, so a
        /// Start armed on a card screen can never confirm a batch, nor the
        /// reverse; armed on the joined id list, so a tick changed between
        /// the two presses re-arms rather than fires.
        case startBatch
        /// Rebuild & restart, on the Menu screen and on an agent's screen. Its
        /// own slot, so nothing else armed can confirm a Mac restart.
        case rebuild
    }

    @Published private(set) var start: String?
    @Published private(set) var startHere: String?
    @Published private(set) var refine: String?
    @Published private(set) var stop: String?
    @Published private(set) var deleteCard: String?
    @Published private(set) var deleteAgent: String?
    @Published private(set) var close: String?
    @Published private(set) var permission: String?
    @Published private(set) var done: String?
    @Published private(set) var lowPriority: String?
    @Published private(set) var clearDone: String?
    @Published private(set) var manualClear: String?
    @Published private(set) var manualOutcome: String?
    @Published private(set) var review: String?
    @Published private(set) var merge: String?
    @Published private(set) var mergeFix: String?
    @Published private(set) var missionEnd: String?
    @Published private(set) var refineBatch: String?
    @Published private(set) var startBatch: String?
    @Published private(set) var rebuild: String?

    /// Card detail hangs plan-gate pending flags here so a timeout cannot
    /// leave `skip_plan_gate` armed for the next Menu tap.
    var onDisarm: (() -> Void)?

    private var timeoutTask: Task<Void, Never>?

    func arm(_ slot: Slot, id: String = "") {
        timeoutTask?.cancel()
        start = nil
        startHere = nil
        refine = nil
        stop = nil
        deleteCard = nil
        deleteAgent = nil
        close = nil
        permission = nil
        done = nil
        lowPriority = nil
        clearDone = nil
        manualClear = nil
        manualOutcome = nil
        review = nil
        merge = nil
        mergeFix = nil
        missionEnd = nil
        refineBatch = nil
        startBatch = nil
        rebuild = nil
        switch slot {
        case .start: start = id
        case .startHere: startHere = id
        case .refine: refine = id
        case .stop: stop = id
        case .deleteCard: deleteCard = id
        case .deleteAgent: deleteAgent = id
        case .close: close = id
        case .permission: permission = id
        case .done: done = id
        case .lowPriority: lowPriority = id
        case .clearDone: clearDone = id
        case .manualClear: manualClear = id
        case .manualOutcome: manualOutcome = id
        case .review: review = id
        case .merge: merge = id
        case .mergeFix: mergeFix = id
        case .missionEnd: missionEnd = id
        case .refineBatch: refineBatch = id
        case .startBatch: startBatch = id
        case .rebuild: rebuild = id
        }
        timeoutTask = Task { [weak self] in
            let nanos = UInt64(Self.timeoutSeconds * 1_000_000_000)
            try? await Task.sleep(nanoseconds: nanos)
            if Task.isCancelled { return }
            self?.disarm()
        }
    }

    /// True only when this slot is already armed **for this id**. Disarms on
    /// success so a third press does not fire again.
    ///
    /// The id is the half that was missing. One `Arm` is drawn across a
    /// *list* — the outbox's waiting cards — and a slot armed on one row
    /// used to confirm the very first press on another: the second row's
    /// button still read `REMOVE`, and that single tap destroyed a card
    /// holding the person's own words, with no undo. A caller with no id in
    /// hand (a detail screen owns one subject and disarms on navigation)
    /// passes nothing and behaves exactly as before.
    func confirm(_ slot: Slot, id: String = "") -> Bool {
        let armed: String?
        switch slot {
        case .start: armed = start
        case .startHere: armed = startHere
        case .refine: armed = refine
        case .stop: armed = stop
        case .deleteCard: armed = deleteCard
        case .deleteAgent: armed = deleteAgent
        case .close: armed = close
        case .permission: armed = permission
        case .done: armed = done
        case .lowPriority: armed = lowPriority
        case .clearDone: armed = clearDone
        case .manualClear: armed = manualClear
        case .manualOutcome: armed = manualOutcome
        case .review: armed = review
        case .merge: armed = merge
        case .mergeFix: armed = mergeFix
        case .missionEnd: armed = missionEnd
        case .refineBatch: armed = refineBatch
        case .startBatch: armed = startBatch
        case .rebuild: armed = rebuild
        }
        guard let armedId = armed, id.isEmpty || armedId == id else {
            return false
        }
        disarm()
        return true
    }

    func disarm() {
        timeoutTask?.cancel()
        timeoutTask = nil
        start = nil
        startHere = nil
        refine = nil
        stop = nil
        deleteCard = nil
        deleteAgent = nil
        close = nil
        permission = nil
        done = nil
        lowPriority = nil
        clearDone = nil
        manualClear = nil
        manualOutcome = nil
        review = nil
        merge = nil
        mergeFix = nil
        missionEnd = nil
        refineBatch = nil
        startBatch = nil
        rebuild = nil
        onDisarm?()
    }
}
