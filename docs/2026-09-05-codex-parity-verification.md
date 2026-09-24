# Codex Stage A verification — 5 September 2026

Plan: *codex desktop mobile parity*.
Scope: rollout observation, automatic-compaction refusal, phone Hide, and
interaction explanations. Stage B remains proposed, not implemented.

## Verification boundary

Other sessions changed this shared worktree while tests/builds ran. An initial
full host run reported 4,115 passes and eight failures: seven stale fixture
assumptions were corrected without changing the concurrent feature behavior,
and one source-introspection failure passed on a fresh run. A release build
reported that a source file changed during compilation.

Final automated verification therefore uses the frozen worktree
`/tmp/bob-codex-parity-verify-lb8lsgli`. Its copied-source manifest is
`/tmp/bob-codex-parity-snapshot-manifest.json`; the initial shared-tree baseline
is `/tmp/bob-codex-parity-baseline`. The verifier confirmed imports resolve to
the frozen worktree, not the shared checkout. This is not evidence that every
future concurrent edit in the shared checkout passes.

## Independent reviews

**Integration: WARN, no unresolved code blocker.** The reviewer inspected the
separate home/away Hide allowlists, sealed authorization, Origin checks, action
counters, revocation, away lease, action recording, and existing root/revision
Hide semantics. No new signal, deletion, runtime dependency, persistent schema,
or process-control authority was introduced. Swift fields decode tolerantly.

The reviewer found a phone Hide timing bug: a refresh could outlast its settling
timeout before successful acceptance reached the view. The repair handles an
already-expired timeout synchronously, restores controls with a note if the row
remains, and dismisses detail if the row is absent. A second review confirmed
the blocker resolved. The regression is a source-contract check, not an iPhone
runtime UI test.

The remaining integration warning is unverified new frozen-bundle and actual
paired-device behavior. This implementation run did not replace or restart the installed app.
The reviewer also ran the extension production dependency audit: zero findings.

**Blind acceptance verification: PASS-WITH-MANUAL.** The refreshed frozen
snapshot passed all 4,215 Python tests (one existing asyncio-mark warning),
436 targeted Python tests, 552 Swift tests, Ruff, byte-compilation, the release
panel build, and the unsigned iOS simulator build. The Swift total includes
three Codex capability and 19 answer-layout tests. The LAN suite passed all
100 tests. Swift sources were unchanged between verification rounds.

The first frozen full Python run caught nine stale expectations from concurrent
question-preview, multi-select and phone-action changes. Their authors had
already corrected them; refreshing the snapshot produced the clean full run.
The verifier kept two unchanged baseline observations separate: its fixed hook
import sample omits existing stdlib `urllib`, and the menu-bar quit path waits
synchronously. Neither was introduced or repaired by Stage A.

**Adversarial bug audit: SHIP, score 0, zero functional blockers.** The read-only
review found no attributable Stage A defects in parser lifecycle, question
authority, compact refusal, tolerant decoding, or phone Hide settling. Physical
Mac/phone behavior and a new frozen app bundle remain unverified.

A final source comparison confirmed all eight changed client/API files match
the verified snapshot. The shared parser and daemon gained independent
navigation/attribution and liveness changes after that snapshot; Stage A hunks
remain intact. Those later changes are outside this verification result.
GitNexus MCP was unavailable due to its storage-version mismatch; CLI impact
analysis and full shared-tree change analysis were used. The latter reported
CRITICAL aggregate risk across concurrent work, not a clean narrow delta.
No commit was performed by this implementation run.

## Installed-runtime baseline probe

- Hook/API listeners are loopback-only on 19873/19874; API token is owner-only.
- State, usage, history JSON and the initial SSE data frame respond. An unknown
  action returns 403 with Bearer auth, 400 with the correct Bob header, and 403
  with an untrusted Origin. No real action was invoked.
- The installed hook matched source when probed, used only stdlib imports,
  parsed under Python 3.9.6, and all expected hook events were wired.
- One panel had the expected executable and menu-bar parent; pid files matched.
- Three editor servers reported version 0.1.11 and answered ping/frontmost.
- Claude's roster normalized successfully, sampled transcript keys matched the
  parser, and expired usage was marked stale while later windows stayed fresh.
- Installed state carried no `interaction_note` fields: these measurements
  validate the existing app's baseline, not the new implementation.

## Recorded-journal check

A read-only check with the frozen parser found four existing journals whose
last task event was `turn_aborted`; all four now parse as waiting. The initial
audit had found interrupted journals incorrectly reported as working. No live
session was interrupted or sent input for this check.

## Physical checks still required

1. Open the same recorded Codex question on the Mac and a paired phone. Confirm
   its question and choices are readable, the explanation directs the answer
   to the original session, and no reply/answer controls are enabled.
2. On the phone, choose an eligible row and press **Hide until this thread
   changes**. Confirm the row leaves both screens after their next updates.
   Send a new turn from the original Codex session and confirm it reappears.

Why not automated: these checks require real displays and a paired phone;
parser state, action authority, route guards, and revision behavior have
automated seams. No installation, restart, commit, push, or release is part of
this implementation run.
