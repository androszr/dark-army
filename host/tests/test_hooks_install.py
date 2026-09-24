"""Dark Army's hooks in each assistant's settings: merged beside the
person's own, never duplicated, rewritten when the table moves on, and read
back field for field so an outdated install is noticed at launch.
"""

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from dark_army_menubar import hooks
from dark_army_daemon.protocol import POST_TOOL_USE_MATCHER
from dark_army_menubar.hooks import (
    GROK_RULES_TEXT,
    HOOKS_CONFIG,
    HOOK_COMMAND,
    NOTIFY_SCRIPT,
    are_grok_rules_installed,
    are_hooks_installed,
    install_hooks,
)


@pytest.fixture(autouse=True)
def settings_path(tmp_path, monkeypatch):
    """Every assistant's configuration file lives in this test's temp folder."""
    p = tmp_path.joinpath("settings.json")
    grok_dir = tmp_path / "grok-hooks"
    grok_rules = tmp_path / "grok-rules"
    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS_PATH", p)
    monkeypatch.setattr(hooks, "GROK_HOOKS_DIR", grok_dir)
    monkeypatch.setattr(hooks, "GROK_HOOKS_PATH", grok_dir / "dark-army.json")
    monkeypatch.setattr(hooks, "GROK_RULES_DIR", grok_rules)
    monkeypatch.setattr(hooks, "GROK_RULES_PATH", grok_rules / "dark-army.md")
    monkeypatch.setattr(hooks, "GROK_CONFIG_PATH", tmp_path / "grok-config.toml")
    monkeypatch.setattr(hooks, "CODEX_CONFIG_PATH", tmp_path / "codex-config.toml")
    monkeypatch.setattr(hooks, "CODEX_HOOKS_PATH", tmp_path / "codex-hooks.json")
    # `install_hooks()` writes the two hook scripts first (the 20 Sep 2026
    # lock-out); under pytest `paths.STATE_DIR` is already a per-process
    # temp dir, so they never land in the real `~/.dark-army`.
    return p


def _write_handler() -> None:
    hooks.install_notify_script()


def _up_to_date() -> bool:
    """What launch asks before deciding to install: is every harness current?"""
    verdict = are_hooks_installed()
    assert isinstance(verdict, bool)
    return verdict


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _store(path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings), encoding="utf-8")


def _commands_for(settings: dict, event: str) -> list[str]:
    """Every command registered for `event`, across its groups, skipping any
    malformed group (`{"hooks": null}` and the like) the installer leaves be."""
    groups = settings.get("hooks", {}).get(event, [])
    return [hook.get("command", "")
            for group in groups if isinstance(group, dict)
            for hook in (group.get("hooks") if isinstance(group.get("hooks"), list) else [])
            if isinstance(hook, dict)]


def _our_command_count(settings: dict, event: str) -> int:
    ours = (HOOK_COMMAND, hooks.SHUNT_COMMAND)
    return len([c for c in _commands_for(settings, event) if any(o in c for o in ours)])


def _group(command, matcher=None, **hook_keys):
    group = {"hooks": [{"type": "command", "command": command, **hook_keys}]}
    if matcher is not None:
        group["matcher"] = matcher
    return group


def test_codex_hooks_include_approval_group_without_persistent_grant():
    config = hooks.codex_hooks_config()
    assert set(config) == {"PreToolUse", "PermissionRequest"}
    approval = config["PermissionRequest"][0]["hooks"][0]
    assert approval["timeout"] == 1860
    assert approval["command"] == HOOK_COMMAND
    assert "updatedPermissions" not in json.dumps(config)


def _installed_then(settings_path, change):
    """Install, apply `change` to the settings on disk, write them back."""
    install_hooks()
    written = _read(settings_path)
    change(written["hooks"])
    _store(settings_path, written)


# --- A first install ---


def test_a_first_install_writes_settings_that_read_as_current(settings_path):
    install_hooks()
    assert settings_path.is_file()
    assert _up_to_date()


@pytest.mark.parametrize("event", sorted(HOOKS_CONFIG))
def test_every_event_in_the_table_runs_our_command_after_install(settings_path, event):
    install_hooks()
    assert _our_command_count(_read(settings_path), event) == len(HOOKS_CONFIG[event])


# --- Nobody else's hooks are touched ---


@pytest.mark.parametrize("event, theirs", [
    ("SessionStart", _group("/Users/me/bin/on-start.sh")),
    ("PostToolUse", _group("/Users/me/bin/after-bash", matcher="Bash")),
    ("PermissionRequest", _group("/opt/vendor/approve-hook")),
], ids=["no-matcher", "other-matcher", "vendor-permission-hook"])
def test_a_persons_own_group_survives_and_ours_joins_it(settings_path, event, theirs):
    _store(settings_path, {"hooks": {event: [theirs]}})
    install_hooks()
    installed = _read(settings_path)["hooks"][event]
    assert installed[0] == theirs, "their group must stay first and unchanged"
    assert _our_command_count(_read(settings_path), event) == len(HOOKS_CONFIG[event])


def test_keys_outside_hooks_are_left_as_they_were(settings_path):
    _store(settings_path, {"model": "sonnet", "permissions": {"deny": ["WebFetch"]}, "hooks": {}})
    install_hooks()
    written = _read(settings_path)
    assert (written["model"], written["permissions"]) == ("sonnet", {"deny": ["WebFetch"]})


def test_a_group_shared_with_the_person_keeps_theirs_and_ours_once(settings_path):
    shared = {"hooks": [{"type": "command", "command": "/Users/me/bin/mine"},
                        {"type": "command", "command": HOOK_COMMAND}]}
    _store(settings_path, {"hooks": {"SessionStart": [shared]}})
    install_hooks()
    written = _read(settings_path)
    assert "/Users/me/bin/mine" in _commands_for(written, "SessionStart")
    assert _our_command_count(written, "SessionStart") == 1


def test_a_command_merely_mentioning_our_script_is_not_ours(settings_path):
    wrapper = "/usr/bin/time " + HOOK_COMMAND
    _store(settings_path, {"hooks": {"SessionStart": [_group(wrapper)]}})
    install_hooks()
    commands = _commands_for(_read(settings_path), "SessionStart")
    assert wrapper in commands
    assert HOOK_COMMAND in commands, "our own exact command still has to be added"


def test_installing_again_and_again_adds_nothing(settings_path):
    for _ in range(3):
        install_hooks()
    written = _read(settings_path)
    assert {event: _our_command_count(written, event) for event in HOOKS_CONFIG} == {
        event: len(groups) for event, groups in HOOKS_CONFIG.items()}


# --- When an install reads as outdated ---


def test_are_hooks_installed_true_after_install(settings_path):
    install_hooks()
    assert _up_to_date()


def _drop_permission_timeout(groups):
    for group in groups["PermissionRequest"]:
        for hook in group["hooks"]:
            hook.pop("timeout", None)


@pytest.mark.parametrize("change", [
    lambda groups: groups.pop("PostToolUse"),
    lambda groups: groups.update(PostToolUse=[_group(HOOK_COMMAND, matcher="Bash")]),
    lambda groups: groups["PostToolUse"].append(_group(HOOK_COMMAND, matcher="Bash")),
    _drop_permission_timeout,
    lambda groups: groups.update(SessionStart=[{"hooks": None}]),
], ids=["event-missing", "wrong-matcher", "stray-group-of-ours", "timeout-missing",
        "malformed-group"])
def test_an_install_that_differs_from_the_table_reads_as_outdated(settings_path, change):
    _installed_then(settings_path, change)
    assert not _up_to_date()


def test_settings_without_hooks_read_as_not_installed(settings_path):
    _store(settings_path, {})
    assert not _up_to_date()


# --- What the next install repairs ---


def test_a_wildcard_group_of_ours_gives_way_to_the_scoped_one(settings_path):
    """An old install hooked PostToolUse for every tool; the table scopes it
    to the ask-user tools. Only the scoped group may remain."""
    _store(settings_path, {"hooks": {"PostToolUse": [_group(HOOK_COMMAND)]}})
    install_hooks()
    matchers = [g.get("matcher") for g in _read(settings_path)["hooks"]["PostToolUse"]]
    assert matchers == [POST_TOOL_USE_MATCHER]


def test_a_stray_group_of_ours_is_removed_by_the_next_install(settings_path):
    _installed_then(settings_path, lambda groups: groups["PostToolUse"].append(
        _group(HOOK_COMMAND, matcher="Bash")))
    install_hooks()
    left = _read(settings_path)["hooks"]["PostToolUse"]
    assert [g.get("matcher") for g in left] == [POST_TOOL_USE_MATCHER]
    assert _up_to_date()


def test_the_permission_timeout_is_put_back_without_a_second_group(settings_path):
    """The pinned timeout has to reach a settings file that already exists;
    judging a group on its command alone once let it read as current."""
    _installed_then(settings_path, _drop_permission_timeout)
    install_hooks()
    ours = [h for g in _read(settings_path)["hooks"]["PermissionRequest"]
            for h in g["hooks"] if h.get("command") == HOOK_COMMAND]
    assert [h.get("timeout") for h in ours] == [1860]
    assert _up_to_date()


def _shared_permission_group():
    return {"hooks": [{"type": "command", "command": "/opt/vendor/their-thing"},
                      {"type": "command", "command": HOOK_COMMAND}]}


def test_a_shared_group_is_never_called_outdated(settings_path):
    """Not ours to rewrite, so it is judged on our command being present:
    anything stricter would append a duplicate that fires our script twice."""
    _installed_then(settings_path, lambda groups: groups.update(
        PermissionRequest=[_shared_permission_group()]))
    assert _up_to_date()
    install_hooks()
    assert _our_command_count(_read(settings_path), "PermissionRequest") == 1


def test_a_stale_group_of_ours_beside_a_shared_one_is_still_outdated(settings_path):
    """The shared group must not answer for the pair: the stale one behind it
    is exactly what the check exists to find."""
    stale = _group(HOOK_COMMAND)

    _installed_then(settings_path, lambda groups: groups.update(
        PermissionRequest=[_shared_permission_group(), stale]))
    assert not _up_to_date()
    install_hooks()
    written = _read(settings_path)
    assert _our_command_count(written, "PermissionRequest") == 1
    assert "/opt/vendor/their-thing" in _commands_for(written, "PermissionRequest")
    assert _up_to_date()


def test_a_malformed_group_neither_crashes_the_install_nor_blocks_ours(settings_path):
    _store(settings_path, {"hooks": {"SessionStart": [{"matcher": "X", "hooks": None}]}})
    install_hooks()
    assert HOOK_COMMAND in _commands_for(_read(settings_path), "SessionStart")


def test_hook_command_unsets_the_frozen_apps_pythonhome():
    """Grok runs PreToolUse in the session's env. A session Dark Army started from
    the .app inherits PYTHONHOME; python3 then dies before the notify
    script is parsed. The command must strip those names first."""
    from dark_army_daemon.subprocess_env import PY2APP_ENV_VARS
    from dark_army_daemon.paths import NOTIFY_SCRIPT_PATH

    assert HOOK_COMMAND.startswith("/usr/bin/env ")
    assert HOOK_COMMAND.endswith(" python3 " + str(NOTIFY_SCRIPT_PATH))
    for name in PY2APP_ENV_VARS:
        assert f"-u {name}" in HOOK_COMMAND, name


def test_the_hook_wrapper_starts_python_under_a_leaked_pythonhome():
    """The screenshot: every Grok tool showed a failed PreToolUse because
    `python3` inherited the .app's PYTHONHOME."""
    import subprocess

    env = dict(os.environ)
    # A home that is not a Python prefix: system 3.9 and Homebrew 3.12 both
    # die with "Python path configuration" — the same fatal the Grok hook
    # showed. The real .app tree is a 3.12 prefix, so Homebrew python3 can
    # limp along inside it and would hide the bug.
    env["PYTHONHOME"] = "/tmp/dark-army-no-such-python-home"
    env["PYTHONPATH"] = "/tmp/dark-army-no-such-python-home"
    bare = subprocess.run(
        ["python3", "-c", "print(1)"],
        env=env, capture_output=True, text=True)
    assert bare.returncode != 0
    wrapped = subprocess.run(
        "/usr/bin/env -u PYTHONHOME -u PYTHONPATH python3 -c 'print(1)'",
        env=env, shell=True, capture_output=True, text=True)
    assert wrapped.returncode == 0, wrapped.stderr
    assert wrapped.stdout.strip() == "1"


def test_an_older_bare_path_command_is_still_ours():
    """An installed settings file from before the env -u wrapper must be
    recognised as ours so upgrade replaces it, not duplicates it."""
    from dark_army_daemon.paths import NOTIFY_SCRIPT_PATH
    from dark_army_menubar.hooks import _command_is_ours

    assert _command_is_ours(str(NOTIFY_SCRIPT_PATH)) is True
    assert _command_is_ours(HOOK_COMMAND) is True
    assert _command_is_ours("/bin/cat " + str(NOTIFY_SCRIPT_PATH)) is False


# --- An unparseable settings.json must not be destroyed silently --------------

def test_unparseable_settings_is_backed_up_and_left_alone(tmp_path, monkeypatch):
    """An unreadable settings.json is copied aside and then *not written*.

    This used to assert the opposite — back up, then replace the file with a
    fresh object holding only our hooks. A backup makes that recoverable; it
    does not make it right. `install_hooks` runs unattended on every launch,
    and the file it cannot parse is reachable in practice (some Claude Code
    builds tolerate comments this strict parser rejects), so "replace the
    user's model, permissions, env and their own hooks" was a scheduled event.
    The Grok half of the same feature has always refused; this now matches it.
    """
    from dark_army_menubar import hooks as h

    settings = tmp_path / "settings.json"
    original = '{\n  // a comment some builds allow\n  "model": "opusplan",\n}\n'
    settings.write_text(original, encoding="utf-8")
    monkeypatch.setattr(h, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(h, "NOTIFY_SCRIPT_PATH", tmp_path / "notify")
    monkeypatch.setattr(h, "GROK_HOOKS_DIR", tmp_path / "grok-hooks")
    monkeypatch.setattr(h, "GROK_HOOKS_PATH", tmp_path / "grok-hooks" / "dark-army.json")

    h.install_hooks()

    backups = list(tmp_path.glob("settings.json.*.bak"))
    assert backups, "the unreadable settings file was read without a copy"
    assert backups[0].read_text(encoding="utf-8") == original
    # And the original is still exactly where it was: the user's config is not
    # ours to replace with our own hooks block.
    assert settings.read_text(encoding="utf-8") == original


def test_valid_settings_are_not_backed_up(tmp_path, monkeypatch):
    """No stray .bak on the normal path."""
    from dark_army_menubar import hooks as h

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opusplan"}), encoding="utf-8")
    monkeypatch.setattr(h, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(h, "NOTIFY_SCRIPT_PATH", tmp_path / "notify")
    monkeypatch.setattr(h, "GROK_HOOKS_DIR", tmp_path / "grok-hooks")
    monkeypatch.setattr(h, "GROK_HOOKS_PATH", tmp_path / "grok-hooks" / "dark-army.json")

    h.install_hooks()

    assert not list(tmp_path.glob("settings.json.*.bak"))
    assert json.loads(settings.read_text(encoding="utf-8"))["model"] == "opusplan"


def test_install_writes_grok_hooks_file(settings_path):
    install_hooks()
    grok_path = hooks.GROK_HOOKS_PATH
    assert grok_path.exists()
    data = json.loads(grok_path.read_text())
    assert "StopCancelled" in data["hooks"]
    assert "SessionStart" in data["hooks"]
    assert hooks.are_grok_hooks_installed() is True


def test_install_writes_grok_rules_file(settings_path):
    """Grok ignores SessionStart stdout, so the bob-actions hint has to
    live in a home rule it actually loads. Missing or stale is the same
    'outdated' signal as a missing hooks file."""
    assert are_grok_rules_installed() is False
    assert not _up_to_date()
    install_hooks()
    path = hooks.GROK_RULES_PATH
    assert path.read_text(encoding="utf-8") == GROK_RULES_TEXT
    assert "bob-actions" in GROK_RULES_TEXT
    assert "ask_user_question" in GROK_RULES_TEXT
    assert "## Work done" in GROK_RULES_TEXT
    assert "search-scope.json" in GROK_RULES_TEXT
    assert "Where to search" in GROK_RULES_TEXT
    assert are_grok_rules_installed() is True
    assert _up_to_date()
    path.write_text("stale", encoding="utf-8")
    assert are_grok_rules_installed() is False
    assert not _up_to_date()


def test_the_grok_hooks_file_carries_no_timeout_key(settings_path):
    """The `timeout` is Claude's PermissionRequest pin and buys Grok nothing —
    Grok never fires that event. It is untested whether Grok's hook loader
    tolerates an unknown key on a hook dict, and a strict validator rejecting
    the file would take every other event down with it."""
    install_hooks()
    data = json.loads(hooks.GROK_HOOKS_PATH.read_text())
    for event, groups in data["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                assert "timeout" not in hook, event
    # And the config the check compares against agrees, so a Grok install is
    # never perpetually "outdated".
    assert hooks.are_grok_hooks_installed() is True
    assert '"timeout": 1860' not in json.dumps(
        hooks.grok_hooks_config(), indent=2)


def test_second_corruption_does_not_destroy_the_first_backup(tmp_path, monkeypatch):
    """A fixed .bak name means a second failure overwrites the only surviving
    copy of the user's original config. Each backup must stand on its own."""
    from dark_army_menubar import hooks as h
    import time as _time

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(h, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(h, "NOTIFY_SCRIPT_PATH", tmp_path / "notify")
    monkeypatch.setattr(h, "GROK_HOOKS_DIR", tmp_path / "grok-hooks")
    monkeypatch.setattr(h, "GROK_HOOKS_PATH", tmp_path / "grok-hooks" / "dark-army.json")

    settings.write_text('{"model": "the-original", }', encoding="utf-8")
    h.install_hooks()
    _time.sleep(1.1)                     # backup names are second-resolution
    settings.write_text("also broken {{{", encoding="utf-8")
    h.install_hooks()

    backups = sorted(tmp_path.glob("settings.json.*.bak"))
    assert len(backups) == 2, f"expected both backups kept, got {backups}"
    assert any("the-original" in b.read_text(encoding="utf-8") for b in backups)


# ── the terminal title env key ───────────────────────────────────────────────
#
# Claude Code writes its own tab title and rewrites it whenever the work changes,
# so the daemon's title writer only has a tab to name once this key is set. The
# pair of them is one feature; see dark_army_daemon/terminal_title.py.

def test_set_title_env_disables_claudes_own_writer(settings_path):
    hooks.set_title_env()
    assert _read(settings_path)["env"][hooks.TITLE_ENV_KEY] == "1"
    assert hooks.is_title_env_installed() is True


def test_set_title_env_keeps_the_users_other_env(settings_path):
    settings_path.write_text(json.dumps({"env": {"FOO": "bar"}}))
    hooks.set_title_env()
    env = _read(settings_path)["env"]
    assert env["FOO"] == "bar" and env[hooks.TITLE_ENV_KEY] == "1"


def test_set_title_env_is_idempotent(settings_path):
    hooks.set_title_env()
    first = settings_path.read_text()
    hooks.set_title_env()
    assert settings_path.read_text() == first


def test_title_env_survives_hook_install(settings_path):
    """Both write the same file through load_claude_settings; neither may drop
    what the other put there."""
    hooks.set_title_env()
    install_hooks()
    assert hooks.is_title_env_installed() is True
    assert _up_to_date()


def test_set_title_env_disables_groks_own_writer(settings_path):
    """Same preference, different file: Grok's writer lives in config.toml."""
    hooks.set_title_env()
    text = hooks.GROK_CONFIG_PATH.read_text()
    assert "enabled = false" in text
    assert hooks._grok_title_enabled(text) == (True, False)


def test_set_title_env_keeps_the_rest_of_grok_config(settings_path):
    hooks.GROK_CONFIG_PATH.write_text(
        "[ui]\ncompact_mode = false\n\n"
        "[ui.notifications.title]\nenabled = true\nitems = [\"session-name\"]\n"
    )
    hooks.set_title_env()
    text = hooks.GROK_CONFIG_PATH.read_text()
    assert "compact_mode = false" in text
    assert "items = [\"session-name\"]" in text
    assert hooks._grok_title_enabled(text) == (True, False)


def test_grok_title_writer_leaves_a_broken_config_alone(settings_path):
    hooks.GROK_CONFIG_PATH.write_text("this is not [toml")
    hooks.set_title_env()
    assert hooks.GROK_CONFIG_PATH.read_text() == "this is not [toml"


def test_set_title_env_disables_codex_own_writer(settings_path):
    hooks.set_title_env()

    text = hooks.CODEX_CONFIG_PATH.read_text(encoding="utf-8")
    assert hooks._CODEX_TITLE_MARKER in text
    assert tomllib.loads(text)["tui"]["terminal_title"] == []


def test_codex_title_keeps_existing_tui_content_and_is_idempotent(settings_path):
    original = '[tui]\nnotifications = true\n\n[features]\napps = true\n'
    hooks.CODEX_CONFIG_PATH.write_text(original, encoding="utf-8")

    hooks.set_title_env()
    first = hooks.CODEX_CONFIG_PATH.read_text(encoding="utf-8")
    hooks.set_title_env()

    assert hooks.CODEX_CONFIG_PATH.read_text(encoding="utf-8") == first
    assert "notifications = true" in first
    assert "[features]\napps = true" in first
    assert first.count(hooks._CODEX_TITLE_MARKER) == 1


@pytest.mark.parametrize("value", ['[]', '["activity", "project-name"]'])
def test_codex_title_never_claims_an_unmarked_user_value(settings_path, value):
    original = f"[tui]\nterminal_title = {value}\n"
    hooks.CODEX_CONFIG_PATH.write_text(original, encoding="utf-8")

    hooks.set_title_env()

    assert hooks.CODEX_CONFIG_PATH.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("original", [
    "this is not [toml",
    "tui = { notifications = true }\n",
    "tui.notifications = true\n",
])
def test_codex_title_leaves_invalid_or_ambiguous_toml_alone(settings_path, original):
    hooks.CODEX_CONFIG_PATH.write_text(original, encoding="utf-8")

    hooks.set_title_env()

    assert hooks.CODEX_CONFIG_PATH.read_text(encoding="utf-8") == original


def test_codex_title_leaves_invalid_utf8_alone(settings_path):
    original = b'model = "gpt-5"\n\xff\xfe'
    hooks.CODEX_CONFIG_PATH.write_bytes(original)

    hooks.set_title_env()

    assert hooks.CODEX_CONFIG_PATH.read_bytes() == original


def test_codex_title_leaves_a_symlink_and_its_target_alone(settings_path):
    target = hooks.CODEX_CONFIG_PATH.with_name("real-codex-config.toml")
    original = '[tui]\nnotifications = true\n'
    target.write_text(original, encoding="utf-8")
    hooks.CODEX_CONFIG_PATH.symlink_to(target)

    hooks.set_title_env()

    assert hooks.CODEX_CONFIG_PATH.is_symlink()
    assert target.read_text(encoding="utf-8") == original


def test_codex_title_atomic_replace_preserves_file_mode(settings_path):
    hooks.CODEX_CONFIG_PATH.write_text('model = "gpt-5"\n', encoding="utf-8")
    hooks.CODEX_CONFIG_PATH.chmod(0o600)

    hooks._set_codex_title_writer()

    assert stat.S_IMODE(hooks.CODEX_CONFIG_PATH.stat().st_mode) == 0o600


def test_codex_title_config_is_replaced_atomically(settings_path, monkeypatch):
    original = 'model = "gpt-5.6-sol"\n'
    hooks.CODEX_CONFIG_PATH.write_text(original, encoding="utf-8")
    seen = {}
    real_replace = os.replace

    def _watch(src, dst):
        seen["src"] = str(src)
        return real_replace(src, dst)

    monkeypatch.setattr(hooks.os, "replace", _watch)
    hooks._set_codex_title_writer()

    assert ".dark-army." in seen["src"] and seen["src"].endswith(".tmp")
    assert tomllib.loads(hooks.CODEX_CONFIG_PATH.read_text())["tui"]["terminal_title"] == []


def test_codex_title_replace_failure_preserves_original(settings_path, monkeypatch):
    original = 'model = "gpt-5.6-sol"\n'
    hooks.CODEX_CONFIG_PATH.write_text(original, encoding="utf-8")

    def _refuse(_src, _dst):
        raise OSError("refused")

    monkeypatch.setattr(hooks.os, "replace", _refuse)
    hooks._set_codex_title_writer()

    assert hooks.CODEX_CONFIG_PATH.read_text(encoding="utf-8") == original
    assert not list(settings_path.parent.glob("codex-config.toml.dark-army.*.tmp"))


def test_title_env_is_absent_when_never_asked_for(settings_path):
    install_hooks()
    assert hooks.is_title_env_installed() is False


# --- Writers refuse a file they could not read -------------------------------

def test_title_env_refuses_an_unreadable_settings_file(tmp_path, monkeypatch):
    """The tab badge is cosmetic and runs on every launch. Reducing an
    unparseable settings.json to a single env key is not a trade it gets to
    make — the Grok half of this same pair has always refused."""
    from dark_army_menubar import hooks as h

    settings = tmp_path / "settings.json"
    original = '{\n  // a comment some builds allow\n  "model": "opusplan",\n}\n'
    settings.write_text(original, encoding="utf-8")
    monkeypatch.setattr(h, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(h, "GROK_CONFIG_PATH", tmp_path / "grok.toml")

    h.set_title_env()

    assert settings.read_text(encoding="utf-8") == original


def test_grok_title_is_left_alone_when_the_table_is_declared_inline(tmp_path, monkeypatch):
    """Appending `[ui.notifications.title]` beside a dotted or inline
    declaration of the same table is a duplicate declaration, and Grok then
    cannot load its config at all — unattended, on every launch."""
    from dark_army_menubar import hooks as h

    config = tmp_path / "grok.toml"
    original = '[ui.notifications]\ntitle = { items = ["session-name"] }\n'
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(h, "GROK_CONFIG_PATH", config)

    h._set_grok_title_writer()

    assert config.read_text(encoding="utf-8") == original
    # Still valid TOML, which is the whole point.
    tomllib.loads(config.read_text(encoding="utf-8"))


def test_grok_title_is_left_alone_when_enabled_is_dotted(tmp_path, monkeypatch):
    from dark_army_menubar import hooks as h

    config = tmp_path / "grok.toml"
    original = "[ui.notifications]\ntitle.enabled = true\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(h, "GROK_CONFIG_PATH", config)

    h._set_grok_title_writer()

    assert config.read_text(encoding="utf-8") == original
    tomllib.loads(config.read_text(encoding="utf-8"))


def test_settings_are_written_atomically(tmp_path, monkeypatch):
    """A truncating write that dies partway leaves a file Claude Code cannot
    parse and that every writer here will then decline to touch — stuck broken
    until somebody edits it by hand."""
    from dark_army_menubar import hooks as h

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opusplan"}), encoding="utf-8")
    monkeypatch.setattr(h, "CLAUDE_SETTINGS_PATH", settings)

    seen = {}
    real_replace = os.replace

    def _watch(src, dst):
        seen["src"] = str(src)
        return real_replace(src, dst)

    monkeypatch.setattr(h.os, "replace", _watch)
    h.write_claude_settings({"model": "opusplan", "env": {"X": "1"}})

    assert seen["src"].endswith(".tmp")
    assert json.loads(settings.read_text(encoding="utf-8"))["env"] == {"X": "1"}


def test_the_notify_script_carries_the_current_version_stamp():
    """A stamp that did not move is how an upgraded daemon ends up refusing
    every session on the machine: the installed script would go on sending no
    enrolment key. `install_notify_script()` rewrites on any content change,
    so the window is one launch wide — but only if the stamp is real."""
    assert "# NOTIFY_SCRIPT_VERSION: 2026-09-23-private-hook-socket" in NOTIFY_SCRIPT


def test_installing_replaces_an_older_notify_script(tmp_path, monkeypatch):
    """Dark Army rewrites the handler on every launch, which is what closes the
    upgrade window in which an old copy sends no key and is refused."""
    from dark_army_menubar import hooks as hooks_mod
    path = tmp_path / "dark-army-notify"
    path.write_text("# NOTIFY_SCRIPT_VERSION: 2026-01-01-ancient\n")
    monkeypatch.setattr(hooks_mod, "NOTIFY_SCRIPT_PATH", path)
    monkeypatch.setattr(hooks_mod, "ensure_state_dir", lambda: tmp_path)
    hooks_mod.install_notify_script()
    assert path.read_text() == hooks_mod.NOTIFY_SCRIPT
    assert "ancient" not in path.read_text()


# ── installs are atomic and skipped when unchanged ───────────────────────────
#
# The notify script, the statusline collector and the Grok hooks file are all
# exec'd/read by name while this app rewrites them at launch. A truncating
# `write_text` therefore raced every hook event, and rewriting an unchanged
# file on every launch made the race a scheduled one.


@pytest.fixture
def notify_path(tmp_path, monkeypatch):
    path = tmp_path / "dark-army-notify"
    monkeypatch.setattr(hooks, "NOTIFY_SCRIPT_PATH", path)
    monkeypatch.setattr(hooks, "ensure_state_dir", lambda: tmp_path)
    return path


def test_an_unchanged_notify_script_is_not_rewritten(notify_path):
    """Same content, same exec bit → no write at all. The inode is the proof:
    the atomic install goes through `os.replace`, which always changes it."""
    _write_handler()
    before = notify_path.stat()
    _write_handler()
    after = notify_path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_a_current_script_without_its_exec_bit_is_reinstalled(notify_path):
    _write_handler()
    notify_path.chmod(0o644)
    _write_handler()
    assert notify_path.stat().st_mode & stat.S_IXUSR


def test_the_notify_script_is_replaced_never_truncated(notify_path, monkeypatch):
    """A hook firing mid-install must see either the old script or the new one.
    With `os.replace` taken away, the write fails and the old copy survives —
    a truncating write would have left a half-written file behind."""
    old = "# NOTIFY_SCRIPT_VERSION: 2026-01-01-ancient\n"
    notify_path.write_text(old, encoding="utf-8")
    notify_path.chmod(0o755)

    def broken_replace(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(hooks.os, "replace", broken_replace)
    with pytest.raises(OSError):
        _write_handler()
    assert notify_path.read_text(encoding="utf-8") == old
    assert not list(notify_path.parent.glob("*.tmp"))


# ── the /ship close-out script is installed the same way ────────────────────
#
# A file copied from the repo (or the bundle's Resources) rather than an
# embedded string, compared by content on every launch. Every project's copy
# hands off to this one, so a stale installed copy would be worse than none.

CLOSE_OUT_REPO_FILE = (Path(__file__).resolve().parents[2]
                       / ".claude" / "skills" / "ship" / "close-out.sh")


@pytest.fixture
def close_out_path(tmp_path, monkeypatch):
    path = tmp_path / "dark-army-close-out"
    monkeypatch.setattr(hooks, "CLOSE_OUT_SCRIPT_PATH", path)
    monkeypatch.setattr(hooks, "ensure_state_dir", lambda: tmp_path)
    return path


def test_close_out_source_is_the_repo_file_on_a_checkout():
    src = hooks.close_out_source()
    assert src is not None
    assert src.name == "close-out.sh"
    assert src.read_bytes() == CLOSE_OUT_REPO_FILE.read_bytes()


def test_the_close_out_script_is_installed_as_an_executable_byte_copy(close_out_path):
    hooks.install_close_out_script()
    assert close_out_path.read_bytes() == CLOSE_OUT_REPO_FILE.read_bytes()
    assert close_out_path.stat().st_mode & stat.S_IXUSR


def test_an_unchanged_close_out_script_is_not_rewritten(close_out_path):
    hooks.install_close_out_script()
    before = close_out_path.stat()
    hooks.install_close_out_script()
    after = close_out_path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_a_close_out_script_without_its_exec_bit_is_reinstalled(close_out_path):
    hooks.install_close_out_script()
    close_out_path.chmod(0o644)
    hooks.install_close_out_script()
    assert close_out_path.stat().st_mode & stat.S_IXUSR


def test_a_stale_close_out_script_is_replaced(close_out_path):
    close_out_path.write_text("#!/bin/bash\necho ancient\n", encoding="utf-8")
    close_out_path.chmod(0o755)
    hooks.install_close_out_script()
    assert close_out_path.read_bytes() == CLOSE_OUT_REPO_FILE.read_bytes()


def test_no_close_out_source_leaves_the_existing_copy_alone(close_out_path, monkeypatch):
    """A clean install with no bundled resource and no checkout: the copy on
    disk is still the best one there is. Never unlinked, never rewritten."""
    old = "#!/bin/bash\necho whatever is there\n"
    close_out_path.write_text(old, encoding="utf-8")
    close_out_path.chmod(0o755)
    monkeypatch.setattr(hooks, "close_out_source", lambda: None)
    hooks.install_close_out_script()
    assert close_out_path.read_text(encoding="utf-8") == old
    assert not list(close_out_path.parent.glob("*.tmp"))


def test_an_unchanged_grok_hooks_file_is_not_rewritten(settings_path):
    hooks.install_grok_hooks()
    before = hooks.GROK_HOOKS_PATH.stat()
    hooks.install_grok_hooks()
    after = hooks.GROK_HOOKS_PATH.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_a_changed_grok_hooks_file_is_replaced_atomically(settings_path):
    hooks.GROK_HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    hooks.GROK_HOOKS_PATH.write_text("{}", encoding="utf-8")
    hooks.install_grok_hooks()
    data = json.loads(hooks.GROK_HOOKS_PATH.read_text(encoding="utf-8"))
    assert "SessionStart" in data["hooks"]
    assert not list(hooks.GROK_HOOKS_DIR.glob("*.tmp"))


# ── the backup is a write path's move, taken once per distinct content ───────


def test_a_reader_never_mints_a_backup(tmp_path, monkeypatch):
    """`is_title_env_installed` runs on every launch. With the backup on the
    read path, a settings.json Dark Army will never write filled ~/.claude with an
    identical .bak per launch."""
    from dark_army_menubar import hooks as h

    settings = tmp_path / "settings.json"
    settings.write_text('{"model": "opusplan", }', encoding="utf-8")  # broken
    monkeypatch.setattr(h, "CLAUDE_SETTINGS_PATH", settings)

    assert h.is_title_env_installed() is False
    assert h.load_claude_settings() == {}
    assert not list(tmp_path.glob("settings.json.*.bak"))


def test_the_same_broken_content_is_backed_up_once(tmp_path, monkeypatch):
    """Write paths back up — but a second write path finding the same bytes
    adds nothing to what the first backup already preserves."""
    from dark_army_menubar import hooks as h

    settings = tmp_path / "settings.json"
    settings.write_text('{"model": "the-original", }', encoding="utf-8")
    monkeypatch.setattr(h, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(h, "NOTIFY_SCRIPT_PATH", tmp_path / "notify")
    monkeypatch.setattr(h, "GROK_HOOKS_DIR", tmp_path / "grok-hooks")
    monkeypatch.setattr(h, "GROK_HOOKS_PATH",
                        tmp_path / "grok-hooks" / "dark-army.json")

    h.install_hooks()
    h.install_hooks()
    h._set_claude_title_env()

    backups = list(tmp_path.glob("settings.json.*.bak"))
    assert len(backups) == 1
    assert "the-original" in backups[0].read_text(encoding="utf-8")


def test_settings_json_keeps_its_mode_across_the_atomic_write(settings_path):
    settings_path.write_text("{}", encoding="utf-8")
    settings_path.chmod(0o600)
    install_hooks()
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o600
    assert not list(settings_path.parent.glob("settings.json.*.tmp"))


def test_refinement_bundle_source_is_installed_verbatim(close_out_path, tmp_path, monkeypatch):
    from dark_army_menubar import dev_build
    bundle = tmp_path / 'Dark Army.app'
    resource = bundle / 'Contents/Resources/dark-army-close-out.sh'
    resource.parent.mkdir(parents=True)
    resource.write_bytes(CLOSE_OUT_REPO_FILE.read_bytes())
    monkeypatch.setattr(dev_build, 'bundle_path', lambda: bundle)
    assert hooks.close_out_source() == resource
    hooks.install_close_out_script()
    assert close_out_path.read_bytes() == resource.read_bytes()
    assert b'close_refinement_terminal' in close_out_path.read_bytes()
    assert b'CODEX_THREAD_ID' in close_out_path.read_bytes()


# --- the shunt guard: a second managed group, and Codex's own file ---


def _shunt_groups(settings: dict, event: str = "PreToolUse") -> list:
    return [g for g in settings.get("hooks", {}).get(event, [])
            if isinstance(g, dict) and any(
                hooks.SHUNT_COMMAND in h.get("command", "")
                for h in g.get("hooks", []) if isinstance(h, dict))]


def test_shunt_group_registers_beside_an_untouched_notify_group(settings_path):
    install_hooks()
    groups = _read(settings_path)["hooks"]["PreToolUse"]
    assert groups[0] == {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
    assert groups[1] == {"matcher": hooks.SHUNT_MATCHER,
                         "hooks": [{"type": "command", "command": hooks.SHUNT_COMMAND}]}
    assert len(groups) == 2
    # The notify entry is the one every other event carries, byte for byte.
    assert groups[0] == HOOKS_CONFIG["SessionStart"][0]


def test_shunt_command_is_ours_and_a_substring_is_not():
    assert hooks._command_is_ours(hooks.SHUNT_COMMAND)
    assert hooks._command_is_ours(str(hooks.SHUNT_SCRIPT_PATH))
    assert hooks._command_is_ours(hooks.SHUNT_COMMAND + " --flag")
    assert not hooks._command_is_ours(f"cat {hooks.SHUNT_SCRIPT_PATH}")
    assert hooks._is_our_managed_group(HOOKS_CONFIG["PreToolUse"][1])


def test_shunt_group_is_pruned_and_rewritten_on_a_matcher_change(settings_path):
    """An older install wrote the guard under a narrower matcher; the current
    one replaces it rather than leaving two guards firing."""
    settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Read", "hooks": [{"type": "command", "command": hooks.SHUNT_COMMAND}]},
    ]}}))
    assert not _up_to_date()
    install_hooks()
    groups = _shunt_groups(_read(settings_path))
    assert len(groups) == 1 and groups[0]["matcher"] == hooks.SHUNT_MATCHER
    assert _up_to_date()


def test_shunt_install_is_idempotent_and_keeps_a_users_group_with_the_same_matcher(settings_path):
    settings_path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": hooks.SHUNT_MATCHER,
         "hooks": [{"type": "command", "command": "/their/guard"}]},
    ]}}))
    install_hooks()
    install_hooks()
    groups = _read(settings_path)["hooks"]["PreToolUse"]
    theirs = [g for g in groups if any(h.get("command") == "/their/guard"
                                       for h in g.get("hooks", []))]
    assert theirs == [{"matcher": hooks.SHUNT_MATCHER,
                       "hooks": [{"type": "command", "command": "/their/guard"}]}]
    # Their group already carries the matcher, so ours is not appended beside
    # it (`_our_hook_present` finds none of ours there → ours is added once).
    assert len(_shunt_groups(_read(settings_path))) == 1
    assert _our_command_count(_read(settings_path), "PreToolUse") == 2


def test_shunt_group_reaches_the_grok_hooks_file_without_a_timeout(settings_path):
    install_hooks()
    data = json.loads(hooks.GROK_HOOKS_PATH.read_text())
    groups = _shunt_groups(data)
    assert len(groups) == 1 and groups[0]["matcher"] == hooks.SHUNT_MATCHER
    assert "timeout" not in groups[0]["hooks"][0]


def test_codex_hooks_file_carries_shunt_and_permission_groups(settings_path):
    install_hooks()
    data = json.loads(hooks.CODEX_HOOKS_PATH.read_text())
    assert list(data) == ["hooks"]
    assert list(data["hooks"]) == ["PreToolUse", "PermissionRequest"]
    assert data["hooks"]["PreToolUse"] == [HOOKS_CONFIG["PreToolUse"][1]]
    assert data["hooks"]["PermissionRequest"] == HOOKS_CONFIG["PermissionRequest"]
    assert hooks.are_codex_hooks_installed() is True


def test_codex_hooks_file_is_key_merged_keeping_foreign_events_and_groups(settings_path):
    hooks.CODEX_HOOKS_PATH.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{"matcher": "shell",
                            "hooks": [{"type": "command", "command": "/their/lint"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/their/stop"}]}],
        },
        "their_key": {"kept": True},
    }))
    install_hooks()
    data = json.loads(hooks.CODEX_HOOKS_PATH.read_text())
    assert data["their_key"] == {"kept": True}
    assert data["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "/their/stop"}]}]
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == {"matcher": "shell", "hooks": [{"type": "command", "command": "/their/lint"}]}
    assert pre[1] == HOOKS_CONFIG["PreToolUse"][1]
    assert len(pre) == 2


def test_codex_hooks_install_is_idempotent_and_atomic(settings_path):
    install_hooks()
    before = hooks.CODEX_HOOKS_PATH.stat()
    install_hooks()
    after = hooks.CODEX_HOOKS_PATH.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    leftovers = [p for p in hooks.CODEX_HOOKS_PATH.parent.iterdir()
                 if p.name.startswith("codex-hooks.json.")]
    assert leftovers == []


def test_codex_hooks_unparseable_file_is_backed_up_once_then_replaced(settings_path):
    hooks.CODEX_HOOKS_PATH.write_text("{not json")
    install_hooks()
    backups = list(hooks.CODEX_HOOKS_PATH.parent.glob("codex-hooks.json.*.bak"))
    assert len(backups) == 1 and backups[0].read_text() == "{not json"
    assert hooks.are_codex_hooks_installed() is True
    # The same broken content is backed up once, not on every launch.
    hooks.CODEX_HOOKS_PATH.write_text("{not json")
    install_hooks()
    assert len(list(hooks.CODEX_HOOKS_PATH.parent.glob("codex-hooks.json.*.bak"))) == 1


def test_a_missing_codex_hooks_file_reads_as_not_installed(settings_path):
    install_hooks()
    assert _up_to_date()
    hooks.CODEX_HOOKS_PATH.unlink()
    assert not _up_to_date()


@pytest.fixture
def shunt_path(tmp_path, monkeypatch):
    path = tmp_path / "dark-army-shunt"
    monkeypatch.setattr(hooks, "SHUNT_SCRIPT_PATH", path)
    monkeypatch.setattr(hooks, "ensure_state_dir", lambda: tmp_path)
    return path


def test_the_shunt_script_is_installed_executable_and_not_rewritten_when_current(shunt_path):
    hooks.install_shunt_script()
    assert shunt_path.read_text() == hooks.SHUNT_SCRIPT
    assert shunt_path.stat().st_mode & stat.S_IXUSR
    before = shunt_path.stat()
    hooks.install_shunt_script()
    after = shunt_path.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    shunt_path.write_text("older\n")
    hooks.install_shunt_script()
    assert shunt_path.read_text() == hooks.SHUNT_SCRIPT


def test_install_hooks_alone_writes_both_scripts_before_the_settings_name_them(
        settings_path, tmp_path, monkeypatch):
    """The write-before-register order is structural, inside `install_hooks()`
    itself, so a third caller can never recreate the lock-out: by the time
    the Claude settings file is written, both scripts it names are on disk
    and current."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(hooks, "NOTIFY_SCRIPT_PATH", state / "dark-army-notify")
    monkeypatch.setattr(hooks, "SHUNT_SCRIPT_PATH", state / "dark-army-shunt")
    monkeypatch.setattr(hooks, "ensure_state_dir", lambda: state)
    seen = {}
    original = hooks.write_claude_settings

    def _watch(settings):
        seen["notify"] = (hooks.NOTIFY_SCRIPT_PATH.is_file()
                          and hooks.NOTIFY_SCRIPT_PATH.read_text() == NOTIFY_SCRIPT)
        seen["shunt"] = (hooks.SHUNT_SCRIPT_PATH.is_file()
                         and hooks.SHUNT_SCRIPT_PATH.read_text() == hooks.SHUNT_SCRIPT)
        return original(settings)

    monkeypatch.setattr(hooks, "write_claude_settings", _watch)
    assert not hooks.NOTIFY_SCRIPT_PATH.exists() and not hooks.SHUNT_SCRIPT_PATH.exists()
    assert install_hooks() is True
    assert seen == {"notify": True, "shunt": True}
    assert hooks.SHUNT_COMMAND in _commands_for(_read(settings_path), "PreToolUse")
    assert HOOK_COMMAND in _commands_for(_read(settings_path), "PreToolUse")
    assert hooks.SHUNT_SCRIPT_PATH.stat().st_mode & stat.S_IXUSR
    assert hooks.NOTIFY_SCRIPT_PATH.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("harness", ["codex", "grok"])
def test_install_hooks_survives_a_harness_home_that_is_a_regular_file(
        settings_path, tmp_path, monkeypatch, caplog, harness):
    """`~/.codex` (or `~/.grok/hooks`) as a regular file used to raise out of
    `install_hooks()` and the app re-raised, so the menu bar never came up.
    Now the Claude settings write still happens, the writer's failure is a
    warning, and the install reports done."""
    blocker = tmp_path / harness
    blocker.write_text("not a directory\n")
    if harness == "codex":
        monkeypatch.setattr(hooks, "CODEX_HOOKS_PATH", blocker / "hooks.json")
    else:
        monkeypatch.setattr(hooks, "GROK_HOOKS_DIR", blocker)
        monkeypatch.setattr(hooks, "GROK_HOOKS_PATH", blocker / "dark-army.json")
        monkeypatch.setattr(hooks, "GROK_RULES_DIR", blocker)
        monkeypatch.setattr(hooks, "GROK_RULES_PATH", blocker / "dark-army.md")
    with caplog.at_level("WARNING", logger="dark-army.hooks"):
        assert install_hooks() is True
    assert hooks.SHUNT_COMMAND in _commands_for(_read(settings_path), "PreToolUse")
    assert hooks.are_claude_hooks_installed() is True
    assert blocker.read_text() == "not a directory\n"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    label = "Codex hooks" if harness == "codex" else "Grok hooks"
    assert any(label in w and "continuing" in w for w in warnings), warnings
    # The other harness's file was still written.
    other = hooks.GROK_HOOKS_PATH if harness == "codex" else hooks.CODEX_HOOKS_PATH
    assert other.is_file()
    assert hooks.are_hooks_installed() is False  # honest: one harness is missing


# --- The settings table itself, pinned against its own past ------------------
# `tests/data/hooks-config.golden.json` is `HOOKS_CONFIG` and
# `grok_hooks_config()` as they stood before the table was rebuilt from rows,
# with the two command strings standing in as placeholders. Any drift in an
# event, a matcher, a group's order or a key (the PermissionRequest timeout
# included) would make every installed group read as outdated and be
# rewritten on the next launch, so the rebuilt table has to equal it exactly.

GOLDEN_HOOKS_CONFIG = Path(__file__).parent / "data" / "hooks-config.golden.json"


def _with_placeholders(value):
    if isinstance(value, dict):
        return {key: _with_placeholders(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_placeholders(item) for item in value]
    return {hooks.HOOK_COMMAND: "<HOOK_COMMAND>",
            hooks.SHUNT_COMMAND: "<SHUNT_COMMAND>"}.get(value, value) \
        if isinstance(value, str) else value


@pytest.mark.parametrize("name, table", [
    ("HOOKS_CONFIG", lambda: hooks.HOOKS_CONFIG),
    ("grok_hooks_config", hooks.grok_hooks_config),
])
def test_the_settings_table_equals_its_golden_copy(name, table):
    golden = json.loads(GOLDEN_HOOKS_CONFIG.read_text(encoding="utf-8"))
    # Lists compare in order, so a group moved within its event fails too.
    assert _with_placeholders(table()) == golden[name]
