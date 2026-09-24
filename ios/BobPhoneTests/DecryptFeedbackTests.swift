import XCTest
import SwiftUI
import UIKit
@testable import BobPhone

@MainActor
final class DecryptFeedbackTests: XCTestCase {
    private func window(width: Double = 375, height: Double = 812) throws -> UIWindow {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: width, height: height)
        return window
    }

    func testActionsAreImmediateAndRapidPressesNeverQueue() {
        var now = 10.0, calls = 0
        let owner = DecryptFeedback(now: { now }, schedules: false)
        owner.arrive("a")
        let generation = owner.state.generation
        for _ in 0..<20 { owner.activate { calls += 1; XCTAssertEqual(now, 10) } }
        XCTAssertEqual(calls, 20)
        XCTAssertNil(owner.state.button)
        XCTAssertEqual(owner.state.generation, generation)
        XCTAssertEqual(owner.state.screen?.kind, .screen)
        owner.activate(enabled: false) { calls += 1 }
        XCTAssertEqual(calls, 20)
        // A disabled press never invokes the action and never starts INPUT.
        XCTAssertNil(owner.state.button)
    }
    func testRefreshDoesNotWaitForDecorationOrInventSuccess() async throws {
        var now = 0.0
        let owner = DecryptFeedback(now: { now }, schedules: false)
        owner.arrive("a")
        XCTAssertEqual(owner.state.screen?.kind.caption, "OPEN")
        enum Refusal: Error { case offline }
        for result in ["unchanged", "offline", "slow"] {
            let operation: () async throws -> String = {
                if result == "offline" { throw Refusal.offline }
                if result == "slow" { now += 10; owner.advance() }
                else { now += 0.05 }
                return result
            }
            do { let value = try await operation(); XCTAssertEqual(value, result) }
            catch { XCTAssertEqual(result, "offline") }
            XCTAssertNotEqual(owner.state.screen?.kind, .refresh)
            if result == "slow" { XCTAssertNil(owner.state.screen) }
        }
    }
    func testNativePickerLaunchAndCancelReturnCoalesceWithArrival() {
        var now = 0.0, launches = 0
        let owner = DecryptFeedback(now: { now }, schedules: false)
        owner.arrive("composer")
        owner.activate { launches += 1 }
        XCTAssertEqual(launches, 1)
        XCTAssertNil(owner.state.button)
        now = 2
        owner.returnedFromPresentation() // Cancel without changing photo selection.
        let returned = owner.state.generation
        XCTAssertEqual(owner.state.screen?.began, 2)
        owner.arrive("composer")
        XCTAssertEqual(owner.state.generation, returned)
        owner.cancel(surface: "composer")
        now = 4
        owner.returnedFromPresentation() // Dismissal callback before completed appearance.
        XCTAssertNil(owner.state.screen)
        owner.arrive("composer")
        let appeared = owner.state.generation
        owner.returnedFromPresentation() // Or callback after appearance.
        XCTAssertEqual(owner.state.generation, appeared)
        XCTAssertEqual(owner.state.screen?.began, 4)
    }
    func testDelayedSchedulerStartStillExpiresItsEpisode() async throws {
        var now = 0.0
        let owner = DecryptFeedback(now: { now })
        owner.arrive("public")
        now = 10 // Main-actor work prevented the task starting before its deadline.
        await Task.yield()
        try await Task.sleep(for: .milliseconds(10))
        XCTAssertNil(owner.state.screen)
        XCTAssertNil(owner.state.button)
    }
    func testReduceMotionCancellationAndFreshArrival() {
        var now = 0.0
        let owner = DecryptFeedback(now: { now }, schedules: false)
        owner.arrive("a")
        owner.setReduced(true)
        XCTAssertEqual(owner.state.screen?.frame(at: now, reduced: owner.reduced), "OPEN")
        XCTAssertNil(owner.state.button)
        owner.cancel(); now = 10; owner.advance()
        XCTAssertNil(owner.state.screen)
        owner.arrive("a")
        XCTAssertEqual(owner.state.screen?.began, 10)
    }
    func testNativeNavigationCompletedArrivalAndBack() async throws {
        let owner = DecryptFeedback(schedules: false)
        let root = UIHostingController(rootView: Text("Root").decryptSurface("root")
            .environment(\.decryptFeedback, owner).environment(\.scenePhase, .active))
        let navigation = UINavigationController(rootViewController: root)
        let window = try window()
        window.rootViewController = navigation; window.makeKeyAndVisible()
        defer { window.isHidden = true }
        try await Task.sleep(for: .milliseconds(100))
        let first = owner.state.surface
        XCTAssertNotNil(first)
        let detail = UIHostingController(rootView: Text("Detail").decryptSurface("detail")
            .environment(\.decryptFeedback, owner).environment(\.scenePhase, .active))
        navigation.pushViewController(detail, animated: false)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertNotNil(owner.state.surface)
        XCTAssertNotEqual(owner.state.surface, first)
        navigation.popViewController(animated: false)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(owner.state.surface, first)
    }
    func testHostedTabVisibilityAndSceneCancellation() async throws {
        let owner = DecryptFeedback(schedules: false)
        func page(active: Bool, phase: ScenePhase) -> some View {
            Text("Kept draft").decryptSurface("tab").environment(\.decryptFeedback, owner)
                .environment(\.decryptActive, active).environment(\.scenePhase, phase)
        }
        let host = UIHostingController(rootView: page(active: false, phase: .active))
        let window = try window()
        window.rootViewController = host; window.makeKeyAndVisible()
        defer { window.isHidden = true }
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertNil(owner.state.surface)
        host.rootView = page(active: true, phase: .active)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertNotNil(owner.state.surface)
        host.rootView = page(active: true, phase: .inactive)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertNil(owner.state.surface)
        host.rootView = page(active: true, phase: .active)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertNotNil(owner.state.surface)
        host.rootView = page(active: false, phase: .active)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertNil(owner.state.surface)
    }

    private final class Route: ObservableObject {
        @Published var path: [Int] = []
        @Published var phase = ScenePhase.active
    }
    private struct NavigationFixture: View {
        @ObservedObject var route: Route
        let owner: DecryptFeedback
        var body: some View {
            NavigationStack(path: $route.path) {
                Text("Root")
                    .navigationDestination(for: Int.self) { value in
                        Text("Destination \(value)").decryptSurface("nested")
                    }
                    .decryptSurface("root")
            }
            .environment(\.decryptFeedback, owner)
            .environment(\.scenePhase, route.phase)
        }
    }
    func testSwiftUINavigationStackKeepsNestedOwnershipOnForeground() async throws {
        let owner = DecryptFeedback(schedules: false)
        let route = Route()
        let host = UIHostingController(rootView: NavigationFixture(route: route, owner: owner))
        let window = try window()
        window.rootViewController = host; window.makeKeyAndVisible()
        defer { window.isHidden = true }
        // A navigation push is an animation: on a loaded CI runner it can
        // outlast a fixed sleep (a 600 ms wait failed on 22 Sep 2026), so each
        // transition is awaited until it lands, with a generous ceiling.
        try await settle { owner.state.surface != nil }
        let root = try XCTUnwrap(owner.state.surface)
        let generation = owner.state.generation
        route.path = [1]
        try await settle { owner.state.surface != nil && owner.state.surface != root
            && owner.state.generation == generation + 2 }
        let nested = try XCTUnwrap(owner.state.surface)
        XCTAssertNotEqual(root, nested)
        // One completed departure cancels the old generation; one arrival starts the new one.
        XCTAssertEqual(owner.state.generation, generation + 2)
        XCTAssertEqual(owner.state.screen?.kind, .screen)
        let settledGeneration = owner.state.generation
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(owner.state.generation, settledGeneration)
        route.phase = .inactive
        try await settle { owner.state.surface == nil }
        XCTAssertNil(owner.state.surface)
        route.phase = .active
        try await settle { owner.state.surface == nested }
        XCTAssertEqual(owner.state.surface, nested)
        route.path = []
        try await settle { owner.state.surface == root }
        XCTAssertEqual(owner.state.surface, root)
    }

    /// Polls until `done` holds or five seconds pass; the assertions after
    /// it say what was wrong when it never does.
    private func settle(_ done: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(5)
        while !done() && Date() < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
    }

    func testCaptionGeometryAndHostedScreenshots() async throws {
        for width in [320.0, 375.0, 430.0] {
            for category in [UIContentSizeCategory.large, .accessibilityExtraExtraExtraLarge] {
                let owner = DecryptFeedback(schedules: false)
                let content = ScrollView { VStack(alignment: .leading, spacing: 16) {
                    Text("A real confirmation stays readable and wraps.").font(Theme.mono(17))
                        .fixedSize(horizontal: false, vertical: true)
                    DecryptButton("CONFIRM ACTION") {}.buttonStyle(AlarmOutline())
                    DecryptButton("Plain selection") {}
                        .buttonStyle(.plain).font(Theme.mono(13)).foregroundStyle(Theme.phosphor)
                    Text("Offline. The Mac could not be reached.").font(Theme.mono(13))
                }.padding().frame(maxWidth: .infinity, alignment: .leading)
                }.background(Theme.bg).preferredColorScheme(.dark).decryptSurface("preview")
                    .environment(\.decryptFeedback, owner).environment(\.scenePhase, .active)
                let host = UIHostingController(rootView: content)
                let window = try window(width: width, height: 850)
                window.rootViewController = host; window.makeKeyAndVisible()
                host.traitOverrides.preferredContentSizeCategory = category
                // Presentation completion is asynchronous under simulator load.
                // Wait for the real appearance callback, never synthesize one.
                for _ in 0..<100 where owner.state.screen == nil {
                    try await Task.sleep(for: .milliseconds(20))
                }
                host.view.layoutIfNeeded()
                XCTAssertEqual(host.view.bounds.width, width)
                XCTAssertNotNil(owner.state.screen)
                let renderer = UIGraphicsImageRenderer(bounds: host.view.bounds)
                let snapshot = renderer.image { _ in host.view.drawHierarchy(in: host.view.bounds, afterScreenUpdates: true) }
                let attachment = XCTAttachment(image: snapshot)
                attachment.name = "decrypt-\(Int(width))-\(category.rawValue)"; attachment.lifetime = .keepAlways
                add(attachment); window.isHidden = true
                let caption = UIHostingController(rootView: DecryptCaption(caption: "OPEN", frame: "#%+/"))
                let before = caption.sizeThatFits(in: CGSize(width: width, height: 200))
                caption.rootView = DecryptCaption(caption: "OPEN", frame: "OPEN")
                let after = caption.sizeThatFits(in: CGSize(width: width, height: 200))
                XCTAssertEqual(before, after)
                XCTAssertLessThanOrEqual(after.width, width)
            }
        }
    }
}
