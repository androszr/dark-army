import SwiftUI

/// One plan opened on the phone: the title (drawn in the body — a bar title
/// cannot wrap), its project and day, `status · area` where the plan states
/// them, then the text through the same renderer the scout reader uses. The
/// text is fetched when this screen appears and at no other time — never on
/// the poll, the background refresh or the widget. Where the plan belongs to
/// a card on the board the phone already holds, a CARD button opens that
/// card's screen — `PhoneInbox.sheet(for:snapshot:)`'s rule, resolved at the
/// draw, so a card no longer on the board draws no button at all.
struct PlanReaderView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    let row: PlanRow

    @State private var plan: PlanBody?
    @State private var detail = ""

    private var title: String {
        if let plan, !plan.title.isEmpty { return plan.title }
        return row.displayTitle
    }

    private var headerLine: String {
        guard let plan, plan.available else { return row.headerLine }
        var fields = row
        fields.status = plan.status
        fields.area = plan.area
        return fields.headerLine
    }

    /// The card this plan belongs to, only while it is on the board.
    private var card: BoardCard? {
        guard !row.cardId.isEmpty else { return nil }
        return client.snapshot.board.cards.first(where: { $0.id == row.cardId })
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(title)
                    .font(Theme.mono(15, weight: .semibold))
                    .foregroundStyle(Theme.phosphorBright)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
                if !row.metaLine.isEmpty {
                    Text(row.metaLine)
                        .font(Theme.mono(11))
                        .foregroundStyle(Theme.faint)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !headerLine.isEmpty {
                    Text(headerLine)
                        .font(Theme.mono(12))
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let card {
                    DecryptButton("CARD · \(card.title)") {
                        sheets.show(.card(card))
                    }
                        .font(Theme.mono(11, weight: .medium))
                        .foregroundStyle(Theme.phosphor)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(minHeight: 44, alignment: .leading)
                        .contentShape(Rectangle())
                        .buttonStyle(.plain)
                        .accessibilityLabel("Open the card \(card.title)")
                }
                content
                Text(plan.map { $0.path.isEmpty ? row.path : $0.path } ?? row.path)
                    .font(Theme.mono(11))
                    .foregroundStyle(Theme.faint)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Theme.bg)
        .navigationTitle("plan")
        .decryptSurface("PlanReaderView")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(Theme.bar, for: .navigationBar)
        .toolbarBackground(.visible, for: .navigationBar)
        .task { await load() }
    }

    @ViewBuilder
    private var content: some View {
        if let plan {
            if plan.available {
                if !plan.body.isEmpty {
                    MarkdownText(source: plan.body, base: 12, mono: true)
                        .equatable()
                        .foregroundStyle(Theme.dim)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Text(detail.isEmpty ? PlanBody.fetchFailedLine : detail)
                    .font(Theme.mono(12))
                    .foregroundStyle(Theme.amber)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else {
            CommentLine(text: "fetching…")
        }
    }

    private func load() async {
        // A refused or failed read may be retried when the view comes back;
        // only a plan already in hand is kept.
        guard plan?.available != true else { return }
        let requested = row.path
        let fetched = await client.planBody(path: requested)
        guard let outcome = PlansLoad.apply(requested: requested,
                                            current: row.path,
                                            fetched: fetched)
        else { return }
        plan = outcome.value
        detail = outcome.detail
    }
}
