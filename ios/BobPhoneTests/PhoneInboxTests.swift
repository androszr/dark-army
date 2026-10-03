import SwiftUI
import UIKit
import XCTest
@testable import BobPhone

/// Table-driven projection, count, and sheet-routing fixtures.
@MainActor
final class PhoneInboxTests: XCTestCase {

    private func snapshot(_ json: String) throws -> Snapshot {
        try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    }

    private func keys(_ items: [PhoneInboxItem]) -> [String] {
        items.map(\.target.key)
    }

    private func ranks(_ items: [PhoneInboxItem]) -> [Int] {
        items.map(\.kind.rawValue)
    }

    // MARK: - five wire kinds, three kinds

    func testFiveWireKindsFoldIntoThreeAndEverythingCounts() throws {
        let snap = try snapshot("""
        {
          "agents": {"waiting": [
            {"session_id":"p1","nickname":"Perm","project":"P"},
            {"session_id":"q1","nickname":"Ask","project":"P",
             "questions":[{"text":"ok?"}]},
            {"session_id":"w1","nickname":"Wait","project":"P"}
          ]},
          "permissions": [
            {"request_id":"pr","session_id":"p1","tool_name":"Bash",
             "description":"ls"}
          ],
          "board": {"cards": [
            {"id":"e1","title":"Ended","project":"P","needs_you":true},
            {"id":"m1","title":"Manual","project":"P",
             "manual_check_due":true,"manual_steps":"look"},
            {"id":"r1","title":"Review","project":"P","closed_by":"cipher",
             "close_note":"shipped"},
            {"id":"pl1","title":"Plan","project":"P","column_name":"backlog",
             "plan_path":"plans/x.md"}
          ]},
          "security": {"available": true, "alerts": [
            {"id":"b1","ts":5,"door":"lan","door_word":"Wi-Fi","peer":"10.0.0.9",
             "count":5,"window_seconds":600,"text":"5 refused attempts"}
          ]}
        }
        """)
        let items = snap.decisionItems
        // The closed card, the ready plan and the burst alert are not
        // entries: the Done column, the Board and the access log carry them.
        XCTAssertEqual(items.count, 5)
        XCTAssertEqual(snap.needsYouCount, 5)
        XCTAssertEqual(Set(keys(items)).count, 5)
        XCTAssertEqual(items.map(\.wire), [
            .permission, .question, .endedWork, .manualCheck, .waiting
        ])
        XCTAssertEqual(items.map(\.kind), [.answer, .answer, .look, .look, .stopped])
        XCTAssertEqual(ranks(items), [0, 0, 1, 1, 2])
        XCTAssertEqual(items.map(\.target.key), [
            "s:p1", "s:q1", "c:e1", "c:m1", "s:w1"
        ])
        XCTAssertEqual(PhoneInboxKind.allCases.map(\.word), ["ANSWER", "LOOK AT", "STOPPED"])
        XCTAssertTrue(items.allSatisfy { $0.project == "P" })
    }

    func testOnlyThePermissionAskIsNotDismissable() {
        XCTAssertFalse(PhoneInboxWireKind.permission.dismissable)
        for wire in [PhoneInboxWireKind.question, .endedWork, .manualCheck, .waiting] {
            XCTAssertTrue(wire.dismissable, wire.name)
        }
    }

    func testRepeatedSessionCollapsesToOnePermission() throws {
        let snap = try snapshot("""
        {
          "agents": {
            "running": [{"session_id":"dup","nickname":"Run","project":"P",
                         "questions":[{"text":"q1"},{"text":"q2"}]}],
            "waiting": [{"session_id":"dup","nickname":"Wait","project":"P"}]
          },
          "permissions": [
            {"request_id":"a","session_id":"dup","tool_name":"Read"},
            {"request_id":"b","session_id":"dup","tool_name":"Write"}
          ],
          "notifications": [{"session_id":"dup","message":"n"}]
        }
        """)
        let items = snap.decisionItems
        XCTAssertEqual(items.count, 1)
        XCTAssertEqual(items[0].wire, .permission)
        XCTAssertEqual(items[0].target.key, "s:dup")
        XCTAssertEqual(items[0].category, .running)
        XCTAssertEqual(snap.permissions.count, 2)
        XCTAssertEqual(items[0].agent(in: snap)?.questionList.count, 2)
    }

    /// A card bound to a session is one subject with it (20 Sep 2026): the
    /// permission on `same` outranks `other`'s check, so `c:other` is
    /// dropped; `c:same` shares only the *id* and stays. The desktop's
    /// `Inbox.oneEntryPerSubject` is the same rule.
    func testACardAndItsBoundSessionAreOneSubject() throws {
        let snap = try snapshot("""
        {
          "agents": {"waiting": [
            {"session_id":"same","nickname":"Agent","project":"P"}
          ]},
          "permissions": [
            {"request_id":"pr","session_id":"same","tool_name":"Bash"}
          ],
          "board": {"cards": [
            {"id":"same","title":"Linked","project":"P","manual_check_due":true,
             "manual_steps":"check"},
            {"id":"other","title":"Bound","project":"P","session_id":"same",
             "manual_check_due":true,"manual_steps":"too"}
          ]}
        }
        """)
        let items = snap.decisionItems
        XCTAssertEqual(items.count, 2)
        XCTAssertEqual(snap.needsYouCount, 2)
        XCTAssertEqual(Set(keys(items)), ["s:same", "c:same"])
    }

    /// The screenshot case: one agent finished a card, flagged its check
    /// and stopped — STOPPED and LOOK AT were two entries for one subject.
    /// The card's check wins over the plain wait.
    func testAWaitingSessionAndItsFlaggedCardCountOnce() throws {
        let snap = try snapshot("""
        {
          "agents": {"waiting": [
            {"session_id":"d1","nickname":"Vex","project":"P"}
          ]},
          "board": {"cards": [
            {"id":"usd","title":"USD/PLN tile","project":"P","session_id":"d1",
             "manual_check_due":true,"manual_steps":"1. Run it."}
          ]}
        }
        """)
        XCTAssertEqual(keys(snap.decisionItems), ["c:usd"])
        XCTAssertEqual(snap.needsYouCount, 1)
    }

    func testCardPrecedenceClearsToTheNextReason() throws {
        func cardJSON(needsYou: Bool, manual: Bool, reviewed: String) -> String {
            """
            {"board":{"cards":[{
              "id":"c1","title":"Stack","project":"P",
              "needs_you":\(needsYou),
              "manual_check_due":\(manual),
              "closed_by":"cipher"
              \(reviewed)
            }]}}
            """
        }
        var snap = try snapshot(cardJSON(needsYou: true, manual: true, reviewed: ""))
        XCTAssertEqual(snap.decisionItems.map(\.wire), [.endedWork])
        snap = try snapshot(cardJSON(needsYou: false, manual: true, reviewed: ""))
        XCTAssertEqual(snap.decisionItems.map(\.wire), [.manualCheck])
        // A closed card awaiting review is the Done column's, not an entry.
        snap = try snapshot(cardJSON(needsYou: false, manual: false, reviewed: ""))
        XCTAssertTrue(snap.decisionItems.isEmpty)
        snap = try snapshot(cardJSON(needsYou: false, manual: false,
                                     reviewed: #","reviewed_at": 12"#))
        XCTAssertTrue(snap.decisionItems.isEmpty)
        XCTAssertEqual(snap.needsYouCount, 0)
    }

    func testPublishedFlagsAreRequiredNotInferred() throws {
        let snap = try snapshot("""
        {"board":{"cards":[
          {"id":"steps","title":"Steps","manual_steps":"do it",
           "manual_check_due":false},
          {"id":"ended","title":"Ended","link_state":"ended","needs_you":false}
        ]}}
        """)
        XCTAssertTrue(snap.decisionItems.isEmpty)
    }

    func testAClosedCardAndAReadyPlanAreNotEntriesWhateverTheirFields() throws {
        let snap = try snapshot("""
        {"board":{"cards":[
          {"id":"author","title":"Author","closed_by":"cipher"},
          {"id":"null","title":"Null","closed_by":"cipher","reviewed_at":null},
          {"id":"pl","title":"Plan","column_name":"backlog","plan_path":"p.md",
           "session_id":"","refine_state":"","queue_reason":"waiting for a slot"},
          {"id":"done","title":"Done","column_name":"done"}
        ]}}
        """)
        XCTAssertTrue(snap.decisionItems.isEmpty)
        XCTAssertEqual(snap.needsYouCount, 0)
        let empty = try snapshot("{}")
        XCTAssertTrue(empty.decisionItems.isEmpty)
        XCTAssertEqual(empty.needsYouCount, 0)
    }

    func testAdmissionUsesSignalsAndKeepsActualCategories() throws {
        let withNote = try snapshot("""
        {
          "agents": {
            "running": [
              {"session_id":"runp","nickname":"RunP"},
              {"session_id":"run","nickname":"Run"}
            ],
            "sleeping": [
              {"session_id":"sleepq","nickname":"SleepQ",
               "questions":[{"text":"huh?"}]},
              {"session_id":"sleep","nickname":"Sleep"}
            ],
            "finished": [{"session_id":"fin","nickname":"Fin"}],
            "abandoned": [{"session_id":"note","nickname":"Note"}]
          },
          "permissions": [
            {"request_id":"pr","session_id":"runp","tool_name":"Bash"}
          ],
          "notifications": [{"session_id":"note","message":"n"}]
        }
        """)
        // The Mac's admission: a prompt admits `runp`; a bare question on a
        // sleeping row admits nothing (it decides the kind, never the
        // entry), and an abandoned row is never listed however its card
        // reads — the desk's `Category.live` excludes it, and an entry the
        // Mac never has is one the phone keeps after the desk moved on.
        XCTAssertEqual(Set(keys(withNote.decisionItems)), ["s:runp"])
        let byKey = Dictionary(uniqueKeysWithValues:
            withNote.decisionItems.map { ($0.target.key, $0) })
        XCTAssertEqual(byKey["s:runp"]?.wire, .permission)
        XCTAssertEqual(byKey["s:runp"]?.category, .running)
        XCTAssertTrue(PhoneInbox.spokenLabel(for: try XCTUnwrap(byKey["s:runp"]))
            .contains("running"))
        XCTAssertFalse(withNote.agents.waiting.contains {
            $0.sessionId == "runp"
        })
    }

    func testAFinishedRowNeedsNobodyWhateverItStillCarries() throws {
        // A tombstone or a quiet-promoted row keeps its last shape — the
        // state it ended in, a question list, sometimes a card the desk
        // already dismissed. None of it is an entry: the Mac lists only
        // live rows, and the phone must not keep an alert the Mac has not.
        let snap = try snapshot("""
        {
          "agents": {
            "waiting": [{"session_id":"live","nickname":"Live"}],
            "finished": [
              {"session_id":"fin","nickname":"Fin","state":"confused",
               "questions":[{"text":"still?"}]},
              {"session_id":"finp","nickname":"FinP"}
            ],
            "abandoned": [{"session_id":"gone","nickname":"Gone"}]
          },
          "permissions": [
            {"request_id":"pr","session_id":"finp","tool_name":"Bash"}
          ],
          "notifications": [
            {"session_id":"fin","message":"n"},
            {"session_id":"gone","message":"n"}
          ]
        }
        """)
        XCTAssertEqual(keys(snap.decisionItems), ["s:live"])
        // The finished row's prompt is not re-listed as an orphan either:
        // the row was seen, it was simply not an entry.
        XCTAssertEqual(snap.needsYouCount, 1)
    }

    func testOrphanPermissionUpgradesAndEmptyIdsCreateNothing() throws {
        let orphan = try snapshot("""
        {"permissions":[
          {"request_id":"pr","session_id":"ghost","tool_name":"Bash",
           "description":"ls"},
          {"request_id":"empty","session_id":"","tool_name":"Read"}
        ],
         "board":{"cards":[{"id":"","title":"Nope","needs_you":true}]}}
        """)
        XCTAssertEqual(orphan.decisionItems.count, 1)
        let item = orphan.decisionItems[0]
        XCTAssertEqual(item.target.key, "s:ghost")
        XCTAssertNil(item.category)
        XCTAssertNil(PhoneInboxRoute.sheet(for: item, snapshot: orphan))
        XCTAssertEqual(PhoneInbox.sentence(for: item),
                       PhoneInbox.orphanUnavailable)

        let upgraded = try snapshot("""
        {
          "agents": {"running": [
            {"session_id":"ghost","nickname":"Back","project":"P"}
          ]},
          "permissions": [
            {"request_id":"pr","session_id":"ghost","tool_name":"Bash"}
          ]
        }
        """)
        XCTAssertEqual(upgraded.decisionItems.count, 1)
        XCTAssertEqual(upgraded.decisionItems[0].target.key, "s:ghost")
        XCTAssertEqual(upgraded.decisionItems[0].category, .running)
        let sheet = PhoneInboxRoute.sheet(for: upgraded.decisionItems[0],
                                          snapshot: upgraded)
        if case .agent(let agent, let category) = sheet?.subject {
            XCTAssertEqual(agent.sessionId, "ghost")
            XCTAssertEqual(category, .running)
        } else {
            XCTFail("expected navigable agent sheet")
        }
    }

    func testProjectGroupsAreDeterministicWithOtherLast() throws {
        let snap = try snapshot("""
        {"board":{"cards":[
          {"id":"z2","title":"Same","project":"Zeta","needs_you":true},
          {"id":"o1","title":"Same","project":"","needs_you":true},
          {"id":"a1","title":"Same","project":"Alpha","needs_you":true},
          {"id":"z1","title":"Same","project":"Zeta","needs_you":true},
          {"id":"a2","title":"Same","project":"Alpha","needs_you":true}
        ]}}
        """)
        let groups = PhoneInbox.groups(snap.decisionItems)
        XCTAssertEqual(groups.map(\.heading), ["Alpha", "Zeta", "Other"])
        XCTAssertEqual(groups[0].items.map(\.target.key), ["c:a1", "c:a2"])
        XCTAssertEqual(groups[1].items.map(\.target.key), ["c:z1", "c:z2"])
        XCTAssertEqual(groups[2].items.map(\.target.key), ["c:o1"])
    }

    func testLiveUpdatesChangeOnlyTheAffectedSubject() throws {
        let due = try snapshot("""
        {"board":{"cards":[
          {"id":"m","title":"M","manual_check_due":true},
          {"id":"e","title":"E","needs_you":true},
          {"id":"r","title":"R","closed_by":"e"},
          {"id":"p","title":"P","column_name":"backlog","plan_path":"x.md"}
        ]}}
        """)
        XCTAssertEqual(Set(keys(due.decisionItems)), ["c:m", "c:e"])
        XCTAssertEqual(due.needsYouCount, 2)

        let next = try snapshot("""
        {"board":{"cards":[
          {"id":"m","title":"M","manual_check_due":false},
          {"id":"e","title":"E","needs_you":false},
          {"id":"r","title":"R","closed_by":"e","reviewed_at":9},
          {"id":"p","title":"P","column_name":"backlog","plan_path":"x.md",
           "session_id":"run"}
        ]}}
        """)
        XCTAssertTrue(next.decisionItems.isEmpty)
        XCTAssertEqual(next.needsYouCount, 0)
    }

    func testTargetsWordsAndBobsLine() throws {
        let snap = try snapshot("""
        {
          "agents": {
            "waiting": [
              {"session_id":"p1","nickname":"Perm"},
              {"session_id":"q1","nickname":"Ask","questions":[{"text":"?"}]},
              {"session_id":"w1","nickname":"Wait","tool_name":"Bash",
               "last_summary":"Pick a name."}
            ]
          },
          "permissions": [
            {"request_id":"pr","session_id":"p1","tool_name":"Bash"}
          ],
          "board": {"cards": [
            {"id":"e","title":"E","needs_you":true},
            {"id":"m","title":"M","manual_check_due":true,"manual_steps":"look",
             "dispatch_error":"refused here","queue_reason":"queued here"}
          ]}
        }
        """)
        let byKey = Dictionary(uniqueKeysWithValues:
            snap.decisionItems.map { ($0.target.key, $0) })
        for key in ["s:p1", "s:q1", "s:w1"] {
            if case .session = byKey[key]?.target {} else { XCTFail(key) }
        }
        for key in ["c:e", "c:m"] {
            if case .card = byKey[key]?.target {} else { XCTFail(key) }
        }
        // Dark Army's own words win, then a queued wait, then nothing at all:
        // the word on the row and the detail beside it already say what
        // this is.
        let manual = try XCTUnwrap(byKey["c:m"])
        XCTAssertEqual(manual.kind.word, "LOOK AT")
        XCTAssertEqual(manual.detail, "look")
        XCTAssertEqual(PhoneInbox.sentence(for: manual, refusal: "refused here",
                                           queueReason: "queued here"),
                       "refused here")
        XCTAssertEqual(PhoneInbox.sentence(for: manual, queueReason: "queued here"),
                       "queued here")
        XCTAssertEqual(PhoneInbox.sentence(for: manual), "")
        // A stopped row's detail is its own one-line summary where it left
        // one, the tool it stopped on otherwise.
        XCTAssertEqual(byKey["s:w1"]?.detail, "Pick a name.")
        XCTAssertEqual(byKey["s:w1"]?.kind.word, "STOPPED")

        let orphan = try snapshot("""
        {"permissions":[
          {"request_id":"pr","session_id":"ghost","tool_name":"Bash"}
        ]}
        """)
        let ghost = try XCTUnwrap(orphan.decisionItems.first)
        let spokenOrphan = PhoneInbox.spokenLabel(for: ghost)
        XCTAssertTrue(spokenOrphan.hasPrefix("ANSWER, ghost"))
        XCTAssertTrue(spokenOrphan.contains(PhoneInbox.orphanUnavailable))
        XCTAssertFalse(spokenOrphan.contains("running"))

        let running = try XCTUnwrap(byKey["s:p1"])
        let spokenRun = PhoneInbox.spokenLabel(for: running)
        XCTAssertTrue(spokenRun.hasPrefix("ANSWER, Perm, waiting"))
        XCTAssertTrue(spokenRun.contains("Bash"))
        let card = try XCTUnwrap(snap.board.cards.first { $0.id == "m" })
        XCTAssertTrue(PhoneInbox.spokenLabel(for: manual, card: card)
            .hasSuffix("refused here"))
    }

    func testSheetRoutePreservesCategoryCardDestinationAndPostsNothing() throws {
        let snap = try snapshot("""
        {
          "agents": {
            "running": [
              {"session_id":"runp","nickname":"RunP","own_terminal":true},
              {"session_id":"host2","nickname":"Host","own_terminal":true}
            ]
          },
          "permissions": [
            {"request_id":"pr","session_id":"runp","tool_name":"Bash"}
          ],
          "board": {"cards": [
            {"id":"m","title":"Manual","manual_check_due":true,
             "session_id":"host2"},
            {"id":"e","title":"Ended","needs_you":true}
          ]}
        }
        """)
        var transport = RecordingTransport()
        let router = PhoneSheetRouter()
        let perm = try XCTUnwrap(snap.decisionItems.first {
            $0.target.key == "s:runp"
        })
        open(perm, snapshot: snap, sheets: router, transport: &transport)
        if case .agent(_, let category) = router.top?.subject {
            XCTAssertEqual(category, .running)
        } else { XCTFail("permission should open the agent sheet") }
        XCTAssertEqual(router.stack.count, 1)

        open(perm, snapshot: snap, sheets: router, transport: &transport)
        XCTAssertEqual(router.stack.count, 1)

        let hosted = try XCTUnwrap(snap.decisionItems.first {
            $0.target.key == "c:m"
        })
        open(hosted, snapshot: snap, sheets: router, transport: &transport)
        if case .agent(let agent, let category) = router.top?.subject {
            XCTAssertEqual(agent.sessionId, "host2")
            XCTAssertEqual(category, .running)
        } else { XCTFail("a card whose agent is live must open the agent sheet") }

        let ended = try XCTUnwrap(snap.decisionItems.first { $0.target.key == "c:e" })
        open(ended, snapshot: snap, sheets: router, transport: &transport)
        if case .card = router.top?.subject {} else { XCTFail("c:e") }
        XCTAssertTrue(transport.actions.isEmpty)

        router.close()
        open(ended, snapshot: snap, sheets: router, transport: &transport)
        XCTAssertEqual(router.stack.count, 1)
        let entry = try XCTUnwrap(router.topState)
        entry.cardDraft(for: "e").draftTitle = "Unsaved"
        open(ended, snapshot: snap, sheets: router, transport: &transport)
        XCTAssertEqual(router.stack.count, 1)
        XCTAssertEqual(router.topState?.cardDraft(for: "e").draftTitle, "Unsaved")
        XCTAssertTrue(transport.actions.isEmpty)
    }

    func testMixedListRendersAtAccessibilityXXXL() throws {
        let snap = try snapshot("""
        {
          "agents": {"waiting": [
            {"session_id":"p1","nickname":"Perm","project":"P"}
          ]},
          "permissions": [
            {"request_id":"pr","session_id":"p1","tool_name":"Bash"}
          ],
          "board": {"cards": [
            {"id":"m","title":"Manual","project":"P","manual_check_due":true,
             "manual_steps":"a long check that must wrap on a small phone"}
          ]}
        }
        """)
        let client = PhoneClient()
        client.snapshot = snap
        let router = PhoneSheetRouter()
        let view = NeedsYouView(client: client)
            .environmentObject(router)
            .environment(\.dynamicTypeSize, .accessibility5)
        let host = UIHostingController(rootView: view)
        let scene = try XCTUnwrap(
            UIApplication.shared.connectedScenes
                .compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.frame = CGRect(x: 0, y: 0, width: 375, height: 812)
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer { window.isHidden = true }
        host.view.layoutIfNeeded()
        XCTAssertGreaterThan(host.view.bounds.height, 0)
        XCTAssertEqual(client.snapshot.decisionItems.count, 2)
        XCTAssertTrue(router.stack.isEmpty)
    }

    // MARK: - routing helper

    private struct RecordingTransport {
        var actions: [(String, [String: String])] = []
        mutating func post(action: String, fields: [String: String] = [:]) {
            actions.append((action, fields))
        }
    }

    private func open(_ item: PhoneInboxItem, snapshot: Snapshot,
                      sheets: PhoneSheetRouter,
                      transport: inout RecordingTransport) {
        PhoneInboxRoute.open(item, snapshot: snapshot, sheets: sheets)
        _ = transport
    }

    // MARK: - dismiss-hide

    func testMatchingAckOmitsTheSubjectAndDropsTheCount() throws {
        let open = try snapshot("""
        {"agents":{"waiting":[
          {"session_id":"q1","nickname":"Ask","project":"P",
           "questions":[{"text":"ok?"}]}
        ]}}
        """)
        XCTAssertEqual(open.decisionItems.count, 1)
        XCTAssertEqual(open.needsYouCount, 1)
        let fp = PhoneInbox.fingerprint(open.decisionItems[0], snapshot: open)
        let hidden = try snapshot("""
        {"agents":{"waiting":[
          {"session_id":"q1","nickname":"Ask","project":"P",
           "questions":[{"text":"ok?"}]}
        ]},
        "inbox":{"available":true,"acks":[
          {"key":"s:q1","kind":"question","fp":"\(fp)"}
        ]}}
        """)
        XCTAssertTrue(hidden.decisionItems.isEmpty)
        XCTAssertEqual(hidden.needsYouCount, 0)
    }

    // MARK: - Mark checked / Mark reviewed from the card screen

    private func board(_ json: String) throws -> Board {
        try JSONDecoder().decode(Board.self, from: Data(json.utf8))
    }

    private func card(_ json: String) throws -> BoardCard {
        try JSONDecoder().decode(BoardCard.self, from: Data(json.utf8))
    }

    /// Seam: `PhoneCardAck`, pure rules over the decoded models. An older
    /// Mac sends neither marker, so neither button is drawn even on a card
    /// that is due; each marker gates its own button alone.
    func testCardAckButtonsAreGatedOnTheirOwnMarkers() throws {
        let due = try card(#"{"id":"m","manual_check_due":true,"manual_steps":"1. look"}"#)
        let closed = try card(#"{"id":"r","column_name":"done","closed_by":"s1","close_note":"tests pass"}"#)
        let older = try board("{}")
        XCTAssertFalse(PhoneCardAck.showsManualClear(card: due, board: older))
        XCTAssertFalse(PhoneCardAck.showsReview(card: closed, board: older))

        let manualOnly = try board(#"{"manual_clear_writable":true}"#)
        XCTAssertTrue(PhoneCardAck.showsManualClear(card: due, board: manualOnly))
        XCTAssertFalse(PhoneCardAck.showsReview(card: closed, board: manualOnly))

        let reviewOnly = try board(#"{"review_writable":true}"#)
        XCTAssertFalse(PhoneCardAck.showsManualClear(card: due, board: reviewOnly))
        XCTAssertTrue(PhoneCardAck.showsReview(card: closed, board: reviewOnly))

        let both = try board(#"{"manual_clear_writable":true,"review_writable":true}"#)
        // Not due, or nothing to read, draws nothing even with the marker.
        let notDue = try card(#"{"id":"m","manual_steps":"1. look"}"#)
        XCTAssertFalse(PhoneCardAck.showsManualClear(card: notDue, board: both))
        let reviewed = try card(#"{"id":"r","column_name":"done","closed_by":"s1","close_note":"x","reviewed_at":1.0}"#)
        XCTAssertFalse(PhoneCardAck.showsReview(card: reviewed, board: both))
        let noNote = try card(#"{"id":"r","column_name":"done","closed_by":"s1"}"#)
        XCTAssertFalse(PhoneCardAck.showsReview(card: noNote, board: both))
    }

    /// Seam: `PhoneCardAck.*Fields` — the echo is the live card's own
    /// text, both halves of the close identity, and the card id.
    func testCardAckPayloadsEchoWhatWasOnScreen() throws {
        let due = try card(#"{"id":"m","manual_check_due":true,"manual_steps":"1. look\n2. press"}"#)
        XCTAssertEqual(PhoneCardAck.manualClearFields(due),
                       ["card_id": "m", "expected_manual_steps": "1. look\n2. press"])
        let closed = try card(#"{"id":"r","column_name":"done","closed_by":"s1","close_note":"tests pass"}"#)
        XCTAssertEqual(PhoneCardAck.reviewFields(closed),
                       ["card_id": "r", "expected_closed_by": "s1",
                        "expected_close_note": "tests pass"])
        XCTAssertEqual(PhoneActions.boardManualClear, "board_manual_clear")
        XCTAssertEqual(PhoneActions.boardReview, "board_review")
    }

    func testMismatchedFingerprintLeavesTheRow() throws {
        let snap = try snapshot("""
        {"agents":{"waiting":[
          {"session_id":"q1","nickname":"Ask","project":"P",
           "questions":[{"text":"ok?"}]}
        ]},
        "inbox":{"available":true,"acks":[
          {"key":"s:q1","kind":"question","fp":"question:deadbeef"}
        ]}}
        """)
        XCTAssertEqual(snap.decisionItems.map(\.wire), [.question])
    }

    func testPermissionStaysWhenAnAckForThatKeyExists() throws {
        let snap = try snapshot("""
        {"agents":{"waiting":[
          {"session_id":"p1","nickname":"Perm","project":"P"}
        ]},
        "permissions":[{"request_id":"r","session_id":"p1","tool_name":"Bash"}],
        "inbox":{"available":true,"acks":[
          {"key":"s:p1","kind":"waiting","fp":"waiting"}
        ]}}
        """)
        XCTAssertEqual(snap.decisionItems.map(\.wire), [.permission])
    }

    func testUnavailableInboxIgnoresAcks() throws {
        let snap = try snapshot("""
        {"agents":{"waiting":[
          {"session_id":"q1","nickname":"Ask","project":"P",
           "questions":[{"text":"ok?"}]}
        ]},
        "inbox":{"available":false,"acks":[
          {"key":"s:q1","kind":"question","fp":"waiting"}
        ]}}
        """)
        XCTAssertEqual(snap.decisionItems.map(\.wire), [.question])
    }
}
