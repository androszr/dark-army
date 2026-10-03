import Foundation
import Security

/// Prepare on this phone: the Mac's card-writing helper, run from the phone
/// against Anthropic directly when the Mac is out of reach.
///
/// Three pieces, each small. `AnthropicKeyStore` keeps the person's own
/// Anthropic key in the Keychain — this device only, readable only while the
/// phone is unlocked, never synced. `AnthropicMessages` is one `POST` to the
/// Messages API. `PhonePreparer` builds the exact prompt the Mac builds
/// (`PreparerBrief`, `CardPrepareRules.promptForIdea`), sends it, reads the
/// answer with the Mac's rules and returns the composer's own
/// `PhonePrepareResult`, so the apply block in `ComposerView.prepare()` is
/// shared and untouched.
///
/// **The key leaves the phone to Anthropic and nowhere else.** The one
/// reader of `AnthropicKeyStore.load()` is `PhonePreparer.prepare`; the URL
/// is a literal, never an address from the pairing or the snapshot; the
/// answer comes back as *fields*, never as the raw body, so nothing of it
/// can land in `lastError`, a receipt, the outbox or a saved file.
/// `host/tests/test_phone_prepare_parity.py` greps the whole of `ios/` for
/// both.

enum AnthropicKeyStore {
    static let service = "local.bob.BobPhone"
    static let account = "anthropic-api-key"

    static var hasKey: Bool { load() != nil }

    /// The last four characters, for the "key saved · ends …1234" row. Never
    /// the key.
    static var suffix: String {
        guard let key = load() else { return "" }
        return String(key.suffix(4))
    }

    @discardableResult
    static func save(_ key: String) -> Bool {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8) else { return false }
        remove()
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            // Stricter than the pairing's item on purpose: the pairing is
            // read by the widget's background refresh with the screen off;
            // this key is read only from a press in the foreground.
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            kSecValueData as String: data,
        ]
        return SecItemAdd(query as CFDictionary, nil) == errSecSuccess
    }

    static func load() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let key = String(data: data, encoding: .utf8),
              !key.isEmpty else { return nil }
        return key
    }

    static func remove() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}

/// Why a phone-side Prepare did not answer, in words a person can read.
/// Anthropic's own `error.message` where it gave one; the transport's
/// sentence otherwise. Never the raw body.
struct PrepareFailure: Error {
    let detail: String
}

enum AnthropicMessages {
    /// The one place this address is written in the whole of `ios/`.
    static let endpoint = URL(string: "https://api.anthropic.com/v1/messages")
    static let model = "claude-haiku-4-5-20251001"
    static let version = "2023-06-01"
    static let maxTokens = 4096
    /// `card_prepare.TIMEOUT_SECONDS`.
    static let timeout: TimeInterval = 60

    static func send(key: String, system: String, user: String) async
        -> Result<String, PrepareFailure> {
        guard let endpoint else {
            return .failure(PrepareFailure(detail: "The address could not be formed."))
        }
        var request = URLRequest(url: endpoint, timeoutInterval: timeout)
        request.httpMethod = "POST"
        request.setValue(key, forHTTPHeaderField: "x-api-key")
        request.setValue(version, forHTTPHeaderField: "anthropic-version")
        request.setValue("application/json", forHTTPHeaderField: "content-type")
        let body: [String: Any] = [
            "model": model,
            "max_tokens": maxTokens,
            "system": system,
            "messages": [["role": "user", "content": user]],
        ]
        guard let payload = try? JSONSerialization.data(withJSONObject: body) else {
            return .failure(PrepareFailure(detail: "The request could not be written."))
        }
        request.httpBody = payload
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            return .failure(PrepareFailure(detail: error.localizedDescription))
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard status == 200 else {
            let message = ((object?["error"] as? [String: Any])?["message"] as? String)
                ?? "Anthropic refused (HTTP \(status))."
            return .failure(PrepareFailure(detail: message))
        }
        let blocks = (object?["content"] as? [[String: Any]]) ?? []
        let text = blocks.compactMap { block -> String? in
            guard (block["type"] as? String) == "text" else { return nil }
            return block["text"] as? String
        }.joined()
        return .success(text)
    }
}

/// Which side wrote the card's fields.
enum PrepareRoute: Equatable {
    case mac, phone

    var sentence: String {
        switch self {
        case .mac: return "Prepared on the Mac"
        case .phone: return "Prepared on this phone"
        }
    }
}

enum PhonePreparer {
    static let noPhotos =
        "Prepare on this phone does not read photos — remove them, or wait for the Mac."
    static let noKey = "No key saved on this phone."

    /// The helpers Dark Army ships, in the roster's shape. The Mac reads the
    /// project's own `.claude/agents`, which the phone cannot see; a helper
    /// the project does not declare is dropped by the Mac at create
    /// (`_roster_verdict`), never refused, so a card prepared here is always
    /// savable.
    static func roster(from pools: [String]) -> [String] {
        pools.filter { $0 != PreparerBrief.name }.sorted()
    }

    /// The Mac's `_prepare_card_text_locked`, on the phone: the same prompt,
    /// the same readers, the refusals in the same order, the same reply
    /// shape. `catalogueProjects` is the outbox's last-known list — the
    /// closed set FOLDER may name.
    static func prepare(idea: String, tool: String, projectName: String,
                        roots: [String], roster: [String],
                        areas: [(slug: String, name: String, concept: String)],
                        candidates: [(id: String, title: String)] = [],
                        hasPhotos: Bool) async -> PhonePrepareResult {
        if hasPhotos {
            return PhonePrepareResult(ok: false, detail: noPhotos, prompt: "", workflow: "")
        }
        guard let key = AnthropicKeyStore.load() else {
            return PhonePrepareResult(ok: false, detail: noKey, prompt: "", workflow: "")
        }
        let prompt = CardPrepareRules.promptForIdea(
            modeHead: PreparerBrief.modeHeadIdea, idea: idea, tool: tool,
            project: projectName, roster: roster, roots: roots,
            candidates: candidates.map(\.title),
            areas: areas.map { (name: $0.name, concept: $0.concept) })
        let raw: String
        switch await AnthropicMessages.send(key: key, system: PreparerBrief.text,
                                            user: prompt) {
        case .failure(let failure):
            return PhonePrepareResult(ok: false, detail: failure.detail,
                                      prompt: "", workflow: "")
        case .success(let text):
            raw = text
        }
        let read = CardPrepareRules.parseIdea(raw, roster: roster)
        if let reason = CardPrepareRules.titleRefusal(read.title)
            ?? CardPrepareRules.summaryRefusal(read.summary) {
            return PhonePrepareResult(ok: false, detail: reason, prompt: "", workflow: "")
        }
        let objective = CardPrepareRules.parseObjective(raw)
        if let reason = CardPrepareRules.objectiveRefusal(objective) {
            return PhonePrepareResult(ok: false, detail: reason, prompt: "", workflow: "")
        }
        if let reason = CardPrepareRules.promptRefusal(read.prompt) {
            return PhonePrepareResult(ok: false, detail: reason, prompt: "", workflow: "")
        }
        return PhonePrepareResult(
            ok: true, detail: "",
            prompt: read.prompt,
            workflow: read.stages.joined(separator: "\n"),
            title: read.title,
            summary: read.summary,
            suggestedRoot: CardPrepareRules.parseFolder(raw, roots: roots),
            suggestedArea: CardPrepareRules.parseArea(
                raw, areas: areas.map { (slug: $0.slug, name: $0.name) }),
            beneficiary: objective["beneficiary"] ?? "",
            intendedBenefit: objective["intended_benefit"] ?? "",
            successCriterion: objective["success_criterion"] ?? "",
            suggestedDependencies: CardPrepareRules.parseDependencies(
                raw, candidates: candidates),
            preparedVia: .phone)
    }
}
