# Ship preflight checklist — structural checks

Deterministic checks run by `/ship` Phase 4 against the **plan file**, using bash
and grep only. No agent spawn — that is the point: these must be cheap, boring,
and identical every time.

`preflight.sh <plan>` runs every fenced `bash` block in this file, then every
block in each `PREFLIGHT-*.md` beside it (the profile's domain checks). `$PLAN`
is the plan path. Every finding is one line starting `BLOCK:` or `WARN:`.

These checks are shared by every project. Domain checks (money, auth, threads,
entitlements) live in the profile files, never here.

---

## 1. Required sections — BLOCK

```bash
for s in "## Idea" "## Context" "## Files to change" "## New files" "## Steps" \
         "## Risks & footguns" "## Test plan" "## Acceptance criteria" "## Out of scope"; do
  grep -qF "$s" "$PLAN" || echo "BLOCK: missing section $s"
done
```

*Fix:* the planner dropped a template heading. Re-spawn with `mode: iterate`.

## 2. Plain-language sections present — BLOCK

Phase 5 shows the user only the plain-language zone; without these headings there
is nothing readable to show. `## Technical detail` is required too — it is the
divider check 3 bounds on.

```bash
for s in "## What this does" "## How I'll build it" "## How you'll know it worked" \
         "## Technical detail"; do
  grep -qF "$s" "$PLAN" || echo "BLOCK: missing plain-language section $s"
done
```

## 3. Jargon leaking into the plain-language zone — WARN

The region from `## What this does` down to the `## Technical detail` divider must
read clean for a non-programmer: no paths, no code, no commands, no version
numbers. Profiles add their own framework names in their own check.

```bash
awk '/^## What this does/{f=1} /^## Technical detail/{f=0} f' "$PLAN" \
  | grep -nE '`|grep |[a-zA-Z0-9_-]+/[a-zA-Z0-9_./-]+\.(ts|tsx|js|py|swift|md|json|yml)([^a-zA-Z]|$)|/api/|GET |POST |[0-9]+\.[0-9]+\.[0-9]+' \
  && echo "WARN: plain-language zone leaks technical detail (lines above) — relocate it below ## Technical detail"
```

*Fix:* rewrite the flagged lines as observable behavior, and keep the paths and
symbols in the technical zone. Never delete the detail — relocate it.

## 4. Unverifiable acceptance criteria — WARN

Every bullet under `## Acceptance criteria` should contain a backtick command,
`grep`, `exits 0`, `MANUAL:`, or `exists`.

```bash
awk '/^## Acceptance criteria/{f=1;next} /^## /{f=0} f && /^[-*0-9]/' "$PLAN" \
  | grep -vE '`|grep|exits 0|MANUAL:|exists' \
  && echo "WARN: criteria above are not command-verifiable"
```

*Fix:* "the screen works" → "`<test gate> path/to/test` passes ≥8 cases".

## 5. A MANUAL criterion without steps or a reason — BLOCK

A `MANUAL:` criterion is instructions for the person who will check it. Each
one must carry a `Steps:` line and a `Why not automated:` line, and there may be
at most two of them.

```bash
manual=$(awk '/^## Acceptance criteria/{f=1;next} /^## /{f=0} f' "$PLAN" | grep -c 'MANUAL:')
steps=$(awk '/^## Acceptance criteria/{f=1;next} /^## /{f=0} f' "$PLAN" | grep -c 'Steps:')
why=$(awk '/^## Acceptance criteria/{f=1;next} /^## /{f=0} f' "$PLAN" | grep -c 'Why not automated:')
[ "$manual" -gt 2 ] && echo "BLOCK: $manual MANUAL criteria — more than two means the plan has not looked for the seam that would automate them"
[ "$steps" -lt "$manual" ] && echo "BLOCK: a MANUAL criterion has no 'Steps:' line — write what to open, press and expect"
[ "$why" -lt "$manual" ] && echo "BLOCK: a MANUAL criterion has no 'Why not automated:' line — a check that cannot say why it is manual should be a test"
true
```

## 6. Stages header names a real specialist — WARN

The board card's stages come from the plan's `**Stages:**` line. A stage the
project has no agent for is a card that promises a pass nobody will run.

```bash
line=$(grep -m1 -E '^\- \*\*Stages:\*\*' "$PLAN" | sed 's/^- \*\*Stages:\*\* *//')
if [ -z "$line" ]; then
  echo "WARN: no Stages line — the card will carry no specialist list"
else
  printf '%s\n' "$line" | tr '|' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' | while read -r stage; do
    [ -f ".claude/agents/$stage.md" ] || echo "WARN: stage '$stage' has no agent file at .claude/agents/$stage.md"
  done
fi
```

## 7. Absolute machine paths — WARN

A plan is read by a session on a possibly different checkout. Paths in it are
repo-relative.

```bash
u=Users; h=home
grep -nE "/$u/[a-z]|/$h/[a-z]|C:\\\\" "$PLAN" && echo "WARN: absolute machine paths above — make them repo-relative"
true
```
