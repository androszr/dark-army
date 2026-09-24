import XCTest
@testable import BobPanel

/// + TERMINAL's three pure rules, driven with no window.
///
/// The point of `AdhocSpawn` is that the decision to draw the button is a
/// table rather than a `body`, so every way it can be absent is a case here.
final class AdhocTerminalTests: XCTestCase {

    private func project(_ root: String, _ label: String) -> EnrolledProject {
        EnrolledProject(root: root, label: label)
    }

    private var enrolledTwo: [EnrolledProject] {
        [project("/a/bob", "bob"), project("/a/other", "other")]
    }

    // MARK: - shouldOffer

    func testEverythingPresentOffersTheButton() {
        XCTAssertTrue(AdhocSpawn.shouldOffer(
            dispatchEnabled: true, enrollmentAvailable: true,
            enrolled: enrolledTwo, tools: ["claude", "codex"]))
    }

    func testLauncherSwitchedOffDrawsNothing() {
        XCTAssertFalse(AdhocSpawn.shouldOffer(
            dispatchEnabled: false, enrollmentAvailable: true,
            enrolled: enrolledTwo, tools: ["claude"]))
    }

    /// "The daemon did not say" and "nothing is enrolled" stay different
    /// answers — `Enrollment.available` is a stated field, and the button
    /// must not appear against a daemon that publishes no ledger at all.
    func testAnUnstatedEnrolmentLedgerDrawsNothing() {
        XCTAssertFalse(AdhocSpawn.shouldOffer(
            dispatchEnabled: true, enrollmentAvailable: false,
            enrolled: enrolledTwo, tools: ["claude"]))
    }

    func testNoEnrolledFolderDrawsNothing() {
        XCTAssertFalse(AdhocSpawn.shouldOffer(
            dispatchEnabled: true, enrollmentAvailable: true,
            enrolled: [], tools: ["claude"]))
    }

    func testNoLaunchableAssistantDrawsNothing() {
        XCTAssertFalse(AdhocSpawn.shouldOffer(
            dispatchEnabled: true, enrollmentAvailable: true,
            enrolled: enrolledTwo, tools: []))
    }

    // MARK: - candidates

    func testCandidatesKeepTheLedgersOwnOrder() {
        let out = AdhocSpawn.candidates(enrolledTwo)
        XCTAssertEqual(out.map(\.root), ["/a/bob", "/a/other"])
        XCTAssertEqual(out.map(\.label), ["bob", "other"])
    }

    func testCandidatesOfNothingIsNothing() {
        XCTAssertTrue(AdhocSpawn.candidates([]).isEmpty)
    }

    // MARK: - canStart

    func testStartNeedsBothAFolderAndAnAssistant() {
        XCTAssertTrue(AdhocSpawn.canStart(root: "/a/bob", tool: "claude"))
        XCTAssertFalse(AdhocSpawn.canStart(root: "", tool: "claude"))
        XCTAssertFalse(AdhocSpawn.canStart(root: "/a/bob", tool: ""))
        XCTAssertFalse(AdhocSpawn.canStart(root: "", tool: ""))
    }

    // MARK: - the wire
}
