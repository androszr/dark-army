import SwiftUI
import UIKit

/// Signal's semantic palette, generated from the same source as the Mac.
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

    static var uiBg: UIColor {
        // Derived, never typed: the scanner ground follows `tokens.json`.
        UIColor(SignalTokens.canvas)
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

    /// Prose scales without a Dynamic Type ceiling, like machine text.
    static func prose(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        let scaled = UIFontMetrics(forTextStyle: textStyle(for: size))
            .scaledValue(for: size)
        return .system(size: scaled, weight: weight, design: .default)
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
        EmptyView()
    }
}

/// Square outlined chrome, at touch measure. The panel's `AlarmOutline` with
/// bigger padding: `hPad`/`vPad` are chrome sizing and may differ from the
/// Mac's — only the colour tokens above are pinned by the drift test.
struct AlarmOutline: ButtonStyle {
    var color: Color = Theme.accent
    var size: CGFloat = 14
    var filled: Bool = false

    static let heldInk: Double = 0.65
    static let heldEdge: Double = 0.45
    static let hPad: CGFloat = 14
    static let vPad: CGFloat = 10
    static let stroke: CGFloat = 1

    func makeBody(configuration: Configuration) -> some View {
        Chrome(configuration: configuration, color: color, size: size, filled: filled)
    }

    /// A `ButtonStyle` is not a `View`, so an `@Environment` read declared on
    /// the style itself never updates — it has to happen inside a real `View`.
    private struct Chrome: View {
        let configuration: Configuration
        let color: Color
        let size: CGFloat
        let filled: Bool
        @Environment(\.isEnabled) private var isEnabled
        @Environment(\.dynamicTypeSize) private var dynamicTypeSize

        private var ink: Color { filled ? Theme.accentInk : color }

        var body: some View {
            configuration.label
                .font(Theme.prose(size, weight: .semibold))
                // Letter-spacing at an accessibility size is width spent on
                // air: the label is already as wide as the screen allows.
                .tracking(0)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                // A held button reads as held: its words fade, and a filled
                // one's fill fades with its edge (review, 25 Sep 2026).
                .foregroundStyle(isEnabled ? ink : ink.opacity(AlarmOutline.heldInk))
                .padding(.horizontal, AlarmOutline.hPad)
                .padding(.vertical, AlarmOutline.vPad)
                .background(filled
                    ? (isEnabled ? color : color.opacity(AlarmOutline.heldEdge))
                    : color.opacity(0.12))
                .clipShape(RoundedRectangle(cornerRadius: Theme.controlRadius))
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(
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
/// `Theme.faint`, the focused edge `Theme.phosphor` — all existing tokens, so
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
            .hidesKeyboard(when: focused)
            .padding(.horizontal, FieldWell.hPad)
            .padding(.vertical, multiline ? FieldWell.vPadTall : FieldWell.vPad)
            .background(Theme.well)
            .overlay(
                RoundedRectangle(cornerRadius: Theme.controlRadius).strokeBorder(
                    focused ? Theme.phosphor : Theme.faint,
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

/// The way down for every keyboard on the phone. A field that has the
/// keyboard shows this at its top trailing corner, inside its own frame, so
/// the button under a field — the one thing a raised keyboard can hide — is
/// never the only exit. The number pad has no return key and a prose field's
/// return key types a new line, so neither can put itself away.
///
/// Not a `.toolbar(placement: .keyboard)` item: that bar is unreliable in
/// detent sheets and repeats once per field that declares it. It resigns
/// whatever holds the keyboard rather than clearing one focus binding, so
/// the fields with no binding of their own work the same way.
struct KeyboardHideButton: View {
    static let glyph: CGFloat = 13
    /// Drawn small beside the text, touched at the platform's 44 points.
    static let reach: CGFloat = 12

    var body: some View {
        DecryptButton(action: KeyboardHideButton.hide) {
            Image(systemName: "keyboard.chevron.compact.down")
                .font(Theme.mono(KeyboardHideButton.glyph))
                .foregroundStyle(Theme.phosphor)
                .frame(width: 28, height: 20)
                .contentShape(Rectangle().inset(by: -KeyboardHideButton.reach))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Hide keyboard")
    }

    static func hide() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder),
                                        to: nil, from: nil, for: nil)
    }
}

/// A field with its own focus binding: the button shows while it is focused.
struct HidesKeyboard: ViewModifier {
    var focused: Bool

    func body(content: Content) -> some View {
        HStack(alignment: .top, spacing: 6) {
            content
            if focused { KeyboardHideButton() }
        }
    }
}

/// A field with no focus binding of its own (the card screen's boxes): the
/// modifier keeps one, private to it, only to know when to show the button.
struct HidesKeyboardOwnFocus: ViewModifier {
    @FocusState private var focused: Bool

    func body(content: Content) -> some View {
        HStack(alignment: .top, spacing: 6) {
            content.focused($focused)
            if focused { KeyboardHideButton() }
        }
    }
}

extension View {
    func hidesKeyboard(when focused: Bool) -> some View {
        modifier(HidesKeyboard(focused: focused))
    }

    func hidesKeyboard() -> some View {
        modifier(HidesKeyboardOwnFocus())
    }
}
