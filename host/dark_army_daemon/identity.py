"""Stable nicknames for agents — an identity, not a description.

`_session_name` answers "what is this session working on", recomputing a label
from the statusline, then the AI title, then the CLI slug. That is the right
answer to that question and the wrong one to "which agent is this": it changes
under you as the work moves on, so it can never be the thing you say out loud.

So a session also gets a **nickname** from a fixed cast.

**The character belongs to the agent, not to the repo.** An earlier draft keyed
this on the working directory, so a project kept its name across days — and it
was wrong for the way this is actually used. Several agents run in one repo at
the same time, routinely; with the repo owning a name, the first agent to start
*was* the project and every sibling got whatever the pool had left over. Keying
on the session makes each agent its own character, which is the thing being
named. The cost is real and accepted: a session id belongs to one run, so the
cast rotates as sessions come and go rather than staying put across days.

What stability there is comes from three rules:

* **Sticky for the session's lifetime.** Once a session has a nickname it keeps
  it — through a daemon restart, through going quiet, and through the half hour
  its tombstone is still on screen. A row must never rename itself while it is
  being read.

  **One exception, and it is narrow** (`reassign`): the moment Dark Army binds a
  session to a card it dispatched itself, it may correct that session's name
  once to Cipher for Refine or a lead of its delivery area for Start. It is bounded three ways — the
  bind can only happen inside `dispatch.DISPATCH_BIND_WINDOW` of the press, it
  happens at most once because a card binds at most once, and a bound session
  is never renamed again. Nothing else may call it: a rename at any other
  moment is the failure this rule exists to prevent.
* **Kept across `--resume`.** That reuses the session id, so it reuses the name.
* **Deterministic before it is persistent.** The first pick is a hash of the
  session id, so the same session lands on the same name on a machine that has
  lost its state file. `hashlib`, deliberately, and never `hash()` — that is
  per-process randomised for `str`, and this project has already shipped one bug
  (theme splitting) that only reproduced under some `PYTHONHASHSEED` values.

Collisions are resolved against *live* agents only: two agents take adjacent
names from the pool rather than a suffix, because "Cipher" and "Vex" are
told apart at a glance and "dark-army-2" is not.

The cast is Dark Army's own callsigns. It has been replaced twice — from an
earlier show on 6 Sep 2026, and on 22 Sep 2026 when fourteen borrowed names
were swapped for callsigns in place, index for index — and neither move needed
a migration: `load()`'s `_in_cast` filter drops every stored name this build no
longer recognises, so a session named under the old cast is renamed exactly
once — at the first launch of the new build — and is sticky again from then on.
Board cards keep the character slugs they recorded; `current_crew` maps an old
slug to its successor on read (`LEGACY_SLUGS`), and the stored column is never
rewritten.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Iterable, Optional

from .paths import STATE_DIR, ensure_state_dir

logger = logging.getLogger("dark-army")

IDENTITY_PATH = STATE_DIR / "identities.json"

# The cast: fourteen Dark Army callsigns plus six of the household —
# Androll, Captcha, Sawa, Franio, Zosia and Ptyś — one word each. **Order is a hash
# index** — `proposed_index` and `cast.character_for`'s djb2 both index this
# tuple, and `panel/Sources/BobPanel/Cast.swift` / `ios/BobPhone/Cast.swift`
# carry the same twenty in the same order. Reordering hands every agent
# somebody else's face; replace all three lists in one edit. **Append only**:
# the six newcomers go on the end so the original fourteen keep their
# indices, and every one of them is an ordinary assignable name. The 22 Sep
# 2026 rebrand replaced the first fourteen in place for the same reason: the
# session that hashed to index *i* still lands on index *i*.
#
# A name here needs no art to be legal. The panel and the phone draw a
# character with no portrait as its initial on a plain tile, and the strip
# draws one with no pixel art as the aggregate glyph plus a count — so the
# two art trees (`assets/portraits`, `assets/cast`) may fill in on different
# days. `assets/cast/manifest.json` may only *declare* slugs from this tuple
# and `ART_ONLY`; that direction is the one that is pinned.
#
# Twenty distinct identities at once. Overflow is `{stem}-{sha1[:4]}` and the
# strip treats that as "no portrait", emptying the category. The three-letter
# tab badges (`terminal_title.short`) are unique across the tuple, and the
# roster was chosen to keep that true — Cip Vex Led Mir Hex Rel For Wat Aud
# Pro Qui Nyx Can Vel, then And/Cap/Saw/Fra/Zos/Pty.
NAMES: tuple[str, ...] = (
    "Cipher", "Vex", "Ledger", "Mira", "Hex", "Relay", "Forge",
    "Watch", "Audit", "Proxy", "Quiet", "Nyx", "Canon", "Velvet",
    "Androll", "Captcha", "Sawa", "Franio", "Zosia", "Ptyś",
)

# Slugs that have art but are never assigned to a session. `overwatch` is the
# chief of staff's alter ego — `bc-planner`'s banner is Overwatch, and it is
# never a nickname a session wears. A declared list rather than a special case
# buried in a test; `Cast.artOnly` is the Swift mirror.
ART_ONLY: tuple[str, ...] = ("overwatch",)

# One line per portrait slug, drawn under a *large* portrait only (the Mac's
# detail header and quiet screens, the phone's agent page) — never on a row,
# tile, badge or banner. Keys in roster order: `NAMES` lowercased, then
# `ART_ONLY`. `CastQuotes` in both clients' `Cast.swift` carries the same
# pairs, byte-pinned by `test_cast_quotes.py`.
QUOTES: dict[str, str] = {
    "cipher": "Root the plan before you root the box.",
    "vex": "Crash it on purpose. Own the dump.",
    "ledger": "Burn rate lies. Tokens don't.",
    "mira": "No AC? No merge. Cry elsewhere.",
    "hex": "Segfaults are just honesty with teeth.",
    "relay": "No dropped packets or handoffs.",
    "forge": "CI red? You're still in the shell.",
    "watch": "Unsigned binaries don't leave the room.",
    "audit": "Follow the write. Ignore the pitch.",
    "proxy": "I live in the gap between your PRs.",
    "quiet": "No status spam. Just a clean diff.",
    "nyx": "Default deny. Prove trust in code.",
    "canon": "Break the contract, I break the branch.",
    "velvet": "Main unlocks when the crew aligns.",
    "androll": "Daemon up or nothing ships.",
    "captcha": "Pretty plans die in my review.",
    "sawa": "Green CI or stay offline.",
    "franio": "Fail fast. Patch once. No cosplay.",
    "zosia": "Backlog hygiene is brain opsec.",
    "ptyś": "Tiny commits. Wide kill radius.",
    "overwatch": "Map the blast radius. Then one key.",
}

# The slugs the 22 Sep 2026 rebrand retired, each to the callsign that took
# its index. Keyed by `legacy_key(old_slug)` — sixteen hex of SHA-256 — so
# the retired names themselves ship in no source file or bundle; the tests
# hold the plaintext pairs (`test_identity.py`, `test_daemon.py`). Read-side
# only: a card's `crew_trail` keeps what it recorded and `current_crew` shows
# the successor.
LEGACY_SLUGS: dict[str, str] = {
    "01e76a28977874f8": "cipher",
    "e195b8562e0b9110": "vex",
    "92a83fffa45524c3": "ledger",
    "79a6a933dfc9b197": "mira",
    "95be9024c49a3ae2": "hex",
    "c0d50e4ee9ac1e92": "relay",
    "f3d8f6ef539025ac": "forge",
    "7e6e9877fff0dbcc": "watch",
    "683b531f41ec88a2": "audit",
    "0907628098f9ec20": "proxy",
    "1534cf2af76ecd84": "quiet",
    "249233040558e805": "nyx",
    "da61d340a01040b0": "canon",
    "ecbd105358e381d7": "velvet",
    "8f8a3bfa0c70f774": "overwatch",
}

# How far into the cast a *first* pick may land. All twenty are first-picks;
# the deep-cuts branch stays unused until a later batch splits the pool.
PRIMARY_CAST = len(NAMES)

# One entry per session ever seen would grow without bound — the failure mode
# this codebase has already had to fix in four other per-session dicts. The map
# is pruned to the most recent entries on save; insertion order is the age proxy.
MAX_SESSION_ENTRIES = 512


def _stem(name: str) -> str:
    """The cast character a nickname occupies, ignoring a trailing overflow tag."""
    return name.split("-", 1)[0]


def _in_cast(name: str) -> bool:
    """Whether a stored name is still one this build can draw.

    The cast has been replaced whole (and once shrank from 36 names), and an
    assignment is otherwise sticky for life — so without this, a session
    named before the change keeps a name no surface can draw and is rendered
    wearing somebody else's face. Dropping it costs that session a rename,
    once, which is the lesser of the two wrongs. Overflow names
    (`Cipher-1a2b`) keep their stem.
    """
    return name in NAMES or name.split("-")[0] in NAMES


def legacy_key(slug: str) -> str:
    """The `LEGACY_SLUGS` key for a slug: sixteen hex of SHA-256 over it."""
    return hashlib.sha256((slug or "").encode("utf-8")).hexdigest()[:16]


def quote_for(slug: str) -> str:
    """The one-line quote a large portrait of `slug` carries; `""` for a
    slug off the roster."""
    return QUOTES.get(slug or "", "")


def current_crew(faces: dict) -> dict:
    """A recorded crew with each retired character slug replaced by its
    successor (`LEGACY_SLUGS`). Unknown values pass through; key order is
    kept. Pure: the stored record is never touched."""
    return {stage: (LEGACY_SLUGS.get(legacy_key(face), face)
                    if isinstance(face, str) else face)
            for stage, face in faces.items()}


def proposed_index(session_id: str) -> int:
    """Stable pool index for a session. Same id, same answer, forever — across
    processes, machines and Python versions."""
    digest = hashlib.sha1(session_id.encode("utf-8", "replace")).digest()
    return int.from_bytes(digest[:8], "big") % len(NAMES)


class IdentityStore:
    """Nickname assignment and its persistence.

    One map, `sessions`, because the character belongs to the agent. It is kept
    on disk so a daemon restart does not reshuffle names under running agents —
    that, and nothing else, is what persistence buys here.
    """

    def __init__(self, path: Optional[Path] = None):
        # Resolved at call time, not bound as a default at import time: a
        # default argument freezes `IDENTITY_PATH` into the signature, so a
        # test that redirects the module constant still writes the real
        # ~/.dark-army/identities.json — which is exactly what the suite
        # was doing, clobbering a running fleet's nicknames mid-run.
        self._path = Path(path) if path is not None else IDENTITY_PATH
        self._sessions: dict[str, str] = {}
        self._dirty = False
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                      # absent or corrupt: start from the hash
        if not isinstance(raw, dict):
            return
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            self._sessions = {str(k): str(v) for k, v in sessions.items()
                              if isinstance(v, str) and v and _in_cast(v)}

    def save(self) -> None:
        """Atomic, and only when something changed — this is called from the
        snapshot path, which runs every few seconds."""
        if not self._dirty:
            return
        if len(self._sessions) > MAX_SESSION_ENTRIES:
            drop = len(self._sessions) - MAX_SESSION_ENTRIES
            for sid in list(self._sessions)[:drop]:
                del self._sessions[sid]
        ensure_state_dir()
        tmp = self._path.with_suffix(".json.tmp")
        # The encoding is named at both ends. `write_text` defaults to the
        # locale, and an .app launched at login inherits no `LANG` — ASCII —
        # so the first `Ptyś` handed out raised UnicodeEncodeError out of
        # the agents-snapshot push, which then failed on every tick until a
        # restart: no rows, no reconcile, no bind, no bind-window expiry. A
        # nickname file must never take the fleet down, so the except is
        # widened to UnicodeError as well.
        try:
            tmp.write_text(json.dumps(
                {"version": 2, "sessions": self._sessions},
                indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
            self._dirty = False
        except (OSError, UnicodeError) as exc:
            logger.warning("Could not save identities: %s", exc)
            try:
                tmp.unlink()
            except OSError:
                pass

    # ── assignment ───────────────────────────────────────────────────────────
    def name_for(self, session_id: str, taken: Iterable[str] = (),
                 preferred: str = "") -> str:
        """The nickname for one agent. `taken` is the set of names held by
        *other live* agents this tick — collisions are resolved against what is
        on screen now, never against history, so a name freed by a finished
        session becomes available again immediately.

        `preferred` is a caller's opinion about who this session *should* be —
        the character of the role Dark Army dispatched it for. It wins the first pick
        when it is a member of the cast and nothing on screen holds it or its
        stem. Anything else about it — empty, off-cast (`overwatch`), already
        taken, or a session that already has a name — falls straight through to
        the hash walk below, so a session Dark Army did not dispatch is named exactly
        as it always was.
        """
        if not session_id:
            return ""
        existing = self._sessions.get(session_id)
        if existing:
            return existing              # sticky: never rename a live row

        held = {_stem(n) for n in taken}
        wanted = ""
        for candidate in NAMES:
            if candidate.lower() == (preferred or "").strip().lower():
                wanted = candidate
                break
        if wanted and wanted not in held and _stem(wanted) not in held:
            self._sessions[session_id] = wanted
            self._dirty = True
            return wanted
        # The main cast first, so a machine running a handful of agents never
        # meets a name it has to think about; the deep cuts are reached only
        # once those are all on screen at once.
        start = proposed_index(session_id) % PRIMARY_CAST
        name = ""
        for step in range(PRIMARY_CAST):
            candidate = NAMES[(start + step) % PRIMARY_CAST]
            if candidate not in held:
                name = candidate
                break
        if not name:
            for candidate in NAMES[PRIMARY_CAST:]:
                if candidate not in held:
                    name = candidate
                    break
        if not name:
            # Every name in the cast is on screen at once. Vanishingly unlikely
            # with 18 of them, but a duplicate row is worse than an ugly one.
            # Four hex of sha1(id), not id[:4]: every Codex id starts `codex:`.
            digest = hashlib.sha1(
                session_id.encode("utf-8", "replace")).hexdigest()
            name = f"{NAMES[start]}-{digest[:4]}"

        self._sessions[session_id] = name
        self._dirty = True
        return name

    def reassign(self, session_id: str, name: str) -> bool:
        """Correct one already-assigned session's nickname. The single
        exception to stickiness, stated in the module docstring.

        Returns False — changing nothing — unless the session already holds a
        name, `name` is one this build can draw, and it differs from what the
        session holds. The disk write is not done here: `_dirty` is set and the
        next `save()` on the snapshot path carries it, which is the same
        rhythm `name_for` uses.
        """
        if not session_id or not name:
            return False
        current = self._sessions.get(session_id)
        if not current or current == name or not _in_cast(name):
            return False
        self._sessions[session_id] = name
        self._dirty = True
        return True

    def touch(self, session_id: str) -> None:
        """Mark an assignment as still in use, for the prune in `save`.

        Insertion order is this map's age proxy, so without this the oldest
        *entry* is dropped rather than the oldest *agent* — and the entry a
        long-running session took at 9am is the first to go, renaming the one
        row that has been on screen all day. Deliberately does not set
        `_dirty`: the order only matters at prune time, and a write per
        snapshot for a map that has not changed is the habit this file's own
        `save` docstring exists to prevent.
        """
        if session_id in self._sessions:
            self._sessions[session_id] = self._sessions.pop(session_id)

    def forget(self, session_id: str) -> None:
        """Release an agent's name so the cast can reuse it."""
        if self._sessions.pop(session_id, None) is not None:
            self._dirty = True

    def assigned(self) -> dict[str, str]:
        return dict(self._sessions)

    def peek(self, session_id: str) -> str:
        """The nickname a session would wear, without assigning one.

        A board-read path that called `name_for` would write an entry for every
        dead session that ever filed a card, which is the opposite of the prune
        this store exists to do. Hit in the map — including overflow names —
        is returned as stored. A miss is the hash pick, never persisted, never
        `_dirty`. Empty id is no one.
        """
        if not session_id:
            return ""
        existing = self._sessions.get(session_id)
        if existing:
            return existing
        return NAMES[proposed_index(session_id)]


__all__ = ["IdentityStore", "IDENTITY_PATH", "NAMES", "ART_ONLY", "PRIMARY_CAST",
           "QUOTES", "LEGACY_SLUGS", "legacy_key", "quote_for", "current_crew",
           "proposed_index", "MAX_SESSION_ENTRIES"]
