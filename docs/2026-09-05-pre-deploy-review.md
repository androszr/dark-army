VERDICT: SHIP

Comprehensive pre-build review of 5 September 2026 changes. No confirmed BLOCK;
two FIX findings and one WARN. This is a source/integration review, not evidence
that a newly installed build has passed its live smoke checks.

Scope: `14197ab` (last commit on 4 September) through `f9670b8`, plus the current
staged, unstaged and untracked files. Eight commits today; 221 tracked paths
changed against the baseline and 47 untracked paths, before this report.
Application code, installed files and services were not modified by this review.
Only this report and the TODO review record were written.

## Do next

### FIX — Reverse jump can open the wrong session when terminals share a name

- **Where:** `vscode-extension/src/extension.ts:159`, and `pickTerminal` at line 184.
- **Trigger and result:** Two terminals have the name `Grok`; the second is focused
  and is also `activeTerminal`. `terminalForTab` uses `terms.find`, selects the
  first terminal, and `pickTerminal` returns it before consulting `activeTerminal`.
  “Bob: Show this session” therefore opens the first session instead of the one
  the person is looking at. Prefix matching has the same ambiguity.
- **Evidence:** The integration reviewer transpiled the current extension in
  memory and reproduced PID 100 being selected with PID 200 focused. Installed
  VS Code exposes terminal titles as editor-tab names and does not guarantee
  unique terminal names. The existing numbered-name test gives the terminals
  different names and does not exercise this case.
- **Fix:** Resolve only unique name matches. Retain ambiguity and use a trustworthy
  focused/remembered terminal identity where available; otherwise open Bob without
  selecting a guessed session. Add the duplicate-name regression fixture.
- **Confidence:** confirmed. This affects navigation; the private terminal-close
  path uses separate identity, ancestry and tty guards.

### FIX — Catch up keeps a recovered launch failure unresolved

- **Where:** `host/bob_companion_daemon/decision_store.py:197–224`.
- **Trigger and result:** A card records `card_dispatch_failed`, is successfully
  dispatched on retry, then reaches Done. The failed-launch episode remains
  `status="open"` while a separate completion episode says the work completed.
  Catch up continues to sort the old failure with unresolved work; its old
  notification receipt also retains the unresolved status.
- **Evidence:** Reproduced with the current DecisionStore in a temporary database:
  failure → `card_dispatched` → `card_done` yields both an open failure and an
  answered completion for the same card. The daemon emits the successful dispatch
  event, but the store drops it at its allow-list. Completion uses a different
  episode slot, and the absence sweep only closes question/permission slots.
- **Fix:** On a proven recovery transition, close or supersede the matching active
  failure episode while retaining its historical text and receipt identity. Do
  not describe successful launch as successful completion. Cover retry, completion
  and old-receipt lookup in a regression fixture.
- **Confidence:** confirmed.

## Rollback warning

### WARN — An older build can reopen accepted work without invalidating acceptance

- **Where:** `host/bob_companion_daemon/board_outcome_store.py:58`.
- **Trigger and result:** The new build accepts a Done card. A rollback to the
  baseline build reopens it into In progress. Returning to the new build leaves
  `outcome_status="accepted"`, ledger `accepted=1`, and `rework_count=0`.
- **Evidence:** Integration reviewer reproduced the sequence in memory using
  both versions of BoardStore against the same SQLite connection. Older writers
  correctly preserve unknown columns, but `_connect_outcomes` does not reconcile
  changes they made to the known card fields.
- **Fix:** Document that outcome acceptance requires rechecking after a rollback
  with board edits, or introduce persisted observation/version information that
  detects older-writer changes and conservatively invalidates affected acceptance.
  An In progress column alone is insufficient: acceptance deliberately does not
  move a card.
- **Confidence:** confirmed. This requires a downgrade and later upgrade; ordinary
  forward migration and current-version reopen paths passed their tests.

## Verification

| Check | Result |
| --- | --- |
| Full host suite, `host/.venv/bin/pytest -q` | 4,838 passed; one existing-style asyncio-marker warning |
| Panel, `swift test` | 570 passed |
| iPhone, `xcodebuild test`, BobPhone scheme, iPhone 17 Pro simulator, signing disabled | 12 passed; test build succeeded |
| Relay, `node --test relay/tests/*.mjs` | 4 passed |
| `host/.venv/bin/ruff check .` | Passed |
| Hook imports and actual system-Python parsing | stdlib-only; Python 3.9 compatible |
| Shell syntax, cast parity, removed-platform scan | Passed; only known LVGL example comment |
| Extension production dependency audit | Zero vulnerabilities |
| Installed extension vs repository 0.1.12 VSIX JavaScript | Byte-identical |
| `git diff --check 14197ab` | One trailing blank line in `host/tests/test_session_title.py:263`; cosmetic |

Static integration review covered package imports/resources/signing order, hook
replacement, API Host/token/Origin gates, separate sealed home/away action lists,
private close authorization and final identity rechecks, confined job deletion,
atomic state writes, additive board migration and schema-marker preservation,
first-run/unknown-preference behavior, and panel ownership/EOF behavior. No further
confirmed findings survived the review.

Graph evidence: the index commit matched HEAD at the start and end. The compare
walk reports 2,856 changed symbols and 356 affected processes, with critical
aggregate reach. Upstream impact was run for all 318 production Python functions
whose bodies changed, including new modules. Results: 191 LOW, 8 MEDIUM, 9 HIGH,
9 CRITICAL and 101 UNKNOWN. UNKNOWN results received supplemental source-reference
searches; they are not evidence of unused code. Selected Swift/extension boundaries
also received impact queries and source tracing.

Graph limitations remain explicit: MCP cannot open this index because its database
reader supports storage version 42 while the index uses 43. The CLI/local backend
works, but compare results have a hard 1,000-symbol listing cap even when rerun
with limit 10,000. Counts and affected processes cover the larger set; the symbol
listing is not a complete or clean graph gate. Some new extension symbols are not
indexed. Direct source review and tests supplement these gaps; this report does
not claim complete graph coverage or a commit-ready graph check.

## Checks for the subsequent rollout

1. Run the strict release build without `--dev`; inspect actual frozen module
   imports, bundled panel/resource bundle, VSIX and signature. The simulator and
   panel test builds above are not a frozen macOS app validation.
2. After installation/restart, verify the installed hook/helper versions, one
   daemon and one owned panel, live API/SSE updates, and reload editor windows so
   already-running 0.1.11 instances pick up 0.1.12. Exercise duplicate-title reverse
   navigation explicitly if FIX 1 is deferred.
3. Coordinate the relay and phone updates with the Mac. On a physical paired
   phone, check sealed home and away reads/actions, locked/cold notification taps,
   grouped receipts, stale-question read-only behavior, and Catch up after restart.
4. Exercise the Mac outcome objective/accept/revise flow and confirm the same
   evidence/status on the phone. Check a lost-reply card-create retry produces one
   card and no second refinement.
5. Check private refinement-terminal closure in an explicitly authorized session;
   no real close, stop, dispatch or delete action was issued by this audit.

No finding cards were filed. The two FIX findings are ready to offer as Backlog
work if the user chooses them.

## Authorized follow-through

After the review, the user authorized fixing its recommendations, rebuilding and
restarting Bob, committing all changes, pushing to the remote, and TestFlight.
Both FIX findings are now implemented with regression coverage. Duplicate title
navigation refuses ambiguity; successful dispatch/refinement, plan attachment and
Done supersede active launch failures without erasing evidence or receipts.
`CONTRIBUTING.md` now documents acceptance rechecking after rollback with edits.

Independent fix verification: **4,855 host tests**, **570 panel tests**, relay
**4 tests**, extension build, Python compilation and ruff all passed. The earlier
12 iPhone simulator tests remain applicable: these review fixes change no iOS code.
Two standing verification discrepancies prompted a repair round: the hook's
`urllib.parse` import is standard library and works under actual Python 3.9; the
whitelist now acknowledges it. Quit now waits for panel/daemon shutdown on a
worker and queues AppKit termination back to the main thread. The extension
runtime version and manifest both report 0.1.13.

The graph listing cap was resolved for the precommit check by running an ephemeral
copy of GitNexus's backend with only its presentation limit raised from 1,000 to
100,000. Its traversal and risk rules were unchanged; the temporary backend was
removed afterward. Fresh index + complete staged/working-tree analysis returned
1,835 changed symbols and 154 affected processes, critical aggregate reach, with
`partial=false` and `truncated=false`. A final check follows the repair round.
Installed-runtime and distribution results are recorded in the rollout section
when those gates complete; the original review above remains historical evidence.

Final repair verification: **4,872 host tests passed** (one existing marker
warning), including the concurrently completed waiting-session eviction fix.
Quit worker boundaries, recovery receipts, navigation ambiguity, runtime version,
actual Python 3.9 hook portability and waiting/finished/Grok behavior pass.
The final strict build succeeded; deep signature verification and frozen imports
passed. Integration verdict **CLEAN**: all 58 daemon and 17 menubar source files,
170 panel resources, 406 icons, close-out helper and sole 0.1.13 VSIX match the
checkout. The final uncapped graph analysis lists all **1,867 changed symbols**
and **155 affected processes**, critical aggregate reach, without partial or
truncated result flags. No new runtime edits occurred during final verification.

## Installed rollout — 5 September 2026

Adversarial audit: **SHIP, score 0**, no confirmed finding; focused 223 tests pass.
The verified app was installed and restarted. Previous daemon 15551 exited;
new daemon **2012** owns exactly one panel **2042**, and both pid files agree.
Prior app retained at `/tmp/bob-before-rollout.app`; private SQLite/JSON backup
is under `~/.bob-companion/backups/pre-rollout-20260905-181703`.

| Area | Status | Detail |
| --- | --- | --- |
| Editor | Stale running windows | 0.1.13 installed; four existing windows still report 0.1.11. Their frontmost operation works; reload needed for new navigation/close contract. |
| Listeners | OK | Hook 19873 and API 19874 loopback only; paired-phone access uses its separate sealed endpoint. |
| Hook/helper | In step | Installed files byte-match source; executable hook parses under actual Python 3.9.6. |
| Hook wiring | OK | No missing events from HOOKS_CONFIG. |
| API | OK | State/usage/history/Catch up 200; SSE initial data frame; Bearer noop 403 and X-Bob-Token noop 400 as designed. |
| Upstreams | OK | Claude agents JSON and latest transcript shape parse. Expired session usage is correctly marked stale, weekly windows still future. |
| Panel | One, owned | PID 2042, parent 2012, installed nested executable; pid file agrees. |
| Relay | READY | Production `dpl_<redacted>` aliased to bob-relay.vercel.app; GET push 405, unauthenticated POST push 401. No real APNs push issued. |

Probe verdict: **DEGRADED only by stale editor windows**. Physical phone APNs,
Face ID and home/away interaction checks remain manual. TestFlight distribution
and remote CI results follow the authorized push.

## Remote distribution

All application changes were committed and pushed as `4fdea2e60b9b3dac58ff8b5cb1b71e5490bbf2ca`.
TestFlight **build 32**, workflow **33978779529**, completed successfully;
xcodebuild reported “Upload succeeded” and “Uploaded package is processing.”
This proves upload, not Apple processing completion or installation on a tester's
phone. No iOS source change is needed for the subsequent CI prerequisite repair.

Initial remote Tests run **33978779410**: panel succeeded, host **33 failed /
4,839 passed**. Every failure was a clean-checkout prerequisite: 32 tests could
not find ignored `vscode-extension/dist/extension.js`, and one could not read
ignored `.claude/agents/bc-bug-auditor.md`. Local verification had both available.
The CI repair builds the extension before pytest and tracks the authored project
agent role documents used by the skills, Codex agent configuration and tests.
The replacement run will establish the final remote verdict.

The installed Mac was built before the all-changes commit, so its baked label is
`main+0@f9670b8-dirty`. A post-push comparison confirms all 75 production Python
source files match the committed checkout exactly (excluding generated version
metadata); the label reflects build order, not stale application code.

Final remote verdict: **PASS**. CI prerequisite repair `656e3f4` was verified in
a fresh isolated export with no preexisting extension build/dependencies:
**52 affected tests passed**. Independent repair verifier passed. GitHub Tests
run [33979372223](https://github.com/androszr/bob-companion/actions/runs/33979372223)
completed successfully: **4,872 host tests**, lint and panel tests. TestFlight
[build 32](https://github.com/androszr/bob-companion/actions/runs/33978779529)
remains the successfully uploaded phone build; CI repair changed no app code.

Remaining manual items: reload the four old editor windows to activate IDE bridge
0.1.13; install the processed TestFlight build and check real APNs/locked Face ID
and paired home/away flows. No user agent terminals were restarted or closed by
this rollout. This final documentation-only record skips redundant CI; application
and CI configuration are covered by the successful run above.
