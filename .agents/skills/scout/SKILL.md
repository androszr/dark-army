---
name: scout
description: >-
  Investigate or scout a question about Dark Army and write a report, never
  code or a plan: scout/<YYYY-MM-DD>-<slug>/report.md with a checked answer
  block, attached to the board card. Use for /scout (alias /ship scout) or a
  request to investigate or scout a question and write a report.
---

# Scout

`/scout <brief>` — investigate, write a report, attach it to this card,
close the card with the report's path in the note, and stop.
`/ship scout <brief>` is an alias for this skill.

The workflow is written once for every provider: read
`.claude/skills/scout/references/scout.md` **completely**, then follow it.
The report's checker is `.claude/skills/scout/scout_check.py`
(`python3 .claude/skills/scout/scout_check.py <report>` prints `ok` or what
is wrong).

No plan, no implementation. Codex's restricted board MCP exposes the two
verbs this needs: `dark_army_attach_report` then `dark_army_close_card`.
Promote — turning the report into a build card — is the person's.
