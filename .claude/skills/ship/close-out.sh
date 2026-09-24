#!/bin/bash
# close-out-contract: leaves-open
# close-out-mode: plan
# The end of a /ship run. It frees the run's baseline worktree and, run bare,
# prints one line saying the terminal was left open: an implementation or
# scout run never closes, clears or wraps up its terminal unasked, for Claude,
# Codex or Grok alike. Two flags close, each with one close request and its
# identity checks; a refusal is reported, never retried and never turned into
# `/clear`:
#   --plan   the last act of a *planning* run. Its whole output is the plan
#            on the card, so the tab closes by itself — but only when Dark
#            Army's own board lists this session as a card's plan writer
#            (`refine_session_id`); anything else is left open.
#   --close  the person asked the agent to close the tab, in words.
# The two lines above are contract markers: a project's copy hands off only
# to an installed copy carrying both, so an older helper that still closes
# unasked, or does not know `--plan`, is never handed the job.
#
# Why a script rather than a line in SKILL.md: Claude's session id is not in
# the environment. Claude Code exports CLAUDE_CODE_BRIDGE_SESSION_ID, which is
# a *different* id from the one Dark Army's hooks record, so the real one has to be
# discovered — by walking up this process's own ancestry until a pid matches a
# row Dark Army is publishing on /api/state. Grok is the other way round: it exports
# GROK_SESSION_ID as the real id, and the walk cannot find it (tools run under
# a detached `grok agent leader`). That split is exactly the sort of thing
# that gets retyped wrong once per project. Codex exports CODEX_THREAD_ID and/or
# CODEX_SESSION_ID: match the exact conversation, never a folder or ancestor.
#
# It prints one line saying what happened and **never fails the caller**. No
# daemon, no token, no VS Code window and "could not work out which session this
# is" are all normal outcomes on some machine somewhere; the plan is on disk and
# the card is on the board either way, so none of them is worth an error.
set -u

# Arguments first: none is the default (close nothing), exactly `--close` is
# the person's request, exactly `--plan` is a planning run's own close, and
# anything else closes nothing.
mode=default
if [ "$#" -eq 1 ] && [ "$1" = "--close" ]; then
  mode=close
elif [ "$#" -eq 1 ] && [ "$1" = "--plan" ]; then
  mode=plan
elif [ "$#" -gt 0 ]; then
  echo "close-out: unknown option $*; terminal left open."
  exit 0
fi

# A ship run's baseline worktree (`gate.sh baseline`, cut under the run's
# scratch directory) is removed here: a worktree left behind survives the
# scratch directory and clutters `git worktree list` for ever. Done before
# the hand-off, so the project's copy acts on its own run.
if [ -z "${BOB_CLOSE_OUT_SHIM:-}" ] && [ -n "${SCRATCH:-}" ] && [ -e "$SCRATCH/baseline" ]; then
  gate="$(dirname "$0")/gate.sh"
  [ -f "$gate" ] || gate=".claude/skills/ship/gate.sh"
  [ -f "$gate" ] && SCRATCH="$SCRATCH" bash "$gate" baseline --remove
fi

# Dark Army installs the current copy of this script machine-wide
# (~/.dark-army/dark-army-close-out, rewritten on every launch; on a Mac
# still on the old install, ~/.bob-companion/bob-companion-close-out).
# Hand off to it, arguments and all, only when it carries both contract
# markers: an installed copy from before these rules would close the tab
# unasked, or refuse `--plan` as an unknown option. The
# guard stops the installed copy — byte-identical to this file — handing off
# to itself for ever. Without a current copy the body below runs.
installed="$HOME/.dark-army/dark-army-close-out"
[ -x "$installed" ] || installed="$HOME/.bob-companion/bob-companion-close-out"
if [ -z "${BOB_CLOSE_OUT_SHIM:-}" ] && [ -x "$installed" ] \
    && grep -q '^# close-out-contract: leaves-open$' "$installed" 2>/dev/null \
    && grep -q '^# close-out-mode: plan$' "$installed" 2>/dev/null; then
  BOB_CLOSE_OUT_SHIM=1 exec bash "$installed" "$@"
fi

if [ "$mode" = "default" ]; then
  echo "close-out: terminal left open; close it in Dark Army when you have read it."
  exit 0
fi

port="${BOB_COMPANION_API_PORT:-19874}"
token_file="$HOME/.dark-army/api-token"
[ -r "$token_file" ] || token_file="$HOME/.bob-companion/api-token"

if [ ! -r "$token_file" ]; then
  echo "close-out: no Dark Army token on this machine; nothing closed."
  exit 0
fi
token=$(cat "$token_file")

state=$(curl -sf --max-time 5 "http://127.0.0.1:$port/api/state") || {
  echo "close-out: Dark Army's daemon is not answering; nothing closed."
  exit 0
}

# Grok exports GROK_SESSION_ID as the same id Dark Army records. Its tools run
# under `grok agent leader`, whose parent is PID 1, so the TUI that owns
# the tab is never an ancestor of this process and the walk below cannot
# find it. Claude is unchanged: it has no such env var (the bridge id is
# a different one) and its tools parent up to the session pid. A person
# asked for this close, so a Grok build closes as well as a refinement.
# The id is read *after* the walk: it leaks into other sessions' environments
# (the pty broker, a Grok leader), and a Claude session that inherited it
# must close its own tab, not the Grok one. A real Grok never matches the walk.
sid=""
grok_sid="${GROK_SESSION_ID:-}"

# Presence matters: an explicitly empty Codex id must not rescue itself through
# the legacy ancestry walk. Grok retains precedence when its id is non-empty.
if [ -z "$grok_sid" ] && { [ "${CODEX_THREAD_ID+x}" ] || [ "${CODEX_SESSION_ID+x}" ]; }; then
  if ! sid=$(python3 -c '
import os, re
ids = []
for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
    if name not in os.environ:
        continue
    value = os.environ[name]
    if value.startswith("codex:"):
        value = value[6:]
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        print("close-out: invalid Codex session identity; left open.")
        raise SystemExit
    ids.append(value.lower())
if len(set(ids)) != 1:
    print("close-out: conflicting Codex session identities; left open.")
else:
    print("codex:" + ids[0])
' 2>/dev/null); then
    echo "close-out: could not validate Codex session identity; left open."
    exit 0
  fi
  case "$sid" in
    codex:*) ;;
    close-out:*) printf '%s\n' "$sid"; exit 0 ;;
    *) echo "close-out: could not validate Codex session identity; left open."; exit 0 ;;
  esac
fi

# Walk up: this shell -> claude -> the terminal's shell -> VS Code. The session
# is whichever ancestor Dark Army is publishing a pid for. Bounded, because an
# unbounded parent walk on a machine with a weird process tree is a hang.
# A Grok tool parents up to PID 1, so for Grok this finds nothing.
if [ -z "$sid" ]; then
  pid=$$
  for _ in 1 2 3 4 5 6 7 8; do
    pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
    [ -n "$pid" ] && [ "$pid" != "1" ] || break
    sid=$(printf '%s' "$state" | PID="$pid" python3 -c '
import json, os, sys
pid = os.environ["PID"]
state = json.load(sys.stdin)
for rows in (state.get("agents") or {}).values():
    if not isinstance(rows, list):
        continue
    for row in rows:
        if isinstance(row, dict) and str(row.get("pid")) == pid:
            print(row.get("session_id") or "")
            raise SystemExit
')
    [ -n "$sid" ] && break
  done
fi
[ -n "$sid" ] || sid="$grok_sid"

# This also protects a Codex target found through legacy ancestry (or Grok's
# env slot): identification is not permission, and duplicates are ambiguous.
case "$sid" in
  codex:*)
    if ! codex_gate=$(printf '%s' "$state" | SID="$sid" python3 -c '
import json, os, sys
try:
    state = json.load(sys.stdin)
    agents = state.get("agents") if isinstance(state, dict) else None
    if not isinstance(agents, dict):
        raise ValueError("invalid agents")
except (ValueError, TypeError):
    print("close-out: unreadable Dark Army snapshot; Codex session left open.")
    raise SystemExit
matches = [row for rows in agents.values() if isinstance(rows, list)
           for row in rows if isinstance(row, dict)
           and row.get("session_id") == os.environ["SID"]]
if not matches:
    print("close-out: this Codex session is not in Dark Army\u0027s snapshot; left open.")
elif len(matches) != 1:
    print("close-out: multiple rows match this Codex session; left open.")
else:
    print("permit")
' 2>/dev/null); then
      echo "close-out: could not verify Codex close permission; left open."
      exit 0
    fi
    # Only successful parsing plus an explicit permit may reach the POST.
    # Empty output can mean a crashed parser, never permission to close.
    case "$codex_gate" in
      permit) ;;
      close-out:*) printf '%s\n' "$codex_gate"; exit 0 ;;
      *) echo "close-out: could not verify Codex close permission; left open."; exit 0 ;;
    esac
    ;;
esac

if [ -z "$sid" ]; then
  echo "close-out: could not work out which session this is; nothing closed."
  exit 0
fi

# A planning run closes itself only when Dark Army's board says it is one: a
# card names this session as its plan writer. Decided off the daemon's own
# snapshot, never off this shell's guess, so an implementation run that ran
# `--plan` by mistake is left open.
if [ "$mode" = "plan" ]; then
  planning=$(printf '%s' "$state" | SID="$sid" python3 -c '
import json, os, sys
try:
    state = json.load(sys.stdin)
    cards = ((state.get("board") or {}).get("cards") or []) if isinstance(state, dict) else []
except (ValueError, TypeError, AttributeError):
    cards = []
sid = os.environ["SID"]
print("yes" if any(isinstance(c, dict) and c.get("refine_session_id") == sid
                   for c in cards) else "no")
' 2>/dev/null)
  if [ "$planning" != "yes" ]; then
    echo "close-out: terminal left open; this session is not a card's planning run."
    exit 0
  fi
fi

# No -f here: a refusal is a 409 with the reason in the body, and -f throws the
# body away. "That session is waiting on a permission prompt" is the single most
# useful thing this script can say, so it must survive.
#
# One close request, and it is the act that ends this script: the daemon
# disposes the tab before it replies, so the reply routinely never arrives —
# this process dies with the terminal, which *is* success. Nothing follows it:
# a refusal or an unreadable reply is reported, never retried and never
# turned into a request to type or clear the conversation.
action="close_terminal"
case "$sid" in codex:*) action="close_refinement_terminal" ;; esac
close_reply=$(curl -s --max-time 10 -X POST "http://127.0.0.1:$port/api/action" \
  -H "X-Bob-Token: $token" -H "Content-Type: application/json" \
  -d "$(SID="$sid" ACTION="$action" python3 -c 'import json, os; print(json.dumps({"action": os.environ["ACTION"], "session_id": os.environ["SID"]}))')")

verdict=$(printf '%s' "$close_reply" | python3 -c '
import json, sys
try:
    reply = json.load(sys.stdin)
except Exception:
    print("unknown")
    raise SystemExit
if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
    print("unknown")
else:
    print("ok" if reply["ok"] else "refused")
')

if [ "$verdict" = "ok" ]; then
  echo "close-out: closed the terminal."
  exit 0
fi

if [ "$verdict" != "refused" ]; then
  echo "close-out: asked Dark Army to close this terminal; no valid reply came back. The terminal may have closed; no retry or clear was sent."
  exit 0
fi

# A parsed refusal, for every provider: the terminal stays as it is and the
# daemon's own reason is repeated.
printf '%s' "$close_reply" | python3 -c '
import json, sys
reply = json.load(sys.stdin)
detail = " ".join(str(reply.get("detail") or "").split())
print("close-out: terminal left open." + (" " + detail if detail else ""))
'
