import Foundation

/// The phone's container around `DetailTab`.
///
/// Main, Conversation, Details and Terminal are the phone's own screens, in
/// that order along the top of the agent sheet. Main is the sheet's lead —
/// the still, who this is, the card and every verb — as one scrolling page;
/// the others keep only the card's title above them. `DetailTab` stays the
/// Mac-shared rule for the stream and is never a case here.
enum AgentScreen: String, CaseIterable {
    case main = "Main"
    case conversation = "Conversation"
    case details = "Details"
    case terminal = "Terminal"

    /// What a fresh open lands on: Main, the first tab.
    static let defaultScreen: AgentScreen = .main

    /// Main whatever the Mac serves: it needs no read of its own. The
    /// argument stays so a caller that once chose by the conversation
    /// marker still compiles.
    static func defaultScreen(supported: Bool) -> AgentScreen { .main }

    /// The chips drawn: Main, Conversation and Details always, Terminal
    /// when hosted.
    static func chips(hosted: Bool) -> [AgentScreen] {
        hosted ? [.main, .conversation, .details, .terminal]
               : [.main, .conversation, .details]
    }

    /// The screen actually drawn. A row without a hosted terminal cannot
    /// leave Terminal selected — `DetailTab.pane`'s rule, one step out.
    static func pane(hosted: Bool, screen: AgentScreen) -> AgentScreen {
        if screen == .terminal && !hosted { return .details }
        return screen
    }

    /// Whether the fixed top keeps only the card's title: every tab but Main,
    /// whose page is the whole lead.
    static func titleOnly(_ screen: AgentScreen) -> Bool { screen != .main }

    /// Which stream tab the screen implies. Only Terminal is a stream.
    static func detailTab(_ screen: AgentScreen) -> DetailTab {
        screen == .terminal ? .terminal : .details
    }
}
