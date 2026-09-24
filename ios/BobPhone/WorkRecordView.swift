import SwiftUI

/// What Dark Army observed a card's last run do, on the phone.
///
/// This is the surface the whole feature exists for: a day later, from away,
/// a person has to be able to open a card and answer *was the thing this card
/// asked for delivered*. So every sentence here is the daemon's own, drawn
/// verbatim — the caption, the verdict line, and the reason a file list is
/// missing. The view composes **no wording of its own** about what happened,
/// which is what keeps the Mac and the phone saying the same thing.
///
/// Fetched only when the card is opened, over the same sealed route the
/// phone already reads Dark Army's other reports on. Never on the poll, never from
/// the background check-in, never from the widget.
struct PhoneWorkRecordSection: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    let card: BoardCard

    @State private var record: WorkRecord?
    @State private var caption = ""
    @State private var unsupported = false
    @State private var unreachable = false

    /// The record's own stamp, so this re-fetches exactly once per finished
    /// run rather than on every snapshot.
    private var fetchKey: String {
        "\(card.id)-\(card.workRecord?.at ?? 0)"
    }

    var body: some View {
        if card.workRecord != nil || unsupported {
            VStack(alignment: .leading, spacing: 8) {
                Rectangle().fill(Theme.hair).frame(height: 1)
                Text("WHAT CHANGED")
                    .font(Theme.mono(10, weight: .medium))
                    .foregroundStyle(Theme.phosphor)
                    .tracking(0.8)
                    .accessibilityAddTraits(.isHeader)
                if unsupported {
                    // A 404 from the sealed reply is a Mac that has never
                    // heard of work records. Said in words — never drawn as
                    // an empty record, which would read as a run that
                    // changed nothing.
                    Text("This Mac's Dark Army is too old to keep a record of what a run changed.")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text(caption.isEmpty ? Self.caption : caption)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                    body(for: record)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .task(id: fetchKey) { await load() }
        }
    }

    /// The Mac's own caption, kept here as the fallback for a reply that
    /// carried none. Byte-identical to `work_record.CAPTION`, and pinned as
    /// such — the wording is a decision about honesty and it is made once,
    /// in the daemon, rather than three times in three languages.
    static let caption = "Dark Army watched this run and wrote this down; nothing here is the assistant's own claim. The files are what changed in this project since the work started, which can include another agent working in the same folder."

    @ViewBuilder
    private func body(for record: WorkRecord?) -> some View {
        if let record {
            let words = WorkRecordFormat.verdictWords(record.verdict)
            if !words.isEmpty {
                Text(words)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !record.report.isEmpty {
                // Somebody else's words: rendered through the phone's own
                // byte-pinned markdown copy, never parsed, never used to
                // decide anything on this screen.
                MarkdownText(source: record.report, base: 12, mono: true)
                    .equatable()
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(WorkRecordFormat.summary(record))
                .font(Theme.mono(12))
                .foregroundStyle(record.filesAvailable ? Theme.dim : .orange)
                .fixedSize(horizontal: false, vertical: true)
            // What the shunt helper did — the Mac's own sentence
            // (`shuntWords`), drawn verbatim and only where something
            // was delegated; the phone counts nothing.
            if record.shuntDelegations > 0 && !record.shuntWords.isEmpty {
                Text(record.shuntWords)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(record.files.enumerated()), id: \.offset) { pair in
                DecryptButton(action: { sheets.show(.workFile(card.id, pair.offset, pair.element)) }) {
                    HStack(spacing: 8) {
                        Text(pair.element.path)
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.phosphor)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .multilineTextAlignment(.leading)
                        Text(WorkRecordFormat.counts(pair.element))
                            .font(Theme.mono(11))
                            .foregroundStyle(Theme.dim)
                    }
                    .frame(minHeight: 44)
                    // The path and its counts are one file, not two
                    // fragments. On the link's *label*, so the link keeps
                    // supplying the press.
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(
                        "\(pair.element.path), \(WorkRecordFormat.counts(pair.element))")
                }
                .buttonStyle(.plain)
            }
        } else if unreachable {
            Text("Dark Army could not be reached for this record.")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
        } else {
            Text("Reading Dark Army's record…")
                .font(Theme.mono(12))
                .foregroundStyle(Theme.dim)
        }
    }

    private func load() async {
        record = nil
        unsupported = false
        unreachable = false
        guard card.workRecord != nil else { return }
        let wanted = card.id
        switch await client.workRecord(cardId: wanted) {
        case .ok(let report):
            guard wanted == card.id else { return }
            caption = report.caption
            record = report.record
        case .unsupported:
            unsupported = true
        case .unreachable:
            unreachable = true
        }
    }
}

/// One file's changes, fetched one file at a time and only for a file that is
/// already on Dark Army's own list for this run.
///
/// A diff is not markdown, so it is drawn as plain monospaced text inside its
/// own horizontal scroll — a long line must never make the screen scroll
/// sideways.
struct PhoneWorkRecordFileView: View {
    @ObservedObject var client: PhoneClient
    let cardId: String
    let index: Int
    let file: WorkRecordFile

    @State private var diff: WorkRecordDiff?
    @State private var failed = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(file.path)
                    .font(Theme.mono(12, weight: .medium))
                    .foregroundStyle(Theme.phosphorBright)
                    .textSelection(.enabled)
                Text(WorkRecordFormat.counts(file))
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.dim)
                if let diff {
                    if diff.available {
                        ScrollView(.horizontal) {
                            Text(diff.text)
                                .font(Theme.mono(10))
                                .foregroundStyle(Theme.dim)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if diff.truncated {
                            Text("· cut short")
                                .font(Theme.mono(11))
                                .foregroundStyle(Theme.faint)
                        }
                    } else {
                        Text(diff.reason)
                            .font(Theme.mono(12))
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                } else if failed {
                    Text("Dark Army could not be reached for that file.")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                } else {
                    Text("Reading that file…")
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .overlay(ScanlineOverlay())
        // The bar cannot wrap a long path; the whole path is the first
        // line of the body, selectable.

        .decryptSurface("WorkRecordView")
        .task(id: "\(cardId)-\(index)") {
            let fetched = await client.workRecordDiff(cardId: cardId,
                                                      file: index)
            if fetched == nil { failed = true }
            diff = fetched
        }
    }
}
