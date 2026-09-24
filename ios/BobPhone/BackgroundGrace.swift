import Foundation

/// The five-minute rule for a phone that left the app and came back.
///
/// `.background` no longer tears the poller down; it stamps
/// `PhoneClient.departedAt` and the return decides, in one place, whether
/// the running connection is kept or a fresh one is started. The loop reads
/// the same rule so a process iOS resumed for other reasons — a
/// `BGAppRefreshTask`, a push — shuts a stale poller down by itself.
///
/// Why 300 seconds: long enough to answer a message and come back to the
/// picture you left, and short enough to stay under
/// `BackgroundRefresh.intervals.first` (15 minutes), so a loop that was
/// left behind has already stopped itself before a background refresh —
/// a second client on the same `to-phone` mailbox, where a pop is a
/// removal — can possibly run. Foundation only, so `swiftc` can run this
/// file alone from the host suite.
enum BackgroundGrace {
    /// How long a departure is forgiven, in seconds.
    static let window: TimeInterval = 300

    enum Verdict: Equatable {
        /// The poller is running and the absence was short: keep it and
        /// ask for one check-in now.
        case keep
        /// Nothing is running, or the absence outran the window: start
        /// fresh, exactly as a cold launch does.
        case restart
    }

    /// The unlock gate's decision. `alive` is `PhoneClient.isPolling`; a
    /// nil `departedAt` is a return from an `.inactive` glance that stamped
    /// nothing. A negative interval — the clock moved backwards — is inside
    /// the window: a restart on a clock adjustment would be the reconnect
    /// this rule exists to avoid.
    static func verdict(departedAt: Date?, now: Date, alive: Bool) -> Verdict {
        guard alive else { return .restart }
        guard let departedAt else { return .keep }
        return now.timeIntervalSince(departedAt) <= window ? .keep : .restart
    }

    /// The loop's own reading: past the window with nobody back. False for
    /// no departure at all and for anything inside the window, the
    /// backwards clock included.
    static func expired(departedAt: Date?, now: Date) -> Bool {
        guard let departedAt else { return false }
        return now.timeIntervalSince(departedAt) > window
    }

    /// What makes a published pairing record *the same pairing*: everything
    /// on it but the four sealed-frame counters. Plain values, so the rule
    /// runs alone under `swiftc`; `PairingRecord.identity` (in
    /// `BobPhoneApp.swift`) is the one bridge from the record.
    struct PairingIdentity: Equatable {
        var token: String
        var host: String
        var hosts: [String]
        var port: Int
        var deviceId: String
        var relayKey: String
        var relayURL: String
        var homeKey: String
        var homeKeyCrossedInClear: Bool
        /// The socket relay's address: a changed one restarts the poller
        /// like a changed mailbox address. Defaulted so a record from
        /// before the socket lane still builds an identity.
        var relayWSURL: String = ""
    }

    /// The pairing-record change handler's decision: true when the running
    /// poller is left alone. The unlock gate's `load()` re-publishes the
    /// record the Keychain holds, and `suspend()` landed the counters there
    /// at `.background`, so on a return inside the grace the loaded copy
    /// differs from the published one in its counters alone — a `start`
    /// on that change would undo `wake()`: cancel the check-in, drop the
    /// digest, show connecting. A changed address (`promote`, `learnHosts`),
    /// token or key is a different pairing and restarts as it always has;
    /// so does a change landing with nothing running, or with no record
    /// before it (a cold launch's first publish).
    static func keepsPoller(old: PairingIdentity?, new: PairingIdentity,
                            polling: Bool) -> Bool {
        guard polling, let old else { return false }
        return old == new
    }
}
