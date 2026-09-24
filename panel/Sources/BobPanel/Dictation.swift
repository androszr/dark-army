import AppKit
import SwiftUI

/// Virtual key codes for the modifier keys a push-to-talk shortcut may be.
///
/// Caps Lock (57) and Fn (63) stay out: Caps Lock latches, and neither is a
/// push-to-talk key. Right-side codes carry a sided label ("R⌘"); left-side
/// codes keep the unsided glyph so a combo still reads "⌘D".
enum DictationKey {
    static let modifierFlag: [UInt16: NSEvent.ModifierFlags] = [
        54: .command,  // right Command
        55: .command,
        56: .shift,
        60: .shift,    // right Shift
        58: .option,
        61: .option,   // right Option
        59: .control,
        62: .control,  // right Control
    ]

    static let sidedLabel: [UInt16: String] = [
        54: "R⌘",
        55: "⌘",
        56: "⇧",
        60: "R⇧",
        58: "⌥",
        61: "R⌥",
        59: "⌃",
        62: "R⌃",
    ]
}

/// Hold-to-talk control. Absent, never inert, when the gate is false.
///
/// Press-and-hold is an AppKit mouseDown/mouseUp on a probe view, not a
/// SwiftUI `DragGesture`. The card sheet is a `ScrollView`; a zero-distance
/// drag there is cancelled in one frame when the press also focuses the
/// field beside the button — MacWhisper then saw a 40 ms tap, cancelled,
/// and `noActiveTextFieldFound`. AppKit delivers mouseUp to the view that
/// got mouseDown even if SwiftUI relaid the button out from under the
/// pointer.
///
/// `focus()` runs on mouseDown; `dictate_down` waits one turn so the
/// TextEditor is first responder before MacWhisper looks for a field.
struct DictateButton: View {
    let focus: () -> Void
    @State private var holding = false
    @State private var sentDown = false

    static func available(_ settings: DaemonClient.Settings) -> Bool {
        settings.macwhisperInstalled && settings.accessibilityTrusted
            && !settings.dictationShortcutLabel.isEmpty
    }

    var body: some View {
        Text(holding ? "Listening…" : "Dictate")
            .font(Theme.mono(10, weight: .medium))
            .tracking(0.8)
            .foregroundStyle(Theme.phosphor)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .overlay(Rectangle().strokeBorder(Theme.phosphor, lineWidth: 1))
            .opacity(holding ? 0.65 : 1)
            .contentShape(Rectangle())
            // Cursor overlay first (its hitTest is nil). The probe sits on
            // top so AppKit actually delivers mouseDown — the other order
            // ate the press and the label never left "Dictate".
            .clickable()
            .overlay(HoldProbe(onDown: beginHold, onUp: endPress))
            .onDisappear { endPress() }
            .accessibilityLabel(holding ? "Listening" : "Dictate")
            .accessibilityAddTraits(.isButton)
    }

    private func beginHold() {
        guard !holding else { return }
        holding = true
        Trace.log("dictate button down")
        focus()
        NSApp.activate(ignoringOtherApps: true)
        // FocusState and AX first-responder land on the next turn. Posting
        // the key in the same runloop is why MacWhisper logged
        // noActiveTextFieldFound on every press.
        DispatchQueue.main.async {
            DictationFocus.promote(reason: "button")
            guard holding, !sentDown else {
                Trace.log("dictate button down dropped holding=\(holding) sent=\(sentDown)")
                return
            }
            sentDown = true
            Panel.send(action: "dictate_down")
        }
    }

    private func endPress() {
        holding = false
        guard sentDown else { return }
        sentDown = false
        Trace.log("dictate button up")
        Panel.send(action: "dictate_up")
    }
}

/// Make an editable NSTextView the window's first responder so MacWhisper's
/// AX lookup (`noActiveTextFieldFound`) can see a real text field and park
/// its waveform overlay on the caret — the same pill it draws in other apps.
///
/// SwiftUI `FocusState` on a `TextEditor` does not always promote the backing
/// NSTextView in time, and the hosting view often eats the AX focused-element
/// role, so MacWhisper looks at a group instead of a text area.
enum DictationFocus {
    private static var keyMonitor: Any?
    private static var focusMonitor: Any?
    private static var keyWindowObserver: Any?

    static func install() {
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { event in
            guard let flag = DictationKey.modifierFlag[event.keyCode] else { return event }
            let down = event.modifierFlags.contains(flag)
            let name = DictationKey.sidedLabel[event.keyCode] ?? "\(event.keyCode)"
            Trace.log("dictate physical \(name) \(down ? "down" : "up")")
            // **Only a modifier of the configured push-to-talk shortcut
            // promotes.** This fired
            // on *every* modifier down — both sides of ⌘, ⇧, ⌥ and ⌃ — with
            // no test against the recorded shortcut, so pressing Shift to
            // type a capital letter, or Control to send an interrupt, moved
            // the caret out of the hosted terminal and into the board's
            // search field behind it. The monitor keeps returning `event`
            // either way; with nothing recorded it promotes nothing at all.
            if down, isShortcutModifier(keyCode: event.keyCode,
                                        label: shortcutLabel) {
                promote(reason: "physical \(name)")
            }
            return event
        }
        // **The caret a person aimed by hand is the commoner case.** The
        // monitor above only fires for Dark Army's own push-to-talk modifier, and
        // `promote` only runs from the Dictate button and the composer's
        // `onAppear` — so a dictation app started from *its* own menu-bar
        // icon or its own hotkey, with the caret sitting in a field the
        // person clicked into, met the untouched hosting view and its
        // `noActiveTextFieldFound`. These two watch the ordinary gestures
        // that move the caret — a click, a keystroke — and publish whatever
        // field already has it. They **never** move focus: `syncAXFocus`
        // decorates the existing first responder and nothing else, so it is
        // safe to run after every press. Deferred one turn because AppKit
        // sets the first responder while handling the event, not before it.
        focusMonitor = NSEvent.addLocalMonitorForEvents(
            matching: [.leftMouseUp, .keyDown]
        ) { event in
            DispatchQueue.main.async { syncAXFocus(window: event.window) }
            return event
        }
        keyWindowObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeKeyNotification,
            object: nil,
            queue: .main
        ) { note in
            syncAXFocus(window: note.object as? NSWindow)
        }
        Trace.log("dictate key monitor installed")
    }

    /// The window's editable text responder, or nil.
    ///
    /// One test covers both kinds of field this panel draws: SwiftUI's
    /// `TextField` is edited through the window's shared **field editor**,
    /// which is an `NSTextView`, and `TextEditor` is an `NSTextView` of its
    /// own. An `NSTextField` is never itself the first responder while it is
    /// being typed into, so asking for one would answer no on every field.
    static func editingTextView(in window: NSWindow?) -> NSTextView? {
        guard let tv = window?.firstResponder as? NSTextView,
              tv.isEditable,
              tv.window != nil,
              !tv.isHiddenOrHasHiddenAncestor
        else { return nil }
        return tv
    }

    /// True while a caret is in an editable field of `window`.
    static func isEditing(_ window: NSWindow?) -> Bool {
        editingTextView(in: window) != nil
    }

    private static var decorated: (ObjectIdentifier, NSRect)?

    /// Publish the already-focused field to the Accessibility API **without
    /// moving the caret**, so an outside dictation app finds a text area
    /// where the person put one.
    ///
    /// Idempotent and memoised on the view's identity *and* its position on
    /// screen: the field editor is one object shared by every `TextField` in
    /// the window, so identity alone would go quiet the moment the caret
    /// moved from one field to the next, and re-posting on every keystroke
    /// would be a `focusedUIElementChanged` storm at VoiceOver.
    @discardableResult
    static func syncAXFocus(window: NSWindow?) -> Bool {
        guard let target = window ?? NSApp.keyWindow ?? NSApp.mainWindow,
              let tv = editingTextView(in: target)
        else {
            decorated = nil
            return false
        }
        let frame = tv.window?.convertToScreen(
            tv.convert(tv.bounds, to: nil)) ?? tv.bounds
        let mark = (ObjectIdentifier(tv), frame)
        if let decorated, decorated.0 == mark.0, decorated.1.equalTo(mark.1) {
            return true
        }
        decorated = mark
        decorate(tv)
        Trace.log("dictate ax sync: \(snapshot(window: tv.window ?? target, text: tv))")
        return true
    }

    /// Hosting views otherwise keep the AX focus, and a dictation app's
    /// overlay never finds a caret to park on.
    private static func decorate(_ tv: NSTextView) {
        tv.setAccessibilityElement(true)
        tv.setAccessibilityRole(tv.isFieldEditor ? .textField : .textArea)
        tv.setAccessibilityFocused(true)
        NSAccessibility.post(element: tv, notification: .focusedUIElementChanged)
    }

    private static var lastGate = ""

    /// The recorded push-to-talk shortcut's drawing label, restated on every
    /// context push. Empty means none was recorded — `DictateButton.available`
    /// is false and the `.flagsChanged` monitor promotes nothing.
    private(set) static var shortcutLabel = ""

    /// Whether a modifier key code is **part of** the configured push-to-talk
    /// shortcut.
    ///
    /// Two shapes, because the recorder writes two. A **modifier-only**
    /// shortcut is stored as its own sided label ("R⌘"), and sidedness was
    /// recorded deliberately, so that one is matched whole: left Command is
    /// not right Command. A shortcut **with a letter** in it is composed by
    /// `dictationShortcutLabel` as glyphs then the letter ("⌥⌘D") and carries
    /// no side, so each of its modifiers matches on the glyph alone —
    /// including the right-hand key, which the label cannot distinguish.
    ///
    /// The component rule is what makes those users work at all. The letter
    /// arrives as an ordinary `keyDown` and **no promote path watches for
    /// it** — this monitor is `.flagsChanged` only — so matching the whole
    /// label would mean a combo shortcut promoted nothing, ever, and its user
    /// would meet `noActiveTextFieldFound` with nothing on screen saying why.
    /// Promoting on the modifier that *starts* the combo is also the right
    /// moment: `promote` has to land before the dictation app looks for a
    /// field.
    ///
    /// It is deliberately not the whole repair for the focus theft, and never
    /// was — the terminal refusal below is, and it stands on its own. This
    /// narrowing is what stops an unrelated Shift or Control moving a caret
    /// in a window with no terminal in it at all.
    static func isShortcutModifier(keyCode: UInt16, label: String) -> Bool {
        guard !label.isEmpty else { return false }
        guard let sided = DictationKey.sidedLabel[keyCode] else { return false }
        // A modifier-only shortcut: the label *is* one of these, side included.
        if DictationKey.sidedLabel.values.contains(label) {
            return sided == label
        }
        // A combo: the unsided glyph has to be one of its components.
        let glyph = sided.hasPrefix("R") ? String(sided.dropFirst()) : sided
        return label.contains(glyph)
    }

    static func logGate(whisper: Bool, trusted: Bool, label: String) {
        shortcutLabel = label
        let line =
            "dictate gate whisper=\(whisper ? 1 : 0) ax=\(trusted ? 1 : 0) "
            + "shortcut=\(label.isEmpty ? "none" : label)"
        guard line != lastGate else { return }
        lastGate = line
        Trace.log(line)
    }

    /// **Whether the caret may be moved at all.**
    ///
    /// False while a hosted terminal holds it. `firstEditableTextView` skips
    /// only `isHiddenOrHasHiddenAncestor`, and the board underneath the agent
    /// detail is kept at **opacity 0** — which is not hidden — so a promotion
    /// while the terminal pane had the caret walked it straight into the
    /// board's search field, and every plain letter after that was read as a
    /// triage verb aimed at the agent list. The guard is about the terminal
    /// holding the caret and not about disabling promotion: the composer's
    /// `onAppear` and the Dictate button have no `TerminalView` under them
    /// and are unaffected.
    static func mayPromote(currentIsTerminal: Bool,
                           currentIsEditable: Bool) -> Bool {
        !currentIsTerminal
    }

    static func promote(reason: String) {
        let window = NSApp.keyWindow ?? NSApp.mainWindow
        guard let window else {
            Trace.log("dictate promote (\(reason)): no key window")
            return
        }
        guard mayPromote(currentIsTerminal: TerminalFocus.terminal(in: window) != nil,
                         currentIsEditable: isEditing(window)) else {
            Trace.log("dictate promote (\(reason)): refused, terminal holds the caret")
            return
        }
        guard let root = window.contentView else {
            Trace.log("dictate promote (\(reason)): no contentView")
            return
        }
        let current = window.firstResponder
        let tv = (current as? NSTextView) ?? firstEditableTextView(in: root)
        guard let tv else {
            Trace.log(
                "dictate promote (\(reason)): no NSTextView "
                + "first=\(describe(current))")
            return
        }
        if current !== tv {
            window.makeFirstResponder(tv)
        }
        // Force the AX post: the caret has just moved, so a memo left over
        // from the field it was in must not swallow the notification.
        decorated = nil
        syncAXFocus(window: window)
        Trace.log("dictate promote (\(reason)): \(snapshot(window: window, text: tv))")
    }

    static func snapshot(window: NSWindow, text: NSTextView) -> String {
        let first = describe(window.firstResponder)
        let role = text.accessibilityRole().map { "\($0.rawValue)" } ?? "?"
        let focused = text.isAccessibilityFocused()
        let range = text.selectedRange()
        var actual = NSRange()
        let rect = text.firstRect(forCharacterRange: range, actualRange: &actual)
        let caret = rect.isEmpty
            ? "empty"
            : "(\(Int(rect.origin.x)),\(Int(rect.origin.y)))"
        return "first=\(first) ax=\(role) axFocused=\(focused ? 1 : 0) "
            + "sel=\(range.location) caret=\(caret)"
    }

    private static func describe(_ responder: NSResponder?) -> String {
        guard let responder else { return "nil" }
        return String(describing: type(of: responder))
    }

    private static func firstEditableTextView(in view: NSView) -> NSTextView? {
        if let tv = view as? NSTextView, tv.isEditable, !tv.isHiddenOrHasHiddenAncestor {
            return tv
        }
        for sub in view.subviews {
            if let found = firstEditableTextView(in: sub) { return found }
        }
        return nil
    }
}

/// Transparent AppKit hit target on top of the drawn label. A parent
/// ScrollView cannot cancel this as a drag the way a SwiftUI DragGesture is.
private struct HoldProbe: NSViewRepresentable {
    var onDown: () -> Void
    var onUp: () -> Void

    func makeNSView(context: Context) -> HoldProbeView {
        let view = HoldProbeView()
        view.onDown = onDown
        view.onUp = onUp
        return view
    }

    func updateNSView(_ view: HoldProbeView, context: Context) {
        view.onDown = onDown
        view.onUp = onUp
    }
}

final class HoldProbeView: NSView {
    var onDown: (() -> Void)?
    var onUp: (() -> Void)?
    private var down = false
    private var upMonitor: Any?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    required init?(coder: NSCoder) { nil }

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .pointingHand)
    }

    override func mouseDown(with event: NSEvent) {
        down = true
        onDown?()
        // mouseUp is not delivered to this view if the pointer has moved
        // (the sheet is a ScrollView). A local monitor catches the release
        // anywhere in the app without a tracking-mode loop, which would
        // stall the runloop and the deferred dictate_down.
        if upMonitor == nil {
            upMonitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseUp) { [weak self] ev in
                self?.finish()
                return ev
            }
        }
    }

    override func mouseUp(with event: NSEvent) {
        finish()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        guard window == nil else { return }
        finish()
    }

    private func finish() {
        if let upMonitor {
            NSEvent.removeMonitor(upMonitor)
            self.upMonitor = nil
        }
        guard down else { return }
        down = false
        onUp?()
    }
}
