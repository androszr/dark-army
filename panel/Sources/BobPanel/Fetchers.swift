import Foundation

extension DaemonClient {
    /// The Checks section: every enrolled project's manual checks, open
    /// first then newest first, narrowed by a search and a status word.
    /// `knowledgeReport(root:)`'s shape: the token rides `request(_:)`, a
    /// 403 is noted, anything else is `nil`.
    func manualChecks(root: String = "", query: String = "",
                      status: String = "") async -> ManualChecksReport? {
        var parts: [String] = []
        for (key, value) in [("root", root), ("q", query), ("status", status)]
        where !value.isEmpty {
            guard let encoded = value.addingPercentEncoding(
                withAllowedCharacters: Self.knowledgeRootUnreserved)
            else { return nil }
            parts.append("\(key)=\(encoded)")
        }
        let path = "/api/manual-checks"
            + (parts.isEmpty ? "" : "?" + parts.joined(separator: "&"))
        var req = request(path)
        req.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return nil
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        noteAuthRefused(code)
        guard code == 200 else { return nil }
        return try? JSONDecoder().decode(ManualChecksReport.self, from: data)
    }

    /// One check file's text. The daemon re-checks the place and the shape;
    /// outside an enrolled project's folder the answer is `available: false`
    /// with the reason in words.
    func manualCheckText(path: String) async -> ManualCheckDocument? {
        guard !path.isEmpty,
              let encoded = path.addingPercentEncoding(
                withAllowedCharacters: Self.knowledgeRootUnreserved)
        else { return nil }
        var req = request("/api/manual-checks?path=\(encoded)")
        req.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            return nil
        }
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        noteAuthRefused(code)
        guard code == 200 else { return nil }
        return try? JSONDecoder().decode(ManualCheckDocument.self, from: data)
    }

    /// One frame of a Dark Army-owned terminal's screen: the rows dirtied since
    /// `since`, on loopback, with the pane's own size — this read is what
    /// sizes the pty, and the panel is the one owner of that size. Nil on
    /// any transport or decode failure; a 5s timeout so a hung daemon never
    /// wedges the pane's poll.
    func terminalFrame(session: String, since: Int, cols: Int, rows: Int,
                       sinceBytes: Int = 0) async -> TerminalFrame? {
        guard !session.isEmpty,
              let encoded = session.addingPercentEncoding(
                withAllowedCharacters: .alphanumerics) else { return nil }
        var req = request("/api/terminal?session=\(encoded)&since=\(since)&cols=\(cols)&rows=\(rows)&since_bytes=\(sinceBytes)")
        req.timeoutInterval = 5
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let frame = try? JSONDecoder().decode(TerminalFrame.self, from: data)
        else { return nil }
        return frame
    }

    /// Type one line into a Dark Army-owned terminal. `X-Bob-Token` on the
    /// request, set by `request()` — never a bearer header, which the daemon
    /// ignores and answers with a silent 403. The refusal is the daemon's
    /// own sentence.
    func terminalInput(session: String, text: String) async -> ActionResult {
        var req = request("/api/action")
        req.httpMethod = "POST"
        req.timeoutInterval = 10
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = ["action": "terminal_input",
                                      "session_id": session, "text": text]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (data, response) = try? await URLSession.shared.data(for: req),
              let http = response as? HTTPURLResponse
        else { return ActionResult(ok: false, detail: "Dark Army did not answer.") }
        let parsed = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let detail = parsed?["detail"] as? String ?? ""
        if http.statusCode == 200 { return ActionResult(ok: true, detail: detail) }
        return ActionResult(ok: false, detail: detail.isEmpty
                            ? "Dark Army refused it (HTTP \(http.statusCode))." : detail)
    }

    /// Raw keystrokes from the native terminal view. No extra Enter.
    func terminalBytes(session: String, data: Data) async -> ActionResult {
        var req = request("/api/action")
        req.httpMethod = "POST"
        req.timeoutInterval = 10
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = ["action": "terminal_input",
                                      "session_id": session,
                                      "bytes": data.base64EncodedString()]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        guard let (resp, response) = try? await URLSession.shared.data(for: req),
              let http = response as? HTTPURLResponse
        else { return ActionResult(ok: false, detail: "Dark Army did not answer.") }
        let parsed = (try? JSONSerialization.jsonObject(with: resp)) as? [String: Any]
        let detail = parsed?["detail"] as? String ?? ""
        if http.statusCode == 200 { return ActionResult(ok: true, detail: detail) }
        return ActionResult(ok: false, detail: detail.isEmpty
                            ? "Dark Army refused it (HTTP \(http.statusCode))." : detail)
    }

    /// Poll the account's limit windows. Separate from the SSE stream because
    /// `/api/events` fires on *structural* change and a session quietly burning
    /// budget makes none — the same reason the menu bar polls rather than waits.
    func refreshUsage() async {
        guard let (data, _) = try? await URLSession.shared.data(for: request("/api/usage")),
              let report = try? JSONDecoder().decode(UsageReport.self, from: data)
        else { return }
        usage = report.bars
    }

    /// Fetch the history report for a range.
    ///
    /// Deliberately **not** part of the SSE refresh loop and not prefetched: this
    /// is a full scan of `history.db` and takes seconds, not milliseconds, so it
    /// runs when someone actually asks for the History tab and holds its result
    /// until they ask for a different range. Re-entry is guarded — the range
    /// buttons are easy to drum on, and each press is a multi-second query.
    func loadHistory(range: String, root: String = "") async {
        // Superseded, not serialised. The old guard was `if case .loading
        // { return }`, which read "one at a time" but meant "the second range
        // you pick is ignored": `.task(id:)` cancels the in-flight fetch and
        // starts a new one, the new one bailed here, and then the cancelled
        // request's continuation wrote *its* result — 30-day figures under the
        // 90d button, or a `.failed` for a range nobody asked for, stuck until
        // a third range was picked. A repeat of the range already in flight is
        // still nothing to do.
        //
        // What it may **not** be keyed on is `historyRange` / `historyRoot`,
        // and that is the second bug this guard has had. Both are `@Published`
        // and both are written by the controls — the project picker binds
        // `$historyRoot` directly — so by the time `.task(id:)` re-fires and
        // calls in, the state already says what was just picked. The guard
        // then read "this exact fetch is in flight", found `.loading` from the
        // *previous* project, and returned; `.task(id:)` had already cancelled
        // that request and `publish` drops a late answer to a question nobody
        // is asking, so `historyLoad` stayed `.loading` for the life of the
        // panel and every later pick re-entered the same early return. The
        // report takes seconds, so picking a project during the first load was
        // the ordinary path into it.
        //
        // So the key is the request actually in flight, which no control can
        // write.
        let key = "\(range)\u{1}\(root)"
        if historyFetching == key { return }
        historyFetching = key
        defer { if historyFetching == key { historyFetching = nil } }
        historyRange = range
        historyRoot = root
        historyLoad = .loading
        // The project rides the query. Percent-encoded because a path may
        // carry a space, and an unescaped one would make the whole query
        // unparseable rather than merely wrong.
        let escaped = root.addingPercentEncoding(
            withAllowedCharacters: .alphanumerics) ?? ""
        var req = request("/api/history?range=\(range)"
                          + (escaped.isEmpty ? "" : "&root=\(escaped)"))
        // The stream must never time out, so `request` sets no interval at all;
        // a report that is merely slow still has to be allowed to finish, but
        // not forever.
        req.timeoutInterval = 90
        // Every write below goes through this: by the time a 90-second request
        // returns, the range on screen may be someone else's. A late answer to
        // a question nobody is asking any more is dropped, never published.
        func publish(_ state: HistoryLoad) {
            guard historyRange == range, historyRoot == root else { return }
            historyLoad = state
        }
        guard let (data, response) = try? await URLSession.shared.data(for: req) else {
            publish(.failed("The daemon did not answer."))
            return
        }
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            publish(.failed("History is unavailable (HTTP "
                            + "\((response as? HTTPURLResponse)?.statusCode ?? 0))."))
            return
        }
        guard let report = try? JSONDecoder().decode(HistoryReport.self, from: data) else {
            publish(.failed("History came back in a shape this panel could not read."))
            return
        }
        guard report.available else {
            publish(.failed("No history database yet — it fills as sessions run."))
            return
        }
        publish(.loaded(report))
    }
}
