"""The per-model weekly window, read live from Anthropic rather than from a cache.

``limits.py`` draws Claude's bars from files on this machine, and for the two
account-wide windows that is enough — the statusline reports them on every turn,
seconds old. The **scoped** window (``weekly_scoped``, "Current week (Fable)")
has no such source: its only local origin is ``~/.claude.json`` →
``cachedUsageUtilization``, which a current Claude Code writes rarely and an
older one wrote hours ago. Measured 2026-09-01: the two account-wide bars were
seconds old and the scoped bar was **15.6 hours** old, drawn beside them as if
it were live.

So this module asks the source. ``GET https://api.anthropic.com/api/oauth/usage``
with the OAuth access token Claude Code already keeps in the login keychain
(generic password, service ``Claude Code-credentials``), and the
``anthropic-beta: oauth-2025-04-20`` header the same client sends. Wire contract
confirmed by hand on 2026-09-01: a ``limits[]`` list whose entries carry ``kind``,
``group``, ``percent`` (0–100), ``severity``, ``resets_at`` (ISO), ``is_active``
and, for the scoped entry, ``scope.model.display_name``.

**This reverses a decision this codebase recorded.** ``limits.py``'s docstring
and two earlier notes said reading a login credential to redraw a progress bar
was a trade not worth making. It was the right call while the cached figure was
warm; it stopped being one when the figure went hours cold while presenting as
current. The reversal is deliberate and narrow, and these are its edges:

* **Read-only, one key.** ``claudeAiOauth.accessToken`` and nothing else. The
  refresh token is never read, credentials are never written, refreshed or
  invalidated, and there is no environment or config fallback — a 401 is an
  ordinary failure that leaves Claude's own login alone.
* **Never near a door.** ``/api/usage`` is ungated on loopback and is served on
  the LAN door and through the relay. Bars leave here carrying keys from
  ``BAR_KEYS`` and nothing else, and no log line in this module ever contains
  the token, a header, a URL parameter or a response body — only a category word
  and, for HTTP, a status code.
* **Never persisted.** The cache is in memory. A restart falls back to whatever
  ``limits.cached_bars()`` still holds, which is today's behaviour. Grok and
  Codex write ``usage_hold`` because they have no local fallback at all; Claude
  has one, and a second persisted shape to duplicate it would be a
  forward-compatibility burden bought for nothing.
* **Never on the loop.** ``get_snapshot`` blocks on ``/usr/bin/security`` and on
  HTTPS. Its only caller is ``ApiServer._usage_report_for.collect()``, which
  already runs under ``run_in_executor``.

Freshening, not replacement: an unavailable or failed fetch leaves the local
snapshot exactly as it is. ``merge_snapshot`` never removes a bar.
"""
from __future__ import annotations

import getpass
import json
import logging
import math
import subprocess
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Optional

from . import limits

logger = logging.getLogger("dark-army.claude-usage")

# The login keychain's generic-password service, as Claude Code names it. The
# account is the current user; both go in as argv elements, never through a
# shell.
KEYCHAIN_SERVICE = "Claude Code-credentials"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"

# One outbound request per five minutes at most while it is working, one per
# minute while it is not. The panel's usage poll and the phone's poll routinely
# land together; the TTL and the single flight below are what keep that from
# being two requests.
SUCCESS_TTL_SECONDS = 300.0
FAILURE_RETRY_SECONDS = 60.0
KEYCHAIN_TIMEOUT_SECONDS = 5.0
FETCH_TIMEOUT_SECONDS = 10.0

# The real response is ~2 KB. A megabyte is generous and still bounded: the read
# happens before `json.loads`, so a hostile or broken endpoint cannot make the
# daemon buffer the whole of whatever it feels like sending.
MAX_BODY_BYTES = 1_000_000

# Exactly the keys a bar may leave this module with. An allow-list rather than a
# deny-list because of where these end up: `/api/usage` is ungated on loopback
# and is served on the LAN door and through the relay, so a field copied through
# by accident is a field published to the network. Adding one here is a
# deliberate act with a test beside it.
BAR_KEYS = frozenset(
    {"kind", "group", "title", "percent", "resets_at", "severity", "source",
     "as_of", "stale"}
)

# Where a bar from here says it came from. `limits.cached_bars` says "cache" and
# `merge_bars` says "statusline"; a reader comparing two figures deserves to
# know which of the three they are looking at.
SOURCE = "oauth"

# The fallback shape, for a response that carries no `limits[]` at all: one
# fractional utilization under this key. Never seen on this machine — the
# confirmed contract is `limits[]` — but it is the shape the earlier
# investigation recorded, and a parser that handles both costs one branch.
_FALLBACK_KEY = "seven_day_overage_included"
_FALLBACK_TITLE = "Current week (Fable)"

_lock = threading.Lock()
_cache: dict = {
    # The last good reading, and the wall clock it was read at.
    "bars": [],
    "fetched_at": None,
    # `time.monotonic()` of that reading — clock-change proof, which `fetched_at`
    # is not, and `fetched_at` is published while this is not.
    "at": None,
    # Monotonic instant before which a failure holds off the next attempt.
    "hold_until": 0.0,
    # One flight at a time. A caller arriving during a refresh takes the last
    # good reading immediately rather than queueing behind the network.
    "inflight": False,
}


def reset_cache() -> None:
    """Forget the reading and both holds. Tests only — mirrors `grok_billing`."""
    with _lock:
        _cache["bars"] = []
        _cache["fetched_at"] = None
        _cache["at"] = None
        _cache["hold_until"] = 0.0
        _cache["inflight"] = False


def _read_access_token() -> Optional[str]:
    """The OAuth access token from the login keychain, or None.

    An argv vector at an absolute path — no shell, so nothing in the username or
    the service name is ever interpreted. Every failure returns None and logs a
    category word: the subprocess output is the credential, so it may not be
    logged even on the error path, and `exc_info` is not used here for the same
    reason."""
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-w",
             "-a", getpass.getuser(), "-s", KEYCHAIN_SERVICE],
            capture_output=True, text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        logger.debug("keychain read timed out")
        return None
    except (OSError, ValueError):
        logger.debug("keychain read failed to run")
        return None

    if result.returncode != 0:
        # Includes the denied-consent case, which is an ordinary failure: the
        # held bar stays and the retry hold is armed.
        logger.debug("keychain read refused (rc=%s)", result.returncode)
        return None

    try:
        payload = json.loads(result.stdout or "")
    except (json.JSONDecodeError, TypeError):
        logger.debug("keychain entry is not JSON")
        return None
    if not isinstance(payload, dict):
        logger.debug("keychain entry is not an object")
        return None

    oauth = payload.get("claudeAiOauth")
    oauth = oauth if isinstance(oauth, dict) else {}
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        logger.debug("keychain entry carries no access token")
        return None
    return token


def _fetch_usage(token: str) -> Optional[dict]:
    """The usage payload, or None.

    Default TLS context, so certificates are verified. The token lives in this
    frame's locals and in the request headers; nothing here logs a header, a
    body, or the URL with anything appended to it."""
    request = urllib.request.Request(
        USAGE_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            # One byte over the cap is enough to know it was over.
            raw = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # 401 included, and deliberately not special: an expired token is a
        # plain failure until Claude Code refreshes itself. We never do.
        logger.debug("usage fetch refused (status=%s)", exc.code)
        return None
    except urllib.error.URLError:
        logger.debug("usage fetch could not connect")
        return None
    except (OSError, ValueError):
        logger.debug("usage fetch failed")
        return None

    if len(raw) > MAX_BODY_BYTES:
        logger.debug("usage response over %d bytes", MAX_BODY_BYTES)
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("usage response is not JSON")
        return None
    return payload if isinstance(payload, dict) else None


def _percent(value) -> Optional[float]:
    """A percentage in 0–100, or None. `limits._num` rejects bools and NaN/inf;
    the range check is this module's own, because a figure outside it is a shape
    we did not confirm rather than a number to be clamped."""
    number = limits._num(value)
    if number is None or not (0.0 <= number <= 100.0):
        return None
    return float(number)


def _bar(entry: dict, title: str, percent: float, now: float) -> dict:
    """One bar, carrying `BAR_KEYS` and nothing else.

    `as_of` and `stale` are stamped here rather than by `limits.mark_freshness`:
    these bars are merged in *after* the local pipeline has run, so nothing
    downstream would ever stamp them."""
    resets = limits._iso_to_epoch(entry.get("resets_at"))
    return {
        "kind": "weekly_scoped",
        "group": "weekly",
        "title": title,
        "percent": percent,
        "resets_at": resets,
        "severity": str(entry.get("severity") or "normal"),
        "source": SOURCE,
        "as_of": now,
        "stale": bool(resets is not None and resets <= now),
    }


def _parse_scoped_bars(payload: dict, now: float) -> list[dict]:
    """Every per-model weekly window in the response, as bars.

    Primary shape: the `limits[]` entries whose `kind` is `weekly_scoped`, titled
    through `limits._title_for` so they come out identical to the cached bar they
    replace. Two entries for one title — the endpoint reporting the same model
    twice — resolve to the `is_active` one.

    Fallback shape, used only when `limits[]` names no scoped window at all: a
    single fractional `utilization`, which is a **fraction** where `percent` is
    0–100. Multiplying the wrong one by a hundred is the mistake this split
    exists to make impossible to write by accident."""
    if not isinstance(payload, dict):
        return []

    entries = payload.get("limits")
    picked: dict[str, tuple[bool, dict]] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        percent = _percent(entry.get("percent"))
        if percent is None:
            continue
        title = limits._title_for(entry)
        active = entry.get("is_active") is True
        # First entry wins unless a later one is the active window. Same title,
        # two windows: the one being spent is the one on screen.
        if title in picked and not (active and not picked[title][0]):
            continue
        picked[title] = (active, _bar(entry, title, percent, now))

    if picked:
        return [bar for _, bar in picked.values()]

    fallback = payload.get(_FALLBACK_KEY)
    if isinstance(fallback, dict):
        fraction = limits._num(fallback.get("utilization"))
        if fraction is not None and 0.0 <= fraction <= 1.0:
            return [_bar(fallback, _FALLBACK_TITLE, float(fraction) * 100.0, now)]
    return []


def _held(bars: list, fetched_at: Optional[float], now: float) -> dict:
    """The last good reading, restaled against *now*.

    A bar this module read forty minutes ago may have crossed its own reset in
    the meantime; `stale` is a statement about the window, not about the read, so
    it is recomputed on every return rather than frozen at fetch time."""
    if not bars:
        return {}
    out = []
    for bar in bars:
        resets = limits._num(bar.get("resets_at"))
        out.append(dict(bar, stale=bool(resets is not None and resets <= now)))
    return {"bars": out, "fetched_at": fetched_at}


def get_snapshot(*, now: Optional[float] = None,
                 fetch=None, read_token=None) -> dict:
    """`{"bars": [...], "fetched_at": epoch}`, or `{}` before the first success.

    Blocks on the keychain and on HTTPS, so it belongs on the executor and
    nowhere else. The module lock is held only across the cache reads and writes
    — never across either piece of I/O — so a second caller arriving mid-flight
    takes the last good reading immediately instead of waiting for the network.

    `now`, `fetch` and `read_token` are injection points for the tests; nothing
    in the daemon passes them."""
    wall = time.time() if now is None else float(now)
    mono = time.monotonic()

    with _lock:
        bars = list(_cache["bars"])
        fetched_at = _cache["fetched_at"]
        at = _cache["at"]
        recent = at is not None and (mono - at) < SUCCESS_TTL_SECONDS
        holding = mono < _cache["hold_until"]
        if _cache["inflight"] or recent or holding:
            return _held(bars, fetched_at, wall)
        _cache["inflight"] = True

    try:
        reader = read_token or _read_access_token
        fetcher = fetch or _fetch_usage
        token = reader()
        payload = fetcher(token) if token else None
        fresh = _parse_scoped_bars(payload, wall) if payload else []
        with _lock:
            if fresh:
                _cache["bars"] = fresh
                _cache["fetched_at"] = wall
                _cache["at"] = mono
                _cache["hold_until"] = 0.0
                return _held(fresh, wall, wall)
            # A reachable endpoint that named no scoped window is a failure like
            # any other here: it arms the retry hold and changes nothing on
            # screen. It is not a reason to drop the figure we hold.
            _cache["hold_until"] = time.monotonic() + FAILURE_RETRY_SECONDS
            return _held(list(_cache["bars"]), _cache["fetched_at"], wall)
    finally:
        with _lock:
            _cache["inflight"] = False


def merge_snapshot(local: dict, live: dict) -> dict:
    """`limits.snapshot()`'s output, freshened with whatever the fetch got.

    Pure — no I/O, no clock, both arguments left untouched. Three properties, and
    each of them is the point:

    * **Nothing is ever removed.** A live bar replaces the local scoped bar of
      the same title, in place, keeping the local bar's `promos` — the promo
      lines were attached by `attach_promos` and describe the window, not the
      reading. A live bar matching no local bar is appended. Local bars the fetch
      did not name — `session`, `weekly_all`, a second scoped window from an old
      cache — survive verbatim.
    * **An empty `live` changes nothing.** That is every failure path, and it
      lands on exactly the bars the user saw before this feature existed.
    * **Everything else is the local snapshot's.** `notices`, `fetched_at` and
      the bar order are untouched; `available` only ever gains truth, because a
      snapshot with bars in it is available by definition."""
    merged = deepcopy(local) if isinstance(local, dict) else {}
    bars = merged.get("bars")
    bars = list(bars) if isinstance(bars, list) else []

    fresh = (live or {}).get("bars") if isinstance(live, dict) else None
    for bar in fresh if isinstance(fresh, list) else []:
        if not isinstance(bar, dict):
            continue
        title = bar.get("title")
        for index, existing in enumerate(bars):
            if (isinstance(existing, dict)
                    and existing.get("kind") == "weekly_scoped"
                    and existing.get("title") == title):
                replacement = dict(bar)
                promos = existing.get("promos")
                if promos is not None:
                    replacement["promos"] = deepcopy(promos)
                bars[index] = replacement
                break
        else:
            bars.append(dict(bar))

    merged["bars"] = bars
    merged["available"] = bool(merged.get("available")) or bool(bars)
    return merged
