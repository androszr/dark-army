import SwiftUI
import WidgetKit

/// The Lock Screen circular tile. Meaning is carried by form — tick
/// length and width, the centre caption word, the number's weight —
/// because vibrant mode flattens every colour to one white material.
struct CircularFleetView: View {
    let summary: FleetSummary
    let stale: Bool

    private var centre: TickRing.Centre {
        TickRing.centre(needsYou: summary.needsYou,
                        working: summary.working,
                        stale: stale)
    }

    var body: some View {
        ZStack {
            Canvas { context, size in
                let live = max(0, summary.needsYou) + max(0, summary.working)
                let r = min(size.width, size.height) / 2 - 2
                let cx = size.width / 2
                let cy = size.height / 2
                for tick in TickRing.ticks(needsYou: summary.needsYou,
                                           working: summary.working) {
                    let length = CGFloat(tick.heavy
                                         ? TickRing.heavyLength
                                         : TickRing.hairLength)
                    let width = CGFloat(tick.heavy
                                        ? TickRing.heavyWidth(live: live)
                                        : TickRing.hairWidth(live: live))
                    let angle = (tick.degrees - 90) * Double.pi / 180
                    let inner = r - length
                    let dx = CGFloat(cos(angle))
                    let dy = CGFloat(sin(angle))
                    var path = Path()
                    path.move(to: CGPoint(x: cx + inner * dx,
                                          y: cy + inner * dy))
                    path.addLine(to: CGPoint(x: cx + r * dx,
                                             y: cy + r * dy))
                    context.stroke(
                        path,
                        with: .color(.primary),
                        style: StrokeStyle(lineWidth: width, lineCap: .round))
                }
            }
            VStack(spacing: 0) {
                // Counts stay readable on a locked phone on purpose: this
                // view is the Lock Screen tile, and redacting the counts
                // here would blank the circle permanently. Two small
                // integers, no project or session name.
                Text(centre.text)
                    .font(.system(size: 22,
                                  weight: centre.waiting ? .bold : .regular,
                                  design: .monospaced))
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                Text(centre.caption)
                    .font(.system(size: 7, weight: .semibold,
                                  design: .monospaced))
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
            }
        }
        .opacity(stale ? 0.55 : 1.0)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(TickRing.spoken(needsYou: summary.needsYou,
                                            working: summary.working,
                                            stale: stale))
        .widgetURL(URL(string: "bobphone://needs"))
    }
}

/// Never-heard-from-the-Mac placeholder: the mark alone, so the circle
/// is never blank.
struct CircularEmptyView: View {
    var body: some View {
        WidgetBrandMark(size: 22)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Dark Army, open the app")
            .widgetURL(URL(string: "bobphone://needs"))
    }
}
