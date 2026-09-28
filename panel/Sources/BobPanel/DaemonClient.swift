import Foundation

// MARK: - Client

/// Talks to the daemon. One reader, held open.
///
/// Every write carries the **desk token**, which arrives on the menu bar's
/// context push over stdin (`PanelContext.deskToken`) and is never read from
/// disk: the file on disk is the session token, which opens only closing a
/// terminal and a few reads. A 403 on a write asks the menu bar for the
/// context again (`noteAuthRefused` → `context_refresh`, at most once a
/// second), so a restarted daemon's fresh token costs one refused press,
/// never a restart.
@MainActor
final class DaemonClient: ObservableObject {
    /// Every writer — `apply`, the two `pending` releases, `loadDoneArchive`
    /// and any test that assigns it — feeds the board's own publisher here,
    /// after the carry-forward and the Done splice, so the feed never sees an
    /// absent section.
    @Published var snapshot = Snapshot() {
        didSet { boardFeed.take(snapshot) }
    }
    /// The board's own feed (`BoardFeed`): the board and its cards observe
    /// this, never the client, so an agents-only frame does not redraw them.
    let boardFeed = BoardFeed()
    @Published var connected = false
    @Published var usage: [UsageBar] = []
    @Published var historyLoad: HistoryLoad = .idle
    @Published var historyRange = "30d"
    /// Which project the History tab is asking about, `""` for every project.
    /// It rides the request rather than being sliced here: acceptance and
    /// rework are the board's arithmetic, scoped to one canonical root, and
    /// the desk cannot re-derive them from a machine-wide answer.
    @Published var historyRoot = ""
    /// The history request actually in flight, `"range\u{1}root"`, or nil.
    ///
    /// Deliberately **not** `@Published` and deliberately not derived from
    /// `historyRange` / `historyRoot`: it is `loadHistory`'s re-entry guard,
    /// and a guard keyed on state the range buttons and the project picker
    /// write themselves compares a fetch against the answer to "what did you
    /// just pick", which is always yes. See `loadHistory`.
    var historyFetching: String?
    /// Which harnesses the History tab is adding up. Both on by default; the
    /// report is fetched once and sliced here so a toggle is free, not another
    /// multi-second scan. The last remaining mark cannot be turned off.
    @Published var historyClaude = true
    @Published var historyGrok = true
    /// Set by the menu bar over stdin — things only it knows: whether the build
    /// is stale, and the Grok window, which it fetches itself.
    @Published var context = PanelContext()

    private let host = "127.0.0.1"
    private let port = 19874
    private var task: Task<Void, Never>?
    /// The limit-bar poll, which the stream cannot stand in for — see `start`.
    private var usageTask: Task<Void, Never>?

    /// Whether anyone is looking. **A hidden panel reads nothing and draws
    /// nothing.**
    ///
    /// The panel is launched once and driven over stdin, so it spends nearly all
    /// of its life ordered out — and it used to hold the stream open through all
    /// of it, publishing every frame into a window nobody could see. That is not
    /// a free assignment: SwiftUI relaid the whole list, re-parsed every row's
    /// markdown and ran `NSHostingView.layout()` for each one. Measured on a
    /// fleet of a dozen sessions at 2–5 frames a second: **~30% of a core,
    /// sustained, while hidden**, with `WindowServer` busy beside it — enough to
    /// make the whole machine feel slow.
    ///
    /// Not publishing was only half of it. Even with the drawing cut, decoding
    /// 33 KB of JSON several times a second off `URLSession.bytes.lines` — an
    /// async sequence that walks the response a byte at a time — still cost
    /// ~15%. So the stream itself goes: hidden, the panel closes it and holds
    /// nothing open at all. Showing reattaches, and the daemon's attach frame
    /// — a whole `state()` on every `/api/events` connect — is the open's
    /// fetch, so what appears is current rather than whatever was last pushed.
    /// Two writers since the window became ordinary (23 Aug 2026): the stdin
    /// fast path (`show`/`hide`/close) stays primary, and the delegate's
    /// `windowDidChangeOcclusionState` writes the platform's own "is anyone
    /// able to see this" answer into the same flag — so a fully covered or
    /// minimised window is as cheap as an ordered-out one at this gate. The
    /// covered edge is written only after `OcclusionGrace.seconds`; that
    /// grace is the delegate's state, not this client's. Both writers are
    /// idempotent against each other through `didSet`'s `oldValue` guard;
    /// do not add a second flag.
    /// Published because the views read it too: the once-a-second clocks
    /// (`ticking:`, the inbox's "waiting for") and, through
    /// `\.agentChatterRunning` set in `PanelView`, every chatter clock pause
    /// on it. (The avatars once gated their animation clock on it; they are
    /// stills now.)
    @Published var visible = true {
        didSet {
            guard visible != oldValue else { return }
            if visible {
                if let held = pending { pending = nil; snapshot = held }
                start()
            } else if !boardOpen {
                stop()
            }
        }
    }

    /// The **board window is open**, which is a second pair of eyes on the same
    /// snapshot.
    ///
    /// `visible` is about the panel, and it was the only thing `decode` asked
    /// before deciding whether to publish — so with the panel ordered out (which
    /// is nearly always: it is launched once and driven over stdin) the board
    /// drew a snapshot frozen at whatever was last applied, and the stream was
    /// not even attached. Every board write landed in the database and nothing
    /// on screen moved: a deleted card sat there, a moved card stayed in its
    /// column, and `refresh()` after the write parked its answer in `pending`
    /// like the rest. It read exactly like a press that did not register.
    @Published var boardOpen = false {
        didSet {
            guard boardOpen != oldValue else { return }
            if boardOpen {
                if let held = pending { pending = nil; snapshot = held }
                start()
            } else if !visible {
                stop()
            }
        }
    }

    /// The newest frame that arrived while hidden — only ever one that raced the
    /// stream's own teardown. One slot: they supersede each other, and a queue
    /// would replay a fleet's whole afternoon on open.
    private var pending: Snapshot?
    /// The last version of every omittable section the daemon actually sent,
    /// carried forward into slim frames that omitted an unchanged one. Held as
    /// a whole `Snapshot` so each section keeps its own type; only the sections
    /// are ever read out of it. Never a recomputation — always the daemon's own
    /// section, verbatim.
    private var carried = Snapshot()
    /// Per section, the `generated_at` of the frame that last carried it. Only
    /// `agents` is read today (it rides out as `Snapshot.agentsStamp`, which is
    /// what every "how long" on a row is aged from), but the stamp is kept for
    /// all of them because the alternative is a second, partial rule.
    private var carriedStamps: [Snapshot.Section: Double] = [:]
    /// The finished column, fetched on demand and held. The frame this client
    /// subscribes to carries only the finished cards that still want a person
    /// (the review-only opt-in), and `apply` splices this back in, so a view
    /// downstream still reads a complete `board.cards`.
    let doneArchive = DoneArchive()
    /// The last snapshot handed to the screen, kept so a fetch that lands
    /// between frames can re-splice it rather than leaving the column short
    /// until the next push.
    private var lastApplied: Snapshot?
    /// The last payload applied (or held), minus its clock — what the dedup in
    /// `prepareFrame` measures each arrival against. Main-actor state like the rest
    /// of this client, so no lock.
    private var lastAppliedPayload = ""

    /// The desk token, off the last context push. `""` until the first push
    /// lands; a write sent before then is refused and asks for the context.
    private var token: String { context.deskToken }

    /// When the last `context_refresh` went out — a burst of 403s (every
    /// write on a board refresh) asks the menu bar once a second, not once
    /// per refusal.
    private var lastContextRefresh: Date?

    /// Ask the menu bar to push the context again, which carries the current
    /// desk token. Debounced to one request per second.
    private func requestContextRefresh() {
        let now = Date()
        if let last = lastContextRefresh, now.timeIntervalSince(last) < 1 { return }
        lastContextRefresh = now
        Panel.send(action: "context_refresh")
    }

    // MARK: - The finished column
    //
    // Here rather than in `BoardClient.swift` because it re-splices the
    // applied snapshot, which is this file's private state.

    /// Fetch the finished column and put it on screen.
    ///
    /// Re-splices the last applied snapshot rather than waiting for the next
    /// frame: the archive routinely lands in a quiet second, and a column that
    /// filled in only when something else moved would read as broken.
    func loadDoneArchive() async {
        guard let report = await doneArchiveReport(), report.available else {
            doneArchive.failed()
            return
        }
        doneArchive.adopt(report)
        guard var snap = lastApplied else { return }
        snap.board.cards = DoneArchive.splice(snap.board.cards,
                                              archive: doneArchive.cards)
        lastApplied = snap
        if visible || boardOpen {
            snapshot = snap
        } else {
            pending = snap
        }
    }

    /// Ask again after a failed fetch — the Retry button's whole action.
    func retryDoneArchive() {
        doneArchive.retry { [weak self] in await self?.loadDoneArchive() }
    }

    /// A write came back 403: the desk token on hand is not the daemon's
    /// current one (a restart mints a fresh one) or has not arrived yet. Ask
    /// the menu bar for the context again; the press after this one carries it.
    func noteAuthRefused(_ code: Int) {
        if code == 403 { requestContextRefresh() }
    }

    /// The pane's stream: one socket per open terminal, carrying the same
    /// token the writes carry (`TerminalStreamConnection`).
    func terminalStream(session: String) -> TerminalStreamConnection {
        TerminalStreamConnection(host: host, port: port, token: token, session: session)
    }

    /// The stream came back 403: same rule as `noteAuthRefused`.
    func noteStreamAuthRefused() { requestContextRefresh() }

    func request(_ path: String) -> URLRequest {
        var req = URLRequest(url: URL(string: "http://\(host):\(port)\(path)")!)
        // `X-Bob-Token`, not `Authorization: Bearer`. The daemon gates *writes*
        // on this exact header (plus an Origin check), so a Bearer token read
        // fine — GETs are ungated — and every action came back 403. That is why
        // Jump silently did nothing.
        req.setValue(token, forHTTPHeaderField: "X-Bob-Token")
        req.timeoutInterval = 0          // SSE must not time out mid-stream
        return req
    }

    func start() {
        // Nothing to read for a window nobody is looking at — see `visible`,
        // which is what starts this again.
        guard visible || boardOpen, task == nil else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                await self?.readEvents()
                // The daemon restarts (rebuilds, upgrades) and the panel should
                // simply reattach rather than needing to be reopened.
                try? await Task.sleep(for: .seconds(2))
            }
        }
        // The limit bars are the one thing on screen the stream cannot refresh.
        // `/api/events` fires on *structural* change and a fleet quietly burning
        // through a five-hour window makes none, so `refreshUsage` used to run
        // only when the panel was shown and when the stream reopened — and the
        // stream stays open for as long as the panel does. On a panel left up,
        // the footer sat on the percentage it was opened with while the header
        // chip beside it, which is recomputed from every snapshot's rows, went
        // on counting: two figures for one window, hours apart. Same interval as
        // the menu bar's own poll, and for the same reason it polls at all.
        usageTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(30))
                guard !Task.isCancelled else { return }
                await self?.refreshUsage()
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
        usageTask?.cancel()
        usageTask = nil
    }

    /// One pass of the SSE stream, ending whenever the connection does.
    ///
    /// No `/api/state` read before the attach: the daemon's first frame on
    /// every connect is a whole `state()` (`_serve_events`), decoded off the
    /// main actor in `consumeStream`. A second read here parsed the whole
    /// fleet on main, and — `GET /api/state` ignoring `?done=review` — never
    /// matched the attach frame's bytes, so both were applied.
    private func readEvents() async {
        // The limit bars ride beside the attach, never ahead of it: with the
        // attach frame the only fleet read, a usage fetch awaited here held
        // the fleet behind `/api/usage` — seconds on a cold cache.
        Task { [weak self] in await self?.refreshUsage() }
        do {
            // `?sections=changed`: any top-level section that is unchanged,
            // clocks aside, arrives absent, and `apply` carries the last one
            // seen forward. An older daemon splits the query off and ignores it
            // (it has since before the board existed), so full frames arrive
            // and the fill-forward never triggers — degradation in either
            // direction is "no improvement", never "wrong board".
            // The second, independent opt-in, on the same query: the
            // finished cards nobody is waiting on are left off every frame
            // and fetched once by `doneArchiveReport()`. An older daemon
            // ignores it the same way it ignores `sections`, so full frames
            // arrive, the splice finds every id already present and does
            // nothing.
            // The third, `cards=delta`: a board whose only change is some
            // cards arrives as those cards plus `card_order`, and `apply`
            // merges them into the board it holds. An older daemon ignores
            // it and sends whole boards, where `cardsDelta` decodes false.
            let (bytes, response) = try await URLSession.shared
                .bytes(for: request("/api/events?sections=changed&done=review&cards=delta"))
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? -1
                Trace.log("sse refused http=\(code)")
                connected = false
                return
            }
            connected = true
            Trace.log("sse open")
            // One `data:` line is one snapshot — dispatched here rather than on
            // the blank line that ends an SSE event, because **`bytes.lines`
            // never yields the blank line**. Measured against the live daemon:
            // five events arrive as five `data:` lines and nothing else, so the
            // `line.isEmpty` branch this loop used to dispatch from could not
            // fire, the payload accumulated forever and not one snapshot was
            // ever decoded. The panel looked alive only because `refresh()`
            // runs on every open — it was polling, not streaming, and a fleet
            // that changed while the panel was up never reached the screen.
            //
            // Safe because the daemon frames one event per line by
            // construction: `_serve_events` writes `data: ` + `json.dumps(...)`,
            // which cannot contain a raw newline. A multi-line `data:` event
            // would need the blank-line dispatch, and would need a byte-level
            // reader to see it.
            //
            // The read, the clock-strip and the JSON decode all run **off the
            // main actor** (`consumeStream` is nonisolated): this class is
            // `@MainActor`, so iterating `bytes.lines` from here walked the
            // response a byte at a time on the main thread and decoded each
            // ~78 KB frame there too, at up to 5 frames a second. Only the
            // decoded `Snapshot` hops back to main (`apply`).
            try await consumeStream(bytes)
            // The stream ended without an error: the daemon closed it, or went
            // away. Worth a line of its own — a panel that stops updating and a
            // panel that is up to date look identical, and this is the moment
            // that tells them apart.
            Trace.log("sse ended")
        } catch {
            // Includes ordinary cancellation, which is why this is not a
            // failure: `stop()` cancels the task on the way out.
            Trace.log("sse dropped: \(error.localizedDescription)")
        }
        connected = false
    }

    /// One payload, normalized and decoded, ready for `apply`.
    struct PreparedFrame {
        let normalized: String
        let snapshot: Snapshot
        /// Whether the decode ran on the main thread — never, by design;
        /// carried so a test (and the verbose trace) can say so.
        let decodedOnMain: Bool
    }

    /// Strip the clocks, dedupe against `lastApplied`, decode — for either
    /// source (`refresh` and `consumeStream`). **Nonisolated and async**, so
    /// a call from this `@MainActor` class runs on the cooperative pool
    /// (SE-0338), not on the main thread: a whole-fleet `/api/state` parse
    /// was a main-thread stall on every board write's refresh. If the
    /// package ever adopts `NonisolatedNonsendingByDefault` (SE-0461) this
    /// would inherit the caller's actor; `decodedOnMain` is what the test
    /// reads to catch that.
    ///
    /// Dedup *before* the decode. Safe against the old "never instead of it"
    /// rule because only an *applied* payload ever lands in
    /// `lastAppliedPayload` — a frame byte-identical (clocks stripped) to one
    /// that decoded fine cannot be the schema disagreement the REJECTED line
    /// exists for. Many pushes carry no news (measured 2026-08-23: 13 of 32
    /// frames in 60s were byte-identical once the clocks were stripped), and
    /// every applied snapshot rebuilds the view graph, which is what
    /// cancelled clicks mid-press. `apply` re-checks on the main actor and
    /// stays the authoritative dedupe.
    nonisolated static func prepareFrame(_ text: String, lastApplied: String,
                                         via source: String) async -> PreparedFrame? {
        let normalized = strippingGeneratedAt(text)
        if normalized == lastApplied {
            if Trace.verbose {
                Trace.log("\(source) snapshot duplicate skipped")
            }
            return nil
        }
        guard let snap = parseFrame(text, via: source) else { return nil }
        return PreparedFrame(normalized: normalized, snapshot: snap,
                             decodedOnMain: onMainThread())
    }

    /// Whether an off-actor decode was overtaken: anything applied between
    /// the read of `lastAppliedPayload` and the decode's return came from the
    /// stream, which is at least as new as a `/api/state` read that started
    /// before it, so the older read is dropped rather than applied over it.
    nonisolated static func overtaken(before: String, now: String) -> Bool {
        before != now
    }

    /// `Thread.isMainThread` asked from a synchronous frame — the SDK marks
    /// it unavailable from async contexts.
    nonisolated private static func onMainThread() -> Bool { Thread.isMainThread }

    /// Move one section between two snapshots, verbatim. The one place the
    /// ten-plus sections are enumerated on this side; `Snapshot.Section` mirrors
    /// the daemon's `_OMITTABLE_SECTIONS`, and a section added there without
    /// a case here would simply never be carried.
    nonisolated static func copy(_ section: Snapshot.Section,
                                from source: Snapshot,
                                into target: inout Snapshot) {
        switch section {
        case .counts: target.counts = source.counts
        case .notifications: target.notifications = source.notifications
        case .agents: target.agents = source.agents
        case .signals: target.signals = source.signals
        case .collaboration: target.collaboration = source.collaboration
        case .permissions: target.permissions = source.permissions
        case .board: target.board = source.board
        case .enrollment: target.enrollment = source.enrollment
        case .devices: target.devices = source.devices
        case .inbox: target.inbox = source.inbox  // case inbox
        case .security: target.security = source.security  // case security
        case .mission: target.mission = source.mission  // case mission
        }
    }

    /// Parse one payload into a snapshot — no actor state touched, so the SSE
    /// reader runs it off the main thread.
    ///
    /// `try?` here has always been able to discard a payload in total silence,
    /// which is the one failure that looks exactly like a healthy panel showing
    /// an old state — so a rejected payload says so, with the first of it
    /// kept: a decode that fails is a schema disagreement between a daemon and a
    /// panel from different builds, and the field it tripped on is in the text.
    nonisolated private static func parseFrame(_ text: String,
                                               via source: String) -> Snapshot? {
        guard let data = text.data(using: .utf8) else {
            Trace.log("\(source) snapshot not utf8 (\(text.count) chars)")
            return nil
        }
        do {
            return try JSONDecoder().decode(Snapshot.self, from: data)
        } catch {
            Trace.log("\(source) snapshot REJECTED \(data.count)B: \(error)")
            return nil
        }
    }

    /// The SSE loop, deliberately **nonisolated**: `bytes.lines` walks the
    /// response a byte at a time and each frame is ~78 KB of JSON at up to 5
    /// frames a second — run from this `@MainActor` class it all landed on
    /// the main thread. Here the read, the clock-strip, the dedupe and the
    /// decode stay off-main; only the decoded `Snapshot` crosses to the actor.
    ///
    /// The dedupe compares against a local copy of `lastAppliedPayload`,
    /// seeded once per connection — `apply` re-checks authoritatively on the
    /// main actor, so a `refresh()` interleaving can only cost a decode,
    /// never apply a stale frame twice.
    nonisolated private func consumeStream(_ bytes: URLSession.AsyncBytes) async throws {
        var lastNormalized = await MainActor.run { lastAppliedPayload }
        for try await line in bytes.lines {
            // `: keep-alive` comment frames land here too, and are skipped.
            guard line.hasPrefix("data:") else { continue }
            let text = String(line.dropFirst(5).trimmingCharacters(in: .whitespaces))
            // Dedup before decoding — see `prepareFrame` for why that is safe.
            guard let prepared = await Self.prepareFrame(
                text, lastApplied: lastNormalized, via: "sse") else { continue }
            lastNormalized = prepared.normalized
            await apply(prepared.snapshot, normalized: prepared.normalized, via: "sse")
        }
    }

    /// Publish one decoded snapshot — the only stream step that needs the
    /// main actor. Internal rather than private as the test seam:
    /// `BoardFeedTests` feeds `prepareFrame`'s output through here, the
    /// stream's own route, carry-forward and splice included.
    func apply(_ snap: Snapshot, normalized: String, via source: String) {
        var snap = snap
        // A delta board (`?cards=delta`) carries only the cards that moved:
        // merge them into the held wire list **before** the carry loop, so
        // `carried` stores a whole board, the Done splice below sees a whole
        // list, and nothing downstream ever meets a delta. `carried.board`
        // is the unspliced wire list — the carry copy runs before the splice
        // — which is exactly the list the daemon's delta was taken against.
        if !snap.carried.contains(.board), snap.board.cardsDelta {
            let (cards, missing) = BoardCardDelta.merge(
                order: snap.board.cardOrder, changed: snap.board.cards,
                held: carried.board.cards)
            snap.board.cards = cards
            snap.board.cardsDelta = false
            snap.board.cardOrder = []
            if !missing.isEmpty {
                // The held board is not the one the daemon assumed. Say so,
                // and ask for a whole snapshot once rather than draw a board
                // with holes in it for longer than one frame.
                Trace.log("\(source) board delta REJECTED \(missing.count) unknown ids")
                Task { [weak self] in await self?.refresh() }
            }
        }
        // A slim frame omitted every section that had not changed — carry the
        // last one the daemon sent forward, so the applied snapshot is always
        // complete and no view learns about optionality. A frame that carried
        // a section becomes the new fill for it, stamped with that frame's
        // clock. A frame whose section was present but undecodable was
        // REJECTED whole in `parseFrame` and never reaches here, so a carry is
        // only ever an *absent* section (see `Snapshot.carried`).
        for section in Snapshot.Section.allCases {
            if snap.carried.contains(section) {
                Self.copy(section, from: carried, into: &snap)
            } else {
                Self.copy(section, from: snap, into: &carried)
                carriedStamps[section] = snap.generatedAt
            }
        }
        // The one stamp anything downstream reads. Equal to `generatedAt` on
        // any frame that carried the fleet — so a full-frame client and an
        // older daemon are the identical case, and `AgentFacts.aged` adds
        // zero there.
        snap.agentsStamp = carriedStamps[.agents] ?? snap.generatedAt
        snap.collaborationStamp = carriedStamps[.collaboration] ?? snap.generatedAt
        // The authoritative dedupe. Every `"generated_at"` was stripped before
        // comparing — the top level's and the board section's, when a frame
        // carries one — since they move on every push whether or not anything
        // else did. Slim and full byte-shapes differ, which only lowers the
        // hit-rate: it decays toward zero rather than hiding news, the safe
        // direction to fail. Before the visibility branch, so a hidden panel
        // dedups too instead of re-holding the same frame.
        //
        // Deliberately measured on the *wire* payload rather than on the
        // composed carried-forward snapshot: an idle slim frame now normalises
        // to a constant string, so it is skipped before any decode and any
        // relayout — the second half of this saving. Composing first would
        // require decoding the frame, which is the expensive half the off-main
        // dedupe exists to skip, and would put a rebuild back on every second.
        // The consequence is that no clock on screen may depend on
        // `snapshot.generatedAt` advancing; each is aged from an absolute
        // stamp against the panel's own second hand instead.
        if normalized == lastAppliedPayload {
            if Trace.verbose {
                Trace.log("\(source) snapshot duplicate skipped \(snap.digest)")
            }
            return
        }
        lastAppliedPayload = normalized
        // Put the held finished cards back before anything downstream sees
        // the snapshot: the board's columns, the inbox, the pipeline band and
        // the Clear Done gate all read `board.cards` and none of them knows
        // the wire withheld anything. After the carry-forward and after the
        // dedupe `return`, so a skipped duplicate cannot blank the column.
        snap.board.cards = DoneArchive.splice(snap.board.cards,
                                              archive: doneArchive.cards)
        lastApplied = snap
        // And ask, against this frame's two tokens, whether what is held is
        // still what the store holds.
        doneArchive.consider(clear: snap.board.doneClearToken,
                             view: snap.board.doneViewToken) { [weak self] in
            await self?.loadDoneArchive()
        }
        // `boardOpen` counts as looking: the board is a window in this
        // process reading the same snapshot, and holding the frame back
        // freezes it (see `boardOpen`).
        if visible || boardOpen {
            snapshot = snap
        } else {
            pending = snap
        }
        if Trace.verbose {
            Trace.log("\(source) snapshot \(snap.digest)"
                      + (visible || boardOpen ? "" : " (held: hidden)"))
        }
    }

    /// Drop every `"generated_at": <number>` pair (and one trailing comma) from
    /// a JSON payload — a single forward scan doing exactly what the
    /// `.regularExpression` scrub it replaces did
    /// (`"generated_at"\s*:\s*[0-9.]+\s*,?`): an NSRegularExpression pass over
    /// a ~78 KB string on every frame was a measurable slice of the hidden
    /// panel's idle cost. Same output on the same input — an occurrence not
    /// followed by a colon and a number is kept, as the regex would have
    /// left it.
    nonisolated static func strippingGeneratedAt(_ text: String) -> String {
        let key = "\"generated_at\""
        guard text.contains(key) else { return text }
        var out = ""
        out.reserveCapacity(text.utf8.count)
        var cursor = text.startIndex
        while let hit = text.range(of: key, range: cursor..<text.endIndex) {
            var i = hit.upperBound
            func skipSpace() {
                while i < text.endIndex,
                      text[i] == " " || text[i] == "\t"
                        || text[i] == "\n" || text[i] == "\r" {
                    i = text.index(after: i)
                }
            }
            skipSpace()
            guard i < text.endIndex, text[i] == ":" else {
                out += text[cursor..<hit.upperBound]
                cursor = hit.upperBound
                continue
            }
            i = text.index(after: i)
            skipSpace()
            let numberStart = i
            while i < text.endIndex, "0123456789.".contains(text[i]) {
                i = text.index(after: i)
            }
            guard i > numberStart else {
                out += text[cursor..<hit.upperBound]
                cursor = hit.upperBound
                continue
            }
            skipSpace()
            if i < text.endIndex, text[i] == "," { i = text.index(after: i) }
            out += text[cursor..<hit.lowerBound]
            cursor = i
        }
        out += text[cursor..<text.endIndex]
        return out
    }


    /// Fetch the state outright. Called after a board write and by the
    /// access-log window; the open's and the reconnect's read is the stream's
    /// attach frame, decoded in `consumeStream`, so this is no longer on
    /// either path. It swallowed both of its failures once, which is why a
    /// panel showing a minute-old fleet was indistinguishable from a current
    /// one — so each says so. The strip, dedupe and decode run off the main
    /// actor (`prepareFrame`); only `apply` is on it.
    func refresh() async {
        // Read before the request goes out, not after it returns: a stream
        // frame applied while the GET is in flight is newer than its answer.
        let last = lastAppliedPayload
        do {
            let (data, response) = try await URLSession.shared
                .data(for: request("/api/state"))
            let code = (response as? HTTPURLResponse)?.statusCode ?? 200
            guard code == 200 else {
                Trace.log("state refused http=\(code)")
                return
            }
            guard let text = String(data: data, encoding: .utf8) else {
                Trace.log("state snapshot not utf8 (\(data.count)B)")
                return
            }
            // Funnelled through the same `prepareFrame` and `apply` as the
            // stream, so a refresh obeys the same dedup and the same
            // hidden-hold — a poll that reapplied an identical snapshot would
            // relayout the panel for nothing, which is the habit this client
            // just gave up.
            connected = true
            guard let prepared = await Self.prepareFrame(
                text, lastApplied: last, via: "state") else { return }
            // The fetch and the decode were off the actor, so a stream frame
            // may have landed meanwhile — and it is newer than this read. Applying ours over
            // it would regress the screen, and a slim stream does not resend
            // the sections it thinks we already hold.
            if Self.overtaken(before: last, now: lastAppliedPayload) {
                Trace.log("state snapshot dropped: a newer frame landed during fetch or decode")
                return
            }
            if Trace.verbose {
                Trace.log("state snapshot decoded on-main=\(prepared.decodedOnMain)")
            }
            apply(prepared.snapshot, normalized: prepared.normalized, via: "state")
        } catch {
            Trace.log("state failed: \(error)")
        }
    }

    /// POST to `/api/action`. The daemon answers 409 when it refuses (a stop
    /// whose PID is no longer a Claude process, say) and puts its reason in
    /// `detail`. That reason is carried back rather than reduced to a Bool: on a
    /// refusal *nothing else on screen moves*, so a discarded 409 is
    /// indistinguishable from a slow success and the user presses again. Nothing
    /// here retries — every action is a deliberate press rather than something
    /// to insist on.
    @discardableResult
    func act(_ action: String, session: String) async -> ActionResult {
        await post(["action": action, "session_id": session])
    }

    /// Answer a tool-approval prompt a channel relayed out of a session. Keyed
    /// by the prompt rather than by the session: the same session can have only
    /// one open at a time today, but the id is what the harness matches on, and
    /// a verdict aimed at a prompt that has since been answered at the terminal
    /// must miss rather than land on its successor.
    func answer(_ requestId: String, allow: Bool) async -> ActionResult {
        await post(["action": "permission_verdict",
                    "request_id": requestId,
                    "behavior": allow ? "allow" : "deny"])
    }

    /// Say something to a live session, as the user. Works only for a session
    /// that ended its turn and is idle — see `RowActions.reply`.
    func reply(_ session: String, text: String) async -> ActionResult {
        await post(["action": "reply", "session_id": session, "text": text])
    }

    /// Close this session's VS Code terminal tab — the tab and the agent both
    /// go. See `BobDaemon.close_session_terminal`.
    ///
    /// The extra key below is the assertion that a human pressed the button,
    /// and it is what separates this from `close-out.sh`'s identical action:
    /// only a person's press finishes the session's board card.
    func closeTerminal(_ session: String) async -> ActionResult {
        await post(["action": "close_terminal", "session_id": session,
                    "by_person": "1"])
    }

    /// Switch a rate-limited Claude session to low priority
    /// (`BobDaemon.low_priority_session`): `/low-priority` typed onto its
    /// input line, then the card dropped. A 409 carries the refusal in words.
    func lowPriority(_ session: String) async -> ActionResult {
        await post(["action": "low_priority", "session_id": session])
    }

    /// Choose an option of the `AskUserQuestion` a session is stopped on. The
    /// daemon types digit-then-Enter into the session's own terminal — see
    /// `BobDaemon.answer_question` for the route and the refusals. The
    /// question id rides along so an answer to a question that has since been
    /// dealt with at the terminal misses rather than landing on its successor.
    func answerQuestion(_ session: String, questionId: String,
                        index: Int) async -> ActionResult {
        await post(["action": "answer_question", "session_id": session,
                    "question_id": questionId, "option_index": String(index)])
    }

    /// Answer every question of a dialog in one ordered burst — see
    /// `BobDaemon.answer_questions`. `post` is `[String: String]`, so the
    /// choices ride as text: one group per question in dialog order joined
    /// with `,`, the several picks of a multi-select question joined with
    /// `+` ("0+2,1"), 0-based. A pick-one question is a group of one, so a
    /// dialog with no multi-select question posts exactly what it always did
    /// ("1,0,2"). The daemon re-checks the count, every index and every
    /// group's shape against the dialog *it* holds before the first keystroke.
    func answerQuestions(_ session: String, questionId: String,
                         indexes: [[Int]]) async -> ActionResult {
        await post(["action": "answer_questions", "session_id": session,
                    "question_id": questionId,
                    "option_indexes": indexes
                        .map { $0.map(String.init).joined(separator: "+") }
                        .joined(separator: ",")])
    }

    /// Close one burst alert off the phone doors' access log. Clear-never-set:
    /// the log entry stays.
    func accessAlertAck(id: String) async -> ActionResult {
        await post(["action": "access_alert_ack", "id": id])
    }

    /// The access log, newest first — `knowledgeReport`'s shape: a
    /// token-gated GET, decoded tolerantly, nil on any refusal.
    func accessLogReport(since: Double? = nil, limit: Int = 500) async -> AccessLogReport? {
        var query = "limit=\(max(1, limit))"
        if let since, since >= 0 { query += "&since=\(since)" }
        var req = request("/api/access-log?\(query)")
        req.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return nil
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        noteAuthRefused(code)
        guard code == 200 else { return nil }
        return try? JSONDecoder().decode(AccessLogReport.self, from: data)
    }

    func post(_ body: [String: String]) async -> ActionResult {
        await postAny(body)
    }

    /// `post`'s sibling for a body that is not all strings — the days value
    /// must reach the daemon as a real integer, which `[String: String]`
    /// cannot carry.
    func postAny(_ body: [String: Any]) async -> ActionResult {
        var req = request("/api/action")
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return ActionResult(ok: false, detail: "The daemon did not answer.")
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 200 { return ActionResult(ok: true, detail: "") }
        noteAuthRefused(code)
        if let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let detail = obj["detail"] as? String, !detail.isEmpty {
            // `current` rides the card guard's refusal and nothing else, so
            // it is carried rather than read here: the sheet decides what to
            // do with the store's own copy.
            return ActionResult(ok: false, detail: detail,
                                current: AreaWireFields.accepts(data)
                                    ? obj["current"] as? [String: Any] : nil)
        }
        Trace.log("action refused http=\(code)")
        return ActionResult(ok: false,
                            detail: ActionResult.refusalText(detail: ""))
    }
}

/// What the board and its cards observe instead of the whole client.
///
/// `DaemonClient.snapshot` is republished on every applied frame, and most
/// frames on a busy fleet are about agents alone — a tool changed, a row went
/// quiet. With the board observing the client, each of those re-ran the board
/// and every card on it though no card had moved. This holds the three
/// things the board draws from — the board itself, the fleet rows its cards
/// name, and the five board-wide facts a card face reads — and assigns each
/// **only when it differs** from what is held, so an agents-only frame that
/// touches no card-named session publishes nothing here.
///
/// Derived from the *applied* snapshot (`snapshot`'s `didSet`), never the
/// wire frame, so a slim frame's absent `board` is already carried forward.
@MainActor
final class BoardFeed: ObservableObject {
    @Published private(set) var board = Board()
    /// The fleet rows the cards name — a card's bound session, an
    /// agent-authored card's author, a live refinement's session — keyed by
    /// session id, `Agents.row(session:)`'s answer for each.
    @Published private(set) var rows: [String: FleetRow] = [:]
    @Published private(set) var chrome = BoardChrome(Board())

    func take(_ snap: Snapshot) {
        let board = snap.board
        var named = Set<String>()
        for card in board.cards {
            if !card.sessionId.isEmpty { named.insert(card.sessionId) }
            if card.isAgentAuthored { named.insert(card.author) }
            if card.refineState == "live", !card.refineSessionId.isEmpty {
                named.insert(card.refineSessionId)
            }
        }
        let rows = named.isEmpty
            ? [:] : snap.agents.index().filter { named.contains($0.key) }
        let chrome = BoardChrome(board)
        if board != self.board { self.board = board }
        if rows != self.rows { self.rows = rows }
        if chrome != self.chrome { self.chrome = chrome }
    }
}

/// The outcome of an `/api/action` press, with the daemon's own words for a
/// refusal. `detail` is empty on success — there is nothing to say when the
/// thing you asked for happened.
