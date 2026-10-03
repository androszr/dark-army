# host/dark_army_daemon/agent_models.py
"""Which model each agent and helper runs on, per assistant, with a
per-project override.

Pure: stdlib plus `dispatch` (the curated model allowlist) and
`card_prepare` (the card preparer's own cheap model). Reads no file and
spawns nothing. Two stored preferences feed it, both written by the menu-bar
app alone (`preferences.DEFAULTS`):

- `agent_models` — the machine-wide table, `{provider: {slot: model}}`.
- `agent_models_by_root` — `{root: {provider: {slot: model}}}`, the
  per-project override map.

Three values are distinguishable and mean different things:

- a **missing** key means *the shipped value* (`SHIPPED`);
- `""` means *Default* — no `--model` flag on the argv, no `model:` line in
  the brief — and is an explicit choice, never read as absent;
- `INHERIT` (`"inherit"`) is legal **only** in an override entry and means
  "this project has no opinion; use the machine-wide value". On the wire it
  removes the entry rather than being stored.

`allowed(provider, slot)` is the allowlist a choice is validated against:
`dispatch.MODELS[provider]` for every slot, plus the preparer's
`card_prepare.HELPER_MODELS[provider]` for the `card-preparer` slot alone
(a headless-helper model need not be one a card may name).
A name off that list is **refused, never trimmed to the nearest**; the daemon's
own `_agent_model_for` re-checks `validate` on the way out, so a hand-edited
`preferences.json` can never put an unknown name on an argv.

`SLOTS` carries all nine names — `main`, the seven roles and `worker` — so a
stored preference for any of them is legal and forward-compatible. The
settings window draws a row only for slots the shipped pack can write
(`pack_render.shipped_roles()`); since 22 Sep 2026 the pack ships a generic
`integration-reviewer`, so every role has a row.

`worker` is the shunt skill's cheap helper — the model
`.claude/skills/shunt/bulk_read.py` and `code_write.py` run on, written into
each project's `workers.json` by `pack_render.pin_worker_models`. It is an
ordinary slot for `allowed` (`dispatch.MODELS` only; every shipped cell is on
that list) and is drawable only when the shipped pack carries the skill
(`slots_for(roles, worker=True)`), the same inert-control rule as the roles.

**Effort** (plans/2026-10-03-card-and-role-effort-level.md) is a second pair of
preferences beside the model ones, with the same three-valued meaning:
`agent_efforts` (`{provider: {slot: level}}`) and `agent_efforts_by_root`
(`{root: {provider: {slot: level}}}`), never a sub-key of the model tables. A
missing key means the shipped level (`SHIPPED_EFFORTS`), `""` is Default (no
flag, no brief line) and `INHERIT` is legal only in an override. The levels
offered are `dispatch.efforts_for(provider, model)` for the slot's resolved
model; only `EFFORT_SLOTS` carry one, because nothing reads an effort for the
card preparer or the shunt worker and an inert control is refused.
"""

from __future__ import annotations

from typing import Iterable

from . import card_prepare
from . import dispatch

PROVIDERS = ("claude", "codex", "grok")

SLOTS = (
    "main",
    "planner",
    "implementer",
    "verifier",
    "bug-auditor",
    "integration-reviewer",
    "security-reviewer",
    "card-preparer",
    "worker",
)

#: The override map's "no opinion" value. Legal only for an override, and
#: never stored: a press carrying it removes that project's entry.
INHERIT = "inherit"

#: The starting setup: provider-specific models for the main session and each
#: role. Codex uses the GPT-6 tier matched to the role; Claude and Grok retain
#: their established defaults.
#: Every non-empty cell is a member of `allowed(provider, slot)` — a test
#: pins it, and `_check_shipped()` below refuses to import otherwise.
SHIPPED: dict[str, dict[str, str]] = {
    "claude": {
        "main": "",
        "planner": "opus",
        "implementer": "opus",
        "verifier": "sonnet",
        "bug-auditor": "sonnet",
        "integration-reviewer": "sonnet",
        "security-reviewer": "sonnet",
        "card-preparer": card_prepare.HELPER_MODELS["claude"],
        "worker": card_prepare.WORKER_MODELS["claude"],
    },
    "codex": {
        "main": "gpt-6-sol",
        "planner": "gpt-6-astra",
        "implementer": "gpt-6-sol",
        "verifier": "gpt-6-luna",
        "bug-auditor": "gpt-6-luna",
        "integration-reviewer": "gpt-6-luna",
        "security-reviewer": "gpt-6-astra",
        "card-preparer": card_prepare.HELPER_MODELS["codex"],
        "worker": card_prepare.WORKER_MODELS["codex"],
    },
    "grok": {
        "main": "",
        "planner": "grok-4.6",
        "implementer": "grok-4.6",
        "verifier": "grok-4.5",
        "bug-auditor": "grok-4.5",
        "integration-reviewer": "grok-4.5",
        "security-reviewer": "grok-4.5",
        "card-preparer": card_prepare.HELPER_MODELS["grok"],
        "worker": card_prepare.WORKER_MODELS["grok"],
    },
}


#: The slots an effort may be chosen for: `main` and the seven roles. The card
#: preparer and the shunt worker get none (out of scope, no reader).
EFFORT_SLOTS = (
    "main",
    "planner",
    "implementer",
    "verifier",
    "bug-auditor",
    "integration-reviewer",
    "security-reviewer",
)

#: The shipped effort per cell. Claude and Grok ship Default everywhere; Codex
#: ships `high` for every role (the level the Codex companion files carry
#: today, `pack_render.SHIPPED_CODEX_EFFORT`) and Default for `main`, so an
#: untouched install changes no byte on disk.
SHIPPED_EFFORTS: dict[str, dict[str, str]] = {
    "claude": {slot: "" for slot in EFFORT_SLOTS},
    "codex": {slot: ("" if slot == "main" else "high") for slot in EFFORT_SLOTS},
    "grok": {slot: "" for slot in EFFORT_SLOTS},
}


def allowed(provider: str, slot: str) -> tuple[str, ...]:
    """The names a choice for `(provider, slot)` may take. `""` (Default) is
    always legal and deliberately not listed, `dispatch.MODELS`' own rule."""
    if provider not in PROVIDERS or slot not in SLOTS:
        return ()
    base = tuple(dispatch.MODELS.get(provider) or ())
    if slot == "card-preparer":
        extra = str(card_prepare.HELPER_MODELS.get(provider) or "")
        if extra and extra not in base:
            base = base + (extra,)
    return base


def validate(provider, slot, model, *, override: bool = False) -> bool:
    """True where `model` may be stored for `(provider, slot)`.

    `""` is always legal; `INHERIT` only where `override` is set; anything
    else must be in `allowed`. An unknown provider or slot is refused whatever
    the model — the table's shape is part of the contract.
    """
    if provider not in PROVIDERS or slot not in SLOTS:
        return False
    if not isinstance(model, str):
        return False
    if model == "":
        return True
    if model == INHERIT:
        return bool(override)
    return model in allowed(provider, slot)


def clean(mapping, *, override: bool = False) -> dict[str, dict[str, str]]:
    """`{provider: {slot: model}}` with every unusable entry dropped and its
    neighbours kept — the startup feed's shape. An `INHERIT` value is dropped
    from an override too: stored, it would mean the same as absent."""
    out: dict[str, dict[str, str]] = {}
    if not isinstance(mapping, dict):
        return out
    for provider, row in mapping.items():
        if provider not in PROVIDERS or not isinstance(row, dict):
            continue
        kept: dict[str, str] = {}
        for slot, model in row.items():
            if not validate(provider, slot, model, override=override):
                continue
            if model == INHERIT:
                continue
            kept[str(slot)] = str(model)
        if kept:
            out[str(provider)] = kept
    return out


def clean_overrides(mapping) -> dict[str, dict[str, dict[str, str]]]:
    """The whole override map, each root's table through `clean`, keyed on
    `dispatch.normalise_root`. Empty tables and unusable roots are dropped."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    if not isinstance(mapping, dict):
        return out
    for root, table in mapping.items():
        key = dispatch.normalise_root(str(root or ""))
        if not key:
            continue
        cleaned = clean(table, override=True)
        if cleaned:
            out[key] = cleaned
    return out


def _stored_global(settings) -> dict:
    if not isinstance(settings, dict):
        return {}
    stored = settings.get("agent_models")
    return stored if isinstance(stored, dict) else {}


def _stored_overrides(settings) -> dict:
    if not isinstance(settings, dict):
        return {}
    stored = settings.get("agent_models_by_root")
    return stored if isinstance(stored, dict) else {}


def resolve_global(settings) -> dict[str, dict[str, str]]:
    """The machine-wide table, fully populated: the stored value where it is
    legal, the shipped value where the key is missing or unusable. `""` is
    kept as `""` — an explicit Default, not a fall-through."""
    stored = clean(_stored_global(settings))
    out: dict[str, dict[str, str]] = {}
    for provider in PROVIDERS:
        row = dict(SHIPPED[provider])
        for slot, model in stored.get(provider, {}).items():
            row[slot] = model
        out[provider] = row
    return out


def resolve(settings, root) -> dict[str, dict[str, str]]:
    """The table one project runs on: override → machine-wide → shipped, the
    one precedence rule. `root` is matched through `dispatch.normalise_root`
    against each stored key the same way, so a trailing slash or a symlink
    cannot make a project lose its overrides."""
    out = resolve_global(settings)
    key = dispatch.normalise_root(str(root or ""))
    if not key:
        return out
    overrides = clean_overrides(_stored_overrides(settings))
    table = overrides.get(key)
    if not table:
        return out
    for provider, row in table.items():
        for slot, model in row.items():
            out[provider][slot] = model
    return out


def slots_for(roles: Iterable[str], *, worker: bool = False) -> list[str]:
    """The drawable slots, in `SLOTS` order: `main` plus every role the
    shipped pack can write a brief for, plus `worker` when the caller says
    the shipped pack carries the shunt skill (`pack_render.ships_shunt()`).
    A slot with no brief — or no `workers.json` — anywhere is the inert
    control the settings window refuses to draw."""
    present = set(roles or ())
    if worker:
        present.add("worker")
    else:
        present.discard("worker")
    return [slot for slot in SLOTS if slot == "main" or slot in present]


def allowed_efforts(provider: str, slot: str, model: str = "") -> tuple[str, ...]:
    """The levels a choice for `(provider, slot)` may take when the slot runs
    on `model` (`""` = the assistant's own default model). `()` for a slot with
    no effort cell. `""` (Default) is always legal and never listed."""
    if provider not in PROVIDERS or slot not in EFFORT_SLOTS:
        return ()
    return dispatch.efforts_for(provider, model)


def validate_effort(provider, slot, effort, *, override: bool = False,
                    model=None) -> bool:
    """True where `effort` may be stored for `(provider, slot)`.

    `""` is always legal; `INHERIT` only where `override` is set; anything
    else must be a level the provider offers — for `model` when the caller
    knows the slot's resolved model, else for any model of the provider (the
    stored-table cleaners cannot know it; the launch seam re-checks against
    the real one).
    """
    if provider not in PROVIDERS or slot not in EFFORT_SLOTS:
        return False
    if not isinstance(effort, str):
        return False
    if effort == "":
        return True
    if effort == INHERIT:
        return bool(override)
    if model is None:
        return effort in dispatch.EFFORTS.get(provider, ())
    return effort in allowed_efforts(provider, slot, str(model or ""))


def clean_efforts(mapping, *, override: bool = False) -> dict[str, dict[str, str]]:
    """`clean`'s twin for the effort tables: every unusable entry dropped, its
    neighbours kept; an `INHERIT` value is dropped from an override too."""
    out: dict[str, dict[str, str]] = {}
    if not isinstance(mapping, dict):
        return out
    for provider, row in mapping.items():
        if provider not in PROVIDERS or not isinstance(row, dict):
            continue
        kept: dict[str, str] = {}
        for slot, effort in row.items():
            if not validate_effort(provider, slot, effort, override=override):
                continue
            if effort == INHERIT:
                continue
            kept[str(slot)] = str(effort)
        if kept:
            out[str(provider)] = kept
    return out


def clean_effort_overrides(mapping) -> dict[str, dict[str, dict[str, str]]]:
    """`clean_overrides`' twin: each root's table through `clean_efforts`."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    if not isinstance(mapping, dict):
        return out
    for root, table in mapping.items():
        key = dispatch.normalise_root(str(root or ""))
        if not key:
            continue
        cleaned = clean_efforts(table, override=True)
        if cleaned:
            out[key] = cleaned
    return out


def _stored_efforts(settings) -> dict:
    if not isinstance(settings, dict):
        return {}
    stored = settings.get("agent_efforts")
    return stored if isinstance(stored, dict) else {}


def _stored_effort_overrides(settings) -> dict:
    if not isinstance(settings, dict):
        return {}
    stored = settings.get("agent_efforts_by_root")
    return stored if isinstance(stored, dict) else {}


def _unchecked_efforts_global(settings) -> dict[str, dict[str, str]]:
    stored = clean_efforts(_stored_efforts(settings))
    out: dict[str, dict[str, str]] = {}
    for provider in PROVIDERS:
        row = dict(SHIPPED_EFFORTS[provider])
        for slot, effort in stored.get(provider, {}).items():
            row[slot] = effort
        out[provider] = row
    return out


def _blank_rejected(efforts, models) -> None:
    """Set to `""` (Default) every level the slot's model in `models` does not
    offer — the blanking the launch seam applies on the way out."""
    for provider, row in efforts.items():
        for slot, effort in list(row.items()):
            if effort and not validate_effort(
                    provider, slot, effort,
                    model=models[provider].get(slot, "")):
                row[slot] = ""


def resolve_efforts_global(settings) -> dict[str, dict[str, str]]:
    """The machine-wide effort table, fully populated and **model-checked**:
    the stored level where legal, the shipped level where the key is missing
    or unusable, and `""` (Default) where the slot's resolved model no longer
    offers the level. `""` is an explicit Default, not a fall-through."""
    out = _unchecked_efforts_global(settings)
    _blank_rejected(out, resolve_global(settings))
    return out


def resolve_efforts(settings, root) -> dict[str, dict[str, str]]:
    """The effort table one project runs on: override, machine-wide, shipped.
    A stored level the slot's resolved model does not offer reads as `""`
    (Default), the way a hand-edited model name does on the way out."""
    out = _unchecked_efforts_global(settings)
    key = dispatch.normalise_root(str(root or ""))
    if key:
        table = clean_effort_overrides(_stored_effort_overrides(settings)).get(key)
        for provider, row in (table or {}).items():
            for slot, effort in row.items():
                out[provider][slot] = effort
    _blank_rejected(out, resolve(settings, root))
    return out


def effort_options(settings, root="") -> dict[str, dict[str, list[str]]]:
    """`{provider: {slot: [levels]}}` for the models `root` really runs on
    (`root` empty: the machine-wide models) — what a pop-up may offer."""
    models = resolve(settings, root)
    return {
        provider: {slot: list(allowed_efforts(
            provider, slot, models[provider].get(slot, "")))
            for slot in EFFORT_SLOTS}
        for provider in PROVIDERS
    }


def published_efforts_by_root(settings) -> dict:
    """The per-project effort overrides as the panel draws them: each stored
    entry kept (an absent slot means Inherit) but a level that project's
    resolved model rejects shown as `""` (Default), and the levels each
    project's models offer, keyed like the override map. Roots are the union
    of both stored maps' keys, as stored."""
    # Keyed exactly as stored (the panel looks a project up by the root it
    # sent), each resolved through `resolve`, which canonicalises on its own.
    raw_efforts = _stored_effort_overrides(settings)
    raw_models = _stored_overrides(settings)
    roots = {str(r) for r in raw_efforts if str(r)} | {
        str(r) for r in raw_models if str(r)}
    efforts: dict = {}
    options: dict = {}
    for root in sorted(roots):
        models = resolve(settings, root)
        table = clean_efforts(raw_efforts.get(root), override=True)
        _blank_rejected(table, models)
        if table:
            efforts[root] = table
        options[root] = effort_options(settings, root)
    return {"efforts": efforts, "options": options}


def effort_slots_for(roles: Iterable[str]) -> list[str]:
    """The drawable effort slots, in `EFFORT_SLOTS` order: `main` plus every
    role the shipped pack writes a brief for. Never the preparer or worker."""
    present = set(roles or ())
    return [slot for slot in EFFORT_SLOTS if slot == "main" or slot in present]


def _check_shipped() -> None:
    for provider, row in SHIPPED_EFFORTS.items():
        for slot, effort in row.items():
            if not validate_effort(provider, slot, effort,
                                   model=SHIPPED[provider][slot]):
                raise RuntimeError(
                    f"agent_models.SHIPPED_EFFORTS names {effort!r} for "
                    f"{provider}/{slot}, which the shipped model refuses")
    for provider, row in SHIPPED.items():
        for slot, model in row.items():
            if not validate(provider, slot, model):
                raise RuntimeError(
                    f"agent_models.SHIPPED names {model!r} for "
                    f"{provider}/{slot}, which allowed() refuses")


_check_shipped()

__all__ = [
    "PROVIDERS", "SLOTS", "INHERIT", "SHIPPED",
    "allowed", "validate", "clean", "clean_overrides",
    "resolve_global", "resolve", "slots_for",
    "EFFORT_SLOTS", "SHIPPED_EFFORTS", "allowed_efforts", "validate_effort",
    "clean_efforts", "clean_effort_overrides", "resolve_efforts_global",
    "resolve_efforts", "effort_slots_for", "effort_options",
    "published_efforts_by_root",
]
