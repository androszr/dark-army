import Foundation
import UserNotifications

/// A local reminder before away access runs down.
///
/// The away window is renewed only by a check-in on home Wi-Fi
/// (`relay.note_lan_proof`, `docs/transport-contract.md`), so a phone that
/// has been away for days finds its writes refused with no warning. This
/// books one local notification twelve hours before the expiry the Mac
/// published — replaced whenever that expiry moves, removed when there is
/// no window — and nothing else: no network, no relay, no new wire field.
/// `fireDate` is the one rule, pure so it can be tabled.
enum LeaseReminder {
    static let identifier = "lease-reminder"
    static let lead: TimeInterval = 12 * 60 * 60
    static let title = "Away access ends soon"
    static let body = "Dark Army can still read your Mac, but pressing anything from away stops in about twelve hours. Check in on home Wi-Fi to renew it."

    /// When to fire: `lead` before the expiry, only where that moment is
    /// still ahead. No window (0) or one already inside the lead is nil.
    static func fireDate(expiresAt: Double, now: Double) -> Date? {
        guard expiresAt > 0 else { return nil }
        let at = expiresAt - lead
        guard at > now else { return nil }
        return Date(timeIntervalSince1970: at)
    }

    /// Book or clear the reminder for this expiry. Idempotent: the same
    /// identifier replaces the previous request.
    static func sync(expiresAt: Double, now: Double = Date().timeIntervalSince1970) {
        let center = UNUserNotificationCenter.current()
        guard let fire = fireDate(expiresAt: expiresAt, now: now) else {
            center.removePendingNotificationRequests(withIdentifiers: [identifier])
            return
        }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let trigger = UNTimeIntervalNotificationTrigger(
            timeInterval: max(60, fire.timeIntervalSinceNow), repeats: false)
        center.add(UNNotificationRequest(identifier: identifier, content: content,
                                         trigger: trigger))
    }
}
