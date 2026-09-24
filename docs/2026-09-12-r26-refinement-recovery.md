# R26 refinement recovery — 2026-09-12

## Observed failure

The live API identifies card `7e52a20ddeff4cb0910c06bd60807d3a` in Prep,
with an empty plan path and `refine_session_id` equal to this session,
`codex:01a096ec-c31b-7b42-aafe-22c570de0d7a`. The card binding is correct.
The same live row, Ptyś, has `can_jump=false` and `can_close=false`.
Fresh OS inspection finds the actual native Codex ancestor, PID 33209,
on `/dev/ttys011`.

Attachment fails in caller attribution before the board move. Without an
attachment, the private automatic-close receipt is absent and close-out
correctly refuses. Moving the card directly would not repair this chain.

The installed daemon (PID 25836 when inspected) does not refresh psutil's
terminal map in `_process_snapshot`. A concurrent repair in the shared
checkout adds `get_terminal_map.cache_clear()` before process enumeration,
and introduces `host/tests/test_codex_attribution_freshness.py`. This R26
session did not author or overwrite that repair.

## Independent regression evidence

The regression creates a real controlling PTY after priming psutil's map.
With only the refresh disabled in an isolated test process, these two tests
fail: `test_real_process_snapshot_refreshes_terminal` sees `None` instead
of the new tty; `test_new_pty_does_not_poison_existing_same_project_attribution`
loses both exact root proofs. With the source refresh enabled, both pass.
No live process cache was changed and no real user terminal was stopped.

Commands/results:

- `host/.venv/bin/pytest -q host/tests/test_codex_attribution_freshness.py -k 'real_process_snapshot or new_pty'`: 2 passed, 9 deselected.
- An isolated Python runner assigns a no-op `cache_clear` object to
  `codex_rollouts.get_terminal_map`, then runs the same two tests: 2 failed,
  9 deselected, the expected red reproduction.
- Earlier source checkpoint: freshness, channel, board-refine and Codex
  parity suites: 308 passed in 2.36 seconds. The concurrently edited
  freshness test file subsequently gained diagnostic cases; this count
  does not certify those later edits.

This proves the stale-map defect and its effect on attribution. It does
not identify the original live refusal's precise predicate: the installed
daemon reports only the generic refusal. A live attachment after loading
the fix is still required to establish recovery.

## Focused live probe

- HTTP API is loopback-only on IPv4 and IPv6; state is readable and SSE
  delivers an initial frame.
- Harmless unknown action: Bearer 403, X-Bob-Token 400, as required.
- Installed Claude hook matches source, parses on the diagnostic Python,
  and no expected hook events are missing.
- Editor bridge replies: project window 51235 uses 0.1.15; three other
  windows use 0.1.11. Exact terminal ownership remains required for close.
- Skipped unrelated Claude transcripts, usage cache and phone transport;
  this probe concerns Codex board attribution, not general fleet health.

## Recovery status

Pending installation/runtime validation and attributed attachment of the
existing R26 plan. No duplicate card, generic board write or direct
database mutation was used. Application installation is being coordinated
with the concurrent session implementing the same fix.
