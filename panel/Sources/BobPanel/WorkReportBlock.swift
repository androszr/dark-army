import SwiftUI

/// A finished agent's report, drawn as the parts it names: `# work done`,
/// then ASKED as prose, CHANGED and VERIFIED as bullets, UNCHECKED as
/// numbered steps with the dim "Why not automated" line under them — or the
/// one line that says nothing was left — and CARD.
///
/// Draws `WorkReport.Parsed` as the daemon sent it (`work_report.py`); every
/// word and caption comes from `WorkReport`, the rule the phone's
/// `PhoneWorkReportBlock` draws by too. An unlabelled report never reaches
/// here: `StdoutPane` keeps the raw markdown block for it.
struct WorkReportBlock: View {
    let parsed: WorkReport.Parsed

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Rectangle().fill(Theme.hair).frame(height: 1)
            Text(WorkReport.heading)
                .font(Theme.mono(10))
                .foregroundStyle(Theme.faint)
            if parsed.cut {
                line(WorkReport.cutNote, color: Theme.faint)
            }
            ForEach(WorkReport.sections(parsed), id: \.self) { section in
                VStack(alignment: .leading, spacing: 3) {
                    Text(caption(section))
                        .font(Theme.mono(10))
                        .foregroundStyle(Theme.faint)
                    content(section)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func caption(_ section: WorkReport.Section) -> String {
        section == .unchecked
            ? WorkReport.uncheckedCaption(count: WorkReport.total(parsed, .unchecked),
                                          nothing: parsed.nothingUnchecked)
            : section.caption
    }

    @ViewBuilder
    private func content(_ section: WorkReport.Section) -> some View {
        switch section {
        case .asked:
            line(parsed.asked, color: Theme.dim)
        case .changed:
            bullets(parsed.changed, total: WorkReport.total(parsed, .changed))
        case .verified:
            bullets(parsed.verified, total: WorkReport.total(parsed, .verified))
        case .unchecked:
            if parsed.nothingUnchecked {
                line(WorkReport.nothingUnchecked, color: Theme.phosphor)
            } else {
                ForEach(Array(WorkReport.numbered(parsed.unchecked).enumerated()),
                        id: \.offset) { item in
                    line(item.element, color: Theme.phosphorBright)
                }
                moreLine(shown: parsed.unchecked.count,
                         total: WorkReport.total(parsed, .unchecked))
                if !parsed.whyNotAutomated.isEmpty {
                    line(parsed.whyNotAutomated, color: Theme.dim)
                }
            }
        case .card:
            line(parsed.card, color: Theme.dim)
        }
    }

    private func bullets(_ items: [String], total: Int) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(Array(items.enumerated()), id: \.offset) { item in
                line("- " + item.element, color: Theme.dim)
            }
            moreLine(shown: items.count, total: total)
        }
    }

    /// "+N more" under a list the daemon clamped; nothing when all is drawn.
    @ViewBuilder
    private func moreLine(shown: Int, total: Int) -> some View {
        let more = WorkReport.more(shown: shown, total: total)
        if !more.isEmpty {
            line(more, color: Theme.faint)
        }
    }

    private func line(_ text: String, color: Color) -> some View {
        Text(Markdown.inline(text))
            .font(Theme.mono(11))
            .foregroundStyle(color)
            .textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
    }
}
