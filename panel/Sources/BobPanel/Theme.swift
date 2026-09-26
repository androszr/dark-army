import AppKit
import SwiftUI

/// Signal's semantic palette. Generated values come from
/// `design-system/tokens.json`; aliases keep older drawing sites compatible.
enum Theme {
    static let canvas = SignalTokens.canvas
    static let surface = SignalTokens.surface
    static let raised = SignalTokens.raised
    static let field = SignalTokens.well
    static let text = SignalTokens.text
    static let muted = SignalTokens.muted
    static let accent = SignalTokens.accent
    static let accentInk = SignalTokens.accentInk
    static let line = SignalTokens.line
    static let control = SignalTokens.control
    static let attention = SignalTokens.attention
    static let danger = SignalTokens.danger
    static let controlRadius = SignalTokens.Radii.control
    static let cardRadius = SignalTokens.Radii.card

    static let bg = canvas
    static let bar = surface
    static let well = field
    static let phosphor = accent
    static let phosphorBright = text
    static let dim = muted
    static let faint = muted
    static let hair = line
    static let rule = line
    static let alarm = danger
    static let amber = attention
    static let card = raised
    static let corner: CGFloat = 6

    static var nsBg: NSColor {
        // Derived, never typed: the window edge follows `tokens.json`.
        NSColor(SignalTokens.canvas)
    }

    static func prose(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .default)
    }

    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

/// A face on a dark square. BrandBar's mark, lifted, so the board
/// header and a live card cannot invent a second one.
///
/// Draws the still portrait out of `Cast.portrait`, smoothly scaled and
/// clipped to the tile, with the state rule laid along the bottom edge —
/// solid while working, dashed while asleep, red while it needs someone —
/// because one still says nothing about state on its own. A character with
/// no portrait yet shows its initial in `Theme.faint`: never a hashed
/// stranger, which reads as a bug where an initial reads as "not chosen".
///
/// Not `AvatarView`: that view is the bigger, circular treatment for the
/// sheet and the empty state; this one is the row-sized square.
struct PixelMark: View {
    var character: String
    var state: Cast.State
    var size: CGFloat = 20

    var body: some View {
        ZStack(alignment: .bottom) {
            RoundedRectangle(cornerRadius: Theme.corner)
                .fill(Theme.bg)
            if let image = Cast.portrait(character) {
                Image(nsImage: image)
                    .interpolation(.high)
                    .resizable()
                    .scaledToFill()
                    .frame(width: size - 2, height: size - 2)
                    .clipShape(RoundedRectangle(cornerRadius: Theme.corner))
                    .opacity(AvatarView.portraitDim)
                    .padding(.bottom, 1)
            } else {
                // Not chosen yet: the character's own initial.
                Text(AvatarView.initial(of: character))
                    .font(Theme.mono(size * 0.5, weight: .medium))
                    .foregroundStyle(Theme.faint)
                    .frame(width: size, height: size)
            }
            RoundedRectangle(cornerRadius: Theme.corner)
                .strokeBorder(Theme.hair, lineWidth: 1)
            StateRule(state: state, width: size * 0.6)
                .padding(.bottom, 2)
        }
        .frame(width: size, height: size)
        // The face is information — who this is and whether they are working,
        // asleep or need someone — so it is named, not hidden. Rows that
        // already speak both facts (`ProcessRow`) put their own label on the
        // whole row, which supersedes this one.
        .accessibilityLabel("\(character), \(state.rawValue)")
    }
}

/// Dark Army's mark: the mask, with a clear ground.
///
/// Not a cast face and never a pose. The prompt subtitle and the 2pt rule
/// under the bar already carry who is waiting. One PNG, byte-identical on
/// the panel and the phone. The Dock tile is this mask painted in Dark
/// Army green by `tools/app_icon_bake.py`, not this file itself.
///
/// Full colour, so it is never tinted. The ground is clear: only the mask
/// shows on the bar. Continuous tone, so it scales smoothly.
struct BrandMark: View {
    var size: CGFloat = 20

    var body: some View {
        if let image = Self.image {
            Image(nsImage: image)
                .interpolation(.high)
                .resizable()
                .scaledToFit()
                .frame(width: size, height: size)
                .accessibilityHidden(true)
        }
    }

    /// Loaded once. `NSImage(contentsOf:)` hits the disk, and this mark is
    /// drawn on every push the bar redraws for.
    private static let image: NSImage? = {
        guard let url = PanelResources.url(folder: "brand", file: "fsociety-mark.png")
        else { return nil }
        return NSImage(contentsOf: url)
    }()
}

/// `root@darkarmy:<path>$ [command]`. No block cursor — a blink is a clock.
struct PromptLine: View {
    var path: String
    var command: String = ""
    var commandColor: Color? = nil
    var size: CGFloat = 12

    var body: some View {
        HStack(spacing: 0) {
            Text("root@darkarmy:")
                .foregroundStyle(Theme.phosphor)
            Text(path)
                .foregroundStyle(Theme.phosphorBright)
            Text("$")
                .foregroundStyle(Theme.phosphor)
            if !command.isEmpty {
                Text(" \(command)")
                    .foregroundStyle(commandColor ?? Theme.phosphor)
            }
        }
        .font(Theme.mono(size))
        .lineLimit(1)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spoken)
    }

    private var spoken: String {
        command.isEmpty ? "root@darkarmy:\(path)$" : "root@darkarmy:\(path)$ \(command)"
    }
}

/// Dust on a tube. Static 1pt black lines every 3pt, never a clock.
/// Hit-testing is off: a scanline that eats a drop is a board that
/// cannot start a card.
///
/// **A tile, not a drawing.** This was a `Canvas` stroking one line per
/// 3pt of pane height — some 470 strokes on a tall board, re-rasterised on
/// every redraw of the pane under it. The pattern repeats every 3pt, so it
/// is now one 1pt × 3pt tile (`ScanlineTile`) drawn once per display scale
/// with the identical stroke, and repeated by the compositor. Rasterised at
/// the scale it is shown at, so a 1× screen gets the same half-and-half
/// antialiased rows the `Canvas` gave it rather than a 2× tile scaled down.
/// Anchored at the pane's top, as the `Canvas`'s first line was.
struct ScanlineOverlay: View {
    var body: some View {
        EmptyView()
    }
}

/// The scanline pattern's one period, rasterised once per display scale.
@MainActor
enum ScanlineTile {
    /// One tile per scale the panel has been shown at — two at most in
    /// practice (a Retina screen and an external one).
    private static var cache: [CGFloat: CGImage] = [:]

    /// A 1pt × 3pt tile at `scale` pixels per point: 1pt of black at 18%
    /// stroked across it, centred on y = 2 from the top — `ScanlineOverlay`'s
    /// old `Canvas` line, drawn by the same CoreGraphics stroke.
    static func image(scale: CGFloat) -> CGImage {
        let scale = max(1, scale)
        if let hit = cache[scale] { return hit }
        let image = draw(scale: scale)
        cache[scale] = image
        return image
    }

    private static func draw(scale: CGFloat) -> CGImage {
        let width = Int((1 * scale).rounded())
        let height = Int((3 * scale).rounded())
        let context = CGContext(
            data: nil, width: width, height: height,
            bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpace(name: CGColorSpace.sRGB)!,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        // Points, top-left origin, as the `Canvas` drew in.
        context.translateBy(x: 0, y: CGFloat(height))
        context.scaleBy(x: scale, y: -scale)
        context.setStrokeColor(CGColor(gray: 0, alpha: 0.18))
        context.setLineWidth(1)
        context.move(to: CGPoint(x: 0, y: 2))
        context.addLine(to: CGPoint(x: 1, y: 2))
        context.strokePath()
        return context.makeImage()!
    }
}

/// Soft CRT falloff. Static, board window only.
///
/// **Normal blending, deliberately.** This used the multiply blend mode, which
/// forces an offscreen pass over the whole pane on every redraw — and buys
/// nothing here: multiply with a source colour of pure black is
/// `(1 − αs)·Cd`, which is exactly normal compositing of black at `αs`. That
/// holds **only while the gradient is black**: tint it and the blend mode
/// matters again, so put it back if you do.
struct VignetteOverlay: View {
    var body: some View {
        EmptyView()
    }
}

/// Square outlined chrome for START / NEW CARD (alarm) and Save / Create
/// (phosphor). Not `.borderedProminent`.
struct AlarmOutline: ButtonStyle {
    var color: Color = Theme.accent
    var size: CGFloat = 12
    var filled: Bool = false

    /// Ink of a held button's words.
    static let heldInk: Double = 0.65
    /// Ink of a held button's edge — faded harder than the words, because a
    /// 1px rule at the ink's own opacity still reads as a live frame around
    /// dead words.
    static let heldEdge: Double = 0.45
    /// Horizontal padding that, with `vPad` and `stroke`, draws the box the
    /// hit shape is built from.
    static let hPad: CGFloat = 10
    static let vPad: CGFloat = 8
    static let stroke: CGFloat = 1

    /// Whether the outlined box is itself the button's target.
    ///
    /// `TapToToggle`'s rule: a disabled `AlarmOutline` sits directly over
    /// `BoardCardView`'s card-level tap target + double-click-to-open, and
    /// a shape left on it would eat that.
    static func boxTakesClick(isEnabled: Bool) -> Bool { isEnabled }

    func makeBody(configuration: Configuration) -> some View {
        Chrome(configuration: configuration, color: color, size: size, filled: filled)
    }

    /// A `ButtonStyle` is not a `View`, so SwiftUI never updates a property
    /// wrapper declared on the style itself — an environment read written
    /// there compiles and returns a constant `true` forever. The read has to
    /// happen inside a real `View`, which is what this is.
    private struct Chrome: View {
        let configuration: Configuration
        let color: Color
        let size: CGFloat
        let filled: Bool
        @Environment(\.isEnabled) private var isEnabled

        private var ink: Color { filled ? Theme.accentInk : color }

        var body: some View {
            let chrome = configuration.label
                .font(Theme.prose(size, weight: .semibold))
                // A held button reads as held: its words fade, and a filled
                // one's fill fades with its edge (review, 25 Sep 2026).
                .foregroundStyle(isEnabled ? ink : ink.opacity(AlarmOutline.heldInk))
                .padding(.horizontal, AlarmOutline.hPad)
                .padding(.vertical, AlarmOutline.vPad)
                .background(filled
                    ? (isEnabled ? color : color.opacity(AlarmOutline.heldEdge))
                    : color.opacity(0.12))
                .clipShape(RoundedRectangle(cornerRadius: Theme.controlRadius))
                .overlay(RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(isEnabled ? color : color.opacity(AlarmOutline.heldEdge), lineWidth: AlarmOutline.stroke))
                .opacity(configuration.isPressed ? 0.65 : 1)
            if AlarmOutline.boxTakesClick(isEnabled: isEnabled) {
                chrome.contentShape(Rectangle())
            } else {
                chrome
            }
        }
    }
}
