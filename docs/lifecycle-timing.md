# Lifecycle timing

Queue wait, execution, human review and rework are reported separately as
**card-time**, not labour. Confirmed `[utc_start,utc_end)` spans are the only
elapsed source. Sleep, restart, clock jumps and missing observations are
gaps, never filled in. Rework overlaps the other three categories: do not
add the four rows.

**One quiet interval is one row.** A confirmed span ends at `previous_utc +
monotonic_delta` and the next starts at a fresh wall-clock read, so the two
never agree to the microsecond; `_confirm_spans` extends the newest
complete span of the same key whose end lies within `SPAN_JOIN_TOLERANCE`
(2s, the same bound past which a sample is already a `utc_disagreement`
gap) rather than matching on equality. The equality join wrote one row per
checkpoint on a real Mac — 960k rows, a 27s report read under the store
lock, a phone's card write queued behind it — and `_coalesce_spans_once`
folds such a file on its next open (`lifecycle_meta` `spans_coalesced`),
gap rows untouched, then VACUUMs.

## What is measured

- **Queue wait.** From `queue_state='queued'` until the dispatching update
  that runs only after `spawned=True` (clears the queue, stamps
  `dispatched_at`). Reorder does not restart it. Unqueue, reset, hard
  refusal and deletion cancel it. A direct successful start is a measured
  **zero**, only when that dispatch boundary was observed. Binding after
  launcher success is unclassified launch time, not queue or execution.
- **Execution.** One episode per implementation attempt, opened when
  dispatching is committed. Active seconds accrue only while the bound
  session is `link_state == 'live'` **and** working, minus
  question/permission. Dispatching, roster stubs, idle terminals and
  refinement do not count. `mark_ended` / submission close; reset /
  replacement / deletion cancel. `mark_live` reopens the same identity.
- **Human review.** Submitted-result waiting from the outcome ledger's
  submission until acceptance, revision request or reopen, plus due
  manual-check episodes as a named cause. Store both and their union.
  Acknowledging Reviewed is not acceptance.
- **Rework.** Overlay from the first qualifying reopen/revision until the
  next submission. Scope-change acceptance removal is not rework.

## Cohort and quantiles

Period is finite `[from,to)` UTC, at most 366 days; default last 30 days
frozen at first request. Episodes with a **known start in the period** are
denominator `N`. Completed, correctly closed episodes with both observed
boundaries and uninterrupted eligible coverage are `n`. Cancellations are
never completed samples. Direct-start queue zero is eligible. No `N` means
coverage unavailable; no `n` means every quantile unavailable.

Quantiles use sorted complete durations and **nearest rank**, index
`max(0, ceil(p*n)-1)` for p50 and p90. Histogram buckets `[0,60)`,
`[60,300)`, `[300,1800)`, `[1800,7200)`, `[7200,∞)` seconds; counts sum to
`n`. Only episodes closed by `as_of = min(to, report snapshot time)` count
as completed: later `mark_live` cannot rewrite an earlier report. Carry-in
(start before `from`) is reported separately and never enters the main
distribution.

## Seeded 120-second fixture

Fake clocks, observe every ≤10 seconds: queue 0–10, launcher/bind at 10,
execution 10–30, question pause 30–40, execution 40–50, review 50–80,
revision at 80, queue 80–90, execution 90–110, review 110–120, acceptance
120.

Expected: queue episodes 10/10; first execution 30 and second 20;
human-review 30/10; one rework overlay 30. The four-category sum exceeds a
partition because rework overlaps. For values 0/10/20/30, nearest-rank
p50=10, p90=30; bucket totals=4.

## Retention and collector health

Detailed evidence is kept **366 UTC days**. Compact card metadata and
retention counts survive. Open episode identity and the initial boundary
are kept; old spans on an open episode are trimmed with an explicit
retention gap. Prune in batches of 1,000 during connect, never during a
report read. Schema 21 is additive; old cards seed as left-censored with
`tracking_since` now and no historical duration backfill. A downgrade /
re-upgrade is a new continuity gap.

`lifecycle_supported` is false-by-absence. Collector failure is a separate
flag (`measurements_available`) and must not set `_outcomes_unavailable` or
stop reconcile. Observational writes use a SAVEPOINT so a board action
still commits.

## Reads

`GET /api/lifecycle` and sealed kind `lifecycle` (home allowlist **and**
`_sealed_run`; no lease, not on the action tuples). One paged collection
per response (`cards` for a project, `episodes` for a card). No duration
arrays on SSE. The board snapshot carries per-card `run_figures` — the
execution total at **minute** granularity beside the outcome ledger's cost
(`docs/context-board.md`, *Cost and time on the card*); duration arrays
still never ride SSE.

**The card timeline is a second read over the same ledger, and it adds one
boundary.** `attach_plan` writes a `plan_attached` boundary (provenance
`attach`) inside its own lock and `_lifecycle_try`'s SAVEPOINT, so a ledger
failure never refuses the attach. It carries no `episode_id`: the report's
replays (`replay_close_time`, `replay_disposition`) skip it and
`lifecycle_prune` never selects it — a moment on the card's timeline, not
episode bookkeeping, and cards planned before it exist read "not observed".
`card_timeline_facts(card_id)` is a read under one lock — the ledger row,
the card's boundaries `ORDER BY ts, id`, its outcome events and run session
ids, no evidence text — composed by `card_timeline.compose` into the
`timeline` the two on-open card reads carry (the *card timeline* plan).
Nothing here rides SSE, and the phone's background sweep never asks for it.

## Validation

Host lifecycle tests 70 passed; related outcome/queue/agent-report 201
passed; full host suite 7156 passed in file-disjoint batches. Panel 953
tests and release build. Phone `xcodebuild` on iPhone 17 Pro with
`LifecycleReportTests` executed (`-skipPackagePluginValidation` for
SwiftTerm). Integration review: new modules live in the already-frozen
`dark_army_daemon` package; write authority unchanged (`lifecycle` is
a read). Security review: sealed `lifecycle` on both doors, no lease, not
on `LAN_ACTIONS`/`REMOTE_ACTIONS`, no key/digest/claim/token on the wire.
Both reviews CLEAN, no unresolved BLOCK. Installed-bundle freeze was not
built this run.
