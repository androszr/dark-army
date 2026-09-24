import SwiftUI

/// The identity bar across the top of every tab — the panel's `BrandBar`.
///
/// The mark, prompt line, and a 2pt rule that turns red **only** when
/// somebody needs you. Colour is never alone: the count is spelled out in
/// words beside it ("1 need you"), the way the panel's is. The mark is
/// identity, not a gauge — it never poses; the subtitle and the rule carry
/// every bit of news about the fleet.
///
/// The trailing end carries two buttons on every tab: a plus that opens the
/// card composer and, rightmost, a person that opens the profile screen —
/// which is where un-pairing lives now. The bar itself navigates nothing:
/// each tab root owns the two booleans and hands closures in, because a
/// navigation link inside bar chrome is exactly where SwiftUI link
/// resolution has a history of failing silently.
struct PhoneBrandBar: View {
    /// `~/needs`, `~/fleet`, `~/board`, or `~/usage`.
    let path: String
    let connected: Bool
    /// Every decision subject — `Snapshot.needsYouCount` over the live
    /// projection.
    let needsYouCount: Int
    let onCompose: () -> Void
    let onProfile: () -> Void

    /// The away standing — `PhoneClient` publishes it, every tab's bar
    /// draws it. A default so no call site changes.
    @ObservedObject var away: AwayState = .shared

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var needsYou: Bool { connected && needsYouCount > 0 }

    private var subtitle: String {
        if !connected { return "offline" }
        if needsYouCount > 0 { return "\(needsYouCount) need you" }
        return "Companion"
    }

    var body: some View {
        VStack(spacing: 0) {
            // Past an accessibility size the identity line and the two
            // buttons stop sharing a line: at those sizes the prompt alone
            // wraps to three, and 88pt of buttons beside it leaves nothing.
            topRow
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            Rectangle()
                .fill(needsYou ? Color.red : Theme.phosphor)
                .frame(height: 2)
            if connected, let line = awayLine {
                Text(line.text)
                    .font(Theme.mono(10))
                    .foregroundStyle(line.urgent ? Theme.alarm : Theme.faint)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 3)
                    .background(Theme.bar)
            }
        }
        .background(Theme.bar)
    }

    @ViewBuilder
    private var topRow: some View {
        if dynamicTypeSize.isAccessibilitySize {
            VStack(alignment: .leading, spacing: 6) {
                identity
                HStack(spacing: 10) {
                    actions
                    Spacer(minLength: 0)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            HStack(spacing: 10) {
                identity
                Spacer(minLength: 8)
                actions
            }
        }
    }

    @ViewBuilder
    private var identity: some View {
        BrandMark(size: 24)
        PromptLine(path: path, command: subtitle,
                   commandColor: needsYou ? .red : Theme.phosphor,
                   size: 12)
        if connected && away.via == .relay {
            // Never colour or absence alone: the word AWAY says the
            // screen is live through the mailbox, not home Wi-Fi.
            Text("AWAY")
                .font(Theme.mono(9, weight: .semibold))
                .foregroundStyle(Theme.amber)
                .padding(.horizontal, 4)
                .padding(.vertical, 2)
                .overlay(Rectangle()
                    .strokeBorder(Theme.amber, lineWidth: 1))
        }
    }

    @ViewBuilder
    private var actions: some View {
        // An acting button goes away when the away window has
        // lapsed — the line under the bar says why in words.
        if away.canAct {
            DecryptButton(action: onCompose) {
                Image(systemName: "plus")
                    .font(Theme.mono(15, weight: .medium))
                    .foregroundStyle(Theme.phosphor)
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("New card")
        }
        DecryptButton(action: onProfile) {
            Image(systemName: "person")
                .font(Theme.mono(15, weight: .medium))
                .foregroundStyle(Theme.phosphor)
                .frame(width: 44, height: 44, alignment: .trailing)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Profile")
    }

    /// The away window, in one quiet line. At home it shows the window
    /// renewing; away it counts down; lapsed it says plainly what to do.
    /// Nil when this pairing has no away channel at all.
    private var awayLine: (text: String, urgent: Bool)? {
        AwaySpan.line(leaseExpiresAt: away.leaseExpiresAt, via: away.via,
                      now: Date().timeIntervalSince1970)
    }
}

/// The away window as words — the phone's **one** formatter for it, shared
/// by the top bar and the profile screen so the two can never disagree.
///
/// It derives no window of its own: the Mac publishes `lease_expires_at` and
/// this is arithmetic over that number and the clock. The length a check-in
/// at home buys is a grant made on the Mac and is deliberately not on the
/// wire — the phone has no business knowing, let alone naming, a number it
/// could never change.
enum AwaySpan {
    /// Inside this much remaining, the line goes to the alarm colour. A
    /// multi-day grant means a person can be a long way from home when it
    /// runs down, and the last day is when that is worth saying.
    static let finalDay: Double = 24 * 60 * 60

    /// The line and whether it is urgent, or nil where there is nothing to
    /// say — no away window at all, or one that is simply not in use at home.
    static func line(leaseExpiresAt: Double,
                     via: PhoneClient.Via,
                     now: Double) -> (text: String, urgent: Bool)? {
        if via == .relay {
            guard leaseExpiresAt > now else {
                // Byte-identical to the Mac's own `LEASE_REFUSAL`, which is
                // what the daemon will say if the phone tries to act.
                return ("// away access lapsed — check in on home Wi-Fi to renew it",
                        true)
            }
            let left = leaseExpiresAt - now
            return ("// AWAY — write access ends in \(span(left))",
                    left < finalDay)
        }
        guard leaseExpiresAt > now else { return nil }
        return ("// away window renewed — good until "
                + until(leaseExpiresAt, now: now), false)
    }

    /// A remaining span, coarse on purpose: days and hours over a day out,
    /// hours and minutes under it. `52h 12m` is a number nobody reads.
    static func span(_ seconds: Double) -> String {
        let left = Int(max(0, seconds))
        if left >= Int(finalDay) {
            return "\(left / 86400)d \((left % 86400) / 3600)h"
        }
        let hours = left / 3600
        let minutes = (left % 3600) / 60
        return hours > 0 ? "\(hours)h \(minutes)m" : "\(minutes)m"
    }

    /// The moment the window closes: time alone while it is today, date and
    /// time once it is not — the same rule the Mac's menu follows.
    static func until(_ leaseExpiresAt: Double, now: Double) -> String {
        let expiry = Date(timeIntervalSince1970: leaseExpiresAt)
        let formatter = DateFormatter()
        formatter.timeStyle = .short
        formatter.dateStyle = Calendar.current.isDate(
            expiry, inSameDayAs: Date(timeIntervalSince1970: now))
            ? .none : .short
        return formatter.string(from: expiry)
    }
}

/// `// last heard 14s ago`, amber, under the bar.
///
/// Amber by the panel's own semantics: not an alarm and not a refusal, a held
/// state a person should know about. It ticks inside its own `TimelineView`
/// so nothing else on the screen re-renders once a second — the one clock the
/// design system allows, because it measures a real elapsed time rather than
/// decorating anything.
struct StaleBanner: View {
    let lastHeard: Date

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            Text("// last heard \(Int(context.date.timeIntervalSince(lastHeard)))s ago")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.amber)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14)
                .padding(.vertical, 4)
                .background(Theme.bar)
        }
    }
}

/// "// as of 5m ago · the Mac is out of reach": the age of a held picture,
/// on the phone's own clock, ticking. `StaleBanner`'s one-clock
/// `TimelineView` pattern; the figure is `FleetAge`'s compact age, spoken in
/// full for VoiceOver.
struct HeldPictureBanner: View {
    let asOf: Date
    /// The tail of the line: the Mac has not answered yet on this launch,
    /// or it has stopped answering. Both are true statements about the
    /// picture's age; only the second says the Mac is out of reach.
    var reaching: Bool = false

    private var tail: String {
        reaching ? "checking with the Mac" : "the Mac is out of reach"
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let started = asOf.timeIntervalSince1970
            Text("// as of \(FleetAge.text(startedAt: started, now: context.date.timeIntervalSince1970)) ago · \(tail)")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.amber)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 14)
                .padding(.vertical, 4)
                .background(Theme.bar)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel("Picture as of \(FleetAge.spoken(startedAt: started, now: context.date.timeIntervalSince1970)) ago; \(tail)")
        }
    }
}

/// The offline line, and the wait behind it.
///
/// Row one is the failure in the Mac's or the relay's own words, exactly as
/// today. Row two says the reconnection is scheduled, which attempt it is and
/// how long until it fires — every figure read from `LinkAttempt`, which the
/// poll loop stamps at the one site that decides the pause. Nothing here
/// counts, times or composes anything: a view that ran its own clock would
/// drift from the loop the moment iOS suspended the app.
///
/// The `TimelineView` is the same justification `StaleBanner` has — it
/// measures a real elapsed time rather than decorating anything — and the two
/// are mutually exclusive on screen, since that one needs `.live` and this one
/// only ever appears while the link is down.
struct ReconnectBar: View {
    /// The failure in the Mac's or the relay's own words — passed in from
    /// `lastError` and rendered verbatim. Nothing here composes, shortens
    /// or paraphrases it.
    let sentence: String
    let link: LinkAttempt
    /// What the attempt in flight is doing, in the client's words, or ""
    /// between attempts. Drawn verbatim in place of the countdown while an
    /// attempt is running; composed nowhere here.
    var phase: String = ""

    private var routeText: String {
        link.route == .relay ? "through the mailbox" : "on home Wi-Fi"
    }

    private func remaining(at now: Date) -> Int {
        guard let due = link.nextAttemptAt else { return 0 }
        return max(0, Int(ceil(due.timeIntervalSince(now))))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Image(systemName: link.route == .relay
                      ? "antenna.radiowaves.left.and.right.slash"
                      : "wifi.exclamationmark")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    // The words carry it; the glyph is never the only sign.
                    .accessibilityHidden(true)
                Text(sentence)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
            }
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let left = remaining(at: context.date)
                let waitText = left > 0 ? "retry in \(left)s" : "retrying now"
                HStack(spacing: 8) {
                    if !phase.isEmpty {
                        // An attempt is in flight: say which rung it is on
                        // rather than a countdown that already reached zero.
                        Text("RECONNECTING… attempt \(link.failures) · " + phase)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.amber)
                            .lineLimit(2, reservesSpace: true)
                    } else {
                        Text("RECONNECTING… attempt \(link.failures) · "
                             + waitText + " · " + routeText)
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.amber)
                            .lineLimit(2, reservesSpace: true)
                        // One cell per real second of the real wait —
                        // determinate, so it can never be the decorative
                        // sweep the design bans, and monospaced so it
                        // cannot reflow.
                        Text(cells(remaining: left))
                            .font(Theme.mono(10))
                            .foregroundStyle(Theme.faint)
                            .accessibilityHidden(true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 8)
        .padding(.vertical, 8)
        .background(Theme.bar)
        .overlay(alignment: .top) {
            Rectangle().fill(Theme.hair).frame(height: 1)
        }
    }

    private func cells(remaining left: Int) -> String {
        let total = max(0, link.waitSeconds)
        guard total > 0 else { return "" }
        let done = min(total, max(0, total - left))
        return String(repeating: "█", count: done)
            + String(repeating: "░", count: total - done)
    }
}

/// Shown while the first snapshot has not landed. A spinner during a genuine
/// wait is information, not the decorative blink the design bans — but once
/// an attempt has actually failed, a spinner alone reads as a frozen app, so
/// the reconnection line replaces it.
struct ConnectingView: View {
    var link: LinkAttempt = LinkAttempt()
    var sentence: String = ""
    /// The client's own words for the step under way. Drawn under the
    /// typed line so a first connect that walks silent addresses and then
    /// the mailbox never reads as frozen.
    var phase: String = ""

    var body: some View {
        VStack(spacing: 14) {
            BrandMark(size: 40)
            PromptLine(path: "~", command: "connecting…")
            if link.failures > 0 {
                ReconnectBar(sentence: sentence, link: link, phase: phase)
            } else {
                AgentChatterView(.line, wait: .connecting, seed: "connecting",
                                 spoken: "Connecting to your Mac")
                    .id("connecting")
                    .padding(.horizontal, 24)
                if !phase.isEmpty {
                    Text(phase)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Theme.bg)
    }
}

/// `// nothing here` and friends — one quiet line, never a blank screen.
struct CommentLine: View {
    let text: String
    var size: CGFloat = 11

    var body: some View {
        Text("// \(text)")
            .font(Theme.mono(size))
            .foregroundStyle(Theme.faint)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
    }
}
