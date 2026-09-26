import SwiftUI

/// The Reports tab's rail: the `>` search line, the project rows, the
/// count and the way back to the board. The list itself is
/// `ScoutReportsPane`, in the board's place. The search field reports its
/// caret through `onEditing` (`PanelView.setStripEditing`, `CommRail`'s
/// hop), so the key monitor stands aside while somebody types.
struct ScoutReportsRail: View {
    let index: ScoutReportIndex
    /// The text search's reply and whether one is in flight; the count
    /// reads the pane's merged list.
    let hits: ScoutReportIndex
    let searching: Bool
    @Binding var search: String
    @Binding var project: String
    var onEditing: (Bool) -> Void = { _ in }
    var onRefresh: () -> Void = {}
    var onBoard: () -> Void = {}

    @FocusState private var searchFocused: Bool

    private var shown: Int {
        ScoutReportsPane.visible(index, search: search, project: project,
                                 hits: hits).count
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Text("# darkarmy · reports")
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                    searchField
                    Text("\(index.rows.count) reports · \(shown) shown"
                         + (searching ? " · searching…" : ""))
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                    projectRows
                    railButton(label: "Refresh", spoken: "Refresh the report list",
                               active: false, action: onRefresh)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
            }
            railButton(label: "‹ Board", spoken: "Back to the board",
                       active: true, action: onBoard)
                .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
                .padding(.horizontal, 14)
                .padding(.bottom, 10)
        }
        .onChange(of: searchFocused) { _, focused in onEditing(focused) }
        .onDisappear { onEditing(false) }
    }

    private var searchField: some View {
        HStack(spacing: 6) {
            Text(">")
                .font(Theme.mono(12, weight: .semibold))
                .foregroundStyle(Theme.phosphor)
                .accessibilityHidden(true)
            TextField(ScoutReportSearch.bodyPlaceholder, text: $search)
                .textFieldStyle(.plain)
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphorBright)
                .focused($searchFocused)
                .accessibilityLabel("Search the reports")
                .accessibilityHint("Matches titles, verdicts, questions and projects at once, and the reports' text after a pause")
            if !search.isEmpty {
                railButton(label: "×", spoken: "Clear the search", active: false,
                           fill: false) { search = "" }
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
    }

    private var projectRows: some View {
        VStack(alignment: .leading, spacing: 3) {
            projectButton(label: "ALL", value: "")
            ForEach(index.projects, id: \.self) { name in
                projectButton(label: name, value: name)
            }
        }
    }

    private func projectButton(label: String, value: String) -> some View {
        let active = project == value
        return railButton(label: label,
                          spoken: value.isEmpty ? "Every project" : "Project \(label)",
                          active: active, leading: true) { project = value }
            .background(active ? Theme.well : Color.clear)
    }

    /// Every press in this rail is a word in the rail's monospace: one
    /// button shape, one cursor affordance.
    private func railButton(label: String, spoken: String, active: Bool,
                            leading: Bool = false, fill: Bool = true,
                            action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(Theme.mono(11, weight: active ? .semibold : .regular))
                .foregroundStyle(active ? Theme.phosphor : Theme.faint)
                .frame(maxWidth: fill ? .infinity : nil,
                       alignment: leading ? .leading : .center)
                .padding(.horizontal, 6)
                .padding(.vertical, leading ? 2 : 6)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable()
        .accessibilityLabel(spoken)
    }
}
