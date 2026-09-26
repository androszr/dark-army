"""Desktop/phone inbox classification parity, plus phone integration pins.

Executes the production Swift reducers under swiftc. Missing Swift is a
failure on this macOS repository, not a skip.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL_INBOX = ROOT / "panel" / "Sources" / "BobPanel" / "Inbox.swift"
PHONE_INBOX = PHONE / "Inbox.swift"
PHONE_MODELS = PHONE / "Models.swift"
PHONE_COLLAB = PHONE / "Collaboration.swift"
PHONE_AREA_WIRE = PHONE / "AreaWire.swift"
#: `BoardCard.runFigures` is typed on the byte-pinned `RunFigures.Figures`,
#: so the models compile with that file beside them (Foundation only).
PHONE_RUN_FIGURES = PHONE / "RunFigures.swift"
PHONE_WORK_REPORT = PHONE / "WorkReport.swift"
NEEDS = PHONE / "NeedsYouView.swift"
APP = PHONE / "BobPhoneApp.swift"
ACTIONS = PHONE / "Actions.swift"
API = ROOT / "host" / "dark_army_daemon" / "api_server.py"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

# Every subject the two reducers used to list, plus the three they no
# longer do — a ready plan, a closed card awaiting review and a burst alert —
# so the fixture proves both what is listed and what is not.
FIVE_KIND = {
    "rows": [
        {"sessionId": "p1", "nickname": "Perm", "project": "P", "kindHint": "permission"},
        {"sessionId": "q1", "nickname": "Ask", "project": "P", "question": "ok?"},
        {"sessionId": "w1", "nickname": "Wait", "project": "P"},
    ],
    "prompts": [
        {"sessionId": "p1", "requestId": "pr", "summary": "Bash: ls"},
    ],
    "cards": [
        {"id": "e1", "title": "Ended", "project": "P", "needsYou": True},
        {"id": "m1", "title": "Manual", "project": "P", "manualCheckDue": True,
         "manualSteps": "look"},
        {"id": "r1", "title": "Review", "project": "P", "closedBy": "cipher",
         "closeNote": "shipped"},
        {"id": "pl1", "title": "Plan", "project": "P", "column": "backlog",
         "planPath": "plans/x.md"},
    ],
    "alerts": [
        {"id": "b1", "ts": 5, "door": "lan", "doorWord": "Wi-Fi",
         "peer": "10.0.0.9", "count": 5, "windowSeconds": 600,
         "text": "5 refused attempts"},
    ],
}

CARD_PRECEDENCE = {
    "rows": [],
    "prompts": [],
    "cards": [
        {"id": "c1", "title": "Stack", "project": "P", "needsYou": True,
         "manualCheckDue": True, "closedBy": "cipher"},
    ],
}

READY_PLAN = {
    "rows": [],
    "prompts": [],
    "cards": [
        {"id": "pl", "title": "Plan", "project": "P", "column": "backlog",
         "planPath": "p.md", "refineSessionId": "old"},
    ],
}

#: The screenshot case (20 Sep 2026): one agent finished a card, flagged its
#: manual check and stopped — listed twice as STOPPED and LOOK AT, so the
#: header said "2 need you". A card and its bound session are one subject.
BOUND_AND_WAITING = {
    "rows": [
        {"sessionId": "d1", "nickname": "Vex", "project": "P"},
    ],
    "prompts": [],
    "cards": [
        {"id": "usd", "title": "USD/PLN tile", "project": "P", "sessionId": "d1",
         "manualCheckDue": True, "manualSteps": "1. Run it."},
    ],
}

LINKED = {
    "rows": [
        {"sessionId": "same", "nickname": "Agent", "project": "P",
         "kindHint": "permission"},
    ],
    "prompts": [
        {"sessionId": "same", "requestId": "pr", "summary": "Bash"},
    ],
    "cards": [
        {"id": "same", "title": "Linked", "project": "P", "manualCheckDue": True,
         "manualSteps": "check"},
        {"id": "other", "title": "Bound", "project": "P", "sessionId": "same",
         "manualCheckDue": True, "manualSteps": "too"},
    ],
}


#: Mission Control asked to start two cards; one also has a hand-check, which
#: outranks the ask. The asked card is ANSWER, beside a real question.
START_ASKED = {
    "rows": [
        {"sessionId": "q1", "nickname": "Ask", "project": "P", "question": "ok?"},
    ],
    "prompts": [],
    "cards": [
        {"id": "a1", "title": "Asked", "project": "P", "startAskId": "k1"},
        {"id": "a2", "title": "Both", "project": "P", "startAskId": "k2",
         "manualCheckDue": True, "manualSteps": "look"},
        {"id": "a3", "title": "Quiet", "project": "P"},
    ],
}


#: Dismissals of every shape the buzz gate subtracts (`live_activity.
#: shown_sessions`): a dismissed waiting row, a question dismissed on its
#: material and another on stale material, a prompt that no ack can hide, a
#: dismissed session kept by its card's surviving entry, and the same with
#: the card dismissed too.
def _dismissed_mix() -> dict:
    from dark_army_daemon.inbox_ack import fingerprint
    return {
        "rows": [
            {"sessionId": "w1", "nickname": "Wait", "project": "P"},
            {"sessionId": "q1", "nickname": "Ask", "project": "P", "question": "ok?"},
            {"sessionId": "q2", "nickname": "Again", "project": "P", "question": "new?"},
            {"sessionId": "p1", "nickname": "Perm", "project": "P",
             "kindHint": "permission"},
            {"sessionId": "b1", "nickname": "Kept", "project": "P"},
            {"sessionId": "b2", "nickname": "Gone", "project": "P"},
        ],
        "prompts": [{"sessionId": "p1", "requestId": "pr", "summary": "Bash: ls"}],
        "cards": [
            {"id": "c1", "title": "Kept card", "project": "P", "sessionId": "b1",
             "needsYou": True},
            {"id": "c2", "title": "Gone card", "project": "P", "sessionId": "b2",
             "needsYou": True},
        ],
        "acks": [
            {"key": "s:w1", "kind": "waiting", "fp": "waiting"},
            {"key": "s:q1", "kind": "question", "fp": fingerprint("question", "ok?")},
            {"key": "s:q2", "kind": "question", "fp": fingerprint("question", "old?")},
            {"key": "s:p1", "kind": "waiting", "fp": "waiting"},
            {"key": "s:b1", "kind": "waiting", "fp": "waiting"},
            {"key": "s:b2", "kind": "waiting", "fp": "waiting"},
            {"key": "c:c2", "kind": "ended_work", "fp": fingerprint("ended_work", "")},
        ],
    }


def _require_swiftc() -> str:
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    return swiftc


def _run(binary: Path, payload: dict) -> list[tuple[str, int, int]]:
    proc = subprocess.run(
        [str(binary)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    lines = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith("COUNT")]
    out = []
    for ln in lines:
        key, wire, kind = ln.split("\t")
        out.append((key, int(wire), int(kind)))
    return out


def _count(binary: Path, payload: dict) -> int:
    proc = subprocess.run(
        [str(binary)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    for ln in proc.stdout.splitlines():
        if ln.startswith("COUNT "):
            return int(ln.split()[1])
    raise AssertionError(proc.stdout)


DESKTOP_HARNESS = r'''
import Foundation

struct AgentQuestion {
    var header = ""
    var text = ""
    var id = ""
}
struct InboxAckRecord {
    var key = ""
    var kind = ""
    var fp = ""
}
struct AccessAlert {
    var id = ""
    var ts: Double = 0
    var door = ""
    var doorWord = ""
    var peer = ""
    var count = 0
    var windowSeconds = 0
    var text = ""
    var doorText: String { doorWord.isEmpty ? door : doorWord }
}
struct Agent {
    var nickname = ""
    var name = ""
    var sessionId = ""
    var project = ""
    var idleSeconds: Double = 0
    var quietSince: Double = 0
    var questionList: [AgentQuestion] = []
    var currentTool = ""
    var lastSummary = ""
    var workReport: WorkReport.Parsed? = nil
    var id: String { sessionId }
}
// The two shapes `Inbox.items` reads for a work report's headline: the
// daemon's parsed report on a row, and the fleet an ended card's row is
// looked up in. The fixtures carry neither, so both stay empty here.
enum WorkReport { struct Parsed { var headline = "" } }
struct Agents {
    func row(session: String) -> (Agent, String)? { nil }
}
struct SectionRow {
    let agent: Agent
}
struct PermissionPrompt {
    var sessionId = ""
    var requestId = ""
    var summary = ""
}
struct BoardCard {
    var id = ""
    var title = ""
    var summary = ""
    var project = ""
    var needsYou = false
    var manualCheckDue = false
    var awaitsReview = false
    var planPath = ""
    var sessionId = ""
    var refineState = ""
    var column = "backlog"
    var manualSteps = ""
    var closeNote = ""
    var closedBy = ""
    var knownFinishedAt: Double? = nil
    var createdAt: Double = 0
    var doneAt: Double? = nil
    var startAskId = ""
    var startAskedAt: Double = 0
}
enum BoardColumn: String { case backlog }

func projectNameOrder(_ a: String, _ b: String) -> Bool {
    if a.isEmpty != b.isEmpty { return b.isEmpty }
    return a.localizedStandardCompare(b) == .orderedAscending
}

struct Fixture: Decodable {
    var rows: [Row] = []
    var prompts: [Prompt] = []
    var cards: [Card] = []
    var acks: [Ack] = []
    var alerts: [Alert] = []
    struct Ack: Decodable {
        var key = ""
        var kind = ""
        var fp = ""
    }
    struct Alert: Decodable {
        var id = ""
        var ts: Double = 0
        var door = ""
        var doorWord = ""
        var peer = ""
        var count = 0
        var windowSeconds = 0
        var text = ""
    }
    enum CodingKeys: String, CodingKey { case rows, prompts, cards, acks, alerts }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        rows = (try? c.decode([Row].self, forKey: .rows)) ?? []
        prompts = (try? c.decode([Prompt].self, forKey: .prompts)) ?? []
        cards = (try? c.decode([Card].self, forKey: .cards)) ?? []
        acks = (try? c.decode([Ack].self, forKey: .acks)) ?? []
        alerts = (try? c.decode([Alert].self, forKey: .alerts)) ?? []
    }
    struct Row: Decodable {
        var sessionId = ""
        var nickname = ""
        var project = ""
        var question: String? = nil
        var questionId: String? = nil
        var kindHint: String? = nil
    }
    struct Prompt: Decodable {
        var sessionId = ""
        var requestId = ""
        var summary = ""
    }
    struct Card: Decodable {
        var id = ""
        var title = ""
        var project = ""
        var needsYou = false
        var manualCheckDue = false
        var closedBy: String? = nil
        var closeNote = ""
        var column = "backlog"
        var planPath = ""
        var sessionId = ""
        var refineState = ""
        var refineSessionId = ""
        var manualSteps = ""
        var summary = ""
        var startAskId = ""

        enum CodingKeys: String, CodingKey {
            case id, title, project, needsYou, manualCheckDue, closedBy
            case closeNote, column, planPath, sessionId, refineState
            case refineSessionId, manualSteps, summary, startAskId
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = (try? c.decode(String.self, forKey: .id)) ?? ""
            title = (try? c.decode(String.self, forKey: .title)) ?? ""
            project = (try? c.decode(String.self, forKey: .project)) ?? ""
            needsYou = (try? c.decode(Bool.self, forKey: .needsYou)) ?? false
            manualCheckDue = (try? c.decode(Bool.self, forKey: .manualCheckDue)) ?? false
            closedBy = try? c.decode(String.self, forKey: .closedBy)
            closeNote = (try? c.decode(String.self, forKey: .closeNote)) ?? ""
            column = (try? c.decode(String.self, forKey: .column)) ?? "backlog"
            planPath = (try? c.decode(String.self, forKey: .planPath)) ?? ""
            sessionId = (try? c.decode(String.self, forKey: .sessionId)) ?? ""
            refineState = (try? c.decode(String.self, forKey: .refineState)) ?? ""
            refineSessionId = (try? c.decode(String.self, forKey: .refineSessionId)) ?? ""
            manualSteps = (try? c.decode(String.self, forKey: .manualSteps)) ?? ""
            summary = (try? c.decode(String.self, forKey: .summary)) ?? ""
            startAskId = (try? c.decode(String.self, forKey: .startAskId)) ?? ""
        }
    }
}

@main
enum Runner {
    static func main() throws {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        let fixture = try JSONDecoder().decode(Fixture.self, from: data)
        let rows: [SectionRow] = fixture.rows.map { row in
            var agent = Agent()
            agent.sessionId = row.sessionId
            agent.nickname = row.nickname
            agent.project = row.project
            if let q = row.question, !q.isEmpty {
                agent.questionList = [AgentQuestion(header: "", text: q,
                                                    id: row.questionId ?? "")]
            }
            return SectionRow(agent: agent)
        }
        let prompts: [PermissionPrompt] = fixture.prompts.map { p in
            var prompt = PermissionPrompt()
            prompt.sessionId = p.sessionId
            prompt.requestId = p.requestId
            prompt.summary = p.summary
            return prompt
        }
        let cards: [BoardCard] = fixture.cards.map { c in
            var card = BoardCard()
            card.id = c.id
            card.title = c.title
            card.project = c.project
            card.needsYou = c.needsYou
            card.manualCheckDue = c.manualCheckDue
            card.awaitsReview = !(c.closedBy ?? "").isEmpty
            card.planPath = c.planPath
            card.sessionId = c.sessionId
            card.refineState = c.refineState
            card.column = c.column
            card.manualSteps = c.manualSteps
            card.closeNote = c.closeNote
            card.closedBy = c.closedBy ?? ""
            card.summary = c.summary
            card.startAskId = c.startAskId
            return card
        }
        let acks: [InboxAckRecord] = fixture.acks.map { a in
            InboxAckRecord(key: a.key, kind: a.kind, fp: a.fp)
        }
        // The alerts are decoded and then deliberately not handed over:
        // the inbox takes none, and the fixture proves it by listing one.
        _ = fixture.alerts
        let items = Inbox.items(rows: rows, prompts: prompts, cards: cards,
                                acks: acks)
        for item in items {
            print("\(item.target.key)\t\(item.wire.rawValue)\t\(item.kind.rawValue)")
        }
        print("COUNT \(items.count)")
    }
}
'''

PHONE_HARNESS = r'''
import Foundation

@main
enum Runner {
    static func main() throws {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        let snap = try JSONDecoder().decode(Snapshot.self, from: data)
        let items = PhoneInbox.items(from: snap)
        for item in items {
            print("\(item.target.key)\t\(item.wire.rawValue)\t\(item.kind.rawValue)")
        }
        print("COUNT \(snap.needsYouCount)")
    }
}
'''


def _phone_snapshot(payload: dict) -> dict:
    waiting = []
    for row in payload.get("rows", []):
        agent = {
            "session_id": row["sessionId"],
            "nickname": row.get("nickname", ""),
            "project": row.get("project", ""),
        }
        if row.get("question"):
            agent["questions"] = [{"text": row["question"]}]
        waiting.append(agent)
    permissions = []
    for prompt in payload.get("prompts", []):
        permissions.append({
            "session_id": prompt["sessionId"],
            "request_id": prompt["requestId"],
            "tool_name": prompt.get("summary", ""),
            "description": "",
        })
    cards = []
    for card in payload.get("cards", []):
        row = {
            "id": card["id"],
            "title": card.get("title", ""),
            "project": card.get("project", ""),
            "needs_you": card.get("needsYou", False),
            "manual_check_due": card.get("manualCheckDue", False),
            "closed_by": card.get("closedBy", ""),
            "close_note": card.get("closeNote", ""),
            "column_name": card.get("column", "backlog"),
            "plan_path": card.get("planPath", ""),
            "session_id": card.get("sessionId", ""),
            "refine_state": card.get("refineState", ""),
            "manual_steps": card.get("manualSteps", ""),
            "summary": card.get("summary", ""),
            "start_ask_id": card.get("startAskId", ""),
        }
        cards.append(row)
    out = {
        "agents": {"waiting": waiting},
        "permissions": permissions,
        "board": {"cards": cards},
    }
    acks = payload.get("acks") or []
    if acks:
        out["inbox"] = {"available": True, "acks": acks}
    alerts = payload.get("alerts") or []
    if alerts:
        out["security"] = {"available": True, "alerts": [{
            "id": a["id"], "ts": a.get("ts", 0), "door": a.get("door", ""),
            "door_word": a.get("doorWord", ""),
            "peer": a.get("peer", ""), "count": a.get("count", 0),
            "window_seconds": a.get("windowSeconds", 0),
            "text": a.get("text", ""),
        } for a in alerts]}
    return out


@pytest.fixture(scope="module")
def binaries(tmp_path_factory):
    swiftc = _require_swiftc()
    folder = tmp_path_factory.mktemp("phone-inbox")
    desktop_main = folder / "desktop_main.swift"
    desktop_main.write_text(DESKTOP_HARNESS)
    desktop_bin = folder / "desktop-inbox"
    built = subprocess.run(
        [swiftc, str(PANEL_INBOX), str(desktop_main), "-o", str(desktop_bin)],
        capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stderr
    phone_main = folder / "phone_main.swift"
    phone_main.write_text(PHONE_HARNESS)
    phone_bin = folder / "phone-inbox"
    built = subprocess.run(
        [swiftc, str(PHONE_MODELS), str(PHONE_AREA_WIRE), str(PHONE_COLLAB),
         str(PHONE_RUN_FIGURES), str(PHONE_WORK_REPORT), str(PHONE_INBOX),
         str(phone_main),
         "-o", str(phone_bin)],
        capture_output=True, text=True, timeout=90)
    assert built.returncode == 0, built.stderr
    return desktop_bin, phone_bin


@pytest.mark.parametrize("name,payload", [
    ("five-kind", FIVE_KIND),
    ("card-precedence", CARD_PRECEDENCE),
    ("ready-plan", READY_PLAN),
    ("linked-subject", LINKED),
    ("start-asked", START_ASKED),
    ("bound-and-waiting", BOUND_AND_WAITING),
])
def test_desktop_and_phone_reducers_agree(binaries, name, payload):
    desktop_bin, phone_bin = binaries
    desktop = _run(desktop_bin, payload)
    phone = _run(phone_bin, _phone_snapshot(payload))
    assert desktop == phone, (name, desktop, phone)
    assert _count(desktop_bin, payload) == _count(phone_bin, _phone_snapshot(payload))


def test_five_wire_kinds_fold_into_three(binaries):
    """Five wire names, three kinds — `(key, wire, kind)` per row, ranked by
    kind then wire. The closed card, the ready plan and the burst alert are
    not listed by either reducer, and every listed row counts."""
    desktop_bin, phone_bin = binaries
    rows = _run(phone_bin, _phone_snapshot(FIVE_KIND))
    assert rows == [
        ("s:p1", 0, 0), ("s:q1", 1, 0),
        ("c:e1", 2, 1), ("c:m1", 3, 1),
        ("s:w1", 4, 2),
    ]
    assert _run(desktop_bin, FIVE_KIND) == rows
    assert _count(phone_bin, _phone_snapshot(FIVE_KIND)) == 5
    assert _count(desktop_bin, FIVE_KIND) == 5


def test_a_stated_security_section_still_lists_no_alert(binaries):
    """A burst alert is the access log's, whichever way the section reads."""
    _, phone_bin = binaries
    snap = _phone_snapshot({"rows": [], "prompts": [], "cards": []})
    snap["security"] = {"available": True, "alerts": [{"id": "b1", "text": "x"}]}
    assert _run(phone_bin, snap) == []


def test_a_card_and_its_bound_session_are_one_subject(binaries):
    """`c:other` is bound to session `same`, whose permission ask outranks
    the card's check, so the card's entry is dropped: two subjects, not
    three. A card with no session (`c:same` shares only the *id*) stays."""
    desktop_bin, phone_bin = binaries
    rows = _run(phone_bin, _phone_snapshot(LINKED))
    assert {row[0] for row in rows} == {"s:same", "c:same"}
    assert _count(phone_bin, _phone_snapshot(LINKED)) == 2
    assert _run(desktop_bin, LINKED) == rows


def test_a_waiting_session_and_its_flagged_card_count_once(binaries):
    """The card's check wins over the plain wait, on both reducers."""
    desktop_bin, phone_bin = binaries
    for rows in (_run(phone_bin, _phone_snapshot(BOUND_AND_WAITING)),
                 _run(desktop_bin, BOUND_AND_WAITING)):
        assert [row[0] for row in rows] == ["c:usd"]
    assert _count(phone_bin, _phone_snapshot(BOUND_AND_WAITING)) == 1


def test_phone_duplicate_normalization_is_separate(binaries):
    _, phone_bin = binaries
    payload = {
        "rows": [
            {"sessionId": "dup", "nickname": "Run", "project": "P",
             "kindHint": "permission"},
            {"sessionId": "dup", "nickname": "Wait", "project": "P"},
        ],
        "prompts": [
            {"sessionId": "dup", "requestId": "a", "summary": "Read"},
            {"sessionId": "dup", "requestId": "b", "summary": "Write"},
        ],
        "cards": [],
    }
    rows = _run(phone_bin, _phone_snapshot(payload))
    assert len(rows) == 1
    assert rows[0][0] == "s:dup"
    assert rows[0][1] == 0


def test_one_projected_list_and_shared_count_consumers():
    needs = NEEDS.read_text()
    assert "ForEach(groups)" in needs
    assert "ForEach(group.items)" in needs
    assert "CARDS WITHOUT A SESSION" not in needs
    assert "snapshot.decisionItems.isEmpty" in needs
    app = APP.read_text()
    assert ".badge(client.snapshot.needsYouCount)" in app
    assert "needsYouCount: client.snapshot.needsYouCount" in app
    models = PHONE_MODELS.read_text()
    assert "var needsYouCount: Int { decisionItems.count }" in models
    assert "agents.waiting.count + board.needsYouCards.count" not in models


def test_matching_ack_is_omitted_from_both_reducers(binaries):
    from dark_army_daemon.inbox_ack import fingerprint, question_material
    desktop_bin, phone_bin = binaries
    material = question_material([{"id": "", "text": "ok?"}])
    fp = fingerprint("question", material)
    payload = {
        "rows": [{"sessionId": "q1", "nickname": "Ask", "project": "P",
                  "question": "ok?"}],
        "prompts": [],
        "cards": [],
        "acks": [{"key": "s:q1", "kind": "question", "fp": fp}],
    }
    assert _run(desktop_bin, payload) == []
    assert _run(phone_bin, _phone_snapshot(payload)) == []
    assert _count(desktop_bin, payload) == 0
    assert _count(phone_bin, _phone_snapshot(payload)) == 0


def test_mismatched_fingerprint_stays_on_both_reducers(binaries):
    desktop_bin, phone_bin = binaries
    payload = {
        "rows": [{"sessionId": "q1", "nickname": "Ask", "project": "P",
                  "question": "ok?"}],
        "prompts": [],
        "cards": [],
        "acks": [{"key": "s:q1", "kind": "question", "fp": "question:deadbeef"}],
    }
    desktop = _run(desktop_bin, payload)
    phone = _run(phone_bin, _phone_snapshot(payload))
    assert desktop == phone
    assert desktop[0][0] == "s:q1"


def test_permission_stays_when_an_ack_for_that_key_exists(binaries):
    desktop_bin, phone_bin = binaries
    payload = {
        "rows": [{"sessionId": "p1", "nickname": "Perm", "project": "P",
                  "kindHint": "permission"}],
        "prompts": [{"sessionId": "p1", "requestId": "pr", "summary": "Bash: ls"}],
        "cards": [],
        "acks": [{"key": "s:p1", "kind": "waiting", "fp": "waiting"}],
    }
    desktop = _run(desktop_bin, payload)
    phone = _run(phone_bin, _phone_snapshot(payload))
    assert desktop == phone
    assert desktop[0][1] == 0  # permission rank


def test_an_ended_card_can_be_dismissed_on_both_reducers(binaries):
    """Dismiss is one verb: a card whose assistant has gone hides like any
    other entry, and an ack for a retired kind changes nothing."""
    from dark_army_daemon.inbox_ack import fingerprint
    desktop_bin, phone_bin = binaries
    ended = {
        "rows": [],
        "prompts": [],
        "cards": [{"id": "e1", "title": "Ended", "project": "P", "needsYou": True}],
        "acks": [{"key": "c:e1", "kind": "ended_work",
                  "fp": fingerprint("ended_work", "")}],
    }
    bare = {k: v for k, v in ended.items() if k != "acks"}
    assert _count(desktop_bin, bare) == 1
    assert _count(desktop_bin, ended) == 0
    assert _count(phone_bin, _phone_snapshot(ended)) == 0
    plan = {
        "rows": [],
        "prompts": [],
        "cards": [{"id": "pl1", "title": "Plan", "project": "P",
                   "column": "backlog", "planPath": "plans/x.md"}],
        "acks": [{"key": "c:pl1", "kind": "plan_ready",
                  "fp": fingerprint("plan_ready", "plans/x.md")}],
    }
    assert _run(desktop_bin, plan) == []
    assert _run(phone_bin, _phone_snapshot(plan)) == []


def test_inbox_ack_is_a_chosen_phone_verb_and_swipe_exists():
    actions = ACTIONS.read_text()
    needs = NEEDS.read_text()
    assert 'static let inboxAck = "inbox_ack"' in actions
    lan = API.read_text().split("LAN_ACTIONS = (", 1)[1].split("REMOTE_ACTIONS = (", 1)[0]
    assert '"inbox_ack"' in lan
    remote = API.read_text().split("REMOTE_ACTIONS = (", 1)[1].split("_LAN_BOARD", 1)[0]
    assert '"inbox_ack"' in remote
    assert "swipeActions(edge: .trailing" in needs
    assert "allowsFullSwipe: false" in needs
    mod = needs.split("private struct InboxSwipeModifier", 1)[1]
    # The one swipe is gated on the reducer's own `dismissable`, which is
    # false for a permission ask alone.
    assert "if PhoneInboxAck.showsDismiss(wire: item.wire, available: available) {" in mod
    actions_src = ACTIONS.read_text()
    assert "available && wire.dismissable" in actions_src
    assert mod.count("swipeActions") == 1
    # The word is the shared list's (`Verbs.swift`) since 25 Sep 2026.
    assert 'Label(Verbs.dismiss.label' in mod
    assert "Delete" not in mod
    assert "UserDefaults" not in (PHONE / "Inbox.swift").read_text()
    assert "UserDefaults" not in needs


def test_sheets_and_action_boundary():
    """The tab routes and does not answer: no answer box, no verdict, no
    board verb and no delete on the list. Every one of those lives on the
    screen a row opens."""
    needs = NEEDS.read_text()
    assert "PhoneInboxRoute.open" in needs
    assert "sheets.show(.catchUp())" not in needs
    assert "sheets.show(.catchUp())" in (PHONE / "FleetView.swift").read_text()
    assert "AnswerBox(" not in needs
    assert "PhoneProcessRow" not in needs
    assert "PhoneBoardCard" not in needs
    for verb in ("Allow", "Deny", "permission_verdict", "pressPermission",
                 "boardUpdate", "boardReset", "boardDelete", "boardManualClear",
                 "boardReview", "PhoneActions.dismiss"):
        assert verb not in needs, verb
    assert needs.count("PhoneActions.inboxAck") == 2
    inbox = PHONE_INBOX.read_text()
    assert 'linkState == "ended"' not in inbox
    assert "manualSteps.isEmpty" not in inbox
    assert "func spokenLabel(" in inbox
    assert "PhoneInbox.spokenLabel(for: item" in needs
    actions = ACTIONS.read_text()
    lan = API.read_text().split("LAN_ACTIONS = (", 1)[1].split(")", 1)[0]
    for verb in ("board_manual_clear", "board_review"):
        assert f'"{verb}"' in actions
        assert f'"{verb}"' in lan
        assert verb not in needs
        assert verb not in inbox
    # Outcome accept / revise stay desktop-only: reading a result on the
    # phone never accepts it.
    for verb in ("board_accept_outcome", "board_request_revision"):
        assert verb not in actions
        assert verb not in needs
        assert verb not in inbox
        assert f'"{verb}"' not in lan


def test_project_registers_inbox_and_preserves_concurrent_files():
    source = PBXPROJ.read_text()
    for name in ("Inbox.swift", "PhoneInboxTests.swift"):
        assert source.count(name) >= 2, name
        assert f"{name} in Sources" in source
    for name in ("PhoneSheet.swift", "PhoneSheetHost.swift",
                 "SheetPresentationTests.swift", "LifecycleReport.swift",
                 "LifecycleReportView.swift", "LifecycleReportTests.swift"):
        assert f"{name} in Sources" in source, name
    # HomeAddressTests is in the test sources phase via a comment-less
    # PBXBuildFile; the file reference is the membership pin.
    assert "path = BobPhoneTests/HomeAddressTests.swift" in source
    assert "DEC1510A0000000000000091" in source


def test_a_start_ask_is_an_answer_entry_and_a_hand_check_outranks_it(binaries):
    """Both reducers agree above; this says what they agree on. The asked
    card is listed once, as ANSWER (kind 0) with wire `startAsked` (5); the
    card that also has a hand-check is listed as its hand-check; a card with
    no ask is not listed."""
    desktop_bin, phone_bin = binaries
    rows = _run(phone_bin, _phone_snapshot(START_ASKED))
    assert rows == _run(desktop_bin, START_ASKED)
    assert ("c:a1", 5, 0) in rows
    assert ("c:a2", 3, 1) in rows
    assert not any(key == "c:a3" for key, _, _ in rows)


@pytest.mark.parametrize("name", ["five-kind", "bound-and-waiting", "dismissed-mix"])
def test_the_buzz_gates_dismissed_reading_is_the_phones_list(binaries, name):
    """The byte-level seam for the buzz gate: the phone's own
    `PhoneInbox.items(from:)` under swiftc, and `live_activity.
    shown_sessions` on the same dicts, name the same sessions — every
    surviving session entry, plus the session of every surviving card entry
    that `listed_sessions` admits (a card never admits a session alone)."""
    from dark_army_daemon import live_activity
    payload = {"five-kind": FIVE_KIND, "bound-and-waiting": BOUND_AND_WAITING,
               "dismissed-mix": _dismissed_mix()}[name]
    _, phone_bin = binaries
    snapshot = _phone_snapshot(payload)
    items = _run(phone_bin, snapshot)
    prompts = {p["session_id"]: p for p in snapshot["permissions"]}
    cards = snapshot["board"]["cards"]
    listed = live_activity.listed_sessions(snapshot["agents"], prompts, notified=[])
    card_session = {card["id"]: card["session_id"] for card in cards}
    by_session = {key[2:] for key, _, _ in items if key.startswith("s:")}
    by_card = {card_session.get(key[2:], "") for key, _, _ in items
               if key.startswith("c:")}
    expected = by_session | (by_card & listed)
    shown = live_activity.shown_sessions(
        snapshot["agents"], prompts, notified=[], cards=cards,
        acks=snapshot.get("inbox", {}).get("acks"))
    assert shown == expected
    if name == "dismissed-mix":
        assert shown == {"q2", "p1", "b1"}
