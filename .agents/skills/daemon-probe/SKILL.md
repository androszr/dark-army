---
name: daemon-probe
description: Deterministic health check of the live Dark Army pipeline —
  the installed hook handler, the daemon's loopback socket and HTTP/SSE API, the
  Claude Code transcript and agents roster it parses, the usage cache, the VS
  Code extension and the panel. Reports what is stale, what is silently
  degraded, and what schema drifted. Use when the user says /daemon-probe, after
  any change to the hook path or the API, or when a surface is showing something
  that looks wrong.
---

# daemon-probe

Dark Army reads four things it does not own — the **Claude Code hook contract**, the
**transcript JSONL**, **`claude agents --json`**, and **`~/.claude.json`'s usage
cache** — and every one of them can change shape without notice. The failure mode
is never an exception; it is a count that keeps rendering and has stopped being
true. This skill is the smoke alarm.

No agent spawn. All bash. **Read-only** — this probe never sends a synthetic hook
event, never stops a session, and never deletes a job. A probe that writes into
the live fleet is indistinguishable from the bug it is looking for.

## Visual convention

This skill spawns nothing, so it gets a single opening banner instead of a
per-agent one. Before Phase 1, read `.claude/skills/daemon-probe/banners/intro.txt`
(Read tool or `cat`) and paste its **verbatim** content as a fenced code block
in your text response. The paste must live in the text response, because shell
output collapses in the terminal scroll. Never generate a banner from memory.
If the file cannot be read, show no banner at all rather than an invented one.

## Args

`/daemon-probe [session-id ...]` — extra session ids to inspect in depth beyond
whatever is currently live.

## Phase 1: is anything running

```bash
# The hook door is the private socket ~/.dark-army/hook.sock (0600, this
# account only); 19873 is its TCP bridge for pre-upgrade sessions, on its
# way out; 19874 is the HTTP + SSE API.
lsof -nP -U | grep hook.sock || echo "NO HOOK SOCKET"
ls -l ~/.dark-army/hook.sock   # expect srw------- owned by you
# The two network listeners.
# Do NOT filter by address: the API binds "localhost", which resolves to ::1 as
# well as 127.0.0.1, and a rogue wildcard bind would be invisible behind an
# -iTCP@127.0.0.1 filter — it would read as "NO LISTENER" instead of as the
# finding it is. Filter by port and read the addresses yourself.
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(19873|19874)\b' || echo "NO LISTENER"

# The menu-bar process (which hosts the daemon on a thread) and the panel.
pgrep -fl 'dark_army_menubar|Dark Army' ; pgrep -fl BobPanel

# Dark Army's own state directory.
ls -la ~/.dark-army/
```

Report, per line: listening / not. **No listener is not automatically a failure**
— the app may simply be closed. Say which, and stop here if it is: every later
phase measures a running daemon.

Also check the lock and pid files agree with reality — a stale `daemon.pid` or
`panel.pid` pointing at a reused pid is the failure mode `proc_pidpath` exists to
prevent.

## Phase 2: the installed hook path

The handler on disk is written from the `NOTIFY_SCRIPT` string in
`dark_army_menubar/hooks.py`. The two drift, and the installed copy is what
actually runs.

```bash
# 1. Does the installed handler match the string that would be installed today?
cd host && .venv/bin/python - <<'PY'
import pathlib
from dark_army_menubar.hooks import NOTIFY_SCRIPT
p = pathlib.Path.home() / ".dark-army" / "dark-army-notify"
if not p.exists():
    print("NOT INSTALLED")
elif p.read_text() != NOTIFY_SCRIPT:
    print("DRIFT: installed handler differs from hooks.NOTIFY_SCRIPT — reinstall")
else:
    print("in step")
PY

# 2. Is it executable, and stdlib-clean?
ls -l ~/.dark-army/dark-army-notify
grep -nE '^\s*(import|from)\s' ~/.dark-army/dark-army-notify

# 3. Does the system Python — the worst case it must survive — even parse it?
/usr/bin/python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read());print('parses under', sys.version.split()[0])" \
  ~/.dark-army/dark-army-notify

# 4. Is it wired into Claude Code's settings, and for which events?
#    Derive the expected set from HOOKS_CONFIG rather than hardcoding it here —
#    a second copy of that roster is a second thing to drift.
cd host && .venv/bin/python - <<'PY'
import json, pathlib
from dark_army_menubar.hooks import HOOKS_CONFIG
s = json.loads((pathlib.Path.home() / ".claude" / "settings.json").read_text())
wired, expected = set(s.get("hooks", {})), set(HOOKS_CONFIG)
print("expected:", len(expected), "wired:", len(wired))
print("MISSING :", sorted(expected - wired) or "none")
print("EXTRA   :", sorted(wired - expected) or "none")
print("env     :", s.get("env", {}))
PY
```

Assert and report:

- every import in the installed handler is stdlib (`json`, `os`, `socket`, `sys`,
  `time`, `subprocess`, `pathlib`, `re`, `uuid`, `hashlib`, `urllib`). **Any
  `dark_army_*` import is a total failure of the event path** — that
  directory has no package to import from.
- it parses under `/usr/bin/python3` (3.9). Walrus is fine; `match` is not.
- `MISSING` is empty. `HOOKS_CONFIG` declares **13** events today — the eleven
  obvious ones plus `PermissionRequest` and `PostToolUseFailure`, which are easy
  to forget and which the protocol converter genuinely handles. A hook installed
  by an older version of Dark Army, or one Claude Code has since renamed, shows up here
  and nowhere else.
- if the `terminal_title` preference is on, `CLAUDE_CODE_DISABLE_TERMINAL_TITLE`
  is in the `env` block; if it is off, the key is *absent*. Either half alone
  means the tab has no writer at all, or two.

**Hooks are read at session start.** A drift finding means the fix is a reinstall
*and* a restart of every running session — say so, or the user will debug a
change that is not loaded.

## Phase 3: the API

```bash
TOKEN=$(cat ~/.dark-army/api-token 2>/dev/null)

# Reads are ungated by design.
curl -sS --max-time 3 http://127.0.0.1:19874/api/state | head -c 4000
curl -sS --max-time 3 http://127.0.0.1:19874/api/usage
curl -sS --max-time 3 'http://127.0.0.1:19874/api/history?limit=3'

# SSE opens and stays open.
curl -sS --max-time 4 -N http://127.0.0.1:19874/api/events | head -c 500

# The write gate. `noop` is deliberately NOT a real action: an unknown action
# falls through to 400 with zero side effects, which is exactly what a probe
# wants. Never send a real verb — stopping a session from a health check is the
# bug this probe exists to find.
curl -sS -o /dev/null -w 'bearer      -> %{http_code}\n' --max-time 3 \
  -H "Authorization: Bearer $TOKEN" -X POST http://127.0.0.1:19874/api/action \
  -d '{"action":"noop"}'
curl -sS -o /dev/null -w 'x-bob-token -> %{http_code}\n' --max-time 3 \
  -H "X-Bob-Token: $TOKEN" -X POST http://127.0.0.1:19874/api/action \
  -d '{"action":"noop"}'
```

**Expected: `bearer -> 403`, `x-bob-token -> 400`.** The 400 is the *pass* —
it means the token was accepted and the daemon got as far as rejecting an
unknown action. Reading it as a failure is the obvious mistake here. A `403` on
the second line means every panel action is dead; a `400` on the first means the
gate is not gating.

Assert and report:

- `/api/state` is valid JSON and every bucket the panel reads is present, even
  when empty: the running / waiting / sleeping groups, `finished`, notifications,
  subagent rows, `trend`, `mesh`. **An absent key is the panel's worst input** —
  Swift's synthesized `Decodable` throws on a missing key even with a default.
- the `Authorization: Bearer` call is refused and the `X-Bob-Token` call is not
  refused *for the same token*. If Bearer is accepted, the gate is wrong; if
  X-Bob-Token is refused, every panel action is silently 403ing.
- the API binds **loopback only**. `lsof` in Phase 1 showing `*:19874` rather
  than `127.0.0.1:19874` is a finding on its own.
- `/api/events` delivers at least an initial frame within the timeout.

## Phase 4: the upstreams Dark Army does not own

This is the phase that catches silent schema drift. For each, report *shape ok* /
*drifted* / *absent*, and never report a parse failure as "no data".

```bash
# 0. The three command-line assistants Start resolves. A missing Claude Code
#    is a failure; Codex and Grok are optional and reported as absent.
cd host && .venv/bin/python - <<'PY'
import subprocess
from dark_army_daemon import dispatch
for tool, installed in dispatch.installed_tools().items():
    binary = dispatch.resolve_executable(tool) if installed else None
    if not binary:
        print(tool, "missing" if tool == "claude" else "optional, absent")
        continue
    result = subprocess.run([binary, "--version"], capture_output=True,
                            text=True, timeout=5)
    print(tool, "found", (result.stdout or result.stderr).strip())
PY

# 1. The background-agents roster.
claude agents --json 2>&1 | head -c 2000

# 2. The transcript for a live session — the file session_stats streams.
ls -lt ~/.claude/projects/*/*.jsonl 2>/dev/null | head -5
tail -2 "$(ls -t ~/.claude/projects/*/*.jsonl | head -1)" | cut -c1-600

# 3. The usage cache the rate-limit windows are built from.
python3 -c "
import json,pathlib
d=json.loads((pathlib.Path.home()/'.claude.json').read_text())
u=d.get('cachedUsageUtilization')
print('cachedUsageUtilization:', json.dumps(u)[:800] if u else 'ABSENT')
"

# 4. What Dark Army makes of all three, through its own parsers.
cd host && .venv/bin/python - <<'PY'
from dark_army_daemon import limits
print("limits.snapshot():", limits.snapshot())
PY
```

Assert and report:

- `claude agents --json` returns parseable JSON with the fields
  `agents_poll.py` normalises. A non-JSON reply (a login prompt, an update
  notice) must degrade to "unknown", never to an empty roster rendered as "no
  agents" — check which one actually happens.
- the newest transcript's last lines carry the keys `session_stats` rolls up:
  model, usage/token counts, tool calls. A renamed field here silently zeroes
  every number in the panel while every row still renders.
- `cachedUsageUtilization` exists and its window reset times are in the
  **future**. A window that already reset is the `stale` case: the figure is not
  old, it is *wrong*, and the surface must show a bare `–` rather than a number.
  Confirm `limits.snapshot()` marks it stale rather than reporting the number.

## Phase 5: the editor and the panel

**The extension's protocol is strict, and getting it wrong looks exactly like a
broken extension.** It is **POST only**, the op goes in a **JSON body**, the
`authToken` from the lock file must be sent as **`x-bob-companion-authorization`**,
and any `Origin` header is rejected outright (anti-DNS-rebind). A `GET
/?op=ping` answers `{"error":"bad token"}` against a perfectly healthy window.

```bash
# The extension's per-window loopback servers.
ls -la ~/.dark-army/ide/ 2>/dev/null

for f in ~/.dark-army/ide/*.lock; do
  port=$(basename "$f" .lock)
  tok=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['authToken'])" "$f")
  for op in ping frontmost; do
    printf '%s %s -> ' "$port" "$op"
    curl -sS --max-time 2 -X POST "http://127.0.0.1:$port/" \
      -H "x-bob-companion-authorization: $tok" \
      -H 'Content-Type: application/json' \
      -d "{\"op\":\"$op\"}"
    echo
  done
done

# What is INSTALLED, for contrast. `code` is often not on PATH; fall back to the
# bundled CLI rather than skipping the check.
CODE=$(command -v code || echo "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
"$CODE" --list-extensions --show-versions 2>/dev/null | grep -iE "dark-army|bob-companion-ide"
```

**The version that matters is the one in the `ping` reply, not the one on disk.**
Each reply carries its own `version`, and that is the code actually running in
that window. `ls ~/.vscode/extensions` is actively misleading — stale version
directories are never pruned (six were present when this was written) — and even
the `code --list-extensions` answer is only what *would* load on next launch: a
window opened before an upgrade keeps serving the old build until it is
reloaded. A `ping` reporting 0.1.3 while 0.1.5 is installed is not a
contradiction; it means that window has not been reloaded, and `frontmost` is
dead in it. Report per window.

Report each window's running version against the `frontmost` requirement
(0.1.4+), and say plainly whether the suppression rule can work right now.
**Every failure path in that rule returns the empty set** — meaning "nobody is
looking, go ahead and interrupt" — so a broken or stale extension is invisible
from the inside, and the only symptom is being interrupted about a session you
are already staring at.

Then the panel: exactly one `BobPanel`, its parent is the menu-bar process (not
`1`), and `~/.dark-army/panel.pid` points at it.

## Phase 6: report

One table, most broken first:

```
| Area | Status | Detail |
|---|---|---|
| Listeners | ok / down | 19873, 19874 |
| Hook handler | in step / drifted / not installed | + stdlib + 3.9 parse |
| Hook wiring | N events / missing: <list> | settings.json |
| API | ok / degraded | token gate, SSE, bucket coverage |
| Upstreams | ok / drifted / absent | agents json, transcript, usage cache |
| Editor | ok / stale version / absent | frontmost reachable? |
| Panel | one / none / orphaned | ppid, pid file |

VERDICT: HEALTHY | DEGRADED | BROKEN
SKIPPED: <any check not run, and why>
```

Be explicit about what was skipped. A report that silently omits the upstream
phase reads as "the parsers are fine" when nobody looked — which is exactly the
failure this skill exists to catch.
