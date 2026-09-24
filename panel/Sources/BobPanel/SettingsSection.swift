import AppKit
import SwiftUI

/// The rail's top band: the usage chips (`SidebarUsageBand`), the brand bar
/// with the ⋯ settings control, and the chip text rules the tests pin.
///
/// The popover-era footer that used to open this file (`SettingsSection`, a
/// digit-width chip row beside the ⋯ menu) went on 20 Sep 2026: nothing had
/// constructed it since the panel became a window, and with it went
/// `UsageRow`'s footer mode — the band is the one caller and always full
/// width. The chips keep the **menu-bar strip's own vocabulary**: bare digits
/// over an under-meter, unframed, colour as an exception signal only — the
/// argument written down for `_usage_image` in CLAUDE.md. The reset time
/// stays on the chip in short form (84% reads very differently ten minutes
/// before rollover than four hours before); the full title and the long
/// "Resets …" sentence are on hover.

/// The rail's standing band: the usage chips given the whole width, with the
/// hairline **below** them.
///
/// Full width is not decoration. A digit-width chip at the top of a 680pt
/// sidebar leaves two thirds of the band empty and every meter three
/// characters long. Equal flexible columns spend the width on the one thing in
/// the chip that encodes a quantity — the meter — and a meter you can read at a
/// glance is the whole point of having it rather than only the digits.
///
/// Columns rather than a stack of labelled bars (four windows at 16pt each is a
/// dashboard, and this band is chrome), and rather than one segmented bar: four
/// windows are four independent denominators, and segments of a single bar claim
/// they share one.
struct SidebarUsageBand: View {
    @ObservedObject var client: DaemonClient

    var body: some View {
        VStack(spacing: 0) {
            UsageRow(client: client)
                .padding(.horizontal, 14)
                .padding(.top, 4)
                .padding(.bottom, 10)
            Divider()
        }
    }
}

/// The rate-limit windows as one row of equal flexible columns.
struct UsageRow: View {
    @ObservedObject var client: DaemonClient

    private var bars: [UsageBar] {
        var all = client.usage
        // Grok now rides `/api/usage` with the others. The stdin copy is the
        // older-daemon fallback, and must not draw a second weekly chip when
        // the poll already brought one.
        let hasGrok = all.contains { $0.kind == "grok_weekly" || $0.provider == "grok" }
        if !hasGrok, let grok = client.context.grokPercent {
            all.append(UsageBar.grok(percent: grok, resetsAt: client.context.grokResetsAt))
        }
        return all
    }

    /// True when this chip is the first of its provider — the bars arrive
    /// grouped (Claude's windows, then Grok's), so "first of its run" is the
    /// same test as "provider differs from the one before it".
    private func opensRun(at index: Int) -> Bool {
        let list = bars
        guard index > 0 else { return true }
        return list[index].provider != list[index - 1].provider
    }

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            ForEach(Array(bars.enumerated()), id: \.element.id) { index, bar in
                // A rule between providers, not between chips. Three Claude
                // windows and one Grok window are two different *accounts*,
                // and without a break the row reads as one series of four —
                // which is what made the mark alone insufficient. The mark
                // says whose; the rule says where one ends.
                if index > 0, opensRun(at: index) {
                    Divider()
                        .frame(height: 26)
                        .padding(.horizontal, 4)
                }
                // Each provider's *run* opens with its mark, exactly as the
                // strip does it — not every chip, which would repeat the
                // Claude burst three times to say something the first one
                // already said.
                UsageChip(bar: bar, showsMark: opensRun(at: index))
            }
        }
    }
}

/// The panel's identity row: the mark, the subtitle, and the ⋯ menu.
/// Drawn on every window — there is no popover.
///
/// The subtitle is a fact, not a tagline. `Companion` when the fleet is quiet;
/// `N need you` when someone is waiting (the same words the tally uses);
/// `offline` when the daemon is gone. The mark says none of that: it is the
/// mask, still, with a clear ground. The Dock tile is the same mask in
/// Dark Army green.
/// Identity does not pose — the subtitle and the 2pt rule carry the news, and
/// a header twitching next to the editor is noise the strip already covers.
///
/// Colour is an exception signal, same house rule as the usage meters.
/// Green is body ink now, so a painted wash would paint the whole bar;
/// the 2pt rule flips red only when someone is waiting.
struct BrandBar: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var board: BoardState
    /// Attention rows + cards without a session, not `counts.attention`. The
    /// raw bucket flaps at turn cadence (`Stop` raises a wait, the next
    /// tool drops it); the section holds for `attentionDwell`, and a bar
    /// that disagreed with the section would flash red over a list that
    /// had not moved.
    let attention: Int

    private var needsYou: Bool { client.connected && attention > 0 }
    private var snapshotBoard: Board { client.snapshot.board }

    private var subtitle: String {
        if !client.connected { return "offline" }
        if attention > 0 { return "\(attention) need you" }
        return "Companion"
    }

    private var ruleColor: Color { needsYou ? .red : Theme.phosphor }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                BrandMark(size: 20)
                PromptLine(path: "~/board", command: subtitle,
                           commandColor: needsYou ? .red : Theme.phosphor,
                           size: 12)
                Spacer(minLength: 8)
                if snapshotBoard.available, !snapshotBoard.dispatchEnabled {
                    Text("Dark Army is not allowed to start sessions")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                }
                SettingsButton(client: client)
                    .frame(width: 20, height: 18)
                    .fixedSize()
            }
            .padding(.horizontal, 14)
            .frame(height: PanelMetrics.brandBar - 2)
            Rectangle()
                .fill(ruleColor)
                .frame(height: 2)
        }
        .frame(height: PanelMetrics.brandBar)
    }
}

/// The ⋯ control in the brand bar: a plain glyph that asks for the settings
/// window (`SettingsWindow.swift`) by posting `.panelOpenSettings`. The orange
/// tint says the running build is stale; the tooltip names the build.
struct SettingsButton: View {
    @ObservedObject var client: DaemonClient

    var body: some View {
        Button {
            NotificationCenter.default.post(name: .panelOpenSettings, object: nil)
        } label: {
            Image(systemName: "ellipsis.circle")
                .font(.system(size: 13, weight: .regular))
                .foregroundStyle(client.context.buildStale ? Color.orange : Theme.dim)
                .frame(width: 20, height: 18)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable()
        .help(client.context.build.isEmpty
              ? "Settings" : "\(client.context.build) — settings")
        .accessibilityLabel("Settings")
    }
}

/// Pure text of a `UsageChip`: the short name, the model suffix, and the
/// percentage. Extracted so tests can pin the strings without instantiating
/// the view — wrapping is refused structurally, and these are the words
/// that used to wrap.
enum UsageChipText {
    /// Short label keyed on `kind`, falling back to the daemon's own title for a
    /// window this panel has not been taught yet — the same tolerance the
    /// decoding has, so a new limit window appears rather than disappearing.
    ///
    /// The provider is **drawn**, never spelled: the mark beside this label is
    /// `ProviderMark`, the same Canvas the agent rows and the strip use. A
    /// literal "✳" here was wrong twice over — a typed glyph standing in for a
    /// drawn one, and the *Claude* shape (eight rays) sitting on Grok's chip,
    /// whose mark is a slanted X.
    static func label(kind: String, shortLabel: String, title: String) -> String {
        switch kind {
        case "session": return "5h"
        case "weekly_all": return "7d"
        // `weekly_scoped` is the real kind — the per-model window, titled from
        // its own scope ("Current week (Fable)"). Guessing at `weekly_fable`
        // meant this chip fell through to the full title and was three times
        // the width of its neighbours.
        case "weekly_scoped": return "7d·\(modelSuffix(title: title))"
        case "grok_weekly": return "7d"
        case let kind where kind.hasPrefix("codex_"):
            return shortLabel.isEmpty ? "7d" : shortLabel
        default: return shortLabel.isEmpty ? title : shortLabel
        }
    }

    /// "Current week (Fable)" → "Fable".
    static func modelSuffix(title: String) -> String {
        guard let open = title.firstIndex(of: "("),
              let close = title.firstIndex(of: ")"),
              open < close else { return "model" }
        return String(title[title.index(after: open)..<close])
    }

    static func figure(percent: Double?, stale: Bool) -> String {
        guard let percent = percent, !stale else { return "–" }
        return "\(Int(percent))%"
    }

    /// Above this age a reading stops being allowed to pass as current. Half an
    /// hour: comfortably clear of the live fetch's own five-minute cycle, so a
    /// working feature never draws the note, and short enough that a figure
    /// nobody could refresh admits it within one sitting.
    static let staleReadSeconds: Double = 1800

    /// The one source whose age is never a failure. Claude Code reports the two
    /// account-wide windows on every turn, so a figure hours old there means
    /// only that nobody took a turn — there is no refresh to have failed. The
    /// note went out on those bars too at first, which put a false alarm on
    /// both meters every time the Mac sat idle for half an hour: exactly how a
    /// warning stops being read before the one real case arrives.
    static let selfReportedSource = "statusline"

    /// "read 41 min ago", for a figure being **held** rather than refreshed.
    ///
    /// nil while the reading is current, because a note on every bar all the
    /// time is noise, and noise is the thing that stops being read. nil too for
    /// a bar carrying no `as_of` at all — an older daemon publishes none, and
    /// guessing an age for it would be the same untruth pointing the other way.
    /// nil, finally, for a self-reported figure: see `selfReportedSource`. An
    /// unnamed source keeps the note — the Codex and Grok bars carry none, and
    /// they genuinely have no local fallback to age gracefully.
    ///
    /// This is not `stale`. `stale` says the window itself has reset and the
    /// figure is now wrong; this says the figure may still be right and nobody
    /// has checked in a while. Both can be true, and they read differently.
    static func ageNote(asOf: Double?, source: String = "",
                        now: Double = Date().timeIntervalSince1970) -> String? {
        guard let asOf = asOf, source != selfReportedSource else { return nil }
        let age = now - asOf
        guard age >= staleReadSeconds else { return nil }
        if age < 3600 { return "read \(Int(age / 60)) min ago" }
        if age < 86400 { return "read \(Int(age / 3600)) h ago" }
        return "read \(Int(age / 86400)) d ago"
    }
}

/// One rate-limit window as a chip: four lines — the name, then the percentage
/// in larger type, then a meter the width of the chip, then the reset time.
/// A chip's width is a share of a resizable window, so any width argument is a
/// guess and wrapping is refused structurally (one line per text view, trimmed
/// with a trailing ellipsis). Hover still carries the full title.
private struct UsageChip: View {
    let bar: UsageBar
    var showsMark = false

    /// The strip's tiers, and the same rule with them: neutral up to 75%, amber
    /// to 90%, red above — and at red the digits change colour *and* weight,
    /// because a 3pt bar is not where you put the news.
    private var tint: Color {
        guard let percent = bar.percent, !bar.stale else { return Theme.faint }
        return percent >= 90 ? .red : percent >= 75 ? .orange : Theme.phosphor
    }

    private var critical: Bool {
        guard let percent = bar.percent, !bar.stale else { return false }
        return percent >= 90
    }

    private var label: String {
        UsageChipText.label(kind: bar.kind, shortLabel: bar.shortLabel, title: bar.title)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                if showsMark {
                    ProviderMark(provider: bar.provider, size: 8)
                }
                Text(label)
                    .font(.system(size: 11))
                    .foregroundStyle(Theme.faint)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }

            Text(figure)
                .font(.system(size: 13, weight: critical ? .semibold : .regular)
                    .monospacedDigit())
                .foregroundStyle(tint)
                .lineLimit(1)
                .truncationMode(.tail)

            // The meter takes the chip's width — a flexible column — with no
            // GeometryReader: the shape is "as wide as the chip".
            meter
                .frame(height: 3)

            Text(resetLabel)
                .font(.system(size: 9).monospacedDigit())
                .foregroundStyle(.tertiary)
                .lineLimit(1)
                .truncationMode(.tail)
        }
        // Vertically fixed — the four lines are what they are. Never
        // horizontally: locking width would collapse the column back to the
        // digits and take the wide meter with it.
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(helpText)
    }

    /// A stale window shows a dash and **no fill**: the figure we hold did not
    /// merely age, it restarted at zero and ours is wrong. The track is still
    /// drawn so the chip keeps its height and the row keeps one baseline —
    /// vanishing would be indistinguishable from a broken feature.
    @ViewBuilder
    private var meter: some View {
        if let percent = bar.percent, !bar.stale {
            ZStack(alignment: .leading) {
                Rectangle().fill(Theme.hair.opacity(0.45))
                GeometryReader { geo in
                    Rectangle().fill(tint)
                        .frame(width: max(2, geo.size.width * min(percent, 100) / 100))
                }
            }
        } else {
            Rectangle().fill(Theme.hair.opacity(0.35))
        }
    }

    private var figure: String {
        UsageChipText.figure(percent: bar.percent, stale: bar.stale)
    }

    /// Short form on screen — "23 Aug 02:00" fits any chip where "Resets 23 Aug
    /// at 02:00" would not. The full sentence is in `help`.
    private var resetLabel: String {
        var text = "—"
        if let at = bar.resetsAt, !bar.stale {
            let date = Date(timeIntervalSince1970: at)
            let formatter = DateFormatter()
            formatter.dateFormat = Calendar.current.isDateInToday(date)
                ? "HH:mm" : "d MMM HH:mm"
            text = formatter.string(from: date)
        }
        // The line already refuses to wrap and trims with a trailing ellipsis,
        // so a chip too narrow for both loses the tail of the note rather than
        // changing height. The whole sentence is in `help` either way.
        if let note = UsageChipText.ageNote(asOf: bar.asOf, source: bar.source) {
            text += " · \(note)"
        }
        return text
    }

    private var helpText: String {
        var text = bar.title.isEmpty ? label : bar.title
        if bar.stale {
            text += " — this window has already reset, so the figure held is wrong, not old."
        } else if let at = bar.resetsAt {
            let date = Date(timeIntervalSince1970: at)
            let formatter = DateFormatter()
            formatter.dateFormat = Calendar.current.isDateInToday(date)
                ? "HH:mm" : "d MMM 'at' HH:mm"
            text += " — resets \(formatter.string(from: date))"
        }
        if let note = UsageChipText.ageNote(asOf: bar.asOf, source: bar.source) {
            text += " (\(note); the last refresh did not get through)"
        }
        return text
    }
}

extension UsageBar {
    /// Grok's weekly window. It does not come from `/api/usage` — the menu bar
    /// fetches it from a different account entirely — so it arrives over stdin
    /// and is dressed as a bar here so the two read alike.
    static func grok(percent: Double, resetsAt: Double? = nil) -> UsageBar {
        var bar = UsageBar()
        bar.kind = "grok_weekly"
        bar.provider = "grok"
        bar.title = "Grok, this week"
        bar.percent = percent
        bar.resetsAt = resetsAt
        return bar
    }

    init() {
        kind = ""
        provider = "claude"
        shortLabel = ""
        title = ""
        percent = nil
        resetsAt = nil
        stale = false
        asOf = nil
        source = ""
    }
}

/// The channel back to the menu bar.
///
/// Commands arrive on stdin; anything the panel wants done that only the menu
/// bar can do — rebuilding, restarting, quitting, and now every preference —
/// goes out on **stdout** as one JSON object per line. These are its business,
/// not the daemon's: they act on the app, and the daemon is a thread inside it.
enum Panel {
    /// `value` is the state a switch wants, not a request to flip whatever the
    /// app currently holds — the panel and the app can briefly disagree, and a
    /// flip resolves that disagreement the wrong way exactly when it matters.
    static func send(action: String, value: Any? = nil) {
        var payload: [String: Any] = ["event": "action", "name": action]
        if let value { payload["value"] = value }
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
}
