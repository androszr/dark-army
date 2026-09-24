import Foundation

/// What the priority box may hold, judged as it is typed rather than at
/// Save: the store's `normalise_priority` rule — empty is "no opinion",
/// otherwise ASCII digits from 0 to 100 — restated so the sentence beside
/// the box and the daemon's refusal can never disagree. Copied byte-equal
/// into `ios/BobPhone/CardDetailView.swift` and pinned by
/// `host/tests/test_priority_input.py`.
enum PriorityInput {
    static let limit = 100
    static let words = "a whole number from 0 to 100, or empty"

    /// `nil` where the text is fine; otherwise one short sentence.
    static func problem(_ text: String) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return nil }
        guard trimmed.allSatisfy({ $0.isASCII && $0.isNumber }),
              let number = Int(trimmed) else { return words }
        return number > limit ? words : nil
    }
}
