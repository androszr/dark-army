import SwiftUI
import UIKit

/// The phone's copy of the Mac panel's fsociety CRT palette.
///
/// A *copy*, deliberately: the phone is an Xcode app target and the panel is a
/// SwiftPM module, there is no shared package, and coupling their builds was
/// refused in the interview. What keeps the two honest is
/// `host/tests/test_phone_theme_drift.py`, which reads both files and fails the
/// moment one token moves without the other — so the literals below must stay
/// **byte-identical** to `panel/Sources/BobPanel/Theme.swift`. Chrome sizing
/// (padding, face sizes, touch targets) may differ; the tokens may not.
enum Theme {
    static let bg = Color(red: 5 / 255, green: 8 / 255, blue: 5 / 255)
    static let bar = Color(red: 3 / 255, green: 6 / 255, blue: 3 / 255)
    static let well = Color(red: 2 / 255, green: 4 / 255, blue: 2 / 255)
    static let phosphor = Color(red: 124 / 255, green: 255 / 255, blue: 124 / 255)
    static let phosphorBright = Color(red: 200 / 255, green: 255 / 255, blue: 200 / 255)
    static let dim = Color(red: 92 / 255, green: 184 / 255, blue: 92 / 255)
    static let faint = Color(red: 61 / 255, green: 122 / 255, blue: 61 / 255)
    static let hair = Color(red: 31 / 255, green: 90 / 255, blue: 31 / 255)
    static let rule = Color(red: 20 / 255, green: 51 / 255, blue: 20 / 255)
    static let alarm = Color(red: 255 / 255, green: 77 / 255, blue: 77 / 255)
    /// Your turn — a held state a person should know about. Not `alarm` (that
    /// is an alarm) and not orange (that means something was refused).
    static let amber = Color(red: 255 / 255, green: 191 / 255, blue: 71 / 255)
    static let card = Color(red: 8 / 255, green: 24 / 255, blue: 8 / 255).opacity(0.85)

    static let corner: CGFloat = 0

    /// Matching `#050805` for UIKit-hosted chrome — the scanner's view
    /// background, where a SwiftUI `Color` cannot reach.
    static var uiBg: UIColor {
        UIColor(red: 5 / 255, green: 8 / 255, blue: 5 / 255, alpha: 1)
    }

    /// The one font seam, and the one place the phone honours Dynamic Type.
    ///
    /// The point size a call site asks for is what it gets at the system's
    /// default text size; every other setting scales it through the metrics of
    /// the text style whose own default is nearest. **No ceiling** — an
    /// accessibility size is asked for in full, and the layouts below reflow
    /// rather than clip. A `maximumPointSize:` here, or a clamp on any subtree
    /// with SwiftUI's own dynamic-type-size modifier, would be that ceiling by
    /// another name — `test_phone_accessibility.py` greps for both.
    ///
    /// The panel's copy of this file deliberately keeps a fixed size: the Mac
    /// has no Dynamic Type, and `test_phone_theme_drift.py` compares the colour
    /// tokens and `corner`, never this body.
    static func mono(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        let scaled = UIFontMetrics(forTextStyle: textStyle(for: size))
            .scaledValue(for: size)
        return .system(size: scaled, weight: weight, design: .monospaced)
    }

    /// Nearest by the style's own default point size at `.large`:
    /// caption2 11, caption1 12, footnote 13, subheadline 15, body 17.
    private static func textStyle(for size: CGFloat) -> UIFont.TextStyle {
        switch size {
        case ..<11.5: return .caption2
        case ..<12.5: return .caption1
        case ..<14.0: return .footnote
        case ..<16.0: return .subheadline
        default: return .body
        }
    }
}

/// A row of cells while they fit, a stack of them once they do not.
///
/// The fork is passed in rather than read here, so the decision stays with the
/// view that owns the layout and there is one rule — `isAccessibilitySize` —
/// rather than a second threshold invented per screen. Never a
/// `GeometryReader` and never a screen measurement: the text size the person
/// chose is the input, not the width it happens to produce.
struct AdaptiveStack<Content: View>: View {
    let stacked: Bool
    var spacing: CGFloat = 8
    var alignment: HorizontalAlignment = .leading
    /// The row's cross-axis alignment while not stacked; `.top` keeps
    /// columns of unequal height flush with each other's first line.
    var rowAlignment: VerticalAlignment = .center
    @ViewBuilder var content: Content

    var body: some View {
        if stacked {
            VStack(alignment: alignment, spacing: spacing) { content }
                .frame(maxWidth: .infinity, alignment: .leading)
        } else {
            HStack(alignment: rowAlignment, spacing: spacing) { content }
        }
    }
}

/// A face on a dark square. The panel's `PixelMark`, over `UIImage`.
///
/// **Static**, and there was never a clock here: one still portrait out of
/// `Cast.portrait`, smoothly scaled and clipped to the tile, with the state
/// rule along the bottom edge — solid while working, dashed while asleep, red
/// while it needs someone. A character with no portrait yet shows its
/// initial in `Theme.faint`, never a hashed stranger.
struct PixelMark: View {
    var character: String
    var state: Cast.State
    var size: CGFloat = 20

    /// How far below full brightness a portrait sits: the signal text has to
    /// stay the brightest thing on the row. The panel's `AvatarView` figure.
    static let portraitDim: Double = 0.88

    /// Sheet lead only. Rows, crew and area tiles keep the default `size`.
    static let sheetSize: CGFloat = 160

    var body: some View {
        ZStack(alignment: .bottom) {
            Rectangle()
                .fill(Theme.bg)
            if let image = Cast.portrait(character) {
                Image(uiImage: image)
                    .interpolation(.high)
                    .resizable()
                    .scaledToFill()
                    .frame(width: size - 2, height: size - 2)
                    .clipShape(Rectangle())
                    .opacity(Self.portraitDim)
                    .padding(.bottom, 1)
            } else {
                // Not chosen yet: the character's own initial.
                Text(Cast.initial(of: character))
                    .font(Theme.mono(size * 0.5, weight: .medium))
                    .foregroundStyle(Theme.faint)
                    .frame(width: size, height: size)
            }
            Rectangle()
                .strokeBorder(Theme.hair, lineWidth: 1)
            StateRule(state: state, width: size * 0.6)
                .padding(.bottom, 2)
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}

/// Dark Army's mark: the mask, with a clear ground.
///
/// Not a cast face and never a pose. The prompt subtitle and the 2pt rule
/// under the bar already carry who is waiting. One PNG, byte-identical on
/// the phone and the panel. The home-screen tile is this mask painted in
/// Dark Army green by `tools/app_icon_bake.py`, not this file itself.
///
/// Full colour, so it is never tinted. The ground is clear: only the mask
/// shows on the bar. Continuous tone, so it scales smoothly.
struct BrandMark: View {
    var size: CGFloat = 20

    var body: some View {
        if let image = Self.image {
            Image(uiImage: image)
                .interpolation(.high)
                .resizable()
                .scaledToFit()
                .frame(width: size, height: size)
                .accessibilityHidden(true)
        }
    }

    /// Loaded once, through `Bundle.main` — `Bundle.module` compiles in an
    /// Xcode app target and then finds nothing at runtime, the same trap
    /// `Cast.castDirectory` exists to avoid.
    private static let image: UIImage? = {
        guard let url = Bundle.main.url(forResource: "brand", withExtension: nil)?
                .appendingPathComponent("fsociety-mark.png")
        else { return nil }
        return UIImage(contentsOfFile: url.path)
    }()
}

/// `root@darkarmy:<path>$ [command]`. No block cursor — a blink is a clock.
struct PromptLine: View {
    var path: String
    var command: String = ""
    var commandColor: Color? = nil
    var size: CGFloat = 12

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Group {
            if dynamicTypeSize.isAccessibilitySize {
                // One wrapping run rather than four cells fighting over a
                // phone's width: at these sizes the path alone can be wider
                // than the screen, and a one-line cap would end it in dots.
                joined
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
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
                // One-line chrome over a shipped vocabulary — every call
                // site passes a literal — and `spoken` carries it whole.
                .lineLimit(1)
            }
        }
        .font(Theme.mono(size))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spoken)
    }

    /// The same four runs, concatenated so they wrap as one paragraph. Each
    /// keeps its own ink; `Text + Text` is the only join that does.
    private var joined: Text {
        var line = Text("root@darkarmy:").foregroundStyle(Theme.phosphor)
            + Text(path).foregroundStyle(Theme.phosphorBright)
            + Text("$").foregroundStyle(Theme.phosphor)
        if !command.isEmpty {
            line = line
                + Text(" \(command)").foregroundStyle(commandColor ?? Theme.phosphor)
        }
        return line
    }

    private var spoken: String {
        command.isEmpty ? "root@darkarmy:\(path)$" : "root@darkarmy:\(path)$ \(command)"
    }
}

/// Dust on a tube. Static 1pt black lines every 3pt, never a clock.
/// Hit-testing is off: a scanline that eats a tap is a row that cannot open.
struct ScanlineOverlay: View {
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
        // Wallpaper. A screen reader reads the news, not the tube.
        .accessibilityHidden(true)
        .allowsHitTesting(false)
    }
}

/// Square outlined chrome, at touch measure. The panel's `AlarmOutline` with
/// bigger padding: `hPad`/`vPad` are chrome sizing and may differ from the
/// Mac's — only the colour tokens above are pinned by the drift test.
struct AlarmOutline: ButtonStyle {
    var color: Color = Theme.phosphor
    var size: CGFloat = 13

    static let heldInk: Double = 0.4
    static let heldEdge: Double = 0.3
    static let hPad: CGFloat = 14
    static let vPad: CGFloat = 8
    static let stroke: CGFloat = 1

    func makeBody(configuration: Configuration) -> some View {
        Chrome(configuration: configuration, color: color, size: size)
    }

    /// A `ButtonStyle` is not a `View`, so an `@Environment` read declared on
    /// the style itself never updates — it has to happen inside a real `View`.
    private struct Chrome: View {
        let configuration: Configuration
        let color: Color
        let size: CGFloat
        @Environment(\.isEnabled) private var isEnabled
        @Environment(\.dynamicTypeSize) private var dynamicTypeSize

        var body: some View {
            configuration.label
                .font(Theme.mono(size, weight: .medium))
                // Letter-spacing at an accessibility size is width spent on
                // air: the label is already as wide as the screen allows.
                .tracking(dynamicTypeSize.isAccessibilitySize ? 0 : size * 0.08)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                .foregroundStyle(isEnabled ? color : color.opacity(AlarmOutline.heldInk))
                .padding(.horizontal, AlarmOutline.hPad)
                .padding(.vertical, AlarmOutline.vPad)
                .overlay(
                    Rectangle().strokeBorder(
                        isEnabled ? color : color.opacity(AlarmOutline.heldEdge),
                        lineWidth: AlarmOutline.stroke)
                )
                .opacity(configuration.isPressed ? 0.65 : 1)
                .contentShape(Rectangle())
        }
    }
}

/// The one well every typed field on the phone sits in.
///
/// Three surfaces ask a person to type — the composer, the reply box under a
/// waiting agent, and pairing — and before this they were three different
/// looks: two bare `TextField`s with no chrome at all beside pickers that
/// carry a `Theme.hair` frame, and one private hairline modifier in
/// `Pairing.swift`. One modifier, used by all three, is what stops them
/// drifting again.
///
/// **No colour of its own.** The fill is `Theme.well`, the resting edge
/// `Theme.hair`, the focused edge `Theme.phosphor` — all existing tokens, so
/// the palette stays exactly what `test_phone_theme_drift.py` compares. The
/// affordance is the *stroke*; the fill is depth, and a lighter fill would be
/// a palette change owed to both surfaces or neither.
///
/// `hPad` / `vPad` / `vPadTall` are chrome sizing like `AlarmOutline`'s and
/// may differ from the Mac's.
struct FieldWell: ViewModifier {
    var focused: Bool
    var multiline: Bool = false
    /// Called for a tap that lands on the padding ring rather than on the
    /// text itself. The gesture sits on the *padded* view, so the field keeps
    /// its own cursor placement — never wrap this in a `Button`, which would
    /// swallow text selection outright.
    var onTap: (() -> Void)? = nil

    static let hPad: CGFloat = 10
    static let vPad: CGFloat = 8
    static let vPadTall: CGFloat = 10
    static let stroke: CGFloat = 1

    func body(content: Content) -> some View {
        content
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, FieldWell.hPad)
            .padding(.vertical, multiline ? FieldWell.vPadTall : FieldWell.vPad)
            .background(Theme.well)
            .overlay(
                Rectangle().strokeBorder(
                    focused ? Theme.phosphor : Theme.hair,
                    lineWidth: FieldWell.stroke)
            )
            .contentShape(Rectangle())
            .onTapGesture { onTap?() }
    }
}

extension View {
    func fieldWell(focused: Bool, multiline: Bool = false,
                   onTap: (() -> Void)? = nil) -> some View {
        modifier(FieldWell(focused: focused, multiline: multiline, onTap: onTap))
    }
}
