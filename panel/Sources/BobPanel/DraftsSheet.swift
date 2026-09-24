import SwiftUI

/// The drafts list: what Dark Army kept, and the two ways out of it.
///
/// A sheet on the panel window rather than a window of its own — see the
/// comment on its presenter in `BoardView.createRow`. Both destructive verbs
/// arm then confirm, the same two-press guard every irreversible button in Dark Army
/// has, and both arms are **view** `@State`: the surface is modal, and a
/// shared `BoardState` slot would leave something behind it armed after it
/// closed (`BoardCardSheet`'s stated rule).
struct DraftsSheet: View {
    @ObservedObject var client: DaemonClient
    @ObservedObject var state: BoardState
    @Environment(\.dismiss) private var dismiss

    /// The one draft whose Delete is armed, and the Clear all arm. Separate
    /// slots for `deleteArmed`/`armed`'s reason: arming one must never
    /// silently re-aim the other.
    @State private var deleteArmedId: String?
    @State private var clearArmed = false
    /// The store's own refusal, in words. One line, `noteDoneScopeChanged`'s
    /// wording pattern.
    @State private var notice = ""

    /// How many rows Clear all would actually delete. The draft open in the
    /// composer is not one of them.
    private var deletableCount: Int {
        state.drafts.filter { $0.id != state.stagingId }.count
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Drafts")
                .font(Theme.mono(13, weight: .medium))
                .foregroundStyle(Theme.phosphorBright)
            Text("Cards you started and closed without saving. Dark Army keeps a "
                 + "copy on this Mac only — nothing here reaches the board "
                 + "until you save it.")
                .font(Theme.mono(11))
                .foregroundStyle(Theme.faint)
                .fixedSize(horizontal: false, vertical: true)
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(state.drafts) { draft in
                        row(draft)
                        Rectangle().fill(Theme.hair).frame(height: 1)
                    }
                }
            }
            .frame(maxHeight: 320)
            .background(Theme.well)
            if !notice.isEmpty {
                Text(notice)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.alarm)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                if deletableCount > 0 {
                    Button {
                        clearAll()
                    } label: {
                        Text(clearArmed
                             ? "Delete \(deletableCount) drafts?"
                             : "CLEAR ALL")
                    }
                    .buttonStyle(AlarmOutline())
                    .clickable()
                    .help("Throw away every draft here, and the files they "
                          + "carry. Asks once more first.")
                }
                Spacer()
                Button("Close") { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(16)
        .frame(width: 460)
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        .cursorAffordances(true)
        // Any change under the arm disarms it. The store's own recount is the
        // backstop, not the affordance.
        .onChange(of: state.drafts.count) { _, _ in
            deleteArmedId = nil
            clearArmed = false
        }
    }

    private func row(_ draft: CardDraft) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(draft.displayTitle)
                    .font(Theme.mono(12, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .lineLimit(1)
                Text(subtitle(draft))
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            Button {
                state.resumeDraft(draft)
                dismiss()
            } label: {
                Text("OPEN")
            }
            .buttonStyle(AlarmOutline())
            .clickable()
            if draft.id == state.stagingId {
                // Deleting the folder under a live composer's staged list is
                // the one way this feature could eat work instead of keeping
                // it. The row says so instead of offering the press.
                Text("open in the composer")
                    .font(Theme.mono(10))
                    .foregroundStyle(Theme.faint)
            } else {
                Button {
                    delete(draft)
                } label: {
                    Text(deleteArmedId == draft.id ? "Sure?" : "DELETE")
                }
                .buttonStyle(AlarmOutline())
                .clickable()
                .help("Throw this draft away, and the files attached to it.")
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 10)
    }

    /// Project and how long ago it was last touched.
    private func subtitle(_ draft: CardDraft) -> String {
        var parts: [String] = []
        let project = draft.project.trimmingCharacters(
            in: .whitespacesAndNewlines)
        parts.append(project.isEmpty ? "Other" : project)
        let age = Date().timeIntervalSince1970 - draft.updatedAt
        parts.append(Format.duration(age) + " ago")
        return parts.joined(separator: " · ")
    }

    private func delete(_ draft: CardDraft) {
        notice = ""
        guard deleteArmedId == draft.id else {
            deleteArmedId = draft.id
            clearArmed = false
            return
        }
        deleteArmedId = nil
        let id = draft.id
        CardDrafts.remove(id)
        Task.detached { CardAttachments.discard(id) }
        state.reloadDrafts()
    }

    private func clearAll() {
        notice = ""
        guard clearArmed else {
            clearArmed = true
            deleteArmedId = nil
            return
        }
        clearArmed = false
        guard let gone = CardDrafts.clearAll(expectedCount: deletableCount,
                                             excluding: state.stagingId)
        else {
            notice = "The list changed; nothing was deleted. "
                + "Confirm again against the new count."
            state.reloadDrafts()
            return
        }
        for id in gone {
            Task.detached { CardAttachments.discard(id) }
        }
        state.reloadDrafts()
    }
}
