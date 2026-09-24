# Codex closed-session liveness verification

Approved plan: *codex closed session liveness*.
The user approved implementation directly in the investigating session.

## Result

The source fix replaces project-folder presence with evidence about the exact
Codex conversation. A native process must stably resume that thread or hold
its exact journal. Another session in the same folder supplies neither proof.
Failed inspection remains unknown; a successful empty scan proves absence.
Existing retirement preserves the last message, usage and finished timestamp,
releases the board claim, and never marks a card Done automatically.

Presence does not grant additional control capabilities. The existing title,
Stop, Close, Hide and enrollment rules remain separate. The 15-minute journal
discovery policy is unchanged, including the previously documented age-out
retention limitation.

## Evidence

- Original screenshot row: Richard, `codex:01a0716b-61ec-7be2-b4ce-0e442c77b564`.
  Its final turn completed at 14:05:58 local time. Five other native processes
  shared the cwd; none held its transcript, but the old scanner reported True.
- Six initial failures reproduced sibling ownership through parser and daemon,
  for both working and waiting roots. Tests exercise actual temporary journals
  and process observations instead of injecting a desired `process_seen` value.
- Final focused run: 185 passed, including concurrent navigation additions.
  Existing control/title/finished suites: 274 passed. Swift: 552 passed.
  Ruff and Python byte-compilation passed.
- Final full-host verification: **4,272 passed, one existing warning**, exit 0
  in 193.87 seconds against the fixed snapshot. Source hashes were unchanged
  from start to finish. All automated plan criteria pass; the installed check
  remains manual.
- Read-only source probing after the integration repairs: Richard reports
  `process_seen=False`, with nine present roots and three absent roots in a
  diagnostic two-hour discovery window. Warm scan: approximately 75 ms. The
  production discovery window was not changed by this diagnostic.

## Reviews

- Integration: CLEAN after repairing both reproduced findings. Real psutil
  processes are reopened to bypass cached creation time/executable fields;
  transient failure to stat a held target journal remains unknown rather than
  silently becoming absence. Both corrections have regression tests and were
  independently rechecked.
- Adversarial audit: SHIP, score 0, no functional blockers in the liveness delta.
- Blind verifier: automated liveness acceptance and the full-host result are
  recorded separately from the installed manual check. Its standing convention
  review found a pre-existing AppKit wait in `app.py`:
  `_on_quit` calls `_shutdown_daemon`, whose future waits up to eight seconds.
  This is an unrelated standing-check FAIL, not a liveness regression or a test
  failure allowance. It remains recorded in TODO.
  The verifier's overall standing-convention verdict is consequently FAIL
  solely for that baseline shutdown wait; plan acceptance is PASS with MANUAL.
- GitNexus: initial impacted runtime symbols LOW. Full untruncated
  `LocalBackend.callTool('detect_changes', {scope: 'all', repo: '.'})` output
  returned 90 symbols / 8 files / 4 flows, MEDIUM, including concurrent navigation
  work. The CLI's display truncates symbol lists despite its limit option;
  backend arrays were inspected directly. Graph coverage is bounded.

## Concurrent work and reproducibility

Other sessions edited, staged, committed and installed shared work during this
run. Those operations were not performed by this implementation session.
The original baseline was captured before source changes; external commits
`986df5d` and `b1c2c0f` captured some intermediate liveness changes. Navigation,
phone/parity, hooks and other unrelated changes were preserved and excluded
from the liveness review verdicts.

Two full runs against moving source encountered unrelated source-inspection
and import mismatches. A private source snapshot then ran 4,271 tests successfully
with one environment failure: its copied source lacked Git metadata required
by `test_no_new_theme_token`. A private copy of Git metadata was added without
changing snapshot source. The final run uses that fixed snapshot and the
existing virtual environment; imports were verified to resolve into the
snapshot. All six liveness function ASTs match the reviewed original source.
Snapshot metadata: `/tmp/bob-liveness-verification-snapshot.json`.

## Installed check still required

Another session installed an intermediate build. The installed-source check
found the liveness change but not the final `_liveness_open_identities` repair;
therefore the final fix is not certified as running in the installed app.
No install, restart or real session control was performed by this session.

After an authorized final rebuild/install: open two Codex terminals in one
enrolled project → prompt both → close one outside Bob → within two roster
refreshes confirm only that row leaves the active list and appears once in
Recently finished → resume that conversation → confirm one active row returns.
Record the running bundle identity and preserve the sibling throughout.

Why not automated: this confirms real terminal teardown and file ownership in
the frozen installed app; deterministic tests cover the decision logic.
