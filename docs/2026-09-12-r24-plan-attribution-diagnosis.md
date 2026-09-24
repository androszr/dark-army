# R24 plan attachment attribution diagnosis

Read-only runtime investigation, 2026-09-12. No application code changed,
no attachment retried and no live session restarted or controlled.

## Confirmed observations

- R24 card `455d10606a324261a21e169e66f17787` is in Prep, with no plan path.
  Its `refine_session_id` is
  `codex:01a096e8-5ce1-70d3-8e7c-b37c1cb076b5`. This is the requesting
  session, not an incorrectly selected card. The daemon log records binding
  and naming Androll at 20:36:46.
- The MCP helper is PID 29419, child of the native Codex PID 29351, listening
  on 55811. The daemon logged that channel and parent PID at 20:36:45.
  The installed channel script matches the checkout.
- The native process has the exact session journal open. A fresh, read-only
  execution of the checkout's `resolve_navigation_proofs` finds its unique
  holder, PID 29351 on `/dev/ttys003`, with no uncertainty. A whole-roster
  scan found six roots and six proofs in approximately 0.11 seconds.
- The live daemon nevertheless publishes `can_jump: false` and
  `can_close: false` for Androll (and the other sampled Codex roots).
  Its HTTP listener is healthy and loopback-only, PID 25836.
- `_handle_board_attach_request` calls `_board_request_session_fresh` before
  looking up the card. The same generic refusal can also originate from the
  downstream `_board_author_guard`. The reply contains no reason code and
  the normal log records no rejected predicate, so the observed reply does
  not identify which guard failed.

## Reproduced defect and remaining uncertainty

`psutil._psposix.get_terminal_map` is memoized. On macOS,
`Process.terminal()` returns `None` when its cached device map lacks the
process's terminal. The bundled psutil bytecode uses this same map. There
is no terminal-map invalidation in the repository's host sources.

A disposable diagnostic Python process populated that cache, created a new
PTY and a temporary child holding it, then queried the child's terminal:

| Observation | Result |
|---|---|
| Newly created terminal | `/dev/ttys000` |
| Device present in cached map | false |
| `Process.terminal()` before invalidation | `None` |
| After clearing the diagnostic process's map | `/dev/ttys000` |

Only the diagnostic child was stopped and its owned PTY handles closed.
No live daemon cache or real session was changed.

`codex_rollouts._title_processes` rejects candidates without a terminal;
`resolve_navigation_proofs` can then mark the project uncertain, and
`_board_request_session_fresh` refuses attribution rather than guessing.
This is a demonstrated defect consistent with missing navigation and
attachment, **not conclusive proof of the R24 rejection's exact cause**.
The running daemon's cache and rejected predicate were not observable.
Global record-generation changes during asynchronous validation and
process-inspection failures remain alternative explanations. In particular,
the missing navigation of older sessions is not explained conclusively by
the new-terminal reproduction.

## Next diagnostic/fix boundary

Before calling this incident resolved, obtain bounded reason diagnostics for
candidate rejection, process inspection, root-generation invalidation and
author-guard rejection in the real daemon, without logging tokens, prompts
or entire argv. Fix the terminal lookup freshness with a regression that
creates a terminal after cache initialization, preserving exact journal/PID
ownership checks. Validate a new Codex Refine session while the daemon stays
running, then attach its plan through the attributed tool. A restart-only
success would be an observation, not a durable fix.

Graph: explicit `bob-companion` repository, indexed HEAD `43b2934`; status
reported only TODO and the new R24 plan stale. The graph found the attach
flow but failed to resolve the two daemon attribution symbols; those were
read directly, so no complete graph coverage is claimed. Installed daemon
and rollout sources differ from current uninstalled human-close edits;
the inspected navigation/attribution functions are present in both.

Skipped: broad Claude hook/usage/phone health checks, unrelated to this
Codex attribution incident; implementation gates, because no product code
changed; any live mutation or application restart.
