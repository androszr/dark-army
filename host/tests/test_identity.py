"""Nickname assignment.

The character belongs to the *agent*, not the repo — several agents run in one
repo at once, and the repo owning a name made the first one to start into the
project and left its siblings with whatever the pool had spare. So most of these
tests are about two things: a name not moving under a live row, and parallel
agents each getting one of their own.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from dark_army_daemon.identity import (
    ART_ONLY, IdentityStore, MAX_SESSION_ENTRIES, NAMES, PRIMARY_CAST, proposed_index,
)


@pytest.fixture
def store(tmp_path):
    return IdentityStore(path=tmp_path / "identities.json")


# ── parallel agents ──────────────────────────────────────────────────────────

def test_agents_in_one_repo_each_get_their_own_character(store):
    """The point of the rework: the repo is not the character."""
    names = set()
    for i in range(6):
        names.add(store.name_for(f"s{i}", names))
    assert len(names) == 6


def test_a_name_freed_by_a_finished_agent_returns_to_the_cast(store):
    first = store.name_for("s1")
    store.forget("s1")
    assert first not in store.assigned().values()


def test_first_picks_come_from_the_main_cast(store):
    taken = set()
    for i in range(PRIMARY_CAST):
        name = store.name_for(f"s{i}", taken)
        assert name in NAMES[:PRIMARY_CAST]
        taken.add(name)


def test_more_agents_than_the_main_cast_fall_through_to_the_rest(store):
    taken = set()
    for i in range(PRIMARY_CAST + 6):
        taken.add(store.name_for(f"s{i}", taken))
    assert len(taken) == PRIMARY_CAST + 6
    assert taken - set(NAMES[:PRIMARY_CAST])


def test_exhausting_the_cast_still_gives_something_unique(store):
    taken = set()
    for i in range(len(NAMES) + 4):
        taken.add(store.name_for(f"s{i}", taken))
    assert len(taken) == len(NAMES) + 4


def test_a_suffixed_name_reserves_its_stem(store):
    assert store.name_for("s2", taken={"Peter-19a7"}) != "Peter"


def test_two_codex_overflow_names_get_different_suffixes(store):
    taken = set(NAMES)
    a = store.name_for("codex:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", taken)
    taken.add(a)
    b = store.name_for("codex:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", taken)
    assert "-" in a and "-" in b
    assert a.split("-", 1)[1] != "code"
    assert b.split("-", 1)[1] != "code"
    assert a.split("-", 1)[1] != b.split("-", 1)[1]


# ── stability ────────────────────────────────────────────────────────────────

def test_name_is_sticky_for_the_session(store):
    name = store.name_for("s1")
    assert store.name_for("s1") == name


def test_name_is_sticky_even_against_a_collision(store):
    """A tombstone stays on screen for half an hour; a later arrival must not
    rename it."""
    name = store.name_for("s1")
    assert store.name_for("s1", taken={name}) == name


def test_a_resumed_session_keeps_its_name(tmp_path):
    """`claude --resume` reuses the session id, so it reuses the character."""
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    name = store.name_for("abc-123")
    store.save()
    assert IdentityStore(path=path).name_for("abc-123") == name


def test_no_session_id_gets_no_name(store):
    assert store.name_for("") == ""


# ── determinism ──────────────────────────────────────────────────────────────

def test_proposed_index_is_stable_across_processes():
    """Not hash(): that is per-process randomised for str, and this project has
    already shipped one bug that only reproduced under some PYTHONHASHSEED."""
    script = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[1])
        from dark_army_daemon.identity import proposed_index
        print(proposed_index("78be5105-f60a-4de3-a80a-b7c8ef9106ff"))
    """)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    seen = set()
    for seed in ("0", "1", "42", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", script, root],
                             capture_output=True, text=True, env=env, check=True)
        seen.add(out.stdout.strip())
    assert len(seen) == 1


def test_a_wiped_state_file_lands_on_the_same_name(tmp_path):
    a = IdentityStore(path=tmp_path / "a.json").name_for("abc-123")
    b = IdentityStore(path=tmp_path / "b.json").name_for("abc-123")
    assert a == b


def test_different_sessions_spread_across_the_cast(tmp_path):
    """A hash that bunched every id onto one name would still pass the collision
    tests above, because the probe would paper over it."""
    names = {IdentityStore(path=tmp_path / f"{i}.json").name_for(f"session-{i}")
             for i in range(40)}
    assert len(names) > 1


# ── persistence ──────────────────────────────────────────────────────────────

def test_assignments_survive_a_restart(tmp_path):
    path = tmp_path / "identities.json"
    first = IdentityStore(path=path)
    name = first.name_for("s1")
    first.save()
    assert IdentityStore(path=path).name_for("s1") == name


def test_save_is_a_no_op_when_nothing_changed(tmp_path):
    path = tmp_path / "identities.json"
    IdentityStore(path=path).save()
    assert not path.exists()          # never written, so never churned


def test_a_non_ascii_nickname_survives_an_ascii_locale(tmp_path, monkeypatch):
    """The LaunchAgent inherits no LANG, so `write_text` defaults to ASCII.
    The night a non-ASCII name was first handed out, `save()` raised
    UnicodeEncodeError out of the agents-snapshot push on every tick: no
    rows, no board reconcile, cards stuck in `dispatching` and `refining`.
    The file names utf-8 at both ends and a save can never raise."""
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    store._sessions = {"s1": "Zażółć"}
    store._dirty = True
    # Stand in for the ASCII locale: Path.write_text/read_text without an
    # explicit encoding go through io.text_encoding → locale. Force ASCII
    # as the default and require the store to name utf-8 itself.
    monkeypatch.setattr("io.text_encoding", lambda enc, stacklevel=2: enc or "ascii")
    store.save()
    assert not store._dirty
    assert json.loads(path.read_bytes().decode("utf-8"))["sessions"]["s1"] == "Zażółć"


def test_save_never_raises_out_of_the_snapshot_path(tmp_path, monkeypatch):
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    store.name_for("s1")

    def boom(self, *a, **k):
        raise UnicodeEncodeError("ascii", "x", 0, 1, "stand-in")
    monkeypatch.setattr(Path, "write_text", boom)
    store.save()                     # logs, does not raise
    assert store._dirty              # still owed, retried next tick


def test_a_corrupt_file_is_survivable(tmp_path):
    path = tmp_path / "identities.json"
    path.write_text("{not json")
    assert IdentityStore(path=path).name_for("s1") in NAMES


def test_session_map_is_bounded(tmp_path):
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    for i in range(MAX_SESSION_ENTRIES + 40):
        store.name_for(f"s{i}")
    store.save()
    saved = json.loads(path.read_text())
    assert len(saved["sessions"]) == MAX_SESSION_ENTRIES
    # The oldest went, not the newest.
    assert f"s{MAX_SESSION_ENTRIES + 39}" in saved["sessions"]
    assert "s0" not in saved["sessions"]


def test_forget_releases_the_name(tmp_path):
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    store.name_for("s1")
    store.forget("s1")
    store.save()
    assert json.loads(path.read_text())["sessions"] == {}


def test_touch_keeps_a_live_agent_out_of_the_prune(tmp_path):
    """Insertion order is the map's age proxy, so without `touch` the prune
    drops the oldest *entry* rather than the oldest *agent* — and the entry a
    session took this morning is the first to go, renaming the one row that has
    been on screen all day."""
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    store.name_for("long-lived")
    for i in range(MAX_SESSION_ENTRIES + 40):
        store.name_for(f"s{i}")
        store.touch("long-lived")
    store.save()
    assert "long-lived" in json.loads(path.read_text())["sessions"]


def test_touch_does_not_dirty_the_store(tmp_path):
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    store.touch("never-seen")
    store.save()
    assert not path.exists()


def test_the_path_is_read_at_call_time(tmp_path, monkeypatch):
    """A default argument would freeze the real ~/.dark-army path into the
    signature, which is how `pytest` came to rewrite a running fleet's
    nicknames."""
    import dark_army_daemon.identity as mod

    monkeypatch.setattr(mod, "IDENTITY_PATH", tmp_path / "redirected.json")
    store = IdentityStore()
    store.name_for("s1")
    store.save()
    assert (tmp_path / "redirected.json").exists()


def test_a_name_no_longer_in_the_cast_is_released(tmp_path):
    """The cast has changed under a sticky assignment more than once (36 → 6,
    then a whole new show). A name made before the change would otherwise be
    kept for life with no face any surface can draw."""
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"version": 2,
                                "sessions": {"s1": "Bertram", "s2": "Cipher"}}))
    store = IdentityStore(path=path)
    assert store.assigned() == {"s2": "Cipher"}
    assert store.name_for("s1") in NAMES


def test_an_old_cast_nickname_is_dropped_on_load(tmp_path):
    """The rename-once-at-upgrade path, and it is not migration code.

    A file holding a retired name, a retired overflow stem and a current name
    is opened by the new build: the two retired entries go (the stem is not
    in the cast, so `_in_cast` misses on both), the current one survives, and
    the dropped session is simply named afresh from the hash. `Bertram` stands
    in for the retired cast here.
    """
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"version": 2, "sessions": {
        "s1": "Bertram", "s2": "Bertram-1a2b", "s3": "Cipher",
        # The 22 Sep 2026 rebrand's own case: a name the old cast used, and
        # its overflow form, are dropped the same way at the first launch.
        "s4": "Elliot", "s5": "Darlene-1a2b"}}))
    store = IdentityStore(path=path)
    assert store.assigned() == {"s3": "Cipher"}
    assert store.name_for("s1") in NAMES
    assert store.name_for("s4") in NAMES
    assert store.name_for("s3") == "Cipher"          # sticky, not reshuffled


def test_overflow_names_keep_their_stem(tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"version": 2, "sessions": {"s1": "Cipher-1a2b"}}))
    assert IdentityStore(path=path).assigned() == {"s1": "Cipher-1a2b"}


def test_the_roster_is_nineteen_and_art_only_is_disjoint():
    assert NAMES == ("Cipher", "Vex", "Ledger", "Mira", "Hex", "Relay",
                     "Forge", "Watch", "Audit", "Proxy", "Quiet", "Nyx",
                     "Canon", "Velvet",
                     "Androll", "Captcha", "Sawa", "Franio", "Zosia", "Ptys")
    # Appended, never inserted: the original fourteen keep their indices, so a
    # saved session keeps its name. Fresh hashes change modulo the new count.
    assert NAMES[:14] == ("Cipher", "Vex", "Ledger", "Mira", "Hex",
                          "Relay", "Forge", "Watch", "Audit", "Proxy",
                          "Quiet", "Nyx", "Canon", "Velvet")
    assert len(set(NAMES)) == 20
    assert PRIMARY_CAST == 20
    assert ART_ONLY == ("overwatch",)
    assert not set(n.lower() for n in NAMES) & set(ART_ONLY)
    # One word each: the tab badge, the strip and the banner all assume it.
    assert all(n.isalpha() for n in NAMES)


def test_the_three_letter_tab_badges_stay_unique():
    """`terminal_title.short` is three letters, so two names sharing a prefix
    would give two VS Code tabs the same badge. That is a constraint on
    *adding* a name, and the six newcomers were chosen to keep it true."""
    from dark_army_daemon.terminal_title import short

    badges = [short(n) for n in NAMES]
    assert len(set(badges)) == len(NAMES), badges
    assert badges == ["Cip", "Vex", "Led", "Mir", "Hex", "Rel", "For", "Wat",
                      "Aud", "Pro", "Qui", "Nyx", "Can", "Vel", "And", "Cap",
                      "Saw", "Fra", "Zos", "Pty"]
    assert {short(n) for n in ("Androll", "Captcha", "Sawa", "Franio", "Zosia", "Ptys")} == {
        "And", "Cap", "Saw", "Fra", "Zos", "Pty"}


# ── the six newcomers are ordinary names ────────────────────────────────────

# A session id whose hash pick *is* that name, so the assertion below is about
# ordinary allocation and not about the collision walk finding it eventually.
FRESH_IDS = {
    "Androll": "probe-7",
    "Captcha": "probe-83",
    "Sawa": "probe-39",
    "Franio": "probe-2",
    "Zosia": "probe-0",
    "Ptys": "probe-1"
}


@pytest.mark.parametrize("name,session_id", sorted(FRESH_IDS.items()))
def test_a_newcomer_is_an_ordinary_first_pick(store, name, session_id):
    """No manual choice and no special case: the newcomers are reached by the
    same hash every other name is reached by."""
    assert NAMES[proposed_index(session_id)] == name
    assert store.name_for(session_id) == name


def test_nineteen_agents_at_once_each_get_a_whole_name(store):
    """The collision walk has to have somewhere to go for all twenty: with
    nineteen taken the twentieth is still a plain cast name, not overflow."""
    taken = set()
    for i in range(len(NAMES)):
        taken.add(store.name_for(f"cap{i}", taken))
    assert taken == set(NAMES)
    assert not any("-" in n for n in taken)


def test_overflow_starts_only_once_all_nineteen_are_held(store):
    """Twenty is the capacity, so the twenty-first agent — and only the
    twenty-first — wears a suffix."""
    taken = set(NAMES)
    twentieth = store.name_for("one-too-many", taken)
    assert "-" in twentieth
    assert twentieth.split("-", 1)[0] in NAMES

    fewer = set(NAMES) - {"Sawa"}
    assert store.name_for("still-room", fewer) == "Sawa"


def test_a_newcomer_and_an_original_both_survive_a_restart(tmp_path):
    """The store is sticky for life and the household names are ordinary members of
    it: a file written before the restart comes back whole."""
    path = tmp_path / "identities.json"
    first = IdentityStore(path=path)
    old_name = first.name_for("older-session", taken=set(NAMES) - {"Cipher"})
    new_name = first.name_for("newer-session", taken=set(NAMES) - {"Franio"})
    assert (old_name, new_name) == ("Cipher", "Franio")
    first.save()

    reopened = IdentityStore(path=path)
    assert reopened.assigned() == {"older-session": "Cipher",
                                   "newer-session": "Franio"}
    assert reopened.name_for("older-session") == "Cipher"
    assert reopened.name_for("newer-session") == "Franio"


def test_a_stored_newcomer_is_recognised_by_load(tmp_path):
    """`_in_cast` is what drops a name this build cannot draw. All six are in
    the cast now, whole and overflowed, so none of them is dropped."""
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"version": 2, "sessions": {
        "s1": "Androll", "s2": "Captcha", "s3": "Sawa", "s4": "Franio",
        "s5": "Sawa-1a2b", "s6": "Bertram", "s7": "Zosia", "s8": "Ptys", "s9": "Ptys-ab12"}}))
    assert IdentityStore(path=path).assigned() == {
        "s1": "Androll", "s2": "Captcha", "s3": "Sawa", "s4": "Franio",
        "s5": "Sawa-1a2b", "s7": "Zosia", "s8": "Ptys", "s9": "Ptys-ab12"}


def test_names_match_ingested_cast_and_baked_icons():
    """One roster, two art trees that may fill in on different days.

    The roster (`NAMES` + `ART_ONLY`) is pinned equal across Python, the
    ingest tool and the panel's Swift. The pixel-art manifest is then held to
    it in **one** direction: it may declare only roster slugs, and every slug
    it does declare must have its frames and its baked light icon. An undrawn
    character is legal; an undeclared one is not.
    """
    import ast
    import re
    from pathlib import Path

    from dark_army_menubar.app import _icon_exists

    repo = Path(__file__).resolve().parents[2]
    ingest_src = (repo / "tools" / "pixelgrid_ingest.py").read_text()
    cast = None
    for node in ast.parse(ingest_src).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == "CAST":
                    cast = list(ast.literal_eval(node.value))
    assert cast is not None

    manifest = json.loads((repo / "assets" / "cast" / "manifest.json").read_text())
    swift = (repo / "panel" / "Sources" / "BobPanel" / "Cast.swift").read_text()
    block = swift.split("static let names")[1].split("]")[0]
    swift_names = re.findall(r'"([^"\W\d_]+)"', block)
    art_block = swift.split("static let artOnly")[1].split("]")[0]
    swift_art_only = re.findall(r'"([^"\W\d_]+)"', art_block)

    lower = [n.lower() for n in NAMES]
    roster = lower + list(ART_ONLY)
    assert lower == [n.lower() for n in swift_names]
    assert list(ART_ONLY) == swift_art_only
    assert cast == roster
    declared = list(manifest["cast"])
    assert set(declared) <= set(roster), set(declared) - set(roster)
    for slug in declared:
        assert (repo / "assets" / "cast" / f"{slug}-work" / "frame_00.png").is_file()
        assert _icon_exists(f"cast-{slug}-work-light-0"), slug


def test_peek_hits_the_map_and_does_not_dirty(tmp_path):
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    store.name_for("s1")
    store.save()
    store._dirty = False
    assert store.peek("s1") == store.assigned()["s1"]
    assert store._dirty is False
    store.save()
    mtime = path.stat().st_mtime
    store.peek("s1")
    store.save()
    assert path.stat().st_mtime == mtime


def test_peek_of_an_unknown_id_is_the_hash_pick_and_does_not_dirty(tmp_path):
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    sid = "never-seen-session"
    assert store.peek(sid) == NAMES[proposed_index(sid)]
    assert store._dirty is False
    assert sid not in store.assigned()
    store.save()
    assert not path.exists()


def test_peek_of_an_empty_id_is_blank(store):
    assert store.peek("") == ""
    assert store._dirty is False


# ── a preferred first pick, and the one rename ───────────────────────────────

def test_a_preferred_name_wins_the_first_pick(store):
    """The role's character, not the hash's. Only ever a *first* pick."""
    sid = "session-with-a-role"
    hashed = NAMES[proposed_index(sid)]
    wanted = next(n for n in NAMES if n != hashed)
    assert store.name_for(sid, (), preferred=wanted) == wanted
    assert store.assigned()[sid] == wanted


def test_a_preferred_name_is_matched_case_insensitively(store):
    assert store.name_for("s1", (), preferred="relay") == "Relay"


def test_a_taken_preferred_name_falls_through_to_the_hash(store):
    """Two live rows called Relay is the confusion the cast exists to
    prevent, so a busy preference is simply not honoured."""
    sid = "s-busy"
    assert store.name_for(sid, {"Relay"}, preferred="Relay") != "Relay"
    assert store.assigned()[sid] == NAMES[proposed_index(sid)]


def test_a_preferred_name_taken_by_an_overflow_stem_is_not_honoured(store):
    sid = "s-stem"
    assert store.name_for(sid, {"Relay-ab12"}, preferred="Relay") != "Relay"


def test_an_off_cast_preferred_name_is_ignored(store):
    """`overwatch` has art but is not a nickname: `load`'s `_in_cast` filter
    would drop it and rename the session at the next daemon restart."""
    sid = "s-planner"
    for name in ART_ONLY:
        assert store.name_for(sid + name, (), preferred=name) \
            == NAMES[proposed_index(sid + name)]


def test_an_empty_preferred_name_is_todays_answer_exactly(store):
    sid = "ordinary-session"
    assert store.name_for(sid, (), preferred="") == NAMES[proposed_index(sid)]


def test_a_session_that_already_has_a_name_ignores_a_preference(store):
    """Stickiness outranks the role: a row somebody may be reading does not
    rename itself just because a card turned up naming it."""
    sid = "s-already"
    first = store.name_for(sid)
    assert store.name_for(sid, (), preferred="Relay") == first


def test_reassign_corrects_an_assigned_session_and_dirties_the_store(store):
    sid = "s-bound"
    first = store.name_for(sid)
    wanted = next(n for n in NAMES if n != first)
    store.save()
    assert store._dirty is False
    assert store.reassign(sid, wanted) is True
    assert store.assigned()[sid] == wanted
    assert store._dirty is True


def test_reassign_refuses_an_unknown_session(store):
    assert store.reassign("never-assigned", "Relay") is False
    assert "never-assigned" not in store.assigned()


def test_reassign_refuses_an_off_cast_name(store):
    sid = "s-off"
    first = store.name_for(sid)
    for name in ART_ONLY:
        assert store.reassign(sid, name) is False
    assert store.assigned()[sid] == first


def test_reassign_refuses_the_name_the_session_already_has(store):
    sid = "s-same"
    first = store.name_for(sid)
    store.save()
    assert store.reassign(sid, first) is False
    assert store._dirty is False


def test_a_reassigned_name_survives_reopening_the_store(tmp_path):
    """The correction is worth nothing if the next daemon restart undoes it:
    `_in_cast` has to accept whatever `reassign` wrote."""
    path = tmp_path / "identities.json"
    store = IdentityStore(path=path)
    sid = "s-persist"
    first = store.name_for(sid)
    wanted = next(n for n in NAMES if n != first)
    assert store.reassign(sid, wanted) is True
    store.save()
    assert IdentityStore(path=path).assigned()[sid] == wanted


@pytest.mark.parametrize("preferred", ["Ptys", "PTYS", "ptys"])
def test_ptys_preference_and_existing_names_survive_restart(tmp_path, preferred):
    path = tmp_path / "identities.json"
    # Upgrade starts with the complete previous roster already assigned.
    previous = {f"old-{i}": name for i, name in enumerate(NAMES[:-1])}
    path.write_text(json.dumps({"version": 2, "sessions": previous}))
    store = IdentityStore(path=path)
    assert store.name_for("new-ptys", preferred=preferred) == "Ptys"
    overflow = store.name_for(FRESH_IDS["Ptys"], taken=set(NAMES))
    assert overflow.startswith("Ptys-")
    store.save()
    reopened = IdentityStore(path=path)
    assert reopened.assigned() == {
        **previous, "new-ptys": "Ptys", FRESH_IDS["Ptys"]: overflow}
    assert reopened.name_for("new-ptys", preferred="Cipher") == "Ptys"


# ── the 22 Sep 2026 rebrand: old slugs read as their successors ─────────────

#: The retired slug at each index and the callsign that took it — the
#: plaintext the daemon's digest-keyed `LEGACY_SLUGS` stands for.
REBRAND = [
    ("elliot", "cipher"), ("darlene", "vex"), ("tyrell", "ledger"),
    ("angela", "mira"), ("mobley", "hex"), ("trenton", "relay"),
    ("romero", "forge"), ("dom", "watch"), ("price", "audit"),
    ("irving", "proxy"), ("leon", "quiet"), ("whiterose", "nyx"),
    ("gideon", "canon"), ("joanna", "velvet"), ("mrrobot", "overwatch"),
]


def test_every_retired_slug_maps_to_the_callsign_at_its_index():
    from dark_army_daemon import identity
    assert len(identity.LEGACY_SLUGS) == len(REBRAND)
    for index, (old, new) in enumerate(REBRAND[:14]):
        assert identity.LEGACY_SLUGS[identity.legacy_key(old)] == new
        assert NAMES[index].lower() == new
    assert identity.LEGACY_SLUGS[identity.legacy_key("mrrobot")] == ART_ONLY[0]


def test_current_crew_maps_old_faces_and_keeps_the_rest_in_order():
    from dark_army_daemon import identity
    faces = {"bc-planner": "mrrobot", "bc-implementer": "elliot",
             "bc-verifier": "ledger", "bc-bug-auditor": "grok-helper"}
    before = dict(faces)
    out = identity.current_crew(faces)
    assert out == {"bc-planner": "overwatch", "bc-implementer": "cipher",
                   "bc-verifier": "ledger", "bc-bug-auditor": "grok-helper"}
    assert list(out) == list(faces)
    assert faces == before                          # pure: input untouched
    assert identity.current_crew({}) == {}


def test_every_cast_name_is_plain_ascii():
    """A non-ASCII letter in a name cost Ptys her Lock Screen portrait on
    25 Sep 2026 — a slug check read her as a stranger. Names, portraits and
    quotes stay ASCII so no wire, file name or shape check can trip again."""
    from dark_army_daemon import identity
    for name in (*identity.NAMES, *identity.ART_ONLY, *identity.QUOTES):
        assert name.isascii(), name


def test_a_name_stored_with_its_old_accent_keeps_its_owner(tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"version": 2, "sessions": {
        "s1": "Ptyś", "s2": "Ptyś-ab12", "s3": "Nobódy"}}),
        encoding="utf-8")
    store = IdentityStore(path=path)
    assert store.name_for("s1") == "Ptys"
    assert store.name_for("s2") == "Ptys-ab12"
    assert "s3" not in store._sessions


def test_a_crew_recorded_with_the_old_accent_reads_back_plain():
    from dark_army_daemon import identity
    assert identity.current_crew({"build": "ptyś", "plan": "vex"}) == {
        "build": "ptys", "plan": "vex"}
