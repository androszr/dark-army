import Foundation

/// New delivery fields are additive only when absent. Validate them at each
/// response root before legacy tolerant containers can swallow a row error.
///
/// This lives apart from `Models.swift` on purpose: the row models there decode
/// tolerantly (`test_models_and_card_detail_decode_tolerantly` forbids a strict
/// `decode` in that file), whereas a present-but-wrong-typed area field must
/// reject the whole frame — the strictness here is the point.
enum AreaWireFields {
    private enum Key: String, CodingKey {
        case area, area_line, areas_supported, suggested_area
        case board, agents, cards, current
        case running, sleeping, waiting, abandoned, finished
    }
    private typealias Container = KeyedDecodingContainer<Key>

    private static func strings(_ c: Container) throws {
        for key in [Key.area, .area_line, .suggested_area] where c.contains(key) {
            _ = try c.decode(String.self, forKey: key)
        }
    }
    private static func rows(_ c: Container, key: Key) throws {
        // Keep the established fallback for an old malformed container. Once
        // it is an array of objects, new fields in every row must be sound.
        guard var rows = try? c.nestedUnkeyedContainer(forKey: key) else { return }
        while !rows.isAtEnd {
            let row = try rows.superDecoder()
            if let fields = try? row.container(keyedBy: Key.self) {
                try strings(fields)
            }
        }
    }
    private static func board(_ c: Container) throws {
        if c.contains(.areas_supported) {
            _ = try c.decode(Bool.self, forKey: .areas_supported)
        }
        try rows(c, key: .cards)
    }
    private static func agents(_ c: Container) throws {
        for key in [Key.running, .sleeping, .waiting, .abandoned, .finished] {
            try rows(c, key: key)
        }
    }
    static func validate(_ decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: Key.self) else { return }
        try strings(c)
        try board(c)
        try agents(c)
        if let nested = try? c.nestedContainer(keyedBy: Key.self, forKey: .board) {
            try board(nested)
        }
        if let nested = try? c.nestedContainer(keyedBy: Key.self, forKey: .agents) {
            try agents(nested)
        }
        if let current = try? c.nestedContainer(keyedBy: Key.self, forKey: .current) {
            try strings(current)
        }
    }
    private struct Envelope: Decodable {
        init(from decoder: Decoder) throws { try AreaWireFields.validate(decoder) }
    }
    static func accepts(_ data: Data) -> Bool {
        (try? JSONDecoder().decode(Envelope.self, from: data)) != nil
    }
}
