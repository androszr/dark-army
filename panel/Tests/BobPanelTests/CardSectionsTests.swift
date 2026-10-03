import XCTest
@testable import BobPanel

/// The card detail's per-stage order, tabled.
///
/// `plans/2026-09-12-card-detail-folds-per-column.md`. Every decision behind
/// the card window's and the phone card screen's sections — which stage a
/// card is in, the order of its sections, which open by default, which are
/// never folded, and what a closed row says — is a pure function here. The
/// views read these and re-derive none of them, so this file is the whole
/// decision; `host/tests/test_card_sections.py` pins that the phone's copy is
/// the same text.
final class CardSectionsTests: XCTestCase {

    func testSixStages() {
        XCTAssertEqual(CardSections.Stage.allCases.count, 6)
    }

    /// Column first, then the two In-progress splits; the tool is not an
    /// argument, so a Codex or Grok card can only land where a Claude one
    /// does — asserted by the signature having nowhere to put it.
    func testStageOverEveryColumn() {
        XCTAssertEqual(CardSections.stage(column: "prep", linkState: "",
                                          manualCheckDue: false, runActive: true), .prep)
        XCTAssertEqual(CardSections.stage(column: "backlog", linkState: "",
                                          manualCheckDue: false, runActive: true), .backlog)
        XCTAssertEqual(CardSections.stage(column: "done", linkState: "ended",
                                          manualCheckDue: true, runActive: true), .done)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "live",
                                          manualCheckDue: false, runActive: true), .running)
        XCTAssertEqual(CardSections.stage(column: "in_progress",
                                          linkState: "dispatching",
                                          manualCheckDue: false, runActive: true), .running)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "ended",
                                          manualCheckDue: false, runActive: true), .ended)
        // The check wins over the link: a due check is why the card is open.
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "ended",
                                          manualCheckDue: true, runActive: true), .manualCheck)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "live",
                                          manualCheckDue: true, runActive: true), .manualCheck)
        // An unknown column is Backlog, `BoardColumn(rawValue:) ?? .backlog`.
        XCTAssertEqual(CardSections.stage(column: "review", linkState: "",
                                          manualCheckDue: false, runActive: true), .backlog)
        XCTAssertEqual(CardSections.stage(column: "", linkState: "",
                                          manualCheckDue: false, runActive: true), .backlog)
    }

    /// `run_active` is the band's own RUN/ENDED split, and the card screen
    /// agrees with it: a bound card the daemon says is not running is
    /// `.ended` — the `live` card whose session Dark Army can no longer hear,
    /// which never reaches `link_state == "ended"` — while a card with no
    /// link at all is `.running` by column (the launcher-off move, a card
    /// sent Back and dropped in again) and only its words differ. The
    /// check still wins, and outside In progress the flag is nothing.
    func testStageReadsRunActiveForABoundCardOnly() {
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "live",
                                          manualCheckDue: false, runActive: false), .ended)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "live",
                                          manualCheckDue: false, runActive: true), .running)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "dispatching",
                                          manualCheckDue: false, runActive: false), .ended)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "",
                                          manualCheckDue: false, runActive: false), .running)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "",
                                          manualCheckDue: false, runActive: true), .running)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "ended",
                                          manualCheckDue: false, runActive: true), .ended)
        XCTAssertEqual(CardSections.stage(column: "in_progress", linkState: "live",
                                          manualCheckDue: true, runActive: false), .manualCheck)
        for column in ["prep", "backlog", "done", "review", ""] {
            XCTAssertEqual(CardSections.stage(column: column, linkState: "live",
                                              manualCheckDue: false, runActive: false),
                           CardSections.stage(column: column, linkState: "live",
                                              manualCheckDue: false, runActive: true),
                           column)
        }
    }

    /// A due check in Prep or Backlog is not a stage: `manual_check_due`
    /// only ever rides an In progress card, and the column still decides.
    func testManualCheckOnlySplitsInProgress() {
        XCTAssertEqual(CardSections.stage(column: "prep", linkState: "",
                                          manualCheckDue: true, runActive: true), .prep)
        XCTAssertEqual(CardSections.stage(column: "backlog", linkState: "",
                                          manualCheckDue: true, runActive: true), .backlog)
    }

    /// Every order starts with status, ends with Delete, and names no section
    /// twice.
    func testEveryOrderIsWellFormed() {
        for stage in CardSections.Stage.allCases {
            let order = CardSections.order(for: stage)
            XCTAssertEqual(order.first, .status, "\(stage)")
            XCTAssertEqual(order.last, .danger, "\(stage)")
            XCTAssertEqual(Set(order).count, order.count, "\(stage) repeats a section")
            XCTAssertGreaterThanOrEqual(order.count, 11, "\(stage)")
        }
    }

    /// Every section named as a lead, and every pinned one, is actually in
    /// that stage's order — a lead nobody draws is a typo.
    func testLeadsAndPinnedAreInTheOrder() {
        for stage in CardSections.Stage.allCases {
            let order = Set(CardSections.order(for: stage))
            for lead in CardSections.leads(for: stage) {
                XCTAssertTrue(order.contains(lead), "\(stage) leads with \(lead) but never draws it")
            }
            for pin in CardSections.pinned {
                XCTAssertTrue(order.contains(pin), "\(stage) omits pinned \(pin)")
            }
        }
    }

    /// The leads are the *top* of the list: every open-by-default section
    /// sits before every folded one. (Pinned sections are not leads — QUEUED
    /// is pinned so it is never hidden, but it is not the point of a Backlog
    /// card, so it may sit below the plan.)
    func testLeadsComeFirst() {
        for stage in CardSections.Stage.allCases {
            let order = CardSections.order(for: stage)
            let leads = CardSections.leads(for: stage)
            let lastLead = order.lastIndex { leads.contains($0) } ?? -1
            let firstFolded = order.firstIndex {
                !leads.contains($0) && !CardSections.pinned.contains($0)
            } ?? order.count
            XCTAssertLessThan(lastLead, firstFolded, "\(stage): a folded section precedes a lead")
        }
    }

    /// The leads per stage, as `plans/2026-09-20-simplify-card-details.md`
    /// states them: nothing opens by default but the stated exceptions —
    /// the check's steps on a card waiting on one, the close note on a
    /// finished card, and (21 Sep 2026) a scout's report once the scout has
    /// come back. Everything else is behind MORE.
    func testTheLeadsPerStage() {
        XCTAssertEqual(CardSections.leads(for: .prep), [])
        XCTAssertEqual(CardSections.leads(for: .backlog), [])
        XCTAssertEqual(CardSections.leads(for: .running), [])
        XCTAssertEqual(CardSections.leads(for: .manualCheck), [.manualCheck])
        XCTAssertEqual(CardSections.leads(for: .ended), [.report])
        XCTAssertEqual(CardSections.leads(for: .done), [.closeSignature, .report])
        XCTAssertEqual(CardSections.pinned, [.status, .verbs, .queue, .dependencies])
    }

    /// WAITS ON (24 Sep 2026) is in every order exactly once, right after
    /// QUEUED and before MORE, pinned so it is never behind a chevron, and
    /// its closed-row tail is the number of cards it waits on.
    func testTheDependenciesSectionFollowsTheQueueEverywhere() {
        XCTAssertEqual(CardSections.Section.dependencies.rawValue, "WAITS ON")
        for stage in CardSections.Stage.allCases {
            let order = CardSections.order(for: stage)
            XCTAssertEqual(order.filter { $0 == .dependencies }.count, 1, "\(stage)")
            let queue = order.firstIndex(of: .queue)!
            XCTAssertEqual(order.firstIndex(of: .dependencies), queue + 1, "\(stage)")
            XCTAssertLessThan(order.firstIndex(of: .dependencies)!,
                              order.firstIndex(of: .more)!, "\(stage)")
            XCTAssertFalse(CardSections.hidden(for: stage).contains(.dependencies))
            XCTAssertTrue(CardSections.isOpen(.dependencies, stage: stage, opened: []))
        }
        var f = CardSections.Facts()
        XCTAssertEqual(CardSections.rowText(.dependencies, facts: f), "WAITS ON")
        f.dependencies = 2
        XCTAssertEqual(CardSections.rowText(.dependencies, facts: f), "WAITS ON · 2")
    }

    /// REPORT is in every order — a scout may sit in any column — leads
    /// where the scout has come back and nowhere else, sits right under the
    /// close note on Done, and its row says whether the report is in.
    func testTheReportSection() {
        XCTAssertEqual(CardSections.Section.report.rawValue, "REPORT")
        for stage in CardSections.Stage.allCases {
            XCTAssertTrue(CardSections.order(for: stage).contains(.report), "\(stage)")
        }
        let done = CardSections.order(for: .done)
        XCTAssertEqual(done.firstIndex(of: .report),
                       done.firstIndex(of: .closeSignature)! + 1)
        XCTAssertTrue(CardSections.hidden(for: .running).contains(.report))
        XCTAssertTrue(CardSections.hidden(for: .prep).contains(.report))
        XCTAssertFalse(CardSections.pinned.contains(.report))
        var f = CardSections.Facts()
        XCTAssertEqual(CardSections.summary(.report, facts: f), "")
        f.isScout = true
        XCTAssertEqual(CardSections.rowText(.report, facts: f), "REPORT · still out")
        f.reportAttached = true
        XCTAssertEqual(CardSections.rowText(.report, facts: f), "REPORT · attached")
    }

    /// A card waiting on a check opens with the status line, then the check
    /// itself, then the next action, then the queue line and what it waits
    /// on, then MORE, with
    /// the work record the first row behind it — and Delete last.
    func testTheManualCheckOrderLeadsWithTheCheck() {
        let order = CardSections.order(for: .manualCheck)
        XCTAssertEqual(Array(order.prefix(7)),
                       [.status, .manualCheck, .verbs, .queue, .dependencies, .more,
                        .workRecord])
        XCTAssertEqual(order.last, .danger)
    }

    /// For every stage, nothing sits above the MORE row but the pinned
    /// three and the stage's lead, and a lead is only ever one of the three
    /// stated exceptions. MORE appears exactly once and is neither pinned
    /// nor a lead, or `hidden(for:)` would hand the row back to itself.
    func testNothingAboveMoreButTheLead() {
        for stage in CardSections.Stage.allCases {
            let order = CardSections.order(for: stage)
            XCTAssertEqual(order.filter { $0 == .more }.count, 1, "\(stage)")
            let above = Set(order.prefix { $0 != .more })
            let allowed = CardSections.pinned.union(CardSections.leads(for: stage))
            XCTAssertTrue(above.isSubset(of: allowed), "\(stage): \(above) above MORE")
            XCTAssertTrue(CardSections.leads(for: stage)
                            .isSubset(of: [.manualCheck, .closeSignature, .report]), "\(stage)")
            XCTAssertFalse(CardSections.leads(for: stage).contains(.more), "\(stage)")
        }
        XCTAssertFalse(CardSections.pinned.contains(.more))
    }

    /// What MORE hides is the order minus what is above it and minus the row
    /// itself, in the order's own order.
    func testHiddenIsTheOrderMinusTheLeadAndMore() {
        for stage in CardSections.Stage.allCases {
            let order = CardSections.order(for: stage)
            let more = order.firstIndex(of: .more)!
            let expected = Array(order[(more + 1)...])
            XCTAssertEqual(CardSections.hidden(for: stage), expected, "\(stage)")
            XCTAssertFalse(CardSections.hidden(for: stage).contains(.more), "\(stage)")
            for s in CardSections.hidden(for: stage) {
                XCTAssertFalse(CardSections.isOpen(s, stage: stage, opened: []), "\(stage) \(s)")
            }
        }
    }

    /// MORE and everything above it are shown with nothing pressed; a
    /// hidden section is shown only once MORE is open, whatever else is.
    func testShownOverMore() {
        for stage in CardSections.Stage.allCases {
            XCTAssertTrue(CardSections.shown(.more, stage: stage, opened: []), "\(stage)")
            for s in CardSections.order(for: stage).prefix(while: { $0 != .more }) {
                XCTAssertTrue(CardSections.shown(s, stage: stage, opened: []), "\(stage) \(s)")
            }
            for s in CardSections.hidden(for: stage) {
                XCTAssertFalse(CardSections.shown(s, stage: stage, opened: []), "\(stage) \(s)")
                XCTAssertFalse(CardSections.shown(s, stage: stage, opened: [s]), "\(stage) \(s)")
                XCTAssertTrue(CardSections.shown(s, stage: stage, opened: [.more]), "\(stage) \(s)")
            }
            for s in CardSections.order(for: stage) {
                XCTAssertTrue(CardSections.shown(s, stage: stage, opened: [.more]), "\(stage) \(s)")
            }
        }
        // MORE's own open state is `isOpen` over `opened`, like any section.
        XCTAssertFalse(CardSections.isOpen(.more, stage: .prep, opened: []))
        XCTAssertTrue(CardSections.isOpen(.more, stage: .prep, opened: [.more]))
    }

    /// The one next action per stage, over what the screen can reach —
    /// including every `.none`.
    func testNextActionPerStage() {
        typealias R = CardSections.Reach
        let nothing = R()
        for stage in CardSections.Stage.allCases {
            XCTAssertEqual(CardSections.nextAction(stage: stage, reach: nothing), .none, "\(stage)")
        }
        // Prep: Refine first, Start where Refine is not offered.
        XCTAssertEqual(CardSections.nextAction(stage: .prep, reach: R(canRefine: true, canStart: true)), .refine)
        XCTAssertEqual(CardSections.nextAction(stage: .prep, reach: R(canStart: true)), .start)
        XCTAssertEqual(CardSections.nextAction(stage: .prep, reach: R(canReply: true, canReview: true)), .none)
        // Backlog: Start, and only Start.
        XCTAssertEqual(CardSections.nextAction(stage: .backlog, reach: R(canStart: true)), .start)
        XCTAssertEqual(CardSections.nextAction(stage: .backlog, reach: R(canRefine: true, canReply: true)), .none)
        // Running: the message box.
        XCTAssertEqual(CardSections.nextAction(stage: .running, reach: R(canReply: true)), .reply)
        XCTAssertEqual(CardSections.nextAction(stage: .running, reach: R(canStart: true, canMarkDone: true)), .none)
        // Manual check: Mark checked, else the message box.
        XCTAssertEqual(CardSections.nextAction(stage: .manualCheck, reach: R(canReply: true, canMarkChecked: true)), .markChecked)
        XCTAssertEqual(CardSections.nextAction(stage: .manualCheck, reach: R(canReply: true)), .reply)
        XCTAssertEqual(CardSections.nextAction(stage: .manualCheck, reach: R(canMarkDone: true)), .none)
        // Ended: Done.
        XCTAssertEqual(CardSections.nextAction(stage: .ended, reach: R(canMarkDone: true)), .done)
        XCTAssertEqual(CardSections.nextAction(stage: .ended, reach: R(canReply: true, canReview: true)), .none)
        // Done: Reviewed.
        XCTAssertEqual(CardSections.nextAction(stage: .done, reach: R(canReview: true)), .review)
        XCTAssertEqual(CardSections.nextAction(stage: .done, reach: R(canStart: true, canMarkDone: true)), .none)
        // The labels the surfaces draw for the plain buttons.
        XCTAssertEqual(CardSections.NextAction.start.rawValue, "START")
        XCTAssertEqual(CardSections.NextAction.reply.rawValue, "SEND A MESSAGE")
        XCTAssertEqual(CardSections.NextAction.none.rawValue, "")
    }

    /// One sentence per stage, always present, and the manual-check one is
    /// `statusLine` itself. `linked` changes one sentence and one only: an
    /// In progress card with no session bound is not "being worked".
    func testStateWordsPerStage() {
        XCTAssertEqual(CardSections.stateWords(for: .prep), "Not planned yet")
        XCTAssertEqual(CardSections.stateWords(for: .backlog), "Planned — ready to start")
        XCTAssertEqual(CardSections.stateWords(for: .running), "An assistant is working on it")
        XCTAssertEqual(CardSections.stateWords(for: .manualCheck), CardSections.statusLine)
        XCTAssertEqual(CardSections.stateWords(for: .ended), "The run ended — not marked done yet")
        XCTAssertEqual(CardSections.stateWords(for: .done), "Done")
        XCTAssertEqual(CardSections.stateWords(for: .running, linked: true),
                       "An assistant is working on it")
        XCTAssertEqual(CardSections.stateWords(for: .running, linked: false),
                       "In progress — nobody working on it yet")
        for stage in CardSections.Stage.allCases {
            XCTAssertFalse(CardSections.stateWords(for: stage).isEmpty, "\(stage)")
            XCTAssertEqual(CardSections.stateWords(for: stage),
                           CardSections.stateWords(for: stage, linked: true), "\(stage)")
            if stage != .running {
                XCTAssertEqual(CardSections.stateWords(for: stage, linked: false),
                               CardSections.stateWords(for: stage, linked: true), "\(stage)")
            }
        }
    }

    /// The MORE row says how many sections it hides, and nothing at zero.
    func testMoreRowText() {
        var f = CardSections.Facts()
        XCTAssertEqual(CardSections.rowText(.more, facts: f), "MORE")
        f.hidden = 9
        XCTAssertEqual(CardSections.rowText(.more, facts: f), "MORE · 9")
        XCTAssertEqual(CardSections.summary(.more, facts: f), "9")
        XCTAssertEqual(CardSections.Section.more.rawValue, "MORE")
        XCTAssertEqual(CardSections.Section.otherVerbs.rawValue, "OTHER ACTIONS")
        XCTAssertEqual(CardSections.Section.collaboration.rawValue, "COLLABORATION")
    }

    /// The timeline is a folded row on every stage, immediately before
    /// ATTACHMENTS or DOCUMENTS, and never a lead: a card's history is there
    /// to read, not the reason the card is open. (WAITING ON, which used to
    /// follow it, is gone: cards no longer wait on other cards.)
    func testTheTimelinePrecedesTheFilesEverywhereAndLeadsNowhere() {
        for stage in CardSections.Stage.allCases {
            let order = CardSections.order(for: stage)
            guard let t = order.firstIndex(of: .timeline) else {
                return XCTFail("\(stage) omits the timeline")
            }
            XCTAssertTrue([.attachments, .documents].contains(order[t + 1]),
                          "\(stage): TIMELINE is not just before the files")
            XCTAssertFalse(CardSections.leads(for: stage).contains(.timeline), "\(stage)")
        }
        XCTAssertNil(CardSections.Section(rawValue: "WAITING ON"))
        XCTAssertFalse(CardSections.pinned.contains(.timeline))
        XCTAssertEqual(CardSections.Section.timeline.rawValue, "TIMELINE")
    }

    func testIsOpenOverPinnedLeadOpenedAndNeither() {
        XCTAssertTrue(CardSections.isOpen(.status, stage: .prep, opened: []))
        XCTAssertTrue(CardSections.isOpen(.manualCheck, stage: .manualCheck, opened: []))
        XCTAssertTrue(CardSections.isOpen(.plan, stage: .prep, opened: [.plan]))
        XCTAssertFalse(CardSections.isOpen(.plan, stage: .prep, opened: []))
        // Nothing but the two stated exceptions opens by itself any more.
        XCTAssertFalse(CardSections.isOpen(.editor, stage: .prep, opened: []))
        XCTAssertFalse(CardSections.isOpen(.editor, stage: .done, opened: []))
    }

    func testSummaryWords() {
        var f = CardSections.Facts()
        f.filesTotal = 1
        XCTAssertEqual(CardSections.summary(.workRecord, facts: f), "1 file")
        f.filesTotal = 4
        XCTAssertEqual(CardSections.summary(.workRecord, facts: f), "4 files")
        f.filesAvailable = false
        XCTAssertEqual(CardSections.summary(.workRecord, facts: f), "folder unreadable")
        var p = CardSections.Facts()
        p.planAttached = true
        XCTAssertEqual(CardSections.summary(.plan, facts: p), "attached")
        p.planApproved = true
        XCTAssertEqual(CardSections.summary(.plan, facts: p), "approved")
        p.planMissing = true
        XCTAssertEqual(CardSections.summary(.plan, facts: p), "missing")
        var m = CardSections.Facts()
        m.manualNote = true
        XCTAssertEqual(CardSections.summary(.manualCheck, facts: m), "note")
        XCTAssertEqual(CardSections.summary(.status, facts: m), "")
        XCTAssertEqual(CardSections.summary(.danger, facts: m), "")
        // The timeline's tail is the card's age, already formatted.
        XCTAssertEqual(CardSections.summary(.timeline, facts: m), "")
        var t = CardSections.Facts()
        t.age = "3d 4h"
        XCTAssertEqual(CardSections.summary(.timeline, facts: t), "3d 4h")
        XCTAssertEqual(CardSections.rowText(.timeline, facts: t), "TIMELINE · 3d 4h")
    }

    /// The row text is the label alone where nothing is known, and
    /// `label · tail` otherwise, with the separator spaced.
    func testRowText() {
        XCTAssertEqual(CardSections.rowText(.editor, facts: CardSections.Facts()), "EDIT CARD")
        var f = CardSections.Facts()
        f.filesTotal = 4
        XCTAssertEqual(CardSections.rowText(.workRecord, facts: f), "WHAT CHANGED · 4 files")
        XCTAssertEqual(CardSections.statusLine, "Finished — waiting on your check")
    }
}

/// CHANGES (review and merge, `plans/2026-10-03-review-and-merge-done-card.md`):
/// a folded section in the Done and ended orders, behind MORE, never a lead
/// and never pinned, so the git read it opens is a press and not a default.
final class CardSectionsChangesTests: XCTestCase {
    func testChangesIsFoldedBehindMoreInDoneAndEnded() {
        for stage in [CardSections.Stage.done, .ended] {
            let order = CardSections.order(for: stage)
            XCTAssertEqual(order.filter { $0 == .changes }.count, 1, "\(stage)")
            XCTAssertGreaterThan(order.firstIndex(of: .changes)!,
                                 order.firstIndex(of: .more)!, "\(stage)")
            XCTAssertFalse(CardSections.leads(for: stage).contains(.changes))
            XCTAssertFalse(CardSections.pinned.contains(.changes))
        }
        XCTAssertEqual(CardSections.rowText(.changes, facts: CardSections.Facts()),
                       "CHANGES")
    }
}
