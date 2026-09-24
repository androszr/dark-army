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


def _check_shipped() -> None:
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
]
