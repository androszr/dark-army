"""The shunt skill's three wrappers, run as subprocesses against stub CLIs.

`bulk_read.py`, `code_write.py` and `exempt.py` are the pack template's own
files (`host/dark_army_menubar/agent_pack/template/.claude/skills/shunt/`),
stdlib-only and 3.9-safe, run here under the test interpreter with a fake
`PATH` holding `claude`, `codex` and `grok` stubs that echo their argv to a
file and print a canned answer. The argv each provider gets is pinned to
`card_prepare.argv`'s flag set minus the brief flags — the one place the
daemon's own headless-helper shape is decided — so the two cannot drift.
"""

from __future__ import annotations

import ast
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dark_army_daemon import card_prepare

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "host/dark_army_menubar/agent_pack/template/.claude/skills/shunt"
BULK = SKILL / "bulk_read.py"
WRITE = SKILL / "code_write.py"
EXEMPT = SKILL / "exempt.py"

#: The flags `card_prepare.argv` adds to carry the preparer's brief and its
#: attachments; the wrappers have no brief and no attachment rule.
BRIEF_FLAGS = {"--agents", "--agent", "--allowed-tools"}

STUB = '''#!/usr/bin/env python3
import json, os, sys, time
log = os.environ["STUB_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"exe": os.path.basename(sys.argv[0]), "argv": sys.argv[1:],
                         "pid": os.getpid(),
                         "env": {k: v for k, v in os.environ.items()
                                 if k.startswith(("CLAUDE", "GROK", "CODEX", "BOB_", "TERM_PROGRAM",
                                                  "DARK_ARMY_HOOK"))}}) + "\\n")
answer = os.environ.get("STUB_ANSWER", "the answer")
if os.environ.get("STUB_SLEEP"):
    # A helper still thinking when the wrapper is told to stop.
    time.sleep(float(os.environ["STUB_SLEEP"]))
probe = os.environ.get("STUB_PROBE")
if probe:
    # A second writer arriving while the helper thinks: does the target
    # exist, and can it be claimed with O_EXCL?
    claimed = True
    try:
        os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        claimed = False
    with open(log, "a") as fh:
        fh.write(json.dumps({"probe": probe, "exists": os.path.exists(probe),
                             "size": os.path.getsize(probe) if os.path.exists(probe) else None,
                             "claimed": claimed}) + "\\n")
if os.environ.get("STUB_FAIL"):
    sys.stderr.write("boom\\n")
    sys.exit(3)
if os.path.basename(sys.argv[0]) == "claude" and "--output-format" in sys.argv:
    sys.stdout.write(json.dumps({"result": answer, "total_cost_usd": 0.0123}))
else:
    sys.stdout.write(answer)
'''


@pytest.fixture()
def stubs(tmp_path):
    """A PATH with the three CLIs stubbed, an isolated HOME, a project."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex", "grok"):
        exe = bin_dir / name
        exe.write_text(STUB)
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "big.py").write_text("line\n" * 400)
    (project / "ref_test.py").write_text("def test_ref():\n    assert True\n")
    log = tmp_path / "stub.log"
    return {"bin": bin_dir, "home": home, "project": project, "log": log}


def _run(stubs, script, *args, env=None, provider=None):
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDE", "GROK", "CODEX", "BOB_"))}
    # The stub is a python script; PATH holds the stub dir and the test
    # interpreter's own bin so `#!/usr/bin/env python3` resolves.
    environment["PATH"] = os.pathsep.join([str(stubs["bin"]),
                                           str(Path(sys.executable).parent),
                                           "/usr/bin", "/bin"])
    environment["HOME"] = str(stubs["home"])
    environment["STUB_LOG"] = str(stubs["log"])
    if provider:
        environment["BOB_SHUNT_PROVIDER"] = provider
    environment.update(env or {})
    return subprocess.run([sys.executable, str(script), *args], cwd=str(stubs["project"]),
                          env=environment, capture_output=True, text=True, timeout=30)


def _calls(stubs):
    if not stubs["log"].exists():
        return []
    return [json.loads(line) for line in stubs["log"].read_text().splitlines() if line]


def _ledger_lines(stubs):
    folder = stubs["home"] / ".dark-army" / "shunt"
    out = []
    for path in sorted(folder.glob("*.jsonl")) if folder.exists() else []:
        out.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    return out


def _flags(argv):
    return {a for a in argv if a.startswith("--")}


def _prepare_flags(provider):
    fields = {"tool": provider, "title": "t", "summary": "s"}
    if provider == "grok":
        fields["brief_path"] = "/tmp/brief.md"
    return _flags(card_prepare.argv("/x/" + provider, model="m", **fields)) - BRIEF_FLAGS


# --- the files themselves ------------------------------------------------------

@pytest.mark.parametrize("script", [BULK, WRITE, EXEMPT], ids=["bulk_read", "code_write", "exempt"])
def test_each_wrapper_parses_under_3_9_and_imports_only_the_stdlib(script):
    tree = ast.parse(script.read_text(encoding="utf-8"), feature_version=(3, 9))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= set(sys.stdlib_module_names), names
    assert "bob_companion" not in script.read_text(encoding="utf-8")


def test_the_shipped_workers_table_is_the_daemons():
    table = json.loads((SKILL / "workers.json").read_text(encoding="utf-8"))
    assert table == card_prepare.WORKER_MODELS


# --- argv parity with the Prepare helper ----------------------------------------

@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_bulk_read_argv_is_the_prepare_helpers_minus_the_brief_flags(stubs, provider):
    proc = _run(stubs, BULK, "--question", "where is x", "big.py", provider=provider)
    assert proc.returncode == 0, proc.stderr
    call = _calls(stubs)[-1]
    assert call["exe"] == provider
    wrapper = _flags(call["argv"])
    # Claude gets `--output-format json` for the cost figure; nothing else differs.
    extra = {"--output-format"} if provider == "claude" else set()
    assert wrapper - extra == _prepare_flags(provider), (wrapper, _prepare_flags(provider))
    # The model is the shipped worker for that provider.
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == card_prepare.WORKER_MODELS[provider]
    if provider == "codex":
        assert argv[:1] == ["exec"] and argv[argv.index("--sandbox") + 1] == "read-only"


def test_the_prompt_carries_the_no_tools_line_the_question_and_the_file(stubs):
    _run(stubs, BULK, "--question", "where is x", "big.py", provider="claude")
    argv = _calls(stubs)[-1]["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert prompt.startswith("Do not use any tools.")
    assert "QUESTION: where is x" in prompt
    assert '<file path="big.py">' in prompt and prompt.count("line\n") == 400


def test_the_worker_never_inherits_the_sessions_identity(stubs):
    env = {"CLAUDE_CODE_SESSION_ID": "parent-1", "CLAUDECODE": "1",
           "CLAUDE_CODE_MESSAGING_TOKEN": "t", "TERM_PROGRAM": "vscode",
           "BOB_COMPANION_ORIGIN": "card:1", "BOB_COMPANION_PORT": "19873"}
    proc = _run(stubs, BULK, "--question", "q", "big.py", env=env)
    assert proc.returncode == 0, proc.stderr
    seen = _calls(stubs)[-1]["env"]
    for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN",
                "TERM_PROGRAM", "BOB_COMPANION_ORIGIN"):
        assert key not in seen, key
    assert seen["BOB_COMPANION_PORT"] == "1"
    # Quiet on the private hook socket too, for a notify hook of either age.
    assert seen["DARK_ARMY_HOOK_SOCKET"] == "/dev/null"
    # And the provider was read off the session id, the ledger keyed by it.
    assert _calls(stubs)[-1]["exe"] == "claude"
    assert (stubs["home"] / ".dark-army" / "shunt" / "parent-1.jsonl").is_file()


def test_the_model_override_wins_for_one_call(stubs):
    _run(stubs, BULK, "--question", "q", "big.py", provider="grok",
         env={"BOB_SHUNT_WORKER_MODEL": "grok-4.6"})
    argv = _calls(stubs)[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "grok-4.6"


# --- bulk_read -----------------------------------------------------------------

def test_bulk_read_prints_the_answer_alone_and_logs_the_claude_cost(stubs):
    proc = _run(stubs, BULK, "--question", "q", "big.py", provider="claude",
                env={"STUB_ANSWER": "found in big.py:12"})
    assert proc.returncode == 0
    assert proc.stdout == "found in big.py:12\n"
    (record,) = _ledger_lines(stubs)
    assert set(record) == {"delegation_id", "at", "session_id", "provider", "mode",
                           "model", "files", "lines_kept_out", "worker_cost_usd",
                           "seconds", "ok"}
    assert record["mode"] == "bulk-read" and record["provider"] == "claude"
    assert record["files"] == ["big.py"] and record["lines_kept_out"] == 400
    assert record["worker_cost_usd"] == 0.0123 and record["ok"] is True
    assert "q" not in json.dumps(record["files"])
    folder = stubs["home"] / ".dark-army" / "shunt"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE(next(folder.glob("*.jsonl")).stat().st_mode) == 0o600


@pytest.mark.parametrize("provider", ["codex", "grok"])
def test_cost_is_null_where_the_assistant_reports_none(stubs, provider):
    proc = _run(stubs, BULK, "--question", "q", "big.py", provider=provider)
    assert proc.returncode == 0, proc.stderr
    (record,) = _ledger_lines(stubs)
    assert record["worker_cost_usd"] is None
    assert record["provider"] == provider


def test_bulk_read_refuses_a_missing_file_before_calling_anyone(stubs):
    proc = _run(stubs, BULK, "--question", "q", "nowhere.py", provider="claude")
    assert proc.returncode == 2
    assert "cannot read nowhere.py" in proc.stderr
    assert _calls(stubs) == [] and _ledger_lines(stubs) == []


def test_a_failed_helper_is_a_nonzero_exit_and_a_ledger_line_that_says_so(stubs):
    proc = _run(stubs, BULK, "--question", "q", "big.py", provider="codex",
                env={"STUB_FAIL": "1"})
    assert proc.returncode == 1
    assert "helper failed" in proc.stderr and "boom" in proc.stderr
    (record,) = _ledger_lines(stubs)
    assert record["ok"] is False
    # Nothing was kept out of the main model: the caller reads big.py another way.
    assert record["lines_kept_out"] == 0 and record["files"] == ["big.py"]


def test_a_question_never_reaches_the_ledger(stubs):
    _run(stubs, BULK, "--question", "SECRET-QUESTION-TEXT", "big.py", provider="claude")
    text = "".join(p.read_text() for p in
                   (stubs["home"] / ".dark-army" / "shunt").glob("*.jsonl"))
    assert "SECRET-QUESTION-TEXT" not in text


# --- code_write ------------------------------------------------------------------

def test_code_write_writes_the_file_prints_one_line_and_logs_the_lines(stubs):
    body = "def test_new():\n    assert 1\n\n\ndef test_two():\n    assert 2\n"
    proc = _run(stubs, WRITE, "--spec", "two tests", "--reference", "ref_test.py",
                "--out", "test_new.py", provider="claude", env={"STUB_ANSWER": body})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "wrote test_new.py (6 lines)\n"
    assert (stubs["project"] / "test_new.py").read_text() == body
    (record,) = _ledger_lines(stubs)
    assert record["mode"] == "code-write" and record["lines_kept_out"] == 6
    assert record["files"] == ["test_new.py"]
    argv = _calls(stubs)[-1]["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert "SPEC: two tests" in prompt and '<reference path="ref_test.py">' in prompt
    assert "def test_ref" in prompt
    assert not list(stubs["project"].glob("test_new.py.*.tmp"))


def test_code_write_strips_a_fence_the_helper_added_anyway(stubs):
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "fenced.py", provider="grok",
                env={"STUB_ANSWER": "```python\nx = 1\n```\n"})
    assert proc.returncode == 0, proc.stderr
    assert (stubs["project"] / "fenced.py").read_text() == "x = 1\n"


def test_code_write_refuses_an_existing_target(stubs):
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "big.py", provider="claude")
    assert proc.returncode == 2
    assert "already exists" in proc.stderr and "never delegated" in proc.stderr
    assert _calls(stubs) == []
    assert (stubs["project"] / "big.py").read_text() == "line\n" * 400


def test_code_write_claims_the_target_before_the_helper_runs(stubs):
    # The refusal used to be a `lexists` at the start and an `os.replace`
    # 10-240 s later; a file created in between was overwritten. Now the
    # target is ours (empty, O_EXCL) from before the helper starts, so a
    # second writer's own O_EXCL claim fails while it thinks.
    target = stubs["project"] / "new.py"
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "new.py", provider="codex",
                env={"STUB_ANSWER": "def test_new():\n    assert 1\n",
                     "STUB_PROBE": str(target)})
    assert proc.returncode == 0, proc.stderr
    (probe,) = [c for c in _calls(stubs) if "probe" in c]
    assert probe["exists"] is True and probe["size"] == 0 and probe["claimed"] is False
    assert target.read_text() == "def test_new():\n    assert 1\n"


def test_code_write_leaves_no_placeholder_when_it_refuses_or_fails(stubs):
    target = stubs["project"] / "new.py"
    # The helper is missing from PATH: refused after the claim, placeholder gone.
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "new.py", provider="codex", env={"PATH": "/usr/bin:/bin"})
    assert proc.returncode == 2 and "no codex executable" in proc.stderr
    assert not target.exists()
    # The helper failed: the placeholder goes with it.
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "new.py", provider="codex", env={"STUB_FAIL": "1"})
    assert proc.returncode == 1 and not target.exists()
    stubs["log"].unlink()
    # A target in a folder that does not exist is refused at the claim.
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "missing/new.py", provider="codex")
    assert proc.returncode == 2 and "cannot create missing/new.py" in proc.stderr
    assert _calls(stubs) == []


def test_code_write_refuses_without_a_reference(stubs):
    proc = _run(stubs, WRITE, "--spec", "s", "--out", "new.py", provider="claude")
    assert proc.returncode == 2
    assert "--reference is required" in proc.stderr
    assert _calls(stubs) == [] and not (stubs["project"] / "new.py").exists()


def test_code_write_leaves_no_file_when_the_helper_fails(stubs):
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "new.py", provider="codex", env={"STUB_FAIL": "1"})
    assert proc.returncode == 1
    assert not (stubs["project"] / "new.py").exists()
    (record,) = _ledger_lines(stubs)
    assert record["ok"] is False and record["lines_kept_out"] == 0


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie answers kill(0); ask ps whether it has really gone.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _wait_until(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_code_write_killed_mid_helper_leaves_no_placeholder_and_no_orphan(stubs):
    """SIGTERM (the assistant's Bash timeout) used to skip the `finally`:
    the empty O_EXCL placeholder stayed, the retry was refused as "already
    exists", and the helper kept running with nobody waiting on it. Now the
    signal unwinds as SystemExit(143): the child is killed, the placeholder
    removed."""
    target = stubs["project"] / "new.py"
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDE", "GROK", "CODEX", "BOB_"))}
    environment.update({
        "PATH": os.pathsep.join([str(stubs["bin"]), str(Path(sys.executable).parent),
                                 "/usr/bin", "/bin"]),
        "HOME": str(stubs["home"]), "STUB_LOG": str(stubs["log"]),
        "BOB_SHUNT_PROVIDER": "codex", "STUB_SLEEP": "30",
    })
    wrapper = subprocess.Popen(
        [sys.executable, str(WRITE), "--spec", "s", "--reference", "ref_test.py",
         "--out", "new.py"], cwd=str(stubs["project"]), env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _wait_until(lambda: any("pid" in c for c in _calls(stubs))), "helper never started"
        (call,) = [c for c in _calls(stubs) if "pid" in c]
        helper_pid = call["pid"]
        assert target.exists() and target.stat().st_size == 0  # claimed while it thinks
        wrapper.send_signal(signal.SIGTERM)
        _, stderr = wrapper.communicate(timeout=15)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
    assert wrapper.returncode == 143, stderr
    assert not target.exists()
    assert _wait_until(lambda: not _pid_alive(helper_pid)), "helper orphaned"
    # The retry is not refused.
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "new.py", provider="codex",
                env={"STUB_ANSWER": "def test_new():\n    assert 1\n"})
    assert proc.returncode == 0, proc.stderr
    assert "adopting" not in proc.stderr
    assert target.read_text() == "def test_new():\n    assert 1\n"


def test_code_write_adopts_an_empty_placeholder_and_still_refuses_content(stubs):
    target = stubs["project"] / "new.py"
    target.write_text("")
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "new.py", provider="codex",
                env={"STUB_ANSWER": "def test_new():\n    assert 1\n"})
    assert proc.returncode == 0, proc.stderr
    assert "adopting an empty placeholder left by an earlier attempt" in proc.stderr
    assert proc.stdout == "wrote new.py (2 lines)\n"
    assert target.read_text() == "def test_new():\n    assert 1\n"
    # A file with content is somebody's work: refused, untouched, no helper call.
    stubs["log"].unlink()
    (stubs["project"] / "theirs.py").write_text("x = 1\n")
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "theirs.py", provider="codex")
    assert proc.returncode == 2 and "already exists" in proc.stderr
    assert "adopting" not in proc.stderr
    assert (stubs["project"] / "theirs.py").read_text() == "x = 1\n"
    assert _calls(stubs) == []


def test_code_write_leaves_an_adopted_empty_file_as_found_on_every_failure(stubs):
    """An empty file that was on disk before the command started is
    somebody's (`pkg/__init__.py` is the usual one): adopted as the target,
    but never unlinked when the helper does not deliver. It used to be
    removed by the same `finally` that removes our own placeholder."""
    pkg = stubs["project"] / "pkg"
    pkg.mkdir()
    target = pkg / "__init__.py"
    target.write_text("")
    # The helper is missing from PATH.
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "pkg/__init__.py", provider="codex", env={"PATH": "/usr/bin:/bin"})
    assert proc.returncode == 2 and "no codex executable" in proc.stderr
    assert "adopting" in proc.stderr
    assert target.exists() and target.stat().st_size == 0
    # The helper failed.
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "pkg/__init__.py", provider="codex", env={"STUB_FAIL": "1"})
    assert proc.returncode == 1
    assert target.exists() and target.stat().st_size == 0
    # The reference is over the prompt bound.
    (stubs["project"] / "huge_ref.py").write_text("x = 1\n" * 200_000)
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "huge_ref.py",
                "--out", "pkg/__init__.py", provider="codex")
    assert proc.returncode == 2 and "smaller exemplar" in proc.stderr
    assert target.exists() and target.stat().st_size == 0
    # The helper timed out (a short bound for the test, via the stub's sleep
    # being longer than the wrapper's own kill below).
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDE", "GROK", "CODEX", "BOB_"))}
    environment.update({
        "PATH": os.pathsep.join([str(stubs["bin"]), str(Path(sys.executable).parent),
                                 "/usr/bin", "/bin"]),
        "HOME": str(stubs["home"]), "STUB_LOG": str(stubs["log"]),
        "BOB_SHUNT_PROVIDER": "codex", "STUB_SLEEP": "30",
    })
    stubs["log"].unlink()
    wrapper = subprocess.Popen(
        [sys.executable, str(WRITE), "--spec", "s", "--reference", "ref_test.py",
         "--out", "pkg/__init__.py"], cwd=str(stubs["project"]), env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _wait_until(lambda: any("pid" in c for c in _calls(stubs))), "helper never started"
        wrapper.send_signal(signal.SIGTERM)
        _, stderr = wrapper.communicate(timeout=15)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
    assert wrapper.returncode == 143, stderr
    assert target.exists() and target.stat().st_size == 0
    # Success still replaces it with the helper's file.
    proc = _run(stubs, WRITE, "--spec", "s", "--reference", "ref_test.py",
                "--out", "pkg/__init__.py", provider="codex",
                env={"STUB_ANSWER": "VERSION = 1\n"})
    assert proc.returncode == 0, proc.stderr
    assert target.read_text() == "VERSION = 1\n"


def _killed_mid_helper(stubs, script, args, sig):
    """Start `script` against a helper that sleeps, send `sig` once the
    helper is up, and return (wrapper exit, stderr, helper pid)."""
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDE", "GROK", "CODEX", "BOB_"))}
    environment.update({
        "PATH": os.pathsep.join([str(stubs["bin"]), str(Path(sys.executable).parent),
                                 "/usr/bin", "/bin"]),
        "HOME": str(stubs["home"]), "STUB_LOG": str(stubs["log"]),
        "BOB_SHUNT_PROVIDER": "codex", "STUB_SLEEP": "30",
    })
    wrapper = subprocess.Popen(
        [sys.executable, str(script), *args], cwd=str(stubs["project"]), env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _wait_until(lambda: any("pid" in c for c in _calls(stubs))), "helper never started"
        (call,) = [c for c in _calls(stubs) if "pid" in c]
        wrapper.send_signal(sig)
        _, stderr = wrapper.communicate(timeout=15)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
    return wrapper.returncode, stderr, call["pid"]


def test_code_write_on_sighup_unwinds_like_sigterm(stubs):
    """A closed terminal is a SIGHUP, which the docstring always promised
    and only SIGTERM delivered: the placeholder stayed and the helper ran on."""
    target = stubs["project"] / "new.py"
    code, stderr, helper_pid = _killed_mid_helper(
        stubs, WRITE, ["--spec", "s", "--reference", "ref_test.py", "--out", "new.py"],
        signal.SIGHUP)
    assert code == 129, stderr
    assert not target.exists()
    assert _wait_until(lambda: not _pid_alive(helper_pid)), "helper orphaned"


@pytest.mark.parametrize("sig,code", [(signal.SIGTERM, 143), (signal.SIGHUP, 129)])
def test_bulk_read_killed_mid_helper_leaves_no_orphan(stubs, sig, code):
    """The assistant's Bash timeout kills the wrapper; without a handler the
    `claude` child ran on to its own 110 s with nobody waiting on it."""
    exit_code, stderr, helper_pid = _killed_mid_helper(
        stubs, BULK, ["--question", "q", "big.py"], sig)
    assert exit_code == code, stderr
    assert _wait_until(lambda: not _pid_alive(helper_pid)), "helper orphaned"
    assert _ledger_lines(stubs) == []


def test_the_wrappers_time_out_inside_the_assistants_default_bash_timeout():
    """Claude Code kills a Bash call at 120 s by default; a wrapper that
    waited longer would be killed before its own cleanup ran. The comment
    beside the figure says so, in both wrappers, and no longer speaks of
    "a minute of silence"."""
    for script in (BULK, WRITE):
        text = script.read_text(encoding="utf-8")
        (line,) = [l for l in text.splitlines() if l.startswith("TIMEOUT_SECONDS = ")]
        assert int(line.split("=")[1].split("#")[0].strip()) < 120, script.name
        assert "120 s default Bash timeout" in text, script.name
        assert "minute of silence" not in text, script.name


# --- exempt ----------------------------------------------------------------------

def test_exempt_on_off_status_round_trip(stubs):
    env = {"CLAUDE_CODE_SESSION_ID": "sess-a", "CODEX_THREAD_ID": "thread-b"}
    marker_dir = stubs["home"] / ".dark-army" / "shunt-exempt"
    on = _run(stubs, EXEMPT, "on", env=env)
    assert on.returncode == 0 and "on for sess-a, thread-b" in on.stdout
    assert (marker_dir / "sess-a").is_file() and (marker_dir / "thread-b").is_file()
    assert stat.S_IMODE(marker_dir.stat().st_mode) == 0o700
    status = _run(stubs, EXEMPT, "status", env=env)
    assert "on sess-a" in status.stdout and "on thread-b" in status.stdout
    off = _run(stubs, EXEMPT, "off", env=env)
    assert off.returncode == 0 and "off for sess-a, thread-b" in off.stdout
    assert not (marker_dir / "sess-a").exists()
    assert "off sess-a" in _run(stubs, EXEMPT, "status", env=env).stdout


def test_exempt_off_prunes_markers_older_than_a_day(stubs):
    marker_dir = stubs["home"] / ".dark-army" / "shunt-exempt"
    marker_dir.mkdir(parents=True)
    stale = marker_dir / "old-session"
    stale.write_text("")
    os.utime(stale, (1, 1))
    fresh = marker_dir / "fresh-session"
    fresh.write_text("")
    proc = _run(stubs, EXEMPT, "off", env={"GROK_SESSION_ID": "sess-g"})
    assert proc.returncode == 0
    assert "pruned 1 marker" in proc.stdout
    assert not stale.exists() and fresh.exists()


def test_exempt_with_no_session_id_exits_zero_and_writes_nothing(stubs):
    proc = _run(stubs, EXEMPT, "on")
    assert proc.returncode == 0 and "nothing to open" in proc.stdout
    assert not (stubs["home"] / ".dark-army" / "shunt-exempt").exists()
    assert _run(stubs, EXEMPT, "bogus").returncode == 2


# --- the state folder's two names -------------------------------------------------
# On a Mac still on the old install only `~/.bob-companion` exists; a wrapper
# updated first must write there and never create `~/.dark-army` early (the
# app's first-launch move would then have to set that folder aside).

def test_a_mac_not_yet_moved_keeps_its_markers_in_the_old_folder(stubs):
    old = stubs["home"] / ".bob-companion"
    old.mkdir()
    env = {"CLAUDE_CODE_SESSION_ID": "sess-old"}
    assert _run(stubs, EXEMPT, "on", env=env).returncode == 0
    assert (old / "shunt-exempt" / "sess-old").is_file()
    assert not (stubs["home"] / ".dark-army").exists()


def test_the_new_folder_wins_where_both_exist(stubs):
    (stubs["home"] / ".bob-companion").mkdir()
    new = stubs["home"] / ".dark-army"
    new.mkdir()
    env = {"CLAUDE_CODE_SESSION_ID": "sess-new"}
    assert _run(stubs, EXEMPT, "on", env=env).returncode == 0
    assert (new / "shunt-exempt" / "sess-new").is_file()
    assert not (stubs["home"] / ".bob-companion" / "shunt-exempt").exists()


def test_every_wrapper_carries_the_same_state_home_rule():
    bodies = set()
    for script in (BULK, WRITE, EXEMPT):
        text = script.read_text(encoding="utf-8")
        start = text.index("def _state_home(env):")
        bodies.add(text[start:text.index("\n\n\n", start)])
    assert len(bodies) == 1
