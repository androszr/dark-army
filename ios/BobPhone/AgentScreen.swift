import Foundation

/// The phone's container around `DetailTab`.
///
/// Conversation, Details and Terminal are the phone's own screens. `DetailTab`
/// stays the Mac-shared rule for the stream and is never a third case here.
enum AgentScreen: String, CaseIterable {
    case conversation = "Conversation"
    case details = "Details"
    case terminal = "Terminal"

    /// What a fresh open lands on when the Mac supports the read.
    static let defaultScreen: AgentScreen = .conversation

    /// `.conversation` where the Mac states the marker, else `.details`.
    static func defaultScreen(supported: Bool) -> AgentScreen {
        supported ? .conversation : .details
    }

    /// The chips drawn: Conversation and Details always, Terminal when hosted.
    static func chips(hosted: Bool) -> [AgentScreen] {
        hosted ? [.conversation, .details, .terminal] : [.conversation, .details]
    }

    /// The screen actually drawn. A row without a hosted terminal cannot
    /// leave Terminal selected — `DetailTab.pane`'s rule, one step out.
    static func pane(hosted: Bool, screen: AgentScreen) -> AgentScreen {
        if screen == .terminal && !hosted { return .details }
        return screen
    }

    /// Which stream tab the screen implies. Conversation is not a stream.
    static func detailTab(_ screen: AgentScreen) -> DetailTab {
        screen == .terminal ? .terminal : .details
    }
}
