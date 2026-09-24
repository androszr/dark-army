#!/usr/bin/env python3
"""bulk_read - send files plus a question to the cheap helper; print the answer.

    python3 .claude/skills/shunt/bulk_read.py --question '...' FILE [FILE ...]

Part of the shunt skill (SKILL.md beside this file). Stdlib only and
3.9-safe: it runs under whatever `python3` the agent's shell has, imports
nothing from Dark Army and needs Dark Army neither running nor installed.

The helper is the project's own assistant CLI on its cheap model
(`workers.json` beside this file, `BOB_SHUNT_WORKER_MODEL` for one call),
run headless with the same flags Dark Army's Prepare helper uses so it can
never become a session on somebody's screen. It is told to use no tools; the
files ride inside the prompt in `<file path="...">` tags; only its answer is
printed here. One line goes to the delegation ledger under
`~/.dark-army/shunt/<session id>.jsonl`: paths, counts, cost where the
assistant reports it - never the question, never the content.

A SIGTERM (the assistant's Bash timeout) or a SIGHUP (a closed terminal)
is turned into `SystemExit(128 + signal)` so `subprocess.run`'s own
unwind kills the helper child before the wrapper goes: nothing runs on to
its own 110 s with nobody waiting on it.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WORKERS_PATH = os.path.join(HERE, "workers.json")
SHIPPED_WORKERS = {"claude": "haiku", "codex": "gpt-6-luna", "grok": "grok-4.5"}
PROVIDERS = ("claude", "codex", "grok")
# Which assistant this shell belongs to, by the id its harness exports.
SESSION_ID_ENV = (
    ("claude", "CLAUDE_CODE_SESSION_ID"),
    ("grok", "GROK_SESSION_ID"),
    ("codex", "CODEX_THREAD_ID"),
    ("codex", "CODEX_SESSION_ID"),
)
# What the worker must never inherit: this session's own identity (or the
# hook stamps the helper as a row of this session), and py2app's leaked
# interpreter variables. Copied from Dark Army's subprocess_env, which this
# script cannot import.
STRIP_ENV = (
    "GROK_SESSION_ID", "GROK_AGENT",
    "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
    "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_CHILD_SESSION",
    "CODEX_THREAD_ID", "CODEX_SESSION_ID",
    "BOB_COMPANION_ORIGIN", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
    "PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONDONTWRITEBYTECODE",
    "PYTHONOPTIMIZE", "PYTHONUNBUFFERED", "RESOURCEPATH", "EXECUTABLEPATH",
    "ARGVZERO", "_PY2APP_LAUNCHED_",
)
STRIP_ENV_PREFIXES = ("CLAUDE_CODE_SESSION_", "CLAUDE_CODE_MESSAGING_",
                      "CLAUDE_CODE_BRIDGE_")
# A loopback port nothing listens on: Codex and Grok have no flag that turns
# the notify hook off, so the helper's hooks connect, are refused and drop
# the event, exactly as Dark Army's Prepare helper is pointed.
QUIET_HOOK_PORT = "1"
# And the private hook socket's silence: connecting to /dev/null fails at
# once, and a socket named in the environment is the only address a newer
# notify hook dials. Both are set, for a notify hook of either age.
QUIET_HOOK_SOCKET = "/dev/null"
# The prompt rides on argv, so it is bounded well under the platform's
# ARG_MAX. Over this the call is refused in words rather than failing later.
MAX_PROMPT_BYTES = 600_000
# The helper's wall-clock bound. A delegation is 10-30 s; 110 s is under
# Claude Code's 120 s default Bash timeout, so the wrapper reports the
# timeout in words and kills the helper itself, rather than being killed
# by the assistant with the helper still running.
TIMEOUT_SECONDS = 110
NO_TOOLS = ("Do not use any tools. Answer only from the files below, in "
            "plain text, as briefly as the question allows.")


def provider_from_env(env):
    """`BOB_SHUNT_PROVIDER`, else the first session id present, else claude."""
    chosen = str(env.get("BOB_SHUNT_PROVIDER") or "").strip().lower()
    if chosen in PROVIDERS:
        return chosen
    for name, key in SESSION_ID_ENV:
        if str(env.get(key) or "").strip():
            return name
    return "claude"


def session_id_from_env(env, provider):
    for name, key in SESSION_ID_ENV:
        if name == provider and str(env.get(key) or "").strip():
            return str(env.get(key)).strip()
    for _name, key in SESSION_ID_ENV:
        if str(env.get(key) or "").strip():
            return str(env.get(key)).strip()
    return ""


def worker_model(provider, env):
    override = str(env.get("BOB_SHUNT_WORKER_MODEL") or "").strip()
    if override:
        return override
    try:
        with open(WORKERS_PATH, "r", encoding="utf-8") as fh:
            table = json.load(fh)
    except (OSError, ValueError):
        table = {}
    if isinstance(table, dict):
        model = str(table.get(provider) or "").strip()
        if model:
            return model
    return SHIPPED_WORKERS[provider]


def worker_env(env):
    out = {}
    for key, value in env.items():
        if key in STRIP_ENV:
            continue
        if any(key.startswith(p) for p in STRIP_ENV_PREFIXES):
            continue
        out[key] = value
    out["DARK_ARMY_HOOK_SOCKET"] = QUIET_HOOK_SOCKET
    out["BOB_COMPANION_PORT"] = QUIET_HOOK_PORT
    out.pop("CLAWD_TANK_PORT", None)
    return out


def worker_argv(provider, executable, model, prompt):
    """The helper's command line. Flag for flag the shape Dark Army's
    Prepare helper runs (`card_prepare.argv`), minus the brief flags it has
    no use for here; a parity test in Dark Army's own suite pins the two.
    """
    if provider == "codex":
        return [executable, "exec",
                "--model", model,
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--sandbox", "read-only",
                "--color", "never",
                "--", prompt]
    if provider == "grok":
        return [executable,
                "--model", model,
                "--output-format", "plain",
                "--no-subagents",
                "--disable-web-search",
                "--no-plan",
                "-p", prompt]
    return [executable,
            "-p", prompt,
            "--model", model,
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--output-format", "json"]


def parse_answer(provider, stdout):
    """`(answer, cost_usd_or_None)`. Claude's `--output-format json` carries
    `result` and `total_cost_usd`; Codex and Grok print text and report no
    cost, which stays `None` - never a made-up zero."""
    text = stdout.decode("utf-8", "replace")
    if provider != "claude":
        return text.strip(), None
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip(), None
    if not isinstance(data, dict):
        return text.strip(), None
    answer = data.get("result")
    if not isinstance(answer, str):
        answer = text
    cost = data.get("total_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        cost = None
    return answer.strip(), (float(cost) if cost is not None else None)


def read_files(paths):
    """`(blocks, lines, error)`. Every file is read whole here, once, and
    counted; a missing or unreadable one refuses the whole call so the
    helper never answers from half the evidence."""
    blocks = []
    lines = 0
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            return [], 0, "cannot read %s: %s" % (path, exc.strerror or exc)
        lines += text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        blocks.append('<file path="%s">\n%s\n</file>' % (path, text))
    return blocks, lines, ""


def _state_home(env):
    """Dark Army's state folder: `~/.dark-army` where it exists, else the
    `~/.bob-companion` of a Mac still on the old install, else `~/.dark-army`.
    Never creates either: a wrapper run before the app has moved the folder
    must not make the new one early."""
    home = env.get("HOME") or os.path.expanduser("~")
    new = os.path.join(home, ".dark-army")
    if os.path.isdir(new):
        return new
    old = os.path.join(home, ".bob-companion")
    if os.path.isdir(old):
        return old
    return new


def ledger_dir(env):
    return os.path.join(_state_home(env), "shunt")


def append_ledger(env, record):
    """One JSON line, 0600, in a 0700 directory this script creates when it
    is first. Never raises: a ledger that cannot be written is a missing
    figure, not a failed delegation."""
    try:
        folder = ledger_dir(env)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        sid = "".join(c if (c.isalnum() or c in "-_.") else "_"
                      for c in str(record.get("session_id") or ""))
        path = os.path.join(folder, "%s.jsonl" % (sid or "unattributed"))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, ValueError):
        pass


def ledger_record(provider, model, mode, files, lines, cost, seconds, ok, sid):
    return {
        "delegation_id": str(uuid.uuid4()),
        "at": time.time(),
        "session_id": sid,
        "provider": provider,
        "mode": mode,
        "model": model,
        "files": [str(p) for p in files],
        "lines_kept_out": int(lines),
        "worker_cost_usd": cost,
        "seconds": round(float(seconds), 3),
        "ok": bool(ok),
    }


def _on_signal(signum, frame):
    """SIGTERM is the assistant's Bash timeout, SIGHUP a closed terminal;
    raised as SystemExit either unwinds through `subprocess.run`, which
    kills the helper child before re-raising. 128 + the signal number is
    the shell's own code (143, 129)."""
    raise SystemExit(128 + signum)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="send files plus a question to the cheap helper")
    parser.add_argument("--question", required=True)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    provider = provider_from_env(env)
    sid = session_id_from_env(env, provider)
    model = worker_model(provider, env)
    question = " ".join(args.question.split())
    if not question:
        print("bulk_read: --question is empty", file=sys.stderr)
        return 2
    blocks, lines, error = read_files(args.files)
    if error:
        print("bulk_read: " + error, file=sys.stderr)
        return 2
    prompt = NO_TOOLS + "\n\nQUESTION: " + question + "\n\n" + "\n\n".join(blocks)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        print("bulk_read: %d files are more than %d bytes in one delegation; "
              "split them across calls" % (len(args.files), MAX_PROMPT_BYTES),
              file=sys.stderr)
        return 2
    executable = shutil.which(provider, path=env.get("PATH"))
    if not executable:
        print("bulk_read: no %s executable on PATH" % provider, file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGHUP, _on_signal)
    started = time.monotonic()
    ok = False
    cost = None
    try:
        proc = subprocess.run(
            worker_argv(provider, executable, model, prompt),
            env=worker_env(env), stdin=subprocess.DEVNULL,
            capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        print("bulk_read: the %s helper gave no answer in %ds" % (
            provider, TIMEOUT_SECONDS), file=sys.stderr)
        proc = None
    except OSError as exc:
        print("bulk_read: could not start %s: %s" % (provider, exc),
              file=sys.stderr)
        proc = None
    seconds = time.monotonic() - started
    answer = ""
    if proc is not None:
        answer, cost = parse_answer(provider, proc.stdout)
        ok = proc.returncode == 0 and bool(answer)
        if not ok:
            err = proc.stderr.decode("utf-8", "replace").strip()
            print("bulk_read: the %s helper failed (exit %s)%s" % (
                provider, proc.returncode, (": " + err[-2000:]) if err else ""),
                file=sys.stderr)
    # A failed helper kept nothing out of the main model: the caller reads
    # the files another way, so the line is a delegation with 0 lines.
    append_ledger(env, ledger_record(
        provider, model, "bulk-read", args.files, lines if ok else 0, cost,
        seconds, ok, sid))
    if not ok:
        return 1
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
