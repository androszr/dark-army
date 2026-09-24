"""A multi-question `AskUserQuestion`, end to end.

The tool takes one to four questions per call; Dark Army used to keep only the
first and silently throw the rest away, and answering that first from the
panel or the phone left the others standing on the terminal. These tests pin
the whole route: both parsers emit the full list beside the unchanged flat
`question`, carriage and persistence round-trip it, the batch verb
(`BobDaemon.answer_questions`) types one complete ordered burst behind the
same guard ladder as the singular verb, both API doors parse the comma-joined
choices defensively, and the phone's Swift sources carry the pick-then-send
shape (a Python lint, `test_phone_needs_you.py`'s pattern — `ios/` has no
test target by explicit decision).
"""

import ast
import asyncio
import json
from pathlib import Path

import pytest

from dark_army_daemon import session_stats as ss
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.protocol import hook_payload_to_daemon_message
from dark_army_daemon.session_store import (
    load_pending_questions,
    save_sessions,
)
from dark_army_menubar.hooks import NOTIFY_SCRIPT

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"


def _tool_input(count=3):
    """A real-shaped AskUserQuestion tool_input with `count` questions."""
    headers = ["Database", "Approach", "Timing", "Scope", "Extra"]
    return {"questions": [
        {
            "question": f"Question {n}?",
            "header": headers[n],
            "multiSelect": False,
            "options": [
                {"label": f"Q{n} option {i}", "description": "d" * 200}
                for i in range(3)
            ],
        }
        for n in range(count)
    ]}


# --- Parsers -----------------------------------------------------------------


def test_three_questions_become_a_list_of_three():
    every = ss.questions_from_tool_input(_tool_input(3), "tu_1")
    assert len(every) == 3
    assert [q["index"] for q in every] == [0, 1, 2]
    # One dialog, one tool call, one shared id.
    assert {q["id"] for q in every} == {"tu_1"}
    assert every[1]["text"] == "Question 1?"
    assert every[1]["header"] == "Approach"
    assert every[1]["options"] == ["Q1 option 0", "Q1 option 1", "Q1 option 2"]
    assert every[1]["details"] == ["d" * ss.MAX_DETAIL_CHARS] * 3


def test_the_list_is_capped_at_max_questions():
    every = ss.questions_from_tool_input(_tool_input(5), "tu_1")
    assert len(every) == ss.MAX_QUESTIONS == 4


def test_each_question_is_capped_like_the_flat_one():
    tin = {"questions": [
        {"question": "q" * 900, "options": [{"label": "o" * 200}] * 6},
        {"question": "Second?", "options": [{"label": "B"}]},
    ]}
    every = ss.questions_from_tool_input(tin, "tu_1")
    assert len(every[0]["text"]) == ss.MAX_QUESTION_CHARS
    assert len(every[0]["options"]) == ss.MAX_OPTIONS
    assert len(every[0]["options"][0]) == ss.MAX_OPTION_CHARS


def test_the_flat_question_is_still_the_first_with_todays_keys():
    """The flat key is never renamed and never re-typed — an older panel or
    phone decodes it exactly as before this change. `multi_select` (5 Sep
    2026) is the one key it has grown, tolerantly: both surfaces decode an
    absent one as false, and a stranger key is ignored by construction."""
    flat = ss.question_from_tool_input(_tool_input(3), "tu_1")
    assert set(flat) == {"text", "options", "details", "header", "id",
                         "multi_select", "has_preview"}
    assert flat["text"] == "Question 0?"
    assert ss.question_from_tool_input({"questions": []}, "tu_1") == {}


def test_notify_script_builds_the_same_message_pair():
    """The embedded handler and the daemon-side converter must attach the
    identical `question` + `questions` pair — the same contract
    `test_notify_script.py` cross-checks for every other event."""
    ns = {"__name__": "dark_army_notify_under_test"}
    exec(compile(NOTIFY_SCRIPT, "dark-army-notify", "exec"), ns)
    # The pid walk shells out to ps; this test is about the payload alone.
    ns["_find_session_pid"] = lambda: (0, "claude")
    # And the memoised walk: in-process it would write its memo under the
    # real ~/.dark-army/hook-pids (and create that folder).
    ns["_session_pid_cached"] = lambda session_id, prefer_grok: (0, "claude")
    hook = {
        "hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/x/proj",
        "tool_name": "AskUserQuestion", "tool_use_id": "tu_9",
        "tool_input": _tool_input(3),
    }
    script_msg = ns["hook_to_message"](hook)
    proto_msg = hook_payload_to_daemon_message(hook)
    assert script_msg["questions"] == proto_msg["questions"]
    assert script_msg["question"] == proto_msg["question"]
    assert len(script_msg["questions"]) == 3
    # The flat sibling stays exactly the first question, index stripped.
    assert "index" not in script_msg["question"]
    assert script_msg["question"]["id"] == "tu_9"


def test_a_real_two_sentence_question_survives_whole():
    """The regression this cap was raised for.

    Both of these are verbatim from a live `/ship` interview, and both were
    mutilated at 160: the first arrived ending "...not the", the second ending
    "Which do you w" — under four intact option buttons, so nothing on screen
    admitted the question was incomplete.
    """
    first = ("Massive won't sell us the index levels on this plan. The ETFs "
             "that track them (SPY, QQQ, DIA) work fine \u2014 but they show the "
             "fund's price (SPY is 765.16, not the S&P's 7,666.60), while the "
             "percentage move is near-identical. How should the tiles read?")
    second = ("The screenshot's tiles carry an intraday sparkline with a "
              "dotted previous-close line. Each one costs a 5-minute-bar "
              "fetch per symbol per refresh. Which do you want?")
    assert len(first) > 160 and len(second) > 160
    tin = {"questions": [{"question": q, "options": [{"label": "A"}]}
                         for q in (first, second)]}
    every = ss.questions_from_tool_input(tin, "tu_1")
    assert [q["text"] for q in every] == [first, second]


def test_an_over_cap_question_says_so_with_an_ellipsis():
    """A cut that leaves no trace reads as a finished sentence. When the cap
    does bite, the last character has to admit it."""
    every = ss.questions_from_tool_input(
        {"questions": [{"question": "q" * 900,
                        "options": [{"label": "o" * 200}], "header": "h" * 200}]},
        "tu_1")
    assert len(every[0]["text"]) == ss.MAX_QUESTION_CHARS
    assert every[0]["text"].endswith("\u2026")
    assert every[0]["options"][0].endswith("\u2026")
    assert every[0]["header"].endswith("\u2026")
    # And an exactly-at-cap question is not decorated with one.
    exact = ss.questions_from_tool_input(
        {"questions": [{"question": "q" * ss.MAX_QUESTION_CHARS}]}, "tu_1")
    assert exact[0]["text"] == "q" * ss.MAX_QUESTION_CHARS


def test_clip_never_exceeds_its_cap():
    for cap in (0, 1, 2, 5, 80, 600):
        for length in (0, 1, 4, 79, 601, 5000):
            assert len(ss.clip("x" * length, cap)) <= cap


def test_notify_script_stays_39_grammar():
    """The installed copy runs under the macOS system python3 (3.9)."""
    ast.parse(NOTIFY_SCRIPT, feature_version=(3, 9))


def test_option_descriptions_ride_as_details():
    """Grok interviews put the reason in `description`. Labels alone lost
    it; `preview` is a different layout and is not this field."""
    tin = {"questions": [{
        "question": "Which store?",
        "options": [
            {"label": "Postgres", "description": "Keep the current store."},
            {"label": "SQLite", "preview": "not a description"},
        ],
    }]}
    every = ss.questions_from_tool_input(tin, "tu_1")
    assert every[0]["details"] == ["Keep the current store.", ""]
    over = ss.questions_from_tool_input(
        {"questions": [{"question": "Q?", "options": [
            {"label": "A", "description": "d" * 400},
        ]}]}, "tu_1")
    assert len(over[0]["details"][0]) == ss.MAX_DETAIL_CHARS
    assert over[0]["details"][0].endswith("\u2026")
    ns = {"__name__": "dark_army_notify_under_test"}
    exec(compile(NOTIFY_SCRIPT, "dark-army-notify", "exec"), ns)
    assert ns["MAX_DETAIL_CHARS"] == ss.MAX_DETAIL_CHARS


# --- The clamp seam ----------------------------------------------------------


def test_clamp_wraps_a_flat_only_message():
    """An older installed hook keeps sending only the flat shape until the
    app reinstalls it; the daemon wraps it into a one-element list."""
    msg = {"event": "tool_use", "tool_name": "AskUserQuestion",
           "question": {"text": "Which?", "options": ["A", "B"],
                        "header": "H", "id": "tu_1"}}
    every = ss.clamp_questions(msg)
    assert every == [{"text": "Which?", "options": ["A", "B"], "details": ["", ""],
                      "header": "H",
                      "id": "tu_1", "index": 0, "multi_select": False,
                      "has_preview": False}]


def test_clamp_recuts_oversize_text_and_options():
    msg = {"questions": [
        {"text": "q" * 900, "options": ["o" * 200, "B"], "header": "H",
         "id": "tu_1", "index": 0},
    ]}
    every = ss.clamp_questions(msg)
    assert len(every[0]["text"]) == ss.MAX_QUESTION_CHARS
    assert len(every[0]["options"][0]) == ss.MAX_OPTION_CHARS


def test_clamp_tolerates_garbage():
    for raw in (None, 7, "no", [], {}, {"questions": "nope"},
                {"questions": [1, 2]}, {"question": "flat-but-not-a-dict"},
                {"questions": [{"text": "   "}]}):
        assert ss.clamp_questions(raw) == []


def test_clamp_restamps_a_silly_index():
    every = ss.clamp_questions({"questions": [
        {"text": "A?", "options": ["A"], "id": "t", "index": 99},
        {"text": "B?", "options": ["B"], "id": "t", "index": True},
    ]})
    assert [q["index"] for q in every] == [0, 1]


# --- Carriage: tracking, persistence, restore --------------------------------


def _ask_msg(count=3):
    msg = hook_payload_to_daemon_message({
        "hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/x/p",
        "tool_name": "AskUserQuestion", "tool_use_id": "tu_1",
        "tool_input": _tool_input(count),
    })
    return msg


def test_tracked_reading_holds_flat_and_list():
    d = BobDaemon()
    d._track_pending_question("tool_use", "s1", _ask_msg(3))
    held = d._pending_questions["s1"]
    assert held["text"] == "Question 0?"
    assert len(held["questions"]) == 3
    # The flat keys stay top-level so a downgraded daemon's clamp_question
    # rebuilds exactly the keys it knows and drops the rest.
    assert {"text", "options", "header", "id"} <= set(held)


def test_the_permission_companion_still_does_not_pop_it():
    """The fix for the measured live bug (the nameless permission companion
    popping the question 50ms after it was set) survives the new shape."""
    d = BobDaemon()
    d._track_pending_question("tool_use", "s1", _ask_msg(3))
    d._track_pending_question("permission", "s1",
                              {"tool_name": "AskUserQuestion"})
    d._track_pending_question("permission", "s1", {})  # the nameless one
    assert "s1" in d._pending_questions
    d._track_pending_question("tool_done", "s1", {"tool_name": "AskUserQuestion"})
    assert "s1" not in d._pending_questions


def test_the_envelope_round_trips_the_list(tmp_path):
    d = BobDaemon()
    d._track_pending_question("tool_use", "s1", _ask_msg(3))
    path = tmp_path / "sessions.json"
    save_sessions({"s1": {"state": "waiting", "last_event": 1.0}}, path,
                  d._pending_questions)

    back = BobDaemon()
    back._session_states["s1"] = {"state": "waiting", "last_event": 1.0}
    back._restore_pending_questions(load_pending_questions(path))
    held = back._pending_questions["s1"]
    assert len(held["questions"]) == 3
    assert held["questions"][2]["text"] == "Question 2?"
    assert held["text"] == "Question 0?"


def test_an_old_flat_only_envelope_restores_as_a_one_list(tmp_path):
    """What is on disk was written by whichever build ran last — a downgrade
    then an upgrade must not blank the row."""
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        "sessions": {"s1": {"state": "waiting", "last_event": 1.0}},
        "pending_questions": {"s1": {"text": "Which?", "options": ["A"],
                                     "header": "", "id": "tu_1"}},
    }))
    d = BobDaemon()
    d._session_states["s1"] = {"state": "waiting", "last_event": 1.0}
    d._restore_pending_questions(load_pending_questions(path))
    held = d._pending_questions["s1"]
    assert held["questions"] == [{"text": "Which?", "options": ["A"],
                                  "details": [""],
                                  "header": "", "id": "tu_1", "index": 0,
                                  "multi_select": False,
                                  "has_preview": False}]


# --- The snapshot ------------------------------------------------------------

SID = "00000000-0000-0000-0000-000000000000"


def _row(daemon):
    stub = {"session_id": SID, "project": "proj", "state": "waiting",
            "subagents": 0, "subagent_ids": [], "_category": "waiting"}
    return daemon._enrich_agent_stubs([stub])["waiting"][0]


def test_enrichment_publishes_the_list_beside_the_flat():
    d = BobDaemon()
    msg = _ask_msg(3)
    msg["session_id"] = SID
    d._track_pending_question("tool_use", SID, msg)
    entry = _row(d)
    assert entry["question"]["text"] == "Question 0?"
    # The flat key keeps exactly its old shape — the list is a sibling.
    assert "questions" not in entry["question"]
    assert len(entry["questions"]) == 3
    # And the freshest reading is mirrored whole for the answer verbs.
    assert len(d._questions[SID]["questions"]) == 3


def test_enrichment_wraps_a_flat_only_reading():
    d = BobDaemon()
    d._pending_questions[SID] = {"text": "Which?", "options": ["A", "B"],
                                 "header": "", "id": "tu_1"}
    entry = _row(d)
    assert entry["question"]["text"] == "Which?"
    assert entry["questions"] == [{"text": "Which?", "options": ["A", "B"],
                                   "header": "", "id": "tu_1", "index": 0}]


# --- Answering: the guard ladder and the burst -------------------------------


def _multi_reading():
    shared = {"header": "", "id": "tu_1"}
    return {
        "text": "Q0?", "options": ["A", "B", "C"], "header": "", "id": "tu_1",
        "questions": [
            dict(shared, text="Q0?", options=["A", "B", "C"], index=0),
            dict(shared, text="Q1?", options=["A", "B"], index=1),
            dict(shared, text="Q2?", options=["A", "B", "C"], index=2),
        ],
    }


def _asked_daemon(monkeypatch, timeline, state="waiting", fail_on_send=None,
                  reading=None):
    """A daemon holding a multi-question dialog for s1, with a terminal that
    records every keystroke and every gap into one timeline."""
    d = BobDaemon()
    d._questions["s1"] = reading if reading is not None else _multi_reading()
    if state is not None:
        d._session_states["s1"] = {"state": state, "last_event": 0.0,
                                   "pid": 4242}
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, st: 4242)

    sends = {"n": 0}

    async def _send(pid, tty, text, newline=True):
        sends["n"] += 1
        if fail_on_send is not None and sends["n"] >= fail_on_send:
            return {"matched": True, "sent": False}
        timeline.append(("send", text))
        return {"matched": True, "sent": True, "terminalName": "zsh"}

    monkeypatch.setattr("dark_army_daemon.vscode_reveal.send_text", _send)

    real_sleep = asyncio.sleep

    async def _sleep(seconds):
        timeline.append(("gap", seconds))
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", _sleep)
    return d


def test_a_three_question_dialog_is_one_ordered_burst(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 0, 2]))
    assert ok, detail
    sends = [t for kind, t in timeline if kind == "send"]
    # Digits are 1-based on the dialog; the digit selects and advances.
    # The fourth send is the Enter that accepts the summary screen the
    # dialog then shows — without it the answers are typed but never submitted.
    assert sends == ["2", "1", "3", "\r"]
    # A recorded gap between every consecutive send — four sends, three gaps,
    # never two sends back to back.
    kinds = [kind for kind, _ in timeline]
    assert kinds == ["send", "gap"] * 3 + ["send"]
    # The in-flight slot is released with the burst.
    assert "s1" not in d._answering


def test_a_one_question_dialog_gets_no_confirming_enter(monkeypatch):
    """The single-question dialog submits on the digit and shows no
    summary screen, so a spare Enter there would land on an idle input line
    and be read as a prompt."""
    timeline = []
    reading = {"text": "Which?", "options": ["A", "B"], "header": "",
               "id": "tu_1",
               "questions": [{"text": "Which?", "options": ["A", "B"],
                              "header": "", "id": "tu_1", "index": 0}]}
    d = _asked_daemon(monkeypatch, timeline, reading=reading)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1]))
    assert ok, detail
    assert [t for kind, t in timeline if kind == "send"] == ["2"]


def test_a_failed_confirmation_says_the_answers_are_in(monkeypatch):
    """The fourth send is the confirmation. Losing it is not a half-answer:
    every choice is entered and the summary is standing, and the sentence has
    to say so rather than send the human back to re-answer."""
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, fail_on_send=4)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 0, 2]))
    assert not ok
    assert detail == ("All 3 answers were entered, but the confirmation was "
                      "not sent — confirm it in the terminal.")
    assert "s1" not in d._answering


def test_a_wrong_length_burst_is_refused_whole(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 0]))
    assert not ok
    assert "3 questions" in detail
    assert timeline == []


def test_an_id_mismatch_is_refused(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_OLD", [1, 0, 2]))
    assert not ok
    assert detail == "That question has already been answered."
    assert timeline == []


def test_an_index_out_of_one_questions_range_refuses_before_any_keystroke(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline)
    # Question 2 (index 1) has two options; 2 is out of *its* range even
    # though questions 1 and 3 could take it. Nothing may be typed first.
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 2, 2]))
    assert not ok
    assert detail == "No such option for question 2."
    assert timeline == []


def test_an_index_past_the_digit_keys_is_refused(monkeypatch):
    timeline = []
    reading = _multi_reading()
    reading["questions"][0]["options"] = [f"o{i}" for i in range(12)]
    d = _asked_daemon(monkeypatch, timeline, reading=reading)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [10, 0, 1]))
    assert not ok
    assert detail == "No such option for question 1."
    assert timeline == []


def test_a_permission_prompt_owns_the_input_line(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline)
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {"s1": object()})
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 0, 2]))
    assert not ok
    assert "permission prompt" in detail
    assert timeline == []


def test_a_hookless_grok_question_can_be_answered(monkeypatch):
    """Grok can be waiting on a question with no hook-stamped state."""
    timeline = []
    reading = {"text": "Which?", "options": ["A", "B"], "header": "",
               "id": "tu_1",
               "questions": [{"text": "Which?", "options": ["A", "B"],
                              "header": "", "id": "tu_1", "index": 0}]}
    d = _asked_daemon(monkeypatch, timeline, state=None, reading=reading)
    from dark_army_daemon.grok_roster import GrokRecord
    d._grok_records["s1"] = GrokRecord(session_id="s1", pid=4242, cwd="/tmp/p")
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1]))
    assert ok, detail
    assert [t for kind, t in timeline if kind == "send"] == ["2"]


@pytest.mark.parametrize("state", ["working", "idle", None])
def test_a_session_that_moved_on_is_refused(monkeypatch, state):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, state=state)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 0, 2]))
    assert not ok
    assert detail == "That session is no longer waiting on a question."
    assert timeline == []


def test_a_second_burst_is_refused_while_one_is_typing(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline)
    d._answering.add("s1")
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 0, 2]))
    assert not ok
    assert "wait for it to finish" in detail
    assert timeline == []


def test_a_mid_burst_failure_says_how_far_it_got(monkeypatch):
    """Dark Army types blind; a partial outcome must be loud. The third send is
    question 3's digit — questions 1 and 2 landed, so the count says 2 of 3
    and the sentence sends the human to the terminal for the rest."""
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, fail_on_send=3)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 0, 2]))
    assert not ok
    assert detail.startswith("Answered 2 of 3")
    assert "finish the rest in the terminal" in detail
    assert "s1" not in d._answering


def test_the_singular_verb_refuses_a_multi_question_dialog(monkeypatch):
    """An out-of-date panel or phone must get a sentence, never a silent
    half-answer that leaves two questions standing."""
    timeline = []
    d = _asked_daemon(monkeypatch, timeline)
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert not ok
    assert detail == ("This dialog asks 3 questions — answer them together "
                      "(update Dark Army's panel or phone if you see no way to).")
    assert timeline == []


def test_the_singular_verb_types_the_digit_alone(monkeypatch):
    timeline = []
    reading = {"text": "Which?", "options": ["A", "B"], "header": "",
               "id": "tu_1",
               "questions": [{"text": "Which?", "options": ["A", "B"],
                              "header": "", "id": "tu_1", "index": 0}]}
    d = _asked_daemon(monkeypatch, timeline, reading=reading)
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert ok, detail
    assert [t for kind, t in timeline if kind == "send"] == ["2"]


def test_the_batch_verb_takes_a_one_question_dialog_too(monkeypatch):
    """A new client may use one verb for every count; the length rule still
    binds it to exactly one answer."""
    timeline = []
    reading = {"text": "Which?", "options": ["A", "B"], "header": "",
               "id": "tu_1",
               "questions": [{"text": "Which?", "options": ["A", "B"],
                              "header": "", "id": "tu_1", "index": 0}]}
    d = _asked_daemon(monkeypatch, timeline, reading=reading)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [0]))
    assert ok, detail
    assert [t for kind, t in timeline if kind == "send"] == ["1"]


def test_a_two_question_pick_one_dialog_types_two_digits_and_one_enter(
        monkeypatch):
    """Observed 2026-09-05 on Claude Code 2.1.261: the Enter between digits
    landed on question 2 and chose its first option; the CLI then
    user-rejected the interview. The digit selects and advances, so the
    burst is two digits and one confirming Enter, never an Enter in between."""
    timeline = []
    reading = {
        "text": "Q0?", "options": ["A", "B"], "header": "", "id": "tu_1",
        "questions": [
            {"text": "Q0?", "options": ["A", "B"], "header": "",
             "id": "tu_1", "index": 0},
            {"text": "Q1?", "options": ["A", "B"], "header": "",
             "id": "tu_1", "index": 1},
        ],
    }
    d = _asked_daemon(monkeypatch, timeline, reading=reading)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1, 1]))
    assert ok, detail
    sends = [t for kind, t in timeline if kind == "send"]
    assert sends == ["2", "2", "\r"]
    assert "\r" not in sends[:-1]
    kinds = [kind for kind, _ in timeline]
    assert kinds == ["send", "gap", "send", "gap", "send"]


# --- The two doors -----------------------------------------------------------


def test_answer_questions_is_a_chosen_lan_action():
    from dark_army_daemon.api_server import ApiServer

    assert "answer_questions" in ApiServer.LAN_ACTIONS


def test_the_comma_joined_choices_are_parsed_defensively():
    from dark_army_daemon.api_server import ApiServer

    parse = ApiServer._parse_option_indexes
    # One group per question; a plain segment is a group of one (the `+`
    # grammar for several picks is pinned in test_multi_select_question.py).
    assert parse("1,0,2") == [[1], [0], [2]]
    assert parse(" 1 , 0 ") == [[1], [0]]
    assert parse("2") == [[2]]
    for bad in ("1,x,2", "", "  ", ",", "1,,2", "-1,0", "1;2", None, 3,
                ["1", "2"]):
        assert parse(bad) is None, bad


def test_the_loopback_intercept_parses_and_refuses(monkeypatch):
    from dark_army_daemon import api_server as api_mod

    srv = api_mod.ApiServer(BobDaemon(), port=0)
    monkeypatch.setattr(srv, "_authorised", lambda request: True)

    def request(payload):
        return api_mod._Request("POST", "/api/action", "", {},
                                json.dumps(payload).encode())

    good = srv._answer_questions_request(request({
        "action": "answer_questions", "session_id": "s1",
        "question_id": "tu_1", "option_indexes": "1,0,2"}))
    assert good == ("s1", "tu_1", [[1], [0], [2]])
    # Malformed choices are a None — a 400 out of `_route` — never a raise.
    for indexes in ("1,x,2", "", None):
        assert srv._answer_questions_request(request({
            "action": "answer_questions", "session_id": "s1",
            "question_id": "tu_1", "option_indexes": indexes})) is None
    # And the other action still falls through to its own intercept.
    assert srv._answer_questions_request(request({
        "action": "answer_question", "session_id": "s1",
        "option_index": "1"})) is None


def test_the_lan_branch_refuses_malformed_choices_with_400():
    from dark_army_daemon.api_server import ApiServer

    srv = ApiServer(BobDaemon(), port=0)
    status, _, _ = asyncio.run(srv._lan_run("answer_questions", {
        "session_id": "s1", "question_id": "tu_1", "option_indexes": "1,x,2"}))
    assert status == 400


def test_the_lan_branch_reaches_the_guarded_coroutine():
    from dark_army_daemon.api_server import ApiServer

    calls = []

    class _Daemon:
        async def answer_questions(self, session_id, question_id, indexes):
            calls.append((session_id, question_id, indexes))
            return True, ""

    srv = ApiServer(_Daemon(), port=0)
    status, _, _ = asyncio.run(srv._lan_run("answer_questions", {
        "session_id": "s1", "question_id": "tu_1", "option_indexes": "1,0,2"}))
    assert status == 200
    assert calls == [("s1", "tu_1", [[1], [0], [2]])]


# --- The phone, by lint (`test_phone_needs_you.py`'s pattern) ----------------


def _read(name: str) -> str:
    path = PHONE / name
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_the_phone_decodes_the_questions_key_tolerantly():
    source = _read("Models.swift")
    assert "case questions" in source
    assert "questions = c.value(.questions, [])" in source, (
        "an absent key from an older Mac must decode as [], never throw")
    assert "var questionList" in source, "the wrap for flat-only daemons"


def test_the_phone_names_the_batch_verb_once():
    source = _read("Actions.swift")
    lines = [l for l in source.splitlines() if "answer_questions" in l]
    assert lines == ['    static let answerQuestions = "answer_questions"']


def test_the_answer_box_has_the_batch_send_and_keeps_the_reply_path():
    source = _read("AnswerBox.swift")
    assert "PhoneActions.answerQuestions" in source
    assert "option_indexes" in source
    # At least one pick per question behind a single send that stays off
    # until the dialog is fully answered.
    assert ".disabled(!complete)" in source
    assert "ANSWER ALL" in source
    # And that send is a two-press: the picks are read back under a
    # "send these N answers?" line before anything is typed into the
    # terminal, the same beat the terminal's own summary screen is.
    assert "confirming" in source
    assert "send these \\(questions.count) answers?" in source
    assert "YES, SEND" in source
    assert "CHANGE" in source
    # And the bob-actions/reply mechanism is untouched beside it.
    assert "PhoneActions.reply" in source
    assert "replyButtons" in source
    # The read-only shape still says why there is no button.
    assert "cannot type into this terminal" in source


def test_the_panel_reads_the_picks_back_before_sending():
    """Desktop and phone answer the same dialog, so the beat is the same on
    both: ANSWER ALL reads the picks back, and a second press sends."""
    source = (ROOT / "panel" / "Sources" / "BobPanel"
              / "ProcessTable.swift").read_text()
    assert "confirmSummary" in source
    assert "SEND THESE \\(questions.count) ANSWERS?" in source
    assert "YES, SEND" in source
    assert 'Button("CHANGE")' in source
    # The send itself is still the batch verb, unchanged.
    assert "actions.answerQuestions(agent, choices: ordered" in source


def test_a_singular_press_is_refused_while_a_burst_is_typing(monkeypatch):
    """The batch verb's one-burst-per-session rule holds for both verbs
    (2026-09-01): a singular press landing during ~1s of in-flight keystrokes
    used to interleave its digit and Enter into the middle of the batch's
    answer on the same input line."""
    timeline = []
    reading = {"text": "Which?", "options": ["A", "B"], "header": "",
               "id": "tu_1",
               "questions": [{"text": "Which?", "options": ["A", "B"],
                              "header": "", "id": "tu_1", "index": 0}]}
    d = _asked_daemon(monkeypatch, timeline, reading=reading)
    d._answering.add("s1")
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert not ok
    assert "wait for it to finish" in detail
    assert timeline == []


def test_the_singular_verb_holds_the_answering_slot_while_typing(monkeypatch):
    """The slot is held across the burst — a one-question pick-one is one
    send, and a multi-select still sleeps between sends, so a second press
    in that gap must see the slot held."""
    timeline = []
    reading = {"text": "Which?", "options": ["A", "B"], "header": "",
               "id": "tu_1",
               "questions": [{"text": "Which?", "options": ["A", "B"],
                              "header": "", "id": "tu_1", "index": 0}]}
    d = _asked_daemon(monkeypatch, timeline, reading=reading)
    held_during: list = []

    real_send = None
    import dark_army_daemon.vscode_reveal as vr
    real_send = vr.send_text

    async def spying(pid, tty, text, newline=True):
        held_during.append("s1" in d._answering)
        return await real_send(pid, tty, text, newline=newline)

    monkeypatch.setattr("dark_army_daemon.vscode_reveal.send_text", spying)
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert ok, detail
    assert held_during == [True], \
        "the slot must be held across the burst"
    assert "s1" not in d._answering, "and released afterwards"
