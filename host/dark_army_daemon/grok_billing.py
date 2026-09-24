"""Grok Build's account-wide usage window, fetched the same way ``/usage`` is.

Live ``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits`` with the
grok.com session token. A failed refresh keeps the last good reading in
memory; a restart loads that same reading from ``usage_hold`` so the chip
does not vanish. The menu bar polls it on a slow timer, never on the strip
tick.

Never logs the token. Failures are empty snapshots, not exceptions: a bar that
cannot be drawn is quieter than one that takes the menu bar down.
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import usage_hold
from .grok_usage import iso_epoch as _iso_epoch

logger = logging.getLogger("dark-army.grok-billing")

AUTH_PATH = Path.home() / ".grok" / "auth.json"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
CLIENT_VERSION_PATH = Path.home() / ".grok" / "version.json"

# Well under the strip tick, well under a weekly window. A 401 or a timeout
# leaves the last good reading in place until the next poll.
TTL_SECONDS = 60.0
FETCH_TIMEOUT = 10.0

_cache: dict = {"at": 0.0, "data": {}}


def _client_version(path: Path = CLIENT_VERSION_PATH) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "1.0.4"
    if isinstance(data, dict):
        for key in ("version", "cli_version"):
            if isinstance(data.get(key), str) and data[key]:
                return data[key]
    return "1.0.4"


def read_token(path: Path = AUTH_PATH) -> str:
    """The grok.com session token, or "". Never logged."""
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(auth, dict):
        return ""
    now = time.time()
    best = ""
    best_expiry = -1.0
    for value in auth.values():
        if not isinstance(value, dict):
            continue
        key = value.get("key")
        if not isinstance(key, str) or not key:
            continue
        expiry = _iso_epoch(value.get("expires_at")) or 0.0
        # Prefer a token that has not expired; among those, the latest expiry.
        if expiry >= now and expiry >= best_expiry:
            best, best_expiry = key, expiry
        elif not best:
            best = key
    return best


def _num(value) -> Optional[float]:
    """A finite usage figure, or None. Bool is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _percent_of(cfg: dict) -> Optional[float]:
    """The fill, from whichever key this payload still carries.

    The field has moved once already (`creditUsagePercent` vs GrokBuild's
    `usagePercent`) and a weekly-limit reset omits it entirely. Walk the
    names we have seen rather than a single path, so a rename degrades to
    the next key instead of a missing bar.
    """
    for key in ("creditUsagePercent", "usagePercent", "usedPercent"):
        percent = _num(cfg.get(key))
        if percent is not None:
            return percent
    products = cfg.get("productUsage")
    if not isinstance(products, list):
        return None
    fallback = None
    for item in products:
        if not isinstance(item, dict):
            continue
        value = _num(item.get("usagePercent"))
        if value is None:
            value = _num(item.get("creditUsagePercent"))
        if value is None:
            continue
        if item.get("product") == "GrokBuild":
            return value
        if fallback is None:
            fallback = value
    return fallback


def _cycle_of(ptype: str) -> str:
    if "WEEKLY" in ptype:
        return "weekly"
    if "MONTHLY" in ptype:
        return "monthly"
    return "period"


def parse_billing(payload: dict, now: Optional[float] = None) -> dict:
    """Reduce the /billing JSON to the fields the menu draws.

    Empty dict if the payload is not a billing config at all — a future
    server that changes the contract should make us go quiet, not invent a
    bar. A *recognised* window that simply omits the fill is different:
    that is what the server does for the first minutes of a period and
    after a weekly-limit reset. The window is open and unused, so the
    honest reading is 0, not "feature broke".
    """
    if not isinstance(payload, dict):
        return {}
    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        return {}

    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    ptype = period.get("type") if isinstance(period.get("type"), str) else ""
    resets_at = _iso_epoch(period.get("end")) or _iso_epoch(cfg.get("billingPeriodEnd"))
    has_period = bool(
        ptype
        or resets_at is not None
        or _iso_epoch(period.get("start")) is not None
        or _iso_epoch(cfg.get("billingPeriodStart")) is not None
    )

    percent = _percent_of(cfg)
    if percent is None:
        if not has_period:
            return {}
        # Reset / start-of-window: period stays, fill is withheld until the
        # first billable turn. Treating that as unknown hid the chip the
        # last time it happened; 0 is what the window actually is.
        logger.info("Grok billing omitted percent; treating open period as 0")
        percent = 0.0

    now = time.time() if now is None else now
    return {
        "percent": float(percent),
        "resets_at": resets_at,
        "cycle": _cycle_of(ptype),
        "stale": bool(resets_at is not None and now >= resets_at),
        "fetched_at": now,
    }


def fetch_billing(
    *,
    auth_path: Optional[Path] = None,
    url: str = BILLING_URL,
    opener=None,
) -> dict:
    """One request. {} on any failure. `opener` is for tests."""
    token = read_token(auth_path if auth_path is not None else AUTH_PATH)
    if not token:
        return {}
    headers = {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-grok-client-version": _client_version(),
        "x-grok-client-identifier": "grok-shell",
        "x-grok-client-mode": "cli-chat-proxy",
        "Accept": "application/json",
    }
    if opener is not None:
        try:
            payload = opener(url, headers)
        except Exception:
            logger.info("Grok billing fetch failed", exc_info=True)
            return {}
        return parse_billing(payload) if isinstance(payload, dict) else {}

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT,
                                    context=ssl.create_default_context()) as resp:
            raw = resp.read()
        payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError, ValueError):
        logger.info("Grok billing fetch failed", exc_info=True)
        return {}
    return parse_billing(payload)


def _held_snapshot(now: float) -> dict:
    """Memory first, then the on-disk last reading. Recomputes `stale`."""
    held = dict(_cache["data"]) if _cache.get("data") else usage_hold.load("grok")
    if not held:
        return {}
    if held.get("resets_at") is not None:
        held["stale"] = now >= held["resets_at"]
    return held


def get_snapshot(*, now: Optional[float] = None, force: bool = False,
                 fetch=None) -> dict:
    """Last good reading, refreshing when the TTL has elapsed.

    A failed refresh keeps the previous reading (and its stale flag). An
    empty memory cache falls back to the on-disk hold, so a restart does
    not blank the chip. Only a machine that has never fetched a figure
    returns ``{}``.
    """
    now = time.time() if now is None else now
    if not _cache.get("data"):
        held = usage_hold.load("grok")
        if held:
            _cache["data"] = dict(held)
            _cache["at"] = float(held.get("fetched_at") or 0.0)
    age = now - float(_cache.get("at") or 0.0)
    if not force and _cache.get("data") and age < TTL_SECONDS:
        return _held_snapshot(now)
    loader = fetch if fetch is not None else fetch_billing
    try:
        fresh = loader()
    except Exception:
        logger.info("Grok billing refresh failed", exc_info=True)
        fresh = {}
    if fresh:
        _cache["at"] = now
        _cache["data"] = fresh
        usage_hold.save("grok", fresh)
        return dict(fresh)
    return _held_snapshot(now)


def reset_cache() -> None:
    """Tests only."""
    _cache["at"] = 0.0
    _cache["data"] = {}
