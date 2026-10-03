import AppKit
import SwiftUI

// The commands the menu bar sends this process over stdin, and the EOF that
// ends it. Split out of `main.swift` on 20 Sep 2026.
extension AppDelegate {
    // MARK: - stdin

    /// JSONSerialization boxes numbers as `NSNumber`. `as? Double` then fails
    /// for an integer-valued percent (`26`) and the Grok chip never appears.
    func jsonDouble(_ value: Any?) -> Double? {
        if let number = value as? NSNumber { return number.doubleValue }
        return value as? Double
    }

    /// Read commands off stdin on a background queue. The menu-bar app owns this
    /// process's lifetime, so end-of-file means it has gone and we should too.
    nonisolated func readCommands(quitOnEOF: Bool) {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            while let line = readLine(strippingNewline: true) {
                guard let data = line.data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data)
                        as? [String: Any],
                      let action = obj["action"] as? String else { continue }
                DispatchQueue.main.async {
                  MainActor.assumeIsolated {
                    guard let self else { return }
                    switch action {
                    case "show":
                        self.setAnchor(obj)
                        self.show(x: obj["x"] as? Double, y: obj["y"] as? Double)
                        self.requestFocus(obj)
                    case "hide":
                        self.hide()
                    case "toggle":
                        self.setAnchor(obj)
                        // A window that exists but cannot be seen —
                        // miniaturised, fully covered, on another Space before
                        // the move-to-active behavior pulls it over — must answer a
                        // strip click by appearing, not by hiding somewhere
                        // off-stage.
                        let seen = self.panel.isVisible
                            && self.panel.occlusionState.contains(.visible)
                        // A toggle that names a session is not a toggle: it came
                        // from a tap that means "show me this one", and hiding
                        // the panel is never that answer.
                        if seen, obj["focus"] == nil {
                            self.hide()
                        } else {
                            self.show(x: obj["x"] as? Double, y: obj["y"] as? Double)
                            self.requestFocus(obj)
                        }
                    case "context":
                        // Facts only the menu bar has: build staleness, and the
                        // Grok window, which it fetches from another account.
                        var ctx = self.client.context
                        if let build = obj["build"] as? String { ctx.build = build }
                        if let stale = obj["build_stale"] as? Bool { ctx.buildStale = stale }
                        if let can = obj["can_rebuild"] as? Bool { ctx.canRebuild = can }
                        if let label = obj["rebuild_label"] as? String, !label.isEmpty {
                            ctx.rebuildLabel = label
                        }
                        if let outcome = obj["rebuild_outcome"] as? String {
                            ctx.rebuildOutcome = outcome
                        }
                        if let err = obj["rebuild_error"] as? String {
                            ctx.rebuildError = err
                        }
                        ctx.rebuildStartedAt = self.jsonDouble(obj["rebuild_started_at"])
                        // The desk token: a present, non-empty value replaces;
                        // absent (an older app) or empty keeps what is held,
                        // so a push never blanks the panel's writes.
                        if let tok = obj["desk_token"] as? String, !tok.isEmpty {
                            ctx.deskToken = tok
                        }
                        ctx.grokPercent = self.jsonDouble(obj["grok_percent"])
                        ctx.grokResetsAt = self.jsonDouble(obj["grok_resets_at"])
                        if let raw = obj["settings"] as? [String: Any] {
                            // Qualified: SwiftUI has its own `Settings`, a Scene.
                            ctx.settings = DaemonClient.Settings(raw, base: ctx.settings)
                        }
                        self.client.context = ctx
                        DictationFocus.logGate(
                            whisper: ctx.settings.macwhisperInstalled,
                            trusted: ctx.settings.accessibilityTrusted,
                            label: ctx.settings.dictationShortcutLabel)
                        self.applyScale(ctx.settings.panelScale)
                    case "quit":
                        PanelExit.requested("quit on stdin")
                    default:
                        break
                    }
                  }
                }
            }
            // End of stdin means the menu-bar app that launched us has gone, so
            // we should follow it. Only when something was actually on the other
            // end, though: run by hand, stdin is often a tty or /dev/null, and
            // /dev/null is EOF immediately — quitting on that would make the
            // panel impossible to look at on its own. `quitOnEOF` is that test
            // (`stdinIsDriven()`, plus --hidden for a driven instance whose pipe
            // we somehow cannot recognise).
            if quitOnEOF {
                DispatchQueue.main.async {
                    MainActor.assumeIsolated { PanelExit.requested("stdin EOF") }
                }
            }
        }
    }
}
