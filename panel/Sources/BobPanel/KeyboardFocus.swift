import SwiftUI

/// Which drawn controls hold the keyboard focus right now.
///
/// SwiftUI keeps its own focus inside the hosting view, so AppKit's
/// `firstResponder` cannot say whether a person has Tabbed onto a button.
/// A control that should take Return and Space when focused claims its
/// focus here, and the key monitor asks `holdsFocus` at the instant of the
/// press — read, never observed, so no view redraws on a focus move. Empty
/// whenever Full Keyboard Access is off: focus then never lands on a button,
/// and triage keeps every key.
///
/// **One writer.** Only `ClaimsKeyboardFocus` notes a claim, and it
/// withdraws it when the control leaves the screen, when its content is
/// covered (`keyboardClaimsSuppressed`) and when focus moves off; a triage
/// move clears every claim (`clear()`), because the selection and the focus
/// must never be two cursors.
@MainActor
final class FocusedControls {
    static let shared = FocusedControls()
    private var ids: Set<UUID> = []

    var holdsFocus: Bool { !ids.isEmpty }

    func note(_ id: UUID, focused: Bool) {
        if focused { ids.insert(id) } else { ids.remove(id) }
    }

    /// Every claim ends: an arrow moved the selection, or the selection
    /// changed under the focus. Return and Space are triage again until a
    /// control is focused anew.
    func clear() {
        ids.removeAll()
    }
}

private struct KeyboardClaimsSuppressedKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    /// True over content drawn but not in front of the person — the board
    /// kept mounted at opacity 0 under an agent's detail. Its controls leave
    /// the focus chain and claim nothing, so Return and Space can never press
    /// a verb nobody can see.
    var keyboardClaimsSuppressed: Bool {
        get { self[KeyboardClaimsSuppressedKey.self] }
        set { self[KeyboardClaimsSuppressedKey.self] = newValue }
    }
}

/// The one place a focus claim is written. `focused` is the control's own
/// focus (its `@FocusState`), true only while that control itself — not a
/// descendant — holds the keyboard. Over suppressed content the control is
/// taken out of the focus chain and claims nothing; leaving the screen
/// withdraws the claim. `onPress`, where given, is what Return (and any
/// other `keys`) does on a control that has no native press for them — and
/// it fires only while the control itself is focused, so a Return on a
/// focused child never bubbles up into its parent's action.
struct ClaimsKeyboardFocus: ViewModifier {
    let focused: Bool
    var keys: Set<KeyEquivalent> = [.return]
    var onPress: (() -> Void)? = nil
    @Environment(\.keyboardClaimsSuppressed) private var suppressed
    @State private var id = UUID()

    @ViewBuilder
    func body(content: Content) -> some View {
        let claimed = content
            .onChange(of: focused && !suppressed, initial: true) { _, now in
                FocusedControls.shared.note(id, focused: now)
            }
            .onDisappear { FocusedControls.shared.note(id, focused: false) }
            .onKeyPress(keys: keys) { _ in
                guard focused, !suppressed, let onPress else { return .ignored }
                onPress()
                return .handled
            }
        if suppressed {
            claimed.focusable(false)
        } else {
            claimed
        }
    }
}

/// A control's own focus, claimed through `ClaimsKeyboardFocus`.
struct ReportsKeyboardFocus: ViewModifier {
    var keys: Set<KeyEquivalent> = [.return]
    var onPress: (() -> Void)? = nil
    @FocusState private var focused: Bool

    func body(content: Content) -> some View {
        content
            .focused($focused)
            .modifier(ClaimsKeyboardFocus(focused: focused, keys: keys, onPress: onPress))
    }
}

extension View {
    /// Report this control's keyboard focus, so Return and Space press it
    /// instead of acting on the selected row. `onPress` gives Return (or
    /// `keys`) an action on a control that has none natively — an inbox
    /// entry, a card tile; nil leaves the keys to the control.
    func reportsKeyboardFocus(keys: Set<KeyEquivalent> = [.return],
                              onPress: (() -> Void)? = nil) -> some View {
        modifier(ReportsKeyboardFocus(keys: keys, onPress: onPress))
    }
}
