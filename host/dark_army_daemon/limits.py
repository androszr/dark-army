# host/dark_army_daemon/limits.py
"""The rate-limit bars Claude Code's own ``/usage`` screen draws.

Up to three bars, and the numbers on them come from three places:

* **The statusline**, which the daemon already ingests per session
  (``protocol.flatten_statusline`` → ``five_hour_pct`` / ``seven_day_pct``, each
  with its own ``_resets_at``). Refreshed on every conversation update, so it is
  seconds old, and it is the **primary** source for the two account-wide windows
  — it can draw those bars from nothing.
* **``~/.claude.json`` → ``cachedUsageUtilization``**, Claude Code's cache of
  ``GET /api/oauth/usage``: a percentage and a reset time per limit window, plus
  the per-model weekly window (``weekly_scoped``, e.g. "Current week (Fable)")
  that nothing else on this machine exposes — the only source for the scoped
  bar. **Claude Code 2.1.220 no longer writes this key**, so on a current
  install it is simply absent; older installs still have it, and the scoped bar
  exists only for them.
* **``~/.claude/limits-cache.json``**, where a current Claude Code keeps a much
  thinner record: a reset instant and an overage flag per window, and no
  percentage at all. Gap-filling only — see ``apply_limits_cache``.

That order is a correction, not a preference. The statusline used to be an
*overlay*: ``merge_bars`` would only overwrite a bar the usage cache had already
supplied, so the cache owned the bars' **shape** and the statusline merely their
freshness. When the cache went away the shape went with it, and a Usage view
holding two live percentages drew no bars at all while the header two inches
above it rendered the same numbers correctly. So ``merge_bars`` now *emits* the
account-wide bars when nothing else has, and the module no longer depends on
another program's file format to draw the two figures it is mostly asked for.

Where both speak, the statusline still wins — **including the reset time**. The
two agree only while the cache is warm: a five-hour window rolls over every five
hours, and a cache written before the rollover names a reset instant that has
already passed. Reading "Resets 18:30" at 21:28 is what taking the label from the
older source looks like, so the source that supplies the percentage supplies the
clock that goes with it.

Whatever survives that, ``mark_freshness`` stamps with the instant it was read
at and flags the windows whose reset is already behind us — with nothing live
reporting, a bar can only be a figure from a window that has since reset, and
saying so is the difference between reading a limit and guessing at one.

**No API call happens here, and that is now only half the story.** This module
still reads nothing but files: the statusline reports the two account-wide
figures for free on every turn, and it can draw both bars from nothing. The
refusal that used to cover all three windows was reversed on 2026-09-01 for the
third one only — see `claude_usage.py`, which fetches the **scoped** window
(`weekly_scoped`) live from `GET /api/oauth/usage` with the login keychain's
OAuth token, and merges the result over this module's output afterwards.

The reason the refusal held for the other two and not this one is age. The
account-wide bars are seconds old. The scoped bar has no live source at all —
`cachedUsageUtilization` is the only thing on this machine that names it, and it
was measured 15.6 hours old while sitting on screen beside two fresh figures,
flagged `stale: false` because its reset had not yet passed. A figure that is
wrong and says nothing is worse than a credential read, which is the trade that
changed. Nothing here calls that module; the merge is one line in
`ApiServer._usage_report_for`, and a failed fetch leaves everything below
untouched.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("dark-army.limits")

# Claude Code's global config, beside the projects directory it also owns.
CLAUDE_CONFIG_PATH = Path.home() / ".claude.json"

# ...and the small file a current Claude Code keeps its limit state in instead.
CLAUDE_LIMITS_CACHE_PATH = Path.home() / ".claude" / "limits-cache.json"

# `limits[]` entry kind → the heading /usage gives it. Copied from the source
# screen so the two can be read side by side; an unknown kind falls back to its
# own name rather than being dropped, because a limit we cannot label is still a
# limit the user is being held to.
_BAR_TITLES = {
    "session": "Current session",
    "weekly_all": "Current week (all models)",
    # weekly_scoped is per model and titled from its scope, e.g. "Current week
    # (Fable)" — handled in _title_for.
}

# The `group` the usage cache files each bar under. Only needed for the bars this
# module synthesises itself, so they come out the same shape as read ones.
_BAR_GROUPS = {"session": "session", "weekly_all": "weekly"}

# Which statusline field refreshes which bar. Only the two account-wide windows
# appear in a statusline payload; the scoped bar has no statusline equivalent.
_STATUSLINE_FIELDS = {
    "session": "five_hour_pct",
    "weekly_all": "seven_day_pct",
}

# ...and which one carries that bar's reset instant. A percentage without its own
# clock is how a live figure ends up under a dead label.
_STATUSLINE_RESET_FIELDS = {
    "session": "five_hour_resets_at",
    "weekly_all": "seven_day_resets_at",
}

# Every field one statusline payload contributes here. Taken together or not at
# all — see `pick_live`.
_LIVE_FIELDS = tuple(_STATUSLINE_FIELDS.values()) + tuple(_STATUSLINE_RESET_FIELDS.values())

# Everything except the usage cache names a window in the *rate-limit type*
# vocabulary (`five_hour`, `seven_day`) while the bars themselves come back keyed
# by `kind` (`session`, `weekly_all`). Two spellings of the same window, and this
# is the one place that has to know both — under the other spelling a promo would
# attach to nothing and silently never print, and limits-cache.json would fill
# nothing.
_RATE_LIMIT_BAR_KINDS = {
    "five_hour": "session",
    "seven_day": "weekly_all",
}

# Promo notices additionally name the per-model windows, which are one bar.
_PROMO_BAR_KINDS = {
    **_RATE_LIMIT_BAR_KINDS,
    "seven_day_sonnet": "weekly_scoped",
    "seven_day_opus": "weekly_scoped",
}


def _title_for(entry: dict) -> str:
    kind = entry.get("kind") or ""
    if kind == "weekly_scoped":
        scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        name = model.get("display_name") or ""
        return f"Current week ({name})" if name else "Current week (scoped)"
    return _BAR_TITLES.get(kind, kind.replace("_", " ") or "Limit")


def _num(value) -> Optional[float]:
    """A finite number from the config, or None.

    `math.isfinite` is the load-bearing part. `json.load` parses the non-standard
    `NaN`/`Infinity` literals by default, and NaN *is* a float — so it satisfies
    every `isinstance(x, (int, float))` guard between here and the screen, then
    detonates at the point of use: `menu_format.usage_text` does `int(percent)`,
    which raises ValueError on NaN and OverflowError on inf. That call sits at the
    top of `_animate_icon`, on the AppKit main thread, five times a second, above
    its try/except — and rumps prints timer tracebacks to stderr, which a built
    .app does not have. The symptom would be a frozen menu bar and an empty log.
    Rejecting it at the file boundary is the only place it costs nothing.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _iso_to_epoch(value) -> Optional[float]:
    """`2026-08-01T23:59:59.675247+00:00` → epoch seconds.

    The cache writes reset times as ISO strings while the statusline writes them
    as epoch numbers; the panel wants one of the two, and epoch is the one that
    formats in the reader's own timezone without a parser."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def read_config(path: Optional[Path] = None) -> dict:
    """Load ~/.claude.json, or `{}` if it cannot be read.

    Never raises: this is somebody else's file, it is ~100 KB of state we do not
    own, and no bar is worth taking the daemon down for. A missing file is the
    normal case on a machine where Claude Code has not run yet.

    The default is resolved here rather than in the signature: a default argument
    captures the constant's value at import, which would make
    `CLAUDE_CONFIG_PATH` unpatchable — and a caller that redirected it would
    silently keep reading the real home directory."""
    path = Path(path) if path else CLAUDE_CONFIG_PATH
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("could not read %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def read_limits_cache(path: Optional[Path] = None) -> dict:
    """What a current Claude Code records about the limit windows.

    Returns `{"windows": {kind: {...}}, "fetched_at": epoch|None}`, keyed by bar
    `kind` so the caller never has to know the other spelling.

    A far thinner record than the usage cache it replaced: a reset instant, a
    status, an overage flag, and **no percentage** — there is no bar to be drawn
    from this file, only fields to be filled in on one. Same read-it-and-shrug
    contract as `read_config`, for the same reason: somebody else's file, and no
    bar is worth taking the daemon down for."""
    path = Path(path) if path else CLAUDE_LIMITS_CACHE_PATH
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {"windows": {}, "fetched_at": None}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("could not read %s", path, exc_info=True)
        return {"windows": {}, "fetched_at": None}

    payload = payload if isinstance(payload, dict) else {}
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    rate = data.get("rate_limits")
    rate = rate if isinstance(rate, dict) else {}

    windows = {}
    for name, entry in rate.items():
        kind = _RATE_LIMIT_BAR_KINDS.get(name)
        if not kind or not isinstance(entry, dict):
            continue
        windows[kind] = {
            # Epoch seconds here, unlike the usage cache's ISO strings — but
            # _iso_to_epoch takes either, and guessing which is a bug waiting for
            # the format to change again.
            "resets_at": _iso_to_epoch(entry.get("resets_at")),
            "status": str(entry.get("status") or ""),
            "overage": bool(entry.get("is_using_overage")),
        }

    # Milliseconds, in both spellings this file has used.
    fetched = _num(data.get("fetched_at")) or _num(payload.get("ts"))
    return {"windows": windows,
            "fetched_at": fetched / 1000 if fetched else None}


def promo_notices(config: dict) -> list[dict]:
    """The promotional lines /usage prints under a bar, e.g. "+50% weekly limits
    promo through Aug 19".

    They arrive as a feature flag (`tengu_rate_limit_promo_notices`) rather than
    as part of the usage payload, and each names the bar it belongs under."""
    features = config.get("cachedGrowthBookFeatures")
    features = features if isinstance(features, dict) else {}
    raw = features.get("tengu_rate_limit_promo_notices")
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        out.append({"bar": str(item.get("bar") or ""), "text": text.strip()})
    return out


def cached_bars(config: dict) -> dict:
    """The bars as Claude Code last cached them.

    Returns `{"bars": [...], "fetched_at": epoch|None}`. `bars` is ordered as the
    source orders it — session first, then the weekly windows — because that
    order is the one the reader has already learned in the terminal."""
    cached = config.get("cachedUsageUtilization")
    cached = cached if isinstance(cached, dict) else {}
    utilization = cached.get("utilization")
    utilization = utilization if isinstance(utilization, dict) else {}
    entries = utilization.get("limits")

    bars = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        percent = _num(entry.get("percent"))
        if percent is None:
            continue
        bars.append({
            "kind": str(entry.get("kind") or ""),
            "group": str(entry.get("group") or ""),
            "title": _title_for(entry),
            "percent": percent,
            "resets_at": _iso_to_epoch(entry.get("resets_at")),
            "severity": str(entry.get("severity") or "normal"),
            "source": "cache",
        })

    fetched = _num(cached.get("fetchedAtMs"))
    return {"bars": bars, "fetched_at": fetched / 1000 if fetched else None}


def claude_payloads(entries) -> list[dict]:
    """The metrics of the agent rows that are spending *this* account's windows.

    Every bar in this module is a Claude window, and the field they are drawn
    from is `five_hour_pct`. Grok rows carry that same key — `grok_roster.
    _overlay_billing` sets it deliberately, so the fleet budget rule can see one
    number under one name — but it holds Grok's **weekly** percentage, against a
    Grok reset instant in `five_hour_resets_at`. Handed to `pick_live` unsorted,
    a Grok row can therefore win the election and furnish the whole "Current
    session" bar: a weekly figure, under a five-hour title, resetting next
    Friday. That is exactly what the panel's footer was showing while its header
    — which groups by provider — showed the true 5h number two inches above it.

    So the caller filters by provider *here*, at the one seam that knows it:
    `pick_live` sees only payloads, and a payload does not say whose account it
    is. An entry with no provider is Claude's, which is what every Claude Code
    row has always been.

    `signals.evaluate_global` and the panel's own budget chips already group by
    provider; this is the third consumer of that field, and the one that had no
    guard."""
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if (entry.get("provider") or "claude") != "claude":
            continue
        metrics = entry.get("metrics")
        if isinstance(metrics, dict):
            out.append(metrics)
    return out


def pick_live(payloads) -> dict:
    """The one statusline payload the bars should be drawn from.

    Every payload must already belong to the Claude account — see
    `claude_payloads`, which is the only thing standing between these bars and a
    Grok weekly percentage wearing the five-hour title.

    Newest, not highest. Rate limits are per account, so every session reports
    the same figure and the freshest tick is simply the truth — but a session
    parked on a prompt keeps its last payload for as long as it stays alive, and
    a `max` across those re-elects a percentage from a window that has since
    reset. It never falls, either: `max` has no way back down.

    Taken whole rather than field-by-field, because the four figures are two
    percentages and the two clocks they reset on. Mixing a percentage from one
    reading with a reset time from another is the bug this module exists to fix,
    only smaller. A payload carrying none of them is skipped so a session whose
    statusline has not reported limits yet does not shadow one that has."""
    best = None
    for metrics in payloads:
        if not isinstance(metrics, dict):
            continue
        picked = {f: metrics[f] for f in _LIVE_FIELDS if _num(metrics.get(f)) is not None}
        if not picked:
            continue
        picked["received_at"] = _num(metrics.get("received_at"))
        if best is None or (picked["received_at"] or 0.0) > (best["received_at"] or 0.0):
            best = picked
    return best or {}


def merge_bars(bars: list, metrics: dict) -> list[dict]:
    """Draw the two account-wide bars from the statusline: overwriting the cache's
    where it has one, emitting them outright where it does not.

    Overwriting needs no timestamp comparison. A statusline payload is by
    construction the value as of the last conversation update, which is at least
    as new as anything a cache holds. So a present statusline number always
    wins, and an absent one changes nothing.

    The bar takes the statusline's reset time along with its percentage. They
    are one reading — the percentage is the fraction of *that* window used — and
    a fresh number under the previous window's clock is worse than either source
    alone, because it looks current. The cache's reset time is kept only when
    the statusline did not report one.

    **Emitting** is the half that used to be missing, and it is the whole reason
    the Usage view could hold two live percentages and draw nothing: a merge that
    only overwrites needs the cache to have named the window first, so a machine
    whose Claude Code no longer writes that cache lost bars it had every figure
    for. A window the statusline reports is a window the account is being held
    to, whatever any cache does or does not remember about it.

    Synthesised bars are appended in the canonical order (session, then the
    weekly window) rather than interleaved, which is the order they come out in
    when the cache is empty — the usual case now, and the one that has to read
    right. `severity` is left `normal`: the statusline does not grade a window,
    and the panel derives its own heat from the percentage anyway.

    `metrics` is the live reading `pick_live` chose across the tracked sessions."""
    out = []
    for bar in bars:
        kind = bar.get("kind") or ""
        live = _num(metrics.get(_STATUSLINE_FIELDS.get(kind, "")))
        if live is None:
            out.append(dict(bar))
            continue
        merged = dict(bar, percent=live, source="statusline",
                      as_of=_num(metrics.get("received_at")))
        resets = _num(metrics.get(_STATUSLINE_RESET_FIELDS.get(kind, "")))
        if resets is not None:
            merged["resets_at"] = resets
        out.append(merged)

    seen = {bar.get("kind") for bar in out}
    for kind, field in _STATUSLINE_FIELDS.items():
        live = _num(metrics.get(field))
        if kind in seen or live is None:
            continue
        out.append({
            "kind": kind,
            "group": _BAR_GROUPS.get(kind, ""),
            "title": _title_for({"kind": kind}),
            "percent": live,
            "resets_at": _num(metrics.get(_STATUSLINE_RESET_FIELDS.get(kind, ""))),
            "severity": "normal",
            "source": "statusline",
            "as_of": _num(metrics.get("received_at")),
        })
    return out


def apply_limits_cache(bars: list, windows: dict,
                       now: Optional[float] = None) -> list[dict]:
    """Fill in from `limits-cache.json` what neither other source said.

    It holds no percentages, so it can only add to a bar that already exists: a
    reset instant when nothing else supplied one, and the overage flag, which
    nothing else on this machine reports at all.

    Gated on that entry's own window still being open. The file describes a
    window; once its reset instant is behind us that window has closed and the
    file says nothing about the one we are in — the copy on this machine is seven
    weeks old and names a five-hour window from June. Hanging its reset time on a
    live bar would flag that bar `stale` and have the panel announce that a window
    the user is actively spending ended weeks ago. An entry with no reset instant
    cannot be dated, so it cannot be trusted either, and contributes nothing."""
    now = time.time() if now is None else now
    out = []
    for bar in bars:
        entry = windows.get(bar.get("kind") or "") or {}
        resets = _num(entry.get("resets_at"))
        if resets is None or resets <= now:
            out.append(dict(bar))
            continue
        merged = dict(bar)
        if _num(merged.get("resets_at")) is None:
            merged["resets_at"] = resets
        # Set only when true. Absent reads as "not on overage" everywhere it is
        # consumed, and a False on every bar is a claim this file only sometimes
        # earns the right to make.
        if entry.get("overage"):
            merged["overage"] = True
        out.append(merged)
    return out


def mark_freshness(bars: list, fetched_at: Optional[float],
                   now: Optional[float] = None) -> list[dict]:
    """Stamp every bar with when it was read, and flag the ones that have expired.

    `as_of` is set for cache-sourced bars from the cache's own fetch time;
    `merge_bars` has already set it on the ones the statusline supplied. One
    field either way, so the panel can date every bar instead of only the ones
    it happens to know the provenance of — a bar with no age on it reads as
    current, which is exactly the bar most likely not to be.

    `stale` means the window this bar measures has already reset. Its percentage
    is then not merely old but wrong: the true figure restarted at zero when the
    clock ran out. We flag rather than zero it — inventing a number nobody
    reported is the same mistake pointing the other way."""
    now = time.time() if now is None else now
    out = []
    for bar in bars:
        as_of = bar.get("as_of")
        as_of = _num(as_of) if as_of is not None else fetched_at
        resets = _num(bar.get("resets_at"))
        out.append(dict(bar, as_of=as_of,
                        stale=bool(resets is not None and resets <= now)))
    return out


def attach_promos(bars: list, promos: list) -> tuple[list, list]:
    """Hang each promo line under the bar it applies to.

    A promo whose bar cannot be matched is attached to nothing rather than
    dropped by the caller — `unattached` in the snapshot — because "+50% weekly
    limits" is information the reader wants even if the naming has moved on."""
    by_kind: dict[str, list] = {}
    unattached = []
    for promo in promos:
        kind = _PROMO_BAR_KINDS.get(promo.get("bar") or "")
        if kind:
            by_kind.setdefault(kind, []).append(promo["text"])
        else:
            unattached.append(promo["text"])
    out = [dict(bar, promos=by_kind.get(bar.get("kind") or "", []))
           for bar in bars]
    return out, unattached


def snapshot(metrics: Optional[dict] = None,
             path: Optional[Path] = None,
             limits_path: Optional[Path] = None) -> dict:
    """Everything the Usage view needs about limits, in one read.

    `metrics` is the live statusline aggregate, or None when nothing has reported.
    `available` is False only when no source named a single window — the panel
    needs to distinguish "you are at 0%" from "we have not been told".

    The order is the precedence: the usage cache lays out whatever bars it still
    has, the statusline overwrites and adds to them, and `limits-cache.json` fills
    what is left. Freshness is stamped last, over all of it."""
    config = read_config(path)
    cached = cached_bars(config)
    bars = merge_bars(cached["bars"], metrics or {})
    bars = apply_limits_cache(bars, read_limits_cache(limits_path)["windows"])
    bars, unattached = attach_promos(bars, promo_notices(config))
    bars = mark_freshness(bars, cached["fetched_at"])
    return {
        "bars": bars,
        "notices": unattached,
        "fetched_at": cached["fetched_at"],
        "available": bool(bars),
    }
