# host/tests/test_phone_action_queue.py
"""The phone's ordered action queue, pinned from here.

A press on the phone is **queued, not awaited** (`docs/phone-contract.md`,
*A press is queued, not awaited*): the receipt is the queue entry, one sender
takes them in creation order with a scope waiting for its own earlier press,
and the controls stay usable. The arithmetic is four pure functions on
`ReceiptLedger` — `nextSendable`, `duplicate`, `mark`, `queueRows` — and the
ledger's own verbs (`retry`, `read`, `releaseHolds`, `refuse`), run here under
`swiftc` against the production `Receipts.swift`, the shape of
`test_phone_inbox.py`'s `PHONE_HARNESS`: the payload's `script` drives a real
ledger and the test reads the rows back (`_ledger`). Only the wiring that has
no seam — which views send through `enqueue`, what no longer dims — is a text
pin on a view or on `Client.swift`, and those pin a rule's presence, never an
exact line or a count. The same cases exist as XCTest in
`ios/BobPhoneTests/OfflineCacheTests.swift`.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"

#: `Receipts.swift` plus what it reaches: the models (and the two run
#: figures/health files `BoardCard` now carries), the wire helpers, the
#: verbs and the reducer `Models.swift` names. `PhoneClient` and
#: `OutboxStore` are stubbed in the harness — the two constants
#: `Receipts.swift` reads off them are values, not behaviour.
SOURCES = [
    "Receipts.swift", "Models.swift", "AreaWire.swift", "Collaboration.swift",
    "Actions.swift", "Inbox.swift", "RunFigures.swift", "RunHealth.swift",
    "HostAddress.swift", "WorkReport.swift", "ReviewRules.swift",
]

HARNESS = r'''
import Foundation

enum PhoneClient {
    static let autostartSettlingKey = "board_autostart"
    static func parallelSettlingKey(_ root: String) -> String { "parallel:\(root)" }
}
enum PrepareRoute: Equatable { case mac, phone }
enum OutboxStore {
    static let backoffStart: TimeInterval = 10
    static let backoffCap: TimeInterval = 300
}

struct Row: Decodable {
    var id = ""
    var action = ""
    var scope = ""
    var state = "queued"
    var createdAt = 0.0
    var heldUntil = 0.0
    var attempts = 0
    var pairingToken = ""
    var lastError = ""
    var fields: [String: String] = [:]
    var acceptedAt = 0.0
    /// "replyHold" builds `.replyHold(sessionId: scope, questionId: "q1")`;
    /// anything else is `.none`.
    var effect = ""
    enum CodingKeys: String, CodingKey {
        case id, action, scope, state, createdAt, heldUntil, attempts
        case pairingToken, lastError, fields, acceptedAt, effect
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? ""
        action = try c.decodeIfPresent(String.self, forKey: .action) ?? ""
        scope = try c.decodeIfPresent(String.self, forKey: .scope) ?? ""
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "queued"
        createdAt = try c.decodeIfPresent(Double.self, forKey: .createdAt) ?? 0
        heldUntil = try c.decodeIfPresent(Double.self, forKey: .heldUntil) ?? 0
        attempts = try c.decodeIfPresent(Int.self, forKey: .attempts) ?? 0
        pairingToken = try c.decodeIfPresent(String.self, forKey: .pairingToken) ?? ""
        lastError = try c.decodeIfPresent(String.self, forKey: .lastError) ?? ""
        fields = try c.decodeIfPresent([String: String].self, forKey: .fields) ?? [:]
        acceptedAt = try c.decodeIfPresent(Double.self, forKey: .acceptedAt) ?? 0
        effect = try c.decodeIfPresent(String.self, forKey: .effect) ?? ""
    }
}
/// A `landed` / `evidenceBeforeSending` probe: an effect by name over a
/// snapshot the payload carries as raw JSON (tolerantly decoded, as the
/// phone decodes it).
struct Probe: Decodable {
    var effect = ""
    var id = ""
    var extra = ""
    enum CodingKeys: String, CodingKey { case effect, id, extra }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        effect = try c.decodeIfPresent(String.self, forKey: .effect) ?? ""
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? ""
        extra = try c.decodeIfPresent(String.self, forKey: .extra) ?? ""
    }
    var built: ReceiptEffect {
        switch effect {
        case "rowGone": return .rowGone(sessionId: id)
        case "rowGoneAnywhere": return .rowGoneAnywhere(sessionId: id)
        case "questionGone": return .questionGone(sessionId: id, questionId: extra)
        case "replyHold": return .replyHold(sessionId: id, questionId: extra)
        case "planApproved": return .planApproved(cardId: id, digest: extra)
        default: return .none
        }
    }
}
struct Payload: Decodable {
    var receipts: [Row] = []
    var pairingToken = "p"
    var now = 1000.0
    var scope = ""
    var action = ""
    var holding: [String] = []
    var fields: [String: String] = [:]
    var snapshot = ""
    var probe: Probe?
    var script: [[String: String]] = []
    enum CodingKeys: String, CodingKey {
        case receipts, pairingToken, now, scope, action, holding, fields, snapshot, probe
        case script
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        receipts = try c.decodeIfPresent([Row].self, forKey: .receipts) ?? []
        pairingToken = try c.decodeIfPresent(String.self, forKey: .pairingToken) ?? "p"
        now = try c.decodeIfPresent(Double.self, forKey: .now) ?? 1000
        scope = try c.decodeIfPresent(String.self, forKey: .scope) ?? ""
        action = try c.decodeIfPresent(String.self, forKey: .action) ?? ""
        holding = try c.decodeIfPresent([String].self, forKey: .holding) ?? []
        fields = try c.decodeIfPresent([String: String].self, forKey: .fields) ?? [:]
        snapshot = try c.decodeIfPresent(String.self, forKey: .snapshot) ?? ""
        probe = try c.decodeIfPresent(Probe.self, forKey: .probe)
        script = try c.decodeIfPresent([[String: String]].self, forKey: .script) ?? []
    }
}

/// A real `ReceiptLedger` driven by the payload's `script`: each step is a
/// verb on the ledger, `as` binds the receipt it made (or re-minted) to a
/// name later steps use as `ref`. The ledger is printed after every step
/// as `LEDGER <json>` — one array of {name, state, authorised, heldUntil,
/// attempts, lastError} rows, newest last — so a test reads state, not
/// source.
@MainActor
func runScript(_ steps: [[String: String]], snapshot: Snapshot?) {
    let ledger = ReceiptLedger()
    var names: [String: String] = [:]   // name -> receipt id
    func id(_ step: [String: String]) -> String { names[step["ref"] ?? ""] ?? "" }
    func bind(_ step: [String: String], _ receiptId: String) {
        if let name = step["as"] { names[name] = receiptId }
    }
    for step in steps {
        let known = Set(ledger.receipts.map(\.id))
        switch step["op"] ?? "" {
        case "enqueue":
            let r = ledger.enqueue(action: step["action"] ?? "x", scope: step["scope"] ?? "s",
                                   fields: [:], effect: .none, pairingToken: "p",
                                   authorised: step["authorised"] == "1")
            bind(step, r.id)
        case "open":
            // "atLeast": a save judged by the card's change number
            // (`.cardRevision(cardId: scope, atLeast:)`); absent is `.none`.
            let effect: ReceiptEffect = step["atLeast"].flatMap(Int.init).map {
                .cardRevision(cardId: step["scope"] ?? "s", atLeast: $0)
            } ?? .none
            let r = ledger.open(action: step["action"] ?? "x", scope: step["scope"] ?? "s",
                                fields: [:], effect: effect, pairingToken: "p")
            bind(step, r.id)
        case "markSending": ledger.markSending(id(step))
        case "accept": ledger.accept(id(step), revision: step["revision"].flatMap(Int.init))
        case "refuse":
            ledger.refuse(id(step), detail: step["detail"] ?? "",
                          surfaced: step["surfaced"] == "1")
        case "holdOff": ledger.holdOff(id(step))
        case "gaveUp": ledger.gaveUp(id(step), why: step["why"] ?? ReceiptLedger.gaveUpLine)
        case "faceNeeded": ledger.gaveUp(id(step), why: ReceiptLedger.faceNeededLine)
        case "retry":
            ledger.retry(id(step))
            if let fresh = ledger.receipts.first(where: { !known.contains($0.id) }) {
                bind(step, fresh.id)
            }
        case "read": ledger.read(id(step))
        case "releaseHolds": ledger.releaseHolds()
        case "settled": if let snap = snapshot { ledger.settled(against: snap) }
        default: break
        }
        let rows: [[String: Any]] = ledger.receipts.map { r in
            let name = names.first(where: { $0.value == r.id })?.key ?? r.id
            return ["name": name, "state": r.state.rawValue, "authorised": r.authorised,
                    "heldUntil": r.heldUntil, "attempts": r.attempts,
                    "lastError": r.lastError, "statusWord": r.statusWord]
        }
        let data = try! JSONSerialization.data(withJSONObject: rows)
        print("LEDGER " + String(decoding: data, as: UTF8.self))
    }
    ledger.forget()
}

@main
enum Runner {
    @MainActor
    static func main() throws {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        let payload = try JSONDecoder().decode(Payload.self, from: data)
        let receipts: [Receipt] = payload.receipts.map { row in
            var r = Receipt(id: row.id, action: row.action, scope: row.scope,
                            fields: row.fields,
                            effect: row.effect == "replyHold"
                                ? .replyHold(sessionId: row.scope, questionId: "q1")
                                : .none,
                            pairingToken: row.pairingToken)
            r.state = Receipt.State(rawValue: row.state) ?? .sent
            r.createdAt = row.createdAt
            r.heldUntil = row.heldUntil
            r.attempts = row.attempts
            r.lastError = row.lastError
            r.acceptedAt = row.acceptedAt
            return r
        }
        let next = ReceiptLedger.nextSendable(in: receipts,
                                              pairingToken: payload.pairingToken,
                                              now: payload.now,
                                              holding: Set(payload.holding))
        print("NEXT \(next?.id ?? "-")")
        print("SPOKEN " + (ReceiptLedger.mark(for: payload.scope, in: receipts)
                           .map(Receipt.spoken(mark:)) ?? "-"))
        print("EFFECT \(ReceiptLedger.effect(for: payload.action, fields: ["session_id": payload.scope, "question_id": "q1", "card_id": payload.scope, "plan_digest": "d1"], scope: payload.scope))")
        print("DUP \(ReceiptLedger.duplicate(action: payload.action, scope: payload.scope, fields: payload.fields, in: receipts))")
        print("HOLD " + receipts.map { ReceiptLedger.holdExpired($0, now: payload.now) ? "1" : "0" }.joined(separator: ","))
        if let probe = payload.probe,
           let snap = try? JSONDecoder().decode(Snapshot.self, from: Data(payload.snapshot.utf8)) {
            print("LANDED \(ReceiptLedger.landed(probe.built, in: snap))")
            print("EVIDENCE \(ReceiptLedger.evidenceBeforeSending(probe.built, in: snap))")
        }
        if !payload.script.isEmpty {
            let snap = try? JSONDecoder().decode(Snapshot.self, from: Data(payload.snapshot.utf8))
            runScript(payload.script, snapshot: snap)
        }
        print("MARK \(ReceiptLedger.mark(for: payload.scope, in: receipts) ?? "-")")
        print("NOTE \(ReceiptLedger.note(for: payload.scope, in: receipts, now: payload.now)?.text ?? "-")")
        print("ROWS " + ReceiptLedger.queueRows(in: receipts, now: payload.now).map(\.id).joined(separator: ","))
        print("WORDS " + receipts.map(\.statusWord).joined(separator: ","))
    }
}
'''


def _text(name: str) -> str:
    path = PHONE / name
    assert path.is_file(), f"{name} moved"
    return path.read_text()


def _block(text: str, header: str, span: int = 2400) -> str:
    start = text.index(header)
    return text[start:start + span]


@pytest.fixture(scope="module")
def queue_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("phone-queue")
    main = folder / "queue_main.swift"
    main.write_text(HARNESS)
    binary = folder / "phone-queue"
    built = subprocess.run(
        [swiftc] + [str(PHONE / name) for name in SOURCES] + [str(main),
                                                             "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    return binary


def _run(binary: pathlib.Path, payload: dict) -> dict:
    """The harness's key/value lines; `LEDGER` is the scripted ledger after
    its last step (`_ledger` for every step). `HOME` sits beside the
    binary so `ReceiptLedger.persist()` writes a scratch Application
    Support, never the account's."""
    home = binary.parent / "home"
    home.mkdir(exist_ok=True)
    proc = subprocess.run([str(binary)], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30,
                          check=False, env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        out[key] = value
    return out


def _ledger(binary: pathlib.Path, script: list[dict], snapshot: str = "") -> dict:
    """`{name: row}` of the scripted ledger after its last step."""
    out = _run(binary, {"script": script, "snapshot": snapshot})
    return {row["name"]: row for row in json.loads(out["LEDGER"])}


def _r(id: str, scope: str, state: str = "queued", created: float = 0,
       action: str = "x", **more) -> dict:
    row = {"id": id, "scope": scope, "state": state, "createdAt": created,
           "action": action}
    row.update(more)
    return row


# --- the arithmetic -------------------------------------------------------------


def test_creation_order_across_scopes(queue_bin):
    """The oldest queued press goes first whatever its scope."""
    out = _run(queue_bin, {"receipts": [
        _r("later", "card-b", created=20),
        _r("first", "card-a", created=10),
        _r("third", "card-c", created=30),
    ]})
    assert out["NEXT"] == "first"


def test_a_scope_waits_for_its_own_earlier_press(queue_bin):
    """Approve then Start on one card: the start is not returned while the
    approval is still in `sent` behind its backoff — and a **different**
    card's press is taken instead, so scopes interleave."""
    out = _run(queue_bin, {"now": 1000, "receipts": [
        _r("approve", "card-a", state="sent", created=10, heldUntil=1500,
           attempts=1),
        _r("start", "card-a", created=20),
        _r("other", "card-b", created=30),
    ]})
    assert out["NEXT"] == "other"
    # And with nothing else in the queue, nothing goes: the scope waits.
    out = _run(queue_bin, {"now": 1000, "receipts": [
        _r("approve", "card-a", state="sent", created=10, heldUntil=1500,
           attempts=1),
        _r("start", "card-a", created=20),
    ]})
    assert out["NEXT"] == "-"


def test_a_sent_press_past_its_backoff_goes_before_the_queued_one_behind_it(queue_bin):
    out = _run(queue_bin, {"now": 2000, "receipts": [
        _r("approve", "card-a", state="sent", created=10, heldUntil=1500,
           attempts=1),
        _r("start", "card-a", created=20),
    ]})
    assert out["NEXT"] == "approve"


def test_an_earlier_refusal_releases_the_later_press(queue_bin):
    """A `.stuck` record (the Mac refused it in words) is finished business
    for the ordering: the press behind it goes."""
    out = _run(queue_bin, {"receipts": [
        _r("approve", "card-a", state="stuck", created=10,
           lastError="this card has no plan yet"),
        _r("start", "card-a", created=20),
    ]})
    assert out["NEXT"] == "start"


def test_a_sending_press_is_skipped_and_holds_its_scope(queue_bin):
    out = _run(queue_bin, {"receipts": [
        _r("out", "card-a", state="sending", created=10),
        _r("next", "card-a", created=20),
        _r("elsewhere", "card-b", created=30),
    ]})
    assert out["NEXT"] == "elsewhere"


def test_another_pairings_press_and_a_spent_one_are_never_taken(queue_bin):
    out = _run(queue_bin, {"pairingToken": "mine", "now": 5000, "receipts": [
        _r("foreign", "card-a", created=10, pairingToken="theirs"),
        _r("spent", "card-b", state="sent", created=20, attempts=5),
        _r("ok", "card-c", created=30, pairingToken="mine"),
    ]})
    assert out["NEXT"] == "ok"


def test_duplicate_is_the_same_scope_and_action_while_unresolved(queue_bin):
    """True in `.sending` — and in `.sent` and `.queued` — false once the
    Mac has answered (`.refused`, `.done`, `.stuck`), so a second copy sent
    after the first returned is a deliberate second press."""
    base = {"scope": "card-a", "action": "board_dispatch"}
    for state in ("sending", "queued", "sent"):
        out = _run(queue_bin, dict(base, receipts=[
            _r("one", "card-a", state=state, action="board_dispatch")]))
        assert out["DUP"] == "true", state
    for state in ("refused", "done", "stuck", "accepted"):
        out = _run(queue_bin, dict(base, receipts=[
            _r("one", "card-a", state=state, action="board_dispatch")]))
        assert out["DUP"] == "false", state
    # A different action on the same scope is not a duplicate: it queues.
    out = _run(queue_bin, dict(base, receipts=[
        _r("one", "card-a", state="queued", action="board_approve_plan")]))
    assert out["DUP"] == "false"


def test_an_answer_stays_a_duplicate_while_it_lands(queue_bin):
    """The answer verbs' `.accepted` copy counts too (`heldWhileLanding`):
    the Mac said yes, the question is still listed for a poll cadence, and
    a second copy under a fresh token would be a second keystroke burst.
    Every other verb is released on the 200 as before."""
    for action in ("answer_question", "answer_questions", "reply"):
        out = _run(queue_bin, {"scope": "s", "action": action, "receipts": [
            _r("one", "s", state="accepted", action=action)]})
        assert out["DUP"] == "true", action
        out = _run(queue_bin, {"scope": "s", "action": action, "receipts": [
            _r("one", "s", state="done", action=action)]})
        assert out["DUP"] == "false", action
    out = _run(queue_bin, {"scope": "s", "action": "board_dispatch", "receipts": [
        _r("one", "s", state="accepted", action="board_dispatch")]})
    assert out["DUP"] == "false"


def test_the_answer_verbs_are_judged_by_the_question_leaving_the_row(queue_bin):
    """`effect(for:)` maps an answer to `.questionGone`, so the 200 closes
    the record `.accepted` rather than `.done` and the mark stays SENT until
    the snapshot agrees — the hold `settlingAnswers` used to be, on the
    receipt. `reply` stays `.none` in the table (the client upgrades it on
    a waiting row against the snapshot)."""
    for action in ("answer_question", "answer_questions"):
        out = _run(queue_bin, {"scope": "s1", "action": action})
        assert out["EFFECT"] == 'questionGone(sessionId: "s1", questionId: "q1")', action
    out = _run(queue_bin, {"scope": "s1", "action": "reply"})
    assert out["EFFECT"] == "none"


def test_a_held_scope_is_skipped_this_sweep_and_its_later_presses_wait(queue_bin):
    """`holding` — a scope whose synchronous press holds `post`'s lock — is
    walked past like a scope with a press in flight: no attempt is spent on
    a transmit that cannot start, and the press behind it on the same scope
    waits too, while another scope's press goes."""
    out = _run(queue_bin, {"holding": ["card-a"], "receipts": [
        _r("first", "card-a", created=10),
        _r("second", "card-a", created=20),
        _r("other", "card-b", created=30),
    ]})
    assert out["NEXT"] == "other"
    out = _run(queue_bin, {"holding": ["card-a"], "receipts": [
        _r("first", "card-a", created=10)]})
    assert out["NEXT"] == "-"


def test_mark_reads_queued_sending_sent_and_nothing_else(queue_bin):
    for state, word in (("queued", "QUEUED"), ("sending", "SENDING…"),
                        ("sent", "SENT"), ("accepted", "SENT")):
        out = _run(queue_bin, {"scope": "s", "receipts": [
            _r("one", "s", state=state)]})
        assert out["MARK"] == word, state
    for state in ("done", "refused", "stuck"):
        out = _run(queue_bin, {"scope": "s", "receipts": [
            _r("one", "s", state=state)]})
        assert out["MARK"] == "-", state
    # The newest press on the scope wins the mark.
    out = _run(queue_bin, {"scope": "s", "receipts": [
        _r("old", "s", state="sent", created=10),
        _r("new", "s", state="queued", created=20)]})
    assert out["MARK"] == "QUEUED"


def test_the_note_is_the_macs_words_until_a_newer_press_is_queued(queue_bin):
    out = _run(queue_bin, {"scope": "s", "now": 100, "receipts": [
        _r("one", "s", state="stuck", created=10,
           lastError="this card has no plan yet")]})
    assert out["NOTE"] == "this card has no plan yet"
    out = _run(queue_bin, {"scope": "s", "now": 100, "receipts": [
        _r("one", "s", state="stuck", created=10,
           lastError="this card has no plan yet"),
        _r("two", "s", state="queued", created=20)]})
    assert out["NOTE"] == "-"


def test_the_note_ages_off_and_is_never_the_phones_own_stop(queue_bin):
    """Every screen re-reads the note on appearance, so a refusal is the
    note only for `refusedShown` (600 s) — a Start refused hours ago must
    not re-arm "Start unplanned?" on a card that has a plan by now — and a
    `.stuck` record wearing one of the phone's own sentences is never the
    note: nobody refused it, and the QUEUE list is where its RETRY lives."""
    fresh = _r("one", "s", state="refused", created=100,
               lastError="this card has no plan yet")
    assert _run(queue_bin, {"scope": "s", "now": 699,
                            "receipts": [fresh]})["NOTE"] == "this card has no plan yet"
    assert _run(queue_bin, {"scope": "s", "now": 800,
                            "receipts": [fresh]})["NOTE"] == "-"
    receipts = _text("Receipts.swift")
    for line in ("The Mac accepted it, but nothing on the board has changed.",
                 "Tried several times and never got an answer. RETRY to try again.",
                 "Too long ago to send safely — RETRY to send it as a new press.",
                 "this was sent to a different pairing",
                 "Face ID or your passcode is needed to act from away."):
        assert line in receipts, line
        out = _run(queue_bin, {"scope": "s", "now": 110, "receipts": [
            _r("one", "s", state="stuck", created=100, action="answer_question",
               lastError=line)]})
        assert out["NOTE"] == "-", line
    out = _run(queue_bin, {"scope": "s", "now": 110, "receipts": [
        _r("one", "s", state="stuck", created=100,
           lastError="away access has lapsed")]})
    assert out["NOTE"] == "away access has lapsed"
    body = _block(receipts, "    static func note(for scope: String", 1400)
    assert "now - refused.createdAt <= refusedShown" in body
    assert "phoneAuthored.contains(refused.lastError)" in body
    client = _text("Client.swift")
    assert "ReceiptLedger.note(for: scope, in: receipts.receipts,\n" \
           "                                  now: Date().timeIntervalSince1970)" in client


def test_a_superseded_refusal_never_comes_back(queue_bin):
    """A later press on the scope the Mac accepted, or that landed, is the
    person having moved on: the older refusal is not the scope's last word
    once that press closes. Only a **newer** refusal is drawn."""
    for state in ("accepted", "done", "refused", "sent"):
        out = _run(queue_bin, {"scope": "s", "now": 100, "receipts": [
            _r("one", "s", state="stuck", created=10,
               lastError="this card has no plan yet"),
            _r("two", "s", state=state, created=20)]})
        assert out["NOTE"] == "-", state
    out = _run(queue_bin, {"scope": "s", "now": 100, "receipts": [
        _r("one", "s", state="done", created=10),
        _r("two", "s", state="stuck", created=20,
           lastError="away access has lapsed")]})
    assert out["NOTE"] == "away access has lapsed"


def test_the_mark_is_spoken_as_drawn(queue_bin):
    """A control drawn QUEUED is spoken "Queued", never "Sending"."""
    for state, word in (("queued", "Queued"), ("sending", "Sending"),
                        ("sent", "Sent"), ("accepted", "Sent")):
        out = _run(queue_bin, {"scope": "s", "receipts": [
            _r("one", "s", state=state)]})
        assert out["SPOKEN"] == word, state


def test_queue_rows_are_oldest_first_and_list_a_fresh_refusal(queue_bin):
    out = _run(queue_bin, {"now": 1000, "receipts": [
        _r("c", "s", state="sent", created=30),
        _r("a", "s", state="queued", created=10),
        _r("done", "s", state="done", created=15),
        _r("fresh-refusal", "s", state="refused", created=900),
        _r("old-refusal", "s", state="refused", created=100),
        _r("b", "s", state="stuck", created=20),
    ]})
    assert out["ROWS"] == "a,b,c,fresh-refusal"


def test_status_words_tell_a_refusal_from_a_phone_side_stop(queue_bin):
    out = _run(queue_bin, {"receipts": [
        _r("q", "s", state="queued"),
        _r("g", "s", state="sending"),
        _r("s", "s", state="sent"),
        _r("l", "s", state="accepted"),
        _r("d", "s", state="done"),
        _r("r", "s", state="refused"),
        _r("m", "s", state="stuck", lastError="away access has lapsed"),
        _r("p", "s", state="stuck",
           lastError="Tried several times and never got an answer. RETRY to try again."),
    ]})
    assert out["WORDS"] == \
        "QUEUED,SENDING…,SENT,LANDING,DONE,REFUSED,REFUSED,STUCK"
    # A declined Face ID sheet on a queued press is the phone's own stop.
    out = _run(queue_bin, {"receipts": [
        _r("f", "s", state="stuck",
           lastError="Face ID or your passcode is needed to act from away.")]})
    assert out["WORDS"] == "STUCK"


# --- the wiring, pinned -----------------------------------------------------------


def test_enqueue_takes_no_lock_and_awaits_no_transport():
    client = _text("Client.swift")
    body = _block(client, "    func enqueue(action: String", 3200)
    assert "writesInFlight" not in body
    assert "refreshAfterWrite" not in body
    assert "channel.request" not in body and "home.request" not in body
    assert "ReceiptLedger.duplicate(" in body
    assert "Self.queuedTwiceRefusal" in body
    # Face ID before the record exists, and remembered on it.
    assert body.index("RemoteAuth.shared.authorize()") < body.index("receipts.enqueue(")
    assert "authorised: knowsItIsAway" in body
    assert "kickSender()" in body


def test_the_sender_is_one_ordered_task_that_passes_the_face_check_through():
    client = _text("Client.swift")
    body = _block(client, "    func flushReceipts() async", 4400)
    assert "ReceiptLedger.nextSendable(" in body
    # A scope whose synchronous press holds `post`'s lock is walked past
    # before the attempt is counted, never sent into `stillSendingRefusal`.
    assert "holding: holding" in body
    assert body.index("writesInFlight[receipt.scope] != nil") \
        < body.index("receipts.markSending(receipt.id)")
    assert "holding.insert(receipt.scope)" in body
    assert "senderKicked = true" not in body[body.index("var holding"):], (
        "a held scope is taken on the next onLive, never a spin on the lock")
    # The answer verbs keep the replay-window give-up with their hold.
    assert "ReceiptLedger.heldWhileLanding.contains(receipt.action)" in body
    # A dial press the Mac refused ends its held value at once (after the
    # post, so past the pinned span above).
    assert "endSettlingPreference(receipt.scope)" in \
        _block(client, "    func flushReceipts() async", 5200)
    assert "receipts.markSending(receipt.id)" in body
    assert "authorised: receipt.authorised" in body
    assert "refreshAfter: false" in body
    assert "senderRunning" in body and "senderKicked" in body
    assert "token: receipt.id" in body


def test_post_asks_for_the_face_only_where_the_press_was_not_authorised():
    client = _text("Client.swift")
    body = client[client.index("func post(action: String"):client.index("func quietPost(")]
    assert "authorised: Bool = false" in body
    assert "authorised ? true : await RemoteAuth.shared.authorize()" in body



def test_a_declined_face_on_a_queued_press_stays_a_row_with_retry(queue_bin):
    """A first press that declines the sheet leaves nothing (the person is
    at the sheet). A queued or replayed press that declines — queued at
    home, delivered by the relay fallback, or a `.sent` press re-taken
    outside the grace — is closed `.stuck` carrying the phone's own
    sentence, so it stays a STUCK row with RETRY rather than vanishing with
    no mark, no note and no row; and a read of that row leaves it alone,
    because nobody refused it."""
    client = _text("Client.swift")
    body = client[client.index("func post(action: String"):client.index("func quietPost(")]
    decline = body[body.index("guard admitted else {"):body.index("let answer = await channel.request")]
    assert "if firstPress {" in decline
    assert "receipts.remove(mark)" in decline
    assert "receipts.refuse(mark, detail: ReceiptLedger.faceNeededLine," in decline
    assert "surfaced: false" in decline
    rows = _ledger(queue_bin, [
        {"op": "enqueue", "as": "a", "scope": "s1"},
        {"op": "faceNeeded", "ref": "a"},
        {"op": "read", "ref": "a"},
    ])
    assert rows["a"]["state"] == "stuck"
    assert rows["a"]["statusWord"] == "STUCK"
    assert "passcode" in rows["a"]["lastError"]


def test_the_face_pass_covers_one_press_for_the_replay_window_and_retry_asks_again(queue_bin):
    """A RETRY re-sends a press under a fresh token as a **new press**, and
    a new press from away is asked for the face: the pass remembered at the
    original tap goes with the old token. And the sender honours a pass
    only while the Mac would still dedupe that press (`replayWindow`): a
    record older than that — held off that long, or read back after a
    relaunch — is closed `.stuck` on `faceNeededLine` rather than sent on
    a stale check. Fails closed: the sender never raises the sheet itself,
    and the STUCK row's RETRY asks."""
    rows = _ledger(queue_bin, [
        {"op": "enqueue", "as": "a", "scope": "s1", "authorised": "1"},
        {"op": "markSending", "ref": "a"},
        {"op": "holdOff", "ref": "a"},
        {"op": "retry", "ref": "a", "as": "again"},
    ])
    assert "a" not in rows, "RETRY mints a fresh token"
    assert rows["again"]["state"] == "sent"
    assert rows["again"]["authorised"] is False
    assert rows["again"]["attempts"] == 0
    assert rows["again"]["heldUntil"] == 0
    client = _text("Client.swift")
    body = _block(client, "    func flushReceipts() async", 4800)
    stale = body[body.index("if receipt.authorised, past {"):body.index("receipts.markSending(receipt.id)")]
    assert "receipts.gaveUp(receipt.id, why: ReceiptLedger.faceNeededLine)" in stale
    assert "continue" in stale
    # `past` is the one clock read both window checks share.
    window = body[body.index("let past = "):body.index("if receipt.authorised, past {")]
    assert "> ReceiptLedger.replayWindow" in window
    assert "RemoteAuth" not in body, "the sender never raises the sheet itself"


def test_the_agent_screens_two_note_readers_split_by_verb():
    """One note per session, two readers on the agent screen: the answer
    box takes an answer's or a reply's refusal, the screen every other
    verb's — never the same refusal drawn twice — and only the reader that
    draws it reads it."""
    receipts = _text("Receipts.swift")
    assert "var isAnswer: Bool { ReceiptLedger.heldWhileLanding.contains(action) }" \
        in receipts
    box = _text("AnswerBox.swift")
    take = _block(box, "    private func takeNote()", 400)
    assert "queued.isAnswer else { return }" in take
    assert "client.readQueueNote(for: agent.sessionId)" in take
    detail = _text("AgentDetailView.swift")
    take = _block(detail, "    private func takeNote(_ queued: QueueNote?)", 400)
    assert "!queued.isAnswer else { return }" in take


def test_the_card_message_line_is_read_off_the_receipt():
    """The message box's confirmation follows the press — queued, sending,
    sent, then "Sent." — rather than a stored "queued" that never moved."""
    card = _text("CardDetailView.swift")
    assert "sendNote = ReceiptLedger.queuedLine" not in card
    line = _block(card, "    private var sendLine: String", 700)
    assert "client.queuedState(action: PhoneActions.boardMessage," in line
    assert "case .queued: return ReceiptLedger.queuedLine" in line
    assert "case .sending: return ReceiptLedger.sendingLine" in line
    assert 'case .done, nil: return "Sent."' in line
    assert "Text(sendLine)" in card and "Text(sendNote)" not in card



def test_a_read_that_changes_nothing_writes_nothing(queue_bin):
    """`read` moves a `.stuck` record wearing the Mac's words to `.refused`
    and touches nothing else: a queued press, an accepted one, and a stuck
    one wearing the phone's own sentence read back unchanged."""
    rows = _ledger(queue_bin, [
        {"op": "enqueue", "as": "queued", "scope": "s1"},
        {"op": "enqueue", "as": "refused", "scope": "s2"},
        {"op": "refuse", "ref": "refused", "detail": "The Mac said no."},
        {"op": "enqueue", "as": "ours", "scope": "s3"},
        {"op": "gaveUp", "ref": "ours"},
        {"op": "read", "ref": "queued"},
        {"op": "read", "ref": "refused"},
        {"op": "read", "ref": "ours"},
    ])
    assert rows["queued"]["state"] == "queued"
    assert rows["refused"]["state"] == "refused"
    assert rows["refused"]["lastError"] == "The Mac said no."
    assert rows["ours"]["state"] == "stuck" and rows["ours"]["statusWord"] == "STUCK"


def test_a_reply_on_a_waiting_row_takes_the_answer_hold(queue_bin):
    """`effect(for:)` cannot see the row, so `enqueue` upgrades a reply on
    a waiting row against the snapshot — to `.replyHold`, judged like
    `.questionGone` but **carrying a payload**: the audit's blocker (20 Sep
    2026) was a queued reply dropped unsent, with no row and no note, when
    the row left `waiting` before the sender took it, because the old
    `.questionGone` read as goal-achieved in `evidenceBeforeSending`."""
    client = _text("Client.swift")
    body = _block(client, "    func enqueue(action: String", 3200)
    assert "action == PhoneActions.reply, effect == .none" in body
    assert "snapshot.agents.waiting.first(" in body
    assert "effect = .replyHold(sessionId: scopeKey," in body
    assert "effect = .questionGone(" not in body
    assert "effect: effect," in body
    # `.replyHold` is never goal-achieved before sending, even once the row
    # has left `waiting`; `.questionGone` on the same snapshot is.
    gone = json.dumps({"agents": {"running": [], "waiting": [], "sleeping": []}})
    for effect, expected in (("replyHold", "false"), ("questionGone", "true")):
        out = _run(queue_bin, {"snapshot": gone, "probe": {"effect": effect, "id": "s1", "extra": "q1"}})
        assert out["EVIDENCE"] == expected, effect


def test_a_queued_reply_is_never_dropped_unsent_and_lands_like_an_answer(queue_bin):
    """`.replyHold` lands on the same evidence as `.questionGone` (the row
    out of `waiting`, or on a different question) and is **never** evidence
    before sending, whatever the snapshot says."""
    gone = json.dumps({"agents": {"waiting": [], "running": [], "sleeping": []}})
    same = json.dumps({"agents": {"waiting": [
        {"session_id": "s1", "question": {"id": "q1"}}]}})
    for effect in ("replyHold", "questionGone"):
        out = _run(queue_bin, {"snapshot": gone,
                               "probe": {"effect": effect, "id": "s1", "extra": "q1"}})
        assert out["LANDED"] == "true", effect
        out = _run(queue_bin, {"snapshot": same,
                               "probe": {"effect": effect, "id": "s1", "extra": "q1"}})
        assert out["LANDED"] == "false", effect
    out = _run(queue_bin, {"snapshot": gone,
                           "probe": {"effect": "replyHold", "id": "s1", "extra": "q1"}})
    assert out["EVIDENCE"] == "false"
    out = _run(queue_bin, {"snapshot": gone,
                           "probe": {"effect": "questionGone", "id": "s1", "extra": "q1"}})
    assert out["EVIDENCE"] == "true"


def test_a_reply_on_a_row_that_keeps_waiting_closes_done_after_the_hold(queue_bin):
    """A row blocked on something else (a permission, an unconsumed
    channel message) stays on the same question: the accepted reply is
    `.done` once `replyHold` has passed — never `.stuck` saying nothing
    changed — and only a `.replyHold` effect expires this way."""
    rows = [_r("reply", "s1", state="accepted", action="reply",
               effect="replyHold", acceptedAt=995),
            _r("late", "s1", state="accepted", action="reply",
               effect="replyHold", acceptedAt=900),
            _r("answer", "s1", state="accepted", action="answer_question",
               acceptedAt=900)]
    out = _run(queue_bin, {"receipts": rows, "now": 1000})
    assert out["HOLD"] == "0,1,0"
    out = _run(queue_bin, {"receipts": rows, "now": 1000.5 + 10})
    assert out["HOLD"] == "1,1,0"
    receipts = _text("Receipts.swift")
    settled = _block(receipts, "    func settled(against snapshot: Snapshot)", 900)
    assert "|| Self.holdExpired(receipts[index], now: now)" in settled
    assert "static let replyHold: TimeInterval = 10" in receipts


def test_approve_is_judged_by_the_digest_it_carried(queue_bin):
    """A card whose plan changed since an older approval already carries a
    non-empty `plan_approved`: "any approval" read as landed on the very
    snapshot that offers Approve and the press was removed unsent (the
    audit's blocker). The effect carries the digest from `plan_digest`."""
    out = _run(queue_bin, {"scope": "c1", "action": "board_approve_plan"})
    assert out["EFFECT"] == 'planApproved(cardId: "c1", digest: "d1")'
    old = json.dumps({"board": {"available": True, "cards": [
        {"id": "c1", "column_name": "backlog", "plan_approved": "old"}]}})
    new = json.dumps({"board": {"available": True, "cards": [
        {"id": "c1", "column_name": "backlog", "plan_approved": "new"}]}})
    probe = {"effect": "planApproved", "id": "c1", "extra": "new"}
    out = _run(queue_bin, {"snapshot": old, "probe": probe})
    assert out["LANDED"] == "false" and out["EVIDENCE"] == "false"
    out = _run(queue_bin, {"snapshot": new, "probe": probe})
    assert out["LANDED"] == "true" and out["EVIDENCE"] == "true"
    # An older caller with no digest: any approval, as before.
    out = _run(queue_bin, {"snapshot": old,
                           "probe": {"effect": "planApproved", "id": "c1"}})
    assert out["LANDED"] == "true"
    card = _text("CardDetailView.swift")
    approve = _block(card, "    private func approvePlan(_ plan: CardPlan)", 400)
    assert '"plan_digest": plan.digest' in approve


def test_delete_on_an_abandoned_row_is_judged_against_every_list(queue_bin):
    """An abandoned row is out of the live buckets by definition, so
    `.rowGone` read "already happened" on the snapshot that offered Delete
    and `delete_abandoned_agent` was removed unsent (the audit's blocker).
    `delete_agent` maps to `.rowGoneAnywhere`, which lands only when the
    row is listed nowhere; `enqueue` upgrades any other row verb the same
    way when its row is already out of the live buckets."""
    out = _run(queue_bin, {"scope": "s1", "action": "delete_agent"})
    assert out["EFFECT"] == 'rowGoneAnywhere(sessionId: "s1")'
    out = _run(queue_bin, {"scope": "s1", "action": "stop_session"})
    assert out["EFFECT"] == 'rowGone(sessionId: "s1")'
    abandoned = json.dumps({"agents": {"waiting": [], "running": [], "sleeping": [],
                                       "finished": [],
                                       "abandoned": [{"session_id": "s1"}]}})
    gone = json.dumps({"agents": {"abandoned": []}})
    out = _run(queue_bin, {"snapshot": abandoned,
                           "probe": {"effect": "rowGone", "id": "s1"}})
    assert out["LANDED"] == "true", "the old judge: already gone from live"
    out = _run(queue_bin, {"snapshot": abandoned,
                           "probe": {"effect": "rowGoneAnywhere", "id": "s1"}})
    assert out["LANDED"] == "false" and out["EVIDENCE"] == "false"
    out = _run(queue_bin, {"snapshot": gone,
                           "probe": {"effect": "rowGoneAnywhere", "id": "s1"}})
    assert out["LANDED"] == "true"
    client = _text("Client.swift")
    body = _block(client, "    func enqueue(action: String", 3200)
    assert "if case .rowGone(let sid) = effect {" in body
    assert "effect = .rowGoneAnywhere(sessionId: sid)" in body


def test_duplicate_reads_the_payload_so_a_different_press_queues(queue_bin):
    """A true double-tap carries identical fields and is refused; a second
    reply with other words, a verdict on a second prompt, a `board_update`
    of the tool then the model, ▲ then ▼, autostart on then off each queue
    behind the first. The two answer verbs are keyed on `question_id`
    alone."""
    cases = [
        ("reply", {"session_id": "s", "text": "one"}, {"session_id": "s", "text": "two"}),
        ("permission_verdict", {"request_id": "r1", "behavior": "allow"},
         {"request_id": "r2", "behavior": "allow"}),
        ("board_update", {"card_id": "c", "tool": "codex"}, {"card_id": "c", "model": "x"}),
        ("board_update", {"card_id": "c", "effort": "low"}, {"card_id": "c", "effort": "high"}),
        ("board_update", {"card_id": "c", "model": "x"}, {"card_id": "c", "effort": "high"}),
        ("board_queue_move", {"card_id": "c", "before_id": "a"}, {"card_id": "c", "before_id": "b"}),
        ("set_board_autostart", {"enabled": "on"}, {"enabled": "off"}),
    ]
    for action, first, second in cases:
        rows = [_r("one", "s", state="queued", action=action, fields=first)]
        out = _run(queue_bin, {"scope": "s", "action": action, "fields": first,
                               "receipts": rows})
        assert out["DUP"] == "true", (action, "the same press twice")
        out = _run(queue_bin, {"scope": "s", "action": action, "fields": second,
                               "receipts": rows})
        assert out["DUP"] == "false", (action, "a different press")
    for action in ("answer_question", "answer_questions"):
        rows = [_r("one", "s", state="accepted", action=action,
                   fields={"question_id": "q1", "option_index": "0"})]
        out = _run(queue_bin, {"scope": "s", "action": action,
                               "fields": {"question_id": "q1", "option_index": "1"},
                               "receipts": rows})
        assert out["DUP"] == "true", (action, "the same dialog, another option")
        out = _run(queue_bin, {"scope": "s", "action": action,
                               "fields": {"question_id": "q2", "option_index": "0"},
                               "receipts": rows})
        assert out["DUP"] == "false", (action, "another dialog")
    client = _text("Client.swift")
    body = _block(client, "    func enqueue(action: String", 3200)
    assert "fields: fields, in: receipts.receipts" in body
    # The queue controls surface the queue's answer rather than dropping it.
    view = _text("PipelineView.swift")
    assert "_ = await client.enqueue(" not in view
    controls = _block(view, "    private func move(_ card: BoardCard, beforeId: String)", 600)
    assert "client.notePipeline(card.id, result.detail)" in controls


def test_a_drawn_refusal_is_read_and_ages_off_the_queue_list(queue_bin):
    """A queued press's refusal closes `.stuck` — the sender has no screen
    behind it — and `.stuck` never ages. Every screen that draws the note
    marks it read (`readQueueNote`: `.stuck` with the Mac's words →
    `.refused`, governed by `refusedShown`), **keeping** the record so it
    stays the subject's newest receipt: the card screen's plan-gate
    sentence cannot be swapped for an older refusal's stale words while
    Start sits armed (the audit's minor). A phone-authored STUCK row is
    left alone: nobody refused it, and RETRY is what it offers."""
    rows = _ledger(queue_bin, [
        {"op": "enqueue", "as": "a", "scope": "s1"},
        {"op": "refuse", "ref": "a", "detail": "Start needs an approved plan."},
        {"op": "read", "ref": "a"},
    ])
    assert rows["a"]["state"] == "refused", "read, and kept as the newest receipt"
    assert rows["a"]["lastError"] == "Start needs an approved plan."
    client = _text("Client.swift")
    assert "func clearQueueNote(" not in client
    reader = _block(client, "    func readQueueNote(for scope: String)", 300)
    assert "receipts.read(note.id)" in reader
    assert "receipts.remove(" not in reader
    card = _text("CardDetailView.swift")
    arrived = _block(card, "    private func noteArrived(_ queued: QueueNote?)", 1200)
    assert "client.readQueueNote(for: card.id)" in arrived
    assert "clearQueueNote" not in card
    # Read before the gate branches, so a plain refusal is read too.
    assert arrived.index("client.readQueueNote(for: card.id)") \
        < arrived.index("isPlanGateRefusal")
    for name, scope in (("AgentDetailView.swift", "agent.sessionId"),
                        ("AnswerBox.swift", "agent.sessionId"),
                        ("NeedsYouView.swift", "scope(item)"),
                        ("PipelineView.swift", "key")):
        assert f"client.readQueueNote(for: {scope})" in _text(name), name


def test_a_successful_poll_releases_the_transport_backoff(queue_bin):
    """A `.sent` press's hold is a transport backoff (`holdOff`); a poll
    that went through proves the transport back, so the hold ends and the
    next sweep re-takes it at poll cadence under the same attempt ceiling
    — a fresh `.queued` press on that scope no longer waits up to
    `backoffCap` behind it (the audit's minor)."""
    rows = _ledger(queue_bin, [
        {"op": "enqueue", "as": "sent", "scope": "s1"},
        {"op": "markSending", "ref": "sent"},
        {"op": "holdOff", "ref": "sent"},
        {"op": "enqueue", "as": "stuck", "scope": "s2"},
        {"op": "refuse", "ref": "stuck", "detail": "no"},
        {"op": "releaseHolds"},
    ])
    assert rows["sent"]["state"] == "sent" and rows["sent"]["heldUntil"] == 0
    assert rows["stuck"]["state"] == "stuck" and rows["stuck"]["heldUntil"] > 0
    client = _text("Client.swift")
    for site in [i for i in range(len(client)) if client.startswith("receipts.settled(against: snapshot)", i)]:
        assert "receipts.releaseHolds()" in client[site - 400:site], "released before settled"


def test_the_card_delete_label_wears_the_mark_while_queued():
    card = _text("CardDetailView.swift")
    body = _block(card, "    private var deleteLabel: String", 400)
    assert "if pressed == .delete { return mark }" in body
    assert 'if settlingHere { return "SENDING…" }' in body


def test_the_scope_is_resolved_in_one_place_for_both_routes():
    client = _text("Client.swift")
    assert len(re.findall(r"\bfunc scopeKey\(", client)) == 1
    assert "Self.scopeKey(" in _block(client, "    func enqueue(action: String", 3200)
    assert "Self.scopeKey(" in client[client.index("func post(action: String"):client.index("func quietPost(")]


def test_no_view_waits_on_a_refresh_after_a_press():
    for name in ("NeedsYouView.swift", "AnswerBox.swift",
                 "AgentDetailView.swift", "PipelineView.swift"):
        assert "refreshAfterWrite" not in _text(name), name


def test_the_answer_box_no_longer_disables_on_a_press():
    box = _text("AnswerBox.swift")
    assert ".disabled(busy)" not in box
    assert "client.enqueue(" in box
    assert "client.post(" not in box
    assert "client.queueMark(for: agent.sessionId)" in box


def test_the_macs_words_are_read_on_arrival_not_only_on_change():
    """A refusal landing while the screen is not up never fires `.onChange`;
    every screen that draws a queue note also reads it in `.onAppear`, and
    the card screen arms the plan gate off it there too — `Arm`'s window
    would otherwise run out unseen."""
    box = _text("AnswerBox.swift")
    appear = box[box.index(".onAppear {"):]
    assert "client.queueNote(for: agent.sessionId)" in appear[:appear.index("}\n        }")]
    detail = _text("AgentDetailView.swift")
    appear = detail[detail.index("        .onAppear {\n            syncWatch()"):]
    assert "client.queueNote(for: agent.sessionId)" in appear[:appear.index("        }")]
    card = _text("CardDetailView.swift")
    appear = card[card.index("        .onAppear {\n            arm.onDisarm"):]
    assert "noteArrived(client.queueNote(for: card.id))" in appear[:appear.index("        .onDisappear")]


def test_a_delete_pops_its_screen_when_the_row_goes_not_when_the_press_is_written():
    """`ok` from `enqueue` means written down. The agent and card screens
    keep themselves up while the press is in play — a refusal from the
    Mac's re-check needs a screen to land on — and pop when the row or
    card is gone from the snapshot, or the mark leaves with no note."""
    detail = _text("AgentDetailView.swift")
    assert "if ok && pop { dismiss() }" not in detail
    assert "if ok && pop { deleteQueued = true }" in detail
    helper = _block(detail, "    private func leaveDeletedDetail()", 600)
    assert "if !rowPresent { dismiss(); return }" in helper
    assert "client.queueNote(for: agent.sessionId) == nil" in helper
    assert "deleteQueued = false" in helper
    assert ".onChange(of: outAction) { _, _ in\n            leaveDeletedDetail()" in detail
    card = _text("CardDetailView.swift")
    assert "if pop { dismiss() }" not in card
    assert "if pop { deleteQueued = true }" in card
    helper = _block(card, "    private func leaveDeletedCard()", 600)
    assert "if !cardIsLive { dismiss(); return }" in helper
    assert "client.queueNote(for: card.id) == nil" in helper
    assert ".onChange(of: cardIsLive) { _, _ in leaveDeletedCard() }" in card


def test_the_spoken_label_says_the_mark_that_is_drawn():
    """QUEUED is spoken "Queued", not "Sending"."""
    for name in ("AnswerBox.swift", "AgentDetailView.swift",
                 "CardDetailView.swift", "PipelineView.swift"):
        text = _text(name)
        code = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("//"))
        assert '"Sending"' not in code, name
    assert _text("Receipts.swift").count("static func spoken(mark: String)") == 1


def test_a_refused_hide_keeps_the_macs_words():
    detail = _text("AgentDetailView.swift")
    helper = detail.split("private func leaveHiddenDetail()", 1)[1].split("var body:", 1)[0]
    assert helper.index("client.queueNote(for: agent.sessionId) == nil") \
        < helper.index('note = "The Mac still lists this thread;')


def test_the_agent_and_card_screens_send_through_the_queue():
    detail = _text("AgentDetailView.swift")
    assert "client.enqueue(" in detail
    # Rebuild & restart alone is synchronous: a banked rebuild would restart
    # the Mac on the phone's clock.
    assert "client.post(" not in detail.replace(
        "client.post(action: PhoneActions.rebuildApp)", "")
    assert detail.count("client.post(action: PhoneActions.rebuildApp)") == 1
    assert "private var busy: Bool { settlingHere || hideAccepted }" in detail
    card = _text("CardDetailView.swift")
    seam = _block(card, "    private func send(_ action: String", 1400)
    # Save (the guarded revision) stays synchronous; the rest queue.
    assert 'if fields["expected_revision"] != nil {' in seam
    assert "client.post(" in seam and "client.enqueue(" in seam
    assert card.count("client.post(") == 1
    assert "private var sending: Bool { settlingHere }" in card
    # The plan gate arms off the receipt, once per press.
    assert "private func noteArrived(_ queued: QueueNote?)" in card
    assert "armedForNote" in card


def test_needs_you_queues_each_dismiss_under_its_own_subject():
    view = _text("NeedsYouView.swift")
    clear = _block(view, "    private func dismissAll(_ rows: [PhoneInboxItem])", 700)
    assert "client.enqueue(" in clear
    assert "refreshAfterWrite" not in clear
    assert "scope: scope(row)" in clear
    assert "client.post(" not in view


def test_the_pipeline_screen_queues_its_presses_and_reads_the_queue_note():
    """Four presses queue. START PROJECT stays on the synchronous `post`:
    its 200's `detail` is the Mac's report of what started and what was
    left alone, which the screen draws, and a queue cannot carry a reply
    body back (the plan's rule for Save, create and Clear done)."""
    view = _text("PipelineView.swift")
    assert view.count("client.enqueue(") == 4
    assert view.count("client.post(") == 1
    start = _block(view, "    private func startProjectButton(root: String)", 900)
    assert "client.post(" in start
    assert "action: PhoneActions.boardStartProject" in start
    assert "client.notePipeline(root, result.detail)" in start
    assert "client.writeInFlight(" not in view
    assert "client.queueNote(for: key)" in view
    assert "client.beginSettlingPreference(key, want: want)" in view
    # The Mac's words outrank the phone's "accepted, but…" backstop.
    assert 'client.queueNote(for: key)?.text ?? client.pipelineNotices[key]' in view
    client = _text("Client.swift")
    backstop = _block(client, "    func beginSettlingPreference(", 1400)
    assert "if self.queueNote(for: key) != nil {" in backstop
    assert backstop.index("self.endSettlingPreference(key)") \
        < backstop.index("Self.preferenceUnsettledNotice")


def test_the_outbox_treats_the_queue_dedupe_as_transport_shaped():
    outbox = _text("Outbox.swift")
    start = outbox.index("static let transportSentences")
    block = outbox[start:outbox.index("]", start)]
    assert "PhoneClient.queuedTwiceRefusal" in block


def test_the_profile_screen_draws_the_queue_oldest_first():
    profile = _text("ProfileView.swift")
    assert profile.count("QUEUE (") == 1
    assert "PENDING (" in profile
    assert "receipts.queueRows" in profile
    assert "receipt.statusWord" in profile
    # RETRY on a press that has not gone yet, or is going out right now, is
    # a no-op, never a second copy; DISCARD waits for a transmit to end.
    assert "guard receipt.state != .queued,\n" \
           "                                  receipt.state != .sending else { return }" in profile
    assert "|| receipt.state == .sending)" in profile
    receipts = _text("Receipts.swift")
    ledger_retry = _block(receipts, "    func retry(_ id: String)", 700)
    assert "receipts[index].state != .sending" in ledger_retry


def test_the_contract_names_the_rule():
    doc = (ROOT / "docs" / "phone-contract.md").read_text()
    assert doc.count("A press is queued, not awaited") == 1
    assert "queuedTwiceRefusal" in doc


def test_a_save_the_store_had_nothing_to_change_lands_at_the_number_the_mac_names(queue_bin):
    """Seen 21 Sep 2026, three times on one card: a save whose field the
    store already held was accepted (200) with the card's revision
    unmoved, so `.cardRevision(atLeast: expected + 1)` never showed and
    the press went STUCK — and RETRY sent the same words to the same
    result. `accept(_:revision:)` lowers the expected number to the one
    the reply carried, so the next frame closes the record; a reply that
    names the stepped number (a real change) leaves the effect alone."""
    held = json.dumps({"board": {"available": True, "cards": [
        {"id": "c1", "column_name": "done", "revision": 9}]}})
    rows = _ledger(queue_bin, [
        {"op": "open", "as": "noop", "scope": "c1", "action": "board_update",
         "atLeast": "10"},
        {"op": "markSending", "ref": "noop"},
        {"op": "accept", "ref": "noop", "revision": "9"},
        {"op": "settled"},
    ], snapshot=held)
    assert rows["noop"]["state"] == "done", rows["noop"]
    # A real change: the reply names 10, the frame still says 9 — not yet.
    rows = _ledger(queue_bin, [
        {"op": "open", "as": "real", "scope": "c1", "action": "board_update",
         "atLeast": "10"},
        {"op": "markSending", "ref": "real"},
        {"op": "accept", "ref": "real", "revision": "10"},
        {"op": "settled"},
    ], snapshot=held)
    assert rows["real"]["state"] == "accepted", rows["real"]
    # No revision on the reply (an older Mac): the rule is untouched.
    rows = _ledger(queue_bin, [
        {"op": "open", "as": "old", "scope": "c1", "action": "board_update",
         "atLeast": "10"},
        {"op": "markSending", "ref": "old"},
        {"op": "accept", "ref": "old"},
        {"op": "settled"},
    ], snapshot=held)
    assert rows["old"]["state"] == "accepted", rows["old"]


def test_an_acknowledgement_of_an_item_already_gone_closes_done_not_refused(queue_bin):
    """`inbox_ack`'s "not waiting on you" / "changed or already gone" say
    the press's goal is already met — the item was cleared by Dismiss
    all, by the Mac, by the row moving on. Drawn REFUSED with RETRY they
    filled the queue with red rows nobody could act on (21 Sep 2026).
    Every other refusal keeps its state and its words."""
    rows = _ledger(queue_bin, [
        {"op": "enqueue", "as": "gone", "scope": "s1", "action": "inbox_ack"},
        {"op": "markSending", "ref": "gone"},
        {"op": "refuse", "ref": "gone", "detail": "that item is not waiting on you"},
        {"op": "enqueue", "as": "moved", "scope": "s2", "action": "inbox_ack"},
        {"op": "markSending", "ref": "moved"},
        {"op": "refuse", "ref": "moved",
         "detail": "that item has changed or is already gone"},
        {"op": "enqueue", "as": "other", "scope": "s3", "action": "inbox_ack"},
        {"op": "markSending", "ref": "other"},
        {"op": "refuse", "ref": "other", "detail": "that item cannot be acknowledged"},
    ])
    assert rows["gone"]["state"] == "done" and rows["gone"]["lastError"] == ""
    assert rows["moved"]["state"] == "done" and rows["moved"]["lastError"] == ""
    assert rows["other"]["state"] == "stuck"
    assert rows["other"]["lastError"] == "that item cannot be acknowledged"


def test_the_goal_met_sentences_are_the_macs_own():
    """Byte for byte `inbox_ack.py`'s two refusals, and both accept sites
    hand the ledger the reply's revision."""
    import sys
    sys.path.insert(0, str(ROOT / "host"))
    from dark_army_daemon import inbox_ack
    receipts = _text("Receipts.swift")
    block = _block(receipts, "static let goalMetRefusals", 400)
    assert f'"{inbox_ack.INBOX_ACK_MISSING_REFUSAL}"' in block
    assert f'"{inbox_ack.INBOX_ACK_STALE_REFUSAL}"' in block
    client = _text("Client.swift")
    assert client.count("receipts.accept(mark, revision: parsed.revision)") == 2
    assert "receipts.accept(mark)" not in client
