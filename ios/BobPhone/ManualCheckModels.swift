import Foundation

/// The Checks section's page as the daemon returns it
/// (`GET /api/manual-checks`, the sealed `manual_checks` kind): every
/// enrolled project's manual checks, open first, then newest first.
///
/// Tolerant: a missing key must never blank the list. Byte-copied to the
/// phone (`test_phone_manual_checks.py` pins the pair).
struct ManualChecksReport: Decodable, Equatable {
    var supported = false
    var available = false
    var root = ""
    var checks: [ManualCheckEntry] = []
    var truncated = false
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case supported, available, root, checks, truncated, reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        supported = c.value(.supported, false)
        available = c.value(.available, false)
        root = c.value(.root, "")
        checks = c.value(.checks, [])
        truncated = c.value(.truncated, false)
        reason = c.value(.reason, "")
    }

    init() {}
}

/// One check file, listed: its answer block, where it lives, the card it
/// was flagged on where there is one, and whether it is in the shape.
struct ManualCheckEntry: Decodable, Equatable, Identifiable {
    var path = ""
    var title = ""
    var card = ""
    var cardId = ""
    var project = ""
    var check = ""
    var created = ""
    var status = ""
    var outcome = ""
    var checkedAt = ""
    var stepsPreview = ""
    var malformed = false
    var problem = ""

    var id: String { path }

    enum CodingKeys: String, CodingKey {
        case path, title, card, project, check, created, status, outcome
        case malformed, problem
        case cardId = "card_id"
        case checkedAt = "checked_at"
        case stepsPreview = "steps_preview"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = c.value(.path, "")
        title = c.value(.title, "")
        card = c.value(.card, "")
        cardId = c.value(.cardId, "")
        project = c.value(.project, "")
        check = c.value(.check, "")
        created = c.value(.created, "")
        status = c.value(.status, "")
        outcome = c.value(.outcome, "")
        checkedAt = c.value(.checkedAt, "")
        stepsPreview = c.value(.stepsPreview, "")
        malformed = c.value(.malformed, false)
        problem = c.value(.problem, "")
    }

    init() {}
}

/// One check file's text, `CardReport`'s shape: availability stated, the
/// reason in words when it is not.
struct ManualCheckDocument: Decodable, Equatable {
    var available = false
    var path = ""
    var text = ""
    var reason = ""

    enum CodingKeys: String, CodingKey {
        case available, path, text, reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        path = c.value(.path, "")
        text = c.value(.text, "")
        reason = c.value(.reason, "")
    }

    init() {}
}
