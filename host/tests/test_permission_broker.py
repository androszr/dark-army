"""The PermissionRequest broker, daemon half.

A session with no channel — which is every board-dispatched one, because
`dispatch.py` deliberately starts an ordinary session — used to stop dead on a
"may I do this?" dialog and reach Dark Army as a bare `waiting` row: no path, no
options, no request id, nothing anybody could answer. The installed hook script
now offers the ask up and holds, polling, while the terminal's own dialog runs
concurrently; this file is the daemon's side of that conversation.

Everything here is about *bounds and refusals*. The feature's whole safety
case is that Dark Army can be off, slow or wrong without costing the person at the
desk anything, so the interesting cases are the ones where the hold is refused,
the row is reaped, or the verdict is not collectable.
"""

import asyncio
import time
from pathlib import Path

import pytest

from dark_army_daemon import alerts
from dark_army_daemon.codex_rollouts import CodexRecord, record_for_hook_session
from dark_army_daemon import channel_server as cs
from dark_army_daemon.daemon import (
    BobDaemon,
    HOOK_PROMPT_HOLD_SECONDS,
    HOOK_PROMPT_POLL_LAPSE_SECONDS,
)


def _daemon():
    return BobDaemon(headless=True)


def _known(daemon, sid="s1", pid=4242):
    daemon._session_states[sid] = {
        "state": "working", "last_event": time.time(), "pid": pid,
    }
    return sid


def _ask(daemon, *, sid="s1", request_id="hook-abc", claim="cl41m",
         tool_name="Read", description="/etc/hosts",
         input_preview='{"file_path": "/etc/hosts"}'):
    return asyncio.run(daemon._handle_message({
        "event": "permission_ask", "session_id": sid, "cwd": "/x/proj",
        "request_id": request_id, "claim": claim, "tool_name": tool_name,
        "description": description, "input_preview": input_preview,
    }))


def _poll(daemon, *, sid="s1", request_id="hook-abc", claim="cl41m"):
    return asyncio.run(daemon._handle_message({
        "event": "permission_poll", "session_id": sid, "cwd": "/x/proj",
        "request_id": request_id, "claim": claim,
    }))


# ── the ask ──────────────────────────────────────────────────────────────────

def test_an_ask_for_a_known_session_is_held_and_shown():
    daemon = _daemon()
    _known(daemon)
    assert _ask(daemon) == {"hold": int(HOOK_PROMPT_HOLD_SECONDS)}
    row = daemon._permission_snapshot()[0]
    assert row["request_id"] == "hook-abc"
    assert row["session_id"] == "s1"
    assert row["tool_name"] == "Read"
    assert row["description"] == "/etc/hosts"
    # And the session is blocked, by the same state effect the legacy
    # `permission` event has always had.
    assert daemon._session_states["s1"]["state"] == "waiting"


def test_the_brokers_own_bookkeeping_never_reaches_a_surface():
    """`claim` is a secret with the channel secret's discipline: `/api/state`
    reads are ungated, so publishing it would let any local process collect
    the verdict and leave the phone's tap doing nothing. The other three are
    internals no surface has a use for."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    row = daemon._permission_snapshot()[0]
    for private in ("claim", "verdict", "last_poll_at", "hold_until", "port"):
        assert private not in row, private
    assert "cl41m" not in repr(daemon._permission_snapshot())


def test_the_daemon_reclamps_what_the_script_sent():
    """The script on disk is a copy and may be older than this process, so
    the bounds it applied are not the bounds this one is willing to publish."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon, description="d" * 900, input_preview="i" * 900)
    row = daemon._permission_snapshot()[0]
    assert len(row["description"]) == 300
    assert len(row["input_preview"]) == 400


@pytest.mark.parametrize("tool", ["AskUserQuestion", "ask_user_question"])
def test_an_ask_user_question_companion_is_refused(tool):
    """`PermissionRequest` fires alongside AskUserQuestion's own PreToolUse.
    That dialog already has an answer path; brokering it would put two
    surfaces on one question."""
    daemon = _daemon()
    _known(daemon)
    assert _ask(daemon, tool_name=tool) == {"hold": 0}
    assert daemon._permission_snapshot() == []


def test_a_session_with_a_live_channel_is_refused():
    """The relay already covers it, with a request id of its own. Two rows
    for one dialog is two cards, two buzzes, and two ways to half-answer."""
    daemon = _daemon()
    _known(daemon)
    daemon._handle_channel_message({
        "type": "channel_attach", "port": 51000, "pid": 4242, "cwd": "/tmp",
        "session_id": "", "is_channel": True, "secret": "",
        "host": cs.HOST_CLAUDE})
    assert daemon._channel_for_session("s1") is not None
    assert _ask(daemon) == {"hold": 0}
    assert daemon._permission_snapshot() == []


def test_an_unknown_session_is_refused():
    """A row nobody can render, on a session that never announced itself —
    the same argument the legacy event's `create=False` makes."""
    daemon = _daemon()
    assert _ask(daemon, sid="never-seen") == {"hold": 0}
    assert daemon._permission_snapshot() == []


def test_an_ask_with_no_request_id_is_refused():
    daemon = _daemon()
    _known(daemon)
    assert _ask(daemon, request_id="") == {"hold": 0}
    assert daemon._permission_snapshot() == []


def test_an_ask_with_no_claim_is_refused():
    """Fail closed, `channel_server.listen()`'s discipline. `hmac.compare_digest`
    of two empty strings is True, and the request id is published in the ungated
    /api/state — so a row minted with no claim is one any local process could poll
    the verdict off. There is no such row."""
    daemon = _daemon()
    _known(daemon)
    assert _ask(daemon, claim="") == {"hold": 0}
    assert daemon._permission_snapshot() == []
    assert _poll(daemon, claim="") == {"verdict": "gone"}


def test_an_ask_raised_inside_a_subagent_is_refused():
    """The one path where the CLI is *known* to serialise: the async-subagent
    spawn context sets `awaitAutomatedChecksBeforeDialog`, so the hooks are
    awaited to completion **before** the dialog is presented. A hold there is
    not a second way to answer — it is a silent freeze with nothing on screen.
    The parent redirect would otherwise make it look like an ordinary ask on a
    live session and grant the hold."""
    daemon = _daemon()
    _known(daemon)
    daemon._session_states["s1"]["subagents"] = {"kid"}
    assert daemon._parent_of_child("kid") == "s1"
    assert _ask(daemon, sid="kid") == {"hold": 0}
    assert daemon._permission_snapshot() == []


def test_a_subagents_ask_still_blocks_the_parent():
    """Refusing the hold is not refusing the event: the parent is stopped on a
    dialog either way, and the legacy state effect is what puts it in
    `waiting`."""
    daemon = _daemon()
    _known(daemon)
    daemon._session_states["s1"]["subagents"] = {"kid"}
    daemon._session_states["s1"]["state"] = "working"
    _ask(daemon, sid="kid")
    assert daemon._session_states["s1"]["state"] == "waiting"
    assert "Raised inside a helper agent" in daemon._session_states["s1"]["ask_note"]


def test_codex_hook_session_needs_one_root():
    root = CodexRecord(session_id="codex:one", thread_id="one", path=Path("/tmp/one"))
    child = CodexRecord(session_id="codex:two", thread_id="two", path=Path("/tmp/two"),
                        parent_thread_id="one")
    assert record_for_hook_session({"one": root, "two": child}, "one") is root
    assert record_for_hook_session({"one": root, "two": child}, "two") is None
    assert record_for_hook_session({"one": root, "two": child}, "unknown") is None


def test_codex_detection_row_is_visible_but_not_held():
    daemon = _daemon()
    record = CodexRecord(session_id="codex:one", thread_id="one",
                         path=Path("/tmp/one"), current_tool="shell_command",
                         turn_active=True)
    daemon._codex_records[record.session_id] = record
    reply = daemon._decide_permission_ask({
        "provider": "codex", "session_id": "one", "request_id": "hook-codex",
        "claim": "secret", "tool_name": "shell_command",
        "description": "touch /tmp/probe"})
    assert reply == {"hold": 0}
    row = daemon._permission_snapshot()[0]
    assert row["session_id"] == "codex:one"
    assert row["description"] == "touch /tmp/probe"
    assert row["answerable"] is False
    assert "Codex terminal" in row["answer_note"]
    assert "claim" not in row
    assert asyncio.run(daemon.answer_permission("hook-codex", "allow"))[0] is False


def test_a_request_id_already_open_is_refused():
    """Overwriting the row would replace its claim, so the script that is
    actually polling could never collect a verdict again — a prompt on every
    surface that nothing can answer."""
    daemon = _daemon()
    _known(daemon)
    assert _ask(daemon) == {"hold": int(HOOK_PROMPT_HOLD_SECONDS)}
    assert _ask(daemon, claim="somebody-elses", tool_name="Bash") == {"hold": 0}
    rows = daemon._permission_snapshot()
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "Read"
    # And the original claim still collects, which is the whole point.
    assert daemon._permission_requests["hook-abc"]["claim"] == "cl41m"
    assert _poll(daemon) == {"verdict": None}


def test_a_refused_ask_still_blocks_the_session():
    """The refusal is about the *hold*, never about the event: a session
    stopped on a dialog is waiting whether or not Dark Army can answer it."""
    daemon = _daemon()
    _known(daemon)
    daemon._session_states["s1"]["state"] = "working"
    assert _ask(daemon, tool_name="AskUserQuestion") == {"hold": 0}
    assert daemon._session_states["s1"]["state"] == "waiting"


def test_a_standing_question_survives_an_ask_naming_ask_user_question():
    """The guard in `_track_pending_question`, exercised through the new
    message: a permission event naming AskUserQuestion is the companion of
    the very PreToolUse that set the question, milliseconds behind, and must
    not pop it."""
    daemon = _daemon()
    _known(daemon)
    daemon._pending_questions["s1"] = {"text": "Which fix?", "options": []}
    _ask(daemon, tool_name="AskUserQuestion")
    assert daemon._pending_questions["s1"]["text"] == "Which fix?"


def test_a_standing_question_survives_an_ask_naming_no_tool():
    daemon = _daemon()
    _known(daemon)
    daemon._pending_questions["s1"] = {"text": "Which fix?", "options": []}
    _ask(daemon, tool_name="")
    assert daemon._pending_questions["s1"]["text"] == "Which fix?"


# ── the poll and the verdict ─────────────────────────────────────────────────

def test_a_poll_with_no_verdict_yet_says_so():
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    assert _poll(daemon) == {"verdict": None}


def test_a_poll_with_the_wrong_claim_is_told_the_row_is_gone():
    """The claim is what makes the verdict collectable by the process that
    asked and nothing else on this machine."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    assert _poll(daemon, claim="guessed") == {"verdict": "gone"}
    # And refusing tells the real broker nothing: its row is untouched.
    assert _poll(daemon) == {"verdict": None}


def test_a_poll_for_an_unknown_request_is_told_the_row_is_gone():
    daemon = _daemon()
    _known(daemon)
    assert _poll(daemon, request_id="hook-nope") == {"verdict": "gone"}


def test_an_answer_is_collected_by_the_next_poll_exactly_once():
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    assert asyncio.run(daemon.answer_permission("hook-abc", "allow")) == (True, "")
    assert _poll(daemon) == {"verdict": "allow"}
    assert daemon._permission_snapshot() == []
    # A second poll cannot answer the dialog a second time.
    assert _poll(daemon) == {"verdict": "gone"}


def test_a_denial_is_carried_the_same_way():
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    assert asyncio.run(daemon.answer_permission("hook-abc", "deny"))[0] is True
    assert _poll(daemon) == {"verdict": "deny"}


def test_answering_a_row_whose_broker_stopped_polling_is_refused():
    """The answer-at-desk race, bounded. A tap that lands after the script
    exited would never be collected, and reporting success for it is the
    lesson `stop_session` already paid for."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    row = daemon._permission_requests["hook-abc"]
    row["last_poll_at"] = time.time() - (HOOK_PROMPT_POLL_LAPSE_SECONDS + 5)
    ok, detail = asyncio.run(daemon.answer_permission("hook-abc", "allow"))
    assert ok is False
    assert detail == "That session is no longer listening."


def test_answering_a_hook_row_never_reaches_the_channel(monkeypatch):
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)

    async def _boom(*a, **k):
        pytest.fail("a hook row has no channel to send down")

    monkeypatch.setattr(daemon, "_send_to_channel", _boom)
    assert asyncio.run(daemon.answer_permission("hook-abc", "allow"))[0] is True


# ── the reap ─────────────────────────────────────────────────────────────────

def test_a_broker_that_stopped_polling_drops_its_row():
    """Without this the row pins its session under "Needs you" for the life
    of the daemon — the exact failure `_reap_permissions` exists to prevent,
    and a hook row has no channel for the usual signal to speak about."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    daemon._permission_requests["hook-abc"]["last_poll_at"] = \
        time.time() - (HOOK_PROMPT_POLL_LAPSE_SECONDS + 5)
    assert daemon._permission_snapshot() == []


def test_a_row_past_its_hold_is_dropped_even_while_polling():
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    row = daemon._permission_requests["hook-abc"]
    row["hold_until"] = time.time() - 1
    row["last_poll_at"] = time.time()
    assert daemon._permission_snapshot() == []


def _answered_at_the_desk(daemon, *, state, request_id="hook-abc", sid="s1"):
    """The shape of a dialog answered at the terminal, not in Dark Army.

    The script kept polling (2.1.263 does not always stop promptly), so the
    15s lapse never fires, and the hold is half an hour away. All that has
    changed is the session: it emitted a later hook event, in `state`.
    """
    now = time.time()
    row = daemon._permission_requests[request_id]
    row["asked_at"] = now - 10
    row["last_poll_at"] = now
    row["hold_until"] = row["asked_at"] + HOOK_PROMPT_HOLD_SECONDS
    daemon._session_states[sid]["state"] = state
    daemon._session_states[sid]["last_event"] = now - 5


@pytest.mark.parametrize("state", ["idle", "error"])
def test_a_desk_answer_that_ends_the_turn_drops_the_row(state):
    """The failure this rung exists for. The person answers at the terminal,
    the tool runs, and the turn *finishes* — `Stop` writes `idle`, a
    `StopFailure` writes `error`. Neither is `working` or `thinking`, so the
    resume rung below never speaks, and before this rung the spent row sat
    there until `hold_until` — thirty seconds once, thirty minutes now."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    _answered_at_the_desk(daemon, state=state)
    assert daemon._permission_snapshot() == []


def test_a_spent_row_no_longer_shadows_the_next_real_ask():
    """What the half-hour window actually cost. `_prompts_by_session` takes
    the *oldest* row per session, so a row nobody reaped is the one the panel
    draws and the one `alerts._permission` keys its "already buzzed" memory
    on — the genuine ask behind it raises no banner and shows the wrong tool
    name for as long as the stale row lives."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    _answered_at_the_desk(daemon, state="idle")
    # A snapshot pass in between, which is the real timeline: the reap rides
    # every read, and the next ask is minutes of work away. It matters that
    # the drop happens *here* — the ask itself writes the session back to
    # `waiting`, so a row that survived this long survives the whole hold.
    daemon._permission_snapshot()
    _ask(daemon, request_id="hook-def", tool_name="Bash",
         description="rm -rf .build", input_preview='{"command": "rm"}')
    by_session = daemon._prompts_by_session()
    assert by_session["s1"]["request_id"] == "hook-def"
    assert by_session["s1"]["tool_name"] == "Bash"


def test_two_asks_in_one_turn_and_the_second_wins():
    """The same-turn shadow, which no clock or state rung can close.

    Ask A is answered at the desk and its tool runs; ask B lands in the same
    turn. Between the two the session is `waiting` the whole time — ask A's
    own `permission` event stamps it, with a `last_event` later than
    `asked_at` — so `_hook_prompt_turn_ended` never speaks, and the script for
    A is still polling, so the lapse does not either. Before
    `_decide_permission_ask` popped the earlier row, `_prompts_by_session`
    took the *oldest* and named A's spent `Read` while B's `Bash rm -rf` was
    the thing the session was actually stopped on: the panel and the phone
    drew the wrong tool, `alerts._permission` keyed "already buzzed" on A's
    request id so B raised no banner at all, and reply, typing, wrap-up and
    `can_close` stayed refused behind a dead row for the whole of the hold.
    """
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    # Exactly what the ask itself wrote, spelled out: the state rung is blind
    # here, and so is the poll lapse while the script keeps coming back.
    assert daemon._session_states["s1"]["state"] == "waiting"
    _poll(daemon)
    daemon._permission_snapshot()
    _ask(daemon, request_id="hook-def", tool_name="Bash",
         description="rm -rf .build", input_preview='{"command": "rm"}')
    assert daemon._session_states["s1"]["state"] == "waiting"
    assert list(daemon._permission_requests) == ["hook-def"]
    by_session = daemon._prompts_by_session()
    assert by_session["s1"]["request_id"] == "hook-def"
    assert by_session["s1"]["tool_name"] == "Bash"


@pytest.mark.parametrize("kwargs, why", [
    ({"tool_name": "AskUserQuestion"}, "the question flow owns that dialog"),
    ({"claim": ""}, "no claim token"),
])
def test_a_refused_ask_evicts_nothing(kwargs, why):
    """The pop sits *after* the six refusals, and this is why. A refused ask
    is one Dark Army is not brokering — a subagent's, an `AskUserQuestion`
    companion, a claimless one — and it is no evidence at all that the
    session's live dialog is over. If the eviction ran first, the companion
    `PermissionRequest` that fires beside every `AskUserQuestion` would take
    down a genuine open row and leave it unanswerable from every surface."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    assert _ask(daemon, request_id="hook-def", **kwargs) == {"hold": 0}
    assert list(daemon._permission_requests) == ["hook-abc"]
    assert daemon._prompts_by_session()["s1"]["request_id"] == "hook-abc"


def test_a_new_ask_leaves_another_sessions_row_alone():
    """The pop is keyed on the session, and two blocked sessions are the
    ordinary case on a machine running a fleet."""
    daemon = _daemon()
    _known(daemon)
    _known(daemon, sid="s2", pid=4343)
    _ask(daemon)
    _ask(daemon, sid="s2", request_id="hook-two", tool_name="Write")
    _ask(daemon, request_id="hook-def", tool_name="Bash")
    assert sorted(daemon._permission_requests) == ["hook-def", "hook-two"]
    by_session = daemon._prompts_by_session()
    assert by_session["s1"]["request_id"] == "hook-def"
    assert by_session["s2"]["request_id"] == "hook-two"


def test_a_new_ask_never_touches_a_channel_row():
    """Channel rows serialise on the channel and carry a request id of their
    own; this rung speaks only for `via == "hook"`."""
    daemon = _daemon()
    _known(daemon, sid="s2", pid=4343)
    daemon._permission_requests["chan-1"] = {
        "request_id": "chan-1", "session_id": "s2", "tool_name": "Read",
        "asked_at": time.time(), "port": 5555, "via": "channel",
    }
    _known(daemon)
    _ask(daemon)
    _ask(daemon, request_id="hook-def", tool_name="Bash")
    assert "chan-1" in daemon._permission_requests


@pytest.mark.parametrize("state", ["confused", "waiting"])
def test_a_notification_while_the_dialog_is_up_keeps_the_row(state):
    """The mirror failure, and why the rung reads a named state set rather
    than a bare `last_event > asked_at`. Claude Code fires a `Notification`
    (`notification_type=permission_prompt`) seconds behind the ask and another
    every ~60s of quiet; `_update_session_state` writes `confused` and moves
    the clock. Reading that as "the turn ended" would reap nearly every hook
    row moments after it opened, with the dialog still on the screen."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    _answered_at_the_desk(daemon, state=state)
    assert len(daemon._permission_snapshot()) == 1


def test_a_session_the_daemon_cannot_see_is_not_judged_by_this_rung():
    """No state means no evidence. Dropping the row here would race the poll
    lapse, which is the rung that speaks for a session that went away."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    row = daemon._permission_requests["hook-abc"]
    row["last_poll_at"] = time.time()
    row["hold_until"] = time.time() + HOOK_PROMPT_HOLD_SECONDS
    daemon._session_states.pop("s1")
    assert len(daemon._permission_snapshot()) == 1


def test_the_poll_lapse_rule_does_not_touch_a_channel_row():
    """A relayed prompt has no `last_poll_at` at all; reading its absence as
    a lapse would reap every channel prompt the moment it arrived."""
    daemon = _daemon()
    daemon._handle_channel_message({
        "type": "channel_attach", "port": 51000, "pid": 4242, "cwd": "/tmp",
        "session_id": "", "is_channel": True, "secret": "",
        "host": cs.HOST_CLAUDE})
    _known(daemon)
    daemon._handle_channel_message({
        "type": "channel_permission_request", "port": 51000, "pid": 4242,
        "request_id": "abcde", "tool_name": "Bash",
        "description": "Delete the build directory",
        "input_preview": '{"command": "rm -rf .build"}'})
    rows = daemon._permission_snapshot()
    assert [r["request_id"] for r in rows] == ["abcde"]


def test_polling_again_keeps_the_row_alive():
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    daemon._permission_requests["hook-abc"]["last_poll_at"] = \
        time.time() - (HOOK_PROMPT_POLL_LAPSE_SECONDS - 2)
    assert _poll(daemon) == {"verdict": None}
    assert len(daemon._permission_snapshot()) == 1


# ── what the row costs the surfaces ──────────────────────────────────────────

def test_a_blocked_session_cannot_have_its_terminal_closed(monkeypatch):
    """`close_session_terminal` refuses the press while a prompt is open, so
    a `can_close` that ignored prompts drew a button the daemon would then
    turn down — which is what the incident's phone showed."""
    from dark_army_daemon import vscode_reveal as vr

    monkeypatch.setattr(vr, "can_close_terminal", lambda pid, tty="": True)
    monkeypatch.setattr(vr, "can_send_text", lambda pid, tty="": True)
    daemon = _daemon()
    _known(daemon)
    stubs = [{"session_id": "s1", "pid": 4242, "_category": "waiting",
              "cwd": ""}]
    assert daemon._enrich_agent_stubs(stubs)["waiting"][0]["can_close"] is True
    _ask(daemon)
    assert daemon._enrich_agent_stubs(stubs)["waiting"][0]["can_close"] is False
    # And it comes back once the ask is over.
    daemon._permission_requests["hook-abc"]["last_poll_at"] = \
        time.time() - (HOOK_PROMPT_POLL_LAPSE_SECONDS + 5)
    assert daemon._enrich_agent_stubs(stubs)["waiting"][0]["can_close"] is True


def test_a_brokered_ask_buzzes_once_and_only_once():
    """No new code carries this: the row lands in `_prompts_by_session()`,
    which is what `AlertPolicy.evaluate` already reads, and `_permission`
    dedupes on the request id. Pinned because the buzz is the whole point of
    the phone half."""
    daemon = _daemon()
    _known(daemon)
    _ask(daemon)
    policy = alerts.AlertPolicy()
    snapshot = {"running": [], "sleeping": [], "abandoned": [], "finished": [],
                "waiting": [{"session_id": "s1", "nickname": "Vex",
                             "project": "proj", "branch": "main"}]}
    now = time.time()
    first = policy.evaluate(snapshot, {}, now,
                            prompts=daemon._prompts_by_session())
    second = policy.evaluate(snapshot, {}, now + 5,
                             prompts=daemon._prompts_by_session())
    assert [a.rule for a in first] == ["permission"]
    assert first[0].title == "Vex wants to run Read"
    assert second == []


# --- the three figures that have to agree ----------------------------------
#
# The hold is not one number but three, kept in two files and a settings
# dict, and the failure they guard against is the *half-change*: raising the
# script's own deadline and the daemon's hold while leaving the CLI's
# `timeout` where it was silently caps the real hold at the old figure with
# nothing anywhere saying so. These pin the relations, never the numbers, so
# they hold whether the live-fire check
# (`docs/2026-09-07-permission-hold-verification.md`) came back CONCURRENT or
# not — a failed live run must not fail the suite.

def test_the_hold_never_outlives_the_longstop():
    """`hold_until` is `asked_at + HOOK_PROMPT_HOLD_SECONDS`, and
    `PERMISSION_STALE_SECONDS` is the age-alone longstop behind it. The hold
    giving up *after* the longstop would make `_reap_permissions` drop a hook
    row with "it went unanswered too long" while the script was still
    politely polling — a row that vanished for a reason that was not true of
    it. Equal is allowed and is the raised setting: `hold_until` wins by
    evaluation order and the log line stays the broker's own."""
    from dark_army_daemon.daemon import PERMISSION_STALE_SECONDS
    assert HOOK_PROMPT_HOLD_SECONDS > 0
    assert HOOK_PROMPT_HOLD_SECONDS <= PERMISSION_STALE_SECONDS


def test_the_hold_outlasts_the_poll_lapse():
    """The poll lapse is the load-bearing eviction signal and does not move
    with the hold. A hold shorter than it would make the age rung fire before
    the evidence rung ever could, which is the wrong way round: the hold is
    a clock, the lapse is evidence."""
    assert HOOK_PROMPT_HOLD_SECONDS > HOOK_PROMPT_POLL_LAPSE_SECONDS


def test_the_script_holds_no_longer_than_the_daemon_asks():
    """The installed script's own `BROKER_WAIT_SECONDS` is the real patience
    — the daemon's `hold` reply is a boolean to it, never a duration — so the
    two have to be written to the same figure by hand. And the CLI's
    `timeout` on the `PermissionRequest` entry has to clear it *strictly*:
    equal is the case where the CLI's kill and the script's own exit race,
    and a script killed mid-write prints nothing and answers nothing."""
    import re

    from dark_army_menubar import hooks

    found = re.search(r"BROKER_WAIT_SECONDS = ([0-9.]+)", hooks.NOTIFY_SCRIPT)
    assert found, "the script no longer states its own deadline"
    wait = float(found.group(1))
    assert wait == HOOK_PROMPT_HOLD_SECONDS

    entry = hooks.HOOKS_CONFIG["PermissionRequest"]
    ours = [h for group in entry for h in group["hooks"]]
    assert len(ours) == 1
    timeout = ours[0]["timeout"]
    assert timeout > wait, (
        "the CLI's timeout must clear the script's deadline, or the hook is "
        "killed before it can print its answer")
