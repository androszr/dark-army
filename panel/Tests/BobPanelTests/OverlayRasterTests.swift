import AppKit
import SwiftUI
import XCTest
@testable import BobPanel

/// The CRT overlays stopped costing a frame without changing a pixel.
///
/// `LegacyScanlines` and `LegacyVignette` are the two bodies exactly as they
/// stood before (a `Canvas` stroking one line per 3pt; a gradient under the
/// multiply blend mode). Each is rendered beside its replacement, at a
/// non-Retina and a Retina scale, and every channel of every pixel must agree
/// within one step in 255. Two grounds: `Theme.well`, what the board really
/// draws on — and so dark (2, 4, 2) that neither overlay moves a channel
/// there by more than one step, which alone would make a ±1 comparison
/// vacuous — and `Theme.phosphor`, bright enough that both overlays move it
/// by tens of steps. A third case proves that margin, so the comparison
/// cannot pass by both sides drawing (next to) nothing. The vignette is also
/// rendered at a board-sized frame: at 300×90 its corners sit inside the
/// gradient's first sixth and barely darken.
@MainActor
final class OverlayRasterTests: XCTestCase {
    private nonisolated static let strip = CGSize(width: 300, height: 90)
    private static let pane = CGSize(width: 1000, height: 700)
    private static let scales: [CGFloat] = [1, 2]
    private static let grounds: [(String, Color)] = [
        ("well", Theme.well), ("phosphor", Theme.phosphor),
    ]

    /// One render, redrawn into a fixed RGBA8 sRGB bitmap so two renders are
    /// compared in the same byte layout whatever format the renderer chose.
    private struct Raster {
        let width: Int
        let height: Int
        let bytes: [UInt8]
    }

    private func render<Overlay: View>(_ overlay: Overlay?, scale: CGFloat,
                                       ground: Color = Theme.well,
                                       size: CGSize = strip,
                                       file: StaticString = #filePath,
                                       line: UInt = #line) throws -> Raster {
        let content = ground
            .frame(width: size.width, height: size.height)
            .overlay { if let overlay { overlay } }
            .environment(\.displayScale, scale)
        let renderer = ImageRenderer(content: content)
        renderer.scale = scale
        let image = try XCTUnwrap(renderer.cgImage, "nothing rendered", file: file, line: line)
        let width = image.width
        let height = image.height
        var bytes = [UInt8](repeating: 0, count: width * height * 4)
        let drawn = bytes.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress, width: width, height: height,
                bitsPerComponent: 8, bytesPerRow: width * 4,
                space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
            else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        XCTAssertTrue(drawn, "could not redraw the render", file: file, line: line)
        return Raster(width: width, height: height, bytes: bytes)
    }

    /// The largest difference in any channel of any pixel, and where.
    private func worst(_ a: Raster, _ b: Raster) -> (delta: Int, pixel: Int) {
        var delta = 0
        var pixel = -1
        for i in a.bytes.indices {
            let d = abs(Int(a.bytes[i]) - Int(b.bytes[i]))
            if d > delta { delta = d; pixel = i / 4 }
        }
        return (delta, pixel)
    }

    private func assertSame(_ legacy: Raster, _ new: Raster, _ what: String,
                            file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(legacy.width, new.width, "\(what) width", file: file, line: line)
        XCTAssertEqual(legacy.height, new.height, "\(what) height", file: file, line: line)
        guard legacy.bytes.count == new.bytes.count else { return }
        let (delta, pixel) = worst(legacy, new)
        XCTAssertLessThanOrEqual(
            delta, 1,
            "\(what): off by \(delta)/255 at x=\(pixel % max(1, legacy.width)), "
            + "y=\(pixel / max(1, legacy.width))", file: file, line: line)
    }

    func testTheScanlineTileMatchesTheCanvasAtEveryScale() throws {
        for scale in Self.scales {
            for (name, ground) in Self.grounds {
                let legacy = try render(LegacyScanlines(), scale: scale, ground: ground)
                let tiled = try render(ScanlineOverlay(), scale: scale, ground: ground)
                XCTAssertEqual(legacy.width, Int(Self.strip.width * scale))
                assertSame(legacy, tiled, "scanlines on \(name) @\(Int(scale))x")
            }
        }
    }

    func testTheNormalBlendVignetteMatchesTheMultiplyAtEveryScale() throws {
        for scale in Self.scales {
            for (name, ground) in Self.grounds {
                for size in [Self.strip, Self.pane] {
                    let legacy = try render(LegacyVignette(), scale: scale,
                                            ground: ground, size: size)
                    let normal = try render(VignetteOverlay(), scale: scale,
                                            ground: ground, size: size)
                    assertSame(legacy, normal,
                               "vignette on \(name) \(Int(size.width))pt @\(Int(scale))x")
                }
            }
        }
    }

    func testTheLegacyOverlaysDrawSomethingSoTheComparisonIsNotVacuous() throws {
        for scale in Self.scales {
            let plain = try render(Optional<EmptyView>.none, scale: scale,
                                   ground: Theme.phosphor)
            let scan = try render(LegacyScanlines(), scale: scale,
                                  ground: Theme.phosphor)
            XCTAssertGreaterThan(worst(plain, scan).delta, 10,
                                 "the Canvas scanlines barely drew @\(Int(scale))x")
            let plainPane = try render(Optional<EmptyView>.none, scale: scale,
                                       ground: Theme.phosphor, size: Self.pane)
            let vignette = try render(LegacyVignette(), scale: scale,
                                      ground: Theme.phosphor, size: Self.pane)
            XCTAssertGreaterThan(worst(plainPane, vignette).delta, 10,
                                 "the multiply vignette barely drew @\(Int(scale))x")
        }
    }

    func testTheTileIsOnePeriodAtThePixelScale() {
        let one = ScanlineTile.image(scale: 1)
        XCTAssertEqual([one.width, one.height], [1, 3])
        let two = ScanlineTile.image(scale: 2)
        XCTAssertEqual([two.width, two.height], [2, 6])
        XCTAssertTrue(ScanlineTile.image(scale: 2) === two, "one tile per scale, cached")
    }
}

// MARK: - The bodies as they stood before

/// `ScanlineOverlay` before it became a tile, verbatim.
private struct LegacyScanlines: View {
    var body: some View {
        Canvas { context, size in
            let ink = Color.black.opacity(0.18)
            var y: CGFloat = 2
            while y < size.height {
                var path = Path()
                path.move(to: CGPoint(x: 0, y: y))
                path.addLine(to: CGPoint(x: size.width, y: y))
                context.stroke(path, with: .color(ink), lineWidth: 1)
                y += 3
            }
        }
        .allowsHitTesting(false)
    }
}

/// `VignetteOverlay` before it dropped the multiply blend, verbatim.
private struct LegacyVignette: View {
    var body: some View {
        RadialGradient(
            colors: [Color.clear, Color.black.opacity(0.45)],
            center: .center,
            startRadius: 40,
            endRadius: 720)
        .blendMode(.multiply)
        .allowsHitTesting(false)
    }
}
