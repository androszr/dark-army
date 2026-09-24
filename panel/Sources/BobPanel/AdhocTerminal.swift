import SwiftUI

/// Starting an assistant with **no card behind it** — the + TERMINAL button
/// above the process table, and the small sheet it opens.
///
/// The rules live here as pure functions and the two views below read them
/// and re-derive nothing, which is the same split `PipelineBand` and
/// `RailLayout` keep: what is offered, and to whom, is a decision that can be
/// tabled in a test, and a `body` that decides it cannot be.
///
/// Nothing here talks to the board. The daemon's `spawn_terminal` writes no
/// card, moves no column and touches `board.db` not at all — the session it
/// produces is an ordinary one that turns up in the fleet like any other.
enum AdhocSpawn {

    /// Whether the button is drawn at all.
    ///
    /// **Absent, never inert**, and every one of the five terms is a
    /// different reason for that: `supported` is the version marker (an older
    /// daemon publishes no key, which decodes false), `dispatchEnabled` is
    /// the one switch that removes Dark Army-as-launcher, `enrollmentAvailable` is
    /// the daemon *stating* it has an enrolment ledger rather than the panel
    /// inferring one from an empty list, `enrolled` is the offer set — with
    /// nothing enrolled there is no folder to start in — and `tools` is what
    /// this Mac can actually launch. A button present with any of them false
    /// is a promise the app cannot keep.
    static func shouldOffer(dispatchEnabled: Bool,
                            enrollmentAvailable: Bool,
                            enrolled: [EnrolledProject],
                            tools: [String]) -> Bool {
        dispatchEnabled && enrollmentAvailable
            && !enrolled.isEmpty && !tools.isEmpty
    }

    /// The folders on offer: the ledger's own list, in the ledger's own
    /// order, with nothing re-sorted and nothing filtered. The daemon
    /// re-reads the same ledger under its dispatch lock at the press, so a
    /// folder un-enrolled between the offer and the press is refused there,
    /// in words — this list is a convenience, never the gate.
    static func candidates(_ enrolled: [EnrolledProject]) -> [EnrolledProject] {
        enrolled
    }

    /// Whether Start may be pressed: a folder and an assistant, both chosen.
    /// Everything else the daemon decides, and its refusal is drawn verbatim.
    static func canStart(root: String, tool: String) -> Bool {
        !root.isEmpty && !tool.isEmpty
    }
}

/// The press that opens the sheet. `BacklogView`'s START PROJECT styling,
/// because it is the same kind of act in the same kind of place: one outlined
/// word on the rail that starts an assistant.
struct AdhocTerminalButton: View {
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: 8) {
                Text("+ TERMINAL")
                    .font(Theme.mono(10, weight: .semibold))
                    .tracking(0.8)
                    .foregroundStyle(Theme.phosphor)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .overlay(Rectangle().stroke(Theme.hair, lineWidth: 1))
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 10)
        .frame(height: PanelMetrics.compactRow, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .clickable()
    }
}

/// Pick a folder, pick an assistant, press Start.
///
/// A sheet on the panel window (`DraftsSheet`'s pattern) rather than a
/// popover: this app draws no popovers, and `PanelExit.requested` already
/// ends every attached sheet before terminating, which a popover would not
/// be covered by.
struct AdhocTerminalSheet: View {
    let projects: [EnrolledProject]
    let tools: [String]
    let installed: [String: Bool]
    @Binding var root: String
    @Binding var tool: String
    /// The daemon's own words on a refusal, drawn verbatim — the board draws
    /// `dispatch_error` the same way, and a sentence Dark Army composed always
    /// beats one this view invents.
    let refusal: String
    let working: Bool
    let start: () -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("New terminal")
                .font(Theme.mono(13, weight: .medium))
                .foregroundStyle(Theme.phosphorBright)
            Text("An assistant in one of your projects, with no card behind it.")
                .font(Theme.mono(10))
                .foregroundStyle(Theme.dim)
                .fixedSize(horizontal: false, vertical: true)

            Picker("Project", selection: $root) {
                if root.isEmpty {
                    // A placeholder only while nothing is chosen, so the menu
                    // never offers "no project" as an answer.
                    Text("Choose a project…").tag("")
                }
                ForEach(AdhocSpawn.candidates(projects)) { project in
                    Text(project.label.isEmpty ? project.root : project.label)
                        .tag(project.root)
                }
            }
            .font(Theme.mono(12))

            HStack(spacing: 8) {
                Text("Assistant")
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                if ProviderChoice.wordChipOnly(tools: tools, selected: tool) {
                    // Never a menu: nothing offered, or a chosen assistant
                    // no longer offered, is an inert word chip that says so.
                    // The "Assistant" text beside this is a sibling rather
                    // than its label, so the chip names itself for VoiceOver.
                    Text(tool.isEmpty ? "—" : tool)
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 2)
                        .overlay(Rectangle().strokeBorder(Theme.hair, lineWidth: 1))
                        .accessibilityLabel(ProviderChoice.groupLabel)
                        .accessibilityValue(ProviderChoice.value(
                            selected: tool, name: ProviderMark.displayName))
                } else {
                    ProviderSwitch(tools: tools, installed: installed, selected: tool,
                                   interactive: true, showsNobody: false,
                                   pick: { tool = $0 })
                }
                Spacer(minLength: 0)
            }

            if !refusal.isEmpty {
                Text(refusal)
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Start", action: start)
                    .keyboardShortcut(.defaultAction)
                    .disabled(working
                              || !AdhocSpawn.canStart(root: root, tool: tool))
            }
        }
        .padding(16)
        .frame(width: 340)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
    }
}
