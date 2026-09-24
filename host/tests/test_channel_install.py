"""Registering the channel with Claude Code.

The interesting part is *not* writing a file — it is that the registration goes
through `claude mcp add` rather than through `~/.claude.json` directly, and that
a half-install is reported as not installed. For the dual-name window it is
registered twice, `dark-army` then `bob`, each with its own `--name=`.
"""
import json
import subprocess

import pytest

from dark_army_menubar import channel_install as ci


@pytest.fixture
def installer(tmp_path, monkeypatch):
    """A channel installer pointed at a scratch directory, with the CLI faked."""
    script = tmp_path / "dark-army-channel"
    monkeypatch.setattr(ci, "CHANNEL_SCRIPT", script)
    monkeypatch.setattr(ci, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ci, "_interpreter", lambda: "/usr/bin/python3")
    calls = []

    def fake_claude(*args):
        calls.append(list(args))
        if args[:2] == ("mcp", "list"):
            return True, (
                f"dark-army: /usr/bin/python3 {script} --name=dark-army - ✔ Connected\n"
                f"bob: /usr/bin/python3 {script} --name=bob - ✔ Connected")
        return True, ""

    monkeypatch.setattr(ci, "_claude", fake_claude)
    # Codex is optional. Individual tests replace this with the registry state
    # they exercise; the default models a machine with no Codex CLI.
    monkeypatch.setattr(
        ci, "_codex",
        lambda *args: (False, "The codex CLI could not be found."),
    )
    return script, calls


def _owned_entry(script):
    return json.dumps({
        "name": ci.CODEX_SERVER_NAME,
        "transport": {
            "type": "stdio",
            "command": "/usr/bin/python3",
            "args": [str(script), "--host=codex", "--name=dark-army"],
        },
    })


def _legacy_owned_entry(script):
    """What the build before the rename registered as `bob-companion-board`."""
    return json.dumps({
        "name": ci.LEGACY_CODEX_SERVER_NAME,
        "transport": {
            "type": "stdio",
            "command": "/usr/bin/python3",
            "args": [str(script), "--host=codex"],
        },
    })


def _foreign_entry():
    return json.dumps({
        "transport": {"type": "stdio", "command": "/other/server", "args": []},
    })


def _absent(name):
    return False, f"No MCP server named '{name}' found."


def _codex_registry(calls, entries):
    """A fake `codex mcp` over `entries` (name → `mcp get` JSON)."""
    def fake_codex(*args):
        calls.append(list(args))
        if args[:2] == ("mcp", "get"):
            name = args[2]
            if name in entries:
                return True, entries[name]
            return _absent(name)
        return True, ""
    return fake_codex


def test_install_writes_the_server_and_registers_it_at_user_scope(installer):
    """User scope, not project: the development-channels flag resolves against
    the configured servers, and a config merely named with `--mcp-config` does
    not register at all — verified live, where it answered "no MCP server
    configured with that name" and never spawned the server."""
    script, calls = installer
    assert ci.install() is True
    assert script.exists()
    assert "channel" in script.read_text()          # the real module, copied
    # And the copy is *current*. The installed script lives outside the bundle,
    # so `main()` rewrites it on every launch; if a packaging change ever stops
    # doing that, an upgraded Dark Army would keep handing sessions a channel with no
    # card tool — a silent absence rather than an error. This fails a test
    # instead of a session. All four verbs, for the same reason: a session
    # already open when Dark Army is upgraded keeps the tool list it answered with at
    # startup, so the copy on disk is the only thing that can be checked here.
    assert "dark_army_add_card" in script.read_text()
    assert "dark_army_close_card" in script.read_text()
    assert "dark_army_attach_plan" in script.read_text()
    assert "dark_army_attach_report" in script.read_text()
    assert "dark_army_needs_manual_check" in script.read_text()
    assert "dark_army_answer_card" in script.read_text()
    assert "dark_army_knowledge_read" in script.read_text()
    assert "dark_army_knowledge_write" in script.read_text()
    # And the legacy spelling, for a session born under `bob`.
    assert '"bob_"' in script.read_text()
    adds = [c for c in calls if c[:2] == ["mcp", "add"]]
    assert [a[:5] for a in adds] == [
        ["mcp", "add", "-s", "user", "dark-army"],
        ["mcp", "add", "-s", "user", "bob"],
    ]
    assert adds[0][5:] == ["--", "/usr/bin/python3", str(script),
                           "--name=dark-army"]
    assert adds[1][5:] == ["--", "/usr/bin/python3", str(script), "--name=bob"]


def test_install_registers_the_current_name_first(installer):
    """Between the script write and the first add, an old `bob` registration
    spawns the new script passive; current first keeps that gap short."""
    _, calls = installer
    ci.install()
    names = [c[4] for c in calls if c[:2] == ["mcp", "add"]]
    assert names == list(ci.SERVER_NAMES) == ["dark-army", "bob"]


def test_every_name_is_removed_before_any_is_added(installer):
    """Adding `dark-army` while the previous build's flagless `bob` is still
    registered would make both active in a plain session: two tool lists and
    a displaced attach. So both removes come first, then the adds."""
    _, calls = installer
    ci.install()
    claude = [(c[1], c[4]) for c in calls if c[0] == "mcp" and len(c) > 4]
    assert claude == [("remove", "dark-army"), ("remove", "bob"),
                      ("add", "dark-army"), ("add", "bob")]
    first_add = next(i for i, c in enumerate(calls) if c[:2] == ["mcp", "add"])
    removes = [i for i, c in enumerate(calls) if c[:2] == ["mcp", "remove"]]
    assert len(removes) == 2 and all(i < first_add for i in removes)


def test_a_failed_legacy_add_is_a_failed_install(installer, monkeypatch):
    """Either registration failing is a failed install: the toggle alerts and
    the launch path logs, rather than claiming a window that is half open."""
    _, calls = installer

    def half(*args):
        calls.append(list(args))
        if args[:2] == ("mcp", "add") and args[4] == "bob":
            return False, "refused"
        return True, ""

    monkeypatch.setattr(ci, "_claude", half)
    assert ci.install() is False
    assert not ci._marker_path().exists()


def test_install_removes_a_previous_registration_first(installer):
    """`claude mcp add` refuses a name that is already there, and reinstalling
    is the ordinary case — the interpreter path moves under an upgrade."""
    _, calls = installer
    ci.install()
    order = [c[1] for c in calls if c[0] == "mcp"]
    assert order.index("remove") < order.index("add")


def test_a_failed_registration_is_a_failed_install(installer, monkeypatch):
    """Otherwise the toggle goes on and every session starts without a channel,
    with nothing anywhere saying why."""
    monkeypatch.setattr(ci, "_claude", lambda *a: (False, "no such command"))
    assert ci.install() is False


def test_an_unreadable_source_does_not_register_anything(installer, monkeypatch):
    _, calls = installer
    monkeypatch.setattr(ci, "source_path", lambda: ci.Path("/nope/gone.py"))
    assert ci.install() is False
    assert calls == []


def test_a_registration_without_its_script_is_not_installed(installer):
    """Half an install fails at every session start. Both halves or neither."""
    script, _ = installer
    ci.install()
    assert ci.installed() is True
    script.unlink()
    assert ci.installed() is False


def test_uninstall_unregisters_and_removes(installer):
    script, calls = installer
    ci.install()
    calls.clear()
    ci.uninstall()
    assert not script.exists()
    assert calls == [["mcp", "remove", "-s", "user", "dark-army"],
                     ["mcp", "remove", "-s", "user", "bob"]]


def test_one_name_listed_is_not_installed(installer, monkeypatch):
    """Half the dual-name window is half an install."""
    script, _ = installer
    ci.install()
    for only in ("dark-army", "bob"):
        monkeypatch.setattr(ci, "_claude", lambda *a, only=only: (
            True, f"{only}: /usr/bin/python3 {script} --name={only} - ✔ Connected"))
        assert ci.installed() is False


def test_the_script_path_alone_does_not_count_as_a_listed_name(installer,
                                                               monkeypatch):
    """`dark-army` is in the copy's own path, so a substring test would find it
    on any line that runs the script. The name has to lead its own line."""
    script, _ = installer
    ci.install()
    monkeypatch.setattr(ci, "_claude", lambda *a: (
        True, f"bob: /usr/bin/python3 {ci.STATE_DIR}/.dark-army/dark-army-channel"
              " - ✔ Connected"))
    assert ci.installed() is False


def test_the_launch_command_names_the_server_the_registration_used():
    """The two have to agree, and they are written in different files."""
    assert f"server:{ci.SERVER_NAME}" in ci.launch_command()
    assert "--dangerously-load-development-channels" in ci.launch_command()
    assert ci.launch_command() == (
        "claude --dangerously-load-development-channels server:dark-army")


def test_the_names_come_from_the_channel_server():
    """One spelling of each name, in the file the copy is made from."""
    from dark_army_daemon import channel_server as cs
    assert ci.SERVER_NAME == cs.CURRENT_NAME
    assert ci.LEGACY_SERVER_NAME == cs.LEGACY_NAME
    assert ci.SERVER_NAMES == ("dark-army", "bob")
    assert ci.CODEX_SERVER_NAME == "dark-army-board"
    assert ci.LEGACY_CODEX_SERVER_NAME == "bob-companion-board"


def test_codex_missing_does_not_break_the_claude_install(installer):
    assert ci.install() is True


def test_codex_absence_adds_the_dedicated_entry(installer, monkeypatch):
    script, _ = installer
    calls = []
    monkeypatch.setattr(ci, "_codex", _codex_registry(calls, {}))
    assert ci.install() is True
    assert calls[-1] == ["mcp", "add", "dark-army-board", "--",
                         "/usr/bin/python3", str(script), "--host=codex",
                         "--name=dark-army"]
    # The legacy name was looked at, found absent and left alone.
    assert ["mcp", "remove", "bob-companion-board"] not in calls


def test_an_owned_codex_entry_is_refreshed_idempotently(installer, monkeypatch):
    script, _ = installer
    calls = []
    monkeypatch.setattr(ci, "_codex", _codex_registry(
        calls, {ci.CODEX_SERVER_NAME: _owned_entry(script)}))
    assert ci.install() is True
    assert [(c[1], c[2]) for c in calls] == [
        ("get", "bob-companion-board"), ("get", "dark-army-board"),
        ("remove", "dark-army-board"), ("add", "dark-army-board")]


def test_an_owned_legacy_codex_entry_is_removed_and_the_new_one_added(
        installer, monkeypatch):
    """The previous build's exact entry is Dark Army's own: an upgrade retires
    it, so Codex does not spawn both names and show both tool lists."""
    script, _ = installer
    calls = []
    monkeypatch.setattr(ci, "_codex", _codex_registry(
        calls, {ci.LEGACY_CODEX_SERVER_NAME: _legacy_owned_entry(script)}))
    assert ci.install() is True
    assert ["mcp", "remove", "bob-companion-board"] in calls
    assert calls[-1][:3] == ["mcp", "add", "dark-army-board"]
    assert calls.index(["mcp", "remove", "bob-companion-board"]) \
        < len(calls) - 1


def test_a_foreign_legacy_codex_entry_is_left_and_the_new_one_still_added(
        installer, monkeypatch):
    """Ownership is by exact command and arguments, never by name: somebody
    else's `bob-companion-board` is left where it is."""
    script, _ = installer
    calls = []
    monkeypatch.setattr(ci, "_codex", _codex_registry(
        calls, {ci.LEGACY_CODEX_SERVER_NAME: _foreign_entry()}))
    assert ci.install() is True
    assert ["mcp", "remove", "bob-companion-board"] not in calls
    assert calls[-1][:3] == ["mcp", "add", "dark-army-board"]


def test_a_legacy_entry_with_the_new_arguments_is_not_owned(installer,
                                                            monkeypatch):
    """Only the previous build's own shape counts under the old name."""
    script, _ = installer
    calls = []
    monkeypatch.setattr(ci, "_codex", _codex_registry(
        calls, {ci.LEGACY_CODEX_SERVER_NAME: _owned_entry(script)}))
    assert ci.install() is True
    assert ["mcp", "remove", "bob-companion-board"] not in calls


def test_an_unreadable_legacy_codex_entry_is_skipped(installer, monkeypatch):
    script, _ = installer
    calls = []
    monkeypatch.setattr(ci, "_codex", _codex_registry(
        calls, {ci.LEGACY_CODEX_SERVER_NAME: "not-json"}))
    assert ci.install() is True
    assert ["mcp", "remove", "bob-companion-board"] not in calls
    assert calls[-1][:3] == ["mcp", "add", "dark-army-board"]


def test_a_failed_codex_step_never_fails_the_claude_install(installer,
                                                            monkeypatch):
    calls = []

    def broken(*args):
        calls.append(list(args))
        if args[:2] == ("mcp", "get"):
            return _absent(args[2])
        return False, "codex exploded"

    monkeypatch.setattr(ci, "_codex", broken)
    assert ci.install() is True


def test_a_foreign_codex_name_is_never_replaced(installer, monkeypatch):
    calls = []

    def fake_codex(*args):
        calls.append(list(args))
        return True, _foreign_entry()

    monkeypatch.setattr(ci, "_codex", fake_codex)
    assert ci.install() is True  # Claude remains installed.
    assert [call[1] for call in calls] == ["get", "get"]


def test_malformed_codex_json_fails_closed(installer, monkeypatch):
    calls = []

    def fake_codex(*args):
        calls.append(list(args))
        return True, "not-json"

    monkeypatch.setattr(ci, "_codex", fake_codex)
    assert ci.install() is True
    assert [call[1] for call in calls] == ["get", "get"]


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("codex", 20),
    None,
])
def test_codex_wrapper_bounds_and_reports_cli_failures(monkeypatch, failure):
    monkeypatch.setattr(ci, "resolve_executable", lambda name: "/opt/bin/codex")
    if failure is not None:
        monkeypatch.setattr(ci.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
            failure))
    else:
        result = type("Result", (), {
            "returncode": 2, "stdout": "", "stderr": "bad command",
        })()
        monkeypatch.setattr(ci.subprocess, "run", lambda *a, **k: result)
    ok, detail = ci._codex("mcp", "get", ci.CODEX_SERVER_NAME, "--json")
    assert ok is False
    assert detail


def test_codex_wrapper_uses_the_installed_app_safe_resolver(monkeypatch):
    seen = []
    monkeypatch.setattr(
        ci, "resolve_executable",
        lambda name: seen.append(name) or "/opt/homebrew/bin/codex",
    )
    result = type("Result", (), {
        "returncode": 0, "stdout": "{}", "stderr": "",
    })()
    monkeypatch.setattr(ci.subprocess, "run", lambda *a, **k: result)

    assert ci._codex("mcp", "get", ci.CODEX_SERVER_NAME, "--json") == (True, "{}")
    assert seen == ["codex"]


def test_uninstall_removes_only_an_owned_codex_entry(installer, monkeypatch):
    script, _ = installer
    script.write_text("server")
    calls = []

    monkeypatch.setattr(ci, "_codex", _codex_registry(
        calls, {ci.CODEX_SERVER_NAME: _owned_entry(script)}))
    ci.uninstall()
    assert [(c[1], c[2]) for c in calls] == [
        ("get", "bob-companion-board"), ("get", "dark-army-board"),
        ("remove", "dark-army-board")]


def test_uninstall_removes_both_owned_codex_names(installer, monkeypatch):
    script, _ = installer
    script.write_text("server")
    calls = []
    monkeypatch.setattr(ci, "_codex", _codex_registry(calls, {
        ci.CODEX_SERVER_NAME: _owned_entry(script),
        ci.LEGACY_CODEX_SERVER_NAME: _legacy_owned_entry(script),
    }))
    ci.uninstall()
    removed = [c[2] for c in calls if c[1] == "remove"]
    assert removed == ["bob-companion-board", "dark-army-board"]


def test_uninstall_leaves_a_foreign_codex_entry_untouched(installer, monkeypatch):
    calls = []

    def fake_codex(*args):
        calls.append(list(args))
        return True, json.dumps({
            "transport": {"type": "stdio", "command": "foreign", "args": []},
        })

    monkeypatch.setattr(ci, "_codex", fake_codex)
    ci.uninstall()
    assert [call[1] for call in calls] == ["get", "get"]


# ── the launch-time skip ─────────────────────────────────────────────────────
#
# install() runs at every launch (construction used to run it on the main
# thread, before the status item existed): four `claude mcp` and up to five
# `codex mcp` subprocesses, each with a 20s timeout. When the last recorded
# install is still current, none of that is needed.


def test_a_second_install_skips_the_cli(installer):
    _, calls = installer
    assert ci.install() is True
    calls.clear()
    assert ci.install() is True
    assert calls == []


def test_force_reruns_the_registration(installer):
    """The toggle's route, and the repair for a registration removed behind
    the marker's back."""
    _, calls = installer
    ci.install()
    calls.clear()
    assert ci.install(force=True) is True
    assert [c[:2] for c in calls if c[0] == "mcp"] == [
        ["mcp", "remove"], ["mcp", "remove"], ["mcp", "add"], ["mcp", "add"]]


def test_the_marker_records_both_commands(installer):
    script, _ = installer
    ci.install()
    recorded = json.loads(ci._marker_path().read_text(encoding="utf-8"))
    assert recorded["claude_commands"] == {
        "dark-army": ["/usr/bin/python3", str(script), "--name=dark-army"],
        "bob": ["/usr/bin/python3", str(script), "--name=bob"],
    }
    assert "claude_command" not in recorded


def test_a_marker_of_the_previous_shape_is_not_already_installed(installer):
    """The build before recorded one `claude_command`. It must not match, so
    the first launch of this build registers the second name."""
    script, calls = installer
    text = ci.source_path().read_text(encoding="utf-8")
    script.write_text(text, encoding="utf-8")
    import hashlib
    ci._marker_path().write_text(json.dumps({
        "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "claude_command": ["/usr/bin/python3", str(script)],
    }), encoding="utf-8")
    assert ci._already_installed(ci._install_signature(text)) is False
    assert ci.install() is True
    assert [c[4] for c in calls if c[:2] == ["mcp", "add"]] == [
        "dark-army", "bob"]


def test_a_hand_edited_copy_defeats_the_skip(installer):
    """The marker alone is not trusted — the copy on disk has to carry the
    recorded content, or the install runs for real."""
    script, calls = installer
    ci.install()
    script.write_text("# stale copy\n", encoding="utf-8")
    calls.clear()
    assert ci.install() is True
    assert any(c[:2] == ["mcp", "add"] for c in calls)
    assert script.read_text(encoding="utf-8") != "# stale copy\n"


def test_a_deleted_copy_defeats_the_skip(installer):
    script, calls = installer
    ci.install()
    script.unlink()
    calls.clear()
    assert ci.install() is True
    assert script.exists()
    assert any(c[:2] == ["mcp", "add"] for c in calls)


def test_a_failed_registration_records_nothing(installer):
    """Half an install must retry next launch, not be remembered as done."""
    _, calls = installer
    working = ci._claude
    ci._claude = lambda *a: (False, "no such command")
    try:
        assert ci.install() is False
    finally:
        ci._claude = working
    calls.clear()
    assert ci.install() is True
    assert any(c[:2] == ["mcp", "add"] for c in calls)


def test_uninstall_forgets_the_recorded_install(installer):
    """Otherwise toggle off → on skips the registration it just removed."""
    _, calls = installer
    ci.install()
    ci.uninstall()
    calls.clear()
    assert ci.install() is True
    assert any(c[:2] == ["mcp", "add"] for c in calls)
