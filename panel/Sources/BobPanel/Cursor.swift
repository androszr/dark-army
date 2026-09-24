import SwiftUI
import AppKit

/// The pointer as an affordance.
///
/// Almost everything in this panel is drawn rather than assembled from AppKit
/// controls — a row is a `VStack` with an `onTapGesture`, a section heading is a
/// `.plain` Button, the jump rail is a rounded rectangle — and a drawn control
/// tells AppKit nothing about itself. So the arrow stayed an arrow over every
/// one of them, and the only way to learn what was clickable was to click it.
/// The hover washes and the tooltips were carrying that alone, and neither
/// answers the question before the mouse stops moving.
///
/// **A tracking area, not `onHover` + `NSCursor.push()`.** The push/pop pair is
/// the usual SwiftUI trick and it is wrong for this list specifically: the
/// daemon repaints these rows every few seconds, rows appear and vanish under
/// the pointer, and a view that is removed while hovered never delivers its
/// exit — so the pop never runs and the pointing hand is left stuck over the
/// whole screen. A `.cursorUpdate` tracking area owns no global state: AppKit
/// asks who is under the pointer and we answer, so a view that disappears
/// simply stops being asked.
///
/// The overlay never takes a click (`hitTest` returns nil): it is a sheet of
/// glass over the control, and the control underneath keeps every gesture it
/// had.
private struct CursorArea: NSViewRepresentable {
    let cursor: NSCursor
    let enabled: Bool
    @Environment(\.cursorAffordances) private var subtree

    func makeNSView(context: Context) -> CursorTrackingView {
        let view = CursorTrackingView()
        view.cursor = CursorAffordance.live(requested: enabled, subtree: subtree) ? cursor : nil
        return view
    }

    func updateNSView(_ view: CursorTrackingView, context: Context) {
        view.cursor = CursorAffordance.live(requested: enabled, subtree: subtree) ? cursor : nil
    }
}

enum CursorTracking {
    enum Work: Equatable { case rebuild, recheckPointer, nothing }
    /// CursorTracking.onRelayout: an `.inVisibleRect` tracking area follows
    /// the view's visible rect on its own, so a relayout does not invalidate
    /// it — the stock remove-and-add was rebuilding ninety-odd areas on every
    /// SSE snapshot for nothing. What a relayout *does* owe is the
    /// stationary-pointer answer, because AppKit only asks when the pointer
    /// crosses a boundary.
    static func onRelayout(hasArea: Bool, wantsCursor: Bool) -> Work {
        guard wantsCursor else { return .nothing }
        return hasArea ? .recheckPointer : .rebuild
    }
}

final class CursorTrackingView: NSView {
    var cursor: NSCursor? {
        didSet {
            guard cursor !== oldValue else { return }
            rebuildTrackingArea()
        }
    }

    private var area: NSTrackingArea?

    /// Transparent to the mouse. Without this the overlay would swallow the tap
    /// of the very control it is describing.
    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        switch CursorTracking.onRelayout(hasArea: area != nil, wantsCursor: cursor != nil) {
        case .rebuild:
            rebuildTrackingArea()
        case .recheckPointer:
            setCursorIfPointerInside()
        case .nothing:
            return
        }
    }

    private func rebuildTrackingArea() {
        if let area {
            removeTrackingArea(area)
            self.area = nil
        }
        guard cursor != nil else { return }
        // `.inVisibleRect` keeps the area honest inside a scroll view — the row
        // scrolled half out of the list must not claim the pointer above it.
        let fresh = NSTrackingArea(
            rect: .zero,
            options: [.cursorUpdate, .mouseEnteredAndExited,
                      .activeInActiveApp, .inVisibleRect],
            owner: self,
            userInfo: nil)
        addTrackingArea(fresh)
        area = fresh
        setCursorIfPointerInside()
    }

    /// A tracking area added under a stationary pointer generates no
    /// `mouseEntered` and no `cursorUpdate` — AppKit only asks when the pointer
    /// *crosses* a boundary. That is exactly what an arm-then-confirm button
    /// does: pressing **Delete** relabels it *Really delete?*, the control
    /// resizes, SwiftUI relays it out, `updateTrackingAreas` rebuilds this area
    /// and nothing ever tells us the hand is still over it — so the pointing
    /// hand fell back to an arrow on the one press that most needs to look
    /// pressable. Asking the window where the pointer is costs nothing and is
    /// ordered by position rather than by an event that will not arrive.
    private func setCursorIfPointerInside() {
        guard let cursor, let window, window.isKeyWindow || NSApp.isActive else { return }
        let local = convert(window.mouseLocationOutsideOfEventStream, from: nil)
        guard visibleRect.contains(local) else { return }
        cursor.set()
    }

    override func cursorUpdate(with event: NSEvent) {
        if let cursor { cursor.set() } else { super.cursorUpdate(with: event) }
    }

    /// Belt to `cursorUpdate`'s braces: these rows are relaid out under a
    /// stationary pointer several times a minute, and a cursor update is only
    /// generated when the pointer *crosses* a boundary.
    ///
    /// Entering sets our cursor; exiting deliberately sets nothing. Handing the
    /// arrow back on exit reads as the obvious other half and is the bug: moving
    /// from one row to the next fires the neighbour's `mouseEntered` and this
    /// view's `mouseExited` in an order AppKit does not promise, so the exit can
    /// land last and wipe the cursor the row you are now on just set. Leaving is
    /// `cursorUpdate`'s job, which is ordered by the pointer's position rather
    /// than by two views' events.
    override func mouseEntered(with event: NSEvent) {
        cursor?.set()
    }
}

private struct CursorAffordancesKey: EnvironmentKey { static let defaultValue = true }
extension EnvironmentValues {
    var cursorAffordances: Bool {
        get { self[CursorAffordancesKey.self] }
        set { self[CursorAffordancesKey.self] = newValue }
    }
}
enum CursorAffordance {
    static func live(requested: Bool, subtree: Bool) -> Bool { requested && subtree }
}

extension View {
    /// Switch every `.clickable()` below this point off in one place — for a
    /// pane that is mounted but covered.
    func cursorAffordances(_ live: Bool) -> some View {
        environment(\.cursorAffordances, live)
    }

    /// The pointing hand over something you can press.
    ///
    /// Reserved for *drawn* controls — rows, headings, chevrons, the jump
    /// rail, the link-weight buttons, and the board's equivalent (Details,
    /// Delete, the assistant chip). Real AppKit controls (the segmented tabs,
    /// the bordered Allow / Deny buttons, the text fields, a picker) are left
    /// alone deliberately: they already look like controls, and macOS keeps
    /// the arrow over a button.
    func clickable(_ enabled: Bool = true) -> some View {
        overlay(CursorArea(cursor: .pointingHand, enabled: enabled))
    }

    /// The up-down resize arrows over a drawn divider you can drag
    /// vertically. Same tracking-area overlay as `clickable()`, a
    /// different cursor: a pointing hand over a divider promises a press,
    /// and what it takes is a drag.
    func resizableVertically(_ enabled: Bool = true) -> some View {
        overlay(CursorArea(cursor: .resizeUpDown, enabled: enabled))
    }
}

/// Make a block of drawn text press like the chevron beside it.
///
/// A modifier rather than three copies of the same four lines, and a modifier
/// rather than a `.onTapGesture` written inline, because the `active` gate has
/// to switch the *hit shape* off as well: a `contentShape(Rectangle())` left
/// behind on an inert block would still swallow a click that belongs to
/// whatever is under it.
struct TapToToggle: ViewModifier {
    let active: Bool
    let action: () -> Void

    func body(content: Content) -> some View {
        if active {
            content
                .contentShape(Rectangle())
                .onTapGesture(perform: action)
                .clickable()
        } else {
            content
        }
    }
}

/// Native interaction adapter for the byte-identical area picker.
struct AreaChoiceButton<Label: View>: View {
    let action: () -> Void
    let label: Label
    init(action: @escaping () -> Void, @ViewBuilder label: () -> Label) {
        self.action = action
        self.label = label()
    }
    var body: some View {
        Button(action: action) { label }
            .buttonStyle(.plain)
            .clickable()
    }
}
