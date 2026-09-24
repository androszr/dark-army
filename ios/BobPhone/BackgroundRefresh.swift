import BackgroundTasks
import Foundation

/// The tile's own clock, when nobody opens the app.
///
/// A `BGAppRefreshTask` the app asks iOS for every time it leaves the
/// foreground and again at the end of each run. When it fires — in the
/// background, screen off, app not launched by a person — a fresh
/// `PhoneClient` makes **one** bounded check-in (home Wi-Fi or the relay,
/// whichever the last poll said was in reach), writes the widget summary
/// and reloads the tile. The widget itself still does no network and holds
/// no Keychain: the app is the only process that ever talks to the Mac, and
/// this is just the app talking on a timer.
///
/// Two things the name "every x minutes" does not promise. `earliestBeginDate`
/// is a floor, not a schedule — iOS runs the task when it decides the phone
/// can afford it, which is typically within a few minutes of the floor on a
/// phone that opens Dark Army now and then, and never while Low Power Mode is on
/// or Background App Refresh is off for Dark Army. And the run has ~30 seconds,
/// so the check-in is budgeted at `budgetSeconds` and cancelled at
/// expiry rather than left to finish: a tile that keeps last time's numbers
/// is the design; a task iOS kills for overrunning is a strike against the
/// next one being scheduled at all.
@MainActor
enum BackgroundRefresh {
    /// Must match `BGTaskSchedulerPermittedIdentifiers` in Info.plist, which
    /// is `$(DARK_ARMY_BUNDLE_ID).refresh` — the app's own bundle id plus
    /// `.refresh`, so the id is set once in ios/Config/Identity.xcconfig.
    static let taskId = (Bundle.main.bundleIdentifier ?? "") + ".refresh"
    /// The person's floor between runs, in minutes. Below 15 iOS coalesces
    /// anyway; above 60 the tile's dimming is the more honest signal.
    static let intervals = [15, 30, 60]
    static let defaultMinutes = 15
    private static let minutesKey = "widgetRefreshMinutes"
    /// What one run may spend on the Mac, under the ~30s iOS grants.
    static let budgetSeconds: TimeInterval = 24

    static var minutes: Int {
        get {
            let stored = UserDefaults.standard.integer(forKey: minutesKey)
            return intervals.contains(stored) ? stored : defaultMinutes
        }
        set {
            UserDefaults.standard.set(newValue, forKey: minutesKey)
            schedule()
        }
    }

    /// The tile's dim horizon: the floor plus five minutes of iOS slack, and
    /// never under the ten the tile always had. Written into every summary
    /// so a 30-minute cadence does not draw a tile that dims at ten.
    static var dimAfterSeconds: TimeInterval {
        max(600, TimeInterval(minutes * 60) + 300)
    }

    /// Called before the app finishes launching — the scheduler refuses a
    /// handler registered any later.
    static func register() {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: taskId,
                                        using: nil) { task in
            guard let refresh = task as? BGAppRefreshTask else {
                task.setTaskCompleted(success: false)
                return
            }
            Task { @MainActor in await handle(refresh) }
        }
    }

    /// One pending request at a time; a resubmit replaces the last floor.
    static func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: taskId)
        request.earliestBeginDate = Date().addingTimeInterval(
            TimeInterval(minutes * 60))
        // A refusal here (simulator, restricted device) is not an error the
        // person can act on; the tile simply stays app-driven.
        try? BGTaskScheduler.shared.submit(request)
    }

    private static func handle(_ task: BGAppRefreshTask) async {
        // The next run is booked first, so a run that is killed mid-way
        // still leaves a floor behind it.
        schedule()
        let pairing = PairingStore()
        pairing.load()
        guard let record = pairing.record else {
            task.setTaskCompleted(success: false)
            return
        }
        let client = PhoneClient()
        // The same three hooks `BobPhoneApp` wires, minus `onLive`: the
        // outbox is not opened here, so a queued card is never sent — and
        // never asks for Face ID — from a process with nobody in front of it.
        client.onPromote = { host in pairing.promote(host: host) }
        // And where home moved to: the Mac's live addresses ride the
        // snapshot, which the relay carries too — so a phone that can
        // only hear the relay learns its way back onto the LAN. Additive,
        // and silent when it has nothing new, so this is no loop either.
        client.onHosts = { hosts in pairing.learnHosts(hosts) }
        client.onCounters = { token, send, recv in
            pairing.persistCounters(token: token, send: send, recv: recv)
        }
        client.storedCounters = { pairing.storedCounters() }
        client.onHomeCounters = { token, send, recv in
            pairing.persistHomeCounters(token: token, send: send, recv: recv)
        }
        client.storedHomeCounters = { pairing.storedHomeCounters() }
        let work = Task { await client.backgroundRefresh(record: record,
                                                         budget: budgetSeconds) }
        task.expirationHandler = { work.cancel() }
        let ok = await work.value
        task.setTaskCompleted(success: ok)
    }
}
