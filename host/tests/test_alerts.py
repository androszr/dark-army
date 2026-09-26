"""The gate between "worth displaying" and "worth interrupting you".

Signals are evaluated every few seconds; a notification is an interrupt. Almost
every test here is about *not* firing.
"""
import pytest

from dark_army_daemon.alerts import (
    AlertPolicy, KINDS, PER_SESSION_COOLDOWN, clear_resolved, marker_current,
    offered_reply,
)
from dark_army_daemon import work_report as _wr


def agent(sid="s1", category=None, signals=(), nickname="Vex", project="repo",
          questions=None):
    row = {"session_id": sid, "nickname": nickname, "project": project,
           "signals": [dict(s) for s in signals]}
    if questions is not None:
        row["questions"] = list(questions)
    return row


def snap(**buckets):
    base = {"waiting": [], "running": [], "sleeping": [], "finished": []}
    base.update(buckets)
    return base


CRIT = {"rule": "stall", "severity": "crit", "text": "no activity for 12m"}
WARN = {"rule": "stall", "severity": "warn", "text": "in Bash for 3m"}
WAITING = {"rule": "waiting", "severity": "warn", "text": "blocked 34m"}
CHURN = {"rule": "churn", "severity": "info", "text": "40 edits across 3 files"}


# ── what gets through ────────────────────────────────────────────────────────

def test_a_critical_signal_interrupts():
    policy = AlertPolicy()
    out = policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)
    assert len(out) == 1
    assert out[0].severity == "crit"
    assert "no activity" in out[0].body


def test_blocked_on_a_human_interrupts_below_crit():
    """Being blocked on you is the whole point of the app — nobody is coming
    unless they are told."""
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent(signals=[WAITING])]), {}, now=100)
    assert len(out) == 1
    assert out[0].rule == "waiting"


def test_a_card_carries_the_actual_question():
    policy = AlertPolicy()
    cards = {"s1": {"message": "Run the migration against prod?"}}
    out = policy.evaluate(snap(waiting=[agent(signals=[WAITING])]), cards, now=100)
    assert len(out) == 1
    assert out[0].rule == "card"
    assert out[0].body == "Run the migration against prod?"


# A `Stop` card's message is not a question — it is the constant `protocol.py`
# raises whenever a turn ends. A banner is read in the second it slides past, so
# when the agent left a one-line `bob-tldr` summary, that is the body.

def test_a_summary_replaces_the_stock_wait_message():
    policy = AlertPolicy()
    row = dict(agent(signals=[WAITING]),
               last_summary="The rebuild is done — want me to restart it?")
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].body == "The rebuild is done — want me to restart it?"


def test_a_real_error_outranks_the_summary():
    """StopFailure carries the harness's own words, about *this* failure; the
    summary describes the turn before it."""
    policy = AlertPolicy()
    row = dict(agent(signals=[WAITING]), last_summary="Ready for review.")
    cards = {"s1": {"hook": "StopFailure", "message": "API error: overloaded"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].body == "API error: overloaded"


def test_without_a_summary_the_card_still_speaks():
    policy = AlertPolicy()
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[agent(signals=[WAITING])]), cards, now=100)
    assert out[0].body == "Waiting for input"


def test_the_title_names_the_agent():
    policy = AlertPolicy()
    out = policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)
    assert out[0].title == "Vex needs you"


def test_an_unnamed_agent_still_gets_a_sentence():
    policy = AlertPolicy()
    out = policy.evaluate(
        snap(running=[agent(nickname="", signals=[CRIT])]), {}, now=100)
    assert out[0].title == "repo needs you"


def test_actions_differ_for_a_card():
    policy = AlertPolicy()
    cards = {"s1": {"message": "?"}}
    out = policy.evaluate(snap(waiting=[agent(signals=[WAITING])]), cards, now=100)
    assert "dismiss" in out[0].actions
    out2 = AlertPolicy().evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)
    assert "dismiss" not in out2[0].actions   # nothing to dismiss on a signal


# ── what does not ────────────────────────────────────────────────────────────

def test_a_warning_alone_does_not_interrupt():
    policy = AlertPolicy()
    assert policy.evaluate(snap(running=[agent(signals=[WARN])]), {}, now=100) == []


def test_observations_never_interrupt():
    policy = AlertPolicy()
    loud = {**CHURN, "severity": "crit"}      # even if something marks it crit
    assert policy.evaluate(snap(running=[agent(signals=[loud])]), {}, now=100) == []


def test_a_signal_that_holds_fires_once():
    """A signal true for an hour is not news."""
    policy = AlertPolicy()
    state = snap(running=[agent(signals=[CRIT])])
    assert len(policy.evaluate(state, {}, now=100)) == 1
    assert policy.evaluate(state, {}, now=160) == []
    assert policy.evaluate(state, {}, now=99999) == []


def test_one_agent_cannot_interrupt_twice_in_a_window():
    policy = AlertPolicy()
    assert len(policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)) == 1
    second = {"rule": "ctx-full", "severity": "crit", "text": "context 94%"}
    assert policy.evaluate(snap(running=[agent(signals=[second])]), {}, now=200) == []


def test_the_cooldown_expires():
    policy = AlertPolicy()
    policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)
    second = {"rule": "ctx-full", "severity": "crit", "text": "context 94%"}
    later = 100 + PER_SESSION_COOLDOWN + 1
    assert len(policy.evaluate(snap(running=[agent(signals=[second])]), {}, now=later)) == 1


def test_only_one_alert_per_agent_per_tick():
    policy = AlertPolicy()
    two = [CRIT, {"rule": "ctx-full", "severity": "crit", "text": "context 94%"}]
    assert len(policy.evaluate(snap(running=[agent(signals=two)]), {}, now=100)) == 1


def test_a_muted_agent_says_nothing():
    policy = AlertPolicy()
    policy.mute("s1")
    assert policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100) == []
    policy.unmute("s1")
    assert len(policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=200)) == 1


def test_suppressed_sessions_say_nothing():
    """You are already looking at that window; a notification would tell you
    what is on your screen."""
    policy = AlertPolicy()
    out = policy.evaluate(snap(running=[agent(signals=[CRIT])]), {},
                          now=100, suppressed={"s1"})
    assert out == []


def test_finished_sessions_never_interrupt():
    policy = AlertPolicy()
    assert policy.evaluate(snap(finished=[agent(signals=[CRIT])]), {}, now=100) == []


def test_a_card_on_a_running_row_is_not_an_interrupt():
    """The card path is about a session that has stopped to ask something."""
    policy = AlertPolicy()
    cards = {"s1": {"message": "?"}}
    assert policy.evaluate(snap(running=[agent()]), cards, now=100) == []


def test_an_entry_without_a_session_id_is_skipped():
    policy = AlertPolicy()
    entry = agent(sid="", signals=[CRIT])
    assert policy.evaluate(snap(running=[entry]), {}, now=100) == []


# ── permission prompts ───────────────────────────────────────────────────────
#
# A relayed tool-approval request is a session stopped dead, so it plays by
# harder rules than a signal: no cooldown, no waiting-category gate, once per
# request id ever.

def prompt(rid="req-1", tool="Bash", preview='{"command": "rm -rf .build"}'):
    return {"request_id": rid, "tool_name": tool,
            "description": "Run shell command", "input_preview": preview}


def test_a_prompt_interrupts_and_names_the_tool_and_the_command():
    policy = AlertPolicy()
    out = policy.evaluate(snap(running=[agent()]), {}, now=100,
                          prompts={"s1": prompt()})
    assert len(out) == 1
    assert out[0].rule == "permission"
    assert out[0].title == "Vex wants to run Bash"
    assert "rm -rf .build" in out[0].body


def test_a_prompt_fires_even_while_the_session_is_still_categorised_running():
    """The hook event that flips the session to `waiting` races the relay;
    the prompt itself is the proof it is blocked."""
    policy = AlertPolicy()
    out = policy.evaluate(snap(running=[agent()]), {}, now=100,
                          prompts={"s1": prompt()})
    assert len(out) == 1


def test_the_same_prompt_fires_exactly_once():
    policy = AlertPolicy()
    state = snap(waiting=[agent()])
    assert len(policy.evaluate(state, {}, now=100, prompts={"s1": prompt()})) == 1
    assert policy.evaluate(state, {}, now=160, prompts={"s1": prompt()}) == []


def test_the_cooldown_does_not_swallow_a_prompt():
    """A status change can wait out a cooldown; a blocked tool call cannot —
    the dialog nobody has seen stays up until somebody is told."""
    policy = AlertPolicy()
    assert len(policy.evaluate(snap(running=[agent(signals=[CRIT])]),
                               {}, now=100)) == 1
    out = policy.evaluate(snap(running=[agent()]), {}, now=150,
                          prompts={"s1": prompt()})
    assert len(out) == 1 and out[0].rule == "permission"


def test_a_second_prompt_is_a_new_question():
    """Deduped on the request id, not on the rule: the same session asking to
    run a second command is news, inside the cooldown or not."""
    policy = AlertPolicy()
    state = snap(waiting=[agent()])
    assert len(policy.evaluate(state, {}, now=100, prompts={"s1": prompt("req-1")})) == 1
    assert len(policy.evaluate(state, {}, now=110, prompts={"s1": prompt("req-2")})) == 1


def test_a_prompt_stamps_the_cooldown_for_ordinary_alerts():
    """You were just interrupted about this agent; the next status alert
    honours the window even though the prompt itself ignored it."""
    policy = AlertPolicy()
    policy.evaluate(snap(waiting=[agent()]), {}, now=100, prompts={"s1": prompt()})
    assert policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=150) == []


def test_a_muted_agents_prompt_is_silent():
    policy = AlertPolicy()
    policy.mute("s1")
    assert policy.evaluate(snap(waiting=[agent()]), {}, now=100,
                           prompts={"s1": prompt()}) == []


def test_a_suppressed_sessions_prompt_is_silent():
    """The dialog is on the screen in front of them — frontmost suppression
    applies to prompts exactly as it does to everything else."""
    policy = AlertPolicy()
    assert policy.evaluate(snap(waiting=[agent()]), {}, now=100,
                           suppressed={"s1"}, prompts={"s1": prompt()}) == []


def test_an_open_prompt_survives_clear_resolved_without_refiring():
    """`clear_resolved` re-arms rules whose signal went away — and a prompt is
    never in the signal list, so without the permission exemption it would
    re-fire the same banner on every tick the dialog stays open."""
    policy = AlertPolicy()
    state = snap(waiting=[agent()])
    assert len(policy.evaluate(state, {}, now=100, prompts={"s1": prompt()})) == 1
    clear_resolved(policy, state, {})
    assert policy.evaluate(state, {}, now=160, prompts={"s1": prompt()}) == []


def test_a_prompt_beats_the_card():
    """One interruption per agent per tick, and the prompt carries the harder
    block: the card can be answered whenever, the tool call is stopped now."""
    policy = AlertPolicy()
    cards = {"s1": {"message": "Run the migration against prod?"}}
    out = policy.evaluate(snap(waiting=[agent()]), cards, now=100,
                          prompts={"s1": prompt()})
    assert len(out) == 1 and out[0].rule == "permission"


def test_a_toolless_prompt_still_gets_a_sentence():
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent()]), {}, now=100,
                          prompts={"s1": prompt(tool="", preview="")})
    assert out[0].title == "Vex needs permission"
    assert out[0].body == "Run shell command"       # description as fallback


def test_a_prompt_offers_no_dismiss():
    """`dismiss` names a card verb, and there is no card. Allow/Deny from the
    banner was considered and deferred."""
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent()]), {}, now=100,
                          prompts={"s1": prompt()})
    assert out[0].actions == ("reveal", "mute")


# ── re-arming ────────────────────────────────────────────────────────────────

def test_a_signal_that_clears_and_returns_is_news_again():
    policy = AlertPolicy()
    hot = snap(running=[agent(signals=[CRIT])])
    cool = snap(running=[agent(signals=[])])
    assert len(policy.evaluate(hot, {}, now=100)) == 1
    clear_resolved(policy, cool, {})
    later = 100 + PER_SESSION_COOLDOWN + 1
    assert len(policy.evaluate(hot, {}, now=later)) == 1


def test_clearing_leaves_other_rules_armed():
    policy = AlertPolicy()
    policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)
    # ctx-full never fired; clearing stall must not resurrect anything odd
    clear_resolved(policy, snap(running=[agent(signals=[])]), {})
    assert policy._fired == {}


# ── bounds ───────────────────────────────────────────────────────────────────

def test_state_for_vanished_sessions_is_forgotten():
    """One entry per session the machine ever ran is the exact shape of the
    leaks this codebase has already had to fix."""
    policy = AlertPolicy()
    for i in range(50):
        policy.evaluate(snap(running=[agent(sid=f"s{i}", signals=[CRIT])]),
                        {}, now=100 + i)
    assert len(policy._fired) == 1
    assert len(policy._last_per_session) == 1


def test_muting_a_session_that_disappears_is_forgotten():
    policy = AlertPolicy()
    policy.mute("gone")
    policy.evaluate(snap(running=[agent(sid="s1")]), {}, now=100)
    assert not policy.is_muted("gone")


# ── what a banner says ───────────────────────────────────────────────────────

def test_an_alert_carries_where_the_agent_is_working():
    policy = AlertPolicy()
    entry = agent(signals=[CRIT], project="dark-army")
    entry["branch"] = "feat/panel"
    out = policy.evaluate(snap(running=[entry]), {}, now=100)
    assert out[0].subtitle == "dark-army · feat/panel"


def test_a_subtitle_never_just_repeats_the_title():
    """Without a nickname the title is already the project, and printing it
    again directly underneath says nothing."""
    policy = AlertPolicy()
    entry = agent(signals=[CRIT], nickname="", project="dark-army")
    out = policy.evaluate(snap(running=[entry]), {}, now=100)
    assert out[0].title == "dark-army needs you"
    assert out[0].subtitle == ""


def test_a_branchless_agent_gets_a_subtitle_anyway():
    policy = AlertPolicy()
    out = policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)
    assert out[0].subtitle == "repo"


# ── the kind: what sort of interruption this is ──────────────────────────────
#
# One word from `KINDS`, decided here where the question slot, the card's hook
# and the prompt's tool are all in hand. The phone's buzz reads it for its
# words and its sound, and never re-derives it.

QUESTION = {"question": "Which database?", "options": ["prod", "staging"]}
GENERIC = {"hook": "Notification", "message": "Waiting for input"}


def test_the_kinds_are_ordered_most_urgent_first():
    # `security` leads: a burst of refused knocks at the phone doors
    # (`access_log.py`) is about the machine, not an agent, and a buzz
    # collapsing it with an agent's ask plays the more urgent cue.
    assert KINDS == ("security", "permission", "question", "attention", "finished")


def test_a_standing_question_under_the_idle_reminder_is_a_question():
    """The card that carries a question to the gate *is* the generic idle
    reminder — the hook alone would call every question a finished turn.
    The entry's question slot is the signal."""
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent(questions=[QUESTION])]),
                          {"s1": dict(GENERIC)}, now=100)
    assert len(out) == 1
    assert out[0].rule == "card"
    assert out[0].kind == "question"


def test_the_singular_question_slot_counts_too():
    policy = AlertPolicy()
    row = agent()
    row["question"] = dict(QUESTION)
    out = policy.evaluate(snap(waiting=[row]), {"s1": dict(GENERIC)}, now=100)
    assert out[0].kind == "question"


def test_the_same_reminder_with_no_question_is_a_finished_turn():
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent()]), {"s1": dict(GENERIC)}, now=100)
    assert out[0].kind == "finished"


def test_an_empty_question_list_is_not_a_question():
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent(questions=[])]),
                          {"s1": dict(GENERIC)}, now=100)
    assert out[0].kind == "finished"


def test_a_stop_card_with_nothing_standing_is_finished():
    policy = AlertPolicy()
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[agent()]), cards, now=100)
    assert out[0].kind == "finished"


def test_a_tldr_summary_under_a_stop_card_is_finished():
    """A `bob-tldr` summary alone is a caption, not a question (22 Sep 2026,
    RC1 in the buzz research report): every turn may leave one, and reading
    it as a question buzzed the phone "asked you a question" for turns that
    asked nothing. The banner body still carries the summary."""
    policy = AlertPolicy()
    row = agent()
    row["last_summary"] = "Ready to merge?"
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].kind == "finished"
    assert out[0].body == "Ready to merge?"


def test_offered_actions_under_a_stop_card_are_a_question():
    """`bob-actions` names the choices — there is a verdict to give."""
    policy = AlertPolicy()
    row = agent()
    row["reply_options"] = ["Accept", "Iterate"]
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].kind == "question"


def test_choices_offered_before_the_last_prompt_are_finished():
    """S5: the transcript's clocks decide. Choices written before the
    person's last prompt answered the previous turn, not this one."""
    policy = AlertPolicy()
    row = agent()
    row["reply_options"] = ["Accept", "Iterate"]
    row["marker_at"] = 1000.0
    row["prompt_at"] = 1005.0
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].kind == "finished"


def test_choices_offered_after_the_last_prompt_are_a_question():
    policy = AlertPolicy()
    row = agent()
    row["reply_options"] = ["Accept", "Iterate"]
    row["marker_at"] = 1010.0
    row["prompt_at"] = 1005.0
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].kind == "question"


def test_choices_with_no_clocks_are_a_question_as_before():
    """Grok and Codex rows carry no transcript clocks: the old reading."""
    policy = AlertPolicy()
    row = agent()
    row["reply_options"] = ["Accept", "Iterate"]
    assert "marker_at" not in row and "prompt_at" not in row
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].kind == "question"


def test_a_stale_question_slot_still_wins_over_the_clocks():
    """The clocks gate the offered choices only; a question dialog up is a
    question whatever they say."""
    policy = AlertPolicy()
    row = agent(questions=[QUESTION])
    row["marker_at"] = 1.0
    row["prompt_at"] = 2.0
    out = policy.evaluate(snap(waiting=[row]), {"s1": dict(GENERIC)}, now=100)
    assert out[0].kind == "question"


@pytest.mark.parametrize("entry, current", [
    ({}, True),                                            # no clocks: as before
    ({"marker_at": 0.0, "prompt_at": 0.0}, True),
    ({"marker_at": 10.0}, True),                           # no prompt seen
    ({"prompt_at": 10.0}, False),                          # marker cleared
    ({"marker_at": 10.0, "prompt_at": 10.0}, True),        # same instant
    ({"marker_at": 9.0, "prompt_at": 10.0}, False),
    ({"marker_at": 11.0, "prompt_at": 10.0}, True),
    ({"marker_at": None, "prompt_at": None}, True),
    ({"marker_at": "junk", "prompt_at": 10.0}, True),      # unreadable: as before
])
def test_marker_current_compares_the_two_clocks(entry, current):
    assert marker_current(entry) is current


@pytest.mark.parametrize("entry, offered", [
    ({"reply_options": ["Accept"]}, True),
    ({"reply_options": ["Accept"], "marker_at": 5.0, "prompt_at": 4.0}, True),
    ({"reply_options": ["Accept"], "marker_at": 3.0, "prompt_at": 4.0}, False),
    ({"reply_options": []}, False),
    ({"reply_options": "Accept"}, False),                  # not a list
    ({}, False),
    ({"last_summary": "Ready to merge?"}, False),          # a caption is not an offer
])
def test_offered_reply_needs_current_named_choices(entry, offered):
    assert offered_reply(entry) is offered


def _episodes_minted(monkeypatch, row):
    """`decision_capture.reconcile` over one waiting row: the kinds of the
    episodes it would observe. The store is never touched."""
    import threading
    from concurrent.futures import Future

    from dark_army_daemon import decision_capture, enrollment

    capture = decision_capture.DecisionCapture.__new__(
        decision_capture.DecisionCapture)
    capture.hook_state = {}
    capture.session_items = {}
    capture.slots = set()
    capture.hook_lock = threading.RLock()
    observed = []

    def submit(method, *args, **kwargs):
        if method == "observe":
            observed.append(args[1]["kind"])
        done = Future()
        done.set_result(None)
        return done

    capture.submit = submit
    monkeypatch.setattr(enrollment, "root_enrolled", lambda cwd: "/repo")
    capture.reconcile({"waiting": [dict(row)]})
    return observed


@pytest.mark.parametrize("extra, minted", [
    ({"last_summary": "Ready to merge?"}, []),                 # S1: a caption
    ({"reply_options": ["Accept", "Iterate"]}, ["response_request"]),
    ({"reply_options": ["Accept"], "marker_at": 1.0, "prompt_at": 2.0}, []),
    ({"reply_options": ["Accept"], "marker_at": 3.0, "prompt_at": 2.0},
     ["response_request"]),
])
def test_the_decision_mirror_reads_the_same_offer_as_the_buzz(
        monkeypatch, extra, minted):
    """RC4: `decision_capture.reconcile` minted a response request on the
    summary alone, the same rule as the buzz; both read `offered_reply`."""
    row = dict({"session_id": "s1", "cwd": "/repo", "project": "repo"}, **extra)
    assert _episodes_minted(monkeypatch, row) == minted


def test_a_blank_summary_and_no_actions_are_still_finished():
    policy = AlertPolicy()
    row = agent()
    row["last_summary"] = "   "
    row["reply_options"] = []
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].kind == "finished"


def test_a_real_error_is_attention_not_finished():
    """StopFailure is the harness's own error — nothing finished."""
    policy = AlertPolicy()
    cards = {"s1": {"hook": "StopFailure", "message": "API error: overloaded"}}
    out = policy.evaluate(snap(waiting=[agent()]), cards, now=100)
    assert out[0].kind == "attention"


def test_a_critical_signal_is_attention():
    policy = AlertPolicy()
    out = policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)
    assert out[0].kind == "attention"


def test_the_waiting_signal_with_no_card_is_attention():
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent(signals=[WAITING])]), {}, now=100)
    assert out[0].rule == "waiting"
    assert out[0].kind == "attention"


def test_a_tool_approval_prompt_is_a_permission():
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent()]), {}, now=100,
                          prompts={"s1": prompt(tool="Bash")})
    assert out[0].kind == "permission"


def test_a_relayed_ask_user_question_is_a_question_with_the_same_title():
    """A channel session relays `AskUserQuestion` through the prompt route;
    the kind tells it apart while the Mac title keeps naming the tool."""
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent()]), {}, now=100,
                          prompts={"s1": prompt(tool="AskUserQuestion")})
    assert out[0].kind == "question"
    assert out[0].title == "Vex wants to run AskUserQuestion"


def test_every_alert_publishes_its_kind():
    policy = AlertPolicy()
    out = policy.evaluate(
        snap(waiting=[agent(questions=[QUESTION]), agent("s2", signals=[WAITING])],
             running=[agent("s3", signals=[CRIT])]),
        {"s1": dict(GENERIC)}, now=100,
        prompts={"s3": prompt()})
    kinds = {a.session_id: a.as_dict()["kind"] for a in out}
    assert kinds == {"s1": "question", "s2": "attention", "s3": "permission"}
    assert all(k in KINDS for k in kinds.values())


# ── what is needed, in the agent's own words ─────────────────────────────────
# `need` and `tool` are the two facts the phone's third line is composed
# from. Decided here beside `kind`, never re-derived downstream, and never
# the command preview, the `description` or a `StopFailure`'s error.

def test_a_question_alert_carries_the_question_text():
    policy = AlertPolicy()
    out = policy.evaluate(
        snap(waiting=[agent(questions=[{"text": "Which database?",
                                        "options": ["prod", "staging"]}])]),
        {"s1": dict(GENERIC)}, now=100)
    assert out[0].kind == "question"
    assert out[0].need == "Which database?"


def test_a_summary_under_a_stop_card_is_the_need():
    policy = AlertPolicy()
    row = agent()
    row["last_summary"] = "  Ready to merge — want me to push?  "
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].need == "Ready to merge — want me to push?"


def test_a_real_error_is_never_the_need():
    """`StopFailure` carries the harness's own error on `body`; the third
    line on a locked screen is the agent's words or nothing."""
    policy = AlertPolicy()
    row = agent()
    row["last_summary"] = "Ready for review."
    cards = {"s1": {"hook": "StopFailure", "message": "API error: overloaded"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].body == "API error: overloaded"
    assert out[0].need == ""
    assert out[0].tool == ""


def test_a_tool_approval_stamps_the_tool_name_never_the_preview():
    policy = AlertPolicy()
    out = policy.evaluate(snap(waiting=[agent()]), {}, now=100,
                          prompts={"s1": prompt(tool="Bash",
                                                preview='{"command": "rm -rf .build"}')})
    assert out[0].rule == "permission"
    assert out[0].tool == "Bash"
    assert out[0].need == ""
    row = out[0].as_dict()
    assert "rm -rf" not in row["need"]
    assert "rm -rf" not in row["tool"]
    assert "Run shell command" not in row["need"]     # the description


def test_a_relayed_ask_user_question_keeps_its_text_when_the_row_has_it():
    """A channel session's `AskUserQuestion` rides the prompt route; the
    row's own question slot carries the text, and the alert keeps it."""
    policy = AlertPolicy()
    row = agent()
    row["question"] = {"text": "Ship it or iterate?", "options": ["Ship", "Iterate"]}
    out = policy.evaluate(snap(waiting=[row]), {}, now=100,
                          prompts={"s1": prompt(tool="AskUserQuestion")})
    assert out[0].rule == "permission"
    assert out[0].kind == "question"
    assert out[0].tool == "AskUserQuestion"
    assert out[0].need == "Ship it or iterate?"


def test_as_dict_states_need_and_tool_even_when_empty():
    policy = AlertPolicy()
    out = policy.evaluate(snap(running=[agent(signals=[CRIT])]), {}, now=100)
    row = out[0].as_dict()
    assert row["need"] == ""
    assert row["tool"] == ""
    assert out[0].kind == "attention"


def test_the_flat_question_slot_wins_over_the_list_and_the_summary():
    """`_need` mirrors `_kind`'s precedence: the dialog's own text first,
    the summary only under a generic card."""
    policy = AlertPolicy()
    row = agent(questions=[{"text": "From the list?"}])
    row["question"] = {"text": "From the flat slot?"}
    row["last_summary"] = "From the summary?"
    out = policy.evaluate(snap(waiting=[row]), {"s1": dict(GENERIC)}, now=100)
    assert out[0].need == "From the flat slot?"
    row2 = agent(questions=[{"options": []}, {"text": "  Second one?  "}])
    out2 = AlertPolicy().evaluate(snap(waiting=[row2]), {"s1": dict(GENERIC)}, now=100)
    assert out2[0].need == "Second one?"


# ── a finished work report: one quiet banner, Mac only ────────────────────────

_REPORT = ("## Work done\n**Asked:** make it quiet.\n"
           "**Changed:** the spawner now follows the preference.\n"
           "**Verified:** the suite passed.\n"
           "**Unchecked:** Nothing - every check above ran.\n")
_HEADLINE = _wr.parse(_REPORT)["headline"]


def reported(sid="s1", report=_REPORT, **kw):
    return dict(agent(sid=sid, **kw), last_report=report,
                work_report=_wr.parse(report))


def test_the_report_headline_stands_in_for_a_missing_summary():
    policy = AlertPolicy()
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[reported(signals=[WAITING])]), cards, now=100)
    assert [a.rule for a in out] == ["card"]
    assert out[0].body == _HEADLINE


def test_a_summary_still_outranks_the_report_headline():
    policy = AlertPolicy()
    row = dict(reported(signals=[WAITING]), last_summary="Ready for review.")
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[row]), cards, now=100)
    assert out[0].body == "Ready for review."


def test_a_resting_report_raises_one_quiet_finished_banner():
    policy = AlertPolicy()
    out = policy.evaluate(snap(sleeping=[reported()]), {}, now=100)
    assert len(out) == 1
    (alert,) = out
    assert alert.rule == "report" and alert.kind == "finished"
    assert alert.title == "Vex finished"
    assert alert.body == _HEADLINE
    assert alert.need == "" and alert.severity == "info"
    assert alert.actions == ("reveal", "mute")
    # Once per report: still true on the next tick, still said once.
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=105) == []


def test_a_running_row_with_a_report_raises_nothing():
    """A report read before the Stop lands: the row is still `running`, and
    the Stop's card is about to banner with the same headline — one banner,
    not two."""
    policy = AlertPolicy()
    assert policy.evaluate(snap(running=[reported()]), {}, now=100) == []


def test_a_stale_report_after_a_restart_raises_nothing():
    """A fresh policy (the app restarted) meets rows whose transcripts gave
    their reports back; an hour-old report is history, not news."""
    from dark_army_daemon.session_stats import FINISHED_IDLE_GRACE_SECONDS
    policy = AlertPolicy()
    old = dict(reported(), idle_seconds=7200)
    assert policy.evaluate(snap(sleeping=[old]), {}, now=100) == []
    edge = dict(reported("s2"), idle_seconds=FINISHED_IDLE_GRACE_SECONDS)
    assert [a.rule for a in policy.evaluate(snap(sleeping=[edge]), {}, now=100)] \
        == ["report"]


def test_a_report_seen_while_looking_is_not_bannered_later():
    policy = AlertPolicy()
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=100,
                           suppressed={"s1"}) == []
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=105) == []
    policy = AlertPolicy()
    policy.mute("s1")
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=100) == []
    policy.unmute("s1")
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=105) == []


def test_a_dismissed_card_does_not_release_a_second_report_banner():
    """The Stop card carried the headline; once the person dismisses it and
    the row drops to `sleeping`, the same report must stay quiet."""
    policy = AlertPolicy()
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[reported()]), cards, now=100)
    assert [a.rule for a in out] == ["card"]
    after = snap(sleeping=[reported()])
    clear_resolved(policy, after, {})
    assert policy.evaluate(after, {}, now=110) == []


def test_the_report_banner_neither_waits_on_nor_stamps_the_cooldown():
    policy = AlertPolicy()
    # A card ten seconds after a report is the next ask, and must show.
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=100)
    assert "s1" not in policy._last_per_session
    cards = {"s1": {"hook": "Notification", "message": "Permission needed"}}
    out = policy.evaluate(snap(waiting=[agent(signals=[WAITING])]), cards, now=110)
    assert [a.rule for a in out] == ["card"]
    # And a report after a recent card is not swallowed by its window.
    policy2 = AlertPolicy()
    policy2.evaluate(snap(waiting=[agent(signals=[WAITING])]),
                     {"s1": {"hook": "Stop", "message": "?"}}, now=100)
    out = policy2.evaluate(snap(sleeping=[reported()]), {}, now=120)
    assert [a.rule for a in out] == ["report"]


def test_the_report_rule_re_arms_once_the_report_clears():
    policy = AlertPolicy()
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=100)
    # The person's next prompt clears the report; the row works again.
    working = snap(running=[agent()])
    clear_resolved(policy, working, {})
    assert policy.evaluate(working, {}, now=200) == []
    second = snap(sleeping=[reported(report=_REPORT.replace("spawner", "notifier"))])
    clear_resolved(policy, second, {})
    out = policy.evaluate(second, {}, now=300)
    assert [a.rule for a in out] == ["report"]
    assert "notifier" in out[0].body


def test_the_report_key_holds_while_the_row_moves_to_finished():
    policy = AlertPolicy()
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=100)
    later = snap(finished=[dict(reported(), alive=True)])
    clear_resolved(policy, later, {})
    policy.evaluate(later, {}, now=250)
    back = snap(sleeping=[reported()])
    clear_resolved(policy, back, {})
    assert policy.evaluate(back, {}, now=260) == []


@pytest.mark.parametrize("how", ["muted", "suppressed", "panel_focused"])
def test_no_report_banner_while_you_are_looking_or_have_muted(how):
    policy = AlertPolicy()
    kwargs = {}
    if how == "muted":
        policy.mute("s1")
    else:
        kwargs[how] = {"s1"}
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=100, **kwargs) == []


def test_never_on_a_waiting_row_beside_a_card():
    """A Codex `## Work done` alone still asks: the card speaks, with the
    headline as its body, and the report rule stays out of it — one banner."""
    policy = AlertPolicy()
    cards = {"s1": {"hook": "Stop", "message": "Waiting for input"}}
    out = policy.evaluate(snap(waiting=[reported()]), cards, now=100)
    assert [a.rule for a in out] == ["card"]
    assert out[0].body == _HEADLINE
    assert policy.evaluate(snap(waiting=[reported()]), cards, now=105) == []


def test_never_beside_a_card_or_a_prompt_on_a_resting_row():
    policy = AlertPolicy()
    cards = {"s1": {"hook": "Notification", "message": "idle"}}
    assert policy.evaluate(snap(sleeping=[reported()]), cards, now=100) == []
    prompts = {"s1": {"request_id": "r1", "tool_name": "Bash"}}
    out = policy.evaluate(snap(sleeping=[reported()]), {}, now=100, prompts=prompts)
    assert [a.rule for a in out] == ["permission"]
    assert policy.evaluate(snap(sleeping=[reported()]), {}, now=101,
                           prompts=prompts) == []


def test_no_report_banner_without_a_parsed_report_or_from_other_buckets():
    policy = AlertPolicy()
    plain = dict(agent(), last_report=_REPORT)            # an older enrich
    assert policy.evaluate(snap(sleeping=[plain]), {}, now=100) == []
    assert policy.evaluate(snap(finished=[dict(reported(), alive=True)],
                                abandoned=[reported("s2")]), {}, now=100) == []


def test_an_unlabelled_report_still_says_something():
    policy = AlertPolicy()
    out = policy.evaluate(snap(sleeping=[reported(report="## Work done\nall fine")]),
                          {}, now=100)
    assert out[0].body == "Wrote a work report"


def test_a_report_from_before_a_restart_is_not_announced_again():
    """`_fired` lives in memory, and a restored row's quiet stamp is its old
    `last_event`: a report that went quiet before this daemon started was
    news for the previous run and is seeded as delivered."""
    started = 1000.0
    policy = AlertPolicy(started_at=started)
    restored = dict(reported(), quiet_since=started - 30, idle_seconds=30)
    assert policy.evaluate(snap(sleeping=[restored]), {}, now=started + 1) == []
    assert ("s1", "report") in policy._fired
    # A row that goes quiet after the start still banners, once.
    fresh = dict(reported("s2"), quiet_since=started + 5, idle_seconds=2)
    out = policy.evaluate(snap(sleeping=[restored, fresh]), {}, now=started + 7)
    assert [(a.session_id, a.rule) for a in out] == [("s2", "report")]
    assert policy.evaluate(snap(sleeping=[restored, fresh]), {}, now=started + 9) == []


def test_an_unknown_start_seeds_nothing():
    policy = AlertPolicy()
    row = dict(reported(), quiet_since=5.0, idle_seconds=3)
    assert [a.rule for a in policy.evaluate(snap(sleeping=[row]), {}, now=8.0)] \
        == ["report"]
