"""`agent_models`: the shipped table, the allowlist, validation, and the one
precedence rule (override → machine-wide → shipped)."""

import pytest

from dark_army_daemon import agent_models as m
from dark_army_daemon import card_prepare, dispatch


# --- the shipped table -------------------------------------------------------

def test_every_shipped_cell_is_allowed():
    for provider, row in m.SHIPPED.items():
        assert set(row) == set(m.SLOTS), provider
        for slot, model in row.items():
            assert m.validate(provider, slot, model), (provider, slot, model)


def test_shipped_covers_every_provider_with_provider_specific_main_defaults():
    assert set(m.SHIPPED) == set(m.PROVIDERS)
    assert {provider: m.SHIPPED[provider]["main"] for provider in m.PROVIDERS} == {
        "claude": "", "codex": "gpt-6-sol", "grok": "",
    }


def test_shipped_codex_role_policy_is_exact():
    assert m.SHIPPED["codex"] == {
        "main": "gpt-6-sol",
        "planner": "gpt-6-astra",
        "implementer": "gpt-6-sol",
        "verifier": "gpt-6-luna",
        "bug-auditor": "gpt-6-luna",
        "integration-reviewer": "gpt-6-luna",
        "security-reviewer": "gpt-6-astra",
        "card-preparer": "gpt-6-luna",
        "worker": "gpt-6-luna",
    }


def test_shipped_preparer_is_the_existing_helper_model():
    """An untouched install prepares cards exactly as today."""
    for provider in m.PROVIDERS:
        assert m.SHIPPED[provider]["card-preparer"] == card_prepare.HELPER_MODELS[provider]


# --- the allowlist -----------------------------------------------------------

def test_allowed_is_the_dispatch_list_for_an_ordinary_slot():
    assert m.allowed("codex", "main") == tuple(dispatch.MODELS["codex"])
    assert m.allowed("claude", "planner") == tuple(dispatch.MODELS["claude"])


def test_current_codex_catalogue_is_offered_on_every_slot():
    assert dispatch.MODELS["codex"] == (
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
    )
    for slot in m.SLOTS:
        assert m.allowed("codex", slot) == dispatch.MODELS["codex"]
        assert m.validate("codex", slot, "gpt-6-sol")
        assert m.validate("codex", slot, "gpt-6-luna")


def test_grok_4_7_is_offered_on_every_grok_settings_slot():
    """Agent models chips are `allowed()`, which is the card catalogue.
    The new pin is first, and legal to store, on every grok slot."""
    assert dispatch.MODELS["grok"][0] == "grok-4.7"
    for slot in m.SLOTS:
        assert m.allowed("grok", slot)[0] == "grok-4.7"
        assert m.validate("grok", slot, "grok-4.7")


def test_the_shipped_helper_models_need_no_widening():
    # Every shipped helper model is already on the dispatch list: no duplicate.
    for provider in ("codex", "grok"):
        assert m.allowed(provider, "card-preparer") == tuple(
            dispatch.MODELS[provider])


def test_allowed_widens_by_the_helper_model_for_the_preparer_alone(
        monkeypatch):
    monkeypatch.setitem(card_prepare.HELPER_MODELS, "codex", "gpt-helper-only")
    assert m.allowed("codex", "card-preparer") == (
        tuple(dispatch.MODELS["codex"]) + ("gpt-helper-only",))
    assert m.validate("codex", "card-preparer", "gpt-helper-only")
    # preparer-only: never legal on another slot
    for slot in ("main", "planner"):
        assert "gpt-helper-only" not in m.allowed("codex", slot)
        assert not m.validate("codex", slot, "gpt-helper-only")


def test_allowed_is_empty_off_the_table():
    assert m.allowed("gemini", "main") == ()
    assert m.allowed("claude", "app-reviewer") == ()


# --- validate ----------------------------------------------------------------

@pytest.mark.parametrize("provider, slot, model", [
    ("gemini", "main", ""),               # unknown provider
    ("claude", "app-reviewer", ""),       # unknown slot
    ("claude", "main", "gpt-5.5"),        # another provider's model
    ("claude", "main", "claude-opus-9"),  # does not exist
    ("codex", "main", "gpt-4-retired"),   # on no list at all
    ("codex", "planner", "gpt-4-retired"),
    ("claude", "main", None),             # not a string
])
def test_validate_refuses(provider, slot, model):
    assert not m.validate(provider, slot, model)


def test_validate_accepts_default_and_every_listed_name():
    for provider in m.PROVIDERS:
        for slot in m.SLOTS:
            assert m.validate(provider, slot, "")
            for name in m.allowed(provider, slot):
                assert m.validate(provider, slot, name)


def test_inherit_is_legal_only_for_an_override():
    assert not m.validate("claude", "main", m.INHERIT)
    assert m.validate("claude", "main", m.INHERIT, override=True)


# --- resolve -----------------------------------------------------------------

def test_resolve_global_is_shipped_when_nothing_is_stored():
    assert m.resolve_global({}) == m.SHIPPED
    assert m.resolve_global({"agent_models": "junk"}) == m.SHIPPED


def test_resolve_global_prefers_the_stored_value_and_keeps_the_rest():
    table = m.resolve_global({"agent_models": {"claude": {"planner": "sonnet"}}})
    assert table["claude"]["planner"] == "sonnet"
    assert table["claude"]["implementer"] == m.SHIPPED["claude"]["implementer"]
    assert table["codex"] == m.SHIPPED["codex"]


def test_an_empty_string_is_an_explicit_default_not_absent():
    stored = {"agent_models": {"claude": {"planner": ""}}}
    assert m.resolve_global(stored)["claude"]["planner"] == ""
    absent = {"agent_models": {"claude": {}}}
    assert m.resolve_global(absent)["claude"]["planner"] == "opus"


def test_resolve_prefers_override_over_global_over_shipped():
    settings = {
        "agent_models": {"claude": {"planner": "sonnet", "verifier": "haiku"}},
        "agent_models_by_root": {"/tmp/proj": {"claude": {"planner": "opus"}}},
    }
    table = m.resolve(settings, "/tmp/proj")
    assert table["claude"]["planner"] == "opus"        # override
    assert table["claude"]["verifier"] == "haiku"      # global
    assert table["claude"]["bug-auditor"] == "sonnet"  # shipped
    other = m.resolve(settings, "/tmp/other")
    assert other["claude"]["planner"] == "sonnet"


def test_resolve_matches_the_root_through_normalisation():
    settings = {"agent_models_by_root": {"/tmp/proj/": {"grok": {"main": "grok-4.5"}}}}
    assert m.resolve(settings, "/tmp/proj")["grok"]["main"] == "grok-4.5"


def test_an_override_default_wins_over_a_global_choice():
    settings = {
        "agent_models": {"codex": {"main": "gpt-5.5"}},
        "agent_models_by_root": {"/tmp/proj": {"codex": {"main": ""}}},
    }
    assert m.resolve(settings, "/tmp/proj")["codex"]["main"] == ""


def test_resolve_drops_an_unknown_name_and_falls_back():
    """A hand-edited file naming a model Dark Army does not know leaves that
    choice unset — the shipped value — rather than launching on it."""
    settings = {"agent_models": {"claude": {"planner": "made-up", "verifier": "haiku"}}}
    table = m.resolve_global(settings)
    assert table["claude"]["planner"] == m.SHIPPED["claude"]["planner"]
    assert table["claude"]["verifier"] == "haiku"


# --- clean -------------------------------------------------------------------

def test_clean_drops_a_bad_entry_and_keeps_its_neighbours():
    cleaned = m.clean({
        "claude": {"planner": "opus", "verifier": "nope", "app-reviewer": "opus"},
        "codex": {"main": "gpt-4-retired"},
        "gemini": {"main": ""},
        "grok": "junk",
    })
    assert cleaned == {"claude": {"planner": "opus"}}


def test_clean_never_stores_inherit():
    assert m.clean({"claude": {"planner": m.INHERIT}}, override=True) == {}
    assert m.clean({"claude": {"planner": m.INHERIT, "main": ""}},
                   override=True) == {"claude": {"main": ""}}


def test_clean_overrides_keys_on_the_canonical_root():
    out = m.clean_overrides({
        "/tmp/proj/": {"claude": {"planner": "opus"}},
        "": {"claude": {"planner": "opus"}},
        "/tmp/empty": {"claude": {"planner": "nope"}},
    })
    assert out == {dispatch.normalise_root("/tmp/proj"): {"claude": {"planner": "opus"}}}


# --- slots -------------------------------------------------------------------

def test_slots_for_keeps_slot_order_and_always_has_main():
    assert m.slots_for(()) == ["main"]
    assert m.slots_for({"verifier", "planner", "app-reviewer"}) == [
        "main", "planner", "verifier"]


# --- the worker slot -----------------------------------------------------------

def test_the_worker_slot_ships_the_shunt_helpers_model_and_it_is_allowed():
    """The shunt skill's cheap helper per assistant, on the ordinary
    allowlist: no headless-only widening, unlike the preparer's."""
    assert "worker" in m.SLOTS
    assert m.SLOTS.index("worker") == m.SLOTS.index("card-preparer") + 1
    for provider in m.PROVIDERS:
        assert m.SHIPPED[provider]["worker"] == card_prepare.WORKER_MODELS[provider]
        assert m.validate(provider, "worker", m.SHIPPED[provider]["worker"])
        assert m.allowed(provider, "worker") == tuple(dispatch.MODELS[provider])


def test_validate_refuses_an_off_list_worker():
    assert not m.validate("codex", "worker", "gpt-4-retired")
    assert not m.validate("claude", "worker", "gpt-5.6-luna")
    assert not m.validate("grok", "worker", "made-up")


def test_slots_for_draws_the_worker_only_when_the_pack_carries_the_skill():
    roles = {"planner", "implementer", "verifier"}
    assert "worker" not in m.slots_for(roles)
    assert "worker" not in m.slots_for(roles | {"worker"})
    with_worker = m.slots_for(roles, worker=True)
    assert with_worker[-1] == "worker" and with_worker[0] == "main"
    assert m.slots_for((), worker=True) == ["main", "worker"]


def test_a_stored_worker_choice_resolves_like_any_other_slot():
    table = m.resolve({"agent_models": {"codex": {"worker": "gpt-5.5"}},
                       "agent_models_by_root": {"/tmp/p": {"claude": {"worker": "sonnet"}}}},
                      "/tmp/p")
    assert table["codex"]["worker"] == "gpt-5.5"
    assert table["claude"]["worker"] == "sonnet"
    assert table["grok"]["worker"] == card_prepare.WORKER_MODELS["grok"]


def test_the_module_never_names_the_pack_allowlist():
    """`test_pack_destinations_named_in_exactly_two_files`' rule, stated here
    so the reason is beside the module: the write allowlist is
    `pack_install`'s alone."""
    from pathlib import Path
    text = Path(m.__file__).read_text(encoding="utf-8")
    assert "PACK_" + "DESTINATIONS" not in text


# --- the reasoning effort beside each model ----------------------------------

def test_every_shipped_effort_cell_is_allowed_for_the_shipped_model():
    assert set(m.SHIPPED_EFFORTS) == set(m.PROVIDERS)
    for provider, row in m.SHIPPED_EFFORTS.items():
        assert set(row) == set(m.EFFORT_SLOTS), provider
        for slot, effort in row.items():
            assert m.validate_effort(provider, slot, effort,
                                     model=m.SHIPPED[provider][slot])


def test_the_codex_roles_ship_high_and_everything_else_ships_default():
    assert m.SHIPPED_EFFORTS["codex"]["main"] == ""
    for slot in m.EFFORT_SLOTS[1:]:
        assert m.SHIPPED_EFFORTS["codex"][slot] == "high"
    for provider in ("claude", "grok"):
        assert set(m.SHIPPED_EFFORTS[provider].values()) == {""}


def test_effort_slots_have_no_preparer_or_worker():
    assert "card-preparer" not in m.EFFORT_SLOTS and "worker" not in m.EFFORT_SLOTS
    slots = m.effort_slots_for(["planner", "worker", "card-preparer"])
    assert slots == ["main", "planner"]
    assert m.effort_slots_for([])[0] == "main"


def test_validate_effort_default_inherit_and_levels():
    assert m.validate_effort("claude", "main", "")
    assert m.validate_effort("claude", "main", "high")
    assert not m.validate_effort("claude", "main", "ultra")
    assert not m.validate_effort("claude", "main", "inherit")
    assert m.validate_effort("claude", "main", "inherit", override=True)
    assert not m.validate_effort("claude", "worker", "low")
    assert not m.validate_effort("gemini", "main", "")
    assert not m.validate_effort("claude", "main", 3)
    assert not m.validate_effort("grok", "main", "max")
    assert m.validate_effort("grok", "main", "minimal")


def test_validate_effort_is_judged_against_the_model_when_known():
    assert m.validate_effort("codex", "main", "max", model="gpt-6-sol")
    assert not m.validate_effort("codex", "main", "max", model="gpt-5.5")
    assert m.validate_effort("codex", "main", "max")


def test_allowed_efforts_follow_the_model():
    assert "max" in m.allowed_efforts("codex", "planner", "gpt-6-astra")
    assert "max" not in m.allowed_efforts("codex", "planner", "gpt-5.5")
    assert m.allowed_efforts("codex", "card-preparer", "") == ()


def test_clean_efforts_drops_the_unusable_and_keeps_neighbours():
    out = m.clean_efforts({"claude": {"planner": "low", "main": "ultra",
                                      "worker": "low"},
                           "gemini": {"main": "low"}, "grok": "x"})
    assert out == {"claude": {"planner": "low"}}
    assert m.clean_efforts({"claude": {"planner": "inherit"}}) == {}
    assert m.clean_efforts({"claude": {"planner": "inherit"}}, override=True) == {}
    assert m.clean_efforts("nope") == {}


def test_effort_precedence_override_global_shipped_and_empty_is_explicit():
    settings = {"agent_efforts": {"claude": {"planner": "medium"},
                                  "codex": {"planner": ""}},
                "agent_efforts_by_root": {"/tmp/p": {"claude": {"planner": "low"}}}}
    glob = m.resolve_efforts_global(settings)
    assert glob["claude"]["planner"] == "medium"
    assert glob["claude"]["verifier"] == ""
    assert glob["codex"]["planner"] == ""           # explicit Default
    assert glob["codex"]["verifier"] == "high"       # shipped
    assert m.resolve_efforts(settings, "/tmp/p")["claude"]["planner"] == "low"
    assert m.resolve_efforts(settings, "/tmp/other")["claude"]["planner"] == "medium"


def test_resolve_efforts_reads_a_level_the_resolved_model_rejects_as_default():
    settings = {"agent_models": {"codex": {"verifier": "gpt-5.5"}},
                "agent_efforts": {"codex": {"verifier": "max"}}}
    assert m.resolve_efforts(settings, "")["codex"]["verifier"] == ""
    settings["agent_models"] = {"codex": {"verifier": "gpt-6-luna"}}
    assert m.resolve_efforts(settings, "")["codex"]["verifier"] == "max"


def test_clean_effort_overrides_keys_on_the_canonical_root():
    out = m.clean_effort_overrides({"/tmp/p/": {"claude": {"main": "low"}},
                                    "": {"claude": {"main": "low"}},
                                    "/tmp/q": {"claude": {"main": "bogus"}}})
    assert list(out.values()) == [{"claude": {"main": "low"}}]


def test_published_global_efforts_are_model_checked():
    settings = {"agent_models": {"codex": {"verifier": "gpt-5.5"}},
                "agent_efforts": {"codex": {"verifier": "max"}}}
    assert m.resolve_efforts_global(settings)["codex"]["verifier"] == ""
    settings["agent_models"] = {"codex": {"verifier": "gpt-6-luna"}}
    assert m.resolve_efforts_global(settings)["codex"]["verifier"] == "max"
    # A project whose own model offers the level still gets it from the
    # machine-wide choice even where the global model would blank it.
    settings = {"agent_models": {"codex": {"verifier": "gpt-5.5"}},
                "agent_models_by_root": {"/tmp/p": {"codex": {"verifier": "gpt-6-luna"}}},
                "agent_efforts": {"codex": {"verifier": "max"}}}
    assert m.resolve_efforts_global(settings)["codex"]["verifier"] == ""
    assert m.resolve_efforts(settings, "/tmp/p")["codex"]["verifier"] == "max"


def test_effort_options_follow_each_projects_models():
    settings = {"agent_models": {"codex": {"verifier": "gpt-6-luna"}},
                "agent_models_by_root": {"/tmp/p": {"codex": {"verifier": "gpt-5.5"}}}}
    assert "max" in m.effort_options(settings)["codex"]["verifier"]
    assert "max" not in m.effort_options(settings, "/tmp/p")["codex"]["verifier"]


def test_published_by_root_blanks_rejected_entries_and_carries_options():
    settings = {"agent_models_by_root": {"/tmp/p": {"codex": {"verifier": "gpt-5.5"}}},
                "agent_efforts_by_root": {"/tmp/p": {"codex": {"verifier": "max",
                                                              "planner": "low"}}}}
    out = m.published_efforts_by_root(settings)
    root = "/tmp/p"
    assert out["efforts"][root]["codex"] == {"verifier": "", "planner": "low"}
    assert "max" not in out["options"][root]["codex"]["verifier"]
    assert "max" in out["options"][root]["codex"]["planner"]
    assert m.published_efforts_by_root({}) == {"efforts": {}, "options": {}}
