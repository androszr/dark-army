---
name: ship
description: Turn a request for a change in {{PROJECT}} into a written plan on
  Dark Army's Kanban board, and stop there. Asks a short clarifying interview, spawns
  {{P}}-planner to write a structured plan with acceptance criteria to plans/,
  runs a deterministic preflight, files the plan as a Backlog card and closes
  its own planning tab — it does not wait for an approval and it does not implement.
  Implementation is a separate run, entered as `/ship implement <plan path>`,
  which is what pressing Start on that card dispatches. Use when the user says
  /ship, "build X", "add X", "fix Y", or describes a change to implement.
---

<!-- This file is the canonical /ship skill. `.agents/skills/ship/` (Codex and
     Grok) is a byte copy produced by Dark Army's agent-pack sync. Edit here, then
     run the sync. A hand edit to the copy is overwritten on the next run and
     leaves the managed copies out of step. -->

# ship

> **Quick start:** `/ship <what you want to build>`. Assesses 0–3 questions,
> writes a plan to `plans/`, files it on Dark Army's Kanban board as a Backlog card,
> and closes its own planning tab. You press Start on the card when you want it built;
> that dispatches `/ship implement <plan path>` in a fresh session.

Flow, plan mode: **interview → plan → deterministic preflight → file the card →
close out**. Flow, implement mode: **implement → blind verify → bug scan →
conditional domain review → handoff**. A brief's model comes from Dark Army's
**Agent models** setting, written into the brief by the agent pack; a brief
with no `model:` line inherits the session's default.

**Three assistants, one text.** Claude, Codex and Grok all run this file. Where
a step depends on a tool only one of them has (a question widget, a board verb,
a sub-agent spawner) the step says so and names the fallback. Never invent a
tool; if the named one is absent, do the fallback.

## Args

`/ship <free-text idea>` — plan mode. Optional; if empty, ask once: "What are we building?"

`/ship implement <plan path>` — implement mode. Straight to Phase 6 on an
already-written plan. This is the form a board card dispatches.

`/ship batch: …` — plan mode for several Prep cards in one session, one plan
and one attach per card. The form Refine on several ticked cards dispatches.

`/ship batch: implement …` — implement mode for several planned Backlog cards
in one session, one card at a time (`dark_army_next_card` between cards). The
form START n TOGETHER dispatches.

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
opens a fresh session whose prompt is built from the card's plan path. Do not
spawn `{{P}}-planner`. Do not write a second plan. The file it names is the
whole brief.

**Why the split.** A plan that ends in "accept?" holds a terminal open and puts
the session in Dark Army's *Needs you*, which is the wrong bucket for it — nothing is
blocked, nothing is half-written, and there is nothing that has to be answered in
the next four seconds. The work is written down; it can wait for somebody who is
looking for work rather than somebody who is being interrupted. Splitting also
buys the implementer a context holding only the plan, instead of the interview,
the preflight and three rounds of planner output that produced it.

If the user explicitly says to implement now, in this session, do it — say in one
sentence that the usual route is the card, then run Phase 6 on the plan you just
wrote. An explicit instruction outranks the default; a guess never does.

**This file is the adapter; the workflow is written once in three references
beside it.** Read the reference for the mode you are in **completely** before
its first phase, and nothing here repeats a rule the references state:

| Mode | Read, in this order |
|---|---|
| Plan (the default) | `references/common.md`, then `references/plan.md` |
| Implement (`/ship implement <plan path>`, or a `Plan: <path>` first line) | `references/common.md`, then `references/implement.md` |
| Batch implement (a `/ship batch: implement …` first line — several Backlog cards started together) | `references/common.md`, then `references/implement.md`, whose `## Batch: several cards in one session` runs each card |
| Batch plan (a `/ship batch: …` first line — several Prep cards refined together) | `references/common.md`, then `references/plan.md`, whose `## Batch: several cards in one session` runs each card |
| Scout (`/ship scout <brief>`, an alias) | `.claude/skills/scout/SKILL.md` alone |
| A change of mode inside one session ("build it now" after planning) | the other mode's reference, before its first phase |

`gate.sh` beside this file is the implement mode's gate helper — the
attempt ledger, the delta, the baseline replay, the red-test classes and the
lane, one command each (`bash .claude/skills/ship/gate.sh` prints its
usage); `references/implement.md` says when each is run.

Where this file and a reference disagree, the reference wins and this file
is the bug. `.agents/skills/ship/` is a byte copy of this directory, references
included, so the relative paths resolve there too.

## Spawning an agent

Each assistant spawns a sub-agent its own way. The role text is always
`.claude/agents/<role>.md`, and the spawn always hands over the same inputs.

- **Claude** — `Agent({ subagent_type: "<role>", description: "...", prompt: "..." })`.
- **Codex** — the project custom agent of the same name (`.codex/agents/<role>.toml`);
  its shim tells it to read the markdown role file. Spawn it with the same prompt.
- **Grok** — `spawn_subagent({ subagent_type: "<role>", description: "...", prompt: "..." })`
  against `.grok/agents/<role>.md`, which likewise points at the markdown role file.

Before dependent work, inspect the callable canonical roster. Planning needs
`{{P}}-planner`; implementation needs `{{P}}-implementer` and `{{P}}-verifier`,
plus the bug auditor and domain reviewers when their stages apply. If a
required role is unavailable, report the role and stage once and leave the
work incomplete. Never substitute an inline specialist or a default agent.
Codex uses `fork_turns="none"` and the neutral handoff packet described in
`references/common.md`; a task label does not establish role identity.

## Agent spawn visual convention

In Codex, announce `<role>: <task>` in one line; ASCII banners are optional.
For other assistants, before an agent spawn, read the corresponding banner from
`.claude/skills/ship/banners/` (Read tool or `cat`) and paste its
**verbatim** content as a fenced code block in your text response. The
paste must live in the text response, because shell output
collapses in the terminal scroll. One banner per agent, never batched.
Never generate a banner from memory. If the file cannot be read, show no
banner at all rather than an invented one.

| Agent | Character | Phase | Banner file |
|---|---|---|---|
| — (skill start) | — | 0 | `intro.txt` |
| `{{P}}-planner` | Overwatch | 3 | `planner.txt` |
| `{{P}}-implementer` | Cipher | 6 | `implementer.txt` |
| `{{P}}-verifier` | Ledger | 6f | `verifier.txt` |
| `{{P}}-bug-auditor` | Hex | 6.7 | `bugauditor.txt` |
| any domain reviewer from the Reviewers table | Watch | 6.8 | `reviewer.txt` |

Re-dispatches announce the role and repair. Read a banner once per unchanged
file and reuse it on later spawns.
