---
name: scout
description: Investigate or scout a question about Dark Army and write a
  report — never code, never a plan. Writes scout/<YYYY-MM-DD>-<slug>/report.md
  with a checked answer block, attaches it to the board card and closes the
  card. Use when the user says /scout (or its alias /ship scout), or asks to
  investigate or scout a question and write a report.
---

# Scout

`/scout <brief>` — investigate, write a report, attach it to this card,
close the card with the report's path in the note, and stop.
`/ship scout <brief>` is an alias for this skill.

**This file is the adapter; the workflow is `references/scout.md` beside
it.** Read that reference **completely** before doing anything, then follow
it. It borrows two rules from the ship skill and names that file by path
when it does. The report's checker is `scout_check.py` beside this file:
`python3 .claude/skills/scout/scout_check.py <report>` prints `ok` or what
is wrong.

No plan, no `bc-planner`, no implementation. The board tools it uses are
`dark_army_attach_report` then `dark_army_close_card`. Promote — turning
the report into a build card — is the person's.
