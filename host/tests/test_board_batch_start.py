"""Several planned Backlog cards, one session, one card at a time.

`plans/2026-09-25-batch-implement-backlog-cards.md`. The seam is
`test_start_project.py`'s: a real `BoardStore` on a temp file with
`dispatch.spawn` stubbed, `_known_project_roots` monkeypatched, a snapshot dict
for `_reconcile_board` and `_agents_snapshot_cache` set directly — so "one
terminal opened" is a fact about the stub, and every board write the session
makes is a row the test reads back.
"""

import time
from pathlib import Path

import pytest

from dark_army_daemon import board
from dark_army_daemon import daemon_board
from dark_army_daemon import dispatch
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    return root


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _plan(project: Path, name: str) -> str:
    path = project / "plans" / f"{name}.md"
    path.write_text(f"# {name}\n\n- **Stages:** bc-implementer\n")
    return str(path)


def _make(store, project, title, *, planned=True, **kw):
    fields = {"title": title, "project": "bob", "prompt": "go",
              "tool": "claude", "root": str(project),
              "column_name": "prep" if planned else "backlog"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    if planned:
        card, detail = store.attach_plan(
            card["id"], _plan(project, title.replace(" ", "-")), "")
        assert card is not None, detail
    # Board order falls back to `created_at`, which must differ to be one.
    time.sleep(0.002)
    return card


def _three(store, project):
    return [_make(store, project, f"card {i}") for i in (1, 2, 3)]


def _stub_spawn(d, monkeypatch, project, opened):
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(project)})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        opened.append({"root": root, "argv": list(argv), "name": name})
        return True, "opened", None

    monkeypatch.setattr(dispatch, "spawn", accept)


async def _started_batch(d, store, project, monkeypatch, sid="s-batch"):
    """Three cards pressed together, the head bound to `sid` as the
    reconcile would bind it."""
    opened: list = []
    _stub_spawn(d, monkeypatch, project, opened)
    cards = _three(store, project)
    ok, detail = await d.start_cards([c["id"] for c in cards])
    assert ok, detail
    store.bind_session(cards[0]["id"], sid)
    return cards, opened


# --- the press ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_cards_opens_one_terminal_and_marks_the_rest_waiting(
        daemon, project, monkeypatch):
    """The success criterion's first half: one press, one terminal, three
    cards in one ordered batch."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, project, opened)
    cards = _three(store, project)
    diary: list = []
    monkeypatch.setattr(d, "_log_card_event",
                        lambda card, kind, *a, **kw: diary.append(kind))

    # Tick order is not run order: the board's own order is.
    ok, detail = await d.start_cards([cards[2]["id"], cards[0]["id"],
                                      cards[1]["id"]])
    assert ok, detail
    assert len(opened) == 1, "a batch opens one terminal, never three"
    name = opened[0]["name"]
    assert name.endswith("Stacked cards 3"), name
    # The phone's Claude app shows the same title, never a random slug.
    argv = opened[0]["argv"]
    assert argv[1:3] == ["--name", name]
    prompt = opened[0]["argv"][-1]
    assert prompt.startswith("/ship batch: implement")
    for k in (1, 2, 3):
        assert f"## Card {k} of 3" in prompt
    assert prompt.count("Plan: ") == 3

    head, second, third = (store.get(c["id"]) for c in cards)
    assert head["column_name"] == "in_progress"
    assert head["link_state"] == "dispatching"
    assert head["batch_rank"] == "1"
    assert second["column_name"] == third["column_name"] == "backlog"
    assert (second["batch_rank"], third["batch_rank"]) == ("2", "3")
    assert head["batch_id"] and \
        head["batch_id"] == second["batch_id"] == third["batch_id"]
    assert second["link_state"] == third["link_state"] == ""
    assert "card 1 started · 2 waiting" in detail
    assert diary == ["card_dispatched"], "one diary line, the head's"


@pytest.mark.asyncio
async def test_start_cards_skips_and_reports_a_card_that_fails_its_gate(
        daemon, project, monkeypatch):
    """An unplanned card, a queued card and a scout are named in the reply
    and never written; the good ones still start together."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, project, opened)
    good = [_make(store, project, "good 1"), _make(store, project, "good 2")]
    unplanned = _make(store, project, "unplanned", planned=False)
    queued = _make(store, project, "queued")
    store.update(queued["id"], {"queue_state": "queued",
                                "queued_at": time.time()})
    scout = _make(store, project, "scout", planned=False, kind="scout")
    skipped = [unplanned, queued, scout]
    before = {c["id"]: store.get(c["id"]) for c in skipped}
    # A queued card also stands ahead in the project's line, which holds a
    # fresh press at the slot gate (its own test below); held still here so
    # this asserts on the per-card skips alone.
    monkeypatch.setattr(d, "_slot_refusal", lambda *a, **kw: False)

    ok, detail = await d.start_cards(
        [c["id"] for c in good + skipped])
    assert ok, detail
    assert len(opened) == 1
    for card in skipped:
        assert card["title"] in detail
        after = store.get(card["id"])
        for key in ("column_name", "link_state", "batch_id", "batch_rank",
                    "dispatch_error", "session_id"):
            assert after[key] == before[card["id"]][key], (card["title"], key)
    assert daemon_board.BATCH_QUEUED_REFUSAL in detail
    assert daemon_board.BATCH_SCOUT_REFUSAL in detail


@pytest.mark.asyncio
async def test_start_cards_refuses_mixed_roots_mixed_tools_and_grok_in_words(
        daemon, project, tmp_path, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, project, opened)
    other = tmp_path / "other"
    (other / "plans").mkdir(parents=True)
    a = _make(store, project, "a")
    elsewhere = _make(store, other, "elsewhere")
    ok, detail = await d.start_cards([a["id"], elsewhere["id"]])
    assert not ok and "different project" in detail

    codexy = _make(store, project, "codexy", tool="codex")
    ok, detail = await d.start_cards([a["id"], codexy["id"]])
    assert not ok and "different assistant" in detail

    g1 = _make(store, project, "g1", tool="grok")
    g2 = _make(store, project, "g2", tool="grok")
    ok, detail = await d.start_cards([g1["id"], g2["id"]])
    assert not ok and detail == daemon_board.BATCH_TOOL_REFUSAL

    ok, detail = await d.start_cards([a["id"]])
    assert not ok and detail == daemon_board.BATCH_START_TOO_FEW_REFUSAL
    assert opened == []
    for card in (a, elsewhere, codexy, g1, g2):
        assert store.get(card["id"])["batch_id"] == ""


@pytest.mark.asyncio
async def test_start_cards_refuses_when_the_project_has_no_free_place(
        daemon, project, monkeypatch):
    """No queue of a batch: the press is refused in words, nothing queued,
    nothing written."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, project, opened)
    running = _make(store, project, "already running")
    store.bind_session(running["id"], "s-other")
    monkeypatch.setattr(d, "_claiming_session_ids", lambda: {"s-other"})
    cards = _three(store, project)

    ok, detail = await d.start_cards([c["id"] for c in cards])
    assert not ok
    assert detail.startswith("no free place in")
    assert opened == []
    for card in cards:
        row = store.get(card["id"])
        assert row["queue_state"] == ""
        assert row["batch_id"] == ""
        assert row["column_name"] == "backlog"


@pytest.mark.asyncio
async def test_a_waiting_card_refuses_a_single_start_in_words(
        daemon, project, monkeypatch):
    d, store = daemon
    cards, opened = await _started_batch(d, store, project, monkeypatch)
    ok, detail = await d.dispatch_card(cards[1]["id"])
    assert not ok
    assert detail == daemon_board.BATCH_WAITING_REFUSAL
    assert len(opened) == 1
    # Leave batch is the existing reset: the mark goes and Start works.
    card, _ = await d.reset_card(cards[1]["id"])
    assert card["batch_id"] == "" and card["batch_rank"] == ""
    assert store.get(cards[2]["id"])["batch_id"], \
        "leaving one waiting card touches nobody else"


# --- the advance --------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_card_closes_in_order_and_binds_the_next(
        daemon, project, monkeypatch):
    """The success criterion's second half: each card closed by the session
    lands in Done with its own note, the next moves into In progress, and at
    the end the three sit in Done."""
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    sid = "s-batch"
    for k, card in enumerate(cards, start=1):
        closed, why = await d.close_card_by_session(sid, f"card {k} built")
        assert closed is not None, why
        assert closed["id"] == card["id"]
        ok, detail, bound = await d.advance_batch_by_session(sid)
        assert ok, detail
        if k < 3:
            nxt = store.get(cards[k]["id"])
            assert bound["id"] == nxt["id"]
            assert nxt["column_name"] == "in_progress"
            assert nxt["link_state"] == "live"
            assert nxt["session_id"] == sid
            assert nxt["dispatched_at"]
            assert detail == f"now on card {k + 1} of 3"
            open_cards = [c for c in store.by_session(sid)
                          if c["column_name"] != "done"]
            assert [c["id"] for c in open_cards] == [nxt["id"]]
        else:
            assert bound is None
            assert "finished" in detail
    done = [store.get(c["id"]) for c in cards]
    assert all(c["column_name"] == "done" for c in done)
    assert [c["close_note"] for c in done] == [
        "card 1 built", "card 2 built", "card 3 built"]


@pytest.mark.asyncio
async def test_next_card_leaves_an_unclosed_card_ended_and_moves_on(
        daemon, project, monkeypatch):
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    sid = "s-batch"
    await d.close_card_by_session(sid, "card 1 built")
    await d.advance_batch_by_session(sid)
    # Card 2 stops short: the session moves on without closing it.
    ok, _detail, bound = await d.advance_batch_by_session(sid)
    assert ok and bound["id"] == cards[2]["id"]
    left = store.get(cards[1]["id"])
    assert left["link_state"] == "ended"
    assert left["session_id"] == sid
    assert left["column_name"] == "in_progress"
    assert store.get(cards[2]["id"])["link_state"] == "live"
    # The reconcile, with the session still alive, keeps card 2 ended.
    d._reconcile_board({"running": [{"session_id": sid}]})
    assert store.get(cards[1]["id"])["link_state"] == "ended"
    assert store.get(cards[2]["id"])["link_state"] == "live"


@pytest.mark.asyncio
async def test_close_under_a_batch_resolves_to_the_live_card(
        daemon, project, monkeypatch):
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    sid = "s-batch"
    await d.advance_batch_by_session(sid)  # card 1 left open, card 2 live
    closed, why = await d.close_card_by_session(sid, "card 2 built")
    assert closed is not None, why
    assert closed["id"] == cards[1]["id"]
    assert store.get(cards[0]["id"])["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_flag_after_close_before_next_picks_the_newest_done(
        daemon, project, monkeypatch):
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    sid = "s-batch"
    await d.close_card_by_session(sid, "card 1 built")
    await d.advance_batch_by_session(sid)
    time.sleep(0.01)
    await d.close_card_by_session(sid, "card 2 built")
    flagged, why = await d.flag_manual_by_session(sid, "1. look\nWhy not "
                                                  "automated: a screen")
    assert flagged is not None, why
    assert flagged["id"] == cards[1]["id"]
    assert store.get(cards[0]["id"])["manual_steps"] == ""


@pytest.mark.asyncio
async def test_next_card_is_refused_for_a_session_not_in_a_batch(
        daemon, project, monkeypatch):
    d, store = daemon
    _stub_spawn(d, monkeypatch, project, [])
    single = _make(store, project, "single")
    store.bind_session(single["id"], "s-single")
    ok, detail, card = await d.advance_batch_by_session("s-single")
    assert not ok and card is None
    assert detail == daemon_board.BATCH_NO_BATCH_REFUSAL
    ok, detail, _ = await d.advance_batch_by_session("s-nobody")
    assert not ok and detail == daemon_board.BATCH_NO_BATCH_REFUSAL
    assert store.get(single["id"])["link_state"] == "live"


@pytest.mark.asyncio
async def test_next_card_through_the_channel_names_no_card_on_the_request(
        daemon, project, monkeypatch):
    """The channel handler resolves the session from the port and answers
    with the next card; a request carrying a card id is not read for one."""
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)

    async def fresh(port):
        return "s-batch" if port == 51000 else None

    monkeypatch.setattr(d, "_board_request_session_fresh", fresh)
    reply = await d._handle_board_next_request({
        "type": "board_next_request", "port": 51000,
        "card_id": cards[2]["id"]})
    assert reply["ok"] is True
    assert reply["card_id"] == cards[1]["id"]
    assert (reply["rank"], reply["size"]) == (2, 3)
    assert reply["plan_path"] == store.get(cards[1]["id"])["plan_path"]
    reply = await d._handle_board_next_request({
        "type": "board_next_request", "port": 1})
    assert reply == {"ok": False,
                     "detail": daemon_board.BATCH_NOT_BOUND_REFUSAL}


# --- release on failure -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dead_session_releases_the_waiting_members_with_the_note(
        daemon, project, monkeypatch):
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    sid = "s-batch"
    await d.close_card_by_session(sid, "card 1 built")
    await d.advance_batch_by_session(sid)  # card 2 live, card 3 waiting
    # The session leaves the snapshot past the grace.
    long_ago = time.time() - d.BOARD_SESSION_GRACE - 10
    for card in cards[:2]:
        d._board_missing_since[card["id"]] = long_ago
    d._reconcile_board({"running": []})
    waiting = store.get(cards[2]["id"])
    assert waiting["column_name"] == "backlog"
    assert waiting["batch_id"] == ""
    assert waiting["dispatch_error"] == daemon_board.BATCH_LEFT_NOTE
    interrupted = store.get(cards[1]["id"])
    assert interrupted["link_state"] == "ended"
    assert interrupted["batch_id"] == ""
    finished = store.get(cards[0]["id"])
    assert finished["column_name"] == "done"
    assert finished["close_note"] == "card 1 built"
    # Start on the released card works as normal.
    ok, detail = await d.dispatch_card(cards[2]["id"])
    assert ok, detail


@pytest.mark.asyncio
async def test_the_bind_window_expiry_releases_the_batch(
        daemon, project, monkeypatch):
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, project, opened)
    cards = _three(store, project)
    ok, detail = await d.start_cards([c["id"] for c in cards])
    assert ok, detail
    store.update(cards[0]["id"], {
        "dispatched_at": time.time() - dispatch.DISPATCH_BIND_WINDOW - 5},
        bump=False)
    d._reconcile_board({"running": []})
    head = store.get(cards[0]["id"])
    assert head["column_name"] == "backlog"
    assert head["batch_id"] == "" and head["link_state"] == ""
    # The head keeps its own give-up reason; only the members get the note.
    assert head["dispatch_error"]
    assert head["dispatch_error"] != daemon_board.BATCH_LEFT_NOTE
    for card in cards[1:]:
        row = store.get(card["id"])
        assert row["batch_id"] == ""
        assert row["dispatch_error"] == daemon_board.BATCH_LEFT_NOTE


@pytest.mark.asyncio
async def test_resetting_the_card_being_worked_releases_the_rest(
        daemon, project, monkeypatch):
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    card, _ = await d.reset_card(cards[0]["id"], {"column_name": "backlog"})
    assert card is not None
    for waiting in cards[1:]:
        row = store.get(waiting["id"])
        assert row["batch_id"] == ""
        assert row["dispatch_error"] == daemon_board.BATCH_LEFT_NOTE


# --- the snapshot -------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_batch_key_is_working_waiting_or_absent(
        daemon, project, monkeypatch):
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    single = _make(store, project, "single")
    state = d._build_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert by_id[cards[0]["id"]]["batch"] == {
        "rank": 1, "size": 3, "state": "working"}
    assert by_id[cards[1]["id"]]["batch"] == {
        "rank": 2, "size": 3, "state": "waiting"}
    assert by_id[cards[2]["id"]]["batch"]["state"] == "waiting"
    assert "batch" not in by_id[single["id"]]
    # A member the session moved past carries nothing.
    await d.advance_batch_by_session("s-batch")
    state = d._build_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert "batch" not in by_id[cards[0]["id"]]
    assert by_id[cards[1]["id"]]["batch"]["state"] == "working"


# --- a single Start is untouched ----------------------------------------------


@pytest.mark.asyncio
async def test_single_start_argv_and_update_are_byte_identical(
        daemon, project, monkeypatch):
    """The golden: `Plan: <path>` first, `/ship implement`, the six update
    keys, no batch mark — exactly what a Start wrote before batches."""
    d, store = daemon
    opened: list = []
    _stub_spawn(d, monkeypatch, project, opened)
    card = _make(store, project, "alone")
    writes: list = []
    real_update = store.update

    def recording(card_id, fields, **kw):
        writes.append((card_id, dict(fields), dict(kw)))
        return real_update(card_id, fields, **kw)

    monkeypatch.setattr(store, "update", recording)
    ok, detail = await d.dispatch_card(card["id"])
    assert ok, detail
    assert len(opened) == 1
    plan = card["plan_path"]
    assert opened[0]["name"] == "alone"
    assert opened[0]["argv"][-1] == (
        f"Plan: {plan}\n\n"
        f"Read that plan first, then run: /ship implement {plan}\n"
        "The plan carries its own acceptance criteria — implement it, "
        "do not re-plan it.")
    dispatch_writes = [w for w in writes if w[1].get("link_state")
                       == "dispatching"]
    assert len(dispatch_writes) == 1
    _cid, fields, kw = dispatch_writes[0]
    assert set(fields) == {"link_state", "column_name", "dispatched_at",
                           "dispatch_error", "queue_state", "queued_at"}
    assert kw == {"bump": False}
    assert store.get(card["id"])["batch_id"] == ""


def test_batch_waiting_is_the_one_definition():
    card = {"batch_id": "t", "session_id": "", "link_state": "",
            "column_name": "backlog"}
    assert daemon_board._batch_waiting(card)
    assert not daemon_board._batch_waiting(dict(card, session_id="s"))
    assert not daemon_board._batch_waiting(dict(card, link_state="ended"))
    assert not daemon_board._batch_waiting(dict(card, column_name="prep"))
    assert not daemon_board._batch_waiting(dict(card, batch_id=""))
    assert not daemon_board._batch_waiting(dict(card, refine_state="live"))
    assert board.MAX_BATCH_CARDS == 8


# --- dispatch 2: the gaps the security review and the audit found -----------


@pytest.mark.asyncio
async def test_a_member_dragged_out_of_backlog_cannot_start_or_walk_the_batch(
        daemon, project, monkeypatch):
    """Security BLOCK: a waiting member moved to In progress keeps its mark.
    Its single Start is refused in words; a session bound to it anyway can
    neither take the batch's next card nor release the batch."""
    d, store = daemon
    cards, opened = await _started_batch(d, store, project, monkeypatch,
                                         sid="s-A")
    store.update(cards[1]["id"], {"column_name": "in_progress"})
    assert store.get(cards[1]["id"])["batch_id"], "the drag keeps the mark"
    ok, detail = await d.dispatch_card(cards[1]["id"])
    assert not ok and detail == daemon_board.BATCH_MEMBER_REFUSAL
    assert "Leave batch" in detail
    assert len(opened) == 1

    # Forced onto another session anyway.
    store.bind_session(cards[1]["id"], "s-B")
    ok, detail, card = await d.advance_batch_by_session("s-B")
    assert not ok and card is None
    assert detail == daemon_board.BATCH_NO_BATCH_REFUSAL
    third = store.get(cards[2]["id"])
    assert third["batch_id"] and third["session_id"] == ""
    assert third["column_name"] == "backlog"

    # B's session ending releases nothing of A's batch.
    d._board_missing_since[cards[1]["id"]] = (
        time.time() - d.BOARD_SESSION_GRACE - 10)
    d._reconcile_board({"running": [{"session_id": "s-A"}]})
    assert store.get(cards[2]["id"])["batch_id"]
    assert store.get(cards[2]["id"])["dispatch_error"] == ""

    # Nor does a reset of B's card.
    store.update(cards[1]["id"], {"batch_id": third["batch_id"],
                                  "batch_rank": "2"}, bump=False)
    store.bind_session(cards[1]["id"], "s-B")
    await d.reset_card(cards[1]["id"])
    assert store.get(cards[2]["id"])["batch_id"]
    # A's own session still walks its batch.
    ok, _detail, bound = await d.advance_batch_by_session("s-A")
    assert ok and bound["id"] == cards[2]["id"]


@pytest.mark.asyncio
async def test_each_closed_member_is_recorded_with_its_own_report(
        daemon, project, monkeypatch):
    """Audit #1: a closed batch card stays `live` in Done, so without a
    record at the advance every member would take the last card's report."""
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    sid = "s-batch"

    collected: dict = {}

    async def collect(card_id, session_id, root, run_at, verdict, report,
                      summary):
        # The advance collects the passed card's record at once; the real
        # collector marks the run recorded, and so does this stand-in.
        d._work_recorded[card_id] = run_at
        collected[card_id] = report

    monkeypatch.setattr(d, "_collect_work_record", collect)

    def reports():
        return dict(collected)

    d._agents_snapshot_cache = {"running": [
        {"session_id": sid, "last_report": "## Work done\nreport 1"}]}
    await d.close_card_by_session(sid, "card 1 built")
    await d.advance_batch_by_session(sid)
    assert reports()[cards[0]["id"]].endswith("report 1")

    d._agents_snapshot_cache = {"running": [
        {"session_id": sid, "last_report": "## Work done\nreport 2"}]}
    await d.close_card_by_session(sid, "card 2 built")
    await d.advance_batch_by_session(sid)
    got = reports()
    assert got[cards[0]["id"]].endswith("report 1")
    assert got[cards[1]["id"]].endswith("report 2")

    # The session's end asks again, and nothing is recorded twice.
    later = {"running": [{"session_id": sid,
                          "last_report": "## Work done\nreport 3"}]}
    d._consider_work_record(store.get(cards[0]["id"]), later)
    assert all(item[0] != cards[0]["id"] for item in d._work_record_queue)
    assert reports()[cards[0]["id"]].endswith("report 1")


@pytest.mark.asyncio
async def test_the_advance_binds_only_a_member_still_waiting_at_bind_time(
        daemon, project, monkeypatch):
    """Audit #2: a drag landing during the advance's executor hops leaves
    that card alone; the next waiting member is bound instead."""
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    real = d._enrollment_refusal
    moved: list = []

    async def racing(card):
        if not moved:
            moved.append(card["id"])
            store.update(cards[1]["id"], {"column_name": "prep"})
        return await real(card)

    monkeypatch.setattr(d, "_enrollment_refusal", racing)
    ok, _detail, bound = await d.advance_batch_by_session("s-batch")
    assert ok and bound["id"] == cards[2]["id"]
    raced = store.get(cards[1]["id"])
    assert raced["column_name"] == "prep" and raced["session_id"] == ""
    # And the store verb refuses on its own.
    card, why = store.bind_waiting_member(cards[1]["id"], "s-x",
                                          raced["batch_id"])
    assert card is None and "no longer waiting" in why


# --- dispatch 3 ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tidy", ["delete", "reopen"])
async def test_removing_the_finished_first_card_does_not_strand_the_batch(
        daemon, project, monkeypatch, tidy):
    """Audit #1: the person tidies the finished first card out of Done
    mid-batch. The session still walks its batch, and its end releases the
    card it never reached."""
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch,
                                          sid="s-A")
    await d.close_card_by_session("s-A", "card 1 built")
    ok, _detail, bound = await d.advance_batch_by_session("s-A")
    assert ok and bound["id"] == cards[1]["id"]
    if tidy == "delete":
        ok, _ = await d.delete_card(cards[0]["id"])
        assert ok
    else:
        card, _ = await d.reset_card(cards[0]["id"],
                                     {"column_name": "backlog"})
        assert card is not None and card["batch_id"] == ""
    # Nothing was released: the session still owns its batch.
    assert store.get(cards[2]["id"])["batch_id"]
    ok, detail, bound = await d.advance_batch_by_session("s-A")
    assert ok, detail
    assert bound["id"] == cards[2]["id"]


@pytest.mark.asyncio
async def test_a_batch_left_with_no_owner_releases_its_waiting_cards(
        daemon, project, monkeypatch):
    """Audit #1, the other half: the card that spoke for the batch is taken
    out while it is the only one — nothing could walk the rest, so they go
    back at once with the note."""
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch,
                                          sid="s-A")
    await d.close_card_by_session("s-A", "card 1 built")
    # The Done first card is now the only card holding the session.
    ok, _ = await d.delete_card(cards[0]["id"])
    assert ok
    for card in cards[1:]:
        row = store.get(card["id"])
        assert row["batch_id"] == ""
        assert row["dispatch_error"] == daemon_board.BATCH_LEFT_NOTE


@pytest.mark.asyncio
async def test_the_passed_cards_record_is_collected_before_the_next_binds(
        daemon, project, monkeypatch):
    """Audit #2: the file list is a diff to the working tree *at
    collection*, so card k is collected at the advance, before card k+1 is
    bound and starts editing the same tree."""
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch)
    events: list = []

    async def collect(card_id, *rest):
        events.append(("collect", card_id))

    real_bind = store.bind_waiting_member

    def bind(card_id, *a, **kw):
        events.append(("bind", card_id))
        return real_bind(card_id, *a, **kw)

    monkeypatch.setattr(d, "_collect_work_record", collect)
    monkeypatch.setattr(store, "bind_waiting_member", bind)
    await d.close_card_by_session("s-batch", "card 1 built")
    await d.advance_batch_by_session("s-batch")
    assert events == [("collect", cards[0]["id"]), ("bind", cards[1]["id"])]
    assert all(item[0] != cards[0]["id"] for item in d._work_record_queue)
    # A card left open is collected the same way, at its own advance.
    await d.advance_batch_by_session("s-batch")
    assert events[2:] == [("collect", cards[1]["id"]),
                          ("bind", cards[2]["id"])]


@pytest.mark.asyncio
async def test_a_dragged_member_shows_it_left_and_the_batch_end_clears_it(
        daemon, project, monkeypatch):
    """Audit #3: a waiting card dragged out of Backlog says so on its tile,
    its Start is refused in the words the tile offers, and the batch's end
    clears its mark wherever it is."""
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch,
                                          sid="s-A")
    store.update(cards[2]["id"], {"column_name": "in_progress"})
    state = d._build_board_state()
    by_id = {c["id"]: c for c in state["cards"]}
    assert by_id[cards[2]["id"]]["batch"]["state"] == "left"
    ok, detail = await d.dispatch_card(cards[2]["id"])
    assert not ok and "Leave batch" in detail

    d._board_missing_since[cards[0]["id"]] = (
        time.time() - d.BOARD_SESSION_GRACE - 10)
    d._reconcile_board({"running": []})
    dragged = store.get(cards[2]["id"])
    assert dragged["batch_id"] == ""
    assert dragged["column_name"] == "in_progress"
    assert dragged["dispatch_error"] == daemon_board.BATCH_LEFT_NOTE
    assert store.get(cards[1]["id"])["batch_id"] == ""


# --- dispatch 4 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_judges_the_batch_on_the_card_as_it_is_under_the_lock(
        daemon, project, monkeypatch):
    """A delete reads the card before it takes `_dispatch_lock`. If the
    advance binds that card while the delete waits, the delete must judge
    it as the card the session is now on, and release the rest."""
    import asyncio
    d, store = daemon
    cards, _opened = await _started_batch(d, store, project, monkeypatch,
                                          sid="s-A")
    await d.close_card_by_session("s-A", "card 1 built")
    bid = store.get(cards[1]["id"])["batch_id"]
    await d._dispatch_lock.acquire()
    try:
        task = asyncio.ensure_future(d.delete_card(cards[1]["id"]))
        await asyncio.sleep(0.05)   # the delete has read the card, waits
        assert not task.done()
        # What the advance, holding the lock, does meanwhile.
        bound, why = store.bind_waiting_member(cards[1]["id"], "s-A", bid)
        assert bound is not None, why
    finally:
        d._dispatch_lock.release()
    ok, detail = await task
    assert ok, detail
    third = store.get(cards[2]["id"])
    assert third["batch_id"] == ""
    assert third["dispatch_error"] == daemon_board.BATCH_LEFT_NOTE
