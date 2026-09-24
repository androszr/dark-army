"""Workflow obligations across local providers and pure rendered projects.

These assertions prevent instruction drift; they do not prove model compliance.
Fresh native interview/continuation is separately required by the live matrix.

Every check runs over the **resolved** instruction set: the provider's adapter
(`SKILL.md`) followed by the references it names for both modes
(`references/common.md`, `plan.md`, `implement.md`), read exactly as
`tools/ship_efficiency.resolve_workflow` reads them. A mutation is applied to
the resolved text, so an obligation that moved into a reference is still
pinned and a reference an adapter stopped naming is a missing obligation.
"""
import importlib.util
from pathlib import Path

import pytest
from dark_army_menubar import pack_render

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    'ship_efficiency', ROOT / 'tools' / 'ship_efficiency.py')
ship_efficiency = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ship_efficiency)

ADAPTERS = {'.claude': '.claude/skills/ship/SKILL.md',
            '.agents': '.agents/skills/ship/SKILL.md'}
LOCAL_REFERENCES = ('.claude/skills/ship/references/common.md',
                    '.claude/skills/ship/references/plan.md',
                    '.claude/skills/ship/references/implement.md',
                    '.claude/skills/ship/references/scout.md')


def resolved(provider):
    """The adapter plus both modes' references, as an assistant has it."""
    return ship_efficiency.resolve_workflow(ROOT, ADAPTERS[provider])


def workflows():
    for provider in ('.agents', '.claude'):
        yield provider, resolved(provider)
    for profile in ('web', 'ios', 'both'):
        rendered = pack_render.render(profile, 'xx', 'fixture-project')
        assert rendered['.agents/skills/ship/SKILL.md'] == rendered['.claude/skills/ship/SKILL.md']
        for reference in ('common', 'plan', 'implement', 'scout'):
            assert rendered[f'.agents/skills/ship/references/{reference}.md'] == \
                rendered[f'.claude/skills/ship/references/{reference}.md']
        yield profile, ship_efficiency.resolve_rendered_workflow(
            rendered, '.agents/skills/ship/SKILL.md')


def test_both_local_adapters_load_the_same_references_per_mode():
    """One canonical workflow: both adapters name the same three files, common
    first, and each mode loads exactly common plus its own reference."""
    for provider, adapter in ADAPTERS.items():
        text = (ROOT / adapter).read_text()
        for mode in ('plan', 'implement', 'scout'):
            refs = ship_efficiency.workflow_reference_set(adapter, text, mode)
            assert refs == [LOCAL_REFERENCES[0],
                            f'.claude/skills/ship/references/{mode}.md'], (provider, mode)
        for reference in LOCAL_REFERENCES:
            assert (ROOT / reference).is_file()
    # A mode change is an explicit load of the other reference.
    for provider in ADAPTERS:
        adapter = (ROOT / ADAPTERS[provider]).read_text()
        assert 'the other mode\'s reference' in adapter


def test_every_local_reference_is_shared_not_copied():
    """The Codex adapter points at the Claude directory's references rather
    than carrying a second copy that could drift."""
    assert not (ROOT / '.agents/skills/ship/references').exists()


def test_codex_board_tool_list_matches_registered_surface():
    text = (ROOT / '.agents/skills/ship/SKILL.md').read_text()
    board = text.split('- **The board:**', 1)[1].split('- **Closing out:**', 1)[0]
    assert "Codex's restricted board MCP exposes" in board
    for tool in ('dark_army_add_card', 'dark_army_attach_plan',
                 'dark_army_attach_report', 'dark_army_close_card'):
        assert tool in board
    assert 'dark_army_needs_manual_check` is unavailable' in board


@pytest.mark.parametrize('name,text', list(workflows()), ids=[n for n, _ in workflows()])
def test_P04_bounded_interview_and_answers_handoff(name, text):
    assert 'Load `templates/questions.md`' in text
    assert '**at most 3**' in text and 'zero' in text
    assert 'key/value map' in text and 'Objective:' in text and 'verbatim' in text
    assert 'use recommendations' in text
    assert 'original Codex session' in text
    assert 'request_user_input_async' in text and 'collaboration mode' in text
    assert 'acknowledgement only means' in text
    assert 'resume the interview' in text
    assert 'has no question widget' not in text


@pytest.mark.parametrize('name,text', list(workflows()), ids=[n for n, _ in workflows()])
def test_P07_P09_P11_modes_progress_and_honest_completion(name, text):
    text = ' '.join(text.split())
    assert 'Plan: <path>' in text and 'Do not spawn' in text
    assert 'no-refinement attribution result' in text
    assert 'unknown reply' in text and 'duplicate' in text
    assert 'actual column' in text and 'attach once' in text
    for stage in ('interview', 'planning', 'preflight', 'attachment', 'implementation',
                  'verification', 'repair', 'bug audit', 'integration review', 'handoff'):
        assert stage in text
    assert 'canonical role in Codex `agent_type`' in text
    assert 'card title or plan slug' in text
    assert 'dark_army_close_card' in text and 'actual success' in text
    assert 'no outstanding manual work' in text
    assert 'do not claim a manual flag' in text
    assert 'new session, not retries' in text
    assert 'has no close' not in text and 'has none, deliberately' not in text
    assert 'Pause for explicit user approval' not in text


def test_local_question_templates_do_not_require_a_provider_tool():
    for provider in ('.agents', '.claude'):
        text = (ROOT / provider / 'skills/ship/templates/questions.md').read_text()
        assert 'AskUserQuestion' not in text
        assert 'zero questions is valid' in text
        assert 'SKILL.md' in text


# Dark Army-specific stages are local contracts, not obligations of rendered profiles.
LOCAL_PROVIDERS = ('.agents', '.claude')
SECURITY_PATHS = (
    'ios/BobPhone/(Client|Pairing|Push|RelayTransport|RemoteAuth|HomeTransport|HostAddress|Outbox|LeaseReminder|BobPhoneApp)',
    'ios/BobPhoneWidget/',
    '(relay|relay_client|devices|lan_hosts|enrollment|paths|channel_server|'
    'terminal_stream|command_receipts|api_server)',
    '(PairingView|RelaySheet)',
)
SECURITY_CONTENT = (
    'LAN_ACTIONS', 'REMOTE_ACTIONS', '_home_open', 'SealedCodec',
    'note_lan_proof', 'set_lease_days', '_PRIVATE_FILES', 'X-Bob-Token',
)


def _section(text, title):
    """Read an actual heading's body, ending at the next heading of its level."""
    import re

    match = re.search(r'^(#{2,3}) ' + re.escape(title) + r'\n', text, re.M)
    assert match, f'missing stage: {title}'
    next_heading = re.search(r'^#{1,' + str(len(match[1])) + r'} ', text[match.end():], re.M)
    end = match.end() + next_heading.start() if next_heading else len(text)
    return ' '.join(text[match.end():end].split())


def _check_local_workflow(provider, text):
    capabilities = _section(text, 'Required capabilities')
    for obligation in (
        'actual callable agent roster', 'before implementation starts',
        'Plan mode requires `bc-planner`', 'Implement mode requires `bc-implementer`',
        '`bc-verifier` and `bc-bug-auditor`', '`bc-integration-reviewer`',
        '`bc-security-reviewer`', 'accepted plan', 'final delta',
        'name the role and affected stage', 'preserve the plan and card',
        'start no dependent implementation and report incomplete',
        'Never substitute `default`, run a specialist inline, or retry',
        'skipped with its reason',
    ):
        assert obligation in capabilities, f'capability: {obligation}'
    delta = _section(text, 'Review delta')
    for obligation in (
        'captured baseline', 'untracked file bytes', 'newly staged changes',
        'newly created files/content', 'review conservatively',
        'whole content for new files', 'deleted content for removed files',
        'git diff --cached', 'Refresh both after every repair',
    ):
        assert obligation in delta, f'delta: {obligation}'
    del provider  # both adapters resolve to the one canonical workflow
    verify = _section(text, 'Phase 6f: blind verify')
    assert 'Independent `bc-verifier` is required even when every criterion is an executable command' in verify
    assert "implementer's test execution is not independent verification" in verify
    assert 'baseline' in verify and 'context' in verify and 'plan' in verify
    assert 'Never pass it the implementer\'s report' in verify
    assert 'instead of spawning the verifier' not in verify
    assert 'inline verification' not in verify
    assert 'Max 2 verify cycles' in verify and 'at most two repair rounds' in verify
    # Deduplication is inside one verifier pass and never across roles.
    assert 'One execution table per verifier pass' in verify
    assert 'no evidence crosses a role' in verify
    assert 'the two executions are independent' in verify
    assert 'neither may cite the other' in verify
    assert 'no cross-session pass cache' in verify
    bug = _section(text, 'Phase 6.7: bug scan')
    assert '`bc-bug-auditor`' in bug
    assert 'Max 3 iterations' in bug and 'at most three audit/repair rounds' in bug
    budget = _section(text, 'Phase 6.5: the run budget and the question')
    assert 'six implementer dispatches' in budget, 'the run cap must sit inside the stage the checker opens'
    assert 'ship-attempts.json' in budget
    integration = _section(text, 'Phase 6.8: conditional integration review')
    security = _section(text, 'Phase 6.9: conditional security review')
    for stage, role, banner in (
        (integration, 'bc-integration-reviewer', 'integration.txt'),
        (security, 'bc-security-reviewer', 'security.txt'),
    ):
        assert f'spawn `{role}`' in stage and f'banners/{banner}' in stage
        assert 'ship-delta-paths.txt' in stage
        assert 'floor, not a ceiling' in stage
        assert 'on judgment and say why' in stage
        assert 'A `BLOCK` returns to `bc-implementer`' in stage
        assert 'rerun affected verification and the blocking review after repair' in stage
        assert 'does not consume the bug-audit allowance' in stage
        assert 'never permits a success handoff while unresolved' in stage
        assert 'marked `In scope: no` does not return to the implementer' in stage
    assert 'whether or not integration review ran' in security
    assert 'only if integration' not in security
    assert '**either** match spawns the agent' in security
    assert 'ship-delta.patch' in security
    for trigger in SECURITY_PATHS + SECURITY_CONTENT:
        assert trigger in security, f'security trigger: {trigger}'
    # The banner must also be wired in the actual spawn map, not just named later.
    banner_map = _section(text, 'Agent spawn visual convention')
    assert '| `bc-security-reviewer` | — |' in banner_map
    assert '`security.txt`' in banner_map
    _check_development_and_graph_gates(_section(text, 'Gates'))
    close = _section(text, 'Phase 7b: close the card, if and only if everything was checked')
    assert 'no integration or security reviewer returned `BLOCK`' in close
    assert 'no required capability or review remains incomplete' in close
    packet = _section(text, 'The handoff packet')
    assert 'omits the implementer\'s report' in packet
    assert 'Never fork this conversation into the verifier' in packet
    assert 'Repairs reuse the implementer' in packet


def _check_development_and_graph_gates(gates):
    assert 'Packaging/hooks: `cd host && ./build.sh --allow-untagged`' in gates
    assert 'panel and extension freshness stay strict' in gates
    assert 'Never use `--dev` or `--install`' in gates
    assert 'Release builds keep their strict tagged, clean version gate' in gates
    assert 'Before any explicitly authorized commit, run GitNexus `detect_changes({scope: "all"})`' in gates
    assert '`node .gitnexus/run.cjs detect-changes --scope all --repo .`' in gates
    assert 'cannot replace the all-changes check' in gates
    assert '`partial`, `truncated`, unavailable and `UNKNOWN` results are unresolved: do not commit on any of them' in gates
    assert 'Commit, push, install and release still require explicit authorization' in gates


@pytest.mark.parametrize('provider', LOCAL_PROVIDERS)
def test_local_required_reviews_and_capability_refusal(provider):
    _check_local_workflow(provider, resolved(provider))


BOUNDED_LOOP_WORKFLOW = (
    'ship-attempts.json', 'six implementer dispatches',
    '**continue**', '**stop**', '**hand back**',
    'AskUserQuestion', 'ask_user_question', 'request_user_input_async', 'bob-tldr',
    'at a gate result and at completion', 'never send a status ping',
)


def _check_bounded_loop(text):
    """The ledger, the six-dispatch cap, the three-way question on every route,
    and the retired "waiting is work" sentence gone — whitespace-normalised."""
    flat = ' '.join(text.split())
    for phrase in BOUNDED_LOOP_WORKFLOW:
        assert phrase in flat, f'bounded loop: {phrase}'
    assert 'Waiting for a helper is ongoing work' not in flat


@pytest.mark.parametrize('name,text', list(workflows()), ids=[n for n, _ in workflows()])
def test_bounded_loop_and_question_per_provider(name, text):
    _check_bounded_loop(text)


SCOPE_WORKFLOW = (
    'in-scope findings only', 'Out-of-scope rows never drive an iteration',
    'dark_army_add_card', 'Prep', 'Follow-up from:', 'filed_followups',
    'Follow-ups not filed', 'never dropped', 'Iteration log',
    '**requirement**', '**expansion**', '**smallest compliant alternative**',
    '**consequences**', '**recommendation**',
    '**fix here**', '**file it**', 'never decide it yourself',
)


def _check_scope_routing(text):
    """Only in-scope findings drive a fix round; every out-of-scope finding is
    filed as a Prep card naming its origin or listed as an unfiled follow-up,
    never dropped; a serious one is put to the person in five parts — and the
    three-round ceiling survives, whitespace-normalised."""
    flat = ' '.join(text.split())
    for phrase in SCOPE_WORKFLOW:
        assert phrase in flat, f'scope routing: {phrase}'
    assert 'Max 3 iterations' in flat


@pytest.mark.parametrize('name,text', list(workflows()), ids=[n for n, _ in workflows()])
def test_scope_routing_per_provider(name, text):
    _check_scope_routing(text)


def test_authoritative_implementer_keeps_development_bundle_strict():
    text = (ROOT / '.claude/agents/bc-implementer.md').read_text()
    gates = _section(text, 'Gates')
    assert '| Bundle (`bundle`) | `cd host && ./build.sh --allow-untagged`' in gates
    assert 'never substitute `--dev` or `--install`' in gates
    assert 'Release builds still require the strict tagged, clean version gate' in gates


# --- the shunt exemption window -----------------------------------------------------

def _check_exemption_window(text):
    """Every reviewer spawn opens the window, every repair route and the
    handoff close it, and the reviewers' unrestricted read stands."""
    verify = _section(text, 'Phase 6f: blind verify')
    assert 'exempt.py on' in verify and 'exempt.py off' in verify
    bug = _section(text, 'Phase 6.7: bug scan')
    assert 'exempt.py on' in bug and 'exempt.py off' in bug
    assert text.count('exempt.py on') >= 4
    assert text.count('exempt.py off') >= 2
    packet = _section(text, 'The handoff packet')
    assert 'never restricted' in packet
    assert 'reviewer reads by itself, never through a summary' in packet
    assert '.claude/skills/shunt/SKILL.md' in packet
    handoff = _section(text, 'Phase 7: handoff')
    assert 'exempt.py off' in handoff


@pytest.mark.parametrize('name,text', list(workflows()), ids=[n for n, _ in workflows()])
def test_exemption_window_per_provider_and_profile(name, text):
    _check_exemption_window(text)


def test_local_security_and_integration_spawns_open_the_window():
    text = resolved('.claude')
    for stage in ('Phase 6.8: conditional integration review',
                  'Phase 6.9: conditional security review'):
        assert 'exempt.py on' in _section(text, stage), stage
