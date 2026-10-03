# The action journal

Dark Army writes down what it is about to do before it does it, records the
outcome under the same note, and on relaunch finishes the harmless half of
whatever was interrupted and refuses to repeat the dangerous half. The idea is
Pi Durable's (a design reference only: no dependency, no code taken).

## Where it lives

- `host/dark_army_daemon/action_journal.py` — pure declarations and decisions,
  no I/O: `ACTIONS`, `may_replay`, `decide`, `CONTINUES`, the two restart notes
  and `INTERRUPTED_WORDS`.
- `host/dark_army_daemon/board_journal_store.py` — `JournalStoreMixin` on
  `BoardStore`'s connection and lock, three tables in `board.db` (schema v33).
- `host/dark_army_daemon/daemon_recovery.py` — `RecoveryMixin` on `BobDaemon`:
  the call-site helpers, `stop_session_recorded`, `recover_actions` and
  `journal_self_check`.

## The three tables

- `action_intents(id, action_id, kind, subject, step, replay, attempt, payload,
  process, created_at)` — written and committed **before** the side effect.
  `id` is `secrets.token_hex(16)`; `replay` is the step's declaration
  snapshotted at write time; `subject` is a card id or a session id.
- `action_results(intent_id, outcome, detail, payload, created_at)` — the
  outcome under the intent's id. `INSERT OR IGNORE`: fulfilment is id
  existence, a second result is ignored.
- `action_attempts(kind, subject, attempts, last_at)` — how many times the
  action was begun for the subject, stepped in the same transaction as the
  action's first intent so a restart loop cannot reset it. It starts over after
  a quiet day (`ATTEMPT_DECAY_SECONDS`).

No `cards` column, no change to `_WRITABLE`, `SINGLE_WRITER` or any snapshot.
No foreign key to cards: the journal is a history, pruned at 30 days and 5000
rows, resolved actions only — an open intent is never pruned. `board.db` is
already private; no new private file. A payload is ids, pids, roots, paths and
columns: `event_log.FORBIDDEN_KEYS` is refused at any depth at write, and no
prompt, argv, question text or answer is ever in one.

## The five actions

| Kind | Steps (replay) | Where |
|---|---|---|
| `card_start` | `spawn` (never), `record` (safe) | `_dispatch_card_locked` |
| `worktree_prepare` | `add` (never), `record` (safe) | `_prepare_worktree`, `_prepare_worktree_then_dispatch` |
| `stop_session` | `terminate` (never), `kill` (never) | `stop_session_recorded`, `_confirm_stop` |
| `autocompact` | `type` (never) | `_flush_auto_compacts` |
| `answer_burst` | `type` (never) | `_type_answer_burst` |

`stop_session` itself stays synchronous and unchanged; the API's two routes
(desk and phone) await `stop_session_recorded`. A direct caller of
`stop_session` leaves no row. A journal that cannot be written never stops the
action: one warning, and it runs unjournalled, as it did before.

`CONTINUES` names the crash that falls between a result and the next intent
(the spawn result and the `record` intent; the folder's `add` result and its
`record` intent): such an action is open too. A folder step that ended in words
rather than a crash writes a `skipped` record result so it is not read as a lost
press.

## Recovery

`recover_actions` runs once in `run()`, after the board connects and before any
card is read, in its own `try`/`except` (a failure is logged and the board stays
open). **It is never a launcher**: it opens no terminal, signals no process,
types no key, runs no git command and moves no card to Done.

- `card_start`: `spawn` open means it is unknown whether a terminal opened. The
  card keeps its column; `RESTART_DURING_START_NOTE` goes on its
  `dispatch_error`, a queued press is dequeued in the same write, and
  `card_dispatch_failed` is written to the diary. `spawn` done and `record` open
  (or never written) finishes the write with the payload's own time and the same
  five fields the live write uses, only when the card is still unlinked
  (`""` link, no session); a card that moved on is left alone; a record whose
  write had already landed is only completed. Attempts above `MAX_SAFE_REPLAYS`,
  a changed declaration or a batch head are interrupted with the note instead.
  Inside `DISPATCH_BIND_WINDOW` of the Start, the bind's receipts come back
  (an empty baseline; the shell or pty pid only when it still exists and, for a
  pty, the broker still holds it); past the window the bind falls back to the
  predicate alone, as it always did after a restart.
- `worktree_prepare`: `add` open writes `RESTART_DURING_PREPARE_NOTE` on the card
  (a Start's own preparation only, never a merge's) and dequeues a queued press;
  the folder is left for the next Start, which reuses it. `record` open records
  the folder if it is still one. The press's re-entry died with the process and
  is not resumed.
- `stop_session`, `autocompact`, `answer_burst`: interrupted, never repeated, one
  `action_interrupted` diary line. Resuming a lost SIGKILL escalation is out of
  scope. For auto-compact, every `type` intent inside `SETTLE_SECONDS` (open or
  sent) seeds `AutoCompactPolicy.restore`, so a restarted policy answers HOLD
  rather than typing `/compact` again.

A second run finds nothing open and changes nothing.

## The self-check

After recovery `journal_self_check` writes one INFO line (`action journal: N
open, N replayed, N interrupted, N mismatches`) and one warning per non-empty
class, ids and counts only: `open_after` (must be empty),
`dispatching_without_receipt` (a `dispatching` card with no finished `record` at
its `dispatched_at`; expected once per upgrade for cards a v32 build started,
counted and never acted on), `record_without_card_link` (a record finished in
the last bind window whose card is neither `dispatching` nor `live` nor Done) and
`worktree_recorded_but_absent`.

## The crash-point harness

`host/tests/test_crash_points.py` drives each action through its real path with
every effect stubbed. `board_journal_store.CRASH_HOOK` (`None` in production)
fires after each intent and result commits; the harness raises `SimulatedCrash`
at the Nth, snapshots the file with `sqlite3`'s backup, reopens it under a fresh
daemon, recovers twice and asserts: nothing open, no launch, signal or keystroke
repeated, no card in Done, the second run changed nothing, and the per-kind
invariant. `test_every_declared_action_has_a_crash_scenario` fails when an action
is added to `ACTIONS` without one.

## What a downgrade sees

An older build opens the same file, logs that a newer build wrote it, reads every
card and never stamps the version down. It ignores the three tables.

## Deliberately not journalled

Refine, batch Start, `start_project`, ad-hoc terminals, Mission Control,
consults, typed replies, `/clear`, `/low-priority` and card messages. Each is one
more `ACTIONS` entry and one `SCENARIOS` coroutine.
