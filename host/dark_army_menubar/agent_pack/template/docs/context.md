# Project context — {{PROJECT}}

Ground truth for every agent in `.claude/agents/`. Read this first, before
exploring the tree. If something here contradicts the code, the code wins —
say so in your report rather than silently working around it.

Three sections here are read by machinery, not only by people, so keep their
shape: the **Identity** table (gates), the **Reviewers** table (which domain
reviewer fires on which paths) and the **Conventions** list (what the
preflight, the verifier and the reviewers enforce).

## Identity

| Field | Value |
|---|---|
| `src_dir` | `{{SRC_DIR}}` |
| Framework | <one line: what this is built with and where it runs> |
| Package manager | <e.g. pnpm 11 (Node 22) / Swift Package Manager / none> |
{{GATE_ROWS}}
| Plans | `plans/<YYYY-MM-DD>-<slug>.md` |
| Reference docs | <the docs a planner should read after this one> |

**Every gate must pass before any handoff.** Run them in the order listed —
the fast ones fail first. Agents read the commands from this table; none of
them hardcodes one.

## Reviewers

One row per domain reviewer. `/ship` Phase 6.8 greps the changed-file list
against each regex and spawns every reviewer that matches; `/review --deep`
hands the diff to the first one. The regex is a floor — a reviewer is also
spawned on judgment when the diff touches its domain through other files.

| Agent | Fires when a changed path matches |
|---|---|
{{REVIEWER_ROWS}}

## Architecture

<Replace this section with the real thing: the entry chain, the data layer,
auth, the external services, the background work. Cite real paths and
symbols. Record decisions with their date and the plan that made them, and
record what was tried and reverted — that is the expensive half.>

## Conventions

The project's own non-negotiables. Each is enforced by a preflight check, a
verifier grep or a domain reviewer; a plan that breaks one is a plan that will
not pass. The planner reads these before writing, the implementer before
coding.

{{CONVENTION_ROWS}}

## What the tests cannot see

The suite is hermetic. Green proves nothing about these surfaces, which is why
the domain reviewers and the audit skill exist:

{{BLIND_SPOT_ROWS}}

## Interview branches

`/ship` asks at most three questions from `.claude/skills/ship/templates/questions.md`.
Branches that the architecture above already settles should be answered from
this file rather than asked.
