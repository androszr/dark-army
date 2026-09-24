#!/bin/bash
# Run the /ship preflight against one plan file.
#
# Extracts every fenced ```bash block from PREFLIGHT-CHECKLIST.md (the
# structural checks every project shares) and from every PREFLIGHT-*.md beside
# it (the profile's domain checks), and runs each block with $PLAN exported.
# Every finding is one line starting "BLOCK:" or "WARN:". Exit 1 on any BLOCK,
# 0 otherwise. No agent, no judgment — that is the point.
#
# Usage: bash .claude/skills/ship/preflight.sh <plan path> [checklist dir]
set -u

PLAN="${1:-}"
DIR="${2:-$(cd "$(dirname "$0")" && pwd)}"

if [ -z "$PLAN" ] || [ ! -r "$PLAN" ]; then
  echo "BLOCK: preflight needs a readable plan path (got '${PLAN}')"
  exit 1
fi
export PLAN

files=("$DIR/PREFLIGHT-CHECKLIST.md")
for f in "$DIR"/PREFLIGHT-*.md; do
  [ -e "$f" ] || continue
  case "$f" in */PREFLIGHT-CHECKLIST.md) continue ;; esac
  files+=("$f")
done

blocks=0
out=$(
  for f in "${files[@]}"; do
    [ -r "$f" ] || { echo "WARN: checklist $f is missing or unreadable"; continue; }
    # Pull each ```bash ... ``` block out and run it in its own subshell so one
    # check's `exit` or unset variable cannot stop the next.
    awk '
      /^```bash[[:space:]]*$/ { inblock=1; block=""; next }
      /^```[[:space:]]*$/ && inblock { inblock=0; print block; print "\x01"; next }
      inblock { block = block $0 "\n" }
    ' "$f" | awk -v RS='\x01' 'NF { print > "/dev/stdout"; print "\x02" }' | while IFS= read -r -d $'\x02' script; do
      bash -c "$script" 2>/dev/null
    done
  done
)

printf '%s\n' "$out" | grep -E '^(BLOCK|WARN):' | sort -u
blocks=$(printf '%s\n' "$out" | grep -c '^BLOCK:')
if [ "$blocks" -gt 0 ]; then
  echo "PREFLIGHT: $blocks BLOCK"
  exit 1
fi
echo "PREFLIGHT: clean"
exit 0
