import Foundation

/// One portrait file for a buzz, or nothing. Foundation only, no secrets.
/// The slug list is `identity.NAMES` lowercased, pinned from the Mac by
/// `test_phone_buzz_kinds.py`.
enum NotificationFace {
    static let slugs: [String] = [
        "cipher", "vex", "ledger", "mira", "hex", "relay", "forge",
        "watch", "audit", "proxy", "quiet", "nyx", "canon", "velvet",
        "androll", "captcha", "sawa", "franio", "zosia", "ptyś",
    ]

    /// A file inside `portraitsDirectory`, or nil. A slug that is not on
    /// the roster, an empty name, `.`, `..`, or any slash is nil. The
    /// parent check is what stops `appendingPathComponent` escaping the
    /// folder; a missing file is nil too.
    /// The identity iOS files this sender's picture under: the slug plus a
    /// fingerprint of the portrait's bytes. iOS keeps the first image it saw
    /// for a sender handle and shows it again, so a slug that kept its name
    /// through a recast (captcha, sawa, franio, zosia, androll, ptyś) went on
    /// showing the old face. New art means a new fingerprint, a new sender and
    /// the new face; the same art stays one conversation. FNV-1a 64, because
    /// Foundation has no hash of its own and this needs no secrecy.
    static func senderIdentifier(slug: String, portrait: Data) -> String {
        var hash: UInt64 = 0xcbf29ce484222325
        for byte in portrait {
            hash ^= UInt64(byte)
            hash = hash &* 0x100000001b3
        }
        return slug + "." + String(hash, radix: 16)
    }

    static func portraitURL(portraitsDirectory: URL, slug: String) -> URL? {
        guard slugs.contains(slug) else { return nil }
        if slug.isEmpty || slug == "." || slug == ".." { return nil }
        if slug.contains("/") || slug.contains("\\") { return nil }
        let file = portraitsDirectory.appendingPathComponent(slug + ".png")
        let parent = file.deletingLastPathComponent().standardizedFileURL
        let root = portraitsDirectory.standardizedFileURL
        guard parent.path == root.path else { return nil }
        guard FileManager.default.fileExists(atPath: file.path) else { return nil }
        return file
    }
}
