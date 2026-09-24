"""Pins for the phone's answer hold: a sent answer stays dimmed until the Mac
says the question is gone.

`ios/` has no test target by explicit decision, so this is a Python lint over
the Swift sources — `test_phone_action_feedback.py`'s pattern, and this file is
its answer-path sibling. The reason it exists is on the daemon side and is not
changed here: `BobDaemon.answer_question` "deliberately touches nothing on
success: no card is dismissed and `_questions` is left alone, because the
question clears itself the moment the transcript carries its `tool_result`".
So the phone's HTTP 200 proves nothing about the row, the refresh inside
`post` polls a snapshot that still lists the old question, and once
`writesInFlight` clears the answer controls came back bright for one poll
cadence (4s home / 8s relay) — an already-answered question inviting a second
tap. The hold closes that gap: `beginSettling` branches the answer verbs into
`settlingAnswers`, keyed session id → the question id answered, and the entry
ends on the snapshot (`pruneSettlingAnswers`: the row out of `waiting`, or on a
different question) with `settlingTimeout` as the backstop.

Stop / Close / Delete keep their own set and their own prune; the answer verbs
are deliberately a *separate* set, because an answered session stays live and
`pruneSettling`'s "no longer listed" test would never fire for it.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
CLIENT = PHONE / "Client.swift"
BOX = PHONE / "AnswerBox.swift"

FALSE_SENTENCE = "Answers and replies do not"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _body(text: str, header: str) -> str:
    """The source from `header` to the line that closes it at the
    declaration's own indent. `header` carries that indent, so every caller
    below names a member one level inside its type."""
    start = text.find(header)
    assert start >= 0, f"no {header!r}"
    indent = header[:len(header) - len(header.lstrip(" "))]
    assert indent, f"{header!r} must carry its own indent"
    end = text.find(f"\n{indent}}}", start)
    assert end > start, f"{header!r} never closes"
    return text[start:end]


# --- The set and the store ----------------------------------------------------


def test_the_answer_verbs_have_their_own_hold_set():
    body = _body(_read(CLIENT), "    private static let answerHoldActions")
    for verb in ("answerQuestion", "answerQuestions", "reply"):
        assert f"PhoneActions.{verb}" in body, f"{verb} is not held"
    assert "stopSession" not in body, (
        "Stop belongs to settlingActions, which prunes against the list")


def test_stop_and_close_semantics_are_untouched():
    """The answer verbs go in a separate set; the old one is byte-for-byte
    the same membership it had before the hold existed."""
    client = _read(CLIENT)
    start = client.find("    private static let settlingActions")
    assert start >= 0
    body = client[start:client.find("]", start)]
    for verb in ("answerQuestion", "answerQuestions", "reply"):
        assert verb not in body, f"{verb} leaked into settlingActions"
    for verb in ("stopSession", "closeTerminal", "deleteAgent"):
        assert f"PhoneActions.{verb}" in body


def test_the_hold_is_a_published_session_to_question_map():
    """Keyed by session, valued by the question id answered: `""` is a legal
    value (a reply on a row with no question), so the type is a dictionary
    and not a set."""
    client = _read(CLIENT)
    assert client.count(
        "@Published private(set) var settlingAnswers: [String: String]") == 1
    assert "private var settlingAnswerTimeouts: [String: Task<Void, Never>]" \
        in client


# --- Taking the hold ----------------------------------------------------------


def test_begin_settling_branches_to_the_answer_hold_first():
    """Both legs of `post` (home and relay) call `beginSettling` on a 200
    *before* `refreshAfterWrite()`; branching inside it is what gives the
    answer hold that ordering on both legs with no new call site."""
    body = _body(_read(CLIENT), "    private func beginSettling(")
    assert "answerHoldActions" in body
    assert "beginSettlingAnswer(" in body
    assert body.find("answerHoldActions") < body.find("settlingActions"), (
        "the answer branch must come before the Stop/Close guard")


def test_the_answer_hold_reuses_the_settling_backstop():
    """One figure, shared with the Stop hold and pinned to the desktop
    panel's `Triage.stoppingTimeout` — a second constant would drift."""
    client = _read(CLIENT)
    body = _body(client, "    private func beginSettlingAnswer(")
    assert "Self.settlingTimeout" in body
    assert "Task.sleep" in body
    assert client.count("settlingTimeout: TimeInterval =") == 1


def test_the_answer_hold_takes_no_hold_on_a_row_that_is_not_waiting():
    """A reply on a running row has no question state to revert to."""
    body = _body(_read(CLIENT), "    private func beginSettlingAnswer(")
    assert "snapshot.agents.waiting" in body
    assert "question.id" in body


# --- Ending the hold ----------------------------------------------------------


def test_the_answer_hold_ends_on_waiting_or_a_new_question():
    """An answered session stays live, so `running` and `sleeping` are not
    evidence of anything here — only leaving `waiting` or standing on a
    different question is proof the answer landed."""
    body = _body(_read(CLIENT), "    private func pruneSettlingAnswers()")
    assert ".waiting" in body
    assert "question.id" in body
    assert ".running" not in body, "the prune must not read running"
    assert ".sleeping" not in body, "the prune must not read sleeping"
    assert "settlingAnswerTimeouts" in body, "a pruned hold cancels its backstop"


def test_the_answer_hold_is_pruned_by_both_snapshot_writers():
    """Cleared by the snapshot, never by the HTTP 200. Both writers (home and
    relay) prune, each beside the session prune it mirrors — the pairing
    arithmetic of `test_the_card_hold_is_cleared_by_the_board_and_never_by_the_200`."""
    client = _read(CLIENT)
    lines = client.splitlines()
    calls = [line for line in lines
             if "pruneSettlingAnswers()" in line and "func " not in line]
    assert len(calls) >= 2, f"pruneSettlingAnswers called {len(calls)} time(s)"
    sites = [m.start() for m in re.finditer(r"pruneSettlingAnswers\(\)", client)
             if not client[max(0, m.start() - 5):m.start()].endswith("func ")]
    anchors = [m.start() for m in re.finditer(r"pruneSettling\(\)", client)]
    paired = [c for c in sites if any(0 < c - a < 120 for a in anchors)]
    assert len(paired) >= 2, (
        f"only {len(paired)} of the answer prunes sit beside a session prune")


# --- The box ------------------------------------------------------------------


def test_the_answer_box_reads_the_queue_mark_and_not_the_hold():
    """A press is queued, not awaited (20 Sep 2026): the box's `busy` is the
    queue's mark for this agent and is read for the label swap alone. The
    answer hold still runs inside `post` after the 200 — the agent screen
    reads it for the row-leaving dim — but it is no longer a lock here: a
    second identical press is turned away by the queue's own dedupe
    (`queuedTwiceRefusal`) and said so in the note."""
    box = _read(BOX)
    body = _body(box, "    private var busy: Bool")
    assert "client.queueMark(for: agent.sessionId) != nil" in body
    assert "settlingAnswers" not in box
    assert "writeInFlight(" not in box


def test_the_hold_rides_the_receipt_so_the_mark_stays_until_the_question_goes():
    """The audit's regression (20 Sep 2026): with the answer verbs mapped to
    `.none`, `accept` closed them `.done` on the 200 — the mark vanished and
    the buttons re-enabled while the question was still listed for a poll
    cadence, and a second tap was not a duplicate. The hold is now the
    receipt's own effect: `effect(for:)` maps an answer to `.questionGone`
    (the client upgrades a reply on a waiting row the same way), so the 200
    leaves the record `.accepted` and the mark `SENT` until the snapshot
    shows the question gone; and `duplicate` counts an `.accepted` copy of
    these verbs (`heldWhileLanding`), so a second tap is turned away."""
    receipts = _read(PHONE / "Receipts.swift")
    table = _body(receipts, "    static func effect(for action: String")
    answers = table[table.index("case PhoneActions.answerQuestion, PhoneActions.answerQuestions:"):
                    table.index("case PhoneActions.permissionVerdict:")]
    assert "return .questionGone(sessionId: session, questionId: question)" in answers
    assert 'fields["question_id"]' in answers
    held = receipts[receipts.index("static let heldWhileLanding"):]
    held = held[:held.index("]")]
    for verb in ("answerQuestion", "answerQuestions", "reply"):
        assert f"PhoneActions.{verb}" in held, f"{verb} is not held while landing"
    dup = _body(receipts, "    static func duplicate(action: String")
    assert "heldWhileLanding.contains(action)" in dup
    assert "$0.state == .accepted" in dup
    # `.questionGone` is unchanged: the row out of `waiting`, or on a
    # different question — `pruneSettlingAnswers`' evidence exactly.
    landed = _body(receipts, "    static func landed(_ effect: ReceiptEffect")
    branch = landed[landed.index("case .questionGone"):landed.index("case .cardGone")]
    assert "snapshot.agents.waiting" in branch
    assert "current[sessionId] != questionId" in branch


def test_the_false_sentence_is_gone():
    """The old comment claimed the refresh inside `post` removes the
    question. It does not — `answer_question` touches nothing on success."""
    assert FALSE_SENTENCE not in _read(CLIENT)


def test_nothing_in_the_box_dims_on_a_press_in_play():
    """The four `.disabled(busy` sites — the numbered buttons, the whole
    multi-question group, the reply buttons and the free-text SEND — are
    gone (20 Sep 2026): the control is the person's again the moment the
    press is written down, and the pressed one wears QUEUED / SENDING… /
    SENT instead of going grey. The two disables left are about the picks
    (`!complete`) and an empty reply, never about a write."""
    box = _read(BOX)
    assert box.count(".disabled(busy") == 0
    assert ".disabled(!complete)" in box
    start = box.find("    private var multiQuestionGroups")
    assert start >= 0, "no multiQuestionGroups"
    end = box.find("\n    private var", start + 1)
    assert end > start
    assert ".disabled(busy)" not in box[start:end]
