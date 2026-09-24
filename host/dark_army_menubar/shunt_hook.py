#!/usr/bin/env python3
"""dark-army-shunt - the shunt guard for Dark Army.

Reads a PreToolUse hook payload (Claude Code, Codex or Grok) from stdin.
When the tool is a whole-file read (`Read` / `read_file`, or a shell
`cat` / `head` / `tail` / `less` / `more`) of a file longer than the
project's shunt threshold, it prints a deny that names the shunt skill;
otherwise it prints nothing. Armed only inside a project that carries
`.claude/skills/shunt/SKILL.md`; never for a reviewer; never for a
session with an exemption marker; never for an instruction file (a skill,
an agent brief, a plan, the root contract), which is read whole because a
summary of a procedure drops the steps. No external dependencies.
"""
# SHUNT_SCRIPT_VERSION: 2026-09-23-dark-army

import json
import os
import re
import shlex
import stat
import sys

DEFAULT_MIN_LINES = 350
SKILL_MARKER = os.path.join(".claude", "skills", "shunt", "SKILL.md")
SETTINGS_REL = os.path.join(".claude", "settings.json")
# Under the state folder. On a machine upgraded from the old install the old
# `~/.bob-companion` is a link to this same folder, so a marker an older
# wrapper wrote through the old name is found here too.
EXEMPT_DIR_NAME = os.path.join(".dark-army", "shunt-exempt")
# The read-only roles, by the tail of their `agent_type` (`bc-verifier`,
# `xy-bug-auditor`, ...): whichever prefix a project's pack renders.
# Instruction files, relative to the project root: the text an agent
# follows, never surveys. A cheap helper's summary of a runbook loses the
# very steps the run needs, so these are read whole whatever their length.
# Markdown only: a script under a skill folder is code like any other.
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md",
                     os.path.join("docs", "context.md"))
INSTRUCTION_DIRS = (os.path.join(".claude", "skills"),
                    os.path.join(".agents", "skills"),
                    os.path.join(".claude", "agents"),
                    os.path.join(".claude", "leads"),
                    "plans")
INSTRUCTION_SUFFIX = ".md"
REVIEWER_SUFFIXES = ("-verifier", "-bug-auditor",
                     "-integration-reviewer", "-security-reviewer")
READ_TOOLS = ("Read", "read_file")
SHELL_TOOLS = ("Bash", "bash", "shell")
# The shell commands that put a whole file into the transcript. `grep`,
# `rg`, `sed -n`, `awk`, `wc` and anything piped through another command
# are not on it, and `head` / `tail` with a count at or under the
# threshold are allowed below.
WHOLE_FILE_COMMANDS = ("cat", "head", "tail", "less", "more")
SESSION_ID_KEYS = ("session_id", "sessionId", "parentSessionId",
                   "parent_session_id")
SESSION_ENV_KEYS = ("CLAUDE_CODE_SESSION_ID", "GROK_SESSION_ID",
                    "CODEX_THREAD_ID", "CODEX_SESSION_ID")
MAX_WALK_DEPTH = 40
# Bounded reads: the settings file and the counted file. A file past the
# scan cap is reported as "more than" the lines counted so far.
MAX_SETTINGS_BYTES = 1 << 20
MAX_SCAN_BYTES = 4 << 20
CHUNK = 1 << 16
REASON = ("{lines} lines{scope}: over Dark Army's {threshold}-line shunt "
          "threshold. Use the shunt skill - python3 "
          ".claude/skills/shunt/bulk_read.py --question '...' {path} - or, "
          "if you are a read-only reviewer, "
          "python3 .claude/skills/shunt/exempt.py on. A writer reads a "
          "section with sed -n or Read with limit.")


def _first(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _home():
    try:
        return os.path.realpath(os.path.expanduser("~"))
    except OSError:
        return ""


def _project_root(cwd):
    """The nearest ancestor of `cwd` (home skipped) carrying the shunt
    skill, or "". The same walk as the notify script's `_project_key`,
    for the same reason: a session under $HOME must never arm on a
    skill somebody mirrored into `~/.claude/skills/`."""
    try:
        start = os.path.realpath(cwd or os.getcwd())
    except OSError:
        return ""
    home = _home()
    here = start
    for _ in range(MAX_WALK_DEPTH):
        if not here:
            break
        if here != home and os.path.isfile(os.path.join(here, SKILL_MARKER)):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return ""


def _positive_int(value):
    try:
        if isinstance(value, bool):
            return 0
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _threshold(root):
    """`BOB_SHUNT_MIN_LINES` in the environment, else `env.BOB_SHUNT_MIN_LINES`
    in the project's `.claude/settings.json` (read here so Codex and Grok
    honour it too), else the default."""
    from_env = _positive_int(os.environ.get("BOB_SHUNT_MIN_LINES"))
    if from_env:
        return from_env
    try:
        with open(os.path.join(root, SETTINGS_REL), "r", encoding="utf-8") as fh:
            settings = json.loads(fh.read(MAX_SETTINGS_BYTES))
    except (OSError, ValueError):
        settings = None
    if isinstance(settings, dict):
        env = settings.get("env")
        if isinstance(env, dict):
            from_project = _positive_int(env.get("BOB_SHUNT_MIN_LINES"))
            if from_project:
                return from_project
    return DEFAULT_MIN_LINES


def _session_ids(payload):
    ids = []
    for key in SESSION_ID_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            ids.append(value.strip())
    for key in SESSION_ENV_KEYS:
        value = os.environ.get(key, "")
        if value.strip():
            ids.append(value.strip())
    return ids


def _exempt(payload):
    agent = _first(payload, "agent_type", "agentType", "subagent_type",
                   "subagentType")
    if isinstance(agent, str):
        name = agent.strip().lower()
        if any(name.endswith(suffix) for suffix in REVIEWER_SUFFIXES):
            return True
    folder = os.path.join(os.path.expanduser("~"), EXEMPT_DIR_NAME)
    for sid in _session_ids(payload):
        if "/" in sid or sid in (".", ".."):
            continue
        if os.path.isfile(os.path.join(folder, sid)):
            return True
    return False


def _looks_binary(chunk):
    """A NUL byte or a UTF-8 failure inside the first chunk (a cut
    multibyte character at its very end is not one) means a PNG, a PDF,
    a database: its newline bytes are not lines and it is never over."""
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError as err:
        return err.start < len(chunk) - 3
    return False


def _count_lines(path, byte_cap=None, from_end=False):
    """`(lines, complete)` for a regular text file, `None` for anything
    else. Opened O_NONBLOCK on a regular-fd test so a FIFO or a device
    can never hang the hook; read in chunks up to the scan cap, or up to
    `byte_cap` when the read itself is bounded in bytes (`head -c`),
    counted from `size - byte_cap` when it is `tail -c`'s bound
    (`from_end`), so only the bytes that read would show are counted."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        cap = MAX_SCAN_BYTES if byte_cap is None else min(byte_cap, MAX_SCAN_BYTES)
        mid_file = False
        if from_end and byte_cap is not None and info.st_size > byte_cap:
            os.lseek(fd, info.st_size - byte_cap, os.SEEK_SET)
            mid_file = True
        lines = 0
        seen = 0
        last = b""
        first = True
        while seen < cap:
            chunk = os.read(fd, min(CHUNK, cap - seen))
            if not chunk:
                break
            if first:
                first = False
                probe = chunk
                if mid_file:
                    # A seek can land inside a multibyte character; its
                    # continuation bytes are not a sign of a binary.
                    skip = 0
                    while skip < 3 and skip < len(probe) and 0x80 <= probe[skip] < 0xC0:
                        skip += 1
                    probe = probe[skip:]
                if _looks_binary(probe):
                    return None
            seen += len(chunk)
            lines += chunk.count(b"\n")
            last = chunk
        if seen >= cap:
            return lines, False
        if last and not last.endswith(b"\n"):
            lines += 1
        return lines, True
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _resolve(raw, cwd):
    text = str(raw or "").strip()
    if not text:
        return ""
    text = os.path.expanduser(text)
    if not os.path.isabs(text):
        text = os.path.join(cwd or os.getcwd(), text)
    return os.path.realpath(text)


def _instruction_file(path, root):
    """Whether the resolved `path` is one of the project's instruction
    files: a root contract named in `INSTRUCTION_FILES`, or a markdown file
    anywhere under an `INSTRUCTION_DIRS` folder."""
    if not path or not root or not path.endswith(INSTRUCTION_SUFFIX):
        return False
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return False
    if rel.startswith(os.pardir + os.sep) or rel == os.pardir:
        return False
    if rel in INSTRUCTION_FILES:
        return True
    return any(rel.startswith(folder + os.sep) for folder in INSTRUCTION_DIRS)


def _over(path, threshold, byte_cap=None, from_end=False):
    """`(lines_text, over)`: the count as words for the reason, and
    whether it is over the threshold. A path that is not a readable
    regular text file is never over. With `byte_cap` only the bytes
    that read would show are counted (the first N, or the last N with
    `from_end`), so bytes are never compared to lines."""
    counted = _count_lines(path, byte_cap, from_end)
    if counted is None:
        return "", False
    lines, complete = counted
    if lines <= threshold:
        return str(lines), False
    return (str(lines) if complete else "more than " + str(lines)), True


def _bounded_read(tool_input, threshold):
    """A `Read` that asks for a slice at or under the threshold is an
    edit reading its section, not a whole-file read."""
    limit = _positive_int(tool_input.get("limit"))
    if limit and limit <= threshold:
        return True
    start = _positive_int(tool_input.get("start_line"))
    end = _positive_int(tool_input.get("end_line"))
    if start and end and end >= start and end - start + 1 <= threshold:
        return True
    return False


def _check_read(tool_input, cwd, threshold, root=""):
    """The offending `(path, lines)` for a Read, or None."""
    raw = _first(tool_input, "file_path", "path", "filePath")
    if not isinstance(raw, str):
        return None
    if tool_input.get("pages"):
        return None     # a PDF read, paged by the harness itself
    if _bounded_read(tool_input, threshold):
        return None
    path = _resolve(raw, cwd)
    if not path or _instruction_file(path, root):
        return None
    lines, over = _over(path, threshold)
    return (raw, lines, "") if over else None


def _shell_text(command):
    """The command as one string. Codex's `shell` carries an argv list,
    usually `["bash", "-lc", "<command>"]`; the last element is the
    command a person would have typed."""
    if isinstance(command, list):
        parts = [str(p) for p in command]
        shell = os.path.basename(parts[0]) in ("bash", "sh", "zsh")
        if len(parts) >= 3 and shell and parts[1] in ("-lc", "-c", "-lic", "-ic"):
            return parts[-1]
        return " ".join(parts)
    return str(command or "")


_SEGMENTS = re.compile(r"\|\||&&|;|\n")
_COUNT_FLAG = re.compile(r"^-(n|c)?(\d+)$")
# `<<EOF`, `<< EOF`, `<<-EOF`, `<<'EOF'`; never the here-string `<<<`.
_HEREDOC = re.compile(r"""(?<!<)<<(?!<)-?\s*(['"]?)([A-Za-z_][\w.-]*)\1""")
# A token sending the segment's stdout to a file: `>`, `>>`, `1>`, `&>`,
# attached (`>copy.txt`) or not. `2>` is stderr and `>&2` is no file.
_STDOUT_TO_FILE = re.compile(r"^(&?>>?|1>>?)(?!&)")


def _count_value(text, command):
    """`(kind, n)` for one count word, `None` when it is unbounded: a
    `+`-prefixed tail count (`tail -n +2`, everything from line 2) and
    a `-`-prefixed head count (`head -n -5`, everything but the last 5)
    each read nearly the whole file."""
    text = str(text)
    if text.startswith("+"):
        if command == "tail":
            return None
        text = text[1:]
    elif text.startswith("-"):
        if command == "head":
            return None
        text = text[1:]
    n = _positive_int(text)
    return n if n else None


def _count_arg(command, tokens):
    """`head` / `tail`'s own bound, when it has one: `("lines", N)` for
    `-n N`, `-nN`, `-N`, `--lines=N`; `("bytes", N)` for `-c N`,
    `--bytes=N`. None when unbounded."""
    for i, tok in enumerate(tokens):
        if tok in ("-n", "--lines", "-c", "--bytes"):
            if i + 1 >= len(tokens):
                return None
            n = _count_value(tokens[i + 1], command)
            if n is None:
                return None
            return ("bytes" if tok in ("-c", "--bytes") else "lines"), n
        if tok.startswith("--lines=") or tok.startswith("--bytes="):
            n = _count_value(tok.split("=", 1)[1], command)
            if n is None:
                return None
            return ("bytes" if tok.startswith("--bytes=") else "lines"), n
        m = _COUNT_FLAG.match(tok)
        if m:
            n = _positive_int(m.group(2)) or 1
            return ("bytes" if m.group(1) == "c" else "lines"), n
    return None


def _strip_heredocs(text):
    """The command text with every heredoc body removed: from the line
    after a `<<WORD` token to the line that is only `WORD`. A `cat
    <<'EOF' > script.sh` whose body says `cat big.py` writes a file, it
    reads none."""
    out = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        m = _HEREDOC.search(line)
        if not m:
            continue
        word = m.group(2)
        dash = m.group(0).startswith("<<-")
        while i < len(lines):
            body = lines[i]
            i += 1
            if (body.lstrip("\t") if dash else body) == word:
                break
    return "\n".join(out)


def _check_shell(tool_input, cwd, threshold, root=""):
    """The offending `(path, lines)` for a shell command, or None."""
    text = _shell_text(_first(tool_input, "command", "cmd"))
    if not text.strip():
        return None
    workdir = _first(tool_input, "workdir", "cwd", "working_directory")
    if isinstance(workdir, str) and workdir.strip():
        cwd = workdir.strip()
    for segment in _SEGMENTS.split(_strip_heredocs(text)):
        if "|" in segment:
            continue      # piped through something else: bounded by it
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.split()
        while tokens and ("=" in tokens[0] and not tokens[0].startswith("-")):
            tokens = tokens[1:]     # FOO=bar cat big.py
        if not tokens:
            continue
        command = os.path.basename(tokens[0])
        if command == "cd" and len(tokens) > 1:
            cwd = _resolve(tokens[1], cwd) or cwd   # cd sub && cat ../big.py
            continue
        if command not in WHOLE_FILE_COMMANDS:
            continue
        args = tokens[1:]
        if any(_STDOUT_TO_FILE.match(tok) for tok in args):
            continue      # cat big.py > copy.txt: a file, not the transcript
        byte_cap = None
        from_end = False
        if command in ("head", "tail"):
            bound = _count_arg(command, args)
            if bound is not None:
                kind, n = bound
                if n <= threshold:
                    continue      # N lines, or N bytes holding at most N lines
                if kind == "bytes":
                    byte_cap = n
                    from_end = command == "tail"
        candidates = []
        skip = False
        read_next = False
        for tok in args:
            if skip:
                skip = False
                continue
            if tok.startswith("<<"):
                break     # a heredoc: the rest of the line is its word
            if tok in ("2>", "2>>"):
                skip = True
                continue
            if tok == "<":
                read_next = True
                continue
            if tok.startswith("-") and not read_next:
                continue
            read_next = False
            candidates.append(tok)
        for raw in candidates:
            path = _resolve(raw, cwd)
            if not path or _instruction_file(path, root):
                continue
            lines, over = _over(path, threshold, byte_cap, from_end)
            if over:
                scope = ""
                if byte_cap is not None and lines.startswith("more than"):
                    scope = " in the %s %d bytes" % (
                        "last" if from_end else "first", byte_cap)
                return raw, lines, scope
    return None


def _deny(path, lines, scope, threshold):
    reason = REASON.format(lines=lines, scope=scope, threshold=threshold,
                           path=path)
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": "deny",
                                  "permissionDecisionReason": reason}}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def decide(payload):
    """`(path, lines, scope, threshold)` to refuse, or None to say
    nothing."""
    if not isinstance(payload, dict):
        return None
    event = _first(payload, "hook_event_name", "hookEventName")
    if event not in (None, "PreToolUse", "pre_tool_use"):
        return None
    tool = _first(payload, "tool_name", "toolName")
    if tool not in READ_TOOLS and tool not in SHELL_TOOLS:
        return None
    tool_input = _first(payload, "tool_input", "toolInput")
    if not isinstance(tool_input, dict):
        return None
    cwd = _first(payload, "cwd", "workingDirectory")
    cwd = cwd if isinstance(cwd, str) else ""
    root = _project_root(cwd)
    if not root:
        return None
    if _exempt(payload):
        return None
    threshold = _threshold(root)
    if tool in READ_TOOLS:
        hit = _check_read(tool_input, cwd, threshold, root)
    else:
        hit = _check_shell(tool_input, cwd, threshold, root)
    if hit is None:
        return None
    return hit[0], hit[1], hit[2], threshold


def main():
    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return
    try:
        verdict = decide(payload)
        if verdict is not None:
            _deny(*verdict)
    except Exception:
        # A closed stdout on the deny write is an allow like every
        # other failure: exit 0, no traceback on stderr. The bytes the
        # failed write left in the interpreter's buffer would make its
        # exit flush fail again (exit 120), so fd 1 is pointed at
        # /dev/null before returning.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        except OSError:
            pass
        return


if __name__ == "__main__":
    main()
