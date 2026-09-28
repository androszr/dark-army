import XCTest
@testable import BobPanel

/// `DockToggle`: the Dock icon shows a hidden window, hides a window that is
/// already in front, and fronts — never hides — a window the same click has
/// just pulled from behind another app.
final class DockToggleTests: XCTestCase {

    private let t0 = Date(timeIntervalSinceReferenceDate: 1_000_000)

    func testAHiddenWindowIsShown() {
        XCTAssertEqual(DockToggle.verdict(seen: false, active: true,
                                          becameActive: nil, now: t0), .show)
        XCTAssertEqual(DockToggle.verdict(seen: false, active: false,
                                          becameActive: nil, now: t0), .show)
    }

    func testAWindowInFrontOfAnActiveAppIsHidden() {
        XCTAssertEqual(DockToggle.verdict(seen: true, active: true,
                                          becameActive: t0.addingTimeInterval(-30),
                                          now: t0), .hide)
        XCTAssertEqual(DockToggle.verdict(seen: true, active: true,
                                          becameActive: nil, now: t0), .hide)
    }

    func testAWindowOfAnInactiveAppIsShown() {
        XCTAssertEqual(DockToggle.verdict(seen: true, active: false,
                                          becameActive: t0.addingTimeInterval(-30),
                                          now: t0), .show)
    }

    func testAClickThatJustActivatedTheAppFrontsRatherThanHides() {
        XCTAssertEqual(DockToggle.verdict(seen: true, active: true,
                                          becameActive: t0.addingTimeInterval(-0.05),
                                          now: t0), .show)
    }

    func testTheActivationWindowEdge() {
        let edge = t0.addingTimeInterval(-DockToggle.activationWindow)
        XCTAssertEqual(DockToggle.verdict(seen: true, active: true,
                                          becameActive: edge, now: t0), .hide)
    }
}
