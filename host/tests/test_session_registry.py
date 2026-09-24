"""Reading Claude Code's live-session registry. It is another program's state
directory, so the tests are mostly about being unbothered by what we find."""
import json

from dark_army_daemon.session_registry import (
    MAX_FILES, RegistryEntry, read_registry,
)


def _write(dirpath, pid, **overrides):
    payload = {
        "pid": pid,
        "sessionId": f"sid-{pid}",
        "cwd": "/repos/bob",
        "kind": "interactive",
        "status": "busy",
        "version": "2.1.232",
        "name": "dark-army-6c",
        "nameSource": "derived",
        "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock",
    }
    payload.update(overrides)
    (dirpath / f"{pid}.json").write_text(json.dumps(payload))
    return payload


def test_reads_the_live_sessions(tmp_path):
    _write(tmp_path, 41588)
    _write(tmp_path, 41106, sessionId="sid-other", cwd="/repos/stocks")
    found = read_registry(tmp_path)
    assert set(found) == {"sid-41588", "sid-other"}
    assert found["sid-41588"].pid == 41588
    assert found["sid-41588"].cwd == "/repos/bob"


def test_derived_names_are_not_user_chosen(tmp_path):
    """The default slug is exactly what `_session_name` step 3 rejects, so the
    registry must not smuggle it back in at step 0."""
    _write(tmp_path, 1, nameSource="derived", name="dark-army-6c")
    assert read_registry(tmp_path)["sid-1"].named_by_user is False


def test_an_explicit_name_is_user_chosen(tmp_path):
    _write(tmp_path, 1, nameSource="explicit", name="the migration")
    entry = read_registry(tmp_path)["sid-1"]
    assert entry.named_by_user is True
    assert entry.name == "the migration"


def test_explicit_but_empty_is_not_a_name(tmp_path):
    _write(tmp_path, 1, nameSource="explicit", name="")
    assert read_registry(tmp_path)["sid-1"].named_by_user is False


def test_addressability_follows_the_socket(tmp_path):
    _write(tmp_path, 1)
    _write(tmp_path, 2, sessionId="sid-2", messagingSocketPath="")
    found = read_registry(tmp_path)
    assert found["sid-1"].addressable is True
    assert found["sid-2"].addressable is False


def test_a_missing_directory_is_the_empty_case(tmp_path):
    assert read_registry(tmp_path / "nope") == {}


def test_a_file_caught_mid_rewrite_is_skipped(tmp_path):
    _write(tmp_path, 1)
    (tmp_path / "2.json").write_text('{"sessionId": "sid-2", "pi')
    found = read_registry(tmp_path)
    assert set(found) == {"sid-1"}


def test_an_entry_without_a_session_id_is_dropped(tmp_path):
    """Without one there is nothing to match it against."""
    (tmp_path / "1.json").write_text(json.dumps({"pid": 1, "name": "x"}))
    assert read_registry(tmp_path) == {}


def test_a_non_object_payload_is_survivable(tmp_path):
    (tmp_path / "1.json").write_text("[]")
    _write(tmp_path, 2, sessionId="sid-2")
    assert set(read_registry(tmp_path)) == {"sid-2"}


def test_bad_types_do_not_raise(tmp_path):
    (tmp_path / "1.json").write_text(json.dumps(
        {"sessionId": "sid-1", "pid": "not-a-pid", "name": None,
         "messagingSocketPath": 17}))
    entry = read_registry(tmp_path)["sid-1"]
    assert entry.pid is None
    assert entry.name == ""


def test_file_count_is_bounded(tmp_path):
    for pid in range(MAX_FILES + 20):
        _write(tmp_path, pid, sessionId=f"sid-{pid}")
    assert len(read_registry(tmp_path)) == MAX_FILES


def test_defaults_are_empty_not_none():
    entry = RegistryEntry()
    assert entry.name == "" and entry.addressable is False
