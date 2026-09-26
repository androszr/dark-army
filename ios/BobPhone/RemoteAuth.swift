import Foundation
import LocalAuthentication

/// The Face ID / passcode gate in front of remote *writes*.
///
/// **The app-open unlock is the face (25 Sep 2026).** `LockGate.unlock()`
/// succeeding calls `grantForSession()`, and from then until the app locks
/// again (`LockGate.lock()` → `reset()`) every remote write proceeds with no
/// second prompt: one Face ID per opening, not one per action. Only a write
/// made without that unlock — none today, but the gate fails closed —
/// falls back to `LAContext.evaluatePolicy(.deviceOwnerAuthentication)` and
/// its five-minute grace. The Mac still checks the device token, the away
/// lease and `REMOTE_ACTIONS` per request. The gate is deliberately on
/// the write path and **not** on the relay key itself: guarding the Keychain
/// record with `SecAccessControl` biometry would prompt on every background
/// read poll, and reads are allowed from away by design.
@MainActor
final class RemoteAuth {
    static let shared = RemoteAuth()

    /// How long one successful prompt covers. Long enough for arm-confirm
    /// and a small burst of related actions; short enough that a phone left
    /// on a table does not stay armed.
    static let graceSeconds: TimeInterval = 300

    private var lastSuccess: Date?
    /// Set by a successful app-open unlock, cleared by `reset()`.
    private var sessionGranted = false

    /// The app-open unlock succeeded: remote writes need no further face
    /// until the app locks (`reset()`).
    func grantForSession() {
        sessionGranted = true
    }

    /// True while the Face ID / passcode sheet for a remote write may be up.
    /// The sheet drives the scene `.inactive` exactly as a consent alert
    /// does; `BobPhoneApp` ORs this into its carve-out so the sheet does not
    /// lock the app, unmount the screen the press was made on, and ask a
    /// second time on `.active`. Cleared synchronously, with no delayed
    /// clearing window: `.inactive` arrives while the sheet is presented and
    /// the flag is up, and `onChange(of: scenePhase)` fires only on a
    /// transition, so the dismissal produces one `.active` and never a
    /// second `.inactive` after the flag drops. Two overlapping prompts are
    /// harmless for the same reason — the second `evaluatePolicy` fails at
    /// once and lowers the flag while the scene is already inactive, so a
    /// counter would guard a case that cannot lock.
    private(set) var prompting = false

    /// Whether a remote write may proceed. Prompts when the grace has
    /// lapsed; a refusal or an unavailable authenticator fails closed.
    func authorize(reason: String = "Confirm it's you to act on your Mac from away") async -> Bool {
        if sessionGranted { return true }
        if let last = lastSuccess,
           Date().timeIntervalSince(last) < Self.graceSeconds {
            return true
        }
        let context = LAContext()
        var check: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication,
                                        error: &check) else {
            return false
        }
        prompting = true
        defer { prompting = false }
        do {
            let ok = try await context.evaluatePolicy(
                .deviceOwnerAuthentication, localizedReason: reason)
            if ok { lastSuccess = Date() }
            return ok
        } catch {
            return false
        }
    }

    /// The next write prompts again. Called from the un-pair path
    /// (`ContentView.forget`) and from `LockGate.lock()` — the grace must
    /// not survive the pairing it was granted under, or the phone being
    /// put down.
    func reset() {
        lastSuccess = nil
        sessionGranted = false
    }
}
