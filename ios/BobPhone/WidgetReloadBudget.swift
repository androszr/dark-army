import Foundation

/// WidgetKit's daily reload budget, kept from the phone's side.
///
/// WidgetKit honours somewhere between 40 and 70 timeline reloads a day
/// for a tile a person looks at, and a reload asked for past that is not
/// refused but *deferred* — until the budget window rolls, which is the
/// tile sitting on whatever timeline it last built for the rest of the
/// day. Reloads asked for while the app is in the foreground are exempt;
/// the ones that count are asked for with nobody watching: a
/// `BGAppRefreshTask`'s check-in and the departure flush at
/// `.background`. Before 21 Sep 2026 both were unconditional — every
/// background run was a fresh `PhoneClient` whose throttle started empty,
/// so it reloaded whether or not a number had moved, and every departure
/// added one more — and a day of heavy use spent the budget by the
/// afternoon, after which the tile showed yesterday's numbers under an
/// age line reading "1 day" while the app itself checked in all day.
///
/// So the counted reloads keep a ledger: at most `perWindow` in any
/// `window`, the stamps persisted (`stampsKey`) so the background task's
/// fresh process and the app's own share one count. And a counted reload
/// is only asked for when it would change what the tile draws
/// (`worthIt`): the figures moved since the tile last drew them, or the
/// note the tile holds is about to dim over numbers the app just
/// confirmed. Foundation only, so `swiftc` can run this file alone from
/// the host suite (`test_phone_widget_reload_budget.py`).
enum WidgetReloadBudget {
    /// Counted reloads allowed inside `window`: three in two hours is 36 a
    /// day, under the 40 WidgetKit promises at the least.
    static let perWindow = 3
    static let window: TimeInterval = 7200
    /// `UserDefaults` key holding the epoch seconds of the counted reloads
    /// spent inside the window.
    static let stampsKey = "widgetCountedReloads"
    /// `UserDefaults` keys for the last reload of any kind: when it was
    /// asked for, and the note it drew (its JSON) — so a fresh process
    /// knows what the tile holds.
    static let lastReloadAtKey = "widgetLastReloadAt"
    static let lastReloadNoteKey = "widgetLastReloadNote"

    /// The stamps still inside the window at `now`. A stamp from the
    /// future — the clock moved back — is kept, never a reason to spend
    /// more.
    static func recent(_ stamps: [Double], now: Date) -> [Double] {
        let t = now.timeIntervalSince1970
        return stamps.filter { t - $0 < window }
    }

    /// Whether one more counted reload fits.
    static func allows(stamps: [Double], now: Date) -> Bool {
        recent(stamps, now: now).count < perWindow
    }

    /// The stamps with `now` spent, pruned to the window.
    static func spend(stamps: [Double], now: Date) -> [Double] {
        recent(stamps, now: now) + [now.timeIntervalSince1970]
    }

    /// Whether a counted reload would change what the tile draws.
    /// `changed`: the figures differ from the ones the tile last drew.
    /// `noteAge`: how old the note the tile holds is — `nil` when no
    /// reload has ever been recorded, which is worth one.
    /// `dimAfter`: the tile's own dim horizon for that note.
    static func worthIt(changed: Bool, noteAge: TimeInterval?,
                        dimAfter: TimeInterval) -> Bool {
        guard let noteAge else { return true }
        return changed || noteAge >= dimAfter
    }
}
