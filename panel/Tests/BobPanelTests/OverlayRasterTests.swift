import AppKit
import SwiftUI
import XCTest
@testable import BobPanel

/// Signal reading surfaces are plain at 1x and Retina scale. The legacy CRT
/// overlays still render in the comparison to prove the pixel check has teeth.
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

    func testScanlineOverlayLeavesReadingSurfaceUntouched() throws {
        for scale in Self.scales {
            for (name, ground) in Self.grounds {
                let plain = try render(Optional<EmptyView>.none, scale: scale, ground: ground)
                let surface = try render(ScanlineOverlay(), scale: scale, ground: ground)
                assertSame(plain, surface, "Signal surface on \(name) @\(Int(scale))x")
            }
        }
    }

    func testVignetteOverlayLeavesReadingSurfaceUntouched() throws {
        for scale in Self.scales {
            for (name, ground) in Self.grounds {
                for size in [Self.strip, Self.pane] {
                    let plain = try render(Optional<EmptyView>.none, scale: scale,
                                           ground: ground, size: size)
                    let surface = try render(VignetteOverlay(), scale: scale,
                                             ground: ground, size: size)
                    assertSame(plain, surface,
                               "Signal surface on \(name) \(Int(size.width))pt @\(Int(scale))x")
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
