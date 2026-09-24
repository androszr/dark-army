"""`sessions.json` on a temp path: what goes out, what comes back, and what
an older or damaged file still yields."""

import json
import stat

import pytest

from dark_army_daemon.session_store import (
    load_pending_questions,
    load_sessions,
    save_sessions,
)


@pytest.fixture
def store(tmp_path):
    return tmp_path / "state" / "sessions.json"


def _session(**extra):
    return {"state": "idle", "last_event": 42.5, **extra}


def _raw(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document))


def _written(path):
    return json.loads(path.read_text())


# -- a write followed by a read ----------------------------------------------

@pytest.mark.parametrize("sessions", [
    {},
    {"one": _session()},
    {"one": _session(state="working"), "two": _session(state="waiting", last_event=7)},
    {"one": _session(project="host", cwd="/code/proj/host", origin="card:abc")},
])
def test_what_is_saved_is_what_is_loaded(store, sessions):
    save_sessions(sessions, store)
    assert load_sessions(store) == sessions


@pytest.mark.parametrize("children", [set(), {"kid-b", "kid-a"}])
def test_live_subagents_are_a_set_in_memory_and_a_sorted_list_on_disk(store, children):
    save_sessions({"one": _session(subagents=children)}, store)
    assert _written(store)["sessions"]["one"]["subagents"] == sorted(children)
    restored = load_sessions(store)["one"]["subagents"]
    assert isinstance(restored, set) and restored == children


def test_the_monotonic_stamp_never_reaches_the_file_but_the_pid_does(store):
    save_sessions({"one": _session(pid=31337, last_event_monotonic=5.0)}, store)
    on_disk = _written(store)["sessions"]["one"]
    assert on_disk["pid"] == 31337
    assert "last_event_monotonic" not in on_disk


def test_a_later_write_replaces_the_earlier_one(store):
    save_sessions({"first": _session()}, store)
    save_sessions({"second": _session()}, store)
    assert set(load_sessions(store)) == {"second"}


def test_the_write_leaves_one_private_file_and_no_temporaries(store):
    save_sessions({"one": _session()}, store)
    assert [p.name for p in store.parent.iterdir()] == ["sessions.json"]
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


def test_the_retired_slot_keys_are_not_written(store):
    save_sessions({"one": _session()}, store)
    assert set(_written(store)) == {"sessions"}


# -- reading what someone else wrote ------------------------------------------

@pytest.mark.parametrize("document", ["", "{ half a file", "[1, 2]", "null"])
def test_an_unreadable_or_foreign_file_reads_as_nothing(store, document):
    _raw(store, document)
    assert load_sessions(store) == {}


def test_a_missing_file_reads_as_nothing(store):
    assert load_sessions(store) == {}


def test_the_older_top_level_layout_still_loads(store):
    _raw(store, {"old": _session(state="working")})
    assert load_sessions(store) == {"old": _session(state="working")}


def test_keys_an_older_daemon_wrote_are_read_past(store):
    _raw(store, {"sessions": {"kept": _session()},
                 "session_order": [["kept", 1]], "next_display_id": 2})
    assert load_sessions(store) == {"kept": _session()}


@pytest.mark.parametrize("entry", [
    {"last_event": 1.0},
    {"state": "idle"},
    {"state": "idle", "last_event": "yesterday"},
    {"unrelated": True},
    17,
    "text",
    ["a", "list"],
])
def test_an_entry_that_is_not_a_restorable_session_is_left_out(store, entry):
    _raw(store, {"sessions": {"good": _session(), "bad": entry}})
    assert set(load_sessions(store)) == {"good"}


@pytest.mark.parametrize("key, value", [
    ("pid", 555),
    ("last_event_monotonic", 3.0),
    ("async_park_at", 88.0),
])
def test_process_only_readings_are_dropped_on_restore(store, key, value):
    _raw(store, {"sessions": {"one": _session(**{key: value})}})
    assert key not in load_sessions(store)["one"]


@pytest.mark.parametrize("value, survives", [
    ("kid-1", False),
    (5, False),
    ({"kid-1": 100.0}, True),
    (["kid-1"], True),
])
def test_only_a_container_survives_as_the_async_child_map(store, value, survives):
    _raw(store, {"sessions": {"one": _session(subagents_async=value)}})
    assert ("subagents_async" in load_sessions(store)["one"]) is survives


def test_a_live_set_stored_as_anything_but_a_list_is_dropped(store):
    _raw(store, {"sessions": {"one": _session(subagents="kid")}})
    assert "subagents" not in load_sessions(store)["one"]


# -- the questions a restart must not forget ----------------------------------

def test_a_pending_question_comes_back_beside_the_sessions(store):
    asked = {"one": {"text": "Which store?", "options": ["SQLite"], "id": "toolu_9"}}
    save_sessions({"one": _session(state="waiting")}, store, asked)
    assert load_pending_questions(store) == asked
    assert load_sessions(store) == {"one": _session(state="waiting")}


def test_no_questions_writes_no_question_key(store):
    save_sessions({"one": _session()}, store, {})
    assert "pending_questions" not in _written(store)
    assert load_pending_questions(store) == {}


def test_only_non_empty_question_objects_are_read_back(store):
    _raw(store, {"sessions": {}, "pending_questions": {
        "a": "words", "b": {}, "c": {"text": "kept"}, "": {"text": "no id"}}})
    assert load_pending_questions(store) == {"c": {"text": "kept"}}


def test_questions_from_a_missing_file_are_none(store):
    assert load_pending_questions(store) == {}
