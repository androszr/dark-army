---
name: integration-pass
description: Standalone audit of everything Dark Army's hermetic test suite
  cannot see — the frozen py2app bundle, the installed hook handler, the loopback
  API and its token, destructive-verb guards, persisted-state compatibility and
  the panel launch contract. Runs deterministic checks, then spawns
  bc-integration-reviewer for the judgment calls. Use when the user says
  /integration-pass, "check the packaging", "will this survive the build", or
  before cutting a release.
---

# integration-pass

Two halves: cheap deterministic checks first, then the reviewer agent for what
greps cannot decide. Run the greps before spawning — a module missing from
`setup.py` does not need an LLM to confirm it.

The premise: a thousand-plus tests pass from source, and not one of them builds a
bundle, installs a hook, binds a socket, or reads a file written by last month's
version. This is the pass for the gap between "the tests are green" and "the copy
in `/Applications` works".

## Args

`/integration-pass [--build]` — with `--build`, actually produce the bundle
(`cd host && ./build.sh`, ~1–2 min) and inspect it. Without it, the bundle
section is static analysis only and is reported as **partially checked**, not as
passing.

## Agent spawn visual convention

Before spawning `bc-integration-reviewer` in Phase 2, read
`.claude/skills/integration-pass/banners/integration.txt` (Read tool or `cat`)
and paste its **verbatim** content as a fenced code block in the text response.
The paste must live in the text response, because shell output
collapses in the terminal scroll. Never generate a banner from memory.
If the file cannot be read, show no banner at all rather than an invented
one. Re-dispatches re-show the banner.

| Agent | Character | Phase | Banner file |
|---|---|---|---|
| `bc-integration-reviewer` | Watch | 2 | `integration.txt` |

## Phase 1: deterministic checks

Run all of these; collect results before spawning anything. No banner here —
Phase 1 spawns nothing.

### 1. Imports the frozen bundle would not have

Every top-level import across the two packages, minus stdlib, minus our own
modules, checked against `setup.py`'s `packages` / `includes`:

```bash
cd host && .venv/bin/python - <<'PY'
import ast, pathlib, sys
own = {"dark_army_daemon", "dark_army_menubar"}
std = set(sys.stdlib_module_names)
found = {}
for p in pathlib.Path(".").glob("dark_army_*/**/*.py"):
    for node in ast.walk(ast.parse(p.read_text())):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        else:
            continue
        for n in names:
            if n not in std and n not in own:
                found.setdefault(n, set()).add(str(p))
declared = pathlib.Path("setup.py").read_text()
for name, where in sorted(found.items()):
    mark = "ok" if f'"{name}"' in declared or f"'{name}'" in declared else "NOT IN setup.py"
    print(f"{mark:>14}  {name}  ({len(where)} file(s))")
PY
```

`NOT IN setup.py` is a **candidate**, not a verdict. py2app walks static imports
itself, so a plain top-level `import psutil` is normally resolved without an
`includes` entry — expect `AppKit`, `Foundation`, `PyObjCTools`, `psutil` and
`filelock` to show up clean in a real build. What the walker cannot see, and what
this list exists to surface, is anything imported **lazily, inside a function, by
string name, or through `objc.loadBundle`**. Cross-check each hit against how it
is imported before calling it: a lazy import missing from `includes` is `BLOCK`, a
static one is `NOTE`. `--build` settles it either way.

### 2. Files read off disk at runtime

```bash
cd host && grep -rnE '(Path|open|read_text|read_bytes|imageNamed|contentsOfFile)\(' \
  dark_army_daemon dark_army_menubar \
  | grep -vE 'Path\.home\(\)|tmp|test' | head -40
grep -n 'resources' setup.py
```

Any runtime read of a repo-relative path that is not under `resources` in
`setup.py` resolves differently inside `Contents/Resources`. Flag each.

### 3. The hook handler's purity

```bash
cd host && .venv/bin/python - <<'PY'
import ast
from dark_army_menubar.hooks import NOTIFY_SCRIPT
allowed = {"json","os","socket","sys","time","subprocess","pathlib","re","uuid","hashlib","urllib"}
bad = set()
for node in ast.walk(ast.parse(NOTIFY_SCRIPT)):
    if isinstance(node, ast.Import):
        bad |= {a.name.split(".")[0] for a in node.names}
    elif isinstance(node, ast.ImportFrom) and node.module:
        bad.add(node.module.split(".")[0])
bad -= allowed
print("UNEXPECTED IMPORTS:", sorted(bad) if bad else "none")
PY

# Must parse under the worst interpreter it can meet.
cd host && .venv/bin/python -c "
from dark_army_menubar.hooks import NOTIFY_SCRIPT
open('/tmp/bc-notify-probe.py','w').write(NOTIFY_SCRIPT)"
/usr/bin/python3 -c "import ast;ast.parse(open('/tmp/bc-notify-probe.py').read());print('parses under 3.9')"
```

Any unexpected import, or a parse failure under `/usr/bin/python3`, is a `BLOCK`:
the whole event path dies silently and the daemon simply sees nothing.

### 4. The loopback boundary and the token

```bash
cd host && grep -rn "0\.0\.0\.0\|INADDR_ANY\|host=\"\"\|API_HOST\s*=" \
  dark_army_daemon/ | head
# Both halves of the write gate: the token AND the Origin allowlist.
grep -rn "x-bob-token\|api-token\|origin" dark_army_daemon/api_server.py
# The two tiers: the file is the session token, the desk token is memory only.
grep -rn "session_token\|_session_authorised\|SESSION_ACTIONS\|SESSION_READS" \
  dark_army_daemon/api_server.py
grep -rn "desk_token" dark_army_daemon/ dark_army_menubar/ | grep -iE "log|print|state|snapshot" | head
grep -rn "chmod\|0o600\|0o700" dark_army_daemon/*.py dark_army_menubar/*.py | head
ls -l ~/.dark-army/api-token 2>/dev/null
# The token must never reach a log line or an error body.
grep -rn "token" dark_army_daemon/api_server.py | grep -iE "log|print|write|body" | head
```

A non-loopback bind is `BLOCK`. A token file that is not owner-only is `WARN`. A
token in a log line is `BLOCK`. A desk token in a log line, on a snapshot or
written to any file is `BLOCK`, and so is a desk verb reachable through
`_session_authorised` (`docs/transport-contract.md`, *The loopback door has
two tokens*).

### 5. Destructive verbs still guarded

```bash
cd host && grep -rn "def stop_session" -A 30 dark_army_daemon/daemon.py | grep -nE "pid|identity|psutil|cmdline|refus"
grep -rn "def delete_abandoned_agent" -A 30 dark_army_daemon/*.py | grep -nE "abandoned|categor|re-?check|refus"
# Deletion scope: jobs/<short-id> and nothing wider.
grep -rn "rmtree\|unlink\|os\.remove" dark_army_daemon/*.py
```

Every `rmtree` must be provably confined to `~/.claude/jobs/<short-id>/`. Anything
that could reach a transcript or a parent directory is `BLOCK`.

### 6. Persisted state

```bash
cd host && grep -rn "os\.replace\|\.rename(" dark_army_daemon/session_store.py \
  dark_army_daemon/jobs_store.py dark_army_menubar/preferences.py
grep -rn "\.get(" dark_army_daemon/session_store.py | wc -l   # defaults on read
grep -rn "settings.json" -B2 -A8 dark_army_menubar/hooks.py | grep -nE "read|update|json\.load|\.get\("
```

Writes must be temp-file + `os.replace`. Anything writing `~/.claude/settings.json`
must read-modify-write, never overwrite — it is the user's file and it holds
things Dark Army knows nothing about.

**Known baseline, so it is not re-reported every run:** only `session_store.py`
writes atomically today. `preferences.py` uses a plain `write_text()`, and
`jobs_store.py` writes nothing. That is a real gap worth filing **once** — it is
not a fresh finding of the diff under review. Report it as `NOTE (pre-existing)`
unless the diff makes it worse, and raise it to a finding the moment a change
starts writing preferences from more than one thread or path.

### 7. Platform and deleted layers

**Scope to source, never to `host/`.** `host/dist/` holds the frozen bundle —
numpy alone carries dozens of `sys.platform` hits — and `host/build/` its
intermediates. A repo-wide grep here fails permanently and teaches everyone to
skip the check. `host/tests/` also holds fixture strings ("Verify LVGL") that
read as findings.

List the two source dirs literally. **Do not factor them into a `SRC="a b"`
variable:** the shell here is zsh, which does not word-split an unquoted
expansion, so `grep … $SRC` passes one nonexistent path, prints a warning to
stderr, and reports clean — a check that silently always passes. (Confirmed the
hard way while writing this file.)

```bash
grep -rn "sys\.platform\|platform\.system()" \
  host/dark_army_daemon host/dark_army_menubar --include='*.py'
grep -rniE "LVGL|SDL2|RGB565|ESP32|bluetooth" \
  host/dark_army_daemon host/dark_army_menubar panel/Sources \
  --include='*.py' --include='*.swift'
```

Any output is a finding — both layers were deliberately removed. A *warning* on
stderr is also a finding: it means the paths were wrong and nothing was scanned.

**One known baseline hit:** `session_stats.py:64` carries `"Verify LVGL clone and
version"` as an example description in a comment. It is not a finding; if it is
the only hit, the check passed. Anything else, or that string appearing in real
code rather than a comment, is real.

### 8. Cast parity

```bash
cd "$(git rev-parse --show-toplevel)" && python3 - <<'PY'
import re, pathlib
py = pathlib.Path("host/dark_army_daemon/identity.py").read_text()
sw = pathlib.Path("panel/Sources/BobPanel/Cast.swift").read_text()
pn = set(re.findall(r'"([A-Z][a-zA-Z]+)"', re.search(r'NAMES[^=]*=\s*\((.*?)\)', py, re.S).group(1)))
sn = set(re.findall(r'"([A-Z][a-zA-Z]+)"', re.search(r'static let names\s*=\s*\[(.*?)\]', sw, re.S).group(1)))
print("PYTHON ONLY:", sorted(pn - sn), "| SWIFT ONLY:", sorted(sn - pn)) if pn != sn else print("cast in step")
PY
```

### With `--build`: inspect the real bundle

Run from the **repo root**, and note the absolute `APP` — a `cd host` in the same
block makes every later `host/dist/...` path resolve to `host/host/dist`.

```bash
ROOT=$(git rev-parse --show-toplevel)
( cd "$ROOT/host" && ./build.sh 2>&1 | tail -20 )
APP="$ROOT/host/dist/Dark Army.app"

# Every declared package actually made it in.
find "$APP/Contents/Resources" -maxdepth 3 -name 'dark_army_*' | head

# Resources read at runtime. py2app FLATTENS the `resources` list by basename —
# `dark_army_menubar/icons` lands at Contents/Resources/icons, NOT at the
# path it had in the repo. Any code resolving it by its source-relative path is
# the finding this line exists to catch.
ls "$APP/Contents/Resources/icons" | head

# The panel binary, and the repo-root stamp that drives the rebuild row.
ls -l "$APP/Contents/Resources/BobPanel" "$APP/Contents/Resources/repo-root"
cat "$APP/Contents/Resources/repo-root"

# The signature is intact — the stamp must be written BEFORE signing.
codesign --verify --deep --strict "$APP" && echo "signature ok"

# Bundle identity, which UNUserNotificationCenter traps without.
/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist"
```

**Never execute the bundled binary to test it.** It has no `--help` handler and
`LSUIElement` is true, so running it launches a **second live menu-bar app** —
two daemons racing for the hook socket and port 19873, two status items, and a hang with no output.
An audit does not start the thing it is auditing. To prove the frozen app can
import what it needs, import into its own interpreter instead — and make it
the bundle's own. The bare `Contents/MacOS/python` is the host's Python: its
`sys.path` is Homebrew's framework and site-packages, so it cannot find
`dark_army_daemon` at all, and a third-party module present on this Mac
would pass for one the bundle lacks. A clean environment, `PYTHONHOME` at
`Contents/Resources`, `-S` (no site-packages) and `-P` (no current directory)
leave `sys.path` as the bundle's `python312.zip`, `lib/python3.12` and
`lib-dynload` alone — what the launcher's `__boot__.py` resolves. `-B` and
`PYTHONDONTWRITEBYTECODE=1` keep it from caching `.pyc` files inside the
bundle: any write there breaks the signature the check above verifies.

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin PYTHONHOME="$APP/Contents/Resources" PYTHONNOUSERSITE=1 \
  PYTHONDONTWRITEBYTECODE=1 "$APP/Contents/MacOS/python" -B -S -P -c "
import dark_army_daemon.daemon, dark_army_menubar.app, dark_army_daemon.relay_ws, Quartz
print('imports ok')
" 2>&1 | tail -3
```

The standing check is `cd host && .venv/bin/pytest -q -rs tests/test_frozen_bundle.py`:
it runs the same seam against every `includes` entry, asserts every `sys.path`
entry is inside the bundle, and proves `setup.py`'s `EXCLUDES` are absent.
Its bundle half skips (and says why) when `host/dist` predates `setup.py` or
`requirements.txt`; a skip there is not a pass.

If that interpreter is absent (py2app layouts vary), say the check was **skipped**
— do not substitute running the app.

## Phase 2: spawn the reviewer

Show the **Watch** banner (`banners/integration.txt`), then spawn:

```
Agent({
  subagent_type: "bc-integration-reviewer",
  description: "Integration review",
  prompt: "Review the current working tree against your brief.
context_path: <abs path to CLAUDE.md>
Deterministic results already collected:
<paste Phase 1 output>
Bundle was built: <yes/no>

Do not re-run those. Focus on what they cannot decide: whether a runtime read
resolves inside the bundle, whether a destructive verb re-checks its guard at
the moment it fires rather than trusting a snapshot, whether a persisted-state
change is readable by an older build, and whether any new thread, timer,
subprocess or shell-out degrades to a log line rather than a traceback."
})
```

## Phase 3: report

Merge both halves into one table, most severe first, then:

```
VERDICT: BLOCK | WARN | CLEAN
SKIPPED: <any check not run, and why>
```

Be explicit about what was skipped. A report that silently omits the bundle
section reads as "packaging is fine" when nobody built it — and packaging is the
one thing here that only fails after the user has already installed it.
