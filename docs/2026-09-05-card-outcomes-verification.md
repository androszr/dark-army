# Card outcomes — final verification

Implemented the *card outcomes and evidence* plan on 5 September 2026.
Cards carry intended benefits and success criteria; explicit human acceptance
requires evidence. Project reports measure accepted distinct outcomes, rework,
observed waiting and attributable monetary cost with coverage disclosed.
Done and Reviewed do not grant acceptance. The phone reads the same reports.

## Evidence

The shared checkout contains concurrent unrelated work. Full tests and builds
used a fixed source snapshot at `/tmp/bob-outcomes-verified-89azkwql`, with
SHA-256 entries in `snapshot-manifest.json`. This includes the final audit fixes.
Outcome-specific sources still matched the checkout at close-out; later changes
to shared modules concern refinement terminal closure and decision capture.
The final focused host run also exercised the latest shared checkout.

| Check | Result |
| --- | --- |
| Full host `env -u CODEX_THREAD_ID -u CODEX_SESSION_ID .venv/bin/python -m pytest -q` | 4,622 passed, two warnings, 210.22 seconds |
| Independent latest-checkout outcome/API/transport tests | 85 passed |
| Independent `swift test --scratch-path /tmp/bob-outcomes-verifier-swift` | 570 passed, including 14 outcome tests |
| Python compileall, targeted Ruff and diff whitespace | Passed |
| Snapshot `host/build.sh` | Passed: release panel, extension and frozen app |
| Unsigned Debug iOS simulator build | Passed |
| Deep strict app signature, before and after import smoke | Passed |
| Bundled interpreter imports | Outcome modules, daemon and menu-bar app passed |
| Packaged sources/resources | Outcome store, aggregation, board and API match snapshot; nested panel, icons and root stamp present |
| Independent bug audit, second iteration | SHIP, score 0, no functional blockers |
| Independent integration review after repairs | CLEAN |
| Independent feature verification | PASS-WITH-MANUAL; literal standing checklist retains two unrelated failures below |

Logs: `/tmp/bob-outcomes-host-final-green.log`,
`/tmp/bob-outcomes-frozen-audited.log`, `/tmp/bob-outcomes-ios-final.log`.
The simulator command used `BobPhone`, `generic/platform=iOS Simulator`, and
`CODE_SIGNING_ALLOWED=NO`. Frozen imports used the bundle's Python with `-B -I`
and its resource import paths, without launching or installing the app.

An earlier full run encountered an ephemeral test-port collision (EADDRINUSE),
before the home-key test reached its assertion. All 12 home-seal tests passed
on rerun, followed by the clean full run above. The two final warnings concern
an existing asyncio-marked synchronous test and an out-of-date personal review
skill mirror; neither is a test failure.

Integration review exercised actual sealed home admission and home/away
encryption round trips. Maximum Unicode evidence plus 150 runs paginated into
10 pages without loss or duplication. Pages are bounded by both row count and
300 KB encoded JSON to stay within transport inflation limits.

Audit fixes have regression coverage for late responses after acceptance,
same-revision page ordering, unavailable-report recovery without draft loss,
and stopping review waits at the reopening action with transaction rollback.

## Remaining checks and limits

After installing updated apps, perform this device walkthrough:

1. On the Mac, open a completed card, enter its objective and save. Reopen it,
   enter evidence and accept the outcome. Confirm one distinct accepted outcome.
2. On the paired phone, confirm the same objective, evidence and acceptance.
3. On the Mac, request revision with a reason. Confirm acceptance is removed
   and rework is recorded on both devices.

This checks real-screen legibility and discoverability. Automated checks cannot
substitute for it. Saving currently closes the Mac editor, hence the reopen step.
Enter evidence after reopening: evidence entered before a generic Save is not
retained by that action.

The installed-daemon probe checked the existing runtime only: hooks, loopback
API/auth, SSE and panel ownership were healthy. That installation does not yet
advertise outcomes support. No installation, launch, commit, push or release
was performed for this feature.

Monetary provider coverage remains partial where inclusion of child sessions
cannot be established. Unknown cost is not zero, and waiting gaps are not
invented. Two unrelated standing-check discrepancies remain: the older hook
allowlist omits stdlib `urllib`, and Quit has a bounded main-thread wait.

GitNexus impact analysis preceded source edits; reported high-risk shared board
surfaces were disclosed before editing. No commit was requested, so pre-commit
graph change analysis was not run. Baseline unrelated work was preserved.
Board attachment and creation refused session attribution; no card was filed
or moved to Done.
