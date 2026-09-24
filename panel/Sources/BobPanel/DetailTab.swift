import Foundation

/// Which of the agent detail's two tabs is on show, and the pure rules that
/// decide whether there are tabs at all.
///
/// A session on a terminal Dark Army hosts has two things worth reading — the
/// live screen and everything the pane showed before hosted terminals arrived
/// — and one pane to draw them in. These are the tabs. **Details is the
/// default on every open**: there is no persistence here and no per-session
/// memory, only `defaultTab`.
///
/// `terminalAttached` is the whole of the stream's lifetime, because not
/// drawing `TerminalPane` *is* the disconnect: its `.onDisappear` hands the
/// pty's width back to the phone and its `dismantleNSView` cancels the
/// socket. Nothing on the daemon changes.
enum DetailTab: String, CaseIterable {
    case details = "Details"
    case terminal = "Terminal"

    /// What a fresh open lands on, always.
    static let defaultTab: DetailTab = .details

    /// Only a hosted row has anywhere to switch to; a row running in an
    /// editor draws exactly today's layout, with no tab bar at all.
    static func showsTabBar(hosted: Bool) -> Bool { hosted }

    /// The tab actually drawn. A row that *stops* being hosted mid-view
    /// cannot leave a dead Terminal tab selected: without a terminal there
    /// is only one pane, and it is Details.
    static func pane(hosted: Bool, tab: DetailTab) -> DetailTab {
        hosted ? tab : .details
    }

    /// Whether the live screen is on show — and so whether the stream is
    /// connected and the Mac is holding that terminal's width.
    static func terminalAttached(hosted: Bool, tab: DetailTab) -> Bool {
        hosted && tab == .terminal
    }
}

/// What the detail says about a terminal the Mac is **not** drawing.
///
/// A row without a hosted terminal is, almost always, a session in an
/// editor window — but not when Dark Army itself opened its terminal
/// (`originBy == "adhoc"`, the + TERMINAL press, or `"mission"`, Mission
/// Control's standing terminal): that terminal was never
/// in an editor, and if the daemon no longer holds it the session inside
/// died with it. Saying "find it in the editor yourself" about it sent a
/// person looking for a tab that does not exist. The daemon retires such a
/// row on its own (`_retire_hostless_sessions`); this is the wording for the
/// frames in between, and it is one pure table both clients read.
enum TerminalWhereabouts: Equatable {
    /// The session runs in an editor window the Mac may or may not raise.
    case editor
    /// Dark Army opened this terminal and no longer holds it.
    case gone
    /// The session's tab is gone. The turn may still be working.
    case lost

    static func of(originBy: String, hosted: Bool, tabGone: Bool = false) -> TerminalWhereabouts {
        if tabGone { return .lost }
        if hosted { return .editor }
        if originBy == "mission" { return .gone }
        return originBy == "adhoc" ? .gone : .editor
    }

    /// The short label beside `# terminal`.
    var label: String {
        switch self {
        case .editor: return "runs in the editor"
        case .gone: return "terminal gone"
        case .lost: return "tab gone"
        }
    }

    /// The dim line under the state, where the Mac cannot raise anything.
    var hint: String {
        switch self {
        case .editor:
            return "Dark Army cannot raise this terminal — find it in the editor yourself."
        case .gone:
            return "Dark Army opened this terminal and no longer holds it — the session inside it has ended."
        case .lost:
            return "The tab is gone, so there is nowhere to open and nowhere to type; the session can still be working."
        }
    }
}
