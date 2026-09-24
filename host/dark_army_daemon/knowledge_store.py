# host/dark_army_daemon/knowledge_store.py
"""A project's own question-and-answer notes, in board.db. BoardStore owns the
connection and the lock; this mixin only adds a table and the verbs.

**Not card-scoped, on purpose.** A project's purpose, its constraints and the
things it must never do outlive every card that happened to be open when
somebody wrote them down, so `knowledge_entries` is keyed on the enrolled
**root** and is named at none of `board.py`'s three card-deletion sites and in
no orphan sweep. Deleting a card, clearing Done and archiving a project's work
all leave these rows exactly where they are.

`card_runs`' shape otherwise: a table rather than columns on `cards`, created by
`CREATE TABLE IF NOT EXISTS` on connect, with no `_ADDED_COLUMNS` entry, so an
older build's tolerant `SELECT * FROM cards` cannot see it and cannot blank it.
It names no member of `board._WRITABLE` and no key in
`ApiServer._BOARD_FIELDS`: nothing a surface can write, and nothing that rides
the wire. The three extra columns (`last_confirmed`, `stale`, `source`) are
PRAGMA-driven ALTERs inside `_connect_knowledge`, not a `SCHEMA_VERSION` bump:
knowledge is not the cards migration.

Channel writes go through `knowledge_put` and must not set `last_confirmed` or
clear `stale`. Person confirm / mark-stale / edit are their own verbs.
"""

from __future__ import annotations

import re
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_entries (
    root        TEXT NOT NULL,
    key         TEXT NOT NULL,
    question    TEXT NOT NULL DEFAULT '',
    answer      TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    last_confirmed REAL NOT NULL DEFAULT 0,
    stale       TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (root, key)
);
CREATE INDEX IF NOT EXISTS knowledge_by_root ON knowledge_entries(root, key);
"""

#: Columns a file opened by an older CREATE (no last_confirmed / stale /
#: source) still lacks. Added by ALTER after `CREATE TABLE IF NOT EXISTS` is
#: a no-op; never `_ADDED_COLUMNS` (that dict is `cards`).
_KNOWLEDGE_COLUMNS = (
    ("last_confirmed", "REAL NOT NULL DEFAULT 0"),
    ("stale", "TEXT NOT NULL DEFAULT ''"),
    ("source", "TEXT NOT NULL DEFAULT ''"),
)

#: The catalogue's question ids are lower-case slugs, and this is the shape a
#: key is reduced to before it is stored. `test_knowledge_skill.py` pins every
#: shipped catalogue key against the same expression, so no catalogue key can
#: be silently rewritten on its way into the store.
_KEY_RE = re.compile(r"[^a-z0-9._-]")

#: The bounds. Clamp-don't-refuse for the two prose fields (`close_note`'s
#: rule: Dark Army is relaying somebody's words), refuse for the things that would
#: make a row meaningless, and a per-root ceiling so a runaway session cannot
#: fill a project's notes with eighty variations of nothing.
MAX_KEY_CHARS = 64
MAX_QUESTION_CHARS = 400
MAX_ANSWER_CHARS = 4000
MAX_ENTRIES_PER_ROOT = 80

_NO_SUCH_NOTE = "Dark Army has no such note"
_EMPTY_ROOT = "Dark Army could not tell which project this is for"
_EMPTY_KEY = "a note needs a question key"
_EMPTY_ANSWER = "a note needs an answer"


def normalise_key(key) -> str:
    """A catalogue key reduced to the stored form. Never raises."""
    return _KEY_RE.sub("", str(key or "").strip().lower())[:MAX_KEY_CHARS]


def _clamp(text, limit: int) -> str:
    return str(text or "")[:limit]


class KnowledgeStoreMixin:
    """Read and write one project's notes. Mixed into `BoardStore`."""

    MAX_KEY_CHARS = MAX_KEY_CHARS
    MAX_QUESTION_CHARS = MAX_QUESTION_CHARS
    MAX_ANSWER_CHARS = MAX_ANSWER_CHARS
    MAX_ENTRIES_PER_ROOT = MAX_ENTRIES_PER_ROOT

    def _connect_knowledge(self) -> None:
        """`_connect_outcomes`' shape: executed at the end of `connect()`, so a
        file stamped at an older schema version gains the table simply by being
        opened by a build that has it. The three extra columns are ALTER'd on
        a file whose CREATE predated them; `SCHEMA_VERSION` stays 22."""
        self._conn.executescript(SCHEMA)
        existing = {
            str(row["name"]) for row in self._conn.execute(
                "PRAGMA table_info(knowledge_entries)").fetchall()
        }
        missing = [(name, spec) for name, spec in _KNOWLEDGE_COLUMNS
                   if name not in existing]
        if not missing:
            return
        with self._conn:
            for name, spec in missing:
                self._conn.execute(
                    f"ALTER TABLE knowledge_entries ADD COLUMN {name} {spec}")

    def knowledge_for(self, root: str) -> list:
        """This root's entries, ordered by key. `[]` for an empty root.

        **There is no "all roots" mode and there never should be.** The one
        caller is a channel verb whose whole security argument is that a
        session can only reach the notes of the project it is already working
        in; a method that could return every project's rows would put that one
        `if` in the daemon rather than in the shape of the API.

        `SELECT *` into a dict: unknown keys are ignored by callers, and a
        file that has not yet been opened by this build cannot happen —
        `_connect_knowledge` runs on every `connect()`.
        """
        root = str(root or "").strip()
        if not root:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM knowledge_entries WHERE root = ? ORDER BY key",
                (root,)).fetchall()
        return [dict(row) for row in rows]

    def knowledge_count(self, root: str) -> int:
        root = str(root or "").strip()
        if not root:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM knowledge_entries WHERE root = ?",
                (root,)).fetchone()
        return int(row["n"]) if row else 0

    def _knowledge_place(self, root, key) -> tuple:
        """`(root, key, refusal)`. Exactly one of root/key or refusal is set."""
        root = str(root or "").strip()
        if not root:
            return "", "", _EMPTY_ROOT
        key = normalise_key(key)
        if not key:
            return "", "", _EMPTY_KEY
        return root, key, ""

    def knowledge_put(self, root: str, key: str, question: str, answer: str,
                      author: str = "") -> tuple:
        """Write one answer. `(ok, detail)`.

        Clamps the two prose fields and refuses the four things that would
        leave a row nobody could read: an empty root, a key that normalises to
        nothing, an empty answer, and — for a **new** key only — a root already
        holding `MAX_ENTRIES_PER_ROOT` entries. An *update* to a key that is
        already there is never refused by the ceiling: a project at its limit
        must still be able to correct what it already said.

        One statement, `ON CONFLICT DO UPDATE`, so `created_at` never moves —
        the row records when the project first answered that question, and
        re-answering it is not a new fact appearing.

        An agent write sets `source = 'agent'` and **must not** set
        `last_confirmed` or set or clear `stale`. A brand-new agent note is
        unconfirmed. A later agent rewrite of a stale note stays stale and
        unconfirmed.
        """
        root, key, refusal = self._knowledge_place(root, key)
        if refusal:
            return False, refusal
        question = _clamp(question, MAX_QUESTION_CHARS).strip()
        answer = _clamp(answer, MAX_ANSWER_CHARS).strip()
        if not answer:
            return False, _EMPTY_ANSWER
        now = time.time()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT 1 FROM knowledge_entries WHERE root = ? AND key = ?",
                (root, key)).fetchone()
            if existing is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM knowledge_entries WHERE root = ?",
                    (root,)).fetchone()
                if int(row["n"] if row else 0) >= MAX_ENTRIES_PER_ROOT:
                    return False, (
                        f"this project already holds {MAX_ENTRIES_PER_ROOT} "
                        "notes — nothing was written")
            self._conn.execute(
                "INSERT INTO knowledge_entries"
                "(root, key, question, answer, author, created_at, updated_at,"
                " source)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(root, key) DO UPDATE SET"
                " question = excluded.question, answer = excluded.answer,"
                " author = excluded.author, updated_at = excluded.updated_at,"
                " source = 'agent'",
                (root, key, question, answer, str(author or ""), now, now,
                 "agent"))
        return True, "stored"

    def knowledge_confirm(self, root: str, key: str) -> tuple:
        """A person confirms a note. `(ok, detail)`.

        `declare_done`'s shape: the guard rides in the UPDATE's own WHERE
        clause, `rowcount == 0` means the note moved or is gone. Sets
        `last_confirmed = now`, `source = 'person'`, `stale = ''` — confirming
        archived advice brings it back.
        """
        root, key, refusal = self._knowledge_place(root, key)
        if refusal:
            return False, refusal
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE knowledge_entries SET last_confirmed = ?,"
                " source = 'person', stale = ''"
                " WHERE root = ? AND key = ?",
                (now, root, key))
            if cur.rowcount == 0:
                return False, _NO_SUCH_NOTE
        return True, "confirmed"

    def knowledge_mark_stale(self, root: str, key: str) -> tuple:
        """A person marks a note stale. `(ok, detail)`.

        Sets `stale = '1'` only. Does not change `last_confirmed` or the
        answer. Not destruction: there is no delete verb.
        """
        root, key, refusal = self._knowledge_place(root, key)
        if refusal:
            return False, refusal
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE knowledge_entries SET stale = '1'"
                " WHERE root = ? AND key = ?",
                (root, key))
            if cur.rowcount == 0:
                return False, _NO_SUCH_NOTE
        return True, "marked stale"

    def knowledge_edit(self, root: str, key: str, question: str,
                       answer: str) -> tuple:
        """A person edits a note's prose. `(ok, detail)`.

        Updates question/answer, `source = 'person'`, `updated_at = now`,
        `author = ''`. Does **not** set `last_confirmed` and does **not**
        clear `stale` — confirm stays its own press. Clamp-don't-refuse on
        prose; empty answer after strip is refused.
        """
        root, key, refusal = self._knowledge_place(root, key)
        if refusal:
            return False, refusal
        question = _clamp(question, MAX_QUESTION_CHARS).strip()
        answer = _clamp(answer, MAX_ANSWER_CHARS).strip()
        if not answer:
            return False, _EMPTY_ANSWER
        now = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE knowledge_entries SET question = ?, answer = ?,"
                " source = 'person', updated_at = ?, author = ''"
                " WHERE root = ? AND key = ?",
                (question, answer, now, root, key))
            if cur.rowcount == 0:
                return False, _NO_SUCH_NOTE
        return True, "stored"
