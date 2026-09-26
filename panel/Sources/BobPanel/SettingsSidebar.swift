import SwiftUI

/// The settings window's left column: the search field, the eight sections,
/// and the foot pinned under them on every section — Restart and Quit side by
/// side, the kill switch under them, all three drawn from the tree's own rows
/// (`SettingsSections.footer`), so the kill switch's armed title comes from
/// `SettingsMenuModel.rows` and nowhere else.
///
/// The section list is a `VStack` of drawn rows rather than a
/// `List(selection:)`: a list's sidebar chrome draws a vibrancy material
/// under `.darkAqua` that fights the Signal surfaces. ↑ / ↓ move the
/// selection (wrapping) once the list holds the keyboard — a click on a
/// section gives it the keyboard, and so does a Tab from the search box.
struct SettingsSidebar: View {
    @ObservedObject var state: SettingsWindowState
    let actions: SettingsActions
    let footer: [SettingsEntry]
    let security: SecuritySection
    @FocusState private var listFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            SettingsSearchField(state: state)
                .padding(.horizontal, 12)
                .padding(.top, 12)
                .padding(.bottom, 10)
            VStack(alignment: .leading, spacing: 2) {
                ForEach(SettingsSectionID.allCases) { section in
                    sectionRow(section)
                }
            }
            .padding(.horizontal, 8)
            .focusable()
            .focused($listFocused)
            .focusEffectDisabled()
            .onKeyPress(.upArrow) {
                state.select(section: Self.step(from: state.section, by: -1))
                return .handled
            }
            .onKeyPress(.downArrow) {
                state.select(section: Self.step(from: state.section, by: 1))
                return .handled
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Sections")
            Spacer(minLength: 12)
            foot
                .padding(12)
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .background(Theme.surface)
    }

    /// The section `delta` rows away, wrapping at both ends.
    static func step(from section: SettingsSectionID, by delta: Int) -> SettingsSectionID {
        let all = SettingsSectionID.allCases
        let index = all.firstIndex(of: section) ?? 0
        let count = all.count
        return all[((index + delta) % count + count) % count]
    }

    private func sectionRow(_ section: SettingsSectionID) -> some View {
        let chosen = state.section == section && !state.isSearching
        let badge = SettingsSections.badge(for: section, security: security)
        return Button {
            state.select(section: section)
            listFocused = true
        } label: {
            HStack(spacing: 8) {
                Text(section.title)
                    .font(Theme.prose(13.5, weight: chosen ? .semibold : .regular))
                    .foregroundStyle(chosen ? Theme.text : Theme.muted)
                Spacer(minLength: 4)
                if let badge {
                    Text(badge)
                        .font(Theme.mono(11, weight: .semibold))
                        .foregroundStyle(Theme.attention)
                        .accessibilityLabel(badge == "1" ? "1 open alert" : "\(badge) open alerts")
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: Theme.controlRadius)
                .fill(chosen ? Theme.raised : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .clickable()
        .accessibilityAddTraits(chosen ? .isSelected : [])
        .accessibilityHint("Command \(section.keyDigit)")
    }

    private var foot: some View {
        let sends = footer.filter {
            if case .send = $0.row.kind { return true }
            return false
        }
        let others = footer.filter { entry in !sends.contains(entry) }
        return VStack(spacing: 8) {
            HStack(spacing: 8) {
                ForEach(sends) { entry in
                    footButton(entry)
                }
            }
            ForEach(others) { entry in
                footButton(entry)
            }
        }
    }

    private func footButton(_ entry: SettingsEntry) -> some View {
        let row = entry.row
        let isKill = row.id == SettingsMenuModel.killRowId
        return SettingsBorderedButton(
            title: row.title,
            danger: SettingsControls.drawsDanger(row),
            armed: isKill && state.killArmed,
            disabled: row.disabled,
            fullWidth: true
        ) {
            switch row.kind {
            case .send(let action, let value):
                actions.sendAction(action: action, value: value)
            case .custom(let custom):
                actions.handleCustom(custom)
            default:
                break
            }
        }
        // The kill switch's two paragraphs stay on hover; the title says
        // what the press does.
        .help(row.tooltip)
        .settingsAnchors([row.id])
        .modifier(SettingsFlash(
            active: state.highlightedRowId == row.id,
            onEnd: {
                if state.highlightedRowId == row.id { state.highlightedRowId = nil }
            }))
    }
}
