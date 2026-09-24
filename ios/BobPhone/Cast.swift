import SwiftUI
import UIKit

/// The cast, their three states, and which agent wears which face.
///
/// The phone's copy of `panel/Sources/BobPanel/Cast.swift`. Fourteen Dark
/// Army callsigns plus six of the household — Androll, Captcha, Sawa,
/// Franio, Zosia and Ptyś — in this order, matching `identity.NAMES` and the panel —
/// pinned by `host/tests/test_phone_theme_drift.py`, which is the only thing
/// standing between three copies of this list and three different answers
/// to "who is this". A **partial** copy: it carries `names`, `artOnly`,
/// `portrait(_:)` and `character(for:)` but not `character(forNickname:)`,
/// and ends with the byte-pinned `CastQuotes`.
///
/// A name here needs no art to be legal: a character with no photograph in
/// `Resources/portraits/` is drawn as its initial on a plain tile, never as
/// a hashed substitute.
enum Cast {
    static let names = ["Cipher", "Vex", "Ledger", "Mira",
                        "Hex", "Relay", "Forge",
                        "Watch", "Audit", "Proxy", "Quiet", "Nyx",
                        "Canon", "Velvet",
                        "Androll", "Captcha", "Sawa", "Franio", "Zosia", "Ptyś"]

    /// Slugs with art that no session is ever assigned. `overwatch` is the
    /// chief of staff's alter ego — `bc-planner`'s banner is Overwatch, and
    /// it is never a nickname a session wears. Mirrors `identity.ART_ONLY`.
    static let artOnly = ["overwatch"]

    enum State: String {
        case work, sleep, alert
    }

    private static var portraits: [String: UIImage?] = [:]

    /// The portraits folder inside the app bundle.
    ///
    /// `Bundle.main`, **not** `Bundle.module`: the panel is a SwiftPM target
    /// with generated resource accessors and this is an Xcode app target with a
    /// blue-folder reference. Copying the panel's line verbatim compiles and
    /// then finds nothing at runtime — every face silently blank.
    private static var portraitDirectory: URL? {
        Bundle.main.url(forResource: "portraits", withExtension: nil)
    }

    /// The still photograph for one slug, loaded once — `nil` where the
    /// tree has none yet. Misses are cached too.
    static func portrait(_ slug: String) -> UIImage? {
        let key = slug.lowercased()
        if let hit = portraits[key] { return hit }
        let path = portraitDirectory?.appendingPathComponent("\(key).png").path
        let image = path.flatMap { UIImage(contentsOfFile: $0) }
        portraits[key] = image
        return image
    }

    /// The face for an agent. An overflow nickname (`Cipher-ab12`) wears the
    /// face of its stem, matching the panel's `character(forNickname:)`. Only
    /// a name off the roster entirely falls through to the session-id hash, so
    /// it still gets a stable face rather than none.
    ///
    /// `&*` / `&+` are load-bearing: the panel wraps at 64 bits and so must
    /// this, or the two draw different faces for the same session.
    static func character(for agent: Agent) -> String {
        let nick = agent.nickname.lowercased()
        if names.contains(where: { $0.lowercased() == nick }) { return nick }
        let stem = nick.split(separator: "-").first.map(String.init) ?? nick
        if let match = names.first(where: { $0.lowercased() == stem }) {
            return match.lowercased()
        }
        var hash = 5381
        for byte in agent.sessionId.utf8 { hash = (hash &* 33) &+ Int(byte) }
        return names[abs(hash) % names.count].lowercased()
    }

    static func state(forBucket bucket: String) -> State {
        switch bucket {
        case "waiting": return .alert
        case "running": return .work
        default: return .sleep
        }
    }

    static func state(for agent: Agent, category: Category) -> State {
        switch category {
        case .waiting: return .alert
        case .running: return .work
        case .sleeping, .finished, .abandoned: return .sleep
        }
    }

    static func initial(of slug: String) -> String {
        String(slug.prefix(1)).uppercased()
    }
}

/// The 2pt line under a face that says what the agent is doing.
///
/// A portrait is one still, so the state is carried beside it — by **form**
/// before colour: solid for `.work`, dashed for `.sleep`, solid red for
/// `.alert`. The panel's `StateRule`, over the same three inks.
struct StateRule: View {
    let state: Cast.State
    var width: CGFloat
    static let height: CGFloat = 2

    var body: some View {
        Path { path in
            path.move(to: CGPoint(x: 0, y: Self.height / 2))
            path.addLine(to: CGPoint(x: width, y: Self.height / 2))
        }
        .stroke(ink, style: StrokeStyle(lineWidth: Self.height, dash: dash))
        .frame(width: width, height: Self.height)
    }

    private var ink: Color {
        switch state {
        case .work: return Theme.phosphor
        case .sleep: return Theme.faint
        case .alert: return Theme.alarm
        }
    }

    private var dash: [CGFloat] {
        state == .sleep ? [3, 2] : []
    }
}

/// One line per portrait slug, drawn under a **large** portrait only — the
/// Mac's detail header and quiet screens, the phone's agent page — and never
/// on a row, tile, badge, inbox entry, crew band, area grid, widget or card.
/// Mirrors `identity.QUOTES`; byte-pinned on both clients from
/// `enum CastQuotes {` to the end of the file by
/// `host/tests/test_cast_quotes.py`, which also pins who may call it.
enum CastQuotes {
    static let lines: [String: String] = [
        "cipher": "Root the plan before you root the box.",
        "vex": "Crash it on purpose. Own the dump.",
        "ledger": "Burn rate lies. Tokens don't.",
        "mira": "No AC? No merge. Cry elsewhere.",
        "hex": "Segfaults are just honesty with teeth.",
        "relay": "No dropped packets or handoffs.",
        "forge": "CI red? You're still in the shell.",
        "watch": "Unsigned binaries don't leave the room.",
        "audit": "Follow the write. Ignore the pitch.",
        "proxy": "I live in the gap between your PRs.",
        "quiet": "No status spam. Just a clean diff.",
        "nyx": "Default deny. Prove trust in code.",
        "canon": "Break the contract, I break the branch.",
        "velvet": "Main unlocks when the crew aligns.",
        "androll": "Daemon up or nothing ships.",
        "captcha": "Pretty plans die in my review.",
        "sawa": "Green CI or stay offline.",
        "franio": "Fail fast. Patch once. No cosplay.",
        "zosia": "Backlog hygiene is brain opsec.",
        "ptyś": "Tiny commits. Wide kill radius.",
        "overwatch": "Map the blast radius. Then one key.",
    ]

    /// The quote for one portrait slug; `""` for a slug off the roster.
    static func line(for slug: String) -> String {
        lines[slug.lowercased()] ?? ""
    }

    /// The quote for a nickname's own character: its lowercased stem before
    /// `-`, and only when that stem is on the roster or art-only. Any other
    /// name says nothing, so a hashed stranger's face never speaks somebody
    /// else's line.
    static func line(forNickname nickname: String) -> String {
        let stem = (nickname.split(separator: "-").first.map(String.init)
                    ?? nickname).lowercased()
        let roster = Cast.names.map { $0.lowercased() } + Cast.artOnly
        guard roster.contains(stem) else { return "" }
        return lines[stem] ?? ""
    }
}
