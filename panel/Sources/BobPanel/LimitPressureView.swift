import SwiftUI

/// How close Claude's budgets ran, drawn from the shared `LimitPressure` fold:
/// the peaks in words, then one column per slice of the range on a fixed 0–100%
/// track. A five-hour bar, a seven-day tick, a hairline where a window reset.
/// An unmeasured slice is an empty track, never a zero-height bar. No amber
/// (that colour means "your turn") and no alarm (that means "needs you").
struct LimitPressureView: View {
    let picture: LimitPressure.Picture

    /// The track's height; a bar is `LimitPressure.height × trackHeight`.
    static let trackHeight: CGFloat = 56

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(picture.headline)
                .font(Theme.mono(11))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .bottom, spacing: 1) {
                ForEach(Array(picture.columns.enumerated()), id: \.offset) { _, column in
                    columnView(column)
                }
            }
            .frame(height: Self.trackHeight)
            HStack(spacing: 12) {
                Text("5h budget").foregroundStyle(Theme.phosphor)
                Text("7d budget").foregroundStyle(Theme.phosphorBright)
                Text("| reset").foregroundStyle(Theme.dim)
            }
            .font(Theme.mono(10))
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(picture.spoken)
    }

    private func columnView(_ column: LimitPressure.Column) -> some View {
        ZStack(alignment: .bottom) {
            Color.clear
            if let h = LimitPressure.height(column.fiveHour) {
                Rectangle().fill(Theme.phosphor)
                    .frame(height: max(1, CGFloat(h) * Self.trackHeight))
            }
            if let h = LimitPressure.height(column.sevenDay) {
                Rectangle().fill(Theme.phosphorBright)
                    .frame(height: 2)
                    .padding(.bottom, max(0, CGFloat(h) * Self.trackHeight - 2))
            }
            if column.reset {
                Rectangle().fill(Theme.hair)
                    .frame(width: 1)
                    .frame(maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity)
    }
}
