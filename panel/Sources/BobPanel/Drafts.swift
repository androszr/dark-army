import Foundation

/// One half-typed card, kept so closing the composer does not throw the work
/// away.
///
/// The `id` **is the composer's `stagingId`** — one identity per composer
/// session, so autosave overwrites one row instead of breeding a row per tick,
/// and the staged file copies (which live in `attachments/<stagingId>/`) are
/// addressable from the row. That is also why deleting a draft can delete its
/// folder: the row names it.
struct CardDraft: Identifiable, Equatable {
    let id: String
    var project: String
    var root: String
    var title: String
    var summary: String
    var prompt: String
    var workflow: String
    /// The composer's one-box idea. Composer scratch that never reaches a
    /// card, kept here for the same reason as the other four: closing the
    /// form must not throw away what was typed into it.
    var idea: String
    var tool: String
    /// The card's kind. `""` is a build card; `"scout"` is an investigation.
    /// Banked with the draft; decoded tolerant, default `""`.
    var kind: String
    var model: String
    var attachments: [String]
    var updatedAt: Double
    /// The objective typed before the card exists — the same three lines
    /// `OutcomeEditor` edits on a saved card, banked here for the same
    /// reason as `idea`: closing the form must not throw them away.
    var beneficiary: String
    var intendedBenefit: String
    var successCriterion: String
    /// The importance number typed before the card exists, `""` for none.
    /// Banked for the same reason as the objective.
    var priority: String
    var area: String
    /// The cards this draft waits on, newline-joined ids; `""` for none.
    /// Absent in an older row, which decodes as empty.
    var blockedBy: String
    /// Whether the composer had opened its second half. Default false so an
    /// older row without the key reopens short unless a later box holds text.
    var expanded: Bool

    init(id: String, project: String = "", root: String = "",
         title: String = "", summary: String = "",
         prompt: String = "", workflow: String = "", idea: String = "",
         tool: String = "",
         kind: String = "",
         model: String = "", attachments: [String] = [],
         updatedAt: Double = 0, priority: String = "", area: String = "",
         beneficiary: String = "", intendedBenefit: String = "",
         successCriterion: String = "",
         expanded: Bool = false, blockedBy: String = "") {
        self.id = id
        self.project = project
        self.root = root
        self.title = title
        self.summary = summary
        self.prompt = prompt
        self.workflow = workflow
        self.idea = idea
        self.tool = tool
        self.kind = kind
        self.model = model
        self.attachments = attachments
        self.updatedAt = updatedAt
        self.beneficiary = beneficiary
        self.intendedBenefit = intendedBenefit
        self.successCriterion = successCriterion
        self.priority = priority
        self.area = area
        self.blockedBy = blockedBy
        self.expanded = expanded
    }

    /// Tolerant decode, `PanelPlacement`'s rule per entry: a wrong shape reads
    /// as absent, never as data. Only the id and a dictionary body are
    /// required — every field falls back to its empty value, and an unknown
    /// key is ignored rather than refused, so a newer panel's addition does
    /// not poison an older one's read.
    init?(id: String, any: Any) {
        guard !id.isEmpty, let dict = any as? [String: Any] else { return nil }
        self.init(
            id: id,
            project: dict["project"] as? String ?? "",
            root: dict["root"] as? String ?? "",
            title: dict["title"] as? String ?? "",
            summary: dict["summary"] as? String ?? "",
            prompt: dict["prompt"] as? String ?? "",
            workflow: dict["workflow"] as? String ?? "",
            idea: dict["idea"] as? String ?? "",
            tool: dict["tool"] as? String ?? "",
            kind: dict["kind"] as? String ?? "",
            model: dict["model"] as? String ?? "",
            attachments: (dict["attachments"] as? [Any])?
                .compactMap { $0 as? String } ?? [],
            updatedAt: (dict["updated_at"] as? NSNumber)?.doubleValue ?? 0,
            priority: dict["priority"] as? String ?? "",
            area: dict["area"] as? String ?? "",
            beneficiary: dict["beneficiary"] as? String ?? "",
            intendedBenefit: dict["intended_benefit"] as? String ?? "",
            successCriterion: dict["success_criterion"] as? String ?? "",
            expanded: dict["expanded"] as? Bool ?? false,
            blockedBy: dict["blocked_by"] as? String ?? "")
    }

    var body: [String: Any] {
        [
            "project": project,
            "root": root,
            "title": title,
            "summary": summary,
            "prompt": prompt,
            "workflow": workflow,
            "idea": idea,
            "tool": tool,
            "kind": kind,
            "model": model,
            "attachments": attachments,
            "updated_at": updatedAt,
            "beneficiary": beneficiary,
            "intended_benefit": intendedBenefit,
            "success_criterion": successCriterion,
            "priority": priority,
            "area": area,
            "blocked_by": blockedBy,
            "expanded": expanded,
        ]
    }

    /// What the list draws when nothing was typed in the title. A draft with
    /// no title is still a draft — the description, the instructions or a
    /// dropped file is what kept it.
    var displayTitle: String {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "(untitled)" : trimmed
    }
}

/// The composer's drafts, in `~/.dark-army/card-drafts.json`.
///
/// **Panel-local, never the daemon.** A `drafts` table in `board.db` would make
/// the daemon a party to text nobody has submitted — a store verb, an API
/// field, SSE frame weight — and a draft *state* on `cards` would be worse:
/// every consumer (the auto-filer, `dispatch_card`, the counts, the queue's
/// claims) would have to learn to exclude draft cards, and a missed exclusion
/// is a card Dark Army files, counts or starts that nobody finished writing. A draft
/// is view state that outlives the view; `PanelPlacement` is the precedent, and
/// the daemon's whole involvement is one entry in the permissions sweep.
///
/// The file's discipline is `PanelPlacement`'s, copied deliberately: a
/// `static var root` test seam, tolerant reads (a wrong shape decodes as
/// absent, a garbage file as empty, a malformed entry drops only itself),
/// read-merge-write of the top-level body so no key may drop another, and an
/// `.atomic` write. The one addition is the chmod: an atomic write is a
/// *replace*, so 0600 is re-set after every write rather than once.
enum CardDrafts {
    /// Where the file lives. `PanelStateDirectory.root` is the real state
    /// folder in production and a throwaway temp folder under XCTest, so a
    /// test that never redirects this seam still cannot bank a draft into the
    /// user's live Drafts sheet. Still overridable per test.
    static var root: URL = PanelStateDirectory.root

    static var path: URL {
        root.appendingPathComponent("card-drafts.json")
    }

    /// How often the open composer banks a copy. One constant, read by the
    /// timer — a second literal `10` is how the two drift.
    static let autosaveInterval: TimeInterval = 10

    private static let key = "drafts"

    // MARK: - File

    private static func readBody() -> [String: Any] {
        guard let data = try? Data(contentsOf: path),
              let obj = try? JSONSerialization.jsonObject(with: data)
                as? [String: Any]
        else { return [:] }
        return obj
    }

    private static func writeBody(_ body: [String: Any]) {
        let file = path
        try? FileManager.default.createDirectory(
            at: file.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        guard let data = try? JSONSerialization.data(withJSONObject: body)
        else { return }
        try? data.write(to: file, options: .atomic)
        // After **every** write, not once: `.atomic` replaces the inode, so a
        // chmod applied at creation is gone by the second save.
        try? FileManager.default.setAttributes([.posixPermissions: 0o600],
                                               ofItemAtPath: file.path)
    }

    private static func readRows() -> [String: Any] {
        readBody()[key] as? [String: Any] ?? [:]
    }

    /// Merge the rows back in, keeping every other top-level key — including
    /// one this build has never heard of, which is what a newer panel's
    /// addition looks like from here.
    private static func writeRows(_ rows: [String: Any]) {
        var body = readBody()
        if rows.isEmpty {
            body.removeValue(forKey: key)
        } else {
            body[key] = rows
        }
        writeBody(body)
    }

    // MARK: - Verbs

    /// Every stored draft, newest first. A malformed entry drops itself and
    /// leaves its siblings alone.
    static func all() -> [CardDraft] {
        readRows()
            .compactMap { CardDraft(id: $0.key, any: $0.value) }
            .sorted {
                $0.updatedAt == $1.updatedAt
                    ? $0.id < $1.id
                    : $0.updatedAt > $1.updatedAt
            }
    }

    static func upsert(_ draft: CardDraft) {
        guard !draft.id.isEmpty else { return }
        var rows = readRows()
        rows[draft.id] = draft.body
        writeRows(rows)
    }

    /// Remove the row and **only** the row. The staged folder is the caller's
    /// business: a successful create keeps it (the card owns those copies),
    /// a person's Delete throws it away.
    static func remove(_ id: String) {
        guard !id.isEmpty else { return }
        var rows = readRows()
        guard rows.removeValue(forKey: id) != nil else { return }
        writeRows(rows)
    }

    /// Delete every row except `excluding`, but only if there are exactly
    /// `expectedCount` of them — re-counted **inside this call, at the moment
    /// of deletion**, which is the load-bearing half of `BulkClearDoneGate`'s
    /// discipline. Returns the ids deleted so the caller can discard their
    /// folders, or `nil` when the recount disagreed and nothing was touched.
    ///
    /// A count, not a membership token, and the reasoning is stated rather
    /// than inherited: Done needs the digest because it has two writers (the
    /// daemon's snapshot and the human), so an equal-count swap is reachable.
    /// `card-drafts.json` has exactly one writer — this panel process, on the
    /// main actor, with a second instance evicted by `PanelLock` — so the only
    /// mutation between press and confirm is this same actor's own autosave
    /// tick or delete, and both change the count.
    ///
    /// `excluding` is the draft currently open in the composer. Deleting its
    /// folder while `stagedAttachments` still lists those files is the one way
    /// this feature could eat work instead of keeping it.
    @discardableResult
    static func clearAll(expectedCount: Int, excluding: String = "")
        -> [String]? {
        var rows = readRows()
        let doomed = rows.keys.filter { excluding.isEmpty || $0 != excluding }
        guard doomed.count == expectedCount else { return nil }
        for id in doomed { rows.removeValue(forKey: id) }
        writeRows(rows)
        return doomed
    }

    /// Whether this composer holds work worth keeping. Any of the five typed
    /// fields non-blank after trimming, or a file already copied in. A chosen
    /// tool, model or project alone is **seed state** — `openComposer` writes
    /// the project and root itself — so a composer opened and closed untouched
    /// leaves nothing behind. The idea counts: a form holding nothing but a
    /// sentence in that box is the whole point of the box.
    static func worthKeeping(draft: BoardDraft, staged: [String]) -> Bool {
        if !staged.isEmpty { return true }
        let typed = [draft.title, draft.summary, draft.prompt, draft.workflow,
                     draft.idea, draft.outcome.beneficiary,
                     draft.outcome.intendedBenefit,
                     draft.outcome.successCriterion, draft.priority, draft.area,
                     draft.blockedBy]
        return typed.contains {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
    }
}
