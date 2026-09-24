# ship — scout mode

Loaded after `references/common.md` by every adapter in scout mode
(`/ship scout <brief>`). It investigates, writes a report, attaches the
report to its board card, and stops. It writes no product code and no
plan. Promote — turning the report into a build card — is the person's.

## Phase S0: preconditions

`common.md`'s Phase 0, minus the baseline patch — a scout changes no
code, so there is nothing to snapshot as a delta. Show the `intro.txt`
banner.

## Phase S1: investigate

Read the brief and the mapped subject documents (`common.md`'s reading
rule). Investigate with reads, `grep`, the graph and the shunt helper
for bulk reads. **Write no product code, no plan.**

## Phase S2: write the report

Write the report under the project's research folder (create
`docs/research/` if it is absent) as
`docs/research/<YYYY-MM-DD>-<slug>.md` with these headings, in this
order:

```
# <title>
## Question
## What was found
## Evidence
## Recommendation
## Open questions
```

`## Evidence` cites paths and line numbers. `## Recommendation` may
recommend implementation and authorises none.

## Phase S3: attach and close

Call `mcp__dark-army__dark_army_attach_report({ path })` once, then
`dark_army_close_card({ note: "Report: <abs path> — <one sentence>" })`. If
`dark_army_attach_report` is absent (an older channel copy), say so, print the
path, and still close with the path in the note. A session started before
the rename carries the same verbs as `mcp__bob__bob_*`; a session keeps the
tool list it was born with.

Never `dark_army_attach_plan`. Never `dark_army_add_card` for the same finding —
Promote is the person's.

## Phase S4: close out

Run `bash .claude/skills/ship/close-out.sh` (it closes nothing), end your last
message with its line verbatim, and stop. Only a person's request in words
earns `--close`.
