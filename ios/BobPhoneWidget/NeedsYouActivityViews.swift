import ActivityKit
import SwiftUI
import WidgetKit

/// The Lock Screen card and the Dynamic Island for the one agent at the top
/// of Needs you: the portrait, the nickname, the kind word, a clock counting
/// from when the row went quiet, and the card or session it is working.
///
/// A tap opens the review (`bobphone://review?run=`) while the card names a
/// review waiting on picks, the agent otherwise (`FleetLinks.waiter`).
///
/// Rendering only — ActivityKit drives every redraw, the app starts and
/// ends the activity and the Mac updates it by push; no timeline provider
/// and no network (`test_phone_widget.py` pins both). The portrait loads
/// off the appex bundle through `WidgetFacePortrait`, which draws the
/// initial on a miss — never a stranger's face. A stale card
/// (`context.isStale`: the Mac went quiet) is dimmed and its clock replaced
/// by a dash, so a frozen count never reads as a live one. A `kind` outside
/// the closed set draws "attention". Nothing here clips; every line wraps.
struct NeedsYouActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: NeedsYouAttributes.self) { context in
            NeedsYouLockScreenView(state: context.state, stale: context.isStale)
                .activityBackgroundTint(WidgetTheme.bg)
                .activitySystemActionForegroundColor(WidgetTheme.accent)
                .widgetURL(FleetLinks.waiter(kind: context.state.kind, runId: context.state.runId, sessionId: context.state.sessionId))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    if context.state.hasFace {
                        WidgetFacePortrait(slug: context.state.slug, size: 44)
                            .opacity(context.isStale ? 0.5 : 1)
                            .accessibilityHidden(true)
                    } else {
                        WidgetBrandMark(size: 36)
                            .opacity(context.isStale ? 0.5 : 1)
                    }
                }
                DynamicIslandExpandedRegion(.trailing) {
                    if context.state.hasFace {
                        NeedsYouClock(state: context.state, stale: context.isStale)
                    } else {
                        FleetFreshness(updated: context.state.updatedDate,
                                       stale: context.isStale)
                    }
                }
                DynamicIslandExpandedRegion(.center) {
                    if context.state.hasFace {
                        NeedsYouWords(state: context.state, stale: context.isStale)
                    } else {
                        FleetCountsRow(state: context.state, stale: context.isStale)
                    }
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 2) {
                        FleetFiguresLine(state: context.state, stale: context.isStale)
                        if !context.state.work.isEmpty {
                            Text(context.state.work)
                                .font(.system(size: 12))
                                .foregroundStyle(context.isStale ? WidgetTheme.muted : WidgetTheme.text)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            } compactLeading: {
                IslandCompactLeading(state: context.state, stale: context.isStale)
            } compactTrailing: {
                IslandCompactTrailing(state: context.state, stale: context.isStale)
            } minimal: {
                IslandMinimal(state: context.state, stale: context.isStale)
            }
            .widgetURL(FleetLinks.waiter(kind: context.state.kind, runId: context.state.runId, sessionId: context.state.sessionId))
            .keylineTint(WidgetTheme.attention)
        }
    }
}

/// The Lock Screen banner, drawn as a shell pane: the glitch mark and
/// `root@darkarmy:~/fleet$` with a live dot, a hairline, the waiting face
/// (portrait, words, clock, work line) when somebody waits, the three
/// counts with a glyph each, a hairline, then the last hour's burn in
/// bright ink beside the running totals in dim ink. Stretched to the full
/// width and led from the left — an unstretched stack is centred by iOS,
/// which left a blank gutter where the face would be. iOS caps the Lock
/// Screen card at 160pt and crops anything taller flush to the edge, so
/// the pane stays inside that in every state — an 80-character work line
/// on two lines, double-digit counts, the hourly rate — at the narrowest
/// phone: a 36pt face, the counts on one line, one hairline, 4pt between
/// rows and a real inset on all four sides. `NeedsYouLockScreenLayoutTests`
/// lays each state out and holds it under the ceiling. One spoken sentence
/// for VoiceOver.
struct NeedsYouLockScreenView: View {
    let state: NeedsYouAttributes.ContentState
    let stale: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                WidgetBrandMark(size: 12)
                WidgetPromptLine(path: "~/fleet", size: 10)
                Spacer(minLength: 8)
                FleetFreshness(updated: state.updatedDate, stale: stale)
            }
            if state.hasFace {
                HStack(alignment: .center, spacing: 12) {
                    WidgetFacePortrait(slug: state.slug, size: 36)
                        .accessibilityHidden(true)
                    NeedsYouWords(state: state, stale: stale)
                    Spacer(minLength: 8)
                    NeedsYouClock(state: state, stale: stale)
                }
                if !state.work.isEmpty {
                    Text(state.work)
                        .font(.system(size: 11))
                        .foregroundStyle(stale ? WidgetTheme.muted : WidgetTheme.text)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            FleetCountsRow(state: state, stale: stale, inline: true)
            FleetRule()
            FleetFiguresLine(state: state, stale: stale)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 18)
        .padding(.vertical, 13)
        .opacity(stale ? 0.6 : 1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(NeedsYouSpoken.label(state: state, stale: stale))
    }
}

/// The island's left pill: always the mask, so the pill says Dark Army at a
/// glance, then the waiting face when somebody waits, else the working
/// count behind the Lock Screen's play glyph.
struct IslandCompactLeading: View {
    let state: NeedsYouAttributes.ContentState
    let stale: Bool

    var body: some View {
        HStack(spacing: 4) {
            WidgetBrandMark(size: 16)
                .opacity(stale ? 0.5 : 1)
            if state.hasFace {
                WidgetFacePortrait(slug: state.slug, size: 20)
                    .clipShape(Circle())
                    .accessibilityHidden(true)
            } else {
                IslandCount(value: state.working, glyph: "play.fill",
                            alarm: false, stale: stale)
            }
        }
    }
}

/// The island's right pill: the wait clock when somebody waits, else the
/// need-you count behind the raised hand, alarm ink only above zero.
struct IslandCompactTrailing: View {
    let state: NeedsYouAttributes.ContentState
    let stale: Bool

    var body: some View {
        if state.hasFace {
            NeedsYouClock(state: state, stale: stale, size: 12)
        } else {
            IslandCount(value: state.needsYou, glyph: "hand.raised.fill",
                        alarm: (state.needsYou ?? 0) > 0, stale: stale)
        }
    }
}

/// The island's single dot, shared with another app: the waiting face, the
/// need-you count while anybody needs you, else the mask.
struct IslandMinimal: View {
    let state: NeedsYouAttributes.ContentState
    let stale: Bool

    var body: some View {
        if state.hasFace {
            WidgetFacePortrait(slug: state.slug, size: 20)
                .clipShape(Circle())
                .accessibilityHidden(true)
        } else if let waiting = state.needsYou, waiting > 0 {
            FleetCountMark(value: waiting, alarm: true, stale: stale)
        } else {
            WidgetBrandMark(size: 20)
                .opacity(stale ? 0.5 : 1)
        }
    }
}

/// A glyph and a number, the Lock Screen count row's cell in miniature.
struct IslandCount: View {
    let value: Int?
    let glyph: String
    let alarm: Bool
    let stale: Bool

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: glyph)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(alarm && !stale ? WidgetTheme.attention : WidgetTheme.muted)
                .accessibilityHidden(true)
            FleetCountMark(value: value, alarm: alarm, stale: stale)
        }
    }
}

/// A one-point Signal hairline, the pane's row divider.
struct FleetRule: View {
    var body: some View {
        Rectangle()
            .fill(WidgetTheme.line)
            .frame(height: 1)
            .accessibilityHidden(true)
    }
}

/// How old the card is: "3 min ago", counted by the system from the last
/// refresh, in whole minutes on iOS 18 (iOS 17's relative style also counts
/// seconds, "3 min, 12 sec ago" — accepted there). It replaced "live",
/// which a card could wear for half an hour without a word from the Mac.
/// The dot is green until the card goes stale, grey after; the words carry
/// the meaning, so colour is never the only signal. An unstamped card (an
/// older relay) draws a dash rather than a guess.
struct FleetFreshness: View {
    let updated: Date?
    let stale: Bool

    var body: some View {
        HStack(spacing: 4) {
            Circle()
                .fill(stale ? WidgetTheme.muted : WidgetTheme.accent)
                .frame(width: 6, height: 6)
            age
                .font(.system(size: 10))
                .foregroundStyle(WidgetTheme.muted)
        }
        .accessibilityHidden(true)
    }

    @ViewBuilder private var age: some View {
        if let updated {
            if #available(iOS 18.0, *) {
                Text(.currentDate, format: .reference(to: updated,
                                                      allowedFields: [.hour, .minute],
                                                      maxFieldCount: 1))
            } else {
                Text("\(Text(updated, style: .relative)) ago")
            }
        } else {
            Text("\u{2013}")
        }
    }
}

/// The nickname and the kind word. The word is spelled, never colour alone.
struct NeedsYouWords: View {
    let state: NeedsYouAttributes.ContentState
    let stale: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(state.nickname.isEmpty ? "an agent" : state.nickname)
                .font(.system(size: 15, weight: .bold))
                .foregroundStyle(stale ? WidgetTheme.muted : WidgetTheme.text)
                .fixedSize(horizontal: false, vertical: true)
            Text(state.kindWord)
                .font(.system(size: 12))
                .foregroundStyle(stale ? WidgetTheme.muted : WidgetTheme.attention)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// The running wait clock, counting up from the moment the row went quiet;
/// a dash once the card is stale.
struct NeedsYouClock: View {
    let state: NeedsYouAttributes.ContentState
    let stale: Bool
    /// The island's pill passes 12: an outer `.font` cannot shrink it,
    /// because the inner one wins.
    var size: CGFloat = 15

    var body: some View {
        if stale || state.since <= 0 {
            Text("\u{2013}")
                .font(.system(size: size, design: .monospaced))
                .foregroundStyle(WidgetTheme.muted)
        } else {
            Text(state.sinceDate, style: .timer)
                .font(.system(size: size, design: .monospaced))
                .monospacedDigit()
                .foregroundStyle(WidgetTheme.accent)
                .multilineTextAlignment(.trailing)
        }
    }
}

/// One sentence for a screen reader, assembled only from what the card
/// draws — the system reads the timer itself, so it is not restated.
/// Three counts: a glyph and a number over a word. A nil count is a dash,
/// never zero. The needs-you number and glyph are alarm ink only while it
/// is above zero; the word is always spelled, so colour is never alone.
struct FleetCountsRow: View {
    let state: NeedsYouAttributes.ContentState
    let stale: Bool
    /// The Lock Screen draws each count on one line — glyph, number, word —
    /// to stay inside the card's height; the island keeps the stacked cell.
    var inline: Bool = false

    var body: some View {
        HStack(alignment: inline ? .firstTextBaseline : .top, spacing: inline ? 12 : 18) {
            cell(state.working, "working", glyph: "play.fill", alarm: false)
            cell(state.needsYou, "need you", glyph: "hand.raised.fill",
                 alarm: (state.needsYou ?? 0) > 0)
            cell(state.standingBy, "standing by", glyph: "moon.zzz.fill", alarm: false)
        }
    }

    private func cell(_ value: Int?, _ word: String, glyph: String,
                      alarm: Bool) -> some View {
        let layout = inline
            ? AnyLayout(HStackLayout(alignment: .firstTextBaseline, spacing: 4))
            : AnyLayout(VStackLayout(alignment: .leading, spacing: 1))
        return layout {
            HStack(spacing: 4) {
                Image(systemName: glyph)
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(alarm && !stale ? WidgetTheme.attention : WidgetTheme.muted)
                    .accessibilityHidden(true)
                Text(value.map(String.init) ?? "\u{2013}")
                    .font(.system(size: inline ? 14 : 17, weight: .bold, design: .monospaced))
                    .foregroundStyle(ink(alarm: alarm))
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(word)
                .font(.system(size: 10))
                .foregroundStyle(WidgetTheme.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func ink(alarm: Bool) -> Color {
        if stale { return WidgetTheme.muted }
        return alarm ? WidgetTheme.attention : WidgetTheme.text
    }
}

/// One count, for the island's compact and minimal slots.
struct FleetCountMark: View {
    let value: Int?
    let alarm: Bool
    let stale: Bool

    var body: some View {
        Text(value.map(String.init) ?? "\u{2013}")
            .font(.system(size: 15, weight: .bold, design: .monospaced))
            .foregroundStyle(stale ? WidgetTheme.muted : (alarm ? WidgetTheme.attention : WidgetTheme.text))
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// The last hour's burn, bright, then the running totals, dim. Before the
/// Mac has watched long enough to say a rate, the totals take the bright
/// slot alone. The cost is private: the person's Lock Screen setting can
/// hide it. The words live on the content state.
struct FleetFiguresLine: View {
    let state: NeedsYouAttributes.ContentState
    let stale: Bool

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if state.hasRate {
                Image(systemName: "bolt.fill")
                    .font(.system(size: 10))
                    .foregroundStyle(stale ? WidgetTheme.muted : WidgetTheme.text)
                    .accessibilityHidden(true)
                if let cost = state.costRateWord {
                    Text(cost)
                        .privacySensitive()
                        .font(.system(size: 13, weight: .bold, design: .monospaced))
                        .foregroundStyle(stale ? WidgetTheme.muted : WidgetTheme.text)
                }
                if let tokens = state.tokensRateWord {
                    Text(tokens)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundStyle(stale ? WidgetTheme.muted : WidgetTheme.text)
                }
                Spacer(minLength: 6)
                Text("\(state.costWord) \u{00B7} \(state.tokensWord)")
                    .privacySensitive()
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(WidgetTheme.muted)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(state.costWord)
                    .privacySensitive()
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(stale ? WidgetTheme.muted : WidgetTheme.text)
                    .fixedSize(horizontal: false, vertical: true)
                Text("\u{00B7}")
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(WidgetTheme.muted)
                Text(state.tokensWord)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(stale ? WidgetTheme.muted : WidgetTheme.text)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

enum NeedsYouSpoken {
    static func label(state: NeedsYouAttributes.ContentState, stale: Bool) -> String {
        var parts: [String] = []
        if state.hasFace {
            parts.append(state.nickname.isEmpty ? "an agent" : state.nickname)
            parts.append("needs you")
            parts.append(state.kindWord)
            if !state.work.isEmpty { parts.append(state.work) }
        }
        parts.append("\(state.working.map(String.init) ?? "\u{2013}") working")
        parts.append("\(state.needsYou.map(String.init) ?? "\u{2013}") need you")
        parts.append("\(state.standingBy.map(String.init) ?? "\u{2013}") standing by")
        if let rate = state.costRateWord {
            parts.append("\(rate.replacingOccurrences(of: "/h", with: "")) an hour")
        }
        parts.append(state.costWord)
        if stale { parts.append("last heard a while ago") }
        return parts.joined(separator: ", ")
    }
}
