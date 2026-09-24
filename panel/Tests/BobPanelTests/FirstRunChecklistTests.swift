import XCTest
@testable import BobPanel

/// The first-run checklist's whole contract, at the seams: the per-root
/// progress table, the once-per-opening decision, the same-root rule, the
/// remembered completion, the launch line's wording and the denied-only
/// notification warning. Nothing here draws a view.
final class FirstRunChecklistTests: XCTestCase {

    // MARK: - fixtures

    private func snapshot(_ json: String) throws -> Snapshot {
        try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    }

    /// A frame with `enrollment` stated. `roots` is the ledger; `facts` the
    /// daemon's per-root observations; `checklist: false` omits the block
    /// entirely, as an older daemon would.
    private func frame(at clock: Double = 100, roots: [String],
                       facts: [(String, Bool, Bool)] = [],
                       own: Set<String> = [],
                       checklist: Bool = true) throws -> Snapshot {
        let enrolled = roots.map { #"{"root": "\#($0)", "label": "\#(($0 as NSString).lastPathComponent)"}"# }
            .joined(separator: ",")
        // `own` roots carry `"own_checkout": true`; every other fact omits
        // the key, as an older daemon would.
        var rows: [String] = []
        var factRoots: Set<String> = []
        for fact in facts {
            factRoots.insert(fact.0)
            let mark: String = own.contains(fact.0) ? #", "own_checkout": true"# : ""
            rows.append(#"{"root": "\#(fact.0)", "editor_observed": \#(fact.1), "session_observed": \#(fact.2)\#(mark)}"#)
        }
        for root in own.subtracting(factRoots).sorted() {
            rows.append(#"{"root": "\#(root)", "editor_observed": false, "session_observed": false, "own_checkout": true}"#)
        }
        let observed = rows.joined(separator: ",")
        let block = checklist ? #", "checklist": {"available": true, "roots": [\#(observed)]}"# : ""
        return try snapshot("""
        {"generated_at": \(clock), "enrollment": {"available": true,
         "enrolled": [\(enrolled)], "pending": [], "other": 0\(block)}}
        """)
    }

    private func states(_ progress: FirstRunChecklist.Progress) -> [FirstRunChecklist.StepState] {
        progress.steps.map(\.state)
    }

    // MARK: - progress

    func testTheThreeStepsHaveExactlyTheseTitles() throws {
        let snap = try frame(roots: ["/p/a"])
        let progress = FirstRunChecklist.progress(root: "/p/a", enrollment: snap.enrollment)
        XCTAssertEqual(progress.steps.map(\.title),
                       ["Enrol a folder", "Open it in VS Code", "Start a session"])
        XCTAssertEqual(FirstRunChecklist.titles.count, 3)
    }

    func testProgressTableAllCombinations() throws {
        typealias S = FirstRunChecklist.StepState
        let cases: [(enrolled: Bool, editor: Bool, session: Bool, expect: [S], note: String)] = [
            (false, false, false, [.current, .next, .next], "nothing yet"),
            (true, false, false, [.done, .current, .next], "enrolled only"),
            (true, true, false, [.done, .done, .current], "enrolled and open"),
            (true, true, true, [.done, .done, .done], "all three"),
            // Facts out of order: a session seen before its window is. Step 3
            // is done on its own evidence; step 2 is still the current one
            // and is not ticked for it.
            (true, false, true, [.done, .current, .done], "session before editor"),
            // Facts for a root that is not enrolled are no evidence at all.
            (false, true, true, [.current, .next, .next], "unenrolled root's facts"),
        ]
        for c in cases {
            let snap = try frame(roots: c.enrolled ? ["/p/a"] : [],
                                 facts: [("/p/a", c.editor, c.session)])
            let progress = FirstRunChecklist.progress(root: "/p/a", enrollment: snap.enrollment)
            XCTAssertEqual(states(progress), c.expect, c.note)
            // Exactly one current step before completion, none after.
            let current = progress.steps.filter { $0.state == .current }.count
            XCTAssertEqual(current, progress.complete ? 0 : 1, c.note)
        }
    }

    func testANilRootIsThreeUnstartedSteps() throws {
        let snap = try frame(roots: [])
        let progress = FirstRunChecklist.progress(root: nil, enrollment: snap.enrollment)
        XCTAssertEqual(states(progress), [.current, .next, .next])
        XCTAssertNil(progress.root)
    }

    func testMissingFactsForAnEnrolledRootLeaveLaterStepsUnticked() throws {
        // Enrolled, but the daemon has published no facts row for it yet.
        let snap = try frame(roots: ["/p/a"], facts: [])
        XCTAssertEqual(states(FirstRunChecklist.progress(root: "/p/a", enrollment: snap.enrollment)),
                       [.done, .current, .next])
    }

    func testNoStepFromRootAMayCombineWithRootB() throws {
        let snap = try frame(roots: ["/p/a", "/p/b"],
                             facts: [("/p/a", false, false), ("/p/b", true, true)])
        XCTAssertEqual(states(FirstRunChecklist.progress(root: "/p/a", enrollment: snap.enrollment)),
                       [.done, .current, .next])
        XCTAssertEqual(states(FirstRunChecklist.progress(root: "/p/b", enrollment: snap.enrollment)),
                       [.done, .done, .done])
    }

    func testAccessibleLabelsCarryTheStateWord() {
        let step = FirstRunChecklist.Step(id: 1, title: "Open it in VS Code", state: .current)
        XCTAssertEqual(step.accessibleLabel, "Current step: Open it in VS Code")
        XCTAssertEqual(FirstRunChecklist.StepState.done.rawValue, "Done")
        XCTAssertEqual(FirstRunChecklist.StepState.next.rawValue, "Next")
    }

    func testInstructionsNameTheFolderAndThePrivateKey() {
        let enrol = FirstRunChecklist.Step(id: 0, title: "", state: .current)
        let open = FirstRunChecklist.Step(id: 1, title: "", state: .current)
        let start = FirstRunChecklist.Step(id: 2, title: "", state: .current)
        XCTAssertTrue(FirstRunChecklist.instruction(for: enrol, folderLabel: "x").contains("private key"))
        XCTAssertTrue(FirstRunChecklist.instruction(for: open, folderLabel: "acme").contains("acme"))
        XCTAssertTrue(FirstRunChecklist.instruction(for: open, folderLabel: "acme").contains("Waiting for VS Code to connect"))
        XCTAssertTrue(FirstRunChecklist.instruction(for: start, folderLabel: "acme").contains("acme"))
        let startInstruction = FirstRunChecklist.instruction(for: start, folderLabel: "acme")
        XCTAssertTrue(startInstruction.contains("Claude Code"))
        XCTAssertTrue(startInstruction.contains("Codex"))
        XCTAssertTrue(startInstruction.contains("Grok"))
        XCTAssertFalse(FirstRunChecklist.instruction(for: start, folderLabel: "acme").contains("prompt first"))
    }

    // MARK: - root selection

    func testSelectRootKeepsTheFollowedRootWhileEnrolledAndSortsOtherwise() {
        XCTAssertEqual(FirstRunChecklist.selectRoot(previous: nil, enrolled: ["/z", "/a"]), "/a")
        XCTAssertEqual(FirstRunChecklist.selectRoot(previous: "/z", enrolled: ["/z", "/a"]), "/z")
        // Removal invalidates and chooses again, deterministically.
        XCTAssertEqual(FirstRunChecklist.selectRoot(previous: "/z", enrolled: ["/m", "/a"]), "/a")
        XCTAssertNil(FirstRunChecklist.selectRoot(previous: "/z", enrolled: []))
    }

    // MARK: - the decision

    func testDecisionTable() throws {
        // No checklist block: an older daemon, ordinary UI.
        XCTAssertEqual(FirstRunChecklist.decide(
            enrollment: try frame(roots: [], checklist: false).enrollment, completed: false),
            .unsupported)
        // No enrolment section at all.
        XCTAssertEqual(FirstRunChecklist.decide(enrollment: Enrollment(), completed: false),
                       .unsupported)
        XCTAssertEqual(FirstRunChecklist.decide(
            enrollment: try frame(roots: []).enrollment, completed: true), .alreadyDone)
        XCTAssertEqual(FirstRunChecklist.decide(
            enrollment: try frame(roots: ["/p/a"], facts: [("/p/a", true, true)]).enrollment,
            completed: false), .skipAndComplete)
        XCTAssertEqual(FirstRunChecklist.decide(
            enrollment: try frame(roots: ["/p/a"], facts: [("/p/a", true, false)]).enrollment,
            completed: false), .show)
        XCTAssertEqual(FirstRunChecklist.decide(
            enrollment: try frame(roots: []).enrollment, completed: false), .show)
    }

    // MARK: - the opening

    func testTheDefaultOpeningShowsTheChecklistOnTheFirstAuthoritativeSnapshot() throws {
        var opening = FirstRunChecklist.Opening()
        // A blank snapshot (nothing decoded yet) decides nothing.
        XCTAssertFalse(opening.advance(snapshot: Snapshot(), completed: false))
        XCTAssertFalse(opening.decided)
        XCTAssertFalse(opening.advance(snapshot: try frame(roots: []), completed: false))
        XCTAssertEqual(opening.placement, .checklist)
        XCTAssertNil(opening.selectedRoot)
    }

    func testASessionAlreadyPresentOnTheFirstSnapshotSkipsAndPersists() throws {
        var opening = FirstRunChecklist.Opening()
        let persist = opening.advance(
            snapshot: try frame(roots: ["/p/a"], facts: [("/p/a", true, true)]),
            completed: false)
        XCTAssertTrue(persist)
        XCTAssertEqual(opening.placement, .none)
    }

    func testASessionArrivingAfterTheChecklistAppearedCompletesItInPlace() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: ["/p/a"]), completed: false)
        XCTAssertEqual(opening.placement, .checklist)
        XCTAssertEqual(opening.selectedRoot, "/p/a")
        opening.advance(snapshot: try frame(at: 110, roots: ["/p/a"],
                                            facts: [("/p/a", true, false)]),
                        completed: false)
        XCTAssertEqual(opening.placement, .checklist)
        let persist = opening.advance(
            snapshot: try frame(at: 120, roots: ["/p/a"], facts: [("/p/a", true, true)]),
            completed: false)
        XCTAssertTrue(persist)
        XCTAssertEqual(opening.placement, .completed)
        // A later quiet frame keeps the all-ticked display for this opening.
        XCTAssertFalse(opening.advance(snapshot: try frame(at: 130, roots: ["/p/a"]),
                                       completed: true))
        XCTAssertEqual(opening.placement, .completed)
        // The next opening, with completion remembered, is ordinary.
        opening.reset()
        XCTAssertFalse(opening.advance(snapshot: try frame(at: 140, roots: ["/p/a"]),
                                       completed: true))
        XCTAssertEqual(opening.placement, .none)
    }

    func testTheCheckedRootFollowsThePickerAndSurvivesUntilUnenrolled() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: ["/p/b", "/p/a"]), completed: false)
        XCTAssertEqual(opening.selectedRoot, "/p/a")            // canonical sort
        opening.choose(root: "/p/b")
        opening.advance(snapshot: try frame(at: 110, roots: ["/p/b", "/p/a"],
                                            facts: [("/p/a", true, true)]),
                        completed: false)
        // Root A's ticks do not complete a checklist following root B.
        XCTAssertEqual(opening.selectedRoot, "/p/b")
        XCTAssertEqual(opening.placement, .checklist)
        // B leaves the ledger: the choice is invalidated and A is chosen again.
        opening.advance(snapshot: try frame(at: 120, roots: ["/p/a"],
                                            facts: [("/p/a", true, false)]),
                        completed: false)
        XCTAssertEqual(opening.selectedRoot, "/p/a")
    }

    func testAnEditorClosingReturnsStepTwoToWaitingBeforeCompletion() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: ["/p/a"], facts: [("/p/a", true, false)]),
                        completed: false)
        let open = try frame(at: 105, roots: ["/p/a"], facts: [("/p/a", true, false)])
        XCTAssertEqual(states(FirstRunChecklist.progress(root: opening.selectedRoot,
                                                         enrollment: open.enrollment)),
                       [.done, .done, .current])
        let closed = try frame(at: 110, roots: ["/p/a"], facts: [("/p/a", false, false)])
        opening.advance(snapshot: closed, completed: false)
        XCTAssertEqual(states(FirstRunChecklist.progress(root: opening.selectedRoot,
                                                         enrollment: closed.enrollment)),
                       [.done, .current, .next])
    }

    func testALegacyDaemonAndACompletedInstallLeaveTheOrdinaryUI() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: [], checklist: false), completed: false)
        XCTAssertEqual(opening.placement, .none)
        XCTAssertTrue(opening.decided)
        var done = FirstRunChecklist.Opening()
        done.advance(snapshot: try frame(roots: []), completed: true)
        XCTAssertEqual(done.placement, .none)
    }

    func testChoosingIsIgnoredOnceComplete() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: ["/p/a"]), completed: false)
        opening.advance(snapshot: try frame(at: 110, roots: ["/p/a", "/p/b"],
                                            facts: [("/p/a", true, true)]),
                        completed: false)
        XCTAssertEqual(opening.placement, .completed)
        opening.choose(root: "/p/b")
        XCTAssertEqual(opening.selectedRoot, "/p/a")
    }

    /// The newcomer's own flow: checklist showing → VS Code covers the panel
    /// (occlusion, **not** an opening boundary) → folder opened, session
    /// started → back. Even if something did end the opening in between,
    /// the re-decision must resume the checklist and complete it — never
    /// find the session and skip the tutorial.
    func testAShownChecklistIsStickyAcrossAReDecisionAndCompletesNotSkips() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: ["/p/a"]), completed: false)
        XCTAssertEqual(opening.placement, .checklist)
        opening.reset()
        XCTAssertFalse(opening.decided)
        XCTAssertTrue(opening.resumeChecklist)
        let persist = opening.advance(
            snapshot: try frame(at: 110, roots: ["/p/a"], facts: [("/p/a", true, true)]),
            completed: false)
        XCTAssertEqual(opening.placement, .completed)
        XCTAssertTrue(persist)
        XCTAssertFalse(opening.resumeChecklist)
        // The same first snapshot on a fresh, never-shown opening skips.
        var fresh = FirstRunChecklist.Opening()
        fresh.advance(snapshot: try frame(at: 110, roots: ["/p/a"], facts: [("/p/a", true, true)]),
                      completed: false)
        XCTAssertEqual(fresh.placement, .none)
    }

    /// With several folders, the one the reader picked follows the sticky
    /// checklist across a put-away; a resumed checklist that re-picked the
    /// first root by sort would show step 3 waiting on the wrong folder.
    func testAStickyChecklistKeepsTheFolderItFollowed() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: ["/p/a", "/p/b"]), completed: false)
        opening.choose(root: "/p/b")
        opening.reset()
        XCTAssertEqual(opening.selectedRoot, "/p/b")
        let persist = opening.advance(
            snapshot: try frame(at: 110, roots: ["/p/a", "/p/b"],
                                facts: [("/p/b", true, true)]),
            completed: false)
        XCTAssertEqual(opening.selectedRoot, "/p/b")
        XCTAssertEqual(opening.placement, .completed)
        XCTAssertTrue(persist)
        // A followed root that left the ledger is dropped, not kept.
        var gone = FirstRunChecklist.Opening()
        gone.advance(snapshot: try frame(roots: ["/p/a", "/p/b"]), completed: false)
        gone.choose(root: "/p/b")
        gone.reset()
        gone.advance(snapshot: try frame(at: 110, roots: ["/p/a"]), completed: false)
        XCTAssertEqual(gone.selectedRoot, "/p/a")
        // A finished checklist carries nothing over.
        opening.reset()
        XCTAssertNil(opening.selectedRoot)
    }

    func testStickinessSurvivesRepeatedResetsAndEndsWithCompletion() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: []), completed: false)
        opening.reset()
        opening.reset()
        XCTAssertTrue(opening.resumeChecklist)
        // Still unfinished on the next opening: resumed, not re-decided.
        opening.advance(snapshot: try frame(at: 110, roots: ["/p/a"]), completed: false)
        XCTAssertEqual(opening.placement, .checklist)
        // Completed: the following opening is ordinary and nothing sticks.
        opening.advance(snapshot: try frame(at: 120, roots: ["/p/a"],
                                            facts: [("/p/a", true, true)]),
                        completed: false)
        XCTAssertEqual(opening.placement, .completed)
        opening.reset()
        XCTAssertFalse(opening.resumeChecklist)
        opening.advance(snapshot: try frame(at: 130, roots: ["/p/a"]), completed: true)
        XCTAssertEqual(opening.placement, .none)
    }

    /// The opening's boundary is the put-away path's own counter, stepped by
    /// `hide()` and `windowWillClose` and by nothing else — not `shown`,
    /// which a status-item click on an occluded panel also steps.
    @MainActor
    func testTheOpeningEndsOnTheHiddenCounterNotOnShownOrOcclusion() {
        let router = FocusRouter()
        XCTAssertEqual(router.hidden, 0)
        router.noteShown()
        XCTAssertEqual(router.hidden, 0, "a deliberate show is not a put-away")
        router.noteHidden()
        XCTAssertEqual(router.hidden, 1)
        XCTAssertEqual(router.shown, 1)
    }

    // MARK: - persistence

    private var originalRoot: URL!
    private var tempRoot: URL!

    override func setUpWithError() throws {
        originalRoot = PanelPlacement.root
        tempRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("bob-checklist-tests-\(UUID().uuidString)",
                                    isDirectory: true)
        try FileManager.default.createDirectory(at: tempRoot,
                                                withIntermediateDirectories: true)
        PanelPlacement.root = tempRoot
    }

    override func tearDownWithError() throws {
        PanelPlacement.root = originalRoot
        try? FileManager.default.removeItem(at: tempRoot)
    }

    private func writeRaw(_ body: [String: Any]) throws {
        try JSONSerialization.data(withJSONObject: body).write(to: PanelPlacement.path)
    }

    private func readRaw() throws -> [String: Any] {
        try XCTUnwrap(try JSONSerialization.jsonObject(
            with: try Data(contentsOf: PanelPlacement.path)) as? [String: Any])
    }

    func testCompletionDefaultsFalseAndRoundTrips() throws {
        XCTAssertFalse(PanelPlacement.checklistCompleted())
        PanelPlacement.saveChecklistCompleted(true)
        XCTAssertTrue(PanelPlacement.checklistCompleted())
        XCTAssertEqual(try readRaw()[PanelPlacement.checklistCompletedKey] as? Bool, true)
        PanelPlacement.saveChecklistCompleted(false)
        XCTAssertFalse(PanelPlacement.checklistCompleted())
    }

    func testCompletionWritePreservesFramesBoardKeysAndUnknownKeys() throws {
        try writeRaw(["frames": ["1": [1, 2, 300, 400]],
                      "board_projects": ["a"],
                      "board_row_flips": ["done"],
                      "left": 12,
                      "from_the_future": ["x": 1]])
        PanelPlacement.saveChecklistCompleted(true)
        let body = try readRaw()
        XCTAssertEqual(body["board_projects"] as? [String], ["a"])
        XCTAssertEqual(body["board_row_flips"] as? [String], ["done"])
        XCTAssertEqual(body["left"] as? Int, 12)
        XCTAssertNotNil(body["from_the_future"])
        XCTAssertNotNil(body["frames"])
        XCTAssertEqual(PanelPlacement.frame(for: 1)?.width, 300)
        XCTAssertEqual(PanelPlacement.boardProjects(), ["a"])
    }

    func testMalformedAndFutureCompletionValuesDecodeFalse() throws {
        for value: Any in ["yes", 1, ["done": true], NSNull()] {
            try writeRaw([PanelPlacement.checklistCompletedKey: value])
            XCTAssertFalse(PanelPlacement.checklistCompleted(), "\(value)")
        }
    }

    func testOtherWritersKeepTheCompletionKey() throws {
        PanelPlacement.saveChecklistCompleted(true)
        PanelPlacement.saveBoardProjects(["p"])
        PanelPlacement.saveBoardRowFlips(["done"])
        PanelPlacement.saveFrame(NSRect(x: 0, y: 0, width: 500, height: 500), for: 7)
        XCTAssertTrue(PanelPlacement.checklistCompleted())
    }

    // MARK: - the launch line

    private func report(_ hooks: String, _ ext: String, _ login: String,
                        detail: String = "") -> DaemonClient.LaunchReport {
        DaemonClient.LaunchReport(
            hooks: .init(status: hooks, detail: detail),
            editorExtension: .init(status: ext, detail: detail),
            loginItem: .init(status: login, detail: detail))
    }

    func testLaunchLineNamesAllThreeAndNeverClaimsAnAttempt() {
        XCTAssertEqual(
            FirstRunChecklist.launchLine(report("unchanged", "changed", "changed")),
            "This launch: hooks current; editor extension installed; login item enabled.")
        XCTAssertEqual(
            FirstRunChecklist.launchLine(report("pending", "pending", "pending")),
            "This launch: hooks checking…; editor extension checking…; login item checking….")
        XCTAssertEqual(
            FirstRunChecklist.launchLine(report("changed", "skipped", "unchanged")),
            "This launch: hooks installed; editor extension skipped; login item unchanged.")
        XCTAssertEqual(
            FirstRunChecklist.launchLine(report("unknown", "unknown", "unknown")),
            "This launch: hooks unknown; editor extension unknown; login item unknown.")
    }

    func testALoginItemWaitingForTheNextLoginNeverSaysEnabled() {
        var r = report("unchanged", "unchanged", "unchanged")
        r.loginItem = .init(status: "changed",
                            detail: "login item updated, effective at the next login")
        XCTAssertEqual(
            FirstRunChecklist.launchLine(r),
            "This launch: hooks current; editor extension current; "
                + "login item updated, effective at the next login.")
        r.loginItem = .init(status: "changed", detail: "login item enabled")
        XCTAssertTrue(FirstRunChecklist.launchLine(r).hasSuffix("login item enabled."))
    }

    func testAFailureOutranksTheSuccessWordingAndCarriesItsDetail() {
        var r = report("unchanged", "unchanged", "unchanged")
        r.loginItem = .init(status: "failed",
                            detail: "login item written but launchd did not register it")
        let line = FirstRunChecklist.launchLine(r)
        XCTAssertTrue(line.hasPrefix("This launch had a problem: "))
        XCTAssertTrue(line.contains("login item failed — login item written but launchd did not register it"))
        XCTAssertFalse(line.contains("login item enabled"))
    }

    func testAnUnknownStatusWordDecodesAsUnknownNotSuccess() {
        let outcome = DaemonClient.LaunchOutcome(["status": "succeeded"],
                                                 base: .init())
        XCTAssertEqual(outcome.status, "unknown")
        let settings = DaemonClient.Settings(
            ["launch": ["hooks": ["status": "changed", "detail": "hooks installed"]]],
            base: DaemonClient.Settings())
        XCTAssertEqual(settings.launch.hooks.status, "changed")
        XCTAssertEqual(settings.launch.editorExtension.status, "pending")
        XCTAssertTrue(settings.launch.anyPending)
    }

    func testAPendingResultUpdatesInPlaceThroughTheContext() {
        var settings = DaemonClient.Settings(
            ["launch": ["extension": ["status": "pending"]]], base: DaemonClient.Settings())
        XCTAssertTrue(FirstRunChecklist.launchLine(settings.launch).contains("editor extension checking…"))
        settings = DaemonClient.Settings(
            ["launch": ["extension": ["status": "changed",
                                      "detail": "editor extension 0.2.0 installed (reload a VS Code window to activate)"]]],
            base: settings)
        XCTAssertTrue(FirstRunChecklist.launchLine(settings.launch).contains("editor extension installed"))
        // An absent block keeps what was held.
        settings = DaemonClient.Settings([:], base: settings)
        XCTAssertEqual(settings.launch.editorExtension.status, "changed")
    }

    // MARK: - notifications

    func testOnlyAnExplicitDenialDrawsTheWarning() {
        XCTAssertTrue(FirstRunChecklist.notificationsDenied("denied"))
        for other in ["authorized", "not_determined", "unavailable", "error", "unknown", ""] {
            XCTAssertFalse(FirstRunChecklist.notificationsDenied(other), other)
        }
        XCTAssertEqual(FirstRunChecklist.deniedLine, "Notifications are off for Dark Army")
    }

    func testNotificationStatusRidesTheContextAndDefaultsUnknown() {
        XCTAssertEqual(DaemonClient.Settings().notificationStatus, "unknown")
        let denied = DaemonClient.Settings(["notification_status": "denied"],
                                           base: DaemonClient.Settings())
        XCTAssertEqual(denied.notificationStatus, "denied")
        // Allowing and returning: the next context clears it, no relaunch.
        let allowed = DaemonClient.Settings(["notification_status": "authorized"], base: denied)
        XCTAssertFalse(FirstRunChecklist.notificationsDenied(allowed.notificationStatus))
    }

    // MARK: - Dark Army's own checkout, own terminal, and the next step

    func testOwnCheckoutDecodesAbsentAsFalseAndPresentAsGiven() throws {
        let snap = try frame(roots: ["/p/a", "/src/dark-army"],
                             facts: [("/p/a", false, false)], own: ["/src/dark-army"])
        let checklist = try XCTUnwrap(snap.enrollment.checklist)
        XCTAssertEqual(checklist.facts(for: "/p/a")?.ownCheckout, false)
        XCTAssertEqual(checklist.facts(for: "/src/dark-army")?.ownCheckout, true)
        XCTAssertEqual(checklist.ownCheckoutRoots, ["/src/dark-army"])
        let explicit = try JSONDecoder().decode(ChecklistRootFacts.self, from: Data(
            #"{"root": "/p/b", "own_checkout": false}"#.utf8))
        XCTAssertFalse(explicit.ownCheckout)
    }

    func testCandidatesDropTheOwnCheckoutAndKeepTheRestSorted() throws {
        let snap = try frame(roots: ["/p/zed", "/src/dark-army", "/p/acme"],
                             own: ["/src/dark-army"])
        XCTAssertEqual(FirstRunChecklist.candidates(enrollment: snap.enrollment).map(\.root),
                       ["/p/acme", "/p/zed"])
        // No facts block at all (an older daemon): nothing is marked, so
        // nothing is dropped.
        let old = try frame(roots: ["/p/b", "/p/a"], checklist: false)
        XCTAssertEqual(FirstRunChecklist.candidates(enrollment: old.enrollment).map(\.root),
                       ["/p/a", "/p/b"])
    }

    func testTheCheckoutAloneLeavesStepOneCurrent() throws {
        var opening = FirstRunChecklist.Opening()
        let snap = try frame(roots: ["/src/dark-army"], own: ["/src/dark-army"])
        XCTAssertFalse(opening.advance(snapshot: snap, completed: false))
        XCTAssertEqual(opening.placement, .checklist)
        XCTAssertNil(opening.selectedRoot)
        XCTAssertEqual(states(FirstRunChecklist.progress(root: opening.selectedRoot,
                                                         enrollment: snap.enrollment)),
                       [.current, .next, .next])
    }

    func testANewcomersFolderBesideTheCheckoutIsTheOneFollowed() throws {
        var opening = FirstRunChecklist.Opening()
        // "/a" sorts before the checkout's root: it would have been picked
        // by the old rule had it been the checkout, and "/z" after.
        let snap = try frame(roots: ["/a/dark-army", "/z/mine"], own: ["/a/dark-army"])
        opening.advance(snapshot: snap, completed: false)
        XCTAssertEqual(opening.selectedRoot, "/z/mine")
        XCTAssertEqual(FirstRunChecklist.selectRoot(
            previous: nil,
            enrolled: FirstRunChecklist.candidates(enrollment: snap.enrollment).map(\.root)),
            "/z/mine")
        // A followed root that turns out to be the checkout is dropped.
        var followed = FirstRunChecklist.Opening()
        followed.advance(snapshot: try frame(roots: ["/a/dark-army"]), completed: false)
        XCTAssertEqual(followed.selectedRoot, "/a/dark-army")
        followed.advance(snapshot: snap, completed: false)
        XCTAssertEqual(followed.selectedRoot, "/z/mine")
    }

    func testOwnTerminalMarksStepTwoNotNeededAndStillCompletes() throws {
        let waiting = try frame(roots: ["/p/a"], facts: [("/p/a", false, false)])
        XCTAssertEqual(states(FirstRunChecklist.progress(root: "/p/a", enrollment: waiting.enrollment,
                                                         ownTerminal: true)),
                       [.done, .notNeeded, .current])
        let observed = try frame(roots: ["/p/a"], facts: [("/p/a", true, false)])
        XCTAssertEqual(states(FirstRunChecklist.progress(root: "/p/a", enrollment: observed.enrollment,
                                                         ownTerminal: true)),
                       [.done, .done, .current])
        let started = try frame(roots: ["/p/a"], facts: [("/p/a", false, true)])
        let progress = FirstRunChecklist.progress(root: "/p/a", enrollment: started.enrollment,
                                                  ownTerminal: true)
        XCTAssertEqual(states(progress), [.done, .notNeeded, .done])
        XCTAssertTrue(progress.complete)
        XCTAssertNil(progress.current)
        // Not enrolled: nothing is excused.
        let none = try frame(roots: [])
        XCTAssertEqual(states(FirstRunChecklist.progress(root: "/p/a", enrollment: none.enrollment,
                                                         ownTerminal: true)),
                       [.current, .next, .next])
    }

    func testOwnTerminalOffIsTodaysTable() throws {
        for (editor, session) in [(false, false), (true, false), (false, true), (true, true)] {
            for enrolled in [false, true] {
                let snap = try frame(roots: enrolled ? ["/p/a"] : [],
                                     facts: [("/p/a", editor, session)])
                XCTAssertEqual(
                    FirstRunChecklist.progress(root: "/p/a", enrollment: snap.enrollment,
                                               ownTerminal: false),
                    FirstRunChecklist.progress(root: "/p/a", enrollment: snap.enrollment))
                XCTAssertFalse(states(FirstRunChecklist.progress(
                    root: "/p/a", enrollment: snap.enrollment)).contains(.notNeeded))
            }
        }
    }

    func testTheNotNeededRowReadsItsWord() {
        let step = FirstRunChecklist.Step(id: 1, title: "Open it in VS Code", state: .notNeeded)
        XCTAssertEqual(step.accessibleLabel, "Not needed: Open it in VS Code")
        XCTAssertTrue(FirstRunChecklist.notNeededNote.contains("Dark Army's own terminal"))
    }

    func testOpeningCompletesWithOwnTerminalAndNoEditor() throws {
        var opening = FirstRunChecklist.Opening()
        opening.advance(snapshot: try frame(roots: ["/p/a"]), completed: false, ownTerminal: true)
        XCTAssertEqual(opening.placement, .checklist)
        XCTAssertTrue(opening.advance(snapshot: try frame(at: 110, roots: ["/p/a"],
                                                          facts: [("/p/a", false, true)]),
                                      completed: false, ownTerminal: true))
        XCTAssertEqual(opening.placement, .completed)
        // The same frames without the flag wait on the editor.
        var plain = FirstRunChecklist.Opening()
        plain.advance(snapshot: try frame(roots: ["/p/a"]), completed: false)
        XCTAssertFalse(plain.advance(snapshot: try frame(at: 110, roots: ["/p/a"],
                                                         facts: [("/p/a", false, true)]),
                                     completed: false))
        XCTAssertEqual(plain.placement, .checklist)
    }

    func testStepTwoNamesTrustAndStepThreeNamesOnlyTheTerminalRoute() {
        let open = FirstRunChecklist.Step(id: 1, title: "", state: .current)
        let start = FirstRunChecklist.Step(id: 2, title: "", state: .current)
        let hint = FirstRunChecklist.instruction(for: open, folderLabel: "acme")
        XCTAssertTrue(hint.contains("trust the folder's authors"))
        XCTAssertTrue(hint.contains("Waiting for VS Code to connect"))
        // The board files a card only under a folder open in VS Code, so
        // step 3 never sends a newcomer with no window there.
        let terminal = FirstRunChecklist.instruction(for: start, folderLabel: "acme")
        XCTAssertTrue(terminal.contains("in a terminal inside acme"))
        XCTAssertFalse(terminal.contains("board"))
        XCTAssertFalse(terminal.contains("START"))
    }

    private func boardProject(_ name: String, _ root: String) throws -> BoardProject {
        try JSONDecoder().decode(BoardProject.self, from: Data(
            #"{"name": "\#(name)", "root": "\#(root)"}"#.utf8))
    }

    func testTheFirstCardIsFiledOnlyWhereTheBoardOffersTheFolder() throws {
        let offered = [try boardProject("Acme App", "/p/acme"), try boardProject("zed", "/p/zed")]
        XCTAssertEqual(FirstRunChecklist.firstCardFiling(selectedRoot: "/p/acme",
                                                         boardProjects: offered),
                       .init(project: "Acme App", root: "/p/acme"))
        // Own terminal, no VS Code window: the board does not list the
        // folder, so the composer opens blank rather than pre-filled with a
        // root `board_create` would refuse.
        XCTAssertEqual(FirstRunChecklist.firstCardFiling(selectedRoot: "/p/mine",
                                                         boardProjects: offered),
                       .init(project: "", root: ""))
        XCTAssertEqual(FirstRunChecklist.firstCardFiling(selectedRoot: nil,
                                                         boardProjects: offered),
                       .init(project: "", root: ""))
        XCTAssertEqual(FirstRunChecklist.firstCardFiling(selectedRoot: "",
                                                         boardProjects: [try boardProject("", "")]),
                       .init(project: "", root: ""))
    }

    func testSetupCompleteNamesTheFirstCard() {
        XCTAssertTrue(FirstRunChecklist.completeLine.contains("write your first card"))
        XCTAssertTrue(FirstRunChecklist.completeLine.contains("START"))
        XCTAssertEqual(FirstRunChecklist.firstCardAction, "Write your first card")
    }
}
