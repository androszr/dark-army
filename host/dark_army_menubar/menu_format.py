"""Formatting helpers for the menu-bar strip's usage cluster.

Pure string formatting kept out of the AppKit/rumps layer so it can be unit
tested.

It used to format the dropdown's agent rows too — model names, durations,
per-session detail lines, the Usage submenu. Those went with the dropdown: the
panel is the only surface now and it does its own formatting in Swift, so what
is left here is what the strip itself draws.
"""

from __future__ import annotations

_LONG_CONTEXT_SUFFIX = "[1m]"

# The two account-wide rate-limit windows, in the order /usage draws them, with
# the short label the menu bar has room for. `weekly_scoped` (the per-model
# window) is deliberately absent: it only exists on installs whose Claude Code
# still writes the usage cache, and a row that is missing on most machines is
# worse in a menu than one that was never promised.
LIMIT_WINDOWS = (("session", "5h"), ("weekly_all", "7d"))

def _bar_for(snapshot: dict, kind: str) -> dict:
    """The bar `limits.snapshot` produced for one window kind, or {}."""
    for bar in (snapshot or {}).get("bars") or []:
        if bar.get("kind") == kind:
            return bar
    return {}


def limit_percent(snapshot: dict, kind: str = "session"):
    """The percentage of one limit window that is spent, or None.

    None for a bar that is missing *and* for one flagged `stale`: stale means the
    window this figure measured has already reset, so the true number restarted at
    zero and the one we hold is not old but wrong. Nothing is the honest reading —
    see `limits.mark_freshness`."""
    bar = _bar_for(snapshot, kind)
    percent = bar.get("percent")
    if bar.get("stale") or not isinstance(percent, (int, float)):
        return None
    return float(percent)


# Where the five-hour meter stops being neutral. Two steps rather than one: at
# three quarters of a window what you do next starts depending on what is left,
# and at nine tenths it stops being advice.
USAGE_WARN_PERCENT = 75.0
USAGE_CRIT_PERCENT = 90.0

# What the strip shows in place of a reading it does not have. An en dash, not a
# zero and not nothing: the slot stays where the eye expects it, and holding the
# slot is what distinguishes "no current reading" from "this feature broke".
USAGE_UNKNOWN = "–"


def usage_text(snapshot: dict) -> str:
    """The five-hour window as the strip sets it: ``25%``.

    Just the number. The old form was ``5H: 25%`` — an abbreviation, a colon and a
    space spent restating something the dropdown's Usage section says in full, on
    the one surface where width is contested by every other app on the bar.

    Three cases, and the difference between the last two matters:
      - a fresh reading  → ``"25%"``
      - a *stale* bar    → ``"–"``, because the window it measured has already
        reset and the figure we hold is not old but wrong (see `limit_percent`) —
        yet vanishing entirely reads as a broken feature, so the slot is held
      - nothing reported → ``""``, and the strip draws no cluster at all."""
    bar = _bar_for(snapshot, "session")
    percent = bar.get("percent")
    if not isinstance(percent, (int, float)):
        return ""
    if bar.get("stale"):
        return USAGE_UNKNOWN
    return f"{round(float(percent))}%"


def usage_tier(percent: float | None) -> str:
    """``ok`` / ``warn`` / ``crit`` for a five-hour reading, or ``stale`` for none.

    Healthy is the *neutral* tier, deliberately: a menu bar that is green whenever
    nothing is wrong has spent its colour before the moment it is needed."""
    if not isinstance(percent, (int, float)):
        return "stale"
    if percent >= USAGE_CRIT_PERCENT:
        return "crit"
    if percent >= USAGE_WARN_PERCENT:
        return "warn"
    return "ok"


def grok_usage_text(snapshot: dict) -> str:
    """The Grok weekly window as the strip sets it: ``36%``.

    Bare digits, like Claude's. It used to carry a leading ``G`` — two bare
    percentages side by side genuinely cannot be told apart — but the strip now
    opens each cluster with its provider's mark, which does that job in the same
    width without asking the reader to decode a letter first. Empty when nothing
    has been fetched; an en dash when the window we measured has reset.

    """
    if not isinstance(snapshot, dict):
        return ""
    percent = snapshot.get("percent")
    if not isinstance(percent, (int, float)):
        return ""
    if snapshot.get("stale"):
        return USAGE_UNKNOWN
    return f"{round(float(percent))}%"


def codex_limit_percent(snapshot: dict):
    """Worst fresh Codex account window, or None when none is trustworthy."""
    bars = (snapshot or {}).get("bars") if isinstance(snapshot, dict) else []
    values = [float(bar["percent"]) for bar in (bars or [])
              if isinstance(bar, dict) and not bar.get("stale")
              and isinstance(bar.get("percent"), (int, float))]
    return max(values) if values else None


def codex_usage_text(snapshot: dict) -> str:
    percent = codex_limit_percent(snapshot)
    return f"{round(percent)}%" if percent is not None else ""
