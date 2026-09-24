"""Isolated P01–P14 journey seams; native provenance is in fixtures/README.md.

Synthetic journal builders below model compatibility and boundary cases; they
are never described as captures from a particular installed provider version.
"""
import json
import time
from pathlib import Path

import pytest

from dark_army_daemon import codex_rollouts as cr
from dark_army_daemon.daemon import BobDaemon


def row(kind, **payload):
    return {'timestamp': '2026-09-12T16:52:57Z', 'type': kind, 'payload': payload}


def message(text, role='assistant'):
    return row('response_item', type='message', role=role,
               content=[{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}])


def call(name, args, cid='c1'):
    return row('response_item', type='function_call', name=name, arguments=json.dumps(args), call_id=cid)


def result(data, cid='c1'):
    return row('response_item', type='function_call_output', output=json.dumps(data), call_id=cid)


def journal(tmp_path, events=(), thread='root', parent='', role='', name='Newton'):
    path = tmp_path / '2026/09/12' / f'rollout-{thread}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    source = {'subagent': {'thread_spawn': {'parent_thread_id': parent, 'agent_role': role,
              'agent_nickname': name, 'depth': 1}}} if parent else 'cli'
    rows = [row('session_meta', id=thread, cwd=str(tmp_path), originator='codex-tui',
                source=source, thread_source='user'),
            row('event_msg', type='task_started', turn_id='turn1'), *events]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return path


def spawn(role='bc-planner', child='child'):
    return [call('collaboration.spawn_agent', {'agent_type': role, 'task_name': 'parity_work',
                    'message': 'Plan: codex-session-parity'}), result({'agent_id': child})]


def test_P03_async_ack_preserves_pending(tmp_path):
    p = journal(tmp_path, [call('request_user_input_async', {'questions': [
        {'title': 'Which surface?', 'options': ['Panel', 'Both']}]}), result({'accepted': True})])
    rec = cr.parse_rollout(p)
    assert rec.stats.question['text'] == 'Which surface?'
    assert rec.activity == 'waiting'


@pytest.mark.parametrize('ending', ['task_complete', 'task_completed'])
@pytest.mark.parametrize('after', ['pending', 'ack', 'unknown', 'stale', 'answers',
                                 'cancel', 'user-reply', 'new-turn', 'abort'])
def test_async_question_outlives_completed_turn(tmp_path, journey, ending, after):
    # Native sequence observed on 2026-09-23: async registration, accepted:true,
    # final prose, task_complete. Codex still shows a queued question afterwards.
    # Content and identifiers below are synthetic; no private transcript is kept.
    events = [
        call('request_user_input_async', {'questions': [
            {'title': 'What next?', 'options': ['Continue', 'Stop', 'Hand back']},
            {'title': 'Any constraints?'}]}, 'ask'),
        result({'accepted': True}, 'ask'),
        message('Waiting for your decision.'),
        row('event_msg', type=ending, turn_id='turn1'),
    ]
    following = {
        'ack': result({'accepted': True}, 'ask'),
        'unknown': result({}, 'ask'),
        'stale': row('event_msg', type='turn_aborted', turn_id='old-turn'),
        'answers': result({'answers': {'next': {'answers': ['Continue']}}}, 'ask'),
        'cancel': result({'cancelled': True}, 'ask'),
        'user-reply': message('Hand back.', role='user'),
        'new-turn': row('event_msg', type='task_started', turn_id='turn2'),
        'abort': row('event_msg', type='turn_aborted', turn_id='turn1'),
    }
    if after in following:
        events.append(following[after])
    rec = cr.parse_rollout(journal(tmp_path, events))
    entry, snapshot = publish(journey[0], rec)
    pending = after in ('pending', 'ack', 'unknown', 'stale')
    assert bool(entry['question']) is pending
    assert bool(entry['questions']) is pending
    if pending:
        assert entry in snapshot['waiting']
        assert entry['question']['text'] == 'What next?'
        assert entry['questions'][0]['options'] == ['Continue', 'Stop', 'Hand back']
        assert entry['questions'][1]['text'] == 'Any constraints?'
        assert not rec.turn_active and rec.current_tool == ''
        assert not cr.stopped_turn(rec)
        assert not entry['can_close'] and not entry['channel'] and not entry['can_type']
    if after == 'answers':
        assert rec.stats.question_results[-1]['outcome'] == 'next: Continue'


def test_P05_role_survives_task_name(tmp_path):
    rec = cr.parse_rollout(journal(tmp_path, spawn()))
    assert rec.stats.agents['child'].subagent_type == 'bc-planner'


def test_P06_completed_child_history_without_running(tmp_path, monkeypatch):
    journal(tmp_path, [*spawn(), row('event_msg', type='task_complete', turn_id='turn1')])
    journal(tmp_path, [row('event_msg', type='task_complete', turn_id='turn1')],
            thread='child', parent='root', role='bc-planner')
    monkeypatch.setattr(cr, 'attach_process_ids', lambda records: None)
    rec = next(r for r in cr.load_recent(tmp_path) if r.thread_id == 'root')
    assert rec.stats.agents == {}
    assert rec.observed_roles == ['bc-planner']


def test_P10_report_survives_chatter(tmp_path):
    report = '## Work done\n\n**Asked:** Test.\n**Changed:** Parser.\n**Verified:** Replay.\n**Unchecked:** Live.'
    rec = cr.parse_rollout(journal(tmp_path, [message(report), message('Attachment succeeded.')]))
    assert rec.stats.last_report == report
    assert rec.stats.last_text == 'Attachment succeeded.'

from dark_army_daemon import dispatch
from dark_army_daemon.board import BoardStore
from tests.test_board_refine import refinement_handoff  # noqa: F401 — shared isolated fixture


@pytest.fixture
def journey(tmp_path, monkeypatch):
    d = BobDaemon(headless=True, sessions_path=tmp_path / 'sessions.json')
    store = BoardStore(tmp_path / 'board.db')
    store.connect()
    d._board = store
    monkeypatch.setattr(d, '_refresh_codex_records', lambda: None)
    monkeypatch.setattr(d, '_refresh_grok_records', lambda: None)
    monkeypatch.setattr(d, '_session_reachable', lambda sid: False)
    monkeypatch.setattr(d, '_schedule_agents_push', lambda: None)
    monkeypatch.setattr(d, '_known_project_roots', lambda: {str(tmp_path)})
    monkeypatch.setattr(cr, 'attach_process_ids', lambda records: None)
    yield d, store
    store.close()


def publish(d, record):
    d._codex_records = {record.session_id: record}
    snapshot = d._enrich_agent_stubs(d._collect_agent_stubs())
    return next(e for entries in snapshot.values() for e in entries
                if e['session_id'] == record.session_id), snapshot


def make_card(store, tmp_path, **kwargs):
    card, detail = store.create({'title': 'Parity fixture', 'summary': 'A complete brief',
        'project': tmp_path.name, 'root': str(tmp_path), 'tool': 'codex',
        'prompt': 'Preserve all full instructions.', **kwargs})
    assert card, detail
    internal = {k: v for k, v in kwargs.items() if k in ('session_id', 'link_state', 'dispatched_at', 'refine_session_id', 'refine_state')}
    if internal:
        store.update(card['id'], internal)
    return store.get(card['id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('hosted', [False, True])
async def test_P01_refine_complete_prompt_and_route(tmp_path, monkeypatch, journey, hosted):
    d, store = journey
    card = make_card(store, tmp_path, beneficiary='Maintainers',
                     intended_benefit='Understand progress', success_criterion='One visible journey')
    calls = []
    async def accept(root, argv, name, **kwargs):
        calls.append((root, argv, kwargs))
        return True, 'fixture', None
    monkeypatch.setattr(dispatch, 'resolve_executable', lambda tool: '/bin/'+tool)
    monkeypatch.setattr(dispatch, 'spawn_local' if hosted else 'spawn', accept)
    d.board_own_terminal_enabled = hosted
    ok, detail = await d.refine_card(card['id'])
    assert ok, detail
    assert len(calls) == 1
    root, argv, _ = calls[0]
    assert root == str(tmp_path) and argv[0] == '/bin/codex'
    assert argv[-2] == '--'
    prompt = argv[-1]
    assert '.agents/skills/ship/SKILL.md' in prompt and 'planning-only' in prompt
    for text in ('A complete brief', 'Parity fixture', 'Preserve all full instructions.',
                 'Maintainers', 'Understand progress', 'One visible journey'):
        assert prompt.count(text) == 1
    got = store.get(card['id'])
    assert got['refine_state'] == 'dispatching' and got['column_name'] == 'prep'
    assert got['session_id'] == ''

@pytest.mark.parametrize('native', [True, False], ids=['native-0.154.0-ack', 'synthetic-sync'])
def test_P02_question_beats_helper_and_has_original_session_guidance(tmp_path, journey, native):
    d, _ = journey
    if native:
        fixtures = Path(__file__).parent / 'fixtures/codex_session_parity'
        target = tmp_path / '2026/09/12'
        target.mkdir(parents=True, exist_ok=True)
        for name in ('root', 'child'):
            (target / f'rollout-{name}.jsonl').write_bytes((fixtures / f'native-{name}.jsonl').read_bytes())
        rec = next(r for r in cr.load_recent(tmp_path) if r.thread_id == 'native-root')
        assert rec.stats.agents['native-child'].subagent_type == 'bc-implementer'
        assert rec.observed_roles == ['bc-implementer']
    else:
        rec = cr.parse_rollout(journal(tmp_path, [*spawn(), call('request_user_input', {
            'questions': [{'question': 'Choose the visible surface?', 'options': [{'label': 'Panel'}]}]}, 'ask')]))
    entry, snapshot = publish(d, rec)
    assert entry in snapshot['waiting']
    assert entry['questions'] and entry['question']['id']
    assert entry['subagent_rows']
    assert not entry['can_type'] and not entry['channel']
    assert 'original Codex session' in entry['interaction_note']


def test_P02_text_fallback_is_explicit_wait_not_inferred_question(tmp_path, journey):
    d, _ = journey
    rec = cr.parse_rollout(journal(tmp_path, [message(
        'Choose the surface? Answer in the original Codex session. '
        '<!-- bob-tldr: Choose the surface before planning. -->'),
        row('event_msg', type='task_complete', turn_id='turn1')]))
    entry, snap = publish(d, rec)
    assert entry in snap['waiting'] and entry['question'] == {}
    assert entry['last_summary'] == 'Choose the surface before planning.'
    assert not entry['channel']


@pytest.mark.parametrize('transition,clears', [
    ('ack', False), ('unrelated', False), ('stale', False), ('unknown', False),
    ('answers', True), ('cancel', True), ('new-turn', True), ('abort', True), ('user-reply', True),
])
def test_P03_async_lifecycle_synthetic_transitions(tmp_path, journey, transition, clears):
    events = [call('request_user_input_async', {'questions': [{'title': 'Surface?', 'options': ['Panel']}]}),
              result({'accepted': True}), *spawn('bc-verifier', 'verifier')]
    # Spawn uses c1 too in the helper builder; use a separate question identity.
    events[0]['payload']['call_id'] = events[1]['payload']['call_id'] = 'ask'
    endings = {
        'ack': result({'accepted': True}, 'ask'), 'unrelated': result({'answers': {}}, 'other'),
        'stale': result({'answers': {}}, 'ask'), 'unknown': result({}, 'ask'),
        'answers': result({'answers': {'surface': {'answers': ['Panel']}}}, 'ask'),
        'cancel': result({'cancelled': True}, 'ask'),
        'new-turn': row('event_msg', type='task_started', turn_id='turn2'),
        'abort': row('event_msg', type='turn_aborted', turn_id='turn1'),
        'user-reply': message('Use the panel.', role='user'),
    }
    ending = endings[transition]
    if transition == 'stale':
        ending['payload']['internal_chat_message_metadata_passthrough'] = {'turn_id': 'old-turn'}
    rec = cr.parse_rollout(journal(tmp_path, [*events, ending]))
    entry, _ = publish(journey[0], rec)
    assert bool(entry['question']) is not clears
    assert not entry['can_type'] and not entry['channel']
    if transition == 'answers':
        assert rec.stats.question_results[-1]['outcome'] == 'surface: Panel'


@pytest.mark.parametrize('name', ['functions.request_user_input', 'functions.request_user_input_async'])
@pytest.mark.parametrize('encoded', [False, True])
def test_P03_synthetic_alias_object_and_json_input(tmp_path, name, encoded):
    question = {'title': 'Surface?', 'options': ['Panel']} if name.endswith('async') else {'question': 'Surface?'}
    event = call(name, {'questions': [question]})
    if not encoded:
        event['payload']['input'] = json.loads(event['payload'].pop('arguments'))
    rec = cr.parse_rollout(journal(tmp_path, [event]))
    assert rec.stats.question['text'] == 'Surface?'


def test_P04_fully_specified_assessment_publishes_no_fabricated_stage(tmp_path, journey):
    rec = cr.parse_rollout(journal(tmp_path, [message(
        'The brief settles the surface and read-only behavior. No questions are needed; '
        'I will pass these assumptions to the planner.')]))
    entry, _ = publish(journey[0], rec)
    assert entry['question'] == {} and entry['subagent_rows'] == []
    assert rec.observed_roles == []
    assert 'No questions' in entry['last_text']


@pytest.mark.parametrize('ending', ['task_complete', 'turn_aborted'])
def test_P06_P09_fast_stage_history_persists_and_repeats_keep_faces(tmp_path, journey, ending):
    d, store = journey
    roles = ['bc-implementer', 'bc-verifier', 'bc-bug-auditor']
    events = []
    for i, role in enumerate(roles):
        events.extend(spawn(role, f'child{i}'))
        journal(tmp_path, [row('event_msg', type=ending, turn_id='turn1')],
                thread=f'child{i}', parent='root', role=role)
    events.extend([message('Verification and bug audit completed; integration review skipped: no integration seam.'),
                   row('event_msg', type='task_complete', turn_id='turn1')])
    journal(tmp_path, events)
    rec = next(r for r in cr.load_recent(tmp_path) if r.thread_id == 'root')
    card = make_card(store, tmp_path, session_id=rec.session_id, link_state='live', column_name='in_progress')
    entry, snapshot = publish(d, rec)
    d._reconcile_board(snapshot)
    got = store.get(card['id'])
    assert got['agent_trail'].splitlines() == roles
    assert rec.stats.agents == {} and entry['subagent_rows'] == []
    assert entry in snapshot['waiting']
    assert 'bc-integration-reviewer' not in got['agent_trail']
    crew = got['crew_trail']
    d._reconcile_board(snapshot)
    assert store.get(card['id'])['crew_trail'] == crew
    for path in (tmp_path/'2026/09/12').glob('rollout-child*.jsonl'):
        path.unlink()
    store.close()
    store.connect()
    assert store.get(card['id'])['crew_trail'] == crew
    assert store.get(card['id'])['agent_trail'].splitlines() == roles


def test_P06_failed_spawn_and_duplicate_journal_never_invent_role(tmp_path, journey):
    p = journal(tmp_path, [call('spawn_agent', {'agent_type': 'bc-verifier'}), result({'error': 'No slot'})])
    copy = p.with_name('rollout-copy.jsonl')
    copy.write_bytes(p.read_bytes())
    roots = cr.load_recent(tmp_path)
    assert len(roots) == 1 and roots[0].observed_roles == [] and roots[0].stats.agents == {}


def test_P06_followup_can_reactivate_completed_role(tmp_path, journey):
    events = [*spawn(), row('event_msg', type='item_completed', item={'type': 'collabAgentToolCall',
        'receiverThreadIds': ['child'], 'agentsStates': {'child': {'status': 'completed'}}}),
        call('collaboration.followup_task', {'target': 'child', 'message': 'Verify the repair'}, 'again'),
        result({'accepted': True}, 'again')]
    rec = cr.parse_rollout(journal(tmp_path, events))
    entry, snapshot = publish(journey[0], rec)
    assert rec.observed_roles == ['bc-planner']
    assert rec.stats.agents['child'].subagent_type == 'bc-planner'
    assert entry in snapshot['running']


@pytest.mark.asyncio
@pytest.mark.parametrize('authored', [False, True])
async def test_P07_attach_same_card_once_and_report_real_column(refinement_handoff, authored):
    d, store, card, plan, records, _, posts = refinement_handoff
    sid = records[0].session_id
    if not authored:
        store.update(card['id'], {'refine_session_id': sid, 'refine_state': 'live'})
    attached, detail = await d.attach_plan_by_session(sid, str(plan))
    assert attached, detail
    assert attached['id'] == card['id'] and attached['column_name'] == 'backlog'
    again, detail = await d.attach_plan_by_session(sid, str(plan))
    assert not again and detail
    assert len(store.cards()) == 1 and store.get(card['id'])['column_name'] == 'backlog'
    assert posts == []


@pytest.mark.asyncio
async def test_P07_refused_attribution_creates_nothing(refinement_handoff):
    d, store, card, plan, records, _, posts = refinement_handoff
    attached, detail = await d.attach_plan_by_session(records[1].session_id, str(plan))
    assert attached is None and detail
    assert len(store.cards()) == 1 and store.get(card['id'])['column_name'] == 'prep'
    assert posts == []


@pytest.mark.asyncio
async def test_P08_start_uses_plan_and_keeps_missing_changed_plan_gates(tmp_path, journey):
    d, store = journey
    plan = tmp_path/'accepted.md'
    plan.write_text('# Accepted plan\n')
    card = make_card(store, tmp_path, prompt='Old idea, please design it', refine_session_id='codex:root', refine_state='live')
    assert store.attach_plan(card['id'], str(plan), 'codex:root')[0]
    card = store.get(card['id'])
    prompt = dispatch.start_prompt(card)
    assert prompt.startswith('Plan: '+str(plan))
    assert 'do not re-plan' in prompt and 'Old idea' not in prompt
    assert not await d._plan_gate_refusal(card)
    changed = dict(card, plan_approved='wrong-digest')
    assert 'changed' in await d._plan_gate_refusal(changed)
    plan.unlink()
    assert await d._plan_gate_refusal(card)


@pytest.mark.parametrize('next_event,retained', [
    ('tool', True), ('synthetic-context', True), ('new-user', False),
])
def test_P10_report_lifetime_finished_row_and_work_record(tmp_path, journey, next_event, retained):
    d, store = journey
    report = '## Work done\n**Asked:** Parity.\n**Changed:** Observation.\n**Verified:** Replay.\n**Unchecked:** Live checks.'
    tail = {'tool': result({'ok': True}),
            'synthetic-context': message('<environment_context>context</environment_context>', 'user'),
            'new-user': message('Start another task.', 'user')}[next_event]
    rec = cr.parse_rollout(journal(tmp_path, [message(report), message('Card left open for live checks.'), tail,
                                             row('event_msg', type='task_complete', turn_id='turn1')]))
    entry, snapshot = publish(d, rec)
    assert bool(entry['last_report']) is retained
    card = make_card(store, tmp_path, session_id=rec.session_id, link_state='live',
                     column_name='in_progress', dispatched_at=time.time())
    d._record_finished(rec.session_id, d._codex_finished_state(rec), 'no process')
    d._codex_records = {}
    finished = d._enrich_agent_stubs(d._collect_agent_stubs())
    row_finished = next(r for r in finished['finished'] if r['session_id'] == rec.session_id)
    assert row_finished['last_report'] == (report if retained else '')
    d._consider_work_record(card, finished)
    assert d._work_record_queue[0][5] == (report if retained else 'Card left open for live checks.')
    assert store.get(card['id'])['column_name'] == 'in_progress'


@pytest.mark.asyncio
async def test_P11_only_confirmed_close_moves_bound_card(journey, tmp_path):
    d, store = journey
    rec = cr.parse_rollout(journal(tmp_path, [message('## Work done\nAll source checks passed.'),
                                             row('event_msg', type='task_complete', turn_id='turn1')]))
    card = make_card(store, tmp_path, session_id=rec.session_id, link_state='live', column_name='in_progress')
    _, snap = publish(d, rec)
    d._reconcile_board(snap)
    assert store.get(card['id'])['column_name'] == 'in_progress'
    refused, detail = await d.close_card_by_session('codex:other', 'Done')
    assert refused is None and detail
    done, detail = await d.close_card_by_session(rec.session_id, 'Source gates passed.')
    assert done and done['column_name'] == 'done', detail
    assert done['closed_by'] == rec.session_id

@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['valid', 'missing', 'expired', 'question', 'helper', 'new-turn',
                                  'old-bridge', 'tty', 'pid', 'lost-reply'])
async def test_P12_private_planning_close_is_exact_and_once(refinement_handoff, monkeypatch, fault):
    from dataclasses import replace
    from dark_army_daemon import vscode_reveal
    d, store, card, plan, records, processes, posts = refinement_handoff
    sid = records[0].session_id
    for name in ('stop_codex_session', '_confirm_codex_stop', 'wrap_up_session'):
        monkeypatch.setattr(d, name, lambda *a, **kw: pytest.fail('No signal or clear fallback'))
    assert (await d.attach_plan_by_session(sid, str(plan)))[0]
    if fault == 'missing': d._refinement_receipts.clear()
    elif fault == 'expired':
        d._refinement_receipts[sid] = replace(d._refinement_receipts[sid], expires=0)
    elif fault == 'question':
        with records[0].path.open('a') as f:
            f.write(json.dumps(call('request_user_input_async', {'questions': [{'title': 'Still waiting?', 'options': ['Yes']}]}))+'\n')
            f.write(json.dumps(result({'accepted': True}))+'\n')
    elif fault == 'helper': records[0].stats.agents['child'] = cr.AgentInfo(agent_id='child', activity='running')
    elif fault == 'new-turn':
        with records[0].path.open('a') as f:
            f.write(json.dumps(row('event_msg', type='task_started', turn_id='turn-2'))+'\n')
    elif fault == 'old-bridge':
        monkeypatch.setattr(vscode_reveal, '_bob_ext_locks', lambda: [{
            'port': 44444, 'authToken': 'fixture', 'extensionVersion': '0.1.11',
            'workspaceFolders': [records[0].cwd]}])
    elif fault == 'tty': processes[0].fresh['terminal'] = 'ttys099'
    elif fault == 'pid': processes[0].fresh['create_time'] = 222
    elif fault == 'lost-reply':
        async def lost(*args, **kwargs):
            if kwargs.get('before_write') and not await kwargs['before_write'](): return None
            posts.append(args)
            return None
        monkeypatch.setattr(vscode_reveal, '_post_json', lost)
    ok, detail = await d.close_refinement_terminal(sid)
    assert ok is (fault == 'valid')
    assert detail
    assert len(posts) == (1 if fault in ('valid', 'lost-reply') else 0)
    assert not (await d.close_refinement_terminal(sid))[0]
    assert len(posts) <= 1
    assert store.get(card['id'])['column_name'] == 'backlog'


def test_P13_incomplete_tail_cache_isolation_and_same_cwd_roots(tmp_path, journey):
    p = journal(tmp_path, spawn())
    other = journal(tmp_path, [], thread='other')
    with p.open('a') as stream:
        stream.write('{"type":"response_item","payload":')
    records = cr.load_recent(tmp_path)
    assert {r.thread_id for r in records} == {'root', 'other'}
    rec = next(r for r in records if r.thread_id == 'root')
    assert rec.observed_roles == ['bc-planner']
    rec.observed_roles.append('invented')
    rec.stats.agents.clear()
    again = next(r for r in cr.load_recent(tmp_path) if r.thread_id == 'root')
    assert again.observed_roles == ['bc-planner'] and list(again.stats.agents) == ['child']
    d, _ = journey
    entry, _ = publish(d, again)
    assert entry['pid'] is None and not entry['can_stop']
    assert other.exists()


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_P14_provider_report_and_history_use_shared_consumers(tmp_path, journey, provider, monkeypatch):
    from dark_army_daemon import session_stats as ss
    d, store = journey
    report = '## Work done\n**Asked:** Observe stages.\n**Changed:** Parser.\n**Verified:** Replay.\n**Unchecked:** Live.'
    sid = 'codex:root' if provider == 'codex' else 'claude-root'
    if provider == 'codex':
        events = [*spawn(), row('event_msg', type='item_completed', item={'type': 'collabAgentToolCall',
                  'receiverThreadIds': ['child'], 'agentsStates': {'child': {'status': 'completed'}}}),
                  message(report), row('event_msg', type='task_complete', turn_id='turn1')]
        rec = cr.parse_rollout(journal(tmp_path, events))
        entry, snapshot = publish(d, rec)
    else:
        transcript = tmp_path/'claude.jsonl'
        transcript.write_text(json.dumps({'type':'assistant','timestamp':'2026-09-12T16:52:57Z',
          'message':{'model':'claude-test','usage':{},'content':[{'type':'text','text':report}]}})+'\n')
        d._session_states[sid] = {'state':'waiting','provider':'claude','cwd':str(tmp_path),
              'project':tmp_path.name,'last_event':time.time()-10,'subagents':set(),
              'subagents_seen':['bc-planner'],'transcript_path':str(transcript)}
        monkeypatch.setattr(ss, 'resolve_transcript', lambda _sid: str(transcript))
        # The real transcript cache/parser supplies the shared stats adapter.
        snapshot = d._enrich_agent_stubs(d._collect_agent_stubs())
        entry = next(r for entries in snapshot.values() for r in entries if r['session_id']==sid)
        assert ss.parse_transcript(str(transcript)).last_report == report
    card = make_card(store, tmp_path, tool=provider, session_id=sid,
                     link_state='live', column_name='in_progress')
    d._reconcile_board(snapshot)
    assert entry['last_report'] == report
    assert entry['subagent_rows'] == []
    assert store.get(card['id'])['agent_trail'] == 'bc-planner'
    assert store.get(card['id'])['column_name'] == 'in_progress'
    if provider == 'codex':
        assert not entry['can_type'] and not entry['channel']
        assert 'original Codex session' in entry['interaction_note']

@pytest.mark.asyncio
async def test_P08_queue_saturation_keeps_candidate_in_backlog(tmp_path, journey, monkeypatch):
    d, store = journey
    rec = cr.parse_rollout(journal(tmp_path, [message('Implementing the accepted plan.')]))
    publish(d, rec)
    make_card(store, tmp_path, session_id=rec.session_id, link_state='live', column_name='in_progress')
    plan = tmp_path/'queued.md'; plan.write_text('# Queue fixture\n')
    card = make_card(store, tmp_path, refine_session_id='codex:planner', refine_state='live')
    assert store.attach_plan(card['id'], str(plan), 'codex:planner')[0]
    monkeypatch.setattr(dispatch, 'spawn', lambda *a, **kw: pytest.fail('A full project cannot spawn'))
    ok, detail = await d.dispatch_card(card['id'])
    assert not ok and detail
    got = store.get(card['id'])
    assert got['queue_state'] == 'queued' and got['column_name'] == 'backlog'
    assert got['link_state'] == ''


def test_P06_path_followup_newer_than_retained_child_is_live(tmp_path, journey):
    root = journal(tmp_path, [call('spawn_agent', {'agent_type':'bc-verifier','task_name':'verify'}),
       result({'task_name':'/root/verify'}),
       call('collaboration.followup_task', {'target':'/root/verify','message':'Check the repair'}, 'again'),
       result({'task_name':'/root/verify'}, 'again')])
    rows = [json.loads(line) for line in root.read_text().splitlines()]
    rows[-1]['timestamp'] = '2026-09-12T16:54:00Z'
    root.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    child = journal(tmp_path, [row('event_msg', type='task_complete', turn_id='turn1')],
                    thread='child', parent='root', role='bc-verifier')
    rows = [json.loads(line) for line in child.read_text().splitlines()]
    rows[0]['payload']['source']['subagent']['thread_spawn']['agent_path']='/root/verify'
    child.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    rec = next(r for r in cr.load_recent(tmp_path) if r.thread_id=='root')
    assert list(rec.stats.agents) == ['child']
    assert rec.stats.agents['child'].activity == 'running'
    assert rec.observed_roles == ['bc-verifier']


@pytest.mark.parametrize('payload', [
    None, [], {'questions':'bad'}, {'questions':[{'title':{},'options':[]}]},
    {'questions':[{'title':'Q','options':[{}]}]}, {'questions':[{'title':'Q','options':'bad'}]},
])
def test_P13_malformed_async_is_not_a_fabricated_question(tmp_path, payload):
    rec = cr.parse_rollout(journal(tmp_path, [call('request_user_input_async', payload)]))
    assert rec.stats.question == {} and rec.observed_roles == []


@pytest.mark.asyncio
async def test_P01_attachment_appears_once_after_full_brief(tmp_path, journey, monkeypatch):
    from dark_army_daemon import attachments
    d, store = journey
    document = tmp_path/'reference.txt'; document.write_text('Fixture reference')
    card = make_card(store, tmp_path, prompt='Keep the full brief.\n'+str(document), attachments='fixture1/ref.txt')
    monkeypatch.setattr(attachments, 'resolve_paths', lambda rels: [str(document)])
    monkeypatch.setattr(dispatch, 'resolve_executable', lambda tool: '/bin/'+tool)
    prompts=[]
    async def capture(root, argv, name, **kw):
        prompts.append(argv[-1]); return True, 'fixture', None
    monkeypatch.setattr(dispatch, 'spawn', capture)
    assert (await d.refine_card(card['id']))[0]
    assert prompts[0].count(str(document)) == 1
    assert prompts[0].count('Keep the full brief.') == 1


def test_P01_inherited_identity_environment_isolation():
    from dark_army_daemon.subprocess_env import clean_env, unset_payload
    inherited = {'CODEX_THREAD_ID':'old', 'CODEX_SESSION_ID':'old',
                 'CLAUDE_CODE_SESSION_ID':'other', 'GROK_SESSION_ID':'other', 'PATH':'/bin'}
    cleaned = clean_env(inherited)
    assert cleaned['PATH'] == '/bin'
    assert all(k not in cleaned for k in inherited if k != 'PATH')
    overrides = unset_payload()
    assert all(overrides[k] is None for k in inherited if k != 'PATH')


@pytest.mark.parametrize('state', ['missing', 'metadata', 'active', 'completed', 'aborted',
    'incomplete-terminal', 'malformed-tail', 'parent-malformed-tail', 'followup',
    'followup-completed', 'followup-chatter', 'duplicate-path'])
def test_P12_P13_native_path_spawn_needs_affirmative_child_terminal(tmp_path, journey, monkeypatch, state):
    """Native 0.154.0 call/ack; later child/followup boundaries are synthetic."""
    from types import SimpleNamespace
    fixtures = Path(__file__).parent / 'fixtures/codex_session_parity'
    root_path = tmp_path / 'root.jsonl'
    root_path.write_text(''.join((fixtures/'native-root.jsonl').read_text().splitlines(True)[:4]))
    children = []
    def event(kind, at, **payload):
        return {'timestamp': f'2026-09-12T16:{at}Z', 'type': kind, 'payload': payload}
    if state != 'missing':
        child_path = tmp_path/'child.jsonl'
        child_rows = [json.loads((fixtures/'native-child.jsonl').read_text())]
        if state != 'metadata':
            child_rows.append(event('event_msg', '52:22', type='task_started', turn_id='child-turn'))
        if state not in ('metadata', 'active'):
            child_rows.append(event('event_msg', '53:00', type='turn_aborted' if state == 'aborted' else 'task_complete', turn_id='child-turn'))
        if state.startswith('followup'):
            with root_path.open('a') as stream:
                stream.write(json.dumps(event('response_item', '54:00', type='function_call', name='followup_task', call_id='follow-native', arguments={'target':'/root/implement','message':'Continue'}))+'\n')
                stream.write(json.dumps(event('response_item', '54:01', type='function_call_output', call_id='follow-native', output={'task_name':'/root/implement'}))+'\n')
            if state == 'followup-completed':
                child_rows += [event('event_msg', '54:02', type='task_started', turn_id='child-next'),
                               event('event_msg', '55:00', type='task_complete', turn_id='child-next')]
            elif state == 'followup-chatter':
                child_rows.append(event('response_item', '55:00', type='message', role='assistant', content=[{'type':'output_text','text':'Still considering the followup.'}]))
        child_path.write_text(''.join(json.dumps(r)+'\n' for r in child_rows))
        if state == 'incomplete-terminal':
            child_path.write_text(child_path.read_text().rsplit('\n', 2)[0]+'\n{"type":"event_msg","payload":')
        elif state == 'malformed-tail':
            with child_path.open('a') as stream: stream.write('{"type":"event_msg","payload":\n')
        children.append(cr.parse_rollout(child_path))
        if state == 'duplicate-path':
            duplicate = json.loads(json.dumps(child_rows))
            duplicate[0]['payload']['id'] = 'other-child'
            other = tmp_path/'other.jsonl';other.write_text(''.join(json.dumps(r)+'\n' for r in duplicate))
            children.append(cr.parse_rollout(other))
    if state == 'parent-malformed-tail':
        with root_path.open('a') as stream: stream.write('{"type":"response_item","payload":')
    record = cr.parse_rollout(root_path)
    assert record.spawned_paths and record.observed_roles == ['bc-implementer']
    assert record.stats.agents == {}  # paths must never become fake live UUIDs
    daemon, _ = journey
    daemon._codex_records = {r.session_id:r for r in [record,*children]}
    allowed = state in ('completed','aborted','followup-completed')
    assert daemon._refinement_busy(record) is (not allowed)
    root = cr.project_title_roots((record,))[0]
    identity = cr._journal_identity(root_path)
    proof = SimpleNamespace(root=root,journal=identity)
    monkeypatch.setattr(cr,'resolve_navigation_proofs',lambda roots:{root.session_id:proof})
    captured = tuple((c.thread_id,c.path,c.parent_thread_id,c.turn_id) for c in children)
    assert (cr.refinement_close_observation((root,),root,identity,record.turn_id,captured) is proof) is allowed


@pytest.mark.asyncio
async def test_P12_new_native_path_spawn_after_receipt_never_writes_close(refinement_handoff):
    d, store, card, plan, records, processes, posts = refinement_handoff
    record = records[0]
    assert (await d.attach_plan_by_session(record.session_id,str(plan)))[0]
    fixtures=Path(__file__).parent/'fixtures/codex_session_parity'
    native=(fixtures/'native-root.jsonl').read_text().splitlines(True)
    with record.path.open('a') as stream:stream.writelines(native[2:4])
    ok, detail = await d.close_refinement_terminal(record.session_id)
    assert not ok and 'open' in detail.lower()
    assert posts == []


@pytest.mark.parametrize('outcome', ['success', 'error', 'rejected', 'unknown'])
def test_P06_native_relative_followup_resumes_completed_child(tmp_path, journey, monkeypatch, outcome):
    fixtures=Path(__file__).parent/'fixtures/codex_session_parity'
    target=tmp_path/'2026/09/12';target.mkdir(parents=True)
    root_path=target/'rollout-root.jsonl'
    native_root=(fixtures/'native-root.jsonl').read_text().splitlines(True)
    followup=[json.loads(line) for line in (fixtures/'native-relative-followup.jsonl').read_text().splitlines()]
    if outcome != 'success':
        followup[-1]['payload']['output']=json.dumps({'error':'Refused'} if outcome=='error' else {'accepted':True} if outcome=='unknown' else {'accepted':False,'task_name':'/root/implement'})
    root_path.write_text(''.join(native_root[:4])+''.join(json.dumps(r)+'\n' for r in followup))
    child_path=target/'rollout-child.jsonl'
    child_path.write_text((fixtures/'native-child.jsonl').read_text()+json.dumps(row('event_msg',type='task_started',turn_id='child-turn'))+'\n'+json.dumps(row('event_msg',type='task_complete',turn_id='child-turn'))+'\n')
    monkeypatch.setattr(cr,'attach_process_ids',lambda records:None)
    records=cr.load_recent(tmp_path);root=next(r for r in records if r.thread_id=='native-root')
    d,_=journey;d._codex_records={r.session_id:r for r in records}
    assert ('native-child' in root.stats.agents) is (outcome=='success')
    assert d._refinement_busy(root) is (outcome in ('success','unknown'))
    assert root.observed_roles==['bc-implementer']


@pytest.mark.parametrize('reverse', [False,True], ids=['parent-newest','descendant-newest'])
def test_P06_active_descendant_survives_completed_ancestors_then_settles(tmp_path, journey, monkeypatch, reverse):
    import os
    parent=journal(tmp_path,[*spawn(role='bc-implementer',child='child'),row('event_msg',type='task_complete',turn_id='turn1')])
    child=journal(tmp_path,[*spawn(role='bc-verifier',child='grandchild'),row('event_msg',type='task_complete',turn_id='turn1')],thread='child',parent='root',role='bc-implementer')
    grandchild=journal(tmp_path,[],thread='grandchild',parent='child',role='bc-verifier')
    for i,path in enumerate((parent,child,grandchild)):
        stamp=time.time()+(-i if reverse else i);os.utime(path,(stamp,stamp))
    monkeypatch.setattr(cr,'attach_process_ids',lambda records:None)
    d,store=journey
    records=cr.load_recent(tmp_path);root=next(r for r in records if r.thread_id=='root')
    assert set(root.stats.agents)=={'grandchild'}
    assert root.stats.agents['grandchild'].subagent_type=='bc-verifier'
    card=make_card(store,tmp_path,session_id=root.session_id,link_state='live',column_name='in_progress')
    entry,snapshot=publish(d,root)
    assert entry in snapshot['running'] and entry['subagents']==1
    assert [row['subagent_type'] for row in entry['subagent_rows']]==['bc-verifier']
    d._reconcile_board(snapshot)
    assert store.get(card['id'])['agent_trail'].splitlines()==['bc-implementer','bc-verifier']
    with grandchild.open('a') as stream:stream.write(json.dumps(row('event_msg',type='task_complete',turn_id='turn1'))+'\n')
    root=next(r for r in cr.load_recent(tmp_path) if r.thread_id=='root')
    assert root.stats.agents=={}
    entry,snapshot=publish(d,root);d._reconcile_board(snapshot)
    assert entry not in snapshot['running']
    assert store.get(card['id'])['agent_trail'].splitlines()==['bc-implementer','bc-verifier']


@pytest.mark.parametrize('identity', [{'task_name':'/root/implement'}, {'agent_id':'child'}])
def test_P13_rejected_spawn_never_creates_role_or_helper(tmp_path, identity):
    rec=cr.parse_rollout(journal(tmp_path,[call('spawn_agent',{'agent_type':'bc-implementer'}),result({**identity,'accepted':False})]))
    assert rec.observed_roles==[] and rec.stats.agents=={} and rec.spawned_paths=={}


@pytest.mark.parametrize('route', ['native-path','synthetic-uuid'])
@pytest.mark.parametrize('timing', ['fast-new','later-new','old-turn','mismatched-end','missing-start','missing-end','missing-request-time','missing-start-time'])
def test_P12_followup_settlement_requires_its_new_turn(tmp_path, journey, monkeypatch, route, timing):
    from types import SimpleNamespace
    fixtures=Path(__file__).parent/'fixtures/codex_session_parity'
    rows=[json.loads(line) for line in (fixtures/'native-root.jsonl').read_text().splitlines()[:4]]
    followup=[json.loads(line) for line in (fixtures/'native-relative-followup.jsonl').read_text().splitlines()]
    if route=='synthetic-uuid':
        rows[-1]['payload']['output']=json.dumps({'agent_id':'native-child'})
        followup[0]['payload'].update(name='send_input',arguments=json.dumps({'id':'native-child','message':'Continue fixture.'}))
        followup[-1]['payload']['output']=json.dumps({'accepted':True})
    if timing=='missing-request-time':followup[0].pop('timestamp')
    root_path=tmp_path/'2026/09/12/rollout-root.jsonl';root_path.parent.mkdir(parents=True)
    root_path.write_text(''.join(json.dumps(r)+'\n' for r in rows+followup))
    child_rows=[json.loads((fixtures/'native-child.jsonl').read_text())]
    def terminal(kind,at,turn):
        return {'timestamp':f'2026-09-12T17:40:{at}Z','type':'event_msg','payload':{'type':kind,'turn_id':turn}}
    child_rows += [terminal('task_started','15.000','old'),terminal('task_complete','18.000' if timing=='old-turn' else '16.000','old')]
    if timing!='old-turn':
        if timing!='missing-start':child_rows.append(terminal('task_started','17.500' if timing=='later-new' else '16.700','new'))
        if timing!='missing-end':child_rows.append(terminal('task_complete','18.000' if timing=='later-new' else '16.900','wrong' if timing=='mismatched-end' else 'new'))
    if timing=='missing-start-time':child_rows[-2].pop('timestamp')
    child_path=root_path.with_name('rollout-child.jsonl');child_path.write_text(''.join(json.dumps(r)+'\n' for r in child_rows))
    monkeypatch.setattr(cr,'attach_process_ids',lambda records:None)
    records=cr.load_recent(tmp_path);root=next(r for r in records if r.thread_id=='native-root');child=next(r for r in records if r.thread_id=='native-child')
    d,_=journey;d._codex_records={r.session_id:r for r in records}
    settled=timing in ('fast-new','later-new')
    assert d._refinement_busy(root) is (not settled)
    assert ('native-child' in root.stats.agents) is (not settled)
    title=cr.project_title_roots((root,))[0];identity=cr._journal_identity(root_path);proof=SimpleNamespace(root=title,journal=identity)
    monkeypatch.setattr(cr,'resolve_navigation_proofs',lambda roots:{title.session_id:proof})
    captured=((child.thread_id,child.path,child.parent_thread_id,child.turn_id),)
    assert (cr.refinement_close_observation((title,),title,identity,root.turn_id,captured) is proof) is settled


@pytest.mark.parametrize('reverse', [False, True])
def test_native_review_is_retained_under_parent_without_attention(tmp_path, monkeypatch, reverse):
    fixture = Path(__file__).parent / 'fixtures/codex_session_parity/native-review-child.jsonl'
    rows = [json.loads(line) for line in fixture.read_text().splitlines()]
    rows[0]['payload']['cwd'] = str(tmp_path)
    child_path = tmp_path / '2026/09/12/rollout-review.jsonl'
    parent_path = journal(tmp_path, [message('Parent output'), row('event_msg', type='task_complete', turn_id='turn1')])
    child_path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    import os
    stamp = time.time()
    os.utime(parent_path if reverse else child_path, (stamp + 1, stamp + 1))
    monkeypatch.setattr(cr, 'attach_process_ids', lambda records: None)
    records = cr.load_recent(tmp_path)
    parent = next(r for r in records if r.thread_id == 'root')
    child = next(r for r in records if r.thread_id == 'review-child')
    assert child.parent_thread_id == 'root' and child.kind == 'background'
    assert not child.turn_active and parent.stats.agents == {}
    assert parent.stats.last_text == 'Parent output'
    assert parent.observed_roles == []
    assert len(parent.review_reports) == 1
    assert len(json.loads(parent.review_reports[0]['text'])['findings']) == 5
    d = BobDaemon(sessions_path=tmp_path / 'state.json')
    d._codex_records = {r.session_id: r for r in records}
    d._roster_refreshed_at = time.monotonic()
    assert set(d._reconciled_categories()) == {'codex:root'}
    assert d._claiming_session_ids() == {'codex:root'}
    assert d._activity_counts()['subagents'] == 0
    assert not any(d._session_capabilities({'session_id': child.session_id, 'provider': 'codex'}).values())
    stubs = d._collect_agent_stubs()
    assert [s['session_id'] for s in stubs] == ['codex:root']
    assert stubs[0]['review_reports'] == parent.review_reports


@pytest.mark.parametrize('parent', [None, '', 'review-child', 7, {}, ' other ', 'two words'])
def test_explicit_orphan_review_cannot_become_root(tmp_path, parent):
    p = journal(tmp_path, thread='review-child')
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    rows[0]['payload']['source'] = {'subagent': 'review'}
    if parent is not None:
        rows[0]['payload']['parent_thread_id'] = parent
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    rec = cr.parse_rollout(p)
    assert rec.is_child and rec.kind == 'background'
    assert not rec.parent_thread_id
    d = BobDaemon(sessions_path=tmp_path / 'state.json')
    d._codex_records = {rec.session_id: rec}
    assert not d._reconciled_categories() and not d._claiming_session_ids()
    assert not d._collect_agent_stubs()
    assert not cr.project_title_roots([rec])


@pytest.mark.parametrize('nested', ['root', 'other', None, 123, 'review-child'])
def test_top_level_and_nested_parent_must_agree(tmp_path, nested):
    p = journal(tmp_path, thread='review-child', parent='root')
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    rows[0]['payload']['parent_thread_id'] = nested
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    rec = cr.parse_rollout(p)
    assert rec.is_child
    assert rec.parent_thread_id == ('root' if nested == 'root' else '')


@pytest.mark.parametrize('fault', ['cycle', 'other_project', 'orphan'])
def test_ambiguous_review_never_attaches(tmp_path, monkeypatch, fault):
    p = journal(tmp_path, thread='root', parent='review-child' if fault == 'cycle' else '')
    child = journal(tmp_path, [message('review'), row('event_msg', type='task_complete', turn_id='turn1')], thread='review-child')
    rows = [json.loads(line) for line in child.read_text().splitlines()]
    rows[0]['payload'].update(source={'subagent': 'review'}, parent_thread_id='missing' if fault == 'orphan' else 'root')
    if fault == 'other_project': rows[0]['payload']['cwd'] = '/another/project'
    child.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    monkeypatch.setattr(cr, 'attach_process_ids', lambda records: None)
    records = cr.load_recent(tmp_path)
    assert all(not r.review_reports for r in records)


def test_review_bounds_are_utf8_and_omissions_are_explicit(tmp_path, monkeypatch):
    journal(tmp_path)
    for i in range(6):
        p = journal(tmp_path, [message('é' * 20_000), row('event_msg', type='task_complete', turn_id='turn1')], thread=f'review-{i}')
        rows = [json.loads(line) for line in p.read_text().splitlines()]
        rows[0]['payload'].update(source={'subagent': 'review'}, parent_thread_id='root')
        p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    monkeypatch.setattr(cr, 'attach_process_ids', lambda records: None)
    root = next(r for r in cr.load_recent(tmp_path) if r.thread_id == 'root')
    assert len(root.review_reports) == 4 and root.review_reports_omitted == 2
    assert sum(len(r['text'].encode()) for r in root.review_reports) <= 65536
    assert all(r['truncated'] and len(r['text'].encode()) <= 16384 for r in root.review_reports)


@pytest.mark.parametrize('ending', ['closed', 'no process'])
def test_review_reports_survive_parent_terminal_lifetime(tmp_path, monkeypatch, journey, ending):
    d, _ = journey
    journal(tmp_path, [message('Parent output'), row('event_msg', type='task_complete', turn_id='turn1')])
    for i in range(6):
        p = journal(tmp_path, [message('é' * 20_000), row('event_msg', type='task_complete', turn_id='turn1')], thread=f'review-{i}')
        rows = [json.loads(line) for line in p.read_text().splitlines()]
        rows[0]['payload'].update(source={'subagent': 'review'}, parent_thread_id='root')
        p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    records = cr.load_recent(tmp_path)
    root = next(r for r in records if r.thread_id == 'root')
    live, _ = publish(d, root)
    expected = [dict(report) for report in live['review_reports']]
    assert len(expected) == 4 and live['review_reports_omitted'] == 2
    monkeypatch.setattr(cr, 'load_recent', lambda: records)
    if ending == 'closed':
        # Confirmed close's settlement seam; no real editor or terminal write.
        d._settle_codex_stop(root.session_id, 'closed', observed=root)
    else:
        from dark_army_daemon import enrollment
        monkeypatch.setattr(enrollment, 'root_enrolled', lambda cwd: True)
        root.process_seen = False
        BobDaemon._refresh_codex_records(d)
    root.review_reports[0]['text'] = 'Later scan must not mutate the tombstone'
    root.review_reports.clear()
    finished = d._enrich_agent_stubs(d._collect_agent_stubs())['finished']
    retained = next(row for row in finished if row['session_id'] == root.session_id)
    assert retained['review_reports'] == expected
    assert retained['review_reports_omitted'] == 2
    assert retained['last_text'] == 'Parent output'
    assert sum(len(r['text'].encode()) for r in retained['review_reports']) <= 65536
    assert all(r['truncated'] for r in retained['review_reports'])


def test_duplicate_child_journals_do_not_attribute_review(tmp_path, monkeypatch):
    journal(tmp_path)
    path = journal(tmp_path, [message('review'), row('event_msg', type='task_complete', turn_id='turn1')], thread='review-child')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]['payload'].update(source={'subagent': 'review'}, parent_thread_id='root')
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    path.with_name('rollout-replacement.jsonl').write_text(path.read_text())
    monkeypatch.setattr(cr, 'attach_process_ids', lambda records: None)
    records = cr.load_recent(tmp_path)
    root = next(r for r in records if r.thread_id == 'root')
    assert not root.review_reports and not root.stats.agents
    assert len([r for r in records if r.thread_id == 'review-child']) == 1


@pytest.mark.parametrize('source,thread_source', [
    ('subagent', 'user'), ({'kind': 'subagent'}, 'user'),
    ({'type': 'subagent'}, 'user'), ('cli', 'subagent'),
    (' SubAgent ', 'user'), ('cli', ' SubAgent '), ({'subAgent': 'review'}, 'user'),
])
def test_every_explicit_child_source_stays_noninteractive(tmp_path, source, thread_source):
    p = journal(tmp_path)
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    rows[0]['payload'].update(source=source, thread_source=thread_source)
    p.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    record = cr.parse_rollout(p)
    assert record.is_child and record.kind == 'background'
    d = BobDaemon(sessions_path=tmp_path / 'state.json')
    d._codex_records = {record.session_id: record}
    d._roster_refreshed_at = time.monotonic()
    assert not d._reconciled_categories() and not d._claiming_session_ids()
    assert not d._collect_agent_stubs()
    assert not any(d._session_capabilities({'provider': 'codex', 'session_id': record.session_id}).values())
    assert not cr.project_title_roots([record])
