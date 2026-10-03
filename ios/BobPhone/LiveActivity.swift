import ActivityKit
import Foundation
import UIKit

/// The Live Activity's rule: which decision is the live card, and what to do
/// about it. Pure — a function of a `Snapshot` and the card already up — so
/// `LiveActivityRuleTests` can table it without ActivityKit.
///
/// **The subject is the head of the decision list restricted to session
/// entries of the three kinds the card can draw a face for** — a permission
/// ask, a question, an agent waiting on somebody. Card entries (a card whose
/// assistant has gone, a hand-check) carry no face and are skipped; the Mac
/// restates the same rule in `live_activity.py` so the two ends agree on
/// who is at the top. The one accepted drift is stated in
/// `docs/phone-contract.md`: the phone removes acknowledged items and the Mac
/// composer does not read the acks.
enum NeedsYouActivityRule {
    /// The Mac's `relay_client.PUSH_WORK_CHARS`: the card title's clamp,
    /// restated so a locally started card reads exactly what the Mac's
    /// update will say.
    static let workChars = 80
    static let sessionKinds: Set<PhoneInboxWireKind> =
        [.permission, .question, .reviewPicks, .waiting]

    enum Step: Equatable {
        case none
        case start(NeedsYouAttributes.ContentState)
        case update(NeedsYouAttributes.ContentState)
        case end
    }

    /// The card to show for this picture, or nil when nobody needs you.
    static func subject(from snapshot: Snapshot) -> NeedsYouAttributes.ContentState? {
        for item in snapshot.decisionItems {
            // A review run waiting on picks (`r:<run id>`): the Mac's
            // `live_activity.waiters` run candidate, rule for rule.
            if case .review(let runId) = item.target, item.wire == .reviewPicks,
               !runId.isEmpty {
                return reviewSubject(item, runId: runId, in: snapshot)
            }
            guard case .session(let id) = item.target, sessionKinds.contains(item.wire),
                  !id.isEmpty else { continue }
            guard let (agent, _) = PhoneInbox.uniqueAgent(session: id,
                                                          agents: snapshot.agents)
            else { continue }
            return NeedsYouAttributes.ContentState(
                nickname: agent.nickname.isEmpty ? item.title : agent.nickname,
                slug: Cast.character(for: agent),
                kind: kindWord(item.wire),
                work: work(for: agent, in: snapshot),
                since: item.since,
                sessionId: id)
        }
        return nil
    }

    /// The card for a run waiting on picks. Bound to a live row it wears
    /// that agent's nickname, face and work line; unbound (Codex on a hosted
    /// terminal never binds a session) it says the project, draws no cast
    /// face (`slug` empty, never a stranger's) and carries the run id alone.
    /// `since` is the run's findings clock, the Mac's `quiet_since` for it.
    static func reviewSubject(_ item: PhoneInboxItem, runId: String,
                              in snapshot: Snapshot) -> NeedsYouAttributes.ContentState {
        if !item.sessionId.isEmpty,
           let (agent, category) = PhoneInbox.uniqueAgent(session: item.sessionId,
                                                          agents: snapshot.agents),
           PhoneInbox.liveBuckets.contains(category) {
            return NeedsYouAttributes.ContentState(
                nickname: agent.nickname.isEmpty ? item.project : agent.nickname,
                slug: Cast.character(for: agent),
                kind: kindWord(.reviewPicks),
                work: work(for: agent, in: snapshot),
                since: item.since,
                sessionId: item.sessionId,
                runId: runId)
        }
        return NeedsYouAttributes.ContentState(
            nickname: item.project, slug: "", kind: kindWord(.reviewPicks),
            work: "", since: item.since, sessionId: "", runId: runId)
    }

    /// The card is up while anybody is working or waiting. Standing by
    /// alone is not a card — the strip's own sum, not a second count.
    static func isUp(_ counts: Counts) -> Bool {
        counts.working + counts.attention > 0
    }

    /// The fleet card, or today's subject when the Mac has not said it
    /// publishes one. Nil when the fleet is empty (or, against an older
    /// Mac, when nobody is waiting). Figures are the Mac's, verbatim.
    static func fleetState(from snapshot: Snapshot) -> NeedsYouAttributes.ContentState? {
        guard snapshot.board.liveActivityFleet else { return subject(from: snapshot) }
        guard isUp(snapshot.counts) else { return nil }
        var state = subject(from: snapshot) ?? waitingFace(from: snapshot)
            ?? NeedsYouAttributes.ContentState()
        state.working = snapshot.counts.working
        state.needsYou = snapshot.counts.attention
        state.standingBy = snapshot.counts.idle
        state.costUsd = snapshot.fleetFigures.costUsd
        state.tokensK = snapshot.fleetFigures.tokensK
        state.costUsdHour = snapshot.fleetFigures.costUsdHour
        state.tokensKHour = snapshot.fleetFigures.tokensKHour
        return state
    }

    /// The fleet card's fallback face: the head of the waiting bucket as an
    /// "attention" face. "Need you" counts every waiting row, but `subject`
    /// skips a waiter whose entry is a board card (a hand-check due), so the
    /// card read "1 need you" with no portrait. The Mac's
    /// `live_activity.fleet_face` states the same rule; the nickname is the
    /// row's own, as the Mac sends it.
    static func waitingFace(from snapshot: Snapshot) -> NeedsYouAttributes.ContentState? {
        guard let agent = snapshot.agents.waiting.first(where: { !$0.sessionId.isEmpty })
        else { return nil }
        let since: Double
        if agent.quietSince > 0 {
            since = agent.quietSince
        } else {
            since = snapshot.generatedAt > 0 && agent.idleSeconds > 0
                ? snapshot.generatedAt - agent.idleSeconds : 0
        }
        return NeedsYouAttributes.ContentState(
            nickname: agent.nickname,
            slug: Cast.character(for: agent),
            kind: "attention",
            work: work(for: agent, in: snapshot),
            since: since,
            sessionId: agent.sessionId)
    }

    static func kindWord(_ wire: PhoneInboxWireKind) -> String {
        switch wire {
        case .permission: return "permission"
        case .question: return "question"
        case .reviewPicks: return "picks"
        default: return "attention"
        }
    }

    /// The Mac's `_compose_push_work`, restated: the bound card's title
    /// (`session_id` or `refine_session_id`), else the daemon's own
    /// `card_title` line for an unnamed row, else the agent's name unless
    /// the row is unnamed — then nothing, never the placeholder —
    /// whitespace-collapsed and clamped to `workChars` with an ellipsis.
    /// The phone never judges whether a row is unnamed (that string belongs
    /// to the daemon, `test_card_title_on_row.py`); it reads the daemon's
    /// judgment off the row's `unnamed` flag, so the card the phone starts
    /// and the Mac's first update carry the same `work` line.
    static func work(for agent: Agent, in snapshot: Snapshot) -> String {
        let id = agent.sessionId
        var line = ""
        if !id.isEmpty {
            let bound = snapshot.board.cards.first {
                $0.sessionId == id || $0.refineSessionId == id
            }
            line = bound?.title ?? ""
        }
        if line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            if !agent.cardTitle.isEmpty {
                line = agent.cardTitle
            } else if !agent.unnamed {
                line = agent.name
            } else {
                line = ""
            }
        }
        return clamp(line)
    }

    static func clamp(_ text: String) -> String {
        let collapsed = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        guard collapsed.count > workChars else { return collapsed }
        return String(collapsed.prefix(workChars - 1)) + "\u{2026}"
    }

    /// What is actually up, before `plan` runs: the state the controller
    /// remembers, unless ActivityKit says the activity it belonged to is
    /// gone — ended by the Mac's push while the app was alive in the
    /// background, swiped off the Lock Screen by the person, or ended by iOS
    /// itself at its eight-hour cap. Then nothing is up, and the next
    /// subject is a `.start`, never an `.update` of a card nobody can see.
    /// `listed` is whether `Activity.activities` still names it; a state of
    /// `.stale` is still a card on screen.
    static func standing(remembered: NeedsYouAttributes.ContentState?,
                         activityState: ActivityState?,
                         listed: Bool) -> NeedsYouAttributes.ContentState? {
        guard let remembered, listed, let activityState else { return nil }
        switch activityState {
        case .ended, .dismissed: return nil
        default: return remembered
        }
    }

    /// What to do given the card already up and the picture just decoded.
    /// Equal states (the Mac's `live_activity.same`, restated: every key but
    /// `since`, which may drift under two seconds) are no step.
    static func plan(current: NeedsYouAttributes.ContentState?,
                     subject: NeedsYouAttributes.ContentState?) -> Step {
        switch (current, subject) {
        case (nil, nil): return .none
        case (nil, let next?): return .start(next)
        case (_?, nil): return .end
        case (let now?, let next?):
            return same(now, next) ? .none : .update(next)
        }
    }

    static func same(_ a: NeedsYouAttributes.ContentState,
                     _ b: NeedsYouAttributes.ContentState) -> Bool {
        a.nickname == b.nickname && a.slug == b.slug && a.kind == b.kind
            && a.work == b.work && a.sessionId == b.sessionId && a.runId == b.runId
            && a.working == b.working && a.needsYou == b.needsYou
            && a.standingBy == b.standingBy && a.costUsd == b.costUsd
            && a.tokensK == b.tokensK
            && a.costUsdHour == b.costUsdHour && a.tokensKHour == b.tokensKHour
            && abs(a.since - b.since) < 2
    }
}

/// Keeps exactly one `NeedsYouAttributes` activity in step with the decision
/// list while the app is open, and hands the Mac the activity's update token
/// so the card keeps moving once the app is closed.
///
/// Main actor, called from `PhoneClient.applyState` (itself main-actor).
/// ActivityKit's request / update / end are awaited inside a `Task`, so the
/// apply path never blocks. Two activities never: on foreground the app
/// adopts an existing activity (`adoptExisting`) rather than requesting a
/// second, and ends any extras.
///
/// The token rides `PhoneClient.registerActivityToken` — `quietPost`, never a
/// Face ID prompt — **only where the Mac says `live_activity_supported`**:
/// against an older Mac the card still starts and ends locally and nothing
/// is posted. An unchanged token is not re-sent. The remembered key's
/// generation forces one re-registration so an upgraded phone tells the
/// Mac which card it draws.
@MainActor
final class LiveActivityController {
    static let shared = LiveActivityController()

    private static let sentTokenKey = "activity.sentToken.v2"

    /// The registration body: token, env, and `"shape"` so the Mac learns
    /// this phone draws the fleet card. One function, so the send and the
    /// unregister cannot disagree.
    static func registrationFields(token: String, env: String) -> [String: String] {
        ["token": token, "env": env, "shape": "2"]
    }

    /// The activity this controller owns, if any.
    private(set) var activity: Activity<NeedsYouAttributes>?
    /// The state last requested or applied — the `current` side of `plan`.
    private(set) var current: NeedsYouAttributes.ContentState?
    /// Whether the last snapshot said the Mac takes activity tokens.
    private(set) var supported = false
    private var tokenWatcher: Task<Void, Never>?
    private var stateWatcher: Task<Void, Never>?
    private var pendingToken = ""
    private var sending = false
    private weak var client: PhoneClient?

    var isEnabled: Bool {
        ActivityAuthorizationInfo().areActivitiesEnabled
    }

    /// One reconcile per applied snapshot.
    func reconcile(snapshot: Snapshot, client: PhoneClient) {
        self.client = client
        supported = snapshot.board.liveActivitySupported
        guard isEnabled else { return }
        settleStanding()
        let subject = NeedsYouActivityRule.fleetState(from: snapshot)
        switch NeedsYouActivityRule.plan(current: current, subject: subject) {
        case .none:
            break
        case .start(let state):
            // `Activity.request` fails in the background; a background
            // refresh runs a fresh client whose apply path never reaches here,
            // but the guard is cheap and the failure is silent.
            guard UIApplication.shared.applicationState == .active else { return }
            start(state)
        case .update(let state):
            update(state)
        case .end:
            end()
        }
        sendTokenIfDue()
    }

    /// On `.active`: adopt an activity the last run left up rather than
    /// requesting a second one, and end any extras. An activity ActivityKit
    /// reports ended or dismissed is not adopted — its token is dead.
    func adoptExisting() {
        settleStanding()
        let live = Activity<NeedsYouAttributes>.activities.filter {
            $0.activityState != .ended && $0.activityState != .dismissed
        }
        guard activity == nil, let first = live.first else {
            for extra in live where extra.id != activity?.id { endActivity(extra) }
            return
        }
        activity = first
        // The send stamp is not part of what the card says, so the plan
        // compares without it — an adopted card is not "changed" by it.
        var adopted = first.content.state
        adopted.updatedAt = 0
        current = adopted
        watchTokens(of: first)
        for extra in live.dropFirst() { endActivity(extra) }
    }

    /// Drop the card if ActivityKit no longer shows it (`NeedsYouActivityRule
    /// .standing`), so the next plan is drawn against what is on screen and a
    /// new subject starts a card rather than updating a ghost. The dead
    /// token is unregistered, which is what lets the Mac forget the activity
    /// and start clean on the next registration.
    private func settleStanding() {
        guard activity != nil || current != nil else { return }
        let live = Activity<NeedsYouAttributes>.activities
        let listed = activity.map { held in live.contains { $0.id == held.id } } ?? false
        let standing = NeedsYouActivityRule.standing(
            remembered: current, activityState: activity?.activityState, listed: listed)
        if standing == nil { noteExternalEnd(of: activity?.id) }
    }

    /// The activity ended out from under the controller. `id` is the
    /// activity the news is about; an old watcher's word on a replaced
    /// activity must not clear the one now up.
    private func noteExternalEnd(of id: String?) {
        guard activity == nil || activity?.id == id else { return }
        activity = nil
        current = nil
        tokenWatcher?.cancel()
        tokenWatcher = nil
        stateWatcher?.cancel()
        stateWatcher = nil
        unregisterIfSent()
    }

    /// Every activity down. `unregistering` posts an empty token on the
    /// record and channels captured *now*, before the forget path tears
    /// them down — `PushRegistrar.unregister`'s reason.
    func endAll(unregistering: Bool) {
        let teardown = unregistering ? client?.pushTeardown() : nil
        let client = self.client
        tokenWatcher?.cancel()
        tokenWatcher = nil
        stateWatcher?.cancel()
        stateWatcher = nil
        for live in Activity<NeedsYouAttributes>.activities { endActivity(live) }
        activity = nil
        current = nil
        pendingToken = ""
        UserDefaults.standard.removeObject(forKey: Self.sentTokenKey)
        if let teardown, let client, supported {
            Task {
                _ = await client.quietPost(record: teardown.record,
                                           channel: teardown.channel,
                                           home: teardown.home,
                                           action: PhoneActions.registerActivityToken,
                                           fields: ["token": ""])
            }
        }
    }

    // MARK: - ActivityKit

    /// Stamped with the moment it is sent, so the card's "N min ago" counts
    /// from this refresh. `current` keeps the unstamped state the plan
    /// compares against.
    private func content(_ state: NeedsYouAttributes.ContentState)
        -> ActivityContent<NeedsYouAttributes.ContentState> {
        let now = Date()
        var stamped = state
        stamped.updatedAt = now.timeIntervalSince1970
        return ActivityContent(state: stamped,
                               staleDate: now.addingTimeInterval(NeedsYouAttributes.staleAfter))
    }

    private func start(_ state: NeedsYouAttributes.ContentState) {
        current = state
        do {
            let started = try Activity.request(attributes: NeedsYouAttributes(),
                                               content: content(state),
                                               pushType: .token)
            activity = started
            watchTokens(of: started)
        } catch {
            // Denied in Settings, too many activities, or a background
            // request: the list on screen is still the truth.
            current = nil
        }
    }

    private func update(_ state: NeedsYouAttributes.ContentState) {
        current = state
        guard let activity else { return }
        let body = content(state)
        Task { await activity.update(body) }
    }

    private func end() {
        let ending = activity
        activity = nil
        current = nil
        tokenWatcher?.cancel()
        tokenWatcher = nil
        stateWatcher?.cancel()
        stateWatcher = nil
        if let ending { endActivity(ending) }
        // The Mac's end kills the token; say so, so it stops pushing at it.
        unregisterIfSent()
    }

    private func endActivity(_ live: Activity<NeedsYouAttributes>) {
        Task { await live.end(nil, dismissalPolicy: .immediate) }
    }

    // MARK: - the token

    private func watchTokens(of live: Activity<NeedsYouAttributes>) {
        tokenWatcher?.cancel()
        tokenWatcher = Task { [weak self] in
            for await data in live.pushTokenUpdates {
                let hex = data.map { String(format: "%02x", $0) }.joined()
                await MainActor.run { self?.noteToken(hex) }
            }
        }
        // The activity's own word on whether it is still up: a Mac push
        // `end` landing while the app is alive in the background, a swipe
        // off the Lock Screen, the eight-hour cap. Without this the
        // controller kept planning updates for a card nobody could see and
        // the next agent got no card until the process died.
        stateWatcher?.cancel()
        let id = live.id
        stateWatcher = Task { [weak self] in
            for await state in live.activityStateUpdates {
                guard state == .ended || state == .dismissed else { continue }
                await MainActor.run { self?.noteExternalEnd(of: id) }
                break
            }
        }
    }

    private func noteToken(_ hex: String) {
        pendingToken = hex
        sendTokenIfDue()
    }

    private func sendTokenIfDue() {
        guard supported, !pendingToken.isEmpty, !sending, let client else { return }
        let defaults = UserDefaults.standard
        guard pendingToken != (defaults.string(forKey: Self.sentTokenKey) ?? "") else { return }
        let token = pendingToken
        sending = true
        Task { [weak self] in
            guard let self else { return }
            let ok = await client.registerActivityToken(
                fields: Self.registrationFields(token: token, env: PushRegistrar.shared.env))
            self.sending = false
            if ok { defaults.set(token, forKey: Self.sentTokenKey) }
        }
    }

    private func unregisterIfSent() {
        let defaults = UserDefaults.standard
        let sent = defaults.string(forKey: Self.sentTokenKey) ?? ""
        pendingToken = ""
        defaults.removeObject(forKey: Self.sentTokenKey)
        guard !sent.isEmpty, supported, let client else { return }
        Task { _ = await client.registerActivityToken(
            fields: Self.registrationFields(token: "", env: PushRegistrar.shared.env)) }
    }
}
