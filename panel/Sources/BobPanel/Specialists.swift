import SwiftUI

/// One delivery lead and named stages, preserving recorded legacy faces.
///
/// The Mac's copy; the phone's is `ios/BobPhone/Specialists.swift`. Everything
/// from the `enum Specialists {` line down is byte-identical between the two,
/// pinned by `host/tests/test_phone_theme_drift.py` — mirror every edit onto
/// the other side in the same commit, and never re-baseline one of them.
///
/// Only `import SwiftUI` above the marker. `AppKit`, `UIKit` and `help(_:)`
/// are all one-platform things that compile here and break the phone, where
/// the phone build checks its own platform.
enum Specialists {

    struct Known { let role: String }

    static let table: [String: Known] = [
        "bc-card-preparer": Known(role: "drafts the card from your words"),
        "bc-planner": Known(role: "writes the plan"),
        "bc-implementer": Known(role: "writes the code"),
        "bc-verifier": Known(role: "checks it against the plan"),
        "bc-bug-auditor": Known(role: "hunts bugs afterwards"),
        "bc-integration-reviewer": Known(role: "checks it survives the real install"),
        "bc-security-reviewer": Known(role: "checks the doors and the keys"),
    ]

    static func short(_ stage: String) -> String {
        let labels = ["bc-implementer": "impl", "bc-verifier": "verify",
                      "bc-bug-auditor": "audit", "bc-integration-reviewer": "integ",
                      "bc-security-reviewer": "sec", "bc-planner": "plan",
                      "bc-card-preparer": "prep"]
        return labels[stage.lowercased()] ?? stage
    }

    /// The declared specialists in a `workflow` field.
    ///
    /// The body of `BoardCard.stages`, character for character. The shared
    /// file cannot call the panel-only model, so the panel's own test asserts
    /// the two agree on ragged input; if `BoardCard.stages` ever changes, that
    /// test is what says so.
    static func parse(_ raw: String) -> [String] {
        raw.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    /// What this specialist does, in a handful of words — `nil` for a name Dark Army
    /// has never heard of.
    ///
    /// Never a fallback string. An invented description of an unknown
    /// specialist would read exactly like a known one, which is worse than
    /// saying nothing.
    static func role(for stage: String) -> String? {
        table[stage.lowercased()]?.role
    }

    /// Known stages use markers; unknown names retain their stable fallback.
    ///
    /// Unknown names use `Cast.character(for:)`'s hand-written djb2,
    /// byte for byte — `&*` and `&+` are load-bearing, and `abs(hash)` is kept
    /// as it stands rather than "improved" on one copy, which would be exactly
    /// the drift the byte pin exists to catch.
    ///
    /// Lowercasing the input before hashing is the one deliberate difference
    /// from `character(for:)`: a stage name is typed by a person, a session id
    /// is not.
    static func face(for stage: String) -> String {
        let key = stage.lowercased()
        if table[key] != nil { return "" }
        var hash = 5381
        for byte in key.utf8 { hash = (hash &* 33) &+ Int(byte) }
        return Cast.names[abs(hash) % Cast.names.count].lowercased()
    }

}

/// The crew a card has been through, drawn as faces.
///
/// A session hands parts of its work to subagents — `bc-planner` writes the
/// plan, `bc-implementer` writes the code, `bc-verifier` checks it against the
/// plan. Dark Army has always known them by name (`SubagentStart` carries the type,
/// and `_subagent_rows` names every live one), and now also knows *who* took
/// each part: the daemon allocates a cast member when a stage is first
/// observed and remembers it on the card forever.
///
/// **The one rule, and everything here serves it: a hollow marker may only come
/// from `workflow`.** That field is *declared* — typed by whoever wrote the
/// card, or stated by the assistant that filed it — and is never inferred from
/// what has already run. Dark Army cannot predict which specialists a session will
/// use, has no say in it, and must not imply otherwise: a confident marker for
/// a stage that never happens is the same failure as the unconditional
/// `Accept` that used to sit on the reply bar, promising a decision nobody had
/// offered. The same argument is why a **pending** tile draws the role's
/// *anchor* face and names no character: nobody has been allocated yet.
///
/// Three states follow from three sources:
///
/// - **done** — in the trail, not currently running.
/// - **active** — in the trail *and* in the session's live subagent rows.
/// - **pending** — in `workflow`, not yet in the trail. The only hollow one.
///
/// An observed stage that no `workflow` mentioned is appended after the
/// declared ones rather than dropped: what actually ran outranks the forecast,
/// and a track that hides a real stage is worse than one that is longer than
/// expected.
///
/// Draws **nothing at all** when there is nothing to say. A card that has never
/// been started has no band, no placeholder and no empty row — the absence is
/// the honest reading, and a row of five grey faces on every backlog card would
/// be pure noise on the column a person scans most.
struct CrewBand: View {
    /// The stages the card declares it expects.
    let workflow: [String]
    /// The stages Dark Army actually saw run, in the order it saw them.
    let trail: [String]
    /// The subagent names the fleet says are running under this card's session
    /// right now. Empty for a card whose session has ended, which is correct:
    /// nothing is active, and every stage it did reach stays `done`.
    let live: Set<String>
    /// `stage -> character`, published by the daemon off the card's own
    /// `crew_trail`. Empty for a card recorded before this existed, and for
    /// every card an older daemon serves — both fall back to anchors.
    let crew: [String: String]
    var lead: String = ""
    /// The lead the daemon promises Start will give (`lead_face`); empty
    /// from an older daemon, which falls back to the area's usual lead.
    var promised: String = ""
    var leadFace: String { promised.isEmpty ? Areas.anchor(lead) : promised }
    var style: Style = .strip

    enum Style { case strip, grid }

    enum Phase { case done, active, pending }

    struct Stage: Identifiable {
        let id: String
        let name: String
        let phase: Phase
        /// The character on this stage, or `""` where none was allocated —
        /// which is every pending stage, and every card older than the trail.
        let character: String
    }

    /// The face size in the strip, and the larger one the running stage gets.
    static let faceSize: CGFloat = 18
    static let runningFaceSize: CGFloat = 22
    static let spacing: CGFloat = 4
    /// How many **non-running** faces the strip draws before it starts
    /// counting instead. A constant, not a measurement: `PanelMetrics`' rule
    /// is that no view reads back the size of the window it is drawn in, and
    /// the failure mode of a wrong constant here is a clipped `+n` chip,
    /// never a resized window. One number for both platforms on purpose — this file is
    /// byte-identical, so a per-platform figure would have to be a parameter,
    /// and a parameter is a place for the two sides to disagree.
    static let stripCap = 4

    /// The declared stages first, in the order they were declared, then
    /// anything observed that was never declared.
    var stages: [Stage] {
        var out: [Stage] = []
        var placed = Set<String>()
        for name in workflow {
            guard !placed.contains(name) else { continue }
            placed.insert(name)
            out.append(stage(name))
        }
        for name in trail where !placed.contains(name) {
            placed.insert(name)
            out.append(stage(name))
        }
        return out
    }

    private func stage(_ name: String) -> Stage {
        Stage(id: name, name: name, phase: phase(for: name),
              character: crew[name] ?? "")
    }

    private func phase(for name: String) -> Phase {
        guard trail.contains(name) else { return .pending }
        return live.contains(name) ? .active : .done
    }

    /// The face a stage wears: its allocated character, else the role's own
    /// anchor, else the stage-name hash `Specialists.face(for:)` has always
    /// used for a role Dark Army has never heard of.
    static func face(_ stage: Stage) -> String {
        if !stage.character.isEmpty { return stage.character }
        return Specialists.face(for: stage.name)
    }

    /// What the band says in words, for the tooltip and for anyone reading it
    /// with VoiceOver — the faces are small and colour is doing real work in
    /// them, so the same fact has to exist as text. **It names every stage,
    /// including the ones the strip dropped**, so nothing is lost to the cap.
    static func caption(workflow: [String], trail: [String],
                        live: Set<String>, crew: [String: String], lead: String = "",
                        promised: String = "") -> String {
        let leadSlug = promised.isEmpty ? Areas.anchor(lead) : promised
        let leadLine = leadSlug.isEmpty ? "" : "\(display(leadSlug)), \(Areas.name(lead)) lead"
        let prefix = leadLine.isEmpty ? "" : leadLine + "; "
        let band = CrewBand(workflow: workflow, trail: trail, live: live, crew: crew)
        let items = band.stages
        if items.isEmpty { return leadLine }
        let done = items.filter { $0.phase == .done }
        // The **same** arithmetic the drawn counter uses, never a second one:
        // a band that showed "step 2 of 3" inside a container VoiceOver read
        // as "1 done of 3" was stating two different numbers about one state,
        // and the person who cannot see the faces got the weaker of the two.
        let count = counterText(items)
        if let running = items.first(where: { $0.phase == .active }) {
            let who = running.character.isEmpty
                ? running.name
                : "\(display(running.character)) running \(running.name)"
            return prefix + "\(who) · \(count)"
        }
        let names = done.map { face($0).isEmpty ? $0.name : display(face($0)) }.joined(separator: ", ")
        if names.isEmpty { return prefix + "\(count) done" }
        return prefix + "\(count) done: \(names)"
    }

    /// How far along, in three or four characters — the one honest gap the
    /// band had. The arithmetic was already here, but only as `caption`'s
    /// accessibility text, so on screen four-done-of-six and one-done-of-six
    /// looked the same.
    ///
    /// `""` for a card with no stages, so a band that draws nothing still
    /// draws nothing. `"step N of M"` while a stage is running — the number is
    /// its **position**, which is what "which step is this?" means — and
    /// `"N of M"` when nothing is, where `N` is how many are done. `caption`
    /// reads this same function, so the drawn count and the spoken one are one
    /// arithmetic and cannot state different numbers about the same band.
    static func counter(workflow: [String], trail: [String],
                        live: Set<String>, crew: [String: String]) -> String {
        counterText(CrewBand(workflow: workflow, trail: trail,
                             live: live, crew: crew).stages)
    }

    /// The count over stages already computed — the one place the arithmetic
    /// lives. Split out from `counter` so `caption` can reach it without
    /// rebuilding the band, and so neither can drift from the other.
    static func counterText(_ items: [Stage]) -> String {
        if items.isEmpty { return "" }
        if let at = items.firstIndex(where: { $0.phase == .active }) {
            return "step \(at + 1) of \(items.count)"
        }
        return "\(items.filter { $0.phase == .done }.count) of \(items.count)"
    }

    /// A slug as a person reads it. The cast's own spelling where there is
    /// one, so `overwatch` reads as a name in a sentence.
    static func display(_ slug: String) -> String {
        if let match = Cast.names.first(where: { $0.lowercased() == slug.lowercased() }) {
            return match
        }
        return slug == "overwatch" ? "Overwatch" : slug
    }

    /// The words under a face: who is on it and what they are doing.
    static func line(_ stage: Stage) -> String {
        switch stage.phase {
        case .done:
            return stage.character.isEmpty
                ? "done" : "\(display(stage.character)) · done"
        case .active:
            return stage.character.isEmpty
                ? "running" : "\(display(stage.character)) · running"
        case .pending:
            return "expected"
        }
    }

    @Environment(\.dynamicTypeSize) var dynamicTypeSize

    var body: some View {
        let items = stages
        if !items.isEmpty || !leadFace.isEmpty {
            // At an accessibility text size the strip *becomes* the grid, one
            // column and uncapped: `Theme.mono` scales with no ceiling, and a
            // cap at that size is the clipping the phone contract forbids.
            if style == .grid || dynamicTypeSize.isAccessibilitySize {
                grid(items)
            } else {
                strip(items)
            }
        }
    }

    /// The stages the strip actually draws, in declared order.
    ///
    /// `stripCap` counts the **non-running** faces. The drop order is: done
    /// stages, oldest first — the recent history is the useful half — then
    /// pending stages, last-declared first. **The running stage is never
    /// dropped**: it is the fact the band is consulted for, and the only one
    /// carrying a name. `caption` names every stage regardless, so nothing is
    /// lost to somebody reading with VoiceOver.
    var drawnStrip: [Stage] {
        let items = stages
        var rest = items.filter { $0.phase != .active }
        while rest.count > Self.stripCap {
            if let oldestDone = rest.firstIndex(where: { $0.phase == .done }) {
                rest.remove(at: oldestDone)
            } else if let lastPending = rest.lastIndex(where: { $0.phase == .pending }) {
                rest.remove(at: lastPending)
            } else {
                break
            }
        }
        let kept = Set(rest.map(\.id))
        return items.filter { $0.phase == .active || kept.contains($0.id) }
    }

    /// How many stages the strip is not drawing.
    var droppedCount: Int { stages.count - drawnStrip.count }

    @ViewBuilder
    private func strip(_ items: [Stage]) -> some View {
        HStack(spacing: Self.spacing) {
            leadMark
            ForEach(drawnStrip) { stage in
                face(stage, size: stage.phase == .active
                        ? Self.runningFaceSize : Self.faceSize)
                if stage.phase == .active, !stage.character.isEmpty {
                    // The name of who is working now, which is the fact the
                    // band is actually consulted for. Only ever one line, and
                    // only when something is running — a finished card's band
                    // speaks for itself. Never truncated: a cast name is at
                    // most nine characters from a closed vocabulary.
                    Text(Self.display(stage.character))
                        .font(Theme.mono(9))
                        .foregroundStyle(Theme.phosphor)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if droppedCount > 0 {
                Text("+\(droppedCount)")
                    .font(Theme.mono(9))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !counter.isEmpty {
                Text(counter)
                    .font(Theme.mono(9))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Self.caption(workflow: workflow, trail: trail,
                                         live: live, crew: crew, lead: lead,
                                         promised: promised))
    }

    /// This band's own step count, from the fields it was already handed.
    private var counter: String {
        Self.counter(workflow: workflow, trail: trail, live: live, crew: crew)
    }

    @ViewBuilder
    private func grid(_ items: [Stage]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            leadMark
            if !counter.isEmpty {
                Text(counter)
                    .font(Theme.mono(9))
                    .foregroundStyle(Theme.dim)
                    .fixedSize(horizontal: false, vertical: true)
            }
            gridTiles(items)
        }
    }

    @ViewBuilder
    private func gridTiles(_ items: [Stage]) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 132), spacing: 8)],
                  alignment: .leading, spacing: 8) {
            ForEach(items) { stage in
                HStack(alignment: .top, spacing: 6) {
                    face(stage, size: 22)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(stage.name)
                            .font(Theme.mono(11))
                            .foregroundStyle(stage.phase == .pending
                                             ? Theme.faint : Theme.phosphor)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(Self.line(stage))
                            .font(Theme.mono(9))
                            .foregroundStyle(Theme.faint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(stage.name), \(Self.line(stage))")
            }
        }
    }

    /// A face at one of three brightnesses. Form before colour: a pending face
    /// also carries a hairline, so the three states are told apart without it.
    @ViewBuilder
    private func face(_ stage: Stage, size: CGFloat) -> some View {
        if Self.face(stage).isEmpty {
            StageMark(name: stage.name, active: stage.phase == .active)
                .opacity(opacity(stage.phase))
        } else {
            PixelMark(character: Self.face(stage),
                      state: stage.phase == .active ? .work : .sleep, size: size)
                .opacity(opacity(stage.phase))
                .overlay(Rectangle().strokeBorder(Theme.faint,
                          lineWidth: stage.phase == .pending ? 1 : 0))
        }
    }

    @ViewBuilder private var leadMark: some View {
        if !leadFace.isEmpty {
            PixelMark(character: leadFace, state: live.isEmpty ? .sleep : .work, size: 22)
                .accessibilityLabel("\(Self.display(leadFace)), \(Areas.name(lead)) lead")
        }
    }

    private func opacity(_ phase: Phase) -> Double {
        switch phase {
        case .done: return 0.55
        case .active: return 1.0
        case .pending: return 0.35
        }
    }
}

/// A stage is a named task; only a recorded character draws a portrait.
struct StageMark: View {
    let name: String
    var active = false
    var body: some View {
        Text(Specialists.short(name))
            .font(Theme.mono(8))
            .foregroundStyle(active ? Theme.phosphor : Theme.faint)
            .fixedSize(horizontal: false, vertical: true)
            .padding(3)
            .overlay(Rectangle().strokeBorder(Theme.faint, lineWidth: 1))
            .accessibilityLabel(name)
    }
}
