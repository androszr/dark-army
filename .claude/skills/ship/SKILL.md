---
name: ship
description: Turn a request for a change in Dark Army (async Python daemon,
  rumps/PyObjC menu bar, SwiftUI panel, Claude Code hooks) into a written
  plan on Dark Army's Kanban board, and stop there. Asks a short clarifying
  interview, spawns bc-planner to write a structured plan with acceptance
  criteria to plans/, runs a deterministic preflight, files the plan as a
  Backlog card and closes its own planning tab — it does not wait for approval and
  it does not implement. Implementation is a separate run, entered as `/ship
  implement <plan path>`, which is what pressing Start on that card
  dispatches. Use when the user says /ship, "build X", "add X to bob-
  companion", or describes a feature to implement.
---

# ship

> **Quick start:** `/ship <what you want to build>`. Assesses 0–3 questions,
> writes a plan to `plans/`, files it on Dark Army's Kanban board as a Backlog card,
> and closes its own planning tab. You press Start on the card when you want it built;
> that dispatches `/ship implement <plan path>` in a fresh session.

Flow, plan mode: **interview → plan → deterministic preflight → file the card →
close out**. Flow, implement mode: **implement → blind verify → bug scan →
conditional integration review → conditional security review → handoff**. A brief's model comes from Dark
Army's **Agent models** setting, written into the brief by the agent pack; a
brief with no `model:` line inherits the session's default.

**This file is the Claude adapter.** The workflow itself is written once, in
three references beside this file, and this file carries only what differs
by assistant. Read the reference for your mode **completely** before its
first phase:

| Mode | Read, in this order |
|---|---|
| Plan (the default) | `references/common.md`, then `references/plan.md` |
| Implement (`/ship implement <plan path>`, or a `Plan: <path>` first line) | `references/common.md`, then `references/implement.md` |
| Batch implement (a `/ship batch: implement …` first line — several Backlog cards started together) | `references/common.md`, then `references/implement.md`, whose `## Batch: several cards in one session` runs each card |
| Batch plan (a `/ship batch: …` first line — several Prep cards refined together) | `references/common.md`, then `references/plan.md`, whose `## Batch: several cards in one session` runs each card |
| Scout (`/ship scout <brief>`, an alias) | `.claude/skills/scout/SKILL.md` alone |
| A change of mode inside one session ("build it now" after planning) | the other mode's reference, before its first phase |

Nothing here repeats a rule the references state; where this file and a
reference disagree, the reference wins and this file is the bug.

## Args

`/ship <free-text idea>` — plan mode. Optional; if empty, ask once: "What are we building?"

`/ship implement <plan path>` — implement mode. Straight to Phase 6 on an
already-written plan. This is the form a board card dispatches.

`/ship batch: …` — plan mode for several Prep cards in one session, one plan
and one attach per card. The form Refine on several ticked cards dispatches.

`/ship batch: implement …` — implement mode for several planned Backlog cards
in one session, one card at a time. The form START n TOGETHER dispatches.

`/ship scout <brief>` is an alias: load and follow the scout skill
(`.claude/skills/scout/SKILL.md`).

## Modes

This skill has two entry points and they do not overlap.

**Plan mode — the default.** Anything that reads as a request for a change, with
or without the word `/ship`. It runs Phases 0–5b: interview, plan, preflight,
file the plan on Dark Army's Kanban board, close the session out. It **stops there**.
It does not implement, and it does not end its turn asking whether it may.

**Implement mode — `/ship implement <plan path>`, or a prompt whose first
non-empty line is `Plan: <path>`.** Skips straight to Phase 6 with that plan.
This is the mode a board card dispatches into: pressing Start on a planned card
opens a fresh session whose prompt is built from the card's `plan_path`
(`dispatch.start_prompt`), even when the stored notes are still the leftover
idea `attach_plan` never rewrote. Do not spawn `bc-planner`. Do not write a
second plan. The file it names is the whole brief.

**Why the split.** A plan that ends in "accept?" holds a terminal open and puts
the session in Dark Army's *Needs you*, which is the wrong bucket for it — nothing is
blocked, nothing is half-written, and there is nothing that has to be answered in
the next four seconds. The work is written down; it can wait for somebody who is
looking for work rather than somebody who is being interrupted. Splitting also
buys the implementer a context holding only the plan, instead of the interview,
the preflight and three rounds of planner output that produced it.

If the user explicitly says to implement now, in this session, do it — say in one
sentence that the usual route is the card, then load `references/implement.md`
and run Phase 6 on the plan you just wrote. An explicit instruction outranks
the default; a guess never does.

## Agent spawn visual convention

Before **every** agent spawn, read the corresponding banner from
`.claude/skills/ship/banners/` (Read tool or `cat`) and paste its
**verbatim** content as a fenced code block in your text response. The
paste must live in the text response, because shell output
collapses in the terminal scroll. One banner per agent, never batched.
Never generate a banner from memory. If the file cannot be read, show no
banner at all rather than an invented one.

| Agent | Character | Phase | Banner file |
|---|---|---|---|
| — (skill start) | — | 0 | `intro.txt` |
| `bc-planner` | Overwatch (Cipher's alter ego — chief of staff) | 3 | `planner.txt` |
| `bc-implementer` | — | 6 | `implementer.txt` |
| `bc-verifier` | — | 6f | `verifier.txt` |
| `bc-bug-auditor` | — | 6.7 | `bugauditor.txt` |
| `bc-integration-reviewer` | — | 6.8 | `integration.txt` |
| `bc-security-reviewer` | — | 6.9 | `security.txt` |

Re-dispatches (iterate cycles, gap fixes, `BLOCK` returns) re-show the same banner
for that agent — the banner marks *a spawn*, not *a phase*.

## What is Claude's here

- **Spawning:** `Agent({ subagent_type: "<role>", description: "<stage>: <card
  title or plan slug>", prompt: "<the handoff packet>" })`, one-shot; the
  packet fields are `references/common.md`'s **The handoff packet**.
- **Questions:** `AskUserQuestion`, for the interview and for the run-budget
  question alike.
- **The board:** `mcp__dark-army__dark_army_attach_plan`, `…_add_card`,
  `dark_army_close_card`, `dark_army_needs_manual_check` and a batch's
  `dark_army_next_card` are in every session; only the channel needs
  `server:dark-army`. Born before the
  rename, a session has them as `mcp__bob__bob_*`, and keeps that list.
  A leftover check is a file, `manual-check/<YYYY-MM-DD>-<slug>/check.md`,
  checked by `python3 .claude/skills/ship/manual_check.py`, flagged with its
  path and then closed (`references/implement.md`, Phase 7b).
- **The gates:** `bash .claude/skills/ship/gate.sh` — the dispatch row, a
  gate run with its log and its budget, the baseline replay by id, the three
  failure classes (`YOURS`, `PRE-EXISTING`, `IN-FLIGHT`) and the lane, one
  command each; `references/implement.md` says when.
- **Closing out:** a planning run's last act is `close-out.sh --plan`,
  which closes its own tab; otherwise `close-out.sh` never closes unasked;
  `--close`, only on the person's words, finds this session by a pid
  ancestry walk and sends one close.
