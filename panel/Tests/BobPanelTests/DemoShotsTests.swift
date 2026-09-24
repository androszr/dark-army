import AppKit
import SwiftUI
import XCTest
@testable import BobPanel

/// The front page's pictures, drawn by the panel's own views from one
/// made-up working day (`Fixtures/demo-shots.json`).
///
/// `testDemoDayDecodes` always runs: the fixture must decode through the
/// real `Snapshot` and tell the story the pictures promise. So does
/// `testTheOpenCardWindowHoldsNothingBack`: the widget card, opened the way
/// a click on the board opens it, holds nothing back from Save. The render only
/// runs when `tools/demo_shots.py panel` sets `BOB_DEMO_SHOTS_OUT`; it hosts
/// `PanelView` and the card window in borderless off-screen windows and
/// never calls `DaemonClient.start()` — `visible` and `boardOpen` are never
/// written, and `DemoShotsProtocol` answers every `URLSession.shared` request
/// from the fixture, so nothing reaches the running Dark Army. The request
/// log proves it: no `/api/events`, no write. The card window's layer opens
/// its card through `BoardState.openEditor`, the person's own route, so the
/// editor holds the card's text; its MORE and PLAN folds are pressed by their
/// labels, and each must then report itself open.
final class DemoShotsTests: XCTestCase {

    // MARK: - The fixture

    static let fixtureURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("Fixtures/demo-shots.json")

    /// Every clock in the day is written against `metadata.base_epoch`;
    /// moving them all by the same amount makes "waiting 2m" read as two
    /// minutes on whatever day the pictures are taken.
    static let timeKeys: Set<String> = ["generated_at", "last_event", "since",
                                        "started_at", "quiet_since"]

    static func isTimeKey(_ key: String) -> Bool {
        timeKeys.contains(key) || key.hasSuffix("_at") || key.hasSuffix("_since")
    }

    static func shifted(_ value: Any, by delta: Double) -> Any {
        if let dict = value as? [String: Any] {
            var out: [String: Any] = [:]
            for (key, inner) in dict {
                if isTimeKey(key), let number = inner as? NSNumber,
                   CFGetTypeID(number) != CFBooleanGetTypeID() {
                    out[key] = number.doubleValue + delta
                } else {
                    out[key] = shifted(inner, by: delta)
                }
            }
            return out
        }
        if let list = value as? [Any] { return list.map { shifted($0, by: delta) } }
        return value
    }

    /// Replace every project folder in the day with a real folder, in memory
    /// only — the card window reads the plan through `BoardDocuments`, which
    /// resolves inside the card's own root on disk.
    static func rerooted(_ value: Any, from: String, to: String) -> Any {
        if let dict = value as? [String: Any] {
            return dict.mapValues { rerooted($0, from: from, to: to) }
        }
        if let list = value as? [Any] { return list.map { rerooted($0, from: from, to: to) } }
        if let text = value as? String, text.hasPrefix(from) {
            return to + text.dropFirst(from.count)
        }
        return value
    }

    struct Day {
        let root: [String: Any]
        let now: Double

        var main: Any { frame("main") }
        var firstRun: Any { frame("first_run") }

        func frame(_ name: String) -> Any {
            ((root["frames"] as? [String: Any]) ?? [:])[name] ?? [:]
        }

        var metadata: [String: Any] { (root["metadata"] as? [String: Any]) ?? [:] }
        var projects: [String] { (metadata["projects"] as? [String]) ?? [] }
        var documents: [String: String] { (root["documents"] as? [String: String]) ?? [:] }
    }

    static func loadDay(now: Double = Date().timeIntervalSince1970) throws -> Day {
        let raw = try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(contentsOf: fixtureURL)) as? [String: Any])
        let meta = try XCTUnwrap(raw["metadata"] as? [String: Any])
        XCTAssertEqual(meta["synthetic"] as? Bool, true)
        let base = try XCTUnwrap((meta["base_epoch"] as? NSNumber)?.doubleValue)
        let shiftedRoot = try XCTUnwrap(shifted(raw, by: now - base) as? [String: Any])
        return Day(root: shiftedRoot, now: now)
    }

    static func decode<T: Decodable>(_ value: Any, as type: T.Type) throws -> T {
        try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: value))
    }

    // MARK: - Always: the story decodes

    func testDemoDayDecodes() throws {
        let day = try Self.loadDay()
        let main = try Self.decode(day.main, as: Snapshot.self)
        let first = try Self.decode(day.firstRun, as: Snapshot.self)

        // The fleet: three working, one waiting on you, two resting, one done.
        XCTAssertEqual(main.agents.running.count, 3)
        XCTAssertEqual(main.agents.waiting.count, 1)
        XCTAssertEqual(main.agents.sleeping.count, 2)
        XCTAssertEqual(main.agents.finished.count, 1)
        XCTAssertEqual(main.agents.waiting.first?.nickname, "Cipher")
        XCTAssertEqual(main.agents.waiting.first?.questionList.first?.options.count, 3)

        // The board: ten cards, two to three to three to two.
        let cards = main.board.cards
        XCTAssertEqual(cards.count, 10)
        let perColumn = ["prep", "backlog", "in_progress", "done"].map { column in
            cards.filter { $0.column == column }.count
        }
        XCTAssertEqual(perColumn, [2, 3, 3, 2])
        let widget = try XCTUnwrap(cards.first { $0.title == "Widget for tomorrow's forecast" })
        XCTAssertFalse(widget.planPath.isEmpty)
        XCTAssertNotNil(cards.first { $0.column == "in_progress" && $0.runFigures != nil })

        // Nothing on any row could open a terminal socket or name a process.
        let rows = main.agents.running + main.agents.waiting + main.agents.sleeping
            + main.agents.finished
        XCTAssertTrue(rows.allSatisfy { $0.pid == nil && !$0.ownTerminal && !$0.channel
            && !$0.canStop && !$0.canClose && !$0.canJump && !$0.canTerminalInput })
        XCTAssertEqual(rows.filter(\.canType).map(\.nickname), ["Cipher"])

        // The inbox: Cipher's answer and the Done card's look — two things.
        let waitingRows = main.agents.waiting.map { SectionRow(agent: $0, category: .waiting) }
        let inbox = Inbox.items(rows: waitingRows, prompts: main.permissions,
                                cards: cards, now: day.now)
        XCTAssertEqual(inbox.count, 2)

        // The first-run frame shows the checklist with one step done.
        XCTAssertEqual(FirstRunChecklist.decide(enrollment: first.enrollment, completed: false),
                       .show)
        XCTAssertEqual(FirstRunChecklist.decide(enrollment: main.enrollment, completed: false),
                       .unsupported)
    }

    /// The card window in the board picture is a saved, titled card. Opened
    /// the way a click on the board opens it, its editor holds the card's
    /// title and nothing holds Save — no "Name it first" beside a dim button.
    @MainActor
    func testTheOpenCardWindowHoldsNothingBack() throws {
        let day = try Self.loadDay()
        let main = try Self.decode(day.main, as: Snapshot.self)
        let card = try XCTUnwrap(main.board.cards.first {
            $0.title == "Widget for tomorrow's forecast" })
        let state = BoardState()
        state.openEditor(card, client: DaemonClient())
        XCTAssertEqual(state.editing, card.id)
        XCTAssertEqual(state.draft.title, card.title)
        XCTAssertTrue(state.draft.expanded)
        XCTAssertNil(state.saveHoldReason(preparing: false),
                     "a saved, titled card holds nothing back from Save")
    }

    // MARK: - On request: the render

    @MainActor
    func testRenderDemoShots() throws {
        guard let outPath = ProcessInfo.processInfo.environment["BOB_DEMO_SHOTS_OUT"],
              !outPath.isEmpty else {
            throw XCTSkip("Set BOB_DEMO_SHOTS_OUT (tools/demo_shots.py panel) to render.")
        }
        let out = URL(fileURLWithPath: outPath, isDirectory: true)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        // The pictures are the same on every Mac: English dates, whatever the
        // machine's own language. The argument domain is volatile — nothing
        // is written to anyone's preferences.
        UserDefaults.standard.setVolatileDomain(
            ["AppleLocale": "en_US", "AppleLanguages": ["en-US"]],
            forName: UserDefaults.argumentDomain)
        NSTimeZone.default = TimeZone(identifier: "America/Los_Angeles") ?? .current

        let day = try Self.loadDay()

        // Real project folders, so the card window can read its plan.
        let demoRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("dark-army-demo-\(UUID().uuidString)/demo-root")
        defer { try? FileManager.default.removeItem(at: demoRoot.deletingLastPathComponent()) }
        for project in day.projects {
            try FileManager.default.createDirectory(
                at: demoRoot.appendingPathComponent(project), withIntermediateDirectories: true)
        }
        for (relative, text) in day.documents {
            let file = demoRoot.appendingPathComponent("pocket-weather")
                .appendingPathComponent(relative)
            try FileManager.default.createDirectory(
                at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            try text.write(to: file, atomically: true, encoding: .utf8)
        }
        // `BoardDocuments` resolves symlinks; hand it the resolved form.
        let realRoot = demoRoot.resolvingSymlinksInPath().path
        let codeFolder = "/" + "Users/you/Code"
        let mainFrame = Self.rerooted(day.main, from: codeFolder, to: realRoot)
        let firstFrame = Self.rerooted(day.firstRun, from: codeFolder, to: realRoot)

        DemoShotsProtocol.serve(state: mainFrame, root: day.root)
        XCTAssertTrue(URLProtocol.registerClass(DemoShotsProtocol.self))
        defer { URLProtocol.unregisterClass(DemoShotsProtocol.self) }

        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        NSApp.appearance = NSAppearance(named: .darkAqua)

        let client = DaemonClient()
        client.connected = true
        var snapshot = try Self.decode(mainFrame, as: Snapshot.self)
        snapshot.agentsStamp = snapshot.generatedAt
        client.snapshot = snapshot
        client.usage = try Self.decode(day.root["usage"] ?? [:], as: UsageReport.self).bars
        var context = DaemonClient.PanelContext()
        if let ctx = day.root["context"] as? [String: Any] {
            context.grokPercent = (ctx["grokPercent"] as? NSNumber)?.doubleValue
            context.build = ctx["build"] as? String ?? ""
        }
        // A first launch that went well, for the checklist's launch line.
        context.settings.launch = DaemonClient.LaunchReport(
            hooks: .init(status: "changed"), editorExtension: .init(status: "changed"),
            loginItem: .init(status: "changed"))
        client.context = context

        let board = BoardState()
        board.rowFlips = ["done"]
        let keys = KeyRouter()
        let focus = FocusRouter()

        // The window, on Inbox (the default tab).
        let panelSize = NSSize(width: 1180, height: 748)
        let panel = DemoWindow(size: panelSize) {
            DemoWindowChrome(title: "Dark Army") {
                PanelView(client: client, keys: keys, focus: focus, board: board)
            }
        }
        defer { panel.close() }
        panel.settle(0.8)
        try panel.write(to: out.appendingPathComponent("window-inbox.png"))

        // The Agents list, before a row is opened. The tab centres were
        // measured on this window: Inbox 697, Agents 804, Comm 955,
        // History 1074, all at y 163.
        panel.click(atTopLeft: NSPoint(x: 857, y: 216))
        panel.settle(0.5)
        try panel.write(to: out.appendingPathComponent("window-agents.png"))

        // The same window on Agents, with the waiting agent open.
        let cipher = try XCTUnwrap(snapshot.agents.waiting.first { $0.nickname == "Cipher" })
        NotificationCenter.default.post(name: .panelShowSession, object: cipher.sessionId,
                                        userInfo: ["provider": cipher.provider])
        panel.settle(0.8)
        XCTAssertTrue(keys.detailOpen, "Cipher's detail must be open for the question shot")
        try panel.write(to: out.appendingPathComponent("window-agents-cipher.png"))

        // Comm, then History, same measured tab row.
        panel.click(atTopLeft: NSPoint(x: 975, y: 216))
        panel.settle(0.6)
        try panel.write(to: out.appendingPathComponent("window-comm.png"))
        panel.click(atTopLeft: NSPoint(x: 1128, y: 216))
        panel.settle(0.6)
        try panel.write(to: out.appendingPathComponent("window-history.png"))

        // The whole board, wider, for the board shot: all four rows at once.
        let boardSize = NSSize(width: 1640, height: 1000)
        // No title: the shot crops the rail away, and a centred title would
        // sit off-centre over what is left.
        let boardWindow = DemoWindow(size: boardSize) {
            DemoWindowChrome(title: "") {
                PanelView(client: client, keys: KeyRouter(), focus: FocusRouter(),
                          board: { let s = BoardState(); s.rowFlips = ["done"]; return s }())
            }
        }
        defer { boardWindow.close() }
        boardWindow.settle(0.8)
        try boardWindow.write(to: out.appendingPathComponent("window-board.png"))
        // Same tabs on the wide window, so Comm has room for the board
        // beside Mission Control. The rail stays 520pt, pinned to the right.
        boardWindow.click(atTopLeft: NSPoint(x: 1464, y: 216))
        boardWindow.settle(0.6)
        try boardWindow.write(to: out.appendingPathComponent("window-comm-wide.png"))
        boardWindow.click(atTopLeft: NSPoint(x: 1588, y: 216))
        boardWindow.settle(0.6)
        try boardWindow.write(to: out.appendingPathComponent("window-history-wide.png"))

        // The card window for the widget card, its plan read from disk.
        let widget = try XCTUnwrap(snapshot.board.cards.first {
            $0.title == "Widget for tomorrow's forecast" })
        let resolved = try XCTUnwrap(BoardDocuments.resolve(widget.planPath, root: widget.root))
        XCTAssertFalse((BoardDocuments.read(resolved) ?? "").isEmpty, "the plan must load")
        // Opened the person's way, so the editor holds the card's own text.
        let cardState = BoardState()
        cardState.openEditor(widget, client: client)
        XCTAssertEqual(cardState.editing, widget.id)
        XCTAssertEqual(cardState.draft.title, widget.title)
        XCTAssertNil(cardState.saveHoldReason(preparing: false),
                     "a saved, titled card holds nothing back from Save")
        let cardWindow = DemoWindow(size: NSSize(width: CardWindowMetrics.minWidth,
                                                 height: 700)) {
            DemoWindowChrome(title: widget.title) {
                CardWindowRoot(client: client, state: cardState)
            }
        }
        defer { cardWindow.close() }
        cardWindow.settle(0.8)
        try cardWindow.write(to: out.appendingPathComponent("card-window-shut.png"))
        // The plan sits under MORE, then its own PLAN heading. Both rows are
        // pressed by their labels (`MORE · n`, `PLAN · attached`), as
        // VoiceOver would, and each must then say it is open: a picture drawn
        // with a fold shut is the fault a measured click could not see.
        let isMore: (String) -> Bool = { $0.hasPrefix("MORE") }
        let isPlan: (String) -> Bool = { $0.hasPrefix("PLAN") }
        XCTAssertTrue(cardWindow.press(isMore), "the MORE row must be pressable by its label")
        cardWindow.settle(0.5)
        XCTAssertEqual(cardWindow.accessibilityValue(ofLabel: isMore), "shown",
                       cardWindow.labels())
        XCTAssertTrue(cardWindow.press(isPlan), "the PLAN row must be pressable by its label")
        cardWindow.settle(0.8)
        XCTAssertEqual(cardWindow.accessibilityValue(ofLabel: isPlan), "shown",
                       cardWindow.labels())
        try cardWindow.write(to: out.appendingPathComponent("card-window.png"))
        DemoWindow.stopAccessibility()

        // The first-run checklist, in a fresh window on the first-run frame.
        var first = try Self.decode(firstFrame, as: Snapshot.self)
        first.agentsStamp = first.generatedAt
        XCTAssertEqual(FirstRunChecklist.decide(enrollment: first.enrollment, completed: false),
                       .show)
        let firstClient = DaemonClient()
        firstClient.connected = true
        firstClient.snapshot = first
        firstClient.usage = client.usage
        firstClient.context = context
        let firstWindow = DemoWindow(size: panelSize) {
            DemoWindowChrome(title: "Dark Army") {
                PanelView(client: firstClient, keys: KeyRouter(), focus: FocusRouter(),
                          board: BoardState())
            }
        }
        defer { firstWindow.close() }
        firstWindow.settle(0.8)
        let railWidth: CGFloat = 520
        try firstWindow.write(to: out.appendingPathComponent("rail-first-run.png"),
                              rect: NSRect(x: panelSize.width - railWidth, y: 0,
                                           width: railWidth, height: panelSize.height))

        // Nothing reached the running app: no event stream, no write.
        let log = DemoShotsProtocol.seen
        try (log.joined(separator: "\n") + "\n")
            .write(to: out.appendingPathComponent("requests.txt"), atomically: true,
                   encoding: .utf8)
        XCTAssertFalse(log.contains { $0.contains("/api/events") }, "\(log)")
        XCTAssertFalse(log.contains { !$0.hasPrefix("GET ") }, "\(log)")
    }
}

// MARK: - The harness

/// A borderless window placed far off every screen, never key, holding one
/// SwiftUI view at a fixed point size, captured at 2x whatever the display.
@MainActor
private final class DemoWindow {
    let window: NSWindow
    let host: NSView
    let size: NSSize

    init<Content: View>(size: NSSize, @ViewBuilder content: () -> Content) {
        self.size = size
        window = NSWindow(contentRect: NSRect(x: -20_000, y: -20_000,
                                              width: size.width, height: size.height),
                          styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.isOpaque = false
        window.backgroundColor = .clear
        window.hasShadow = false
        window.appearance = NSAppearance(named: .darkAqua)
        let hosting = NSHostingView(rootView: content()
            .frame(width: size.width, height: size.height)
            .environment(\.colorScheme, .dark))
        hosting.frame = NSRect(origin: .zero, size: size)
        host = hosting
        window.contentView = hosting
        window.orderBack(nil)
    }

    func settle(_ seconds: TimeInterval) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.2))
    }

    func write(to url: URL, rect: NSRect? = nil) throws {
        let area = rect ?? host.bounds
        let rep = try XCTUnwrap(NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: Int(area.width * 2),
            pixelsHigh: Int(area.height * 2), bitsPerSample: 8, samplesPerPixel: 4,
            hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0))
        rep.size = area.size
        host.cacheDisplay(in: area, to: rep)
        let srgb = rep.converting(to: .sRGB, renderingIntent: .default) ?? rep
        let data = try XCTUnwrap(srgb.representation(using: .png, properties: [:]))
        try data.write(to: url)
    }

    func close() { window.orderOut(nil); window.close() }

    /// A left click at a point measured from the window's top-left corner,
    /// delivered straight to the window — no cursor moves, nothing else on
    /// the screen is touched.
    func click(atTopLeft point: NSPoint) {
        let location = NSPoint(x: point.x, y: size.height - point.y)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            if let event = NSEvent.mouseEvent(
                with: type, location: location, modifierFlags: [],
                timestamp: ProcessInfo.processInfo.systemUptime,
                windowNumber: window.windowNumber, context: nil, eventNumber: 0,
                clickCount: 1, pressure: type == .leftMouseDown ? 1 : 0) {
                window.sendEvent(event)
            }
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
    }

    /// Every accessibility element under the window, breadth first.
    ///
    /// SwiftUI builds its accessibility tree only once an assistive client
    /// has asked for it, so the walk first says it is one
    /// (`AXEnhancedUserInterface` on this test process's own application —
    /// nothing outside the process is touched). SwiftUI's nodes answer the
    /// `NSAccessibility` messages without formally adopting the protocol, so
    /// the walk sends them through `AnyObject`'s dynamic lookup.
    func elements() -> [AnyObject] {
        if NSApp.accessibilityAttributeValue(Self.enhanced) as? Bool != true {
            NSApp.accessibilitySetValue(true, forAttribute: Self.enhanced)
            settle(0.5)
        }
        var queue: [AnyObject] = [host]
        var out: [AnyObject] = []
        while !queue.isEmpty, out.count < 20_000 {
            let element = queue.removeFirst()
            out.append(element)
            queue += (element.accessibilityChildren?() ?? nil ?? []).map { $0 as AnyObject }
        }
        return out
    }

    private static let enhanced = NSAccessibility.Attribute(rawValue: "AXEnhancedUserInterface")

    /// Take back what `elements()` said, so later layers draw as before.
    static func stopAccessibility() {
        NSApp.accessibilitySetValue(false, forAttribute: enhanced)
    }

    private static func label(_ element: AnyObject) -> String {
        [element.accessibilityLabel?() ?? nil, element.accessibilityTitle?() ?? nil]
            .compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
    }

    func labels() -> String {
        elements().map(Self.label).filter { !$0.isEmpty }.joined(separator: " | ")
    }

    /// Press the first element whose label matches, as VoiceOver would.
    func press(_ matches: (String) -> Bool) -> Bool {
        for element in elements() where matches(Self.label(element)) {
            if element.accessibilityPerformPress?() == true { return true }
        }
        return false
    }

    /// The accessibility value of the first element whose label matches —
    /// a fold row answers `shown` or `hidden`.
    func accessibilityValue(ofLabel matches: (String) -> Bool) -> String? {
        guard let element = elements().first(where: { matches(Self.label($0)) }) else { return nil }
        let value: (() -> Any?)? = element.accessibilityValue
        return (value?() ?? nil) as? String
    }
}

/// The macOS title bar is outside a window's content view, so a capture of
/// the content has none. This draws one — the three lights and the title —
/// so the pictures read as a Mac window, not a floating panel.
private struct DemoWindowChrome<Content: View>: View {
    let title: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Color(nsColor: Theme.nsBg)
                HStack(spacing: 8) {
                    Circle().fill(Color(red: 1.0, green: 0.37, blue: 0.34))
                    Circle().fill(Color(red: 1.0, green: 0.74, blue: 0.18))
                    Circle().fill(Color(red: 0.16, green: 0.78, blue: 0.25))
                }
                .frame(width: 52, height: 12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.leading, 12)
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .padding(.horizontal, 80)
            }
            .frame(height: 28)
            content
        }
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// Answers every request from the fixture and writes down what was asked.
/// Anything the day does not hold is a 404 with `{}` — never a pass-through.
private final class DemoShotsProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var requests: [String] = []
    private static var bodies: [String: Data] = [:]

    static var seen: [String] { lock.lock(); defer { lock.unlock() }; return requests }

    static func serve(state: Any, root: [String: Any]) {
        lock.lock(); defer { lock.unlock() }
        requests = []
        var map: [String: Data] = [:]
        map["/api/state"] = try? JSONSerialization.data(withJSONObject: state)
        map["/api/usage"] = try? JSONSerialization.data(withJSONObject: root["usage"] ?? [:])
        let board = ((state as? [String: Any])?["board"] as? [String: Any]) ?? [:]
        let done = ((board["cards"] as? [[String: Any]]) ?? []).filter {
            ($0["column_name"] as? String) == "done" }
        map["/api/board?column=done"] = try? JSONSerialization.data(
            withJSONObject: ["available": true, "cards": done])
        bodies = map
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let url = request.url
        var key = url?.path ?? ""
        if let query = url?.query, !query.isEmpty { key += "?" + query }
        Self.lock.lock()
        Self.requests.append((request.httpMethod ?? "GET") + " " + key)
        let body = Self.bodies[key]
        Self.lock.unlock()
        let status = body == nil ? 404 : 200
        let response = HTTPURLResponse(url: url ?? URL(string: "about:blank")!,
                                       statusCode: status, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body ?? Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
