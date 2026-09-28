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
  multiplier is a per-`Rate` field because Fable 5.1 and Mythos 5.1 price cache
  reads at $0.25/MTok — 0.025x their $10 input rate, not 0.10x; the flat rule
  overstated their cache reads fourfold.
* **An unknown model costs nothing and says so.** Pricing a local or unrecognised
  model at some neighbour's rate produces a number that looks real. `cost_usd`
  returns None instead, and callers surface "N turns unpriced".

Rates are USD per million tokens. Override without editing code via
~/.dark-army/pricing.json.

**Three publishers, one table, checked on 2026-09-28.** Anthropic's pricing
page for the Claude rows, xAI's model list for Grok (`grok-4.5`, `grok-4.6`,
`grok-4.7`), OpenAI's API pricing page for the seven Codex ids in
`dispatch.MODELS["codex"]`, standard tier only (no batch, flex, fast mode or
data-residency uplift). What those pages said that day, and did not say:

* Anthropic: Sonnet 5 is $2/$10 with no end date printed, so the old
  "$3/$15 after 2026-08-31" row was corrected. Fable 5 and Mythos 5 read cache
  at $1/MTok (0.10x), not $0.25; Fable 5.1 and Mythos 5.1 do read at $0.25
  (0.025x); Opus 5.5 reads at $0.20 on $4 (0.05x).
* xAI: a request whose prompt reaches 200,000 tokens is billed at the long
  tier on every token. No cache-write price is published.
* OpenAI: `gpt-5.6-sol` states its cutoff (short context is up to 272K input
  tokens) and both tiers. `gpt-5.5` states `<272K` and prints only the short
  tier, and no cache-write price, so a longer prompt or a cache write on it
  is unpriced rather than guessed. The `gpt-6-*` table prints a long tier but
  **no cutoff number**, and `gpt-5.6-terra` / `gpt-5.6-luna` print one tier and
  no cutoff: all five are priced at the short tier, whatever the prompt.
  On OpenAI's page, input is either uncached, cached or a cache write — the
  last two are subsets of `input_tokens`, never additional tokens.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from typing import Optional

from .paths import STATE_DIR

logger = logging.getLogger("dark-army.pricing")

PRICING_OVERRIDE_PATH = STATE_DIR / "pricing.json"

# Multipliers on the input rate. The write multipliers hold for every Claude
# model; the read multiplier is only the *default* — Fable/Mythos 5.1 override
# it on their own rows ($0.25/MTok reads on a $10 input rate is 0.025x).
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.00

# Fable 5.1 / Mythos 5.1 cache reads: $0.25 per MTok on a $10.00 input rate.
_FABLE_CACHE_READ = 0.025


@dataclass(frozen=True)
class Rate:
    """USD per million tokens."""

    input: float
    output: float
    # Some models launch with promotional pricing that expires. The fields
    # say when, per local day, so a turn is priced at the rate in force on the
    # day it ran. (Sonnet 5 carried one until its page dropped the end date.)
    intro_input: Optional[float] = None
    intro_output: Optional[float] = None
    intro_until: Optional[str] = None      # local date, YYYY-MM-DD, inclusive
    # Multiplier on the input rate for cache reads. 0.10 is the API's rule for
    # most models; Fable/Mythos 5.1 price reads at $0.25/MTok (0.025x of $10).
    cache_read_multiplier: float = CACHE_READ_MULTIPLIER
    # Whose price list this row is. Claude's rows are the only ones the
    # legacy estimate (`history._price_row`) and the prefix and family tiers
    # may reach: a Grok or Codex id is matched exactly or not at all.
    provider: str = "claude"
    # Multiplier on the input rate for a (5-minute) cache write. None means
    # the publisher prints no write price, and a write is then unpriced — never
    # given Claude's 1.25x. The 1-hour write is Claude's alone.
    cache_write_multiplier: Optional[float] = CACHE_WRITE_5M_MULTIPLIER
    # The long-context tier, stored only where the publisher prints the
    # cutoff. `long_from` is the first prompt size billed at the long tier;
    # the tier then applies to every token of that request. A cutoff with no
    # printed long price (`long_input` None) makes such a request unpriced.
    long_from: Optional[int] = None
    long_input: Optional[float] = None
    long_output: Optional[float] = None
    long_cache_read_multiplier: Optional[float] = None
    long_cache_write_multiplier: Optional[float] = None

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
    # Fable 5 and Mythos 5 read cache at $1/MTok on $10 — the default 0.10x.
    # Only the 5.1 rows carry the $0.25 read (Anthropic's page, 2026-09-28).
    "claude-fable-5": Rate(10.00, 50.00),
    # Same ordering rule: the 5-1 row must sit above "claude-mythos-5".
    "claude-mythos-5-1": Rate(10.00, 50.00, cache_read_multiplier=_FABLE_CACHE_READ),
    "claude-mythos-5": Rate(10.00, 50.00),
    # Same ordering rule: the 5-5 row must sit above "claude-opus-5". Opus 5.5
    # reads cache at $0.20/MTok on a $4.00 input rate, 0.05x.
    "claude-opus-5-5": Rate(4.00, 20.00, cache_read_multiplier=0.05),
    "claude-opus-5": Rate(5.00, 25.00),
    "claude-opus-4-8": Rate(5.00, 25.00),
    "claude-opus-4-7": Rate(5.00, 25.00),
    "claude-opus-4-6": Rate(5.00, 25.00),
    # $2/$10 with no end date on the publisher's page on 2026-09-28. The
    # launch notes said $3/$15 from 2026-09-01; the page no longer does.
    "claude-sonnet-5": Rate(2.00, 10.00),
    "claude-sonnet-4-6": Rate(3.00, 15.00),
    "claude-haiku-4-5": Rate(1.00, 5.00),
    # --- Grok (xAI), standard us-east-1 / us-west-2 prices ---------------
    # Cache reads are $0.50 on $2 (0.25x) short and $1.00 on $4 long for
    # 4.6 / 4.7; $0.30 on $2 (0.15x) and $0.60 on $4 for 4.5. No write price.
    "grok-4.7": Rate(2.00, 6.00, provider="grok", cache_read_multiplier=0.25,
                     cache_write_multiplier=None, long_from=200_000,
                     long_input=4.00, long_output=12.00,
                     long_cache_read_multiplier=0.25),
    "grok-4.6": Rate(2.00, 6.00, provider="grok", cache_read_multiplier=0.25,
                     cache_write_multiplier=None, long_from=200_000,
                     long_input=4.00, long_output=12.00,
                     long_cache_read_multiplier=0.25),
    "grok-4.5": Rate(2.00, 6.00, provider="grok", cache_read_multiplier=0.15,
                     cache_write_multiplier=None, long_from=200_000,
                     long_input=4.00, long_output=12.00,
                     long_cache_read_multiplier=0.15),
    # --- Codex (OpenAI), standard tier ------------------------------------
    # Cached input is 0.10x and a cache write 1.25x of input on every row
    # that prints one. No cutoff printed for these five: short tier only.
    "gpt-6-astra": Rate(10.00, 50.00, provider="codex",
                        cache_write_multiplier=1.25),
    "gpt-6-sol": Rate(2.00, 10.00, provider="codex",
                      cache_write_multiplier=1.25),
    "gpt-6-luna": Rate(0.10, 0.50, provider="codex",
                       cache_write_multiplier=1.25),
    "gpt-5.6-terra": Rate(2.00, 12.00, provider="codex",
                          cache_write_multiplier=1.25),
    "gpt-5.6-luna": Rate(0.20, 1.20, provider="codex",
                         cache_write_multiplier=1.25),
    # Short context is "≤272K input tokens"; long is $8 / $0.80 / $10 / $30.
    "gpt-5.6-sol": Rate(4.00, 20.00, provider="codex",
                        cache_write_multiplier=1.25, long_from=272_001,
                        long_input=8.00, long_output=30.00,
                        long_cache_read_multiplier=0.10,
                        long_cache_write_multiplier=1.25),
    # "<272K context length": $5 / $0.50 cached / $30. No cache-write price
    # and no long-tier price are printed, so neither is invented.
    "gpt-5.5": Rate(5.00, 30.00, provider="codex",
                    cache_write_multiplier=None, long_from=272_000),
}

#: Id prefixes that belong to a non-Claude publisher. The family tier below
#: matches a substring, and a Claude family word inside one of these would
#: bill a Grok or Codex turn at a Claude rate.
_FOREIGN_PREFIXES = ("gpt", "grok", "o1", "o3", "o4", "codex")

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
            fields = {
                "input": float(spec["input"]),
                "output": float(spec["output"]),
                "intro_input": float(intro_input) if intro_input is not None else None,
                "intro_output": float(intro_output) if intro_output is not None else None,
                "intro_until": spec.get("intro_until"),
            }
            # An explicit multiplier replaces the row's; an absent one keeps
            # the built-in row's own (Fable 5.1's 0.025x, Opus 5.5's 0.05x)
            # rather than silently resetting it to 0.10. A new id starts
            # from the dataclass defaults, 0.10 included.
            if spec.get("cache_read_multiplier") is not None:
                fields["cache_read_multiplier"] = float(spec["cache_read_multiplier"])
            builtin = RATES.get(model)
            RATES[model] = (dataclasses.replace(builtin, **fields)
                            if builtin is not None else Rate(**fields))
        except (TypeError, ValueError):
            logger.warning("Skipping unparseable price entry for %r", model)
    logger.info("Loaded %d price override(s) from %s", len(data), PRICING_OVERRIDE_PATH)


def resolve_rate(model: Optional[str],
                 provider: Optional[str] = None) -> Optional[Rate]:
    """Find the rate for a model id: exact, then prefix, then family.

    The prefix tier handles datestamped ids (`claude-opus-4-6-20260215`) and
    suffixed ones (`claude-opus-4-8[1m]`) without a table entry each. It and
    the family tier reach Claude rows only: `gpt-5.5` is a prefix of
    `gpt-5.5-pro`, a different price, so a Grok or Codex id is exact or None.

    `provider`, when given, is a filter: a row of another provider's list is
    None. The legacy estimate passes "claude", which is what keeps a Grok or
    Codex turn exactly as unpriced there as it was before those rows existed."""
    _load_overrides()
    if not model:
        return None
    name = model.strip().lower()
    rate = RATES.get(name)
    if rate is None:
        for known, candidate in RATES.items():
            if candidate.provider == "claude" and name.startswith(known):
                rate = candidate
                break
    if rate is None and not name.startswith(_FOREIGN_PREFIXES):
        for family, candidate in FAMILY_RATES.items():
            if family in name:
                rate = candidate
                break
    if rate is None or (provider is not None and rate.provider != provider):
        return None
    return rate


def cost_usd(model: Optional[str], *, input_tokens: int = 0, output_tokens: int = 0,
             cache_read: int = 0, cache_write_5m: int = 0, cache_write_1h: int = 0,
             day: Optional[str] = None, prompt_tokens: Optional[int] = None,
             cached_subset: bool = False,
             provider: Optional[str] = None) -> Optional[float]:
    """Cost of one turn at the published rate, or None if it cannot be priced.

    None, not 0.0: a zero would sum into a total and quietly understate it, and no
    surface would ever say which turns were missing. None when the model has no
    row (or, with `provider`, no row on that provider's list), when a cache
    write meets a row with no published write price, when the prompt reaches
    a cutoff whose long price is not printed, and when the cached subset is
    larger than the input it is a subset of.

    `cached_subset` says `input_tokens` already *includes* the cache read and
    the cache writes (Grok, Codex); they are then priced at their own rates and
    subtracted from input rather than charged twice. Without it (Claude) the
    three are separate counts. `prompt_tokens` is the one request's prompt,
    which picks the tier; absent, it is the whole prompt the counts describe."""
    rate = resolve_rate(model, provider)
    if rate is None:
        return None
    input_tokens = max(0, input_tokens)
    output_tokens = max(0, output_tokens)
    cache_read = max(0, cache_read)
    cache_write_5m = max(0, cache_write_5m)
    cache_write_1h = max(0, cache_write_1h)
    if cached_subset:
        uncached = input_tokens - cache_read - cache_write_5m - cache_write_1h
        if uncached < 0:
            return None
        prompt = input_tokens
    else:
        uncached = input_tokens
        prompt = input_tokens + cache_read + cache_write_5m + cache_write_1h
    if prompt_tokens is not None:
        prompt = max(0, prompt_tokens)
    in_rate, out_rate = rate.for_day(day)
    read_multiplier = rate.cache_read_multiplier
    write_multiplier = rate.cache_write_multiplier
    if rate.long_from is not None and prompt >= rate.long_from:
        if rate.long_input is None or rate.long_output is None:
            return None
        in_rate, out_rate = rate.long_input, rate.long_output
        if rate.long_cache_read_multiplier is not None:
            read_multiplier = rate.long_cache_read_multiplier
        write_multiplier = rate.long_cache_write_multiplier
    if cache_write_5m and write_multiplier is None:
        return None
    if cache_write_1h and rate.provider != "claude":
        return None
    total = (
        uncached * in_rate
        + output_tokens * out_rate
        + cache_read * in_rate * read_multiplier
        + cache_write_5m * in_rate * (write_multiplier or 0.0)
        + cache_write_1h * in_rate * CACHE_WRITE_1H_MULTIPLIER
    )
    return total / 1_000_000
