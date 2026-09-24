"""How a card's run is going, decided once here and drawn verbatim everywhere.

The decision half of the run-health line: a size class for the run (turns
and tokens against the project's own *finished* runs — typical / large /
worrying), how many times the session asked, how many refusals it hit, how
full its context is, and two counts off the store's ledgers — attempts (one
per dispatch of the card) and returns (rework episodes) — plus the fix rounds
the implementer needed, read off the transcript's per-spawn list.

Everything is composed from data the daemon already has. **Nothing here reads
the daemon's two deduped-by-type helper lists** (the session's seen set and
the card's recorded trail): neither can count a second `bc-implementer`
spawn. `SessionStats.agents` is one entry per spawned agent id, and
`stats_to_dict` publishes it as `spawn_counts`; that is the only observed
count of fix rounds. Codex and Grok keep no
per-spawn record (`NO_SPAWN_LIST_PROVIDERS`), so their rows' `fix_rounds` is
**absent**, never zero — zero would say "no fix rounds" about a run nobody
counted.

**The published figures are quantised before the dict is built.** A changed
`board` section rides every `?sections=changed` frame, and a per-turn figure
would make the board news on every assistant message of every bound session.
`quantise_turns` / `quantise_tokens_k` / `quantise_ctx` are that mitigation,
and `test_run_health.py` pins that two rows one turn apart compose equal.

The `Ledger` is the sidecar where a finished run's last reading is written
down (`run-health.json`, `paths._PRIVATE_FILES`): a finished card keeps its
line after its session's tombstone is gone, and the frozen readings of the
same root are the yardstick the next run is judged against. A run joins the
pool only after it ends, so a live run never moves its own cut.

No sqlite, no daemon import, no clock but `time.time()` for the ledger's age.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from pathlib import Path
from typing import Optional

from . import paths

logger = logging.getLogger("dark-army.run-health")

#: How many finished runs of a root it takes before the root's own medians
#: replace the defaults as the yardstick.
MIN_BASELINE_RUNS = 5
#: `(turns, tokens)` cuts a run is judged against until the project has
#: `MIN_BASELINE_RUNS` finished readings. A stated starting point, not a
#: measurement: the project's own runs replace it at five.
DEFAULT_TYPICAL = (40, 1_500_000)
DEFAULT_LARGE = (120, 5_000_000)
#: With a project baseline, the cuts are these multiples of the medians.
TYPICAL_RATIO = 1.5
LARGE_RATIO = 3.0
#: A context this full turns the line amber on its own.
CTX_WORRY_PCT = 90
#: So do this many fix rounds.
FIX_ROUNDS_WORRY = 3
#: The stage whose repeated spawns are fix rounds.
IMPLEMENTER_STAGE = "bc-implementer"
#: Providers whose row carries no transcript-backed spawn list: Codex keeps
#: no per-spawn record and Grok's history names no helper spawns, so both
#: publish `spawn_counts: {}` whatever ran. An empty count is not a count
#: of nothing, so `fix_rounds` is withheld for them rather than read as 0.
NO_SPAWN_LIST_PROVIDERS = frozenset({"codex", "grok"})
#: The ledger keeps the newest this many readings, no older than this.
MAX_LEDGER = 500
LEDGER_DAYS = 90

#: The keys `compose` publishes, and no other — pinned exactly by the tests
#: so a money or a time figure (the sibling plan's) can never slip in here.
PUBLISHED_KEYS = frozenset({
    "class", "basis", "turns", "tokens_k", "ctx_pct", "asks", "refusals",
    "attempts", "returns", "fix_rounds", "attention", "live",
})
#: The three loop-side counters a session state carries, copied onto the
#: stub by `_collect_agent_stubs` and published on the row beside `stats`.
COUNTER_KEYS = ("permission_asks", "permission_denied", "stop_failures")

CLASSES = ("typical", "large", "worrying")


# ── the figures ──────────────────────────────────────────────────────────────

def _int_or_none(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def tokens_of(stats: Optional[dict]) -> Optional[int]:
    """Tokens the run *spent*: fresh input plus output, never cache reads —
    a cached context re-read is the same context, not more work. None where
    the stats dict names neither."""
    if not isinstance(stats, dict):
        return None
    inp = _int_or_none(stats.get("total_input_tokens"))
    out = _int_or_none(stats.get("output_tokens"))
    if inp is None and out is None:
        return None
    return max(0, inp or 0) + max(0, out or 0)


def quantise_turns(turns: Optional[int]) -> Optional[int]:
    """Exact up to 20, then floored to a multiple of 5."""
    n = _int_or_none(turns)
    if n is None:
        return None
    n = max(0, n)
    return n if n <= 20 else (n // 5) * 5


def quantise_tokens_k(tokens: Optional[int]) -> Optional[int]:
    """Thousands, floored to two significant figures: 999,499 → 990,
    2,140,000 → 2100. Under ten thousand the figure is exact."""
    n = _int_or_none(tokens)
    if n is None:
        return None
    k = max(0, n) // 1000
    if k < 10:
        return k
    magnitude = 10 ** (len(str(k)) - 2)
    return (k // magnitude) * magnitude


def quantise_ctx(pct) -> Optional[int]:
    """Floored to a multiple of 5, clamped to 0..100."""
    n = _int_or_none(pct)
    if n is None:
        return None
    return (min(100, max(0, n)) // 5) * 5


# ── the yardstick ────────────────────────────────────────────────────────────

def _reading_figures(record: dict) -> Optional[tuple]:
    """`(turns, tokens)` off one ledger record, or None where it has no
    turns. Tokens are the reading's own `tokens_k`, back in tokens."""
    if not isinstance(record, dict):
        return None
    reading = record.get("reading")
    if not isinstance(reading, dict):
        return None
    turns = _int_or_none(reading.get("turns"))
    if turns is None:
        return None
    tokens_k = _int_or_none(reading.get("tokens_k"))
    return turns, (tokens_k or 0) * 1000


def baseline(records, root: str) -> tuple:
    """`(basis, typical_cut, large_cut)` for one root.

    `basis` is `"project"` once the root has `MIN_BASELINE_RUNS` frozen
    readings with a turn count — the cuts are then `TYPICAL_RATIO` /
    `LARGE_RATIO` times the medians of turns and tokens — and `"default"`
    before that, with the two default cut pairs. Records of another root are
    ignored: a busy project must not set the bar for a quiet one.
    """
    figures = []
    for record in records or ():
        if not isinstance(record, dict) or str(record.get("root") or "") != root:
            continue
        pair = _reading_figures(record)
        if pair is not None:
            figures.append(pair)
    if len(figures) < MIN_BASELINE_RUNS:
        return "default", DEFAULT_TYPICAL, DEFAULT_LARGE
    med_turns = statistics.median(f[0] for f in figures)
    med_tokens = statistics.median(f[1] for f in figures)
    typical = (int(med_turns * TYPICAL_RATIO), int(med_tokens * TYPICAL_RATIO))
    large = (int(med_turns * LARGE_RATIO), int(med_tokens * LARGE_RATIO))
    return "project", typical, large


def classify(turns: Optional[int], tokens: Optional[int], cuts) -> str:
    """`"typical"` / `"large"` / `"worrying"`, or `""` where `turns` is
    unknown. `cuts` is `(typical_cut, large_cut)`, each `(turns, tokens)`;
    a run over either half of a cut is past it, and an unknown token count
    is judged on turns alone."""
    if turns is None:
        return ""
    typical_cut, large_cut = cuts[0], cuts[1]

    def past(cut) -> bool:
        if turns > int(cut[0]):
            return True
        return tokens is not None and tokens > int(cut[1])

    if past(large_cut):
        return "worrying"
    if past(typical_cut):
        return "large"
    return "typical"


def fix_rounds(spawn_counts: Optional[dict]) -> Optional[int]:
    """Implementer spawns after the first. None where there is no spawn
    list at all — a provider that keeps none, or an older row — because a
    zero there would be a count nobody took."""
    if not isinstance(spawn_counts, dict):
        return None
    return max(0, (_int_or_none(spawn_counts.get(IMPLEMENTER_STAGE)) or 0) - 1)


def attention(size_class: str, ctx_pct: Optional[int],
              rounds: Optional[int]) -> bool:
    """Whether the line turns amber: a worrying run, a context at or past
    `CTX_WORRY_PCT`, or `FIX_ROUNDS_WORRY` fix rounds. The word is always
    drawn first; this is never the only signal."""
    if size_class == "worrying":
        return True
    if ctx_pct is not None and ctx_pct >= CTX_WORRY_PCT:
        return True
    return rounds is not None and rounds >= FIX_ROUNDS_WORRY


def _counts(counts: Optional[dict]) -> tuple:
    counts = counts if isinstance(counts, dict) else {}
    return (max(0, _int_or_none(counts.get("attempts")) or 0),
            max(0, _int_or_none(counts.get("returns")) or 0))


def compose(row: Optional[dict], counts: Optional[dict],
            ledger_entry: Optional[dict], cuts) -> Optional[dict]:
    """The published dict, or None where there is neither a row nor a
    frozen reading — the absent case, which the decoration leaves off the
    card.

    `row` is the snapshot row the rail draws (`stats`, `metrics`,
    `spawn_counts` inside `stats`, the three counters); `counts` is the
    store's `{"attempts", "returns"}`; `ledger_entry` a frozen record; `cuts`
    the `baseline()` triple. `turns`, `tokens_k`, `ctx_pct` and `fix_rounds`
    are **omitted** where unknown, never zero-filled. `live` says whether the
    reading came off a row on this frame rather than the ledger. Attempts and
    returns are always the store's current figures — a card sent back after
    its run froze still shows the return.
    """
    attempts, returns = _counts(counts)
    if isinstance(row, dict):
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        turns = quantise_turns(stats.get("assistant_messages")) if stats else None
        raw_turns = _int_or_none(stats.get("assistant_messages")) if stats else None
        raw_tokens = tokens_of(stats)
        basis, typical_cut, large_cut = cuts[0], cuts[1], cuts[2]
        size_class = classify(raw_turns, raw_tokens, (typical_cut, large_cut))
        tool_counts = stats.get("tool_counts") if isinstance(
            stats.get("tool_counts"), dict) else {}
        asks = ((_int_or_none(tool_counts.get("AskUserQuestion")) or 0)
                + (_int_or_none(row.get("permission_asks")) or 0))
        refusals = ((_int_or_none(row.get("permission_denied")) or 0)
                    + (_int_or_none(row.get("stop_failures")) or 0))
        # Codex and Grok keep no per-spawn list: `stats.agents` is empty
        # for them whatever ran, and an empty count is not a count of
        # nothing.
        provider = str(row.get("provider") or "")
        rounds = None if provider in NO_SPAWN_LIST_PROVIDERS else fix_rounds(
            stats.get("spawn_counts") if stats else None)
        ctx = quantise_ctx(metrics.get("ctx_used_pct")) if metrics else None
        out = {
            "class": size_class,
            "basis": str(basis or "default"),
            "asks": max(0, asks),
            "refusals": max(0, refusals),
            "attempts": attempts,
            "returns": returns,
            "attention": attention(size_class, ctx, rounds),
            "live": True,
        }
        if turns is not None:
            out["turns"] = turns
        tokens_k = quantise_tokens_k(raw_tokens)
        if tokens_k is not None:
            out["tokens_k"] = tokens_k
        if ctx is not None:
            out["ctx_pct"] = ctx
        if rounds is not None:
            out["fix_rounds"] = rounds
        return out
    if isinstance(ledger_entry, dict) and isinstance(
            ledger_entry.get("reading"), dict):
        out = _typed_reading(ledger_entry["reading"])
        out["attempts"] = attempts
        out["returns"] = returns
        out["live"] = False
        out.setdefault("class", "")
        out.setdefault("basis", "default")
        out.setdefault("asks", 0)
        out.setdefault("refusals", 0)
        out.setdefault("attention", False)
        return out
    return None


# ── the ledger ───────────────────────────────────────────────────────────────

#: `{path: ((mtime_ns, size), Ledger)}` — the enrolment ledger's memo: one
#: `stat` per frame on a quiet machine, a read and a parse only when the
#: file has moved.
_cache: dict = {}

# The shape a frozen reading is re-typed through when it comes back off
# disk: the file is 0600 in a 0700 directory, but a hand edit (or a
# same-user process) can still put a string where a number goes, and the
# board section must never carry a value the clients did not agree on.
_READING_TYPES = {
    "class": str, "basis": str, "turns": int, "tokens_k": int,
    "ctx_pct": int, "asks": int, "refusals": int, "fix_rounds": int,
    "attention": bool,
}


def _typed_reading(reading: dict) -> dict:
    """The published keys of a stored reading, each coerced to its type;
    a value that will not coerce is dropped (an absent key is what the
    clients already tolerate, a wrong-typed one is not)."""
    out: dict = {}
    for key, kind in _READING_TYPES.items():
        if key not in reading:
            continue
        value = reading[key]
        try:
            if kind is bool:
                out[key] = bool(value)
            elif kind is int:
                if isinstance(value, bool) or isinstance(value, (list, dict)):
                    continue
                out[key] = int(value)
            else:
                out[key] = str(value)
        except (TypeError, ValueError):
            continue
    return out


def _clean_entry(entry) -> Optional[dict]:
    """One ledger entry as the ledger is willing to hold it, or `None`.
    `at` and `dispatched_at` are floats, `reading` is a dict, ids are
    strings; anything else is a corrupt entry and reads as absent, so a
    bad field can never take `Ledger.load()` — and the board with it —
    down."""
    if not isinstance(entry, dict) or not isinstance(entry.get("reading"), dict):
        return None
    try:
        at = float(entry.get("at") or 0.0)
        run_at = float(entry.get("dispatched_at") or 0.0)
    except (TypeError, ValueError):
        return None
    cid = entry.get("card_id")
    if not isinstance(cid, str) or not cid:
        return None
    root = entry.get("root")
    return {
        "card_id": cid, "root": root if isinstance(root, str) else "",
        "at": at, "dispatched_at": run_at,
        "reading": _typed_reading(entry["reading"]),
    }


def _revision(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _reading_newer(new: dict, old) -> bool:
    """Whether `new` supersedes `old` for the same dispatch: not the same
    reading, and neither turns nor tokens have gone backwards (an absent
    figure counts as 0, so a reading that gained one is newer)."""
    if not isinstance(old, dict):
        return True
    if new == old:
        return False
    for key in ("turns", "tokens_k"):
        if (_int_or_none(new.get(key)) or 0) < (_int_or_none(old.get(key)) or 0):
            return False
    return True


class Ledger:
    """The frozen readings, one per card (the latest run replaces the
    previous), newest last. Bounded at `MAX_LEDGER` entries and
    `LEDGER_DAYS`; a corrupt or missing file reads as empty; written with
    `paths.atomic_write_json`. A deleted card leaves a harmless entry that
    ages out."""

    def __init__(self, entries: Optional[list] = None,
                 path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else None
        self.entries: list = [e for e in (entries or [])
                              if isinstance(e, dict) and e.get("card_id")]
        self._cuts: dict = {}

    @classmethod
    def load(cls, path=None) -> "Ledger":
        """The ledger at `path` (default `paths.RUN_HEALTH_PATH`), memoised
        on the file's `(mtime_ns, size)`."""
        path = Path(path) if path is not None else paths.RUN_HEALTH_PATH
        rev = _revision(path)
        cached = _cache.get(str(path))
        if cached is not None and cached[0] == rev:
            return cached[1]
        entries: list = []
        if rev is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.debug("run-health ledger unreadable", exc_info=True)
                data = None
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                entries = [e for e in map(_clean_entry, data["entries"])
                           if e is not None]
        ledger = cls(entries, path)
        ledger._prune()
        _cache[str(path)] = (rev, ledger)
        return ledger

    def entry_for(self, card_id: str) -> Optional[dict]:
        cid = str(card_id or "")
        if not cid:
            return None
        for entry in reversed(self.entries):
            if entry.get("card_id") == cid:
                return entry
        return None

    def for_root(self, root: str) -> list:
        root = str(root or "")
        return [e for e in self.entries if str(e.get("root") or "") == root]

    def cuts_for(self, root: str) -> tuple:
        """`baseline()` over this root's readings, memoised per instance —
        the instance is memoised on the file, so one median per root per
        change of the file rather than per card per frame."""
        root = str(root or "")
        # One dict operation, not check-then-index: `freeze` prunes on a
        # sibling executor thread and rebinds `_cuts` between the two.
        cuts = self._cuts
        found = cuts.get(root)
        if found is None:
            found = cuts.setdefault(root, baseline(self.for_root(root), root))
        return found

    def _prune(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        floor = now - LEDGER_DAYS * 86400
        kept = [e for e in self.entries
                if isinstance(e, dict) and float(e.get("at") or 0.0) >= floor]
        kept.sort(key=lambda e: float(e.get("at") or 0.0))
        self.entries = kept[-MAX_LEDGER:]
        self._cuts = {}

    def freeze(self, card_id: str, root: str, reading: dict,
               dispatched_at: float, now: Optional[float] = None) -> bool:
        """Write `reading` down as the final one for this card's run.

        Idempotent on the run **and its reading**: a card whose entry
        already names this `dispatched_at` is rewritten only when the new
        reading is *newer* — its turns and tokens are each at or past the
        frozen ones and something differs — because an `ended → live`
        resume keeps the card's `dispatched_at` (`_reopen_attempt`'s rung)
        and the second quiet spell's reading is the one the card should
        keep. The identical reading writes nothing; a reading that has gone
        *backwards* (fewer turns than the frozen one — not the same
        transcript) is left alone. Returns whether anything was written.
        """
        cid = str(card_id or "")
        if not cid or not isinstance(reading, dict):
            return False
        try:
            run_at = float(dispatched_at or 0.0)
        except (TypeError, ValueError):
            run_at = 0.0
        now = time.time() if now is None else now
        frozen = {k: v for k, v in reading.items() if k in PUBLISHED_KEYS}
        frozen["live"] = False
        existing = self.entry_for(cid)
        if existing is not None and float(existing.get("dispatched_at") or 0.0) == run_at:
            if not _reading_newer(frozen, existing.get("reading")):
                return False
        self.entries = [e for e in self.entries if e.get("card_id") != cid]
        self.entries.append({
            "card_id": cid, "root": str(root or ""), "at": float(now),
            "dispatched_at": run_at, "reading": frozen,
        })
        self._prune(now)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            paths.atomic_write_json(self.path, {"version": 1,
                                                "entries": self.entries})
            _cache[str(self.path)] = (_revision(self.path), self)
        return True


__all__ = [
    "MIN_BASELINE_RUNS", "DEFAULT_TYPICAL", "DEFAULT_LARGE", "TYPICAL_RATIO",
    "LARGE_RATIO", "CTX_WORRY_PCT", "FIX_ROUNDS_WORRY", "IMPLEMENTER_STAGE",
    "MAX_LEDGER", "LEDGER_DAYS", "PUBLISHED_KEYS", "COUNTER_KEYS",
    "NO_SPAWN_LIST_PROVIDERS",
    "tokens_of", "quantise_turns", "quantise_tokens_k", "quantise_ctx",
    "baseline", "classify", "fix_rounds", "attention", "compose", "Ledger",
]
