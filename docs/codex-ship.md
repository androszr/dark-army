# Diagnose ship in Codex

Check the failing stage before changing configuration. A discovered skill, a
callable role, working graph storage and an attributable board action are
separate facts. None proves the others. Preserve the exact invocation, surface,
stage and error when reporting a failure.

1. **Skill discovery.** Inspect the session's discovered skills and read the
   loaded `ship` entry. In this environment the displayed default prompt is
   `$ship`; the repository's board dispatch text uses `/ship implement <plan>`
   (or begins with `Plan: <path>`). Do not assume every Codex surface has the same
   slash menu. A named existing plan enters implementation, without replanning.
2. **Canonical roles.** Inspect the actual callable `agent_type` roster. Plan
   mode needs `bc-planner`; implementation needs `bc-implementer`, `bc-verifier`
   and `bc-bug-auditor`. Review boundaries additionally require
   `bc-integration-reviewer` and/or `bc-security-reviewer`. A TOML file or a
   nickname is not evidence of native role exposure. Name any missing role and
   stage before dependent work; keep the plan/card and report incomplete. Never
   substitute a default agent or inline specialist. Local definitions are under
   `.codex/agents/`, with authoritative `.claude/agents/` briefs. See
   [the crew](agents.md). New definitions need a fresh session.
3. **Questions.** Use only a question route callable and permitted by the current
   session: `request_user_input` where the collaboration mode allows it,
   `request_user_input_async` when available, or the skill's plain-text fallback.
   Registration of an async question is not an answer. Keep settled assumptions;
   do not require Claude's question tool in Codex. An implement run has two
   questions of its own, both through these same routes: the spent-budget
   question (Phase 6.5 — continue, stop or hand back) and the out-of-scope
   escalation (Phase 6.7 — a serious finding outside the plan, put in five
   parts with fix here, file it or stop); beside them, two failed verify cycles
   (Phase 6f) stop the run and surface to the person. None is decided by the
   run itself.
4. **Graph.** Bind the repository, check freshness, then query/context and
   upstream impact before changing existing symbols. Storage errors, `UNKNOWN`,
   partial or truncated results remain unresolved. The existing runner supports
   `node .gitnexus/run.cjs analyze --force --index-only`; no dependency upgrade is
   implied. Use a compatible already-installed runner if necessary, and record
   which one answered. Before an authorized commit, run all-changes detection;
   compare mode cannot cover an uncommitted working tree on `main` by itself.
5. **Board attribution.** Attach the plan first. Only a confirmed no-refinement
   result permits creating a card. An absent tool, an ownership refusal or an
   unknown reply permits no generic board write, guessed identity or duplicate.
   Report the actual column returned. Missing manual-check tools leave the card
   open with exact steps; a report is never evidence that a card reached Done.
6. **Optional terminal close.** Close-out closes nothing by default. Only an
   explicit `--close`, on the person's request, uses the attachment receipt,
   which a receipt older than ten minutes refuses. The helper checks exact
   identity, attempts once and reports a refusal. Never retry or send Codex
   `/clear`. Past ten minutes, point the person at Dark Army's Close control,
   which closes the tab and finishes the card. Changing the checkout script
   does not update the installed copy.
7. **The shunt guard needs one-time trust.** Dark Army writes
   `~/.codex/hooks.json` (the shunt group on `PreToolUse`, matcher `shell`
   among others) on every launch, but Codex runs no user-layer hook until
   the person trusts hooks once in the TUI; until then Codex is skill-and-brief
   only, and a large `cat` is refused by nothing. To tell the guard is armed:
   start Codex in an enrolled project and ask it to `cat` a file over the
   threshold — the refusal names the shunt skill. The line `warning: loading
   hooks from both <home>/hooks.json and <home>/config.toml` appears only
   when hooks are declared in both files; with `hooks.json` alone it does
   not, and its absence is not evidence the hook is trusted.

Development packaging uses `cd host && ./build.sh --allow-untagged`. It bypasses
only the version gate; component freshness remains strict. Release builds still
require a clean tagged checkout. Neither an install nor a release follows from
ordinary implementation authorization.

## Execution contract repaired on 23 September 2026

The common ship reference now specifies the native Codex spawn explicitly:
canonical `agent_type`, unique `task_name`, `fork_turns="none"`, and a neutral
packet with absolute paths. Repairs use `followup_task` on the existing
implementer; reviewer contexts stay fresh. A specialist never recursively
starts the workflow. Already supplied complete instructions count as read;
linked long-form contracts are selected by the task's subject.

Codex uses one-line stage announcements; banners are optional and unchanged
ones are read once. The coordinator reports progress during bounded waits
without pinging helpers. The generated pack no longer allows inline blind
verification, and the fast lane can close a checked card without demanding a
verdict from the audit it legitimately skipped. Direct user instructions take
precedence over automatic skill routing. Models, sandboxes and the independent
full test runs were preserved. See `docs/codex-setup-audit.md` for scope,
replication, remaining costs and validation. No speedup or error-rate reduction
has been measured by this instruction-only repair.

## Evidence for the 19 September 2026 repair

- **Confirmed defects repaired in the checkout:** local Codex security stage,
  banner and seventh role definition; Claude's inline-verifier exception;
  development packaging command; incomplete compare-only commit instruction.
  Both skills now inspect callable roles before dependent work and review the
  captured baseline delta, including staged and new content. Negative tests
  guard the actual local contracts separately from generated pack behavior.
- **Graph:** the original storage mismatch was version 43 versus engine 42.
  Rebuilt with the existing cached GitNexus 1.6.12 runner (no install/upgrade),
  `analyze --force --index-only`: 36,900 nodes, 134,963 edges, 586 flows in
  35.6 seconds. CLI status matched HEAD `a56b334` and all 1,052 covered files;
  MCP's cached freshness still said four commits behind. Repository query and
  `workflows` context succeeded. Upstream file impacts for the two test modules
  returned `UNKNOWN`, zero callers/processes: pytest collection and CI text
  confirmed their entry points, but this is not a LOW-risk graph verdict.
  Existing function bodies in the two planned contract modules were preserved;
  only new tests/helpers were added there. The gate repair below changes one
  existing test outside those modules.
  The index skipped oversized `daemon.py` and truncated process discovery;
  no runtime code changed, and this is not a global graph all-clear.
- **Native capability at implementation start:** canonical implementer, verifier
  and bug auditor were callable. `bc-security-reviewer` was absent. Adding its
  TOML cannot change this session's frozen roster. Final mechanical security
  content triggers match the workflow/test changes themselves, so the security
  review remains incomplete pending the fresh-session check below; it is not
  reported as skipped or passed. No runtime integration boundary changed.
- **All-changes graph check:** `detect_changes(scope="all")` reported 56 changed
  symbols, 37 affected processes and 20 changed files, with CRITICAL aggregate
  risk and no partial/truncated flags. This includes pre-existing panel/phone
  changes and concurrent `agent_report.py` edits, not an isolated verdict on
  this task. New helpers are not mapped until reindexing. No commit is authorized
  or attempted; the result is not an isolated clean verdict.
- **Automated checks:** local contract tests 44 passed; unchanged close-out,
  pack-render and build-check regression tests 248 passed in 180.21 seconds;
  ruff and compileall passed. First full host run: 7,867 passed, two failures
  in concurrently added panel/phone `Areas.swift` UI contracts (504.42 seconds).
  Both exact failing tests passed after their owner's concurrent fixes; these
  files were not edited by this run. Document bounds passed after compacting
  this run's prose, then concurrent TODO additions exceeded the ceiling again.
  First isolated full run: 7,788 passed, 34 failed, 16 skipped. Thirty-two
  failures needed the existing ignored extension build artifact, which was
  copied into the isolated checkout without building or changing extension code.
  One build path-probe failure passed its exact rerun; its original cause remains
  unconfirmed. The other failure exposed the test-harness issue described below.
  Final independent run: **7,822 passed, 16 skipped, 3 warnings** in 863.84
  seconds, exit 0; lint and standing checks passed. Verifier verdict:
  **PASS-WITH-MANUAL**. These results cover the isolated task snapshot,
  not later concurrent changes in the shared tree. Current shared contract
  checks (52) and document bounds (8) also passed. Static tests prove
  instruction consistency, not model compliance or native role loading.
- **Review artifacts:** verification tree `/tmp/codex-ship-reliability-verify`;
  task-only patch `/tmp/codex-ship-reliability-isolated-delta.patch`, rebuilt
  against the captured baseline to exclude concurrent edits. These are
  temporary local artifacts. The extension artifact was copied without
  rebuilding; its SHA-256 is
  `299bec27f4a62523089d2868a778e1b5a0470406ab4cf3ad90d130b05310aeba`.
- **Independent bug audit:** SHIP, score 0, no functional blockers. Both
  providers' integration path triggers were false; their security content
  triggers were true. Integration review was skipped because no runtime
  boundary changed; required native security review remains incomplete.
- **Fresh-session result:** not yet run; no native security verdict is claimed.
- **Original reported symptom:** the exact failing invocation, stage and error
  remain unspecified and unreproduced. Repairing confirmed defects does not
  establish that the person's original failure is resolved.

## Narrow test-harness repair required by the full-suite gate

The existing `test_refinement_disposal_serializes_board_mutations[start]`
failed both in the isolated checkout and alone in the shared checkout. A
read-only diagnostic trace showed the fixture's root admitted by the cached
workspace lookup, the real `/opt/homebrew/bin/codex` discovered, and a valid
`spawn_agent` request after the close finished. Its last assertion counted
all transport requests as close requests. The fixture left workspace caching
and executable discovery dependent on the surrounding tests and machine.

The necessary scope addition changes only this test in
`host/tests/test_close_terminal_button.py`: stub project roots, executable
resolution and spawning; keep the mutex/backlog assertions while close is
blocked; require exactly one start after the close task completes, and exactly
one close transport request. Other mutation variants require no start. No
runtime dispatch or terminal behavior changed. Upstream impact was UNKNOWN,
with zero graph callers/processes; context, CI invocation and pytest collection
confirmed its four parametrized entry points, not a LOW graph verdict.
The affected module and build path probe passed together: 87 tests.

## Fresh-session smoke check (manual)

1. Open a new Codex session in this checkout after the shim change. Ask it to
   inspect the discovered `ship` entry and actual callable canonical roles,
   without implementing, filing a card or closing a terminal.
2. Ask it to run a bounded read-only review of these workflow changes with
   `agent_type = "bc-security-reviewer"`. Provide the accepted plan and captured
   baseline delta so unrelated dirty work is excluded.
3. Confirm the actual spawn uses that canonical role, the reviewer returns its
   own verdict, and it edits no files. A role-shaped task name is insufficient.
4. Record the session reference, loaded skill path, actual spawn, verdict and
   no-write evidence here. Until then leave the card open and the security
   review incomplete.

Why not automated: repository tests cannot reload another native host's frozen
role roster or prove that host accepted the canonical custom-role spawn.
