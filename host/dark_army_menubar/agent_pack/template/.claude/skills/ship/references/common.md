# ship — the common contract

Loaded completely by the adapter in both modes, before `references/plan.md`
or `references/implement.md`. What is here holds for every phase and every
assistant.

## Ground truth

`docs/context.md` is the project's ground truth and every agent reads it first.
Its **Identity table** names the source directory, the plans directory and the
**gates** (lint, typecheck, test, build — whichever this project has), and its
**Reviewers table** names which domain reviewer fires on which paths. Nothing in
this skill or in the agent briefs hardcodes a gate command; they all read the
table. When the table and the tree disagree, the tree wins — say so in the
report rather than silently working around it.

## Progress and role identity

Announce interview, planning, preflight, attachment, implementation,
verification, repair, bug audit, conditional integration review, security
review and handoff with their results. Use the canonical role in Codex `agent_type`
and name the card title or plan slug. State skipped stages and their reasons.
Helpers report at a gate result and at completion, plus blockers; the coordinator relays
these in one line (`pytest-full 11 failed`) and gives a brief update at least
once a minute during long waits; "still working?" from the person means that
relay was missed. A skipped stage is never announced as completed. If a
custom agent is missing, state the missing role and stop that stage rather
than claim that specialist ran.
Wait up to 60 seconds between updates, subject to the harness limit;
never send a status ping to a working helper. Banners are presentation, not evidence.
In Codex a one-line announcement replaces the ASCII banner; where a banner
is used, read it once and reuse it while unchanged.

## Codex execution

Use the callable tool schema, not Claude tool names or guessed wrappers:

- Spawn with `agent_type="<canonical role>"`, a unique `task_name`,
  `fork_turns="none"`, and `message` containing only the neutral handoff
  packet. Include the absolute repository root and scratch paths: a fresh
  child has no conversation to infer them from. Omit model and reasoning
  overrides; the role configuration owns those choices. `task_name` is a
  label, never a substitute for `agent_type`.
- Call native collaboration tools directly when exposed in that namespace;
  do not put them inside `functions.exec`. Store the returned agent id.
  Reuse the implementer with `followup_task` for a repair when available;
  `send_message` alone does not restart an idle agent. Keep the same ledger
  and increment the dispatch ordinal even when reusing the agent.
- Every reviewer starts fresh. Never use the default full-history fork for
  a reviewer, and never replace a missing reviewer by acting as it yourself.
  The helper executes its assigned role only; it does not invoke `/ship`,
  restart planning or delegate the entire workflow.
- A resumed helper reads changed inputs and missing ranges only. Complete
  root instructions already supplied by the harness count as read. Keep
  evidence paths and fingerprints; do not reload unchanged files merely
  because another instruction names them. Reviewers derive their own scope.
- Batch independent bounded reads and queries, with output sized to avoid
  truncation. Wait for builds to finish before editing their inputs. Run
  independent read-only reviews together only against a stable delta; no
  edits until all those reviews return. After repair, rerun affected checks.
- Use the question tool actually available in the current mode. An async
  question is pending until answered; continue independent work meanwhile.
  Never turn a workflow default into a new approval request when the person
  already authorized the action. A missing capability is reported once.


## Phase 0: preconditions

0. Announce the mode. Codex uses one line; other assistants may show `intro.txt`.
1. `git rev-parse --abbrev-ref HEAD`. If it is the default branch: **this user
   works directly on it.** Note it in one sentence and continue — do not block,
   and do not create a branch for them. Ask only if the change is unusually risky.
   If `HEAD` is a `card/` or `batch/` branch and `git rev-parse
   --show-toplevel` sits inside a `.worktrees/` folder, Dark Army made this
   worktree for the card: this run owns that branch — commit to it as you go
   (after the project's own pre-commit checks, Phase 7), never to `main`, and never push. Its baseline is clean by
   construction.
2. **Snapshot the tree as a baseline patch, not as a path list.** Trees here are
   habitually dirty. Do not ask whether to proceed. Run, before anything is
   written (`$SCRATCH` is any scratch directory outside the repo):

   ```bash
   SCRATCH="$SCRATCH" bash .claude/skills/ship/gate.sh snapshot
   ```

   That writes `pre-ship.patch`, `pre-ship-staged.patch`, `pre-ship-status.txt`
   and `pre-ship-head.txt`. The patches are **text-only**: packaged builds,
   images and archives are excluded, because `git apply` cannot replay a
   binary hunk and one dirty artifact would mark the whole baseline
   unbuildable.

   A **path** list cannot express "this file, but only those hunks", so a plan
   that touches an already-dirty file — the normal case — would put the
   implementer in contradiction with its own dispatch. The baseline patch can:
   downstream agents diff the tree against it and attribute only the new hunks.
   Pass all three paths to every downstream agent, and tell them the rule: *files
   dirty at baseline and **not** in the plan's `## Files to change` are entirely
   out of scope; files dirty at baseline **and** in the plan are in scope only
   for hunks absent from the baseline patch.*
3. Confirm `docs/context.md` exists and has an `## Identity` table. If not, stop:
   the agents have no ground truth.
4. Confirm each gate named in the Identity table can run (the tool is installed,
   the dependency directory exists). A gate that cannot run is *skipped*, not
   passed, and any criterion that depends on it becomes MANUAL — say so.

**Working on the default branch changes what the review agents can see.** There
is no `main...HEAD` range, so `{{P}}-verifier`, `{{P}}-bug-auditor` and every
domain reviewer must each be told to audit the **uncommitted working tree**
(`git status`, `git diff`, untracked files) instead of a branch diff. Omitting
this makes them silently review nothing.

## What every agent reads, and how much

`docs/context.md` is the ground truth and every role reads it **whole**, every
run, beside `CLAUDE.md` / `AGENTS.md` at the project root. A role reads its
own brief whole. Everything else is read on need: the files the plan names,
the files the delta touched, and whatever those point at. **Uncertainty never
selects less** — a boundary you did not expect, a file you cannot classify or
conflicting instructions widen what you read, never narrow it. Keep every
full-file read bounded, record the path, digest and
ranges completed, and reread only what is missing or changed. A truncated tool
output is an incomplete read, never permission to assume the rest. Pass the
same rule to every helper you spawn.

The orchestrator does **not** pre-read the context document or the
references for a helper. It puts the selected paths in the packet; the
helper reads them.

## The handoff packet

Every stage is spawned with a **small, neutral packet** — paths, never
narrative — and a fresh context: the canonical role and `"<stage>: <card
title or plan slug>"`, `plan_path` and the plan's digest, `context_path`
(`docs/context.md`), the Phase 0 baseline paths and the rule about them, the
current changed-path list and its digest, unresolved scope questions in one
line each or `none`, and the role's own inputs (`ledger` and `dispatch` for the
implementer, `iteration` and, from iteration 2, `filed_followups` for the
auditor, `gaps` — in-scope findings only — on a re-dispatch).

**The blind verifier's packet omits the implementer's report, every claimed
pass result and every interpreted summary, entirely.** It reads the files and
runs the criteria itself. Never fork the whole conversation into the verifier;
on a harness that cannot isolate history, say so in the report and use the
supported fresh-role route before claiming blindness. Read access inside a
read-only role is never restricted. **Repairs reuse the implementer** where the
harness supports it, passing the concrete unresolved findings and the changed
delta only. A boundary discovered during a repair widens what is read and
invalidates every review that ran before it; the repair counters are unchanged.

**Big reads go to the shunt helper; reviewers are exempt.** The shunt skill
(`.claude/skills/shunt/SKILL.md`) sends a whole-file read or a boilerplate
write to a cheap helper, and a guard refuses a whole-file read over the
project's threshold on every assistant. The planner and the implementer
delegate a survey, a *where is X handled* question or a generated file,
never an edit, a debugging read or anything security-relevant; every packet
names the skill. The orchestrator opens a per-session exemption window
(`python3 .claude/skills/shunt/exempt.py on`) before spawning a reviewer
and closes it (`exempt.py off`) before any implementer re-dispatch; a
reviewer refused a read anyway runs `exempt.py on` itself — a reviewer
reads by itself, never through a summary.
