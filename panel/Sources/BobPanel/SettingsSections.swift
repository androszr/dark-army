import Foundation

/// The settings window's eight sidebar sections, in the order the sidebar
/// lists them and ⌘1…⌘8 reach them.
enum SettingsSectionID: String, CaseIterable, Identifiable {
    case general, sessions, board, models, projects, devices, security, advanced

    var id: String { rawValue }

    var title: String {
        switch self {
        case .general: return "General"
        case .sessions: return "Sessions"
        case .board: return "Board"
        case .models: return "Models"
        case .projects: return "Projects"
        case .devices: return "Devices"
        case .security: return "Security"
        case .advanced: return "Advanced"
        }
    }

    /// The page's one-line lede, from the approved mockup.
    var subtitle: String {
        switch self {
        case .general:
            return "How Dark Army gets your attention, how big the panel draws, and dictation."
        case .sessions:
            return "What Dark Army does inside a running session."
        case .board:
            return "Starting, queueing and finishing work from the board."
        case .models:
            return "Which model each assistant runs on, machine-wide. A project can "
                + "override any one choice under Projects."
        case .projects:
            return "Enrolled folders, their agent pack, knowledge, checks and model overrides."
        case .devices:
            return "Your paired phone and how it reaches this Mac."
        case .security:
            return "Refused attempts to reach Dark Army over the phone doors."
        case .advanced:
            return "Repairs, diagnostics and development tools. You should rarely need these."
        }
    }

    /// 1…8: the case's ordinal, the digit ⌘ reaches it by.
    var keyDigit: Int {
        (Self.allCases.firstIndex(of: self) ?? 0) + 1
    }
}

/// One section's page: its groups, in the section table's member order.
struct SettingsPage: Equatable, Identifiable {
    var id: SettingsSectionID
    var groups: [SettingsGroup]

    var title: String { id.title }
}

/// Where each `SettingsGroup` is drawn. **The second heading table**, beside
/// `SettingsSearch.looseSection`: it names sections for *groups*, never rows,
/// so `SettingsMenuModel.rows` stays the only inventory. A group this table
/// does not know has no placement at all — there is no fallback section, and
/// `SettingsSectionsTests` fails on it rather than drawing it somewhere made up.
enum SettingsSections {
    enum Placement: Equatable {
        case page(SettingsSectionID)
        /// The sidebar's foot: Restart, Quit and the kill switch, on every section.
        case footer
    }

    /// Each section's groups, in drawing order. Advanced lists its own group
    /// before the design system's; Board lists the loose board switches before
    /// Pipeline, whose rows the page splices under the master switch.
    static let members: [(SettingsSectionID, [String])] = [
        (.general, ["submenu:notifications", "submenu:panel-size", "submenu:dictation"]),
        (.sessions, ["loose:Sessions"]),
        (.board, ["loose:Board", "submenu:pipeline"]),
        (.models, [SettingsMenuModel.agentModelsRowId]),
        (.projects, ["submenu:projects"]),
        (.devices, ["submenu:devices"]),
        (.security, ["submenu:security"]),
        (.advanced, ["submenu:advanced", "submenu:design-system"]),
    ]

    /// The sidebar foot's groups, in order.
    static let footerMembers = ["loose:Kill switch", "loose:This app"]

    static func placement(groupId: String) -> Placement? {
        if footerMembers.contains(groupId) { return .footer }
        for (section, ids) in members where ids.contains(groupId) {
            return .page(section)
        }
        return nil
    }

    /// Master row id → the ids that only matter while it is on: a group id
    /// (drawn spliced under the master) or a row id. Dependents are drawn
    /// indented and dimmed on the master's *drawn* state, and stay pressable —
    /// dimmed, never disabled: the two phone switches are independent by design.
    static let dependents: [String: [String]] = [
        "toggle:set_board_dispatch": ["submenu:pipeline"],
        "toggle:set_lan_access": [
            "toggle:set_remote_access", "custom:relayAddress", "toggle:set_relay_ws",
            "custom:relaySocketAddress", "info:away-needs-lan",
        ],
    ]

    /// The master a dependent id belongs to, or nil.
    static func master(of id: String) -> String? {
        dependents.first { $0.value.contains(id) }?.key
    }

    /// Always eight pages, in `SettingsSectionID.allCases` order. A page may
    /// be empty where its groups are absent (Models against a menu bar that
    /// sends no table).
    static func pages(_ groups: [SettingsGroup]) -> [SettingsPage] {
        SettingsSectionID.allCases.map { section in
            let ids = members.first { $0.0 == section }?.1 ?? []
            return SettingsPage(
                id: section,
                groups: ids.compactMap { id in groups.first { $0.id == id } })
        }
    }

    /// The sidebar foot's entries — the kill switch, Restart, Quit — with the
    /// dividers between them dropped: the foot draws its own spacing.
    static func footer(_ groups: [SettingsGroup]) -> [SettingsEntry] {
        footerMembers.flatMap { id in
            (groups.first { $0.id == id }?.entries ?? []).filter { !$0.isDivider }
        }
    }

    /// The count a sidebar row wears, or nil. Only Security has one: the open
    /// alerts, the same `security.alerts.count` its page states in words, so
    /// the two can never disagree. Nil at zero and while the log is unavailable.
    static func badge(for section: SettingsSectionID, security: SecuritySection) -> String? {
        guard section == .security, security.available, !security.alerts.isEmpty else {
            return nil
        }
        return String(security.alerts.count)
    }
}
