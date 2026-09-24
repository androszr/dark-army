"""Fixtures every host test runs under, most of them autouse."""

import os
import sys
import tempfile

# Bytecode for anything a test imports goes to a temp folder, never beside the
# source. Tests load the agent pack's shunt wrappers straight from the vendored
# template (`test_work_record._wrapper_module`), and a `__pycache__` left there
# carries this checkout's absolute path — which `test_agent_pack_contract`
# then finds in the vendored tree. Set before the imports below.
if not sys.pycache_prefix:
    sys.pycache_prefix = os.path.join(tempfile.gettempdir(),
                                      "dark-army-tests-pycache")

# A suite started inside a terminal Dark Army hosts inherits the live hook
# door's address, and every client reads it as its one address — at import
# (`socket_server.HOOK_SOCK_PATH`, `channel_server.DAEMON_SOCKET`) and in
# every script a test runs. Dropped before any `dark_army_*` module is
# imported, so no test payload can reach the running daemon; the autouse
# fixture below keeps a test that sets it from leaking into the next.
os.environ.pop("DARK_ARMY_HOOK_SOCKET", None)

import pytest  # noqa: E402 — after the bytecode redirect above, on purpose

import dark_army_daemon.session_store as session_store
import dark_army_daemon.subprocess_env as subprocess_env  # noqa: E402

# The suite finds `node`, `swiftc` and friends from any shell, a terminal
# Dark Army opened with the bare launchd PATH included.
subprocess_env.adopt_login_path()
import dark_army_daemon.grok_roster as grok_roster
import dark_army_daemon.grok_billing as grok_billing
import dark_army_daemon.claude_usage as claude_usage
import dark_army_daemon.identity as identity
import dark_army_daemon.codex_rollouts as codex_rollouts
import dark_army_daemon.usage_hold as usage_hold
import dark_army_daemon.enrollment as enrollment
import dark_army_daemon.paths as paths
import dark_army_daemon.subprocess_env as subprocess_env

#: The real gate, captured before any fixture replaces it. `enforce_enrolment`
#: puts these back for a test that is *about* the gate.
_REAL_RESOLVE = enrollment.resolve
_REAL_ROOT_ENROLLED = enrollment.root_enrolled


@pytest.fixture(autouse=True)
def _no_live_hook_socket(monkeypatch):
    """No test inherits an address for the live hook door."""
    monkeypatch.delenv("DARK_ARMY_HOOK_SOCKET", raising=False)


@pytest.fixture(autouse=True)
def _no_machine_login_path(monkeypatch, tmp_path_factory):
    """`clean_env` appends this Mac's `/etc/paths` entries to PATH. Tests see
    an empty login path unless they set one, so a comparison of a child's
    environment is the same on every machine and in CI."""
    nowhere = tmp_path_factory.getbasetemp() / "no-login-path"
    monkeypatch.setattr(subprocess_env, "PATHS_FILE", str(nowhere / "paths"))
    monkeypatch.setattr(subprocess_env, "PATHS_DIR", str(nowhere / "paths.d"))


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    """Each test persists sessions into its own temp folder, never the
    machine's state folder, and the live rosters below read empty paths."""
    own_file = tmp_path.joinpath("sessions.json")
    monkeypatch.setattr(session_store, "SESSIONS_PATH", own_file)
    # Grok's live roster lives in the user's home; a test that calls
    # _reconciled_categories must not pick up the machine's real sessions.
    monkeypatch.setattr(grok_roster, "ACTIVE_SESSIONS_PATH", tmp_path / "grok-active.json")
    monkeypatch.setattr(grok_roster, "SESSIONS_DIR", tmp_path / "grok-sessions")
    # Codex's journals are also a live roster. Never let a developer's active
    # threads leak into snapshot assertions.
    monkeypatch.setattr(codex_rollouts, "SESSIONS_DIR", tmp_path / "codex-sessions")
    codex_rollouts._CACHE.clear()
    # The shared process reading is module memory: a test's stubbed
    # `_process_snapshot` must never serve another test from the memo.
    monkeypatch.setattr(codex_rollouts, "_PROCESS_SNAPSHOT", None)
    monkeypatch.setattr(codex_rollouts, "_LAST_ROOT_IDS", frozenset())
    monkeypatch.setattr(codex_rollouts, "_HELD_MEMO", None)
    monkeypatch.setattr(codex_rollouts, "_CLASSIFIED", None)
    # Billing hits grok.com with the user's session token. Tests never should.
    monkeypatch.setattr(grok_billing, "AUTH_PATH", tmp_path / "no-grok-auth.json")
    grok_billing.reset_cache()
    # Last-known usage chips. A test that fetches a Grok figure would otherwise
    # write the developer's real ~/.dark-army/usage-last.json.
    monkeypatch.setattr(usage_hold, "USAGE_HOLD_PATH", tmp_path / "usage-last.json")
    # Nicknames. `BobDaemon()` builds its own IdentityStore, and dozens of tests
    # build a daemon — so a plain `pytest` was rewriting the live
    # ~/.dark-army/identities.json with its fixtures. Watched on a running
    # fleet: the real map went to {} for six seconds, then {"s1": "Cipher"},
    # then {"a": "Hex", "b": "Vex"}. Nothing renames on screen (the
    # daemon holds its own copy in memory) — the damage lands at the next
    # restart, which adopts whatever the last test left behind.
    monkeypatch.setattr(identity, "IDENTITY_PATH", tmp_path / "identities.json")


@pytest.fixture(autouse=True)
def _isolate_login_items(tmp_path, monkeypatch):
    """Point the LaunchAgent plist `launchd` knows at a temp folder.

    The path is the real `~/Library/LaunchAgents`: a test that redirected
    only one of the plists `launchd` then knew deleted the machine's real
    login item on 22 Sep 2026 while `launchctl` itself was stubbed. The
    default is what covers the test that forgets."""
    from dark_army_menubar import launchd
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setattr(launchd, "PLIST_PATH",
                        agents / f"{launchd.PLIST_LABEL}.plist")


class _RealMachineRefused(AssertionError):
    """A test reached something that acts on the real machine without
    stubbing it. An AssertionError on purpose: no `except OSError` in the
    code under test can swallow it."""


def _refuse(what):
    def refuse(*_args, **_kwargs):
        raise _RealMachineRefused(f"{what} from a test: stub it explicitly")
    return refuse


@pytest.fixture(autouse=True)
def _refuse_real_machine_writers(tmp_path, monkeypatch):
    """Every writer that reaches past the state folder, refused or redirected
    by default. A test that is about one of them overrides it explicitly.

    - `launchctl` (launchd's `subprocess.run`) fails;
    - the log file lives under the test's own folder;
    - the VS Code CLI (`vscode_extension.run_code`) refuses.
    """
    import subprocess as _subprocess
    import types as _types
    from dark_army_menubar import launchd, logsetup, vscode_extension
    monkeypatch.setattr(launchd, "subprocess", _types.SimpleNamespace(
        run=_refuse("launchctl"),
        SubprocessError=_subprocess.SubprocessError,
        TimeoutExpired=_subprocess.TimeoutExpired))
    log = tmp_path / "Logs" / logsetup.LOG_DIR_NAME / logsetup.LOG_NAME
    monkeypatch.setattr(logsetup, "log_file_path", lambda: log)
    monkeypatch.setattr(vscode_extension, "run_code", _refuse("the VS Code CLI"))


@pytest.fixture(autouse=True)
def _open_the_enrolment_door(tmp_path, monkeypatch):
    """Admit every message, so a test can be about what happens past the door.

    `BobDaemon._handle_message` turns away anything whose project is not
    enrolled, and the three hookless rosters are filtered by the same ledger.
    Almost every test here builds its messages and its records by hand and is
    about the state machine behind the gate, not about the gate — so minting a
    key per fixture would be noise in a hundred places and would say nothing.

    The gate itself is tested against the **real** module: ask for the
    `enforce_enrolment` fixture to put it back, and `test_enrollment.py` covers
    `enrollment.py` end to end with no patching at all. The real
    `ENROLLMENT_PATH` is redirected either way, so no test can read or write the
    machine's own ledger.
    """
    monkeypatch.setattr(paths, "ENROLLMENT_PATH", tmp_path / "enrollment.json")
    enrollment.invalidate()
    monkeypatch.setattr(enrollment, "resolve", lambda key: "/enrolled-in-tests")
    monkeypatch.setattr(enrollment, "root_enrolled",
                        lambda cwd: str(cwd or ""))
    yield
    enrollment.invalidate()


@pytest.fixture
def enforce_enrolment(monkeypatch):
    """Undo `_open_the_enrolment_door` for one test, restoring the real gate.

    `monkeypatch.undo()` is not usable here — it would also undo the session,
    roster and identity isolation above — so the two functions are put back by
    name from the originals captured at import, before any fixture ran.
    """
    monkeypatch.setattr(enrollment, "resolve", _REAL_RESOLVE)
    monkeypatch.setattr(enrollment, "root_enrolled", _REAL_ROOT_ENROLLED)
    enrollment.invalidate()
    return enrollment


@pytest.fixture(autouse=True)
def _no_live_claude_usage(monkeypatch):
    """Nothing in this suite reads the login keychain or reaches Anthropic.

    `ApiServer._usage_report_for` now freshens the scoped Claude bar through
    `claude_usage.get_snapshot()`, which shells out to `/usr/bin/security` and
    then opens an HTTPS connection. Left alone, every `/api/usage` test would do
    both — a consent prompt on somebody's machine, a network round trip in CI,
    and a figure that changes between runs.

    `{}` is the module's own "nothing to add" answer, so the default here is the
    behaviour the feature had before it existed: the local snapshot, untouched.
    A test that is *about* the merge patches `get_snapshot` itself, and
    `test_claude_usage.py` drives the real functions against injected seams.
    """
    monkeypatch.setattr(claude_usage, "get_snapshot", lambda **kw: {})
    claude_usage.reset_cache()
    yield
    claude_usage.reset_cache()


def pytest_configure(config):
    """Keep the test run out of the Dock.

    Homebrew's `python3.12` is a stub that re-execs the framework's
    `Python.app`, whose Info.plist declares no `LSUIElement`. The first test
    that draws through AppKit (`test_menubar.py`'s strip renderings) therefore
    promotes the whole pytest process to a *foreground* application and macOS
    hands it a Dock tile — a rocket per concurrent run, outliving nothing but
    the run itself. Asking for the prohibited activation policy before any
    test has touched AppKit makes the process `BackgroundOnly`; offscreen
    drawing into an `NSBitmapImageRep` is unaffected.

    `sharedApplication()` itself checks the process in as a foreground app,
    so the policy alone still flashed a tile per process — nine at once under
    `-n auto`. Marking the in-memory Info.plist `LSBackgroundOnly` first
    makes the check-in background from the start.
    """
    try:
        from AppKit import NSApplication
        from Foundation import NSBundle
    except Exception:  # pragma: no cover - no PyObjC, no Dock tile either
        return
    try:
        NSBundle.mainBundle().infoDictionary()["LSBackgroundOnly"] = "1"
        NSApplication.sharedApplication().setActivationPolicy_(2)
    except Exception:  # pragma: no cover - never fail a run over an icon
        pass
