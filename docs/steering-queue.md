# The steering queue

Everything Dark Army types into a session waits in that session's **lane**,
one sender at a time, in the order it arrived. Each durable entry is written
down before it is typed; after a restart what never started is typed again
only while fresh. The idea is Pi Durable's many-clients-steer-one-session
(a design reference only). Code: `host/dark_army_daemon/steering.py` (rules and
`Lanes`, no I/O), `board_steering_store.py` (the table),
`daemon_steering.py` (`SteeringMixin`, the daemon half).

## The lane

A lane key is `pty:<handle>` when the session runs in a terminal Dark Army
hosts (the input line is the real resource, so a `/clear`'d session's new id
shares it) and `sid:<session id>` otherwise. A lane is a FIFO with one worker
task, started on demand and gone when empty, on the daemon loop. Nothing waits
for idle: with nothing ahead an entry is typed at once. Every safety check the
verb made is still made **at its turn**, because each verb's body moved
verbatim to a `_<verb>_now` function the lane runs.

A caller takes its place in the lane **before** the store write (`reserve`), so
arrival order is the order of the calls however long the write takes. A caller
awaits its own entry for at most `STEER_REPLY_WAIT_SECONDS` (20 s) and is then
told `STEER_QUEUED_NOTE` as a success; the typing carries on. A cancelled
caller never cancels the typing. A lane holds `STEER_MAX_PENDING` (8) entries;
the ninth is refused with `STEER_FULL_REFUSAL`. At shutdown a waiting caller is told the truth: a recorded entry "will be typed after the restart if still fresh" (`STEER_RESTARTING_NOTE`), a memory-only one that nothing was typed, and the entry cut off mid-typing `STEER_INTERRUPTED_REFUSAL`. A body that awaits another verb
on its own lane runs it inline (`running_key`), so there is no self-deadlock.
The note reaches the Mac panel as a plain success (it drops a 200's detail);
the agent detail lists them (see the section on the agent detail, below).

## Kinds

| Kind | Durable | Replayed after a restart | Journal |
|---|---|---|---|
| `reply`, `card_message`, `card_ask`, `terminal_line`, `wrap_up`, `low_priority` | yes | if fresh | the queue's `steer`/`type` |
| `answer` | yes | never (the question on screen may have changed) | the burst's own `answer_burst` |
| `autocompact` | no | never (the policy re-decides) | its own `autocompact` |
| `permission_key` (Grok's digit), `raw` keys | no | never | none |

The daemon's three acting places keep their contract: auto-compact is still
decided on the executor and performed on the loop, now as a memory-only entry
that is submitted and not awaited (a done-callback calls `note_failed` for a
refusal, a full lane or a failure); the question burst is a durable `answer`
entry; alerts type nothing and do not ride the lane. The lane orders acts and
decides none. Raw keys (`terminal_stream_input`, `terminal_input` with
`raw`/`data`) are synchronous: written inline on an idle lane (today's latency
and answer) and queued in order, answered `(True, "")`, behind a busy one.
`daemon_review`'s Continue line is memory-only on the terminal's lane. Grok's permission digit holds the lane only until the key is written and drained; the wait for Grok to record the answer runs outside it. Raw keys do not count toward the 8-entry cap (they have their own, 256).

## The table

`steer_queue` in `board.db` (schema v34, additive, no `cards` column, no new
private file): `id, action_id, session_id, kind, source, dedupe, payload,
created_at, state, ok, detail, resolved_at`. `steer_enqueue` refuses a
`FORBIDDEN_KEYS` name at any depth. `steer_resolve` is guarded
`WHERE state = 'queued'` and **blanks `payload` in the same statement**, so the
words are on disk only while the message waits. A `queued` row is never pruned;
resolved rows go after `STEER_RETENTION_SECONDS` (900) or beyond
`MAX_STEER_ROWS`. The words are never in a journal payload, a diary line, a log
line or a snapshot; a card's not-delivered note carries an 80-character
excerpt on that card's own thread. A store that cannot be written is logged
once and the entry runs memory-only: typing is never refused for it.

## Restart

`recover_steering()` runs in `run()` after `recover_actions()`, in its own
`try`. Per `queued` row, `steering.replay_verdict`: a `type` intent with a
result is `finished` (copied, never retyped); an open or interrupted intent is
`interrupted` (`recover_actions` wrote the diary line; a card kind gets a note
to check the terminal); an `answer` is `own` if its burst began and otherwise
`not_delivered`; a replayable kind with no intent and age within
`STEER_REPLAY_FRESH_SECONDS` (120) is replayed, after a settle pause
(`STEER_REPLAY_SETTLE_SECONDS`, 10) in its original order; anything else is
`not_delivered` (the row's detail is `STEER_NOT_DELIVERED_WHY`, so a phone's resend gets words): one `steer_not_delivered` diary line (kind, source, age and
why, all strings, never the words) and for a card kind a `bob` note. Freshness
is judged again at the moment of typing, so a 115-second entry plus the pause is
dropped. A negative age is stale. A replay re-runs its verb's guards, and a
replayed `card_ask` re-checks a live Claude channel and refuses otherwise: it
**never** consults, so recovery is never a launcher. It is idempotent.

## The phone's mark and the Mac twin

`ApiServer._sealed_run` tags the call `Caller("phone", dedupe_digest(device,
token))`; the loopback door tags nothing (`mac`). The digest (32 hex of SHA-256,
never the token) is kept on the row, so a resend after a restart returns the
recorded answer (or the interrupted refusal, or joins the entry still queued)
and types nothing more. A Mac press that repeats what Dark Army is already
replaying joins the replayed entry (`find_replay_twin`: replayed, same kind and
words, from the Mac, within 120 s); two deliberate identical live presses are
both typed.

## Downgrade

A v33 build opening a v34 file logs the newer-build line, reads every card, never
stamps the version down and ignores the table. Its queued rows are judged by
freshness on the next v34 launch, so they are dropped and said, never typed late.

## What the agent detail shows

Each agent row may carry **`message_queue`**, composed once per push on the loop
by `SteeringMixin._message_queue_details` inside `_collect_agent_stubs` (so
`Lanes` is never read from the executor) from `steering.compose_detail`:
exactly `{"headline", "items", "more_line"}`, items exactly `{"group"
("waiting" | "undelivered"), "label", "status", "excerpt", "why"}`. The daemon
writes every word; the Mac's `MessageQueueBand` and the phone's
`messageQueueBlock` draw it verbatim and derive nothing, so there is no byte-pinned
twin (`test_message_queue_surfaces.py` pins placement and the absence of any
client literal). The key is **absent** when there is nothing to show, an older
client decodes it as `nil`, and a raising composition costs the field, never
the fleet. Read-only: no cancel, retry or discard.

- **Waiting** entries come off the lane (`Lanes.views`) in lane order: the head
  being typed reads "being typed now", the rest "waiting its turn". A lane is
  listed only when two or more listable entries are in it, or one waits out a
  restart's settle pause (`steering.listable`), so an ordinary reply typed at
  once never appears and the page never flickers on it. `raw` and
  `permission_key` are never listed.
- **Not delivered** entries are remembered **in memory only** for
  `UNDELIVERED_SECONDS` (the store's retention, 900): one the body refused or
  that failed, a replay dropped at its turn or by recovery (the tree's
  `STEER_NOT_DELIVERED_WHY` is its reason), and one cut off mid-typing by a
  restart ("may only partly have landed — check the terminal"). At most eight
  per session and sixty-four sessions. The note is made **before**
  `steer_resolve` blanks the stored words. `_forget_session` does not clear it,
  so a tombstone row keeps it; a restart loses it (the diary line is the durable
  record).
- **Words.** A clamped excerpt (80 characters, one line) rides only for
  `reply`, `card_message` and `card_ask`. A `terminal_line` can land in a
  password prompt, so it and every other kind show the label alone ("A line from
  the phone, 14:03"). No id, digest, mark, source token, `created_at` or full
  text is on the field (`test_message_queue_detail.py`'s privacy walk), and
  `message_queue` and `excerpt` sit in `buzz_ledger.FORBIDDEN_KEYS`.
- **Amendment.** *The words are never in a snapshot* above is amended for
  exactly this excerpt: it is the person's own message, in memory, the same
  exposure as the sealed `conversation` read and every row's `last_text`.
- **Push.** `Lanes(on_change=)` fires on a join, a start and a finish;
  `_message_queue_changed` buys one `_schedule_agents_push` only when the shown
  signature moved. The field holds no clock (`HH:MM` text), so it is neither a
  `_CLOCK_FIELDS` nor a `_POLL_ECHO_FIELDS` member. Expiry of an undelivered
  entry shows on the next push of anything, not by itself.

## Deliberately not queued

A person typing in a terminal Dark Army does not host; permission verdicts for
the hook broker or channel; launches. `_answering` and the per-handle
`session_io._pty_lock` stay as defence in depth.
