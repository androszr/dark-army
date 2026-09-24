import XCTest
@testable import BobPhone

/// The made-up working day the Mac's front-page pictures are drawn from
/// (`panel/Tests/Fixtures/demo-shots.json`), read by the phone.
///
/// `testDemoDayDecodesOnPhone`: the day must decode through the phone's
/// `Snapshot` and leave something under Needs you. The render that drew the
/// front page's phone picture moved out with that picture on 23 Sep 2026;
/// the decode stays, so the phone keeps reading the same day the Mac does.
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
}
