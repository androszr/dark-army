"""Tests for session-title (ai-title) derivation and its payload wiring."""
import json

from dark_army_daemon.ai_title import (
    MAX_PROMPT_CHARS,
    history_title,
    read_ai_title,
    session_display_name,
    _first_user_prompt,
    ascii_fold,
)


def _write_transcript(tmp_path, lines):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    return str(p)


def test_read_ai_title_latest_for_session(tmp_path):
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "first prompt"}},
        {"type": "ai-title", "aiTitle": "Old title", "sessionId": "s1"},
        {"type": "assistant", "message": {"content": "..."}},
        {"type": "ai-title", "aiTitle": "New title", "sessionId": "s1"},
        {"type": "ai-title", "aiTitle": "Other session", "sessionId": "s2"},
    ])
    # latest title for s1, ignoring s2's
    assert read_ai_title(tx, "s1") == "New title"
    # no session filter -> last one wins
    assert read_ai_title(tx, "") == "Other session"


def test_missing_transcript_is_empty():
    assert read_ai_title("/does/not/exist.jsonl", "s1") == ""
    assert read_ai_title("", "s1") == ""


def test_display_name_fallback_chain(tmp_path):
    # ai-title present
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "hello world this is a long first prompt indeed"}},
        {"type": "ai-title", "aiTitle": "Fix the bug", "sessionId": "s1"},
    ])
    assert session_display_name(tx, "s1") == "Fix the bug"

    # no ai-title -> first prompt, whole: a 300-character brief is a brief, not
    # a runaway one, and the row folds it itself.
    tx2 = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "x" * 300}},
    ])
    assert session_display_name(tx2, "s1") == "x" * 300

    # only a pasted log is folded here, and `ascii_fold` renders the … as ...
    tx2b = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "x" * 5000}},
    ])
    name = session_display_name(tx2b, "s1")
    assert name.startswith("x") and name.endswith("...")
    assert len(name) == MAX_PROMPT_CHARS + 3

    # nothing -> short session id
    tx3 = _write_transcript(tmp_path, [{"type": "assistant", "message": {"content": "hi"}}])
    assert session_display_name(tx3, "abcdef123456") == "abcdef12"


def test_first_user_prompt_handles_list_content(tmp_path):
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "list-form prompt"}]}},
    ])
    assert _first_user_prompt(tx) == "list-form prompt"


def test_first_user_prompt_skips_meta_turns(tmp_path):
    """isMeta turns are Claude Code's own injected messages, not the human's.
    A resumed session otherwise displayed as "<local-command-caveat>Caveat: …"."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "isMeta": True, "message": {"content":
            "<local-command-caveat>Caveat: The messages below were generated…"}},
        {"type": "user", "message": {"content": "the real first prompt"}},
    ])
    assert _first_user_prompt(tx) == "the real first prompt"


def test_first_user_prompt_skips_an_empty_attachment_placeholder(tmp_path):
    """Claude Code writes the literal '(no content)' for a turn that had no
    text. That is not a name."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "(no content)"}},
        {"type": "user", "message": {"content": "the real first prompt"}},
    ])
    assert _first_user_prompt(tx) == "the real first prompt"
    assert history_title(tx, "s1") == "the real first prompt"


def test_first_user_prompt_skips_synthetic_wrappers_without_meta_flag(tmp_path):
    """Belt-and-braces: the same turns must be skipped when isMeta is absent."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content":
            "<task-notification><task-id>abc</task-id></task-notification>"}},
        {"type": "user", "message": {"content": "<command-name>/loop</command-name>"}},
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "<local-command-stdout>output</local-command-stdout>"}]}},
        {"type": "user", "message": {"content": "actually typed this"}},
    ])
    assert _first_user_prompt(tx) == "actually typed this"


def test_session_display_name_falls_back_to_id_when_all_synthetic(tmp_path):
    """No genuine prompt at all -> short id, never a synthetic wrapper."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>x"}},
        {"type": "user", "message": {"content": "<task-notification>y</task-notification>"}},
    ])
    assert session_display_name(tx, "abcdef123456") == "abcdef12"


def test_ascii_fold_polish_and_punctuation():
    assert ascii_fold("Wygeneruj brakujące elementy…") == "Wygeneruj brakujace elementy..."
    assert ascii_fold("żółć ŁĄKA ćma śnieg źle ęą") == "zolc LAKA cma snieg zle ea"
    assert ascii_fold("Build finished — deploy ready") == "Build finished - deploy ready"
    assert ascii_fold("plain ascii") == "plain ascii"
    assert ascii_fold("") == ""


def test_session_display_name_folds_ai_title(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(
        {"type": "ai-title", "aiTitle": "Wygeneruj brakujące elementy", "sessionId": "s1"}
    ), encoding="utf-8")
    assert session_display_name(str(p), "s1") == "Wygeneruj brakujace elementy"


def test_a_dragged_in_file_does_not_become_the_name(tmp_path):
    """Dropping a screenshot onto the prompt used to spend all forty characters
    of the name on its path — every screenshot session called the same thing, and
    the terminal tab read `Gav · '/Users/you/Desktop/Zrzut ekra…`."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content":
            "'/Users/me/Desktop/Zrzut ekranu 2026-08-16 o 15.50.40.png'\n\n"
            "how i got into this view?"}},
    ])
    assert session_display_name(tx, "s1") == "how i got into this view?"


def test_an_attachment_only_prompt_is_skipped_for_the_next_one(tmp_path):
    """A turn that was nothing but a file names nothing. Reading on is right:
    the instruction the person meant is in one of the turns after it."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "'/Users/me/a.png'"}},
        {"type": "user", "message": {"content": "Rewrite the sprite pipeline"}},
    ])
    assert session_display_name(tx, "s1") == "Rewrite the sprite pipeline"


def test_urls_and_mentions_lead_the_same_way(tmp_path):
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "@host/daemon.py why is this slow"}},
    ])
    assert session_display_name(tx, "s1") == "why is this slow"


def test_history_title_prefers_ai_title_then_generated_then_prompt(tmp_path):
    """History must not wear a short id, and it must keep Polish as written."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "napraw kursor w panelu"}},
        {"type": "ai-title", "aiTitle": "Naprawa kursora", "sessionId": "s1"},
    ])
    assert history_title(tx, "s1", generated="Dark Army name") == "Naprawa kursora"

    tx2 = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "napraw kursor w panelu"}},
    ])
    assert history_title(tx2, "s1", generated="Nazwa od Boba") == "Nazwa od Boba"
    assert history_title(tx2, "s1") == "napraw kursor w panelu"


def test_history_title_is_empty_when_nothing_names_the_session(tmp_path):
    """A missing name stays missing — the panel already draws '—'."""
    tx = _write_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": "hi"}},
    ])
    assert history_title(tx, "abcdef123456") == ""
    assert history_title("", "abcdef123456") == ""
    assert history_title() == ""


def test_a_relative_path_is_left_alone(tmp_path):
    """`./build.sh fails on main` is a sentence, not an attachment — eating its
    subject to save eleven characters is the worse trade."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "./build.sh fails on main"}},
    ])
    assert session_display_name(tx, "s1") == "./build.sh fails on main"


def test_first_user_prompt_keeps_a_whole_sentence(tmp_path):
    """The display cut is not here at all.

    At 40, and then at 200, the daemon shipped a name that ended mid-word with
    the ellipsis already in the string, so no surface could offer the rest — a
    panel chevron unfolded onto the same truncated sentence. The fold is the
    panel's `lineLimit` now; this function ships what the person typed.
    """
    prompt = ("ta ikona jump to - powinna byc wieksza i na cala wysokosc. "
              "zaproponuj w jakiej formie powinna ona byc")
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": prompt}},
    ])
    assert _first_user_prompt(tx) == prompt
    assert "…" not in _first_user_prompt(tx)


def test_first_user_prompt_survives_past_the_old_two_hundred(tmp_path):
    """The exact shape of the bug: a brief that died at 200, mid-word."""
    prompt = ("aplikacja stosunkowo wolno dziala. wolno reaguje na kliknieca "
              "itp. przeanalizuj modelem claude-fable-5 co mozemy zrobic aby "
              "przyspieszyc jej dzialanie/reagowanie i przygotuj plan "
              "wprowadzenia poprawek zeby dzialala plynnie")
    assert len(prompt) > 200
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": prompt}},
    ])
    assert _first_user_prompt(tx) == prompt


def test_first_user_prompt_still_folds_a_pasted_log(tmp_path):
    """The remaining cut is a payload guard, an order of magnitude further out."""
    tx = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": "x" * 5000}},
    ])
    name = _first_user_prompt(tx)
    assert len(name) == MAX_PROMPT_CHARS + 1 and name.endswith("…")
