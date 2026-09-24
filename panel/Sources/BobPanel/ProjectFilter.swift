/// The board's project filter as data: one item per project, plus the ALL
/// predicate that lights the pinned chip.
///
/// Membership, order and the stale-tick union live in `projectFilterItems`;
/// the switch row is that list without the nameless ALL head, because ALL is
/// drawn outside the scrolling strip. Display names (`""` → "Other") go
/// through `projectFilterDisplayName`. The pointing-hand overlay is for
/// drawn chips — see `ProjectSwitchChip`.

/// One row of the filter, as data — pure, `Equatable`. `nil` name is the
/// ALL verb (clears every tick); `""` is the "Other" pile, a real member.
struct ProjectFilterItem: Equatable {
    let title: String
    /// The project this row toggles. `nil` is the ALL verb, which clears
    /// every tick; `""` is the "Other" row — a real, selectable project
    /// pile, which is exactly the distinction the old `Picker`'s colliding
    /// `""` tags erased.
    let name: String?
    let ticked: Bool
}

/// "Other" for the no-project pile, the name itself for everything else.
func projectFilterDisplayName(_ name: String) -> String {
    name.isEmpty ? "Other" : name
}

/// Lit when nothing is ticked, or every known name is. The empty-`allNames`
/// guard is load-bearing: `allSatisfy` is vacuously true of `[]`, and a
/// stale tick against a snapshot that has not arrived yet must not light ALL.
func projectFilterShowsAll(selected: Set<String>, allNames: [String]) -> Bool {
    if selected.isEmpty { return true }
    return !allNames.isEmpty && allNames.allSatisfy { selected.contains($0) }
}

/// The menu's rows: "All projects" first (never ticked — it is a verb, not a
/// member), then one row per known project **union the persisted ticks**, in
/// `allNames` order with stale ticks appended sorted.
func projectFilterItems(selected: Set<String>,
                        allNames: [String]) -> [ProjectFilterItem] {
    var items = [ProjectFilterItem(title: "All projects",
                                   name: nil, ticked: false)]
    let names = allNames + selected.subtracting(allNames).sorted()
    for name in names {
        items.append(ProjectFilterItem(title: projectFilterDisplayName(name),
                                       name: name,
                                       ticked: selected.contains(name)))
    }
    return items
}

/// The scrolling chips: `projectFilterItems` minus the nameless ALL head,
/// which is drawn pinned outside the strip.
func projectSwitchRows(selected: Set<String>,
                       allNames: [String]) -> [ProjectFilterItem] {
    projectFilterItems(selected: selected, allNames: allNames)
        .filter { $0.name != nil }
}
