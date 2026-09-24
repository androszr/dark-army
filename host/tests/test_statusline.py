# host/tests/test_statusline.py
"""Statusline collector: payload flattening, the settings installer, and the
standalone script executed for real.

The sample payload is a verbatim capture from Claude Code 2.1.212 (fields
confirmed live, not copied from docs)."""

import json
import socket
import subprocess
import sys
import threading

import pytest

from dark_army_daemon.protocol import flatten_statusline
from dark_army_menubar import hooks, statusline

REAL_PAYLOAD = {
    "session_id": "fceab7fa-13c4-4832-be31-2dfb456472ec",
    "transcript_path": "/Users/x/.claude/projects/-p/fceab7fa.jsonl",
    "cwd": "/Users/x/Documents/shop-front",
    "effort": {"level": "high"},
    "session_name": "Plan ulepszeń mission-control",
    "model": {"id": "claude-opus-5", "display_name": "claude-opus-5"},
    "workspace": {
        "current_dir": "/Users/x/Documents/shop-front",
        "project_dir": "/Users/x/Documents/shop-front",
        "added_dirs": [],
        "repo": {"host": "gitlab.com", "owner": "someone", "name": "dark-army"},
    },
    "version": "2.1.212",
    "cost": {
        "total_cost_usd": 15.7779551,
        "total_duration_ms": 2693805,
        "total_api_duration_ms": 1499131,
        "total_lines_added": 440,
        "total_lines_removed": 131,
    },
    "context_window": {
        "total_input_tokens": 149666,
        "total_output_tokens": 653,
        "context_window_size": 200000,
        "used_percentage": 75,
        "remaining_percentage": 25,
    },
    "exceeds_200k_tokens": False,
    "fast_mode": False,
    "thinking": {"enabled": True},
    "rate_limits": {
        "five_hour": {"used_percentage": 41, "resets_at": 1785065400},
        "seven_day": {"used_percentage": 12, "resets_at": 1785628800},
    },
}


# --- flatten_statusline ------------------------------------------------------


def test_flatten_reads_the_fields_nothing_else_exposes():
    f = flatten_statusline(REAL_PAYLOAD)
    assert f["cost_usd"] == pytest.approx(15.7779551)   # no cost field in transcripts
    assert f["ctx_used_pct"] == 75
    assert f["five_hour_pct"] == 41
    assert f["five_hour_resets_at"] == 1785065400
    assert f["seven_day_pct"] == 12
    assert f["session_name"] == "Plan ulepszeń mission-control"
    assert f["repo"] == "someone/dark-army"
    assert f["effort"] == "high"
    assert f["thinking"] is True
    assert f["fast_mode"] is False
    assert f["cc_version"] == "2.1.212"


def test_absent_metrics_are_none_not_zero():
    """A cost of 0.00 rendered for a session that simply hasn't reported yet is a
    lie the UI would repeat."""
    f = flatten_statusline({"session_id": "s1"})
    assert f["cost_usd"] is None
    assert f["ctx_used_pct"] is None
    assert f["five_hour_pct"] is None
    assert f["session_id"] == "s1"


@pytest.mark.parametrize("payload", [None, [], "text", 42])
def test_flatten_tolerates_junk(payload):
    assert flatten_statusline(payload) == {}


def test_flatten_rejects_non_numeric_metrics():
    f = flatten_statusline({"session_id": "s", "cost": {"total_cost_usd": "1.50"}})
    assert f["cost_usd"] is None


def test_flatten_survives_wrong_nested_types():
    f = flatten_statusline({"session_id": "s", "cost": "nope", "workspace": 3})
    assert f["cost_usd"] is None and f["repo"] == ""


def test_booleans_are_not_mistaken_for_numbers():
    f = flatten_statusline({"session_id": "s", "cost": {"total_cost_usd": True}})
    assert f["cost_usd"] is None


# --- installer ---------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect both the Claude settings file and our state paths into tmp."""
    settings = tmp_path / "settings.json"
    script = tmp_path / "dark-army-statusline"
    chain = tmp_path / "statusline-chain"
    monkeypatch.setattr(hooks, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(statusline, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(statusline, "STATUSLINE_SCRIPT_PATH", script)
    monkeypatch.setattr(statusline, "STATUSLINE_CHAIN_PATH", chain)
    monkeypatch.setattr(statusline, "STATUSLINE_COMMAND", str(script))
    monkeypatch.setattr(statusline, "ensure_state_dir", lambda: tmp_path)
    return {"settings": settings, "script": script, "chain": chain}


def _settings(sandbox):
    return json.loads(sandbox["settings"].read_text())


def test_install_writes_script_and_points_settings_at_it(sandbox):
    statusline.install_statusline()
    assert sandbox["script"].exists()
    assert sandbox["script"].stat().st_mode & 0o111, "script must be executable"
    assert _settings(sandbox)["statusLine"] == {
        "type": "command", "command": str(sandbox["script"])
    }
    assert statusline.is_statusline_installed()


def test_install_preserves_unrelated_settings(sandbox):
    sandbox["settings"].write_text(json.dumps({"model": "claude-opus-5", "hooks": {"Stop": []}}))
    statusline.install_statusline()
    data = _settings(sandbox)
    assert data["model"] == "claude-opus-5"
    assert data["hooks"] == {"Stop": []}


def test_install_chains_the_users_existing_statusline(sandbox):
    """Only one statusLine can exist. Taking someone's away silently would be a
    hostile install."""
    sandbox["settings"].write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "~/.claude/mine.sh"}}
    ))
    statusline.install_statusline()
    assert sandbox["chain"].read_text().strip() == "~/.claude/mine.sh"


def test_reinstall_does_not_chain_us_to_ourselves(sandbox):
    sandbox["settings"].write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "~/.claude/mine.sh"}}
    ))
    statusline.install_statusline()
    statusline.install_statusline()
    statusline.install_statusline()
    assert sandbox["chain"].read_text().strip() == "~/.claude/mine.sh"
    assert statusline.is_statusline_installed()


def test_uninstall_restores_the_displaced_command(sandbox):
    sandbox["settings"].write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "~/.claude/mine.sh"}}
    ))
    statusline.install_statusline()
    assert statusline.uninstall_statusline() is True
    assert _settings(sandbox)["statusLine"]["command"] == "~/.claude/mine.sh"
    assert not sandbox["chain"].exists()


def test_uninstall_removes_the_key_when_there_was_nothing_before(sandbox):
    statusline.install_statusline()
    statusline.uninstall_statusline()
    assert "statusLine" not in _settings(sandbox)


def test_uninstall_leaves_a_foreign_statusline_alone(sandbox):
    sandbox["settings"].write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "somebody-elses"}}
    ))
    assert statusline.uninstall_statusline() is False
    assert _settings(sandbox)["statusLine"]["command"] == "somebody-elses"


def test_launch_refresh_overwrites_a_stale_copy(sandbox):
    sandbox["script"].write_text("# STATUSLINE_SCRIPT_VERSION: 2026-01-01-old\n")
    statusline.install_statusline_script()
    assert statusline.installed_script_version() == statusline.SCRIPT_VERSION
    assert sandbox["script"].stat().st_mode & 0o111, "script must be executable"


def test_script_refresh_never_touches_settings(sandbox):
    assert not sandbox["settings"].exists()
    statusline.install_statusline_script()
    assert not sandbox["settings"].exists()
    foreign = json.dumps(
        {"statusLine": {"type": "command", "command": "~/.claude/mine.sh"}}
    ).encode()
    sandbox["settings"].write_bytes(foreign)
    statusline.install_statusline_script()
    assert sandbox["settings"].read_bytes() == foreign


def test_a_substring_match_is_not_ours(sandbox):
    sandbox["settings"].write_text(json.dumps({"statusLine": {
        "type": "command", "command": "wrapper " + str(sandbox["script"]),
    }}))
    assert statusline.is_statusline_installed() is False


# --- the real script, executed -----------------------------------------------


class _FakeDaemon:
    """One-shot loopback server that records the message and replies."""

    def __init__(self, reply: dict):
        self.reply = reply
        self.received = None
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
            with conn:
                data = conn.makefile("r", encoding="utf-8").readline()
                self.received = json.loads(data)
                conn.sendall(json.dumps(self.reply).encode() + b"\n")
        except OSError:
            pass
        finally:
            self._sock.close()

    def join(self):
        self._thread.join(timeout=3)


def _run_script(tmp_path, payload, port, chain_dir=None, env_extra=None):
    script = tmp_path / "dark-army-statusline"
    script.write_text(statusline.STATUSLINE_SCRIPT)
    script.chmod(0o755)
    # HOME is always set, even with no chain to test: the script resolves
    # CHAIN_PATH off Path.home(), and with HOME unset that falls through to the
    # passwd database — so these tests read the developer's real
    # ~/.dark-army/statusline-chain and *run* whatever statusline Dark Army
    # displaced on that machine. The suite passed or failed depending on whose
    # laptop it ran on.
    env = {
        "PATH": "/usr/bin:/bin",
        "BOB_COMPANION_PORT": str(port),
        "HOME": str(chain_dir or tmp_path),
    }
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(script)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=10, env=env,
    )


def test_script_forwards_the_payload_verbatim(tmp_path):
    """Verbatim forwarding is what keeps the contract in one place: the daemon
    flattens, the script never parses."""
    daemon = _FakeDaemon({"others_waiting": 0})
    result = _run_script(tmp_path, REAL_PAYLOAD, daemon.port)
    daemon.join()

    assert result.returncode == 0, result.stderr
    assert daemon.received["event"] == "statusline"
    assert daemon.received["session_id"] == REAL_PAYLOAD["session_id"]
    assert daemon.received["data"] == REAL_PAYLOAD


def test_script_renders_model_context_and_cost(tmp_path):
    daemon = _FakeDaemon({"others_waiting": 0})
    result = _run_script(tmp_path, REAL_PAYLOAD, daemon.port)
    daemon.join()
    assert "opus 5" in result.stdout
    assert "ctx 75%" in result.stdout
    assert "$15.78" in result.stdout
    assert "waiting" not in result.stdout


def test_script_surfaces_other_blocked_agents(tmp_path):
    """The cross-agent signal: the session you are looking at tells you another
    one is stuck."""
    daemon = _FakeDaemon({"others_waiting": 2})
    result = _run_script(tmp_path, REAL_PAYLOAD, daemon.port)
    daemon.join()
    assert "2 agents waiting" in result.stdout


def test_script_singularises_one_waiting_agent(tmp_path):
    daemon = _FakeDaemon({"others_waiting": 1})
    result = _run_script(tmp_path, REAL_PAYLOAD, daemon.port)
    daemon.join()
    assert "1 agent waiting" in result.stdout


def test_script_still_prints_when_the_daemon_is_down(tmp_path):
    """A statusline that prints nothing because a background app is not running
    would be worse than no integration at all."""
    result = _run_script(tmp_path, REAL_PAYLOAD, 1)  # nothing listening on port 1
    assert result.returncode == 0, result.stderr
    assert "opus 5" in result.stdout


def test_script_survives_junk_on_stdin(tmp_path):
    script = tmp_path / "dark-army-statusline"
    script.write_text(statusline.STATUSLINE_SCRIPT)
    script.chmod(0o755)
    result = subprocess.run(
        [sys.executable, str(script)], input="not json at all",
        capture_output=True, text=True, timeout=10,
        env={"PATH": "/usr/bin:/bin", "BOB_COMPANION_PORT": "1"},
    )
    assert result.returncode == 0, result.stderr


def test_script_runs_the_chained_command_and_prints_its_output(tmp_path):
    home = tmp_path / "home"
    (home / ".dark-army").mkdir(parents=True)
    (home / ".dark-army" / "statusline-chain").write_text("echo THEIR LINE\n")

    daemon = _FakeDaemon({"others_waiting": 3})
    result = _run_script(tmp_path, REAL_PAYLOAD, daemon.port, chain_dir=home)
    daemon.join()

    assert result.stdout.strip() == "THEIR LINE"
    # Telemetry still flows even though the display belongs to the user.
    assert daemon.received["data"]["session_id"] == REAL_PAYLOAD["session_id"]


def test_chained_command_receives_the_same_stdin(tmp_path):
    home = tmp_path / "home"
    (home / ".dark-army").mkdir(parents=True)
    (home / ".dark-army" / "statusline-chain").write_text(
        "%s -c 'import json,sys; print(json.load(sys.stdin)[\"model\"][\"id\"])'\n"
        % sys.executable
    )
    result = _run_script(tmp_path, REAL_PAYLOAD, 1, chain_dir=home)
    assert result.stdout.strip() == "claude-opus-5"


def test_falls_back_to_our_line_when_the_chained_command_fails(tmp_path):
    home = tmp_path / "home"
    (home / ".dark-army").mkdir(parents=True)
    (home / ".dark-army" / "statusline-chain").write_text("exit 3\n")
    result = _run_script(tmp_path, REAL_PAYLOAD, 1, chain_dir=home)
    assert "opus 5" in result.stdout


# --- daemon side -------------------------------------------------------------

import time  # noqa: E402

from dark_army_daemon.agents_poll import AgentRecord  # noqa: E402
from dark_army_daemon.daemon import BobDaemon  # noqa: E402


def _msg(session_id="s1", **overrides):
    payload = dict(REAL_PAYLOAD, session_id=session_id)
    payload.update(overrides)
    return {"event": "statusline", "session_id": session_id, "data": payload}


@pytest.mark.asyncio
async def test_statusline_stores_metrics_without_touching_session_state():
    """It fires on every assistant message. Treating it as activity would make
    staleness eviction meaningless — a session would look alive as long as its
    terminal stayed open."""
    d = BobDaemon()
    await d._handle_message(_msg("s1"))
    assert d._session_states == {}, "statusline must not create or revive a session"
    assert d._session_metrics["s1"]["cost_usd"] == pytest.approx(15.7779551)
    assert d._session_metrics["s1"]["ctx_used_pct"] == 75
    assert "received_at" in d._session_metrics["s1"]


@pytest.mark.asyncio
async def test_statusline_reply_counts_other_agents_not_this_one():
    d = BobDaemon()
    d._session_states["s1"] = {"state": "waiting", "last_event": time.time()}
    d._session_states["s2"] = {"state": "waiting", "last_event": time.time()}
    d._session_states["s3"] = {"state": "working", "last_event": time.time()}

    reply = await d._handle_message(_msg("s1"))
    assert reply["others_waiting"] == 1, "the asking session must not count itself"
    assert reply["working"] == 1
    assert reply["total"] == 3


@pytest.mark.asyncio
async def test_statusline_reply_includes_blocked_background_agents():
    """A background agent is exactly what the human in a terminal cannot see."""
    d = BobDaemon()
    d._agent_records = {
        "bg": AgentRecord("bg", kind="background", activity="blocked"),
    }
    reply = await d._handle_message(_msg("s1"))
    assert reply["others_waiting"] == 1


@pytest.mark.asyncio
async def test_statusline_pushes_are_throttled():
    d = BobDaemon()
    pushes = []
    d._schedule_agents_push = lambda: pushes.append(1)

    for _ in range(5):
        await d._handle_message(_msg("s1"))
    assert len(pushes) == 1, "a burst must collapse to one push"

    d._last_metrics_push -= 10.0        # pretend the interval elapsed
    await d._handle_message(_msg("s1"))
    assert len(pushes) == 2


@pytest.mark.asyncio
async def test_metrics_are_pruned_when_the_session_disappears():
    d = BobDaemon()
    await d._handle_message(_msg("gone"))
    assert "gone" in d._session_metrics
    d._collect_agent_stubs()            # runs on every snapshot push
    assert "gone" not in d._session_metrics, "metrics outlived their session"


@pytest.mark.asyncio
async def test_metrics_reach_the_snapshot():
    d = BobDaemon()
    d._session_states["s1"] = {"state": "idle", "last_event": time.time()}
    await d._handle_message(_msg("s1"))
    entry = d.detailed_snapshot()["sleeping"][0]
    assert entry["metrics"]["cost_usd"] == pytest.approx(15.7779551)
    assert entry["metrics"]["five_hour_pct"] == 41


@pytest.mark.asyncio
async def test_sessions_without_metrics_report_an_empty_dict():
    d = BobDaemon()
    d._session_states["s1"] = {"state": "idle", "last_event": time.time()}
    assert d.detailed_snapshot()["sleeping"][0]["metrics"] == {}


# ── the project's enrolment key ───────────────────────────────────────────────

def _plant_key(root, value, folder=".dark-army"):
    (root / folder).mkdir(parents=True, exist_ok=True)
    (root / folder / "key").write_text(value)


def test_the_statusline_message_carries_the_project_key(tmp_path):
    root = tmp_path / "proj"
    _plant_key(root, "statusline-key", folder=".dark-army")
    payload = dict(REAL_PAYLOAD)
    payload["workspace"] = {"current_dir": str(root)}
    daemon = _FakeDaemon({"others_waiting": 0})
    result = _run_script(tmp_path, payload, daemon.port)
    daemon.join()
    assert result.returncode == 0, result.stderr
    assert daemon.received["key"] == "statusline-key"


def test_the_statusline_message_carries_an_old_name_key(tmp_path):
    """A project holding only `.bob-companion/key` keeps sending it for the
    read window."""
    root = tmp_path / "proj"
    _plant_key(root, "old-statusline-key", folder=".bob-companion")
    payload = dict(REAL_PAYLOAD)
    payload["workspace"] = {"current_dir": str(root)}
    daemon = _FakeDaemon({"others_waiting": 0})
    result = _run_script(tmp_path, payload, daemon.port)
    daemon.join()
    assert result.returncode == 0, result.stderr
    assert daemon.received["key"] == "old-statusline-key"


def test_the_statusline_prefers_the_new_name_key(tmp_path):
    root = tmp_path / "proj"
    _plant_key(root, "new-statusline-key", folder=".dark-army")
    _plant_key(root, "old-statusline-key", folder=".bob-companion")
    payload = dict(REAL_PAYLOAD)
    payload["workspace"] = {"current_dir": str(root)}
    daemon = _FakeDaemon({"others_waiting": 0})
    result = _run_script(tmp_path, payload, daemon.port)
    daemon.join()
    assert result.returncode == 0, result.stderr
    assert daemon.received["key"] == "new-statusline-key"


def test_the_statusline_key_walk_skips_the_home_state_directory(tmp_path):
    """Neither `~/.dark-army/key` nor `~/.bob-companion/key` may ever be read
    as a project's key, or a session anywhere under $HOME enrols the whole
    home directory. Planted under both names."""
    home = tmp_path / "home"
    _plant_key(home, "bobs-own-state", folder=".dark-army")
    _plant_key(home, "bobs-old-state", folder=".bob-companion")
    nested = home / "code" / "clone"
    nested.mkdir(parents=True)
    payload = dict(REAL_PAYLOAD)
    payload["workspace"] = {"current_dir": str(nested)}
    daemon = _FakeDaemon({"others_waiting": 0})
    result = _run_script(tmp_path, payload, daemon.port, chain_dir=home)
    daemon.join()
    assert result.returncode == 0, result.stderr
    assert daemon.received["key"] == ""


@pytest.mark.asyncio
async def test_an_unkeyed_statusline_never_learns_others_waiting(
        enforce_enrolment, tmp_path):
    """The reply is `{}` rather than a refusal string: `others_waiting` counts
    agents in *other* projects, so answering it to an unenrolled collector is a
    cross-project leak, not merely an over-permissive refusal."""
    from dark_army_daemon import enrollment
    from dark_army_daemon.daemon import BobDaemon
    root = tmp_path / "proj"
    root.mkdir()
    assert enrollment.enroll(str(root))[0]
    key = (root / enrollment.KEY_RELATIVE).read_text().strip()
    d = BobDaemon()
    reply = await d._handle_message(
        {"event": "statusline", "session_id": "s", "data": {"session_id": "s"}})
    assert reply == {}
    reply = await d._handle_message(
        {"event": "statusline", "session_id": "s", "key": key,
         "data": {"session_id": "s"}})
    assert "others_waiting" in reply


def test_an_unchanged_collector_is_not_rewritten(sandbox):
    """Same content, same exec bit → no write. Claude Code runs this file on
    every conversation update; the launch-time refresh must not race it with
    a rewrite it does not need. The inode is the proof: the atomic install
    goes through `os.replace`, which always changes it."""
    statusline.install_statusline_script()
    before = sandbox["script"].stat()
    statusline.install_statusline_script()
    after = sandbox["script"].stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_the_collector_is_replaced_never_truncated(sandbox, monkeypatch):
    """With `os.replace` taken away, the old copy survives intact — a
    truncating write would have left half a script for Claude Code to run."""
    import os as _os
    old = "# STATUSLINE_SCRIPT_VERSION: 2026-01-01-old\n"
    sandbox["script"].write_text(old, encoding="utf-8")
    monkeypatch.setattr(_os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(OSError("gone")))
    with pytest.raises(OSError):
        statusline.install_statusline_script()
    assert sandbox["script"].read_text(encoding="utf-8") == old
    assert not list(sandbox["script"].parent.glob("*.tmp"))


# --- the private hook socket ---------------------------------------------------

class _FakeUnixDaemon(_FakeDaemon):
    """`_FakeDaemon` on a Unix socket path instead of a loopback port."""

    def __init__(self, path: str, reply: dict):
        self.reply = reply
        self.received = None
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()


@pytest.fixture
def short_dir():
    # AF_UNIX paths are capped near 104 bytes; tmp_path is longer.
    import shutil
    import tempfile
    path = tempfile.mkdtemp(prefix="sl-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_script_reads_the_daemons_reply_over_the_private_socket(tmp_path, short_dir):
    """A socket named in the environment is the one address: the reply comes
    back through it (the port named beside it is never dialled)."""
    path = f"{short_dir}/hook.sock"
    daemon = _FakeUnixDaemon(path, {"others_waiting": 2, "agents": []})
    result = _run_script(tmp_path, REAL_PAYLOAD, 1,
                         env_extra={"DARK_ARMY_HOOK_SOCKET": path})
    daemon.join()
    assert result.returncode == 0, result.stderr
    assert daemon.received["event"] == "statusline"
    assert "2 agents waiting" in result.stdout


def test_script_with_the_quiet_socket_prints_its_own_line(tmp_path):
    result = _run_script(tmp_path, REAL_PAYLOAD, 1,
                         env_extra={"DARK_ARMY_HOOK_SOCKET": "/dev/null"})
    assert result.returncode == 0, result.stderr
    assert "opus 5" in result.stdout
    assert "waiting" not in result.stdout


def test_the_collector_dials_the_default_socket_with_nothing_named(tmp_path, short_dir):
    home = short_dir
    import os
    os.makedirs(f"{home}/.dark-army")
    daemon = _FakeUnixDaemon(f"{home}/.dark-army/hook.sock", {"others_waiting": 1})
    script = tmp_path / "dark-army-statusline"
    script.write_text(statusline.STATUSLINE_SCRIPT)
    result = subprocess.run(
        [sys.executable, str(script)], input=json.dumps(REAL_PAYLOAD),
        capture_output=True, text=True, timeout=10,
        env={"PATH": "/usr/bin:/bin", "HOME": home},
    )
    daemon.join()
    assert result.returncode == 0, result.stderr
    assert "1 agent waiting" in result.stdout
