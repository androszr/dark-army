"""Dark Army naming a session that Claude Code no longer names."""
import json

from dark_army_daemon import ai_title, session_title


# --- the command line ------------------------------------------------------
# Each of these flags stops the namer from becoming a session on somebody's
# screen. They are asserted one by one rather than as a list, so a failure says
# which guarantee was dropped.


def test_the_namer_writes_no_transcript():
    # Otherwise every title leaves a session-shaped file for Dark Army's own
    # transcript scan, history and agents snapshot to find.
    assert "--no-session-persistence" in session_title.argv("claude", "hi")


def test_the_namer_loads_no_settings_and_therefore_no_hooks():
    args = session_title.argv("claude", "hi")
    i = args.index("--setting-sources")
    assert args[i + 1] == ""


def test_the_namer_starts_no_mcp_servers():
    # The channel is registered user-scope, so without this every title spawns
    # a dark-army-channel that announces itself to the daemon.
    assert "--strict-mcp-config" in session_title.argv("claude", "hi")
    assert "--mcp-config" not in session_title.argv("claude", "hi")


def test_the_namer_runs_the_cheap_model_and_the_given_binary():
    args = session_title.argv("/opt/claude", "hi")
    assert args[0] == "/opt/claude"
    assert args[args.index("--model") + 1] == session_title.MODEL


def test_the_instruction_travels_in_the_prompt():
    # Not in --system-prompt: with it there the model answered the request
    # instead of naming it.
    args = session_title.argv("claude", "fix the sprite pipeline")
    assert "--system-prompt" not in args
    prompt = args[args.index("-p") + 1]
    assert prompt.startswith(session_title.PROMPT_HEAD)
    assert prompt.endswith("fix the sprite pipeline")


def test_a_pasted_log_is_cut_before_it_is_sent():
    prompt = session_title.prompt_for("x" * 5000)
    body = prompt[len(session_title.PROMPT_HEAD):]
    assert len(body) == session_title.REQUEST_CHARS


# --- reading the answer ----------------------------------------------------


def test_a_plain_answer_is_the_title():
    assert session_title.clean("Rewrite the sprite pipeline\n") == \
        "Rewrite the sprite pipeline"


def test_the_answer_is_the_last_line_not_the_first():
    # A preamble comes before the answer and never after it.
    assert session_title.clean("Sure! Here it is:\n\nPanel cursor fixes") == \
        "Panel cursor fixes"


def test_quotes_and_stray_punctuation_are_not_part_of_the_name():
    assert session_title.clean('"Naprawa kursora w panelu."') == \
        "Naprawa kursora w panelu"
    assert session_title.clean("**Panel cursor**") == "Panel cursor"


def test_an_essay_is_not_a_title_and_is_refused():
    # Rejected rather than truncated: a model that answered the request has not
    # produced a long title, and the opening prompt is the better row.
    assert session_title.clean("Well, the reason your panel does not respond "
                               "to clicks is that the row is being rebuilt") == ""
    assert session_title.clean("") == ""


# --- the shop --------------------------------------------------------------


def test_a_session_is_asked_about_once_even_before_an_answer_lands():
    shop = session_title.TitleShop()
    assert shop.consider("s1", "fix the panel") == session_title.ASK
    # The snapshot runs every few seconds and the helper takes ten of them.
    assert shop.consider("s1", "fix the panel") == session_title.PASS


def test_a_session_with_nothing_to_go_on_is_not_asked_about():
    shop = session_title.TitleShop()
    assert shop.consider("s1", "   ") == session_title.PASS
    assert shop.consider("", "fix the panel") == session_title.PASS


def test_switching_the_feature_off_stops_the_next_question():
    shop = session_title.TitleShop(enabled=False)
    assert shop.consider("s1", "fix the panel") == session_title.PASS


def test_a_title_is_kept_and_a_miss_is_remembered_too():
    shop = session_title.TitleShop()
    shop.consider("s1", "fix the panel")
    assert shop.note("s1", "Panel cursor fixes") == "Panel cursor fixes"
    assert shop.title_for("s1") == "Panel cursor fixes"

    shop.consider("s2", "fix the panel")
    assert shop.note("s2", "a whole paragraph that is clearly not a title at "
                           "all, going on and on") == ""
    assert shop.title_for("s2") == ""
    # Remembered: a miss retried per snapshot is a subprocess every few seconds.
    assert shop.consider("s2", "fix the panel") == session_title.PASS


def test_a_helper_that_never_ran_is_not_retried_either():
    shop = session_title.TitleShop()
    shop.consider("s1", "fix the panel")
    shop.note_failed("s1")
    assert shop.consider("s1", "fix the panel") == session_title.PASS


def test_only_the_hits_are_persisted_and_they_come_back(tmp_path):
    path = tmp_path / "titles.json"
    shop = session_title.TitleShop(path=path)
    shop.consider("s1", "fix the panel")
    shop.note("s1", "Panel cursor fixes")
    shop.consider("s2", "fix the panel")
    shop.note_failed("s2")

    assert json.loads(path.read_text()) == {"s1": "Panel cursor fixes"}

    # A restart keeps what it paid for, and asks again about what it did not:
    # the cause of a miss is usually structural, and may have been fixed.
    again = session_title.TitleShop(path=path)
    again.load()
    assert again.title_for("s1") == "Panel cursor fixes"
    assert again.consider("s1", "fix the panel") == session_title.PASS
    assert again.consider("s2", "fix the panel") == session_title.ASK


def test_the_store_is_bounded(tmp_path):
    shop = session_title.TitleShop(path=tmp_path / "titles.json", max_titles=3)
    for i in range(6):
        sid = f"s{i}"
        shop.consider(sid, "work")
        shop.note(sid, f"Title {i}")
    assert len(shop._titles) == 3
    assert shop.title_for("s0") == ""       # the oldest went, not a random one
    assert shop.title_for("s5") == "Title 5"


# --- where the name lands --------------------------------------------------


def _transcript(tmp_path, first_prompt, ai=None):
    path = tmp_path / "t.jsonl"
    lines = [json.dumps({"type": "user", "message": {"content": first_prompt}})]
    if ai:
        lines.append(json.dumps({"type": "ai-title", "aiTitle": ai,
                                 "sessionId": "s1"}))
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_a_generated_title_replaces_the_opening_prompt(tmp_path):
    t = _transcript(tmp_path, "wogole nic nie moge zrobic z ta sesja, napraw to")
    assert ai_title.session_display_name(t, "s1").startswith("wogole nic")
    assert ai_title.session_display_name(t, "s1", "Naprawa sesji w panelu") == \
        "Naprawa sesji w panelu"


def test_claude_codes_own_title_still_wins(tmp_path):
    # A machine with the tab badge switched off gets real ai-titles again, and
    # must not be stuck with whatever Dark Army generated on the day it was on.
    t = _transcript(tmp_path, "fix the panel", ai="Panel click handling")
    assert ai_title.session_display_name(t, "s1", "Something Dark Army made up") == \
        "Panel click handling"


def test_the_prompt_the_row_shows_is_the_prompt_the_namer_is_sent(tmp_path):
    # One reader for both, or the row and the title would be about different
    # halves of the transcript.
    t = _transcript(tmp_path, "'/Users/me/Desktop/shot.png' napraw kursor")
    assert ai_title.first_user_prompt(t) == "napraw kursor"


# --- the daemon's half -----------------------------------------------------


def _daemon(tmp_path):
    from dark_army_daemon.daemon import BobDaemon
    return BobDaemon(sessions_path=tmp_path / "sessions.json")


def test_the_daemon_queues_a_live_session_once(tmp_path):
    d = _daemon(tmp_path)
    t = _transcript(tmp_path, "napraw kursor w panelu")
    d._consider_title("s1", t, {"_category": "running"})
    d._consider_title("s1", t, {"_category": "running"})
    assert d._title_queue == [("s1", "napraw kursor w panelu")]


def test_the_daemon_does_not_pay_to_name_a_tombstone(tmp_path):
    d = _daemon(tmp_path)
    t = _transcript(tmp_path, "napraw kursor w panelu")
    d._consider_title("s1", t, {"_category": "finished"})
    d._consider_title("s2", t, {"_category": "abandoned"})
    assert d._title_queue == []
    # And the skip left no mark, so the same session gets named if it turns out
    # to be alive after all.
    d._consider_title("s1", t, {"_category": "running"})
    assert d._title_queue == [("s1", "napraw kursor w panelu")]


def test_the_daemons_titles_live_beside_its_sessions_file(tmp_path):
    # Keyed off the injected path, so a test never writes into ~/.dark-army.
    d = _daemon(tmp_path)
    assert d._titles_shop.path == tmp_path / "titles.json"


def test_a_grok_session_without_a_generated_title_is_queued(tmp_path, monkeypatch):
    from urllib.parse import quote
    from dark_army_daemon import grok_roster

    cwd = "/tmp/proj"
    directory = tmp_path / quote(cwd, safe="") / "g1"
    directory.mkdir(parents=True)
    (directory / "chat_history.jsonl").write_text(
        json.dumps({"type": "user", "content": "czemu grok nie nazywa sesji"}) + "\n"
    )
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path)

    d = _daemon(tmp_path)
    name = d._session_name("g1", {
        "provider": "grok",
        "cwd": cwd,
        "cli_name": "",
        "_category": "running",
    }, "")
    assert name == "czemu grok nie nazywa sesji"
    assert d._title_queue == [("g1", "czemu grok nie nazywa sesji")]
    d._session_name("g1", {
        "provider": "grok",
        "cwd": cwd,
        "cli_name": "",
        "_category": "running",
    }, "")
    assert d._title_queue == [("g1", "czemu grok nie nazywa sesji")]


def test_a_grok_generated_title_is_not_replaced(tmp_path):
    d = _daemon(tmp_path)
    name = d._session_name("g1", {
        "provider": "grok",
        "cwd": "/tmp/proj",
        "cli_name": "Fix Grok session naming vs Claude tabs",
        "_category": "running",
    }, "")
    assert name == "Fix Grok session naming vs Claude tabs"
    assert d._title_queue == []


