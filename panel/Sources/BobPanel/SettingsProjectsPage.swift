import Foundation

/// The Projects section as a list and a detail: the enrolled folders down the
/// left, the chosen one's rows on the right. Pure, over the `submenu:projects`
/// row `SettingsMenuModel.projectRows` built — the rows are split, never
/// rebuilt or retitled.
enum SettingsProjectsPage {
    struct ProjectItem: Equatable, Identifiable {
        /// Off the `submenu:project:<root>` id, never the label: two projects
        /// may share a label.
        var root: String
        var label: String
        /// The project's nested rows, `info:project-root:<root>` first and
        /// Un-enrol last.
        var rows: [SettingsRow]

        var id: String { root }
    }

    struct Layout: Equatable {
        var projects: [ProjectItem]
        /// What sits under the list: the enrolment line where there is one,
        /// and **Enrol a folder…**. Dividers dropped.
        var listFooter: [SettingsRow]
        /// The project whose root matches the selection, else the first; nil
        /// with nothing enrolled.
        var selected: ProjectItem?
    }

    static let projectPrefix = "submenu:project:"

    static func build(projectsRow: SettingsRow, selectedRoot: String?) -> Layout {
        guard case .submenu(_, let nested) = projectsRow.kind else {
            return Layout(projects: [], listFooter: [], selected: nil)
        }
        var projects: [ProjectItem] = []
        var footer: [SettingsRow] = []
        for row in nested {
            switch row.kind {
            case .submenu(let title, let rows) where row.id.hasPrefix(projectPrefix):
                projects.append(ProjectItem(
                    root: String(row.id.dropFirst(projectPrefix.count)),
                    label: title, rows: rows))
            case .divider:
                continue
            default:
                footer.append(row)
            }
        }
        let selected = projects.first { $0.root == selectedRoot } ?? projects.first
        return Layout(projects: projects, listFooter: footer, selected: selected)
    }
}
