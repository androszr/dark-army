# <Title>

**Date:** <YYYY-MM-DD>
**Slug:** <slug>
**Status:** draft | accepted | implemented
**Surfaces:** <daemon | menu bar | panel | hooks | extension | tools — all that apply>
**Stages:** <the specialists this card's dispatched session can still run, in
  order, separated by ` | `. An implementation card normally uses
  `bc-implementer | bc-verifier | bc-bug-auditor`; add
  `bc-integration-reviewer` only when this plan requires that audit. Do not list
  `bc-planner` on an implementation card because planning finished before the
  card was filed. A planning brief whose next command is `/ship <idea>` uses only
  `bc-planner`.>
- **Area:** <slug>
<!-- Area: backbone | desk | pocket | ledger | play | conductor | gate | universal. Choose the area served by the work; a lead grants no permission. -->
- **From report:** <path, or omit the line>
<!-- From report: the research report this plan is built on, when the card or idea names one; the planner reads it first. -->
- **Who benefits:** <who this work is for, one short line>
- **Intended benefit:** <what good it should do for them, one to three sentences on one line>
- **Success criterion:** <one observable sentence a person could check to know it worked>
<!-- Objective: the card's Objective: block verbatim when one was given; otherwise your own reading of the idea, in the person's words. Dark Army copies these three lines onto the card's empty objective boxes when the plan is attached, so a placeholder or NONE must not be left here. -->

<!-- ================================================================
     PLAIN-LANGUAGE ZONE — this is what the user reads to approve.
     Register: everyday language a non-programmer follows on first
     read. Banned here: file paths, function/class names, framework
     names (rumps, AppKit, SwiftUI, asyncio), selectors, routes,
     commands, backticks, version numbers, acronyms the user didn't
     use. Say "the menu-bar strip", not "the NSStatusItem title".
     ================================================================ -->

## What this does

<2–4 sentences. What changes for the person watching their agents, and why it is
worth doing. Intent and outcome only — zero implementation vocabulary.>

## How I'll build it

<Numbered steps as plain sentences. Each step says what the user will be able
to see or do differently once it is done, plus one short "how" clause. Every
step here maps to one or more entries in the technical ## Steps below — same
plan at two altitudes, nothing invented, nothing lost.>

1. <What becomes visible or possible — by doing what, in one clause.>

## How you'll know it worked

<The acceptance criteria restated as things a person can observe. Mark every
item only the user can confirm by trying it with "(needs you to check)".>

- <Observable outcome — "the strip shows a red number the moment an agent asks a question".>
- <Observable outcome> (needs you to check)

---

## Technical detail

Everything below is the implementation record — read by the implementer, the
verifier, and the preflight. Full precision here: exact paths, symbols,
commands. The two zones are the same plan at two altitudes — nothing above may
exist only down here in spirit, and nothing down here gets summarized away.

## Idea

<One paragraph, in the user's terms. What they asked for and why.>

## Context

<What already exists that this touches. Cite real paths and symbols —
`host/dark_army_daemon/session_stats.py:120`. Note anything in CLAUDE.md, a
subject document or an earlier plan that constrains the design, including
anything already tried and reverted.>

## Files to change

| File | Change |
|---|---|
| `host/...` | <what and why> |

## New files

| File | Purpose |
|---|---|
| `host/...` | <what it holds> |

## Threading & surfaces

<Required for any plan touching host/dark_army_menubar/ or panel/.
For each piece of new work: which thread it runs on (AppKit main / daemon loop /
a worker), how it crosses (rumps.Timer, callAfter, run_coroutine_threadsafe),
and which existing source of truth it reads (_activity_counts(),
session_stats.categorize(), the agents snapshot). If it adds to the menu-bar
title, say where it sits in STRIP_LADDER and what is given up first.>

## Steps

1. <Ordered, concrete, independently completable. Name the module, the function,
   the constant. "Add the count" is too vague; "add `_mesh_badge()` in
   `host/dark_army_menubar/app.py`, rendered from the `mesh` field the agents
   snapshot already carries, appended by `_compose_strip()` as a new
   `STRIP_LADDER` rung above the usage cluster" is right. Tag each step with the
   plain-language step it realizes, e.g. "(plain step 2)", so the two zones stay
   in lockstep.>

## Risks & footguns

- <What is likely to go wrong here, and the specific thing to watch for.
  Threading, eviction timing, strip width, rumps menu keys, ragged panel decode,
  py2app resource lists, hook handler purity, persisted-state compatibility.>

## Test plan

- <Unit: which file under `host/tests/`, which cases — including the edge cases
  that make the state wrong rather than just absent.>
- <Manual: exact steps, on which surface, with what expected observation.>

## Acceptance criteria

Each must be checkable by running something. Prefix human-only checks `MANUAL:`.
This list is what the blind verifier executes — it stays fully technical; its
human-readable counterpart lives in `## How you'll know it worked` above.

- [ ] `cd host && .venv/bin/pytest -q` exits 0
- [ ] <`command` → expected result>
- [ ] <`grep -c ...` → expected count>
- [ ] MANUAL: <exact steps, exact expected observation, on which surface>

## Out of scope

- <What this deliberately does not do, so the implementer does not wander and
  the verifier does not fail it for something it was never meant to cover.>
- Follow-up card: <later work worth a card. The planning run files it in
  Prep; the implement run never files it again.>

## Iteration log

<!-- Appended by bc-planner on each `iterate`. Newest last. -->
