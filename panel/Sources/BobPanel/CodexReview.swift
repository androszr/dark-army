import Foundation
import SwiftUI

/// A report is display data, never a session address or permission to act.
struct CodexReviewReport: Decodable, Equatable {
    var sessionId: String
    var label: String
    var text: String
    var truncated: Bool

    enum CodingKeys: String, CodingKey { case sessionId = "session_id", label, text, truncated }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = (try? c.decode(String.self, forKey: .sessionId)) ?? ""
        label = (try? c.decode(String.self, forKey: .label)) ?? "Codex review"
        text = (try? c.decode(String.self, forKey: .text)) ?? ""
        truncated = (try? c.decode(Bool.self, forKey: .truncated)) ?? false
    }
}

struct CodexReview: Equatable {
    struct Finding: Equatable {
        let title: String
        let explanation: String
        let path: String
        let start: Int?
        let end: Int?
        var location: String {
            guard !path.isEmpty else { return "" }
            guard let start else { return path }
            return path + ":\(start)" + (end.map { "–\($0)" } ?? "")
        }
    }
    let findings: [Finding]
    let conclusion: String
    let explanation: String
    static let maxBytes = 65536

    static func decode(_ source: String) -> CodexReview? {
        guard source.utf8.count <= maxBytes,
              let data = source.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let entries = object["findings"] as? [[String: Any]], entries.count <= 100,
              let conclusion = object["overall_correctness"] as? String, !conclusion.isEmpty,
              let explanation = object["overall_explanation"] as? String else { return nil }
        var findings: [Finding] = []
        for entry in entries {
            guard let title = entry["title"] as? String, !title.isEmpty,
                  let body = entry["body"] as? String else { return nil }
            let location = entry["code_location"] as? [String: Any]
            let lines = location?["line_range"] as? [String: Any]
            let start = (lines?["start"] as? Int).flatMap { $0 > 0 ? $0 : nil }
            let end = (lines?["end"] as? Int).flatMap { $0 >= (start ?? 1) ? $0 : nil }
            findings.append(Finding(title: title, explanation: body,
                path: location?["absolute_file_path"] as? String ?? "", start: start, end: end))
        }
        return CodexReview(findings: findings, conclusion: conclusion, explanation: explanation)
    }

    enum Presentation: Equatable { case review(CodexReview), raw(String), prose(String) }
    static func presentation(_ source: String, truncated: Bool = false) -> Presentation {
        let trimmed = source.trimmingCharacters(in: .whitespacesAndNewlines)
        if !truncated, let review = decode(source) { return .review(review) }
        if truncated || source.utf8.count > maxBytes || trimmed.hasPrefix("{") || trimmed.hasPrefix("[") {
            let bytes = Array(source.utf8.prefix(maxBytes))
            let raw = String(decoding: bytes, as: UTF8.self)
            return .raw(raw + (source.utf8.count > maxBytes ? "\n… output truncated" : ""))
        }
        return .prose(source)
    }
}

struct CodexReviewOutput: View {
    let source: String
    var truncated = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if truncated { Text("Partial review — output truncated").foregroundStyle(.orange) }
            switch CodexReview.presentation(source, truncated: truncated) {
            case .review(let review):
                ForEach(Array(review.findings.enumerated()), id: \.offset) { _, finding in
                    VStack(alignment: .leading, spacing: 5) {
                        Text(finding.title).font(Theme.mono(12, weight: .semibold))
                        MarkdownText(source: finding.explanation, base: 11, mono: true)
                        if !finding.location.isEmpty { Text(finding.location).foregroundStyle(Theme.faint) }
                    }
                    Rectangle().fill(Theme.hair).frame(height: 1)
                }
                Text(review.conclusion).font(Theme.mono(12, weight: .semibold))
                MarkdownText(source: review.explanation, base: 11, mono: true)
                DisclosureGroup("Raw review") { Text(source) }
            case .raw(let raw): Text(raw)
            case .prose(let prose): MarkdownText(source: prose, base: 11, mono: true).equatable()
            }
        }
        .font(Theme.mono(11))
        .textSelection(.enabled)
        .fixedSize(horizontal: false, vertical: true)
    }
}
