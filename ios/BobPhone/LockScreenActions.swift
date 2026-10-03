import Foundation
import UserNotifications

/// Allow, Deny and Acknowledge on the banner itself.
///
/// The Mac's per-phone switch ("Answer from the lock screen", the Devices
/// menu) puts an `act` word and one or two opaque ids on the buzz — never
/// the ask's words — and names the category iOS draws the buttons for.
/// Each button is `.authenticationRequired`, so iOS asks for the unlock
/// before the action runs; the press then makes **one** write through
/// `PhoneClient.lockScreenWrite` — `permission_verdict` for Allow / Deny,
/// `dismiss` for Acknowledge, the verbs the app posts — and the Mac
/// re-checks the pairing, the lease and the verb as it does for any press.
/// A picks buzz also carries **Open review**, a `.foreground` button that
/// writes nothing: it asks for the unlock, opens the app and hands the run's
/// short id to `PhoneRouter.openReview` (`opens`), on every paired phone.
/// A buzz without the fields (the switch off, an older Mac) carries no
/// category and taps open the app exactly as before.
enum LockScreenActions {
    static let permissionCategory = "bob.permission"
    static let acknowledgeCategory = "bob.acknowledge"
    static let allow = "bob.allow"
    static let deny = "bob.deny"
    static let acknowledge = "bob.acknowledge.press"
    static let reviewCategory = "bob.review"
    static let openReview = "bob.review.open"

    struct Press: Equatable {
        let action: String
        let fields: [String: String]
    }

    static func register() {
        let allowAction = UNNotificationAction(
            identifier: allow, title: "Allow", options: [.authenticationRequired])
        let denyAction = UNNotificationAction(
            identifier: deny, title: "Deny",
            options: [.authenticationRequired, .destructive])
        let ackAction = UNNotificationAction(
            identifier: acknowledge, title: "Acknowledge",
            options: [.authenticationRequired])
        let permission = UNNotificationCategory(
            identifier: permissionCategory, actions: [allowAction, denyAction],
            intentIdentifiers: [], options: [])
        let ack = UNNotificationCategory(
            identifier: acknowledgeCategory, actions: [ackAction],
            intentIdentifiers: [], options: [])
        let openAction = UNNotificationAction(
            identifier: openReview, title: "Open review",
            options: [.foreground, .authenticationRequired])
        let review = UNNotificationCategory(
            identifier: reviewCategory, actions: [openAction],
            intentIdentifiers: [], options: [])
        UNUserNotificationCenter.current().setNotificationCategories([permission, ack, review])
    }

    /// The one write a button means, or nil for a tap on the banner body
    /// or a payload that does not carry what the button needs. Pure.
    static func press(actionIdentifier: String, userInfo: [AnyHashable: Any]) -> Press? {
        let act = userInfo["act"] as? String ?? ""
        let session = userInfo["session_id"] as? String ?? ""
        let request = userInfo["request_id"] as? String ?? ""
        guard idOK(session) else { return nil }
        switch actionIdentifier {
        case allow, deny:
            guard act == "permission", idOK(request) else { return nil }
            return Press(action: PhoneActions.permissionVerdict,
                         fields: ["request_id": request,
                                  "behavior": actionIdentifier == allow ? "allow" : "deny"])
        case acknowledge:
            guard act == "acknowledge" else { return nil }
            return Press(action: PhoneActions.dismiss, fields: ["session_id": session])
        default:
            return nil
        }
    }

    /// The review run an **Open review** press names, or nil for anything
    /// else (the body tap, another button, an out-of-shape id). Pure.
    static func opens(actionIdentifier: String, userInfo: [AnyHashable: Any]) -> String? {
        guard actionIdentifier == openReview,
              userInfo["act"] as? String == "review",
              let run = userInfo["run_id"] as? String, idOK(run) else { return nil }
        return run
    }

    /// `relay_client.PUSH_ID_SHAPE`.
    static func idOK(_ id: String) -> Bool {
        guard !id.isEmpty, id.count <= 120 else { return false }
        return id.unicodeScalars.allSatisfy {
            ($0.value >= 48 && $0.value <= 57) || ($0.value >= 65 && $0.value <= 90)
                || ($0.value >= 97 && $0.value <= 122) || ".-_:".unicodeScalars.contains($0)
        }
    }

    @MainActor
    static func perform(_ press: Press) async {
        let pairing = PairingStore()
        pairing.load()
        guard let record = pairing.record else { return }
        let client = PhoneClient()
        client.onPromote = { host in pairing.promote(host: host) }
        client.onCounters = { token, send, recv in
            pairing.persistCounters(token: token, send: send, recv: recv)
        }
        client.storedCounters = { pairing.storedCounters() }
        client.onHomeCounters = { token, send, recv in
            pairing.persistHomeCounters(token: token, send: send, recv: recv)
        }
        client.storedHomeCounters = { pairing.storedHomeCounters() }
        _ = await client.lockScreenWrite(record: record, action: press.action,
                                         fields: press.fields)
    }
}
