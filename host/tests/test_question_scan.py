"""What the agent is asking, read out of the transcript it already streams.

A permission prompt has a documented relay; `AskUserQuestion` has none — it
blocks on terminal input, and a channel event queues *behind* it — but Dark Army can
still answer one through the keystroke route (`BobDaemon.answer_question`,
digit-then-Enter via `vscode_reveal.send_text`), so what this module publishes
is both the caption and the buttons. These tests are mostly about the half that
never changed: not showing a question that has already been answered, which
would send somebody to a terminal with nothing to do — and, now that a verdict
can be aimed at one, making sure it carries the id that lets a late verdict
miss.
"""
import json

from dark_army_daemon import session_stats as ss


def _transcript(tmp_path, lines):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return str(path)


def _ask(tool_id="tu_1", question="Postgres or SQLite?", options=("Postgres", "SQLite"),
         header="Database"):
    return {"type": "assistant", "timestamp": "2026-08-16T10:00:00Z",
            "message": {"model": "claude-opus-5", "content": [
                {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion",
                 "input": {"questions": [{
                     "question": question, "header": header,
                     "options": [{"label": o, "description": "…"} for o in options],
                     "multiSelect": False}]}}]}}


def _answer(tool_id="tu_1"):
    return {"type": "user", "timestamp": "2026-08-16T10:01:00Z",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "Postgres"}]}}


def _other_tool_result(tool_id="tu_99"):
    return {"type": "user", "timestamp": "2026-08-16T10:00:30Z",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def test_a_pending_question_is_readable_from_the_transcript(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [_ask()]))
    assert stats.question["text"] == "Postgres or SQLite?"
    assert stats.question["options"] == ["Postgres", "SQLite"]
    assert stats.question["header"] == "Database"
    # And it is still an ordinary tool call in the counts.
    assert stats.tool_counts["AskUserQuestion"] == 1


def test_an_answered_question_is_forgotten(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [_ask(), _answer()]))
    assert stats.question == {}


def test_the_question_carries_its_tool_use_id_until_answered(tmp_path):
    """`answer_question` matches on the id, so a verdict aimed at a question
    the terminal already dealt with misses rather than choosing an option of
    whatever was asked next — the same argument `answer_permission` makes for
    `request_id`. Cleared with the question, because it is part of it."""
    stats = ss.parse_transcript(_transcript(tmp_path, [_ask(tool_id="tu_7")]))
    assert stats.question["id"] == "tu_7"

    stats = ss.parse_transcript(_transcript(
        tmp_path, [_ask(tool_id="tu_7"), _answer(tool_id="tu_7")]))
    assert stats.question == {}


def test_another_tool_result_does_not_clear_the_question(tmp_path):
    """A session asks, then keeps reading files while it waits. Matching on
    "any tool_result" would clear the question on the very next Read."""
    stats = ss.parse_transcript(_transcript(
        tmp_path, [_ask(), _other_tool_result()]))
    assert stats.question["text"] == "Postgres or SQLite?"


def test_the_latest_question_is_the_one_kept(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _ask("tu_1", "First?"), _answer("tu_1"),
        _ask("tu_2", "Second?")]))
    assert stats.question["text"] == "Second?"


def test_a_question_with_no_text_is_not_a_question(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [_ask(question="  ")]))
    assert stats.question == {}


def test_a_malformed_question_payload_is_survived(tmp_path):
    """The shape is the harness's, not ours, and a future one that changes it
    must make Dark Army quiet rather than make it crash on every parse."""
    for payload in ({"questions": "nope"}, {"questions": []}, {}, {"questions": [1]}):
        line = {"type": "assistant", "timestamp": "2026-08-16T10:00:00Z",
                "message": {"model": "claude-opus-5", "content": [
                    {"type": "tool_use", "id": "x", "name": "AskUserQuestion",
                     "input": payload}]}}
        assert ss.parse_transcript(_transcript(tmp_path, [line])).question == {}


def test_long_text_is_cut_to_a_caption(tmp_path):
    """This is one line on a row, not a transcript viewer — the full text is a
    keystroke away in the terminal the row can jump to."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _ask(question="q" * 900, options=("o" * 200, "b"))]))
    assert len(stats.question["text"]) == ss.MAX_QUESTION_CHARS
    assert len(stats.question["options"][0]) == ss.MAX_OPTION_CHARS


def test_the_question_survives_an_incremental_reparse(tmp_path):
    """The scanner resumes from a byte offset, and the pending id lives in the
    accumulator — a question asked before the resume point must not be
    forgotten, nor answered twice."""
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(_ask()) + "\n")
    cache = ss.StatsCache()
    first = cache.get(str(path))
    assert first.question["text"] == "Postgres or SQLite?"

    with open(path, "a") as fh:
        fh.write(json.dumps(_answer()) + "\n")
    assert cache.get(str(path)).question == {}


# ── the wait with no tool call behind it ─────────────────────────────────────

def _says(text, ts="2026-08-16T10:00:00Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"model": "claude-opus-5",
                        "content": [{"type": "text", "text": text}]}}


def test_the_agents_last_words_are_kept(tmp_path):
    """The commonest wait has no `AskUserQuestion` behind it at all: a turn that
    simply ends with "Plan ready. accept?". The tail of the transcript is the
    only place that question exists."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("Plan gotowy."), _says("Kryteria akceptacji: … accept?")]))
    assert stats.last_text.endswith("accept?")


def test_the_end_of_a_long_message_is_the_half_that_is_kept(tmp_path):
    """A turn opens with what it did and closes with what it wants. Trimming
    from the front would keep the report and drop the question."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("x" * 4000 + " so: accept?")]))
    assert len(stats.last_text) == ss.MAX_LAST_TEXT_CHARS
    assert stats.last_text.endswith("so: accept?")


def test_a_cut_message_starts_at_a_boundary_and_says_it_was_cut(tmp_path):
    """`text[-1200:]` opened the unfolded row mid-word — seen live as "era, a
    każdy punkt…", the back half of a word, which reads as a corrupted payload
    rather than a long message with its head off. The cut walks forward to the
    message's own paragraph break and marks itself."""
    # The kept paragraph has to be most of the tail, or the boundary sits
    # outside the search window and the sentence rule answers first — which is
    # the fallback's own test, below. Sized off the cap so raising it does not
    # quietly turn this into that test.
    head = "Ala ma kota. " * (ss.MAX_LAST_TEXT_CHARS // 10)
    kept = "Alternatywa B — odrzucona. " * (int(ss.MAX_LAST_TEXT_CHARS * 0.8) // 27)
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says(head + "\n\n" + kept.strip())]))
    assert stats.last_text.startswith("… Alternatywa B")
    assert stats.last_text.endswith("odrzucona.")
    assert "\n" not in stats.last_text        # the head's paragraph is gone
    assert len(stats.last_text) <= ss.MAX_LAST_TEXT_CHARS


def test_a_cut_falls_back_to_a_sentence_then_a_word(tmp_path):
    """No paragraph or line break in reach: the sentence end is next, and a
    single space is the floor. Never mid-word."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("premiera. " * (ss.MAX_LAST_TEXT_CHARS // 8) + "accept?")]))
    text = stats.last_text
    assert text.startswith("… ")
    assert not text[2:].startswith("era")
    assert text[2:].startswith("premiera.")


def test_a_short_message_is_untouched(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [_says("Plan ready.")]))
    assert stats.last_text == "Plan ready."


def test_whitespace_is_folded_but_the_lines_survive(tmp_path):
    """Indentation, tabs and runs of blank lines are noise; the line breaks are
    the message's structure. Folding both together turned a closing list into
    one unbroken paragraph — the caption flattens what it shows (two lines with
    a cut), and that is the caption's business, not the parser's."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("line one\n\n\n   line two\ttabbed")]))
    assert stats.last_text == "line one\n\nline two tabbed"


def test_a_list_keeps_its_items_apart(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("Two caveats:\n- gate does not exit 0\n- the check is manual")]))
    assert stats.last_text.splitlines() == [
        "Two caveats:", "- gate does not exit 0", "- the check is manual"]


def test_a_tool_only_turn_does_not_blank_the_last_words(tmp_path):
    """A session that says something and then runs six tools is still waiting
    on what it said."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("accept?"), _ask(), _other_tool_result()]))
    assert stats.last_text == "accept?"


# ── the agent's own TL;DR ────────────────────────────────────────────────────
#
# The tail above is developer prose — paths, pids, backticks. When the agent
# was asked (by the SessionStart hook's injected hint) to also say what it
# wants in one plain sentence, that sentence rides behind an HTML comment,
# `<!-- bob-tldr: … -->`, and becomes `last_summary`: the caption the panel
# prefers, with the raw tail kept behind the chevron.


def test_a_tldr_marker_becomes_the_summary_and_leaves_the_prose(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("Daemon on pid 8123 rebuilt, `host/.venv` refreshed. "
              "<!-- bob-tldr: The rebuild is done - want me to restart it? -->")]))
    assert stats.last_summary == "The rebuild is done - want me to restart it?"
    # The marker is metadata: the full text the chevron shows must not open
    # or close with the machinery.
    assert "bob-tldr" not in stats.last_text
    assert stats.last_text == "Daemon on pid 8123 rebuilt, `host/.venv` refreshed."


def test_no_marker_means_no_summary(tmp_path):
    """The common case, and it must cost nothing: most sessions never saw the
    hint, and their rows keep exactly the behaviour they had."""
    stats = ss.parse_transcript(_transcript(tmp_path, [_says("accept?")]))
    assert stats.last_summary == ""
    assert stats.last_text == "accept?"


def test_the_newest_marker_wins(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("… <!-- bob-tldr: Pick a database. -->"),
        _says("… <!-- bob-tldr: Approve the plan? -->")]))
    assert stats.last_summary == "Approve the plan?"


def test_a_new_message_without_a_marker_clears_the_summary(tmp_path):
    """A summary describes the message beside it. Carrying an old one under
    new words would send someone to a terminal expecting a different
    question."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("… <!-- bob-tldr: Approve the plan? -->"),
        _says("Plan zaakceptowany, robię dalej.")]))
    assert stats.last_summary == ""


def test_a_tool_only_turn_leaves_the_summary_alone(tmp_path):
    """Same rule as last_text: a turn that only runs tools has not spoken, so
    it moves neither the words nor their summary."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("… <!-- bob-tldr: Approve the plan? -->"),
        _ask(), _other_tool_result()]))
    assert stats.last_summary == "Approve the plan?"


def test_a_marker_in_a_user_message_is_ignored(tmp_path):
    """A user pasting a transcript (or this test file) into the prompt must
    not put words in the agent's mouth."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("working on it"),
        {"type": "user", "timestamp": "2026-08-16T10:01:00Z",
         "message": {"content": [
             {"type": "text",
              "text": "look: <!-- bob-tldr: not the agent's words -->"}]}}]))
    assert stats.last_summary == ""


def test_marker_case_and_newlines_are_tolerated(tmp_path):
    """The instruction says one line; a model that wraps it anyway has still
    answered, and the fold is what keeps a caption a caption."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("done. <!-- BOB-TLDR: two\n  words -->")]))
    assert stats.last_summary == "two words"


def test_summary_is_cut_to_a_caption_from_the_front(tmp_path):
    """Head kept, unlike last_text's tail: a summary leads with its point, and
    anything past the cap is the one-sentence instruction being ignored."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("<!-- bob-tldr: head " + "x" * 500 + " -->")]))
    assert len(stats.last_summary) == ss.MAX_SUMMARY_CHARS
    assert stats.last_summary.startswith("head x")


def test_a_marker_only_message_still_sets_the_summary(tmp_path):
    """A message that is nothing but the marker has spoken its summary; the
    tail keeps the previous prose rather than going blank."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("Report: …"),
        _says("<!-- bob-tldr: Approve the plan? -->")]))
    assert stats.last_summary == "Approve the plan?"
    assert stats.last_text == "Report: …"


def test_the_summary_survives_an_incremental_reparse(tmp_path):
    """The scanner resumes from a byte offset; a summary set before the resume
    point must neither vanish nor leak past the message that clears it."""
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(_says("… <!-- bob-tldr: Approve? -->")) + "\n")
    cache = ss.StatsCache()
    assert cache.get(str(path)).last_summary == "Approve?"

    with open(path, "a") as fh:
        fh.write(json.dumps(_says("moving on")) + "\n")
    assert cache.get(str(path)).last_summary == ""


def test_the_hint_and_the_parser_agree_on_the_marker():
    """The instruction lives in the stdlib-only NOTIFY_SCRIPT, the parser in
    session_stats — different processes, no shared constant. This is the test
    that holds the two ends of the contract together."""
    from dark_army_menubar.hooks import NOTIFY_SCRIPT
    assert "<!-- bob-tldr:" in NOTIFY_SCRIPT
    match = ss._TLDR_RE.search("<!-- bob-tldr: hello -->")
    assert match and match.group(1) == "hello"


# ── the answers the agent is offering ────────────────────────────────────────
#
# `<!-- bob-actions: Accept | Iterate -->`, from the same hint and parsed the
# same way. It exists because the panel drew an **Accept** button on every
# reachable stopped row — including rows whose last turn was a status report
# with nothing proposed — so a button has to come from the agent naming the
# choice, never from a surface guessing at one.


def test_declared_actions_become_the_offered_answers(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("Plan is ready. <!-- bob-actions: Accept | Iterate -->")]))
    assert stats.last_actions == ["Accept", "Iterate"]
    # Metadata, like the summary: never left in the prose a chevron unfolds.
    assert "bob-actions" not in stats.last_text
    assert stats.last_text == "Plan is ready."


def test_a_turn_that_offers_nothing_offers_nothing(tmp_path):
    """The common case, and the whole point: no declaration, no buttons."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("Installed and running.")]))
    assert stats.last_actions == []


def test_a_later_message_without_actions_clears_them(tmp_path):
    """A button for a decision two turns old is worse than no button — it
    still sends the word."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("… <!-- bob-actions: Accept | Iterate -->"),
        _says("Done, nothing to decide.")]))
    assert stats.last_actions == []


def test_actions_are_capped_deduped_and_trimmed(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("<!-- bob-actions: Accept |  accept | Iterate | Ship | Later -->")]))
    assert stats.last_actions == ["Accept", "Iterate", "Ship"]


def test_an_over_long_label_is_refused_not_cut(tmp_path):
    """A label is the reply, so a slice types a word the agent never wrote.

    Observed live: `Commit only price-history` drawn — and sendable — as
    `Commit only price-histor`.
    """
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("<!-- bob-actions: " + "y" * 80 + " -->")]))
    assert stats.last_actions == []


def test_one_over_long_label_refuses_the_whole_declaration(tmp_path):
    """Three choices drawn as two is a wrong menu, not a smaller one — the
    reader presses the best of what is shown, not knowing one was withheld."""
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("<!-- bob-actions: Commit only price-history "
              "| Everything | Wait -->")]))
    assert stats.last_actions == []


def test_a_label_exactly_at_the_cap_is_kept_whole(tmp_path):
    label = "z" * ss.MAX_ACTION_CHARS
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("<!-- bob-actions: " + label + " | Iterate -->")]))
    assert stats.last_actions == [label, "Iterate"]


def test_the_two_markers_coexist(tmp_path):
    stats = ss.parse_transcript(_transcript(tmp_path, [
        _says("Ready.\n<!-- bob-tldr: Approve the plan? -->\n"
              "<!-- bob-actions: Accept | Iterate -->")]))
    assert stats.last_summary == "Approve the plan?"
    assert stats.last_actions == ["Accept", "Iterate"]
    assert stats.last_text == "Ready."


# --- The keystroke route re-checks the state it is about to type into --------
#
# `answer_question` is not a message, it is a digit and an Enter typed onto the
# session's input line. If the dialog has closed, they are submitted as an
# ordinary prompt and the agent reads a bare "2" as an instruction from the
# human — so the state is re-checked at the instant of the act, the same rule
# `stop_session` (identity), `delete_abandoned_agent` (category) and
# `dispatch_card` (a fresh read of the card) already keep.

import asyncio
import pytest

from dark_army_daemon.daemon import BobDaemon

QUESTION = {"text": "Postgres or SQLite?", "options": ["Postgres", "SQLite"],
            "header": "Database", "id": "tu_1"}


def _asked_daemon(monkeypatch, state, sent):
    """A daemon holding a question for s1, with a terminal that accepts text."""
    d = BobDaemon()
    d._questions["s1"] = dict(QUESTION)
    if state is not None:
        d._session_states["s1"] = {"state": state, "last_event": 0.0, "pid": 4242}
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, st: 4242)

    async def _send(pid, tty, text, newline=True):
        sent.append(text)
        return {"matched": True, "sent": True, "terminalName": "zsh"}

    monkeypatch.setattr("dark_army_daemon.vscode_reveal.send_text", _send)
    monkeypatch.setattr(BobDaemon, "QUESTION_KEY_GAP_SECONDS", 0, raising=False)
    return d


def test_a_waiting_session_is_answered(monkeypatch):
    """The legitimate path: a dialog that is genuinely up is always `waiting`,
    so the guard must not stand in front of it."""
    sent = []
    d = _asked_daemon(monkeypatch, "waiting", sent)
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert ok, detail
    # A digit selects the option at that absolute index and submits a
    # one-question dialog; nothing follows it.
    assert sent == ["2"]


@pytest.mark.parametrize("state", ["working", "thinking", "idle", "confused"])
def test_a_session_that_moved_on_is_refused_and_nothing_is_typed(monkeypatch, state):
    """The bug this closes: a restored question outliving its dialog. Typing
    into a busy agent is the failure, so the assertion that matters is that
    nothing was sent — not merely that the call returned False."""
    sent = []
    d = _asked_daemon(monkeypatch, state, sent)
    ok, detail = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert not ok
    assert detail == "That session is no longer waiting on a question."
    assert sent == []


def test_a_session_with_no_state_at_all_is_refused(monkeypatch):
    """A late verdict must not act on a session that has ended — the reason
    the `permission` branch of the state machine never creates one either."""
    sent = []
    d = _asked_daemon(monkeypatch, None, sent)
    ok, _ = asyncio.run(d.answer_question("s1", "tu_1", 1))
    assert not ok
    assert sent == []
