import AppKit
import SwiftUI

// The panel's one keyboard reader. Split out of `main.swift` on 20 Sep 2026;
// the ordering it enforces — a focused hosted terminal first, then the
// switcher, then the triage rungs — is `docs/panel-window-contract.md`'s.
extension AppDelegate {
    func installKeyMonitor() {
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self else { return event }
            // **A sheet owns every key it is sent.** The card sheet is a form of
            // text fields, and none of them publish to `KeyRouter` — only the
            // panel's filter, the reply strip and the board search do — so
            // `keys.editing` is false while somebody is typing a title, and the
            // letter verbs below ate the press: Space, S, D, R, W and `/` never
            // reached the field, which is a form that silently refuses half the
            // alphabet. Two tests, because one of them is narrow and the other
            // is total. `event.window?.isSheet` is the direct one — true of
            // exactly the presses aimed at a sheet, however the sheet is
            // presented and whichever window it is attached to — but `window`
            // is optional and an event that arrives without one would fall
            // straight through it into the verbs. `panel.attachedSheet` closes
            // that: while a sheet is up nothing typed anywhere is triage, and
            // the tracked content view is `panel.contentView`. Escape is
            // covered by this
            // guard too, and is handed back for AppKit to dismiss the sheet
            // with rather than consumed.
            if event.window?.isSheet == true || self.panel?.attachedSheet != nil {
                return event
            }
            // **And the card window owns every key it is sent**, for exactly
            // the reason above: it is that same form of text fields, moved out
            // of the sheet into its own window (`CardWindow.swift`), so both
            // tests above go false while somebody types a title and the letter
            // verbs would eat the alphabet again. Same two-test shape — `owns`
            // is the narrow one (identity against the controller's window) and
            // `isKey` the total one, covering an event that arrives with no
            // window while the card window is key, exactly `attachedSheet`'s
            // role above. **Below** the sheet guard on purpose: a file chooser
            // or dialog sheet attached to the card window has to satisfy
            // `isSheet` first.
            if self.cardWindow?.owns(event.window) == true
                || self.cardWindow?.isKey == true {
                return event
            }
            // **And the knowledge window owns every key it is sent**, same
            // two-test shape beside the card-window guard: an editor there
            // without a caret guard eats triage keys. `owns` is the narrow
            // one and `isKey` the total one, **below** the sheet test so a
            // file chooser attached to it satisfies `isSheet` first.
            if self.knowledgeWindow?.owns(event.window) == true
                || self.knowledgeWindow?.isKey == true {
                return event
            }
            // **And the access-log window**, same two-test shape and the
            // same place in the ladder.
            if self.accessLogWindow?.owns(event.window) == true
                || self.accessLogWindow?.isKey == true {
                return event
            }
            // **And the pairing QR owns every key it is sent**, same two-test
            // shape, **below** the sheet and card-window guards: S/R/D/W must
            // not fire while the QR is key, and Escape must close the pairing
            // window (it is closable) rather than hide the panel.
            if PairingWindowController.current?.owns(event.window) == true
                || PairingWindowController.current?.isKey == true {
                return event
            }
            // **And the settings window owns every key it is sent**, same
            // two-test shape, **below** the sheet guard: the enrolment
            // `NSOpenPanel` and any alert have to satisfy `isSheet` first.
            // Without this, `s`/`d`/`r`/`w`/`/` typed into the search field
            // would fire as triage on the agent list behind it, and Escape
            // would hide the panel instead of clearing the box.
            if self.settingsWindow?.owns(event.window) == true
                || self.settingsWindow?.isKey == true {
                return event
            }
            // **A focused native terminal owns every key it is sent** — not
            // Escape alone. Space, Tab, ↑ ↓ ← →, Return and the letter verbs
            // are all keys the agent on the other end of the pty reads, and
            // every one of them was consumed here before it reached the pane.
            // The `keys.editing` floor further down does not cover it: that
            // flag is raised from a 0.15s focus *poll* through the one
            // `stripEditing` boolean the reply strip also writes and the
            // selection watcher clears, so at any moment it reads false with
            // the caret in the pane, Space vanished and typing a word with a
            // "w" in it pressed **Close terminal** — twice, arm then confirm,
            // and the terminal the person was typing into ended. Two tests,
            // the shape the window guards above use: the published flag, and
            // the floor that cannot lag — AppKit's own answer about who holds
            // the caret at the instant of the press.
            let terminalWindow = event.window ?? NSApp.keyWindow
            if self.keys.terminalFocused || TerminalFocus.holdsCaret(terminalWindow) {
                // …and a handful of them AppKit will not carry there by
                // itself: ⌘⌫ and its family arrive as editing selectors
                // SwiftTerm has no case for and are dropped in silence, and
                // ⇧⏎ arrives with its modifiers already discarded. One pure
                // table decides for every combination — `TerminalKeys` —
                // and this is the only place its verdicts are performed.
                switch TerminalKeys.verdict(
                    keyCode: event.keyCode,
                    flags: event.modifierFlags,
                    characters: event.charactersIgnoringModifiers) {
                case .type(let bytes):
                    return TerminalFocus.send(bytes, to: terminalWindow)
                        ? nil : event
                case .app(let action):
                    guard let view = TerminalFocus.terminal(in: terminalWindow) else {
                        return event
                    }
                    switch action {
                    case .copy:
                        // **Validated, as the Edit menu validates it.**
                        // `validateUserInterfaceItem` gates `copy:` on
                        // `selection.active`, but calling the method straight
                        // bypasses that — and SwiftTerm's `copy(_:)` is
                        // unconditional: with no selection it does
                        // `clearContents()` and writes an empty string, so
                        // ⌘C on an unselected screen would **wipe the system
                        // clipboard**. With no selection this does nothing,
                        // exactly as VS Code.
                        guard view.selection?.active == true else { return nil }
                        view.copy(self)
                    case .paste: view.paste(self)
                    case .selectAll: view.selectAll(self)
                    }
                    return nil
                case .nobody:
                    // Only where a terminal really holds the caret. This
                    // branch is entered on the 0.15s poll *or* `holdsCaret`,
                    // so for up to 150 ms after the caret leaves the pane the
                    // flag is still true — and swallowing ⌘X / ⌘Z there would
                    // take them away from the board's search field, which had
                    // them before this table existed.
                    return TerminalFocus.terminal(in: terminalWindow) != nil
                        ? nil : event
                case .swiftTerm:
                    return event
                }
            }
            // **A focused control owns every key it is sent** — the rung
            // below the terminal and above Escape and the verbs. Today that
            // is the card face's assistant switcher (`ProviderSwitch`),
            // which handles ← / → / Space / Return / Escape itself; consumed
            // here, the arrows would move the row selection and the switcher
            // would look focused and do nothing.
            if self.keys.controlFocused { return event }
            // Anything with a command or control modifier belongs to the system
            // or to the field editor (⌘A, ⌘V, ⌘Q), never to triage.
            let modified = event.modifierFlags
                .intersection([.command, .control, .option])
                .isEmpty == false

            if event.keyCode == 53 {                     // Escape
                // Ordering documentation, and nothing else: a sheet is the
                // innermost thing Escape backs out of — before the filter,
                // before a drilled-in project, before the panel itself — so the
                // rank is written where the ladder is. The guard at the top of
                // this monitor tests the same `attachedSheet` — and the card
                // window beside it — and returns
                // first, so this line cannot fire; it is kept because the
                // sequence below is the thing a reader is here for, and the
                // reason Escape must not be consumed with a card open (it would
                // collapse the whole sidebar and take the card with it) belongs
                // beside the rungs it outranks. With the card in its own
                // window, its Close button carries `.cancelAction`, so a
                // passed-through Escape presses it — and is refused while a
                // create is on the wire, like every other close route. Delete it if you would rather
                // read that as prose — but do not delete the top guard on the
                // strength of it: this one covers Escape only.
                if self.panel?.attachedSheet != nil { return event }
                // The rank is `RailLayout.escapeRung`'s, table-tested: a
                // text box is given up first, then an open History run,
                // then a chosen History day, then History itself, then
                // the selected agent's detail, then the drill, then the
                // panel hides.
                switch RailLayout.escapeRung(editing: self.keys.editing,
                                             filterActive: self.keys.filterActive,
                                             detailOpen: self.keys.detailOpen,
                                             drilledIn: self.keys.drilledIn,
                                             historyRun: self.keys.historyRun,
                                             historyDay: self.keys.historyDay,
                                             historyOpen: self.keys.historyOpen) {
                case .endEditing:
                    // A filter is a thing you back out of before the panel is,
                    // even when the field itself no longer has focus.
                    self.keys.send(.clearFilter)
                    // …and a focused reply field is a *second* thing Escape has
                    // to be able to leave. `clearFilter` only ever touched the
                    // filter, so with a reply field focused this branch
                    // swallowed the press and changed nothing: Escape neither
                    // released the caret nor closed the panel, and clicking
                    // outside was the only way out. Resigning first responder
                    // here rather than in the view because the field's focus
                    // lives inside `ReplyBar`; the intent clears the flag that
                    // tells this monitor letters are being typed.
                    self.keys.send(.endEditing)
                    self.panel?.makeFirstResponder(nil)
                case .closeHistoryRun:
                    self.keys.send(.closeHistoryRun)
                case .closeHistoryDay:
                    self.keys.send(.closeHistoryDay)
                case .leaveHistory:
                    self.keys.send(.leaveHistory)
                case .deselect:
                    self.keys.send(.deselect)
                case .back:
                    self.keys.send(.back)
                case .hide:
                    self.hide()
                }
                return nil
            }
            guard !modified else { return event }

            // A focused ReplyBar or filter keeps the caret. Arrows and
            // Enter used to sit above this guard and retarget the waiter
            // (destroying the draft) while Space was already gated.
            guard !self.keys.editing else { return event }
            // …and so does a caret the view never published. `KeyRouter`
            // is raised by exactly three fields — the fleet filter, the reply
            // strip, the board search — so a caret anywhere else left the
            // letter verbs eating the alphabet, and an outside dictation app
            // inserting by simulated keystrokes had its transcript read as
            // triage: a dictated "stop" armed **Stop**. Asking AppKit who
            // holds the caret only ever *widens* the pass-through and never
            // narrows it, so the rule above stands and this is its floor:
            // `DictationFocus.isEditing` answers for the window's own first
            // responder, which is the field editor SwiftUI edits every
            // `TextField` through and the `NSTextView` behind every
            // `TextEditor` — a hidden or unparented one answering no, so a
            // search field that has scrolled out of existence cannot hold
            // the keyboard hostage.
            guard !DictationFocus.isEditing(event.window ?? NSApp.keyWindow) else {
                return event
            }

            switch event.keyCode {
            case 126: self.keys.send(.up);   return nil   // ↑
            case 125: self.keys.send(.down); return nil   // ↓
            case 36, 76: self.keys.send(.jump); return nil // Return / Enter
            default: break
            }
            switch event.keyCode {
            case 123: self.keys.send(.left);  return nil  // ←
            case 124: self.keys.send(.right); return nil  // →
            case 48:                                          // Tab
                if event.modifierFlags.contains(.shift) {
                    self.keys.send(.prevTab)
                } else {
                    self.keys.send(.nextTab)
                }
                return nil
            default: break
            }
            switch event.charactersIgnoringModifiers?.lowercased() {
            case " ":  self.keys.send(.open);        return nil
            case "d":  self.keys.send(.dismiss);     return nil
            case "s":  self.keys.send(.stop);        return nil
            case "r":  self.keys.send(.retire);      return nil
            case "w":  self.keys.send(.wrapUp);      return nil
            case "/":  self.keys.send(.focusFilter); return nil
            default:   return event
            }
        }
    }
}
