"""Pins for the phone's in-flight button feedback.

`ios/` has no test target by explicit decision, so the Swift half here is a
Python lint over the source — `test_phone_usage_tab.py`'s pattern. It pins the
published scope-keyed in-flight record, the worded dedupe refusal, the
SENDING… label swap, the one-`client.post`-per-view-file scope discipline,
the Arm timeout, the settling backstop's parity with the desktop panel's
`stoppingTimeout`, and the choices-wipe surface. The daemon half answers the
investigate-and-pin item from
`plans/2026-09-01-phone-in-flight-button-feedback.md`: whether a poll can
catch a session out of `waiting` — or see the question's id move — while an
AskUserQuestion dialog stands.

Two findings from that investigation, recorded here so the next reader of a
"my picks vanished" report starts in the right place:

* **List vs detail are two boxes.** The "Needs you" list row's `AnswerBox`
  and the agent detail screen's `AnswerBox` are two separate SwiftUI view
  identities with two separate `@State choices` — picks made on the list row
  never appear on the detail screen, by construction. A user who picks on
  one surface and sends from the other experiences that as picks vanishing.
The card screen's half of the same feedback — its settling hold, its
post-write refresh and the SENDING… swap on all seven of its controls — is
pinned next door in `test_phone_card_write_feedback.py`.

* **An idle_prompt used to pop the standing question.** Claude Code fires an
  `idle_prompt` Notification ~60s after a session goes quiet, and a session
  parked on an AskUserQuestion dialog *is* quiet. That Notification arrives
  as an `add` event, which `BobDaemon._track_pending_question` treated as a
  clearing event — the pending question was dropped while the dialog still
  stood in the terminal, so the phone's row kept its `waiting` category (the
  card held it there) but lost its `question`, and the AnswerBox changed
  shape under the user's picks. Fixed 1 Sep 2026: an `add` whose hook is
  "Notification" names no tool and may not clear the slot.
  `test_an_idle_prompt_leaves_the_standing_question_alone` pins the fix.

The app-wide sweep at the bottom is the standing floor: every phone view that
draws an acting control must say a press was received. Recorded here because
it is the fact that makes the sweep's negative half meaningful —
`RecentlyView.swift`, `UsageView.swift`, `FleetView.swift` and
`ProcessTable.swift` draw **no** acting control at all, so a `Button(`
appearing in one of them is a new gap rather than an exception, and the sweep
fails rather than letting it ship silent.
"""

import re
import time

import pytest

from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.protocol import hook_payload_to_daemon_message

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
CLIENT = PHONE / "Client.swift"
ANSWER_BOX = PHONE / "AnswerBox.swift"
DETAIL = PHONE / "AgentDetailView.swift"
ARM = PHONE / "Arm.swift"
OUTBOX = PHONE / "Outbox.swift"
NEEDS_YOU = PHONE / "NeedsYouView.swift"
CATCH_UP = PHONE / "CatchUpView.swift"
BOARD_VIEW = PHONE / "BoardView.swift"
PROFILE = PHONE / "ProfileView.swift"
TRIAGE = ROOT / "panel" / "Sources" / "BobPanel" / "Triage.swift"

REFUSAL = "Still sending your last tap — give it a moment."


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- The seam: a published, scope-keyed record --------------------------------


def test_the_in_flight_record_is_published_and_scope_keyed():
    """Reverting to a private set silently kills every view-side disable
    while everything still compiles — the exact shape of the reported bug."""
    client = _read(CLIENT)
    assert client.count("@Published private(set) var writesInFlight") == 1
    # Keyed by scope with the three fallback rungs, so board and composer
    # call sites keep their per-card / per-verb dedupe without passing one.
    assert 'fields["session_id"]' in client
    assert 'fields["card_id"]' in client
    assert 'return "verb:\\(action)"' in client
    # And the views read it through the one seam.
    assert "func writeInFlight(for sessionId: String) -> String?" in client


def test_the_dedupe_refusal_says_so_in_words_exactly_once():
    """Losing this sentence returns the swallowed-tap-says-nothing bug: both
    views' `send` helpers only set the orange note when `detail` is
    non-empty. The sentence lives once, as `stillSendingRefusal`, and the
    guard answers with the constant."""
    client = _read(CLIENT)
    assert client.count(REFUSAL) == 1, (
        "the refusal sentence must be spelled exactly once, at the constant")
    assert client.count("static let stillSendingRefusal") == 1
    # The definition carries the literal.
    definition = client.find("static let stillSendingRefusal")
    assert 0 <= client.find(REFUSAL) - definition < 200
    # And the guard's answer is the constant, not decoration somewhere else.
    guard = client.find("if writesInFlight[scopeKey] != nil")
    assert guard >= 0, "the dedupe guard is gone"
    answer = client.find("Self.stillSendingRefusal", guard)
    assert answer >= 0 and answer - guard < 200, (
        "the dedupe guard no longer answers with the refusal constant")


def test_the_outbox_treats_the_dedupe_refusal_as_transport_shaped():
    """The outbox's 4s onLive sweep can collide with a live composer CREATE —
    both key to `verb:board_create` via the scope fallback, and a relay
    CREATE holds the verb for many seconds behind the Face ID sheet. That
    collision is transport-shaped: it must be retried on a later poll, never
    painted on the queued entry as a refusal the Mac never uttered. One
    constant referenced from both sides, so a rewording cannot split the
    guard from the classification."""
    outbox = _read(OUTBOX)
    assert REFUSAL not in outbox, (
        "Outbox.swift respells the sentence — the next rewording splits it "
        "from the guard; it must reference PhoneClient.stillSendingRefusal")
    start = outbox.find("static let transportSentences")
    assert start >= 0, "transportSentences is gone"
    block = outbox[start:outbox.index("]", start)]
    assert "PhoneClient.stillSendingRefusal" in block, (
        "the dedupe refusal fell out of transportSentences — the sweep "
        "would paint it on a queued card as the Mac's own words")


def test_the_only_silent_action_failures_are_cancellation():
    """A `PhoneActionResult(ok: false, detail: "")` anywhere but a teardown
    catch is a new invisible failure path — the views write no note for an
    empty detail, deliberately, so only cancellation may return one."""
    lines = _read(CLIENT).splitlines()
    silent = [i for i, line in enumerate(lines)
              if 'PhoneActionResult(ok: false, detail: "")' in line]
    assert silent, "the cancellation branches went missing entirely"
    for i in silent:
        neighbourhood = "\n".join(lines[max(0, i - 2):i + 1])
        # The sealed home transport catches the cancellation itself and
        # hands it back as `Trouble.cutShort`; that branch is the same
        # teardown, one layer down.
        assert ("catch is CancellationError" in neighbourhood
                or "Trouble.cutShort" in neighbourhood), (
            f"silent failure outside a cancellation catch at "
            f"Client.swift:{i + 1}: {lines[i].strip()}")


# --- The views: one post seam each, scoped, with the label swap ---------------


@pytest.mark.parametrize("path", [ANSWER_BOX, DETAIL], ids=["box", "detail"])
def test_each_view_posts_through_one_scoped_helper(path):
    """A new button wired straight to `client.enqueue` (or the synchronous
    `client.post`) would bypass the per-agent scope and the note plumbing.
    Since 20 Sep 2026 the seam queues the press (`enqueue`); `post` is not
    called from these two views at all."""
    text = _read(path)
    posts = [line for line in text.splitlines()
             if "client.post" in line or "client.enqueue" in line]
    assert len(posts) == 1, f"{path.name} posts from {len(posts)} places"
    assert "client.post(" not in text, f"{path.name} still awaits a press"
    assert "scope: agent.sessionId" in text, (
        f"{path.name}'s send helper lost the session scope — a permission "
        "verdict's fields carry only request_id, so without the scope that "
        "write keys on the bare verb and locks nothing")


@pytest.mark.parametrize("path", [ANSWER_BOX, DETAIL], ids=["box", "detail"])
def test_each_view_relabels_from_the_queue(path):
    """A press is queued, not awaited (20 Sep 2026): the pressed control
    wears the queue's mark for this agent — QUEUED, SENDING…, SENT — read
    through `queueMark(for:)`, and nothing dims on a press in play. The
    in-flight record (`writeInFlight`) is `post`'s own lock and is read by
    no view any more."""
    text = _read(path)
    assert "SENDING…" in text, f"{path.name} lost the label swap"
    assert "client.queueMark(for: agent.sessionId)" in text, (
        f"{path.name} does not wear the queue's mark")
    assert "writeInFlight(" not in text, (
        f"{path.name} still reads the transmit lock")


def test_the_detail_screen_dims_only_while_the_row_is_leaving():
    text = _read(DETAIL)
    assert "client.settling.contains(agent.sessionId)" in text, (
        "the detail no longer holds through settling")
    assert "private var busy: Bool { settlingHere || hideAccepted }" in text


def test_the_detail_screen_holds_stop_and_close_through_settling():
    """After a confirmed Stop or Close is accepted, the button stays dimmed
    and reads SENDING… until the agent leaves the list — a Mac that takes a
    few seconds to comply must never read as a dropped press."""
    text = _read(DETAIL)
    assert "settlingHere" in text
    for label in ("stopLabel", "closeLabel"):
        body = text[text.find(f"private var {label}"):]
        body = body[:body.find("}\n")]
        assert "settlingHere" in body, f"{label} snaps back during settling"


# --- Settling: the panel's stopping rule at poll cadence ----------------------


def _figure(text: str, name: str) -> str:
    match = re.search(rf"{name}: TimeInterval = (\d+)", text)
    assert match, f"no {name} figure"
    return match.group(1)


def test_the_settling_backstop_matches_the_panels_stopping_timeout():
    """Same figure, same reason — a row frozen forever is worse than one
    that admits it does not know. Drift here means the two surfaces answer
    the same accepted-but-slow Stop differently."""
    client = _read(CLIENT)
    assert "settlingTimeout: TimeInterval = 12" in client
    assert (_figure(client, "settlingTimeout")
            == _figure(_read(TRIAGE), "stoppingTimeout"))


def test_settling_is_pruned_by_both_snapshot_writers():
    """Cleared by the snapshot, never by the HTTP 200. Dropping either call
    leaves that leg's users dimmed for the full backstop every time — the
    relay leg is the one somebody forgets."""
    lines = _read(CLIENT).splitlines()
    calls = [line for line in lines
             if "pruneSettling()" in line and "func " not in line]
    assert len(calls) >= 2, f"pruneSettling called {len(calls)} time(s)"


# --- Arm ----------------------------------------------------------------------


def test_the_arm_waits_long_enough_to_read_the_armed_copy():
    """8s was shorter than reading "Really close the terminal?" plus its
    warning line; a revert restores the mid-read lapse. The expiry itself
    must stay — an armed destructive verb on a phone in hand must not sit
    live indefinitely."""
    arm = _read(ARM)
    assert arm.count("timeoutSeconds: TimeInterval = 30") == 1
    assert "timeoutTask" in arm, "the expiry was removed, not retimed"


# --- The choices-wipe surface (item d) ----------------------------------------


def test_the_only_choice_wipes_are_the_dialog_change_and_the_send():
    """Exactly two wipe paths: the `.onChange` sweep when the dialog itself
    changes (`drafts.reset`, which keeps the new id's slot) and the
    post-ANSWER-ALL clear (`drafts.clear`). The picks live in the shared
    `AnswerDrafts` store rather than view `@State`, so both surfaces show
    one set of picks; a third wipe is a new path nobody argued for."""
    box = _read(ANSWER_BOX)
    assert "choices = [:]" not in box, "picks must live in AnswerDrafts, not @State"
    assert box.count("drafts.reset(") == 1
    assert box.count("drafts.clear(") == 1
    assert ".onChange(of: agent.question.id)" in box, (
        "the wipe no longer watches the dialog's own id")


# --- Daemon side: can a poll catch the row out of `waiting`? ------------------

SID = "s-question"


def _tool_input():
    return {"questions": [{
        "question": "Which database?", "header": "Database",
        "multiSelect": False,
        "options": [{"label": "Postgres", "description": ""},
                    {"label": "SQLite", "description": ""}],
    }]}


def _ask_msg(tool_use_id="tu_1"):
    return hook_payload_to_daemon_message({
        "hook_event_name": "PreToolUse", "session_id": SID, "cwd": "/x/p",
        "tool_name": "AskUserQuestion", "tool_use_id": tool_use_id,
        "tool_input": _tool_input(),
    })


def _at_poll_cadence(d, sid=SID):
    """The category a phone poll would see. The phone polls every 4s (8s via
    relay), so the next snapshot it can take is past the 3s waiting
    hysteresis — simulated by rewinding the event stamp rather than
    sleeping."""
    d._session_states[sid]["last_event_monotonic"] = time.monotonic() - 5.0
    return d._reconciled_categories()[sid]


def _question_row(d):
    stub = {"session_id": SID, "project": "proj", "state": "waiting",
            "subagents": 0, "subagent_ids": [], "_category": "waiting"}
    return d._enrich_agent_stubs([stub])["waiting"][0]


async def _asked_daemon():
    d = BobDaemon()
    await d._handle_message({"event": "session_start", "session_id": SID,
                             "pid": 4242})
    await d._handle_message({"event": "dismiss", "hook": "UserPromptSubmit",
                             "session_id": SID})
    await d._handle_message(_ask_msg())
    return d


@pytest.mark.asyncio
async def test_waiting_holds_at_poll_cadence_while_the_question_pends():
    """Flap path (i) from the plan: if the row left `waiting` for one poll,
    the phone's List would drop the row's subtree and the `@State` picks
    with it. It does not — the events that arrive while a dialog stands
    (the named permission companion, its nameless Notification twin, and
    the statusline on every assistant message) all leave the bucket at
    `waiting` by the time the next poll can look."""
    d = await _asked_daemon()
    assert _at_poll_cadence(d) == "waiting"
    assert SID in d._pending_questions

    # The permission companion Claude Code sends milliseconds behind the
    # PreToolUse, then its nameless Notification twin a few seconds later.
    await d._handle_message({"event": "permission", "session_id": SID,
                             "tool_name": "AskUserQuestion"})
    assert _at_poll_cadence(d) == "waiting"
    assert SID in d._pending_questions

    await d._handle_message({"event": "permission", "session_id": SID})
    assert _at_poll_cadence(d) == "waiting"
    assert SID in d._pending_questions


@pytest.mark.asyncio
async def test_a_statusline_neither_moves_the_bucket_nor_restarts_the_clock():
    """The statusline fires on every assistant message; if it stamped
    `last_event_monotonic`, the waiting hysteresis would veto the bucket to
    `running` for 3s after each one and the phone's 4s poll could catch it.
    `_handle_statusline` deliberately touches no session state."""
    d = await _asked_daemon()
    assert _at_poll_cadence(d) == "waiting"
    stamp = d._session_states[SID]["last_event_monotonic"]
    await d._handle_message({"event": "statusline", "session_id": SID,
                             "data": {"session_id": SID}})
    assert d._session_states[SID]["last_event_monotonic"] == stamp
    # No cadence rewind needed: nothing was stamped, so the very next
    # snapshot still reads `waiting`.
    assert d._reconciled_categories()[SID] == "waiting"
    assert SID in d._pending_questions


@pytest.mark.asyncio
async def test_the_question_id_is_stable_across_those_snapshots():
    """Flap path (ii): if `agent.question.id` moved between polls, the
    phone's `.onChange` would wipe the picks. The id is the tool_use id and
    nothing that arrives while the dialog stands rewrites it."""
    d = await _asked_daemon()
    assert _question_row(d)["question"]["id"] == "tu_1"
    await d._handle_message({"event": "permission", "session_id": SID,
                             "tool_name": "AskUserQuestion"})
    assert _question_row(d)["question"]["id"] == "tu_1"
    await d._handle_message({"event": "statusline", "session_id": SID,
                             "data": {"session_id": SID}})
    assert _question_row(d)["question"]["id"] == "tu_1"


@pytest.mark.asyncio
async def test_another_pre_tool_use_is_the_sanctioned_clear():
    """A PreToolUse for a different tool means the dialog was answered and
    the model moved on — that clear is correct, and it is the boundary of
    the stability pinned above."""
    d = await _asked_daemon()
    await d._handle_message(hook_payload_to_daemon_message({
        "hook_event_name": "PreToolUse", "session_id": SID, "cwd": "/x/p",
        "tool_name": "Bash", "tool_input": {"command": "ls"},
    }))
    assert SID not in d._pending_questions


@pytest.mark.asyncio
async def test_an_idle_prompt_leaves_the_standing_question_alone():
    """The finding, fixed (1 Sep 2026): an idle_prompt Notification ~60s into
    a standing dialog arrives as an `add` event, and it used to drop the
    pending question while the dialog was still on the terminal — the phone's
    AnswerBox changed shape under the user's picks. The predecessor of this
    test pinned that wipe and said its failure would be the fix landing;
    `_track_pending_question` now ignores an `add` whose hook is
    "Notification" (evidence about no tool), so the question survives and the
    row keeps both its category and its dialog."""
    d = await _asked_daemon()
    assert _at_poll_cadence(d) == "waiting"
    await d._handle_message(hook_payload_to_daemon_message({
        "hook_event_name": "Notification", "session_id": SID, "cwd": "/x/p",
        "notification_type": "idle_prompt", "message": "Waiting for input",
    }))
    assert SID in d._pending_questions          # the fix: no wipe
    assert _at_poll_cadence(d) == "waiting"     # the card still holds the row


def test_codex_hide_uses_existing_scope_and_waits_for_snapshot_removal():
    detail, client = _read(DETAIL), _read(CLIENT)
    assert 'if agent.canHide {' in detail
    assert 'Hide until this thread changes' in detail
    assert 'send(PhoneActions.hideSession, ["session_id": agent.sessionId])' in detail
    assert 'PhoneActions.deleteAgent, PhoneActions.hideSession,' in client
    assert 'hideAccepted = true' in detail
    assert 'if hideAccepted && !rowPresent { dismiss() }' in detail
    assert '.onChange(of: client.snapshot.generatedAt)' in detail
    # `outAction` is the queued verb (for the label swap), never a dim.
    assert 'private var busy: Bool { settlingHere || hideAccepted }' in detail
    assert 'client.queuedAction(for: agent.sessionId)' in detail
    assert 'if !result.detail.isEmpty { note = result.detail }' in detail


def test_phone_codex_explanation_replaces_generic_caption_in_shared_box():
    source = _read(ANSWER_BOX)
    assert 'Text(agent.interactionNote)' in source
    assert 'if agent.interactionNote.isEmpty {' in source
    assert 'if agent.canType {' in source
    assert 'stopped && agent.channel' in source


def test_codex_hide_acceptance_recovers_if_refresh_outlasted_settling_timeout():
    detail = _read(DETAIL)
    # The recovery is in the same helper called immediately after acceptance,
    # not only in an observer whose transition could precede the response.
    helper = detail.split('private func leaveHiddenDetail()', 1)[1].split('var body:', 1)[0]
    assert 'hideAccepted && rowPresent && !settlingHere' in helper
    assert 'hideAccepted = false' in helper
    assert 'note = "The Mac still lists this thread;' in helper
    acceptance = detail.split('if await send(PhoneActions.hideSession', 1)[1].split('.buttonStyle', 1)[0]
    assert acceptance.index('hideAccepted = true') < acceptance.index('leaveHiddenDetail()')


# --- The four gaps this plan closed ------------------------------------------


def test_needs_you_routes_and_its_one_verb_says_it_was_pressed():
    """Mark done / Send back left the list (20 Sep 2026): the tab routes
    and does not answer, and the card screen is where those two arm. The
    one verb left — Dismiss all — dims and says it is hiding."""
    text = _read(NEEDS_YOU)
    assert "writeInFlight(for: card.id)" not in text
    assert "PhoneActions.boardUpdate" not in text
    assert "PhoneActions.boardReset" not in text
    assert text.count("HIDING…") == 1
    assert ".disabled(dismissingAll)" in text


def test_needs_you_never_takes_a_card_settling_hold():
    """The attractive wrong move, pinned from both ends.
    `pruneSettlingCards` ends its hold only when the card leaves the board
    outright, and the backstop then writes `deleteUnsettledNotice` — a card
    marked done stays listed, so enrolling these verbs would paint a false
    "the delete may not have landed" line on a card nobody deleted."""
    assert "beginSettlingCard" not in _read(NEEDS_YOU), (
        "NeedsYouView reads settlingCards; it must never write it")
    client = _read(CLIENT)
    calls = [i for i, line in enumerate(client.splitlines())
             if "beginSettlingCard(" in line and "func " not in line]
    assert len(calls) == 2, f"beginSettlingCard called {len(calls)} time(s)"
    lines = client.splitlines()
    for i in calls:
        above = "\n".join(lines[max(0, i - 6):i])
        assert "action == PhoneActions.boardDelete" in above, (
            f"a settling hold outside the Delete gate at Client.swift:{i + 1}")


def test_the_needs_you_note_is_a_refusal_in_the_macs_words():
    """The only note the list draws now is a refused dismiss, in the Mac's
    own words and in refusal colour; a landed dismiss removes the row and
    says nothing. Nothing rides `PhoneBoardCard.notice`."""
    text = _read(NEEDS_YOU)
    assert "notes[item.id] = result.ok ? \"\" : result.detail" in text
    assert ".foregroundStyle(.orange)" in text
    assert "PhoneClient.cardMarkedDoneLine" not in text
    assert "PhoneClient.cardSentBackLine" not in text
    assert "notice:" not in text


def test_the_phone_authored_lines_are_spelled_once():
    """`stillSendingRefusal`'s own discipline: a sentence spelled twice is a
    sentence the next rewording splits."""
    for sentence in ("Marked done.", "Sent back.", "Caught up to here."):
        hits = {path.name: path.read_text().count(sentence)
                for path in sorted(PHONE.glob("*.swift"))
                if sentence in path.read_text()}
        assert hits == {"Client.swift": 1}, (
            f"{sentence!r} is spelled in {hits}, not once at its constant")


def test_mark_caught_up_confirms_and_then_goes_inert():
    """It writes UserDefaults and nothing else, so the confirming line plus
    the button going inert is the only feedback available at all."""
    text = _read(CATCH_UP)
    assert text.count("PhoneClient.catchUpMarkedLine") == 1
    start = text.index("Button(\"Mark caught up\")")
    disabled = text[start:text.index("Text(\"Reading never answers", start)]
    assert ".disabled(" in disabled, "the button lost its gate entirely"
    assert "checkpoint" in disabled and "<=" in disabled, (
        "the button can still be pressed on a cursor already covered")


def test_the_outbox_sweep_is_observable():
    """`OutboxStore.sync` returns at its guard when a sweep is already
    running, so a view-local flag flickers off in the same frame."""
    assert _read(OUTBOX).count("@Published private(set) var syncing") == 1
    board = _read(BOARD_VIEW)
    assert board.count("sending: outbox.syncing") == 1
    row = board[board.index("private struct OutboxRow"):]
    assert "SENDING…" in row
    assert row.count(".disabled(sending)") == 2, (
        "RETRY and its conflicting sibling REMOVE must both dim")


def test_the_receipt_retry_dims_on_the_id_it_pressed():
    """`retryReceipt` mints a fresh id, so the row identity changes on
    completion: the flag is keyed on the id pressed and cleared
    unconditionally, never compared against the ledger afterwards."""
    text = _read(PROFILE)
    assert "retryingReceipt" in text
    assert "defer { retryingReceipt = \"\" }" in text, (
        "the flag must clear unconditionally, not on a ledger lookup")
    assert text.count(".disabled(!retryingReceipt.isEmpty") == 2, (
        "every QUEUE RETRY and its DISCARD sibling dim while one is out")
    # DISCARD additionally waits for a transmit in flight to end: forgetting
    # a record mid-transmit loses the answer to a press that may still land.
    assert ".disabled(!retryingReceipt.isEmpty\n" \
           "                                  || receipt.state == .sending)" in text


# --- The standing sweep -------------------------------------------------------

#: Every phone view that draws a control which writes something. A file
#: added here without a sending word and a disable fails below.
ACTING_VIEWS = [
    "NeedsYouView.swift", "AgentDetailView.swift", "AnswerBox.swift",
    "CardDetailView.swift", "PipelineView.swift", "ComposerView.swift",
    "BoardView.swift", "ProfileView.swift", "CatchUpView.swift",
]
#: Files that draw no acting control at all. A `Button(` appearing in one is
#: a new gap, not an exception.
QUIET_VIEWS = [
    "RecentlyView.swift", "UsageView.swift", "FleetView.swift",
    "ProcessTable.swift",
]
#: The words that say a press was received. Three of them name a request
#: still out; the fourth is *Mark caught up*'s, which writes UserDefaults and
#: nothing else — there is no request to be out, so the honest word is that it
#: landed. A view whose control writes but says none of these is the gap.
#: Since 20 Sep 2026 a press is queued, not awaited, and the pressed control
#: wears the queue's own mark — `QUEUED`, `SENDING…`, `SENT` — read through
#: `client.queueMark(`; a view that draws that mark says it was pressed.
SENDING_WORDS = ("SENDING…", "SAVING…", "HIDING…",
                 "PhoneClient.catchUpMarkedLine", "client.queueMark(")


def _says_it_was_pressed(text: str) -> bool:
    return any(word in text for word in SENDING_WORDS) and ".disabled(" in text


@pytest.mark.parametrize("name", ACTING_VIEWS)
def test_every_acting_button_says_it_was_pressed(name):
    """The standing floor: the next button somebody adds cannot quietly go
    back to saying nothing."""
    assert _says_it_was_pressed(_read(PHONE / name)), (
        f"{name} draws an acting control with no sending word or no dim")


def test_the_sweep_is_not_vacuous():
    """Anti-rot: a doctored copy with the words stripped must fail the same
    predicate, and the list must not be allowed to empty itself."""
    assert ACTING_VIEWS and QUIET_VIEWS
    doctored = _read(PHONE / ACTING_VIEWS[0])
    for word in SENDING_WORDS:
        doctored = doctored.replace(word, "")
    assert not _says_it_was_pressed(doctored)


@pytest.mark.parametrize("name", QUIET_VIEWS)
def test_the_quiet_views_still_draw_no_button(name):
    """Routing a sheet submits no action and needs no in-flight wording."""
    source = _read(PHONE / name)
    # Only the inventoried navigation controls are exempt. A button with
    # any other action still requires the acting-view feedback contract.
    routes = {
        "RecentlyView.swift": [".catchUp()", ".agent(agent, category)", ".card(card)"],
        "FleetView.swift": [".catchUp()", ".agent(row.agent, row.category)"],
    }
    routed = 0
    for route in routes.get(name, []):
        control = f"DecryptButton(action: {{ sheets.show({route}) }})"
        routed += source.count(control)
        source = source.replace(control, "Sheet route")
    assert routed == {"RecentlyView.swift": 3, "FleetView.swift": 3}.get(name, 0)
    assert "Button(" not in source, (
        f"{name} grew a button — wire it to the in-flight record and move "
        "it into ACTING_VIEWS")
