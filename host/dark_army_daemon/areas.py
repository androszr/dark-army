"""Delivery areas and their leads. Pure table; a lead grants no capability."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Area:
    slug: str
    name: str
    concept: str
    pool: tuple[str, ...]


CHIEF_OF_STAFF = "cipher"
UNIVERSAL = "universal"
AREAS: tuple[Area, ...] = (
    Area('backbone', 'Backbone', 'services, data & transport', ('relay', 'hex', 'forge')),
    Area('desk', 'Desk', 'the Mac window & menu bar', ('vex', 'zosia')),
    Area('pocket', 'Pocket', 'phones & widgets', ('mira', 'ptyś')),
    Area('ledger', 'Ledger', 'numbers that must be right', ('audit', 'ledger')),
    Area('play', 'Play', 'worlds, art & feel', ('franio', 'quiet')),
    Area('conductor', 'Conductor', 'agents, prompts & LLM features', ('velvet', 'canon')),
    Area('gate', 'Gate', 'security, release & operations', ('nyx', 'watch', 'captcha', 'sawa')),
    Area('universal', 'Universal', 'the fixer', ('proxy', 'androll')),
 )
AREA_REFUSAL = "area must be one of: " + ", ".join(a.slug for a in AREAS) + ", or empty"


def slugs() -> tuple[str, ...]:
    return tuple(a.slug for a in AREAS)


def get(slug: str) -> Area | None:
    key = (slug or "").strip().lower() or UNIVERSAL
    return next((a for a in AREAS if a.slug == key), None)


def name(slug: str) -> str:
    area = get(slug)
    return area.name if area else ""


def pool_for(slug: str) -> tuple[str, ...]:
    area = get(slug)
    return area.pool if area else ()


def anchor(slug: str) -> str:
    pool = pool_for(slug)
    return pool[0] if pool else ""


def normalise(value) -> tuple[str, str]:
    key = str(value or "").strip().lower()
    if key in ("", "none"):
        return "", ""
    for area in AREAS:
        if key in (area.slug, area.name.lower()):
            return area.slug, ""
    return "", AREA_REFUSAL


def _djb2(text: str) -> int:
    """djb2 over UTF-8 — unchanged from the crew allocator."""
    h = 5381
    for byte in text.encode("utf-8"):
        h = (h * 33) + byte
    return h


def allocate(slug: str, card_id: str, busy: set[str] | frozenset[str]) -> str:
    """Usual lead first, then the ordered pool; deterministic honest copy if full.

    The old role hash start did not pick area anchors. The explicit Start
    contract requires Relay then Hex, so only exhaustion uses the hash.
    Naming callers reject a busy copy and let the identity store choose.
    """
    pool = pool_for(slug)
    if not pool:
        return ""
    for candidate in pool:
        if candidate not in busy:
            return candidate
    return pool[_djb2(f"{card_id}:{slug}") % len(pool)]


def brief_path(slug: str) -> str:
    area = get(slug) if slug else None
    return f".claude/leads/{area.slug}.md" if area else ""


def lead_line(name: str, slug: str) -> str:
    area = get(slug) if slug else None
    if not area:
        return ""
    stem = (name or "").split("-", 1)[0].lower()
    role = "lead" if stem in area.pool else "stand-in"
    return f"{name} · {area.name} {role}"
