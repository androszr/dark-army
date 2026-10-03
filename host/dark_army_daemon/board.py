"""The Kanban board's store — cards, columns, and nothing else.

Everything else Dark Army holds is a reading of something that is already happening:
sessions, transcripts, rate-limit windows. This is the first thing in the app
that a person *writes*, and it is the only state here whose truth is not
recoverable from the machine if the file is lost. Hence a database rather than a
JSON blob overwritten wholesale, and hence the same shape `history.py` settled
on: one connection, `check_same_thread=False`, a `threading.Lock` around every
statement, WAL so a read never blocks a write (its file capped at
`WAL_SIZE_LIMIT_BYTES`), and `busy_timeout` so the two never surface as
"database is locked".

**Synchronous by design.** sqlite3 blocks and the daemon shares one event loop
with every surface, so nothing here awaits — the caller owes the executor hop
(`BobDaemon._board_call`). A board write on the loop is a stall in the thing that
drives the menu bar, the panel and the terminal titles.

**What this file deliberately does not do.** It does not know what a session is,
it cannot spawn one, and it never decides that a card is finished. Dispatch lives
in `dispatch.py` and is reached only from `BobDaemon.dispatch_card`; the link
between a card and a running session is written *into* here by the daemon's
reconcile and is never derived here. Keeping the store ignorant is what makes
"can anything on the board start a process?" answerable by reading one other
file.

**Forward compatibility is a rule, not an aspiration.** A card is read with
`SELECT *` into a dict and unknown keys are ignored; a write only ever names the
columns it means to change. So a database written by a newer build still opens in
an older one, and a row an older build touched keeps the newer build's fields —
the same contract `sessions.json` and `preferences.json` live under, and the
reason the panel decodes through tolerant helpers.
"""

from __future__ import annotations

from . import areas

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

from . import attachments, board_outcomes, merges, scout_report, work_record, worktrees
from .board_lifecycle_store import LifecycleStoreMixin
from .board_outcome_store import OutcomeStoreMixin
from .board_queue import queue_key
from .knowledge_store import KnowledgeStoreMixin
# A constant, never a capability. `dispatch` imports only `agents_poll`,
# `grok_leader` and `vscode_reveal`, none of which import `board`, so this
# is acyclic; and what it brings the store is a frozen dict of model names,
# so "the board knows nothing about sessions and can spawn nothing" still
# holds. Never call a launcher function from here. `normalise_root` is the
# folder canonicaliser, pure path arithmetic, and the one spelling of "same
# project" the dependency refusal in `_update_locked` shares with the queue.
from .dispatch import MODELS, normalise_root
from .paths import BOARD_PATH, STATE_DIR, ensure_state_dir

logger = logging.getLogger("dark-army.board")

#: v16 adds one column: `revision`, the card's change number.
#: v18 adds one column: `priority`, the card's 0..100 importance number.
#: v19 adds one column: `crew_trail`, which character took each observed stage.
#: v20 adds one table: the per-project knowledge notes (`knowledge_store.py`).
#: No `cards` column, `card_runs`' reason — an older build reads a
#: 20-stamped file and simply never sees the table.
#: The knowledge_entries column set (including last_confirmed / stale /
#: source) lives in the mixin; v23 is the delivery area. Those three are
#: PRAGMA-driven ALTERs in `_connect_knowledge`, not a cards migration.
#: v21 adds the lifecycle timing ledger (`board_lifecycle_store.py`): attempts,
#: boundaries, episodes, confirmed spans. No `cards` column and no cascade
#: from Clear Done; an older build reads a 21-stamped file and never sees it.
#: v22 retires the folders: `_retire_initiatives` empties the `initiatives`
#: table and clears `initiative_id` / `initiative_by`, and never drops them.
#: An older build still SELECTs the table and INSERTs a card naming the
#: columns; dropping either made that build treat the whole board as missing.
#: v24 adds three `card_runs` columns — `shunt_delegations`,
#: `shunt_lines_kept_out`, `shunt_worker_cost_usd` — through the ADD COLUMN
#: list, each with a DEFAULT, so a v23 build's `close_run` UPDATE still lands
#: and a v24 build reading a v23 row sees zero delegations and no cost.
#: v25 adds two `cards` columns, `kind` and `report_path`, through the ADD
#: COLUMN list.
#: v26 adds one `cards` column, `manual_check_path`, through the ADD COLUMN
#: list: the check file `flag_manual` was handed, `''` for none.
#: v27 adds two `cards` columns, `batch_id` and `batch_rank`, through the ADD
#: COLUMN list: one session working an ordered list of cards (a batch
#: refinement today), `''` on every card no batch press wrote.
#: v28 adds two `cards` columns, `report_verdict` and
#: `report_recommendation`, through the ADD COLUMN list: the attached
#: report's answer block as `attach_report` read it, `''` where it had none.
#: v29 adds no column: `blocked_by` gates Start again (card dependencies,
#: `docs/card-dependencies.md`), so any value a build from before 20 Sep 2026
#: left in it is emptied once (`_clear_retired_blocked_by`) rather than
#: suddenly holding a card nobody linked. It also adds one column,
#: `manual_session_id`: the session whose `flag_manual` wrote the steps, so a
#: dependency reads as finished only while that run is the one bound.
#: v30 adds two `cards` columns, `worktree_path` and `worktree_branch`,
#: through the ADD COLUMN list: the folder and branch a started card works in
#: (`docs/card-worktrees.md`), `''` on every card that works in the main
#: checkout. Written by `record_worktree` / `clear_worktree` alone.
#: v31 adds four `cards` columns, `merge_state` / `merge_note` (what the last
#: MERGE press came to) and `review_verdict` / `review_tip` (a review's
#: verdict and the branch version it judged), through the ADD COLUMN list
#: (`docs/card-worktrees.md`, *Review and merge*). Written by `record_merge`
#: and `record_review_verdict` alone.
SCHEMA_VERSION = 31

#: Ceiling for board.db-wal, applied per connection in `connect()`. SQLite
#: reuses a WAL file from its start after a checkpoint but never shrinks
#: it, so the coalesce VACUUM in `_coalesce_spans_once` once left 177 MB
#: on disk for the daemon's lifetime. Eight MiB is twice the default
#: `wal_autocheckpoint` (1000 pages x 4 KiB): ordinary traffic never
#: reaches it, so the file is only ever cut after a one-off big job.
WAL_SIZE_LIMIT_BYTES = 8 * 1024 * 1024

#: `BoardStore.create` returns this as `detail` when a second create names a
#: token the store already holds. Compared to the constant, never as a
#: substring — the refine-on-create skip keys on it.
ALREADY_CREATED = "already created"

#: The columns, four of them. Not configurable: every surface — the board
#: window, the queued count on the menu bar, the reconcile that moves a
#: dispatched card — is written against these names, and a fifth would be a
#: change to all of them rather than a row in a settings file.
#:
#: **`ready` was a fourth once and it is gone (schema 2).** It sat between
#: Backlog and In progress and meant "a human has decided this should be done"
#: — which is a distinction nobody could read off the board, because Backlog
#: already means exactly that and Done already means the opposite. Two columns
#: for one idea is how a board stops being pointable at. What Ready was really
#: for was the *dispatch gate*: a card had to be moved there before Start would
#: touch it, so the move was the deliberation. That job is now the drop itself —
#: dragging a card into In progress **is** the confirmation, and it starts the
#: assistant the card names (see `daemon.dispatch_card`). `_retire_ready`
#: folds any surviving Ready card back into Backlog on first open.
#:
#: **`prep` (schema 6) is not Ready returned**, and the difference is checkable
#: rather than asserted. Ready was a waiting room for a button: nothing but a
#: hand ever moved a card out of it, and its whole meaning was "the button may
#: now be pressed". Prep has its **own verb** — Refine, which dispatches a
#: planning session onto the card (`daemon.refine_card`) — and its **own exit
#: condition**: `attach_plan` writes the plan the refinement produced and moves
#: the card to Backlog in the same statement. So the two piles answer different
#: questions with one line each: Prep is "written down, not yet turned into a
#: plan" (`plan_path` empty), Backlog is "planned, waiting to be picked up"
#: (`plan_path` set, or old enough to predate the field). Every newly created
#: card defaults into Prep; existing cards were not moved on the upgrade.
#: A schema-5 build opening this file keeps working — it INSERTs without the
#: new columns (all defaulted), its `counts()` silently drops `prep` from the
#: count and its panel draws no Prep column, but nothing is destroyed and
#: nothing is rewritten. That degradation is accepted, not fixed.
COLUMNS = ("prep", "backlog", "in_progress", "done")

#: What a card's *refinement* is doing — the planning session Refine dispatched
#: onto it. Deliberately a separate field from `link_state`: `bind_session`
#: moves a card to In progress, which a refining card must never do, so the
#: refinement's lifecycle cannot ride the dispatch's field. `""` (never
#: refined, or cleared), `dispatching` (Refine pressed, no session bound yet),
#: `live` (the planning session is in the fleet), `ended` (it left and the
#: plan never arrived — Refine is offered again).
REFINE_STATES = ("", "dispatching", "live", "ended")


def with_ready_alias(counts: dict) -> dict:
    """One-generation compatibility: `ready` is an alias of `backlog`,
    not a fourth column. Surfaces shipped against schema 1 still index
    `counts["ready"]`."""
    out = dict(counts)
    out["ready"] = int(out.get("backlog") or 0)
    return out


def review_only(state: dict) -> dict:
    """The board section with the withheld Done cards removed.

    One spelling of "the lighter board", read by `api_server` alone. A Done
    card carries `done_preview` when it reached the snapshot through the
    recent-preview leg **alone** — the awaiting-review leg never stamps it —
    so this drops exactly the finished cards nobody is waiting on and keeps
    every card that still wants a person. It re-derives no judgment of its
    own: `BoardStore.done_awaiting_review`'s SQL decides, and this reads the
    record of which read produced the row.
    """
    cards = state.get("cards") or []
    return dict(state,
                cards=[c for c in cards if not c.get("done_preview")])


#: Which assistant a card names. `""` means nobody yet, which is a legitimate
#: state — a card can be written before it is decided who should take it — and
#: it is what makes Start absent rather than broken.
TOOLS = ("", "claude", "codex", "grok")

#: What a card's `session_id` link is doing. `""` (never dispatched),
#: `dispatching` (Start pressed, no session bound yet), `live` (the snapshot
#: lists it), `ended` (it does not, and has not for the grace).
#: Deliberately *not* a copy of the session's category: `running`/`waiting`/
#: `sleeping` are read live off the agents snapshot by whoever draws the card,
#: so the board can never disagree with the rest of Dark Army about what an assistant
#: is doing.
LINK_STATES = ("", "dispatching", "live", "ended")

#: The per-project work queue, at v8. `queued` means a person made the Start
#: gesture on this card and its declared files collide with work already
#: running in the same project — so Dark Army is holding it rather than opening a
#: second terminal in one tree. Two states only: a card is in the queue or it
#: is not. Written by the daemon alone (`_WRITABLE`, and excluded from
#: `ApiServer._BOARD_FIELDS`), because a surface that could *set* it would be
#: manufacturing a claim on Dark Army's future auto-start; a surface may only ever
#: **clear** it, through the dedicated `board_unqueue` action —
#: `board_reset`'s clear-never-set precedent.
QUEUE_STATES = ("", "queued")

#: How many cards one project may hold in its queue. Refused at the store like
#: `MAX_CARDS`, so every route inherits it. Past eight queued cards a person is
#: batching, and Backlog is the surface for a batch — the queue is a live
#: pipeline, not a second backlog. It also bounds the per-pass interference
#: work the drain does (at most eight plan reads per project, all mtime-cached)
#: and the pipeline band the panel draws. Eight matches `MAX_BLOCKERS`'
#: calibration: past any real pile.
MAX_QUEUED_PER_PROJECT = 8

#: How many cards one batch press may hand one session (`refine_cards`; the
#: batch-implement sibling shares it). Eight is `MAX_QUEUED_PER_PROJECT`'s
#: calibration: a batch is one project's pile, and past eight a person is
#: better served by two sittings than by one interview that never ends.
MAX_BATCH_CARDS = 8

#: Refused at the store rather than at a surface, so every route inherits the
#: bound — including the channel tool, which is reachable by anything on the
#: machine that can open a socket (see `daemon._handle_channel_message`).
#: 500 cards is far past the point a board is useful.
#:
#: **It is not, as this comment used to claim, well short of the point the SSE
#: frame is a problem** — measured: every card's full prompt rides `/api/state`
#: and therefore every frame, so 20 cards of 8,000 characters is 165 KB and the
#: 500 allowed here is 4.1 MB, against an `api_server` docstring tuned around a
#: ~19 KB snapshot. The bound is still the right bound; the reasoning was wrong
#: and is corrected rather than deleted. The fix, when it is done, is to
#: truncate the prompt in the snapshot: `GET /api/board` already serves the full
#: text, so the editor loses nothing.
MAX_CARDS = 500
#: A prompt is handed to an agent as one argv element. 8000 characters is longer
#: than anybody types and shorter than anything that could be mistaken for a file.
MAX_PROMPT_CHARS = 8000
MAX_TITLE_CHARS = 200
#: The stage track's two bounds. A card's `workflow` (what it says it expects)
#: and its `agent_trail` (what actually ran) are both newline-separated lists of
#: names, and both ride the SSE frame, so both are bounded here rather than at a
#: surface — the same rule `MAX_CARDS` and `MAX_PROMPT_CHARS` live under.
#:
#: 12 is well past any real pipeline (`/ship` runs five). The per-entry clamp
#: matters more than the count: a stage name is free text arriving from three
#: different assistants, and Codex synthesises its own.
MAX_STAGES = 12
MAX_STAGE_CHARS = 48
#: The plain-language line. A paragraph of plain words is legitimate; the card
#: face's three-line cap is what keeps the column glanceable now, and the
#: snapshot trim is what keeps the frame bounded. Deliberately *not* the prompt:
#: it is what somebody who does not read code sees on the card and at the top of
#: the detail sheet.
MAX_SUMMARY_CHARS = 2000
#: The one sentence a reader of the Done column sees under "closed by …". It is
#: a *statement somebody made*, not a report: the whole point of the line is
#: that it can be taken in at a glance beside the card it closed, and anything
#: longer belongs in the transcript the session left behind. Clamped rather than
#: refused (see `declare_done`) — this is Dark Army's own record of somebody's words,
#: not instructions about to be handed to an agent, so the
#: refuse-don't-truncate argument that governs `MAX_PROMPT_CHARS` does not
#: apply here.
MAX_CLOSE_NOTE_CHARS = 400
#: The steps somebody still has to carry out by hand, at v9. Bounded because
#: they ride every SSE frame that carries the card — `_trim_card_for_snapshot`
#: clamps the *prompt* and this sits inside the budget that leaves, so no frame
#: arithmetic changes. Clamped rather than refused, `MAX_CLOSE_NOTE_CHARS`'
#: reason exactly: this is a note Dark Army is relaying to a person, not instructions
#: about to be handed to an agent, so a long one truncated is still readable
#: where a long one refused is a check nobody hears about. A thousand characters
#: is four or five numbered steps plus the reason, which is already past the
#: point where the check should have been a test.
MAX_MANUAL_STEPS_CHARS = 1000
#: How many other cards one card may wait on. A board is not a dependency
#: graph tool; eight is past any real "this needs those" pile and short of a
#: string that would bloat the snapshot the way an unbounded prompt did.
MAX_BLOCKERS = 8
#: How long one id in that list may be. Card ids are 32 hex; the bound keeps a
#: `board_update` from storing a 200 KB "id" that every frame would then carry.
MAX_CARD_ID_CHARS = 64
#: Per-card chat thread, at v12. A new table rather than columns on `cards`,
#: because a thread is unbounded text and the forward-compatibility rule is
#: cheapest that way: a v11 build never touches `card_messages`, and this
#: build's `connect()` sweep collects orphans a v11 `delete()` left behind.
#: 200 messages is past any real "did you take X into account" thread and
#: short of a dump; 4000 characters is the question about to be handed to an
#: agent (refused over the limit) and Dark Army's record of an answer (clamped).
MAX_THREAD_MESSAGES = 200
MAX_MESSAGE_CHARS = 4000
MESSAGE_KINDS = ("question", "answer", "note")
#: How a message reached (or came from) the card. `"terminal"` means *typed
#: onto the session's own input line* — `board_message`, the keystroke route,
#: as distinct from `"session"`'s channel push and `"consultant"`'s helper.
#: A write-time validator only: `messages` validates nothing on read, so an
#: older daemon reads a `terminal` row through `SELECT *` unharmed and an
#: older panel falls through to its plain branch. No `CHECK` constraint.
MESSAGE_VIAS = ("", "session", "consultant", "terminal")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One unit of work somebody wrote down. `column_name`, not `column`: the latter
-- is a SQL keyword and quoting it everywhere is a footgun with no upside.
CREATE TABLE IF NOT EXISTS cards (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    root            TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    -- The business line: what this card means to somebody who will never open
    -- the prompt. Separate from `title` (which is a label) and from `prompt`
    -- (which is machine input) because it is the only one of the three written
    -- for a reader rather than for a list or for a CLI.
    summary         TEXT NOT NULL DEFAULT '',
    prompt          TEXT NOT NULL DEFAULT '',
    tool            TEXT NOT NULL DEFAULT '',
    column_name     TEXT NOT NULL,
    position        REAL NOT NULL DEFAULT 0,
    session_id      TEXT NOT NULL DEFAULT '',
    link_state      TEXT NOT NULL DEFAULT '',
    dispatch_error  TEXT NOT NULL DEFAULT '',
    dispatched_at   REAL DEFAULT NULL,
    session_ended_at REAL DEFAULT NULL,
    author          TEXT NOT NULL DEFAULT 'user',
    -- The stage track. `workflow` is what the card says it expects (declared by
    -- whoever wrote it, never inferred); `agent_trail` is what Dark Army actually saw
    -- run. Newline-separated names, and the two are deliberately kept apart: a
    -- hollow "still to come" marker may only ever come from the first.
    workflow        TEXT NOT NULL DEFAULT '',
    agent_trail     TEXT NOT NULL DEFAULT '',
    -- Who was on it, at v19. `stage\tcharacter` per line, one line per entry
    -- in `agent_trail`: the cast member Dark Army allocated when that stage was
    -- first observed. Written by `record_agents` alone, and **never rewritten
    -- for a stage already in it** — that is the whole memory: a finished part
    -- of the job keeps the face that did it even after that character has
    -- moved on to somebody else's card. Outside `_WRITABLE`, `REVISED_COLUMNS`
    -- and `ApiServer._BOARD_FIELDS`: a surface that could write it could put a
    -- face on a stage that never ran.
    crew_trail      TEXT NOT NULL DEFAULT '',
    -- Dark Army's record of *somebody's statement*: which session declared the work
    -- finished, and the one sentence they said it with. Not a fact Dark Army
    -- established — nothing here inspects the work — which is why the column
    -- names the speaker as well as the words, and why the panel draws them as
    -- "closed by …" rather than as a tick. Written by `declare_done` alone and
    -- cleared by `update` when a card leaves Done; deliberately outside
    -- `_WRITABLE`, so no surface can paint a hand-dragged card with a
    -- signature nobody made.
    closed_by       TEXT NOT NULL DEFAULT '',
    close_note      TEXT NOT NULL DEFAULT '',
    -- The outstanding hand-check, at v9. Non-empty means *somebody still has
    -- to check this*, and the text **is** the steps — one column rather than a
    -- boolean plus a note, because two columns for one fact is two things that
    -- can disagree, and a flag with no steps behind it is the state this
    -- column exists to make impossible. Written by `flag_manual` alone (the
    -- session that did the work, through the channel) and emptied by
    -- `clear_manual` alone (a person pressing Mark checked); outside
    -- `_WRITABLE` for `closed_by`'s reason, so a surface may clear one and can
    -- never stamp somebody else's card with a chore.
    manual_steps    TEXT NOT NULL DEFAULT '',
    -- The check file the flag named, at v26: the realpath of a
    -- `manual-check/<date>-<slug>/check.md` under the card's root, or ''.
    -- Written by `flag_manual` alone, beside `manual_steps`, so the card,
    -- the Checks section and the outcome verb can reach one another.
    -- Deliberately single-spaced so the CREATE and ALTER spellings can be
    -- pinned against each other by a grep, `area`'s own comment.
    manual_check_path TEXT NOT NULL DEFAULT '',
    -- The session that wrote those steps, at v29, by `flag_manual` alone and
    -- emptied with them by `clear_manual`. A rework run re-binds a new
    -- session while the old steps stay on the card; a card dependency is
    -- finished-and-waiting only while the flagging run is still the bound
    -- one (`board_queue.dependency_met`). '' on a flag from before v29.
    -- Deliberately single-spaced so the CREATE and ALTER spellings can be
    -- pinned against each other by a grep.
    manual_session_id TEXT NOT NULL DEFAULT '',
    -- The human acknowledgement of an assistant's close, at v10. NULL means
    -- nobody has looked at the finished work yet, which is what pins the card
    -- to the top of Done wearing its review banner; a timestamp means a person
    -- pressed Reviewed and the card may sink into the ordinary archive.
    -- Written by `mark_reviewed` alone and cleared by `update` when a card
    -- leaves Done (Reopen undoes the whole close); outside `_WRITABLE`, so no
    -- generic Save can silently acknowledge a review the person did not make.
    -- Nullable for `queued_at`'s reason — there is no zero time that means
    -- "not yet reviewed" — and deliberately single-spaced so the CREATE and
    -- ALTER spellings can be pinned against each other by a grep.
    reviewed_at REAL DEFAULT NULL,
    -- Cards this one waits on, at v5. Newline-separated ids, same shape as
    -- `workflow`. A finished card never blocks anyone; missing ids stay in the
    -- string (a later restore should still mean something) and are ignored at
    -- read time. Cycle, self and same-project checks live in `update`, not
    -- here; what it gates is `docs/card-dependencies.md` (emptied once, v29).
    blocked_by      TEXT NOT NULL DEFAULT '',
    -- The refinement, at v6. `plan_path` is the exit condition of the Prep
    -- column — written by `attach_plan` alone, never through `update`, so no
    -- surface can stamp a card "planned" without a plan having actually been
    -- attached. `refine_session_id`/`refine_state` are the planning session's
    -- own link, kept apart from `session_id`/`link_state` because
    -- `bind_session` moves a card to In progress and a refining card must
    -- never move.
    plan_path       TEXT NOT NULL DEFAULT '',
    refine_session_id TEXT NOT NULL DEFAULT '',
    refine_state    TEXT NOT NULL DEFAULT '',
    -- One session, an ordered list of cards, at v27. `batch_id` is the token
    -- one batch press minted and wrote on every card it handed that session;
    -- `batch_rank` is the card's 1-based place in that order ('2'). Written
    -- beside the link fields a press already writes and cleared with them, so
    -- `_launch_inflight` counts one session once. Daemon bookkeeping, on
    -- `session_id`'s ring. Deliberately single-spaced so the CREATE and ALTER
    -- spellings can be pinned against each other by a grep.
    batch_id TEXT NOT NULL DEFAULT '',
    batch_rank TEXT NOT NULL DEFAULT '',
    -- The approval of a plan *version*, at v15. `plan_approved` is the
    -- SHA-256 of the plan file's bytes as they were when a person read them
    -- and said yes; `plan_approved_at` is when they said it. Two columns
    -- rather than a boolean because the whole point is *which wording* was
    -- approved: a bare flag would still be true after the file moved under
    -- the card, which is the state this pair exists to make impossible.
    -- Written by `approve_plan` alone (`plan_path`'s ring, outside
    -- `_WRITABLE`), so no generic Save can stamp a card as approved against
    -- a plan nobody read. Empty **never gates** — approval is opt-in, and
    -- every card on every existing board carries ''. `plan_approved_at` is
    -- nullable for `reviewed_at`'s reason (there is no zero time that means
    -- "not approved") and both are deliberately single-spaced so the CREATE
    -- and ALTER spellings can be pinned against each other by a grep.
    plan_approved TEXT NOT NULL DEFAULT '',
    plan_approved_at REAL DEFAULT NULL,
    -- The card's change number, at v16. Steps up whenever a write changes
    -- one of `REVISED_COLUMNS` — what a *person* reads on the card, never
    -- Dark Army's bookkeeping. It is the store's own (`create_token`'s ring: the
    -- store writes it and no verb is named for it), so it is in neither
    -- `_WRITABLE` nor `SINGLE_WRITER`, and a surface that could set it could
    -- walk a stale save past the guard it exists to arm. `INTEGER NOT NULL
    -- DEFAULT 0` is `outcome_revision`'s precedent, so a v15 build's INSERTs
    -- keep working and a downgrade simply stops guarding. Deliberately
    -- single-spaced so the CREATE and ALTER spellings can be pinned against
    -- each other by a grep, `plan_approved`'s own comment.
    revision INTEGER NOT NULL DEFAULT 0,
    -- The standing instruction "start this the moment its plan lands", at
    -- v17. A thing a person states (ring 1, `_WRITABLE`), like `tool` and
    -- `model`: `''` is off and `'1'` is on, and there is no third value —
    -- `create` and `_update_locked` normalise every incoming shape onto
    -- those two, because `ApiServer._board_fields` stringifies every
    -- allow-listed key and a JSON `true` therefore arrives as `"True"`.
    -- TEXT rather than INTEGER because every boolean-ish column in this
    -- store already is (`queue_state`, `plan_approved`),
    -- and the whole API pipeline already carries TEXT end to end.
    -- The daemon clears it at the moment it acts on it — `queue_state`'s
    -- precedent for the one piece of bookkeeping a ring-1 column carries —
    -- so the tick is spent on the *attempt*, and a card sent back and
    -- planned again has to be ticked again on purpose. Deliberately NOT
    -- spelled `auto_start`: `board_autostart` already means "the queue
    -- drain may start the head of the line", one line away in the same
    -- snapshot. Deliberately single-spaced so the CREATE and ALTER
    -- spellings can be pinned against each other by a grep, `model`'s own
    -- comment.
    start_when_planned TEXT NOT NULL DEFAULT '',
    -- How important this piece of work is, 0..100, at v18. A thing a person
    -- states (ring 1, `_WRITABLE`), on `model`'s and `tool`'s side of the
    -- line: Dark Army suggests one once per card and a person may retype or empty
    -- it at any time. TEXT rather than INTEGER because `_ADDED_COLUMNS`'
    -- own stated rule is that every added column is TEXT NOT NULL DEFAULT
    -- '' — and because the empty string is load-bearing here:
    --
    --   `''` means "nobody has scored this"; `'0'` means "scored, lowest".
    --
    -- They are distinguishable on the card face (`''` draws nothing, `'0'`
    -- draws `P 0`) and deliberately indistinguishable in the sort:
    -- `CAST('' AS INTEGER)` is 0 in SQLite, so an unscored card sorts
    -- exactly where a card scored 0 does — the bottom of its column. An
    -- INTEGER column would need a sentinel every reader has to know about,
    -- and `ApiServer._board_fields`' `str(... or "")` coercion would mangle
    -- it. Deliberately single-spaced so the CREATE and ALTER spellings can
    -- be pinned against each other by a grep, `model`'s own comment.
    priority TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    -- The card's kind, at v25. `''` is a build card (never published as the
    -- word "ship"); `'scout'` is an investigation ending in a report. A
    -- thing a person states (ring 1, `_WRITABLE`), chosen at creation and
    -- editable only in Prep. `report_path` is the scout's written result,
    -- written by `attach_report` alone (`plan_path`'s ring). Deliberately
    -- single-spaced so the CREATE and ALTER spellings can be pinned against
    -- each other by a grep, `area`'s own comment.
    kind TEXT NOT NULL DEFAULT '',
    report_path TEXT NOT NULL DEFAULT '',
    -- The attached report's answer block, at v28: its one-line verdict and
    -- its recommendation token, as `attach_report` read them at the attach
    -- (that verb alone writes them, `report_path`'s ring). A display record
    -- for the card face, never Promote's input: Promote re-reads the file
    -- at the press, and where the two disagree the file wins for Promote
    -- and these win for the face. `''` where the report had no block.
    report_verdict TEXT NOT NULL DEFAULT '',
    report_recommendation TEXT NOT NULL DEFAULT '',
    -- The card's own worktree, at v30: the folder under
    -- `<root>/.worktrees/` its terminal opens in and the branch checked out
    -- there (`docs/card-worktrees.md`). Daemon bookkeeping, written by
    -- `record_worktree` and emptied by `clear_worktree` alone — never
    -- `_WRITABLE`, never an API field, never counted by `revision`. `''`
    -- where the card works in the main checkout. Deliberately
    -- single-spaced so the CREATE and ALTER spellings can be pinned
    -- against each other by a grep.
    worktree_path TEXT NOT NULL DEFAULT '',
    worktree_branch TEXT NOT NULL DEFAULT '',
    -- Review and merge, at v31 (`docs/card-worktrees.md`): what the last
    -- MERGE press came to (`merges.STATES`) with its sentence, and a
    -- review's verdict (`merges.VERDICTS`) with the branch tip it judged.
    -- Daemon bookkeeping, written by `record_merge` and
    -- `record_review_verdict` alone — never `_WRITABLE`, never an API
    -- field, never counted by `revision`.
    merge_state TEXT NOT NULL DEFAULT '',
    merge_note TEXT NOT NULL DEFAULT '',
    review_verdict TEXT NOT NULL DEFAULT '',
    review_tip TEXT NOT NULL DEFAULT '',
    -- Retired at v22 as unused '' text. Kept so an older build's INSERT
    -- and SELECT still work. This build never reads or writes them.
    initiative_id   TEXT NOT NULL DEFAULT '',
    initiative_by   TEXT NOT NULL DEFAULT '',
    -- The per-project work queue, at v8. `queue_state` is membership ('' or
    -- 'queued') and `queued_at` is the record of the person's Start gesture —
    -- FIFO by the moment they made it. The drain now obeys the coalesced key
    -- `COALESCE(queue_rank, queued_at, 0)`, then id; a drag never restamps
    -- this. Both are the daemon's own bookkeeping in `session_id`'s ring:
    -- store-writable so the dispatch path can set them through `update`, and
    -- excluded at the API layer so no surface can put a card in a queue Dark Army
    -- will later act on. `queued_at` is nullable rather than NOT NULL, which
    -- `_ADDED_COLUMNS`' rule allows and `dispatched_at` is the precedent for:
    -- there is no zero time that means "not queued".
    queue_state     TEXT NOT NULL DEFAULT '',
    queued_at       REAL DEFAULT NULL,
    -- The person's preferred place in line, at v11. NULL means never dragged,
    -- which is why this is nullable rather than NOT NULL — there is no rank
    -- value that means "use the enqueue stamp". Written by `move_queued`
    -- alone (`plan_path`'s ring, outside `_WRITABLE`) and cleared by the
    -- store whenever `queue_state` becomes `''`. A hand-set rank lives on
    -- the same axis as the enqueue stamps (epoch seconds). Deliberately
    -- single-spaced so the CREATE and ALTER spellings can be pinned against
    -- each other by a grep, `reviewed_at`'s own comment.
    queue_rank REAL DEFAULT NULL,
    -- Files attached while the card was written, at v11. Newline-separated
    -- relative paths (`<staging-folder>/<sanitised-name>`), the same shape
    -- as `workflow` and `blocked_by`. The sanitised charset cannot contain
    -- a newline, so the separator is safe by construction. Written by a
    -- person (ring 1, `_WRITABLE`); the panel copies the bytes, the daemon
    -- re-checks the shape at every write and the files at every use.
    -- `dark_army_add_card` gains no attachments argument — an unauthenticated
    -- socket must not create durable objects against a bounded resource.
    attachments     TEXT NOT NULL DEFAULT '',
    -- Which model the card's assistant runs on, at v13. `''` means Default —
    -- launch with no model flag at all — and is always legal; anything else
    -- must be in `dispatch.MODELS[tool]`, checked at `create` and `update` so
    -- every route inherits it. A thing a person states (ring 1, `_WRITABLE`),
    -- and the store clears it itself when the card is retooled, so no route
    -- can leave a grok model on a claude card. `dark_army_add_card` gains no model
    -- argument: an unauthenticated socket must not choose which differently
    -- priced model a later human Start spends money on. Deliberately
    -- single-spaced so the CREATE and ALTER spellings can be pinned against
    -- each other by a grep, `queue_rank`'s own comment.
    model TEXT NOT NULL DEFAULT '',
    -- Client create-idempotency token. A retried `board_create` with the
    -- same staging id returns this row, unchanged. Empty is "no token";
    -- the partial unique index is what makes many tokenless cards legal.
    -- Written by `create` alone, outside `_WRITABLE`. Deliberately
    -- single-spaced so the CREATE and ALTER spellings can be pinned
    -- against each other by a grep, `model`'s own comment.
    create_token TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    done_at         REAL DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS cards_by_column ON cards(column_name, position);
CREATE INDEX IF NOT EXISTS cards_by_session ON cards(session_id);
-- Partial unique: empty token is "no token", so many tokenless cards stay
-- legal. A plain UNIQUE on '' would allow only one. Older files gain the
-- column via ALTER first; `connect()` runs this statement after that.
CREATE UNIQUE INDEX IF NOT EXISTS cards_by_create_token ON cards(create_token) WHERE create_token != '';

-- Retired at v22. The table stays so an older build's SELECT * FROM
-- initiatives still runs; this build never reads it and empties it on
-- the upgrade. A dropped table made that older snapshot treat the whole
-- board as missing.
CREATE TABLE IF NOT EXISTS initiatives (
    id         TEXT PRIMARY KEY,
    project    TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,
    position   REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS initiatives_by_project ON initiatives(project, position);

-- The per-card question-and-answer thread, at v12. A table of its own so a
-- v11 build's `SELECT * FROM cards` never sees it and cannot blank it.
-- `kind` is question | answer | note; `via` is '' | session | consultant —
-- consultant is a helper session that was never the card's `session_id`.
CREATE TABLE IF NOT EXISTS card_messages (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'user',
    kind TEXT NOT NULL DEFAULT 'question',
    via TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_card ON card_messages(card_id, created_at);

-- What Dark Army observed one run of a card do, at v15. A table rather than columns
-- on `cards`, for `card_messages`' reason and one sharper: the report text is
-- unbounded prose and a v14 build's `SELECT * FROM cards` must not be able to
-- see it, let alone blank it on the next write. It arrives by
-- `CREATE TABLE IF NOT EXISTS` inside `connect()`; columns added since
-- (v24's three `shunt_*` figures) come through `_ADDED_COLUMNS["card_runs"]`,
-- each with a DEFAULT, so an older build's UPDATE still lands.
--
-- **One row per card.** The interview settled "the latest replaces the
-- previous"; a *history* of runs is what `event_log` and the outcome ledger
-- already are, and a second one here would be a third answer about the same
-- day. The run's identity is the card's own `dispatched_at`, kept as `run_at`
-- — already the bind-window clock, already restart-safe, and no new id minted.
--
-- Written by `open_run` / `close_run` alone. `card_runs` names no member of
-- `_WRITABLE` and no key in `ApiServer._BOARD_FIELDS`, so no panel, phone,
-- channel tool or LAN door can write a word of it. That matters more here
-- than for `closed_by`: a record a surface could write is a record that
-- proves nothing, and the whole point of this one is that a person can trust
-- it without trusting the agent.
--
-- The three `shunt_*` columns are what the shunt skill's helper did during
-- the run (v24), folded off the session's delegation ledger by `close_run`.
-- The cost is NULL — never 0 — where any delegation reported no figure.
-- No comment sits inside the column list on purpose: SQLite's `DROP
-- COLUMN` rewrites this text, and Ubuntu 24.04's 3.45 chokes on an
-- apostrophe in a `--` line it has just cut around ("incomplete input";
-- `test_work_record.py`'s v23 downgrade test, in CI on 20 Sep 2026).
CREATE TABLE IF NOT EXISTS card_runs (
    card_id TEXT PRIMARY KEY,
    run_at REAL NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    root TEXT NOT NULL DEFAULT '',
    baseline TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT '',
    report TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '',
    files_available INTEGER NOT NULL DEFAULT 0,
    files_reason TEXT NOT NULL DEFAULT '',
    files_changed INTEGER NOT NULL DEFAULT 0,
    files_total INTEGER NOT NULL DEFAULT 0,
    lines_added INTEGER NOT NULL DEFAULT 0,
    lines_removed INTEGER NOT NULL DEFAULT 0,
    recorded_at REAL DEFAULT NULL,
    shunt_delegations INTEGER NOT NULL DEFAULT 0,
    shunt_lines_kept_out INTEGER NOT NULL DEFAULT 0,
    shunt_worker_cost_usd REAL DEFAULT NULL
);
"""


def _clamp(text, limit: int) -> str:
    return str(text or "")[:limit]


def parse_stages(value) -> list:
    """A stored stage list back into names. Newline-separated, order preserved.

    Tolerant on the way in — a list, a newline string or a comma string all
    arrive here, because `workflow` is set by three callers (the panel's editor,
    the channel tool, a test) and refusing one of those shapes would be a
    refusal a person has to debug rather than a list that works.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        text = str(value)
        parts = text.split("\n") if "\n" in text else text.split(",")
    out: list = []
    for part in parts:
        name = _clamp(part.strip(), MAX_STAGE_CHARS)
        if name and name not in out:
            out.append(name)
        if len(out) >= MAX_STAGES:
            break
    return out


def join_stages(names) -> str:
    """The stored form. Bounded here as well as in `parse_stages`, so a value
    that reached a writer by some other route still cannot exceed the column's
    contract."""
    return "\n".join(parse_stages(names))


def parse_crew(value) -> dict:
    """A stored crew back into ``{stage: character}``. Order preserved.

    ``stage\tcharacter`` per line. A line with no tab, or with either half
    empty, is **ignored** rather than refused: this column is read on every
    snapshot frame and a ragged value must degrade to "no crew" — which draws
    the roles' anchor faces — not to a board that will not decorate.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        pairs = [(str(k), str(v)) for k, v in value.items()]
    else:
        pairs = []
        for line in str(value).split("\n"):
            if "\t" not in line:
                continue
            stage, _, character = line.partition("\t")
            pairs.append((stage, character))
    out: dict = {}
    for stage, character in pairs:
        name = _clamp(stage.strip(), MAX_STAGE_CHARS)
        who = _clamp(character.strip(), MAX_STAGE_CHARS)
        if not name or not who or name in out:
            continue
        out[name] = who
        if len(out) >= MAX_STAGES:
            break
    return out


def join_crew(mapping) -> str:
    """The stored form. Bounded here as well as in `parse_crew`."""
    return "\n".join(f"{stage}\t{who}" for stage, who in parse_crew(mapping).items())


def parse_ids(value) -> list:
    """A stored blocker list back into card ids. Newline-separated, de-duped.

    Copied from `parse_stages` rather than shared with it: a stage name is
    clamped and a card id is not, and requiring 32-hex here would drop the
    short ids tests and older rows still use. A missing/deleted id stays in
    the string; the daemon's dependency resolver reads a missing card as met
    (`daemon_board._dependency_entries`).
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        text = str(value)
        parts = text.split("\n") if "\n" in text else text.split(",")
    out: list = []
    for part in parts:
        name = part.strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= MAX_BLOCKERS:
            break
    return out


def join_ids(ids) -> str:
    """The stored form. Bounded here as well as in `parse_ids`."""
    return "\n".join(parse_ids(ids))


def normalise_flag(value) -> str:
    """A boolean-ish card column's two legal values: `'1'` or `''`.

    `ApiServer._board_fields` stringifies every allow-listed key, so a JSON
    `true` reaches the store as `"True"` and a client sending `"1"` / `""`
    is the intended shape. Both have to land on the same two values or the
    column stops being answerable with a comparison.
    """
    return "" if str(value or "").strip().lower() in (
        "", "0", "false", "no") else "1"


#: The top of the importance scale. 0..100 inclusive; the rubric the helper is
#: given (`card_priority.PROMPT_HEAD`) is anchored to these bounds.
MAX_PRIORITY = 100

#: What a bad number is refused with. One sentence, shown verbatim by both
#: surfaces exactly as the store's other refusals are.
PRIORITY_REFUSAL = "priority must be a whole number from 0 to 100, or empty"

#: The card's kind. `''` is a build card — the default, and the only value
#: published for one; the word `"ship"` is accepted on write and stored as
#: `''` so neither client ever has to compare against it. `"scout"` is an
#: investigation ending in a report.
KIND_SCOUT = "scout"
KINDS = ("", "scout")
KIND_REFUSAL = "kind must be build or scout"
KIND_LOCKED_REFUSAL = ("a card's kind is chosen in Prep — move it back to "
                       "change it")
SCOUT_PLAN_REFUSAL = "a scout takes a report, not a plan"
REPORT_NOT_SCOUT_REFUSAL = ("only a scout card takes a report — close a "
                            "build card or attach its plan")
#: The opening words of the refusal `attach_report_by_session` composes for
#: a report under the project's `scout/` folder that fails
#: `scout_report.check`; the problems follow it.
REPORT_MALFORMED_REFUSAL = "that report is not in the scout shape — "


def normalise_kind(value) -> tuple:
    """`(stored_value, refusal)`. `''` / `"ship"` store as `''`; `"scout"`
    stores as `"scout"`; anything else is refused in `KIND_REFUSAL`'s words.
    """
    text = str(value if value is not None else "").strip().lower()
    if text in ("", "ship"):
        return "", ""
    if text == KIND_SCOUT:
        return KIND_SCOUT, ""
    return "", KIND_REFUSAL


def normalise_priority(value) -> tuple:
    """`(stored_value, refusal)`. `''` is "not scored" and is always legal.

    `normalise_flag` is deliberately **not** reusable here: it maps `"0"` onto
    `""`, which is exactly the distinction this column exists to keep.

    The digit test is `isascii() and isdigit()`, not `isdigit()` alone —
    Arabic-Indic digits pass `isdigit()` and `int()` and would store a string
    no client's own `Int(...)` parses the same way.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return "", ""
    if not (text.isascii() and text.isdigit()):
        return "", PRIORITY_REFUSAL
    number = int(text)
    if number > MAX_PRIORITY:
        return "", PRIORITY_REFUSAL
    return str(number), ""


#: The single-writer ring, as a checkable table rather than prose: each column
#: here is written by exactly one named verb on `BoardStore`, and is therefore
#: excluded from `_WRITABLE` — a surface that could set one through `update`
#: could forge the very statement the column exists to be incapable of lying
#: about (a plan that was never attached, a stage that never ran, a verifier's
#: signature on a hand-drag, a chore nobody flagged, a review nobody made).
#: Tests pin all three legs: disjoint from `_WRITABLE`, every verb exists, and
#: the API's own allow-list stays inside `_WRITABLE`. `project` (relabel_root)
#: is deliberately absent — it is also legitimately written by surfaces, so it
#: lives in `_WRITABLE`, not this ring.
SINGLE_WRITER = {
    "plan_path": "attach_plan",
    "report_path": "attach_report",
    "report_verdict": "attach_report",
    "report_recommendation": "attach_report",
    "agent_trail": "record_agents",
    "crew_trail": "record_agents",
    "closed_by": "declare_done",
    "close_note": "declare_done",
    "manual_steps": "flag_manual",
    "manual_check_path": "flag_manual",
    "manual_session_id": "flag_manual",
    "reviewed_at": "mark_reviewed",
    "queue_rank": "move_queued",
    "plan_approved": "approve_plan",
    "plan_approved_at": "approve_plan",
    # The card's own worktree (v30): set by `record_worktree`, emptied by
    # `clear_worktree` — `manual_steps`' set/clear pair. A surface that
    # could write the path could aim the release's `git worktree remove`
    # at a folder Dark Army never made.
    "worktree_path": "record_worktree",
    "worktree_branch": "record_worktree",
    # Review and merge (v31): the merge pair is `record_merge`'s, the review
    # pair `record_review_verdict`'s. Leaving Done empties the merge pair
    # through `update()`, `closed_by`'s documented bypass.
    "merge_state": "record_merge",
    "merge_note": "record_merge",
    "review_verdict": "record_review_verdict",
    "review_tip": "record_review_verdict",
}

#: Which columns the card's `revision` counts.
#:
#: **The revision counts changes to what a person reads on the card, not
#: Dark Army's bookkeeping.** `session_id`, `link_state`, `dispatch_error`,
#: `dispatched_at`, `session_ended_at`, `queue_state`, `queued_at`,
#: `queue_rank`, `agent_trail`, `position`, `done_at`, `updated_at`,
#: `reviewed_at`, `plan_approved*`, `create_token` and the outcome columns are
#: deliberately **out**: the reconcile writes several of them every few
#: seconds, and a revision that moved on them would make the phone
#: re-download every card continuously *and* refuse a save because a session
#: bound itself while somebody was typing.
#:
#: The mirror rule is just as load-bearing and is pinned by a test: every
#: column in `ApiServer._BOARD_FIELDS` (bar the outcome ring, which has
#: `outcome_revision` of its own) must be in here, or a surface could change
#: a field the guard cannot see.
REVISED_COLUMNS = frozenset({
    "title", "summary", "prompt", "workflow", "column_name", "tool",
    "model", "project", "root", "blocked_by",
    "attachments", "plan_path", "closed_by", "close_note", "manual_steps",
    "start_when_planned",
    # The importance number, at v18. In here because it is drawn on the card
    # face: a change to it must move the card's change number, or a phone
    # holding a stale copy would never re-fetch it. The
    # `_BOARD_FIELDS ⊆ REVISED_COLUMNS` mirror requires it anyway.
    "priority",
    "area",
    "kind", "report_path",
    # The attached report's verdict line, at v28: drawn on the card face.
    "report_verdict", "report_recommendation",
    # The check file, at v26: drawn under the card's manual-check section.
    "manual_check_path",
})

#: What a save guarded by a stale `expected_revision` is refused with. The
#: opening words (`"this card changed on the Mac"`) are what both surfaces
#: recognise by prefix, exactly as `PLAN_GATE_REFUSAL` is recognised today —
#: so the sentence may be extended at the end and never at the front.
CARD_CHANGED_REFUSAL = ("this card changed on the Mac while you were "
                        "editing it — check what moved, then save again")

#: What a phone's Mark checked is refused with when the steps it echoes are
#: no longer the steps on the card — or the card has no check left. The
#: echo is `board_approve_plan`'s shape (what was shown, handed back) with
#: `expected_revision`'s rule (absent means no guard): the Mac's own
#: unarmed press sends none and keeps today's statement. Exact text, not a
#: digest — `manual_steps` is already a bounded stored column.
MANUAL_CHECK_CHANGED_REFUSAL = "that check has changed or is already cleared"
#: The opening words of the refusal `flag_manual_by_session` composes for a
#: check file under the project's `manual-check/` folder that fails
#: `manual_check.check`; the problems follow it.
MANUAL_CHECK_MALFORMED_REFUSAL = "that check is not in the manual-check shape — "
#: A check file named anywhere but `<root>/manual-check/<date>-<slug>/`.
MANUAL_CHECK_PLACE_REFUSAL = ("a manual check file lives under the project's "
                              "manual-check folder")
#: A second Passed / Failed on a check whose file already says so. Equal to
#: `manual_check.RECORDED` (pinned by a test): the store cannot import it.
MANUAL_OUTCOME_RECORDED_REFUSAL = "that check already has an outcome"
#: The person's note on a recorded outcome, clamped (`close_note`'s rule).
MAX_MANUAL_OUTCOME_CHARS = 400
#: The review's twin: the close a phone echoes back is not the close on the
#: card, or the card is reviewed already. Keyed on the close identity
#: (`closed_by` + `close_note`), **not** `expected_revision`: a title edit
#: bumps the revision and would refuse a still-valid review of the same close.
REVIEW_CHANGED_REFUSAL = "that close has changed or is already reviewed"
#: One half of the close identity without the other. Refused before the
#: UPDATE: a client that could skip the note half could review a different
#: close by the same session.
REVIEW_HALF_ECHO_REFUSAL = "that review is missing its close identity"

#: The one order every card read hands out: column, then importance, then the
#: drag order, then creation. Three call sites (`cards`, `by_session`,
#: `by_refine_session`) share this constant so they cannot drift.
#:
#: `CAST(priority AS INTEGER)` is what folds `''` and `'0'` together —
#: `CAST('' AS INTEGER)` is 0 in SQLite — so an unscored card sorts exactly
#: where a card scored 0 does, at the bottom of its column. The panel states
#: the same fold once in Swift (`BoardCard.priorityValue`).
#:
#: **The index does not change.** `cards_by_column ON cards(column_name,
#: position)` no longer fully serves this clause, and that is fine: `MAX_CARDS`
#: is 500, so the residual sort is over at most a few hundred rows already
#: materialised by `SELECT *`. An expression index would be a second thing to
#: keep in step with this constant for no measurable gain.
#:
#: Deliberately **not** applied to the other `ORDER BY`s in this file:
#: `queued_cards` is ordered by `queue_key` (a record of when a person pressed
#: Start and how they have dragged the line since), `done_since` /
#: `done_awaiting_review` are *time* reads, and `_column_order` is the drag
#: arithmetic and must stay on the position axis alone.
CARD_ORDER_SQL = (" ORDER BY column_name,"
                  " CAST(priority AS INTEGER) DESC, position, created_at")


class BoardStore(KnowledgeStoreMixin, LifecycleStoreMixin, OutcomeStoreMixin):
    """Thread-safe SQLite wrapper for the board. Synchronous — see the docstring."""

    #: Columns a caller may set. Anything else in a fields dict is ignored
    #: rather than raising: the panel and the channel both build these, and a
    #: surface a version ahead must not be able to crash a write.
    _WRITABLE = frozenset({
        "beneficiary", "intended_benefit", "success_criterion", "outcome_check_on",
        "project", "root", "title", "summary", "prompt", "tool", "column_name",
        "position", "session_id", "link_state", "dispatch_error",
        "dispatched_at", "session_ended_at", "author", "done_at",
        # `workflow` is declared by whoever writes the card and is editable.
        # `agent_trail` is deliberately **not** here: it is Dark Army's record of what
        # it saw run, written only by `record_agents` from the reconcile, and a
        # surface that could set it could claim a stage had happened that never
        # did — the same argument that keeps `session_id` and `link_state` out.
        # `crew_trail` is out for exactly the same reason, one rung on: it says
        # *who* was on a stage, and a face on a stage that never ran is the
        # same lie with a portrait attached.
        "workflow",
        # Waiting-on marks. Cycle detection in `update` is what makes a
        # surface writing this survivable; `position` stays writable for the
        # store but is deliberately not an API field — reorder is a verb.
        "blocked_by",
        # The refinement link, at v6 — `session_id`'s precedent exactly:
        # store-writable (the daemon's reconcile writes them through `update`)
        # and excluded at the API layer (`ApiServer._BOARD_FIELDS`), so no
        # surface can claim a refinement is running that is not. `plan_path`
        # is deliberately **not** here: it is written by `attach_plan` alone,
        # because a surface that could set it could stamp a card "planned"
        # and walk it past the plan gate with no plan behind it.
        "refine_session_id", "refine_state",
        # The batch mark, at v27 — the refinement link's ring exactly:
        # written by the daemon beside `refine_state` when one press hands
        # one session several cards, cleared with the link, and absent from
        # `ApiServer._BOARD_FIELDS`, so no surface can claim two cards share
        # a session. Not `SINGLE_WRITER` (the reconcile and `reset_card`
        # clear it through `update`), not `REVISED_COLUMNS` (bookkeeping).
        "batch_id", "batch_rank",
        # The queue pair, at v8 — `session_id`'s ring exactly: store-writable
        # (the dispatch path writes them through `update`) and excluded at the
        # API layer (`ApiServer._BOARD_FIELDS`), so a surface can never put a
        # card into a queue Dark Army will later drain, nor reorder one somebody
        # else is waiting in. Clearing is a verb of its own (`board_unqueue`),
        # and `update` clears both itself on any write of `column_name`.
        "queue_state", "queued_at",
        # `queue_rank` is deliberately **not** here, on `plan_path`'s ring:
        # it is the person's preferred place in line, set by `move_queued`
        # alone and cleared by the store itself whenever a card leaves the
        # queue. A surface that could write it raw could reorder somebody
        # else's queue, and a daemon path that could set it through `update`
        # would be a second writer of one order.
        # Files attached at writing time, at v11 — a thing a person states,
        # like `workflow`. Bounds (count, shape, extension) are refused at
        # `create`/`update` via `attachments.field_refusal`, so every route
        # inherits them. The copies themselves live on disk; this column is
        # only the relative paths.
        "attachments",
        # Which model the assistant runs on, at v13 — a thing a person states,
        # like `tool` beside it. Validated against `dispatch.MODELS` for the
        # card's tool at both `create` and `update`, and cleared by the store
        # itself on a retool (see `update`).
        "model",
        # The standing "start it when its plan lands" tick, at v17 — a thing
        # a person states, like `model` and `tool` beside it, and that is the
        # whole feature: a surface must be able to set it, so it is in
        # neither `SINGLE_WRITER` nor the outcome ring. The daemon's own
        # clear at the moment of the auto-start is the one bookkeeping write
        # on it, and it rides this same ring for `queue_state`'s stated
        # reason: the dispatch path writes it through `update`. Normalised
        # to `'1'` / `''` at both `create` and `_update_locked`, so the
        # API layer's `str()` coercion cannot store garbage.
        "start_when_planned",
        # The importance number, at v18 — `model`'s own line of argument: a
        # thing a person states about their own card. Deliberately **not**
        # in `SINGLE_WRITER`: a person edits it, and Dark Army's one suggestion
        # (`card_priority.py`) is just another writer of the same field
        # going through the ordinary `update`. Validated at both `create`
        # and `_update_locked` by `normalise_priority`, so the API layer's
        # `str()` coercion cannot store garbage.
        "priority",
    "area",
        "kind",
        # `create_token` is deliberately **not** here: only `create` writes
        # it, and a Save that could rewrite it could steal another card's
        # identity. Not `SINGLE_WRITER` either — that ring is later verbs,
        # and this column is never updated.
        #
        # `revision` is `create_token`'s ring for the same reason: the store
        # writes it, no verb is named for it, and it is therefore in neither
        # this set nor `SINGLE_WRITER`. A surface that could set it could
        # walk a stale save straight past the guard the column exists to arm.
        # `manual_steps` is deliberately **not** here, on `closed_by`'s side of
        # the line: it is a statement the session that did the work made about
        # its own work — "somebody still has to check this, here is how" — and
        # a surface that could write it could stamp a card nobody flagged with
        # a chore, or re-raise one a person has just said they had done. One
        # writer (`flag_manual`), one clearer (`clear_manual`), and the API's
        # own allow-list excludes it too, so the asymmetry `board_unqueue` has
        # holds here: a surface may clear the flag and may never set one.
        #
        # `reviewed_at` is deliberately **not** here either, though it records
        # a *human's* act rather than a session's. The card sheet saves
        # several fields on every edit (it saves `project` every time — see
        # `update`), so a generically writable review field could ride a
        # draft, and a Save must never silently acknowledge a review the
        # person did not make. The only meaningful transition is one-way on a
        # Done, agent-closed card, and `mark_reviewed`'s WHERE clause is the
        # race answer the store already trusts — so the gesture arrives as a
        # named verb (`board_review`): the field is closed, the act is open.
    })

    #: Added to an existing table after it shipped. `CREATE TABLE IF NOT EXISTS`
    #: is a no-op on a database that already has the table, so a new column has
    #: to arrive by ALTER or every query naming it fails at read time — loudly,
    #: but long after the upgrade. Empty at v1 and kept as the seam.
    #:
    #: **Every entry added here must carry a DEFAULT (or be nullable).**
    #: An older build opening a newer file goes on INSERTing into a table that
    #: already carries the newer column — `_migrate` never lowers the recorded
    #: version, and `_add_missing_columns` is PRAGMA-driven either way, so the
    #: shape it writes into is the newer one whatever the marker says — and a
    #: `NOT NULL` column with no default makes every one of those INSERTs fail.
    #: The forward-compatibility rule this file lives under ("an older build
    #: still opens the file") is only true if the column can be omitted.
    _ADDED_COLUMNS: dict = {"cards": (
        ("summary", "TEXT NOT NULL DEFAULT ''"),
        # The stage track, at v2. Both carry a DEFAULT, which is the rule stated
        # above and not a formality here: a v1 build opening this file goes on
        # INSERTing into a table that has these columns, and a NOT NULL column
        # without a default would fail every write it made.
        ("workflow", "TEXT NOT NULL DEFAULT ''"),
        ("agent_trail", "TEXT NOT NULL DEFAULT ''"),
        # The crew, at v19. Same DEFAULT rule as every column above it: a v18
        # build opening this file goes on INSERTing into a table that has it,
        # reads `''` as "no crew" and draws the roles' anchor faces.
        ("crew_trail", "TEXT NOT NULL DEFAULT ''"),
        # The close signature, at v4. Same rule, same reason: a schema-3 build
        # opening this file goes on INSERTing into a table that has these two,
        # and a NOT NULL column with no default would break every write it made.
        # The CREATE path above and this ALTER path must agree exactly —
        # `_add_missing_columns` is PRAGMA-driven and only ever sees this list.
        ("closed_by", "TEXT NOT NULL DEFAULT ''"),
        ("close_note", "TEXT NOT NULL DEFAULT ''"),
        # Waiting-on marks, at v5. Same DEFAULT rule as the v4 pair: a
        # schema-4 build opening this file goes on INSERTing into a table
        # that has the column.
        ("blocked_by", "TEXT NOT NULL DEFAULT ''"),
        # The refinement, at v6. Same DEFAULT rule: a schema-5 build opening
        # this file goes on INSERTing into a table that has all three, and it
        # simply does not draw the Prep column its panel has never heard of —
        # accepted degradation, stated at `COLUMNS`, not something to "fix".
        ("plan_path", "TEXT NOT NULL DEFAULT ''"),
        ("refine_session_id", "TEXT NOT NULL DEFAULT ''"),
        ("refine_state", "TEXT NOT NULL DEFAULT ''"),
        # Retired at v22, kept for downgrade. Same DEFAULT rule: a schema-6
        # build opening this file goes on INSERTing into a table that has
        # both. This build never reads them. Spelled identically to the
        # CREATE path above; `_add_missing_columns` is PRAGMA-driven and
        # only ever sees this list.
        ("initiative_id", "TEXT NOT NULL DEFAULT ''"),
        ("initiative_by", "TEXT NOT NULL DEFAULT ''"),
        # The queue pair, at v8. Same DEFAULT rule, and `queued_at` is the
        # nullable shape the rule explicitly admits (`dispatched_at` is the
        # precedent): a schema-7 build opening this file goes on INSERTing
        # without naming either, and its board simply draws no queue —
        # accepted degradation. Spelled identically to the CREATE path above;
        # `_add_missing_columns` is PRAGMA-driven and only ever sees this list.
        ("queue_state", "TEXT NOT NULL DEFAULT ''"),
        ("queued_at", "REAL DEFAULT NULL"),
        # The outstanding hand-check, at v9. Same DEFAULT rule: a schema-8
        # build opening this file goes on INSERTing without naming it, and its
        # board simply draws no badge — accepted degradation. Spelled
        # identically to the CREATE path above; `_add_missing_columns` is
        # PRAGMA-driven and only ever sees this list.
        ("manual_steps", "TEXT NOT NULL DEFAULT ''"),
        # The review acknowledgement, at v10. Nullable is the shape the rule
        # explicitly admits (`queued_at` is the precedent): a schema-9 build
        # opening this file goes on INSERTing without naming it, and its board
        # simply never draws the review banner — accepted degradation. Spelled
        # identically to the CREATE path above, both reading
        # reviewed_at REAL DEFAULT NULL — `_add_missing_columns` is
        # PRAGMA-driven and only ever sees this list.
        ("reviewed_at", "REAL DEFAULT NULL"),
        # Attached files, at v11. Same DEFAULT rule: a schema-10 build
        # opening this file goes on INSERTing without naming it, and its
        # board simply draws no attachments — accepted degradation. Spelled
        # identically to the CREATE path above; `_add_missing_columns` is
        # PRAGMA-driven and only ever sees this list.
        ("attachments", "TEXT NOT NULL DEFAULT ''"),
        # The person's preferred place in the queue, at v11. Nullable is the
        # shape the rule explicitly admits (`queued_at` is the precedent):
        # there is no rank value that means "never dragged". A schema-10
        # build opening this file goes on INSERTing without naming it, and
        # its drain simply uses FIFO-by-stamp — accepted degradation. Spelled
        # identically to the CREATE path above, both reading
        # queue_rank REAL DEFAULT NULL — `_add_missing_columns` is
        # PRAGMA-driven and only ever sees this list.
        ("queue_rank", "REAL DEFAULT NULL"),
        # The card's model, at v13. Same rule as every other added column and
        # not a formality: a schema-12 build opening this file goes on
        # INSERTing into a table that has it, and a NOT NULL column with no
        # default would fail every write it made. Its writes never blank it
        # either — `update` writes only the columns it was named. Spelled
        # identically to the CREATE path above, which a grep for the column's
        # declaration proves by finding exactly these two lines.
        ("model", "TEXT NOT NULL DEFAULT ''"),
        # Client create-idempotency token. Same DEFAULT rule: a build that
        # has never heard of it goes on INSERTing without naming it.
        # Spelled identically to the CREATE path above, both reading
        # create_token TEXT NOT NULL DEFAULT '' — `_add_missing_columns` is
        # PRAGMA-driven and only ever sees this list.
        ("create_token", "TEXT NOT NULL DEFAULT ''"),
        ("beneficiary", "TEXT NOT NULL DEFAULT ''"),
        ("intended_benefit", "TEXT NOT NULL DEFAULT ''"),
        ("success_criterion", "TEXT NOT NULL DEFAULT ''"),
        ("outcome_check_on", "TEXT NOT NULL DEFAULT ''"),
        ("outcome_revision", "INTEGER NOT NULL DEFAULT 0"),
        ("outcome_status", "TEXT NOT NULL DEFAULT 'unaccepted'"),
        # The plan approval pair, at v15. Same DEFAULT rule, and `plan_approved_at`
        # is the nullable shape the rule explicitly admits (`reviewed_at` is the
        # precedent): a schema-14 build opening this file goes on INSERTing
        # without naming either, and its surfaces simply draw no approval —
        # accepted degradation. Spelled identically to the CREATE path above,
        # both reading plan_approved TEXT NOT NULL DEFAULT '' and
        # plan_approved_at REAL DEFAULT NULL — `_add_missing_columns` is
        # PRAGMA-driven and only ever sees this list.
        ("plan_approved", "TEXT NOT NULL DEFAULT ''"),
        ("plan_approved_at", "REAL DEFAULT NULL"),
        # The card's change number, at v16. Same DEFAULT rule and
        # `outcome_revision`'s precedent for the type: a schema-15 build
        # opening this file goes on INSERTing without naming it, and its
        # saves are simply unguarded — accepted degradation. Spelled
        # identically to the CREATE path above, both reading
        # revision INTEGER NOT NULL DEFAULT 0 — `_add_missing_columns` is
        # PRAGMA-driven and only ever sees this list.
        ("revision", "INTEGER NOT NULL DEFAULT 0"),
        # The standing "start it when the plan lands" tick, at v17. Same
        # DEFAULT rule and `model`'s precedent for the type: a schema-16
        # build opening this file goes on INSERTing without naming it, and
        # its surfaces simply never draw the tick — accepted degradation.
        # Spelled identically to the CREATE path above, both reading
        # start_when_planned TEXT NOT NULL DEFAULT '' — `_add_missing_columns`
        # is PRAGMA-driven and only ever sees this list.
        ("start_when_planned", "TEXT NOT NULL DEFAULT ''"),
        # The importance number, at v18. Same DEFAULT rule and `model`'s
        # precedent for the type: a schema-17 build opening this file goes
        # on INSERTing without naming it, and its surfaces simply draw no
        # number — accepted degradation. Spelled identically to the CREATE
        # path above, both reading priority TEXT NOT NULL DEFAULT '' —
        # `_add_missing_columns` is PRAGMA-driven and only ever sees this
        # list.
        ("priority", "TEXT NOT NULL DEFAULT ''"),
        # area TEXT NOT NULL DEFAULT '' — forward-compatible card metadata.
        ("area", "TEXT NOT NULL DEFAULT ''"),
        # The card's kind and the scout's report, at v25. Same DEFAULT rule
        # and `area`'s precedent: a schema-24 build opening this file goes
        # on INSERTing without naming either, and its surfaces simply draw
        # no SCOUT tag and no report — accepted degradation. Spelled
        # identically to the CREATE path above, both reading
        # kind TEXT NOT NULL DEFAULT '' and
        # report_path TEXT NOT NULL DEFAULT '' — `_add_missing_columns` is
        # PRAGMA-driven and only ever sees this list.
        ("kind", "TEXT NOT NULL DEFAULT ''"),
        ("report_path", "TEXT NOT NULL DEFAULT ''"),
        # The check file a flag named, at v26. Same DEFAULT rule: a
        # schema-25 build goes on INSERTing without it and draws no file.
        # Spelled identically to the CREATE path above, reading
        # manual_check_path TEXT NOT NULL DEFAULT ''.
        ("manual_check_path", "TEXT NOT NULL DEFAULT ''"),
        # The flagging session, at v29 (`flag_manual` alone). Same DEFAULT rule:
        # a v28 build INSERTs without naming it and never reads it.
        ("manual_session_id", "TEXT NOT NULL DEFAULT ''"),
        # The batch mark, at v27. Same DEFAULT rule: a schema-26 build goes
        # on INSERTing without either and simply never counts a batch once.
        # Spelled identically to the CREATE path above, reading
        # batch_id TEXT NOT NULL DEFAULT '' and
        # batch_rank TEXT NOT NULL DEFAULT ''.
        ("batch_id", "TEXT NOT NULL DEFAULT ''"),
        ("batch_rank", "TEXT NOT NULL DEFAULT ''"),
        # The report's answer block, at v28. Same DEFAULT rule: a schema-27
        # build goes on INSERTing and attaching without either, and its
        # cards simply draw no verdict line. Spelled identically to the
        # CREATE path above, reading
        # report_verdict TEXT NOT NULL DEFAULT '' and
        # report_recommendation TEXT NOT NULL DEFAULT ''.
        ("report_verdict", "TEXT NOT NULL DEFAULT ''"),
        ("report_recommendation", "TEXT NOT NULL DEFAULT ''"),
        # The card's own worktree, at v30. Same DEFAULT rule: a schema-29
        # build goes on INSERTing without either, never reads them and never
        # removes a worktree. Spelled identically to the CREATE path above,
        # reading worktree_path TEXT NOT NULL DEFAULT '' and
        # worktree_branch TEXT NOT NULL DEFAULT ''.
        ("worktree_path", "TEXT NOT NULL DEFAULT ''"),
        ("worktree_branch", "TEXT NOT NULL DEFAULT ''"),
        # Review and merge, at v31. Same DEFAULT rule: a schema-30 build goes
        # on INSERTing without any of them and never reads them.
        ("merge_state", "TEXT NOT NULL DEFAULT ''"),
        ("merge_note", "TEXT NOT NULL DEFAULT ''"),
        ("review_verdict", "TEXT NOT NULL DEFAULT ''"),
        ("review_tip", "TEXT NOT NULL DEFAULT ''"),
    ),
        # `card_runs` gains columns the same way (v24), keyed on its own
        # table: `_add_missing_columns` is PRAGMA-driven per table, so an
        # older file's `card_runs` (created by `CREATE TABLE IF NOT EXISTS`
        # in the first executescript, before this list runs) is brought up
        # to the current shape. Each carries a DEFAULT for the reason the
        # `cards` entries do: a v23 build's `close_run` UPDATE names none of
        # them and must still land.
        "card_runs": (
            ("shunt_delegations", "INTEGER NOT NULL DEFAULT 0"),
            ("shunt_lines_kept_out", "INTEGER NOT NULL DEFAULT 0"),
            ("shunt_worker_cost_usd", "REAL DEFAULT NULL"),
        )}

    def __init__(self, path: Path = BOARD_PATH):
        self._path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        # Reentrant, so a verb can hold it across its own read-then-write
        # while still calling `get`/`queued_count`, which
        # each take it too. `update` and `bind_session` rely on that: the
        # daemon calls them from the executor while a person's drag lands on
        # the loop, and a read outside the lock let `bind_session` overwrite
        # a Done move that arrived between its read and its write.
        self._lock = threading.RLock()

    # --- lifecycle ---

    def connect(self) -> None:
        if self._conn is not None:
            return
        # The state folder goes through the seam that creates and narrows
        # it; anywhere else is a test's or a caller's folder.
        if self._path.parent.name == STATE_DIR.name:
            ensure_state_dir()
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        # Before any executescript: the coalesce VACUUM in `_connect_lifecycle`
        # must already run under the cap.
        conn.execute(f"PRAGMA journal_size_limit = {WAL_SIZE_LIMIT_BYTES}")
        # The create-token unique index names a column older files only
        # gain in `_add_missing_columns`, so it cannot run as part of the
        # first executescript. Split on the only UNIQUE INDEX in `_SCHEMA`.
        _idx = _SCHEMA.find("CREATE UNIQUE INDEX IF NOT EXISTS")
        conn.executescript(_SCHEMA if _idx < 0 else _SCHEMA[:_idx])
        self._conn = conn
        self._add_missing_columns()
        if _idx >= 0:
            self._conn.executescript(_SCHEMA[_idx:])
        self._migrate()
        self._repair_worktree_roots()
        self._sweep_orphan_messages()
        self._sweep_orphan_runs()
        self._connect_outcomes()
        self._connect_lifecycle()
        # Last, and not swept: the knowledge notes are keyed on the project
        # root, not on a card, so they have no orphan to sweep.
        self._connect_knowledge()

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            # The wait seconds held in memory (`WAIT_FLUSH_SECONDS`): a
            # clean close writes them, so only a crash costs the interval.
            self.flush_outcome_waits()
            try:
                self._conn.close()
            except Exception:
                # Shutdown, usually after something else already went wrong.
                logger.debug("closing board.db failed", exc_info=True)
            self._conn = None

    def _add_missing_columns(self) -> None:
        """Bring an older file's table up to the current column set. Driven off
        `PRAGMA table_info` rather than the schema version, so it stays correct
        even for a database carrying a version that does not match its shape."""
        with self._lock:
            for table, columns in self._ADDED_COLUMNS.items():
                have = {row["name"] for row in
                        self._conn.execute(f"PRAGMA table_info({table})")}
                if not have:
                    continue
                added = [name for name, decl in columns if name not in have]
                for name, decl in columns:
                    if name in have:
                        continue
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                if added:
                    self._conn.commit()
                    logger.info("board.db: added %s to %s",
                                ", ".join(added), table)

    def _migrate(self) -> None:
        """Record the schema version, forward only. A file written by a newer
        build is read anyway — the tolerant read is what makes that safe.

        Forward only is load-bearing because the `.app` bundle and a dev
        checkout open the same `board.db`: if the older of the two stamped its
        own number back down, the newer build would re-run every upgrade step
        the next time it opened the file. So a newer marker is logged and
        kept, never overwritten.

        `_retire_ready` is unaffected by that: it runs on `found < 2`, and a
        marker above `SCHEMA_VERSION` never satisfies it, while a fresh file
        (`found == 0`) still enters it exactly as before.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
            found = int(row["value"]) if row and str(row["value"]).isdigit() else 0
            # Before the marker moves, never after: a wipe that failed behind
            # a written 29 would never be tried again, and a leftover list
            # would start holding cards. Idempotent, so a re-run is harmless.
            if found < 29:
                self._clear_retired_blocked_by()
            if found > SCHEMA_VERSION:
                logger.warning(
                    "board.db was written by a newer build (schema %d > %d); "
                    "reading it anyway", found, SCHEMA_VERSION)
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(max(found, SCHEMA_VERSION)),))
            self._conn.commit()
        if found < 2:
            self._retire_ready()
        if found < 22:
            self._retire_initiatives()

    def _clear_retired_blocked_by(self) -> None:
        """Empty every leftover dependency list, once, on the upgrade to v29.

        `_retire_initiatives`' shape and reason: a column *this* project
        retired (20 Sep 2026) and now reads again, so it knows exactly what a
        stale value would do — hold a card behind links nobody on this board
        made. Emptied, never dropped: no column moves, and a v28 build opening
        the file after a downgrade reads the column exactly as it did.
        Forward-only like every rung here, so a person's links written after
        the upgrade are never touched by a later open.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET blocked_by = '' WHERE blocked_by != ''")
            self._conn.commit()
        if cur.rowcount:
            logger.info("board.db: cleared %d leftover dependency list(s)",
                        cur.rowcount)

    def _repair_worktree_roots(self) -> None:
        """Point every card whose `root` names a card folder
        (`<root>/.worktrees/card-…`) back at its checkout. On every open, not
        a schema rung: an older build can still file such a card after a
        downgrade, and the repair is idempotent. `revision` does not move —
        nobody changed the card, Dark Army corrected where it works."""
        like = f"%{worktrees.WORKTREES_DIR}/{worktrees.FOLDER_PREFIX}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, root FROM cards WHERE root LIKE ?",
                (like,)).fetchall()
            fixed = 0
            for row in rows:
                home = worktrees.checkout_root(str(row["root"] or ""))
                if home and home != row["root"]:
                    self._conn.execute(
                        "UPDATE cards SET root = ? WHERE id = ?",
                        (home, row["id"]))
                    fixed += 1
            if fixed:
                self._conn.commit()
        if fixed:
            logger.info("board.db: pointed %d card(s) filed from a card "
                        "folder back at the checkout", fixed)

    def _sweep_orphan_messages(self) -> None:
        """Drop `card_messages` whose card is gone.

        A v11 build's `delete()` never heard of this table, so a card it
        destroyed leaves rows behind. Named rather than a blanket rewrite of
        unknown tables — the forward-compatibility rule forbids the latter.
        """
        with self._lock:
            self._conn.execute(
                "DELETE FROM card_messages WHERE card_id NOT IN "
                "(SELECT id FROM cards)")
            self._conn.commit()

    def _retire_ready(self) -> None:
        """Fold the retired `ready` column back into Backlog.

        Run once, on the upgrade to schema 2, and **not** as a blanket "rewrite
        any column I do not recognise": that would be a downgraded build
        silently destroying a newer one's data, which is the exact thing the
        forward-compatibility rule at the top of this file forbids. `ready` is
        named explicitly because it is a column *this* project retired and whose
        meaning it therefore knows.

        Backlog rather than In progress, and it is not a close call: a card in
        Ready had never been started, and In progress now means an assistant is
        working — putting them there would have the board assert a run that
        never happened.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET column_name = 'backlog' "
                " WHERE column_name = 'ready'")
            self._conn.commit()
        if cur.rowcount:
            logger.info("board.db: moved %d card(s) out of the retired "
                        "Ready column into Backlog", cur.rowcount)

    def _retire_initiatives(self) -> None:
        """Empty the retired folders, and never drop their shape.

        Run once, on the upgrade to schema 22, and named for `_retire_ready`'s
        reason: this is a table and two columns *this* project retired and
        whose meaning it therefore knows. The table and the two `cards`
        columns stay — an older build still SELECTs `initiatives` on every
        snapshot and INSERTs a card naming `initiative_id`, and dropping
        either made that build treat the whole board as missing. Membership
        is cleared and the table is emptied; no card moves column.
        """
        with self._lock:
            self._conn.execute("DELETE FROM initiatives")
            have = {row["name"] for row in
                    self._conn.execute("PRAGMA table_info(cards)")}
            assignments = [f"{column} = ''" for column in
                           ("initiative_id", "initiative_by") if column in have]
            if assignments:
                self._conn.execute("UPDATE cards SET " + ", ".join(assignments))
            self._conn.commit()
        logger.info("board.db: emptied the retired folders (initiatives)")

    # --- reads ---

    #: Kept in SQLite for an older build, stripped from every Python-facing
    #: card dict so this build never reads or writes them.
    _RETIRED_CARD_COLUMNS = ("initiative_id", "initiative_by")

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        """One card as a plain dict. `SELECT *` and `dict(row)` on purpose: a
        column this build does not know about rides along untouched rather than
        being dropped on the next write. Retired folder columns are the
        exception — they stay in the file for a downgrade and are never
        handed to a caller of this build."""
        return {k: row[k] for k in row.keys()
                if k not in BoardStore._RETIRED_CARD_COLUMNS}

    def get(self, card_id: str) -> Optional[dict]:
        if not card_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cards WHERE id = ?", (str(card_id),)).fetchone()
        return self._row(row) if row is not None else None

    def cards_by_id(self, ids) -> dict:
        """`{id: card}` for the named ids that exist; one `SELECT`.

        The dependency resolver's fallback for ids outside the snapshot's
        frame — a dependency finished a week ago is not in the Done preview —
        so the frame pays one read however many cards name one, never a
        `get` per card. An id that names no card is simply absent.
        """
        wanted = []
        for value in ids or ():
            text = str(value or "").strip()
            if text and text not in wanted:
                wanted.append(text)
        if not wanted:
            return {}
        out: dict = {}
        with self._lock:
            # SQLite's default host-parameter ceiling is 999; a frame's worth
            # of dependency ids is bounded by MAX_BLOCKERS per card, but a
            # chunk keeps the statement legal whatever the board holds.
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                rows = self._conn.execute(
                    "SELECT * FROM cards WHERE id IN (%s)"
                    % ",".join("?" * len(chunk)), tuple(chunk)).fetchall()
                for row in rows:
                    card = self._row(row)
                    out[card["id"]] = card
        return out

    def _card_by_create_token_locked(self, token: str):
        """The card that already holds this create token, or None.

        Empty is "no token" and must not be queried: the partial unique
        index excludes `''`, so `WHERE create_token = ''` would return an
        arbitrary tokenless row. Caller holds `_lock`.
        """
        if not token:
            return None
        row = self._conn.execute(
            "SELECT * FROM cards WHERE create_token = ?", (token,)).fetchone()
        return self._row(row) if row is not None else None

    def cards(self, columns: Optional[Iterable[str]] = None) -> list:
        """Every card, or only those in `columns`. `CARD_ORDER_SQL`: column,
        then importance (highest first, unscored last), then position, then
        creation — so the top of a column is the most important work and a
        board with neither scores nor hand-ordering still reads oldest-first
        inside a column."""
        sql = "SELECT * FROM cards"
        params: tuple = ()
        if columns is not None:
            wanted = [c for c in columns if c in COLUMNS]
            if not wanted:
                return []
            sql += " WHERE column_name IN (%s)" % ",".join("?" * len(wanted))
            params = tuple(wanted)
        sql += CARD_ORDER_SQL
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def reports_index_rows(self) -> list:
        """Every card that has a report, narrowed to what the report list
        reads: `id`, `title`, `column_name`, `root`, `report_path`, in
        `CARD_ORDER_SQL`. Read-only — no column, no schema step; the
        scout-report index (`scout_index.build`) is its one reader, on the
        executor."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, column_name, root, report_path FROM cards"
                " WHERE report_path != ''" + CARD_ORDER_SQL).fetchall()
        return [{"id": r[0], "title": r[1], "column_name": r[2],
                 "root": r[3], "report_path": r[4]} for r in rows]

    def plans_index_rows(self) -> list:
        """Every card that has a plan, narrowed to what the plan list
        reads: `id`, `title`, `column_name`, `root`, `plan_path`, in
        `CARD_ORDER_SQL` (so the first card on a shared plan is the one
        its row names). Read-only — no column, no schema step; the plan
        index (`plan_index.build`) is its one reader, on the executor."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, column_name, root, plan_path FROM cards"
                " WHERE plan_path != ''" + CARD_ORDER_SQL).fetchall()
        return [{"id": r[0], "title": r[1], "column_name": r[2],
                 "root": r[3], "plan_path": r[4]} for r in rows]

    def by_session(self, session_id: str) -> list:
        """Every card bound to this session. Uses the `cards_by_session` index.

        **An empty session id returns `[]`, and that guard is load-bearing.**
        `session_id` defaults to `''` in the schema, so `WHERE session_id = ''`
        matches every card nobody has started — the whole backlog. The one
        caller closes a card when this returns exactly one match, so without
        the guard a board holding a single unbound card would have that card
        closed by any session Dark Army could not otherwise place. The failure is
        silent and destructive, which is why `declare_done` refuses an empty
        `session_id` on the card as well.
        """
        sid = str(session_id or "")
        if not sid:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE session_id = ?"
                + CARD_ORDER_SQL, (sid,)).fetchall()
        return [self._row(r) for r in rows]

    def batch_members(self, batch_id: str) -> list:
        """Every card one batch press marked, in `CARD_ORDER_SQL`.

        `by_session`'s guard for the same reason: `batch_id` defaults to
        `''`, so `WHERE batch_id = ''` would be every card no batch ever
        touched. An empty id returns `[]`. Read-only; the batch-implement
        advance (`advance_batch_by_session`) is its caller, and picks the
        next member by `batch_rank`, never by this order."""
        bid = str(batch_id or "")
        if not bid:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE batch_id = ?"
                + CARD_ORDER_SQL, (bid,)).fetchall()
        return [self._row(r) for r in rows]

    def release_batch_waiting(self, batch_id: str, note: str) -> int:
        """Take the batch mark off every member no session holds — the ones
        still waiting in Backlog and any dragged out of it, in any column —
        with `note` as its orange line. Returns how many.

        `relabel_root`'s shape and its argument: this is Dark Army observing
        that the session a batch was handed has gone (or never bound), not a
        person editing a card, so it is one statement over the batch in one
        transaction and **no `revision` step**. The WHERE clause is the
        whole scope — the mark, no session, and not still binding
        (`dispatching`, the head's own give-up clears that one) — so a
        member already worked (bound, In progress, Done) is untouched
        however often this runs. No column test: a waiting member a person
        dragged out of Backlog would otherwise keep its mark for good, and
        with it a Start refused in words. An empty id is a no-op; the note
        is clamped at `MAX_CLOSE_NOTE_CHARS`.
        """
        bid = str(batch_id or "")
        if not bid:
            return 0
        text = str(note or "").strip()[:MAX_CLOSE_NOTE_CHARS]
        now = time.time()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "UPDATE cards SET batch_id = '', batch_rank = '',"
                    " dispatch_error = ?, updated_at = ?"
                    " WHERE batch_id = ? AND session_id = ''"
                    " AND link_state <> 'dispatching'",
                    (text, now, bid))
                moved = int(cur.rowcount or 0)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return moved

    def add_message(self, card_id: str, author: str, text: str,
                    kind: str = "question", via: str = "") -> tuple:
        """Append one message to a card's thread. `(row_or_None, detail)`.

        Refused at the store so every route inherits the bounds. A `question`
        over `MAX_MESSAGE_CHARS` is refused rather than truncated — it is
        about to be handed to an agent (`MAX_PROMPT_CHARS`' argument). An
        `answer` or `note` is clamped (`MAX_CLOSE_NOTE_CHARS`' argument: Dark Army's
        record of somebody's words).
        """
        if self.get(card_id) is None:
            return None, "no such card"
        kind = str(kind or "question")
        if kind not in MESSAGE_KINDS:
            return None, f"unknown message kind {kind}"
        via = str(via or "")
        if via not in MESSAGE_VIAS:
            return None, f"unknown message via {via}"
        text = str(text or "").strip()
        if not text:
            return None, "a message needs some text"
        if kind == "question":
            if len(text) > MAX_MESSAGE_CHARS:
                return None, (f"that question is longer than "
                              f"{MAX_MESSAGE_CHARS} characters")
        else:
            text = _clamp(text, MAX_MESSAGE_CHARS).strip()
            if not text:
                return None, "a message needs some text"
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) FROM card_messages WHERE card_id = ?",
                (str(card_id),)).fetchone()[0]
            if n >= MAX_THREAD_MESSAGES:
                return None, (f"this card already has {MAX_THREAD_MESSAGES} "
                              "messages")
            now = time.time()
            made = {
                "id": uuid.uuid4().hex,
                "card_id": str(card_id),
                "author": str(author or "user"),
                "kind": kind,
                "via": via,
                "text": text,
                "created_at": now,
            }
            self._conn.execute(
                "INSERT INTO card_messages "
                "(id, card_id, author, kind, via, text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (made["id"], made["card_id"], made["author"], made["kind"],
                 made["via"], made["text"], made["created_at"]))
            self._conn.commit()
        return made, "added"

    def messages(self, card_id: str) -> list:
        """This card's thread, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM card_messages WHERE card_id = ? "
                "ORDER BY created_at, id", (str(card_id),)).fetchall()
        return [self._row(r) for r in rows]

    def message_counts(self) -> dict:
        """`{card_id: count}` in one `GROUP BY`, for the snapshot."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT card_id, COUNT(*) AS n FROM card_messages "
                "GROUP BY card_id").fetchall()
        return {r["card_id"]: int(r["n"]) for r in rows}

    # --- what Dark Army observed one run do (`card_runs`, v15) ------------------
    #
    # Four verbs and they are the only writers. Every one of them rides the
    # single-writer shape the rest of this file uses — the guard in the
    # write's own WHERE clause, `rowcount == 0` meaning *that card moved* —
    # because `_board_call` hands each verb to the executor and a human can
    # start the card again between a read and an unconditional write.

    def open_run(self, card_id: str, run_at: float, root: str,
                 baseline: str) -> tuple:
        """Mark where a project stood as a run begins. `(ok, detail)`.

        `INSERT OR REPLACE`, which *is* the "latest replaces previous" rule:
        a second Start on the same card throws the previous record away
        rather than stacking one beside it. The row lands with
        `recorded_at` NULL, which is what `run_headlines` filters on — an
        open run publishes nothing, because a record of a run that has not
        ended yet would read as a finding.
        """
        cid = str(card_id or "")
        if not cid:
            return False, "a run needs a card"
        try:
            when = float(run_at)
        except (TypeError, ValueError):
            return False, "a run needs a time"
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO card_runs"
                " (card_id, run_at, root, baseline, recorded_at)"
                " VALUES (?, ?, ?, ?, NULL)",
                (cid, when, str(root or ""), str(baseline or "")))
            self._conn.commit()
        return True, "opened"

    def close_run(self, card_id: str, run_at: float, session_id: str,
                  verdict: str, report: str, summary: str, files,
                  files_available: bool, files_reason: str = "",
                  files_total: Optional[int] = None, *,
                  shunt_delegations: int = 0, shunt_lines_kept_out: int = 0,
                  shunt_worker_cost_usd: Optional[float] = None) -> tuple:
        """Write down what Dark Army saw this run do. `(record_or_None, detail)`.

        `report` and `summary` are **clamped**, `close_note`'s rule: Dark Army is
        relaying somebody's words and cutting them short is better than
        refusing to keep any of them. `files` is **refused** when it is not
        the parsers' own shape — a malformed list is a bug in the collector,
        and storing half of one would read to every surface as a successful
        reading of a project that barely changed.

        The guard is `WHERE card_id = ? AND run_at = ?`, so a late collector
        for run *n* cannot stamp the row a newer run *n+1* has already
        opened with the previous run's evidence. `rowcount == 0` therefore
        means *that card was started again* — the interesting case, not an
        error, and the caller drops the record.

        Where `open_run` never landed — git unavailable at dispatch, or a
        card started before this shipped — the row is INSERTed here with the
        supplied `run_at`, an empty baseline and no file list. A run that
        ended is always recorded; only its *starting point* can be missing.

        The three `shunt_*` keywords are what `work_record.read_shunt_ledger`
        folded off the session's delegation ledger; the defaults are "no
        delegation, no cost known", which is what a caller that never read
        a ledger means. The cost is stored as NULL, never 0, when unknown.
        """
        cid = str(card_id or "")
        if not cid:
            return None, "a run needs a card"
        try:
            when = float(run_at)
        except (TypeError, ValueError):
            return None, "a run needs a time"
        rows = list(files or [])
        if not work_record.files_ok(rows):
            return None, "that file list is not the shape Dark Army writes"
        available = bool(files_available)
        payload = json.dumps(rows) if available else ""
        added, removed = work_record.totals(rows)
        # The list is bounded (`clamp_files`); the *count* is not. A record
        # that lists 200 files and says 431 changed is honest; one that says
        # 200 is the bound quietly rewriting the finding.
        total = len(rows) if files_total is None else max(int(files_total),
                                                          len(rows))
        try:
            delegations = max(int(shunt_delegations or 0), 0)
        except (TypeError, ValueError):
            delegations = 0
        try:
            kept_out = max(int(shunt_lines_kept_out or 0), 0)
        except (TypeError, ValueError):
            kept_out = 0
        cost = None
        if isinstance(shunt_worker_cost_usd, (int, float)) \
                and not isinstance(shunt_worker_cost_usd, bool):
            cost = float(shunt_worker_cost_usd)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE card_runs SET session_id = ?, verdict = ?,"
                " report = ?, summary = ?, files = ?, files_available = ?,"
                " files_reason = ?, files_changed = ?, files_total = ?,"
                " lines_added = ?, lines_removed = ?, recorded_at = ?,"
                " shunt_delegations = ?, shunt_lines_kept_out = ?,"
                " shunt_worker_cost_usd = ?"
                " WHERE card_id = ? AND run_at = ?",
                (str(session_id or ""), str(verdict or ""),
                 work_record.clamp_report(report),
                 work_record.clamp_summary(summary), payload,
                 1 if available else 0, str(files_reason or ""),
                 len(rows), total, added, removed, now,
                 delegations, kept_out, cost, cid, when))
            changed = cur.rowcount
            if not changed:
                existing = self._conn.execute(
                    "SELECT card_id FROM card_runs WHERE card_id = ?",
                    (cid,)).fetchone()
                if existing is not None:
                    self._conn.commit()
                    return None, "that card was started again"
                self._conn.execute(
                    "INSERT INTO card_runs"
                    " (card_id, run_at, session_id, root, baseline, verdict,"
                    "  report, summary, files, files_available, files_reason,"
                    "  files_changed, files_total, lines_added, lines_removed,"
                    "  recorded_at, shunt_delegations, shunt_lines_kept_out,"
                    "  shunt_worker_cost_usd)"
                    " VALUES (?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    "  ?, ?, ?)",
                    (cid, when, str(session_id or ""), str(verdict or ""),
                     work_record.clamp_report(report),
                     work_record.clamp_summary(summary), payload,
                     1 if available else 0, str(files_reason or ""),
                     len(rows), total, added, removed, now,
                     delegations, kept_out, cost))
            self._conn.commit()
        return self.run_for(cid), "recorded"

    def run_for(self, card_id: str):
        """This card's record with `files` decoded, or None.

        A `files` payload this build cannot parse comes back as an empty list
        with `files_available` forced false: a record written by something
        newer must degrade into "Dark Army could not list them", never into
        "nothing changed".
        """
        cid = str(card_id or "")
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM card_runs WHERE card_id = ?", (cid,)).fetchone()
        if row is None:
            return None
        out = {k: row[k] for k in row.keys()}
        raw = out.get("files") or ""
        files = []
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                files = [r for r in parsed if isinstance(r, dict)]
            else:
                out["files_available"] = 0
                out["files_reason"] = work_record.GIT_FAILED_REASON
        out["files"] = files
        out["files_available"] = bool(out.get("files_available"))
        # The v24 trio, normalised for a row an older build wrote: absent or
        # NULL counts read as zero, and the cost stays `None` where it is
        # NULL — "unknown" is a value here, never rounded to 0.
        out["shunt_delegations"] = int(out.get("shunt_delegations") or 0)
        out["shunt_lines_kept_out"] = int(out.get("shunt_lines_kept_out") or 0)
        usd = out.get("shunt_worker_cost_usd")
        out["shunt_worker_cost_usd"] = (
            float(usd) if isinstance(usd, (int, float))
            and not isinstance(usd, bool) else None)
        return out

    def run_headlines(self) -> dict:
        """`{card_id: headline}` for every *finished* run, in one query.

        `length(report)` rather than `report`: the text is up to 4000
        characters and pulling it into a per-frame query would put it through
        the snapshot path once per card — exactly the mistake
        `BOARD_SNAPSHOT_PROMPT_CHARS` exists to have fixed once already.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT card_id, verdict, recorded_at, files_changed,"
                " files_total, lines_added, lines_removed, files_available,"
                " length(report) AS report_chars,"
                " shunt_delegations, shunt_lines_kept_out,"
                " shunt_worker_cost_usd"
                " FROM card_runs WHERE recorded_at IS NOT NULL").fetchall()
        out = {}
        for row in rows:
            usd = row["shunt_worker_cost_usd"]
            out[row["card_id"]] = {
                "verdict": row["verdict"] or "",
                "at": float(row["recorded_at"] or 0.0),
                "files": int(row["files_changed"] or 0),
                "files_total": int(row["files_total"] or 0),
                "added": int(row["lines_added"] or 0),
                "removed": int(row["lines_removed"] or 0),
                "files_available": bool(row["files_available"]),
                "report": bool(int(row["report_chars"] or 0)),
                # The helper's figures ride the headline so a tile can say
                # "3 delegations" without the fetch; the sentence itself is
                # composed by `work_record.shunt_words` on the full record.
                "shunt_delegations": int(row["shunt_delegations"] or 0),
                "shunt_lines_kept_out": int(row["shunt_lines_kept_out"] or 0),
                "shunt_worker_cost_usd": (
                    float(usd) if isinstance(usd, (int, float)) else None),
            }
        return out

    def run_health_counts(self, card_ids) -> dict:
        """`{card_id: {"attempts", "returns"}}` for the run-health line, in
        two reads under the one lock. **Read-only; no schema change.**

        `attempts` is **the same count `run_figures()` publishes** — the
        distinct implementation session ids among the card's `outcome_runs`
        rows, i.e. the sessions that actually bound — and never a count of
        `lifecycle_attempts`, which also holds a row for a dispatch whose
        bind window ran out with nothing bound. The two lines share one card
        tile, so one word "attempts" has to mean one thing; `test_run_health`
        pins them equal on one store driven through a timed-out dispatch and
        a successful retry. An `ended → live` resume keeps its session id,
        so a resumed session is still one attempt. `returns` is
        `outcome_cards.rework_count`, bumped once per reopen from Done or
        revision request while a submission stands. Neither ledger is
        written here and no counter is added: the two counts already exist,
        and this is the one read that joins them. A card in neither table
        reads `{"attempts": 0, "returns": 0}`.
        """
        ids = [str(c) for c in (card_ids or ()) if c]
        out = {cid: {"attempts": 0, "returns": 0} for cid in ids}
        if not ids:
            return out
        with self._lock:
            # SQLite's bound-parameter ceiling is 999 on older builds; the
            # Done archive can hand in more than that, so chunk.
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" for _ in chunk)
                for row in self._conn.execute(
                        "SELECT card_id, COUNT(DISTINCT session_id) AS n"
                        " FROM outcome_runs WHERE phase='implementation'"
                        " AND session_id != '' AND card_id IN"
                        f" ({marks}) GROUP BY card_id", chunk).fetchall():
                    out[row["card_id"]]["attempts"] = int(row["n"] or 0)
                for row in self._conn.execute(
                        "SELECT card_id, rework_count FROM outcome_cards"
                        f" WHERE card_id IN ({marks})", chunk).fetchall():
                    out[row["card_id"]]["returns"] = int(row["rework_count"] or 0)
        return out

    def _sweep_orphan_runs(self) -> None:
        """Drop `card_runs` whose card is gone. `_sweep_orphan_messages`'
        reason exactly: a build that predates this table destroys a card
        without knowing to take the run with it."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM card_runs WHERE card_id NOT IN "
                "(SELECT id FROM cards)")
            self._conn.commit()

    def by_refine_session(self, session_id: str) -> list:
        """Every card whose *refinement* is this session.

        **An empty session id returns `[]`, and the guard is load-bearing for
        `by_session`'s documented reason**: `refine_session_id` defaults to
        `''`, so `WHERE refine_session_id = ''` matches every card nobody has
        ever refined — the whole unrefined board — and the single-match caller
        (`attach_plan_by_session`) would then attach a plan to a card at
        random.
        """
        sid = str(session_id or "")
        if not sid:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE refine_session_id = ?"
                + CARD_ORDER_SQL, (sid,)).fetchall()
        return [self._row(r) for r in rows]

    def queued_cards(self, project: Optional[str] = None) -> list:
        """The queue, in the order the drain obeys.

        Coalesced key then id: `COALESCE(queue_rank, queued_at, 0)`. A
        hand-set rank lives on the same axis as the enqueue stamps, so NULL
        and non-NULL interleave; a never-dragged card keeps FIFO-by-stamp.
        The trailing 0 matches Python's `or 0.0` so a card with both NULL
        does not disagree across the two spellings. The id breaks a tie
        because two enqueues inside one clock tick must still have *an*
        order — an unordered queue drains differently on every pass, which
        is indistinguishable from a bug.

        `project=None` returns every project's queue in one read; the drain
        groups them itself rather than paying a query per project.
        """
        sql = ("SELECT * FROM cards WHERE queue_state = 'queued'")
        args: tuple = ()
        if project is not None:
            sql += " AND project = ?"
            args = (str(project),)
        sql += " ORDER BY COALESCE(queue_rank, queued_at, 0), id"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def queued_count(self, project: str) -> int:
        """How many cards this project already holds. `MAX_QUEUED_PER_PROJECT`
        is refused at the store, so the caller asks here rather than counting a
        list it fetched a moment ago."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM cards WHERE queue_state = 'queued'"
                " AND project = ?", (str(project or ""),)).fetchone()
        return int(row[0]) if row else 0

    def done_since(self, since: float, limit: int = 50) -> list:
        """Recently finished cards, newest first. The board snapshot carries
        these rather than the whole archive: the frame rides the SSE stream, and
        a board used for a year must not grow it without bound. The full archive
        is a separate on-demand fetch (`GET /api/board`)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE column_name = 'done'"
                "   AND COALESCE(done_at, updated_at) >= ?"
                " ORDER BY COALESCE(done_at, updated_at) DESC LIMIT ?",
                (float(since), int(limit))).fetchall()
        return [self._row(r) for r in rows]

    def done_awaiting_review(self, limit: int = 50) -> list:
        """Agent-closed Done cards no human has acknowledged, newest first.

        `done_since`'s sibling with the window removed: that read is bounded at
        24 hours so the frame cannot grow with the archive, but a close nobody
        has reviewed must never age out of the live board — a card closed on
        Friday still wears its banner on Monday. Bounded at the same limit as
        the recent preview, so the frame's worst case is stated rather than
        open-ended.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE column_name = 'done'"
                "   AND closed_by != '' AND reviewed_at IS NULL"
                " ORDER BY COALESCE(done_at, updated_at) DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [self._row(r) for r in rows]

    def done_manual_check_pending(self, limit: int = 50) -> list:
        """Done cards whose manual steps are still on the card, newest first.

        `done_awaiting_review`'s third-leg sibling. A reviewed card older than
        the 24-hour preview dropped out of the live board while its manual
        check was still open, and the phone — which holds the whole Done
        archive — kept listing it under Needs you where no dismiss could ever
        land: the daemon answered "not waiting on you" for a card it no longer
        carried (23 Sep 2026). Cleared steps (`clear_manual`) take a
        card out of this read, so it shrinks as checks are done.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE column_name = 'done'"
                # Whitespace of every kind, `manual_check_due`'s own
                # `str.strip()`: SQLite's bare TRIM keeps newlines.
                "   AND TRIM(COALESCE(manual_steps, ''), ' ' || char(9, 10, 13)) != ''"
                " ORDER BY COALESCE(done_at, updated_at) DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [self._row(r) for r in rows]

    def counts(self) -> dict:
        """Cards per column, every column present even at zero — a surface
        reading `counts["backlog"]` must never have to handle a missing key.

        Does not include `ready`. That name was a fourth column and is gone;
        the one-generation snapshot alias is `with_ready_alias`, not this dict.
        """
        out = {c: 0 for c in COLUMNS}
        with self._lock:
            rows = self._conn.execute(
                "SELECT column_name, COUNT(*) AS n FROM cards "
                "GROUP BY column_name").fetchall()
        for row in rows:
            name = row["column_name"]
            if name in out:
                out[name] = int(row["n"])
        return out

    @staticmethod
    def _done_scope_token(ids) -> str:
        """SHA-256 over an unambiguous sequence of UTF-8 card ids.

        The digest is a concurrency token, not a secret. Prefixing every id
        with its byte length means no two different id sequences can collapse
        to the same input merely by concatenating to the same bytes.
        """
        digest = hashlib.sha256()
        for card_id in ids:
            encoded = str(card_id).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def _done_scope_locked(self) -> tuple:
        rows = self._conn.execute(
            "SELECT id FROM cards WHERE column_name = 'done' ORDER BY id"
        ).fetchall()
        ids = [row["id"] for row in rows]
        return len(ids), self._done_scope_token(ids)

    def done_scope(self) -> tuple:
        """Store-wide Done ``(count, exact-membership token)`` in one read."""
        with self._lock:
            return self._done_scope_locked()

    @staticmethod
    def _done_view_token(rows) -> str:
        """SHA-256 over what a *held copy* of the Done column can go stale
        against: which cards are finished, what revision each is at, and
        whether each still wants a person's eye.

        Deliberately **not** an extension of the membership digest above.
        `clear_done`'s contract is exact membership, and a content-sensitive
        token there would refuse a Clear Done because somebody retitled a
        finished card — a refusal with nothing behind it. So the membership
        digest is left byte for byte alone and this rides beside it.

        Same length-prefixed discipline, for the same reason: no two different
        sequences may collapse to the same input merely by concatenating to
        the same bytes.
        """
        digest = hashlib.sha256()
        for row in rows:
            wants_review = bool(row["closed_by"]) and row["reviewed_at"] is None
            for part in (str(row["id"]),
                         str(int(row["revision"] or 0)),
                         "1" if wants_review else "0"):
                encoded = part.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        return digest.hexdigest()

    def done_tokens(self) -> tuple:
        """``(count, membership token, view token)`` under **one** lock.

        Two tokens read in two calls could disagree about which cards exist —
        a client would then hold a copy stamped with a pair that never
        described any real state of the store. The count and the membership
        token come from the membership read unchanged: this method adds a
        field, it does not redefine one.
        """
        with self._lock:
            count, clear_token = self._done_scope_locked()
            rows = self._conn.execute(
                "SELECT id, revision, closed_by, reviewed_at FROM cards"
                " WHERE column_name = 'done' ORDER BY id").fetchall()
            return count, clear_token, self._done_view_token(rows)

    def total(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM cards").fetchone()
        return int(row["n"]) if row else 0

    # --- writes ---

    def create(self, fields: dict) -> tuple:
        """Mint a card. `(card_or_None, detail)`.

        Refuses rather than truncates when a bound is exceeded: silently keeping
        the first 8000 characters of somebody's instructions and handing *that*
        to an agent is a worse outcome than a message saying it was too long.
        """
        fields = dict(fields or {})
        token = str(fields.get("create_token") or "").strip().lower()
        if token and not attachments.folder_name_ok(token):
            return None, "that is not a create token"
        if token:
            with self._lock:
                existing = self._card_by_create_token_locked(token)
                if existing is not None:
                    return existing, ALREADY_CREATED
        refusal = board_outcomes.objective_refusal(fields)
        if refusal:
            return None, refusal
        # A create may name a number; one that does not gets `''`, which is
        # "nobody has scored this" and is what the reconcile's one look at
        # `card_priority.py` keys on. An off-range number is refused here in
        # the same words `_update_locked` uses — never silently kept as `''`.
        area, refusal = areas.normalise(fields.get("area"))
        if refusal:
            return None, refusal
        priority, refusal = normalise_priority(fields.get("priority"))
        if refusal:
            return None, refusal
        kind, refusal = normalise_kind(fields.get("kind"))
        if refusal:
            return None, refusal
        title = str(fields.get("title") or "").strip()
        if not title:
            return None, "a card needs a title"
        if len(title) > MAX_TITLE_CHARS:
            return None, f"title is longer than {MAX_TITLE_CHARS} characters"
        prompt = str(fields.get("prompt") or "")
        if len(prompt) > MAX_PROMPT_CHARS:
            return None, f"instructions are longer than {MAX_PROMPT_CHARS} characters"
        summary = str(fields.get("summary") or "").strip()
        if len(summary) > MAX_SUMMARY_CHARS:
            return None, f"the summary is longer than {MAX_SUMMARY_CHARS} characters"
        # Prep, not Backlog: a new card is "written down, not yet turned into
        # a plan", which is exactly the Prep column's one-line meaning. A
        # caller that names a column still gets it — the migration moved no
        # existing rows, and tests and restores write columns explicitly.
        column = str(fields.get("column_name") or "prep")
        if column not in COLUMNS:
            return None, f"unknown column {column!r}"
        tool = str(fields.get("tool") or "")
        if tool not in TOOLS:
            return None, f"unknown tool {tool!r}"
        # `""` is Default and always legal, including on a card with no
        # assistant chosen yet; anything else has to be one of the handful of
        # strings Dark Army ships for *that* tool. `MODELS.get` is what makes a card
        # with no tool, or a tool with no catalogue, refuse a model rather than
        # carry one nothing will ever honour.
        model = str(fields.get("model") or "")
        if model and model not in MODELS.get(tool, ()):
            return None, f"this card names a model Dark Army does not offer for {tool}"
        if self.total() >= MAX_CARDS:
            return None, f"the board is full ({MAX_CARDS} cards)"
        attached = "\n".join(attachments.split_field(fields.get("attachments")))
        reason = attachments.field_refusal(attached)
        if reason:
            return None, reason

        now = time.time()
        card = {
            "id": uuid.uuid4().hex,
            "project": _clamp(fields.get("project"), 200),
            # The checkout, never a card folder: that folder is released when
            # its card finishes (`worktrees.checkout_root`).
            "root": _clamp(worktrees.checkout_root(
                str(fields.get("root") or "")), 1024),
            "title": title,
            "summary": summary,
            "prompt": prompt,
            "tool": tool,
            "column_name": column,
            "position": float(fields.get("position") or now),
            "session_id": "",
            "link_state": "",
            "dispatch_error": "",
            "dispatched_at": None,
            "session_ended_at": None,
            "author": _clamp(fields.get("author") or "user", 200),
            "workflow": join_stages(fields.get("workflow")),
            # Never seeded from `workflow`: an expected stage is not a stage
            # that ran, and conflating them is exactly the lie the two-column
            # split exists to prevent.
            "agent_trail": "",
            # Same argument, one rung on: nobody has worked this card yet, so
            # nobody's face belongs on it.
            "crew_trail": "",
            "plan_path": "",
            "refine_session_id": "",
            "refine_state": "",
            # Never born queued. A queue slot is a record of a person's Start
            # gesture on an existing card, so minting one cannot make it —
            # which is also why `dark_army_add_card` gains no queue argument.
            "queue_state": "",
            "queued_at": None,
            "queue_rank": None,
            "attachments": attached,
            "model": model,
            "start_when_planned": normalise_flag(fields.get("start_when_planned")),
            "priority": priority,
            "area": area,
            "kind": kind,
            "report_path": "",
            "report_verdict": "",
            "report_recommendation": "",
            "create_token": token,
            "created_at": now,
            "updated_at": now,
            "done_at": now if column == "done" else None,
        }
        card.update({k: fields.get(k, "") for k in board_outcomes.OBJECTIVE_LIMITS})
        card.update(outcome_revision=0, outcome_status="unaccepted")
        # A new card starts at zero, stated rather than left to the column
        # default: this dict is what `create` *returns*, and a caller that
        # got a card with no `revision` key would guard its first save
        # against nothing.
        card["revision"] = 0
        # The cards this one waits on, judged by the same reading
        # `_update_locked` uses; `''` when none. A refusal refuses the create.
        card["blocked_by"], refusal = self._blocked_by_refusal(
            card["id"], fields.get("blocked_by"), card["root"])
        if refusal:
            return None, refusal
        with self._lock:
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO cards (%s) VALUES (%s)" % (
                            ", ".join(card), ", ".join("?" * len(card))),
                        tuple(card.values()))
                    self._outcome_seed(card)
                    self._lifecycle_seed(card)
            except sqlite3.IntegrityError:
                existing = self._card_by_create_token_locked(token)
                if existing is None:
                    raise
                return existing, ALREADY_CREATED
        return card, "created"

    def update(self, card_id: str, fields: dict, *, bump: bool = True) -> tuple:
        """Change some of a card's fields. `(card_or_None, detail)`.

        `bump=False` is **Dark Army's own move**, and it is daemon-internal: no
        surface can reach it (`ApiServer` calls `daemon.update_card`, which
        passes no such keyword), and the three callers that use it are the
        dispatch write, `bind_session` and the bind-window expiry — the
        places where Dark Army moves a card's *column* without a person changing a
        word on it. Without it a card bound to a session while somebody was
        typing would refuse their save, which is verbatim the failure
        `REVISED_COLUMNS`' own docstring says it avoids. It never suppresses
        a write, only the counter.

        Only the named columns are written. That is the forward-compatibility
        half of the contract: a build that does not know about a column cannot
        blank it by writing a whole row it composed itself.

        The lock spans the whole read-then-write, not just the UPDATE: the
        validations below are judged against `current`, and a write landing
        between the read and the UPDATE (a Done drag on the loop while the
        reconcile runs on the executor) would be judged against a card that no
        longer exists. `_lock` is reentrant, so the `get` calls inside are fine.
        """
        with self._lock:
            return self._update_locked(card_id, fields, bump=bump)

    def _update_locked(self, card_id: str, fields: dict, *,
                       bump: bool = True) -> tuple:
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        refusal = self._objective_guard(current, fields or {})
        if refusal:
            return None, refusal
        # The card's own optimistic guard, `_objective_guard`'s position and
        # shape exactly: before any column is written, judged against
        # `current` under the same lock the UPDATE below runs under, so a
        # write landing between the read and the statement cannot slip past.
        #
        # **Absent means no guard**, and that is load-bearing: the channel's
        # card verbs, the reconcile, the auto-filer and every surface older
        # than this column send no `expected_revision` and must go on working
        # unchanged. `expected_revision` is not a column, so the
        # "ignore what is not in `_WRITABLE`" loop below drops it before the
        # UPDATE with no further code — `expected_outcome_revision`'s route.
        expected = (fields or {}).get("expected_revision")
        if expected is not None:
            if type(expected) is not int \
                    or expected != int(current.get("revision") or 0):
                return None, CARD_CHANGED_REFUSAL
        updates: dict = {}
        for key, value in (fields or {}).items():
            if key not in self._WRITABLE:
                continue
            if key == "column_name":
                if value not in COLUMNS:
                    return None, f"unknown column {value!r}"
            elif key == "tool":
                if value not in TOOLS:
                    return None, f"unknown tool {value!r}"
            elif key == "root":
                # A card folder is never a card's root (`worktrees.checkout_root`).
                value = worktrees.checkout_root(str(value or ""))
            elif key == "model":
                # Checked against the *resolved* tool below rather than here:
                # a write may name both in one go, and the pair has to be
                # judged together.
                value = str(value or "")
            elif key == "link_state":
                if value not in LINK_STATES:
                    return None, f"unknown link state {value!r}"
            elif key == "refine_state":
                if value not in REFINE_STATES:
                    return None, f"unknown refine state {value!r}"
            elif key == "title":
                value = str(value or "").strip()
                if not value:
                    return None, "a card needs a title"
                if len(value) > MAX_TITLE_CHARS:
                    return None, f"title is longer than {MAX_TITLE_CHARS} characters"
            elif key == "summary":
                value = str(value or "").strip()
                if len(value) > MAX_SUMMARY_CHARS:
                    return None, ("the summary is longer than "
                                  f"{MAX_SUMMARY_CHARS} characters")
            elif key == "prompt":
                value = str(value or "")
                if len(value) > MAX_PROMPT_CHARS:
                    return None, ("instructions are longer than "
                                  f"{MAX_PROMPT_CHARS} characters")
            elif key == "workflow":
                # Normalised rather than refused: a stage list is a convenience
                # a person types, and dropping a blank or a repeat is not the
                # kind of mistake worth an error message.
                value = join_stages(value)
            elif key == "blocked_by":
                value, refusal = self._blocked_by_refusal(
                    card_id, value, str(current.get("root") or ""))
                if refusal:
                    return None, refusal
            elif key == "position":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return None, "bad position"
            elif key == "start_when_planned":
                # Normalised, never refused: the two legal values are `'1'`
                # and `''`, and every shape a surface can send (a JSON
                # `true` arriving as `"True"`, a bare `"1"`, an empty
                # string, `None`) has to land on one of them.
                value = normalise_flag(value)
            elif key == "area":
                value, refusal = areas.normalise(value)
                if refusal:
                    return None, refusal
            elif key == "priority":
                # Refused rather than clamped: a client that sent 101 or
                # "high" has misunderstood the field, and silently storing
                # 100 would hide that from the person who typed it.
                # Clearing (`''`) is always legal — it is "no opinion".
                value, refusal = normalise_priority(value)
                if refusal:
                    return None, refusal
            elif key == "kind":
                value, refusal = normalise_kind(value)
                if refusal:
                    return None, refusal
                if (str(current.get("column_name") or "") != "prep"
                        and value != str(current.get("kind") or "")):
                    return None, KIND_LOCKED_REFUSAL
            elif key == "queue_state":
                if value not in QUEUE_STATES:
                    return None, f"unknown queue state {value!r}"
            elif key == "queued_at":
                # Nullable by design — there is no zero time meaning "not
                # queued" — so `None` passes through and anything else has to
                # be a real number. A junk stamp would silently reorder a
                # queue rather than fail, which is why this refuses.
                if value is not None:
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        return None, "bad queued_at"
            elif key == "attachments":
                value = "\n".join(attachments.split_field(value))
                reason = attachments.field_refusal(value)
                if reason:
                    return None, reason
            updates[key] = value
        if updates.get("queue_state") == "queued" \
                and str(current.get("queue_state") or "") != "queued":
            # The bound is refused here rather than at the caller, like
            # `MAX_CARDS`, so every route inherits it — and only on a card
            # *joining* the queue, so a re-stamp of a card already in it can
            # never be refused by the count it is itself part of.
            project = str(updates.get("project", current.get("project")) or "")
            if self.queued_count(project) >= MAX_QUEUED_PER_PROJECT:
                return None, (f"{project} already has "
                              f"{MAX_QUEUED_PER_PROJECT} cards queued")
            # A re-queue cannot inherit a stale rank if a clear path was
            # ever missed. Belt and braces on join.
            updates.setdefault("queue_rank", None)
        # The model belongs to the tool, so the pair is resolved together and
        # the store is the one place it happens — every route (the chip menu
        # sends `{"tool": x}` alone) inherits it, and the panel merely mirrors.
        # The clear is the third store-side implicit write, beside the queue
        # pair on `column_name` and `closed_by` on leaving Done, and safe in
        # the same direction: it can only ever remove a stale claim, never
        # manufacture one. Without it a grok model would survive onto a claude
        # card and `guard()` would refuse the next Start in words the presser
        # did not cause.
        if "model" in updates or "tool" in updates:
            new_tool = str(updates.get("tool", current.get("tool")) or "")
            named = str(updates.get("model") or "")
            if named and named not in MODELS.get(new_tool, ()):
                return None, ("this card names a model Dark Army does not offer "
                              f"for {new_tool}")
            retooled = "tool" in updates and updates["tool"] != current.get("tool")
            if retooled and not named:
                updates["model"] = ""
        if not updates:
            # Named fields, none of them writable — a caller that thinks it
            # changed something and did not. Answering with the card and
            # "nothing to change" made that indistinguishable from success all
            # the way up to the panel, which reported the write as landed.
            if fields:
                return None, "no writable fields in that update"
            return current, "nothing to change"
        # A card arriving in Done gets its stamp, and one leaving loses it: the
        # archive query orders on it, and a card sent back to Ready that kept a
        # done_at would sort into a list it is no longer in.
        if "column_name" in updates:
            if updates["column_name"] == "done":
                updates.setdefault("done_at", time.time())
            else:
                updates["done_at"] = None
                # Reopen is the undo, and it has to undo the *whole* close. A
                # card sent back to Backlog still carrying `closed_by` would,
                # on its next hand-drag into Done, claim a verifier had checked
                # it — a signature for a close that was explicitly withdrawn.
                # This is the only place `_WRITABLE`'s exclusion is bypassed
                # for these two, and clearing is the safe direction: it can
                # only ever remove a claim, never manufacture one.
                updates["closed_by"] = ""
                updates["close_note"] = ""
                # And the acknowledgement goes with the close it acknowledged —
                # the third field through this documented bypass, same argument
                # verbatim: Reopen undoes the *whole* close, and a card sent
                # back to Backlog still carrying `reviewed_at` would, on its
                # next agent close, skip the review banner for a close nobody
                # has seen. Clearing is the safe direction.
                updates["reviewed_at"] = None
                # And the merge pair: a card out of Done has no merge to
                # report (a new Start makes a fresh branch). The review pair
                # stays — it is a verdict of a version, aged by `review_tip`.
                updates["merge_state"] = ""
                updates["merge_note"] = ""
            # A hand-move of a queued card is the person overriding the queue,
            # so the queue slot goes with it — in *both* directions, which is
            # why this sits outside the done/not-done branch above. The
            # `closed_by` clear's precedent, and safe in the same direction:
            # clearing can only ever remove a claim on Dark Army's future
            # auto-start, never manufacture one. The dispatch path relies on
            # it too — the update that writes `link_state='dispatching'` also
            # moves the card, so there is no window where a card is both
            # queued and dispatching.
            updates.setdefault("queue_state", "")
            updates.setdefault("queued_at", None)
        if updates.get("queue_state") == "":
            # Leave-the-queue clear: `closed_by` / `close_note` /
            # `reviewed_at`'s bypass, safe in the same direction (clearing
            # removes a preference, never manufactures one). Every existing
            # clear path writes `queue_state: ""` through `update`, so the
            # rank drops with zero daemon edits.
            updates["queue_rank"] = None
        updates["updated_at"] = time.time()
        # The change number steps up only where this write actually changes
        # something a person reads (`REVISED_COLUMNS`), so a no-op save is
        # not a revision and the reconcile's `link_state` churn is not one
        # either. Written as part of the same statement — the store's own
        # column, never a value the caller supplies.
        bumped = bump and any(
            key in REVISED_COLUMNS and value != current.get(key)
            for key, value in updates.items())
        assignments = [f"{k} = ?" for k in updates]
        if bumped:
            assignments.append("revision = revision + 1")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE cards SET %s WHERE id = ?" % ", ".join(assignments),
                tuple(updates.values()) + (str(card_id),))
            after = self.get(card_id)
            next_sample = self._outcome_transition(current, after)
            self._lifecycle_transition(current, after)
            self._objective_written(current, fields or {}, after)
        # update() still holds the store lock. Only a committed human reopen
        # may consume the in-memory review sample; rollback retains it whole.
        if next_sample is not None:
            self._outcome_samples[card_id] = next_sample
        return self.get(card_id), "updated"

    def declare_done(self, card_id: str, session_id: str, note: str) -> tuple:
        """A session says the card it is working on is finished. `(card_or_None,
        detail)`.

        **Its own verb, for `record_agents`' reason.** `closed_by` and
        `close_note` stay outside `_WRITABLE`, so there is exactly one writer
        and no surface can forge a signature — a `board_update` that could set
        `closed_by` could paint a card somebody dragged across by hand with a
        verifier's name on it, which is the precise lie this column exists to
        be incapable of telling.

        This is **not** Dark Army deciding the work is done. It records that somebody
        said so, and who. `mark_ended`'s rule is untouched: a session ending
        still moves nothing.

        The scope is the security property. The card is named by the caller,
        but it is only closed when the row's own `session_id` is a non-empty
        string equal to `session_id` — so the only card a caller can ever reach
        is one it is already the running author of. The empty check is the
        second of the two guards `by_session` describes.

        **The guard is the write's own WHERE clause, not a read above it**, and
        that is the whole of what makes the claim below true. `_board_call`
        hands every verb to the default executor, so a human's Reopen — an
        `update` moving the card back to Backlog — can land between a read and
        an unconditional `WHERE id = ?`, and the card would end up back in Done
        wearing a signature for a close the human had just explicitly withdrawn:
        the one state this pair of columns exists to be incapable of. The same
        window swallowed a `board_delete`, the UPDATE matching nothing and the
        re-read answering "closed" over a card that no longer existed. So the
        identity and column tests ride in the statement and `rowcount == 0`
        means *that card moved, and nothing was closed*. The read above is for
        the wording of a refusal only; it decides nothing.

        The column is pinned to the one that was **observed**, not merely to
        "not `done`". `update` does not clear `session_id` when a card leaves
        In progress, so a card a human has just pressed Back on is still bound
        and still not in Done — a `!= 'done'` test would let the close land on
        it anyway, which is the withdrawn-close case itself. Pinning the exact
        column catches every move, in either direction, and the observed value
        can never be `done` because the read above refuses that outright.

        One statement under one lock, so a card can never be seen sitting in
        Done with no signature, or wearing a signature outside Done.
        """
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        owner = str(current.get("session_id") or "")
        caller = str(session_id or "")
        if not owner or not caller or owner != caller:
            return None, "that card is not this session's to close"
        seen_column = str(current.get("column_name") or "")
        if seen_column == "done":
            return None, "that card is already done"
        text = _clamp(note, MAX_CLOSE_NOTE_CHARS).strip()
        if not text:
            return None, "a close needs a reason"
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE cards SET column_name = 'done', done_at = ?,"
                " closed_by = ?, close_note = ?, updated_at = ?,"
                " revision = revision + 1"
                " WHERE id = ? AND session_id = ? AND column_name = ?",
                (now, caller, text, now, str(card_id), caller, seen_column))
            changed = cur.rowcount
            if changed:
                after = self.get(card_id)
                self._outcome_transition(current, after)
                self._lifecycle_transition(current, after)
        if not changed:
            # Somebody moved, reopened or deleted the card between the read
            # above and this write. Nothing happened, and saying so is the
            # honest answer — re-reading would report on whatever is there now.
            return None, "that card moved or was deleted — nothing was closed"
        return self.get(card_id), "closed"

    def flag_manual(self, card_id: str, session_id: str, steps: str,
                    path: str = "") -> tuple:
        """A session recording the hand-check it is handing back. `(card_or_None,
        detail)`.

        `declare_done`'s verb, shape and scope, said about the opposite
        outcome: that one is *this is finished*, this one is *this is not
        finished being checked, and here is what to do*. Same single-writer
        argument — `manual_steps` is outside `_WRITABLE`, so no surface can
        stamp a card nobody flagged — and same guard discipline: the identity
        and column tests ride in the UPDATE's own WHERE clause, not in a read
        above it, because `_board_call` hands every verb to the executor and a
        person can drag or delete the card in the seconds this spends there.
        `rowcount == 0` therefore means *that card moved, and nothing was
        flagged*; the read above decides only the wording of a refusal.

        The card must be the caller's own (a non-empty `session_id` equal to
        the row's, `by_session`'s two guards). **A card in Done may be
        flagged** (v26): a card with an open check goes to Done, and the
        order an agent picks — flag then close, or close then flag — must
        not leave a check unrecorded. The WHERE still pins the session and
        the column the read saw, so nothing widens beyond the session's own
        card.

        ``path`` is the check file, already validated and realpath'd by the
        daemon (`_manual_check_path_refusal`); it is written into
        `manual_check_path` in the same UPDATE. An empty path writes `''`,
        so re-flagging without a file clears a stale link.

        The steps are clamped rather than refused
        (`MAX_MANUAL_STEPS_CHARS`) — Dark Army is relaying somebody's words to a
        person, `close_note`'s case exactly — but an *empty* one is refused: a
        badge with nothing behind it is the state this column is built to be
        incapable of showing.
        """
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        owner = str(current.get("session_id") or "")
        caller = str(session_id or "")
        if not owner or not caller or owner != caller:
            return None, "that card is not this session's to flag"
        seen_column = str(current.get("column_name") or "")
        text = _clamp(steps, MAX_MANUAL_STEPS_CHARS).strip()
        if not text:
            return None, "a manual check needs its steps"
        check_path = str(path or "")
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET manual_steps = ?, manual_check_path = ?,"
                " manual_session_id = ?,"
                " updated_at = ?, revision = revision + 1"
                " WHERE id = ? AND session_id = ? AND column_name = ?",
                (text, check_path, caller, now, str(card_id), caller,
                 seen_column))
            self._conn.commit()
            changed = cur.rowcount
        if not changed:
            return None, "that card moved or was deleted — nothing was flagged"
        return self.get(card_id), "flagged"

    def by_manual_check_path(self, path: str) -> list:
        """Every card whose flag named this check file, exact match.

        An empty path returns `[]`, `by_session`'s guard and argument:
        `manual_check_path` defaults to `''`, so `WHERE … = ''` would match
        every card nobody flagged with a file."""
        text = str(path or "")
        if not text:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE manual_check_path = ?"
                + CARD_ORDER_SQL, (text,)).fetchall()
        return [self._row(r) for r in rows]

    #: A stored worktree path or branch longer than this is refused: both
    #: are composed by `worktrees.py` from the root and an eight-character
    #: id, so anything longer was not.
    MAX_WORKTREE_FIELD_CHARS = 1024

    def record_worktree(self, card_id: str, path: str, branch: str) -> tuple:
        """The folder and branch a started card works in. `(card_or_None,
        detail)`.

        Daemon bookkeeping and the one writer of the pair (`SINGLE_WRITER`),
        `declare_done`'s shape: one UPDATE, the guard in its own WHERE,
        `rowcount == 0` meaning the card is gone. No `revision` step — what
        folder a run works in is Dark Army's record, not a person's edit,
        and a change number that moved here would refuse the save of
        whoever had the card open. The branch must be non-empty; the path may
        be empty (a finished card keeps the memory of its branch after the
        folder goes, and the backfill records one found by name). The
        emptying is `clear_worktree`'s, never a record of `''`.
        """
        path = str(path or "")
        branch = str(branch or "")
        if not branch:
            return None, "a worktree needs a branch"
        if len(path) > self.MAX_WORKTREE_FIELD_CHARS \
                or len(branch) > self.MAX_WORKTREE_FIELD_CHARS:
            return None, "that worktree name is too long"
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET worktree_path = ?, worktree_branch = ?,"
                " updated_at = ? WHERE id = ?",
                (path, branch, time.time(), str(card_id)))
            self._conn.commit()
            changed = cur.rowcount
        if not changed:
            return None, "that card is gone"
        return self.get(card_id), "recorded"

    def clear_worktree(self, card_id: str, *, branch: bool = False) -> tuple:
        """`record_worktree`'s other half: the folder has been removed.
        `(card_or_None, detail)`. By default only the card's note of the
        folder goes and **the branch name stays** — the card's memory of the
        branch it has not yet merged. `branch=True` (a landed merge that
        deleted the branch) empties the name too. A card with nothing to
        clear is `rowcount == 0`."""
        if branch:
            sql = ("UPDATE cards SET worktree_path = '', worktree_branch = '',"
                   " updated_at = ? WHERE id = ?"
                   " AND (worktree_path != '' OR worktree_branch != '')")
        else:
            sql = ("UPDATE cards SET worktree_path = '', updated_at = ?"
                   " WHERE id = ? AND worktree_path != ''")
        with self._lock:
            cur = self._conn.execute(sql, (time.time(), str(card_id)))
            self._conn.commit()
            changed = cur.rowcount
        if not changed:
            return None, "that card has no worktree recorded"
        return self.get(card_id), "cleared"

    #: A stored merge sentence is cut here; the daemon composes it, the
    #: bound is a belt for a path-heavy conflict.
    MAX_MERGE_NOTE_CHARS = 2000

    def record_merge(self, card_id: str, state: str, note: str = "") -> tuple:
        """What the last MERGE press came to. `(card_or_None, detail)`.

        The one writer of the pair (`SINGLE_WRITER`), `record_worktree`'s
        shape: one UPDATE, the card's Done column and the guard in its own
        WHERE, `rowcount == 0` meaning the card is gone or not in Done. `state`
        is one of `merges.STATES`; `''` empties both columns (the press
        starting over). No `revision` step: it is Dark Army's record, not a
        person's edit.
        """
        state = str(state or "")
        if state not in merges.STATES:
            return None, "that is not a merge state"
        note = "" if not state else str(note or "")[:self.MAX_MERGE_NOTE_CHARS]
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET merge_state = ?, merge_note = ?,"
                " updated_at = ? WHERE id = ? AND column_name = 'done'",
                (state, note, time.time(), str(card_id)))
            self._conn.commit()
            changed = cur.rowcount
        if not changed:
            return None, "that card is gone or not in Done"
        return self.get(card_id), "recorded"

    def record_review_verdict(self, card_id: str, verdict: str,
                              tip: str = "") -> tuple:
        """A review's verdict and the branch tip it judged. `(card_or_None,
        detail)`. `verdict` is one of `merges.VERDICTS`; `tip` a full commit
        hash or `''`. The one writer of the pair; no `revision` step."""
        verdict = str(verdict or "")
        tip = str(tip or "")
        if verdict not in merges.VERDICTS:
            return None, "that is not a review verdict"
        if tip and not merges.is_tip(tip):
            return None, "that is not a commit"
        if not verdict:
            tip = ""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET review_verdict = ?, review_tip = ?,"
                " updated_at = ? WHERE id = ?",
                (verdict, tip, time.time(), str(card_id)))
            self._conn.commit()
            changed = cur.rowcount
        if not changed:
            return None, "that card is gone"
        return self.get(card_id), "recorded"

    def clear_manual(self, card_id: str,
                     expected_manual_steps: Optional[str] = None) -> tuple:
        """A person saying they have done the check. `(card_or_None, detail)`.

        ``expected_manual_steps`` is the phone's current-state echo — the
        steps that were on its screen when the press was confirmed. Absent
        (`None`, the Mac's own press) means no extra guard, exactly
        `expected_revision`'s rule; present, it is ANDed into the same UPDATE
        WHERE, so a stale screen cannot clear a *different* check. Empty or
        mismatched is `MANUAL_CHECK_CHANGED_REFUSAL` with nothing written.
        It is a confirmation, not a capability: pairing, the seal and the
        lease all ran above this, and the one-shot `manual_steps != ''` test
        stays in the statement whether or not the echo is present.

        The other half of the single-writer pair, and the *only* route by which
        `manual_steps` empties. Its own verb rather than a field on `update`
        for `flag_manual`'s reason: a surface that could write this column
        could set one as easily as clear one, and the whole value of the badge
        is that it appears only where a session put it.

        Unarmed on the surface that offers it. It destroys a note, which is why
        it is not entirely free — but the undo is that the session can say it
        again, and an arm on a verb somebody presses after doing a chore is
        ceremony that teaches people to press twice.

        No session test: this is explicitly a *human* clearing a human's chore,
        and the card may well have no live session left by the time anybody
        gets to it.
        """
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        if not str(current.get("manual_steps") or ""):
            return None, "that card has no manual check outstanding"
        where = " WHERE id = ? AND manual_steps != ''"
        params: tuple = (str(card_id),)
        if expected_manual_steps is not None:
            echo = str(expected_manual_steps)
            if not echo or echo != str(current.get("manual_steps") or ""):
                return None, MANUAL_CHECK_CHANGED_REFUSAL
            where += " AND manual_steps = ?"
            params += (echo,)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET manual_steps = '', manual_session_id = '',"
                " updated_at = ?, revision = revision + 1" + where,
                (now,) + params)
            self._conn.commit()
            changed = cur.rowcount
        if not changed:
            if expected_manual_steps is not None:
                return None, MANUAL_CHECK_CHANGED_REFUSAL
            return None, "that card moved or was deleted — nothing was cleared"
        return self.get(card_id), "cleared"

    def mark_reviewed(self, card_id: str,
                      expected_closed_by: Optional[str] = None,
                      expected_close_note: Optional[str] = None) -> tuple:
        """A person acknowledging an assistant's close. `(card_or_None, detail)`.

        ``expected_closed_by`` / ``expected_close_note`` are the phone's
        current-state echo — the close it drew when the press was confirmed.
        Both `None` (the Mac's own press) is today's statement; both present
        are ANDed into the same UPDATE WHERE, so a stale screen cannot
        review a *different* close. One without the other is
        `REVIEW_HALF_ECHO_REFUSAL` before any UPDATE; a mismatch is
        `REVIEW_CHANGED_REFUSAL` with nothing written. Keyed on the close
        identity and not `expected_revision`, because a title edit moves the
        revision and would refuse a still-valid review of the same close.

        `declare_done`'s shape, pointed the other way: that verb records the
        assistant's statement, this one records that a human has *seen* it,
        which is what lets the "FINISHED · REVIEW" banner drop and the card
        sink into the ordinary Done order. One-way and store-written —
        `reviewed_at` is outside `_WRITABLE` and outside the API's field
        allow-list, so a card sheet Save can never silently acknowledge a
        review the person did not make; the gesture arrives only as the named
        verb (`board_review`).

        Same guard discipline as the other single-writer verbs: the column,
        signature and not-yet-reviewed tests ride in the UPDATE's own WHERE
        clause, because `_board_call` hands every verb to the executor and a
        person can reopen or delete the card in the seconds this spends there.
        `rowcount == 0` therefore means *that card moved, and nothing was
        acknowledged*; the read above decides only the wording of a refusal.

        No session test, `clear_manual`'s reason exactly: this is a human
        acknowledging a close, and the closing session is routinely long gone
        by the time anybody reads the banner.
        """
        echoed = (expected_closed_by is not None,
                  expected_close_note is not None)
        if any(echoed) and not all(echoed):
            return None, REVIEW_HALF_ECHO_REFUSAL
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        if str(current.get("column_name") or "") != "done":
            return None, "that card is not in Done"
        if not str(current.get("closed_by") or ""):
            return None, "only an assistant's close takes a review"
        if current.get("reviewed_at") is not None:
            return None, (REVIEW_CHANGED_REFUSAL if all(echoed)
                          else "that card is already reviewed")
        where = (" WHERE id = ? AND column_name = 'done'"
                 " AND closed_by != '' AND reviewed_at IS NULL")
        params: tuple = (str(card_id),)
        if all(echoed):
            by, note = str(expected_closed_by), str(expected_close_note)
            if not by or by != str(current.get("closed_by") or "") \
                    or note != str(current.get("close_note") or ""):
                return None, REVIEW_CHANGED_REFUSAL
            where += " AND closed_by = ? AND close_note = ?"
            params += (by, note)
        now = time.time()
        with self._lock:
            # `updated_at` **is** stamped: a person pressing Reviewed is
            # somebody acting on the card, unlike `record_agents`' observing.
            cur = self._conn.execute(
                "UPDATE cards SET reviewed_at = ?, updated_at = ?" + where,
                (now, now) + params)
            self._conn.commit()
            changed = cur.rowcount
        if not changed:
            if all(echoed):
                return None, REVIEW_CHANGED_REFUSAL
            return None, ("that card moved, was reopened or is already "
                          "reviewed — nothing changed")
        return self.get(card_id), "reviewed"

    def attach_plan(self, card_id: str, path: str, session_id: str) -> tuple:
        """A refinement attaching the plan it wrote. `(card_or_None, detail)`.

        `declare_done`'s shape, for `declare_done`'s reason: the guard rides in
        the write's own WHERE clause, not in a read above it, because
        `_board_call` hands every verb to the executor and a human can move or
        delete the card between a read and an unconditional `WHERE id = ?`.
        `rowcount == 0` therefore means *that card moved — nothing was
        attached*, and the read above decides only the wording of a refusal.

        One statement does the whole exit: `plan_path` written, the card moved
        to Backlog, `refine_state` and the batch mark (`batch_id`,
        `batch_rank`) cleared. The Prep column's exit condition is
        this write and nothing else — `plan_path` is outside `_WRITABLE`, so
        there is exactly one writer, which is what makes "a Backlog card has a
        plan" a checkable claim rather than a convention.

        The card must still be in `prep` with no plan: attaching to a card that
        already has one is re-refinement, which is deliberately out of scope
        (the refusal says so), and attaching to a card somebody already dragged
        onward is second-guessing a human gesture. Path validation (containment,
        `.md`, size) is the daemon's job — the store does not touch the disk.

        `session_id` is recorded onto `refine_session_id` when the caller has
        one, so a hand-run `/ship` that filed the card and attaches in the same
        breath leaves the same audit trail a dispatched refinement does.
        """
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        if str(current.get("kind") or "") == KIND_SCOUT:
            return None, SCOUT_PLAN_REFUSAL
        if str(current.get("column_name") or "") != "prep":
            return None, "that card is not in Prep — only a Prep card takes a plan"
        if str(current.get("plan_path") or ""):
            return None, "that card already has a plan attached"
        text = str(path or "").strip()
        if not text:
            return None, "a plan needs a path"
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET plan_path = ?, column_name = 'backlog',"
                " refine_state = '', refine_session_id = ?, updated_at = ?,"
                " batch_id = '', batch_rank = '',"
                " revision = revision + 1"
                " WHERE id = ? AND column_name = 'prep' AND plan_path = ''",
                (text, str(session_id or current.get("refine_session_id") or ""),
                 now, str(card_id)))
            changed = cur.rowcount
            if changed:
                # The exit of Prep as a moment on the card's timeline, in a
                # SAVEPOINT so a ledger failure can never refuse the attach.
                self._lifecycle_try(self._plan_attached_locked, current, now)
            self._conn.commit()
        if not changed:
            return None, "that card moved — nothing was attached"
        return self.get(card_id), "attached"

    def attach_report(self, card_id: str, path: str, session_id: str, *,
                      verdict: str = "", recommendation: str = "") -> tuple:
        """A scout attaching the report it wrote. `(card_or_None, detail)`.

        `attach_plan`'s shape, for `attach_plan`'s reason: the guard rides in
        the write's own WHERE clause, not in a read above it, because
        `_board_call` hands every verb to the executor and a human can move
        the card between a read and an unconditional `WHERE id = ?`.
        `rowcount == 0` therefore means *that card moved — nothing was
        attached*, and the read above decides only the wording of a refusal.

        The card must still be an In-progress scout bound to `session_id`:
        attaching to a build card is the wrong verb, attaching to a Done
        scout is too late, and a different session cannot point this card at
        a file it did not write. A second attach by the same session
        **replaces** the path — a scout may revise its report. Path
        validation (containment, `.md`, size) is the daemon's job — the
        store does not touch the disk. The card stays In progress; closing
        it is `declare_done`.

        `verdict` / `recommendation` are the report's answer block as the
        daemon read it at the attach (`scout_report.read_header`), written
        in the same UPDATE under the same guard, so a re-attach replaces
        them with the path. The verdict is collapsed to one line and
        clamped at `MAX_SUMMARY_CHARS` (clamp-don't-refuse, `close_note`'s
        rule); a recommendation outside `scout_report.RECOMMENDATIONS` is
        stored as `''`, never as the stray word.
        """
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        if str(current.get("column_name") or "") != "in_progress":
            return None, ("that card is not in progress — a report is "
                          "attached by the running scout")
        if str(current.get("kind") or "") != KIND_SCOUT:
            return None, REPORT_NOT_SCOUT_REFUSAL
        text = str(path or "").strip()
        if not text:
            return None, "a report needs a path"
        line = " ".join(str(verdict or "").split())
        line = line[:MAX_SUMMARY_CHARS].rstrip()
        word = str(recommendation or "").strip().lower()
        if word not in scout_report.RECOMMENDATIONS:
            word = ""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET report_path = ?, report_verdict = ?,"
                " report_recommendation = ?, updated_at = ?,"
                " revision = revision + 1"
                " WHERE id = ? AND column_name = 'in_progress'"
                " AND session_id = ? AND kind = 'scout'",
                (text, line, word, now, str(card_id),
                 str(session_id or "")))
            changed = cur.rowcount
            self._conn.commit()
        if not changed:
            return None, "that card moved — nothing was attached"
        return self.get(card_id), "attached"

    def approve_plan(self, card_id: str, plan_path: str, digest: str,
                     approved_at: float) -> tuple:
        """Record that a person read *this version* of the card's plan.
        `(card_or_None, detail)`.

        `attach_plan`'s shape, for `attach_plan`'s reason: the guard rides in
        the write's own WHERE clause rather than in a read above it, because
        `_board_call` hands every verb to the executor and a human can move or
        repoint the card between a read and an unconditional `WHERE id = ?`.
        `rowcount == 0` therefore means *that card's plan is no longer the one
        being approved*.

        `digest` is the caller's echo of what the daemon hashed off disk a
        moment ago — the store never touches the disk (`attach_plan`'s stated
        rule) and never computes a hash. It only writes down what it was told,
        against the exact `plan_path` it was told it belongs to.
        """
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        path = str(plan_path or "").strip()
        if not path:
            return None, "a plan needs a path"
        if not str(current.get("plan_path") or ""):
            return None, "that card has no plan to approve"
        mark = str(digest or "").strip()
        if not mark:
            return None, "an approval needs the plan it approves"
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET plan_approved = ?, plan_approved_at = ?,"
                " updated_at = ? WHERE id = ? AND plan_path = ?",
                (mark, float(approved_at or now), now, str(card_id), path))
            self._conn.commit()
            changed = cur.rowcount
        if not changed:
            return None, "that card's plan changed — read it again"
        return self.get(card_id), "approved"

    def record_agents(self, card_id: str, names, crew=None) -> tuple:
        """Add stages to a card's observed trail. `(card_or_None, changed)`.

        Append-only and order-preserving: the trail is a record of what ran, in
        the order it ran, so an existing entry is never moved and a stage that
        has since stopped is never removed. Its own verb rather than a field on
        `update` because `agent_trail` is deliberately outside `_WRITABLE` —
        there is one writer, the reconcile, and this is it.

        `crew` is ``{stage: character}`` for the stages being added. It is
        merged **`setdefault`-shaped, never `update`-shaped**: a stage already
        carrying a character keeps it forever. That is the whole memory — a
        finished part of the job remembers who did it even after that character
        has been allocated to somebody else's card — and rewriting it would
        make the record say something Dark Army never observed.

        `changed` is the caller's whole reason for calling: `_reconcile_board`
        folds it into whether the board earns an SSE frame, and a session whose
        stages are all already recorded — which is every snapshot after the
        first for a given stage — must buy nothing.

        Does **not** stamp `updated_at`. A stage arriving is Dark Army observing, not
        somebody editing the card, and the Done archive sorts on that column.
        """
        current = self.get(card_id)
        if current is None:
            return None, False
        trail = parse_stages(current.get("agent_trail"))
        merged = list(trail)
        for name in parse_stages(names):
            if name not in merged and len(merged) < MAX_STAGES:
                merged.append(name)
        faces = parse_crew(current.get("crew_trail"))
        merged_faces = dict(faces)
        for stage, character in parse_crew(crew).items():
            if stage in merged and stage not in merged_faces:
                merged_faces[stage] = character
        if merged == trail and merged_faces == faces:
            return current, False
        with self._lock:
            self._conn.execute(
                "UPDATE cards SET agent_trail = ?, crew_trail = ? WHERE id = ?",
                ("\n".join(merged), join_crew(merged_faces), str(card_id)))
            self._conn.commit()
        return self.get(card_id), True

    def fill_area_if_empty(self, card_id: str, slug: str) -> tuple:
        """Seed plan metadata without overwriting a person's concurrent choice."""
        slug, refusal = areas.normalise(slug)
        if refusal or not slug:
            return self.get(card_id), False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET area = ?, revision = revision + 1"
                " WHERE id = ? AND area = ''", (slug, str(card_id)))
            self._conn.commit()
            changed = cur.rowcount
        return self.get(card_id), bool(changed)

    def fill_objective_if_empty(self, card_id: str, objective: dict) -> tuple:
        """Seed the objective a plan states, field by field, into boxes that
        are still empty. `(card, changed)`, `fill_area_if_empty`'s shape.

        A person's typed words always win: each field is its own conditional
        UPDATE, so a plan that names three things fills only the ones nobody
        has written. Clamped to `OBJECTIVE_LIMITS` rather than refused — this
        is Dark Army reading a document, not a person's Save, and a long sentence
        cut short is better than a card left blank. The outcome revision
        steps once when anything landed, so an open Mac editor reloads
        rather than saving stale empties over the seed; the outcome ledger's
        copy follows in the same transaction.
        """
        fields = {}
        for key in ("beneficiary", "intended_benefit", "success_criterion"):
            value = str((objective or {}).get(key) or "").strip()
            if value:
                fields[key] = value[:board_outcomes.OBJECTIVE_LIMITS[key]]
        if not fields:
            return self.get(card_id), False
        with self._lock:
            changed = 0
            for key, value in fields.items():
                cur = self._conn.execute(
                    f"UPDATE cards SET {key} = ? WHERE id = ? AND {key} = ''",
                    (value, str(card_id)))
                changed += cur.rowcount
            if changed:
                self._conn.execute(
                    "UPDATE cards SET revision = revision + 1,"
                    " outcome_revision = outcome_revision + 1 WHERE id = ?",
                    (str(card_id),))
                row = self._conn.execute(
                    "SELECT * FROM cards WHERE id = ?", (str(card_id),)).fetchone()
                if row is not None:
                    self._conn.execute(
                        "UPDATE outcome_cards SET objective = ? WHERE card_id = ?",
                        (json.dumps({k: row[k] for k in board_outcomes.OBJECTIVE_LIMITS}),
                         str(card_id)))
            self._conn.commit()
        return self.get(card_id), bool(changed)

    def fill_dependencies_if_empty(self, card_id: str, ids) -> tuple:
        """Seed the cards this one waits on, only while nobody has set any.
        `(card_or_None, detail)`.

        `fill_area_if_empty`'s job for the plan's `Depends on:` header, but
        written through `_update_locked` rather than a bare UPDATE, because a
        dependency list has refusals a slug has not — a self-wait, a cycle, a
        card in another project — and a seed must not be the one writer that
        skips them. The emptiness check and the write share `_lock`, so a
        person's list typed meanwhile always wins. A refusal answers
        `(None, words)` and writes nothing; a card that already has a list
        answers it unchanged with `"already set"`.
        """
        wanted = parse_ids(ids)
        with self._lock:
            current = self.get(card_id)
            if current is None:
                return None, "no such card"
            if parse_ids(current.get("blocked_by")):
                return current, "already set"
            if not wanted:
                return current, "nothing to fill"
            card, detail = self._update_locked(
                card_id, {"blocked_by": join_ids(wanted)})
            if card is None:
                return None, detail
            return card, "filled"

    def fill_workflow_if_empty(self, card_id: str, names) -> tuple:
        """Declare stages only while the card still has no workflow.

        Startup backfill races ordinary card edits through the executor. The
        empty check therefore belongs in the ``UPDATE`` itself: a person who
        declares a workflow while Dark Army is resolving a plan always wins. This is
        application metadata, not a human edit, so neither ``updated_at`` nor
        the observation-only ``agent_trail`` is touched.
        """
        workflow = join_stages(names)
        if not workflow:
            return self.get(card_id), False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET workflow = ?, revision = revision + 1"
                " WHERE id = ? AND workflow = ''",
                (workflow, str(card_id)),
            )
            self._conn.commit()
            changed = cur.rowcount
        return self.get(card_id), bool(changed)

    def move(self, card_id: str, column: str) -> tuple:
        """A card's column, which is the only move this slice offers. Ordering
        within a column is `position`, in the schema so drag-and-drop can arrive
        later without a migration."""
        if column not in COLUMNS:
            return None, f"unknown column {column!r}"
        return self.update(card_id, {"column_name": column})

    def _blocked_by_refusal(self, card_id: str, value, root: str) -> tuple:
        """`(joined_ids, refusal)` for a `blocked_by` value on card `card_id`
        in project `root`: the one reading `create` and `_update_locked` share.

        Refuses a too-long id, a self-wait, a cycle and a card in another
        project, in those words. An id naming no card is not refused — it
        cannot hold anything (a missing dependency reads as met) and a
        restore should still mean something (`parse_ids`). The cap and the
        order are `parse_ids`' / `join_ids`'.
        """
        ids = parse_ids(value)
        if any(len(i) > MAX_CARD_ID_CHARS for i in ids):
            return "", ("a card id is at most "
                        f"{MAX_CARD_ID_CHARS} characters")
        if str(card_id) in ids:
            return "", "a card cannot wait on itself"
        if self._creates_cycle(card_id, ids):
            return "", "those cards already wait on each other"
        # Same project only: the queue, the drain and the parallel limit are
        # per project, so a hold on another project's card would stall this
        # queue with nothing on this project's screen saying why.
        mine = normalise_root(str(root or ""))
        for other_id in ids:
            other = self.get(other_id)
            if other is not None and normalise_root(
                    str(other.get("root") or "")) != mine:
                return "", ("a card can only wait on a card in its own "
                            "project")
        return join_ids(ids), ""

    def _creates_cycle(self, card_id: str, ids) -> bool:
        """Whether any path from the named blockers reaches `card_id`.

        A missing id is not a node — it cannot close a cycle. A path that
        reaches `card_id` is. Walked here rather than at a surface so every
        writer inherits it.
        """
        target = str(card_id)
        seen: set = set()
        stack = list(ids)
        while stack:
            nid = stack.pop()
            if nid == target:
                return True
            if nid in seen:
                continue
            seen.add(nid)
            node = self.get(nid)
            if node is None:
                continue
            stack.extend(parse_ids(node.get("blocked_by")))
        return False

    def reorder(self, card_id, column, before_id, extra=None) -> tuple:
        """Place `card_id` in `column` immediately before `before_id`.

        Empty `before_id` appends. The midpoint is computed here so a client
        never sends a raw float — `position` is writable in the store and
        deliberately not an API field. A gap too small to split (or two
        neighbours sharing a position) reindexes **this column only** to
        1, 2, 3… then places. `extra` rides the same `update` so a drag back
        to Backlog can unlink in the same write.
        """
        if column not in COLUMNS:
            return None, f"unknown column {column!r}"
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        before_id = str(before_id or "")
        # The gap drawn immediately above a card uses that card's own id as
        # `before_id`. That is "put it where it already is", not a missing
        # neighbour — 409 here is a lie, and a few pixels of slop toward the
        # card's top edge would otherwise look like a broken drag.
        if before_id == str(card_id):
            return current, "ok"
        others = self._column_order(column, exclude_id=card_id)
        pos = self._place_position(others, before_id)
        if pos is None:
            return None, "that card is not in the column"
        if self._gap_too_tight(others, before_id):
            self._reindex_column(column, exclude_id=card_id)
            others = self._column_order(column, exclude_id=card_id)
            pos = self._place_position(others, before_id)
            if pos is None:
                return None, "that card is not in the column"
        fields = dict(extra or {})
        fields["column_name"] = column
        fields["position"] = pos
        return self.update(card_id, fields)

    def _column_order(self, column: str, exclude_id: str) -> list:
        sql = ("SELECT * FROM cards WHERE column_name = ? AND id != ? "
               "ORDER BY position, created_at")
        with self._lock:
            rows = self._conn.execute(
                sql, (column, str(exclude_id))).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _place_position(others: list, before_id: str):
        """Midpoint (or append / insert-at-start). `None` if `before_id` is
        set and not in `others` — refuse rather than guess."""
        if not before_id:
            if not others:
                return 1.0
            return float(others[-1].get("position") or 0) + 1.0
        ids = [c["id"] for c in others]
        if before_id not in ids:
            return None
        idx = ids.index(before_id)
        hi = float(others[idx].get("position") or 0)
        if idx == 0:
            return hi - 1.0
        lo = float(others[idx - 1].get("position") or 0)
        return (lo + hi) / 2.0

    @staticmethod
    def _gap_too_tight(others: list, before_id: str) -> bool:
        if not before_id:
            return False
        ids = [c["id"] for c in others]
        if before_id not in ids:
            return False
        idx = ids.index(before_id)
        if idx == 0:
            return False
        hi = float(others[idx].get("position") or 0)
        lo = float(others[idx - 1].get("position") or 0)
        return abs(hi - lo) < 1e-6 or lo == hi

    def _reindex_column(self, column: str, exclude_id: str) -> None:
        """Rewrite this column to 1, 2, 3… excluding `exclude_id`. Other
        columns are untouched. Own lock, then the subsequent `update` takes
        it again — this is not an `RLock`."""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM cards WHERE column_name = ? AND id != ? "
                "ORDER BY position, created_at",
                (column, str(exclude_id))).fetchall()
            for i, row in enumerate(rows, start=1):
                self._conn.execute(
                    "UPDATE cards SET position = ?, updated_at = ? WHERE id = ?",
                    (float(i), now, row["id"]))
            self._conn.commit()

    def move_queued(self, card_id: str, before_id: str) -> tuple:
        """Place a queued card immediately before `before_id`. `(card_or_None,
        detail)`.

        Empty `before_id` appends. `queue_rank` is written here alone, on
        `plan_path`'s ring: a surface that could set it through `update`
        would be a second writer of one order. `queued_at` is not in the
        SET list — it is the record of the person's Start gesture, and a
        drag must not restamp it.

        Rank is minted on the enqueue-stamp axis (epoch seconds), so a
        never-dragged card (rank NULL, stamp now) still lands at the back
        of a hand-ordered queue. `MAX_QUEUED_PER_PROJECT` is not consulted:
        a move adds no card.

        The UPDATE's WHERE clause is the race answer: `rowcount == 0`
        means the card was dispatched, unqueued or deleted between the
        pointer going down and the drop landing (`declare_done`'s shape).
        """
        current = self.get(card_id)
        if current is None:
            return None, "no such card"
        if str(current.get("queue_state") or "") != "queued":
            return None, "that card is not queued"
        before_id = str(before_id or "")
        # The gap drawn immediately above a card uses that card's own id as
        # `before_id`. That is "put it where it already is", not a missing
        # neighbour — `reorder`'s drop-on-your-own-gap slop rule.
        if before_id == str(card_id):
            return current, "ok"
        others = [c for c in self.queued_cards(current.get("project"))
                  if str(c.get("id") or "") != str(card_id)]
        if before_id:
            ids = [c["id"] for c in others]
            if before_id not in ids:
                return None, "that card is no longer queued"
        if self._queue_gap_too_tight(others, before_id):
            self._reindex_queue(current.get("project"), exclude_id=card_id)
            others = [c for c in self.queued_cards(current.get("project"))
                      if str(c.get("id") or "") != str(card_id)]
            if before_id:
                ids = [c["id"] for c in others]
                if before_id not in ids:
                    return None, "that card is no longer queued"
        rank = self._place_queue_rank(others, before_id)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET queue_rank = ?, updated_at = ? "
                "WHERE id = ? AND queue_state = 'queued'",
                (rank, now, str(card_id)))
            self._conn.commit()
            if cur.rowcount == 0:
                return None, "that card moved"
        return self.get(card_id), "ok"

    @staticmethod
    def _place_queue_rank(others: list, before_id: str) -> float:
        """Mint a rank from neighbours' effective keys.

        Before the head → `head_key - 1.0`. Between two → midpoint.
        Append → `tail_key + 1e-3`, never `+ 1.0`: a one-second step on
        an enqueue stamp can land up to a second in the future, and a
        Start in that window would jump the card just put last. A
        millisecond step cannot: the cap is 8, 8 ms is still inside one
        human gesture, and a later Start carries a later stamp.
        """
        if not others:
            return 1.0
        keys = [queue_key(c)[0] for c in others]
        if not before_id:
            return keys[-1] + 1e-3
        idx = [c["id"] for c in others].index(before_id)
        hi = keys[idx]
        if idx == 0:
            return hi - 1.0
        return (keys[idx - 1] + hi) / 2.0

    @staticmethod
    def _queue_gap_too_tight(others: list, before_id: str) -> bool:
        if not before_id or not others:
            return False
        ids = [c["id"] for c in others]
        if before_id not in ids:
            return False
        idx = ids.index(before_id)
        if idx == 0:
            return False
        hi = queue_key(others[idx])[0]
        lo = queue_key(others[idx - 1])[0]
        return abs(hi - lo) < 1e-6 or lo == hi

    def _reindex_queue(self, project, exclude_id: str) -> None:
        """Rewrite this project's queued cards to `queue_rank` = 1, 2, 3…
        excluding `exclude_id`. Other projects are untouched.

        Small integers are safe on the rank axis because a fresh enqueue
        carries an epoch stamp and therefore sorts after them. Do not
        "simplify" this by re-stamping `queued_at` — that is the record of
        when the person pressed Start.

        Own lock, then the subsequent UPDATE takes it again — this is not
        an `RLock`.
        """
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM cards WHERE queue_state = 'queued' "
                "AND project = ? AND id != ? "
                "ORDER BY COALESCE(queue_rank, queued_at, 0), id",
                (str(project or ""), str(exclude_id))).fetchall()
            for i, row in enumerate(rows, start=1):
                self._conn.execute(
                    "UPDATE cards SET queue_rank = ?, updated_at = ? "
                    "WHERE id = ?",
                    (float(i), now, row["id"]))
            self._conn.commit()

    def delete(self, card_id: str) -> tuple:
        """Destroy a card. `(ok, detail)`. Nothing is archived — the board is the
        list, and a deleted card was written by the person deleting it."""
        with self._lock:
            card = self.get(card_id)
            with self._conn:
                if card is not None:
                    self._lifecycle_on_delete([card])
                self._conn.execute("DELETE FROM card_messages WHERE card_id = ?",
                                   (str(card_id),))
                self._conn.execute("DELETE FROM card_runs WHERE card_id = ?",
                                   (str(card_id),))
                cur = self._conn.execute("DELETE FROM cards WHERE id = ?",
                                         (str(card_id),))
        return (cur.rowcount > 0), ("deleted" if cur.rowcount else "no such card")

    def clear_done(self, expected_count: int, expected_token: str) -> tuple:
        """Destroy every card still in Done. ``(ok, deleted_count, detail)``.

        Count and exact membership are checked under the same lock and
        transaction as the category-scoped DELETE, so even an equal-count swap
        cannot make a confirmation clear a different Done set. This is one
        statement rather than calls to :meth:`delete`: partial completion and
        one snapshot per card are both unacceptable for a bulk action.
        """
        expected = int(expected_count)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                actual, actual_token = self._done_scope_locked()
                if actual != expected or actual_token != str(expected_token):
                    self._conn.rollback()
                    return False, 0, (
                        "Done changed while you were confirming; "
                        "nothing was cleared")
                gone = [self._row(row) for row in self._conn.execute(
                    "SELECT * FROM cards WHERE column_name = 'done'")]
                ids = [row["id"] for row in gone]
                if ids:
                    self._lifecycle_on_delete(gone)
                    self._conn.executemany(
                        "DELETE FROM card_messages WHERE card_id = ?",
                        [(i,) for i in ids])
                    self._conn.executemany(
                        "DELETE FROM card_runs WHERE card_id = ?",
                        [(i,) for i in ids])
                cur = self._conn.execute(
                    "DELETE FROM cards WHERE column_name = 'done'")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        deleted = int(cur.rowcount or 0)
        return True, deleted, "deleted"

    def relabel_root(self, root, new_label) -> int:
        """Every card of `root` starts answering to `new_label`. Returns how
        many moved.

        **Why this is a verb of its own and not an `update()` per card.**
        `update()` is a person's edit — it steps the revision on every card
        and re-runs every field rule — where a rename is Dark Army observing that
        the folder on disk changed its name: one statement over every card
        of the root, in one transaction, so no reader sees half a project
        under each label.

        Bounds: an empty `root` or `new_label` is a no-op returning 0 — a
        rename to nothing is not a rename. `root` is matched **exactly**, never
        by containment: the caller resolved it, and a containment match here
        would relabel a nested project's cards from its parent.
        """
        root = str(root or "")
        label = str(new_label or "")
        if not root or not label:
            return 0
        now = time.time()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "UPDATE cards SET project = ?, updated_at = ?,"
                    " revision = revision + 1"
                    " WHERE root = ? AND project <> ?",
                    (label, now, root, label))
                moved = int(cur.rowcount or 0)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return moved

    def bind_session(self, card_id: str, session_id: str) -> tuple:
        """The card is now being worked on by this session. Writes the link
        and moves a Backlog (or Prep) card to In progress in one call,
        because a bound card sitting unstarted is a state no surface has a
        way to draw. A card already in Done stays in Done — `session_id` +
        `live` are still recorded so a later Delete can close the terminal
        (2026-08-24).

        The lock spans the read *and* the write: the column decision is made
        from `current`, and this runs on the executor while a person's drag
        lands on the loop — read outside the lock, a Done drag arriving in
        that window was silently overwritten back to In progress."""
        with self._lock:
            current = self.get(card_id)
            if current is None:
                return None, "no such card"
            fields = {
                "session_id": str(session_id or ""),
                "link_state": "live",
                "session_ended_at": None,
                "dispatch_error": "",
            }
            if str(current.get("column_name") or "") != "done":
                fields["column_name"] = "in_progress"
            # Dark Army's own move, so the change number stays put: a session
            # binding itself must never refuse the save of somebody who was
            # typing into this card at the time.
            return self.update(card_id, fields, bump=False)

    def bind_waiting_member(self, card_id: str, session_id: str,
                            batch_id: str) -> tuple:
        """`bind_session`, but only while the card is still a **waiting**
        member of `batch_id`: the mark, no session, no link, Backlog.
        `(card_or_None, detail)`.

        The re-read and the bind share one hold of the store lock (an
        `RLock`, so `bind_session` re-enters it). That closes the window
        in which a person's drag or reset could land between the
        batch-implement advance judging a member and binding it; a card
        that moved in that window is refused, never bound."""
        bid = str(batch_id or "")
        with self._lock:
            current = self.get(card_id)
            if current is None:
                return None, "no such card"
            if not (bid and str(current.get("batch_id") or "") == bid
                    and not str(current.get("session_id") or "")
                    and not str(current.get("link_state") or "")
                    and str(current.get("column_name") or "") == "backlog"):
                return None, "that card is no longer waiting in this batch"
            return self.bind_session(card_id, session_id)

    def mark_ended(self, card_id: str, when: Optional[float] = None) -> tuple:
        """The session doing this card is gone. **Not** done: a session ending
        says nothing about whether the work was finished, and Dark Army deciding
        otherwise is the one thing a board must not do."""
        return self.update(card_id, {
            "link_state": "ended",
            "session_ended_at": float(when if when is not None else time.time()),
        })

    def mark_live(self, card_id: str) -> tuple:
        return self.update(card_id, {
            "link_state": "live", "session_ended_at": None})

    def prune_done(self, older_than: float) -> int:
        """Drop Done cards finished before `older_than`. Returns how many went.

        Never touches anything outside Done: this is the one automatic deletion
        in the board and it may only ever collect cards somebody has already
        declared finished. That used to read "cards a human already finished",
        which was exact when a hand-drag was the only way into Done; a card
        closed by `declare_done` is collected here too. Still correct — a
        closed card is a finished card, and the difference is *who said so*,
        which is a question about the signature rather than about the sweep.

        One sub-state is skipped: an agent close no human has yet acknowledged
        (`closed_by` set, `reviewed_at` NULL). The banner's whole point is that
        only a person clears it, and the one automatic deletion must not be the
        thing that removes it before anybody has looked. It has no daemon
        caller today, but the contract must stay true if one arrives.
        `clear_done` is deliberately untouched: its exact-membership
        confirmation *is* the human's word."""
        with self._lock:
            gone = [self._row(row) for row in self._conn.execute(
                "SELECT * FROM cards WHERE column_name = 'done'"
                "  AND COALESCE(done_at, updated_at) < ?"
                "  AND (closed_by = '' OR reviewed_at IS NOT NULL)",
                (float(older_than),))]
            ids = [row["id"] for row in gone]
            with self._conn:
                if ids:
                    self._lifecycle_on_delete(gone)
                    self._conn.executemany(
                        "DELETE FROM card_messages WHERE card_id = ?",
                        [(i,) for i in ids])
                    self._conn.executemany(
                        "DELETE FROM card_runs WHERE card_id = ?",
                        [(i,) for i in ids])
                cur = self._conn.execute(
                    "DELETE FROM cards WHERE column_name = 'done'"
                    "  AND COALESCE(done_at, updated_at) < ?"
                    "  AND (closed_by = '' OR reviewed_at IS NOT NULL)",
                    (float(older_than),))
        return int(cur.rowcount or 0)
