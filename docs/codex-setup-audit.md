# Codex agent setup audit — 23 September 2026

The user requested direct repairs without invoking skills or launching the
ship crew. This was an inspection of instructions, generated files and their
consumers. No ship run, delegated agent, board action, commit or installation
was performed. The audit establishes concrete setup defects; it does not
establish a measured provider speed or error-rate comparison.

## Improvements applied

| Problem | Repair |
|---|---|
| Fresh contexts requested in prose, but Codex defaults to inheriting the conversation | Require canonical `agent_type`, unique `task_name`, explicit `fork_turns="none"`, and a neutral packet containing absolute paths. Reviewers never inherit the implementer's conversation. |
| Claude-style orchestration leaves Codex to guess tool arguments and repair behavior | State native collaboration calls, keep them outside `functions.exec`, retain agent ids, and use `followup_task` to restart an idle implementer. Reuse never resets repair counters. |
| Automatic `/ship` routing also applies to a specialist's assigned task | Explicitly limit specialists to their role; direct user instructions and requests to avoid skills override automatic routing. |
| Repeated root reads, transitive document loads and banner reads | Count complete instructions already in context as read, select long-form links by subject, reread only changed or missing content, remove duplicated root guidance, and use one-line Codex stage announcements. Mandatory subject documents still load whole. |
| Long waits conflict with progress requirements | Bound waits by the harness and 60 seconds, relay real results, and report progress without waking workers with status pings. |
| Generic workflow claims blind verification while allowing the coordinator to verify itself | Remove both inline fallback paths; require a callable independent verifier, including command-only criteria. |
| Fast lane skips the audit, but the close condition requires its verdict | Accept an explicitly justified fast-lane skip, while retaining judgment floors and all required checks. |
| Local Codex entry says to close the terminal; shared contract says leave it open | Correct the entry and stale generic agreement. Remove the old duplicate clear-session workflow in arpg-web. Port the existing leave-open helper to starter-pack. |
| A graph query can substitute for impact, and UNKNOWN says to grep then proceed | Require upstream impact; text evidence must bound an unresolved change before proceeding. Record unresolved dependencies instead of treating zero callers as safety. |
| Documentation names missing generators, obsolete models and unavailable card-close behavior | Name the actual managed renderer, describe the current role policy, and decide card actions from callable tools and their replies. |

Models, reasoning settings, sandboxes, full-suite requirements, baseline
protection, review triggers and retry budgets were preserved. The write changes
are instructions and template synchronization, plus copying the existing
close-out helper into the older starter-pack source.

## Replication

- **Dark Army:** root guidance, Codex adapter, shared references and setup docs.
- **Vendored template:** generic agreement, adapter, common and implementation
  references, and the Codex configuration's generator comment. This is the
  source future Dark Army builds package.
- **The enrolled projects:** the three projects in the local agent-pack
  ledger. Applied bounded replacements to their existing files;
  both `.claude/skills/ship` and `.agents/skills/ship` copies match. Kept
  project rules, model selections, MCP settings and unrelated dirty work.
- **starter-pack:** applied the relevant changes to its older monolithic
  workflow and agreement, retaining its actual generator tooling. It has no
  fast lane, so its mandatory bug-audit close condition stays.

The installed Dark Army app was not rebuilt. Its older bundled template can
restore older managed instructions on a later resync; a future app build must
include this checkout's updated template. Existing sessions may retain loaded
instructions; the changes are intended for new runs. No live reload is claimed.

## Validation

- **193 local tests passed:** provider parity, context routing and reading
  budget, agent briefs, pack rendering, and root-document bounds.
- **31 starter-pack checks passed:** all three rendered profiles, mirrored
  skills, agent shims, read-only roles, and positive/negative plan fixtures.
- All three enrolled projects passed copy-integrity and instruction checks;
  their Codex configuration and the local role TOMLs parsed successfully.
- The copied close-out helper passed `bash -n`; local and starter-pack diffs
  passed `git diff --check`.
- Mandatory scenario instruction loads total **1,422,554 bytes**, below the
  unchanged **1,423,725-byte** ceiling. This is a static count, not a runtime
  token or speed measurement.

The first local check exposed exact-wording contract failures and added reading
weight. Those were repaired in the instructions, without weakening tests or
raising the budget. The final local run passed in 1.61 seconds.
Repository contract tests and rendered fixtures validate instruction consistency
and copy integrity, not model compliance. No live workflow benchmark was run.

## Further improvements to evaluate

1. **One broad test run per unchanged tree.** Currently the implementer and
   verifier each run the full suite. Measure a version where the implementer
   runs focused regression tests and the fresh verifier owns the full suite.
   Keep all acceptance checks and verify fault detection before adopting it.
2. **Smaller subject documents.** Dark Army's scope map still pulls large
   host/panel contracts, and unknown paths select all subjects. Split those
   contracts by real module boundaries and update routing and coverage tests
   together; do not merely omit required context to hit a token target.
3. **Measure model and reasoning choices.** Compare equivalent tasks with the
   same models, tool access and starting tree. Record time to first edit,
   stage duration, spawned context size, test commands, retries and findings.
   Tune a role only when its measured accuracy and latency support the change.

These are proposed follow-ups, not claims that those reductions were applied.

## Evidence limitations

GitNexus was one commit behind and did not index the Markdown workflow or
shell template targets: upstream impact returned UNKNOWN. Text references
confirmed the local adapters, pack renderer and contract tests consume the
edited files. No indexed Python/Swift function was edited by this task, and
UNKNOWN was not reported as a low-risk graph result. Concurrent product-code
changes in the shared checkout were excluded from this task.
