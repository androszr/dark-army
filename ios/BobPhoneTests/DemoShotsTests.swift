import SwiftUI
import UIKit
import XCTest
@testable import BobPhone

/// The made-up working day the Mac's front-page pictures are drawn from
/// (`panel/Tests/Fixtures/demo-shots.json`), read by the phone.
///
/// `testDemoDayDecodesOnPhone`: the day must decode through the phone's
/// `Snapshot` and leave something under Needs you, so the phone keeps
/// reading the same day the Mac does.
///
/// `testRenderPhoneShots` draws the iPhone screens of the README's showcase
/// slides (`docs/images/SHOTS.md`, *The showcase slides*): Fleet, Board and
/// Cipher's sheet on Main, with his card's journey. It runs only when
/// `BOB_DEMO_SHOTS_OUT` is set (as `TEST_RUNNER_BOB_DEMO_SHOTS_OUT`) on a
/// throwaway simulator. The client is never started — no pairing record, no
/// poll — so nothing leaves the simulator; the tab and the sheet are chosen
/// through the router's own deep-link slot.
@MainActor
final class DemoShotsTests: XCTestCase {

    static let fixtureURL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent()
        .appendingPathComponent("panel/Tests/Fixtures/demo-shots.json")

    static let timeKeys: Set<String> = ["generated_at", "last_event", "since",
                                        "started_at", "quiet_since"]

    static func shifted(_ value: Any, by delta: Double) -> Any {
        if let dict = value as? [String: Any] {
            var out: [String: Any] = [:]
            for (key, inner) in dict {
                let isTime = timeKeys.contains(key) || key.hasSuffix("_at")
                    || key.hasSuffix("_since")
                if isTime, let number = inner as? NSNumber,
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

    /// The day's main frame, every clock moved to now.
    static func mainFrame() throws -> Snapshot {
        let raw = try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(contentsOf: fixtureURL)) as? [String: Any])
        let meta = try XCTUnwrap(raw["metadata"] as? [String: Any])
        XCTAssertEqual(meta["synthetic"] as? Bool, true)
        let base = try XCTUnwrap((meta["base_epoch"] as? NSNumber)?.doubleValue)
        let now = Date().timeIntervalSince1970
        let frames = try XCTUnwrap(raw["frames"] as? [String: Any])
        let main = shifted(try XCTUnwrap(frames["main"]), by: now - base)
        return try JSONDecoder().decode(Snapshot.self,
                                        from: JSONSerialization.data(withJSONObject: main))
    }

    func testDemoDayDecodesOnPhone() throws {
        let snapshot = try Self.mainFrame()
        XCTAssertGreaterThanOrEqual(snapshot.needsYouCount, 1)
        let fleet = snapshot.agents.running.count + snapshot.agents.waiting.count
            + snapshot.agents.sleeping.count
        XCTAssertEqual(fleet, 6)
        XCTAssertEqual(snapshot.agents.waiting.first?.nickname, "Cipher")
    }

    func testRenderPhoneShots() throws {
        let env = ProcessInfo.processInfo.environment
        guard let outPath = env["BOB_DEMO_SHOTS_OUT"], !outPath.isEmpty else {
            throw XCTSkip("Set BOB_DEMO_SHOTS_OUT to render the phone's showcase screens.")
        }
        let out = URL(fileURLWithPath: outPath, isDirectory: true)
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }.first)
        let client = PhoneClient()
        client.snapshot = try Self.mainFrame()
        client.status = .live
        client.lastHeard = Date()

        let window = UIWindow(windowScene: scene)
        window.windowLevel = .alert + 1
        window.overrideUserInterfaceStyle = .dark
        window.rootViewController = UIHostingController(rootView: ContentView(
            client: client, pairing: PairingStore(), outbox: OutboxStore()))
        window.makeKeyAndVisible()
        defer { window.isHidden = true }

        func wait(_ seconds: TimeInterval) {
            RunLoop.main.run(until: Date().addingTimeInterval(seconds))
        }
        // Past the Needs you decrypt, so the tabs draw settled.
        wait(DecryptMotion.screenDuration + DecryptMotion.finalHold + 0.6)

        func tab(_ destination: PhoneTab, named file: String) throws {
            PhoneRouter.shared.go(destination)
            wait(1.2)
            try write(window, to: out.appendingPathComponent(file))
        }
        try tab(.fleet, named: "phone-fleet.png")
        try tab(.board, named: "phone-board.png")

        // The widget's own deep link: Cipher's sheet, opening on Main.
        let cipher = try XCTUnwrap(client.snapshot.agents.waiting
            .first { $0.nickname == "Cipher" })
        let url = try XCTUnwrap(URL(string: "\(FleetLinks.scheme)://fleet?session=\(cipher.sessionId)"))
        PhoneRouter.shared.open(url)
        wait(1.5)
        try write(window, to: out.appendingPathComponent("phone-cipher.png"))
    }

    /// The window as the screen draws it, at the device's own scale, in
    /// standard sRGB so the compositor needs no profile.
    private func write(_ window: UIWindow, to url: URL) throws {
        let format = UIGraphicsImageRendererFormat()
        format.scale = window.screen.scale
        format.preferredRange = .standard
        let renderer = UIGraphicsImageRenderer(bounds: window.bounds, format: format)
        let image = renderer.image { _ in
            _ = window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        try XCTUnwrap(image.pngData()).write(to: url)
    }
}
