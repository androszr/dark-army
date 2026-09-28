# ship — the common contract

The one authoritative statement of what every `/ship` run does regardless of
mode or assistant. Both local adapters — `.claude/skills/ship/SKILL.md` for
Claude and `.agents/skills/ship/SKILL.md` for Codex and Grok — load this file
**completely** before their mode's reference (`references/plan.md` or
`references/implement.md`), and nothing in an adapter repeats a rule stated
here; the adapter carries only what differs by assistant (tool names, the
spawn shape, its banner directory). A change of mode inside one session —
"build it now" after the plan was written — loads the other mode's reference
before its first phase. Generated copies of these files in other projects are
output of the agent pack, not a second source.

## Spawning an agent

Each assistant spawns a sub-agent its own way. The role text is always
`.claude/agents/<role>.md`, the spawn hands over the handoff packet stated
under **The handoff packet** below, and the canonical role name is the one on
the wire:

- **Claude** — `Agent({ subagent_type: "<role>", description: "...", prompt: "..." })`.
- **Codex** — the project custom agent of the same name (`.codex/agents/<role>.toml`);
  its shim tells it to read the markdown role file and the mapped references.
  Spawn it with the same prompt.
- **Grok** — `spawn_subagent({ subagent_type: "<role>", description: "...", prompt: "..." })`
  against `.grok/agents/<role>.md`, which likewise points at the markdown role file.

If the assistant cannot spawn a sub-agent at all, that role is **missing**:
see **Required capabilities**. Never run a specialist inline in this
session's context — the blind verifier in particular exists to have never
seen the implementer's account, and a session that did both has.

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

## Required capabilities

Before dependent work, inspect the actual callable agent roster in this session.
Files on disk and nickname labels are not evidence that a role is callable.
Plan mode requires `bc-planner`. Implement mode requires `bc-implementer`,
`bc-verifier` and `bc-bug-auditor`, plus `bc-integration-reviewer` and
`bc-security-reviewer` whenever the accepted plan reaches their review boundaries.
Report each required role as available or missing before implementation starts.
Re-evaluate conditional roles against the final delta using the review stages
in `references/implement.md`. A genuinely unnecessary stage is skipped with its reason.

If a required role is missing, name the role and affected stage, preserve the
plan and card, start no dependent implementation and report incomplete. If a
new conditional need emerges during work, stop before dependent work or a success
handoff. Never substitute `default`, run a specialist inline, or retry a missing
role repeatedly. A new shim requires a fresh session to prove native discovery.
Keep question-tool selection tied to session availability and settled assumptions.

The plan template's `- **Area:**` header names one of the eight area slugs;
it suggests an area without changing a person's existing card selection.

## What every agent reads, and how much

`CLAUDE.md` and `AGENTS.md` are the compact root contract and are read
**completely** by every role, every run. Beyond them a role reads its own brief
whole and the subject documents `docs/agent-context.json` selects — the union
of what the plan's `Surfaces:` header selects, what every changed or planned
path selects and the map's cross-cutting additions. **Uncertainty never selects
less**: an unmapped path, an unknown surface, an empty scope, conflicting
instructions or a boundary discovered mid-work selects the fallback, every
subject document. The planner starts from the idea and its surfaces and
widens on discovery; each reviewer derives the union from the plan and the
delta by itself, never from the implementer's selection. An unavailable
graph result (`UNKNOWN`, stale index, a Python name `impact` cannot
resolve) does **not** widen a verifier, auditor or door reviewer that
already holds `$SCRATCH/ship-delta-paths.txt` — those paths plus the
plan's surfaces are the union. A selected document is read whole; the map
chooses documents, never paragraphs.

The orchestrator does **not** pre-read the subject documents. It puts the
selected paths in the packet; the helper reads them.

Keep every full-file read bounded, record the
path, digest and ranges completed, and reread only the ranges that are missing
or the bytes that changed. A truncated tool output is an incomplete read, never
permission to assume the rest.

Pass the same rule to every helper you spawn, in the packet.

## Phase 0: preconditions

0. Announce the mode. Codex uses one line; other assistants may show `intro.txt`.
1. `git rev-parse --abbrev-ref HEAD`. If `main`: **this user works directly on
   `main`.** Note it in one sentence and continue — do not block, and do not
   create a branch for them. Ask only if the change is unusually risky.
   If `HEAD` is a `card/` or `batch/` branch and `git rev-parse
   --show-toplevel` sits inside a `.worktrees/` folder, Dark Army made this
   worktree for the card: this run owns that branch — commit to it as you go
   (after the GitNexus change check, Phase 7), never to `main`, and never push. Its baseline is clean by
   construction.
2. **Snapshot the tree as a baseline patch, not as a path list.** This tree is
   habitually dirty — 79 entries at the time of writing, including `daemon.py`,
   `app.py` and `Models.swift`, which are the centre of gravity of almost any
   change. Do not ask whether to proceed. Run, before anything is written:

   ```bash
   SCRATCH="$SCRATCH" bash .claude/skills/ship/gate.sh snapshot
   ```

   That writes `pre-ship.patch`, `pre-ship-staged.patch`, `pre-ship-status.txt`
   and `pre-ship-head.txt`. The patches are **text-only**: `*.vsix`, images
   and zip files are excluded, because `git apply` cannot replay a binary
   hunk (no full index line) and one dirty packaged extension would mark
   the whole baseline unbuildable. The committed `.vsix` stays the install
   lockfile; snapshot simply does not record a rebuild of it.

   A **path** list cannot express "this file, but only those hunks", so a plan
   that touches an already-dirty file — the normal case — would put the
   implementer in contradiction with its own dispatch. The baseline patch can:
   downstream agents diff the tree against it and attribute only the new hunks.
   Pass these paths and the captured file bytes to every downstream agent, and tell them the rule: *files
   dirty at baseline and **not** in the plan's `## Files to change` are entirely
   out of scope; files dirty at baseline **and** in the plan are in scope only
   for hunks absent from the baseline patch.*
   Also capture the tracked and untracked file bytes required by **Review delta**.
3. Confirm `CLAUDE.md` exists — it is the agents' ground truth. If not, stop.
4. Confirm `host/.venv/bin/pytest` exists. If not, say so and offer the setup
   line from CONTRIBUTING.md; the gates cannot run without it.
5. Note whether `swift` is on PATH. Without a toolchain the panel gate is
   *skipped*, not passed, and any panel-facing criterion becomes MANUAL.
6. If a task modifies a symbol, obey the GitNexus impact requirement in
   `AGENTS.md` before editing it. **One** upstream `impact` per named symbol, `repo` bound; `query`
   locates a symbol but does not replace impact. On `UNKNOWN`, inspect text
   callers and dynamic entry points; proceed only when that evidence bounds
   the change, otherwise report the unresolved dependency. Warn before any
   HIGH or CRITICAL change. Packet the evidence and source revision to the
   implementer; repeat only when the target or its dependencies changed.

**Working on `main` changes what the review agents can see.** There is no
`main...HEAD` range, so `bc-verifier`, `bc-bug-auditor`,
`bc-integration-reviewer` and `bc-security-reviewer` must each be told to
audit the **uncommitted working tree** (`git status`, `git diff`, untracked
files) instead of a branch diff. Omitting this makes them silently review
nothing.

## The handoff packet

Every stage is spawned with a **small, neutral packet** — paths, never
narrative — and a fresh context. The required fields:

- `role` — the canonical role, and `task` — `"<stage>: <card title or plan slug>"`
- `plan_path` and the plan's digest (`shasum -a 256`), so a plan edited
  mid-run is noticed rather than assumed
- `baseline_patch`, `baseline_staged`, `baseline_status`, `baseline_files`,
  `baseline_head` — the Phase 0 snapshot, and the rule about it
- the **current delta identity**: `$SCRATCH/ship-delta-paths.txt` and
  `$SCRATCH/ship-delta.patch` from **Review delta**, refreshed after every
  repair, plus their digest
- `context_path` (`CLAUDE.md`) and the reference **paths** the map selected
  (not their contents — the helper reads them), with the widening rule
  stated above
- unresolved scope questions, in one line each, or `none`
- the role's own inputs: `ledger` and `dispatch` for the implementer,
  `iteration` and, from iteration 2, `filed_followups` for the auditor,
  `gaps` — in-scope findings only — on a re-dispatch

**The blind verifier's packet omits the implementer's report, every claimed
pass result and every interpreted summary, entirely.** It receives the plan,
the context, the baseline artifacts and the delta identity, and it reads the
files and runs the criteria itself. Never fork this conversation into the
verifier: on a harness that can start a fresh context, do; on one that cannot
isolate history, say so in the run's report and use the supported fresh-role
mechanism before claiming blindness. Read access inside a read-only role is
never restricted — every reviewer may open any file and widen its own scope.

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

**Repairs reuse the implementer** where the harness supports it, passing the
concrete unresolved findings and the changed delta only — never the
reviewers' prose in full and never the whole earlier conversation. The
auditor's iteration ≥2 delta mode still includes plausible regressions. A new
boundary discovered during a repair widens the reference set and invalidates
every review that ran before it; the repair counters and the separate
integration/security `BLOCK` handling in `references/implement.md` are
unchanged by any of this.

## Review delta

Capture tracked working-tree bytes, index changes and untracked file bytes before
implementation, alongside status and the baseline patches. Pass these baseline
artifacts to every reviewer. Compare the final tree with that captured baseline,
including newly staged changes and newly created files/content. Existing dirty
hunks are the user's work; they are not this run's delta. If attribution cannot
be separated confidently, state the uncertainty and review conservatively.

Prepare `$SCRATCH/ship-delta-paths.txt` (one changed path per line) and
`$SCRATCH/ship-delta.patch` (zero-context added/deleted content) from that byte
comparison, including whole content for new files and deleted content for removed
files. Refresh both after every repair. Check `git diff`, `git diff --cached`
and `git ls-files --others --exclude-standard` against the saved baseline to
account for each path; these commands alone do not subtract existing dirty work.
Neither `main...HEAD` alone nor an unstaged-only diff covers this run.
The integration and security stages consume these same artifacts independently.

Independent read-heavy review agents may run in parallel only when their
inputs are stable. Do not let multiple agents edit the same working tree
concurrently.
