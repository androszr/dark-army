#!/usr/bin/env python3
"""code_write - have the cheap helper write a new boilerplate file to disk.

    python3 .claude/skills/shunt/code_write.py --spec '...' --reference REF --out TARGET

Part of the shunt skill (SKILL.md beside this file). Stdlib only and
3.9-safe; imports nothing from Dark Army and needs it neither running nor
installed.

Two refusals are the rule, not a courtesy: `--reference` is required (a
write with nothing to copy the style from is a guess), and TARGET must not
exist (an edit is never delegated) - claimed empty with O_EXCL before the
helper starts, so a file that appears while it thinks is never overwritten.
The one exception is an empty regular file: that is the placeholder an
earlier attempt left behind when it was killed, adopted with a line on
stderr saying so. The helper runs headless and read-only on the cheap model
and prints the whole file; this wrapper writes it through a temporary
sibling and `os.replace` over its own placeholder, prints only
`wrote TARGET (N lines)`, and appends one ledger line with
`lines_kept_out` = N. The content never enters the main model's context.

A SIGTERM (the assistant's Bash timeout) or a SIGHUP (a closed terminal)
is turned into `SystemExit(128 + signal)` so the `finally` runs: the helper
child is killed and the placeholder this command created is removed;
nothing is orphaned and nothing is left claimed. An adopted placeholder is
never removed: it was on disk before this command started, so every
failure path leaves it exactly as found. The timeout is under Claude
Code's 120 s default Bash timeout for the same reason.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WORKERS_PATH = os.path.join(HERE, "workers.json")
SHIPPED_WORKERS = {"claude": "haiku", "codex": "gpt-6-luna", "grok": "grok-4.5"}
PROVIDERS = ("claude", "codex", "grok")
SESSION_ID_ENV = (
    ("claude", "CLAUDE_CODE_SESSION_ID"),
    ("grok", "GROK_SESSION_ID"),
    ("codex", "CODEX_THREAD_ID"),
    ("codex", "CODEX_SESSION_ID"),
)
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
QUIET_HOOK_PORT = "1"
# And the private hook socket's silence: connecting to /dev/null fails at
# once, and a socket named in the environment is the only address a newer
# notify hook dials. Both are set, for a notify hook of either age.
QUIET_HOOK_SOCKET = "/dev/null"
MAX_PROMPT_BYTES = 600_000
TIMEOUT_SECONDS = 110
INSTRUCTION = (
    "Do not use any tools. Write one complete new file that follows the "
    "SPEC below, copying the shape, imports, naming and style of the "
    "REFERENCE file exactly. Print only the file's contents - no prose "
    "before or after, no code fence.")


def provider_from_env(env):
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
    """Same shape as bulk_read.py's, which is Dark Army's Prepare helper's
    (`card_prepare.argv`) minus the brief flags. Read-only on every
    provider: the worker prints, this wrapper writes."""
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
    text = stdout.decode("utf-8", "replace")
    if provider != "claude":
        return text, None
    try:
        data = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(data, dict):
        return text, None
    answer = data.get("result")
    if not isinstance(answer, str):
        answer = text
    cost = data.get("total_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        cost = None
    return answer, (float(cost) if cost is not None else None)


def strip_fence(text):
    """A helper that fenced the file anyway loses the fence, not the file."""
    lines = text.strip("\n").split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
    body = "\n".join(lines)
    if body and not body.endswith("\n"):
        body += "\n"
    return body


def is_empty_placeholder(target):
    """An empty regular file (never a symlink) is the placeholder a killed
    earlier attempt left; a file with any content is somebody's work."""
    try:
        return (not os.path.islink(target) and os.path.isfile(target)
                and os.path.getsize(target) == 0)
    except OSError:
        return False


def claim_target(target):
    """Create `target` empty with O_EXCL before the helper runs, so a file
    that appears while the helper thinks (10-110 s) is never overwritten:
    the placeholder is ours from this moment and `write_atomic` replaces
    it. An existing *empty* regular file is adopted (an earlier attempt's
    placeholder, said on stderr); anything else is refused. Returns
    `(refusal, created)`: `refusal` is None when the target is claimed,
    else the line to print; `created` is True only when this call made the
    file, so `release_target` knows whether the empty file is ours to
    remove or somebody's to leave."""
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if is_empty_placeholder(target):
            print("code_write: adopting an empty placeholder left by an "
                  "earlier attempt: %s" % target, file=sys.stderr)
            return None, False
        return ("code_write: %s already exists; an edit is never delegated - "
                "read the section with sed -n and edit it yourself" % target), False
    except OSError as exc:
        return "code_write: cannot create %s: %s" % (target, exc.strerror or exc), False
    os.close(fd)
    return None, True


def release_target(target):
    """Remove the placeholder `claim_target` made; only ever an empty file
    of ours, on a path that did not exist when this command started. The
    caller runs this only when `claim_target` said it created the file:
    an adopted empty file (a `pkg/__init__.py`, say) is left as found."""
    try:
        if os.path.isfile(target) and os.path.getsize(target) == 0:
            os.unlink(target)
    except OSError:
        pass


def write_atomic(target, text):
    folder = os.path.dirname(os.path.abspath(target)) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=os.path.basename(target) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        # `claim_target` in main() is what "an edit is never delegated"
        # means on disk: the target is our empty placeholder; the rename is
        # atomic, so it is whole or empty, never half-written.
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _on_signal(signum, frame):
    """SIGTERM is the assistant's Bash timeout, SIGHUP a closed terminal;
    raised as SystemExit either unwinds through every `finally`, so the
    helper child is killed and a placeholder of ours released. 128 + the
    signal number is the shell's own code (143, 129)."""
    raise SystemExit(128 + signum)


def run_helper(argv, env):
    """Run the helper and wait for it. On a timeout, a SIGTERM (SystemExit
    from the handler) or Ctrl-C the child is killed and reaped before the
    exception continues, so no helper is ever orphaned."""
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise
    return proc.returncode, stdout, stderr


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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="have the cheap helper write a new boilerplate file")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--reference", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    spec = " ".join(args.spec.split())
    if not spec:
        print("code_write: --spec is empty", file=sys.stderr)
        return 2
    if not args.reference:
        print("code_write: --reference is required; name a file whose shape "
              "and style the new one must copy", file=sys.stderr)
        return 2
    try:
        with open(args.reference, "r", encoding="utf-8", errors="replace") as fh:
            reference = fh.read()
    except OSError as exc:
        print("code_write: cannot read reference %s: %s" % (
            args.reference, exc.strerror or exc), file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGHUP, _on_signal)
    refusal, created = claim_target(args.out)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    try:
        return _run_helper(args, spec, reference)
    finally:
        # Any path that did not write the file leaves no empty placeholder
        # of ours; an adopted one was there before us and stays.
        if created:
            release_target(args.out)


def _run_helper(args, spec, reference):
    """The helper round trip, with `args.out` already claimed."""
    env = dict(os.environ)
    provider = provider_from_env(env)
    sid = session_id_from_env(env, provider)
    model = worker_model(provider, env)
    prompt = (INSTRUCTION + "\n\nSPEC: " + spec + "\n\nTARGET PATH: " + args.out
              + '\n\n<reference path="%s">\n%s\n</reference>' % (args.reference, reference))
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        print("code_write: the reference is more than %d bytes; choose a "
              "smaller exemplar" % MAX_PROMPT_BYTES, file=sys.stderr)
        return 2
    executable = shutil.which(provider, path=env.get("PATH"))
    if not executable:
        print("code_write: no %s executable on PATH" % provider, file=sys.stderr)
        return 2
    started = time.monotonic()
    ok = False
    cost = None
    lines = 0
    proc = None
    try:
        proc = run_helper(worker_argv(provider, executable, model, prompt),
                          worker_env(env))
    except subprocess.TimeoutExpired:
        print("code_write: the %s helper gave no answer in %ds" % (
            provider, TIMEOUT_SECONDS), file=sys.stderr)
    except OSError as exc:
        print("code_write: could not start %s: %s" % (provider, exc),
              file=sys.stderr)
    seconds = time.monotonic() - started
    if proc is not None:
        returncode, stdout, stderr = proc
        answer, cost = parse_answer(provider, stdout)
        body = strip_fence(answer)
        if returncode == 0 and body.strip():
            try:
                write_atomic(args.out, body)
                lines = body.count("\n")
                ok = True
            except OSError as exc:
                print("code_write: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
        else:
            err = stderr.decode("utf-8", "replace").strip()
            print("code_write: the %s helper wrote nothing (exit %s)%s" % (
                provider, returncode, (": " + err[-2000:]) if err else ""),
                file=sys.stderr)
    append_ledger(env, ledger_record(
        provider, model, "code-write", [args.out], lines, cost, seconds, ok, sid))
    if not ok:
        return 1
    print("wrote %s (%d lines)" % (args.out, lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
