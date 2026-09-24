"""Token pricing — the *fallback* path for cost, never the primary one.

Claude Code reports each session's real spend through the statusline
(`cost.total_cost_usd`), and that figure is authoritative. This module exists for
what that cannot cover: sessions that ran before the collector was installed, and
transcripts backfilled from history. Everything it produces is labelled
`estimated` (history.COST_ESTIMATED) and must stay visually distinct from a
measured figure — summing the two silently turns a guess into a number.

Two design choices worth keeping:

* **Cache rates are multipliers, not separate numbers.** A cache read costs 0.1x
  the input rate for most models, a 5-minute cache write 1.25x, a one-hour write
  2x. Encoding the rule rather than four hand-copied figures per model means a
  new model needs one line and cannot be internally inconsistent. The read
  multiplier is a per-`Rate` field because Fable and Mythos price cache reads at
  $0.25/MTok — 0.025x their $10 input rate, not 0.10x; the flat rule overstated
  their cache reads fourfold.
* **An unknown model costs nothing and says so.** Pricing a local or unrecognised
  model at some neighbour's rate produces a number that looks real. `cost_usd`
  returns None instead, and callers surface "N turns unpriced".

Rates are USD per million tokens, from the Anthropic pricing tables current at
2026-07-26. Override without editing code via ~/.dark-army/pricing.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .paths import STATE_DIR

logger = logging.getLogger("dark-army.pricing")

PRICING_OVERRIDE_PATH = STATE_DIR / "pricing.json"

# Multipliers on the input rate. The write multipliers hold for every model;
# the read multiplier is only the *default* — Fable/Mythos override it on their
# own rows ($0.25/MTok reads against a $10 input rate is 0.025x, not 0.10x).
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00

# Fable / Mythos cache reads: $0.25 per MTok on a $10.00 input rate.
_FABLE_CACHE_READ = 0.025


@dataclass(frozen=True)
class Rate:
    """USD per million tokens."""

    input: float
    output: float
    # Some models launch with promotional pricing that expires. Sonnet 5 is
    # $2/$10 through 2026-08-31, $3/$15 after — a session priced at the standard
    # rate today would be overstated by a third.
    intro_input: Optional[float] = None
    intro_output: Optional[float] = None
    intro_until: Optional[str] = None      # local date, YYYY-MM-DD, inclusive
    # Multiplier on the input rate for cache reads. 0.10 is the API's rule for
    # most models; Fable/Mythos price reads at $0.25/MTok (0.025x of $10).
    cache_read_multiplier: float = CACHE_READ_MULTIPLIER

    def for_day(self, day: Optional[str]) -> tuple[float, float]:
        """The (input, output) rate in force on `day` (local YYYY-MM-DD)."""
        if self.intro_until and self.intro_input is not None and day:
            if day <= self.intro_until:
                return self.intro_input, self.intro_output or self.output
        return self.input, self.output


# Exact model ids. Datestamped and suffixed variants resolve through the prefix
# and family tiers below, so this table only needs the canonical names.
RATES: dict[str, Rate] = {
    # Order is load-bearing: resolve_rate's prefix tier takes the first
    # startswith hit, so the 5-1 row must sit above "claude-fable-5" or a
    # datestamped 5-1 id would resolve through the older row.
    "claude-fable-5-1": Rate(10.00, 50.00, cache_read_multiplier=_FABLE_CACHE_READ),
    "claude-fable-5": Rate(10.00, 50.00, cache_read_multiplier=_FABLE_CACHE_READ),
    # Same ordering rule: the 5-1 row must sit above "claude-mythos-5".
    "claude-mythos-5-1": Rate(10.00, 50.00, cache_read_multiplier=_FABLE_CACHE_READ),
    "claude-mythos-5": Rate(10.00, 50.00, cache_read_multiplier=_FABLE_CACHE_READ),
    # Same ordering rule: the 5-5 row must sit above "claude-opus-5". Opus 5.5
    # reads cache at $0.20/MTok on a $4.00 input rate, 0.05x.
    "claude-opus-5-5": Rate(4.00, 20.00, cache_read_multiplier=0.05),
    "claude-opus-5": Rate(5.00, 25.00),
    "claude-opus-4-8": Rate(5.00, 25.00),
    "claude-opus-4-7": Rate(5.00, 25.00),
    "claude-opus-4-6": Rate(5.00, 25.00),
    "claude-sonnet-5": Rate(3.00, 15.00,
                            intro_input=2.00, intro_output=10.00,
                            intro_until="2026-08-31"),
    "claude-sonnet-4-6": Rate(3.00, 15.00),
    "claude-haiku-4-5": Rate(1.00, 5.00),
}

# Last resort when an id matches no known model: price by family. Deliberately
# coarse — it is better than nothing for an unrecognised point release, and it is
# still reported as an estimate.
FAMILY_RATES: dict[str, Rate] = {
    "fable": RATES["claude-fable-5-1"],
    "mythos": RATES["claude-mythos-5"],
    "opus": RATES["claude-opus-4-8"],
    "sonnet": RATES["claude-sonnet-4-6"],
    "haiku": RATES["claude-haiku-4-5"],
}

_overrides_loaded = False


def _load_overrides() -> None:
    """Merge ~/.dark-army/pricing.json over the built-in table, once.

    A price list baked into a release goes stale the day a model ships. The file
    is a flat {model_id: {input, output, ...}} map; anything malformed is skipped
    loudly rather than silently ignored."""
    global _overrides_loaded
    if _overrides_loaded:
        return
    _overrides_loaded = True
    try:
        data = json.loads(PRICING_OVERRIDE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read %s; using built-in prices",
                       PRICING_OVERRIDE_PATH, exc_info=True)
        return
    if not isinstance(data, dict):
        logger.warning("%s is not an object; using built-in prices",
                       PRICING_OVERRIDE_PATH)
        return
    for model, spec in data.items():
        if not isinstance(spec, dict) or "input" not in spec or "output" not in spec:
            logger.warning("Skipping malformed price entry for %r", model)
            continue
        try:
            # Intro rates are coerced like the base rates: a JSON file written
            # by hand holds "2.0" as easily as 2.0, and a string that survived
            # to for_day() would turn the token count into string repetition.
            intro_input = spec.get("intro_input")
            intro_output = spec.get("intro_output")
            RATES[model] = Rate(
                float(spec["input"]), float(spec["output"]),
                intro_input=float(intro_input) if intro_input is not None else None,
                intro_output=float(intro_output) if intro_output is not None else None,
                intro_until=spec.get("intro_until"),
            )
        except (TypeError, ValueError):
            logger.warning("Skipping unparseable price entry for %r", model)
    logger.info("Loaded %d price override(s) from %s", len(data), PRICING_OVERRIDE_PATH)


def resolve_rate(model: Optional[str]) -> Optional[Rate]:
    """Find the rate for a model id: exact, then prefix, then family.

    The prefix tier handles datestamped ids (`claude-opus-4-6-20260215`) and
    suffixed ones (`claude-opus-4-8[1m]`) without a table entry each."""
    _load_overrides()
    if not model:
        return None
    name = model.strip().lower()
    if name in RATES:
        return RATES[name]
    for known, rate in RATES.items():
        if name.startswith(known):
            return rate
    for family, rate in FAMILY_RATES.items():
        if family in name:
            return rate
    return None


def cost_usd(model: Optional[str], *, input_tokens: int = 0, output_tokens: int = 0,
             cache_read: int = 0, cache_write_5m: int = 0, cache_write_1h: int = 0,
             day: Optional[str] = None) -> Optional[float]:
    """Estimated cost of one turn, or None if the model is unknown.

    None, not 0.0: a zero would sum into a total and quietly understate it, and no
    surface would ever say which turns were missing."""
    rate = resolve_rate(model)
    if rate is None:
        return None
    in_rate, out_rate = rate.for_day(day)
    total = (
        max(0, input_tokens) * in_rate
        + max(0, output_tokens) * out_rate
        + max(0, cache_read) * in_rate * rate.cache_read_multiplier
        + max(0, cache_write_5m) * in_rate * CACHE_WRITE_5M_MULTIPLIER
        + max(0, cache_write_1h) * in_rate * CACHE_WRITE_1H_MULTIPLIER
    )
    return total / 1_000_000
