import SwiftUI

/// Navigation inside the existing details; no client or refresh schedule of its own.
struct CollaborationView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @ObservedObject var client: PhoneClient
    let focus: CollaborationFocus
    var helperFocus = ""
    var initiallyExpanded = false
    var onSession: (String, String, String) -> Void = { _, _, _ in }
    var onCard: (BoardCard) -> Void = { _ in }
    @State private var expanded = false
    @State private var showAll = false
    @State private var routeNote = ""

    private var evidence: Collaboration { client.snapshot.collaboration ?? Collaboration() }
    private var rows: [CollaborationEdge] { focus.edges(in: evidence) }
    private var isolatedNodes: [CollaborationNode] {
        let roots = focus.roots(in: evidence)
        let connected = Set(rows.flatMap { [$0.source, $0.target] })
        return evidence.nodes.filter { roots.contains($0.id) && !connected.contains($0.id) }
    }
    private var agents: [Agent] {
        let a = client.snapshot.agents
        return a.running + a.waiting + a.sleeping + a.finished + a.abandoned
    }
    var body: some View {
        DisclosureGroup("Collaboration", isExpanded: $expanded) {
            VStack(alignment: .leading) {
                Group {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(evidence.coverage).foregroundStyle(.secondary)
                        Text("Counts describe observed attempts, without delivery or outcome evidence. State labels describe when this picture was observed.")
                        if rows.isEmpty && evidence.supported {
                            Text("No relationships observed in the retained coverage.")
                        }
                        ForEach(showAll ? isolatedNodes : Array(isolatedNodes.prefix(max(0, 5 - rows.count)))) { node in
                            endpoint(node.id).id(node.id)
                        }
                        ForEach(showAll ? rows : Array(rows.prefix(5))) { edge in
                            VStack(alignment: .leading, spacing: 6) {
                                Label(edge.observation, systemImage: edge.kind == "parent" ? "arrow.turn.down.right" : "arrow.right")
                                endpoint(edge.source)
                                endpoint(edge.target)
                                if edge.kind == "message" {
                                    Text(verbatim: "Address: " + edge.address).textSelection(.enabled)
                                    Text(edge.lastObserved).foregroundStyle(.secondary)
                                }
                            }
                            .padding(8)
                            .overlay(RoundedRectangle(cornerRadius: 4).stroke(Theme.hair))
                            .id(edge.id)
                        }
                        if !showAll && rows.count + isolatedNodes.count > 5 {
                            DecryptButton(action: { showAll = true }) { Text("Show \(rows.count + isolatedNodes.count - 5) more evidence rows") }
                        }
                        if !routeNote.isEmpty { Text(routeNote).foregroundStyle(.secondary) }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }


            }
        }
        .font(Theme.mono(12))
        .padding(12)
        .onAppear { if initiallyExpanded { expanded = true } }
        .onChange(of: focus) { _, _ in showAll = false; routeNote = "" }
    }

    @ViewBuilder private func endpoint(_ id: String) -> some View {
        if let node = evidence.nodes.first(where: { $0.id == id }) {
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: "\(node.kind): \(node.label) · \(node.stateLabel)")
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                if node.kind == "helper" {
                    Text(verbatim: "Owner: " + node.ownerSessionId)
                }
                if !node.reason.isEmpty { Text(verbatim: "Evidence note: " + node.reason) }
                switch agentRoute(node) {
                case .session:
                    DecryptButton(action: { navigate(node) }) { Text(node.kind == "helper" ? "Open owning agent" : "Open agent") }
                        .accessibilityLabel("Open \(node.label), \(node.stateLabel)")
                case .unavailable(let reason): Text(reason).foregroundStyle(.secondary)
                default: EmptyView()
                }
                if node.cards.count > 1 {
                    Text("Linked to several cards; communication totals belong to the session.")
                }
                ForEach(node.cards, id: \.self) { link in
                    switch cardRoute(link) {
                    case .card:
                        DecryptButton(action: { navigate(link) }) { Text("Open card: " + link.title) }
                        Text(verbatim: link.kind).foregroundStyle(.secondary)
                    case .unavailable(let reason): Text(reason).foregroundStyle(.secondary)
                    default: EmptyView()
                    }
                }
            }
        }
    }

    private func agentRoute(_ node: CollaborationNode) -> CollaborationRoute {
        CollaborationRoute.agent(node, current: agents.map { ($0.provider, $0.sessionId) })
    }
    private func cardRoute(_ link: CollaborationCard) -> CollaborationRoute {
        CollaborationRoute.card(link, current: client.snapshot.board.cards.map { ($0.id, $0.root) })
    }
    private func navigate(_ node: CollaborationNode) {
        routeNote = CollaborationControls.openAgent(node.id, client: client, sheets: sheets) ?? ""
    }
    private func navigate(_ link: CollaborationCard) {
        routeNote = CollaborationControls.openCard(link, client: client, sheets: sheets) ?? ""
    }
}

/// The map controls' production press handlers share the existing phone detail stack.
@MainActor
enum CollaborationControls {
    static func openAgent(_ nodeID: String, client: PhoneClient, sheets: PhoneSheetRouter) -> String? {
        guard let node = client.snapshot.collaboration?.nodes.first(where: { $0.id == nodeID }) else {
            return "Agent unavailable in this view"
        }
        let fleet = client.snapshot.agents
        let groups: [(Category, [Agent])] = [(.running, fleet.running), (.waiting, fleet.waiting),
            (.sleeping, fleet.sleeping), (.finished, fleet.finished), (.abandoned, fleet.abandoned)]
        let agents = groups.flatMap { $0.1 }
        switch CollaborationRoute.agent(node, current: agents.map { ($0.provider, $0.sessionId) }) {
        case .session(let provider, let id, _):
            let targets = groups.flatMap { category, agents in
                agents.filter { $0.provider == provider && $0.sessionId == id }.map { ($0, category) }
            }
            guard targets.count == 1 else { return "Agent unavailable in this view" }
            sheets.show(.agent(targets[0].0, targets[0].1)); return nil
        case .unavailable(let reason): return reason
        default: return "Agent unavailable in this view"
        }
    }

    static func openCard(_ link: CollaborationCard, client: PhoneClient, sheets: PhoneSheetRouter) -> String? {
        let cards = client.snapshot.board.cards
        switch CollaborationRoute.card(link, current: cards.map { ($0.id, $0.root) }) {
        case .card(let id, let root):
            guard let card = cards.first(where: { $0.id == id && $0.root == root }) else {
                return "Card unavailable in this view"
            }
            sheets.show(.card(card)); return nil
        case .unavailable(let reason): return reason
        default: return "Card unavailable in this view"
        }
    }
}
