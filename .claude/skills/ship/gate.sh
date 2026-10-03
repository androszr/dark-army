#!/bin/bash
# gate.sh — the ship run's gate helper: the attempt ledger, the baseline
# replay, the failure classes and the lane, in one place instead of five
# hundred characters of inline shell per gate.
#
# Why a script: the 21 Sep 2026 audit of four implement runs found each
# helper retyping the ledger row, the delta digest and the worktree recipe
# by hand — slowly, and not always the same way — and every run spending its
# attempts working out which red tests were its own. The rules are the
# briefs'; this file only makes them one command each. It needs `SCRATCH`
# (the run's scratch directory), `git`, `python3` and, for the baseline
# replay, `host/.venv/bin/pytest` (or `SHIP_PYTEST`).
#
#   gate.sh snapshot                       Phase 0 text-only baseline (no binaries)
#   gate.sh dispatch <n> <reason>          the orchestrator's row at every spawn
#   gate.sh run <gate> <dispatch> -- <cmd…> run a gate, log it, count it, stop it
#   gate.sh baseline <id…> | --remove      replay failing ids at the pre-ship tree
#   gate.sh classify <id…>                 YOURS / PRE-EXISTING / IN-FLIGHT per id
#   gate.sh lane                           fast or full, from the delta alone
#   gate.sh digest                         the delta digest the ledger rows carry
#
# Exit codes of `run`: the gate's own, or 3 when the attempt budget is spent
# and the gate did not run, or 4 when the same ids failed twice running —
# both mean stop and report `STATUS: partial`, never a fourth try.
set -u

SCRATCH="${SCRATCH:-}"
if [ -z "$SCRATCH" ]; then
    echo "gate.sh: SCRATCH is not set — export the run's scratch directory first" >&2
    exit 2
fi
mkdir -p "$SCRATCH"
REPO="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
LEDGER="$SCRATCH/ship-attempts.json"
PYTEST="${SHIP_PYTEST:-$REPO/host/.venv/bin/pytest}"
PRE_EXISTING="$SCRATCH/pre-existing.txt"
IN_FLIGHT="$SCRATCH/in-flight.txt"
FAST_LINES="${SHIP_FAST_LINES:-150}"
# Binary artifacts a `git diff` cannot replay with `git apply` (no full
# index line). The committed `.vsix` stays the install lockfile; a dirty
# rebuild of it must not poison the baseline patch.
SNAPSHOT_EXCLUDES=(
    ':(exclude)*.vsix'
    ':(exclude)*.png'
    ':(exclude)*.gif'
    ':(exclude)*.jpg'
    ':(exclude)*.jpeg'
    ':(exclude)*.webp'
    ':(exclude)*.icns'
    ':(exclude)*.zip'
)

# The path and content floors the review phases grep for. Kept here so the
# lane and `references/implement.md` cannot drift apart: the reference names
# this script, this script holds the patterns.
INTEGRATION_PATHS='host/setup\.py|host/build\.sh|host/requirements|dark_army_menubar/(hooks|first_run|preferences|notifier|panel_process)\.py|dark_army_daemon/(api_server|session_store|jobs_store|signals)\.py|panel/(Package\.swift|Sources/BobPanel/(BoardModels|EnrollmentModels|TerminalModels|ActionModels|BoardClient|Fetchers|OutcomeClient|Lifecycle|Models|DaemonClient)\.swift)|vscode-extension/'
SECURITY_PATHS='ios/BobPhone/(Client|Pairing|Push|RelayTransport|RemoteAuth|HomeTransport|HostAddress|Outbox|LeaseReminder|BobPhoneApp)\.swift|ios/BobPhoneWidget/|dark_army_daemon/(relay|relay_client|devices|lan_hosts|enrollment|paths|channel_server|terminal_stream|command_receipts|api_server)\.py|panel/Sources/BobPanel/(PairingView|RelaySheet)\.swift'
SECURITY_CONTENT='LAN_ACTIONS|REMOTE_ACTIONS|_home_open|SealedCodec|note_lan_proof|set_lease_days|_PRIVATE_FILES|X-Bob-Token'

delta_digest() {
    (cd "$REPO" && { git diff HEAD; git ls-files --others --exclude-standard | xargs -I{} cat {} 2>/dev/null; } \
        | shasum -a 256 | cut -c1-16)
}

# ledger_field <gate> <dispatch> <what>: `attempts` (rows so far) or `last`
# (the last row's failing ids, one per line). The ledger is JSON Lines; a
# reader that expects one document is wrong, so this one reads a line at a time.
ledger_field() {
    python3 - "$LEDGER" "$1" "$2" "$3" <<'PY'
import json, sys
path, gate, dispatch, what = sys.argv[1:]
rows = []
try:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("gate") == gate and str(row.get("dispatch")) == dispatch and row.get("attempt") is not None:
                rows.append(row)
except FileNotFoundError:
    pass
if what == "attempts":
    print(len(rows))
elif rows:
    print("\n".join(rows[-1].get("failing_ids") or []))
PY
}

append_row() {
    python3 - "$LEDGER" "$@" <<'PY'
import json, sys
path, gate, attempt, digest, code, dispatch, ids = sys.argv[1:8]
row = {"gate": gate,
       "attempt": None if attempt == "null" else int(attempt),
       "failing_ids": [i for i in ids.split("\n") if i],
       "delta_digest": digest,
       "exit": None if code == "null" else int(code),
       "dispatch": int(dispatch)}
if len(sys.argv) > 8:
    row["reason"] = sys.argv[8]
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row) + "\n")
PY
}

# excluded <id>: an id already classed PRE-EXISTING or IN-FLIGHT never lands
# in `failing_ids`, so it cannot trip the same-failure stop.
excluded() {
    { [ -f "$PRE_EXISTING" ] && grep -qxF -- "$1" "$PRE_EXISTING"; } ||
    { [ -f "$IN_FLIGHT" ] && grep -qxF -- "$1" "$IN_FLIGHT"; }
}

cmd_snapshot() {
    git -C "$REPO" diff -- . "${SNAPSHOT_EXCLUDES[@]}" > "$SCRATCH/pre-ship.patch"
    git -C "$REPO" diff --cached -- . "${SNAPSHOT_EXCLUDES[@]}" > "$SCRATCH/pre-ship-staged.patch"
    git -C "$REPO" status --porcelain --untracked-files=all > "$SCRATCH/pre-ship-status.txt"
    git -C "$REPO" rev-parse HEAD > "$SCRATCH/pre-ship-head.txt"
    echo "snapshot $SCRATCH/pre-ship.patch"
}

# Drop binary hunks a leftover hand-rolled patch may still carry, so one
# `.vsix` line cannot mark the whole baseline unbuildable.
strip_binary_patch() {
    python3 - "$1" "$2" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8", errors="replace").read()
chunks = re.split(r"(?=^diff --git )", text, flags=re.M)
kept = []
for chunk in chunks:
    if not chunk.strip():
        continue
    if "Binary files " in chunk or "GIT binary patch" in chunk:
        continue
    kept.append(chunk)
open(dst, "w", encoding="utf-8").write("".join(kept))
PY
}

cmd_dispatch() {
    local n="${1:?dispatch ordinal}" reason="${2:?reason}"
    append_row dispatch null "" null "$n" "" "$reason"
    echo "dispatch $n ($reason) recorded in $LEDGER"
}

cmd_run() {
    local gate="${1:?gate}" dispatch="${2:?dispatch}" budget=""
    shift 2
    if [ "${1:-}" = "--budget" ]; then budget="$2"; shift 2; fi
    [ "${1:-}" = "--" ] && shift
    [ $# -gt 0 ] || { echo "gate.sh run: no command after --" >&2; exit 2; }
    if [ -z "$budget" ]; then
        case "$gate" in pytest-full) budget=2 ;; *) budget=3 ;; esac
    fi
    local attempt
    attempt="$(ledger_field "$gate" "$dispatch" attempts)"
    if [ "$attempt" -ge "$budget" ]; then
        echo "BUDGET SPENT: $gate has used its $budget attempt(s) in dispatch $dispatch — stop and report STATUS: partial"
        exit 3
    fi
    local log="$SCRATCH/gate-$gate-d$dispatch-a$attempt.log"
    echo "gate $gate · dispatch $dispatch · attempt $attempt · log $log"
    # Pytest lives in host/.venv. A run from the repo root 127s on
    # `.venv/bin/pytest`. cd here for pytest* gates when host/ exists.
    if [[ "$gate" == pytest* ]] && [ -d "$REPO/host" ]; then
        cd "$REPO/host" || exit 2
    fi
    echo "argv: $*" > "$log"
    echo "cwd: $(pwd)" >> "$log"
    "$@" 2>&1 | tee -a "$log"
    local code="${PIPESTATUS[0]}"
    local ids="" id
    case "$gate" in
        pytest*)
            while IFS= read -r id; do
                [ -n "$id" ] || continue
                excluded "$id" && continue
                ids="${ids}${id}"$'\n'
            done < <(grep -E '^(FAILED|ERROR) ' "$log" | awk '{print $2}' | sort -u)
            ;;
        *)
            if [ "$code" != "0" ]; then
                # Past the two header lines (argv, cwd), which name the command.
                ids="$(tail -n +3 "$log" | grep -m1 -iE 'error' || true)"
            fi
            ;;
    esac
    local count
    count="$(printf '%s' "$ids" | grep -c . || true)"
    # Command-not-found / pytest usage / no tests collected with no parsed
    # ids is an operator error, not a test failure: do not spend an attempt
    # and do not trip the same-failure stop.
    if [[ "$gate" == pytest* ]] && [ "$count" -eq 0 ] && { [ "$code" = "127" ] || [ "$code" = "4" ] || [ "$code" = "5" ]; }; then
        echo "OPERATOR: no failing ids parsed (exit $code) — cwd/argv, not a test failure; this run is not an attempt"
        exit "$code"
    fi
    local previous
    previous="$(ledger_field "$gate" "$dispatch" last)"
    append_row "$gate" "$attempt" "$(delta_digest)" "$code" "$dispatch" "$ids"
    echo "gate $gate · attempt $attempt · exit $code · $count failing id(s) counted · ledger $LEDGER"
    # Empty id lists never count as "the same ids twice" (a 127 or a
    # missing file is not a test failure).
    if [ "$count" -gt 0 ] && [ "$(printf '%s' "$ids" | sort)" = "$(printf '%s\n' "$previous" | sort)" ]; then
        echo "SAME FAILURE TWICE: the same ids were red on attempt $((attempt - 1)) — stop and report STATUS: partial"
        exit 4
    fi
    exit "$code"
}

# baseline_prepare: the pre-ship tree as a second worktree beside this one —
# never a checkout or a reset of it. Prints nothing; returns 1 when the
# baseline cannot be rebuilt, which every id then reports as UNAVAILABLE.
baseline_prepare() {
    local base="$SCRATCH/baseline" head
    [ -f "$base/.unavailable" ] && return 1
    [ -d "$base/.git" ] || [ -f "$base/.git" ] && return 0
    head="$(cat "$SCRATCH/pre-ship-head.txt" 2>/dev/null || true)"
    [ -n "$head" ] || return 1
    git -C "$REPO" worktree add --detach "$base" "$head" >/dev/null 2>&1 || return 1
    local filtered="$SCRATCH/.apply-patch"
    for patch in "$SCRATCH/pre-ship-staged.patch" "$SCRATCH/pre-ship.patch"; do
        [ -s "$patch" ] || continue
        strip_binary_patch "$patch" "$filtered"
        [ -s "$filtered" ] || continue
        if ! git -C "$base" apply "$filtered" >/dev/null 2>&1; then
            touch "$base/.unavailable"
            return 1
        fi
    done
    # Untracked files the baseline held: copied from the tree as it stands,
    # the nearest thing to their bytes the run still has.
    if [ -f "$SCRATCH/pre-ship-status.txt" ]; then
        while IFS= read -r line; do
            case "$line" in "?? "*) ;; *) continue ;; esac
            local rel="${line#\?\? }"
            rel="${rel%/}"
            if [ -f "$REPO/$rel" ] && [ ! -e "$base/$rel" ]; then
                mkdir -p "$base/$(dirname "$rel")" && cp "$REPO/$rel" "$base/$rel"
            elif [ -d "$REPO/$rel" ] && [ ! -e "$base/$rel" ]; then
                mkdir -p "$base/$(dirname "$rel")" && cp -R "$REPO/$rel" "$base/$rel"
            fi
        done < "$SCRATCH/pre-ship-status.txt"
    fi
    return 0
}

# cmd_baseline <id…>: one line per id — PRE-EXISTING (red at baseline too),
# YOURS (green there), UNAVAILABLE (never collected there). Only a test the
# baseline collected and ran can be called PRE-EXISTING; the rule fails
# toward fixing.
cmd_baseline() {
    if [ "${1:-}" = "--remove" ]; then
        git -C "$REPO" worktree remove --force "$SCRATCH/baseline" >/dev/null 2>&1 && echo "baseline worktree removed" || echo "no baseline worktree to remove"
        return 0
    fi
    [ $# -gt 0 ] || { echo "gate.sh baseline: no test ids" >&2; exit 2; }
    if ! baseline_prepare; then
        for id in "$@"; do echo "UNAVAILABLE $id (baseline could not be rebuilt: treat as yours)"; done
        return 0
    fi
    local n log
    n="$(ls "$SCRATCH"/baseline-*.log 2>/dev/null | wc -l | tr -d ' ')"
    log="$SCRATCH/baseline-$n.log"
    : > "$log"
    # One pytest run per id: a single id pytest cannot find aborts the whole
    # invocation ("ERROR: not found"), which would report every other id as
    # never collected.
    for id in "$@"; do
        echo "=== $id" >> "$log"
        (cd "$SCRATCH/baseline/host" && "$PYTEST" -v -p no:cacheprovider "$id") >> "$log" 2>&1
        if grep -qF -- "$id PASSED" "$log"; then
            echo "YOURS $id (green at baseline)"
        elif grep -qF -- "$id FAILED" "$log" || grep -qF -- "$id ERROR" "$log"; then
            echo "PRE-EXISTING $id (red at baseline too — never fix it in this run)"
            grep -qxF -- "$id" "$PRE_EXISTING" 2>/dev/null || echo "$id" >> "$PRE_EXISTING"
        else
            echo "UNAVAILABLE $id (not collected at baseline: treat as yours)"
        fi
    done
    echo "baseline log $log"
}

# candidate_paths <id>: the test file, the module it is named after, and —
# for source-grep tests — the trees those tests actually read, including
# ios/BobPhone/, docs/, and the agent trees .claude/agents/,
# .codex/agents/, .grok/agents/. A test that reads a repo tree outside
# host/ lists that tree here. A trailing slash matches every path under it.
candidate_paths() {
    local file="${1%%::*}" name
    name="$(basename "$file" .py)"
    name="${name#test_}"
    echo "host/$file"
    echo "host/dark_army_daemon/$name.py"
    echo "host/dark_army_menubar/$name.py"
    case "$file" in
        tests/test_phone_*|tests/test_agent_chatter.py|tests/test_detail_tabs.py|tests/test_card_title_in_agent_detail.py)
            echo "ios/BobPhone/"
            ;;
    esac
    case "$file" in
        tests/test_agent_chatter.py|tests/test_detail_tabs.py)
            echo "panel/Sources/BobPanel/"
            ;;
    esac
    case "$file" in
        tests/test_ship_context.py|tests/test_ship_efficiency.py)
            echo "docs/"
            echo "CLAUDE.md"
            echo "AGENTS.md"
            ;;
    esac
    case "$file" in
        tests/test_agent_briefs.py|tests/test_card_preparer_brief.py|tests/test_ship_context.py|tests/test_crew.py|tests/test_objective_carried.py|tests/test_review_skill.py|tests/test_ship_follow_up_filing.py|tests/test_board_workflow.py|tests/test_ship_gate.py)
            echo ".claude/agents/"
            ;;
    esac
    case "$file" in
        tests/test_agent_briefs.py|tests/test_agent_models_own_checkout.py|tests/test_card_preparer_brief.py|tests/test_ship_context.py)
            echo ".codex/agents/"
            ;;
    esac
    case "$file" in
        tests/test_agent_briefs.py|tests/test_card_preparer_brief.py)
            echo ".grok/agents/"
            ;;
    esac
    case "$file" in
        tests/test_ship_follow_up_filing.py|tests/test_ship_gate.py)
            echo ".claude/skills/ship/"
            ;;
    esac
    case "$file" in
        tests/test_agent_models_own_checkout.py)
            echo "host/dark_army_daemon/agent_models.py"
            ;;
    esac
    case "$file" in
        tests/test_no_change"lo"g.py)
            echo ".claude/"
            echo ".agents/"
            echo ".codex/"
            echo ".grok/agents/"
            echo "tools/"
            echo "host/dark_army_menubar/agent_pack/"
            echo "docs/"
            ;;
    esac
}

# in_list <needle> <haystack-lines>: exact file, or prefix when needle ends /.
in_list() {
    python3 - "$1" "$2" <<'PY'
import sys
needle, blob = sys.argv[1], sys.argv[2]
lines = [ln for ln in blob.splitlines() if ln]
if needle.endswith("/"):
    sys.exit(0 if any(
        ln.startswith(needle) or ln.rstrip("/") == needle.rstrip("/")
        for ln in lines) else 1)
sys.exit(0 if needle in lines else 1)
PY
}

# first_flight: a candidate dirty now, clean at baseline, absent from delta.
# Candidates arrive on stdin; the Python program is a -c string so stdin
# stays the pipe (a heredoc would swallow it).
first_flight() {
    local now="$1" then="$2" delta="$3" cands
    cands="$(cat)"
    python3 -c '
import sys
now = set(ln for ln in sys.argv[1].splitlines() if ln)
then = set(ln for ln in sys.argv[2].splitlines() if ln)
delta = set(ln for ln in sys.argv[3].splitlines() if ln)
cands = [ln for ln in sys.argv[4].splitlines() if ln]
for cand in cands:
    if cand.endswith("/"):
        prefix = cand
        hits = [p for p in now
                if (p.startswith(prefix) or p.rstrip("/") == prefix.rstrip("/"))
                and p not in then
                and p not in delta
                and not any(d.endswith("/") and p.startswith(d) for d in delta)]
        if hits:
            print(sorted(hits)[0])
            sys.exit(0)
    elif cand in now and cand not in then and cand not in delta:
        print(cand)
        sys.exit(0)
sys.exit(1)
' "$now" "$then" "$delta" "$cands"
}

# cmd_classify <id…>: YOURS when the delta touches the test, its module, or
# a tree the test greps *and no sibling outside the delta is dirty*; else
# PRE-EXISTING when red at baseline; else IN-FLIGHT when a candidate
# (including ios/BobPhone/, docs/, and the agent trees .claude/agents/,
# .codex/agents/, .grok/agents/) is dirty now, was not at baseline, and
# is not in the delta. A prefix match on one of these trees is not YOURS when another file
# in that tree outside the delta is the dirty source — that sibling is
# IN-FLIGHT. CLAUDE.md / AGENTS.md on an inventory id are the same
# load-set: a dirty docs/ sibling still wins. An unbuildable baseline
# does not default remaining ids to YOURS when those dirty sources exist.
# A file dirty at baseline and changed again stays YOURS.
cmd_classify() {
    [ $# -gt 0 ] || { echo "gate.sh classify: no test ids" >&2; exit 2; }
    local paths_file="$SCRATCH/ship-delta-paths.txt"
    local status_now status_then delta_blob
    status_now="$(git -C "$REPO" status --porcelain --untracked-files=all 2>/dev/null | awk '{print $2}')"
    status_then="$(awk '{print $2}' "$SCRATCH/pre-ship-status.txt" 2>/dev/null || true)"
    delta_blob=""
    [ -f "$paths_file" ] && delta_blob="$(cat "$paths_file")"
    local -a pending=()
    local id cand owned flight
    for id in "$@"; do
        owned=""
        while IFS= read -r cand; do
            [ -n "$cand" ] || continue
            if in_list "$cand" "$delta_blob"; then owned="$cand"; break; fi
        done < <(candidate_paths "$id")
        if [ -n "$owned" ]; then
            # A directory prefix matches every file in that tree. The test
            # file is the first candidate, so owning it can mask a later
            # dirty tree candidate. CLAUDE.md / AGENTS.md are in the
            # inventory load set with docs/. Check for foreign candidates
            # before calling any of those owned paths YOURS.
            if [[ "$owned" == */ || "$owned" == "CLAUDE.md" || "$owned" == "AGENTS.md" || "$owned" == "host/${id%%::*}" ]]; then
                flight="$(candidate_paths "$id" | first_flight "$status_now" "$status_then" "$delta_blob" || true)"
                if [ -n "$flight" ]; then
                    echo "IN-FLIGHT $id ($flight changed by another run since this one's baseline — report it, never fix or count it)"
                    grep -qxF -- "$id" "$IN_FLIGHT" 2>/dev/null || echo "$id" >> "$IN_FLIGHT"
                    continue
                fi
            fi
            echo "YOURS $id (the delta touches $owned)"
        else
            pending+=("$id")
        fi
    done
    [ ${#pending[@]} -gt 0 ] || return 0
    local verdicts
    verdicts="$(cmd_baseline "${pending[@]}")"
    for id in "${pending[@]}"; do
        local line flight
        line="$(printf '%s\n' "$verdicts" | grep -F -- " $id " | head -1)"
        case "$line" in
            "PRE-EXISTING "*) echo "$line"; continue ;;
        esac
        flight="$(candidate_paths "$id" | first_flight "$status_now" "$status_then" "$delta_blob" || true)"
        if [ -n "$flight" ]; then
            echo "IN-FLIGHT $id ($flight changed by another run since this one's baseline — report it, never fix or count it)"
            grep -qxF -- "$id" "$IN_FLIGHT" 2>/dev/null || echo "$id" >> "$IN_FLIGHT"
        else
            echo "YOURS $id ${line#* $id }"
        fi
    done
}

cmd_lane() {
    local paths="$SCRATCH/ship-delta-paths.txt" patch="$SCRATCH/ship-delta.patch"
    if [ ! -f "$paths" ] || [ ! -f "$patch" ]; then
        echo "LANE: full (no delta identity under $SCRATCH — refresh it first)"
        return 0
    fi
    local lines integration security content
    lines="$(grep -cE '^[+-][^+-]' "$patch" || true)"
    integration="$(grep -E "$INTEGRATION_PATHS" "$paths" | tr '\n' ' ')"
    security="$(grep -E "$SECURITY_PATHS" "$paths" | tr '\n' ' ')"
    content="$(grep -oE "$SECURITY_CONTENT" "$patch" | sort -u | tr '\n' ' ')"
    local lane="fast"
    [ "$lines" -le "$FAST_LINES" ] || lane="full"
    [ -z "$integration" ] || lane="full"
    [ -z "$security$content" ] || lane="full"
    echo "LANE: $lane"
    echo "lines: $lines (fast at or under $FAST_LINES)"
    echo "integration: ${integration:-no}"
    echo "security: ${security:-no}${content:+ content: $content}"
}

case "${1:-}" in
    snapshot) cmd_snapshot ;;
    dispatch) shift; cmd_dispatch "$@" ;;
    run) shift; cmd_run "$@" ;;
    baseline) shift; cmd_baseline "$@" ;;
    classify) shift; cmd_classify "$@" ;;
    lane) cmd_lane ;;
    digest) delta_digest ;;
    *) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
