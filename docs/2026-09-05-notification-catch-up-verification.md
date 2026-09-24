# Notification destinations and catch-up verification

Accepted plan: notification destinations and catch-up.

Status: implementation and automated verification complete. Bug audit SHIP (score 0); feature verification PASS-WITH-MANUAL. Integration review is CLEAN. This report does not claim deployment or a physical-device pass.

## Physical-device checks still required

Run the five manual acceptance cases from the plan in these two sessions. These checks require an explicitly installed test build and deployed matching relay; neither is part of this implementation run.

1. **Notification routing and unlock.** Pair an iPhone with a reachable Mac and enroll projects A and B. Select B, lock the phone, then trigger a question in A. Tap its notification, cancel Face ID, unlock again, and confirm the exact A question opens without submitting an answer. Terminate the app and repeat with a card notification. Disable connectivity during another tap, restore it, and confirm the pending destination opens once. Answer an older question on the Mac, create a successor in the same session, and tap the old notification: only the original saved question/outcome should appear, with no answer controls aimed at its successor.
   Why not automated: simulator tests exercise routing state, protected persistence, and cancellation; they do not establish real APNs delivery, Face ID cancellation, or the rendered cold-launch sequence.
2. **Recovery and access boundaries.** Record decisions in both projects, end their agents, restart Bob, and read both projects in Catch up. Mark the complete global digest caught up, create another event, and confirm it remains new; a filtered or partly loaded digest must not advance the global checkpoint. Tap a grouped notification and confirm it shows exactly its saved members. Open a deleted/expired target and confirm a clear explanation. Let the away control lease expire: history should still read, while an attempted write must refuse.
   Why not automated: store, paging, receipt membership and sealed-access fixtures cover these contracts, but a paired-device session verifies their combined presentation and live transport.

## Scope and baseline

The shared checkout contains concurrent unrelated changes. Baseline patches are retained under `/tmp/bob-notification-implementation-baseline.*`; only the notification/history additions belong to this implementation. No commit, installation, relay deployment, or release is included.

The installed baseline daemon was probed read-only: loopback listeners, hook wiring and system-Python compatibility, token enforcement, state/usage/history responses, initial SSE frame, editor connections, and panel ownership passed. That probe describes the previously installed application, not the new source.

## Automated evidence

| Check | Result |
| --- | --- |
| Final feature Python regression files | 55 passed independently; `/tmp/bob-notification-verifier-feature.log` |
| Wider repair regressions | 296 passed before final ordering guard; final store 29 and API 14 passed |
| iOS XCTest | 12 passed, 0 failures on a dedicated iOS 26.5 simulator; `/tmp/bob-notification-verifier-ios-dedicated.log` |
| Panel tests and release build | 566 passed; release build exits 0 |
| Relay payload fixtures | 4 passed |
| Python compileall, focused Ruff, whitespace | Passed |
| Isolated macOS bundle | `build.sh` exits 0; `/tmp/bob-notification-verifier-bundle-final.log` |
| Frozen imports and behavior | Bundled Python, with bundle-only import paths, imported daemon/capture/store and preserved an observed answer against a late delivery callback |
| Signature after smoke | Strict deep verification exits 0; the probe's 56 unsigned bytecode files were removed without modifying signed resources |
| Integration reviewer | CLEAN; independent failure/recovery probes passed |
| Final isolated full host suite | 4,532 passed, 1 pre-existing pytest-mark warning, no exclusions, 149.54s; `/tmp/bob-notification-post-audit-host.log` |
| Blind feature verifier | PASS-WITH-MANUAL; formal whole-repository verdict retains the two baseline standing failures below |
| Bug auditor | Final SHIP, score 0. Independent reproductions confirmed moved-root receipts refuse and repeated manual/Done cycles retain separate identities through restart; 34 store tests passed |

The shared-tree host run had 4,632 passes and two failures in concurrent terminal-close work. Its helper changed during the run; a focused rerun then had broader mismatches. The final isolated full run retains all final feature sources and restores only `.claude/skills/ship/close-out.sh` and `host/tests/test_ship_close_out.py` to the recorded initial `b1c2c0f` baseline. The first isolated run additionally exposed six failures from the new unrelated `_refinement_receipts.pop(sid, None)` cleanup in `_forget_session`, and one missing-Git-metadata fixture failure. The final isolation also omits only that cleanup line (absent at baseline) and initializes an index for the two unchanged theme files. No notification behavior is removed, no tests are skipped, and the shared checkout is untouched.

The verifier also reports two pre-existing standing-check failures: the literal hook import allowlist omits stdlib `urllib` (actual system-Python purity passes), and an unchanged AppKit Quit path blocks on `future.result(timeout=8)`. These are outside the notification delta and are not presented as a clean whole-repository review.

GitNexus CLI impact checks preceded production edits. Broad CRITICAL graph risks were reported before edits; unresolved UNKNOWN results were corroborated through source call sites. MCP graph access was unavailable because of a storage-version mismatch. No commit was requested or made.


Post-audit packaging: `/tmp/bob-notification-post-audit-bundle.log` exits 0; bundled Python with `-B` imports daemon/capture/store and independently rejects an old moved-card receipt without leaking excluded-project content. Strict deep signature verification remains exit 0. Focused post-audit repair tests: 59 passed. Only `decision_store.py` and its tests changed in this repair.
