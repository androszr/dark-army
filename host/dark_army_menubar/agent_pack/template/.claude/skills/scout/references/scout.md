# scout — investigate and report

Loaded by the scout skill (`/scout <brief>`; `/ship scout <brief>` is its
alias) on every provider. It investigates, writes a report, attaches the
report to its board card, and stops. It writes no product code and no
plan. Promote — turning the report into a build card — is the person's.

## Phase S0: preconditions

Phase 0 of `.claude/skills/ship/references/common.md` (read that phase
alone), minus the baseline patch — a scout changes no code, so there is
nothing to snapshot as a delta. Show the
`.claude/skills/ship/banners/intro.txt` banner.

## Phase S1: investigate

Read the brief and the mapped subject documents (*What every agent reads, and how much*
in `.claude/skills/ship/references/common.md`). Investigate with reads, `grep`, the graph and the shunt helper
for bulk reads. **Write no product code, no plan.**

## Phase S2: write the report

Every report gets its own folder under the project's `scout/` folder at
the project root: `scout/<YYYY-MM-DD>-<slug>/report.md`, with any
captures, logs or scratch files the investigation gathered beside it in
that same folder. Create the folder if it is absent. The report opens with
the answer block — copy it and fill every line — then the five headings,
in this order:

```
# <title>

- **Card:** <the card's title, as your prompt gave it>
- **Project:** <the project's name>
- **Question:** <the question, one line>
- **Verdict:** <the answer, one line>
- **Confidence:** <high | medium | low>
- **Recommendation:** <build | do-not-build | needs-decision | more-scouting>
- **Follow-up:** <card title> — <one-line summary>
- **Sources:** <where you looked, comma-separated, or none>

## Question
## What was found
## Evidence
## Recommendation
## Open questions
```

The block is the lines straight under the title, with no blank line inside
it. Every key is required except `Follow-up`, which is one line per card
you would suggest — none, one or several; Promote titles the build card
after the first. `## Evidence` cites paths and line numbers.
`## Recommendation` may recommend implementation and authorises none.

Then check the shape and fix the report until the checker prints `ok`:

```
python3 .claude/skills/scout/scout_check.py scout/<YYYY-MM-DD>-<slug>/report.md
```

It prints one problem per line otherwise (`missing Verdict`, `heading
"## Evidence" out of order`, …). Dark Army runs the same check when you
attach, and refuses a report under `scout/` that fails it.

**The `scout/` folder is git-ignored**, so the report exists only in this
checkout: a baseline worktree, a scratch copy of the project, another
session's worktree or another machine never has it. Every hand-off quotes
the **absolute** path, which is what the card stores and what Promote
writes into the build card's notes.

## Phase S3: attach and close

Call `mcp__dark-army__dark_army_attach_report({ path: "<abs path>" })` once,
with the report's absolute path, then
`dark_army_close_card({ note: "Report: <abs path> — <verdict>" })`. If the
attach is refused for its shape, fix the report until the checker prints
`ok` and attach again. If `dark_army_attach_report` is absent (an older
channel copy), say so, print the path, and still close with the path in
the note. A session started before the rename carries the same verbs as
`mcp__bob__bob_*`; a session keeps the tool list it was born with.

Never `dark_army_attach_plan`. Never `dark_army_add_card` for the same finding —
Promote is the person's.

## Phase S4: close out

Run `bash .claude/skills/ship/close-out.sh` (it closes nothing), end your last
message with its line verbatim, and stop. Only a person's request in words
earns `--close`.
