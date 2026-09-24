"""The shunt guard (`dark_army_menubar/shunt_hook.py`, installed as the
bytes of `hooks.SHUNT_SCRIPT`).

Every decision is driven in-process: `decide()` is imported and called with a
synthetic PreToolUse payload, `HOME` and the project on `tmp_path`, and what it
prints — one deny object or nothing — is the whole contract. The process
layer (`main()`'s stdin parse, the closed-stdout allow, exit 0) is proven by
the few tests that write the installed bytes to a temp file and spawn the test
interpreter and the 3.9 system one. The 3.9 parse and the stdlib-only import
set are pinned here too, because the installed copy runs under the system
interpreter.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest import mock

import pytest

from dark_army_menubar import shunt_hook
from dark_army_menubar.hooks import (SHUNT_MATCHER, SHUNT_REVIEWER_SUFFIXES,
                                         SHUNT_SCRIPT)


@pytest.fixture()
def project(tmp_path):
    """An enrolled-looking project carrying the skill, a 351-line file, a
    350-line one, and an isolated HOME beside it."""
    root = tmp_path / "proj"
    (root / ".claude" / "skills" / "shunt").mkdir(parents=True)
    (root / ".claude" / "skills" / "shunt" / "SKILL.md").write_text("# shunt\n")
    (root / "big.py").write_text("x = 1\n" * 351)
    (root / "small.py").write_text("x = 1\n" * 350)
    home = tmp_path / "home"
    home.mkdir()
    return root, home


def _environment(home, env=None):
    """The guard's environment: the session variables stripped, `HOME` on
    `tmp_path`, plus whatever the test adds."""
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("CLAUDE", "GROK", "CODEX", "BOB_"))}
    environment["HOME"] = str(home)
    environment.update(env or {})
    return environment


def _run(payload, home, *, env=None):
    """`(stdout, exit)` of the guard for one payload, in-process: `decide()`
    under the guard's environment, the deny printed the way `main()` prints
    it. Exit is always 0 here; `_run_process` proves the process contract."""
    with mock.patch.dict(os.environ, _environment(home, env), clear=True):
        verdict = shunt_hook.decide(payload)
        out = io.StringIO()
        if verdict is not None:
            with contextlib.redirect_stdout(out):
                shunt_hook._deny(*verdict)
    return out.getvalue(), 0


def _run_process(payload, home, *, env=None, raw=None):
    """`(stdout, exit)` of the installed bytes under a subprocess."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(SHUNT_SCRIPT)
        path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, path],
            input=(raw if raw is not None else json.dumps(payload)).encode("utf-8"),
            env=_environment(home, env), timeout=10.0, capture_output=True)
    finally:
        os.unlink(path)
    return proc.stdout.decode("utf-8"), proc.returncode


def _read(root, path, **extra):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Read",
               "tool_input": {"file_path": path}, "cwd": str(root),
               "session_id": "sess-1"}
    payload.update(extra)
    return payload


def _bash(root, command, tool="Bash", **extra):
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"command": command}, "cwd": str(root),
               "session_id": "sess-1"}
    payload.update(extra)
    return payload


def _deny(out):
    data = json.loads(out)
    return data["hookSpecificOutput"]


# --- the string itself -------------------------------------------------------

def test_the_installed_bytes_are_the_module_file():
    source = pathlib.Path(shunt_hook.__file__).read_text(encoding="utf-8")
    assert SHUNT_SCRIPT == source
    assert SHUNT_SCRIPT.startswith("#!/usr/bin/env python3\n")


def test_the_guard_parses_under_python_3_9_and_imports_only_the_stdlib():
    tree = ast.parse(SHUNT_SCRIPT, feature_version=(3, 9))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= set(sys.stdlib_module_names), names
    assert "bob_companion" not in SHUNT_SCRIPT
    assert "socket" not in names and "subprocess" not in names


def test_the_matcher_names_every_read_and_shell_tool_the_guard_watches():
    watched = set(SHUNT_MATCHER.split("|"))
    assert watched == {"Read", "Bash", "read_file", "bash", "shell"}
    for suffix in SHUNT_REVIEWER_SUFFIXES:
        assert suffix in SHUNT_SCRIPT


# --- the read tool -----------------------------------------------------------

def test_a_read_over_the_threshold_is_denied_with_the_skill_named(project):
    root, home = project
    out, code = _run(_read(root, "big.py"), home)
    assert code == 0
    verdict = _deny(out)
    assert verdict["hookEventName"] == "PreToolUse"
    assert verdict["permissionDecision"] == "deny"
    reason = verdict["permissionDecisionReason"]
    assert reason, "Codex refuses a deny with an empty reason"
    assert "351 lines" in reason and "350-line" in reason
    assert "python3 .claude/skills/shunt/bulk_read.py" in reason
    assert "exempt.py on" in reason and "sed -n" in reason


def test_a_read_at_the_threshold_is_allowed(project):
    root, home = project
    out, code = _run(_read(root, "small.py"), home)
    assert (out, code) == ("", 0)


def test_an_absolute_path_and_groks_read_file_are_judged_the_same(project):
    root, home = project
    payload = {"hook_event_name": "PreToolUse", "tool_name": "read_file",
               "tool_input": {"path": str(root / "big.py")}, "cwd": str(root)}
    out, _ = _run(payload, home)
    assert _deny(out)["permissionDecision"] == "deny"


def test_a_bounded_read_of_a_slice_is_an_edit_reading_its_section(project):
    root, home = project
    payload = _read(root, "big.py")
    payload["tool_input"]["limit"] = 40
    payload["tool_input"]["offset"] = 100
    assert _run(payload, home) == ("", 0)
    payload["tool_input"]["limit"] = 351
    assert _deny(_run(payload, home)[0])["permissionDecision"] == "deny"


# --- the shell tool ----------------------------------------------------------

@pytest.mark.parametrize("command", [
    "sed -n 10,40p big.py",
    "grep -n x big.py",
    "rg TODO big.py",
    "awk 'NR<20' big.py",
    "wc -l big.py",
    "cat big.py | grep x",
    "head -n 20 big.py",
    "tail -50 big.py",
    "cat small.py",
    "cat missing.py",
])
def test_a_bounded_or_piped_shell_read_is_allowed(project, command):
    root, home = project
    assert _run(_bash(root, command), home) == ("", 0)


@pytest.mark.parametrize("command", [
    "cat big.py",
    "cat -n big.py",
    "head -n 400 big.py",
    "tail big.py",
    "less big.py",
    "more big.py",
    "cd sub && cat ../big.py",
    "FOO=1 cat big.py",
])
def test_a_whole_file_shell_read_is_denied(project, command):
    root, home = project
    (root / "sub").mkdir(exist_ok=True)
    out, code = _run(_bash(root, command), home)
    assert code == 0
    assert _deny(out)["permissionDecision"] == "deny"
    assert "big.py" in _deny(out)["permissionDecisionReason"]


def test_codex_shell_argv_list_and_grok_bash_are_judged_too(project):
    root, home = project
    codex = _bash(root, ["bash", "-lc", "cat big.py"], tool="shell")
    assert _deny(_run(codex, home)[0])["permissionDecision"] == "deny"
    grok = _bash(root, "cat big.py", tool="bash")
    assert _deny(_run(grok, home)[0])["permissionDecision"] == "deny"


# --- exemptions ----------------------------------------------------------------

@pytest.mark.parametrize("agent", ["bc-verifier", "xy-bug-auditor",
                                   "bc-integration-reviewer",
                                   "proj-security-reviewer"])
def test_reviewer_agent_type_is_never_blocked(project, agent):
    root, home = project
    assert _run(_read(root, "big.py", agent_type=agent), home) == ("", 0)
    assert _run(_bash(root, "cat big.py", subagent_type=agent), home) == ("", 0)


def test_a_writer_agent_type_is_still_blocked(project):
    root, home = project
    out, _ = _run(_read(root, "big.py", agent_type="bc-implementer"), home)
    assert _deny(out)["permissionDecision"] == "deny"


def test_marker_file_exempts_the_session(project):
    root, home = project
    marker_dir = home / ".dark-army" / "shunt-exempt"
    marker_dir.mkdir(parents=True)
    (marker_dir / "sess-1").write_text("")
    assert _run(_read(root, "big.py"), home) == ("", 0)
    # Another session's marker exempts nobody else.
    out, _ = _run(_read(root, "big.py", session_id="sess-2"), home)
    assert _deny(out)["permissionDecision"] == "deny"


def test_a_marker_for_the_parent_session_covers_a_sub_agent(project):
    root, home = project
    marker_dir = home / ".dark-army" / "shunt-exempt"
    marker_dir.mkdir(parents=True)
    (marker_dir / "parent-9").write_text("")
    payload = _read(root, "big.py", session_id="child-1",
                    parentSessionId="parent-9")
    assert _run(payload, home) == ("", 0)


# --- the threshold ---------------------------------------------------------------

def test_the_threshold_comes_from_the_environment_first(project):
    root, home = project
    assert _run(_read(root, "big.py"), home, env={"BOB_SHUNT_MIN_LINES": "400"}) == ("", 0)
    out, _ = _run(_read(root, "small.py"), home, env={"BOB_SHUNT_MIN_LINES": "100"})
    assert "100-line" in _deny(out)["permissionDecisionReason"]


def test_the_threshold_comes_from_the_projects_settings_next(project):
    root, home = project
    (root / ".claude" / "settings.json").write_text(json.dumps(
        {"env": {"BOB_SHUNT_MIN_LINES": "600"}, "permissions": {"allow": []}}))
    assert _run(_read(root, "big.py"), home) == ("", 0)
    # The environment still wins over the project.
    out, _ = _run(_read(root, "big.py"), home, env={"BOB_SHUNT_MIN_LINES": "10"})
    assert "10-line" in _deny(out)["permissionDecisionReason"]


def test_a_broken_settings_file_or_a_bad_value_falls_back_to_the_default(project):
    root, home = project
    (root / ".claude" / "settings.json").write_text("{not json")
    assert _deny(_run(_read(root, "big.py"), home)[0])["permissionDecision"] == "deny"
    (root / ".claude" / "settings.json").write_text(json.dumps({"env": {"BOB_SHUNT_MIN_LINES": "lots"}}))
    assert _deny(_run(_read(root, "big.py"), home)[0])["permissionDecision"] == "deny"


# --- the four silent cases ---------------------------------------------------------

def test_no_skill_in_the_project_means_the_guard_is_disarmed(project):
    root, home = project
    (root / ".claude" / "skills" / "shunt" / "SKILL.md").unlink()
    assert _run(_read(root, "big.py"), home) == ("", 0)


def test_a_session_under_home_never_arms_on_a_mirrored_skill(project):
    root, home = project
    # Somebody mirrored the skill into their home folder; a session whose cwd
    # is the home directory itself must not arm on it.
    (home / ".claude" / "skills" / "shunt").mkdir(parents=True)
    (home / ".claude" / "skills" / "shunt" / "SKILL.md").write_text("# shunt\n")
    (home / "big.py").write_text("x\n" * 400)
    payload = _read(home, "big.py")
    assert _run(payload, home) == ("", 0)


def test_a_missing_path_and_non_json_stdin_print_nothing(project):
    root, home = project
    assert _run(_read(root, "nowhere.py"), home) == ("", 0)
    assert _run_process(None, home, raw="not json at all") == ("", 0)
    assert _run_process(None, home, raw="") == ("", 0)


def test_the_process_prints_what_decide_decides(project):
    """The installed bytes under a real interpreter agree with the in-process
    call on one deny and one allow, so the rest of the file can stay
    in-process."""
    root, home = project
    for name in ("big.py", "small.py"):
        payload = _read(root, name)
        assert _run_process(payload, home) == _run(payload, home)
    assert _deny(_run_process(_read(root, "big.py"), home)[0])[
        "permissionDecision"] == "deny"


@pytest.mark.parametrize("how", ["broken pipe", "closed fd"])
def test_a_deny_that_cannot_reach_stdout_is_a_silent_allow(project, how):
    """`_deny` used to sit outside `main()`'s try: a harness that had closed
    the pipe turned the deny into a traceback and an exit the harness
    reports as a hook error. Every failure is an allow — exit 0, nothing on
    stderr — under the test interpreter and the 3.9 system one."""
    root, home = project
    payload = json.dumps(_read(root, "big.py")).encode("utf-8")
    environment = _environment(home)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(SHUNT_SCRIPT)
        path = f.name
    try:
        for python in (sys.executable, "/usr/bin/python3"):
            if how == "broken pipe":
                read_end, write_end = os.pipe()
                os.close(read_end)
                proc = subprocess.Popen([python, path], stdin=subprocess.PIPE,
                                        stdout=write_end, stderr=subprocess.PIPE,
                                        env=environment)
                os.close(write_end)
                _, stderr = proc.communicate(payload, timeout=30.0)
                code = proc.returncode
            else:
                done = subprocess.run(f"{python} {path} >&-", shell=True, input=payload,
                                      stderr=subprocess.PIPE, env=environment, timeout=30.0)
                code, stderr = done.returncode, done.stderr
            assert (code, stderr) == (0, b""), (python, how, code, stderr)
    finally:
        os.unlink(path)


def test_another_event_or_tool_prints_nothing(project):
    root, home = project
    other = _read(root, "big.py", hook_event_name="PostToolUse")
    assert _run(other, home) == ("", 0)
    edit = _read(root, "big.py", tool_name="Edit")
    assert _run(edit, home) == ("", 0)


def test_a_directory_or_a_fifo_is_never_over_the_threshold(project):
    root, home = project
    (root / "folder").mkdir()
    assert _run(_read(root, "folder"), home) == ("", 0)
    os.mkfifo(root / "pipe")
    assert _run(_read(root, "pipe"), home) == ("", 0)


# --- the audit's findings (2026-09-20, dispatch 4) ---------------------------

def test_a_binary_file_is_never_over_whatever_its_newline_bytes(project):
    # A PNG or a PDF is full of 0x0A bytes that are not lines; the guard used
    # to refuse `Read` of an image as "N lines".
    root, home = project
    (root / "art.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\n" * 2000)
    assert _run(_read(root, "art.png"), home) == ("", 0)
    (root / "cat.pdf").write_bytes(b"%PDF-1.4\n" + b"\xff\xfe\n" * 2000)
    assert _run(_read(root, "cat.pdf"), home) == ("", 0)
    # A shell read of it says nothing either.
    assert _run(_bash(root, "cat art.png"), home) == ("", 0)


def test_a_read_that_names_pages_is_a_pdf_read_the_harness_slices(project):
    root, home = project
    paged = _read(root, "big.py")
    paged["tool_input"]["pages"] = "1-5"
    assert _run(paged, home) == ("", 0)


def test_the_reason_shows_a_writer_the_slice_route(project):
    root, home = project
    reason = _deny(_run(_read(root, "big.py"), home)[0])["permissionDecisionReason"]
    assert reason.endswith("A writer reads a section with sed -n or Read with limit.")
    assert "exempt.py on. A writer reads" in reason


def test_dark_armys_own_checkout_dials_the_threshold_so_a_writer_reads_the_contracts(tmp_path):
    # This checkout carries `.claude/settings.json` with the dial below in
    # its `env` (`.gitignore` un-ignores it; it may also carry a permissions
    # allowlist); the test rebuilds the same project on tmp_path and never
    # points the guard at the real checkout.
    repo = pathlib.Path(__file__).resolve().parents[2]
    expected = '{\n  "env": {\n    "BOB_SHUNT_MIN_LINES": "1200"\n  }\n}\n'
    checked_in = json.loads((repo / ".claude" / "settings.json").read_text())
    assert checked_in["env"] == json.loads(expected)["env"]
    root = tmp_path / "checkout"
    (root / ".claude" / "skills" / "shunt").mkdir(parents=True)
    (root / ".claude" / "skills" / "shunt" / "SKILL.md").write_text("# shunt\n")
    (root / ".claude" / "settings.json").write_text(expected)
    (root / "docs").mkdir()
    contract = repo / "docs" / "transport-contract.md"
    (root / "docs" / "transport-contract.md").write_bytes(contract.read_bytes())
    assert contract.read_text().count("\n") > 350, "the case needs a file over the default"
    home = tmp_path / "home"
    home.mkdir()
    writer = _read(root, "docs/transport-contract.md", agent_type="bc-implementer")
    assert _run(writer, home) == ("", 0)
    # The dial is a ceiling, not a disarm.
    (root / "huge.md").write_text("x\n" * 1201)
    out, _ = _run(_read(root, "huge.md", agent_type="bc-implementer"), home)
    assert "1200-line" in _deny(out)["permissionDecisionReason"]


def test_a_plus_prefixed_tail_count_reads_the_whole_file(project):
    root, home = project
    out, code = _run(_bash(root, "tail -n +2 big.py"), home)
    assert code == 0 and _deny(out)["permissionDecision"] == "deny"
    out, _ = _run(_bash(root, "tail --lines=+2 big.py"), home)
    assert _deny(out)["permissionDecision"] == "deny"
    # `tail -n -5` is `tail -n 5`: bounded.
    assert _run(_bash(root, "tail -n -5 big.py"), home) == ("", 0)


def test_a_minus_prefixed_head_count_reads_the_whole_file(project):
    root, home = project
    out, code = _run(_bash(root, "head -n -5 big.py"), home)
    assert code == 0 and _deny(out)["permissionDecision"] == "deny"
    out, _ = _run(_bash(root, "head --lines=-5 big.py"), home)
    assert _deny(out)["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", [
    "cat big.py > copy.txt",
    "cat big.py >> copy.txt",
    "cat big.py >copy.txt",
    "cat big.py 1> copy.txt",
    "cat big.py &> copy.txt",
])
def test_a_read_whose_stdout_goes_to_a_file_never_reaches_the_transcript(project, command):
    root, home = project
    assert _run(_bash(root, command), home) == ("", 0)
    # Redirecting stderr alone leaves stdout on the transcript.
    out, _ = _run(_bash(root, "cat big.py 2> err.txt"), home)
    assert _deny(out)["permissionDecision"] == "deny"


def test_a_heredoc_body_is_not_scanned_for_reads(project):
    root, home = project
    script = "cat > run.sh <<'EOF'\ncat big.py\nhead big.py\nEOF\nls"
    assert _run(_bash(root, script), home) == ("", 0)
    dashed = "cat <<-EOF\n\tcat big.py\n\tEOF\necho done"
    assert _run(_bash(root, dashed), home) == ("", 0)
    # A read after the terminator line is judged again.
    after = "cat <<EOF\ncat big.py\nEOF\ncat big.py"
    out, _ = _run(_bash(root, after), home)
    assert _deny(out)["permissionDecision"] == "deny"
    # A here-string is not a heredoc; the read beside it is still seen.
    out, _ = _run(_bash(root, "cat <<< hi; cat big.py"), home)
    assert _deny(out)["permissionDecision"] == "deny"


def test_a_byte_count_is_judged_by_the_lines_inside_those_bytes(project):
    root, home = project
    # big.py is 351 lines of 6 bytes: 1200 bytes hold 200 lines, allowed.
    assert _run(_bash(root, "head -c 1200 big.py"), home) == ("", 0)
    assert _run(_bash(root, "head --bytes=1200 big.py"), home) == ("", 0)
    # 2500 bytes hold the whole file: denied by its own line count.
    out, _ = _run(_bash(root, "head -c 2500 big.py"), home)
    reason = _deny(out)["permissionDecisionReason"]
    assert reason.startswith("351 lines:")
    # A cap that cuts a longer file says which bytes were counted.
    (root / "long.py").write_text("x = 1\n" * 5000)
    out, _ = _run(_bash(root, "head -c 3000 long.py"), home)
    reason = _deny(out)["permissionDecisionReason"]
    assert reason.startswith("more than 500 lines in the first 3000 bytes:")


def test_a_tail_byte_count_is_judged_by_the_lines_in_the_last_bytes(project):
    """`tail -c N` used to be judged by the whole file's count, with a
    reason naming that count; it shows the last N bytes and is judged by
    the lines inside them, exactly as `head -c N` is by the first N."""
    root, home = project
    (root / "long.py").write_text("x = 1\n" * 5000)   # 30,000 bytes
    # The last 2000 bytes hold 333 lines: allowed outright.
    assert _run(_bash(root, "tail -c 2000 long.py"), home) == ("", 0)
    assert _run(_bash(root, "tail --bytes=2000 long.py"), home) == ("", 0)
    # The last 3000 hold 500: denied, and the reason says which bytes.
    out, _ = _run(_bash(root, "tail -c 3000 long.py"), home)
    reason = _deny(out)["permissionDecisionReason"]
    assert reason.startswith("more than 500 lines in the last 3000 bytes:")
    out, _ = _run(_bash(root, "tail --bytes=3000 long.py"), home)
    assert _deny(out)["permissionDecisionReason"].startswith(
        "more than 500 lines in the last 3000 bytes:")
    # A bound wider than the file is the whole file, by its own count.
    out, _ = _run(_bash(root, "tail -c 2500 big.py"), home)
    assert _deny(out)["permissionDecisionReason"].startswith("351 lines:")
    # A seek that lands inside a multibyte character is not a binary file.
    (root / "accents.py").write_text("\u00e9\n" * 5000, encoding="utf-8")  # 3 bytes a line
    out, _ = _run(_bash(root, "tail -c 3002 accents.py"), home)
    assert _deny(out)["permissionDecisionReason"].startswith(
        "more than 1001 lines in the last 3002 bytes:")
    assert _run(_bash(root, "tail -c 1000 accents.py"), home) == ("", 0)


# --- instruction files ---------------------------------------------------------

@pytest.mark.parametrize("rel", [
    ".claude/skills/ship/references/implement.md",
    ".agents/skills/ship/references/implement.md",
    ".claude/agents/xy-implementer.md",
    ".claude/leads/gate.md",
    "plans/2026-09-23-a-long-plan.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "docs/context.md",
])
def test_an_instruction_file_is_read_whole_whatever_its_length(project, rel):
    """A skill, an agent brief, a plan or the root contract is the text an
    agent follows: a helper's summary of it drops the steps, so the guard
    never refuses one — by Read, by a shell read, on Claude, Codex or Grok."""
    root, home = project
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("- step\n" * 400)
    writer = {"agent_type": "xy-implementer"}
    assert _run(_read(root, rel, **writer), home) == ("", 0)
    assert _run(_read(root, str(target), **writer), home) == ("", 0)
    assert _run(_bash(root, "cat " + rel), home) == ("", 0)
    assert _run(_bash(root, ["bash", "-lc", "cat " + rel], tool="shell"), home) == ("", 0)
    assert _run(_bash(root, "cat " + rel, tool="bash"), home) == ("", 0)


@pytest.mark.parametrize("rel", [
    ".claude/skills/shunt/bulk_read.py",   # code under a skill folder
    "docs/transport-contract.md",          # a doc that is not the contract
    "src/plans/notes.md",                  # `plans` below the root
    "README.md",
    "notes/CLAUDE.md",                     # a contract name below the root
    ".claude/skillsbackup/notes.md",       # a prefix, not the folder
])
def test_a_long_file_outside_the_instruction_set_is_still_refused(project, rel):
    root, home = project
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x = 1\n" * 400)
    out, _ = _run(_read(root, rel), home)
    assert _deny(out)["permissionDecision"] == "deny"
    out, _ = _run(_bash(root, "cat " + rel), home)
    assert _deny(out)["permissionDecision"] == "deny"


def test_an_instruction_file_of_another_project_is_not_exempt(project, tmp_path):
    """The set is relative to the project the guard armed on: a long skill
    file in some other folder is an ordinary long file."""
    root, home = project
    other = tmp_path / "elsewhere" / ".claude" / "skills" / "x" / "SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("- step\n" * 400)
    out, _ = _run(_read(root, str(other)), home)
    assert _deny(out)["permissionDecision"] == "deny"


def test_the_installed_bytes_exempt_an_instruction_file_too(project):
    root, home = project
    skill = root / ".claude" / "skills" / "ship" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("- step\n" * 400)
    assert _run_process(_read(root, ".claude/skills/ship/SKILL.md"), home) == ("", 0)
    out, _ = _run_process(_read(root, "big.py"), home)
    assert _deny(out)["permissionDecision"] == "deny"
