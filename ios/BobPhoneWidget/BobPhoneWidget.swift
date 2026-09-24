import SwiftUI
import WidgetKit

/// The home-screen tile and the Lock Screen circle, one timeline for
/// both. It draws whatever the app last heard — the App-Group summary
/// and nothing else. **No network, ever**: fetching from the provider
/// would need the pairing record out of a shared Keychain group (a
/// migration that can throw every paired phone back to the QR screen)
/// and would spend the relay's command budget from a process with no
/// user in front of it. What keeps the numbers moving with the app
/// closed is the *app's* `BackgroundRefresh` — a scheduled check-in that
/// writes the same summary and reloads this tile — so the dim horizon
/// below is the summary's own `dimAfter`, sized to that schedule.
/// Stale-as-normal is still the design, shown dimmed with a self-updating
/// age line rather than hidden.
@main
struct BobPhoneWidgetBundle: WidgetBundle {
    var body: some Widget {
        BobFleetWidget()
        NeedsYouActivityWidget()
    }
}

struct FleetEntry: TimelineEntry {
    let date: Date
    let summary: FleetSummary?
    /// The second timeline entry admits its age up front.
    let dimmed: Bool
    /// Whose face this hour draws, or `nil` from an older app's note — in
    /// which case the tile draws no face column at all rather than a gap.
    let face: FleetSummary.Face?
}

/// Which face an hour wears. Whole hours since the epoch — **no calendar,
/// no time zone, no locale**: the tile must pick the same face for the same
/// instant on any phone, and a local hour repeats once a year at the DST
/// fallback, which is a tile that visibly goes backwards.
enum FaceRotation {
    /// How many hourly entries the timeline plans ahead — about half a day,
    /// so the face keeps changing with the app closed and no signal.
    static let rotationHours = 12

    static func hour(_ date: Date) -> Int {
        Int(date.timeIntervalSince1970 / 3600)
    }

    static func face(_ faces: [FleetSummary.Face],
                     at date: Date) -> FleetSummary.Face? {
        guard !faces.isEmpty else { return nil }
        return faces[((hour(date) % faces.count) + faces.count) % faces.count]
    }
}

struct FleetProvider: TimelineProvider {
    func placeholder(in context: Context) -> FleetEntry {
        FleetEntry(date: .now, summary: nil, dimmed: false, face: nil)
    }

    func getSnapshot(in context: Context,
                     completion: @escaping (FleetEntry) -> Void) {
        let summary = FleetSummary.load()
        completion(FleetEntry(date: .now, summary: summary, dimmed: false,
                              face: FaceRotation.face(summary?.faces ?? [],
                                                      at: .now)))
    }

    func getTimeline(in context: Context,
                     completion: @escaping (Timeline<FleetEntry>) -> Void) {
        // Two entries: the numbers as of now, and the same numbers
        // `dimAfter` on (ten minutes for a summary that carries none),
        // flagged dim — a closed app means an ageing tile, and the tile
        // says so by itself. `.never` because reloads are app-driven
        // (`PhoneClient` calls `reloadTimelines` when the figures change,
        // and `BackgroundRefresh` on its schedule); WidgetKit's own
        // refresh budget is deliberately not relied on.
        let summary = FleetSummary.load()
        let now = Date()
        let dimAfter = summary?.dimAfter ?? 600
        let faces = summary?.faces ?? []
        // The dim clock counts from the note's own `generatedAt` — when
        // the app last heard from the Mac — not from this reload, so the
        // tile's honesty no longer depends on a reload at every departure
        // (each of which spends WidgetKit's daily budget;
        // `WidgetReloadBudget`). A note already past its horizon draws
        // dim from the first entry.
        let wrote = summary.map { Date(timeIntervalSince1970: $0.generatedAt) }
            ?? now
        let dimAt = max(now.addingTimeInterval(1),
                        wrote.addingTimeInterval(dimAfter))
        var entries = [
            FleetEntry(date: now, summary: summary,
                       dimmed: now >= wrote.addingTimeInterval(dimAfter),
                       face: FaceRotation.face(faces, at: now)),
            FleetEntry(date: dimAt, summary: summary,
                       dimmed: true,
                       face: FaceRotation.face(faces, at: dimAt)),
        ]
        // Then one entry per whole hour after that, so the face keeps
        // turning over with the app dead and the phone in flight mode. The
        // dates sit on the same epoch-hour boundaries `FaceRotation.hour`
        // divides by, so an entry always sits inside the hour whose face it
        // carries. All dim by construction, which is the honest reading — a
        // tile still on entry 3 is a tile whose app has been closed for
        // hours — and the timeline's policy is unchanged: reloads stay
        // app-driven and nothing here leans on WidgetKit's refresh budget.
        var boundary = (dimAt.timeIntervalSince1970 / 3600).rounded(.up) * 3600
        if boundary <= dimAt.timeIntervalSince1970 { boundary += 3600 }
        for step in 0..<FaceRotation.rotationHours {
            let date = Date(timeIntervalSince1970: boundary
                            + Double(step) * 3600)
            entries.append(FleetEntry(date: date, summary: summary,
                                      dimmed: true,
                                      face: FaceRotation.face(faces, at: date)))
        }
        completion(Timeline(entries: entries, policy: .never))
    }
}

/// The tile's `kind` is its identity to WidgetKit, and the reload budget
/// hangs off that identity: a kind that spent its budget stays deferred
/// until WidgetKit decides otherwise, and removing and re-adding the tile
/// inherits the same ledger. `BobFleetWidget` was that kind on 21 Sep 2026
/// — a day of unconditional background reloads left it drawing a day-old
/// note with the app writing a fresh one every few seconds and no crash
/// on record — so the tile became `BobFleetTile`, a fresh identity with a
/// fresh budget. A placed tile of the old kind goes blank after the update
/// and is added again from the gallery; the summary file and the
/// `WidgetReloadBudget` ledger are unchanged. Renaming again is the last
/// resort, never the routine: the ledger is what keeps this one inside
/// its budget.
struct BobFleetWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "BobFleetTile",
                            provider: FleetProvider()) { entry in
            FleetWidgetView(entry: entry)
        }
        .configurationDisplayName("Dark Army fleet")
        .description("Who needs you, who is working, and what is waiting.")
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular])
    }
}
