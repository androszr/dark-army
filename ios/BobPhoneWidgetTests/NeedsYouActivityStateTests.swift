import SwiftUI
import XCTest

/// `NeedsYouAttributes.ContentState` against the exact JSON `push.js`
/// writes into `aps.content-state`, and against the keys it may one day
/// omit. Only the `Codable` half is exercised here — `WidgetTileTests`'
/// header says why nothing in this bundle should load a portrait.
///
/// No `import BobPhone`: `ios/Shared/NeedsYouActivity.swift` is compiled
/// into this bundle beside the widget's own sources.
final class NeedsYouActivityStateTests: XCTestCase {

    private func decode(_ json: String) throws -> NeedsYouAttributes.ContentState {
        try JSONDecoder().decode(NeedsYouAttributes.ContentState.self,
                                 from: Data(json.utf8))
    }

    func testTheExactWireShapeDecodes() throws {
        // What `push.js` sends for an update, key for key.
        let state = try decode("""
        {"nickname":"Vex","slug":"vex","kind":"permission",
         "work":"Rename the strip ladder","since":1758355200,"session_id":"s-1"}
        """)
        XCTAssertEqual(state.nickname, "Vex")
        XCTAssertEqual(state.slug, "vex")
        XCTAssertEqual(state.kind, "permission")
        XCTAssertEqual(state.kindWord, "permission")
        XCTAssertEqual(state.work, "Rename the strip ladder")
        XCTAssertEqual(state.since, 1_758_355_200)
        XCTAssertEqual(state.sinceDate, Date(timeIntervalSince1970: 1_758_355_200))
        XCTAssertEqual(state.sessionId, "s-1")
    }

    func testAMissingWorkDecodesEmptyAndAMissingKindIsAttention() throws {
        let state = try decode("""
        {"nickname":"Vex","slug":"vex","since":10,"session_id":"s-1"}
        """)
        XCTAssertEqual(state.work, "")
        XCTAssertEqual(state.kind, "attention")
        XCTAssertEqual(state.kindWord, "attention")
        let empty = try decode("{}")
        XCTAssertEqual(empty, NeedsYouAttributes.ContentState())
        XCTAssertEqual(empty.kindWord, "attention")
    }

    func testSinceDecodesFromAnIntegerOrAFloatAndAWrongTypeIsZero() throws {
        XCTAssertEqual(try decode("{\"since\":42}").since, 42)
        XCTAssertEqual(try decode("{\"since\":42.5}").since, 42.5)
        XCTAssertEqual(try decode("{\"since\":\"soon\"}").since, 0)
        XCTAssertEqual(try decode("{\"since\":null}").since, 0)
    }

    func testAnUnknownKindDrawsAsAttentionAndAnUnknownKeyIsIgnored() throws {
        let state = try decode("""
        {"kind":"security","question":"never here","session_id":"s-9"}
        """)
        XCTAssertEqual(state.kind, "security")
        XCTAssertEqual(state.kindWord, "attention")
        XCTAssertEqual(state.sessionId, "s-9")
    }

    func testTheStateRoundTripsThroughItsOwnEncoder() throws {
        let bare = NeedsYouAttributes.ContentState(
            nickname: "Mira", slug: "mira", kind: "question", work: "w",
            since: 7, sessionId: "s")
        let bareData = try JSONEncoder().encode(bare)
        let bareKeys = try XCTUnwrap(JSONSerialization.jsonObject(with: bareData) as? [String: Any])
        XCTAssertEqual(Set(bareKeys.keys), ["nickname", "slug", "kind", "work", "since", "session_id"])
        XCTAssertEqual(try JSONDecoder().decode(NeedsYouAttributes.ContentState.self, from: bareData), bare)

        let full = NeedsYouAttributes.ContentState(
            nickname: "Mira", slug: "mira", kind: "question", work: "w",
            since: 7, sessionId: "s", working: 1, needsYou: 0, standingBy: 2,
            costUsd: 1.5, tokensK: 45, costUsdHour: 4.2, tokensKHour: 1100)
        let data = try JSONEncoder().encode(full)
        let keys = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(Set(keys.keys), [
            "nickname", "slug", "kind", "work", "since", "session_id",
            "working", "needs_you", "standing_by", "cost_usd", "tokens_k",
            "cost_usd_hour", "tokens_k_hour",
        ])
        XCTAssertEqual(try JSONDecoder().decode(NeedsYouAttributes.ContentState.self, from: data), full)
    }

    func testTheElevenKeyWireDecodesEveryField() throws {
        let state = try decode("""
        {"nickname":"Vex","slug":"vex","kind":"permission",
         "work":"Rename","since":10,"session_id":"s-1",
         "working":1,"needs_you":2,"standing_by":3,"cost_usd":1.25,"tokens_k":45}
        """)
        XCTAssertEqual(state.working, 1)
        XCTAssertEqual(state.needsYou, 2)
        XCTAssertEqual(state.standingBy, 3)
        XCTAssertEqual(state.costUsd, 1.25)
        XCTAssertEqual(state.tokensK, 45)
        XCTAssertTrue(state.hasFace)
        XCTAssertEqual(state.costWord, "$1.25")
        XCTAssertEqual(state.tokensWord, "45k tok")
        XCTAssertFalse(state.hasRate)
    }

    func testTheRatesDecodeAndReadAsPerHourAndTokensScaleToMillions() throws {
        let state = try decode("""
        {"session_id":"","cost_usd":87.53,"tokens_k":23000,
         "cost_usd_hour":4.2,"tokens_k_hour":1100}
        """)
        XCTAssertEqual(state.costRateWord, "$4.20/h")
        XCTAssertEqual(state.tokensRateWord, "1.1M tok/h")
        XCTAssertEqual(state.tokensWord, "23M tok")
        XCTAssertTrue(state.hasRate)
        XCTAssertEqual(try decode("{\"cost_usd_hour\":3}").costUsdHour, 3.0)
        XCTAssertNil(try decode("{\"cost_usd_hour\":\"x\"}").costRateWord)
        typealias S = NeedsYouAttributes.ContentState
        XCTAssertEqual(S.tokenFigure(0), "0k")
        XCTAssertEqual(S.tokenFigure(990), "990k")
        XCTAssertEqual(S.tokenFigure(1000), "1.0M")
        XCTAssertEqual(S.tokenFigure(9900), "9.9M")
        XCTAssertEqual(S.tokenFigure(23000), "23M")
    }

    func testTheSixKeyWireLeavesTheFleetFieldsNil() throws {
        let state = try decode("""
        {"nickname":"Vex","slug":"vex","kind":"permission",
         "work":"Rename","since":10,"session_id":"s-1"}
        """)
        XCTAssertNil(state.working)
        XCTAssertNil(state.needsYou)
        XCTAssertNil(state.standingBy)
        XCTAssertNil(state.costUsd)
        XCTAssertNil(state.tokensK)
        XCTAssertTrue(state.hasFace)
    }

    func testAnEmptySessionHasNoFaceAndUnknownFigures() throws {
        let state = try decode("{\"session_id\":\"\"}")
        XCTAssertFalse(state.hasFace)
        XCTAssertEqual(state.costWord, "cost unknown")
        XCTAssertEqual(state.tokensWord, "\u{2013}")
        XCTAssertEqual(try decode("{\"cost_usd\":1}").costUsd, 1.0)
        XCTAssertNil(try decode("{\"cost_usd\":\"x\"}").costUsd)
    }

    func testTheTwoTimingConstants() {
        XCTAssertEqual(NeedsYouAttributes.staleAfter, 1800)
        XCTAssertEqual(NeedsYouAttributes.dismissalDelay, 0)
    }
}

/// The Lock Screen card, laid out for real in every state it can be in and
/// held under the system's height. iOS gives a Live Activity about 160pt on
/// the Lock Screen and crops anything taller flush to the edge — the prompt
/// line and the figures line were drawn touching the rim before 22 Sep 2026.
/// Each state also renders a PNG into `BOB_LOCKSCREEN_SNAPSHOTS` when that
/// variable names a folder, so a person can look at them.
@MainActor
final class NeedsYouLockScreenLayoutTests: XCTestCase {

    /// The narrowest Lock Screen card in the lineup (iPhone SE / mini,
    /// 375pt screen less the system's inset) and the widest (Pro Max).
    private let widths: [CGFloat] = [343, 360, 400]
    private let ceiling: CGFloat = 160

    private func height(_ state: NeedsYouAttributes.ContentState, stale: Bool = false,
                        width: CGFloat, name: String) -> CGFloat {
        let view = NeedsYouLockScreenView(state: state, stale: stale)
            .background(WidgetTheme.bg)
        let host = UIHostingController(rootView: view)
        let size = host.sizeThatFits(in: CGSize(width: width, height: .greatestFiniteMagnitude))
        if let dir = ProcessInfo.processInfo.environment["BOB_LOCKSCREEN_SNAPSHOTS"] {
            let renderer = ImageRenderer(content: view.frame(width: width))
            renderer.scale = 3
            if let data = renderer.uiImage?.pngData() {
                try? data.write(to: URL(fileURLWithPath: dir)
                    .appendingPathComponent("\(name)-\(Int(width)).png"))
            }
        }
        return size.height
    }

    private static var ago: (Double) -> Double {
        { minutes in Date().timeIntervalSince1970 - minutes * 60 }
    }

    private static var states: [(String, NeedsYouAttributes.ContentState, Bool)] { [
        ("nobody-waiting", .init(working: 3, needsYou: 0, standingBy: 2,
                                 costUsd: 6.04, tokensK: 640, updatedAt: ago(0.2)), false),
        ("waiting", .init(nickname: "Canon", slug: "canon", kind: "attention",
                          work: "Test and fix dark army rename", since: ago(1.8),
                          sessionId: "s-1", working: 2, needsYou: 3, standingBy: 0,
                          costUsd: 7.15, tokensK: 690, updatedAt: ago(3)), false),
        ("waiting-long-work-with-rate",
         .init(nickname: "Nyx", slug: "nyx", kind: "permission",
               work: String(repeating: "Rename the strip ladder and every label ", count: 2)
                   .prefix(80).description,
               since: ago(75), sessionId: "s-2", working: 12, needsYou: 11,
               standingBy: 10, costUsd: 1234.56, tokensK: 98_765,
               costUsdHour: 42.1, tokensKHour: 9_999), false),  // unstamped: a dash
        ("waiting-stale", .init(nickname: "Vex", slug: "vex", kind: "question",
                                work: "Card screen", since: ago(52), sessionId: "s-3",
                                working: nil, needsYou: nil, standingBy: nil,
                                updatedAt: ago(40)), true),
    ] }

    func testTheSendStampRoundTripsAndAnOldPayloadHasNone() throws {
        let stamped = try JSONDecoder().decode(NeedsYouAttributes.ContentState.self,
                                               from: Data(#"{"updated_at":1758355200}"#.utf8))
        XCTAssertEqual(stamped.updatedDate, Date(timeIntervalSince1970: 1_758_355_200))
        let old = try JSONDecoder().decode(NeedsYouAttributes.ContentState.self,
                                           from: Data(#"{"since":10}"#.utf8))
        XCTAssertNil(old.updatedDate)
    }

    /// The island's pills are narrow (about 60pt a side beside the camera)
    /// and the single dot is shared with another app: each piece is laid out
    /// on black and held inside its slot, and every no-face piece carries
    /// the mask or a glyph, so the island never shows a bare number.
    func testTheIslandPiecesFitTheirSlots() {
        for (name, state, stale) in Self.states {
            let pieces: [(String, AnyView, CGFloat)] = [
                ("leading", AnyView(IslandCompactLeading(state: state, stale: stale)), 60),
                ("trailing", AnyView(IslandCompactTrailing(state: state, stale: stale)), 60),
                ("minimal", AnyView(IslandMinimal(state: state, stale: stale)), 36),
            ]
            if let dir = ProcessInfo.processInfo.environment["BOB_LOCKSCREEN_SNAPSHOTS"] {
                // A picture for a person, not a measurement: the compact
                // island with its camera gap, and the single dot beside it.
                let mock = HStack(spacing: 18) {
                    HStack(spacing: 0) {
                        IslandCompactLeading(state: state, stale: stale)
                        Spacer(minLength: 0)
                        IslandCompactTrailing(state: state, stale: stale)
                    }
                    .padding(.horizontal, 12)
                    .frame(width: 250, height: 37)
                    .background(Capsule().fill(Color.black))
                    IslandMinimal(state: state, stale: stale)
                        .frame(width: 37, height: 37)
                        .background(Circle().fill(Color.black))
                }
                .padding(14)
                .background(Color(white: 0.85))
                .environment(\.colorScheme, .dark)
                let renderer = ImageRenderer(content: mock)
                renderer.scale = 3
                try? renderer.uiImage?.pngData()?.write(to: URL(fileURLWithPath: dir)
                    .appendingPathComponent("island-\(name).png"))
            }
            for (slot, piece, maxWidth) in pieces {
                let view = piece.padding(6).background(Color.black)
                let size = UIHostingController(rootView: view)
                    .sizeThatFits(in: CGSize(width: 200, height: 100))
                XCTAssertLessThanOrEqual(size.width - 12, maxWidth, "\(name) \(slot)")
                XCTAssertLessThanOrEqual(size.height - 12, 36, "\(name) \(slot)")
            }
        }
    }

    func testEveryStateFitsTheLockScreenAtEveryWidth() {
        for (name, state, stale) in Self.states {
            for width in widths {
                let h = height(state, stale: stale, width: width, name: name)
                XCTAssertLessThanOrEqual(h, ceiling, "\(name) at \(width)pt is \(h)pt tall")
                print("lockscreen \(name) @\(Int(width)): \(h)pt")
            }
        }
    }
}

/// The real home-screen tiles, written only when `BOB_LOCKSCREEN_SNAPSHOTS`
/// names a folder. The layout tests above already write the Lock Screen
/// card and the island into that same folder. Nothing here runs in CI.
@MainActor
final class FleetWidgetSurfaceShots: XCTestCase {

    func testWriteTheHomeScreenTiles() throws {
        guard let dir = ProcessInfo.processInfo.environment["BOB_LOCKSCREEN_SNAPSHOTS"],
              !dir.isEmpty else {
            throw XCTSkip("Set BOB_LOCKSCREEN_SNAPSHOTS to write the tiles.")
        }
        let folder = URL(fileURLWithPath: dir, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let now = Date().timeIntervalSince1970
        let bars = [
            FleetSummary.Bar(provider: "claude", label: "5h", percent: 62),
            FleetSummary.Bar(provider: "grok", label: "5h", percent: 18),
            FleetSummary.Bar(provider: "codex", label: "5h", percent: 34),
        ]
        let alarm = FleetSummary(
            needsYou: 1, working: 2, todo: 5, generatedAt: now, dimAfter: 600,
            bars: bars,
            faces: [.init(slug: "cipher", name: "Cipher", doing: "needs you",
                          rule: "alert", sessionId: "s-cipher")])
        let quiet = FleetSummary(
            needsYou: 0, working: 3, todo: 5, generatedAt: now, dimAfter: 600,
            bars: bars,
            faces: [.init(slug: "vex", name: "Vex", doing: "working",
                          rule: "work", sessionId: "s-vex")])
        let date = Date(timeIntervalSince1970: now)
        try write(medium(alarm, date: date), "widget-medium-alarm.png", folder, 364, 170)
        try write(medium(quiet, date: date), "widget-medium-quiet.png", folder, 364, 170)
        try write(small(alarm), "widget-small-alarm.png", folder, 170, 170)
        try write(small(quiet), "widget-small-quiet.png", folder, 170, 170)
        try write(circle(alarm), "widget-circle-alarm.png", folder, 76, 76)
        try write(circle(quiet), "widget-circle-quiet.png", folder, 76, 76)
    }

    private func medium(_ summary: FleetSummary, date: Date) -> some View {
        MediumFleetView(summary: summary, stale: false,
                        face: summary.faces.first, date: date)
            .padding(14)
            .frame(width: 364, height: 170, alignment: .topLeading)
            .background(WidgetTheme.bg)
    }

    private func small(_ summary: FleetSummary) -> some View {
        SmallFleetView(summary: summary, stale: false)
            .padding(16)
            .frame(width: 170, height: 170, alignment: .topLeading)
            .background(WidgetTheme.bg)
    }

    private func circle(_ summary: FleetSummary) -> some View {
        CircularFleetView(summary: summary, stale: false)
            .frame(width: 76, height: 76)
            .environment(\.colorScheme, .dark)
            .background(Color.black)
    }

    private func write<V: View>(_ view: V, _ name: String, _ folder: URL,
                                _ width: CGFloat, _ height: CGFloat) throws {
        let renderer = ImageRenderer(content: view.frame(width: width, height: height))
        renderer.scale = 3
        let data = try XCTUnwrap(renderer.uiImage?.pngData(), "no pixels for \(name)")
        try data.write(to: folder.appendingPathComponent(name))
    }
}
