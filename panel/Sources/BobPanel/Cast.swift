import AppKit
import SwiftUI

/// The cast, their three states, and which agent wears which face.
///
/// Fourteen Dark Army callsigns plus six of the household — Androll,
/// Captcha, Sawa, Franio, Zosia and Ptys, appended so the original fourteen keep their
/// indices (the 22 Sep 2026 rebrand swapped those fourteen in place). Keep this in step with `identity.NAMES` and the phone's copy,
/// **in this order**: `character(for:)` indexes the array by a hash, so a
/// reordering hands every agent somebody else's face.
/// `host/tests/test_identity.py` and `test_phone_theme_drift.py` pin the
/// three lists equal.
///
/// A name here needs no art to be legal. The panel draws a portrait out of
/// `Resources/portraits/<slug>.png` where one exists and the character's
/// initial on a plain tile where none does — never a hashed substitute,
/// because a stranger's face reads as a bug and an initial reads as "not
/// chosen yet". The menu-bar strip keeps its own pixel-art tree
/// (`assets/cast`) and its own fallback; nothing here reads that tree.
enum Cast {
    static let names = ["Cipher", "Vex", "Ledger", "Mira",
                        "Hex", "Relay", "Forge",
                        "Watch", "Audit", "Proxy", "Quiet", "Nyx",
                        "Canon", "Velvet",
                        "Androll", "Captcha", "Sawa", "Franio", "Zosia", "Ptys"]

    /// Slugs with art that no session is ever assigned. `overwatch` is the
    /// chief of staff's alter ego — `bc-planner`'s banner is Overwatch, and
    /// it is never a nickname a session wears. Mirrors `identity.ART_ONLY`.
    static let artOnly = ["overwatch"]

    enum State: String {
        case work, sleep, alert
    }

    private static var portraits: [String: NSImage?] = [:]

    /// The still photograph for one slug, loaded once — `nil` where the
    /// tree has none yet. Misses are cached too: a row is redrawn on every
    /// push, and a missing file must not cost a disk hit each time.
    static func portrait(_ slug: String) -> NSImage? {
        let key = slug.lowercased()
        if let hit = portraits[key] { return hit }
        let url = PanelResources.url(folder: "portraits", file: "\(key).png")
        let image = url.flatMap { NSImage(contentsOf: $0) }
        portraits[key] = image
        return image
    }

    /// The face for an agent. An overflow nickname (`Cipher-ab12`) wears the
    /// face of its stem, exactly as `character(forNickname:)` does — the two
    /// must agree, or one card and one row draw different people for the same
    /// session. Only a name off the roster entirely falls through to the
    /// session-id hash, so it still gets a stable face rather than none.
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

    /// The face for a nickname with no live `Agent` behind it — a board card
    /// whose filing session is gone. The stem before `-` is the roster match
    /// so `Cipher-ab12` wears Cipher, not a hash of the whole string.
    static func character(forNickname nickname: String) -> String {
        let stem = nickname.split(separator: "-").first.map(String.init) ?? nickname
        if let match = names.first(where: {
            $0.caseInsensitiveCompare(stem) == .orderedSame
        }) {
            return match.lowercased()
        }
        var hash = 5381
        for byte in nickname.utf8 { hash = (hash &* 33) &+ Int(byte) }
        return names[abs(hash) % names.count].lowercased()
    }

    /// The face pose for a snapshot bucket name — the string form the board
    /// and the pipeline band carry instead of a `Category`. One convention,
    /// three callers: hoisted out of `BoardCardView.liveMarkState` when the
    /// band gained faces, because two copies is how a convention drifts.
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
}

/// The 2pt line under a face that says what the agent is doing.
///
/// A portrait is one still, the same whether the agent is working, asleep or
/// waiting on you — so the state has to be carried beside it, and it is
/// carried by **form** before colour: solid for `.work`, dashed for `.sleep`,
/// solid red for `.alert`. Colour only reinforces the shape, which is
/// `menubar_cast_icons.RULE_INK`'s own rule restated on this surface. Never
/// colour alone.
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

/// The circle an avatar is clipped to.
///
/// It earns its place by giving the column one footprint — a photograph is
/// rectangular and portraits are not all framed alike, so without a fixed
/// shape the left edge of the list is ragged and the faces read as pasted-in
/// rather than as a cast. What survives of the old off-white disc is the
/// hairline ring and the shadow; the fill is never seen behind an opaque
/// photograph, and it is the *initial* tile that draws its own ground.
///
/// The brightest thing in the panel has to be the signal text — the only
/// part that is ever urgent — so the portrait is dimmed a touch rather than
/// drawn at full brightness. A bright photograph would otherwise win the
/// row from a red line.
private struct AvatarDisc: View {
    var size: CGFloat

    var body: some View {
        Circle()
            .strokeBorder(.black.opacity(0.10), lineWidth: 0.5)
            .shadow(color: .black.opacity(0.28), radius: 2.5, y: 1)
            .frame(width: size, height: size)
    }
}

/// One character, held still, with the state rule beneath.
///
/// There is no clock here any more. The animated version needed a
/// visibility gate because a hidden panel was measured still building a dozen
/// avatars 12.5 times a second; a still image has no schedule to gate, which
/// is a strict reduction. The state a row is in is read from `StateRule`.
struct AvatarView: View {
    let character: String
    let state: Cast.State
    var size: CGFloat = 44

    /// How far below full brightness a portrait sits. See `AvatarDisc`.
    static let portraitDim: Double = 0.88

    var body: some View {
        VStack(spacing: 3) {
            ZStack {
                if let image = Cast.portrait(character) {
                    Image(nsImage: image)
                        .interpolation(.high)
                        .resizable()
                        .scaledToFill()
                        .frame(width: size, height: size)
                        .clipShape(Circle())
                        .opacity(Self.portraitDim)
                } else {
                    // Not chosen yet: the character's own initial, never a
                    // stranger's face.
                    Circle()
                        .fill(Theme.well)
                    Text(Self.initial(of: character))
                        .font(Theme.mono(size * 0.42, weight: .medium))
                        .foregroundStyle(Theme.faint)
                }
                AvatarDisc(size: size)
            }
            .frame(width: size, height: size)
            StateRule(state: state, width: size * 0.6)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(character), \(state.rawValue)")
    }

    static func initial(of slug: String) -> String {
        String(slug.prefix(1)).uppercased()
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
        "ptys": "Tiny commits. Wide kill radius.",
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
