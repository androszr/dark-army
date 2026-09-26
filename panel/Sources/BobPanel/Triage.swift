import AppKit
import SwiftUI

/// What a key press asks for, named for the intent rather than the key. The
/// mapping from keys to intents lives in `main.swift` beside the monitor that
/// reads them; everything below this line only ever sees the intent.
enum TriageIntent: Equatable {
    case up, down
    case left, right
    case nextTab, prevTab
    case back
    case open, jump, dismiss, stop, retire, wrapUp, focusFilter, clearFilter
    /// Show or hide the one-line shortcut legend under the brand bar —
    /// the `?` key and the **keys** chip (`TriageKeys`, `TriageLegend`).
    case toggleKeys
    /// Give up a focused reply field. Distinct from `clearFilter`, which is
    /// about the filter and only ever cleared that: with a reply field focused
    /// Escape used to be a dead key — nothing reset `stripEditing`, so
    /// `editing` stayed true, and the monitor swallowed the press rather than
    /// letting it reach the field, close the panel, or do anything at all.
    case endEditing
    /// Close the selected agent's detail — the wide pane it took from the
    /// board — by unselecting the row. Escape's rung between giving up a
    /// text box and popping the drill (`RailLayout.escapeRung`).
    case deselect
    /// Close the run the ledger has open. Innermost History rung.
    case closeHistoryRun
    /// Close the day the ledger is describing.
    case closeHistoryDay
    /// Leave History and bring the board back.
    case leaveHistory
    /// Close the report the Reports tab has open; the list comes back.
    case closeReport
    /// Leave the Reports tab and bring the board back.
    case leaveReports
}

/// One press. The sequence number is what makes two identical presses in a row
/// two events: SwiftUI's `onChange` fires on a *change*, and `.down` following
/// `.down` is not one.
struct TriageEvent: Equatable {
    let intent: TriageIntent
    let seq: Int
}

/// Key presses, from the panel's event monitor to the view.
///
/// The monitor has to live on the AppKit side — one reader beside a delicate
/// focus story (SwiftUI owns the responder chain inside the hosting view) —
/// but the selection it drives is view state. This is the one wire between
/// them.
@MainActor
final class KeyRouter: ObservableObject {
    @Published private(set) var event: TriageEvent?
    /// Whether the filter field currently holds focus. Published *up* from the
    /// view rather than guessed at from `panel.firstResponder`: SwiftUI decides
    /// its own responder chain inside an `NSHostingView`, and a monitor that
    /// mistakes a focused field for an unfocused list eats the letters someone
    /// is typing — "d" would dismiss a row instead of filtering for Audit.
    @Published var editing = false
    /// Whether the filter currently holds text, focused or not. Escape clears
    /// a live filter before it hides the panel — an unfocused field with a
    /// query in it is still a filter.
    @Published var filterActive = false
    /// Whether the Agents view is *showing* a project drill-in. Published *up*
    /// from the view the same way `editing` is, so Escape can pop it without
    /// the monitor guessing at view state. False on Inbox and while the
    /// project shelf is folded — backing out of something you cannot see
    /// would eat the press that should hide the panel.
    @Published var drilledIn = false
    /// Whether the Agents tab is drawing a selected row's detail in the
    /// board's pane. Published *up* from the view like the two above, so
    /// Escape can close the detail before it pops the drill or hides the
    /// panel (`RailLayout.escapeRung`).
    @Published var detailOpen = false
    /// History's three Escape rungs, published up the way `detailOpen` is.
    /// A run is inside a day, and a day is inside History.
    @Published var historyRun = false
    @Published var historyDay = false
    @Published var historyOpen = false
    /// The Reports tab's two rungs: a report open inside the tab.
    @Published var reportOpen = false
    @Published var reportsOpen = false
    /// The hosted native terminal holds the caret. Escape must reach the
    /// pty (vim, grok, a cancel) rather than close the detail.
    @Published var terminalFocused = false
    /// A focused non-text control owns every key it is sent. Published *up*
    /// from the view like `editing` — today the card face's assistant
    /// switcher (`BoardState.switcherFocused`), which handles ← / → / Space /
    /// Return / Escape itself through `onKeyPress`. The monitor's guard on
    /// this sits **below** the terminal guard and **above** Escape and the
    /// letter verbs, and it is not `editing`: Escape on `editing` sends
    /// `.clearFilter`, which would wipe the board search under a focused
    /// switcher.
    @Published var controlFocused = false
    private var seq = 0

    func send(_ intent: TriageIntent) {
        seq += 1
        event = TriageEvent(intent: intent, seq: seq)
    }
}

/// A request from outside the panel to put one session on screen.
///
/// The only sender today is a tap on a notification banner: the menu-bar app
/// shows the panel and names the session in the same command, and the view puts
/// that row where the eye already is — the band, or its project's list, opened.
/// A router rather than a call because the panel's selection is view state and
/// the command arrives on stdin, which is exactly the split `KeyRouter` draws.
///
/// The request is *not* consumed on read. It carries a sequence number and the
/// view remembers the last one it applied, so the two things that can deliver it
/// — the request landing, and the panel becoming visible with one already
/// pending — can both call the same function and only one of them acts.
@MainActor
final class FocusRouter: ObservableObject {
    struct Request: Equatable {
        let sessionId: String
        let seq: Int
    }

    @Published private(set) var request: Request?
    /// How many times the panel has been *deliberately* opened — bumped by
    /// the stdin `show`/`toggle` path when the window was not already on
    /// screen, never by an occlusion flip. The view aims its default
    /// selection at whoever needs you on this, not on `client.visible`,
    /// which is the SSE gate and flips whenever a window covers the panel.
    @Published private(set) var shown = 0
    private var seq = 0

    /// The panel was just opened on purpose.
    func noteShown() {
        shown += 1
    }

    /// Stepped when the panel is actually **put away** — `hide()` (Escape,
    /// the status-item toggle, stdin `hide`) and `windowWillClose` (⌘W, the
    /// red button) — and by nothing else. The first-run checklist's opening
    /// ends on this, never on the occlusion gate: a newcomer's own flow
    /// covers the panel with VS Code and comes back, and that must not
    /// start a new opening.
    @Published private(set) var hidden = 0

    func noteHidden() {
        hidden += 1
    }

    /// Ask for a session. Two taps on the same agent are two requests, for the
    /// reason `TriageEvent` carries a sequence number: `onChange` fires on a
    /// change, and the same id twice is not one.
    func send(_ sessionId: String) {
        guard !sessionId.isEmpty else { return }
        seq += 1
        request = Request(sessionId: sessionId, seq: seq)
    }
}

/// A request from outside the panel to put one session's *card* on screen.
///
/// `FocusRouter`'s twin, and deliberately a second object rather than a flag on
/// the first: the two halves are applied by two different views (the rail's
/// `PanelView`, the board's `BoardView`), each keeping its own applied-sequence
/// mark, and one router with two readers would have them fighting over which of
/// them had consumed a request.
///
/// The only sender is the "Dark Army: Show this session" press inside VS Code, which
/// aims at the *work* rather than at an interruption. A banner tap keeps its
/// rail-only focus.
@MainActor
final class CardFocusRouter: ObservableObject {
    struct Request: Equatable {
        let sessionId: String
        let seq: Int
    }

    @Published private(set) var request: Request?
    private var seq = 0

    /// Ask for a session's card. Two presses on the same terminal are two
    /// requests, `FocusRouter.send`'s reason exactly.
    func send(_ sessionId: String) {
        guard !sessionId.isEmpty else { return }
        seq += 1
        request = Request(sessionId: sessionId, seq: seq)
    }
}

/// The row verbs, and the state that has to be shared between the two ways of
/// reaching them.
///
/// These used to be `@State` inside `AgentRowView`, which was correct while a
/// mouse was the only way to press them. It stops being correct the moment the
/// keyboard can commit a stop: arm-then-confirm is a *gate*, and a gate with two
/// copies of its own state is not a gate — pressing S twice would have armed the
/// row's invisible keyboard copy while the visible button still read "Stop".
/// One object, one armed row, one place a refusal is recorded.
@MainActor
final class RowActions: ObservableObject {
    /// The session id whose Stop is armed, if any. At most one: arming a second
    /// row disarms the first, because two rows both offering to end a session on
    /// the next press is exactly the ambiguity the gate exists to remove.
    @Published private(set) var armedStop: String?
    /// The same gate for Retire, held separately so arming one does not read as
    /// arming the other: they are different verbs on different rows — Stop ends
    /// a run, Retire deletes the record of one that is already over.
    @Published private(set) var armedRetire: String?
    /// And the same gate again for Wrap up, which types `/clear` into a live
    /// session. Held apart from the other two for the reason they are held apart
    /// from each other: three verbs sharing one armed slot would let a press
    /// aimed at one commit another.
    @Published private(set) var armedWrapUp: String?
    /// And once more for Low priority, which types a *toggle* into a live
    /// session. Its own slot for the same reason as the three above.
    @Published private(set) var armedLowPriority: String?
    /// The daemon's own words for the last refusal, per row. Held until the next
    /// press on that row rather than flashed: on a refusal nothing else on the
    /// row moves, so the message is the only evidence the press was received.
    @Published private(set) var refusals: [String: String] = [:]

    private var disarmTask: Task<Void, Never>?

    func refusal(for agent: Agent) -> String { refusals[agent.id] ?? "" }

    /// A failed Jump leaves the screen exactly as it was, which is what a dead
    /// button looks like — so a row that cannot be jumped to says so rather than
    /// swallowing the press. That matters more from the keyboard than from the
    /// mouse: the ↗ icon is simply absent on such a row, but Enter is not.
    func jump(_ agent: Agent, client: DaemonClient) {
        if agent.ownTerminal {
            // A terminal Dark Army itself hosts has no editor window to raise.
            // Selecting the row opens the agent's full pane, which *is*
            // the live screen. Never `.panelDidJump`: that yields the
            // panel, and the panel is the destination.
            NotificationCenter.default.post(
                name: .panelShowSession, object: agent.sessionId)
            return
        }
        guard agent.canJump else {
            note(agent, "No terminal to jump to.")
            return
        }
        Task {
            if await run("reveal_session", agent, client) {
                NotificationCenter.default.post(name: .panelDidJump, object: nil)
            }
        }
    }

    func dismiss(_ agent: Agent, client: DaemonClient) {
        Task {
            await run(agent.canHide ? "hide_session" : "dismiss", agent, client)
        }
    }

    /// Armed, then committed — the same two presses the button has always taken,
    /// now the only copy of that rule. The arm lapses on its own after six
    /// seconds, so a row left armed by a stray press does not stay loaded.
    func stop(_ agent: Agent, client: DaemonClient) {
        guard !stopping.contains(agent.id) else { return }
        guard agent.canStop else {
            note(agent, "Dark Army cannot safely stop this session.")
            return
        }
        if armedStop == agent.id {
            disarm()
            beginStopping(agent.id)
            Task {
                if await run("stop_session", agent, client) == false {
                    endStopping(agent.id)
                }
            }
            return
        }
        armedStop = agent.id
        armedRetire = nil
        armedLowPriority = nil
        disarmTask?.cancel()
        disarmTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(6))
            guard !Task.isCancelled else { return }
            self?.armedStop = nil
        }
    }

    /// Retire an abandoned background agent — the one verb here that deletes
    /// something. `claude agents --json` lists a blocked background agent
    /// forever and the CLI has no way to retire one, so without this the row is
    /// permanent. Armed like Stop, and refused by the daemon for anything that
    /// is not in the `abandoned` bucket at the moment of the press.
    func retire(_ agent: Agent, client: DaemonClient) {
        guard !stopping.contains(agent.id) else { return }
        if armedRetire == agent.id {
            disarm()
            beginStopping(agent.id)
            Task {
                if await run("delete_agent", agent, client) == false {
                    endStopping(agent.id)
                }
            }
            return
        }
        armedRetire = agent.id
        armedStop = nil
        armedLowPriority = nil
        disarmTask?.cancel()
        disarmTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(6))
            guard !Task.isCancelled else { return }
            self?.armedRetire = nil
        }
    }

    /// Acknowledge a finished turn by closing the session's terminal tab.
    ///
    /// Armed then confirmed, like Stop and Retire and for the same reason: the
    /// press ends the agent and closes its tab, and there is no undo. The row
    /// leaves the snapshot because `_forget_session` / `_settle_codex_stop`
    /// drops it, which is what `pruneWrapping` was already written against.
    ///
    /// Offered only where `canClose` says a VS Code window can dispose the
    /// tab. The keyboard can still aim it at a row where the button is absent,
    /// so the guard is repeated here rather than left to the view.
    func wrapUp(_ agent: Agent, client: DaemonClient) {
        guard !stopping.contains(agent.id), !wrapping.contains(agent.id) else { return }
        guard agent.canClose else {
            note(agent, "Dark Army cannot close this session's terminal — it needs a VS Code window.")
            return
        }
        if armedWrapUp == agent.id {
            disarm()
            beginWrapping(agent.id)
            Task {
                let result = await client.closeTerminal(agent.sessionId)
                note(agent, result.ok ? "" : result.detail)
                if !result.ok { endWrapping(agent.id) }
            }
            return
        }
        armedWrapUp = agent.id
        armedStop = nil
        armedRetire = nil
        armedLowPriority = nil
        disarmTask?.cancel()
        disarmTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(6))
            guard !Task.isCancelled else { return }
            self?.armedWrapUp = nil
        }
    }

    /// Switch a rate-limited Claude session to low priority: types
    /// `/low-priority` onto its input line and dismisses its card. Armed then
    /// confirmed, because the command is a toggle. No `beginWrapping` — the
    /// row is not leaving; the bar disappears when the next snapshot's
    /// `can_low_priority` reads false.
    ///
    /// Offered only where `canLowPriority` says the daemon would accept it;
    /// the guard is repeated here for the same reason `wrapUp` repeats its own.
    func lowPriority(_ agent: Agent, client: DaemonClient) {
        guard !stopping.contains(agent.id) else { return }
        guard agent.canLowPriority else {
            note(agent, "Dark Army cannot switch this session to low priority from here.")
            return
        }
        if armedLowPriority == agent.id {
            disarm()
            Task {
                let result = await client.lowPriority(agent.sessionId)
                note(agent, result.ok ? "" : result.detail)
            }
            return
        }
        armedLowPriority = agent.id
        armedStop = nil
        armedRetire = nil
        armedWrapUp = nil
        disarmTask?.cancel()
        disarmTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(6))
            guard !Task.isCancelled else { return }
            self?.armedLowPriority = nil
        }
    }

    func disarm() {
        disarmTask?.cancel()
        disarmTask = nil
        armedStop = nil
        armedRetire = nil
        armedWrapUp = nil
        armedLowPriority = nil
    }

    /// Answer a tool-approval prompt relayed out of a session.
    ///
    /// No arm-then-confirm, deliberately, where Stop and Retire both have one:
    /// those are irreversible acts Dark Army initiates, while this is a question
    /// already open in the session's own terminal that one keypress answers
    /// there. A remote copy that costs two presses is slower than walking to
    /// the terminal, which is the entire point of having it.
    ///
    /// A refusal lands in the same per-row slot as every other verb's. The
    /// common one is not a failure at all — the terminal answered first, and the
    /// prompt was gone before this press arrived.
    /// Answered prompts, hidden from the moment they are pressed rather than
    /// when the daemon's next snapshot says so.
    ///
    /// This is the one verb here that *must* be optimistic. Stop and Dismiss
    /// change the row within a tick and a stale second reads as nothing; this
    /// bar carries two buttons, so a bar that is still there after the press
    /// reads as *the press did not register* — and the second press lands on a
    /// prompt that has already been allowed and answers back "already
    /// answered", which is a failure message for something that worked. Seen
    /// exactly once, by a human, on the first real use.
    @Published private(set) var answered: Set<String> = []

    func answerPermission(_ prompt: PermissionPrompt, allow: Bool,
                          agent: Agent, client: DaemonClient) {
        answered.insert(prompt.requestId)
        Task {
            let result = await client.answer(prompt.requestId, allow: allow)
            // Not restored on failure: every way this fails — the terminal
            // answered first, the session ended — is a prompt that is gone. The
            // daemon's words still land under the row, because "the terminal
            // got there first" is worth knowing and costs nothing to say.
            note(agent, result.ok ? "" : result.detail)
        }
    }

    /// Rows whose reply is in flight. HTTP-in-flight is not pruned by the
    /// snapshot; only the post-success remainder (ids already in `skipDwell`)
    /// is. Held from the press until HTTP finishes; on success, until the
    /// card leaves Needs you or a prompt arrives, or `sendingTimeout`;
    /// dropped immediately on a refused send.
    @Published private(set) var sending: Set<String> = []

    /// Successful replies that leave Needs you without the 5s dwell.
    /// Not published — only `refreshAttentionHold` reads it.
    private(set) var skipDwell: Set<String> = []

    static let sendingTimeout: TimeInterval = 8

    private var sendingTimeouts: [String: Task<Void, Never>] = [:]

    /// Answer a session that ended its turn waiting — "Plan ready. accept?".
    ///
    /// This is the *other* half of what reachability makes possible, and it
    /// works for a narrower case than it looks: the session has to be **idle**.
    /// A session blocked inside `AskUserQuestion` or on plan approval is
    /// waiting on terminal input, and an event pushed at it queues behind the
    /// very dialog it would answer — so the panel offers this only where the
    /// daemon says the session is reachable, and the daemon refuses in words
    /// when it is not (no Claude channel, a down Grok leader, a Grok session
    /// that is not a live client of the leader).
    func reply(_ agent: Agent, text: String, client: DaemonClient) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sending.insert(agent.id)
        armSendingTimeout(agent.id)
        Task {
            let result = await client.reply(agent.sessionId, text: text)
            if result.ok {
                skipDwell.insert(agent.id)
                note(agent, "")
            } else {
                sending.remove(agent.id)
                cancelSendingTimeout(agent.id)
                note(agent, result.detail)
            }
        }
    }

    /// Choose an option of the `AskUserQuestion` this row is stopped on.
    ///
    /// A reply in every way that matters to the row's state — the session is
    /// about to resume — so it rides `sending`/`skipDwell` rather than growing
    /// a fourth in-flight set. **No arm-then-confirm**, deliberately, where
    /// Stop, Retire and Wrap up all have one: those destroy something, while
    /// choosing an option the agent itself offered is what the row is *for*,
    /// and a double press on the commonest verb in the panel is friction with
    /// nothing behind it. The daemon refuses in words when the press cannot
    /// land — no window that can type, a permission prompt owning the input
    /// line, or a question the terminal already dealt with.
    func answerQuestion(_ agent: Agent, index: Int, client: DaemonClient) {
        sending.insert(agent.id)
        armSendingTimeout(agent.id)
        Task {
            let result = await client.answerQuestion(
                agent.sessionId, questionId: agent.question.id, index: index)
            if result.ok {
                skipDwell.insert(agent.id)
                note(agent, "")
            } else {
                sending.remove(agent.id)
                cancelSendingTimeout(agent.id)
                note(agent, result.detail)
            }
        }
    }

    /// Answer every question of a multi-question dialog in one press.
    /// `answerQuestion`'s pattern exactly — `sending`/`skipDwell`, the refusal
    /// note, and **no arm-then-confirm**, for the same reason: choosing the
    /// options the agent itself offered is what the row is for. `choices` is
    /// one group of 0-based indexes per question, in dialog order — a group
    /// of one on a pick-one question, every ticked box on a multi-select one;
    /// the daemon re-checks the count, every index and every group's shape
    /// against the dialog it holds before the first keystroke, and refuses in
    /// words when the burst cannot land.
    func answerQuestions(_ agent: Agent, choices: [[Int]], client: DaemonClient) {
        sending.insert(agent.id)
        armSendingTimeout(agent.id)
        Task {
            let result = await client.answerQuestions(
                agent.sessionId, questionId: agent.question.id,
                indexes: choices)
            if result.ok {
                skipDwell.insert(agent.id)
                note(agent, "")
            } else {
                sending.remove(agent.id)
                cancelSendingTimeout(agent.id)
                note(agent, result.detail)
            }
        }
    }

    func isSending(_ agent: Agent) -> Bool { sending.contains(agent.id) }

    /// Drop the post-success remainder that no longer needs a human, or that
    /// now has a permission prompt. In-flight (HTTP-not-yet-back) ids stay
    /// until the request finishes; the snapshot does not prune them.
    func pruneSending(_ stillNeeding: Set<String>, prompted: Set<String> = []) {
        let keepPostSuccess = stillNeeding.subtracting(prompted)
        let finished = skipDwell.subtracting(keepPostSuccess)
        sending.subtract(finished)
        skipDwell.formIntersection(keepPostSuccess)
        for id in finished { cancelSendingTimeout(id) }
    }

    /// Forget ids the daemon no longer lists. Five-letter ids are reused
    /// eventually, and an id remembered forever would silently swallow a real
    /// prompt months later.
    func pruneAnswered(_ live: Set<String>) {
        answered.formIntersection(live)
    }

    private func armSendingTimeout(_ id: String) {
        sendingTimeouts[id]?.cancel()
        sendingTimeouts[id] = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.sendingTimeout))
            guard !Task.isCancelled else { return }
            self?.sending.remove(id)
            self?.skipDwell.remove(id)
            self?.sendingTimeouts[id] = nil
        }
    }

    private func cancelSendingTimeout(_ id: String) {
        sendingTimeouts[id]?.cancel()
        sendingTimeouts[id] = nil
    }

    /// Rows whose Stop or Retire has been *confirmed* and not yet taken effect.
    ///
    /// The gap is real and it is about a second: the daemon has to check the pid
    /// is still the harness it recorded, signal it, and the fleet has to come
    /// back round on the next snapshot before the row moves out of the live
    /// buckets. In that second the confirmed press left the screen exactly as it
    /// was — the button snapped back from "Really stop?" to "Stop", which reads
    /// as *the press was dropped*, and the natural response is to press it
    /// again. So the row greys out and spins from the press until the snapshot
    /// agrees, the same shape `sending` gives a reply in flight.
    @Published private(set) var stopping: Set<String> = []

    /// Backstop for the case the snapshot never disagrees — a daemon that goes
    /// away mid-press, a session the signal did not actually end. A row frozen
    /// grey forever is worse than one that admits it does not know.
    static let stoppingTimeout: TimeInterval = 12

    private var stoppingTimeouts: [String: Task<Void, Never>] = [:]

    func isStopping(_ agent: Agent) -> Bool { stopping.contains(agent.id) }

    /// Drop rows the fleet no longer reports as live. Cleared by the *snapshot*
    /// rather than by the HTTP reply: 200 means the daemon accepted the verb,
    /// not that the session is over, and clearing on it would drop the spinner
    /// back into the same second of nothing it exists to cover.
    func pruneStopping(_ stillLive: Set<String>) {
        let done = stopping.subtracting(stillLive)
        guard !done.isEmpty else { return }
        stopping.subtract(done)
        for id in done { cancelStoppingTimeout(id) }
    }

    private func beginStopping(_ id: String) {
        stopping.insert(id)
        stoppingTimeouts[id]?.cancel()
        stoppingTimeouts[id] = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.stoppingTimeout))
            guard !Task.isCancelled else { return }
            self?.stopping.remove(id)
            self?.stoppingTimeouts[id] = nil
        }
    }

    /// Only on a refusal — the daemon said no in words, and the row has to be
    /// usable again to act on them.
    private func endStopping(_ id: String) {
        stopping.remove(id)
        cancelStoppingTimeout(id)
    }

    private func cancelStoppingTimeout(_ id: String) {
        stoppingTimeouts[id]?.cancel()
        stoppingTimeouts[id] = nil
    }

    /// Rows whose Wrap up has been confirmed and not yet taken effect. The same
    /// second `stopping` covers, for the same reason — a confirmed press that
    /// leaves the screen unchanged reads as a dropped press — but a shorter one
    /// in practice: `/clear` ends the session id within a tick and the row is
    /// replaced by its successor.
    @Published private(set) var wrapping: Set<String> = []

    static let wrappingTimeout: TimeInterval = 12

    private var wrappingTimeouts: [String: Task<Void, Never>] = [:]

    func isWrappingUp(_ agent: Agent) -> Bool { wrapping.contains(agent.id) }

    /// Cleared by the snapshot, exactly like `pruneStopping`: the 200 says the
    /// daemon typed the command, not that the session took it.
    func pruneWrapping(_ stillLive: Set<String>) {
        let done = wrapping.subtracting(stillLive)
        guard !done.isEmpty else { return }
        wrapping.subtract(done)
        for id in done { cancelWrappingTimeout(id) }
    }

    private func beginWrapping(_ id: String) {
        wrapping.insert(id)
        wrappingTimeouts[id]?.cancel()
        wrappingTimeouts[id] = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.wrappingTimeout))
            guard !Task.isCancelled else { return }
            self?.wrapping.remove(id)
            self?.wrappingTimeouts[id] = nil
        }
    }

    private func endWrapping(_ id: String) {
        wrapping.remove(id)
        cancelWrappingTimeout(id)
    }

    private func cancelWrappingTimeout(_ id: String) {
        wrappingTimeouts[id]?.cancel()
        wrappingTimeouts[id] = nil
    }

    @discardableResult
    private func run(_ action: String, _ agent: Agent, _ client: DaemonClient) async -> Bool {
        let result = await client.act(action, session: agent.sessionId)
        note(agent, result.ok ? "" : result.detail)
        return result.ok
    }

    private func note(_ agent: Agent, _ text: String) {
        if text.isEmpty {
            refusals.removeValue(forKey: agent.id)
        } else {
            refusals[agent.id] = text
        }
    }
}

/// Whether an agent matches what was typed into the filter.
///
/// Terms are ANDed and each is matched against every field a person would think
/// to type — the nickname they read on the row, the project and branch under it,
/// the model, the tool, the harness, and the bound card's title and summary
/// when one is passed. Nil-card behaviour is the original haystack. Free
/// function rather than a method on `Agent` so it can be read next to the
/// field it serves, and so the fields it searches are visible in one place
/// rather than spread over a decoding type.
func agentMatches(_ agent: Agent, filter: String, card: BoardCard? = nil) -> Bool {
    let terms = filter.lowercased().split(separator: " ").map(String.init)
    guard !terms.isEmpty else { return true }
    let haystack = [
        agent.nickname, agent.name, agent.project, agent.branch,
        agent.currentTool, agent.provider, agent.kind,
        agent.metrics.modelId ?? agent.stats.model ?? "",
        card?.title ?? "", card?.summary ?? "",
    ].joined(separator: " ").lowercased()
    return terms.allSatisfy { haystack.contains($0) }
}
