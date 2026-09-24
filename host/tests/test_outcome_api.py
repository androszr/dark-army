import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from dark_army_daemon.api_server import ApiServer, _Request
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon import channel_server, relay


@pytest.fixture
def setup(tmp_path,monkeypatch):
    d=BobDaemon(headless=True)
    s=BoardStore(tmp_path/'board.db');s.connect();d._board=s
    monkeypatch.setattr(d,'_publish_board',AsyncMock())
    c,_=s.create(dict(title='Test',root='/project',tool='claude',intended_benefit='Benefit',success_criterion='Criterion'))
    api=ApiServer(d,port=0);api.token='token'
    yield api,d,s,c
    s.close()


def payload(c,**kw):
    return dict(action='board_accept_outcome',card_id=c['id'],expected_outcome_revision='0',request_key='one',evidence='Observed success',**kw)


@pytest.mark.parametrize('headers',[{}, {'x-bob-token':'wrong'}, {'authorization':'Bearer token'}, {'x-bob-token':'token','origin':'https://evil.example'}])
def test_write_authorization_unchanged(setup,headers):
    api,_,s,c=setup
    req=_Request('POST','/api/action','',headers,json.dumps(payload(c)).encode())
    assert api._board_request(req) is None
    assert not s.outcome_card_report(c['id'])['card']['accepted']


def test_originless_native_token_accepted_and_host_gate(setup):
    api,_,_,c=setup
    req=_Request('POST','/api/action','',{'x-bob-token':'token'},json.dumps(payload(c)).encode())
    assert api._board_request(req)[0]=='board_accept_outcome'
    req.headers['host']='evil.example'
    assert not api._loopback_host(req)


@pytest.mark.asyncio
async def test_loopback_decision_and_report(setup):
    api,d,s,c=setup
    status,_,body=await api._board_action('board_accept_outcome',payload(c))
    assert status==200 and json.loads(body)['outcome_revision']==1
    assert s.get(c['id'])['column_name']=='prep'
    status,_,body=await api._outcome_report_for('card='+c['id'])
    report=json.loads(body)
    assert status==200 and report['card']['accepted'] and report['measurements_available']
    assert report['events'][0]['evidence']=='Observed success'


@pytest.mark.asyncio
@pytest.mark.parametrize('condition',['working','dispatching','refining','question','permission','manual'])
async def test_acceptance_refused_for_unfinished_interaction(setup,monkeypatch,condition):
    api,d,s,c=setup
    if condition=='dispatching':s.update(c['id'],dict(link_state='dispatching'))
    elif condition=='refining':s.update(c['id'],dict(refine_state='dispatching'))
    else:
        s.bind_session(c['id'],'sid')
        row=dict(session_id='sid',provider='claude')
        if condition=='working':
            d._agents_snapshot_cache={'running':[row]}
            monkeypatch.setattr(d,'_claiming_session_ids',lambda:{'sid'})
        if condition=='question':d._agents_snapshot_cache={'waiting':[dict(row,question={'questions':['Which?']})]}
        if condition=='permission':monkeypatch.setattr(d,'_prompts_by_session',lambda:{'sid':{}})
        if condition=='manual':s.flag_manual(c['id'],'sid','1. Look at it')
    result=await api._board_action('board_accept_outcome',payload(c))
    assert result[0]==409
    assert not s.outcome_card_report(c['id'])['card']['accepted']


@pytest.mark.asyncio
async def test_quiet_live_terminal_can_accept(setup,monkeypatch):
    api,d,s,c=setup;s.bind_session(c['id'],'sid')
    d._agents_snapshot_cache={'running':[{'session_id':'sid'}]}
    monkeypatch.setattr(d,'_claiming_session_ids',lambda:set())
    assert (await api._board_action('board_accept_outcome',payload(c)))[0]==200


@pytest.mark.asyncio
@pytest.mark.parametrize('door',['home','away'])
async def test_sealed_read_both_doors_and_no_outcome_writes(setup,door):
    api,d,s,c=setup;s.accept_outcome(c['id'],0,'accept','proof')
    async def run(kind,p):
        return await api._sealed_run(kind,p,'device',actions=api.LAN_ACTIONS if door=='home' else api.REMOTE_ACTIONS,check_lease=door=='away',record=False)
    status,_,body=await run('outcomes',dict(query='card='+c['id']))
    assert status==200 and json.loads(body)['card']['accepted']
    assert (await run('action',payload(c)))[0]==404
    assert (await run('action',dict(action='board_update',card_id=c['id'],success_criterion='forged')))[0]==403


@pytest.mark.asyncio
@pytest.mark.parametrize('query',['root=/project&limit=101','root=/project&limit=0','root=/project&offset=-1','root=/project&from=NaN','root=/project&to=inf','root=/project&from=4&to=3','root=/project&limit=1&limit=2',''])
async def test_report_bounds(setup,query):
    assert (await setup[0]._outcome_report_for(query))[0]==400


@pytest.mark.asyncio
async def test_unavailable_store_is_explicit(setup):
    api,d,s,c=setup;d._board=None
    assert json.loads((await api._outcome_report_for('card='+c['id']))[2])['available'] is False
    assert (await api._board_action('board_accept_outcome',payload(c)))[0]==409


def test_agent_and_generic_field_ownership(setup):
    api,_,_,_=setup
    for name in ['outcome_status','outcome_revision','accepted','evidence','first_accepted_at']:
        assert name not in api._BOARD_FIELDS
    tools=json.dumps(channel_server.tools_for_host(channel_server.HOST_CLAUDE))
    assert 'board_accept_outcome' not in tools and 'request_outcome_revision' not in tools
    assert 'outcome_status' not in tools
    # Nothing an agent can call names the objective, on either host: Prepare
    # may draft it and the opening prompt may carry it, but no tool writes it.
    for host in (channel_server.HOST_CLAUDE, channel_server.HOST_CODEX):
        tools=json.dumps(channel_server.tools_for_host(host))
        for name in ('beneficiary','intended_benefit','success_criterion','board_accept_outcome'):
            assert name not in tools, (host, name)
    for actions in [api.LAN_ACTIONS,api.REMOTE_ACTIONS]:
        assert 'board_accept_outcome' not in actions and 'board_request_revision' not in actions


def test_observer_only_actionable_waits(setup,monkeypatch):
    _,d,s,c=setup;s.bind_session(c['id'],'sid')
    monkeypatch.setattr(d,'_prompts_by_session',lambda:{})
    seen=[]
    # The pass writes every card through the one batch verb.
    monkeypatch.setattr(s,'observe_outcomes',lambda samples,**kw:seen.append(samples[-1][1]))
    d._observe_board_outcomes({'waiting':[dict(session_id='sid',provider='claude',state='error')]},set())
    assert seen[-1]==set()
    d._observe_board_outcomes({'running':[dict(session_id='sid',provider='claude',subagents=[{}])]}, {'sid'})
    assert seen[-1]==set()
    d._observe_board_outcomes({'waiting':[dict(session_id='sid',provider='claude',question={'text':'Which?'})]}, set())
    assert seen[-1]=={'question'}


@pytest.mark.asyncio
async def test_observer_failure_does_not_accept_or_block_card_operations(setup,monkeypatch):
    _,d,s,c=setup
    monkeypatch.setattr(s,'observe_outcomes',lambda *a,**k:(_ for _ in ()).throw(OSError('disk')))
    d._observe_board_outcomes({},set())
    report=await d.card_outcome_report(card_id=c['id'])
    assert report['measurements_available'] is False
    assert s.update(c['id'],{'title':'Still editable'})[0]
    assert not report['card']['accepted']

@pytest.mark.asyncio
async def test_identical_replay_survives_new_activity(setup):
    api,d,s,c=setup
    first=await api._board_action('board_accept_outcome',payload(c))
    assert first[0]==200
    s.update(c['id'],dict(link_state='dispatching'))
    again=await api._board_action('board_accept_outcome',payload(c))
    assert again[0]==200 and json.loads(again[2])['outcome_revision']==1


def test_snapshot_outcome_text_is_on_demand(setup):
    _,d,_,c=setup
    compact=d._trim_card_for_snapshot(c)
    assert compact['outcome_revision']==0
    assert 'intended_benefit' not in compact and 'success_criterion' not in compact
    assert c['intended_benefit']=='Benefit'


@pytest.mark.asyncio
async def test_api_refuses_nontext_objective(setup):
    api,_,s,c=setup
    status,_,_=await api._board_action('board_update',dict(card_id=c['id'],intended_benefit=42,expected_outcome_revision='0'))
    assert status==409 and s.get(c['id'])['intended_benefit']=='Benefit'


def test_history_absent_children_partial_and_pruned_cost_retained(setup,tmp_path,monkeypatch):
    from dark_army_daemon.history import HistoryStore, COST_MEASURED
    _,d,s,c=setup
    h=HistoryStore(tmp_path/'history.db');h.connect();d._history=h
    monkeypatch.setattr(d,'_prompts_by_session',lambda:{})
    s.bind_session(c['id'],'sid')
    s.record_outcome_run(c['id'],'claude','sid','implementation')
    snapshot={'waiting':[dict(session_id='sid',provider='claude')]}
    d._observe_board_outcomes(snapshot,set())
    assert s.outcome_card_report(c['id'])['card']['cost']['coverage']=='unknown'
    # Monetary units/provenance are real; child inclusion is not promised by
    # HistoryStore, so it cannot qualify for full lifecycle cost coverage.
    h.upsert_session('sid',provider='claude',first_seen=1,last_seen=1,cost_usd=4,cost_source=COST_MEASURED)
    d._observe_board_outcomes(snapshot,set())
    r=s.outcome_card_report(c['id'])['card']
    assert r['cost']['totals']=={'USD':4} and r['cost']['coverage']=='partial'
    h.prune(days=1)
    assert h.session_record('sid') is None
    d._observe_board_outcomes({},set())
    assert s.outcome_card_report(c['id'])['card']['cost']['totals']=={'USD':4}
    h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('door',['home','away'])
async def test_sealed_create_carries_the_objective_but_update_still_refuses(setup,monkeypatch,door):
    """`board_create` alone is exempt from the door's objective 403: a card may
    be written *with* its objective from the phone. Editing one that exists
    stays a Mac-side act."""
    api,d,s,c=setup
    monkeypatch.setattr(d,'_known_project_roots',lambda:{'/project'})
    monkeypatch.setattr(relay,'lease_valid',lambda _:True)
    async def run(p):
        return await api._sealed_run('action',p,'device',actions=api.LAN_ACTIONS if door=='home' else api.REMOTE_ACTIONS,check_lease=door=='away',record=False)
    status,_,body=await run(dict(action='board_create',title='New',root='/project',tool='claude',
                                 beneficiary='Ops',intended_benefit='Fewer pages',success_criterion='No page in a week'))
    assert status==200, body
    made=s.get(json.loads(body)['card_id'])
    assert (made['beneficiary'],made['intended_benefit'],made['success_criterion'])==('Ops','Fewer pages','No page in a week')
    assert (await run(dict(action='board_update',card_id=made['id'],success_criterion='forged')))[0]==403
    assert s.get(made['id'])['success_criterion']=='No page in a week'
    # The store's bound still bites on the exempt path: reject, never trim.
    status,_,_=await run(dict(action='board_create',title='Too long',root='/project',tool='claude',beneficiary='b'*201))
    assert status==409
