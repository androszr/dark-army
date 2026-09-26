---
name: ship
description: >-
  Plan, implement, verify, and audit a non-trivial Dark Army change with a
  board handoff between planning and implementation. An existing Plan path or
  /ship implement enters implement mode directly. Skip explanation-only work,
  tiny edits, releases, and installation alone.
---

# Ship

Run the repository's guarded delivery pipeline. Preserve the user's existing dirty worktree and never commit, push, install, or release unless explicitly requested.

**This file is the Codex and Grok adapter.** The workflow itself is written
once, in three references under the Claude skill directory, and this file
carries only what differs by assistant. Read the reference for your mode
**completely** before its first phase:

| Mode | Read, in this order |
|---|---|
| Plan (the default) | `.claude/skills/ship/references/common.md`, then `.claude/skills/ship/references/plan.md` |
| Implement (`/ship implement <plan path>`, or a `Plan: <path>` first line) | `.claude/skills/ship/references/common.md`, then `.claude/skills/ship/references/implement.md` |
| Batch implement (a `/ship batch: implement …` first line — several Backlog cards started together) | `.claude/skills/ship/references/common.md`, then `.claude/skills/ship/references/implement.md`, whose `## Batch: several cards in one session` runs each card |
| Batch plan (a `/ship batch: …` first line — several Prep cards refined together) | `.claude/skills/ship/references/common.md`, then `.claude/skills/ship/references/plan.md`, whose `## Batch: several cards in one session` runs each card |
| Scout (`/ship scout <brief>`, an alias) | `.claude/skills/scout/SKILL.md` alone |
| A change of mode inside one session ("build it now" after planning) | the other mode's reference, before its first phase |

Nothing here repeats a rule the references state; where this file and a
reference disagree, the reference wins and this file is the bug. The
preflight checklist, the plan template and the interview questions are this
directory's own copies (`PREFLIGHT-CHECKLIST.md`, `templates/plan.md`,
`templates/questions.md`); `tools/sync_ship_stages_template.py --check` keeps
the template's `Stages` block in step with the Claude copy — that script
checks the `Stages` block alone and is not a workflow synchroniser.

## Modes

Two entry points, and they do not overlap.

**Plan mode — the default** for a bare idea. Writes a plan, files it on the board, closes its own tab through `close-out.sh --plan` when the board names it the planning run (otherwise leaves it open), and stops. It does not implement, and it does not end its turn asking whether it may.

**Implement mode — `/ship implement <plan path>`, or a prompt whose first non-empty line is `Plan: <path>`.** This is what pressing Start on a planned card dispatches (`dispatch.start_prompt` builds it from the card's `plan_path`, even when the stored notes are still the leftover idea). Skip planning. Do not spawn `bc-planner`. Do not write a second plan. Read that file — it is the whole brief — and run Phase 6 onward from `.claude/skills/ship/references/implement.md` on it.

A card's leftover idea is not a brief to re-plan. If the prompt names an existing plan, that plan is the brief. An explicit "build it now" outranks the default; a guess never does.

`/ship scout <brief>` is an alias: load and follow the scout skill
(`.claude/skills/scout/SKILL.md`).

## Agent spawn visual convention

1. In Codex, announce each spawn in one line: `<role>: <task>`. ASCII banners
   are optional presentation, not a prerequisite for work. In Grok, read the matching banner from this skill's
   `banners/` directory (a file-read tool or `cat`) and paste its **verbatim**
   content as a fenced code block in the text response. The paste must live
   in the text response, because shell output collapses in the terminal scroll.
   One banner per agent, never batched. Never generate a banner from memory.
   If the file cannot be read, show no banner at all rather than an invented
   one.
2. Map each spawn to its banner file:

   | Agent | Character | Banner file | When |
   |---|---|---|---|
   | — (skill start) | — | `intro.txt` | Phase 0 |
   | `bc-planner` | Overwatch (Cipher's alter ego — chief of staff) | `planner.txt` | Phase 3 |
   | `bc-implementer` | — | `implementer.txt` | Phase 6 |
   | `bc-verifier` | — | `verifier.txt` | Phase 6f |
   | `bc-bug-auditor` | — | `bugauditor.txt` | Phase 6.7 |
   | `bc-integration-reviewer` | — | `integration.txt` | Phase 6.8 |
   | `bc-security-reviewer` | — | `security.txt` | Phase 6.9 |

3. Re-dispatches announce the role and repair. Cache an unchanged banner after
   its first complete read; do not reopen it on every spawn.

## What is Codex's and Grok's here

- **Spawning:** Codex spawns the project custom agent of the canonical name
  (`.codex/agents/<role>.toml`, `agent_type` on the wire); its shim tells it to
  read the markdown brief, the compact root and the mapped references. Grok
  spawns `spawn_subagent({ subagent_type: "<role>", ... })` against
  `.grok/agents/<role>.md`. Both hand over the handoff packet
  `.claude/skills/ship/references/common.md` states, and nothing else.
  In Codex set `fork_turns="none"` explicitly; use the native spawn and
  follow-up tools as described in the common reference. Never inherit this
  conversation into a reviewer.
- **Questions:** Codex uses `request_user_input` only when its current
  collaboration mode permits it, or `request_user_input_async` when available;
  Grok uses `ask_user_question`. With neither callable, the plain question in
  the final response plus `<!-- bob-tldr -->` and "Answer in the original Codex
  session", exactly as the references state.
- **The board:** Codex's restricted board MCP exposes `dark_army_add_card`,
  `dark_army_attach_plan`, `dark_army_attach_report`,
  `dark_army_close_card` and a batch's `dark_army_next_card` only;
  `dark_army_needs_manual_check` is unavailable,
  so an unchecked step is listed under `## Work done` and no
  manual flag is claimed. Grok has the board tools only where Dark Army registered
  them. A session started before the rename carries the same verbs as `bob_*`;
  a session keeps the tool list it was born with.
- **Closing out:** `bash .claude/skills/ship/close-out.sh` is the same script
  for all three assistants; `--plan` ends a planning run by closing its tab,
  and otherwise it never closes unasked. `--close`, only on the
  person's words, finds Grok by `GROK_SESSION_ID` and Codex by exact
  `CODEX_THREAD_ID` / `CODEX_SESSION_ID`, and never clears. A Codex close is
  refused ten minutes after the plan attachment; then point the person at
  Dark Army's Close control.
- **Bounded reading on a sandbox that cannot write `.agents`:** the references
  above live under `.claude/`, which the sandbox can read; a hand edit to a
  rendered copy elsewhere is overwritten by the pack and is never the source.
