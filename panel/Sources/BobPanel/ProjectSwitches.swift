import AppKit
import SwiftUI

/// One on/off chip in the board's project-switch row. Shape borrowed from
/// the rail's tab chips; ink and type from `Theme`, because this is board
/// chrome, not the fleet.
struct ProjectSwitchChip: View {
    let title: String
    let on: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(Theme.mono(11, weight: on ? .semibold : .regular))
                .lineLimit(1)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: Theme.corner)
                        .fill(on ? Theme.card : Color.clear)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.corner)
                        .stroke(on ? Theme.hair : Theme.rule)
                )
                .foregroundStyle(on ? Theme.phosphor : Theme.faint)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable()
    }
}

/// Projects as a row of on/off chips under the identity bar. ALL is pinned
/// outside the scrolling region so the way back from a selection that hides
/// everything is never off-screen.
struct ProjectSwitchStrip: View {
    @ObservedObject var board: BoardState
    let names: [String]
    @State private var gate = StripPanGate()

    var body: some View {
        HStack(spacing: 8) {
            ProjectSwitchChip(
                title: "ALL",
                on: projectFilterShowsAll(selected: board.projectFilter,
                                          allNames: names)
            ) {
                board.clearProjectFilter()
            }
            Rectangle().fill(Theme.rule).frame(width: 1, height: 16)
            PanScroller(gate: gate) { chipRow }
                .frame(maxWidth: .infinity)
        }
        .padding(.horizontal, 14)
        .frame(height: PanelMetrics.projectSwitches)
    }

    private var chipRow: some View {
        HStack(spacing: 6) {
            ForEach(projectSwitchRows(selected: board.projectFilter,
                                      allNames: names),
                    id: \.name) { item in
                ProjectSwitchChip(title: item.title, on: item.ticked) {
                    guard !gate.suppressesToggle else { return }
                    guard let name = item.name else { return }
                    board.toggleProjectFilter(name)
                }
            }
        }
        .fixedSize(horizontal: true, vertical: true)
    }
}

/// Press-vs-pan threshold for the switch row. A reference box so nothing
/// here invalidates a `@State` and re-renders the strip mid-drag. The 4pt
/// threshold is enforced in exactly one place — `note(dx:)` — and that same
/// call both starts the pan and latches the suppression, so "did we pan"
/// cannot have two answers. Only `dx` is measured; a vertical drag is not
/// a pan and must not suppress a press.
final class StripPanGate {
    static let threshold: CGFloat = 4
    private(set) var panning = false
    private(set) var suppressesToggle = false

    /// True once `|dx| >= threshold`, then latched: a later 1pt note keeps
    /// panning rather than un-beginning when the pointer drifts back.
    func note(dx: CGFloat) -> Bool {
        if panning { return true }
        guard abs(dx) >= Self.threshold else { return false }
        panning = true
        suppressesToggle = true
        return true
    }

    /// Stops panning, keeps suppression. The mouse-up that fires a SwiftUI
    /// `Button` is dispatched in the same runloop turn as the recognizer's
    /// `.ended`, so clearing here would let a 40pt drag ending over a chip
    /// toggle it.
    func end() {
        panning = false
    }

    func release() {
        suppressesToggle = false
    }

    func reset() {
        panning = false
        suppressesToggle = false
    }
}

/// Horizontal `NSScrollView` wrapper that pans by dragging. Wheel and
/// trackpad scrolling come with the scroll view; the pan recognizer is
/// what makes a grab follow the pointer. Floor is macOS 14, so the
/// SwiftUI point-accurate scroll API is not available.
struct PanScroller<Content: View>: NSViewRepresentable {
    let gate: StripPanGate
    let content: Content

    init(gate: StripPanGate, @ViewBuilder content: () -> Content) {
        self.gate = gate
        self.content = content()
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(gate: gate)
    }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasHorizontalScroller = false
        scroll.hasVerticalScroller = false
        scroll.horizontalScrollElasticity = .allowed
        scroll.verticalScrollElasticity = .none
        let hosting = NSHostingView(rootView: content)
        hosting.sizingOptions = .intrinsicContentSize
        scroll.documentView = hosting
        sizeDocument(hosting, in: scroll)
        let pan = NSPanGestureRecognizer(
            target: context.coordinator,
            action: #selector(Coordinator.panned(_:)))
        pan.delaysPrimaryMouseButtonEvents = false
        scroll.addGestureRecognizer(pan)
        context.coordinator.scrollView = scroll
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.gate = gate
        context.coordinator.scrollView = scroll
        guard let hosting = scroll.documentView as? NSHostingView<Content> else {
            return
        }
        hosting.sizingOptions = .intrinsicContentSize
        hosting.rootView = content
        hosting.invalidateIntrinsicContentSize()
        sizeDocument(hosting, in: scroll)
    }

    /// Width is the chips' own size, never the clip's. A hosting view
    /// without `.intrinsicContentSize` reports none, Auto Layout sizes the
    /// document to the clip, `maxX` is 0, and the row neither pans nor
    /// overflows. Height still follows the clip, as the strip's constant.
    private func sizeDocument(_ hosting: NSHostingView<Content>,
                              in scroll: NSScrollView) {
        let intrinsic = hosting.intrinsicContentSize
        let clipHeight = scroll.contentView.bounds.height
        hosting.frame.size = NSSize(
            width: max(intrinsic.width, 0),
            height: clipHeight > 0 ? clipHeight : max(intrinsic.height, 0))
    }

    final class Coordinator: NSObject {
        var gate: StripPanGate
        weak var scrollView: NSScrollView?
        private var origin: NSPoint = .zero

        init(gate: StripPanGate) {
            self.gate = gate
        }

        @objc func panned(_ recognizer: NSPanGestureRecognizer) {
            guard let scrollView else { return }
            switch recognizer.state {
            case .began:
                origin = scrollView.contentView.bounds.origin
            case .changed:
                let dx = recognizer.translation(in: scrollView).x
                guard gate.note(dx: dx) else { return }
                let documentWidth = scrollView.documentView?.frame.width ?? 0
                let clipWidth = scrollView.contentView.bounds.width
                let maxX = max(0, documentWidth - clipWidth)
                let x = min(max(origin.x - dx, 0), maxX)
                scrollView.contentView.scroll(to: NSPoint(x: x, y: 0))
                scrollView.reflectScrolledClipView(scrollView.contentView)
            case .ended, .cancelled, .failed:
                gate.end()
                DispatchQueue.main.async { [gate] in
                    gate.release()
                }
            default:
                break
            }
        }
    }
}
