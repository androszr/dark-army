import Foundation

/// The home-screen tile's whole diet, and the only thing the app and the
/// widget share: a handful of counts, one meter per provider, and the
/// moment the app last heard from the Mac. Written by `PhoneClient` after
/// every decoded snapshot and read by the widget's timeline provider — the
/// provider does **no network** and touches **no Keychain**; staleness is
/// the accepted normal case and the tile says so with an age line instead
/// of pretending.
///
/// Compiled into both targets. Lives in the App Group container
/// (`widget-summary.json`), never in the app's own sandbox, because the
/// widget process cannot see that.
struct FleetSummary: Codable, Equatable {
    /// One provider's worst current window — the number a person would act
    /// on. `percent` is 0–100; `stale` is carried so a summary written from
    /// only-stale bars can say so rather than draw a confident meter.
    struct Bar: Codable, Equatable {
        var provider = ""
        var label = ""
        var percent: Double = 0
        var stale = false
    }

    /// One agent, as much of it as a tile needs: which photograph to draw,
    /// what to call it, what it is doing, and the form of the 2pt rule
    /// underneath. Composed by the app from the daemon's own buckets and
    /// the app's one identity table (`Cast.character(for:)`); the widget
    /// derives none of it — it receives a slug and two words and draws them.
    struct Face: Codable, Equatable {
        /// The portrait file's stem — `Cast.character(for:)`'s answer.
        var slug = ""
        /// What to call it, clamped to `maxNameChars` by the writer.
        var name = ""
        /// "needs you" / "working" / "standing by".
        var doing = ""
        /// "work" | "alert" | "sleep" — `Cast.StateRule`'s form.
        var rule = "sleep"
        /// The session the face stands for, so a tap on it can open that
        /// agent (`bobphone://fleet?session=`); empty for the idle cast.
        var sessionId = ""

        init(slug: String = "", name: String = "", doing: String = "",
             rule: String = "sleep", sessionId: String = "") {
            self.slug = slug
            self.name = name
            self.doing = doing
            self.rule = rule
            self.sessionId = sessionId
        }

        /// Tolerant for the same reason the summary is: a note written by a
        /// build that later adds a fifth key must still decode here.
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            slug = try c.decodeIfPresent(String.self, forKey: .slug) ?? ""
            name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
            doing = try c.decodeIfPresent(String.self, forKey: .doing) ?? ""
            rule = try c.decodeIfPresent(String.self, forKey: .rule) ?? "sleep"
            sessionId = try c.decodeIfPresent(String.self, forKey: .sessionId) ?? ""
        }
    }

    /// The Mac's own `counts.attention` — the same reading the menu-bar
    /// strip draws. The widget derives nothing.
    var needsYou = 0
    /// `counts.working`, same source.
    var working = 0
    /// Prep + Backlog, each read on its own — the menu bar's to-do recipe.
    var todo = 0
    /// When the app wrote this note, epoch seconds on the phone's clock.
    /// The tile's age line and its dimming both read this.
    var generatedAt: Double = 0
    /// How long after `generatedAt` the tile should start admitting its
    /// age: the app's background-refresh floor plus slack, never under
    /// 600. The app writes it; the widget reads it and derives nothing.
    var dimAfter: Double = 600
    var bars: [Bar] = []
    /// Who the agents are, for the tile's face column — the ones who need
    /// you, else the ones working, else the whole cast standing by. Empty
    /// from an older app, and the widget draws no column at all rather than
    /// a blank one.
    var faces: [Face] = []
    /// At most half a day of hourly rotation is worth carrying.
    static let maxFaces = 8
    /// A nickname longer than this does not fit the 84pt column.
    static let maxNameChars = 14

    init(needsYou: Int = 0, working: Int = 0, todo: Int = 0,
         generatedAt: Double = 0, dimAfter: Double = 600, bars: [Bar] = [],
         faces: [Face] = []) {
        self.needsYou = needsYou
        self.working = working
        self.todo = todo
        self.generatedAt = generatedAt
        self.dimAfter = dimAfter
        self.bars = bars
        self.faces = faces
    }

    /// Tolerant on every key: a synthesized decoder throws on a missing one
    /// even with a default, and a summary written by an older app (no
    /// `dimAfter`) must still draw rather than fall back to "open Dark Army".
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        needsYou = try c.decodeIfPresent(Int.self, forKey: .needsYou) ?? 0
        working = try c.decodeIfPresent(Int.self, forKey: .working) ?? 0
        todo = try c.decodeIfPresent(Int.self, forKey: .todo) ?? 0
        generatedAt = try c.decodeIfPresent(Double.self, forKey: .generatedAt) ?? 0
        dimAfter = try c.decodeIfPresent(Double.self, forKey: .dimAfter) ?? 600
        bars = try c.decodeIfPresent([Bar].self, forKey: .bars) ?? []
        faces = try c.decodeIfPresent([Face].self, forKey: .faces) ?? []
    }

    /// The App Group the app and the widget share. Read from the
    /// `DarkArmyAppGroup` Info.plist key (`group.$(DARK_ARMY_BUNDLE_ID)`,
    /// set once in ios/Config/Identity.xcconfig); with the key missing, it
    /// is derived from the running bundle's id, the extension suffix dropped.
    static let appGroup: String = {
        if let value = Bundle.main.object(forInfoDictionaryKey: "DarkArmyAppGroup") as? String,
           !value.isEmpty, !value.contains("$(") {
            return value
        }
        var base = Bundle.main.bundleIdentifier ?? ""
        for suffix in [".widget", ".notification"] where base.hasSuffix(suffix) {
            base = String(base.dropLast(suffix.count))
        }
        return "group." + base
    }()
    static let fileName = "widget-summary.json"

    /// Everything but the clock — what "the numbers changed" means for the
    /// app's reload throttle.
    ///
    /// `faces` is deliberately **not** a term here, and must not become one.
    /// A roster flaps at turn cadence: an agent moving `running` →
    /// `waiting` → `running` inside a minute would spend a reload every poll
    /// while the numbers a person actually acts on never moved. The note is
    /// written on every check-in regardless, so a changed roster rides the
    /// next reload the counts earn, or the freshness rung at the latest.
    var figures: (Int, Int, Int, [Bar]) { (needsYou, working, todo, bars) }

    static func fileURL() -> URL? {
        FileManager.default
            .containerURL(forSecurityApplicationGroupIdentifier: appGroup)?
            .appendingPathComponent(fileName)
    }

    /// nil for a phone that has never opened Dark Army (or has no App Group in a
    /// mis-provisioned build) — the widget draws its "open Dark Army" placeholder.
    static func load() -> FleetSummary? {
        guard let url = fileURL(),
              let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(FleetSummary.self, from: data)
    }

    /// Best-effort, atomic. A failed write keeps the previous note, which
    /// the age line then reports honestly — and the writer is told why, in
    /// words the profile screen can show (`PhoneClient.widgetWriteReport`):
    /// a tile stuck on yesterday's note looked exactly like a budget problem
    /// for a day before anyone could ask the phone whether the file was
    /// being written at all.
    @discardableResult
    func store() -> String {
        guard let url = Self.fileURL() else {
            return "no shared container"
        }
        let data: Data
        do { data = try JSONEncoder().encode(self) } catch {
            return "encode failed: \(error.localizedDescription)"
        }
        do { try data.write(to: url, options: .atomic) } catch {
            return "write failed: \(error.localizedDescription)"
        }
        return ""
    }
}

/// The deep links the widget may aim at the app. One builder, compiled into
/// both targets, so the widget composes no URL by hand and the app's
/// `PhoneRouter.open` reads the same shape.
enum FleetLinks {
    static let scheme = "bobphone"

    /// `bobphone://fleet?session=<id>`, or nil for an empty id.
    static func agent(_ sessionId: String) -> URL? {
        guard !sessionId.isEmpty else { return nil }
        var parts = URLComponents()
        parts.scheme = scheme
        parts.host = "fleet"
        parts.queryItems = [URLQueryItem(name: "session", value: sessionId)]
        return parts.url
    }
}

