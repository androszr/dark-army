import Foundation

// What a write comes back as, and the usage report. Split out of
// `Models.swift` on 20 Sep 2026.

struct ActionResult {
    let ok: Bool
    let detail: String

    init(ok: Bool, detail: String, current: [String: Any]? = nil) {
        self.ok = ok
        self.detail = detail
        self.current = current
    }

    /// Whether this refusal is the daemon's **plan gate** — the one refusal
    /// whose own words offer a way through ("…or confirm starting without
    /// one"), so the caller answers it by raising the "Start unplanned?"
    /// confirmation rather than by drawing it. Matched on the refusal's
    /// opening words against `daemon.PLAN_GATE_REFUSAL`; a pytest pins the
    /// two strings to each other (`test_board_refine.py`), because the panel
    /// deciding this from `planPath` alone is exactly the bug this closes —
    /// a card whose plan *file* is gone gates on the daemon's `isfile`, which
    /// no local field can see, and without this route every control on screen
    /// sends no flag and the card is permanently unstartable. Degraded mode
    /// on a wording drift is the refusal drawn as plain words — visible, and
    /// caught by the pytest before it ships.
    var isPlanGateRefusal: Bool {
        detail.hasPrefix("this card has no plan yet")
    }

    /// The gate's **second** rung: a card whose approved plan has since been
    /// edited. Matched the same way, against `daemon.PLAN_CHANGED_REFUSAL`,
    /// and deliberately a second sentence rather than a reuse of the first —
    /// the confirmation it raises has to say "start with a changed plan",
    /// because a card that has a plan somebody merely has not re-read is not
    /// an unplanned card and a button must not lie about what it agrees to.
    /// The same pytest pins both strings, and that neither is a prefix of the
    /// other: two matchers firing on one refusal would silently turn this
    /// confirmation back into the unplanned one.
    var isPlanChangedRefusal: Bool {
        detail.hasPrefix("this card's plan has changed")
    }

    /// Either rung — a refusal the plan gate's confirmation can answer.
    var isPlanConfirmable: Bool { isPlanGateRefusal || isPlanChangedRefusal }

    /// The card guard's refusal: this Save was judged against a change
    /// number the store has already moved past. Matched on its opening words
    /// against `board.CARD_CHANGED_REFUSAL`, exactly as the two above are,
    /// and pinned to the daemon's constant by a pytest.
    ///
    /// **The Mac gets the guard for the same reason the phone does.** A Mac
    /// that silently overwrote a phone's edit would make the phone's guard
    /// theatre.
    var isCardChangedRefusal: Bool {
        detail.hasPrefix("this card changed on the Mac")
    }

    /// What the store holds, reported inside that refusal alone. Absent
    /// everywhere else, and `nil` rather than a blank card when it is.
    var currentRevision: Int? { current?["revision"] as? Int }
    var current: [String: Any]?

    /// What a refusal reads as when the daemon sent no words with it.
    ///
    /// Every refusal path funnels through here so the wording cannot drift
    /// between them, and the sentence carries **no status number**: an HTTP
    /// code on a card's orange line tells the person nothing they can act on,
    /// and the number is still recorded where it is useful — `Trace.log`, at
    /// the call site. A non-empty detail is passed through untouched, so the
    /// daemon's own words (the plan gate's included) always win.
    static let unexplainedRefusal = "Dark Army refused this and did not say why."

    static func refusalText(detail: String) -> String {
        detail.isEmpty ? unexplainedRefusal : detail
    }
}

/// The outcome of a Prepare press. Every field is empty on a refusal — the
/// composer must not fill anything from a failed call.
///
/// `title` and `summary` are written only by the one-box idea mode, and an
/// **older daemon answers without them**, which reads here as empty. Empty is
/// therefore not "blank this field": both composers apply a returned value
/// only when it is non-empty, which is what makes talking to an older Mac a
/// degrade rather than a data loss.
struct PrepareResult {
    let ok: Bool
    let detail: String
    let prompt: String
    let workflow: String
    var title: String = ""
    var summary: String = ""
    /// Which open project Dark Army thinks the card belongs to, as a *root*, or `""`
    /// for no opinion. Same rule as `title`/`summary`: an older daemon sends
    /// no key, which arrives here as empty, and empty is an absent opinion —
    /// never an instruction to blank the folder somebody already picked.
    var suggestedRoot: String = ""
    var suggestedArea: String = ""
    /// The drafted objective — who benefits, the intended benefit, the
    /// success criterion — idea mode only. Same rule again, one step
    /// stricter: the composer applies each only when it is non-empty **and**
    /// the box is empty, because these are the person's own words whenever
    /// they were typed.
    var beneficiary: String = ""
    var intendedBenefit: String = ""
    var successCriterion: String = ""
}

// MARK: - Usage

/// One rate-limit window, exactly as `/api/usage` reports it.
///
/// `stale` means the window has already reset, so the percentage we hold did not
/// merely age — it restarted at zero and ours is wrong. The view shows a dash and
/// no meter rather than a figure it cannot stand behind, and rather than
/// vanishing, which is indistinguishable from a broken feature.
struct UsageBar: Decodable, Identifiable {
    /// The title is part of the identity because `kind` is not unique. Every
    /// per-model weekly window is `weekly_scoped` — only the title varies — so
    /// two of them collide on a two-part id, and a `ForEach(id: \.id)` over a
    /// colliding pair is undefined. Only one scoped window has ever arrived at
    /// once, which is why this never bit; the live fetch can return more.
    /// One rule for every kind: a split id rule is how the collision returns.
    var id: String { "\(provider):\(kind):\(title)" }
    var kind = ""
    var provider = "claude"
    var shortLabel = ""
    var title = ""
    var percent: Double?
    var resetsAt: Double?
    var stale = false
    /// When this figure was read, epoch seconds. Absent on an older daemon,
    /// which decodes as nil and draws no age note — never as "read just now".
    var asOf: Double?
    /// Where this figure came from: `"statusline"` (Claude Code's own per-turn
    /// report), `"cache"`, or `"oauth"` (the live scoped fetch). Empty where the
    /// daemon names none — an older build, and the Codex and Grok bars it
    /// assembles itself. `ageNote` reads it: a statusline figure is old because
    /// nobody took a turn, which is not a refresh that failed.
    var source = ""

    enum CodingKeys: String, CodingKey {
        case kind, provider, title, percent, stale, source
        case shortLabel = "label"
        case resetsAt = "resets_at"
        case asOf = "as_of"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.value(.kind, "")
        provider = c.value(.provider, "claude")
        shortLabel = c.value(.shortLabel, "")
        title = c.value(.title, "")
        percent = c.maybe(.percent)
        resetsAt = c.maybe(.resetsAt)
        stale = c.value(.stale, false)
        asOf = c.maybe(.asOf)
        source = c.value(.source, "")
    }
}

struct UsageReport: Decodable {
    var bars: [UsageBar] = []

    enum Outer: String, CodingKey { case limits }
    enum Inner: String, CodingKey { case bars }

    init(from decoder: Decoder) throws {
        let outer = try decoder.container(keyedBy: Outer.self)
        let limits = try? outer.nestedContainer(keyedBy: Inner.self, forKey: .limits)
        bars = ((try? limits?.decodeIfPresent([UsageBar].self, forKey: .bars)) ?? nil) ?? []
    }
}
