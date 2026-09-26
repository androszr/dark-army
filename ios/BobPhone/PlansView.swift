import SwiftUI

/// The Menu's Plans screen: every plan the Mac lists, newest first by the
/// day in its file name, each row its title, its status and area where the
/// plan states them, its project and day, and the card it belongs to; a `>`
/// line narrows the list as you type (`PlanSearch`). The list is asked for
/// once when the screen appears — never on the poll or the background
/// refresh — and a tapped row pushes the reader, which fetches the text at
/// that moment. Drawing is on the main actor; the read suspends on the
/// sealed door, `scoutReportsIndex`'s route.
struct PlansView: View {
    @ObservedObject var client: PhoneClient
    @State private var index = PlanIndex()
    @State private var detail = ""
    @State private var loading = false
    @State private var loaded = false
    @State private var query = ""
    /// The path of the plan pushed onto this tab's stack.
    @State private var openedPath: String?
    @FocusState private var searchFocused: Bool

    private var rows: [PlanRow] {
        index.rows.filter { PlanSearch.matches(query: query, row: $0) }
    }

    var body: some View {
        List {
            searchField
                .listRowInsets(EdgeInsets(top: 8, leading: 0, bottom: 4, trailing: 0))
                .listRowSeparator(.hidden)
                .listRowBackground(Theme.bg)
            if !detail.isEmpty {
                Text(detail)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
                    .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            if let line = emptyLine {
                CommentLine(text: line)
                    .listRowInsets(EdgeInsets(top: 6, leading: 14, bottom: 6, trailing: 14))
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
            ForEach(rows) { row in
                PlanListRow(row: row) { openedPath = row.path }
                    .listRowInsets(EdgeInsets())
                    .listRowSeparator(.hidden)
                    .listRowBackground(Theme.bg)
            }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .scrollDismissesKeyboard(.interactively)
        .background(Theme.bg)
        .navigationTitle("plans")
        .decryptSurface("PlansView")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        // On the list itself, never inside a lazy row.
        .navigationDestination(item: $openedPath) { path in
            PlanReaderView(client: client, row: row(for: path))
        }
        .task { await load() }
    }

    private var emptyLine: String? {
        if loading && index.rows.isEmpty { return "reading the plans…" }
        if PlanSearch.isSearching(query), rows.isEmpty, !index.rows.isEmpty {
            return PlanSearch.noMatchLine(query: query)
        }
        if loaded, index.rows.isEmpty, detail.isEmpty { return "no plans yet" }
        return nil
    }

    private func row(for path: String) -> PlanRow {
        if let row = index.rows.first(where: { $0.path == path }) { return row }
        var row = PlanRow()
        row.path = path
        return row
    }

    private var searchField: some View {
        HStack(spacing: 8) {
            Text(">")
                .font(Theme.mono(12, weight: .medium))
                .foregroundStyle(Theme.phosphor)
                .accessibilityHidden(true)
            TextField("", text: $query,
                      prompt: Text(PlanSearch.placeholder).foregroundStyle(Theme.faint))
                .font(Theme.mono(12))
                .foregroundStyle(Theme.phosphorBright)
                .textFieldStyle(.plain)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.search)
                .onSubmit { searchFocused = false }
                .focused($searchFocused)
                .accessibilityLabel("Search the plans")
                .accessibilityHint("Narrows the list to plans whose title, project, status, area, file name or card contain the words")
            if searchFocused || !query.isEmpty {
                DecryptButton(query.isEmpty ? "× DONE" : "× CLEAR") {
                    query = ""
                    searchFocused = false
                }
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.phosphor)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                    .buttonStyle(.plain)
                    .accessibilityLabel(query.isEmpty ? "Hide the keyboard"
                                                      : "Clear the search")
            }
        }
        .frame(maxWidth: .infinity, minHeight: 36, alignment: .leading)
        .padding(.horizontal, 12)
        .fieldWell(focused: searchFocused, onTap: { searchFocused = true })
        .padding(.horizontal, 12)
    }

    /// Once per screen: a return from the reader keeps the list it had.
    private func load() async {
        guard !loaded else { return }
        let requested = "index"
        index = PlanIndex()
        detail = ""
        loading = true
        let fetched = await client.plansIndex()
        loading = false
        guard let outcome = PlansLoad.apply(requested: requested,
                                            current: "index",
                                            fetched: fetched)
        else { return }
        index = outcome.value
        detail = outcome.detail
        // A failed fetch stays retryable: the next visit asks again.
        loaded = fetched != nil
    }
}

/// One plan in the list: the title, then `status · area` where the plan
/// states them, `project · day`, and the card it belongs to. One element
/// for a screen reader, spoken from the fields it draws.
struct PlanListRow: View {
    let row: PlanRow
    let onOpen: () -> Void

    var body: some View {
        DecryptButton(action: onOpen) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(row.displayTitle)
                        .font(Theme.mono(12, weight: .semibold))
                        .foregroundStyle(Theme.phosphorBright)
                        .fixedSize(horizontal: false, vertical: true)
                    if !row.headerLine.isEmpty {
                        Text(row.headerLine)
                            .font(Theme.mono(12))
                            .foregroundStyle(Theme.dim)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !row.metaLine.isEmpty {
                        Text(row.metaLine)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.faint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !row.cardLine.isEmpty {
                        Text(row.cardLine)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 4)
                Text("›")
                    .font(Theme.mono(14))
                    .foregroundStyle(Theme.faint)
                    .accessibilityHidden(true)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(spoken)
        .accessibilityHint("Opens the plan")
    }

    private var spoken: String {
        var words = row.displayTitle
        if !row.headerLine.isEmpty { words += ". " + row.headerLine }
        if !row.metaLine.isEmpty { words += ". " + row.metaLine }
        if !row.cardLine.isEmpty { words += ". " + row.cardLine }
        return words
    }
}
