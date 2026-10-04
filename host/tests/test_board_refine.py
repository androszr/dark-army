# host/tests/test_board_refine.py
"""The refinement verb, the attach ladder and the plan gate — the daemon-level
half of the Prep column.

`test_dispatch.py` owns the pure guards (`refine_guard`, `refine_prompt`);
this file owns what the daemon does with them: the dispatch that must not move
the card, the bind that must not either, the channel ladder that resolves a
caller to exactly one card, and the gate that asks for a confirmed press
before unplanned work enters In progress.
"""

import asyncio
import os
import time
from dataclasses import replace

import pytest

from dark_army_daemon import attachments, board_workflow, dispatch
from dark_army_daemon import board as board_mod
from dark_army_daemon import daemon_board
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


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


def _make(store, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": "/tmp",
              "prompt": "go", "tool": "claude",
              "summary": "make the thing work"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _arm_spawn(daemon_obj, monkeypatch, spawns=None):
    monkeypatch.setattr(daemon_obj, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: f"/bin/{tool}")

    async def accept(root, argv, name, **_kw):
        if spawns is not None:
            spawns.append({"root": root, "argv": argv, "name": name})
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)


# --- refine_card ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_refine_honours_the_dispatch_preference(daemon, monkeypatch):
    """One switch: Refine is Dark Army starting a session, and a second toggle for
    the same capability would be two copies of a gate."""
    d, store = daemon
    card = _make(store)
    d.board_dispatch_enabled = False

    def boom(*a, **k):
        raise AssertionError("the guard ran with dispatch switched off")

    monkeypatch.setattr(dispatch, "refine_guard", boom)
    ok, detail = await d.refine_card(card["id"])
    assert not ok
    assert "not allowed to start sessions" in detail


@pytest.mark.asyncio
async def test_a_spawned_refine_leaves_the_card_in_prep(daemon, monkeypatch):
    """The whole difference from `dispatch_card`: no column move, no
    `session_id`, no `link_state` — the refinement has its own fields because
    `bind_session` moves a card to In progress and a refining card must not."""
    d, store = daemon
    card = _make(store)
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert spawns[0]["argv"] == ["/bin/claude", dispatch.refine_prompt(card)]
    got = store.get(card["id"])
    assert got["column_name"] == "prep"
    assert got["session_id"] == "" and got["link_state"] == ""
    assert got["refine_state"] == "dispatching"
    assert got["dispatched_at"] is not None
    assert card["id"] in d._refine_baseline


@pytest.mark.asyncio
async def test_a_refused_refine_spawn_leaves_the_card_untouched(daemon,
                                                                monkeypatch):
    d, store = daemon
    card = _make(store)
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")

    async def refuse(root, argv, name, **_kw):
        return False, "no VS Code window could start it", None

    monkeypatch.setattr(dispatch, "spawn", refuse)
    ok, detail = await d.refine_card(card["id"])
    assert not ok
    assert "no VS Code window" in detail
    got = store.get(card["id"])
    assert got["refine_state"] == "" and got["dispatched_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, exe, wants_sep", [
    ("grok", "/bin/grok", True),
    ("claude", "/bin/claude", False),
    ("codex", "/bin/codex", True),
])
async def test_refinement_dispatches_the_card_tool(
        daemon, monkeypatch, tool, exe, wants_sep):
    """Refine launches the assistant the chip already names, with
    `argv_for`'s separator for grok/codex and no `--model` even when the
    card names one — the card's model applies to Start alone."""
    d, store = daemon
    extra = {"model": "grok-4.5"} if tool == "grok" else {}
    card = _make(store, tool=tool, **extra)
    resolved = []
    spawns = []
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda t: resolved.append(t) or f"/bin/{t}")

    async def accept(root, argv, name, **_kw):
        spawns.append({"root": root, "argv": argv, "name": name})
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert resolved == [tool]
    assert store.get(card["id"])["tool"] == tool
    argv = spawns[0]["argv"]
    assert argv[0] == exe
    assert argv[-1].startswith("/ship ")
    if wants_sep:
        assert argv[-2] == "--"
    else:
        assert "--" not in argv
    if tool == "grok":
        assert "--model" not in argv


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, exe, wants_sep", [
    ("grok", "/bin/grok", True),
    ("claude", "/bin/claude", False),
    ("codex", "/bin/codex", True),
])
async def test_refine_hands_every_assistant_the_card_instructions(
        daemon, monkeypatch, tool, exe, wants_sep):
    """Claude, Grok and Codex all receive the same brief: /ship idea, then
    the title and the stored instructions. The argv shape is `argv_for`'s
    (separator for grok/codex); the body is not."""
    d, store = daemon
    brief = ("Roadmap: docs/audit.md — R02\n"
             "Success criterion: same blocking count on both surfaces.")
    card = _make(store, tool=tool, title="[R02] Unify the list",
                 summary="add the missing decisions", prompt=brief)
    spawns = []
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda t: f"/bin/{t}")

    async def accept(root, argv, name, **_kw):
        spawns.append({"root": root, "argv": argv, "name": name})
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    argv = spawns[0]["argv"]
    assert argv[0] == exe
    prompt = argv[-1]
    assert prompt == dispatch.refine_prompt(card)
    assert prompt.startswith("/ship add the missing decisions")
    assert "Title: [R02] Unify the list" in prompt
    assert "Instructions:\n" + brief in prompt
    if wants_sep:
        assert argv[-2] == "--"
    else:
        assert "--" not in argv


@pytest.mark.asyncio
async def test_refine_strips_baked_in_attachment_paths_from_instructions(
        daemon, monkeypatch, tmp_path):
    """Once Refine carries the stored prompt, a path line baked in by an
    older Prepare must not be listed twice — Start's strip-then-append."""
    d, store = daemon
    folder = tmp_path / "attachments"
    rel_folder = "abcd1234-efgh5678-ijkl9012-mnop34"
    staging = folder / rel_folder
    staging.mkdir(parents=True)
    dest = staging / "shot.png"
    dest.write_bytes(b"ok")
    abs_path = str(dest.resolve())
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", folder)
    card = _make(store, prompt=f"look at this\n{abs_path}\nand plan",
                 attachments=f"{rel_folder}/shot.png")
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    prompt = spawns[0]["argv"][1]
    assert "look at this" in prompt
    assert "and plan" in prompt
    assert prompt.count(abs_path) == 1
    assert "Attached files (open with your file tools):" in prompt


@pytest.mark.asyncio
async def test_refine_refuses_when_the_named_tool_is_not_on_path(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store, tool="grok")
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: None)
    ok, detail = await d.refine_card(card["id"])
    assert not ok
    assert detail == ("grok is not installed on this Mac "
                      "(Dark Army looked on PATH and at its usual install sites)")
    assert store.get(card["id"])["refine_state"] == ""


@pytest.mark.asyncio
async def test_refine_appends_attachment_paths_to_the_ship_line(
        daemon, monkeypatch, tmp_path):
    """Refine's prompt is `/ship <summary>` plus the card brief, and would
    otherwise never name the files. Same block Start already appends, after
    refine_guard."""
    d, store = daemon
    folder = tmp_path / "attachments"
    rel_folder = "abcd1234-efgh5678-ijkl9012-mnop34"
    staging = folder / rel_folder
    staging.mkdir(parents=True)
    dest = staging / "shot.png"
    dest.write_bytes(b"ok")
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", folder)
    card = _make(store, attachments=f"{rel_folder}/shot.png")
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    prompt = spawns[0]["argv"][1]
    assert prompt.startswith("/ship make the thing work")
    assert "Attached files (open with your file tools):" in prompt
    assert str(dest.resolve()) in prompt


@pytest.mark.asyncio
async def test_refine_with_a_vanished_attachment_still_ships_the_idea(
        daemon, monkeypatch, tmp_path):
    """A missing copy is omitted, not a refused planning session."""
    d, store = daemon
    folder = tmp_path / "attachments"
    folder.mkdir()
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", folder)
    card = _make(store, attachments="abcd1234-efgh5678-ijkl9012-mnop34/shot.png")
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert spawns[0]["argv"] == ["/bin/claude", dispatch.refine_prompt(card)]


# --- the shared in-flight budget ----------------------------------------------


@pytest.mark.asyncio
async def test_a_refining_card_blocks_a_same_project_dispatch(daemon,
                                                              monkeypatch):
    """Binding is by elimination, so "the first new session in this project"
    has to be a single session — whichever verb produced it.

    Since the per-project work queue (23 Aug 2026) that refusal is **absorbed
    rather than reported**: "another card in this project is already starting"
    is one of `dispatch.is_transient`'s three, so the second card is queued and
    the drain takes it when the first has bound. What this test protects is
    unchanged and is the half that matters — no second session is started into
    the project — and it now also pins the card as queued rather than lost.
    """
    d, store = daemon
    refining = _make(store, title="being refined")
    other = _make(store, title="other work", column_name="backlog")
    _arm_spawn(d, monkeypatch)
    ok, detail = await d.refine_card(refining["id"])
    assert ok, detail
    ok, detail = await d.dispatch_card(other["id"], allow_unplanned=True)
    assert not ok
    assert store.get(other["id"])["queue_state"] == "queued"
    assert store.get(other["id"])["link_state"] == ""


@pytest.mark.asyncio
async def test_a_dispatching_card_blocks_a_same_project_refine(daemon,
                                                               monkeypatch):
    d, store = daemon
    starting = _make(store, title="starting", column_name="backlog")
    prep = _make(store, title="to refine")
    _arm_spawn(d, monkeypatch)
    ok, detail = await d.dispatch_card(starting["id"], allow_unplanned=True)
    assert ok, detail
    ok, detail = await d.refine_card(prep["id"])
    assert not ok
    assert "another card in this project" in detail


# --- the refine bind ------------------------------------------------------------


def test_bind_refining_card_binds_by_elimination_and_moves_nothing(daemon):
    d, store = daemon
    card = _make(store)
    now = time.time()
    store.update(card["id"], {"refine_state": "dispatching",
                              "dispatched_at": now})
    d._refine_baseline[card["id"]] = {"already-running"}
    snapshot = {"running": [
        {"session_id": "already-running", "provider": "claude",
         "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 1},
        {"session_id": "the-planner", "provider": "claude",
         "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 2},
    ]}
    changed = d._bind_refining_card(store.get(card["id"]), snapshot, now + 3)
    assert changed
    got = store.get(card["id"])
    assert got["refine_session_id"] == "the-planner"
    assert got["refine_state"] == "live"
    # The pin the whole design hangs on: a full bind and the card never moved.
    assert got["column_name"] == "prep"
    assert got["session_id"] == "" and got["link_state"] == ""


def test_bind_refining_a_grok_card_ignores_a_claude_row(daemon):
    """Mirror of the old always-claude ignore: a Grok plan must not glue
    onto a Claude window that happened to appear in the same project."""
    d, store = daemon
    card = _make(store, tool="grok")
    now = time.time()
    store.update(card["id"], {"refine_state": "dispatching",
                              "dispatched_at": now})
    d._refine_baseline[card["id"]] = set()
    snapshot = {"running": [
        {"session_id": "a-claude", "provider": "claude",
         "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 1},
    ]}
    assert not d._bind_refining_card(store.get(card["id"]), snapshot, now + 2)
    assert store.get(card["id"])["refine_session_id"] == ""


def test_bind_refining_ignores_the_wrong_provider_and_project(daemon):
    d, store = daemon
    card = _make(store)
    now = time.time()
    store.update(card["id"], {"refine_state": "dispatching",
                              "dispatched_at": now})
    d._refine_baseline[card["id"]] = set()
    snapshot = {"running": [
        {"session_id": "a-codex", "provider": "codex",
         "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 1},
        {"session_id": "elsewhere", "provider": "claude",
         "kind": "interactive",
         "project": "other", "cwd": "/tmp", "started_at": now + 1},
    ]}
    assert not d._bind_refining_card(store.get(card["id"]), snapshot, now + 2)
    assert store.get(card["id"])["refine_session_id"] == ""


def test_bind_window_expiry_clears_the_refine_state(daemon):
    d, store = daemon
    card = _make(store)
    started = time.time() - dispatch.DISPATCH_BIND_WINDOW - 5
    store.update(card["id"], {"refine_state": "dispatching",
                              "dispatched_at": started})
    changed = d._bind_refining_card(store.get(card["id"]), {"running": []},
                                    time.time())
    assert changed
    got = store.get(card["id"])
    assert got["refine_state"] == ""
    assert got["column_name"] == "prep"
    assert "refinement session never appeared" in got["dispatch_error"]


def test_a_codex_refinement_give_up_names_the_trust_question(daemon):
    d, store = daemon
    card = _make(store, tool="codex")
    started = time.time() - dispatch.DISPATCH_BIND_WINDOW - 5
    store.update(card["id"], {"refine_state": "dispatching",
                              "dispatched_at": started})
    assert d._bind_refining_card(store.get(card["id"]), {"running": []},
                                 time.time())
    error = store.get(card["id"])["dispatch_error"]
    assert "Codex may be asking to trust this folder" in error
    assert error.endswith("press Refine again")


def test_reconcile_marks_a_vanished_refinement_ended(daemon):
    """`ended` after the grace, id kept as a record — which is what makes
    Refine appear again (`refine_guard` admits `ended`)."""
    d, store = daemon
    # A refinement's run row puts the card's cost-and-time figures on the
    # wire, and that sibling term buys its own frame (`test_run_figures.py`);
    # hold it still so this asserts on the refine-state change alone.
    d._run_figures_drifted = lambda: False
    card = _make(store)
    store.update(card["id"], {"refine_session_id": "the-planner",
                              "refine_state": "live"})
    empty = {"running": [], "waiting": [], "sleeping": [], "finished": []}
    assert not d._reconcile_board(empty)          # starts the absence timer
    d._refine_missing_since[card["id"]] -= d.BOARD_SESSION_GRACE + 1
    assert d._reconcile_board(empty)
    got = store.get(card["id"])
    assert got["refine_state"] == "ended"
    assert got["refine_session_id"] == "the-planner"
    assert got["column_name"] == "prep"


def test_snapshot_carries_the_refinement_pair(daemon):
    """The panel's refiner jump reads `refine_session_id` + `refine_state`
    straight off the board snapshot — pin that the trim/decorate path carries
    both verbatim, in the style of the author/closer contract test, so a
    future snapshot diet cannot quietly take the chip's aim with it."""
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"refine_session_id": "the-planner",
                              "refine_state": "live"})
    by_id = {c["id"]: c for c in d._build_board_state()["cards"]}
    got = by_id[card["id"]]
    assert got["refine_session_id"] == "the-planner"
    assert got["refine_state"] == "live"


@pytest.mark.asyncio
async def test_reset_card_clears_a_stuck_refinement(daemon):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"refine_session_id": "s1",
                              "refine_state": "live",
                              "dispatch_error": "stuck"})
    d._refine_baseline[card["id"]] = set()
    d._refine_missing_since[card["id"]] = 1.0
    got, detail = await d.reset_card(card["id"])
    assert got is not None, detail
    assert got["refine_state"] == "" and got["refine_session_id"] == ""
    assert card["id"] not in d._refine_baseline
    assert card["id"] not in d._refine_missing_since


# --- attach_plan_by_session -----------------------------------------------------


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    plan = root / "plans" / "the-plan.md"
    plan.write_text("# a plan\n\n- **Stages:** bc-implementer | bc-verifier\n")
    return root, plan


@pytest.mark.asyncio
async def test_attach_rung_one_reaches_the_refined_card(daemon, tmp_path):
    d, store = daemon
    root, plan = _project(tmp_path)
    card = _make(store, root=str(root))
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    assert got["column_name"] == "backlog"
    assert got["plan_path"] == os.path.realpath(str(plan))
    assert got["refine_state"] == ""


@pytest.mark.asyncio
async def test_attach_rung_two_reaches_the_card_the_caller_authored(daemon,
                                                                    tmp_path):
    """What makes a hand-run /ship whole: Phase 5 files the card into Prep and
    the very next tool call attaches the plan to it."""
    d, store = daemon
    root, plan = _project(tmp_path)
    card = _make(store, root=str(root), author="hand-run-1")
    got, detail = await d.attach_plan_by_session("hand-run-1",
                                                 "plans/the-plan.md")
    assert got is not None, detail
    assert got["id"] == card["id"]
    assert got["column_name"] == "backlog"
    assert got["plan_path"] == os.path.realpath(str(plan))


@pytest.mark.asyncio
async def test_attach_ambiguity_fails_closed(daemon, tmp_path):
    d, store = daemon
    root, plan = _project(tmp_path)
    _make(store, title="one", root=str(root), author="hand-run-1")
    _make(store, title="two", root=str(root), author="hand-run-1")
    got, detail = await d.attach_plan_by_session("hand-run-1", str(plan))
    assert got is None
    assert "more than one" in detail


@pytest.mark.asyncio
async def test_attach_with_no_matching_card_is_refused_in_words(daemon,
                                                                tmp_path):
    d, store = daemon
    root, plan = _project(tmp_path)
    _make(store, root=str(root), author="somebody-else")
    got, detail = await d.attach_plan_by_session("stranger-1", str(plan))
    assert got is None
    assert "no Prep card" in detail


@pytest.mark.asyncio
async def test_attach_refuses_an_empty_session(daemon, tmp_path):
    """`by_refine_session`'s empty-string trap, closed at this level too: an
    unattributable attach must not scan the board."""
    d, store = daemon
    root, plan = _project(tmp_path)
    _make(store, root=str(root))
    got, detail = await d.attach_plan_by_session("", str(plan))
    assert got is None
    assert "which session" in detail


@pytest.mark.asyncio
@pytest.mark.parametrize("path_builder, said", [
    # An absolute path outside the card's root.
    (lambda root, tmp: str(tmp / "outside.md"), "inside the card's own project"),
    # A ../ walk out of the root.
    (lambda root, tmp: "plans/../../outside.md", "inside the card's own project"),
    # Not Markdown.
    (lambda root, tmp: "plans/notes.txt", "Markdown"),
    # Nothing there.
    (lambda root, tmp: "plans/missing.md", "no file at that path"),
])
async def test_attach_containment_refusals(daemon, tmp_path, path_builder, said):
    d, store = daemon
    root, _ = _project(tmp_path)
    (tmp_path / "outside.md").write_text("# out\n")
    (root / "plans" / "notes.txt").write_text("text\n")
    card = _make(store, root=str(root), author="hand-run-1")
    got, detail = await d.attach_plan_by_session(
        "hand-run-1", path_builder(root, tmp_path))
    assert got is None, detail
    assert said in detail
    assert store.get(card["id"])["plan_path"] == ""


@pytest.mark.asyncio
async def test_attach_refuses_an_oversize_plan(daemon, tmp_path):
    d, store = daemon
    root, plan = _project(tmp_path)
    plan.write_text("x" * (board_workflow.MAX_PLAN_BYTES + 1))
    _make(store, root=str(root), author="hand-run-1")
    got, detail = await d.attach_plan_by_session("hand-run-1", str(plan))
    assert got is None
    assert "too large" in detail


# --- the plan gate --------------------------------------------------------------


def _planned(store, tmp_path, **kw):
    root, plan = _project(tmp_path)
    card = _make(store, root=str(root), **kw)
    attached, detail = store.attach_plan(card["id"], str(plan), "planner-1")
    assert attached is not None, detail
    return attached


@pytest.mark.asyncio
async def test_the_gate_refuses_an_unplanned_update_into_in_progress(daemon):
    d, store = daemon
    card = _make(store)
    got, detail = await d.update_card(card["id"],
                                      {"column_name": "in_progress"})
    assert got is None
    assert "no plan yet" in detail
    assert store.get(card["id"])["column_name"] == "prep"


@pytest.mark.asyncio
async def test_the_gate_refuses_the_reorder_door_too(daemon):
    d, store = daemon
    card = _make(store)
    got, detail = await d.reorder_card(card["id"], "in_progress", "")
    assert got is None
    assert "no plan yet" in detail


@pytest.mark.asyncio
async def test_the_gate_refuses_an_unplanned_dispatch(daemon, monkeypatch):
    d, store = daemon
    card = _make(store, column_name="backlog")
    _arm_spawn(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert "no plan yet" in detail


@pytest.mark.asyncio
async def test_the_confirmed_press_passes_the_gate(daemon):
    d, store = daemon
    card = _make(store)
    got, detail = await d.update_card(card["id"],
                                      {"column_name": "in_progress"},
                                      allow_unplanned=True)
    assert got is not None, detail
    assert got["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_a_planned_card_passes_silently(daemon, tmp_path):
    d, store = daemon
    card = _planned(store, tmp_path)
    got, detail = await d.update_card(card["id"],
                                      {"column_name": "in_progress"})
    assert got is not None, detail
    assert got["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_a_plan_whose_file_is_gone_gets_the_gate_again(daemon, tmp_path):
    """A path to a file that is gone is worse than no plan — the card claims a
    stage that cannot be read."""
    d, store = daemon
    card = _planned(store, tmp_path)
    os.unlink(card["plan_path"])
    got, detail = await d.update_card(card["id"],
                                      {"column_name": "in_progress"})
    assert got is None
    assert "no plan yet" in detail


@pytest.mark.asyncio
async def test_a_card_with_a_session_is_tracking_not_admitting(daemon):
    """The session/link exemption: a card whose assistant already ran being
    moved is tracking, not admitting unplanned work."""
    d, store = daemon
    card = _make(store, column_name="backlog", tool="claude")
    store.bind_session(card["id"], "sess-1")
    store.update(card["id"], {"column_name": "backlog"})
    got, detail = await d.update_card(card["id"],
                                      {"column_name": "in_progress"})
    assert got is not None, detail
    assert got["column_name"] == "in_progress"


@pytest.mark.asyncio
async def test_a_save_of_a_card_already_in_progress_is_not_gated(daemon):
    """An *arrival*, not an edit: re-saving a card already sitting in
    In progress must not be refused over a field the save is not touching."""
    d, store = daemon
    card = _make(store, column_name="in_progress")
    got, detail = await d.update_card(card["id"],
                                      {"column_name": "in_progress",
                                       "title": "renamed"})
    assert got is not None, detail
    assert got["title"] == "renamed"


def test_bind_session_is_structurally_outside_the_gate(daemon):
    """The daemon's own writes go through the store, and the gate lives only
    in the surface-facing verbs — a bound card with no plan still moves."""
    d, store = daemon
    card = _make(store, column_name="backlog", tool="claude")
    got, detail = store.bind_session(card["id"], "sess-1")
    assert got is not None, detail
    assert got["column_name"] == "in_progress"
    assert got["plan_path"] == ""


# --- the gate is a confirmation, not a wall -------------------------------------
#
# The panel cannot see the daemon's `isfile` — `planPath` says "planned" about
# a card whose plan file a branch switch has deleted — so the *refusal itself*
# is the signal: the panel matches its opening words
# (`ActionResult.isPlanGateRefusal`) and answers by raising the same
# "Start unplanned?" confirmation the empty-path routes already show. The
# invariant these tests pin: every state the daemon refuses with the plan-gate
# refusal has a reachable on-screen route that produces the confirmation. The
# Python half is that the refusal is one recognisable constant on every gated
# state; the Swift half is a source-level lint in the house style of
# `test_panel_cursor.py`, because the panel has no test target of its own.

from pathlib import Path

from dark_army_daemon import daemon as daemon_mod

PANEL = Path(__file__).resolve().parents[2] / "panel" / "Sources" / "BobPanel"

# The opening words `ActionResult.isPlanGateRefusal` matches. Reword the
# refusal and this must be retuned *with* the Swift matcher, or the degraded
# mode is the refusal drawn as plain words with no way through.
_PANEL_MATCH_PREFIX = "this card has no plan yet"

# The gate's second rung, and its own matcher on both surfaces. Deliberately a
# separate sentence: both surfaces match by prefix, so one constant becoming a
# prefix of the other would make both matchers fire on it and turn the
# changed-plan confirmation silently back into the unplanned one.
_CHANGED_MATCH_PREFIX = "this card's plan has changed"
PHONE = Path(__file__).resolve().parents[2] / "ios" / "BobPhone"


@pytest.mark.asyncio
async def test_every_gated_state_speaks_the_one_refusal(daemon, tmp_path):
    """Empty `plan_path` and a plan file that is gone must produce the *same*
    refusal, verbatim `PLAN_GATE_REFUSAL` — the panel recognises the string,
    so a second wording on either branch would be a state whose refusal no
    surface answers with the confirmation."""
    d, store = daemon
    unplanned = _make(store)
    _, detail = await d.update_card(unplanned["id"],
                                    {"column_name": "in_progress"})
    assert detail == daemon_mod.PLAN_GATE_REFUSAL
    gone = _planned(store, tmp_path)
    os.unlink(gone["plan_path"])
    _, detail = await d.update_card(gone["id"],
                                    {"column_name": "in_progress"})
    assert detail == daemon_mod.PLAN_GATE_REFUSAL


def test_the_panel_matcher_and_the_refusal_agree():
    """The cross-language contract, pinned from the Python side: the daemon's
    constant must open with the words the Swift matcher looks for, and the
    matcher must look for exactly those words."""
    assert daemon_mod.PLAN_GATE_REFUSAL.startswith(_PANEL_MATCH_PREFIX)
    api = (PANEL / "ActionModels.swift").read_text()
    assert f'detail.hasPrefix("{_PANEL_MATCH_PREFIX}")' in api, (
        "ActionResult.isPlanGateRefusal no longer matches the daemon's "
        "plan-gate refusal — retune the two together")


def test_every_flagless_route_answers_the_gate_with_the_confirmation():
    """Source-level lint (`test_panel_cursor.py`'s pattern): each panel route
    that can send an In-progress move with no `skipPlanGate` flag must route
    `isPlanGateRefusal` into `pendingUnplanned` — the confirmation dialog —
    rather than drawing it as words. Counted per file: `BoardView.swift` has four
    such sends (start, place's dispatch branch, place's launcher-off reorder,
    move's launcher-off update) and `BoardState.swift` one (`startCard`, the
    Start button's one copy, pressed by the tile and the card window alike —
    `plans/2026-09-20-simplify-card-details.md`). A new flagless route added
    without its fallback shows up here as a count that did not move."""
    board_src = (PANEL / "BoardView.swift").read_text()
    card_src = (PANEL / "BoardState.swift").read_text()
    assert board_src.count("isPlanConfirmable") >= 4, (
        "a flagless route in BoardView.swift lost its plan-gate fallback")
    assert card_src.count("isPlanConfirmable") >= 1, (
        "the Start button lost its plan-gate fallback")
    # The tile and the card window both press that one copy.
    for view in ("BoardCardView.swift", "BoardCardSheet.swift"):
        assert "state.startCard(" in (PANEL / view).read_text(), view
    # And each fallback actually raises the confirmation, not a refusal line.
    for src, name in ((board_src, "BoardView.swift"), (card_src, "BoardState.swift")):
        for i, line in enumerate(src.splitlines()):
            if "isPlanConfirmable" not in line:
                continue
            window = "\n".join(src.splitlines()[i:i + 20])
            assert "pendingUnplanned" in window, (
                f"{name}: an isPlanConfirmable branch does not raise the "
                "confirmation within 20 lines")
            assert "planChanged:" in window, (
                f"{name}: a plan-gate fallback does not say which rung "
                "raised it, so the dialog cannot word itself")


def test_the_drag_path_mirrors_the_refining_refusal():
    """Gap 2's panel half: `place` and `drop` refuse a refining card bound for
    In progress in `dispatch.guard`'s own words, so the daemon is never the
    only thing saying so."""
    board_src = (PANEL / "BoardView.swift").read_text()
    assert board_src.count(
        "this card is being refined — wait for the planner") >= 2, (
        "the drag paths no longer mirror canStart's !card.isRefining")


# --- attach rung 2 recency ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_authored_note_fails_closed(daemon, tmp_path):
    """The mis-attach case, closed by recency: a hand-run /ship in a session
    that *earlier* filed one unrelated note via `bob_add_card` must not have
    this plan attached to that note — nothing clears `plan_path`, so the
    mis-attach is uncorrectable short of deleting the card. Out-of-window
    means rung 2 finds nothing and the refusal tells the skill to file a
    fresh card, which then attaches within any window."""
    d, store = daemon
    root, plan = _project(tmp_path)
    note = _make(store, title="an unrelated note", root=str(root),
                 author="hand-run-1")
    stale = time.time() - daemon_mod.ATTACH_AUTHOR_WINDOW_SECONDS - 60
    store._conn.execute("UPDATE cards SET created_at = ? WHERE id = ?",
                        (stale, note["id"]))
    store._conn.commit()
    got, detail = await d.attach_plan_by_session("hand-run-1", str(plan))
    assert got is None
    assert "no Prep card" in detail
    assert store.get(note["id"])["plan_path"] == ""
    assert store.get(note["id"])["column_name"] == "prep"


@pytest.mark.asyncio
async def test_rung_one_is_not_windowed(daemon, tmp_path):
    """The recency bound is rung 2's alone: a *refinement* legitimately takes
    longer than the window (the interview is a conversation with a human), and
    rung 1's card is bound by `refine_session_id`, which no stale note can
    wear — so an old refine-bound card still attaches."""
    d, store = daemon
    root, plan = _project(tmp_path)
    card = _make(store, root=str(root))
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    stale = time.time() - daemon_mod.ATTACH_AUTHOR_WINDOW_SECONDS - 3600
    store._conn.execute("UPDATE cards SET created_at = ? WHERE id = ?",
                        (stale, card["id"]))
    store._conn.commit()
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    assert got["column_name"] == "backlog"


# --- create_card_and_refine: the composer's one press ---------------------------


@pytest.mark.asyncio
async def test_create_and_refine_creates_then_spawns(daemon, monkeypatch):
    """One press, two halves: the card is written to Prep and then Refine
    runs on it exactly as a board press would — through `refine_card`, so
    the preference, the lock and the guard all apply."""
    d, store = daemon
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    card, detail, refine_ok, refine_detail = await d.create_card_and_refine({
        "title": "do the thing", "project": "bob", "root": "/tmp",
        "prompt": "go", "tool": "claude", "summary": "make the thing work"})
    assert card is not None, detail
    assert detail == "created"
    assert refine_ok, refine_detail
    assert len(spawns) == 1
    got = store.get(card["id"])
    assert got["column_name"] == "prep"
    assert got["refine_state"] == "dispatching"
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_create_and_refine_refusal_keeps_the_card(daemon, monkeypatch):
    """A refused refinement never undoes the create. The reason lands on the
    new card's `dispatch_error` — the line both surfaces already draw — since
    the composer has dismissed by the time it could be shown anywhere else."""
    d, store = daemon
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    d.board_dispatch_enabled = False
    card, detail, refine_ok, refine_detail = await d.create_card_and_refine({
        "title": "do the thing", "project": "bob", "root": "/tmp",
        "prompt": "go", "tool": "claude"})
    assert card is not None, detail
    assert refine_ok is False
    assert "not allowed to start sessions" in refine_detail
    assert spawns == []
    got = store.get(card["id"])
    assert got["column_name"] == "prep"
    assert got["refine_state"] == ""
    assert "not allowed to start sessions" in got["dispatch_error"]


@pytest.mark.asyncio
async def test_create_and_refine_refusal_on_create_refines_nothing(
        daemon, monkeypatch):
    d, store = daemon
    _arm_spawn(d, monkeypatch)

    async def boom(card_id):
        raise AssertionError("refine_card ran with no card written")

    monkeypatch.setattr(d, "refine_card", boom)
    card, detail, refine_ok, refine_detail = await d.create_card_and_refine({
        "title": "do the thing", "project": "bob", "root": "/nowhere/at/all",
        "prompt": "go", "tool": "claude"})
    assert card is None
    assert "not a project Dark Army can see" in detail
    assert refine_ok is False and refine_detail == ""
    assert store.cards() == []


@pytest.mark.asyncio
async def test_create_and_refine_spawn_failure_is_written_on_the_card(
        daemon, monkeypatch):
    d, store = daemon
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")

    async def refuse(root, argv, name, **_kw):
        return False, "boom", None

    monkeypatch.setattr(dispatch, "spawn", refuse)
    card, detail, refine_ok, refine_detail = await d.create_card_and_refine({
        "title": "do the thing", "project": "bob", "root": "/tmp",
        "prompt": "go", "tool": "claude"})
    assert card is not None, detail
    assert refine_ok is False and refine_detail == "boom"
    got = store.get(card["id"])
    assert got["column_name"] == "prep"
    assert got["refine_state"] == ""
    assert got["dispatch_error"] == "boom"


@pytest.fixture
def refinement_handoff(tmp_path, monkeypatch):
    """Real board and running Codex roots; only process/editor boundaries fake."""
    from dark_army_daemon import codex_rollouts, vscode_reveal
    from tests.test_codex_rollouts import _root, _native_holder
    root = str(tmp_path.resolve())
    records, processes = [], []
    for i, tid in enumerate(("01a07176-a834-70a3-8ab5-d1c408a3b8f4",
                             "11a07176-a834-70a3-8ab5-d1c408a3b8f4")):
        path = tmp_path / (tid + ".jsonl")
        import json
        path.write_text("\n".join(json.dumps(row) for row in [
            {"type": "session_meta", "payload": {"id": tid, "cwd": root,
             "originator": "codex-tui", "source": "cli", "thread_source": "user"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        ]) + "\n")
        records.append(_root(tid, cwd=root, path=path, activity="working", turn_id="turn-1",
                             last_event=time.time(), process_seen=True))
        processes.append(_native_holder(701 + i, path, f"ttys04{i}", cwd=root))
    monkeypatch.setattr(codex_rollouts, "_process_snapshot", lambda: processes)
    monkeypatch.setattr(codex_rollouts, "load_recent", lambda: records)
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    d._codex_records = {r.session_id: r for r in records}
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\nImplement the accepted change.\n")
    card = _make(store, root=root, tool="codex", author=records[0].session_id)
    posts = []
    monkeypatch.setattr(vscode_reveal, "_bob_ext_locks", lambda: [{
        "port": 44444, "authToken": "fixture", "extensionVersion": "0.1.12",
        "workspaceFolders": [root]}])
    monkeypatch.setattr(vscode_reveal, "_session_in_vscode", lambda pid: True)

    async def post(port, token, body, **kwargs):
        if kwargs.get('before_write') and not await kwargs['before_write']():
            return None
        posts.append(body)
        return {"matched": True, "closed": True}
    monkeypatch.setattr(vscode_reveal, "_post_json", post)
    yield d, store, card, plan, records, processes, posts
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('rung', ['bound', 'author'])
async def test_codex_attachment_mints_private_receipt_before_publication(refinement_handoff, monkeypatch, rung):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    if rung == 'bound':
        store.update(card['id'], {'refine_state': 'live', 'refine_session_id': sid})
    published = []
    async def publish():
        published.append(d._refinement_receipts[sid])
    monkeypatch.setattr(d, '_publish_board', publish)
    attached, detail = await d.attach_plan_by_session(sid, str(plan))
    assert attached, detail
    receipt = d._refinement_receipts[sid]
    assert published == [receipt] and receipt.card_id == card['id']
    assert receipt.root.session_id == sid and receipt.turn_id == 'turn-1'
    assert receipt.expires > time.monotonic() + 590
    assert all('receipt' not in k and 'turn_id' not in k for k in attached)
    assert not posts


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['missing', 'outside', 'not_md', 'moved', 'unknown_turn', 'generation'])
async def test_failed_or_stale_attachment_mints_no_close_receipt(refinement_handoff, monkeypatch, fault):
    from copy import deepcopy
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    path = str(plan)
    if fault == 'missing': plan.unlink()
    elif fault == 'outside': path = '/etc/hosts'
    elif fault == 'not_md': path = str(plan.with_suffix('.txt')); plan.rename(path)
    elif fault == 'moved': store.update(card['id'], {'column_name': 'done'})
    elif fault == 'unknown_turn': records[0].turn_id = ''
    else:
        real = d._plan_path_refusal
        def validate(*args):
            d._codex_records[sid] = deepcopy(records[0])
            return real(*args)
        monkeypatch.setattr(d, '_plan_path_refusal', validate)
    await d.attach_plan_by_session(sid, path)
    assert sid not in d._refinement_receipts and posts == []


@pytest.mark.asyncio
async def test_second_distinct_attachment_revokes_automatic_close(refinement_handoff):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    _make(store, root=records[0].cwd, tool='codex', author=sid)
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    assert d._refinement_receipts[sid].state == 'ambiguous'
    assert not (await d.close_refinement_terminal(sid))[0]
    assert posts == []


def _codex_batch_receipt_cards(handoff, count=3, *, token="batch-close-1", session_index=0):
    d, store, first, plan, records, processes, posts = handoff
    sid = records[session_index].session_id
    root = records[session_index].cwd
    cards = [first] if session_index == 0 else []
    cards += [_make(store, root=root, tool="codex", author=sid)
              for _ in range(count - len(cards))]
    plans = []
    for rank, card in enumerate(cards, start=1):
        updated, detail = store.update(card["id"], {
            "refine_session_id": sid, "refine_state": "live",
            "batch_id": token, "batch_rank": str(rank)})
        assert updated, detail
        member_plan = plan.parent / f"batch-{session_index}-{rank}.md"
        member_plan.write_text(f"# Plan {rank}\n- **Card:** {card['id']}\n")
        plans.append(member_plan)
    return sid, cards, plans


async def _attach_receipt_members(daemon, sid, plans):
    attached = []
    for plan in plans:
        card, detail = await daemon.attach_plan_by_session(sid, str(plan))
        assert card, detail
        attached.append(card)
    return attached


@pytest.mark.asyncio
async def test_batch_members_extend_one_close_receipt(refinement_handoff):
    d, store, card, plan, records, processes, posts = refinement_handoff
    token = "batch-close-1"
    sid, cards, plans = _codex_batch_receipt_cards(refinement_handoff, token=token)
    attached = await _attach_receipt_members(d, sid, plans)
    receipt = d._refinement_receipts[sid]
    assert len(d._refinement_receipts) == 1
    assert receipt.state == "ready" and receipt.batch_id == token
    assert receipt.card_id == cards[0]["id"]
    assert receipt.members == tuple((row["id"], row["plan_path"], row["updated_at"])
                                    for row in attached)
    assert receipt.expires > time.monotonic() + 590


@pytest.mark.asyncio
async def test_batch_receipt_closes_after_the_last_member(refinement_handoff, monkeypatch):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid, cards, plans = _codex_batch_receipt_cards(refinement_handoff)
    await _attach_receipt_members(d, sid, plans)
    real_settle = d._settle_codex_stop
    consumed = []

    def settle(*args, **kwargs):
        consumed.append(d._refinement_receipts[sid].state)
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(d, "_settle_codex_stop", settle)
    ok, detail = await d.close_refinement_terminal(sid)
    assert ok, detail
    assert "attached plans stay in Backlog" in detail
    assert len(posts) == 1
    assert all(store.get(c["id"])["column_name"] == "backlog" for c in cards)
    assert [store.get(c["id"])["plan_path"] for c in cards] == [
        os.path.realpath(str(p)) for p in plans]
    assert consumed == ["consumed"]
    assert sid not in d._refinement_receipts


async def _attach_outside_batch_and_check(refinement_handoff, outside_token):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid, cards, plans = _codex_batch_receipt_cards(refinement_handoff, count=2)
    await _attach_receipt_members(d, sid, plans)
    outside = _make(store, root=records[0].cwd, tool="codex", author=sid)
    updated, detail = store.update(outside["id"], {
        "refine_session_id": sid, "refine_state": "live",
        "batch_id": outside_token, "batch_rank": "1" if outside_token else ""})
    assert updated, detail
    outside_plan = plan.parent / "outside-batch.md"
    outside_plan.write_text(f"# Outside\n- **Card:** {outside['id']}\n")
    assert (await d.attach_plan_by_session(sid, str(outside_plan)))[0]
    assert d._refinement_receipts[sid].state == "ambiguous"
    assert not (await d.close_refinement_terminal(sid))[0]
    assert posts == []


@pytest.mark.asyncio
async def test_attach_outside_the_batch_still_revokes_close(refinement_handoff):
    await _attach_outside_batch_and_check(refinement_handoff, "")


@pytest.mark.asyncio
async def test_attach_with_a_different_batch_id_still_revokes_close(refinement_handoff):
    await _attach_outside_batch_and_check(refinement_handoff, "another-batch")


@pytest.mark.asyncio
async def test_batch_receipt_of_another_session_is_never_extended(refinement_handoff):
    d, store, card, plan, records, processes, posts = refinement_handoff
    token = "shared-token"
    sid0, cards0, plans0 = _codex_batch_receipt_cards(
        refinement_handoff, count=1, token=token)
    sid1, cards1, plans1 = _codex_batch_receipt_cards(
        refinement_handoff, count=1, token=token, session_index=1)
    await _attach_receipt_members(d, sid0, plans0)
    first_receipt = d._refinement_receipts[sid0]
    await _attach_receipt_members(d, sid1, plans1)
    assert d._refinement_receipts[sid0] is first_receipt
    assert len(first_receipt.members) == 1
    assert d._refinement_receipts[sid1].card_id == cards1[0]["id"]
    assert len(d._refinement_receipts[sid1].members) == 1


@pytest.mark.asyncio
async def test_expired_batch_receipt_refuses(refinement_handoff):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid, cards, plans = _codex_batch_receipt_cards(refinement_handoff, count=2)
    await _attach_receipt_members(d, sid, plans)
    d._refinement_receipts[sid] = replace(d._refinement_receipts[sid], expires=0)
    ok, detail = await d.close_refinement_terminal(sid)
    assert not ok and detail == "No unexpired, unused plan attachment permits this close. Left open."
    assert posts == []


@pytest.mark.asyncio
async def test_batch_receipt_is_single_use(refinement_handoff):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid, cards, plans = _codex_batch_receipt_cards(refinement_handoff, count=2)
    await _attach_receipt_members(d, sid, plans)
    assert (await d.close_refinement_terminal(sid))[0]
    assert not (await d.close_refinement_terminal(sid))[0]
    assert len(posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["bind_session", "edited", "prep"])
async def test_a_started_or_edited_member_refuses_the_batch_close(
        refinement_handoff, mutation):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid, cards, plans = _codex_batch_receipt_cards(refinement_handoff, count=2)
    await _attach_receipt_members(d, sid, plans)
    target = cards[1] if mutation == "prep" else cards[0]
    if mutation == "bind_session":
        changed, detail = store.bind_session(target["id"], records[1].session_id)
    else:
        changed, detail = store.update(target["id"], {
            "summary": "edited"} if mutation == "edited" else {"column_name": "prep"})
    assert changed, detail
    assert not (await d.close_refinement_terminal(sid))[0]
    assert posts == []


@pytest.mark.asyncio
async def test_a_same_member_reattached_renews_nothing(refinement_handoff):
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid, cards, plans = _codex_batch_receipt_cards(refinement_handoff, count=2)
    await _attach_receipt_members(d, sid, plans[:1])
    receipt = d._refinement_receipts[sid]
    d._remember_refinement_attachment(
        sid, store.get(cards[0]["id"]), d._refinement_root_capture(sid),
        records[0].cwd, receipt.journal, batch_id=receipt.batch_id)
    assert d._refinement_receipts[sid] is receipt
    assert receipt.members == ((cards[0]["id"], receipt.plan_path,
                                receipt.card_updated_at),)
    assert d._refinement_receipts[sid].expires == receipt.expires


# --- the gate's second rung: an approved plan that has since been edited --------


def test_the_two_refusals_are_two_sentences():
    """Both surfaces match by prefix, so neither constant may be a prefix of
    the other: two matchers firing on one refusal would silently turn the
    changed-plan confirmation back into the unplanned one, and the button on
    screen would then be agreeing to something it does not say."""
    gate = daemon_mod.PLAN_GATE_REFUSAL
    changed = daemon_mod.PLAN_CHANGED_REFUSAL
    assert gate != changed
    assert not gate.startswith(changed[:20])
    assert not changed.startswith(gate[:20])
    assert changed.startswith(_CHANGED_MATCH_PREFIX)


def test_both_surfaces_match_both_refusals():
    """The cross-language contract for the pair, pinned from the Python side
    for both clients at once."""
    api = (PANEL / "ActionModels.swift").read_text()
    assert f'detail.hasPrefix("{_PANEL_MATCH_PREFIX}")' in api
    assert f'detail.hasPrefix("{_CHANGED_MATCH_PREFIX}")' in api
    actions = (PHONE / "Actions.swift").read_text()
    assert f'planGatePrefix = "{_PANEL_MATCH_PREFIX}"' in actions
    assert f'planChangedPrefix = "{_CHANGED_MATCH_PREFIX}"' in actions


# --- starting the work by itself once the plan lands (`start_when_planned`) ---


async def _drain_auto_starts(d):
    """The auto-start is scheduled, never awaited by the attach — see
    `_schedule_auto_start`, whose whole reason is the channel's five-second
    call timeout. Tests drain it explicitly."""
    tasks = list(d._auto_start_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_a_ticked_card_starts_itself_when_its_plan_lands(daemon,
                                                               tmp_path,
                                                               monkeypatch):
    d, store = daemon
    root, plan = _project(tmp_path)
    _arm_spawn(d, monkeypatch)
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    card = _make(store, root=str(root), start_when_planned="1")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    await _drain_auto_starts(d)
    after = store.get(card["id"])
    assert after["column_name"] == "in_progress"
    assert after["link_state"] == "dispatching"
    # Spent on the attempt: a card sent back and planned again has to be
    # ticked again on purpose.
    assert after["start_when_planned"] == ""


@pytest.mark.asyncio
async def test_an_unticked_card_waits_for_a_person(daemon, tmp_path,
                                                   monkeypatch):
    d, store = daemon
    root, plan = _project(tmp_path)
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    card = _make(store, root=str(root))
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    await _drain_auto_starts(d)
    assert spawns == []
    assert store.get(card["id"])["column_name"] == "backlog"


@pytest.mark.asyncio
async def test_a_hard_refusal_lands_as_an_orange_line_and_moves_nothing(
        daemon, tmp_path, monkeypatch):
    """Dark Army's launcher is off. The card stays where `attach_plan` put it, the
    tick is spent, and the attach's own reply still says the plan attached."""
    d, store = daemon
    root, plan = _project(tmp_path)
    _arm_spawn(d, monkeypatch)
    d.board_dispatch_enabled = False
    card = _make(store, root=str(root), start_when_planned="1")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None and detail == "attached"
    await _drain_auto_starts(d)
    after = store.get(card["id"])
    assert after["column_name"] == "backlog"
    assert after["start_when_planned"] == ""
    assert "not allowed to start sessions" in after["dispatch_error"]


@pytest.mark.asyncio
async def test_an_unknown_project_root_refuses_the_same_way(daemon, tmp_path,
                                                            monkeypatch):
    d, store = daemon
    root, plan = _project(tmp_path)
    _arm_spawn(d, monkeypatch)
    monkeypatch.setattr(d, "_known_project_roots", lambda: set())
    card = _make(store, root=str(root), start_when_planned="1")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None and detail == "attached"
    await _drain_auto_starts(d)
    after = store.get(card["id"])
    assert after["column_name"] == "backlog"
    assert after["start_when_planned"] == ""
    assert after["dispatch_error"]


@pytest.mark.asyncio
async def test_a_second_attach_is_refused_and_schedules_nothing(daemon,
                                                                tmp_path,
                                                                monkeypatch):
    """`attach_plan`'s WHERE guard refuses a card that already has a plan, so
    the trigger can fire at most once per card."""
    d, store = daemon
    root, plan = _project(tmp_path)
    _arm_spawn(d, monkeypatch)
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {os.path.realpath(str(root))})
    card = _make(store, root=str(root), start_when_planned="1")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    await d.attach_plan_by_session("planner-1", str(plan))
    await _drain_auto_starts(d)
    # Re-arm the tick by hand and attach again: the store refuses.
    store.update(card["id"], {"start_when_planned": "1",
                              "refine_session_id": "planner-1",
                              "refine_state": "live"})
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is None, detail
    assert not d._auto_start_tasks
    assert store.get(card["id"])["start_when_planned"] == "1"


@pytest.mark.asyncio
async def test_bobs_own_two_writes_do_not_move_the_revision(daemon, tmp_path,
                                                            monkeypatch):
    """Both `_auto_start_after_refine` writes pass `bump=False`. Without it
    Dark Army's own clear — and its own refusal note — would refuse the save of
    whoever had the card open, which is verbatim the failure
    `REVISED_COLUMNS`' docstring exists to avoid."""
    d, store = daemon
    root, plan = _project(tmp_path)
    _arm_spawn(d, monkeypatch)
    d.board_dispatch_enabled = False
    card = _make(store, root=str(root), start_when_planned="1")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})
    # `attach_plan` bumps once, by design; the auto-start's two writes must
    # not move it again.
    got, _ = await d.attach_plan_by_session("planner-1", str(plan))
    before = got["revision"]
    await _drain_auto_starts(d)
    after = store.get(card["id"])
    assert after["start_when_planned"] == "" and after["dispatch_error"]
    assert after["revision"] == before


# --- the objective rides into the planning session ----------------------------


@pytest.mark.asyncio
async def test_refine_ends_the_prompt_with_the_objective_block(daemon,
                                                               monkeypatch):
    d, store = daemon
    card = _make(store, beneficiary="Ops", intended_benefit="fewer pages",
                 success_criterion="No page in a week.")
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert spawns[0]["argv"] == [
        "/bin/claude",
        dispatch.refine_prompt(card) + dispatch.objective_block(card),
    ]
    assert dispatch.objective_block(card).startswith(
        dispatch.OBJECTIVE_BLOCK_HEAD)


@pytest.mark.asyncio
async def test_refine_with_an_empty_objective_is_argv_unchanged(daemon,
                                                                monkeypatch):
    d, store = daemon
    card = _make(store, beneficiary="", intended_benefit=" ",
                 success_criterion="")
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert spawns[0]["argv"] == ["/bin/claude", dispatch.refine_prompt(card)]


# --- refine_cards: several Prep cards, one planning session --------------------
#
# `plans/2026-09-25-batch-refine-prep-cards.md`. One press hands one session an
# ordered list of cards; each is written exactly as a single Refine writes its
# one, plus a shared `batch_id` / `batch_rank`, and each attaches its own plan
# by the plan file's `- **Card:**` header.


def _batch(store, n=3, **kw):
    return [_make(store, title=f"card {k}", summary=f"thing {k}", **kw)
            for k in range(1, n + 1)]


def _record_events(d, monkeypatch):
    events = []
    monkeypatch.setattr(
        d, "_log_card_event",
        lambda card, kind, session_id="", **detail: events.append(
            (card.get("id"), kind, detail)))
    return events


@pytest.mark.asyncio
async def test_refine_cards_spawns_one_session_and_marks_every_card(
        daemon, monkeypatch):
    """(success criterion — "one session opens") Three Prep cards, one press:
    one spawn whose prompt is the batch form with a block per card, and all
    three cards `dispatching` under one shared batch mark."""
    d, store = daemon
    cards = _batch(store)
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    events = _record_events(d, monkeypatch)
    ok, detail = await d.refine_cards([c["id"] for c in cards])
    assert ok, detail
    assert len(spawns) == 1
    assert spawns[0]["name"] == "refine: 3 cards"
    prompt = spawns[0]["argv"][-1]
    assert prompt.startswith("/ship batch:")
    for k, card in enumerate(cards, start=1):
        assert f"## Card {k} of 3 — {card['title']}" in prompt
        assert f"Card id: {card['id']}" in prompt
    got = [store.get(c["id"]) for c in cards]
    assert {g["refine_state"] for g in got} == {"dispatching"}
    marks = {g["batch_id"] for g in got}
    assert len(marks) == 1 and "" not in marks
    assert [g["batch_rank"] for g in got] == ["1", "2", "3"]
    assert all(g["column_name"] == "prep" and g["session_id"] == ""
               for g in got)
    dispatched = [e for e in events if e[1] == "card_dispatched"]
    assert sorted(e[0] for e in dispatched) == sorted(c["id"] for c in cards)
    assert all(e[2]["batch"] == 3 and e[2]["phase"] == "refinement"
               for e in dispatched)


@pytest.mark.asyncio
async def test_refine_cards_refuses_fewer_than_two_and_more_than_the_cap(
        daemon, monkeypatch):
    d, store = daemon
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    one = _make(store)
    ok, detail = await d.refine_cards([one["id"]])
    assert not ok and detail == daemon_board.BATCH_TOO_FEW_REFUSAL
    # The same card twice is still one card.
    ok, detail = await d.refine_cards([one["id"], one["id"], " "])
    assert not ok and detail == daemon_board.BATCH_TOO_FEW_REFUSAL
    many = _batch(store, n=board_mod.MAX_BATCH_CARDS + 1)
    ok, detail = await d.refine_cards([c["id"] for c in many])
    assert not ok
    assert detail == daemon_board.BATCH_TOO_MANY_REFUSAL.format(
        limit=board_mod.MAX_BATCH_CARDS)
    assert spawns == []
    assert all(store.get(c["id"])["refine_state"] == "" for c in many)


@pytest.mark.asyncio
async def test_refine_cards_refuses_mixed_roots_and_mixed_tools_in_words(
        daemon, monkeypatch, tmp_path):
    d, store = daemon
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    first = _make(store, title="here")
    elsewhere = _make(store, title="over there", root=str(tmp_path))
    ok, detail = await d.refine_cards([first["id"], elsewhere["id"]])
    assert not ok
    assert detail == daemon_board.BATCH_MIXED_ROOT_REFUSAL.format(
        title="over there")
    grok = _make(store, title="the grok one", tool="grok")
    ok, detail = await d.refine_cards([first["id"], grok["id"]])
    assert not ok
    assert detail == daemon_board.BATCH_MIXED_TOOL_REFUSAL.format(
        title="the grok one")
    assert spawns == []


@pytest.mark.asyncio
async def test_refine_cards_refuses_when_one_card_is_already_refining_or_planned(
        daemon, monkeypatch, tmp_path):
    """The first card that could not be refined alone refuses the whole
    press, naming that card — and nothing is written to the others."""
    d, store = daemon
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    cards = _batch(store)
    store.update(cards[1]["id"], {"refine_session_id": "someone",
                                  "refine_state": "live"})
    ok, detail = await d.refine_cards([c["id"] for c in cards])
    assert not ok
    assert detail == "card 2: this card is already being refined"
    for c in (cards[0], cards[2]):
        got = store.get(c["id"])
        assert got["refine_state"] == "" and got["dispatched_at"] is None
        assert got["batch_id"] == ""

    root, plan = _project(tmp_path)
    fresh = _batch(store, n=2)
    planned = _make(store, title="planned")
    assert store.attach_plan(planned["id"], str(plan), "")[0] is not None
    ok, detail = await d.refine_cards([fresh[0]["id"], planned["id"],
                                       fresh[1]["id"]])
    assert not ok
    assert detail.startswith("planned: ")
    assert all(store.get(c["id"])["refine_state"] == "" for c in fresh)
    assert spawns == []


@pytest.mark.asyncio
async def test_refine_cards_refuses_a_member_naming_a_finished_plan(
        daemon, monkeypatch):
    d, store = daemon
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    plain = _make(store, title="plain")
    named = _make(store, title="already planned",
                  prompt="Plan: plans/2026-09-25-x.md\n\nimplement it")
    ok, detail = await d.refine_cards([plain["id"], named["id"]])
    assert not ok
    assert detail == daemon_board.BATCH_NAMED_PLAN_REFUSAL.format(
        title="already planned")
    assert spawns == []


@pytest.mark.asyncio
async def test_batch_in_flight_counts_once(daemon, monkeypatch, tmp_path):
    """Three cards marked `dispatching` by one press are one launch: the
    machine-wide bound (2) is not spent by a single terminal, so a card of
    another project may still refine, and a card of the same project is
    refused only by the per-project rule."""
    d, store = daemon
    _arm_spawn(d, monkeypatch)
    cards = _batch(store)
    ok, detail = await d.refine_cards([c["id"] for c in cards])
    assert ok, detail
    in_flight = d._launch_inflight(store.cards())
    assert len(in_flight) == 1
    other_root = tmp_path / "other"
    other_root.mkdir()
    roots = {"/private/tmp", "/tmp", os.path.realpath(str(other_root))}
    other = _make(store, title="elsewhere", project="other",
                  root=str(other_root))
    ok, detail = dispatch.refine_guard(
        store.get(other["id"]), roots=roots, in_flight=in_flight,
        now=time.time())
    assert ok, detail
    same = _make(store, title="same project")
    ok, detail = dispatch.refine_guard(
        store.get(same["id"]), roots=roots, in_flight=in_flight,
        now=time.time())
    assert not ok
    assert detail == dispatch.PROJECT_BUSY_REFUSAL
    # And a board with no batch is counted exactly as before.
    for c in cards:
        store.update(c["id"], {"batch_id": "", "batch_rank": ""})
    assert len(d._launch_inflight(store.cards())) == 3


@pytest.mark.asyncio
async def test_every_batch_card_binds_to_the_one_session(daemon, monkeypatch):
    d, store = daemon
    d._run_figures_drifted = lambda: False
    _arm_spawn(d, monkeypatch)
    cards = _batch(store)
    ok, detail = await d.refine_cards([c["id"] for c in cards])
    assert ok, detail
    now = time.time()
    snapshot = {"running": [
        {"session_id": "the-planner", "provider": "claude",
         "kind": "interactive", "project": "bob", "cwd": "/tmp",
         "started_at": now + 1},
    ], "waiting": [], "sleeping": [], "finished": []}
    assert d._reconcile_board(snapshot)
    got = [store.get(c["id"]) for c in cards]
    assert {g["refine_session_id"] for g in got} == {"the-planner"}
    assert {g["refine_state"] for g in got} == {"live"}
    assert all(g["column_name"] == "prep" for g in got)


def _batch_project(tmp_path, cards, header=True):
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True, exist_ok=True)
    plans = []
    for k, card in enumerate(cards, start=1):
        plan = root / "plans" / f"plan-{k}.md"
        lines = ["# a plan", ""]
        if header:
            lines.append(f"- **Card:** {card['id']}")
        lines.append("- **Stages:** bc-implementer | bc-verifier")
        plan.write_text("\n".join(lines) + "\n")
        plans.append(plan)
    return root, plans


def _bound_batch(store, root, n=3, sid="planner-1", token="b0a7c4f1e2d3c4b5"):
    cards = _batch(store, n=n, root=str(root))
    for k, card in enumerate(cards, start=1):
        store.update(card["id"], {"refine_session_id": sid,
                                  "refine_state": "live",
                                  "batch_id": token,
                                  "batch_rank": str(k)})
    return [store.get(c["id"]) for c in cards]


@pytest.mark.asyncio
async def test_attach_with_several_bound_cards_picks_by_the_plan_card_header(
        daemon, tmp_path):
    """(success criterion — "the three cards sit in Backlog, each with its own
    attached plan") One session bound to three cards attaches three plans,
    each landing on the card its header names, in whatever order."""
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    cards = _bound_batch(store, root)
    _, plans = _batch_project(tmp_path, cards)
    for k in (1, 0, 2):
        got, detail = await d.attach_plan_by_session("planner-1", str(plans[k]))
        assert got is not None, detail
        assert got["id"] == cards[k]["id"]
    after = [store.get(c["id"]) for c in cards]
    assert [a["column_name"] for a in after] == ["backlog"] * 3
    assert [a["plan_path"] for a in after] == [
        os.path.realpath(str(p)) for p in plans]
    assert {a["batch_id"] for a in after} == {""}
    assert {a["batch_rank"] for a in after} == {""}


@pytest.mark.asyncio
async def test_attach_with_several_bound_cards_and_no_header_fails_closed_listing_them(
        daemon, tmp_path):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    cards = _bound_batch(store, root, n=2)
    _, plans = _batch_project(tmp_path, cards, header=False)
    got, detail = await d.attach_plan_by_session("planner-1", str(plans[0]))
    assert got is None
    assert "- **Card:** <id>" in detail
    for card in cards:
        assert f"{card['id']} — {card['title']}" in detail
    assert all(store.get(c["id"])["column_name"] == "prep" for c in cards)


@pytest.mark.asyncio
async def test_attach_header_naming_a_card_outside_the_set_is_refused(
        daemon, tmp_path):
    """The header chooses *within* the session's bound set and nowhere else:
    naming a real card this session was never handed changes nothing."""
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    cards = _bound_batch(store, root, n=2)
    stranger = _make(store, title="not in the batch", root=str(root))
    _, plans = _batch_project(tmp_path, [stranger])
    got, detail = await d.attach_plan_by_session("planner-1", str(plans[0]))
    assert got is None
    assert "- **Card:** <id>" in detail
    assert stranger["id"] not in detail
    assert store.get(stranger["id"])["plan_path"] == ""
    assert all(store.get(c["id"])["plan_path"] == "" for c in cards)


@pytest.mark.asyncio
async def test_single_card_attach_never_reads_the_header(daemon, tmp_path,
                                                         monkeypatch):
    """One bound card is today's ladder exactly: the header is never read,
    so a header naming another card changes nothing."""
    d, store = daemon
    root, plan = _project(tmp_path)
    card = _make(store, root=str(root))
    other = _make(store, title="other", root=str(root))
    plan.write_text(f"# a plan\n\n- **Card:** {other['id']}\n")
    store.update(card["id"], {"refine_session_id": "planner-1",
                              "refine_state": "live"})

    def never(_path):
        raise AssertionError("a single refinement read the Card header")

    monkeypatch.setattr(board_workflow, "read_plan_card", never)
    got, detail = await d.attach_plan_by_session("planner-1", str(plan))
    assert got is not None, detail
    assert got["id"] == card["id"]
    assert store.get(other["id"])["plan_path"] == ""


@pytest.mark.asyncio
async def test_batch_refinement_ending_planless_leaves_the_note_on_unplanned_cards(
        daemon, tmp_path):
    """Two of three attached, then the session leaves for longer than the
    grace: the third stays in Prep, `ended`, with the note saying its plan
    never arrived — and a single refinement's `ended` still writes no note."""
    d, store = daemon
    d._run_figures_drifted = lambda: False
    root = tmp_path / "proj"
    root.mkdir()
    cards = _bound_batch(store, root)
    _, plans = _batch_project(tmp_path, cards)
    for k in (0, 1):
        got, detail = await d.attach_plan_by_session("planner-1", str(plans[k]))
        assert got is not None, detail
    single = _make(store, title="alone")
    store.update(single["id"], {"refine_session_id": "planner-2",
                                "refine_state": "live"})
    empty = {"running": [], "waiting": [], "sleeping": [], "finished": []}
    d._reconcile_board(empty)                     # starts the absence timers
    for cid in list(d._refine_missing_since):
        d._refine_missing_since[cid] -= d.BOARD_SESSION_GRACE + 1
    assert d._reconcile_board(empty)
    third = store.get(cards[2]["id"])
    assert third["column_name"] == "prep"
    assert third["refine_state"] == "ended"
    assert third["dispatch_error"] == daemon_board.BATCH_UNPLANNED_NOTE.format(
        rank="3")
    assert third["batch_id"] == "" and third["batch_rank"] == ""
    assert [store.get(c["id"])["column_name"] for c in cards[:2]] == [
        "backlog", "backlog"]
    alone = store.get(single["id"])
    assert alone["refine_state"] == "ended" and alone["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_single_refine_argv_and_update_are_byte_identical(daemon,
                                                                monkeypatch):
    """A single Refine after the batch verb landed: the argv is today's
    `/ship <summary>` prompt and the store update carries today's three
    keys and no batch mark."""
    d, store = daemon
    card = _make(store)
    spawns = []
    _arm_spawn(d, monkeypatch, spawns)
    updates = []
    real_update = store.update

    def recording_update(card_id, fields, *a, **kw):
        updates.append((card_id, dict(fields)))
        return real_update(card_id, fields, *a, **kw)

    monkeypatch.setattr(store, "update", recording_update)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert spawns == [{"root": "/tmp", "argv": [
        "/bin/claude", "/ship make the thing work\n\nTitle: do the thing"
        "\n\nInstructions:\ngo"], "name": "refine: do the thing"}]
    mine = [f for cid, f in updates if cid == card["id"]]
    assert len(mine) == 1
    assert set(mine[0]) == {"refine_state", "dispatched_at", "dispatch_error"}
    assert mine[0]["refine_state"] == "dispatching"
    assert mine[0]["dispatch_error"] == ""
    got = store.get(card["id"])
    assert got["batch_id"] == "" and got["batch_rank"] == ""


def _record_closes(d, monkeypatch):
    closed = []

    async def close(sid, *a, **kw):
        closed.append(sid)
        return True, "closed"

    monkeypatch.setattr(d, "_close_session_terminal", close)
    return closed


@pytest.mark.asyncio
async def test_deleting_one_batch_card_keeps_the_shared_planning_terminal(
        daemon, tmp_path, monkeypatch):
    """One session plans every card of a batch: deleting one card cancels
    that card, not the others' interviews. The terminal closes only when the
    last card still being refined goes — and a single refinement's delete
    still closes it at once."""
    d, store = daemon
    closed = _record_closes(d, monkeypatch)
    root = tmp_path / "proj"
    root.mkdir()
    cards = _bound_batch(store, root)
    for card in (cards[2], cards[1]):
        ok, detail = await d.delete_card(card["id"])
        assert ok, detail
        assert closed == []
    ok, detail = await d.delete_card(cards[0]["id"])
    assert ok, detail
    assert closed == ["planner-1"]

    single = _make(store, title="alone")
    store.update(single["id"], {"refine_session_id": "planner-2",
                                "refine_state": "live"})
    ok, detail = await d.delete_card(single["id"])
    assert ok, detail
    assert closed == ["planner-1", "planner-2"]


@pytest.mark.asyncio
async def test_a_batch_whose_siblings_are_planned_closes_on_the_last_delete(
        daemon, tmp_path, monkeypatch):
    """An attached sibling keeps `refine_session_id` as a record but no
    longer needs the terminal, so it does not hold it open."""
    d, store = daemon
    closed = _record_closes(d, monkeypatch)
    root = tmp_path / "proj"
    root.mkdir()
    cards = _bound_batch(store, root)
    _, plans = _batch_project(tmp_path, cards)
    for k in (0, 1):
        got, detail = await d.attach_plan_by_session("planner-1", str(plans[k]))
        assert got is not None, detail
    ok, detail = await d.delete_card(cards[2]["id"])
    assert ok, detail
    assert closed == ["planner-1"]


@pytest.mark.asyncio
async def test_the_last_batch_card_left_still_reads_the_card_header(
        daemon, tmp_path):
    """With one batch card left, a plan whose header names another card —
    one that was reset out of the batch — is refused, not landed on the
    survivor for good; the survivor's own plan then attaches."""
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    cards = _bound_batch(store, root)
    _, plans = _batch_project(tmp_path, cards)
    got, detail = await d.attach_plan_by_session("planner-1", str(plans[0]))
    assert got is not None, detail
    got, detail = await d.reset_card(cards[1]["id"])
    assert got is not None, detail
    got, detail = await d.attach_plan_by_session("planner-1", str(plans[1]))
    assert got is None
    assert "- **Card:** <id>" in detail
    assert f"{cards[2]['id']} — {cards[2]['title']}" in detail
    third = store.get(cards[2]["id"])
    assert third["column_name"] == "prep" and third["plan_path"] == ""
    assert store.get(cards[1]["id"])["plan_path"] == ""
    got, detail = await d.attach_plan_by_session("planner-1", str(plans[2]))
    assert got is not None, detail
    assert got["id"] == cards[2]["id"]
    assert got["plan_path"] == os.path.realpath(str(plans[2]))


@pytest.mark.asyncio
async def test_the_last_batch_card_left_takes_a_plan_with_no_header(
        daemon, tmp_path):
    d, store = daemon
    root = tmp_path / "proj"
    root.mkdir()
    cards = _bound_batch(store, root, n=2)
    _, plans = _batch_project(tmp_path, cards)
    got, detail = await d.attach_plan_by_session("planner-1", str(plans[0]))
    assert got is not None, detail
    bare = root / "plans" / "bare.md"
    bare.write_text("# a plan\n\n- **Stages:** bc-implementer\n")
    got, detail = await d.attach_plan_by_session("planner-1", str(bare))
    assert got is not None, detail
    assert got["id"] == cards[1]["id"]
