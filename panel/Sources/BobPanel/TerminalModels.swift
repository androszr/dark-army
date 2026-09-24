import Foundation

// MARK: - A Dark Army-owned terminal's screen

/// One styled run of a terminal row: `[text, fg, bg, attrs]` on the wire
/// (`vtgrid.runs_of`). Colours are `-1` for the default, 0…255 for the
/// palette, or `0x1000000 | rgb` for truecolour; `attrs` is a bitmask
/// (1 bold, 2 dim, 4 underline, 8 inverse, 16 italic).
struct TerminalRun: Decodable, Equatable {
    var text: String
    var fg: Int = -1
    var bg: Int = -1
    var attrs: Int = 0

    init(text: String, fg: Int = -1, bg: Int = -1, attrs: Int = 0) {
        self.text = text
        self.fg = fg
        self.bg = bg
        self.attrs = attrs
    }

    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        text = (try? c.decode(String.self)) ?? ""
        fg = (try? c.decode(Int.self)) ?? -1
        bg = (try? c.decode(Int.self)) ?? -1
        attrs = (try? c.decode(Int.self)) ?? 0
    }
}

/// One changed row: `[y, [runs…]]` on the wire.
struct TerminalRowChange: Decodable, Equatable {
    var y: Int
    var runs: [TerminalRun]

    init(y: Int, runs: [TerminalRun]) {
        self.y = y
        self.runs = runs
    }

    init(from decoder: Decoder) throws {
        var c = try decoder.unkeyedContainer()
        y = (try? c.decode(Int.self)) ?? -1
        runs = (try? c.decode([TerminalRun].self)) ?? []
    }
}

/// One frame of `GET /api/terminal` (and the phone's sealed `terminal`
/// kind). Every key decodes tolerantly: `rows_changed` is legitimately
/// empty on an unchanged frame, and an absent key must never read as a
/// blank screen — `unchanged` is a **present** key for that reason.
struct TerminalFrame: Decodable, Equatable {
    var available = false
    var session = ""
    var name = ""
    var revision = 0
    var cols = 0
    var rows = 0
    var rowsChanged: [TerminalRowChange] = []
    var more = false
    var overflowed = false
    var unchanged = false
    var cursor: [Int] = []
    var cursorVisible = true
    var title = ""
    var exited = false
    var reason = ""
    /// Lines that have scrolled off the live screen, oldest first. `nil`
    /// means the key was omitted (an unchanged frame) — keep what we hold.
    /// Present-and-empty is a cleared scrollback.
    var history: [[TerminalRun]]? = nil
    /// Raw pty bytes, base64. Absent on the phone's grid poll.
    var data = ""
    var bytesRead = 0
    var ringOverflowed = false

    var dataBytes: Data { Data(base64Encoded: data) ?? Data() }
    var hasData: Bool { !data.isEmpty }

    init() {}

    enum CodingKeys: String, CodingKey {
        case available, session, name, revision, cols, rows, more, overflowed
        case unchanged, cursor, title, exited, reason, history, data
        case rowsChanged = "rows_changed"
        case cursorVisible = "cursor_visible"
        case bytesRead = "bytes_read"
        case ringOverflowed = "ring_overflowed"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        session = c.value(.session, "")
        name = c.value(.name, "")
        revision = c.value(.revision, 0)
        cols = c.value(.cols, 0)
        rows = c.value(.rows, 0)
        rowsChanged = c.value(.rowsChanged, [])
        more = c.value(.more, false)
        overflowed = c.value(.overflowed, false)
        unchanged = c.value(.unchanged, false)
        cursor = c.value(.cursor, [])
        cursorVisible = c.value(.cursorVisible, true)
        title = c.value(.title, "")
        exited = c.value(.exited, false)
        reason = c.value(.reason, "")
        history = c.maybe(.history)
        data = c.value(.data, "")
        bytesRead = c.value(.bytesRead, 0)
        ringOverflowed = c.value(.ringOverflowed, false)
    }
}

/// The grid a pane holds between frames: a dumb renderer's memory, never
/// an emulator. `apply` folds one frame in — a full reset on a new session,
/// a size change or an `overflowed` frame, else only the rows the frame
/// carries — and is pure so it is table-testable.
struct TerminalScreen: Equatable {
    var session = ""
    var cols = 0
    var rows = 0
    var revision = -1
    var lines: [[TerminalRun]] = []
    var cursor: (Int, Int) = (0, 0)
    var cursorVisible = true
    var title = ""
    var exited = false
    /// Scrolled-off lines, oldest first. Replaced only when a frame
    /// carries `history`; an unchanged poll leaves this alone.
    var history: [[TerminalRun]] = []

    static func == (lhs: TerminalScreen, rhs: TerminalScreen) -> Bool {
        lhs.session == rhs.session && lhs.cols == rhs.cols && lhs.rows == rhs.rows
            && lhs.revision == rhs.revision && lhs.lines == rhs.lines
            && lhs.cursor == rhs.cursor && lhs.cursorVisible == rhs.cursorVisible
            && lhs.title == rhs.title && lhs.exited == rhs.exited
            && lhs.history == rhs.history
    }

    mutating func apply(_ frame: TerminalFrame) {
        guard frame.available else {
            self = TerminalScreen()
            return
        }
        let reset = frame.session != session || frame.cols != cols
            || frame.rows != rows || frame.overflowed || lines.count != frame.rows
        if reset {
            session = frame.session
            cols = frame.cols
            rows = frame.rows
            lines = Array(repeating: [], count: max(0, frame.rows))
        }
        for change in frame.rowsChanged where change.y >= 0 && change.y < lines.count {
            lines[change.y] = change.runs
        }
        revision = frame.revision
        if frame.cursor.count >= 2 { cursor = (frame.cursor[0], frame.cursor[1]) }
        cursorVisible = frame.cursorVisible
        title = frame.title
        exited = frame.exited
        if let history = frame.history { self.history = history }
    }

    /// The plain text of one row — the spoken form and the tests' form.
    func text(_ y: Int) -> String {
        guard y >= 0, y < lines.count else { return "" }
        return lines[y].map(\.text).joined()
    }
}


/// The `mission` section of `/api/state`: whether Mission Control — the one
/// standing, read-only chief-of-staff session — is alive, which session it
/// is and where it lives (`BobDaemon.mission_snapshot`). `available` is
/// stated by the daemon and decodes false when the section is absent (an
/// older daemon); every other key through the tolerant helpers. Never a
/// handle: the panel aims its Comm column by `sessionId` alone.
struct MissionSection: Decodable, Equatable {
    var available = false
    var alive = false
    var exited = false
    var sessionId = ""
    var root = ""
    var name = ""
    var openedAt: Double = 0

    enum CodingKeys: String, CodingKey {
        case available, alive, exited, root, name
        case sessionId = "session_id"
        case openedAt = "opened_at"
    }

    init(available: Bool = false, alive: Bool = false, exited: Bool = false,
         sessionId: String = "", root: String = "", name: String = "",
         openedAt: Double = 0) {
        self.available = available
        self.alive = alive
        self.exited = exited
        self.sessionId = sessionId
        self.root = root
        self.name = name
        self.openedAt = openedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.value(.available, false)
        alive = c.value(.alive, false)
        exited = c.value(.exited, false)
        sessionId = c.value(.sessionId, "")
        root = c.value(.root, "")
        name = c.value(.name, "")
        openedAt = c.value(.openedAt, 0)
    }
}

extension Agent {
    /// A row with only its session id set — what a terminal pane is handed
    /// for a session whose row is not on the snapshot yet (Mission Control
    /// in the seconds between its spawn and its first hook). Every other
    /// field is the decoder's default, so nothing here says more than the
    /// snapshot would.
    static func stub(sessionId: String) -> Agent {
        Agent(stubSessionId: sessionId)
    }
}
