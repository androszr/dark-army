import Foundation

// How close Claude's usage limits ran, shared by the Mac and the phone.
//
// Everything from the `enum LimitPressure {` line down is copied byte for byte
// into `ios/BobPhone/LimitPressure.swift` and pinned by
// `host/tests/test_limit_pressure.py`: edit both copies together, never one.
// The Mac's History and the phone's History screen both fold the daemon's
// `limits_report` series through it, so the two draw the same columns.

/// The limit chart's arithmetic: which column a reading lands in, the peak of
/// each column, where a five-hour reset falls, and the words that say it.
/// Foundation only — no views, no report types, no theme.
enum LimitPressure {
    static let dash = "—"
    /// Claude's five-hour window, in seconds: resets are marked only where a
    /// column is no wider than this.
    static let resetWindow = 5.0 * 3600

    /// One bucket of the daemon's series. Absent is nil, never 0: an
    /// unmeasured window drawn as 0% is a measurement nobody made.
    struct Point: Equatable {
        var ts: Double
        var fiveHour: Double?
        var sevenDay: Double?
    }

    /// One slice of the chart's x-axis. `fiveHour` and `sevenDay` are the
    /// highest readings inside the slice, nil where nothing was measured.
    struct Column: Equatable {
        var start: Double
        var end: Double
        var fiveHour: Double?
        var sevenDay: Double?
        var reset: Bool
    }

    struct Picture: Equatable {
        var columns: [Column]
        var fiveHourPeak: Double?
        var sevenDayPeak: Double?
        var headline: String
        var spoken: String
    }

    /// One column per two hours, between 12 and 84: a week is 84 on both
    /// clients, a day is 12, and a month or more stays 84.
    static func columnCount(from: Double, to: Double) -> Int {
        let hours = (to - from) / 3600
        if !(hours > 0) { return 12 }
        let wanted = (hours / 2).rounded()
        return Int(min(84, max(12, wanted)))
    }

    /// Folds the daemon's series into columns over `[from, to]`. The last slice
    /// includes `to`. A column's value is the maximum of its readings (the peak
    /// rule the daemon's buckets use); an empty slice is nil, never 0. Nil when
    /// there is no window or nothing measured inside it.
    static func picture(points: [Point], resets: [Double],
                        from: Double?, to: Double?) -> Picture? {
        guard let from = from, let to = to, from < to else { return nil }
        let count = columnCount(from: from, to: to)
        let width = (to - from) / Double(count)
        func index(_ ts: Double) -> Int? {
            if ts < from || ts > to { return nil }
            return min(count - 1, Int((ts - from) / width))
        }
        var fiveHour = [Double?](repeating: nil, count: count)
        var sevenDay = [Double?](repeating: nil, count: count)
        var marked = [Bool](repeating: false, count: count)
        for point in points {
            guard let i = index(point.ts) else { continue }
            if let value = point.fiveHour {
                fiveHour[i] = max(fiveHour[i] ?? value, value)
            }
            if let value = point.sevenDay {
                sevenDay[i] = max(sevenDay[i] ?? value, value)
            }
        }
        // A reset line says "a window rolled over here"; in a column wider
        // than one five-hour window nearly every column would carry one.
        if width <= resetWindow {
            for instant in resets {
                if let i = index(instant) { marked[i] = true }
            }
        }
        let fiveHourPeak = fiveHour.compactMap { $0 }.max()
        let sevenDayPeak = sevenDay.compactMap { $0 }.max()
        if fiveHourPeak == nil && sevenDayPeak == nil { return nil }
        let columns = (0..<count).map { i in
            Column(start: from + Double(i) * width,
                   end: i == count - 1 ? to : from + Double(i + 1) * width,
                   fiveHour: fiveHour[i], sevenDay: sevenDay[i],
                   reset: marked[i])
        }
        var parts: [String] = []
        var said: [String] = []
        if let peak = fiveHourPeak {
            parts.append("5h peak \(percent(peak))")
            said.append("five-hour limit peaked at \(spokenPercent(peak))")
        }
        if let peak = sevenDayPeak {
            parts.append("7d peak \(percent(peak))")
            said.append(fiveHourPeak == nil
                ? "seven-day limit peaked at \(spokenPercent(peak))"
                : "seven-day at \(spokenPercent(peak))")
        }
        let sentence = "Claude's " + said.joined(separator: "; ")
        return Picture(columns: columns, fiveHourPeak: fiveHourPeak,
                       sevenDayPeak: sevenDayPeak,
                       headline: parts.joined(separator: " · "),
                       spoken: sentence)
    }

    /// 0...1 of a 0–100% track, always: the scale is never stretched to the
    /// window's own peak. Nil stays nil.
    static func height(_ pct: Double?) -> Double? {
        guard let pct = pct else { return nil }
        return min(1, max(0, pct / 100))
    }

    /// "97%", or a dash where nothing was measured.
    static func percent(_ pct: Double?) -> String {
        guard let pct = pct else { return dash }
        return "\(Int(pct.rounded()))%"
    }

    private static func spokenPercent(_ pct: Double) -> String {
        "\(Int(pct.rounded())) percent"
    }
}
