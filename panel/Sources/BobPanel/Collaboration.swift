import Foundation

/// Byte-pinned with BobPhone. Evidence never grants an action capability.
struct Collaboration: Decodable {
    var version = 0
    var available = false
    var partial = true
    var reasons: [String] = []
    var nodes: [CollaborationNode] = []
    var edges: [CollaborationEdge] = []
    var omittedNodes = 0
    var omittedEdges = 0
    var supported: Bool { version == 1 && available }
    var coverage: String {
        guard supported else { return "Collaboration evidence unavailable from this picture." }
        if partial {
            return "Partial observed history. " + reasons.map {
                switch $0 {
                case "recipient_cap": return "Some recipient addresses exceeded the source limit."
                case "message_evidence_unavailable": return "Some providers supply no message evidence."
                case "projection_cap": return "\(omittedNodes) nodes and \(omittedEdges) connections omitted."
                case "ancestry_missing", "ancestry_cycle", "ancestry_unavailable":
                    return "Some parent relationships are unknown."
                default: return "Some identity evidence is incomplete."
                }
            }.joined(separator: " ")
        }
        return "Observed evidence within recent retention; no complete lifetime history."
    }
    enum CodingKeys: String, CodingKey {
        case version, available, partial, reasons, nodes, edges
        case omittedNodes = "omitted_nodes", omittedEdges = "omitted_edges"
    }
    init() {}
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = c.value(.version, 0)
        available = c.value(.available, false)
        partial = c.value(.partial, true)
        reasons = c.value(.reasons, [])
        let decodedNodes: [CollaborationLossy<CollaborationNode>]? = c.maybe(.nodes)
        let decodedEdges: [CollaborationLossy<CollaborationEdge>]? = c.maybe(.edges)
        let rawNodes = decodedNodes ?? []
        let rawEdges = decodedEdges ?? []
        if decodedNodes == nil || decodedEdges == nil {
            partial = true; reasons.append("invalid_source")
        }
        nodes = rawNodes.compactMap(\.value).filter { !$0.id.isEmpty }
        edges = rawEdges.compactMap(\.value).filter { !$0.id.isEmpty }
        if nodes.count != rawNodes.count || edges.count != rawEdges.count {
            partial = true; reasons.append("invalid_source")
        }
        omittedNodes = c.value(.omittedNodes, 0)
        omittedEdges = c.value(.omittedEdges, 0)
        // Duplicated wire IDs never become Dictionary(uniqueKeysWithValues:) traps.
        let duplicates = Dictionary(grouping: nodes, by: \.id).filter { $0.value.count > 1 }.keys
        if !duplicates.isEmpty {
            nodes.removeAll { duplicates.contains($0.id) }
            partial = true
        }
        if !supported { nodes = []; edges = [] }
    }
}

struct CollaborationCard: Decodable, Identifiable, Hashable {
    var id = "", root = "", title = "", kind = ""
    enum CodingKeys: String, CodingKey { case id, root, title, kind }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, ""); root = c.value(.root, "")
        title = c.value(.title, ""); kind = c.value(.kind, "")
    }
}

struct CollaborationNode: Decodable, Identifiable {
    var id = "", kind = "", provider = "", sessionId = "", ownerSessionId = ""
    var helperId = "", label = "", address = "", resolution = "", presence = ""
    var lifecycle = "", source = "", reason = ""
    var inboxObserved: Bool?
    var cards: [CollaborationCard] = []
    var known: Bool {
        ["session", "helper"].contains(kind) && resolution == "resolved"
            && ["present", "retained"].contains(presence)
            && ["live", "ended", "unknown"].contains(lifecycle)
            && ["claude", "codex", "grok"].contains(provider)
    }
    var stateLabel: String {
        if resolution == "ambiguous" { return "Ambiguous address — several agents match" }
        if resolution == "unresolved" { return "Unknown recipient — no matching identity" }
        guard known else { return "Unknown identity evidence" }
        if lifecycle == "ended" { return "Known ended recipient · retained evidence" }
        if presence == "retained" { return "Retained identity · ending unknown" }
        if kind == "helper" { return "Live helper · no inbox observed" }
        if inboxObserved == false { return "Present session · no inbox observed" }
        if inboxObserved == true { return "Present session · inbox observed" }
        return "Present session · inbox unknown"
    }
    enum CodingKeys: String, CodingKey {
        case id, kind, provider, label, address, resolution, presence, lifecycle, source, reason, cards
        case sessionId = "session_id", ownerSessionId = "owner_session_id"
        case helperId = "helper_id", inboxObserved = "inbox_observed"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, ""); kind = c.value(.kind, ""); provider = c.value(.provider, "")
        sessionId = c.value(.sessionId, ""); ownerSessionId = c.value(.ownerSessionId, "")
        helperId = c.value(.helperId, ""); label = c.value(.label, "")
        address = c.value(.address, ""); resolution = c.value(.resolution, "")
        presence = c.value(.presence, ""); lifecycle = c.value(.lifecycle, "")
        source = c.value(.source, ""); reason = c.value(.reason, "")
        inboxObserved = c.maybe(.inboxObserved); cards = c.value(.cards, [])
    }
}

struct CollaborationEdge: Decodable, Identifiable {
    var id = "", kind = "", source = "", target = "", address = "", last = ""
    var count = 0
    var observation: String {
        kind == "parent" ? "Observed parent relationship"
            : "Observed SendMessage calls: \(count) · session-level total"
    }
    var lastObserved: String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let fractional = parser.date(from: last)
        parser.formatOptions = [.withInternetDateTime]
        guard let date = fractional ?? parser.date(from: last) else { return "Last observed: undated" }
        return "Last observed: " + date.formatted(date: .abbreviated, time: .shortened)
    }
    enum CodingKeys: String, CodingKey { case id, kind, source, target, address, last, count }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, ""); kind = c.value(.kind, "")
        source = c.value(.source, ""); target = c.value(.target, "")
        address = c.value(.address, ""); last = c.value(.last, "")
        count = c.value(.count, 0)
    }
}

enum CollaborationFocus: Equatable {
    case session(provider: String, id: String)
    case card(String)

    func roots(in evidence: Collaboration) -> Set<String> {
        Set(evidence.nodes.filter { node in
            switch self {
            case .session(let provider, let id):
                return node.provider == provider && node.ownerSessionId == id
            case .card(let id):
                return node.cards.contains { $0.id == id }
            }
        }.map(\.id))
    }
    func edges(in evidence: Collaboration) -> [CollaborationEdge] {
        guard evidence.supported else { return [] }
        let roots = roots(in: evidence), ids = Set(evidence.nodes.map(\.id))
        var seen = Set<String>()
        return evidence.edges.filter {
            ["parent", "message"].contains($0.kind) && ids.contains($0.source)
                && ids.contains($0.target) && (roots.contains($0.source) || roots.contains($0.target))
                && seen.insert($0.id).inserted
        }.sorted { ($0.kind == "parent" ? 0 : 1, $0.id) < ($1.kind == "parent" ? 0 : 1, $1.id) }
    }
}

enum CollaborationRoute: Equatable {
    case session(provider: String, id: String, helper: String)
    case card(id: String, root: String)
    case unavailable(String)

    /// The map's complete effect surface: navigation or an explanation only.
    func follow(session: (String, String, String) -> Void,
                card: (String, String) -> Void,
                unavailable: (String) -> Void) {
        switch self {
        case .session(let provider, let id, let helper): session(provider, id, helper)
        case .card(let id, let root): card(id, root)
        case .unavailable(let reason): unavailable(reason)
        }
    }

    static func agent(_ node: CollaborationNode, current: [(provider: String, id: String)]) -> Self {
        guard node.known else { return .unavailable(node.stateLabel) }
        let hits = current.filter { $0.provider == node.provider && $0.id == node.ownerSessionId }
        guard hits.count == 1 else { return .unavailable("Agent unavailable in this view") }
        return .session(provider: node.provider, id: node.ownerSessionId, helper: node.helperId)
    }
    static func card(_ link: CollaborationCard, current: [(id: String, root: String)]) -> Self {
        guard !link.id.isEmpty, !link.root.isEmpty,
              ["implementation", "refinement", "inherited-from-parent"].contains(link.kind),
              current.filter({ $0.id == link.id && $0.root == link.root }).count == 1 else {
            return .unavailable("Card unavailable in this view")
        }
        return .card(id: link.id, root: link.root)
    }
}

/// Consume each ragged row independently so one malformed record cannot erase peers.
private struct CollaborationLossy<T: Decodable>: Decodable {
    let value: T?
    init(from decoder: Decoder) throws { value = try? T(from: decoder) }
}
