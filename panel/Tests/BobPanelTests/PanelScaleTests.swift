import AppKit
import XCTest
@testable import BobPanel

final class PanelScaleTests: XCTestCase {

    func testStepsAreTheFourNamedPercentsInAscendingOrder() {
        XCTAssertEqual(PanelScale.steps.map(\.label),
                       ["100%", "125%", "150%", "175%"])
        XCTAssertEqual(PanelScale.steps.map(\.percent),
                       [100, 125, 150, 175])
    }

    func testResolvedReturnsTheInputForEachOfferedStep() {
        for percent in [100, 125, 150, 175] {
            XCTAssertEqual(PanelScale.resolved(percent), percent)
        }
    }

    func testResolvedFallsBackToDefaultForUnknownPercents() {
        XCTAssertEqual(PanelScale.defaultPercent, 100)
        for percent in [0, -1, 99, 126, 300, Int.max] {
            XCTAssertEqual(PanelScale.resolved(percent), PanelScale.defaultPercent,
                           "percent \(percent)")
        }
    }

    func testFactorMatchesTheOfferedStepsAndGarbageIsOne() {
        XCTAssertEqual(PanelScale.factor(100), 1.0)
        XCTAssertEqual(PanelScale.factor(125), 1.25)
        XCTAssertEqual(PanelScale.factor(150), 1.5)
        XCTAssertEqual(PanelScale.factor(175), 1.75)
        XCTAssertEqual(PanelScale.factor(300), 1.0)
        XCTAssertEqual(PanelScale.factor(0), 1.0)
    }

    func testLogicalSizeDividesByTheFactorAndDoesNotDivideByZero() {
        let size = PanelScale.logicalSize(NSSize(width: 1400, height: 900),
                                          factor: 1.75)
        XCTAssertEqual(size.width, 800)
        XCTAssertEqual(size.height, 900 / 1.75, accuracy: 0.01)
        let zero = PanelScale.logicalSize(NSSize(width: 1400, height: 900),
                                          factor: 0)
        XCTAssertEqual(zero.width, 1400 / 0.01, accuracy: 0.01)
        XCTAssertEqual(zero.height, 900 / 0.01, accuracy: 0.01)
    }

    func testMinContentSizeScalesTheLogicalFloor() {
        let size = PanelScale.minContentSize(factor: 1.75)
        XCTAssertEqual(size.width, PanelPlacement.minWidth * 1.75)
        XCTAssertEqual(size.height, PanelPlacement.minHeight * 1.75)
    }

    func testClampedWithScaleFloorsToTheScaledMinimum() {
        let frame = PanelPlacement.clamped(
            NSRect(x: 0, y: 0, width: 520, height: 400),
            to: NSRect(x: 0, y: 0, width: 3000, height: 2000),
            scale: 1.75)
        XCTAssertEqual(frame.width, 910)
        XCTAssertEqual(frame.height, 630)
    }

    func testCappedContentMinSizeNeverExceedsTheVisibleFrame() {
        let wanted = NSSize(width: 1085, height: 1085)
        let tiny = PanelScale.cappedContentMinSize(
            wanted, visible: NSSize(width: 800, height: 600))
        XCTAssertEqual(tiny.width, 800)
        XCTAssertEqual(tiny.height, 600)
        let roomy = PanelScale.cappedContentMinSize(
            NSSize(width: 620, height: 620),
            visible: NSSize(width: 3000, height: 2000))
        XCTAssertEqual(roomy.width, 620)
        XCTAssertEqual(roomy.height, 620)
        let missing = PanelScale.cappedContentMinSize(
            wanted, visible: .zero)
        XCTAssertEqual(missing.width, 1085)
        XCTAssertEqual(missing.height, 1085)
    }

    func testClampedWithoutScaleKeepsTodaysFloor() {
        let visible = NSRect(x: 0, y: 0, width: 1600, height: 1000)
        let sliver = NSRect(x: 100, y: 100, width: 40, height: 30)
        let frame = PanelPlacement.clamped(sliver, to: visible)
        XCTAssertEqual(frame.width, PanelPlacement.minWidth)
        XCTAssertEqual(frame.height, PanelPlacement.minHeight)
    }

    func testGrownFrameOnTheWayUpMultipliesTheCurrentSize() {
        let current = NSRect(x: 100, y: 80, width: 1400, height: 800)
        let visible = NSRect(x: 0, y: 0, width: 3000, height: 2000)
        let grown = PanelScale.grownFrame(current, from: 1.0, to: 1.75,
                                          visible: visible)
        XCTAssertEqual(grown.width, 1400 * 1.75)
        XCTAssertEqual(grown.height, 800 * 1.75)
        XCTAssertEqual(grown.origin.x, 100)
        XCTAssertEqual(grown.origin.y, 80)
    }

    func testGrownFrameOnTheWayDownLeavesTheSize() {
        let current = NSRect(x: 100, y: 80, width: 2450, height: 1400)
        let visible = NSRect(x: 0, y: 0, width: 3000, height: 2000)
        let down = PanelScale.grownFrame(current, from: 1.75, to: 1.0,
                                         visible: visible)
        XCTAssertEqual(down.width, 2450)
        XCTAssertEqual(down.height, 1400)
        XCTAssertEqual(down.origin, current.origin)
    }

    func testGrownFrameClampsToTheVisibleFrameAtTheNewFactor() {
        let current = NSRect(x: 0, y: 0, width: 1400, height: 800)
        let tiny = NSRect(x: 0, y: 0, width: 1600, height: 900)
        let grown = PanelScale.grownFrame(current, from: 1.0, to: 1.75,
                                          visible: tiny)
        XCTAssertEqual(grown.width, 1600)
        XCTAssertEqual(grown.height, 900)
        let sliver = NSRect(x: 40, y: 40, width: 200, height: 100)
        let roomy = NSRect(x: 0, y: 0, width: 3000, height: 2000)
        let floored = PanelScale.grownFrame(sliver, from: 1.0, to: 1.75,
                                            visible: roomy)
        XCTAssertEqual(floored.width, PanelPlacement.minWidth * 1.75)
        XCTAssertEqual(floored.height, PanelPlacement.minHeight * 1.75)
    }
}
