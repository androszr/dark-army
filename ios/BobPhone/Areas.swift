import SwiftUI

/// Delivery roster, mirrored byte for byte on Mac and phone.
enum Areas {
    struct Area {
        let slug: String
        let name: String
        let concept: String
        let pool: [String]
    }
    static let chiefOfStaff = "cipher"
    static let all: [Area] = [
        Area(slug: "backbone", name: "Backbone", concept: "services, data & transport", pool: ["relay", "hex", "forge"]),
        Area(slug: "desk", name: "Desk", concept: "the Mac window & menu bar", pool: ["vex", "zosia"]),
        Area(slug: "pocket", name: "Pocket", concept: "phones & widgets", pool: ["mira", "ptys"]),
        Area(slug: "ledger", name: "Ledger", concept: "numbers that must be right", pool: ["audit", "ledger"]),
        Area(slug: "play", name: "Play", concept: "worlds, art & feel", pool: ["franio", "quiet"]),
        Area(slug: "conductor", name: "Conductor", concept: "agents, prompts & LLM features", pool: ["velvet", "canon"]),
        Area(slug: "gate", name: "Gate", concept: "security, release & operations", pool: ["nyx", "watch", "captcha", "sawa"]),
        Area(slug: "universal", name: "Universal", concept: "the fixer", pool: ["proxy", "androll"]),
    ]
    static func name(_ slug: String) -> String {
        all.first { $0.slug == slug }?.name ?? ""
    }
    static func anchor(_ slug: String) -> String {
        all.first { $0.slug == slug }?.pool.first ?? ""
    }
    static func anchorName(_ slug: String) -> String {
        Cast.names.first { $0.lowercased() == anchor(slug) } ?? ""
    }
}

enum AreaSuggestion {
    static func decide(offer: String, current: String) -> String? {
        guard current.isEmpty, Areas.all.contains(where: { $0.slug == offer }) else { return nil }
        return offer
    }
}

enum DependencySuggestion {
    /// Whether Prepare's folder suggestion may move the project: not while
    /// the waits-on box holds a choice, because a move clears the box (its
    /// cards were the old project's) and Prepare must leave a filled box
    /// exactly as it was. Judged on the box as it stood before Prepare's own
    /// fill, which a move may still clear.
    static func allowsProjectMove(boxBeforePrepare: String) -> Bool {
        boxBeforePrepare.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    /// The waits-on box Prepare may fill: nil unless `current` is empty, then
    /// the offered ids the client can still see (`listed`: this project's
    /// cards not in Done), in offer order, joined by newlines — the store's
    /// own shape. A card done or deleted since the press never lands.
    static func decide(offer: [String], current: String, listed: Set<String>) -> String? {
        guard current.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        let kept = offer.filter { listed.contains($0) }
        return kept.isEmpty ? nil : kept.joined(separator: "\n")
    }
}

struct AreaGrid: View {
    @Binding var selected: String

    static func label(_ area: Areas.Area) -> String {
        "\(area.name), \(area.concept), usually \(Areas.anchorName(area.slug))"
    }
    static func selection(_ slug: String, current: String) -> String {
        current == slug ? "" : slug
    }

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 8)],
                  alignment: .leading, spacing: 8) {
            ForEach(Areas.all, id: \.slug) { area in
                let usually = Areas.anchorName(area.slug)
                AreaChoiceButton {
                    selected = Self.selection(area.slug, current: selected)
                } label: {
                    HStack(alignment: .top, spacing: 6) {
                        PixelMark(character: Areas.anchor(area.slug), state: .sleep, size: 22)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(area.name).font(Theme.mono(11)).foregroundStyle(Theme.phosphor)
                            Text("usually \(usually)").font(Theme.mono(9)).foregroundStyle(Theme.faint)
                        }
                        Spacer(minLength: 0)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(8)
                    .overlay(Rectangle().stroke(selected == area.slug ? Theme.phosphor : Theme.faint.opacity(0.35), lineWidth: selected == area.slug ? 2 : 1))
                }
                .accessibilityLabel(Self.label(area))
                .accessibilityAddTraits(selected == area.slug ? .isSelected : [])
            }
        }
    }
}
