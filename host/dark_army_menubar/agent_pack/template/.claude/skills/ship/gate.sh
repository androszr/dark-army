#!/bin/bash
# gate.sh — the ship run's gate helper: the attempt ledger, the delta, the
# baseline replay, the failure classes and the lane, one command each instead
# of a hand-typed shell recipe per gate. Generic: it knows no test runner and
# no project layout; the Identity table's commands are passed to it.
#
# It needs `SCRATCH` (the run's scratch directory, holding Phase 0's
# `pre-ship*.patch|txt` files), `git` and `python3`. Optional:
#   SHIP_BASELINE_CMD  the test row's command narrowed to one test, with `{id}`
#                      where the id goes, run inside the baseline worktree —
#                      unset, every baseline replay is UNAVAILABLE (= yours)
#   SHIP_FAST_LINES    the fast lane's line ceiling (150)
#   SHIP_FAIL_REGEX    one extra regex whose first group is a failing test id
#   SHIP_SOURCE_TREES  `<test-file regex>=<tree/>;…` — trees a test greps
#                      beyond its own file (a phone source-grep test reading
#                      `ios/App/`), so a sibling dirty there is IN-FLIGHT
#
#   gate.sh snapshot                       Phase 0 text-only baseline (no binaries)
#   gate.sh dispatch <n> <reason>          the orchestrator's row at every spawn
#   gate.sh run <gate> <dispatch> -- <cmd…> run a gate, log it, count it, stop it
#   gate.sh delta                          the run's own changes, minus the baseline
#   gate.sh baseline <id…> | --remove      replay failing ids at the pre-ship tree
#   gate.sh classify <id…>                 YOURS / PRE-EXISTING / IN-FLIGHT per id
#   gate.sh lane [<path regex>…]           fast or full, from the delta alone
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
PRE_EXISTING="$SCRATCH/pre-existing.txt"
IN_FLIGHT="$SCRATCH/in-flight.txt"
DELTA_PATHS="$SCRATCH/ship-delta-paths.txt"
DELTA_PATCH="$SCRATCH/ship-delta.patch"
FAST_LINES="${SHIP_FAST_LINES:-150}"
# Binary artifacts a `git diff` cannot replay with `git apply` (no full
# index line): a dirty packaged build or image must not poison the baseline.
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

# failing_ids <log>: the test ids a runner printed as failed — pytest's
# `FAILED path::id`, XCTest's `Test Case '-[Suite test]' failed`, swift-test's
# and Jest's `✘`/`✕ name`, Vitest's `FAIL file > name` — plus SHIP_FAIL_REGEX.
failing_ids() {
    python3 - "$1" "${SHIP_FAIL_REGEX:-}" <<'PY'
import re, sys
log, extra = sys.argv[1], sys.argv[2]
# The two header lines (argv, cwd) name the command; never read ids off them.
skip = 2
patterns = [
    r"^(?:FAILED|ERROR) (\S+)",
    r"Test Case '([^']+)' (?:failed|errored)",
    r"^\s*[✘✕×] (.+?)\s*(?:\(\d+ ?m?s\))?$",
    r"^ FAIL  (.+)$",
]
if extra:
    patterns.append(extra)
seen = []
with open(log, encoding="utf-8", errors="replace") as fh:
    for n, line in enumerate(fh):
        if n < skip:
            continue
        for pat in patterns:
            m = re.search(pat, line.rstrip("\n"))
            if m and m.group(1).strip() not in seen:
                seen.append(m.group(1).strip())
                break
print("\n".join(seen))
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

# Drop binary hunks a hand-rolled patch may still carry, so one such line
# cannot mark the whole baseline unbuildable.
strip_binary_patch() {
    python3 - "$1" "$2" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8", errors="replace").read()
kept = [c for c in re.split(r"(?=^diff --git )", text, flags=re.M)
        if c.strip() and "Binary files " not in c and "GIT binary patch" not in c]
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
        case "$gate" in *-full) budget=2 ;; *) budget=3 ;; esac
    fi
    local attempt
    attempt="$(ledger_field "$gate" "$dispatch" attempts)"
    if [ "$attempt" -ge "$budget" ]; then
        echo "BUDGET SPENT: $gate has used its $budget attempt(s) in dispatch $dispatch — stop and report STATUS: partial"
        exit 3
    fi
    local log="$SCRATCH/gate-$gate-d$dispatch-a$attempt.log"
    echo "gate $gate · dispatch $dispatch · attempt $attempt · log $log"
    # The Identity table's commands are written from the project root; a
    # gate run from a subdirectory 127s on a relative tool path.
    cd "$REPO" || exit 2
    echo "argv: $*" > "$log"
    echo "cwd: $(pwd)" >> "$log"
    "$@" 2>&1 | tee -a "$log"
    local code="${PIPESTATUS[0]}"
    local ids="" id
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        excluded "$id" && continue
        ids="${ids}${id}"$'\n'
    done < <(failing_ids "$log")
    # A command that never ran (127: not found) with no id parsed is an
    # operator error — cwd or argv — not a test failure: no attempt is spent
    # and the same-failure stop is not tripped.
    if [ -z "$ids" ] && [ "$code" = "127" ]; then
        echo "OPERATOR: the command was not found (exit 127) — fix cwd/argv; this run is not an attempt"
        exit "$code"
    fi
    if [ -z "$ids" ] && [ "$code" != "0" ]; then
        # No test id to name: the first error line past the two header lines.
        ids="$(tail -n +3 "$log" | grep -m1 -iE 'error' || true)"
    fi
    local previous
    previous="$(ledger_field "$gate" "$dispatch" last)"
    append_row "$gate" "$attempt" "$(delta_digest)" "$code" "$dispatch" "$ids"
    local count
    count="$(printf '%s' "$ids" | grep -c . || true)"
    echo "gate $gate · attempt $attempt · exit $code · $count failing id(s) counted · ledger $LEDGER"
    # An empty id list is never "the same ids twice".
    if [ "$count" -gt 0 ] && [ "$(printf '%s' "$ids" | sort)" = "$(printf '%s\n' "$previous" | sort)" ]; then
        echo "SAME FAILURE TWICE: the same ids were red on attempt $((attempt - 1)) — stop and report STATUS: partial"
        exit 4
    fi
    exit "$code"
}

# cmd_delta: the run's own changes — every path dirty now that was clean at
# the Phase 0 snapshot, plus every baseline-dirty path whose diff has moved
# since. Writes `ship-delta-paths.txt` and `ship-delta.patch` under $SCRATCH.
# A baseline-dirty path that changed again cannot be told from the person's
# own edit, so it counts as the run's: toward reviewing, never away from it.
cmd_delta() {
    python3 - "$REPO" "$SCRATCH" <<'PY'
import re, subprocess, sys
from pathlib import Path
repo, scratch = Path(sys.argv[1]), Path(sys.argv[2])
def git(*args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).stdout
def status_paths(text):
    out = set()
    for line in text.splitlines():
        if len(line) > 3:
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            out.add(path.rstrip("/"))
    return out
def per_file(patch):
    files, cur = {}, None
    for line in patch.splitlines():
        m = re.match(r"^diff --git a/(.*?) b/(.*)$", line)
        if m:
            cur = m.group(2)
            files[cur] = []
        if cur is not None:
            files[cur].append(line)
    return {k: "\n".join(v) for k, v in files.items()}
then_status = status_paths((scratch / "pre-ship-status.txt").read_text() if (scratch / "pre-ship-status.txt").exists() else "")
then_patch = per_file((scratch / "pre-ship.patch").read_text() if (scratch / "pre-ship.patch").exists() else "")
then_staged = per_file((scratch / "pre-ship-staged.patch").read_text() if (scratch / "pre-ship-staged.patch").exists() else "")
now_status = status_paths(git("status", "--porcelain", "--untracked-files=all"))
now_patch = per_file(git("diff", "HEAD"))
delta = set()
for path in now_status:
    if path not in then_status:
        delta.add(path)
    elif now_patch.get(path, "") != (then_patch.get(path, "") or then_staged.get(path, "")):
        delta.add(path)
tracked = sorted(p for p in delta if p in now_patch)
untracked = sorted(p for p in delta if p not in now_patch)
patch = git("diff", "HEAD", "--", *tracked) if tracked else ""
for path in untracked:
    full = repo / path
    if full.is_file():
        try:
            body = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        patch += f"diff --git a/{path} b/{path}\n+++ b/{path}\n" + "".join("+" + l + "\n" for l in body.splitlines())
(scratch / "ship-delta-paths.txt").write_text("\n".join(sorted(delta)) + ("\n" if delta else ""))
(scratch / "ship-delta.patch").write_text(patch)
print(f"delta: {len(delta)} path(s) → {scratch / 'ship-delta-paths.txt'}, patch {scratch / 'ship-delta.patch'}")
PY
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
# YOURS (green there), UNAVAILABLE (never ran there). The replay command is
# SHIP_BASELINE_CMD with `{id}` substituted, run in the worktree; a project's
# dependencies must already resolve there (a link to the main checkout's, or
# the main checkout's tool binary), or every id is UNAVAILABLE — which the
# rule reads as yours, toward fixing.
cmd_baseline() {
    if [ "${1:-}" = "--remove" ]; then
        git -C "$REPO" worktree remove --force "$SCRATCH/baseline" >/dev/null 2>&1 && echo "baseline worktree removed" || echo "no baseline worktree to remove"
        return 0
    fi
    [ $# -gt 0 ] || { echo "gate.sh baseline: no test ids" >&2; exit 2; }
    if [ -z "${SHIP_BASELINE_CMD:-}" ]; then
        for id in "$@"; do echo "UNAVAILABLE $id (SHIP_BASELINE_CMD is not set: treat as yours)"; done
        return 0
    fi
    if ! baseline_prepare; then
        for id in "$@"; do echo "UNAVAILABLE $id (baseline could not be rebuilt: treat as yours)"; done
        return 0
    fi
    local n log
    n="$(ls "$SCRATCH"/baseline-*.log 2>/dev/null | wc -l | tr -d ' ')"
    log="$SCRATCH/baseline-$n.log"
    : > "$log"
    for id in "$@"; do
        echo "=== $id" >> "$log"
        local cmd="${SHIP_BASELINE_CMD//\{id\}/$id}"
        (cd "$SCRATCH/baseline" && bash -c "$cmd") >> "$log" 2>&1
        local code=$?
        local ran
        ran="$(sed -n "/^=== $(printf '%s' "$id" | sed 's/[][\\.*^$/]/\\&/g')\$/,\$p" "$log" | tail -n +2 | grep -c . || true)"
        if [ "$code" = "0" ]; then
            echo "YOURS $id (green at baseline)"
        elif [ "$ran" -gt 0 ] && ! tail -n +2 "$log" | grep -qiE 'not found|no such file|could not find|no tests? (ran|collected)|unable to find'; then
            echo "PRE-EXISTING $id (red at baseline too — never fix it in this run)"
            grep -qxF -- "$id" "$PRE_EXISTING" 2>/dev/null || echo "$id" >> "$PRE_EXISTING"
        else
            echo "UNAVAILABLE $id (did not run at baseline: treat as yours)"
        fi
    done
    echo "baseline log $log"
}

# candidate_path <id>: the file part of a test id — `path::name`,
# `path > name`, `path:name` — as the seam a red test's ownership is read from.
candidate_path() {
    local id="$1"
    id="${id%%::*}"; id="${id%% > *}"; id="${id%%:*}"
    echo "$id"
}

# candidate_paths <id>: the test's file plus, from SHIP_SOURCE_TREES, every
# tree a test matching the pair's regex greps. A trailing slash is a prefix.
candidate_paths() {
    local file pair regex tree
    file="$(candidate_path "$1")"
    [ -n "$file" ] && echo "$file"
    local IFS=';'
    for pair in ${SHIP_SOURCE_TREES:-}; do
        [ -n "$pair" ] || continue
        regex="${pair%%=*}"; tree="${pair#*=}"
        if [ -n "$file" ] && printf '%s' "$file" | grep -qE -- "$regex"; then echo "$tree"; fi
    done
}

# in_list <needle> <haystack-lines>: exact file, or prefix when needle ends /.
in_list() {
    python3 - "$1" "$2" <<'PY'
import sys
needle, lines = sys.argv[1], [ln for ln in sys.argv[2].splitlines() if ln]
if needle.endswith("/"):
    sys.exit(0 if any(ln.startswith(needle) or ln.rstrip("/") == needle.rstrip("/") for ln in lines) else 1)
sys.exit(0 if needle in lines else 1)
PY
}

# first_flight <now> <then> <delta>: the first candidate (stdin) dirty now,
# clean at the baseline and absent from the delta — a prefix candidate
# matches any file under it that is not in the delta.
first_flight() {
    local cands
    cands="$(cat)"
    python3 -c '
import sys
now = set(sys.argv[1].splitlines()) - {""}
then = set(sys.argv[2].splitlines()) - {""}
delta = set(sys.argv[3].splitlines()) - {""}
for cand in [c for c in sys.argv[4].splitlines() if c]:
    if cand.endswith("/"):
        hits = [p for p in now if p.startswith(cand) and p not in then and p not in delta
                and not any(d.endswith("/") and p.startswith(d) for d in delta)]
        if hits:
            print(sorted(hits)[0]); sys.exit(0)
    elif cand in now and cand not in then and cand not in delta:
        print(cand); sys.exit(0)
sys.exit(1)
' "$1" "$2" "$3" "$cands"
}

# cmd_classify <id…>: YOURS when the delta touches the test's file or a tree
# it greps *and no sibling in that tree outside the delta is dirty*; else
# PRE-EXISTING when red at baseline; else IN-FLIGHT when a candidate is
# dirty now, was clean at baseline and is not in the delta — another run's
# half-built work, reported, never fixed, never counted; else YOURS.
cmd_classify() {
    [ $# -gt 0 ] || { echo "gate.sh classify: no test ids" >&2; exit 2; }
    [ -f "$DELTA_PATHS" ] || cmd_delta >/dev/null
    local status_now status_then delta_blob
    status_now="$(git -C "$REPO" status --porcelain --untracked-files=all 2>/dev/null | cut -c4-)"
    status_then="$(cut -c4- "$SCRATCH/pre-ship-status.txt" 2>/dev/null || true)"
    delta_blob="$(cat "$DELTA_PATHS")"
    local -a pending=()
    local id cand owned flight
    for id in "$@"; do
        owned=""
        while IFS= read -r cand; do
            [ -n "$cand" ] || continue
            if in_list "$cand" "$delta_blob"; then owned="$cand"; break; fi
        done < <(candidate_paths "$id")
        if [ -n "$owned" ]; then
            # A tree prefix owns every file under it in the delta; a sibling
            # there that another run dirtied since the baseline still wins.
            if [[ "$owned" == */ ]]; then
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
        local line
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

# cmd_lane [<regex>…]: fast when the delta is at or under FAST_LINES added
# and deleted lines and no delta path matches any Reviewers-table regex
# given; full otherwise. The regexes are the table's, pasted by the caller.
cmd_lane() {
    cmd_delta >/dev/null
    local lines matched="" regex hit
    lines="$(grep -cE '^[+-][^+-]' "$DELTA_PATCH" || true)"
    for regex in "$@"; do
        hit="$(grep -E "$regex" "$DELTA_PATHS" | tr '\n' ' ')"
        [ -z "$hit" ] || matched="${matched}${regex} → ${hit}; "
    done
    local lane="fast"
    [ "$lines" -le "$FAST_LINES" ] || lane="full"
    [ -z "$matched" ] || lane="full"
    echo "LANE: $lane"
    echo "lines: $lines (fast at or under $FAST_LINES)"
    echo "reviewers: ${matched:-no path matched}"
}

case "${1:-}" in
    snapshot) cmd_snapshot ;;
    dispatch) shift; cmd_dispatch "$@" ;;
    run) shift; cmd_run "$@" ;;
    delta) cmd_delta ;;
    baseline) shift; cmd_baseline "$@" ;;
    classify) shift; cmd_classify "$@" ;;
    lane) shift; cmd_lane "$@" ;;
    digest) delta_digest ;;
    *) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
