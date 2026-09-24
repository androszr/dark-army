# host/tests/test_role_naming.py
"""Naming a session Dark Army dispatched after the job it is doing.

A session's nickname was a pure hash of its session id, so a refinement
session routinely wore the face of the character whose job is drafting cards —
the face and the work disagreeing, which is the one thing a face on a screen
is for. These are the daemon-side seams of the fix: which role each linked
session is doing, which character that role gives it, the first pick, the
one-time correction at the bind, and the write seam that stops a card naming a
helper this project does not have.

Everything Swift about it is pinned by `test_phone_theme_drift.py`; the pure
allocation is `test_crew.py`'s; `test_identity.py` owns `preferred` and
`reassign` on their own.
"""

import asyncio
import threading
import time

import pytest

from dark_army_daemon import (board_workflow, card_prepare, areas,
                                  dispatch, identity, origin)
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.identity import IdentityStore, NAMES, proposed_index

PLANNERS = {"Cipher"}
IMPLEMENTERS = {n for n in NAMES if n.lower() in areas.pool_for("backbone")}



@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json",
                  identities_path=tmp_path / "identities.json")
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
              "summary": "make the thing work", "area": "backbone"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _roster_at(root, *names):
    """A project with a real `.claude/agents` directory naming `names`."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for name in names:
        (agents / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: does the {name} job\n---\n\nbody\n")
    return str(root)


def _visible(d, monkeypatch, *roots):
    """Make `roots` the projects Dark Army can see.

    Every roster judgment is contained to a known project root, so a test that
    does not say a folder is visible is testing the silence, not the refusal.
    """
    seen = {dispatch.normalise_root(str(r)) for r in roots}
    monkeypatch.setattr(d, "_known_project_roots", lambda: seen)
    return seen


# ── which role each linked session is doing ──────────────────────────────────

def test_a_refining_card_names_its_session_as_the_planner(daemon):
    d, _store = daemon
    d._board_state = {"cards": [
        {"id": "card-1", "refine_session_id": "s-plan", "refine_state": "live"},
    ]}
    assert d._card_roles_by_session() == {"s-plan": ("refine", "card-1", "")}


def test_a_live_card_names_its_session_as_the_implementer(daemon):
    d, _store = daemon
    d._board_state = {"cards": [
        {"id": "card-2", "session_id": "s-build", "link_state": "live", "area": "backbone"},
    ]}
    assert d._card_roles_by_session() == {"s-build": ("start", "card-2", "backbone")}


def test_a_dispatching_link_counts_too(daemon):
    d, _store = daemon
    d._board_state = {"cards": [
        {"id": "c", "session_id": "s", "link_state": "dispatching"},
    ]}
    assert d._card_roles_by_session()["s"][0] == "start"


def test_an_ended_or_empty_link_names_nobody(daemon):
    """`update()` does not clear `session_id` when a card leaves In progress,
    so a dead link would otherwise keep naming a session that moved on."""
    d, _store = daemon
    d._board_state = {"cards": [
        {"id": "c1", "session_id": "s-ended", "link_state": "ended"},
        {"id": "c2", "session_id": "s-blank", "link_state": ""},
        {"id": "c3", "refine_session_id": "s-old", "refine_state": "ended"},
    ]}
    assert d._card_roles_by_session() == {}


def test_no_board_state_at_all_names_nobody(daemon):
    d, _store = daemon
    d._board_state = None
    assert d._card_roles_by_session() == {}


def test_the_first_card_in_snapshot_order_wins(daemon):
    d, _store = daemon
    d._board_state = {"cards": [
        {"id": "first", "session_id": "s", "link_state": "live"},
        {"id": "second", "session_id": "s", "link_state": "live"},
    ]}
    assert d._card_roles_by_session()["s"] == ("start", "first", "")


# ── the role's character ─────────────────────────────────────────────────────

def test_a_planner_is_never_mr_robot(daemon):
    """`overwatch` is `bc-planner`'s anchor and is deliberately not in the cast:
    stored, `IdentityStore.load`'s `_in_cast` filter would drop it and rename
    the session at the next daemon restart — the failure stickiness exists to
    prevent. Excluded in exactly one place, here."""
    d, _store = daemon
    art_only = {s.lower() for s in identity.ART_ONLY}
    for card_id in (f"card-{n}" for n in range(40)):
        who = d._role_nickname("refine", card_id, "", ())
        assert who in PLANNERS
        assert who.lower() not in art_only


def test_an_implementer_takes_an_implementer_name(daemon):
    d, _store = daemon
    assert d._role_nickname("start", "card-x", "backbone", ()) in IMPLEMENTERS


def test_every_pool_member_busy_gives_no_answer(daemon):
    """`crew.allocate` returns an honest second copy when the pool is full,
    which is right for a stage marker and wrong for a live row: two agents
    called Relay is the confusion the cast exists to prevent. Rejected here,
    and the caller falls back to the hash walk."""
    d, _store = daemon
    assert d._role_nickname("refine", "card-x", "", PLANNERS) == ""
    assert d._role_nickname("start", "card-x", "backbone", IMPLEMENTERS) == ""


def test_a_busy_overflow_name_still_counts_as_busy(daemon):
    d, _store = daemon
    busy = {f"{n}-ab12" for n in IMPLEMENTERS}
    assert d._role_nickname("start", "card-x", "backbone", busy) == ""


def test_an_unknown_or_empty_stage_gives_no_answer(daemon):
    d, _store = daemon
    assert d._role_nickname("", "card-x", "", ()) == ""
    assert d._role_nickname("sf-planner", "card-x", "", ()) == ""


# ── the first pick ───────────────────────────────────────────────────────────

def _parsed(*session_ids):
    return [({"session_id": sid}, None, None) for sid in session_ids]


def test_a_refining_session_is_named_from_the_planning_pool(daemon):
    d, _store = daemon
    d._board_state = {"cards": [
        {"id": "card-1", "refine_session_id": "s-plan", "refine_state": "live"},
    ]}
    assert d._assign_nicknames(_parsed("s-plan"))["s-plan"] in PLANNERS


def test_a_dispatched_session_is_named_from_the_building_pool(daemon):
    d, _store = daemon
    d._board_state = {"cards": [
        {"id": "card-2", "session_id": "s-build", "link_state": "live", "area": "backbone"},
    ]}
    assert d._assign_nicknames(_parsed("s-build"))["s-build"] in IMPLEMENTERS


def test_a_session_bob_did_not_dispatch_keeps_todays_hash_name(daemon):
    """The out-of-scope half, pinned: an assistant somebody started themselves
    is named exactly as it always was. Asserted against `proposed_index`'s own
    answer rather than a hard-coded string, so nothing here can re-baseline
    it."""
    d, _store = daemon
    d._board_state = {"cards": []}
    sid = "a-session-nobody-dispatched"
    assert d._assign_nicknames(_parsed(sid))[sid] == NAMES[proposed_index(sid)]


def test_a_session_that_already_holds_a_name_is_not_renamed_by_the_pick(daemon):
    d, _store = daemon
    d._board_state = {"cards": []}
    sid = "s-existing"
    first = d._assign_nicknames(_parsed(sid))[sid]
    d._board_state = {"cards": [
        {"id": "c", "refine_session_id": sid, "refine_state": "live"},
    ]}
    assert d._assign_nicknames(_parsed(sid))[sid] == first


# ── the one-time correction at the bind ──────────────────────────────────────

def _dispatching(store, card, *, refine):
    now = time.time()
    key = "refine_state" if refine else "link_state"
    store.update(card["id"], {key: "dispatching", "dispatched_at": now})
    return now


def _snapshot(sid, now):
    return {"running": [
        {"session_id": sid, "provider": "claude", "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 1},
    ]}


def test_a_refinement_bind_renames_its_session_to_a_planner(daemon):
    d, store = daemon
    card = _make(store)
    sid = "the-planner-session"
    now = _dispatching(store, card, refine=True)
    hashed = d._identities.name_for(sid)
    d._nicknames_shown = {sid: hashed}
    d._refine_baseline[card["id"]] = set()

    assert d._bind_refining_card(store.get(card["id"]), _snapshot(sid, now), now + 2)
    assert d._identities.assigned()[sid] in PLANNERS


def test_a_dispatch_bind_renames_its_session_to_an_implementer(daemon):
    d, store = daemon
    card = _make(store, column_name="backlog")
    sid = "the-builder-session"
    now = _dispatching(store, card, refine=False)
    d._nicknames_shown = {sid: d._identities.name_for(sid)}
    d._dispatch_baseline[card["id"]] = set()

    assert d._bind_dispatched_card(store.get(card["id"]), _snapshot(sid, now), now + 2)
    assert d._identities.assigned()[sid] in IMPLEMENTERS


def test_a_second_bind_attempt_changes_nothing(daemon):
    """A bound session is never renamed again — the one exception to
    stickiness is exactly one rename, at exactly one moment."""
    d, store = daemon
    card = _make(store)
    sid = "s-once"
    now = _dispatching(store, card, refine=True)
    d._nicknames_shown = {sid: d._identities.name_for(sid)}
    d._refine_baseline[card["id"]] = set()
    assert d._bind_refining_card(store.get(card["id"]), _snapshot(sid, now), now + 2)
    after = d._identities.assigned()[sid]

    d._nicknames_shown = {sid: after}
    d._rename_for_role(sid, card["id"], "refine", "" )
    assert d._identities.assigned()[sid] == after


def test_a_rename_survives_reopening_the_identity_store(daemon, tmp_path):
    """Worth nothing if the next daemon restart undoes it: `_in_cast` has to
    accept whatever the correction wrote."""
    d, store = daemon
    card = _make(store)
    sid = "s-persist"
    now = _dispatching(store, card, refine=True)
    d._nicknames_shown = {sid: d._identities.name_for(sid)}
    d._refine_baseline[card["id"]] = set()
    assert d._bind_refining_card(store.get(card["id"]), _snapshot(sid, now), now + 2)
    d._identities.save()

    reopened = IdentityStore(path=tmp_path / "identities.json")
    assert reopened.assigned()[sid] == d._identities.assigned()[sid]
    assert reopened.assigned()[sid] in PLANNERS


def test_the_rename_leaves_an_unnamed_session_alone(daemon):
    """`reassign` is a *correction*, not an assignment: a session with no name
    yet is `_assign_nicknames`' business, and writing one here would put an
    entry in the map for a row that may never be drawn."""
    d, _store = daemon
    d._nicknames_shown = {}
    d._rename_for_role("never-assigned", "card-1", "refine", "" )
    assert "never-assigned" not in d._identities.assigned()


def test_the_rename_does_nothing_when_the_whole_pool_is_busy(daemon):
    d, _store = daemon
    sid = "s-crowded"
    first = d._identities.name_for(sid)
    d._nicknames_shown = {sid: first}
    d._nicknames_shown.update({f"other-{n}": n for n in PLANNERS})
    d._rename_for_role(sid, "card-1", "refine", "" )
    assert d._identities.assigned()[sid] == first


# ── the write seam: only helpers this project has ────────────────────────────

@pytest.mark.asyncio
async def test_roster_refuses_an_update_naming_a_helper_this_project_lacks(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = _roster_at(tmp_path / "proj", "bc-implementer", "bc-verifier")
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-implementer", ""),
                                                  ("bc-verifier", "")])
    _visible(d, monkeypatch, root)
    card = _make(store, root=root)

    updated, detail = await d.update_card(card["id"], {"workflow": "sf-planner"})
    assert updated is None
    assert "sf-planner" in detail
    assert "does not have" in detail
    assert store.get(card["id"])["workflow"] == ""


@pytest.mark.asyncio
async def test_roster_accepts_an_update_naming_a_real_helper(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = _roster_at(tmp_path / "proj", "bc-implementer", "bc-verifier")
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-implementer", ""),
                                                  ("bc-verifier", "")])
    _visible(d, monkeypatch, root)
    card = _make(store, root=root)

    updated, detail = await d.update_card(card["id"], {"workflow": "bc-implementer"})
    assert updated is not None, detail
    assert store.get(card["id"])["workflow"] == "bc-implementer"


@pytest.mark.asyncio
async def test_roster_judges_a_move_against_the_root_the_save_is_setting(
        daemon, tmp_path, monkeypatch):
    """The card editor sends `root` and `workflow` in one payload, so a save
    that moves the card *and* edits the box must be judged against the project
    the card is arriving in — not the one it is leaving."""
    d, store = daemon
    old_root = _roster_at(tmp_path / "old", "bc-verifier")
    new_root = _roster_at(tmp_path / "new", "bc-implementer")
    rosters = {old_root: [("bc-verifier", "")],
               new_root: [("bc-implementer", "")]}
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: rosters.get(str(r), []))
    _visible(d, monkeypatch, old_root, new_root)
    card = _make(store, root=old_root)

    # Arriving in a project that *has* the helper: accepted, though the project
    # being left does not have it.
    updated, detail = await d.update_card(
        card["id"], {"root": new_root, "workflow": "bc-implementer"})
    assert updated is not None, detail
    assert store.get(card["id"])["workflow"] == "bc-implementer"

    # And the other way: a helper only the *old* project has is refused.
    refused, detail = await d.update_card(
        card["id"], {"root": new_root, "workflow": "bc-verifier"})
    assert refused is None
    assert "bc-verifier" in detail
    assert store.get(card["id"])["workflow"] == "bc-implementer"


@pytest.mark.asyncio
async def test_a_card_already_holding_an_off_roster_helper_stays_editable(
        daemon, tmp_path, monkeypatch):
    """Only a *changed* workflow is judged, or the one card that most needs a
    person becomes the one card nobody can save."""
    d, store = daemon
    _roster_at(tmp_path / "proj", "bc-implementer")
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-implementer", "")])
    card = _make(store, root=str(tmp_path / "proj"))
    store.update(card["id"], {"workflow": "sf-planner"})

    updated, detail = await d.update_card(card["id"], {"title": "a new title"})
    assert updated is not None, detail
    assert updated["title"] == "a new title"
    assert store.get(card["id"])["workflow"] == "sf-planner"


@pytest.mark.asyncio
async def test_a_project_with_no_agents_directory_refuses_nothing(
        daemon, monkeypatch):
    """A project with no `.claude/agents` directory must not have its board
    locked. `read_roster` never raises, so `[]` covers both "no directory" and
    "nothing in it"."""
    d, store = daemon
    monkeypatch.setattr(card_prepare, "read_roster", lambda r, user_dir=None: [])
    _visible(d, monkeypatch, "/tmp")
    card = _make(store)

    updated, detail = await d.update_card(card["id"], {"workflow": "sf-planner"})
    assert updated is not None, detail
    assert store.get(card["id"])["workflow"] == "sf-planner"


@pytest.mark.asyncio
async def test_clearing_the_box_is_always_allowed(daemon, monkeypatch):
    d, store = daemon
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-implementer", "")])
    card = _make(store)
    store.update(card["id"], {"workflow": "sf-planner"})

    updated, detail = await d.update_card(card["id"], {"workflow": ""})
    assert updated is not None, detail
    assert store.get(card["id"])["workflow"] == ""


@pytest.mark.asyncio
async def test_roster_is_not_read_at_all_for_a_card_naming_no_folder(
        daemon, monkeypatch):
    """`read_roster("")` merges `~/.claude/agents` with a `.claude/agents`
    relative to the daemon's own cwd, so a rootless card would be seeded
    against `$HOME`'s helpers rather than a project's. It is not read."""
    d, _store = daemon
    calls = []

    def _spy(r, user_dir=None):
        calls.append(r)
        return [("bc-verifier", "")]

    monkeypatch.setattr(card_prepare, "read_roster", _spy)
    monkeypatch.setattr(d, "_known_project_roots", lambda: set())

    card, detail = await d.create_card({
        "title": "no folder", "project": "", "root": "",
        "prompt": "just words, no plan", "tool": "claude",
    })
    assert card is not None, detail
    assert calls == []


@pytest.mark.asyncio
async def test_roster_drops_a_plan_seeded_helper_this_project_lacks(
        daemon, tmp_path, monkeypatch):
    """A plan copied out of another repository must not stock this project's
    card with that repository's staff."""
    d, store = daemon
    root = tmp_path / "proj"
    _roster_at(root, "bc-verifier")
    plans = root / "plans"
    plans.mkdir()
    plan = plans / "work.md"
    plan.write_text("# A plan\n\n- **Stages:** sf-planner | bc-verifier\n\n"
                    "## Idea\n\nwords\n")
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-verifier", "")])
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(root)})

    card, detail = await d.create_card({
        "title": "seeded", "project": "proj", "root": str(root),
        "prompt": f"Plan: {plan}", "tool": "claude",
    })
    assert card is not None, detail
    assert card["workflow"] == "bc-verifier"


def test_keep_known_stages_canonicalises_and_preserves_order():
    kept, dropped = board_workflow.keep_known_stages(
        ["BC-Verifier", "sf-planner", "bc-implementer"],
        ["bc-implementer", "bc-verifier"])
    assert kept == ["bc-verifier", "bc-implementer"]
    assert dropped == ["sf-planner"]


def test_resolve_without_a_declared_list_filters_nothing(tmp_path):
    """An empty roster argument means today's behaviour, so every existing
    caller is unchanged."""
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    plan = root / "plans" / "work.md"
    plan.write_text("# A plan\n\n- **Stages:** sf-planner\n\n## Idea\n\nx\n")
    card = {"root": str(root), "prompt": f"Plan: {plan}"}
    assert board_workflow.resolve(card, {str(root)}) == ["sf-planner"]


def test_two_binds_in_one_pass_get_two_different_names(daemon):
    d, store = daemon
    first, second = _make(store), _make(store)
    now = _dispatching(store, first, refine=False)
    _dispatching(store, second, refine=False)
    sid_a, sid_b = "s-pass-0", "s-pass-1"
    d._nicknames_shown = {sid_a: d._identities.name_for(sid_a),
                          sid_b: d._identities.name_for(sid_b, {"Proxy"})}
    d._dispatch_baseline[first["id"]] = set()
    d._dispatch_baseline[second["id"]] = {sid_a}
    assert d._bind_dispatched_card(store.get(first["id"]), _snapshot(sid_a, now), now + 2)
    assert d._bind_dispatched_card(store.get(second["id"]), _snapshot(sid_b, now), now + 2)
    named = d._identities.assigned()
    assert named[sid_a] == "Relay"
    assert named[sid_b] == "Hex"


def test_roster_filters_the_launch_backfill_too(daemon, tmp_path, monkeypatch):
    """The backfill runs on every launch, so without the same roster filter a
    card whose off-roster `workflow` somebody *cleared* — the documented cure —
    would be re-seeded from the plan header on the next daemon restart."""
    d, store = daemon
    root = tmp_path / "proj"
    _roster_at(root, "bc-verifier")
    (root / "plans").mkdir()
    plan = root / "plans" / "work.md"
    plan.write_text("# A plan\n\n- **Stages:** sf-planner | bc-verifier\n\n"
                    "## Idea\n\nwords\n")
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-verifier", "")])
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(root)})
    card = _make(store, root=str(root), prompt=f"Plan: {plan}", workflow="")

    assert d._backfill_board_workflows() == 1
    assert store.get(card["id"])["workflow"] == "bc-verifier"


# ── the refusal is contained to a project Dark Army can see ────────────────────────

@pytest.mark.asyncio
async def test_roster_refuses_nothing_for_a_root_bob_cannot_see(
        daemon, tmp_path, monkeypatch):
    """`root` rides in on the payload — it is in `ApiServer._BOARD_FIELDS` and
    `board_update` is on both `LAN_ACTIONS` and `REMOTE_ACTIONS` — so without
    containment a paired phone could aim the roster walk at any absolute path
    on the machine and read the refuse/accept split as a yes-no oracle for
    "does that directory declare an agent called X". The walk must not happen
    at all, which is why this asserts on `read_roster` never being called and
    not merely on the save landing."""
    d, store = daemon
    outside = _roster_at(tmp_path / "somebody-elses-tree", "bc-implementer")
    asked = []

    def spy(r, user_dir=None):
        asked.append(str(r))
        return [("bc-implementer", "")]

    monkeypatch.setattr(card_prepare, "read_roster", spy)
    _visible(d, monkeypatch, tmp_path / "the-one-real-project")
    card = _make(store, root=outside)

    updated, detail = await d.update_card(card["id"], {"workflow": "sf-planner"})
    assert updated is not None, detail
    assert store.get(card["id"])["workflow"] == "sf-planner"
    assert asked == []


@pytest.mark.asyncio
async def test_roster_refuses_nothing_when_the_card_names_no_folder(
        daemon, monkeypatch):
    """A card that names no folder has no project roster to judge it against.
    `read_roster("")` would merge `~/.claude/agents` with a *relative*
    `.claude/agents` resolved against the daemon's own cwd, so a rootless card
    could be refused by names living in `$HOME`."""
    d, store = daemon
    asked = []

    def spy(r, user_dir=None):
        asked.append(str(r))
        return [("bc-implementer", "")]

    monkeypatch.setattr(card_prepare, "read_roster", spy)
    monkeypatch.setattr(d, "_known_project_roots", lambda: set())
    card = _make(store, root="")

    updated, detail = await d.update_card(card["id"], {"workflow": "sf-planner"})
    assert updated is not None, detail
    assert store.get(card["id"])["workflow"] == "sf-planner"
    assert asked == []


@pytest.mark.asyncio
async def test_the_roster_read_does_not_hold_the_board_write_lock(
        daemon, monkeypatch):
    """A directory walk on an unresponsive mount must not stall every board
    edit on the machine. `update_card`'s docstring promises the lock spans the
    read and the write and nothing else; this is that promise, tested."""
    d, store = daemon
    entered = threading.Event()
    release = threading.Event()

    def hang(r, user_dir=None):
        entered.set()
        assert release.wait(10)
        return [("bc-implementer", "")]

    monkeypatch.setattr(card_prepare, "read_roster", hang)
    _visible(d, monkeypatch, "/tmp")
    slow, other = _make(store), _make(store)

    stalled = asyncio.create_task(
        d.update_card(slow["id"], {"workflow": "bc-implementer"}))
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 10), 10)

    # The board is still writable while that walk hangs.
    updated, detail = await asyncio.wait_for(
        d.update_card(other["id"], {"title": "still moving"}), 5)
    assert updated is not None, detail
    assert updated["title"] == "still moving"

    release.set()
    landed, detail = await asyncio.wait_for(stalled, 10)
    assert landed is not None, detail


# ── an agent's card is filtered, never refused ───────────────────────────────

@pytest.mark.asyncio
async def test_an_agent_card_drops_an_off_roster_stage_rather_than_refusing(
        daemon, tmp_path, monkeypatch):
    """`bob_add_card` is a note a person has to read, and Codex synthesises its
    own stage names. Losing the whole note because one expected helper is a
    stranger is worse than a shorter list, so this path filters where the
    Expected specialists box refuses."""
    d, store = daemon
    root = _roster_at(tmp_path / "proj", "bc-verifier")

    async def fresh(_port):
        return "s1"

    monkeypatch.setattr(d, "_board_request_session_fresh", fresh)
    monkeypatch.setattr(d, "_session_place", lambda _sid: (root, "proj"))
    monkeypatch.setattr(d, "_board_author_guard", lambda _port: (lambda: True))
    monkeypatch.setattr(d, "_board_projects",
                        lambda: [{"root": dispatch.normalise_root(root),
                                  "name": "proj"}])
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-verifier", "")])
    _visible(d, monkeypatch, root)

    reply = await d._handle_board_card_request({
        "type": "board_card_request", "port": 51000,
        "title": "while you are in there", "notes": "also this",
        "stages": "sf-planner\nbc-verifier"})

    assert reply["ok"] is True, reply.get("detail")
    card = store.get(reply["card_id"])
    assert "bc-verifier" in card["workflow"]
    assert "sf-planner" not in card["workflow"]


@pytest.mark.asyncio
async def test_an_agent_card_whose_every_stage_is_off_roster_is_still_created(
        daemon, tmp_path, monkeypatch):
    d, store = daemon
    root = _roster_at(tmp_path / "proj", "bc-verifier")

    async def fresh(_port):
        return "s1"

    monkeypatch.setattr(d, "_board_request_session_fresh", fresh)
    monkeypatch.setattr(d, "_session_place", lambda _sid: (root, "proj"))
    monkeypatch.setattr(d, "_board_author_guard", lambda _port: (lambda: True))
    monkeypatch.setattr(d, "_board_projects",
                        lambda: [{"root": dispatch.normalise_root(root),
                                  "name": "proj"}])
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-verifier", "")])
    _visible(d, monkeypatch, root)

    reply = await d._handle_board_card_request({
        "type": "board_card_request", "port": 51000,
        "title": "a note worth keeping", "notes": "the detail",
        "stages": "sf-planner\nsf-verifier"})

    assert reply["ok"] is True, reply.get("detail")
    card = store.get(reply["card_id"])
    assert card["title"] == "a note worth keeping"
    assert "sf-planner" not in card["workflow"]


@pytest.mark.asyncio
async def test_a_composer_create_drops_an_off_roster_stage_rather_than_refusing(
        daemon, tmp_path, monkeypatch):
    """The phone's report, 8 Sep 2026: Prepare ran with one project in the
    picker, wrote that project's helpers into the faces, then suggested a
    different folder — and every SAVE after that was refused for names the
    composer has no box to correct. A create carries a workflow nobody typed,
    so it drops the strangers; the refusal stays on the card editor."""
    d, _store = daemon
    root = _roster_at(tmp_path / "proj", "bc-implementer", "bc-verifier")
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-implementer", ""),
                                                  ("bc-verifier", "")])
    _visible(d, monkeypatch, root)

    card, detail = await d.create_card({
        "title": "from another project's picker", "project": "proj",
        "root": root, "prompt": "do the thing", "tool": "claude",
        "workflow": "sf-implementer\nsf-verifier\nbc-verifier",
    })
    assert card is not None, detail
    assert card["workflow"] == "bc-verifier"


@pytest.mark.asyncio
async def test_a_composer_create_survives_a_wholly_off_roster_workflow(
        daemon, tmp_path, monkeypatch):
    d, _store = daemon
    root = _roster_at(tmp_path / "proj", "bc-verifier")
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [("bc-verifier", "")])
    _visible(d, monkeypatch, root)

    card, detail = await d.create_card({
        "title": "a card worth keeping", "project": "proj", "root": root,
        "prompt": "do the thing", "tool": "claude",
        "workflow": "sf-implementer\nsf-verifier",
    })
    assert card is not None, detail
    assert card["title"] == "a card worth keeping"
    assert card["workflow"] == ""


@pytest.mark.asyncio
async def test_a_create_against_an_unreadable_roster_keeps_its_stages(
        daemon, tmp_path, monkeypatch):
    """The filter's silence is the seeding branch's silence: a project with no
    agents directory must not have its cards quietly emptied."""
    d, _store = daemon
    root = str(tmp_path / "proj")
    (tmp_path / "proj").mkdir()
    monkeypatch.setattr(card_prepare, "read_roster",
                        lambda r, user_dir=None: [])
    _visible(d, monkeypatch, root)

    card, detail = await d.create_card({
        "title": "no agents here", "project": "proj", "root": root,
        "prompt": "do the thing", "tool": "claude",
        "workflow": "sf-implementer",
    })
    assert card is not None, detail
    assert card["workflow"] == "sf-implementer"


def test_refine_is_cipher(daemon):
    d, _ = daemon
    assert d._role_nickname("refine", "c", "gate", ()) == "Cipher"

def test_busy_cipher_stem_means_no_preference(daemon):
    d, _ = daemon
    assert d._role_nickname("refine", "c", "", {"Cipher-ab12"}) == ""

def test_no_area_uses_universal(daemon):
    d, _ = daemon
    assert d._role_nickname("start", "c", "", ()) == "Proxy"
    assert d._role_nickname("start", "c", "", {"Proxy"}) == "Androll"

@pytest.mark.parametrize("area", areas.AREAS, ids=lambda a: a.slug)
def test_area_start_uses_usual_lead_then_walk(daemon, area):
    d, _ = daemon
    assert d._role_nickname("start", "c", area.slug, ()).lower() == area.pool[0]
    assert d._role_nickname("start", "c", area.slug, {area.pool[0]}).lower() == area.pool[1]
    assert d._role_nickname("start", "c", area.slug, set(area.pool)) == ""


# ── the face does not change when Start is pressed ───────────────────────────

def test_a_started_session_wears_its_lead_before_the_bind(daemon):
    """The first frame a Start's session appears in, the board does not link
    it yet — it used to wear a hashed stranger for a frame and be renamed at
    the bind. Its origin stamp names the card from the first hook."""
    d, _store = daemon
    d._board_state = {"cards": [{"id": "card-g", "area": "gate",
                                 "link_state": "dispatching"}]}
    d._session_states["s-new"] = {"origin": origin.stamp("card-start", "card-g")}
    assert d._assign_nicknames(_parsed("s-new"))["s-new"] == "Nyx"


def test_a_refine_stamp_names_the_chief_of_staff_before_the_bind(daemon):
    d, _store = daemon
    d._board_state = {"cards": [{"id": "card-r", "area": "gate"}]}
    d._session_states["s-plan"] = {"origin": origin.stamp("card-refine", "card-r")}
    assert d._assign_nicknames(_parsed("s-plan"))["s-plan"] == "Cipher"


def test_an_adhoc_stamp_keeps_the_hash_name(daemon):
    d, _store = daemon
    d._board_state = {"cards": []}
    sid = "s-adhoc"
    d._session_states[sid] = {"origin": origin.stamp("adhoc")}
    assert d._assign_nicknames(_parsed(sid))[sid] == NAMES[proposed_index(sid)]


def test_a_card_promises_the_free_lead_and_start_gives_that_one(daemon):
    """Nyx busy on another card: the Gate card shows Watch, not Nyx, and the
    session Start opens is named Watch — the face does not change."""
    d, _store = daemon
    d._nicknames_shown = {"s-other": "Nyx"}
    card = {"id": "card-g", "area": "gate", "column_name": "backlog"}
    assert d._promised_lead(card) == "watch"
    d._board_state = {"cards": [dict(card, link_state="dispatching")]}
    d._session_states["s-new"] = {"origin": origin.stamp("card-start", "card-g")}
    parsed = [({"session_id": "s-other"}, None, None),
              ({"session_id": "s-new"}, None, None)]
    d._identities.name_for("s-other", set(), preferred="Nyx")
    assert d._assign_nicknames(parsed)["s-new"] == "Watch"


def test_the_promise_is_frozen_while_the_card_runs(daemon):
    """Once the new session wears the promised name it is on screen too, and
    a fresh allocation would move the card onto the next member."""
    d, _store = daemon
    card = {"id": "card-g", "area": "gate", "column_name": "backlog"}
    assert d._promised_lead(card) == "nyx"
    d._nicknames_shown = {"s-new": "Nyx"}
    running = dict(card, column_name="in_progress", link_state="live",
                   session_id="s-new")
    assert d._promised_lead(running) == "nyx"


def test_a_running_card_after_a_restart_shows_its_own_session(daemon):
    """No promise survives a restart, and the card's own session counts as
    busy: recomputing would move a Gate card run by Nyx onto Watch."""
    d, _store = daemon
    d._nicknames_shown = {"s-run": "Nyx"}
    running = {"id": "card-g", "area": "gate", "column_name": "in_progress",
               "link_state": "live", "session_id": "s-run"}
    assert d._promised_lead(running) == "nyx"
    d._nicknames_shown = {"s-run": "Relay-ab12"}
    assert d._promised_lead(running) == "relay"


def test_a_done_card_promises_nobody(daemon):
    d, _store = daemon
    assert d._promised_lead({"id": "c", "area": "gate",
                             "column_name": "done"}) == ""


def test_the_board_frame_is_bought_when_the_faces_on_screen_move(daemon):
    d, _store = daemon
    d._nicknames_shown = {}
    assert d._lead_faces_drifted() is False       # an empty screen is no move
    d._nicknames_shown = {"s": "Nyx"}
    assert d._lead_faces_drifted() is True
    assert d._lead_faces_drifted() is False
    d._nicknames_shown = {}
    assert d._lead_faces_drifted() is True        # emptying it is a move
