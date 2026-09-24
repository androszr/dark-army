# host/tests/test_card_priority.py
"""The scorer's pure half: the command line, the prompt, the cleaning of what
comes back, the one-attempt-ever rule, and the persistence of hits only —
plus the daemon's two halves driven with the subprocess stubbed out.

`test_session_title.py`'s shape, one module along.
"""

import asyncio
import json

import pytest

from dark_army_daemon import board, card_priority
from dark_army_daemon.card_priority import (
    ASK, PASS, PriorityShop, argv, clean, prompt_for)


# --- the command line ---------------------------------------------------------


def test_argv_carries_all_four_flags():
    """Each flag is argued for in session_title.py's docstring, and dropping
    any one turns a scoring call into a visible session on somebody's screen."""
    a = argv("/bin/claude", title="t", summary="s", project="p")
    assert a[0] == "/bin/claude"
    assert "--no-session-persistence" in a
    assert "--strict-mcp-config" in a
    assert "--setting-sources" in a
    assert a[a.index("--setting-sources") + 1] == ""
    assert "--model" in a
    assert a[a.index("--model") + 1] == card_priority.MODEL


def test_the_instruction_rides_in_the_prompt_not_a_system_prompt():
    a = argv("/bin/claude", title="t", summary="s", project="p")
    assert "--system-prompt" not in a
    prompt = a[a.index("-p") + 1]
    assert prompt.startswith(card_priority.PROMPT_HEAD)


# --- the prompt ---------------------------------------------------------------


def test_prompt_carries_the_card_and_the_rubric():
    text = prompt_for(title="the strip freezes", summary="menu bar work",
                      project="bob")
    assert "the strip freezes" in text
    assert "menu bar work" in text
    assert "bob" in text
    assert "single whole number" in text
    assert "90-100" in text and "0-14" in text


def test_prompt_shows_no_other_card_and_no_folder_list():
    """Scores are absolute: the helper sees one card, which is what makes
    "once ever" honest."""
    text = prompt_for(title="t", summary="s", project="p")
    assert "EXISTING" not in text
    assert text.count("TITLE:") == 1


def test_prompt_is_bounded():
    """A pasted essay cannot become the prompt we send."""
    text = prompt_for(title="t" * 10_000, summary="s" * 10_000, project="p")
    assert len(text) < 10_000


# --- cleaning -----------------------------------------------------------------


def test_clean_accepts_a_bare_number():
    assert clean("75") == "75"
    assert clean(" 80 ") == "80"
    assert clean("0") == "0"
    assert clean("100") == "100"


def test_clean_takes_the_last_nonempty_line():
    """A preamble comes before the answer and never after it."""
    assert clean("Let me think about this.\n\n42\n") == "42"


def test_clean_returns_the_canonical_decimal_form():
    """So a stored value never differs from what the store would normalise."""
    assert clean("07") == "7"
    assert board.normalise_priority(clean("07"))[0] == "7"


def test_clean_rejects_rather_than_clamping_or_truncating():
    """A model that answers "somewhere around 80, because…" has misunderstood
    the job, and unscored is a better resting state than two characters of an
    essay."""
    assert clean("") == ""
    assert clean("NONE") == ""
    assert clean("101") == ""
    assert clean("-5") == ""
    assert clean("7.5") == ""
    assert clean("about 80") == ""
    assert clean("The work is important. " * 40) == ""
    assert clean("usage: claude [-h] [--model MODEL]") == ""


def test_clean_rejects_non_ascii_digits():
    """Arabic-Indic digits pass `isdigit()` and `int()` and would store a
    string no client's own `Int(...)` parses the same way."""
    assert clean("٧٥") == ""


# --- the shop -----------------------------------------------------------------


def test_consider_asks_once_and_passes_for_ever_after():
    shop = PriorityShop()
    assert shop.consider("c1") == ASK
    assert shop.consider("c1") == PASS
    assert shop.consider("c1") == PASS


def test_consider_passes_after_a_failure_too():
    """One attempt per card, ever — a miss is remembered exactly like a hit."""
    shop = PriorityShop()
    assert shop.consider("c1") == ASK
    shop.note_failed("c1")
    assert shop.consider("c1") == PASS


def test_consider_passes_while_disabled_and_on_no_id():
    shop = PriorityShop(enabled=False)
    assert shop.consider("c1") == PASS
    shop.enabled = True
    assert shop.consider("") == PASS


def test_note_records_the_cleaned_number():
    shop = PriorityShop()
    shop.consider("c1")
    assert shop.note("c1", "here you go\n80\n") == "80"
    assert shop.note("c2", "about eighty") == ""


def test_trim_drops_the_oldest():
    shop = PriorityShop(max_cards=3)
    for n in range(5):
        shop.consider(f"c{n}")
        shop.note(f"c{n}", "50")
    assert shop.consider("c0") == ASK
    assert shop.consider("c4") == PASS


def test_save_and_load_persist_hits_and_drop_misses(tmp_path):
    path = tmp_path / "card-priority.json"
    shop = PriorityShop(path=path)
    shop.consider("hit")
    shop.note("hit", "90")
    shop.consider("miss")
    shop.note_failed("miss")
    shop.save()
    assert json.loads(path.read_text()) == {"hit": "90"}

    fresh = PriorityShop(path=path)
    fresh.load()
    assert fresh.consider("hit") == PASS
    assert fresh.consider("miss") == ASK


# --- the daemon's two halves --------------------------------------------------
#
# The executor half queues and spends nothing; the loop half runs the
# subprocess. Driven here with `argv` stubbed to `/bin/echo`, so no real CLI is
# involved and nothing on the machine is touched.


@pytest.fixture()
def scoring_daemon(tmp_path, monkeypatch):
    from dark_army_daemon.daemon import BobDaemon
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    d._board = board.BoardStore(path=tmp_path / "board.db")
    d._board.connect()
    d._priority_shop = card_priority.PriorityShop(
        path=tmp_path / "card-priority.json")
    d._priority_queue = []
    d._priority_task = None
    return d


def _card(store, **fields):
    base = {"project": "p", "root": "/tmp/p", "title": "a card",
            "summary": "some words", "column_name": "prep"}
    base.update(fields)
    card, detail = store.create(base)
    assert card is not None, detail
    return card


def test_consider_queues_a_fresh_card_once(scoring_daemon):
    card = _card(scoring_daemon._board)
    scoring_daemon._consider_priority(card)
    scoring_daemon._consider_priority(card)
    assert len(scoring_daemon._priority_queue) == 1
    assert scoring_daemon._priority_queue[0][0] == card["id"]


def test_consider_skips_a_card_that_already_has_a_number(scoring_daemon):
    card = _card(scoring_daemon._board, priority="60")
    scoring_daemon._consider_priority(card)
    assert scoring_daemon._priority_queue == []
    # …and **learns it**, rather than merely declining. A skip that recorded
    # nothing would put the card back in front of the helper the moment
    # somebody emptied the box.
    assert scoring_daemon._priority_shop.consider(card["id"]) == PASS


def test_a_number_typed_by_hand_and_then_cleared_is_not_re_scored(scoring_daemon):
    """The plain acceptance line: clearing the number puts the card back at
    the bottom of its column — it does not hand it to the helper again."""
    card = _card(scoring_daemon._board, priority="60")
    scoring_daemon._consider_priority(card)
    row, detail = scoring_daemon._board.update(card["id"], {"priority": ""})
    assert row is not None, detail
    scoring_daemon._consider_priority(row)
    scoring_daemon._consider_priority(row)
    assert scoring_daemon._priority_queue == []


def test_the_hand_typed_number_survives_a_restart(scoring_daemon, tmp_path):
    """`learn` persists it, so a daemon that comes back does not re-score a
    card somebody emptied while it was down."""
    card = _card(scoring_daemon._board, priority="60")
    scoring_daemon._consider_priority(card)
    assert json.loads((tmp_path / "card-priority.json").read_text()) \
        == {card["id"]: "60"}

    fresh = card_priority.PriorityShop(path=tmp_path / "card-priority.json")
    fresh.load()
    assert fresh.consider(card["id"]) == PASS


def test_learn_is_idempotent_so_the_reconcile_does_not_rewrite_the_file(tmp_path):
    """The caller runs every few seconds over every card; `note`'s
    unconditional save there would be a file rewrite per scored card per
    pass."""
    path = tmp_path / "card-priority.json"
    shop = card_priority.PriorityShop(path=path)
    shop.learn("c1", "60")
    first = path.stat().st_mtime_ns
    for _ in range(5):
        shop.learn("c1", "60")
    assert path.stat().st_mtime_ns == first
    # A *changed* number is still written.
    shop.learn("c1", "70")
    assert json.loads(path.read_text()) == {"c1": "70"}
    # And an empty value is not an answer.
    shop.learn("c2", "")
    assert "c2" not in json.loads(path.read_text())


def test_consider_skips_a_done_card(scoring_daemon):
    """Scoring one would re-stamp `updated_at` and silently reorder Recently
    finished and the review banner, which order on
    `COALESCE(done_at, updated_at)`."""
    card = _card(scoring_daemon._board, column_name="done")
    scoring_daemon._consider_priority(card)
    assert scoring_daemon._priority_queue == []


def test_consider_skips_a_card_with_no_words(scoring_daemon):
    card = _card(scoring_daemon._board, title="x", summary="")
    card = dict(card, title="", summary="")
    scoring_daemon._consider_priority(card)
    assert scoring_daemon._priority_queue == []


def test_flush_and_score_land_the_number_on_the_row(scoring_daemon,
                                                    monkeypatch):
    card = _card(scoring_daemon._board)
    monkeypatch.setattr(card_priority, "argv",
                        lambda claude_bin, **fields: ["/bin/echo", "80"])

    from dark_army_daemon import agents_poll
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: "/bin/echo")
    # The scorer runs in `STATE_DIR`, which under pytest is a temp folder that
    # may not have been created yet — `create_subprocess_exec` raises if its
    # `cwd` is missing, and that lands in the same "did not finish" branch a
    # real timeout does.
    from dark_army_daemon import daemon as _D
    _D.STATE_DIR.mkdir(parents=True, exist_ok=True)

    async def drive():
        scoring_daemon._consider_priority(card)
        await scoring_daemon._flush_card_priorities()
        assert scoring_daemon._priority_task is not None
        await scoring_daemon._priority_task

    asyncio.run(drive())
    assert scoring_daemon._board.get(card["id"])["priority"] == "80"


def test_a_number_typed_while_the_helper_runs_is_not_overwritten(
        scoring_daemon, monkeypatch):
    """The helper takes ten to forty-five seconds, which is exactly long
    enough for somebody to open the brand-new card and type a number. Dark Army
    re-reads the row and gives way: a person's number is never replaced by
    the model's guess, and the column does not reorder under them."""
    card = _card(scoring_daemon._board)
    monkeypatch.setattr(card_priority, "argv",
                        lambda claude_bin, **fields: ["/bin/echo", "80"])

    from dark_army_daemon import agents_poll
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: "/bin/echo")
    from dark_army_daemon import daemon as _D
    _D.STATE_DIR.mkdir(parents=True, exist_ok=True)

    async def drive():
        scoring_daemon._consider_priority(card)
        await scoring_daemon._flush_card_priorities()
        # The person types theirs while the subprocess is in flight.
        scoring_daemon._board.update(card["id"], {"priority": "25"})
        await scoring_daemon._priority_task

    asyncio.run(drive())
    assert scoring_daemon._board.get(card["id"])["priority"] == "25"


def test_a_card_deleted_while_the_helper_runs_is_not_an_error(
        scoring_daemon, monkeypatch):
    """The read gives `None` and the task returns. Unscored is a resting
    state, so nothing writes `dispatch_error` and nothing raises."""
    card = _card(scoring_daemon._board)
    monkeypatch.setattr(card_priority, "argv",
                        lambda claude_bin, **fields: ["/bin/echo", "80"])

    from dark_army_daemon import agents_poll
    monkeypatch.setattr(agents_poll, "find_claude_binary", lambda: "/bin/echo")
    from dark_army_daemon import daemon as _D
    _D.STATE_DIR.mkdir(parents=True, exist_ok=True)

    async def drive():
        scoring_daemon._consider_priority(card)
        await scoring_daemon._flush_card_priorities()
        scoring_daemon._board.delete(card["id"])
        await scoring_daemon._priority_task

    asyncio.run(drive())
    assert scoring_daemon._board.get(card["id"]) is None


def test_the_miss_paths_are_bounded_too():
    """A Mac with no `claude` binary marks every card and never writes a hit.

    `consider` and `note_failed` both grow the dict without an answer, so
    trimming only on the hit paths would grow it for the daemon's life.
    """
    shop = card_priority.PriorityShop(path=None, max_cards=3)
    for n in range(10):
        assert shop.consider(f"ask-{n}") == card_priority.ASK
    assert len(shop._scores) == 3

    shop = card_priority.PriorityShop(path=None, max_cards=3)
    for n in range(10):
        shop.note_failed(f"miss-{n}")
    assert len(shop._scores) == 3
