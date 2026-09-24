import AppKit

/// The panel's one size dial. Four named steps; every reader goes through
/// `resolved`, so a hand-edited `preferences.json` saying 300 draws at 100%
/// *and* ticks 100%.
///
/// Lives here, not in `Theme.swift`, which is byte-pinned against the phone.
enum PanelScale {
    static let steps: [(label: String, percent: Int)] = [
        ("100%", 100),
        ("125%", 125),
        ("150%", 150),
        ("175%", 175),
    ]
    static let defaultPercent = 100

    /// The member of `steps` equal to `percent`, else `defaultPercent`.
    static func resolved(_ percent: Int) -> Int {
        steps.contains { $0.percent == percent } ? percent : defaultPercent
    }

    static func factor(_ percent: Int) -> CGFloat {
        CGFloat(resolved(percent)) / 100
    }

    /// The canvas SwiftUI should lay out into, given the window's real size.
    /// `max(factor, 0.01)` so a zero cannot divide.
    static func logicalSize(_ physical: NSSize, factor: CGFloat) -> NSSize {
        let denom = max(factor, 0.01)
        return NSSize(width: physical.width / denom,
                      height: physical.height / denom)
    }

    static func minContentSize(factor: CGFloat) -> NSSize {
        NSSize(width: PanelPlacement.minWidth * factor,
               height: PanelPlacement.minHeight * factor)
    }

    /// AppKit's `contentMinSize`, capped so a later live resize cannot snap
    /// the window larger than the screen `clamped` already respects. A
    /// non-positive visible dimension is ignored rather than collapsing the
    /// floor to zero.
    static func cappedContentMinSize(_ wanted: NSSize, visible: NSSize) -> NSSize {
        NSSize(
            width: visible.width > 0 ? min(wanted.width, visible.width) : wanted.width,
            height: visible.height > 0 ? min(wanted.height, visible.height) : wanted.height)
    }

    /// Physical frame after a scale-step. Going *up* multiplies the current
    /// size by `newFactor / oldFactor` so the logical rail/board split is
    /// preserved until the screen runs out; going *down* leaves the size
    /// (the window is the user's). Then `PanelPlacement.clamped` at the new
    /// factor. Zero factors are guarded the same way as `logicalSize`.
    static func grownFrame(_ frame: NSRect,
                           from oldFactor: CGFloat,
                           to newFactor: CGFloat,
                           visible: NSRect) -> NSRect {
        var out = frame
        let old = max(oldFactor, 0.01)
        let new = max(newFactor, 0.01)
        if new > old {
            let ratio = new / old
            out.size.width *= ratio
            out.size.height *= ratio
        }
        return PanelPlacement.clamped(out, to: visible, scale: new)
    }
}

/// Hosts the SwiftUI tree in a coordinate space scaled by `factor`. The
/// window's frame stays in real points; `bounds` is `frame / factor`, so
/// every font size, `PanelMetrics` constant and literal padding grows
/// together. The only input is the frame AppKit already assigned.
final class ScaleHostView: NSView {
    private let hosting: NSView
    var factor: CGFloat = 1 {
        didSet {
            guard oldValue != factor else { return }
            needsLayout = true
        }
    }

    init(hosting: NSView) {
        self.hosting = hosting
        super.init(frame: .zero)
        wantsLayer = true
        hosting.wantsLayer = true
        addSubview(hosting)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        applyContentsScale()
        needsLayout = true
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        applyContentsScale()
        needsLayout = true
    }

    override func layout() {
        setBoundsSize(PanelScale.logicalSize(frame.size, factor: factor))
        hosting.frame = bounds
        applyContentsScale()
    }

    /// `contentsScale = backingScaleFactor * factor` so a 175% canvas on
    /// Retina is rendered at full resolution rather than magnified from a
    /// 1× raster. Written from `layout` and again when the backing scale
    /// changes (dragging onto a Retina display), because that change does
    /// not always re-run `layout`.
    private func applyContentsScale() {
        hosting.layer?.contentsScale = (window?.backingScaleFactor ?? 2) * factor
    }
}
