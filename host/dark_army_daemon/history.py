"""Durable history — the part of the record that outlives the process.

Everything else in the daemon holds *current* state: sessions.json is overwritten
wholesale, metrics live in a dict. This module is the only thing that remembers,
and it has to, because Claude Code prunes its own transcripts (~30 days). Once a
transcript is gone the tokens it described are unrecoverable, so the database —
not the transcript — is the system of record.

Two writers share it. The live writer (the daemon, as events arrive) records what
only a running supervisor can know: when a session entered each state, how long it
sat waiting on a human, its measured cost. The backfill writer (a transcript scan,
landing later) fills in per-turn token detail, including history from before Dark Army
was ever installed.

Design notes worth keeping:

* **Days are local.** Bucketing by UTC while filtering by a local calendar puts
  late-evening work on the wrong day — a bug worth avoiding by construction rather
  than discovering, so each row stores the local day it belongs to.
* **Cost carries its provenance.** `measured` comes from Claude Code's own
  `total_cost_usd` or from Grok's stamped ticks; `estimated` is ours, from
  tokens and a Claude price list. `cost_usd`, `measured_cost_usd` and
  `estimated_cost_usd` keep exactly that meaning, and Grok and Codex never
  reach the Claude estimate. Summing measured and estimated without saying so
  would quietly turn a guess into a number.
* **Token cost is a third, separate figure.** `token_cost_usd` (on
  `session_efficiency` and, split by provider, on `other_daily`) is the counted
  tokens of Claude, Grok and Codex at each publisher's per-token price, read
  at query time and never written back. Grok and Codex are priced per turn,
  because a long-context tier is chosen per request; a turn with no rate or
  no counted tokens adds no dollar and counts as unpriced. The dollar a
  provider *reported* — Claude's statusline total, Grok's ticks — rides beside
  it as `reported_cost_usd`, never summed into it and absent, not 0, where
  nothing was reported. Codex reports no dollar.
* **Writes are synchronous and must run off the event loop.** sqlite3 blocks; the
  caller is responsible for the executor. Nothing here awaits.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .paths import HISTORY_PATH, STATE_DIR, ensure_state_dir

logger = logging.getLogger("dark-army.history")

SCHEMA_VERSION = 7

# Turn-level and sample-level rows are the bulk and the least interesting once
# they have been rolled up. State events and compactions age at the same
# horizon; sessions and digests outlive their turns only while they are inside
# it — nothing here is kept indefinitely, or the tables grow for the life of
# the machine. See `prune`.
DEFAULT_RETENTION_DAYS = 90

COST_MEASURED = "measured"
COST_ESTIMATED = "estimated"

# Schema versions whose arrival taught the transcript scanner to read a field it
# had been walking past. Upgrading *into* one of these forgets every scan
# position so the new column gets backfilled; upgrading into any other version
# (v4's derived tables, say) leaves the positions alone. See `_migrate`.
_RESCAN_ON_UPGRADE_TO = {2, 3}

# Schema versions that taught the *Claude* transcript scanner a new column
# and nothing else. v7 stores each turn's cache-write split (5-minute vs
# 1-hour), which only Claude transcripts carry, so only Claude positions are
# forgotten: a Grok `updates.jsonl` position (`…/updates.jsonl`) is kept, or
# the upgrade would re-read every Grok session for a column it never fills.
# Forgetting alone backfills nothing — `add_turn` fills the split on the
# re-read's conflict, which `INSERT OR IGNORE` by itself would not.
_CLAUDE_RESCAN_ON_UPGRADE_TO = {7}

# Schema versions that change how a *digest* is rolled up out of rows already in
# the database. Digests are keyed on the session's `last_seen` and only rebuild
# when the session moves on, so a change to the aggregation itself is invisible
# to that trigger: every existing row would keep the old shape forever. Dropping
# them forces one rebuild, which reads `turns` rather than transcripts — seconds,
# not the minutes a full rescan costs.
#
# v5: two defects in how `session_tools` rolled up, both measured on the live
# 104,000-turn database. **Subagent turns counted as the parent's own** — 41.9%
# of all tool calls — which inverts the delegation signal: the more a session
# fans out, the smaller its `Agent` share gets. **Only the five busiest tools
# were kept**, dropping 6.5% of call volume and pushing `Agent` out of the list
# entirely for 79 sessions. Together they are why the animation classifier
# returned two distinct answers across the whole corpus.
_REBUILD_DIGESTS_ON_UPGRADE_TO = {5}

# The turns the reports that predate Codex history read. `codex_history`
# writes Codex journal turns into `turns` for the History ledger's token cost
# alone; every older report (the daily table, the model mix, the hourly
# profile, the totals, the Usage view's attribution) keeps reading exactly
# the rows it read before, so none of them folds Codex into Claude and the
# Usage view does not count journals it already reads through
# `codex_spenders` a second time.
_NOT_CODEX = "COALESCE(provider, 'claude') != 'codex'"

# Stamped onto a digest that must rebuild. Below every real `last_seen`, so
# `digest_candidates`' staleness test fires, and negative so it can never be
# mistaken for a genuine mtime.
_DIGEST_REBUILD_MTIME = -1.0

# Fills the cache-write split on a turn already stored. It restates
# `idx_turns_message`'s own WHERE word for word because SQLite uses a partial
# index only when the query implies the index's condition, and `message_id = ?`
# cannot imply `!= ''` for a bound parameter. Without the restatement every call
# scanned the whole turns table: ~15 ms a turn at 150k turns, and the v7
# re-read of every transcript pinned a core for 26 minutes (28 Sep 2026). The
# trailing clause makes a re-read that finds the same split write nothing.
_FILL_CACHE_SPLIT_SQL = (
    "UPDATE turns SET cache_write_5m = ?, cache_write_1h = ?"
    " WHERE message_id = ? AND message_id IS NOT NULL AND message_id != ''"
    " AND (cache_write_5m IS NOT ? OR cache_write_1h IS NOT ?)"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One API response. The atomic grain: every token and cost rollup derives from
-- here, per turn, so a session that mixes models is never priced as one.
CREATE TABLE IF NOT EXISTS turns (
    message_id      TEXT,
    session_id      TEXT NOT NULL,
    ts              REAL NOT NULL,
    day             TEXT NOT NULL,          -- local calendar day, YYYY-MM-DD
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    cache_creation  INTEGER NOT NULL DEFAULT 0,
    tool_name       TEXT,
    is_sidechain    INTEGER NOT NULL DEFAULT 0,
    agent_id        TEXT,
    duration_ms     INTEGER,
    -- What caused this response, as Claude Code itself stamps it on the record.
    -- NULL means "not caused by one of these", which is the common case and is
    -- why every report here divides by the window's whole cost, not by the sum of
    -- its attributed rows.
    attr_skill      TEXT,
    attr_agent      TEXT,                   -- subagent type, namespace stripped
    attr_plugin     TEXT,
    attr_mcp_server TEXT,
    attr_mcp_tool   TEXT,
    -- Measured cost on this turn, when the harness stamped one (Grok ticks).
    -- NULL means "price from tokens" for Claude, or "unknown" for a Grok turn
    -- that arrived unpriced — never treat NULL as free.
    cost_usd        REAL,
    provider        TEXT,                   -- claude | grok | codex
    -- The cache write's split by TTL, when the record stated one. NULL means
    -- the record gave only the total (`cache_creation`); 0 means it said 0.
    cache_write_5m  INTEGER,
    cache_write_1h  INTEGER
);
-- Claude Code writes several records per response, all sharing message.id, and
-- only the last carries final usage. Summing them multiplies the bill; this index
-- is what makes re-scanning the same transcript idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_message
    ON turns(message_id) WHERE message_id IS NOT NULL AND message_id != '';
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_day     ON turns(day);

CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    project       TEXT,
    cwd           TEXT,
    title         TEXT,
    kind          TEXT,                     -- interactive | background
    first_seen    REAL,
    last_seen     REAL,
    ended_at      REAL,
    end_reason    TEXT,
    primary_model TEXT,
    cost_usd      REAL,
    cost_source   TEXT,                     -- measured | estimated
    provider      TEXT                      -- claude | grok
);
CREATE INDEX IF NOT EXISTS idx_sessions_last_seen ON sessions(last_seen);

-- The axis no transcript has: what the session was doing, and when it changed.
-- `waiting` spans in here are how long an agent sat blocked on a human.
CREATE TABLE IF NOT EXISTS state_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    day        TEXT NOT NULL,
    session_id TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_session ON state_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_state_day     ON state_events(day);

-- Periodic samples of the live-only metrics (statusline). Sampled, not streamed:
-- the source ticks on every assistant message and none of these move that fast.
CREATE TABLE IF NOT EXISTS metric_samples (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  REAL NOT NULL,
    day                 TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    cost_usd            REAL,
    ctx_pct             REAL,
    five_hour_pct       REAL,
    five_hour_resets_at REAL,
    seven_day_pct       REAL,
    lines_added         INTEGER,
    lines_removed       INTEGER,
    provider            TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_session ON metric_samples(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_metrics_day     ON metric_samples(day);

-- Context compaction, as Claude Code records it in the transcript itself
-- (`type: system, subtype: compact_boundary`). Kept rather than inferred: a
-- compaction is visible in `metric_samples` only as a sudden fall in ctx_pct,
-- which is a guess that a 1/min sampler can miss entirely — and the transcript
-- carries what the guess never could, namely how full the window was when it
-- gave (`pre_tokens`) and whether the user asked for it (`trigger`).
CREATE TABLE IF NOT EXISTS compactions (
    uuid       TEXT,                     -- the transcript record's own uuid
    session_id TEXT NOT NULL,
    ts         REAL NOT NULL,
    day        TEXT NOT NULL,            -- local calendar day, YYYY-MM-DD
    trigger    TEXT,                     -- auto | manual
    pre_tokens INTEGER
);
-- Same idempotence rule as `turns`: re-reading a transcript must not double the
-- count, and the record's uuid is what makes the row identifiable.
CREATE UNIQUE INDEX IF NOT EXISTS idx_compactions_uuid
    ON compactions(uuid) WHERE uuid IS NOT NULL AND uuid != '';
CREATE INDEX IF NOT EXISTS idx_compactions_session ON compactions(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_compactions_day     ON compactions(day);

-- Incremental transcript scanning: skip unchanged files, and re-read only the
-- appended tail of changed ones.
CREATE TABLE IF NOT EXISTS scan_state (
    path  TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    lines INTEGER NOT NULL
);

-- One compact, self-contained description of a session: enough for a human (or a
-- model) to say what the work *was*, with no transcript in hand. Everything here
-- is derived — `sessions` and `turns` remain the system of record — so dropping
-- the table costs a rebuild and nothing else.
--
-- It exists because the alternative is re-deriving the same thing on every read:
-- `title` and `first_prompt` are the only fields that need a transcript, and a
-- transcript read per session per page load is exactly what the history database
-- was built to stop.
CREATE TABLE IF NOT EXISTS session_digests (
    session_id     TEXT PRIMARY KEY,
    title          TEXT,
    first_prompt   TEXT,       -- clamped, synthetic prefixes skipped, secrets redacted
    project        TEXT,
    first_seen     REAL,
    last_seen      REAL,
    turns          INTEGER,
    agents         INTEGER,    -- the session itself plus its distinct subagents
    duration_ms    INTEGER,
    cost_usd       REAL,
    tools          TEXT,       -- JSON object: tool name -> call count, top few
    attr           TEXT,       -- JSON array: skill / agent / mcp attribution values
    -- The session's own `last_seen` as of the build. A session that has moved on
    -- since is stale by comparison, which is the whole refresh trigger.
    source_mtime   REAL,
    updated_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_digests_last_seen ON session_digests(last_seen);

-- A durable theme. `theme_id` is minted once and never changes, which is what
-- stops a theme from silently renaming itself on every synthesis run: names and
-- descriptions are attributes, identity is the slug.
CREATE TABLE IF NOT EXISTS themes (
    theme_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    bob_anim    TEXT NOT NULL,
    source      TEXT,          -- heuristic | llm
    created_at  REAL,
    updated_at  REAL,
    retired_at  REAL           -- NULL = active
);

-- A session belongs to exactly one theme, so the primary key is the session.
CREATE TABLE IF NOT EXISTS theme_sessions (
    session_id  TEXT PRIMARY KEY,
    theme_id    TEXT NOT NULL,
    assigned_at REAL
);
CREATE INDEX IF NOT EXISTS idx_theme_sessions_theme ON theme_sessions(theme_id);

-- Audit trail. Synthesis spends real money (or real subscription budget) on the
-- user's behalf, so every run is accountable even when it succeeded quietly.
CREATE TABLE IF NOT EXISTS theme_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at        REAL NOT NULL,
    mode          TEXT,        -- heuristic | incremental | full
    model         TEXT,
    sessions_in   INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    status        TEXT,        -- ok | error
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_theme_runs_at ON theme_runs(run_at);
"""


def local_day(ts: float) -> str:
    """The calendar day `ts` belongs to, in the machine's own timezone.

    Not UTC. Bucketing by UTC day while a human filters by their own calendar puts
    everything after ~17:00 local (in UTC+ zones) on tomorrow, which is exactly the
    kind of off-by-one that gets reported as "the numbers are wrong" months later.
    """
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# Which `metric_samples` rows are Claude's budget. Grok's overlay files its
# *weekly* percentage under `five_hour_pct`, so the column alone cannot be drawn
# as Claude's five-hour limit. Rows written before the `provider` column, or by
# an older build after a downgrade, are Claude's only when they carry a
# seven-day reading: Grok's writer never sets one (0 of 47,594 on one Mac), and
# joining `sessions.provider` cannot tell (Grok sessions with a NULL provider
# exist).
_CLAUDE_LIMIT_SAMPLE = (
    "(provider = 'claude' OR (provider IS NULL AND seven_day_pct IS NOT NULL))")


def _bucket_seconds(days: Optional[int]) -> int:
    """How finely to sample a time series for a range of `days`.

    Metrics arrive at up to one row per session per minute, and the chart they
    end up in is a few hundred pixels wide — so the series is bucketed to a few
    hundred points whatever the range. Each bucket reports its *peak* rather than
    its mean: the interesting thing about a budget is the highest it reached, and
    an average would smooth away the very spike you opened the chart to find."""
    if not days:
        return 86400          # "all": one point per day, or a year is 1500 points
    if days <= 1:
        return 300
    if days <= 7:
        return 1800
    if days <= 30:
        return 3 * 3600
    return 6 * 3600


def _median(values: list) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return (ordered[mid] if len(ordered) % 2
            else (ordered[mid - 1] + ordered[mid]) / 2)


class HistoryStore:
    """Thread-safe SQLite wrapper. Synchronous by design — see module docstring."""

    def __init__(self, path: Path = HISTORY_PATH):
        self._path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

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
        # WAL so a reader (the API answering a history query) never blocks the
        # writer; busy_timeout so the two never surface as "database is locked".
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_SCHEMA)
        self._conn = conn
        self._add_missing_columns()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.close()
            except Exception:
                # Called on shutdown, often after something else already went
                # wrong. Refusing to close is not a reason to fail the shutdown.
                logger.debug("closing history.db failed", exc_info=True)
            self._conn = None

    # Columns added to an existing table after it shipped. `_SCHEMA` cannot place
    # them: `CREATE TABLE IF NOT EXISTS` is a no-op on a database that already has
    # the table, so a v2 file would keep its v2 `turns` forever and every query
    # naming a new column would fail with "no such column" — the loudest possible
    # failure, but only at read time, long after the upgrade.
    _ADDED_COLUMNS = {
        "turns": (
            ("attr_skill", "TEXT"),
            ("attr_agent", "TEXT"),
            ("attr_plugin", "TEXT"),
            ("attr_mcp_server", "TEXT"),
            ("attr_mcp_tool", "TEXT"),
            ("cost_usd", "REAL"),
            ("provider", "TEXT"),
            # Nullable, no default: an older build's INSERT omits them and
            # still lands, and NULL is exactly "the split was not recorded".
            ("cache_write_5m", "INTEGER"),
            ("cache_write_1h", "INTEGER"),
        ),
        "sessions": (
            ("provider", "TEXT"),
        ),
        # Nullable, no default: a default of 'claude' would mark every older
        # build's Grok row as Claude's.
        "metric_samples": (
            ("provider", "TEXT"),
        ),
    }

    def _add_missing_columns(self) -> None:
        """Bring an older file's tables up to the current column set.

        Driven off `PRAGMA table_info` rather than the schema version, so it is
        idempotent and stays correct even if a database somehow carries a version
        that does not match its shape."""
        with self._lock:
            for table, columns in self._ADDED_COLUMNS.items():
                have = {row["name"] for row in
                        self._conn.execute(f"PRAGMA table_info({table})")}
                if not have:
                    continue          # table absent entirely; _SCHEMA just made it
                added = [name for name, decl in columns if name not in have]
                for name, decl in columns:
                    if name in have:
                        continue
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                if added:
                    self._conn.commit()
                    logger.info("history.db: added %s to %s",
                                ", ".join(added), table)

    def _migrate(self) -> None:
        """Record the schema version. Future migrations branch on what they find;
        `_SCHEMA` itself is idempotent, so a fresh file and an upgraded one
        converge."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            found = int(row["value"]) if row else 0
            if found > SCHEMA_VERSION:
                logger.warning(
                    "history.db was written by a newer build (schema %d > %d); "
                    "reading it anyway", found, SCHEMA_VERSION,
                )
            if 0 < found < SCHEMA_VERSION:
                # Some schema bumps teach the scanner to read something it
                # previously walked past — compactions in v2, attribution in v3 —
                # and every transcript is already recorded at its current mtime,
                # so without forgetting those positions the scanner skips them all
                # and the new column stays empty for as long as the database
                # lives. Forgetting costs one cold scan: turns dedup on
                # message_id, so re-reading a file inserts no duplicates, and
                # both statements a re-read drives — the insert and
                # `_FILL_CACHE_SPLIT_SQL` — find their turn through the index.
                # The fill did not until 28 Sep 2026, and a v7 re-read cost a
                # full table scan per stored turn.
                #
                # But only *some* bumps. v4 added derived tables (digests, themes)
                # that no transcript feeds, and a cold re-read of every transcript
                # on record to backfill nothing is minutes of I/O for no column.
                # So the rescan is opt-in per version rather than automatic, and
                # a bump that needs one has to say so.
                crossed = _RESCAN_ON_UPGRADE_TO & set(range(found + 1,
                                                            SCHEMA_VERSION + 1))
                claude_only = _CLAUDE_RESCAN_ON_UPGRADE_TO & set(
                    range(found + 1, SCHEMA_VERSION + 1))
                if crossed:
                    cleared = self._conn.execute("DELETE FROM scan_state").rowcount
                    logger.info(
                        "history.db upgraded from schema %d to %d; forgetting %d "
                        "transcript scan positions so the new columns can be "
                        "backfilled", found, SCHEMA_VERSION, cleared,
                    )
                elif claude_only:
                    cleared = self._conn.execute(
                        "DELETE FROM scan_state"
                        " WHERE path NOT LIKE '%/updates.jsonl'").rowcount
                    logger.info(
                        "history.db upgraded from schema %d to %d; forgetting %d "
                        "Claude transcript scan positions so the cache-write "
                        "split can be backfilled", found, SCHEMA_VERSION, cleared,
                    )
                else:
                    logger.info(
                        "history.db upgraded from schema %d to %d; no transcript "
                        "rescan needed", found, SCHEMA_VERSION,
                    )
                if _REBUILD_DIGESTS_ON_UPGRADE_TO & set(
                        range(found + 1, SCHEMA_VERSION + 1)):
                    # Invalidated, not deleted. `digest_candidates` only revisits
                    # sessions inside DEFAULT_RETENTION_DAYS, so a DELETE here
                    # orphans every older session permanently — and prune() keeps
                    # a digest for as long as its session is inside retention
                    # precisely because it is the only surviving description of
                    # work whose turns have already aged out.
                    # A sentinel mtime sorts below every real last_seen, so the
                    # same rows rebuild in the same order, while the stale copy
                    # stays readable until its replacement lands.
                    stale = self._conn.execute(
                        "UPDATE session_digests SET source_mtime = ?",
                        (_DIGEST_REBUILD_MTIME,)).rowcount
                    logger.info(
                        "history.db upgraded from schema %d to %d; marking %d "
                        "session digest(s) for rebuild with the corrected "
                        "roll-up", found, SCHEMA_VERSION, stale,
                    )
            # Only ever forward. `found > SCHEMA_VERSION` means an older build is
            # reading a newer file — it says so above and reads on, but stamping
            # the version *down* would re-arm every upgrade step on the next open
            # by the newer build. The .app bundle and a dev checkout share one
            # history.db, so alternating between them is a normal Tuesday.
            if found < SCHEMA_VERSION:
                if found < 6:
                    # Existing rows predate the provider column. Grok sessions
                    # the live writer already recorded carry a grok model id;
                    # everything else is Claude, including NULL (the scanner
                    # that filled this file only read ~/.claude/projects).
                    self._conn.execute(
                        "UPDATE sessions SET provider = 'grok'"
                        " WHERE COALESCE(provider, '') = ''"
                        "   AND COALESCE(primary_model, '') LIKE 'grok%'"
                    )
                    self._conn.execute(
                        "UPDATE sessions SET provider = 'claude'"
                        " WHERE COALESCE(provider, '') = ''"
                    )
                    self._conn.execute(
                        "UPDATE turns SET provider = 'claude'"
                        " WHERE COALESCE(provider, '') = ''"
                    )
                self._conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )
                self._conn.commit()

    # --- writes ---

    def record_state(self, session_id: str, from_state: Optional[str],
                     to_state: str, reason: str = "", ts: Optional[float] = None) -> None:
        """Append a state transition. Same-to-same is dropped: the interesting
        quantity is how long a state lasted, and a repeated `working` would cut
        that span into meaningless pieces."""
        if not session_id or not to_state or from_state == to_state:
            return
        ts = time.time() if ts is None else ts
        self._write(
            "INSERT INTO state_events(ts, day, session_id, from_state, to_state, reason)"
            " VALUES(?,?,?,?,?,?)",
            (ts, local_day(ts), session_id, from_state, to_state, reason or None),
        )

    def record_metrics(self, session_id: str, metrics: dict,
                       ts: Optional[float] = None,
                       provider: str = "claude") -> None:
        if not session_id or not metrics:
            return
        ts = time.time() if ts is None else ts
        self._write(
            "INSERT INTO metric_samples(ts, day, session_id, cost_usd, ctx_pct,"
            " five_hour_pct, five_hour_resets_at, seven_day_pct, lines_added,"
            " lines_removed, provider) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (ts, local_day(ts), session_id,
             metrics.get("cost_usd"), metrics.get("ctx_used_pct"),
             metrics.get("five_hour_pct"), metrics.get("five_hour_resets_at"),
             metrics.get("seven_day_pct"), metrics.get("lines_added"),
             metrics.get("lines_removed"), provider),
        )

    def upsert_session(self, session_id: str, **fields) -> None:
        """Insert or update a session row, touching only the fields given.

        COALESCE on the update side so a later call with a field absent cannot
        blank what an earlier one established — the live writer learns a session's
        title, kind and cost at different moments, in no fixed order."""
        if not session_id:
            return
        known = ("project", "cwd", "title", "kind", "first_seen", "last_seen",
                 "ended_at", "end_reason", "primary_model", "cost_usd",
                 "cost_source", "provider")
        values = {k: v for k, v in fields.items() if k in known and v is not None}
        now = time.time()
        values.setdefault("first_seen", now)
        values.setdefault("last_seen", now)

        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        # first_seen is the one field that must NOT move on update.
        updates = ", ".join(
            f"{k} = COALESCE(excluded.{k}, {k})" for k in values if k != "first_seen"
        )
        sql = (
            f"INSERT INTO sessions(session_id, {columns})"
            f" VALUES(?, {placeholders})"
            f" ON CONFLICT(session_id) DO UPDATE SET {updates}"
            if updates else
            f"INSERT INTO sessions(session_id, {columns})"
            f" VALUES(?, {placeholders})"
            f" ON CONFLICT(session_id) DO NOTHING"
        )
        self._write(sql, (session_id, *values.values()))

    def set_title(self, session_id: str, title: str) -> bool:
        """Overwrite the title without touching last_seen.

        Official names — Dark Army's generated title, a later ``ai-title`` — use
        this rather than :meth:`upsert_session`, which would also stamp
        ``last_seen = now`` and jump the row to the top of History."""
        title = (title or "").strip()
        if not session_id or not title:
            return False
        return bool(self._write(
            "UPDATE sessions SET title = ? WHERE session_id = ?",
            (title, session_id),
            want_rowcount=True,
        ))

    def fill_title(self, session_id: str, title: str) -> bool:
        """Set the title only when the row has none.

        Scan-derived names (the transcript's ``ai-title``, Dark Army's generated
        title, the opening prompt) must not overwrite a ``/rename`` or a
        statusline ``session_name`` the live writer already stored. Official
        names still go through :meth:`upsert_session`, whose COALESCE prefers
        the new value."""
        title = (title or "").strip()
        if not session_id or not title:
            return False
        return bool(self._write(
            "UPDATE sessions SET title = ? WHERE session_id = ?"
            " AND (title IS NULL OR TRIM(title) = '')",
            (title, session_id),
            want_rowcount=True,
        ))

    def untitled_session_ids(self) -> list[str]:
        """Sessions whose History row still has no name."""
        return [row["session_id"] for row in self._query(
            "SELECT session_id FROM sessions"
            " WHERE title IS NULL OR TRIM(title) = ''"
        )]

    def add_turn(self, session_id: str, ts: float, **fields) -> bool:
        """Insert one deduplicated turn. Returns False if `message_id` was already
        recorded — the normal outcome when a transcript is re-scanned.

        A re-scanned turn that now states its cache-write split fills
        `cache_write_5m` / `cache_write_1h` on the stored row, and nothing
        else: the insert is `OR IGNORE`, so without this second statement a
        forgotten scan position would re-read every transcript and store no
        split. `cost_usd`, the token counts and `provider` are never
        rewritten — a Grok tick must survive a re-scan untouched."""
        if not session_id:
            return False
        write_5m = fields.get("cache_write_5m")
        write_1h = fields.get("cache_write_1h")
        split = write_5m is not None and write_1h is not None
        row = {
            "message_id": fields.get("message_id"),
            "session_id": session_id,
            "ts": ts,
            "day": local_day(ts),
            "model": fields.get("model"),
            "input_tokens": int(fields.get("input_tokens") or 0),
            "output_tokens": int(fields.get("output_tokens") or 0),
            "cache_read": int(fields.get("cache_read") or 0),
            "cache_creation": int(fields.get("cache_creation") or 0),
            "tool_name": fields.get("tool_name"),
            "is_sidechain": 1 if fields.get("is_sidechain") else 0,
            "agent_id": fields.get("agent_id"),
            "duration_ms": fields.get("duration_ms"),
            # Empty string collapsed to NULL: the reports read "attributed" as
            # "column is not null", and a "" would be a bucket with no name.
            **{column: (fields.get(column) or None) for column in
               ("attr_skill", "attr_agent", "attr_plugin",
                "attr_mcp_server", "attr_mcp_tool")},
            "cost_usd": fields.get("cost_usd"),
            "provider": fields.get("provider") or None,
            "cache_write_5m": int(write_5m) if split else None,
            "cache_write_1h": int(write_1h) if split else None,
        }
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        added = self._write(
            f"INSERT OR IGNORE INTO turns({columns}) VALUES({placeholders})",
            tuple(row.values()),
            want_rowcount=True,
        )
        if not added and split and row["message_id"]:
            self._write(
                _FILL_CACHE_SPLIT_SQL,
                (row["cache_write_5m"], row["cache_write_1h"], row["message_id"],
                 row["cache_write_5m"], row["cache_write_1h"]),
            )
        return added

    def add_compaction(self, session_id: str, ts: float, uuid: str = "",
                       trigger: str = "", pre_tokens: Optional[int] = None) -> bool:
        """Record one context compaction. Returns False if already recorded.

        Deduplicated on the transcript record's uuid, so a re-scanned file adds
        nothing — the same contract `add_turn` has, for the same reason."""
        if not session_id:
            return False
        return self._write(
            "INSERT OR IGNORE INTO compactions(uuid, session_id, ts, day, trigger,"
            " pre_tokens) VALUES(?,?,?,?,?,?)",
            (uuid or None, session_id, ts, local_day(ts), trigger or None,
             pre_tokens),
            want_rowcount=True,
        )

    def set_estimated_cost(self, session_id: str, cost: Optional[float]) -> bool:
        """Record a computed cost, but never over a measured one.

        The backfill scanner arrives after the live writer and would otherwise
        replace Claude Code's own `total_cost_usd` with our arithmetic — trading
        the one number in this system that needs no disclaimer for one that does.
        Returns False when a measured figure was left in place."""
        if not session_id or cost is None:
            return False
        return self._write(
            "UPDATE sessions SET cost_usd = ?, cost_source = ?"
            " WHERE session_id = ? AND (cost_source IS NULL OR cost_source != ?)",
            (cost, COST_ESTIMATED, session_id, COST_MEASURED),
            want_rowcount=True,
        )

    def scan_position(self, path: str) -> tuple[float, int]:
        """(mtime, lines already read) for a transcript, or (0, 0) if unseen."""
        rows = self._query("SELECT mtime, lines FROM scan_state WHERE path = ?", (path,))
        return (rows[0]["mtime"], rows[0]["lines"]) if rows else (0.0, 0)

    def scan_positions(self) -> dict[str, tuple[float, int]]:
        """Every recorded scan position, keyed by path.

        The scanner asks about a few thousand transcripts per sweep and nearly
        all of them are unchanged, so asking one path at a time turned a sweep
        into a few thousand round trips for a table it could have read whole."""
        return {
            row["path"]: (row["mtime"], row["lines"])
            for row in self._query("SELECT path, mtime, lines FROM scan_state")
        }

    def set_scan_position(self, path: str, mtime: float, lines: int) -> None:
        self._write(
            "INSERT INTO scan_state(path, mtime, lines) VALUES(?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime,"
            " lines = excluded.lines",
            (path, mtime, lines),
        )

    def session_time_range(self, session_id: str) -> tuple[Optional[float], Optional[float]]:
        """First and last turn timestamps for a session, from `turns`."""
        rows = self._query(
            "SELECT MIN(ts) AS first, MAX(ts) AS last FROM turns WHERE session_id = ?",
            (session_id,),
        )
        return (rows[0]["first"], rows[0]["last"]) if rows else (None, None)

    def session_token_totals(self, session_id: str) -> list[dict]:
        """Per-model token sums for a session — the grain cost must be computed
        at, since a session that mixes models cannot be priced as one."""
        return [dict(r) for r in self._query(
            "SELECT model, day, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation"
            " FROM turns WHERE session_id = ? GROUP BY model, day",
            (session_id,),
        )]

    def session_cost(self, session_id: str) -> Optional[dict]:
        """`{provider, cost_usd, cost_source}` off one session's `sessions`
        row, or None — the three fields the board's observation pass reads,
        for every card with a session, on every pass. One primary-key
        lookup: `session_record` also counts turns, tokens, tools and
        attribution over `turns` (seven more queries per card per pass),
        which that pass never read."""
        if not session_id:
            return None
        rows = self._query(
            "SELECT COALESCE(provider, 'claude') AS provider,"
            " cost_usd, cost_source FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        return dict(rows[0]) if rows else None

    def session_record(self, session_id: str) -> Optional[dict]:
        """One session's durable record, or None if history has no row for it.

        Derived live from `sessions` + `turns` (and a compaction count). The
        orphaned `session_digests` table is not read: nothing writes it.
        `tokens` is None when no turn rows remain — a gap, never a quiet zero.
        Own turns are stamped `agent_id=''`, never NULL, so the subagent count
        is `COALESCE(agent_id,'') != ''` and tools use `session_tools`' default.
        """
        if not session_id:
            return None
        rows = self._query(
            "SELECT session_id, project, title, kind,"
            " COALESCE(provider, 'claude') AS provider,"
            " primary_model, first_seen, last_seen, ended_at,"
            " end_reason, cost_usd, cost_source"
            " FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        if not rows:
            return None
        rec = dict(rows[0])
        rec["turns"] = int(self._scalar(
            "SELECT COUNT(*) FROM turns WHERE session_id = ?",
            (session_id,),
        ) or 0)
        token_rows = self._query(
            "SELECT SUM(input_tokens + output_tokens + cache_read"
            " + cache_creation) AS tokens FROM turns WHERE session_id = ?",
            (session_id,),
        )
        raw_tokens = token_rows[0]["tokens"] if token_rows else None
        rec["tokens"] = int(raw_tokens) if raw_tokens is not None else None
        rec["subagents"] = int(self._scalar(
            "SELECT COUNT(DISTINCT agent_id) FROM turns"
            " WHERE session_id = ? AND COALESCE(agent_id, '') != ''",
            (session_id,),
        ) or 0)
        rec["tools"] = [
            [name, int(n)] for name, n in self.session_tools(session_id, top=8)
        ]
        rec["attribution"] = list(self.session_attribution(session_id))
        rec["compactions"] = int(self._scalar(
            "SELECT COUNT(*) FROM compactions WHERE session_id = ?",
            (session_id,),
        ) or 0)
        return rec

    def prune(self, days: int = DEFAULT_RETENTION_DAYS) -> dict:
        """Age out rows older than `days`, and the bookkeeping nothing feeds.

        Turns and metric samples were always pruned; state events and
        compactions now age at the same horizon — they were "small" only per
        row, and a table nothing ever deletes from is a database that grows
        for the life of the machine. Three more bounded sweeps:

        * ``scan_state`` rows whose transcript no longer exists on disk — the
          file was pruned by Claude Code (or the Grok session cleared), so the
          position can never be asked about again.
        * ``sessions`` and ``session_digests`` older than the retention window
          **with no remaining turns**. The narrative-keep rule survives at the
          horizon: a session whose turns are still inside retention keeps its
          row and its digest, and a row with no ``last_seen`` at all is never
          aged (it may be a session that only just registered).
        """
        removed = {"turns": 0, "metric_samples": 0, "state_events": 0,
                   "compactions": 0, "scan_state": 0, "sessions": 0,
                   "session_digests": 0, "themes_retired": 0}
        if days <= 0:
            return removed
        cutoff = time.time() - days * 86400
        with self._lock:
            if self._conn is None:
                return removed
            try:
                for table in ("turns", "metric_samples", "state_events",
                              "compactions"):
                    cur = self._conn.execute(
                        f"DELETE FROM {table} WHERE ts < ?", (cutoff,)
                    )
                    removed[table] = cur.rowcount
                # Scan positions for transcripts that are gone. Checked against
                # the filesystem rather than by age: an unchanged old transcript
                # still needs its position, or the next sweep re-reads it whole.
                stale_paths = [
                    row[0]
                    for row in self._conn.execute("SELECT path FROM scan_state")
                    if not os.path.exists(row[0])
                ]
                for path in stale_paths:
                    self._conn.execute(
                        "DELETE FROM scan_state WHERE path = ?", (path,)
                    )
                removed["scan_state"] = len(stale_paths)
                # Sessions (and their digests) whose turns have all aged out and
                # whose own last activity is past the horizon. Digests were the
                # surviving description of work whose *turns* had gone; that is
                # still true inside the window — past it, the whole record goes
                # together rather than leaving one table to grow forever.
                for table in ("sessions", "session_digests"):
                    cur = self._conn.execute(
                        f"DELETE FROM {table} WHERE last_seen IS NOT NULL"
                        " AND last_seen < ? AND session_id NOT IN"
                        " (SELECT DISTINCT session_id FROM turns)",
                        (cutoff,),
                    )
                    removed[table] = cur.rowcount
                # A theme nothing points at any more, which a merge can leave
                # behind: an empty theme would otherwise render as a card with
                # no evidence under it.
                cur = self._conn.execute(
                    "UPDATE themes SET retired_at = ? WHERE retired_at IS NULL"
                    " AND theme_id NOT IN (SELECT DISTINCT theme_id"
                    "                      FROM theme_sessions)",
                    (time.time(),),
                )
                removed["themes_retired"] = cur.rowcount
                self._conn.commit()
            except sqlite3.Error:
                # Same rule as every other path here: retention failing is a log
                # line, not an exception thrown at whatever timer called us.
                logger.warning("history prune failed", exc_info=True)
        return removed

    # --- reads (the panel's history tab lands on these) ---

    def session_count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM sessions") or 0

    def waiting_spans(self, since: Optional[float] = None) -> list[dict]:
        """How long each `waiting` stretch lasted, newest first.

        This is the metric the whole exercise is for: not what the agents spent,
        but how long they sat blocked on a human. A span still open (no following
        transition) is reported with `ended_at` None and measured to now."""
        rows = self._query(
            "SELECT session_id, ts, to_state, reason FROM state_events"
            " WHERE ts >= ? ORDER BY session_id, ts",
            (since if since is not None else 0.0,),
        )
        spans: list[dict] = []
        open_span: dict[str, dict] = {}
        for row in rows:
            sid = row["session_id"]
            pending = open_span.pop(sid, None)
            if pending is not None:
                pending["ended_at"] = row["ts"]
                pending["seconds"] = row["ts"] - pending["started_at"]
                spans.append(pending)
            if row["to_state"] == "waiting":
                open_span[sid] = {
                    "session_id": sid, "started_at": row["ts"],
                    "reason": row["reason"], "ended_at": None,
                }
        now = time.time()
        for pending in open_span.values():
            pending["seconds"] = now - pending["started_at"]
            spans.append(pending)
        spans.sort(key=lambda s: s["started_at"], reverse=True)
        return spans

    def daily_tokens(self, days: int = 30) -> list[dict]:
        cutoff = time.time() - days * 86400
        return [dict(r) for r in self._query(
            "SELECT day, model, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation, COUNT(*) AS turns"
            f" FROM turns WHERE ts >= ? AND {_NOT_CODEX}"
            " GROUP BY day, model ORDER BY day",
            (cutoff,),
        )]

    # --- reports (the panel's history tab) ---

    def _since(self, days: Optional[int]) -> float:
        return 0.0 if not days else time.time() - days * 86400

    def daily_report(self, days: int = 30) -> list[dict]:
        """Per local day: tokens, turns, and cost.

        Cost is summed from (day, model) groups rather than from a day total —
        a day that used two models cannot be priced at one rate, and the
        promotional-rate window is resolved per day. A group that already
        carries measured `cost_usd` (Grok ticks) is not re-priced."""
        from . import pricing

        rows = self._query(
            "SELECT day, model, COALESCE(provider, 'claude') AS provider,"
            " SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation, COUNT(*) AS turns,"
            " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)"
            "   AS measured_turns,"
            " SUM(cost_usd) AS measured_cost"
            f" FROM turns WHERE ts >= ? AND {_NOT_CODEX}"
            " GROUP BY day, model, provider ORDER BY day",
            (self._since(days),),
        )
        out: dict[str, dict] = {}
        for row in rows:
            day = out.setdefault(row["day"], {
                "day": row["day"], "input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_creation": 0, "turns": 0,
                "cost_usd": 0.0, "unpriced_turns": 0,
                "claude_cost_usd": 0.0, "grok_cost_usd": 0.0,
                "claude_turns": 0, "grok_turns": 0,
            })
            for field in ("input_tokens", "output_tokens", "cache_read",
                          "cache_creation", "turns"):
                day[field] += row[field] or 0
            provider = row["provider"] if row["provider"] == "grok" else "claude"
            turns = row["turns"] or 0
            day[f"{provider}_turns"] += turns
            cost, unpriced = self._cost_of_turn_group(pricing, row)
            day["unpriced_turns"] += unpriced
            if cost is not None:
                day["cost_usd"] += cost
                day[f"{provider}_cost_usd"] += cost
        return [out[day] for day in sorted(out)]

    def by_project(self, days: Optional[int] = None) -> list[dict]:
        """Sessions and measured/estimated cost per project, dearest first."""
        return [dict(r) for r in self._query(
            "SELECT COALESCE(project, '?') AS project, COUNT(*) AS sessions,"
            " MAX(COALESCE(last_seen, 0)) AS last_active,"
            " SUM(COALESCE(cost_usd, 0)) AS cost_usd,"
            " SUM(CASE WHEN cost_source = ? THEN 1 ELSE 0 END) AS measured_sessions,"
            " SUM(CASE WHEN COALESCE(provider, 'claude') = 'claude'"
            "          THEN COALESCE(cost_usd, 0) ELSE 0 END) AS claude_cost_usd,"
            " SUM(CASE WHEN provider = 'grok'"
            "          THEN COALESCE(cost_usd, 0) ELSE 0 END) AS grok_cost_usd,"
            " SUM(CASE WHEN COALESCE(provider, 'claude') = 'claude'"
            "          THEN 1 ELSE 0 END) AS claude_sessions,"
            " SUM(CASE WHEN provider = 'grok' THEN 1 ELSE 0 END) AS grok_sessions"
            " FROM sessions WHERE COALESCE(last_seen, 0) >= ?"
            " GROUP BY project ORDER BY cost_usd DESC",
            (COST_MEASURED, self._since(days)),
        )]

    def project_cwd_facts(self) -> list[dict]:
        """Sessions and last-active per (stored project, cwd), all time.

        Grouped this way so a caller can remap `project` through
        `workspace.project_label` and re-aggregate. `by_project` stays
        grouped on the stored column for the History tab.
        """
        return [dict(r) for r in self._query(
            "SELECT COALESCE(project, '?') AS project,"
            " COALESCE(cwd, '') AS cwd, COUNT(*) AS sessions,"
            " MAX(COALESCE(last_seen, 0)) AS last_active"
            " FROM sessions WHERE COALESCE(last_seen, 0) >= ?"
            " GROUP BY project, cwd"
            " ORDER BY project, cwd",
            (self._since(None),),
        )]

    def by_model(self, days: Optional[int] = None) -> list[dict]:
        """Token mix per model, and what it would have cost at list prices.

        Grouped by (model, day) and folded here rather than by model alone in
        SQL: prices move with the day — intro windows open and close — so one
        row spanning a rate change could only ever be priced at one of the two.
        Same shape `_attribution_rows` uses, for the same reason.

        The section this feeds is called "Where it goes" and had no cost column
        at all, which left it unable to answer its own heading."""
        from . import pricing

        folded: dict[str, dict] = {}
        for row in self._query(
            "SELECT COALESCE(model, '?') AS model, day, COUNT(*) AS turns,"
            " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,"
            " SUM(cache_read) AS cache_read, SUM(cache_creation) AS cache_creation,"
            " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)"
            "   AS measured_turns,"
            " SUM(cost_usd) AS measured_cost"
            f" FROM turns WHERE ts >= ? AND {_NOT_CODEX} GROUP BY model, day",
            (self._since(days),),
        ):
            entry = folded.setdefault(row["model"], {
                "model": row["model"], "turns": 0, "input_tokens": 0,
                "output_tokens": 0, "cache_read": 0, "cache_creation": 0,
                "cost_usd": 0.0, "unpriced_turns": 0,
            })
            for key in ("turns", "input_tokens", "output_tokens",
                        "cache_read", "cache_creation"):
                entry[key] += row[key] or 0
            cost, unpriced = self._cost_of_turn_group(pricing, row)
            entry["unpriced_turns"] += unpriced
            if cost is not None:
                entry["cost_usd"] += cost

        ranked = sorted(folded.values(), key=lambda e: e["output_tokens"],
                        reverse=True)
        for entry in ranked:
            if entry["unpriced_turns"] and not entry["cost_usd"]:
                # Priced nothing at all: report no cost rather than $0.00, which
                # a reader would take for "this model was free".
                entry["cost_usd"] = None
        return ranked

    # --- the agent dimension (the efficiency half of the agent report) ---

    #: How many bound parameters one `IN (…)` may carry. SQLite's own limit is
    #: higher, but a 90-day board can name more sessions than any single
    #: statement should hold, so every id-filtered query here chunks.
    SESSION_ID_CHUNK = 500
    #: The most sessions one report will filter on. Past it the report says
    #: `sessions_truncated` rather than silently sampling: a figure quietly
    #: computed over part of its own input is worse than a figure that says so.
    MAX_JOINED_SESSIONS = 5000

    @staticmethod
    def _cache_hit_ratio(cache_read: int, input_tokens: int):
        """Share of the reading that came from cache, or None with no reading.

        A genuine 0.0 — reading happened and none of it was cached — is a
        measurement and stays 0.0. An empty denominator is not a measurement
        and must never be published as one."""
        denominator = (cache_read or 0) + (input_tokens or 0)
        if denominator <= 0:
            return None
        return (cache_read or 0) / denominator

    def _chunks(self, session_ids):
        """`session_ids` in bindable batches, bounded and de-duplicated.

        Returns `(batches, truncated)`; order is the caller's, so a truncated
        set is a prefix rather than an arbitrary sample."""
        seen, ordered = set(), []
        for sid in session_ids:
            sid = str(sid or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            ordered.append(sid)
        truncated = len(ordered) > self.MAX_JOINED_SESSIONS
        ordered = ordered[:self.MAX_JOINED_SESSIONS]
        return ([ordered[i:i + self.SESSION_ID_CHUNK]
                 for i in range(0, len(ordered), self.SESSION_ID_CHUNK)],
                truncated)

    def by_agent(self, days: Optional[int] = None,
                 session_ids=None) -> list[dict]:
        """What each kind of helper cost, dearest first.

        `attr_agent` is the subagent type Claude Code itself stamps on the
        turn, so this needs no new column and no new collection — the agent
        dimension has been in `turns` all along with no reader.

        Grouped to (name, model, day) in SQL and folded here, `by_model`'s
        split and `by_model`'s reason: prices move with the day, so one row
        spanning a rate change could only be priced at one of the two.
        Dispatch counts come from a second statement grouped on the name
        alone — `COUNT(DISTINCT agent_id)` inside a (name, model, day) group
        counts one dispatch once per model *and* per day it touched.

        `session_ids`, when given, narrows the whole rollup to those sessions
        (the board's own work). It is chunked at `SESSION_ID_CHUNK` and the
        folds are unioned here, which is exactly the same answer as one
        unchunked query."""
        from . import pricing

        since = self._since(days)
        totals = ("COUNT(*) AS turns, SUM(input_tokens) AS input_tokens,"
                  " SUM(output_tokens) AS output_tokens,"
                  " SUM(cache_read) AS cache_read,"
                  " SUM(cache_creation) AS cache_creation,"
                  " SUM(COALESCE(duration_ms, 0)) AS duration_ms,"
                  " SUM(CASE WHEN COALESCE(tool_name, '') != '' THEN 1 ELSE 0 END)"
                  "   AS tool_calls,"
                  " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)"
                  "   AS measured_turns,"
                  " SUM(cost_usd) AS measured_cost")
        group_sql = (f"SELECT attr_agent AS name, model, day, {totals}"
                     " FROM turns WHERE ts >= ? AND COALESCE(attr_agent, '') != ''")
        count_sql = ("SELECT attr_agent AS name,"
                     " COUNT(DISTINCT agent_id) AS dispatches,"
                     " COUNT(DISTINCT session_id) AS sessions"
                     " FROM turns WHERE ts >= ? AND COALESCE(attr_agent, '') != ''")
        rows, counts = [], []
        if session_ids is None:
            rows = self._query(group_sql + " GROUP BY attr_agent, model, day",
                               (since,))
            counts = self._query(count_sql + " GROUP BY attr_agent", (since,))
        else:
            batches, _truncated = self._chunks(session_ids)
            for batch in batches:
                marks = ",".join("?" * len(batch))
                rows += self._query(
                    group_sql + f" AND session_id IN ({marks})"
                    " GROUP BY attr_agent, model, day",
                    (since, *batch))
                counts += self._query(
                    count_sql + f" AND session_id IN ({marks})"
                    " GROUP BY attr_agent",
                    (since, *batch))

        folded: dict[str, dict] = {}
        for row in rows:
            entry = folded.setdefault(row["name"], {
                "name": row["name"], "turns": 0, "input_tokens": 0,
                "output_tokens": 0, "cache_read": 0, "cache_creation": 0,
                "duration_ms": 0, "tool_calls": 0, "dispatches": 0,
                "sessions": 0, "cost_usd": 0.0, "unpriced_turns": 0,
                # Which models this kind of helper ran on. Published as a list
                # rather than a second table: the desk's model filter asks
                # "which helpers touched this model", which is membership.
                "_model_turns": {},
            })
            for key in ("turns", "input_tokens", "output_tokens", "cache_read",
                        "cache_creation", "duration_ms", "tool_calls"):
                entry[key] += row[key] or 0
            model = row["model"] or "?"
            entry["_model_turns"][model] = (
                entry["_model_turns"].get(model, 0) + (row["turns"] or 0))
            cost, unpriced = self._cost_of_turn_group(pricing, row)
            entry["unpriced_turns"] += unpriced
            if cost is not None:
                entry["cost_usd"] += cost
        for row in counts:
            entry = folded.get(row["name"])
            if entry is None:
                continue
            # Summed across chunks: a session belongs to exactly one batch, so
            # its dispatches are counted in exactly one of them.
            entry["dispatches"] += row["dispatches"] or 0
            entry["sessions"] += row["sessions"] or 0

        ranked = sorted(folded.values(),
                        key=lambda e: (e["cost_usd"], e["turns"]), reverse=True)
        for entry in ranked:
            entry["cache_hit_ratio"] = self._cache_hit_ratio(
                entry["cache_read"], entry["input_tokens"])
            entry["models"] = [name for name, _turns in sorted(
                entry.pop("_model_turns").items(),
                key=lambda pair: pair[1], reverse=True)]
            if entry["unpriced_turns"] and not entry["cost_usd"]:
                # `by_model`'s rule: priced nothing at all reports no cost
                # rather than $0.00, which a reader would take for "free".
                entry["cost_usd"] = None
        return ranked

    def top_dispatches(self, days: Optional[int] = None,
                       limit: int = 20) -> list[dict]:
        """The heaviest individual dispatches, dearest first.

        One row per `agent_id` — a single spawned helper — which the columns
        have always supported and nothing has ever read. Ranked in SQL by
        token volume first so the scan is bounded, then priced and re-ranked
        here: pricing is Python's, grouping is SQL's."""
        from . import pricing

        since = self._since(days)
        limit = max(1, min(int(limit or 0) or 20, 100))
        heaviest = self._query(
            "SELECT agent_id FROM turns"
            " WHERE ts >= ? AND COALESCE(agent_id, '') != ''"
            " GROUP BY agent_id"
            " ORDER BY SUM(input_tokens + output_tokens + cache_read"
            "            + cache_creation) DESC LIMIT ?",
            (since, limit),
        )
        ids = [row["agent_id"] for row in heaviest]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._query(
            "SELECT agent_id, COALESCE(attr_agent, '') AS name, session_id,"
            " model, day, COUNT(*) AS turns, MIN(ts) AS first_ts,"
            " MAX(ts) AS last_ts, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens,"
            " SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation,"
            " SUM(COALESCE(duration_ms, 0)) AS duration_ms,"
            " SUM(CASE WHEN COALESCE(tool_name, '') != '' THEN 1 ELSE 0 END)"
            "   AS tool_calls,"
            " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)"
            "   AS measured_turns,"
            " SUM(cost_usd) AS measured_cost"
            f" FROM turns WHERE ts >= ? AND agent_id IN ({marks})"
            " GROUP BY agent_id, model, day",
            (since, *ids),
        )
        folded: dict[str, dict] = {}
        for row in rows:
            entry = folded.setdefault(row["agent_id"], {
                "agent_id": row["agent_id"], "name": row["name"],
                "session_id": row["session_id"], "turns": 0,
                "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
                "cache_creation": 0, "duration_ms": 0, "tool_calls": 0,
                "cost_usd": 0.0, "unpriced_turns": 0, "model": "",
                "first_ts": row["first_ts"], "last_ts": row["last_ts"],
                "_model_turns": {},
            })
            if not entry["name"]:
                entry["name"] = row["name"]
            model = row["model"] or "?"
            entry["_model_turns"][model] = (
                entry["_model_turns"].get(model, 0) + (row["turns"] or 0))
            for key in ("turns", "input_tokens", "output_tokens", "cache_read",
                        "cache_creation", "duration_ms", "tool_calls"):
                entry[key] += row[key] or 0
            entry["first_ts"] = min(entry["first_ts"], row["first_ts"])
            entry["last_ts"] = max(entry["last_ts"], row["last_ts"])
            cost, unpriced = self._cost_of_turn_group(pricing, row)
            entry["unpriced_turns"] += unpriced
            if cost is not None:
                entry["cost_usd"] += cost
        ranked = sorted(folded.values(),
                        key=lambda e: (e["cost_usd"], e["turns"]), reverse=True)
        for entry in ranked:
            entry["cache_hit_ratio"] = self._cache_hit_ratio(
                entry["cache_read"], entry["input_tokens"])
            models = entry.pop("_model_turns")
            # The model this dispatch mostly ran on. One name, because the
            # row is one dispatch and a list there would read as a mix.
            entry["model"] = max(models, key=models.get) if models else ""
            if entry["unpriced_turns"] and not entry["cost_usd"]:
                entry["cost_usd"] = None
        return ranked[:limit]

    def session_efficiency(self, session_ids,
                           days: Optional[int] = None) -> dict:
        """`{session_id: {turns, tokens, duration_ms, cost_usd, …}}`.

        The per-session half of the board join: what each session that worked
        a card cost, at the (model, day) grain pricing needs. A session with
        no surviving turn rows is simply absent from the answer — the caller
        reports that as partial, never as a zero.

        `days` bounds it exactly as `by_agent`'s does, and for a reason the
        report cannot do without: a session bound to a card inside a 7-day
        window may have started weeks earlier, and folding its whole lifetime
        into a row under a stated `from`/`to` is a figure that does not
        describe the period it is printed under. Absent means no bound.

        Chunked like `by_agent`, for the same reason and with the same bound.
        """
        from . import pricing

        since = self._since(days)
        batches, _truncated = self._chunks(session_ids)
        folded: dict[str, dict] = {}
        for batch in batches:
            marks = ",".join("?" * len(batch))
            for row in self._query(
                "SELECT session_id, model, day, COUNT(*) AS turns,"
                " SUM(input_tokens) AS input_tokens,"
                " SUM(output_tokens) AS output_tokens,"
                " SUM(cache_read) AS cache_read,"
                " SUM(cache_creation) AS cache_creation,"
                " SUM(COALESCE(duration_ms, 0)) AS duration_ms,"
                " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)"
                "   AS measured_turns,"
                " SUM(cost_usd) AS measured_cost"
                " FROM turns WHERE ts >= ?"
                f" AND session_id IN ({marks})"
                " GROUP BY session_id, model, day",
                (since, *batch),
            ):
                entry = folded.setdefault(row["session_id"], {
                    "session_id": row["session_id"], "turns": 0,
                    "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
                    "cache_creation": 0, "duration_ms": 0, "cost_usd": 0.0,
                    "unpriced_turns": 0,
                    # The model this session mostly ran on. One name, the
                    # same rule as `top_dispatches`. Absent until the fold
                    # below pops the counter.
                    "_model_turns": {},
                })
                for key in ("turns", "input_tokens", "output_tokens",
                            "cache_read", "cache_creation", "duration_ms"):
                    entry[key] += row[key] or 0
                model = row["model"] or "?"
                entry["_model_turns"][model] = (
                    entry["_model_turns"].get(model, 0) + (row["turns"] or 0))
                cost, unpriced = self._cost_of_turn_group(pricing, row)
                entry["unpriced_turns"] += unpriced
                # `cost_usd` stays the old mixed figure: measured when the
                # group stored one, otherwise the price-list estimate. The
                # two new fields keep those apart and are omitted, never
                # stored as 0, when that kind of price did not happen.
                if cost is not None:
                    entry["cost_usd"] += cost
                    measured_n = row["measured_turns"] or 0
                    if measured_n == 0:
                        entry["estimated_cost_usd"] = (
                            entry.get("estimated_cost_usd", 0.0) + cost)
                    else:
                        entry["measured_cost_usd"] = (
                            entry.get("measured_cost_usd", 0.0) + cost)
        for entry in folded.values():
            entry["cache_hit_ratio"] = self._cache_hit_ratio(
                entry["cache_read"], entry["input_tokens"])
            models = entry.pop("_model_turns")
            entry["model"] = max(models, key=models.get) if models else ""
            if entry["unpriced_turns"] and not entry["cost_usd"]:
                entry["cost_usd"] = None
        self._add_token_figures(folded, batches, since)
        return folded

    def _add_token_figures(self, folded: dict, batches: list,
                           since: float) -> None:
        """Put the token cost and the reported dollar on each session rollup.

        Beside `cost_usd` / `measured_cost_usd` / `estimated_cost_usd`, never
        in them. `token_cost_usd` is present only where some counted token met
        a published rate, and `<provider>_token_cost_usd` splits it by the
        provider of the turns priced (each present only where that provider
        priced something); `token_unpriced_turns` counts the turns that did not.
        `reported_cost_usd` is Claude's statusline total for the whole session
        (`sessions.cost_usd` where `cost_source` is measured — one number, not
        split by day or window) or the sum of Grok's stamped ticks inside the
        window; absent where nothing was reported, and a stored 0 is present.
        Codex reports no dollar. `cache_split_unknown_turns` counts Claude
        turns whose cache write had no 5-minute/1-hour split and was priced at
        the 5-minute rate."""
        from . import pricing

        for batch in batches:
            marks = ",".join("?" * len(batch))
            for part in self._token_parts(pricing, since, marks, tuple(batch)):
                entry = folded.get(part["session_id"])
                if entry is None:
                    continue
                if part["cost"] is not None:
                    entry["token_cost_usd"] = (
                        entry.get("token_cost_usd", 0.0) + part["cost"])
                    # Split by the provider of the turns actually priced,
                    # never by the label a run was filed under: a Codex
                    # card's run can be a Claude session.
                    key = f"{part['provider']}_token_cost_usd"
                    entry[key] = entry.get(key, 0.0) + part["cost"]
                entry["token_unpriced_turns"] = (
                    entry.get("token_unpriced_turns", 0) + part["unpriced"])
                entry["cache_split_unknown_turns"] = (
                    entry.get("cache_split_unknown_turns", 0)
                    + part["split_unknown"])
                if part["reported"] is not None:
                    entry["reported_cost_usd"] = (
                        entry.get("reported_cost_usd", 0.0) + part["reported"])
            for sid, cost in self._claude_reported(marks, tuple(batch)).items():
                entry = folded.get(sid)
                if entry is not None:
                    entry["reported_cost_usd"] = cost
        for entry in folded.values():
            entry.setdefault("token_unpriced_turns", 0)
            entry.setdefault("cache_split_unknown_turns", 0)

    def _claude_reported(self, marks: str, batch: tuple) -> dict:
        """`{session_id: cost}` for the Claude sessions whose statusline total
        was recorded — `cost_source` measured, a stored 0 included."""
        return {
            row["session_id"]: float(row["cost_usd"])
            for row in self._query(
                "SELECT session_id, cost_usd FROM sessions"
                f" WHERE session_id IN ({marks}) AND cost_source = ?"
                "   AND cost_usd IS NOT NULL"
                "   AND COALESCE(provider, 'claude') = 'claude'",
                (*batch, COST_MEASURED),
            )
        }

    def _token_parts(self, pricing, since: float, marks: str, batch: tuple):
        """The token-cost pieces for some sessions: one dict per Claude
        (session, day, model, split-known) group and one per Grok or Codex turn.

        Each piece is `{session_id, day, provider, cost, unpriced,
        split_unknown, reported}`. Claude is grouped because its price does
        not move with prompt length (a `[1m]` id is its own row) — only by day
        (a rate's day) and by whether the split was recorded. Grok and Codex
        are priced turn by turn: a long-context tier is chosen per request,
        and a day's sum would pick one tier for short and long prompts alike.

        This never goes through `_cost_of_turn_group`: a stamped Grok tick is
        not a token price and does not stand in for a missing rate, and a
        token price never replaces a tick. The tick is `reported`."""
        for row in self._query(
            "SELECT session_id, day, model, COUNT(*) AS turns,"
            " SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens,"
            " SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation,"
            " SUM(cache_write_5m) AS cache_write_5m,"
            " SUM(cache_write_1h) AS cache_write_1h,"
            " SUM(CASE WHEN cache_creation > 0 THEN 1 ELSE 0 END)"
            "   AS writing_turns,"
            " (cache_write_5m IS NOT NULL AND cache_write_1h IS NOT NULL)"
            "   AS split"
            " FROM turns WHERE ts >= ?"
            " AND COALESCE(provider, 'claude') = 'claude'"
            f" AND session_id IN ({marks})"
            " GROUP BY session_id, day, model, split",
            (since, *batch),
        ):
            counted = ((row["input_tokens"] or 0) + (row["output_tokens"] or 0)
                       + (row["cache_read"] or 0) + (row["cache_creation"] or 0))
            cost = self._price_row(pricing, row) if counted > 0 else None
            yield {
                "session_id": row["session_id"], "day": row["day"],
                "provider": "claude", "cost": cost,
                "unpriced": 0 if cost is not None else (row["turns"] or 0),
                "split_unknown": 0 if row["split"] else (row["writing_turns"] or 0),
                "reported": None,
            }
        for row in self._query(
            "SELECT session_id, day, model, provider, input_tokens,"
            " output_tokens, cache_read, cache_creation, cost_usd"
            " FROM turns WHERE ts >= ? AND provider IN ('grok', 'codex')"
            f" AND session_id IN ({marks})",
            (since, *batch),
        ):
            provider = row["provider"]
            cost = self._token_turn_cost(pricing, row)
            yield {
                "session_id": row["session_id"], "day": row["day"],
                "provider": provider, "cost": cost,
                "unpriced": 0 if cost is not None else 1,
                "split_unknown": 0,
                "reported": (float(row["cost_usd"])
                             if provider == "grok" and row["cost_usd"] is not None
                             else None),
            }

    @staticmethod
    def _token_turn_cost(pricing, row) -> Optional[float]:
        """One Grok or Codex turn at its publisher's per-token price, or None.

        Both count the cache read (and Codex its cache write) *inside*
        `input_tokens`, so the uncached input is the difference; a cached part
        larger than the input it belongs to is unpriced, not a negative bill.

        **A Grok turn is not one request.** Grok's usage record sums every
        model call of a user turn — `input_tokens` on this Mac routinely runs
        to millions, far past the 500k prompt a single call may carry — so
        the long tier, which xAI applies per request whose prompt reaches
        200,000, cannot be chosen from the turn. A turn under the cutoff is
        exact (every call in it was under too); a turn at or over it is
        unpriced rather than billed at a tier nobody can see. A Codex
        `token_count` is one request's usage, so its tier is exact."""
        model = row["model"]
        provider = row["provider"]
        input_tokens = row["input_tokens"] or 0
        output_tokens = row["output_tokens"] or 0
        cache_read = row["cache_read"] or 0
        cache_write = row["cache_creation"] or 0
        if input_tokens + output_tokens + cache_read + cache_write <= 0:
            return None
        rate = pricing.resolve_rate(model, provider)
        if rate is None:
            return None
        if (provider == "grok" and rate.long_from is not None
                and input_tokens >= rate.long_from):
            return None
        return pricing.cost_usd(
            model, input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read=cache_read, cache_write_5m=cache_write,
            day=row["day"], cached_subset=True, provider=provider)

    def other_daily(self, days: Optional[int] = None, exclude=()) -> list[dict]:
        """Per local day, the turns whose session is not a named card.

        Published as `other_days` beside `daily`. Nothing here writes into
        `daily`.

        The ledger's "other sessions" line. Not a subtraction from
        `daily_report`: that total mixes a price-list estimate into
        `cost_usd` and folds every non-Grok provider into Claude. This
        query prices with the same `_cost_of_turn_group` rules and then
        keeps Claude and Grok apart. A provider that is neither is not
        folded into Claude: a Codex turn adds to no measured, estimated or
        unpriced key here, only to the token side below.

        `exclude` is the named cards' session ids. Empty means every turn
        in the window is other. The exclude set is applied in Python, so
        it is not truncated. The sessions that remain are read in batches
        of `SESSION_ID_CHUNK` (500), every batch, with no
        `MAX_JOINED_SESSIONS` cap: a partial sum served as the whole other
        line is a lie. One unbounded `IN` is how this fails on a long
        `all` range.

        Estimates and the unpriced count are split by provider the same
        way measured cost is. `estimated_cost_usd` and `unpriced_sessions`
        are those two splits added, never a Codex turn folded into Claude.
        `unpriced_sessions` counts sessions, not turns. A measured or
        estimated dollar is never stored as 0 to mean unknown.

        The token side rides beside those keys and never in them:
        `claude_token_cost_usd`, `grok_token_cost_usd` and
        `codex_token_cost_usd` (Codex turns are in this table now, and only
        ever on their own key), with `<provider>_token_unpriced_sessions` for
        the sessions some of whose turns that day had no rate or no counted
        tokens, and `<provider>_token_unpriced_session_ids` naming them, so a
        reader over several days counts each session once. The reported dollar is `claude_reported_cost_usd` (a leftover
        session's statusline total, whole, on the latest day it has a turn in
        the window) and `grok_reported_cost_usd` (that day's ticks); each is
        present only where something was reported, and a stored 0 is present.
        `cache_split_unknown_turns` counts Claude writes priced at the
        5-minute rate for want of a recorded split.
        """
        from . import pricing

        since = self._since(days)
        excluded = {str(sid) for sid in (exclude or []) if str(sid or "")}
        present = [row["session_id"] for row in self._query(
            "SELECT DISTINCT session_id AS session_id FROM turns"
            " WHERE ts >= ?",
            (since,),
        )]
        leftover = [sid for sid in present if sid not in excluded]
        if not leftover:
            return []
        # Every leftover session, in batches of 500. Not `_chunks`: that
        # helper stops at `MAX_JOINED_SESSIONS` and would publish a prefix
        # as the whole other line. DISTINCT has no order, so the prefix
        # would also be arbitrary.
        seen, ordered = set(), []
        for sid in leftover:
            sid = str(sid or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            ordered.append(sid)
        size = self.SESSION_ID_CHUNK if self.SESSION_ID_CHUNK > 0 else 500
        batches = [ordered[i:i + size] for i in range(0, len(ordered), size)]
        grouped = (
            "SELECT day, session_id, model,"
            " COALESCE(provider, '') AS provider, COUNT(*) AS turns,"
            " SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens,"
            " SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation,"
            " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)"
            "   AS measured_turns,"
            " SUM(cost_usd) AS measured_cost"
            " FROM turns WHERE ts >= ? AND session_id IN ({marks})"
            " GROUP BY day, session_id, model, provider"
        )
        rows = []
        token_parts = []
        claude_reported: dict = {}
        for batch in batches:
            marks = ",".join("?" * len(batch))
            rows += self._query(
                grouped.format(marks=marks),
                (since, *batch),
            )
            token_parts += list(self._token_parts(
                pricing, since, marks, tuple(batch)))
            claude_reported.update(self._claude_reported(marks, tuple(batch)))
        folded: dict[str, dict] = {}
        for row in rows:
            day = folded.get(row["day"])
            if day is None:
                day = folded[row["day"]] = self._other_day(row["day"])
            provider = row["provider"] or ""
            out_tokens = row["output_tokens"] or 0
            if provider == "claude":
                day["claude_output_tokens"] += out_tokens
            elif provider == "grok":
                day["grok_output_tokens"] += out_tokens
            cost, _unpriced_turns = self._cost_of_turn_group(pricing, row)
            measured_n = row["measured_turns"] or 0
            sid = str(row["session_id"] or "")
            if cost is None:
                if sid and provider == "claude":
                    day["_unpriced_claude"].add(sid)
                elif sid and provider == "grok":
                    day["_unpriced_grok"].add(sid)
            elif measured_n == 0 and provider == "grok":
                day["grok_estimated_cost_usd"] += cost
            elif measured_n == 0 and provider == "claude":
                day["claude_estimated_cost_usd"] += cost
            elif provider == "grok":
                day["grok_measured_cost_usd"] += cost
            elif provider == "claude":
                day["claude_measured_cost_usd"] += cost
        latest_claude_day: dict = {}
        for part in token_parts:
            day = folded.get(part["day"])
            if day is None:
                day = folded[part["day"]] = self._other_day(part["day"])
            provider = part["provider"]
            if part["cost"] is not None:
                day[f"{provider}_token_cost_usd"] += part["cost"]
            if part["unpriced"]:
                day[f"_token_unpriced_{provider}"].add(part["session_id"])
            day["cache_split_unknown_turns"] += part["split_unknown"]
            if part["reported"] is not None:
                day["grok_reported_cost_usd"] = (
                    day.get("grok_reported_cost_usd", 0.0) + part["reported"])
            if provider == "claude":
                sid = part["session_id"]
                if part["day"] > latest_claude_day.get(sid, ""):
                    latest_claude_day[sid] = part["day"]
        for sid, cost in claude_reported.items():
            key = latest_claude_day.get(sid)
            if key is None:
                continue
            day = folded[key]
            day["claude_reported_cost_usd"] = (
                day.get("claude_reported_cost_usd", 0.0) + cost)
        out = []
        for day in sorted(folded):
            row = folded[day]
            for provider in ("claude", "grok", "codex"):
                unpriced_ids = row.pop(f"_token_unpriced_{provider}")
                row[f"{provider}_token_unpriced_sessions"] = len(unpriced_ids)
                # The ids too, so a reader over several days counts a
                # session once, not once per day it had a turn.
                row[f"{provider}_token_unpriced_session_ids"] = sorted(unpriced_ids)
            claude_unpriced = row.pop("_unpriced_claude")
            grok_unpriced = row.pop("_unpriced_grok")
            row["claude_unpriced_sessions"] = len(claude_unpriced)
            row["grok_unpriced_sessions"] = len(grok_unpriced)
            row["unpriced_sessions"] = len(claude_unpriced | grok_unpriced)
            row["estimated_cost_usd"] = (
                row["claude_estimated_cost_usd"] + row["grok_estimated_cost_usd"])
            out.append(row)
        return out

    @staticmethod
    def _other_day(day: str) -> dict:
        """One `other_daily` day before any turn lands on it. The leading
        underscore keys are working sets, popped before the row is served."""
        return {
            "day": day,
            "claude_measured_cost_usd": 0.0,
            "grok_measured_cost_usd": 0.0,
            "claude_estimated_cost_usd": 0.0,
            "grok_estimated_cost_usd": 0.0,
            "estimated_cost_usd": 0.0,
            "unpriced_sessions": 0,
            "claude_unpriced_sessions": 0,
            "grok_unpriced_sessions": 0,
            "claude_output_tokens": 0,
            "grok_output_tokens": 0,
            "_unpriced_claude": set(),
            "_unpriced_grok": set(),
            "claude_token_cost_usd": 0.0,
            "grok_token_cost_usd": 0.0,
            "codex_token_cost_usd": 0.0,
            "cache_split_unknown_turns": 0,
            "_token_unpriced_claude": set(),
            "_token_unpriced_grok": set(),
            "_token_unpriced_codex": set(),
        }

    def waiting_summary(self, days: Optional[int] = None) -> dict:
        """How long agents sat blocked on a human — the metric this layer is for.

        Reported with a median as well as a total: one forgotten overnight block
        would otherwise dominate a mean and hide the everyday pattern."""
        since = self._since(days)
        spans = self.waiting_spans(since=since)
        seconds = sorted(s["seconds"] for s in spans)
        open_now = [s for s in spans if s["ended_at"] is None]
        projects = self._project_map(since)
        return {
            "spans": len(spans),
            "total_seconds": sum(seconds),
            "median_seconds": _median(seconds),
            "longest_seconds": seconds[-1] if seconds else 0.0,
            "open_now": len(open_now),
            "by_reason": self._reason_counts(spans),
            "by_day": self._waiting_by_day(spans),
            "by_project": self._waiting_by_project(spans, projects),
            # The tail, not a dump: the aggregate above says how bad it is on
            # average, and this says which individual blocks were worth noticing.
            "longest": [
                {**span, "project": projects.get(span["session_id"], "?")}
                for span in sorted(spans, key=lambda s: s["seconds"], reverse=True)[:10]
            ],
        }

    def _project_map(self, since: float) -> dict:
        """session_id → project, for sessions touched inside the range.

        Bounded by `since` rather than read whole: `last_seen` only moves forward,
        so a session with a span inside the range cannot have a last_seen before
        it."""
        return {row["session_id"]: row["project"] or "?" for row in self._query(
            "SELECT session_id, project FROM sessions"
            " WHERE COALESCE(last_seen, 0) >= ?",
            (since,),
        )}

    @staticmethod
    def _waiting_by_day(spans: list) -> list[dict]:
        """Waiting, per local day. A span is credited to the day it *started* —
        splitting one across midnight would be more precise about the clock and
        less true about the event, which is a single block that began when it
        began."""
        days: dict[str, list] = {}
        for span in spans:
            days.setdefault(local_day(span["started_at"]), []).append(span["seconds"])
        return [{"day": day, "spans": len(values), "total_seconds": sum(values),
                 "median_seconds": _median(values), "longest_seconds": max(values)}
                for day, values in sorted(days.items())]

    @staticmethod
    def _waiting_by_project(spans: list, projects: dict) -> list[dict]:
        """Waiting, per project, worst total first — which repo is where the
        throughput actually goes."""
        grouped: dict[str, list] = {}
        for span in spans:
            grouped.setdefault(
                projects.get(span["session_id"], "?"), []
            ).append(span["seconds"])
        rows = [{"project": project, "spans": len(values),
                 "total_seconds": sum(values), "median_seconds": _median(values)}
                for project, values in grouped.items()]
        rows.sort(key=lambda r: r["total_seconds"], reverse=True)
        return rows

    @staticmethod
    def _reason_counts(spans: list) -> dict:
        counts: dict[str, int] = {}
        for span in spans:
            counts[span.get("reason") or "unknown"] = (
                counts.get(span.get("reason") or "unknown", 0) + 1
            )
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    def totals(self, days: Optional[int] = None) -> dict:
        since = self._since(days)
        turn_row = self._query(
            "SELECT COUNT(*) AS turns, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation,"
            " SUM(CASE WHEN COALESCE(provider, 'claude') = 'claude'"
            "          THEN 1 ELSE 0 END) AS claude_turns,"
            " SUM(CASE WHEN provider = 'grok' THEN 1 ELSE 0 END) AS grok_turns,"
            " SUM(CASE WHEN COALESCE(provider, 'claude') = 'claude'"
            "          THEN output_tokens ELSE 0 END) AS claude_output_tokens,"
            " SUM(CASE WHEN provider = 'grok'"
            "          THEN output_tokens ELSE 0 END) AS grok_output_tokens"
            f" FROM turns WHERE ts >= ? AND {_NOT_CODEX}",
            (since,),
        )
        # Cost is split by provenance rather than summed into one figure. The two
        # are not the same kind of number: `measured` is what Claude Code says the
        # work cost (or what Grok stamped in ticks), `estimated` is what it would
        # have cost at API list prices — which, on a subscription, is a
        # comparison and not a bill. Adding them would produce a total that is
        # true of nothing. Provider splits let the panel filter without a
        # second scan of this database.
        session_row = self._query(
            "SELECT COUNT(*) AS sessions, SUM(COALESCE(cost_usd, 0)) AS cost_usd,"
            " SUM(CASE WHEN cost_source = ? THEN COALESCE(cost_usd, 0) ELSE 0 END)"
            "   AS measured_cost_usd,"
            " SUM(CASE WHEN cost_source = ? THEN 0 ELSE COALESCE(cost_usd, 0) END)"
            "   AS estimated_cost_usd,"
            " SUM(CASE WHEN cost_source = ? THEN 1 ELSE 0 END) AS measured_sessions,"
            " SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced_sessions,"
            " SUM(CASE WHEN COALESCE(provider, 'claude') = 'claude'"
            "          THEN COALESCE(cost_usd, 0) ELSE 0 END) AS claude_cost_usd,"
            " SUM(CASE WHEN provider = 'grok'"
            "          THEN COALESCE(cost_usd, 0) ELSE 0 END) AS grok_cost_usd,"
            " SUM(CASE WHEN COALESCE(provider, 'claude') = 'claude'"
            "          THEN 1 ELSE 0 END) AS claude_sessions,"
            " SUM(CASE WHEN provider = 'grok' THEN 1 ELSE 0 END) AS grok_sessions,"
            " SUM(CASE WHEN COALESCE(provider, 'claude') = 'claude'"
            "           AND cost_source = ? THEN 1 ELSE 0 END)"
            "   AS claude_measured_sessions,"
            " SUM(CASE WHEN provider = 'grok' AND cost_source = ?"
            "          THEN 1 ELSE 0 END) AS grok_measured_sessions,"
            " SUM(CASE WHEN COALESCE(provider, 'claude') = 'claude'"
            "           AND cost_usd IS NULL THEN 1 ELSE 0 END)"
            "   AS claude_unpriced_sessions,"
            " SUM(CASE WHEN provider = 'grok' AND cost_usd IS NULL"
            "          THEN 1 ELSE 0 END) AS grok_unpriced_sessions"
            " FROM sessions WHERE COALESCE(last_seen, 0) >= ?",
            (COST_MEASURED, COST_MEASURED, COST_MEASURED,
             COST_MEASURED, COST_MEASURED, since),
        )
        out = dict(turn_row[0]) if turn_row else {}
        out.update(dict(session_row[0]) if session_row else {})
        return {k: (v or 0) for k, v in out.items()}

    def limits_report(self, days: Optional[int] = None) -> dict:
        """Claude's rate-limit budget over time — the axis claude-usage hands to
        another project entirely.

        Claude's alone. Grok's overlay files its *weekly* percentage under
        `five_hour_pct` (`grok_roster._overlay_billing`), so an unfiltered read
        draws Grok's week as Claude's five-hour budget. Every query here goes
        through `_CLAUDE_LIMIT_SAMPLE`; `sessions.provider` cannot tell the two
        apart (Grok sessions with a NULL provider exist).

        The `resets` list matters as much as the series: a five-hour budget is a
        sawtooth, and without the reset boundaries drawn in, a fall from 90% to 4%
        reads as a broken metric rather than as a window rolling over.

        `current` is the newest sample in the *database*, not in the range, and
        carries its own `ts` — a caller showing it as "now" has to check how old
        it is, because a daemon that was down all morning has nothing newer.

        `from` / `to` are the chart's x-axis: the range asked for (`since` to now),
        (floored to a bucket, as the series' own `ts` are), or for `days=None` the first bucket's `ts`, None when the series is empty."""
        since = self._since(days)
        bucket = _bucket_seconds(days)
        series = [dict(r) for r in self._query(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS ts,"
            " MAX(five_hour_pct) AS five_hour_pct,"
            " MAX(seven_day_pct) AS seven_day_pct"
            " FROM metric_samples WHERE ts >= ?"
            "   AND (five_hour_pct IS NOT NULL OR seven_day_pct IS NOT NULL)"
            f"   AND {_CLAUDE_LIMIT_SAMPLE}"
            " GROUP BY 1 ORDER BY 1",
            (bucket, bucket, since),
        )]
        resets = [r["resets_at"] for r in self._query(
            "SELECT DISTINCT five_hour_resets_at AS resets_at FROM metric_samples"
            " WHERE ts >= ? AND five_hour_resets_at IS NOT NULL"
            f"   AND {_CLAUDE_LIMIT_SAMPLE} ORDER BY 1",
            (since,),
        )]
        latest = self._query(
            "SELECT ts, five_hour_pct, five_hour_resets_at, seven_day_pct"
            " FROM metric_samples WHERE five_hour_pct IS NOT NULL"
            f"   AND {_CLAUDE_LIMIT_SAMPLE}"
            " ORDER BY ts DESC LIMIT 1"
        )
        current = dict(latest[0]) if latest else {}
        now = time.time()
        if days is not None:
            # Series buckets are labelled by their floored start, so `from` is
            # floored the same way or the first bucket falls before the axis.
            start = int(since // bucket) * bucket
        else:
            start = series[0]["ts"] if series else None
        return {"bucket_seconds": bucket, "from": start, "to": now,
                "series": series, "resets": resets,
                "current": current, **self._burn_rate(current)}

    @staticmethod
    def _burn_rate(current: dict) -> dict:
        """Budget burned per hour, and when this pace would exhaust it.

        Measured from the start of the current window rather than between the last
        two samples: the budget starts every window at zero, which makes the
        average pace both trivial to compute and exactly what a projection needs —
        and immune to the flat stretch while you read a diff.

        A projection landing after the reset is not reported at all. The window
        refills first, so the honest answer is "you will not reach it", and a
        timestamp there would be a warning about something that cannot happen."""
        pct = current.get("five_hour_pct")
        resets_at = current.get("five_hour_resets_at")
        ts = current.get("ts")
        if pct is None or not resets_at or not ts:
            return {"burn_pct_per_hour": None, "projected_full_at": None}
        elapsed_hours = (ts - (resets_at - 5 * 3600)) / 3600
        if elapsed_hours <= 0:
            return {"burn_pct_per_hour": None, "projected_full_at": None}
        burn = pct / elapsed_hours
        remaining = 100.0 - pct
        full_at = None
        if burn > 0 and remaining > 0:
            eta = ts + remaining / burn * 3600
            if eta < resets_at:
                full_at = eta
        return {"burn_pct_per_hour": burn, "projected_full_at": full_at}

    def hourly_report(self, days: Optional[int] = None) -> list[dict]:
        """Turns and tokens by hour of the *local* day.

        Local, from the same clock as `day`: a UTC histogram of somebody's working
        day is shifted by their offset and reads as a stranger's schedule. `days`
        on each row is how many distinct days contributed, so a caller can show a
        daily average instead of a total that grows with the range."""
        rows = self._query(
            "SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER)"
            "   AS hour,"
            " COUNT(*) AS turns, SUM(output_tokens) AS output_tokens,"
            " SUM(input_tokens + output_tokens + cache_read + cache_creation)"
            "   AS tokens,"
            " COUNT(DISTINCT day) AS days"
            f" FROM turns WHERE ts >= ? AND {_NOT_CODEX} GROUP BY hour",
            (self._since(days),),
        )
        by_hour = {row["hour"]: dict(row) for row in rows}
        # Every hour, worked or not: a histogram with gaps in it reads as missing
        # data rather than as an hour nobody was at the keyboard.
        return [by_hour.get(hour, {"hour": hour, "turns": 0, "output_tokens": 0,
                                   "tokens": 0, "days": 0})
                for hour in range(24)]

    def recent_sessions(self, days: Optional[int] = None,
                        limit: int = 50) -> list[dict]:
        """The latest sessions with their own totals, newest first.

        `turns` and `tokens` come from rows that retention eventually drops, so a
        session older than the retention window reports 0 of both while its cost
        survives. That is a gap, not a quiet zero, and the caller is expected to
        render it as one."""
        return [dict(r) for r in self._query(
            "SELECT s.session_id, s.project, s.title, s.kind, s.first_seen,"
            " s.last_seen, s.ended_at, s.end_reason, s.primary_model, s.cost_usd,"
            " s.cost_source, COALESCE(s.provider, 'claude') AS provider,"
            " (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.session_id)"
            "   AS turns,"
            " (SELECT SUM(t.input_tokens + t.output_tokens + t.cache_read"
            "   + t.cache_creation) FROM turns t WHERE t.session_id = s.session_id)"
            "   AS tokens"
            " FROM sessions s WHERE COALESCE(s.last_seen, 0) >= ?"
            " ORDER BY COALESCE(s.last_seen, 0) DESC LIMIT ?",
            (self._since(days), max(1, int(limit))),
        )]

    def context_pressure(self, days: Optional[int] = None) -> dict:
        """How hard the context window is being pushed, and where it gave.

        Two sources with two different denominators, which is why they are
        reported as separate columns and never added up: peaks come from
        `metric_samples`, so they exist only for sessions Dark Army watched live and
        only as far back as retention; compactions come from the transcripts, so
        they cover history from before Dark Army existed and are kept forever."""
        since = self._since(days)
        rows: dict[str, dict] = {}

        def slot(project: str) -> dict:
            return rows.setdefault(project, {
                "project": project, "sessions": 0, "peak_pct": None,
                "over_75": 0, "over_90": 0, "compactions": 0, "manual": 0,
                "avg_pre_tokens": None,
            })

        # Per-session peaks first, then grouped: MAX over the whole project would
        # answer "did anything get close", not "how many sessions did".
        for row in self._query(
            "SELECT COALESCE(s.project, '?') AS project, COUNT(*) AS sessions,"
            " MAX(p.peak) AS peak_pct,"
            " SUM(CASE WHEN p.peak >= 75 THEN 1 ELSE 0 END) AS over_75,"
            " SUM(CASE WHEN p.peak >= 90 THEN 1 ELSE 0 END) AS over_90"
            " FROM (SELECT session_id, MAX(ctx_pct) AS peak FROM metric_samples"
            "       WHERE ts >= ? AND ctx_pct IS NOT NULL GROUP BY session_id) p"
            " LEFT JOIN sessions s ON s.session_id = p.session_id"
            " GROUP BY project",
            (since,),
        ):
            slot(row["project"]).update(
                {k: row[k] for k in ("sessions", "peak_pct", "over_75", "over_90")}
            )

        for row in self._query(
            "SELECT COALESCE(s.project, '?') AS project, COUNT(*) AS compactions,"
            " SUM(CASE WHEN c.trigger = 'manual' THEN 1 ELSE 0 END) AS manual,"
            " AVG(c.pre_tokens) AS avg_pre_tokens"
            " FROM compactions c LEFT JOIN sessions s"
            "   ON s.session_id = c.session_id"
            " WHERE c.ts >= ? GROUP BY project",
            (since,),
        ):
            slot(row["project"]).update(
                {k: row[k] for k in ("compactions", "manual", "avg_pre_tokens")}
            )

        ordered = sorted(rows.values(),
                         key=lambda r: (r["over_90"], r["compactions"],
                                        r["peak_pct"] or 0), reverse=True)
        return {
            "by_project": ordered,
            "compactions": sum(r["compactions"] for r in ordered),
            "manual_compactions": sum(r["manual"] for r in ordered),
            "sessions_over_90": sum(r["over_90"] for r in ordered),
            "sessions_sampled": sum(r["sessions"] for r in ordered),
        }

    # --- usage attribution (what /usage calls "what's contributing") ---
    #
    # Claude Code's own /usage screen answers this from the same transcripts, and
    # says so: "approximate, based on local sessions on this machine". Everything
    # below inherits that caveat — a machine that also drives Claude on the web or
    # on a second laptop is only seeing its own share.
    #
    # Weighted by *cost*, not by turns or by tokens: an Opus turn and a Haiku turn
    # are not the same claim on a budget, and a table that ranked by token count
    # would put the cheapest model on top of the list of what is spending your
    # week.

    # Below this, a row is noise rather than a finding — and the tables say so
    # ("nothing over 1%") instead of listing a tail of rounding errors.
    MIN_ATTRIBUTION_PCT = 1
    # A behaviour has to account for this much of the window before it is worth a
    # sentence. Higher than the table cut: a headline is a stronger claim.
    MIN_BEHAVIOUR_PCT = 5
    # Behaviour thresholds. Ours, not read off the wire — the shapes come from
    # what /usage names, the numbers from what the columns can actually test.
    BIG_CACHE_WRITE = 100_000      # a >100k-token cache miss, rewritten in one turn
    LONG_CONTEXT = 150_000         # everything the model was handed, one turn
    PARALLEL_WINDOW = 300          # 5 minutes: the grain "at the same time" means
    PARALLEL_SESSIONS = 4          # "4+ sessions running in parallel"
    LONG_SESSION = 8 * 3600        # a session still going 8 hours later

    _ATTRIBUTION_DIMENSIONS = (
        ("skills", "attr_skill"),
        ("agents", "attr_agent"),
        ("plugins", "attr_plugin"),
        ("mcp_servers", "attr_mcp_server"),
        ("mcp_tools", "attr_mcp_tool"),
    )

    def usage_attribution(self, days: Optional[int] = None,
                          since: Optional[float] = None) -> dict:
        """Cost share per model and per skill / subagent / plugin / MCP server,
        and the behaviours that shaped the window.

        `since` is an explicit window start and wins over `days`. The limit
        windows are not "the last N days" — the five-hour one resets at a wall
        clock instant the statusline reports, and a rolling five hours ending
        *now* covers a different set of turns than the window the bar is
        actually measuring. Given the reset time, the caller can hand us the
        real edge; `days` stays for callers that have none.

        The grouping is SQL's, the pricing Python's, and the split is not a
        stylistic one: cost per (model, day) cannot be expressed in SQLite without
        a rate table in the database, and putting one there would mean a second
        copy of `pricing.py` that could drift from the first. So the query returns
        one row per (dimension value, model, day) — a few thousand rows for a busy
        week — and each is priced once on the way past.

        Percentages are of the window's *whole* cost, so they do not sum to 100:
        a turn can be both a skill's and an MCP server's, and most turns are
        neither. That is the same thing /usage means by "these are independent
        characteristics of your usage, not a breakdown"."""
        since = self._since(days) if since is None else since
        total = self._priced_total(since)
        out = {
            "window_cost_usd": total["cost_usd"],
            "turns": total["turns"],
            "unpriced_turns": total["unpriced_turns"],
            "window_start": since or None,
            "models": self._model_rows(since, total["cost_usd"]),
            "behaviours": [],
        }
        for key, _column in self._ATTRIBUTION_DIMENSIONS:
            out[key] = []
        if total["cost_usd"] <= 0:
            # No priced turn in the window: every share would be a division by
            # zero, and "0%" everywhere reads as a measurement rather than as
            # "nothing to measure". The model table is still returned — its
            # token counts are measurements, not shares, and a window with an
            # unpriced model in it is exactly when you want to see them.
            return out

        for key, column in self._ATTRIBUTION_DIMENSIONS:
            out[key] = self._attribution_rows(column, since, total["cost_usd"])
        out["behaviours"] = self._behaviours(since, total["cost_usd"])
        return out

    def _model_rows(self, since: float, total_cost: float, *,
                    provider: Optional[str] = None,
                    until: Optional[float] = None) -> list[dict]:
        """Per model: the four token kinds, the turns, and the estimated cost.

        This is the one table on the Usage view that is a real breakdown — every
        turn ran on exactly one model, so unlike the skill and MCP tables these
        shares do sum to 100. The four token counts are kept apart rather than
        added up because they are not interchangeable: a cache read is charged
        at a tenth of fresh input and an output token at five times it, so
        "12M tokens" says almost nothing about what a model cost you.

        A model we have no rate for still gets its row, with `cost_usd` None —
        the tokens were real even when the price is not known."""
        from . import pricing

        where, params = f"ts >= ? AND {_NOT_CODEX}", [since]
        if provider is not None:
            where += " AND COALESCE(provider, 'claude') = ?"
            params.append(provider)
        if until is not None:
            where += " AND ts <= ?"
            params.append(until)
        rows = self._query(
            "SELECT model, day, COUNT(*) AS turns, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation"
            f" FROM turns WHERE {where} GROUP BY model, day",
            tuple(params),
        )
        folded: dict[str, dict] = {}
        for row in rows:
            entry = folded.setdefault(row["model"] or "", {
                "model": row["model"] or "", "turns": 0, "input_tokens": 0,
                "output_tokens": 0, "cache_read": 0, "cache_creation": 0,
                "cost_usd": 0.0, "unpriced_turns": 0,
            })
            for field in ("turns", "input_tokens", "output_tokens",
                          "cache_read", "cache_creation"):
                entry[field] += row[field] or 0
            cost = self._price_row(pricing, row)
            if cost is None:
                entry["unpriced_turns"] += row["turns"] or 0
            else:
                entry["cost_usd"] += cost

        ranked = sorted(folded.values(), key=lambda e: e["cost_usd"], reverse=True)
        for entry in ranked:
            entry["pct"] = (round(entry["cost_usd"] / total_cost * 100)
                            if total_cost > 0 else None)
            if entry["unpriced_turns"] and not entry["cost_usd"]:
                # Priced nothing at all: report no cost rather than $0.00, which
                # a reader would take for "this model was free".
                entry["cost_usd"] = None
                entry["pct"] = None
        return ranked

    def usage_provider_groups(self, since: float, until: float) -> list[dict]:
        """A provider's own cost denominator; legacy mixed attribution survives.

        Identity comes from the recorded provider, including unknown providers,
        never from the spelling of a model. Unpriced work has no percentage.
        """
        providers = self._query(
            "SELECT DISTINCT COALESCE(provider, 'claude') AS provider FROM turns"
            " WHERE ts >= ? AND ts <= ? ORDER BY provider", (since, until))
        groups = []
        for provider in providers:
            name = provider["provider"]
            if name == "codex":
                continue  # Codex journals are the single accounting source.
            rows = self._model_rows(since, 0, provider=name, until=until)
            total = sum(row["cost_usd"] or 0 for row in rows)
            partial = any(row["unpriced_turns"] for row in rows)
            for row in rows:
                row["pct"] = (round(100 * row["cost_usd"] / total)
                              if total and row["cost_usd"] is not None else None)
            groups.append({"provider": name, "available": True,
                           "reason": "Some recorded work has no known price." if partial else "",
                           "measurement": "estimated_cost_share" if total else "unpriced",
                           "window_start": since, "window_end": until,
                           "partial": partial, "models": rows})
        return groups

    def _attribution_rows(self, column: str, since: float,
                          total_cost: float) -> list[dict]:
        """One dimension's table, dearest first."""
        from . import pricing

        rows = self._query(
            f"SELECT {column} AS name, model, day, COUNT(*) AS turns,"
            " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,"
            " SUM(cache_read) AS cache_read, SUM(cache_creation) AS cache_creation"
            f" FROM turns WHERE ts >= ? AND {column} IS NOT NULL"
            f" GROUP BY {column}, model, day",
            (since,),
        )
        folded: dict[str, dict] = {}
        for row in rows:
            entry = folded.setdefault(row["name"], {
                "name": row["name"], "cost_usd": 0.0, "turns": 0,
                "output_tokens": 0, "unpriced_turns": 0,
            })
            entry["turns"] += row["turns"] or 0
            entry["output_tokens"] += row["output_tokens"] or 0
            cost = self._price_row(pricing, row)
            if cost is None:
                entry["unpriced_turns"] += row["turns"] or 0
            else:
                entry["cost_usd"] += cost

        ranked = sorted(folded.values(), key=lambda e: e["cost_usd"], reverse=True)
        for entry in ranked:
            # Rounded to whole points, like the source screen: a share quoted to
            # two decimals invites a precision the estimate does not have.
            entry["pct"] = round(entry["cost_usd"] / total_cost * 100)
        return [e for e in ranked if e["pct"] >= self.MIN_ATTRIBUTION_PCT]

    def _behaviours(self, since: float, total_cost: float) -> list[dict]:
        """The four "what's contributing" claims, each as a share of the window.

        Each is a predicate over turns rather than a category of them, which is
        why they overlap freely: one expensive turn can be a big cache write, at
        long context, inside a subagent-heavy session, during a four-session
        pile-up."""
        # Every one of these groups to (model, day) in SQL, for the same reason
        # the tables do: that is the grain pricing needs, and it is the difference
        # between folding a few dozen rows and folding every turn of the week.
        totals = ("COUNT(*) AS turns, SUM(input_tokens) AS input_tokens,"
                  " SUM(output_tokens) AS output_tokens, SUM(cache_read) AS"
                  " cache_read, SUM(cache_creation) AS cache_creation")
        found = []
        for key, sql, params in (
            ("cache_miss",
             f"SELECT model, day, {totals} FROM turns"
             f" WHERE ts >= ? AND {_NOT_CODEX} AND cache_creation >= ?"
             " GROUP BY model, day",
             (since, self.BIG_CACHE_WRITE)),
            ("long_context",
             f"SELECT model, day, {totals} FROM turns"
             f" WHERE ts >= ? AND {_NOT_CODEX}"
             " AND (input_tokens + cache_read + cache_creation) >= ?"
             " GROUP BY model, day",
             (since, self.LONG_CONTEXT)),
            ("subagents",
             f"SELECT model, day, {totals} FROM turns"
             f" WHERE ts >= ? AND {_NOT_CODEX} AND is_sidechain = 1"
             " GROUP BY model, day",
             (since,)),
            # Concurrency is not a column, so it is derived: bucket the window
            # into five-minute slots, keep the slots in which four or more
            # sessions each produced a turn, and charge those slots' turns to it.
            ("parallel",
             "WITH slots AS ("
             "  SELECT CAST(ts / ? AS INTEGER) AS slot,"
             "         COUNT(DISTINCT session_id) AS sessions"
             f"  FROM turns WHERE ts >= ? AND {_NOT_CODEX}"
             "  GROUP BY slot HAVING sessions >= ?)"
             f" SELECT t.model, t.day, {totals} FROM turns t"
             " JOIN slots ON CAST(t.ts / ? AS INTEGER) = slots.slot"
             " WHERE t.ts >= ? AND COALESCE(t.provider, 'claude') != 'codex'"
             " GROUP BY t.model, t.day",
             (self.PARALLEL_WINDOW, since, self.PARALLEL_SESSIONS,
              self.PARALLEL_WINDOW, since)),
            # Long sessions: the session's own span, from its first turn in the
            # database to its last — not `last_seen - first_seen`, which a
            # reconnect days later would stretch without a turn in between.
            ("long_sessions",
             "WITH spans AS ("
             f"  SELECT session_id FROM turns WHERE ts >= ? AND {_NOT_CODEX}"
             "  GROUP BY session_id HAVING MAX(ts) - MIN(ts) >= ?)"
             f" SELECT t.model, t.day, {totals} FROM turns t"
             " JOIN spans ON spans.session_id = t.session_id"
             " WHERE t.ts >= ? AND COALESCE(t.provider, 'claude') != 'codex'"
             " GROUP BY t.model, t.day",
             (since, self.LONG_SESSION, since)),
        ):
            share = self._share_of(sql, params, total_cost)
            if share["pct"] >= self.MIN_BEHAVIOUR_PCT:
                found.append({"key": key, **share})
        return sorted(found, key=lambda b: b["pct"], reverse=True)

    def _share_of(self, sql: str, params: tuple, total_cost: float) -> dict:
        """What fraction of the window's cost the rows of `sql` account for.

        `sql` must group to (model, day) and sum the token columns: that is the
        grain a price applies to, so the summing has to happen on both sides of
        the pricing call, never only before it."""
        from . import pricing

        cost, turns = 0.0, 0
        for row in self._query(sql, params):
            turns += row["turns"] or 0
            priced = self._price_row(pricing, row)
            if priced is not None:
                cost += priced
        return {"pct": round(cost / total_cost * 100), "cost_usd": cost,
                "turns": turns}

    def _priced_total(self, since: float) -> dict:
        """The window's denominator: every turn, priced per (model, day)."""
        from . import pricing

        cost, turns, unpriced = 0.0, 0, 0
        for row in self._query(
            "SELECT model, day, COUNT(*) AS turns, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read) AS cache_read,"
            " SUM(cache_creation) AS cache_creation,"
            " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)"
            "   AS measured_turns,"
            " SUM(cost_usd) AS measured_cost"
            f" FROM turns WHERE ts >= ? AND {_NOT_CODEX} GROUP BY model, day",
            (since,),
        ):
            turns += row["turns"] or 0
            priced, group_unpriced = self._cost_of_turn_group(pricing, row)
            unpriced += group_unpriced
            if priced is not None:
                cost += priced
        return {"cost_usd": cost, "turns": turns, "unpriced_turns": unpriced}

    @staticmethod
    def _cost_of_turn_group(pricing, row) -> tuple[Optional[float], int]:
        """(cost, unpriced_turns) for one GROUP BY row.

        A group that stored measured `cost_usd` on every turn uses that sum —
        Grok stamps ticks, and re-pricing them from a Claude rate card would
        invent a different bill. A group with no measured cost is estimated
        from tokens. A mixed group keeps the measured subset and counts the
        rest as unpriced: we never estimate a turn that arrived without a
        figure, because absence means unknown, not free.
        """
        turns = row["turns"] or 0
        measured_n = row["measured_turns"] or 0 if "measured_turns" in row.keys() else 0
        measured = row["measured_cost"] or 0.0 if "measured_cost" in row.keys() else 0.0
        if measured_n >= turns and turns:
            return float(measured), 0
        if measured_n == 0:
            priced = HistoryStore._price_row(pricing, row)
            if priced is None:
                return None, turns
            return priced, 0
        return (float(measured) if measured_n else None), turns - measured_n

    @staticmethod
    def _price_row(pricing, row) -> Optional[float]:
        """Price one grouped row at Claude's list prices.

        Claude's list only: a Grok or Codex id is None here, exactly as it was
        before those publishers had rows, so `cost_usd` / `estimated_cost_usd`
        and every report built on this keep their meaning. Their token price
        is `_token_turn_cost`'s.

        The cache write is charged by its recorded split when the grouped row
        carries one (`cache_write_5m` and `cache_write_1h` both summed and not
        NULL — a group of turns that all stated it). Otherwise the whole
        `cache_creation` total is charged at the 5-minute rate, the
        conservative choice, because the 1-hour rate is the dearer of the two
        and an unknown split cannot be given a made-up hour share."""
        keys = row.keys()
        write_5m = row["cache_write_5m"] if "cache_write_5m" in keys else None
        write_1h = row["cache_write_1h"] if "cache_write_1h" in keys else None
        if write_5m is None or write_1h is None:
            write_5m, write_1h = row["cache_creation"] or 0, 0
        return pricing.cost_usd(
            row["model"],
            input_tokens=row["input_tokens"] or 0,
            output_tokens=row["output_tokens"] or 0,
            cache_read=row["cache_read"] or 0,
            cache_write_5m=write_5m or 0,
            cache_write_1h=write_1h or 0,
            day=row["day"],
            provider="claude",
        )

    # --- digests and themes (the panel's product tab) ---

    def session_tools(self, session_id: str, top: int = 40,
                      own_turns_only: bool = True) -> list[tuple]:
        """The tools a session leaned on, busiest first.

        Two defaults here were wrong for years and both distorted the same
        consumer — the tool-mix classifier in `themes.py`:

        **`top` was 5.** Five is a fine number for a menu label and a poor one for
        a denominator: 45% of sessions on record use more than five distinct
        tools, so 6.5% of all call volume fell outside the list — and for 79
        sessions the tool that fell out was `Agent`, the one signal that says the
        work was delegated.

        **Subagent turns were counted as the session's own** — 41.9% of every tool
        call in the database. A session that delegates spawns hundreds of subagent
        Bash and Read calls, all stamped with the parent `session_id` and an
        `agent_id`. Folding those into the parent's mix inverts the signal: the
        more heavily a session delegates, the *smaller* its `Agent` share becomes.
        Measured, the fix moves the number of sessions reading as delegation-heavy
        from 1 to 10. `own_turns_only` restores the obvious reading; pass False
        for the whole-tree total."""
        sql = ("SELECT tool_name, COUNT(*) AS n FROM turns"
               " WHERE session_id = ? AND tool_name IS NOT NULL"
               "   AND tool_name != ''")
        if own_turns_only:
            # `''`, not NULL: the scanner stamps every turn with an agent id and
            # writes the empty string for the session's own. 61,451 of the 104,000
            # rows on record are `''` and *none* are NULL, so the obvious
            # `agent_id IS NULL` matches nothing at all and silently returns an
            # empty tool mix for every session.
            sql += " AND COALESCE(agent_id, '') = ''"
        sql += " GROUP BY tool_name ORDER BY n DESC LIMIT ?"
        return [(r["tool_name"], r["n"]) for r in self._query(
            sql, (session_id, max(1, int(top))))]

    def session_attribution(self, session_id: str, limit: int = 6) -> list[str]:
        """Distinct skill / agent / MCP-server names a session used.

        Free, concrete nouns ("gitnexus", "releasing") that say more about what a
        session *was* than any token count — and unlike the prompt text, they were
        never written by a human, so nothing here needs redacting."""
        seen: list[str] = []
        for column in ("attr_skill", "attr_agent", "attr_mcp_server"):
            for row in self._query(
                f"SELECT DISTINCT {column} AS v FROM turns"
                f" WHERE session_id = ? AND {column} IS NOT NULL AND {column} != ''",
                (session_id,),
            ):
                value = row["v"]
                if value not in seen:
                    seen.append(value)
                if len(seen) >= limit:
                    return seen
        return seen

    def unassigned_count(self, days: Optional[int] = None) -> int:
        return self._scalar(
            "SELECT COUNT(*) FROM session_digests d"
            " LEFT JOIN theme_sessions ts ON ts.session_id = d.session_id"
            " WHERE ts.session_id IS NULL AND COALESCE(d.last_seen, 0) >= ?",
            (self._since(days),),
        ) or 0

    def _write(self, sql: str, params: tuple, want_rowcount: bool = False):
        with self._lock:
            if self._conn is None:
                logger.debug("history write before connect(); dropped")
                return False
            try:
                cur = self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.Error:
                # History is an observer of the system, never a gate on it: a
                # write that fails must cost a log line, not an event.
                logger.warning("history write failed", exc_info=True)
                return False
            return cur.rowcount > 0 if want_rowcount else True

    def _query(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            if self._conn is None:
                return []
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                logger.warning("history query failed", exc_info=True)
                return []

    def _scalar(self, sql: str, params: tuple = ()):
        rows = self._query(sql, params)
        return rows[0][0] if rows else None
