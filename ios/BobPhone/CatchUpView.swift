import SwiftUI

struct CatchUpView: View {
    @EnvironmentObject private var sheets: PhoneSheetRouter
    @Environment(\.decryptFeedback) private var decryptFeedback
    @ObservedObject var client: PhoneClient
    var notificationItems: [DecisionItem]? = nil
    @State private var state = CatchUpState()
    @State private var page: CatchUpPage?
    @State private var loading = false
    /// A pull is out. `load(reset: true)` raises `loading`, which already
    /// draws the typed line, so the pull swaps that one line's words
    /// rather than adding a second line under it.
    @State private var refreshing = false
    @State private var offline = false
    @State private var project = ""
    @State private var window = "since"
    @State private var checkpoint = 0
    @State private var loadedGeneration = ""
    @State private var loadSequence = 0
    /// The phone's own word for a saved checkpoint, drawn under the button.
    /// Nothing goes to the Mac here — the cursor is `UserDefaults` — so the
    /// only feedback available is this line plus the button going inert.
    @State private var marked = ""
    /// The seen-notification rows unfolded in place (the words fallback).
    @State private var expandedEntries: Set<String> = []
    private var checkpointKey: String { "catchup.cursor." + PhoneRouter.shared.generation }
    private var items: [DecisionItem] {
        (notificationItems ?? state.items).filter { project.isEmpty || $0.root == project }
    }
    private var roots: [String] { Array(Set((notificationItems ?? state.items).map(\.root))).sorted() }

    /// One catch-up row as one sentence, from the three lines it already
    /// draws and nothing else.
    private func spoken(_ item: DecisionItem) -> String {
        let asked = item.questionText.isEmpty ? item.title : item.questionText
        let came = item.outcome.isEmpty
            ? (item.unresolved ? "Unresolved" : "Outcome not observed")
            : item.outcome
        let when = Date(timeIntervalSince1970: item.updatedAt).formatted()
        return [asked, came, item.project, when]
            .filter { !$0.isEmpty }.joined(separator: ", ")
    }

    var body: some View {
        Group {
        if client.status == .unpaired || (!loadedGeneration.isEmpty && loadedGeneration != PhoneRouter.shared.generation) {
            Text("Pair with Dark Army to read decision history.")
        } else { List {
            if notificationItems == nil {
                Picker("Show", selection: $window.decrypting(decryptFeedback)) {
                    Text("Since last caught up").tag("since")
                    Text("Today").tag("today")
                    Text("7 days").tag("week")
                    Text("Retained history").tag("all")
                }
                .onChange(of: window) { Task { await load(reset: true) } }
            }
            Picker("Project", selection: $project.decrypting(decryptFeedback)) {
                Text("All projects").tag("")
                ForEach(roots, id: \.self) { root in
                    // The path distinguishes two folders with the same name.
                    Text("\(label(root)) · \(root)").tag(root)
                }
            }
            if loading {
                AgentChatterView(.line, wait: refreshing ? .refreshing : .readingHistory,
                                 seed: window + project + (refreshing ? "/refresh" : ""),
                                 spoken: "Loading decisions")
                    .id(window + project + (refreshing ? "/refresh" : ""))
            }
            if offline {
                Text("Offline. Saved records will load when the Mac is reachable.")
                DecryptButton("Retry") { Task { await load(reset: state.upperCursor == nil) } }
                    .accessibilityLabel("Try loading the decision history again")
            }
            if offline || client.status != .live {
                seenOnThisPhone
            }
            if let page {
                if !page.available { Text(page.reason.isEmpty ? "Decision history is unavailable." : page.reason) }
                if page.historyGap { Text("Some older records are no longer available. Your previous caught-up position predates retained history.") }
                if page.coverage.storageGap { Text("History is incomplete: Dark Army could not save some events.") }
                Text("Up to \(page.coverage.retentionDays) days and \(page.coverage.eventLimit) events retained.")
                if page.coverage.observationStart > 0 {
                    Text("Observation began \(Date(timeIntervalSince1970: page.coverage.observationStart).formatted()). Earlier decisions are unavailable.")
                }
            }
            if !loading && !offline && items.isEmpty && (page?.available == true || notificationItems != nil) {
                Text("No decisions in this view.")
            }
            ForEach(items) { item in
                DecryptButton(action: { sheets.show(.decision(item)) }) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(item.questionText.isEmpty ? item.title : item.questionText)
                        Text(item.outcome.isEmpty ? (item.unresolved ? "Unresolved" : "Outcome not observed") : item.outcome)
                            .foregroundStyle(Theme.dim)
                        Text("\(item.project) · \(Date(timeIntervalSince1970: item.updatedAt).formatted())")
                            .font(Theme.mono(10)).foregroundStyle(Theme.faint)
                    }
                    // One decision, one sentence — the question, what came of
                    // it, and where. `.ignore` is legal here and only here:
                    // this sits on the DecryptButton's *label*, and the link
                    // itself supplies the activation.
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(spoken(item))
                }
                .buttonStyle(.plain)
            }
            if state.nextCursor != nil && notificationItems == nil {
                Text("Partial history loaded. More decisions remain below.")
                DecryptButton("Load more") { Task { await load(reset: false) } }.disabled(loading)
                    .accessibilityHint("Loads the decisions below these")
            }
            if notificationItems == nil {
                DecryptButton("Mark caught up") {
                    guard let cursor = state.checkpoint(project: project, timeFilter: window) else { return }
                    checkpoint = max(checkpoint, cursor)
                    UserDefaults.standard.set(checkpoint, forKey: checkpointKey)
                    marked = PhoneClient.catchUpMarkedLine
                }
                // The last term is "nothing left to mark": a cursor already
                // covered by the saved checkpoint. `checkpoint` is written by
                // the press itself, so the button goes inert on the same
                // frame the confirming line appears rather than sitting there
                // looking pressable for ever.
                .disabled(loading || offline
                          || (state.checkpoint(project: project, timeFilter: window)
                                .map { $0 <= checkpoint } ?? true))
                if !marked.isEmpty {
                    Text(marked)
                        .font(Theme.mono(10)).foregroundStyle(Theme.dim)
                }
                Text("Reading never answers a question. Mark caught up covers only the fully loaded all-project view.")
                    .font(Theme.mono(10)).foregroundStyle(Theme.faint)
            }
        }
        }}
        .font(Theme.mono(12))
        .scrollContentBackground(.hidden).background(Theme.bg)

        .decryptSurface("CatchUpView")
        .task {
            loadedGeneration = PhoneRouter.shared.generation
            checkpoint = UserDefaults.standard.integer(forKey: checkpointKey)
            if notificationItems == nil { await load(reset: true) }
        }
        .tint(Theme.phosphor)
        .refreshable {
            if notificationItems == nil {
                refreshing = true
                defer { refreshing = false }
                await load(reset: true)
            }
        }
    }

    private func label(_ root: String) -> String {
        (notificationItems ?? state.items).first { $0.root == root }?.project ?? root
    }

    /// The buzzes this phone saw, newest first, for a Mac out of reach: each
    /// row opens the card or agent it names from the held picture, or, when
    /// neither is in it any more, unfolds its own words in place. Read off
    /// the log this phone keeps; nothing here asks the Mac.
    @ViewBuilder private var seenOnThisPhone: some View {
        let seen = client.notificationLog.entries
        if !seen.isEmpty {
            if let asOf = client.pictureAsOf ?? seen.first.map({ Date(timeIntervalSince1970: $0.seenAt) }) {
                // The fleet's rule for the tail: "checking with the Mac"
                // until the poller has actually given up on it.
                HeldPictureBanner(asOf: asOf, reaching: client.status != .unreachable)
                    .listRowInsets(EdgeInsets())
            }
            PhoneSectionHeader(title: "NOTIFICATIONS SEEN ON THIS PHONE")
            ForEach(seen) { entry in
                DecryptButton(action: { openHeld(entry) }) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(entry.title.isEmpty ? "A notification" : entry.title)
                            .fixedSize(horizontal: false, vertical: true)
                        if !entry.subtitle.isEmpty {
                            Text(entry.subtitle).foregroundStyle(Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        TimelineView(.periodic(from: .now, by: 60)) { context in
                            Text("\(FleetAge.text(startedAt: entry.seenAt, now: context.date.timeIntervalSince1970)) ago")
                                .font(Theme.mono(10)).foregroundStyle(Theme.faint)
                        }
                        if expandedEntries.contains(entry.id) {
                            Text("arrived \(Date(timeIntervalSince1970: entry.seenAt).formatted())")
                                .font(Theme.mono(10)).foregroundStyle(Theme.faint)
                                .fixedSize(horizontal: false, vertical: true)
                            if !entry.kind.isEmpty {
                                Text(entry.kind).font(Theme.mono(10)).foregroundStyle(Theme.faint)
                            }
                            Text("The card or agent this was about is no longer in the picture this phone holds.")
                                .foregroundStyle(Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    // `.combine`, not `.ignore`: this sits on a DecryptButton's
                    // label and the press must survive the replacement.
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(entry.spoken(now: Date().timeIntervalSince1970))
                }
                .buttonStyle(.plain)
            }
        }
    }

    /// The row's press: the held card or agent opens as its own sheet; the
    /// words fallback unfolds the row where it is.
    private func openHeld(_ entry: NotificationLogEntry) {
        switch HeldDestination.resolve(entry: entry, snapshot: client.snapshot) {
        case .card(let card)?: sheets.show(.card(card))
        case .agent(let agent, let category)?: sheets.show(.agent(agent, category))
        case .words(_)?, .none:
            if expandedEntries.contains(entry.id) { expandedEntries.remove(entry.id) }
            else { expandedEntries.insert(entry.id) }
        }
    }
    private func load(reset: Bool) async {
        if !reset && loading { return }
        loadSequence += 1
        let sequence = loadSequence
        loading = true; offline = false
        defer { if sequence == loadSequence { loading = false } }
        marked = ""
        if reset { state = CatchUpState(); page = nil }
        var query = "limit=100&cursor=\(window == "since" ? checkpoint : 0)"
        if let next = state.nextCursor { query += "&page_cursor=\(next)" }
        if let upper = state.upperCursor { query += "&upper_cursor=\(upper)" }
        if window == "today" { query += "&since=\(Calendar.current.startOfDay(for: Date()).timeIntervalSince1970)" }
        if window == "week" { query += "&since=\(Date().timeIntervalSince1970 - 7 * 86400)" }
        guard let result = await client.fetchCatchUp(query: query), !Task.isCancelled,
              loadedGeneration == PhoneRouter.shared.generation, sequence == loadSequence else {
            if sequence == loadSequence && !Task.isCancelled { offline = true }
            return
        }
        page = result
        state.append(result)
    }
}

/// Re-evaluated for every fresh snapshot. An old destination never inherits a
/// replacement question's controls, even if it changes while this view is up.
struct DecisionDetailView: View {
    @Environment(\.phoneSheetEntry) private var sheetEntry
    @EnvironmentObject private var sheets: PhoneSheetRouter
    let item: DecisionItem
    @ObservedObject var client: PhoneClient
    private var currentAgent: Agent? {
        let a = client.snapshot.agents
        return (a.waiting + a.running + a.sleeping).first { $0.sessionId == item.sessionId }
    }
    var body: some View {
        if client.status == .unpaired {
            Text("Pair with Dark Army to read this decision.")
                .decryptSurface("unpaired decision")
        } else if !item.cardId.isEmpty, let card = client.snapshot.board.cards.first(where: { $0.id == item.cardId }) {
            PhoneCardDetailView(seed: card, client: client,
                                retained: sheetEntry?.cardDraft(for: card.id))
        } else if let agent = item.liveAgent(in: client.snapshot) {
            AgentDetailView(seed: agent, category: .waiting, client: client, targetRequestId: item.kind == "permission" ? item.sourceId : nil)
                .id(item.id)
        } else {
            List {
                Text(item.title)
                Text("\(item.project) · \(item.root)").foregroundStyle(Theme.dim)
                Text(Date(timeIntervalSince1970: item.openedAt).formatted())
                if !item.questionText.isEmpty { Text(item.questionText) }
                ForEach(Array(item.questions.enumerated()), id: \.offset) { _, question in
                    Text(question.text)
                    ForEach(Array(question.options.enumerated()), id: \.offset) { _, option in Text(option) }
                }
                Text(item.outcome.isEmpty ? "Outcome not observed." : item.outcome)
                Text(item.provenance).foregroundStyle(Theme.faint)
                Text("Saved record. This question or card is no longer the current target. No answer is submitted by opening it.")
                if item.truncated { Text("Some text was shortened to the storage limit.") }
                if let current = currentAgent {
                    DecryptButton(action: { sheets.show(.agent(current, .waiting)) }) {
                        Text("Open current work")
                    }
                    .buttonStyle(.plain)
                }
            }
            .font(Theme.mono(12)).scrollContentBackground(.hidden).background(Theme.bg)

        .decryptSurface("CatchUpView")
        }
    }
}

/// A tapped notification. Held first: `model.open()` composes the card or
/// agent the buzz names from the picture this phone already holds (or the
/// buzz's own words) before anything is asked of the Mac, and draws it under
/// the amber "as of" line; the live page replaces it the moment the Mac
/// answers. The held view consumes no receipt and retains nothing on the
/// sheet entry — both stay on the live page's branch, so `resolve()` keeps
/// running until the real page lands or Close is pressed.
struct NotificationDestinationView: View {
    @Environment(\.phoneSheetEntry) private var sheetEntry
    let route: PendingReceipt
    @ObservedObject var client: PhoneClient
    @ObservedObject private var router = PhoneRouter.shared
    var onAvailability: (Bool) -> Void = { _ in }
    @StateObject private var model: NotificationDestinationModel

    init(route: PendingReceipt, client: PhoneClient, onAvailability: @escaping (Bool) -> Void = { _ in }) {
        self.route = route
        self.client = client
        self.onAvailability = onAvailability
        _model = StateObject(wrappedValue: NotificationDestinationModel(
            route: route, log: client.notificationLog,
            snapshot: { client.snapshot },
            isLive: { client.status == .live },
            accepts: { PhoneRouter.shared.accepts(route) },
            refresh: { await client.refreshNow() },
            fetch: { await client.fetchCatchUp(query: $0) }))
    }

    private var page: CatchUpPage? { model.page }
    private var loading: Bool { model.loading }
    private var offline: Bool { model.offline }

    var body: some View {
            Group {
                if let page, page.available {
                    Group {
                        if page.items.count == 1, let item = page.items.first {
                            DecisionDetailView(item: item, client: client)
                        } else { CatchUpView(client: client, notificationItems: page.items) }
                    }.onAppear { router.consume(route) }
                } else if page == nil, let held = model.held {
                    // Only while no page has landed: a page the Mac answered
                    // with but marked unavailable (expired, an older Mac, no
                    // history) falls through to its reason below — the Mac
                    // has spoken, and a held view under "out of reach" would
                    // contradict it.
                    VStack(spacing: 0) {
                        HeldPictureBanner(
                            asOf: client.pictureAsOf ?? Date(timeIntervalSince1970: model.entry?.seenAt ?? 0),
                            reaching: loading)
                        HeldDestinationView(held: held, client: client)
                        if offline {
                            // A live Mac that answered nothing this once
                            // leaves something to press; with the radios
                            // off the press is harmless.
                            DecryptButton("Retry") { Task { await resolve() } }.disabled(loading)
                                .font(Theme.mono(12))
                                .padding()
                        }
                    }
                } else {
                    VStack(spacing: 16) {
                        if loading {
                            AgentChatterView(.line, wait: .opening, seed: route.receiptId,
                                             spoken: "Opening notification")
                                .id(route.receiptId)
                        }
                        if let page { Text(page.reason.isEmpty ? "This notification is unavailable." : page.reason) }
                        if offline && model.held == nil { Text("Offline. This notification will open after Dark Army reconnects.") }
                        if page == nil { DecryptButton("Retry") { Task { await resolve() } }.disabled(loading) }
                    }.padding().font(Theme.mono(12))
                    .decryptSurface("notification")
                }
            }
            .background(Theme.bg)
        .task {
            model.open()
            if let held = sheetEntry?.resolvedNotification(
                for: route, unlocked: router.unlocked,
                generation: router.generation, requestGeneration: router.requestGeneration) {
                model.page = held
            } else {
                await resolve()
            }
        }
        .onChange(of: client.snapshot.generatedAt) { _, _ in
            if page == nil && !loading { Task { await resolve() } }
        }
        // The Mac coming back is a reason of its own, even before a fresh
        // picture changes `generatedAt`.
        .onChange(of: client.status == .live) { _, live in
            if live && page == nil && !loading { Task { await resolve() } }
        }
        .interactiveDismissDisabled(page == nil || page?.available == false)
        .onChange(of: page?.available, initial: true) { _, available in
            onAvailability(available == true)
        }
    }
    private func resolve() async {
        guard router.accepts(route) else { return }
        guard let result = await model.resolve() else { return }
        guard !Task.isCancelled, router.accepts(route) else { return }
        sheetEntry?.rememberNotification(result, route: route,
                                         authorized: router.accepts(route),
                                         requestGeneration: router.requestGeneration)
    }
}
