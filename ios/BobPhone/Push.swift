import SwiftUI
import UIKit
import UserNotifications

/// The APNs half of the buzz: iOS hands the device token to this delegate,
/// the registrar hands it to the Mac, and the Mac's alert drain does the
/// rest. The notification itself carries a name, a count, one short line
/// naming the board card — or, failing that, the session — the agent is
/// working under, and one clamped line saying what is needed (the agent's
/// own summary or question, or a tool's bare name); each line is composed
/// and clamped Mac-side and clamped again relay-side, and nothing else about
/// the alert travels — never the command, a path, the project or the branch.
/// The delegate reads none of those lines; everything the person acts on is
/// fetched over the sealed channel once the app is open.
final class PushDelegate: NSObject, UIApplicationDelegate,
                          UNUserNotificationCenterDelegate {

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions:
            [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        LockScreenActions.register()
        // Before launch finishes, or the scheduler refuses the handler.
        BackgroundRefresh.register()
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { @MainActor in
            PushRegistrar.shared.noteToken(hex)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        // A simulator, or Apple unreachable right now. Nothing to do —
        // registration is re-attempted on the next foreground.
    }

    /// A push arriving while the app is foregrounded is suppressed: the
    /// screen already shows the fleet, and a banner over it would restate
    /// what is on screen with less detail. It is still written down — the
    /// log of buzzes seen is what Catch up reads with the Mac out of reach.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        await PushRegistrar.shared.bank(notification, tapped: false)
        return []
    }

    /// A tap aims the app at the Needs-you tab — through the router's
    /// pending slot, so the Face-ID gate stays in front of everything.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let info = response.notification.request.content.userInfo
        // Open review: a foreground button that writes nothing. Banked like
        // a tap, then the router's slot takes the run behind the gate.
        if let runId = LockScreenActions.opens(actionIdentifier: response.actionIdentifier,
                                               userInfo: info) {
            await PushRegistrar.shared.bank(response.notification, tapped: true)
            await MainActor.run { PhoneRouter.shared.openReview(runId: runId) }
            return
        }
        // A button on the banner: one write, then done — the app is not
        // opened and the gate is not passed. An unknown button falls
        // through to nothing.
        if let press = LockScreenActions.press(actionIdentifier: response.actionIdentifier,
                                               userInfo: info) {
            await LockScreenActions.perform(press)
            return
        }
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier else { return }
        // Written down before the route is taken: the held-first open
        // reads the entry for this receipt out of the log, and the entry
        // carries the subject ids the payload rode in on.
        await PushRegistrar.shared.bank(response.notification, tapped: true)
        let receipt = info["receipt_id"] as? String
        let version = info["destination_version"] as? Int
        await MainActor.run {
            if version == 1, let receipt, UUID(uuidString: receipt) != nil {
                PhoneRouter.shared.tap(receipt: receipt)
            } else { PhoneRouter.shared.go(.needs) }
        }
    }
}

/// Owns the token's journey to the Mac: `register_push_token` when the
/// token changes, after pairing, and on foreground when the last accepted
/// send is over a day old — iOS rotates tokens whenever it likes, and a
/// changed address must never strand the buzzes.
///
/// **Every send rides the client's quiet route** (`PhoneClient.quietPost`):
/// the same LAN walk `post` does, then the same sealed relay rung — but
/// **never a Face ID prompt**. It is deliberately not `PhoneClient.post`,
/// whose relay rung asks RemoteAuth for the face: a silent maintenance call
/// must never raise "Confirm it's you" at a user who did nothing. Skipping
/// the local prompt widens nothing — the verb only names where this phone's
/// own buzzes go, and the Mac still checks the device token, the away lease
/// and `REMOTE_ACTIONS` per request. Without the relay rung a phone that
/// reaches its Mac only through the mailbox (a strict home network, a VPN)
/// never registered at all, which is the bug this route fixes.
@MainActor
final class PushRegistrar {
    static let shared = PushRegistrar()

    /// Re-send at least this often even with an unchanged token, so an
    /// un-registered Mac (a restore, a cleared relay store) heals itself.
    static let resendInterval: TimeInterval = 24 * 60 * 60
    private static let sentTokenKey = "push.sentToken"
    private static let sentAtKey = "push.sentAt"

    /// The token iOS last handed the delegate. Memory only — the system is
    /// the source of truth and re-answers `registerForRemoteNotifications`
    /// on every launch.
    private var currentToken = ""
    /// The pairing the sends authenticate as. A value copy, handed in on
    /// every `ensure` — never a reach into the Keychain from here.
    private var record: PairingRecord?
    /// The transport. Weak and wired once from `BobPhoneApp.onAppear` — the
    /// registrar borrows the client's walk and channel, it never owns them.
    weak var client: PhoneClient?
    private var sending = false
    /// True while the system's notification-permission alert may be up. The
    /// alert drives the scene `.inactive` exactly as a dictation consent
    /// alert does, and locking there demanded a second Face ID right after
    /// Allow — `BobPhoneApp` ORs this into its `consenting` carve-out.
    private(set) var requestingConsent = false

    /// Which APNs host the Mac should aim at for this build. `#if DEBUG`
    /// mislabels an Xcode Release device run as prod — accepted residual,
    /// noted in the README; TestFlight (the normal path) is prod and Xcode
    /// Run (the normal debug path) is dev.
    var env: String {
        #if DEBUG
        return "dev"
        #else
        return "prod"
        #endif
    }

    /// Ask for notification permission and an APNs token — **only once a
    /// pairing record exists**. Before pairing there is nobody to buzz, and
    /// a permission dialog over the QR scanner would be Dark Army asking for
    /// something it cannot yet use. Called on `.active` and when pairing
    /// lands; both idempotent — iOS answers an already-decided permission
    /// silently, and an unchanged token is not re-sent.
    func ensure(record: PairingRecord?) {
        self.record = record
        guard record != nil else { return }
        let center = UNUserNotificationCenter.current()
        // Raised *before* the ask: the permission alert makes the scene
        // inactive, and the flag must already be up when that fires.
        requestingConsent = true
        center.requestAuthorization(options: [.alert, .badge, .sound]) {
            granted, _ in
            Task { @MainActor in
                PushRegistrar.shared.requestingConsent = false
                guard granted else { return }
                UIApplication.shared.registerForRemoteNotifications()
            }
        }
        sendIfDue()
    }

    /// The delegate's hand-off. Sends only when something changed or aged.
    func noteToken(_ hex: String) {
        currentToken = hex
        sendIfDue()
    }

    /// The corner number is the Mac's waiting count from the moment the
    /// last buzz was composed; opening the app supersedes it — the screen
    /// itself is now the truth. The banners still sitting in Notification
    /// Center are the same stale reading, one per buzz, and they went by
    /// the same rule: a person who has the list on screen is not helped by
    /// an alert about a row the desk has since dealt with.
    func clearBadge() {
        let center = UNUserNotificationCenter.current()
        // Read the tray into the log before it is emptied — the banners
        // waiting there are the buzzes this phone slept through.
        Task { @MainActor in
            for notification in await center.deliveredNotifications() {
                bank(notification, tapped: false)
            }
            // A badge that cannot be cleared is not a reason to keep the tray.
            try? await center.setBadgeCount(0)
            center.removeAllDeliveredNotifications()
        }
    }

    /// The Mac's own word that nobody needs you, applied to the banners: a
    /// buzz that arrived while the app was open and has since been dealt
    /// with on the desk would otherwise sit in Notification Center until
    /// the next launch. Called from the client on every applied snapshot;
    /// a no-op when there is nothing delivered.
    func clearDeliveredIfQuiet(needsYou: Int) {
        guard needsYou == 0 else { return }
        let center = UNUserNotificationCenter.current()
        Task { @MainActor in
            for notification in await center.deliveredNotifications() {
                bank(notification, tapped: false)
            }
            center.removeAllDeliveredNotifications()
        }
    }

    /// Buzzes seen but not yet written down: the delegate and the two
    /// tray reads run before the Face ID gate, and the log is a protected
    /// file read only behind it, so what they see waits here in memory
    /// until `PhoneClient.absorbBankedNotifications` drains it after the
    /// unlock. Bounded at the log's own cap; a kill between the two loses
    /// what is banked (never the receipt, which the router keeps on disk).
    private(set) var banked: [NotificationLogEntry] = []

    /// A buzz banked while the gate is already open — the app up, the log
    /// loaded — is absorbed at once rather than at the next unlock, so a
    /// row that arrived on screen reaches Catch up now. The client refuses
    /// the drain until its session has started and the log has been read,
    /// which only the unlock block does, so the protected file is still
    /// never written before the process's first unlock (after a re-lock
    /// the session survives and a bank does write, as `heldPicture` does).
    func bank(_ notification: UNNotification, tapped: Bool) {
        let entry = NotificationLogEntry.from(notification, tapped: tapped)
        if let index = banked.firstIndex(where: { $0.id == entry.id }) {
            banked[index].tapped = banked[index].tapped || entry.tapped
        } else {
            banked.append(entry)
            if banked.count > NotificationLogStore.maxEntries {
                banked.removeFirst(banked.count - NotificationLogStore.maxEntries)
            }
        }
        client?.absorbBankedNotificationsIfOpen()
    }

    /// Drain what is banked.
    func takeBanked() -> [NotificationLogEntry] {
        defer { banked = [] }
        return banked
    }

    /// Drop what is banked without filing it — the un-pair's call. The
    /// banked list is memory the log never saw; left alone it would be
    /// filed under the *next* pairing's token at the next unlock, and the
    /// old Mac's buzzes would read as the new one's.
    func forgetBanked() {
        banked = []
    }

    private func sendIfDue() {
        guard record != nil, !currentToken.isEmpty, !sending,
              let client else { return }
        let defaults = UserDefaults.standard
        let sent = defaults.string(forKey: Self.sentTokenKey) ?? ""
        let sentAt = defaults.double(forKey: Self.sentAtKey)
        let aged = Date().timeIntervalSince1970 - sentAt > Self.resendInterval
        guard currentToken != sent || aged else { return }
        let token = currentToken
        sending = true
        Task { [weak self] in
            guard let self else { return }
            // The client's quiet route: LAN first, then the sealed relay
            // rung, never a Face ID prompt. A failure keeps the stamp old,
            // so the next foreground (or the next token change) retries.
            let ok = await client.registerPush(
                fields: ["token": token, "env": self.env])
            self.sending = false
            if ok {
                defaults.set(token, forKey: Self.sentTokenKey)
                defaults.set(Date().timeIntervalSince1970,
                             forKey: Self.sentAtKey)
            }
        }
    }

    /// Point the Mac's buzzes away from this phone — the forget path, run
    /// on the record and channel captured while they still existed
    /// (`PhoneClient.pushTeardown()`), because the record authenticates the
    /// request and the channel is the away rung home cannot answer.
    /// Best-effort; the authoritative kill is the Mac's own un-pair
    /// (`relay.forget`), which deletes the channel the buzz routes through.
    func unregister(record: PairingRecord, channel: RelayChannel?,
                    home: HomeChannel?, using client: PhoneClient) async {
        forgetSendStamp()
        _ = await client.quietPost(record: record, channel: channel,
                                   home: home,
                                   action: PhoneActions.registerPushToken,
                                   fields: ["token": ""])
    }

    /// A new pairing must not inherit the old one's "already sent" verdict.
    func forgetSendStamp() {
        let defaults = UserDefaults.standard
        defaults.removeObject(forKey: Self.sentTokenKey)
        defaults.removeObject(forKey: Self.sentAtKey)
    }
}
