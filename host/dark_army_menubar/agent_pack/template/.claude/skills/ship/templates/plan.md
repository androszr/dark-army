# <Title>

<!-- A list, not five lines of bold text. A single newline is not a line break in
     Markdown — every renderer joins those lines into one run-on paragraph. `-`
     is what makes them separate lines. -->

- **Date:** <YYYY-MM-DD>
- **Slug:** <slug>
- **Status:** draft | accepted | implemented
- **Surfaces:** <the surfaces this touches — screen, API, data, background job,
  config — all that apply>
- **Stages:** <the specialists this card's dispatched session can still run, in
  order, separated by ` | `. An implementation card normally uses
  `{{P}}-implementer | {{P}}-verifier | {{P}}-bug-auditor`; add a domain
  reviewer from the Reviewers table in docs/context.md only when this plan
  requires that audit. Do not list `{{P}}-planner` on an implementation card
  because planning finished before the card was filed.>
- **Area:** <slug>
<!-- Area: backbone | desk | pocket | ledger | play | conductor | gate | universal. Choose the area served by the work; a lead grants no permission. -->
- **From report:** <path, or omit the line>
<!-- From report: the research report this plan is built on, when the card or idea names one; the planner reads it first. -->
- **Who benefits:** <who this work is for, one short line>
- **Intended benefit:** <what good it should do for them, one to three sentences on one line>
- **Success criterion:** <one observable sentence a person could check to know it worked>
<!-- Objective: the card's Objective: block verbatim when one was given; otherwise your own reading of the idea, in the person's words. Dark Army copies these three lines onto the card's empty objective boxes when the plan is attached, so a placeholder or NONE must not be left here. -->

<!-- ================================================================
     PLAIN-LANGUAGE ZONE — this is what the user reads on the card.
     Register: everyday language a non-programmer follows on first
     read. Banned here: file paths, function/class names, framework
     names, selectors, routes, commands, backticks, version numbers,
     acronyms the user didn't use. Say "the holdings screen", not
     "the HoldingsView".
     ================================================================ -->

## What this does

<2–4 sentences. What changes for the person using the product, and why it is
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

- <Observable outcome — "the total at the top updates the moment a lot is added".>
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
`src/lib/example.ts:120`. Note anything in docs/context.md that constrains the
design, including anything already tried and reverted.>

## Files to change

| File | Change |
|---|---|
| `<path>` | <what and why> |

## New files

| File | Purpose |
|---|---|
| `<path>` | <what it holds> |

## Steps

1. <Ordered, concrete, independently completable. Name the module, the function,
   the constant. "Add the total" is too vague; "add `totalValue()` to
   `src/lib/positions.ts`, computed from the lots the engine already loads,
   rendered by the existing summary row" is right. Tag each step with the
   plain-language step it realizes, e.g. "(plain step 2)", so the two zones stay
   in lockstep.>

## Risks & footguns

- <What is likely to go wrong here, and the specific thing to watch for. Pull
  from the profile's conventions in docs/context.md: money precision, auth
  gates, persisted-state compatibility, background work, permissions.>

## Test plan

- <Unit: which test file, which cases — including the edge cases that make the
  state wrong rather than just absent.>
- <Manual: numbered steps, on which surface, with what expected
  observation — and the one line saying why a test could not make it.>

## Acceptance criteria

Each must be checkable by running something. Prefix human-only checks `MANUAL:`.
This list is what the blind verifier executes — it stays fully technical; its
human-readable counterpart lives in `## How you'll know it worked` above.

**A `MANUAL:` criterion has to earn its place.** It is legitimate only where
the observation genuinely cannot be made in-process — a real screen, a real
device, a second real terminal, a timing you can only feel. Everything else has
a seam (a stubbed network call, a fake path, a store reopened on a temp file, a
snapshot fixture), and the plan must use it and **name it**. More than two
`MANUAL:` criteria is a plan that has not looked hard enough, not a hard
problem — say so and go back to the seams.

The ones that survive are written as **numbered steps somebody can follow**,
not as a hint about the shape of the test: what to open, what to press, what
they should see, in words a non-developer could act on. Then one line beginning
`Why not automated:` — required, because a check that cannot say why it is
manual is one that should have been a test.

- [ ] `<test gate from docs/context.md>` exits 0
- [ ] <`command` → expected result>
- [ ] <`grep -c ...` → expected count>
- [ ] MANUAL: <one line saying what is being judged.>
      Steps: <1. open this → 2. press that → 3. expect this.>
      Why not automated: <the one sentence.>

## Out of scope

- <What this deliberately does not do, so the implementer does not wander and
  the verifier does not fail it for something it was never meant to cover.>
- Follow-up card: <later work worth a card. The planning run files it in
  Prep; the implement run never files it again.>

## Iteration log

<!-- Appended by {{P}}-planner on each `iterate`. Newest last. -->
