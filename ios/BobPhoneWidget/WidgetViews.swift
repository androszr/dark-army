import SwiftUI
import UIKit
import WidgetKit

/// The widget's semantic roles use the same generated Signal tokens as the
/// phone. WidgetKit keeps its own compact typography and native accessory ink.
enum WidgetTheme {
    static let bg = SignalTokens.surface
    static let text = SignalTokens.text
    static let muted = SignalTokens.muted
    static let accent = SignalTokens.accent
    static let attention = SignalTokens.attention
    static let danger = SignalTokens.danger
    static let line = SignalTokens.line

    /// The usage steps, `menu_format.USAGE_WARN_PERCENT` /
    /// `USAGE_CRIT_PERCENT` — the panel formats its own rows in Swift and
    /// so does the widget; the constants are the contract, pinned by
    /// `test_phone_widget.py`. Colour is an exception signal: neutral ink
    /// to 75, amber to 90, red above.
    static let warnPercent = 75.0
    static let critPercent = 90.0

    /// The medium tile's three columns, decided by numbers rather than by a
    /// `GeometryReader` — a widget's content width is known and a reader
    /// inside a fixed container is a second source of truth.
    ///
    /// `329 − 120 − 84 − 2×12 = 101 ≥ 96`: the count column keeps 19pt of
    /// slack over its widest realistic line, and even a three-digit
    /// `NEEDS YOU` (~92pt) fits. The spacing came down from 14 to buy that;
    /// do not tidy it back without redoing the arithmetic.
    static let mediumContentWidth: CGFloat = 329
    static let usageColumnWidth: CGFloat = 120
    static let faceColumnWidth: CGFloat = 84
    static let columnSpacing: CGFloat = 12
    static let minCountColumnWidth: CGFloat = 96

    static func meterColor(_ percent: Double) -> Color {
        if percent >= critPercent { return danger }
        if percent >= warnPercent { return attention }
        return text
    }
}

/// A summary older than its own `dimAfter` draws dimmed even on the
/// timeline's first entry — the app wrote it, went away, and something
/// else reloaded us.

struct FleetWidgetView: View {
    @Environment(\.widgetFamily) private var family
    let entry: FleetEntry

    private var stale: Bool {
        guard let summary = entry.summary else { return false }
        if entry.dimmed { return true }
        return Date().timeIntervalSince1970 - summary.generatedAt > summary.dimAfter
    }

    var body: some View {
        familyBackground {
            if let summary = entry.summary {
                Group {
                    if family == .accessoryCircular {
                        CircularFleetView(summary: summary, stale: stale)
                    } else if family == .systemMedium {
                        MediumFleetView(summary: summary, stale: stale,
                                        face: entry.face, date: entry.date)
                            .opacity(stale ? 0.55 : 1.0)
                    } else {
                        SmallFleetView(summary: summary, stale: stale)
                            .opacity(stale ? 0.55 : 1.0)
                    }
                }
            } else if family == .accessoryCircular {
                CircularEmptyView()
            } else {
                // Never heard from the Mac: no counts to fake, one instruction.
                VStack(spacing: 6) {
                    WidgetBrandMark(size: 20)
                    // The tile's only content, so it speaks — the medium
                    // header's prompt does not.
                    WidgetPromptLine(path: "~", command: "open Dark Army",
                                     commandColor: WidgetTheme.muted, size: 11)
                }
                .widgetURL(URL(string: "bobphone://needs"))
            }
        }
    }

    @ViewBuilder
    private func familyBackground<Content: View>(
        @ViewBuilder content: () -> Content
    ) -> some View {
        if family == .accessoryCircular {
            content()
                .containerBackground(for: .widget) { AccessoryWidgetBackground() }
        } else {
            content()
                .containerBackground(WidgetTheme.bg, for: .widget)
        }
    }
}

/// Dark Army's mark on the tile — the same PNG the phone's `BrandMark` draws,
/// loaded through `Bundle.main`, which in an app extension *is* the appex.
/// The brand folder is in the widget's own Resources phase for exactly that
/// reason; without it the loader returns nil and the tile simply draws no
/// mark. No framed square: the tile's container background is already
/// `WidgetTheme.bg`, so the phone's box-and-hairline would be invisible
/// chrome.
struct WidgetBrandMark: View {
    var size: CGFloat = 14

    var body: some View {
        Group {
            if let image = Self.image {
                Image(uiImage: image)
                    .interpolation(.high)
                    .resizable()
                    .scaledToFit()
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    /// One read per extension process, at first draw — never per-render.
    /// A widget has ~30 MB and no budget for disk in a body. It reads the
    /// 96px copy, never the 768px master: a Live Activity draws an image
    /// that large as a grey box on the Lock Screen.
    private static let image: UIImage? = {
        guard let url = Bundle.main.url(forResource: "brand", withExtension: nil)?
                .appendingPathComponent("fsociety-mark-widget.png")
        else { return nil }
        return UIImage(contentsOfFile: url.path)
    }()
}

/// `root@darkarmy:<path>$ [command]`, mirroring the phone's and the panel's
/// `PromptLine`. No block cursor — a blink is a clock.
///
/// One parameter the originals lack: `sensitive`, which redacts the command
/// on the lock screen. The waiting count is a number the tile already hides
/// there, and the words that restate it must hide with it.
struct WidgetPromptLine: View {
    var path: String
    var command: String = ""
    var commandColor: Color? = nil
    var sensitive: Bool = false
    var size: CGFloat = 10

    var body: some View {
        HStack(spacing: 0) {
            Text("root@darkarmy:")
                .foregroundStyle(WidgetTheme.accent)
            Text(path)
                .foregroundStyle(WidgetTheme.text)
            Text("$")
                .foregroundStyle(WidgetTheme.accent)
            if !command.isEmpty {
                let words = Text(" \(command)")
                    .foregroundStyle(commandColor ?? WidgetTheme.text)
                if sensitive {
                    words.privacySensitive()
                } else {
                    words
                }
            }
        }
        .font(.system(size: size, design: .monospaced))
        .lineLimit(1)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spoken)
    }

    private var spoken: String {
        command.isEmpty ? "root@darkarmy:\(path)$" : "root@darkarmy:\(path)$ \(command)"
    }
}

/// systemSmall: the needs-you count, big, with the working count under it.
/// The whole tile deep-links to the Needs-you tab (behind the face check).
struct SmallFleetView: View {
    let summary: FleetSummary
    let stale: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                // The mark alone: 158pt would truncate a prompt mid-path.
                WidgetBrandMark(size: 12)
                Text("NEEDS YOU")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(WidgetTheme.muted)
            }
            Text("\(summary.needsYou)")
                .font(.system(size: 40, weight: .bold, design: .monospaced))
                .foregroundStyle(summary.needsYou > 0
                                 ? WidgetTheme.attention : WidgetTheme.text)
                .privacySensitive()
                // VoiceOver reads the header and the bare digit as two
                // unrelated utterances; one sentence says what the number is.
                .accessibilityLabel("\(summary.needsYou) need you")
            Text("\(summary.working) working")
                .font(.system(size: 11))
                .foregroundStyle(WidgetTheme.text)
                .privacySensitive()
                .accessibilityLabel("\(summary.working) working")
            Spacer(minLength: 0)
            AgeLine(summary: summary, stale: stale)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .widgetURL(URL(string: "bobphone://needs"))
    }
}

/// systemMedium: the three counts, then one meter per provider — that
/// provider's highest non-stale window, the number a person would act on.
/// The meter zone links to the Usage tab; everything else keeps the tile's
/// own needs-you link.
struct MediumFleetView: View {
    let summary: FleetSummary
    let stale: Bool
    /// **Absent, not empty**: an older app's note carries no roster, the
    /// provider hands down `nil`, and the middle column is not in the layout
    /// at all — the count column simply widens back to what it has today.
    /// The tile must never fall through to the "open Dark Army" placeholder
    /// for a missing roster; that sentence means "never heard from the Mac".
    let face: FleetSummary.Face?
    /// The entry's own date, not `Date()` — the slogan must be a pure
    /// function of the timeline entry, or two renderings of one entry say
    /// different things.
    let date: Date

    /// Mirrors `BrandBar.subtitle`, with `stale` in `offline`'s slot — the
    /// exceptional state first. Read off the same summary the counts below
    /// read; never a second derivation of attention.
    private var prompt: (command: String, color: Color, sensitive: Bool) {
        if stale { return ("stale", WidgetTheme.muted, false) }
        if summary.needsYou > 0 {
            return ("\(summary.needsYou) need you", WidgetTheme.attention, true)
        }
        return ("Companion", WidgetTheme.text, false)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            // Identity chrome: the count lines and the age line below already
            // speak every fact this states, so it adds no second voice.
            HStack(spacing: 6) {
                WidgetBrandMark(size: 14)
                WidgetPromptLine(path: "~/board", command: prompt.command,
                                 commandColor: prompt.color,
                                 sensitive: prompt.sensitive, size: 10)
                Spacer(minLength: 0)
            }
            .accessibilityHidden(true)
            Rectangle()
                .fill(WidgetTheme.line)
                .frame(height: 1)
            HStack(alignment: .top, spacing: WidgetTheme.columnSpacing) {
                VStack(alignment: .leading, spacing: 4) {
                    CountLine(label: "NEEDS YOU", value: summary.needsYou,
                              alarmed: summary.needsYou > 0)
                    CountLine(label: "WORKING", value: summary.working,
                              alarmed: false)
                    CountLine(label: "TO-DO", value: summary.todo, alarmed: false)
                    Spacer(minLength: 0)
                    AgeLine(summary: summary, stale: stale)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                // A face with a session behind it is a tap target for that
                // agent; the idle cast stays plain chrome.
                if let face {
                    if let url = FleetLinks.agent(face.sessionId) {
                        Link(destination: url) { WidgetFaceColumn(face: face, date: date) }
                    } else {
                        WidgetFaceColumn(face: face, date: date)
                    }
                }
                if !summary.bars.isEmpty {
                    Link(destination: URL(string: "bobphone://usage")!) {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(summary.bars, id: \.provider) { bar in
                                ProviderMeter(bar: bar)
                            }
                            Spacer(minLength: 0)
                        }
                    }
                    .frame(width: WidgetTheme.usageColumnWidth)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .widgetURL(URL(string: "bobphone://needs"))
    }
}

struct CountLine: View {
    let label: String
    let value: Int
    let alarmed: Bool

    var body: some View {
        HStack(spacing: 6) {
            Text("\(value)")
                .font(.system(size: 18, weight: .bold, design: .monospaced))
                .foregroundStyle(alarmed ? WidgetTheme.attention
                                         : WidgetTheme.text)
                .privacySensitive()
            Text(label)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(WidgetTheme.muted)
        }
        // One utterance per line — "3 need you", never a bare "3" followed
        // by an unrelated "NEEDS YOU".
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label == "NEEDS YOU"
                            ? "\(value) need you"
                            : "\(value) \(label.lowercased())")
    }
}

struct ProviderMeter: View {
    let bar: FleetSummary.Bar

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Text(bar.provider.uppercased())
                    .font(.system(size: 9, weight: .semibold))
                    .foregroundStyle(WidgetTheme.muted)
                Text(bar.stale ? "–" : "\(Int(bar.percent))%")
                    .font(.system(size: 11, weight: .semibold,
                                  design: .monospaced))
                    .foregroundStyle(bar.stale ? WidgetTheme.muted
                                     : WidgetTheme.meterColor(bar.percent))
                    .privacySensitive()
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Rectangle()
                        .fill(WidgetTheme.line)
                    if !bar.stale {
                        Rectangle()
                            .fill(WidgetTheme.meterColor(bar.percent))
                            .frame(width: geo.size.width
                                   * min(1.0, max(0.0, bar.percent / 100.0)))
                    }
                }
            }
            .frame(height: 3)
        }
    }
}

/// "4m ago", counting itself up — `style: .relative` is redrawn by the
/// system, so the age stays current without a single timeline reload. The
/// date is the summary's `generatedAt` — when the app last heard from the
/// Mac — not when the widget last reloaded; the two drift apart the moment
/// the app closes, and the age line reports the honest one. The line turns
/// amber once the tile is admitting staleness.
struct AgeLine: View {
    let summary: FleetSummary
    let stale: Bool

    var body: some View {
        Text(Date(timeIntervalSince1970: summary.generatedAt),
             style: .relative)
            .font(.system(size: 9, design: .monospaced))
            .foregroundStyle(stale ? WidgetTheme.attention : WidgetTheme.muted)
    }
}


/// The middle column of the wide tile: a still portrait, the 2pt state rule
/// under it, who this is and what they are doing, and a slogan.
///
/// One VoiceOver element with one composed sentence, exactly as `CountLine`
/// does. `children: .ignore` already silences the portrait and both lines —
/// **nothing here is hidden from VoiceOver by hand**; this file's hidden
/// count is pinned at two (the brand mark and the medium header).
struct WidgetFaceColumn: View {
    let face: FleetSummary.Face
    let date: Date

    private var slogan: String { WidgetSlogans.line(for: date) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            WidgetFacePortrait(slug: face.slug, size: 44)
            WidgetStateRule(rule: face.rule, width: 44)
            // The words that name a waiting agent are the words the counts
            // hide on the lock screen, so they hide with them.
            Text("\(face.name) · \(face.doing)")
                .font(.system(size: 9))
                .foregroundStyle(WidgetTheme.text)
                .privacySensitive()
            Text(slogan)
                .font(.system(size: 8))
                .foregroundStyle(WidgetTheme.muted)
            Spacer(minLength: 0)
        }
        .frame(width: WidgetTheme.faceColumnWidth, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(face.name), \(face.doing). \(slogan)")
    }
}

/// One portrait, loaded through `Bundle.main` — **which in an appex is the
/// appex**, which is why the `portraits` folder is in the widget's own
/// Resources phase. The panel's SwiftPM resource accessor compiles here and
/// finds nothing at runtime: every face silently blank, and no gate in this
/// repo would notice.
///
/// The full-size PNG is 512×512 and decodes to about a megabyte; the
/// extension has about thirty, and a dozen rendered entries holding a dozen
/// of those is how a widget is jetsammed. So the loader reduces to a 3×
/// thumbnail immediately and caches **only that**, letting the full decode
/// go out of scope. Misses are cached too, as `Cast.portrait(_:)` does.
///
/// A slug with no art draws its initial on a plain tile — the phone's and
/// the panel's rule: never a hashed stranger.
struct WidgetFacePortrait: View {
    let slug: String
    var size: CGFloat = 44

    var body: some View {
        Group {
            if let image = Self.portrait(slug) {
                Image(uiImage: image)
                    .resizable()
                    .interpolation(.high)
                    .scaledToFill()
            } else {
                ZStack {
                    Rectangle().fill(WidgetTheme.line)
                    Text(String(slug.prefix(1)).uppercased())
                        .font(.system(size: size * 0.45, weight: .bold,
                                      design: .monospaced))
                        .foregroundStyle(WidgetTheme.text)
                }
            }
        }
        .frame(width: size, height: size)
        .clipped()
    }

    private static var cache: [String: UIImage?] = [:]

    private static func portrait(_ slug: String) -> UIImage? {
        let key = slug.lowercased()
        if let hit = cache[key] { return hit }
        let url = Bundle.main.url(forResource: "portraits", withExtension: nil)?
            .appendingPathComponent("\(key).png")
        let thumbnail = url
            .flatMap { UIImage(contentsOfFile: $0.path) }?
            .preparingThumbnail(of: CGSize(width: 132, height: 132))
        cache[key] = thumbnail
        return thumbnail
    }
}

/// The 2pt line `Cast.StateRule` draws, restated the way `WidgetTheme`
/// restates the inks — the widget target compiles neither `Cast.swift` nor
/// `Theme.swift`. **Form before colour**: solid for work, solid for alert,
/// dashed for sleep and for anything this build does not recognise. The
/// words above already say the state, so colour is never alone.
struct WidgetStateRule: View {
    let rule: String
    var width: CGFloat = 44

    var body: some View {
        Path { path in
            path.move(to: CGPoint(x: 0, y: 1))
            path.addLine(to: CGPoint(x: width, y: 1))
        }
        .stroke(ink, style: StrokeStyle(lineWidth: 2, dash: dash))
        .frame(width: width, height: 2)
    }

    private var ink: Color {
        switch rule {
        case "work": return WidgetTheme.accent
        case "alert": return WidgetTheme.attention
        default: return WidgetTheme.muted
        }
    }

    private var dash: [CGFloat] {
        switch rule {
        case "work", "alert": return []
        default: return [3, 2]
        }
    }
}

/// The slogans, shipped in code with a stated bound — the precedent set by
/// the panel's and the phone's waiting lines, and deliberately **not** a
/// copy of them. That pair is byte-pinned between the two by
/// `host/tests/test_agent_chatter.py`, and a third copy in a target neither
/// test reads is exactly the drift those pins exist to stop.
enum WidgetSlogans {
    /// Wider than this and the line wraps past two rows in an 84pt column.
    static let maxLineChars = 22

    static let lines = [
        "hello, friend.",
        "we are fsociety.",
        "control is illusion.",
        "we do not forgive.",
        "we do not forget.",
        "expect us.",
        "bonsoir, friend.",
        "join us.",
        "power belongs to all.",
        "the world is a stage.",
        "delete your debt.",
        "our democracy, hacked.",
    ]

    /// Picked by the same hour bucket as the face but on its own modulus, so
    /// the two do not lock in step and repeat as a pair.
    static func line(for date: Date) -> String {
        let hour = Int(date.timeIntervalSince1970 / 3600)
        return lines[((hour % lines.count) + lines.count) % lines.count]
    }
}
