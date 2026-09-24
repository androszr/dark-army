import SwiftUI

/// The Mac's access log, read once on open through `Client.accessLogReport`:
/// every refused knock on the phone doors, newest first — when, which door,
/// where from and why — with the open burst alerts on top. Read-only: the
/// acknowledgement lives on the Needs you swipe.
///
/// Above the refusals, the link's timing: the phone's own samples
/// (`LinkTimingStore`, one line per route × kind, then the newest trips
/// one per row with their legs) and — where the Mac sent any — the Mac's
/// ten-minute `timing` rollups, their sentences verbatim. A Mac too old to
/// know them sends none, and the phone draws its own side alone.
struct AccessLogView: View {
    @ObservedObject var client: PhoneClient
    @ObservedObject var timing: LinkTimingStore
    @State private var report = AccessLogReport()
    @State private var loading = false
    @State private var detail = ""

    static let fetchFailedLine = "Dark Army could not read the access log."
    static let unavailableLine = "Dark Army could not open its access log."

    private static let timeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter
    }()

    static func timeText(_ ts: Double) -> String {
        guard ts > 0 else { return "—" }
        return timeFormatter.string(from: Date(timeIntervalSince1970: ts))
    }

    /// The door, in the words the Mac's own sentence uses.
    static func doorWord(_ door: String) -> String {
        switch door {
        case "lan": return "Wi-Fi"
        case "pairing": return "pairing"
        case "upload": return "upload"
        case "relay": return "relay"
        default: return door.isEmpty ? "—" : door
        }
    }

    /// Open alerts come from the live snapshot where the Mac states the
    /// section, so a swipe's acknowledgement lands here on the next poll;
    /// the fetched copy is the fallback for a screen opened first.
    private var openAlerts: [AccessAlert] {
        if client.snapshot.security.available {
            return client.snapshot.security.alerts
        }
        return report.openAlerts
    }

    /// The phone's newest trips, newest first, capped for the screen.
    private var shownSamples: [LinkTimingSample] {
        Array(timing.samples.suffix(LinkTimingView.shownSamples).reversed())
    }

    /// The Mac's own ten-minute rollups, where the report carried any.
    private var macFigures: [AccessLogEntry] {
        report.entries.filter { $0.kind == "timing" }
    }

    /// The refusals and alert lines, without the rollups.
    private var refusals: [AccessLogEntry] {
        report.entries.filter { $0.kind != "timing" }
    }

    var body: some View {
        List {
            if !detail.isEmpty {
                Text(detail)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }

            PhoneSectionHeader(title: "LINK TIMING")
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)

            if timing.samples.isEmpty {
                Text("No trips recorded yet.")
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                    .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }

            ForEach(LinkTimingSummary.lines(timing.samples), id: \.self) { line in
                Text(line)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 4)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }

            ForEach(Array(shownSamples.enumerated()), id: \.offset) { _, sample in
                Text(LinkTimingSummary.format(sample))
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 3)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }

            if !macFigures.isEmpty {
                PhoneSectionHeader(title: "MAC'S OWN FIGURES")
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)

                ForEach(macFigures) { entry in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(Self.timeText(entry.ts))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                        Text(entry.text)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .accessibilityElement(children: .combine)
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
                }
            }

            PhoneSectionHeader(title: openAlerts.isEmpty ? "NO OPEN ALERTS" : "OPEN ALERTS")
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)

            ForEach(openAlerts) { alert in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text(Self.timeText(alert.ts))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                        Text(Self.doorWord(alert.door))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                    }
                    Text(alert.text)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.phosphorBright)
                        .fixedSize(horizontal: false, vertical: true)
                    Text("Acknowledge it from Needs you.")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .accessibilityElement(children: .combine)
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            }

            PhoneSectionHeader(title: "EVERY REFUSAL, NEWEST FIRST")
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)

            if refusals.isEmpty {
                Text(report.available
                     ? "Nothing has been refused."
                     : (loading ? "Loading…" : Self.unavailableLine))
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.faint)
                    .fixedSize(horizontal: false, vertical: true)
                    .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }

            ForEach(refusals) { entry in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text(Self.timeText(entry.ts))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                        Text(Self.doorWord(entry.door))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                    }
                    Text(entry.text)
                        .font(Theme.mono(12))
                        .foregroundStyle(entry.kind == "burst" ? Theme.amber : Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                    if !entry.deviceId.isEmpty {
                        Text("phone \(entry.deviceId)")
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .accessibilityElement(children: .combine)
                .listRowInsets(EdgeInsets())
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .navigationTitle("access log")
        .decryptSurface("AccessLogView")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .task {
            timing.loadIfNeeded()
            await load()
        }
    }

    private func load() async {
        loading = true
        defer { loading = false }
        guard let fetched = await client.accessLogReport() else {
            detail = Self.fetchFailedLine
            return
        }
        report = fetched
        detail = fetched.available ? "" : Self.unavailableLine
    }
}
