"""A **multi-select** `AskUserQuestion`, end to end (5 Sep 2026).

`tool_input.questions[i].multiSelect` used to be dropped by every builder, so
both surfaces drew a several-answers question as pick-one, and the burst the
daemon typed — digit then Enter — assumed the pick-one widget: on the
multi-select widget it toggled option N, toggled option 1, submitted nothing,
and the dialog stayed up. These tests pin the fix at every rung: the hook
script and the module parser emit `multi_select` identically, the clamp
carries it and defaults it false, the envelope and the snapshot round-trip
it, the batch verb types the multi-select sequence (`MULTI_SELECT_SUBMIT_KEYS`)
for a flagged question and refuses a group that does not fit the question
before any keystroke, the singular verb routes a flagged one-question dialog
through the same burst, the wire's `+` grammar is parsed defensively, and the
panel's and phone's Swift sources carry the tick-box shape (a Python lint —
`ios/` has no test target by explicit decision).

`test_multi_question.py`'s `_asked_daemon` helper is copied rather than
imported across test files.
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import session_stats as ss
from dark_army_daemon.daemon import (
    MULTI_SELECT_SUBMIT_KEYS, PICK_ONE_SUBMIT_KEYS, PREVIEW_PICK_ONE_SUBMIT_KEYS,
    BobDaemon, _dialog_has_summary)
from dark_army_daemon.protocol import hook_payload_to_daemon_message
from dark_army_daemon.session_store import (
    load_pending_questions,
    save_sessions,
)
from dark_army_menubar.hooks import NOTIFY_SCRIPT

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"
DAEMON_PY = ROOT / "host" / "dark_army_daemon" / "daemon.py"


def _tool_input(flags=(True, False, None)):
    """A real-shaped tool_input, one question per flag; None omits the key."""
    out = []
    for n, flag in enumerate(flags):
        q = {"question": f"Question {n}?", "header": f"H{n}",
             "options": [{"label": f"Q{n} option {i}", "description": "d"}
                         for i in range(3)]}
        if flag is not None:
            q["multiSelect"] = flag
        out.append(q)
    return {"questions": out}


def _hook(tool_input, tool_use_id="tu_1", session_id="s1"):
    return {"hook_event_name": "PreToolUse", "session_id": session_id,
            "cwd": "/x/proj", "tool_name": "AskUserQuestion",
            "tool_use_id": tool_use_id, "tool_input": tool_input}


def _script_ns():
    ns = {"__name__": "dark_army_notify_under_test"}
    exec(compile(NOTIFY_SCRIPT, "dark-army-notify", "exec"), ns)
    ns["_find_session_pid"] = lambda: (0, "claude")
    # And the memoised walk: in-process it would write its memo under the
    # real ~/.dark-army/hook-pids (and create that folder).
    ns["_session_pid_cached"] = lambda session_id, prefer_grok: (0, "claude")
    return ns


# --- The flag leaves the hook ------------------------------------------------


def test_the_hook_and_the_module_emit_the_flag_identically():
    hook = _hook(_tool_input((True, False, None)))
    script_msg = _script_ns()["hook_to_message"](hook)
    proto_msg = hook_payload_to_daemon_message(hook)
    assert script_msg["questions"] == proto_msg["questions"]
    assert script_msg["question"] == proto_msg["question"]
    assert [q["multi_select"] for q in proto_msg["questions"]] == \
        [True, False, False]
    # The flat sibling is the first question, so it carries the flag too.
    assert proto_msg["question"]["multi_select"] is True
    assert "index" not in proto_msg["question"]


def test_grok_snake_multi_select_is_honoured():
    """Grok's tool writes `multi_select`; Claude writes `multiSelect`. Either
    real True is a tick-box question, so Dark Army types the multi-select burst."""
    tin = {"questions": [{"question": "Q?", "multi_select": True,
                          "options": [{"label": "A"}, {"label": "B"}]}]}
    every = ss.questions_from_tool_input(tin, "tu_1")
    assert every[0]["multi_select"] is True
    script = _script_ns()["hook_to_message"](_hook(tin))
    assert script["questions"][0]["multi_select"] is True


@pytest.mark.parametrize("value", ["yes", 1, None, "true", [True]])
def test_a_non_bool_multiselect_is_false(value):
    """Strict `is True`: the widget is chosen by the client on a real bool,
    and a truthy string must not make Dark Army type the multi-select sequence at
    a pick-one widget."""
    tin = {"questions": [{"question": "Q?", "multiSelect": value,
                          "options": [{"label": "A"}]}]}
    every = ss.questions_from_tool_input(tin, "tu_1")
    assert every[0]["multi_select"] is False
    script = _script_ns()["hook_to_message"](_hook(tin))
    assert script["questions"][0]["multi_select"] is False


def test_clamp_carries_the_flag_and_defaults_it_false():
    with_flag = ss.clamp_question({"text": "Q?", "options": ["A", "B"],
                                   "header": "", "id": "t",
                                   "multi_select": True})
    assert with_flag["multi_select"] is True
    without = ss.clamp_question({"text": "Q?", "options": ["A", "B"],
                                 "header": "", "id": "t"})
    assert without["multi_select"] is False
    # And a stringly flag from a hand-edited envelope reads as pick-one.
    stringly = ss.clamp_question({"text": "Q?", "options": ["A"],
                                  "multi_select": "true"})
    assert stringly["multi_select"] is False


def test_the_envelope_round_trips_the_flag(tmp_path):
    d = BobDaemon()
    d._track_pending_question(
        "tool_use", "s1",
        hook_payload_to_daemon_message(_hook(_tool_input((True, False)))))
    path = tmp_path / "sessions.json"
    save_sessions({"s1": {"state": "waiting", "last_event": 1.0}}, path,
                  d._pending_questions)

    back = BobDaemon()
    back._session_states["s1"] = {"state": "waiting", "last_event": 1.0}
    back._restore_pending_questions(load_pending_questions(path))
    held = back._pending_questions["s1"]
    assert held["multi_select"] is True
    assert held["questions"][0]["multi_select"] is True
    assert held["questions"][1]["multi_select"] is False


SID = "00000000-0000-0000-0000-000000000000"


def _row(daemon):
    stub = {"session_id": SID, "project": "proj", "state": "waiting",
            "subagents": 0, "subagent_ids": [], "_category": "waiting"}
    return daemon._enrich_agent_stubs([stub])["waiting"][0]


def test_enrichment_publishes_the_flag_on_flat_and_list():
    d = BobDaemon()
    msg = hook_payload_to_daemon_message(
        _hook(_tool_input((True, False)), session_id=SID))
    d._track_pending_question("tool_use", SID, msg)
    entry = _row(d)
    assert entry["question"]["multi_select"] is True
    assert [q["multi_select"] for q in entry["questions"]] == [True, False]
    assert d._questions[SID]["questions"][0]["multi_select"] is True


# --- The burst ---------------------------------------------------------------


def _one_multi():
    q = {"text": "Which?", "options": ["A", "B", "C"], "header": "",
         "id": "tu_1", "multi_select": True}
    return dict(q, questions=[dict(q, index=0)])


def _one_single():
    q = {"text": "Which?", "options": ["A", "B", "C"], "header": "",
         "id": "tu_1", "multi_select": False}
    return dict(q, questions=[dict(q, index=0)])


def _mixed():
    first = {"text": "Q0?", "options": ["A", "B", "C"], "header": "",
             "id": "tu_1", "multi_select": False, "index": 0}
    second = {"text": "Q1?", "options": ["A", "B", "C"], "header": "",
              "id": "tu_1", "multi_select": True, "index": 1}
    flat = {k: v for k, v in first.items() if k != "index"}
    return dict(flat, questions=[first, second])


def _asked_daemon(monkeypatch, timeline, reading, state="waiting",
                  fail_on_send=None):
    """A daemon holding `reading` for s1, with a terminal that records every
    keystroke and every gap into one timeline."""
    d = BobDaemon()
    d._questions["s1"] = reading
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


def _sends(timeline):
    return [t for kind, t in timeline if kind == "send"]


def test_the_submit_keys_are_the_read_sequence():
    assert MULTI_SELECT_SUBMIT_KEYS == \
        ("\x1b[A", "\x1b[6~", "\x1b[6~", "\x1b[B", "\r")
    # Never two Downs: with Submit focused a second one leaves the widget.
    assert MULTI_SELECT_SUBMIT_KEYS.count("\x1b[B") == 1
    assert MULTI_SELECT_SUBMIT_KEYS[-1] == "\r"


def test_a_multi_select_one_question_burst_toggles_then_submits(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[0, 2]]))
    assert ok, detail
    # The sequence submits the ticked set, and then one more Enter: a
    # one-question *multi-select* dialog ends on the client's "Review your
    # answers" screen ("Submit answers" focused), which only a single
    # pick-one question skips. Without it the dialog stood on "Ready to
    # submit your answers?" and the row stayed `waiting`.
    assert _sends(timeline) == ["1", "3", "\x1b[A", "\x1b[6~", "\x1b[6~",
                                "\x1b[B", "\r", "\r"]
    # A gap between every consecutive send, the trailing Enter included.
    kinds = [kind for kind, _ in timeline]
    assert kinds == ["send", "gap"] * 7 + ["send"]
    assert all(g == daemon_mod.QUESTION_KEY_GAP_SECONDS
               for kind, g in timeline if kind == "gap")
    assert "s1" not in d._answering


def test_the_digits_go_in_ascending_order_whatever_the_client_sent(monkeypatch):
    """Sorted, never deduped: the order the human ticked in is not the
    widget's concern, and a stable order keeps the read-back honest."""
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[2, 0]]))
    assert ok, detail
    assert _sends(timeline)[:2] == ["1", "3"]


def test_a_mixed_dialog_types_single_then_multi_then_confirms(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _mixed())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[1], [0, 1]]))
    assert ok, detail
    assert _sends(timeline) == ["2", "1", "2", *MULTI_SELECT_SUBMIT_KEYS,
                                "\r"]
    kinds = [kind for kind, _ in timeline]
    assert kinds == ["send", "gap"] * 8 + ["send"]


def test_a_plain_segment_on_a_multi_select_question_is_a_set_of_one(monkeypatch):
    """An older client sends a bare index; on a flagged question that is a
    set of one and still takes the multi-select sequence — `digit, Enter`
    would toggle two boxes and submit nothing."""
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [1]))
    assert ok, detail
    assert _sends(timeline) == ["2", *MULTI_SELECT_SUBMIT_KEYS, "\r"]


def test_a_one_element_group_on_a_multi_select_question_is_the_same(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[1]]))
    assert ok, detail
    assert _sends(timeline) == ["2", *MULTI_SELECT_SUBMIT_KEYS, "\r"]


def test_a_single_select_group_of_one_is_the_digit_alone(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_single())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[1]]))
    assert ok, detail
    assert _sends(timeline) == ["2"]


# --- The refusals, all before the first keystroke ----------------------------


def test_a_group_on_a_single_select_question_is_refused_before_any_keystroke(
        monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_single())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[0, 1]]))
    assert not ok
    assert detail == "Question 1 takes one answer."
    assert timeline == []
    assert "s1" not in d._answering


def test_an_empty_group_is_refused(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[]]))
    assert not ok
    assert detail == "No such option for question 1."
    assert timeline == []


def test_a_duplicate_pick_is_refused(monkeypatch):
    """A repeated digit toggles the box off again on the multi-select
    widget, so it is refused rather than silently deduped."""
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[1, 1]]))
    assert not ok
    assert detail == "Question 1 names the same option twice."
    assert timeline == []


def test_a_multi_select_index_out_of_range_is_refused_whole(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[0, 5]]))
    assert not ok
    assert detail == "No such option for question 1."
    assert timeline == []


def test_a_refusal_on_the_second_question_types_nothing_for_the_first(
        monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _mixed())
    # Question 2 is fine; question 1 is pick-one and got two.
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[0, 1], [0]]))
    assert not ok
    assert detail == "Question 1 takes one answer."
    assert timeline == []


@pytest.mark.parametrize("bad", [["1"], [{"a": 1}], [None], [True], [1.5]])
def test_a_group_that_is_not_indexes_is_refused(monkeypatch, bad):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", bad))
    assert not ok
    assert detail == "No such option."
    assert timeline == []


def test_the_length_rule_still_binds_groups(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _mixed())
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[0]]))
    assert not ok
    assert "2 questions" in detail
    assert timeline == []


# --- The singular verb -------------------------------------------------------


def test_the_singular_verb_uses_the_multi_burst_on_a_flagged_question(
        monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 2))
    assert ok, detail
    assert _sends(timeline) == ["3", *MULTI_SELECT_SUBMIT_KEYS, "\r"]
    assert "s1" not in d._answering


def test_the_singular_verb_types_the_digit_alone_on_an_unflagged_question(
        monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_single())
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert ok, detail
    assert _sends(timeline) == ["2"]


def test_the_singular_verb_still_refuses_a_bad_option_on_a_flagged_question(
        monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi())
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 7))
    assert not ok
    assert detail == "No such option."
    assert timeline == []


# --- Mid-burst failure -------------------------------------------------------


def test_a_mid_burst_failure_on_a_multi_select_question_counts_the_question(
        monkeypatch):
    """The first digit landed, so the terminal is on this question and the
    count says so; the sentence sends the human there for the rest."""
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi(), fail_on_send=3)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[0, 2]]))
    assert not ok
    assert detail.startswith("Answered 1 of 1")
    assert "finish the rest in the terminal" in detail
    assert "s1" not in d._answering


def test_a_first_send_failure_still_names_the_window(monkeypatch):
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_multi(), fail_on_send=1)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[0, 2]]))
    assert not ok
    assert detail.startswith("No VS Code window owns")


# --- The wire ----------------------------------------------------------------


def test_the_plus_grammar_is_parsed_defensively():
    from dark_army_daemon.api_server import ApiServer

    parse = ApiServer._parse_option_indexes
    assert parse("0+2,1") == [[0, 2], [1]]
    assert parse("1") == [[1]]
    assert parse(" 0 + 1 ") == [[0, 1]]
    assert parse("1,0,2") == [[1], [0], [2]]
    for bad in ("+", "0+", "+1", "0++1", "0+-1", "a+1", "", ",", "1,,2",
                "0+,1", None, 3, [[0, 1]]):
        assert parse(bad) is None, bad


def test_the_lan_branch_carries_groups_to_the_coroutine():
    from dark_army_daemon.api_server import ApiServer

    calls = []

    class _Daemon:
        async def answer_questions(self, session_id, question_id, indexes):
            calls.append((session_id, question_id, indexes))
            return True, ""

    srv = ApiServer(_Daemon(), port=0)
    status, _, _ = asyncio.run(srv._lan_run("answer_questions", {
        "session_id": "s1", "question_id": "tu_1", "option_indexes": "0+2,1"}))
    assert status == 200
    assert calls == [("s1", "tu_1", [[0, 2], [1]])]
    status, _, _ = asyncio.run(srv._lan_run("answer_questions", {
        "session_id": "s1", "question_id": "tu_1", "option_indexes": "0+"}))
    assert status == 400
    assert len(calls) == 1


def test_the_loopback_intercept_carries_groups(monkeypatch):
    from dark_army_daemon import api_server as api_mod

    srv = api_mod.ApiServer(BobDaemon(), port=0)
    monkeypatch.setattr(srv, "_authorised", lambda request: True)
    request = api_mod._Request("POST", "/api/action", "", {}, json.dumps({
        "action": "answer_questions", "session_id": "s1",
        "question_id": "tu_1", "option_indexes": "0+2,1"}).encode())
    assert srv._answer_questions_request(request) == \
        ("s1", "tu_1", [[0, 2], [1]])


# --- The record of the reading -----------------------------------------------


def test_the_submit_keys_name_the_client_version():
    """The reading of the widget is the whole safety case for the escape
    sequence; the client version it was read from must sit beside the
    constant so a CLI bump has one place to re-check."""
    source = DAEMON_PY.read_text()
    lines = source.splitlines()
    at = next(i for i, line in enumerate(lines)
              if line.startswith("MULTI_SELECT_SUBMIT_KEYS = "))
    nearby = "\n".join(lines[max(0, at - 60):at + 1])
    assert "2.1.261" in nearby
    assert re.search(r"never a second\s+Down", nearby, re.I) or \
        "Never a second" in nearby
    assert source.count("MULTI_SELECT_SUBMIT_KEYS") >= 3


def test_the_pick_one_keys_are_empty_and_name_the_client_version():
    assert PICK_ONE_SUBMIT_KEYS == ()
    source = DAEMON_PY.read_text()
    lines = source.splitlines()
    at = next(i for i, line in enumerate(lines)
              if line.startswith("PICK_ONE_SUBMIT_KEYS"))
    nearby = "\n".join(lines[max(0, at - 60):at + 1])
    assert "2.1.261" in nearby
    assert re.search(r"nothing to type after the digit", nearby, re.I)


# --- The panel, by lint ------------------------------------------------------


def _panel(name: str) -> str:
    path = PANEL / name
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_the_panel_model_decodes_the_flag_tolerantly():
    source = _panel("Models.swift")
    assert 'case multiSelect = "multi_select"' in source
    assert "multiSelect = c.value(.multiSelect, false)" in source


def test_the_panel_routes_a_multi_select_question_to_the_batch_shape():
    source = _panel("ProcessTable.swift")
    assert "multiSelect: Bool = false" in source
    assert "questionCount > 1 || multiSelect" in source
    assert "agent.questionList.count > 1 || agent.question.multiSelect" in source


def test_the_panel_picker_holds_sets_and_draws_boxes():
    source = _panel("ProcessTable.swift")
    assert "[Int: Set<Int>]" in source
    assert '"☑"' in source and '"☐"' in source
    assert "PICK ANY THAT APPLY" in source
    assert "formSymmetricDifference" in source
    # The singular wording at one question, the plural literal kept above.
    assert '"SEND THIS ANSWER?"' in source
    assert "SEND THESE \\(questions.count) ANSWERS?" in source
    assert '"ANSWER"' in source
    assert "ANSWER ALL \\(questions.count)" in source
    assert "actions.answerQuestions(agent, choices: ordered" in source


def test_the_panel_joins_groups_with_plus():
    assert _panel("DaemonClient.swift").count('joined(separator: "+")') == 1
    assert "choices: [[Int]]" in _panel("Triage.swift")


def test_row_bars_knows_nothing_of_multi_select():
    """`QuestionOptionRow` keeps the one-tap pick-one shape exactly: a
    multi-select question never reaches it, so it needs no branch."""
    source = _panel("RowBars.swift")
    assert "struct QuestionOptionRow" in source
    assert "multiSelect" not in source and "multi_select" not in source


# --- The phone, by lint ------------------------------------------------------


def _phone(name: str) -> str:
    path = PHONE / name
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_the_phone_model_decodes_the_flag_tolerantly():
    source = _phone("Models.swift")
    assert 'case multiSelect = "multi_select"' in source
    assert "multiSelect = c.value(.multiSelect, false)" in source


def test_the_phone_drafts_hold_sets_and_toggle_on_multi():
    source = _phone("AnswerDrafts.swift")
    assert "[Int: Set<Int>]" in source
    assert "multi: Bool" in source
    assert "formSymmetricDifference" in source
    assert "setChoice" not in source


def test_the_answer_box_draws_boxes_and_sends_plus_joined_groups():
    source = _phone("AnswerBox.swift")
    assert "questions.count > 1 || agent.question.multiSelect" in source
    assert '"☑"' in source and '"☐"' in source
    assert "pick any that apply" in source
    assert source.count('joined(separator: "+")') == 1
    assert ".disabled(!complete)" in source
    assert '"send this answer?"' in source
    assert "send these \\(questions.count) answers?" in source
    assert "drafts.pick(" in source


def test_the_needs_you_list_draws_no_question_of_its_own():
    """The list routes and does not answer (20 Sep 2026): the questions,
    their multi-select hint and the answer box are the agent screen's,
    where `AnswerBox` carries the hint."""
    source = _phone("NeedsYouView.swift")
    assert "q.multiSelect" not in source
    assert "AnswerBox(" not in source
    assert "AnswerBox(agent:" in _phone("AgentDetailView.swift")


def test_no_new_action_name_on_either_door():
    """The grammar change is inside one existing verb: no new phone action,
    and `LAN_ACTIONS` / `REMOTE_ACTIONS` are not widened for it."""
    from dark_army_daemon.api_server import ApiServer

    source = _phone("Actions.swift")
    assert source.count("answer_questions") == 1
    assert "multiSelect" not in source and "multi_select" not in source
    assert "answer_questions" in ApiServer.LAN_ACTIONS
    assert not any("multi" in name for name in ApiServer.LAN_ACTIONS)
    assert not any("multi" in name for name in ApiServer.REMOTE_ACTIONS)


# --- The summary screen and the preview layout -------------------------------


def _one_preview():
    q = {"text": "Which?", "options": ["A", "B", "C"], "header": "",
         "id": "tu_1", "multi_select": False, "has_preview": True}
    return dict(q, questions=[dict(q, index=0)])


def test_the_summary_screen_rule_is_the_clients():
    """Claude Code 2.1.261 skips "Review your answers" only for a single
    pick-one question (`hideSubmitTab`). Everything else ends there."""
    single = {"multi_select": False}
    assert _dialog_has_summary([single]) is False
    assert _dialog_has_summary([{"multi_select": False, "has_preview": True}]) is False
    assert _dialog_has_summary([{"multi_select": True}]) is True
    assert _dialog_has_summary([single, single]) is True
    assert _dialog_has_summary([{"multi_select": True}, single]) is True


def test_a_preview_pick_one_question_gets_digit_then_enter(monkeypatch):
    """On the preview layout a digit only moves focus; Enter selects."""
    assert PREVIEW_PICK_ONE_SUBMIT_KEYS == ("\r",)
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, _one_preview())
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert ok, detail
    # Digit, Enter — and no second Enter: a single pick-one question, preview
    # or not, submits from that select and shows no summary screen.
    assert _sends(timeline) == ["2", "\r"]
    timeline.clear()
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[2]]))
    assert ok, detail
    assert _sends(timeline) == ["3", "\r"]


def test_a_preview_question_in_a_longer_dialog_keeps_the_confirm(monkeypatch):
    first = {"text": "Q0?", "options": ["A", "B"], "header": "", "id": "tu_1",
             "multi_select": False, "has_preview": True, "index": 0}
    second = {"text": "Q1?", "options": ["A", "B"], "header": "", "id": "tu_1",
              "multi_select": False, "index": 1}
    reading = dict({k: v for k, v in first.items() if k != "index"},
                   questions=[first, second])
    timeline = []
    d = _asked_daemon(monkeypatch, timeline, reading)
    ok, detail = asyncio.run(d.answer_questions("s1", "tu_1", [[0], [1]]))
    assert ok, detail
    assert _sends(timeline) == ["1", "\r", "2", "\r"]


def test_the_hook_and_the_module_carry_has_preview_identically():
    """Non-empty `preview` on any option flags the question; an absent,
    empty or non-string preview does not. Both builders agree byte for
    byte, and the clamp carries the strict bool through."""
    tin = {"questions": [
        {"question": "With?", "options": [{"label": "A", "preview": "x"},
                                          {"label": "B"}]},
        {"question": "Without?", "options": [{"label": "A", "preview": ""},
                                             {"label": "B", "preview": 3}]},
    ]}
    every = ss.questions_from_tool_input(tin, "tu_1")
    assert [q["has_preview"] for q in every] == [True, False]
    script = _script_ns()["hook_to_message"](_hook(tin))
    assert [q["has_preview"] for q in script["questions"]] == [True, False]
    assert script["questions"] == every
    assert ss.clamp_question({"text": "T", "options": ["A"],
                              "has_preview": True})["has_preview"] is True
    assert ss.clamp_question({"text": "T", "options": ["A"],
                              "has_preview": "yes"})["has_preview"] is False
    assert ss.clamp_question({"text": "T", "options": ["A"]})["has_preview"] is False
