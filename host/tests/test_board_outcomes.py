"""Observed outcome ledger: decisions, retention and conservative measurements."""
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from dark_army_daemon.board import BoardStore
from dark_army_daemon import board_outcomes as m


@pytest.fixture
def store(tmp_path):
    s = BoardStore(tmp_path / 'board.db')
    s.connect()
    yield s
    s.close()


def card(s, **fields):
    c, reason = s.create(dict(title='Outcome', root='/project', tool='claude',
                            intended_benefit='Less delay', success_criterion='One click', **fields))
    assert c, reason
    return c


def report(s, c):
    return s.outcome_card_report(c['id'])['card']


def decide(s, c, key='accept', evidence='Observed one click', accept=True):
    current = s.get(c['id'])
    verb = s.accept_outcome if accept else s.request_outcome_revision
    return verb(c['id'], current['outcome_revision'], key, evidence)


def test_done_reviewed_is_not_acceptance(store):
    c = card(store, column_name='done')
    store.mark_reviewed(c['id'])
    assert not report(store, c)['accepted']
    assert report(store, c)['submission_at'] is None
    assert report(store, c)['wait_coverage'] == 'unknown'


def test_acceptance_is_separate_idempotent_and_verbatim(store):
    c = card(store)
    result, _ = decide(store,c,evidence='  user evidence\nreference: a/b  ')
    assert result['outcome_revision'] == 1
    assert store.get(c['id'])['column_name'] == 'prep'
    assert store.accept_outcome(c['id'],0,'accept','  user evidence\nreference: a/b  ')[0] == result
    assert store.accept_outcome(c['id'],0,'accept','different')[0] is None
    page=store.outcome_card_report(c['id'])
    assert page['events'][0]['evidence'] == '  user evidence\nreference: a/b  '
    assert page['events'][0]['actor'] == 'user'


@pytest.mark.parametrize('field,value', [('beneficiary','x'*201),('intended_benefit','x'*1001),('success_criterion',3),('outcome_check_on','2026-02-30'),('outcome_check_on','20260905')])
def test_objective_bounds_atomic(store,field,value):
    c=card(store)
    before=store.get(c['id'])
    assert store.update(c['id'],{field:value,'title':'changed','expected_outcome_revision':0})[0] is None
    assert store.get(c['id']) == before


def test_stale_scope_edit_confirmation_and_history(store):
    c=card(store);decide(store,c)
    fields=dict(success_criterion='Two clicks',expected_outcome_revision=1)
    assert store.update(c['id'],fields)[0] is None
    fields['confirm_outcome_scope_change']=True
    updated,_=store.update(c['id'],fields)
    assert updated['outcome_revision']==2
    r=report(store,c)
    assert not r['accepted'] and r['rework_count']==0
    assert store.accept_outcome(c['id'],1,'stale','Evidence')[0] is None
    assert any(e['kind']=='scope_changed' for e in store.outcome_card_report(c['id'])['events'])


def test_objective_write_even_unchanged_requires_revision(store):
    c=card(store)
    assert store.update(c['id'],{'beneficiary':''})[0] is None
    assert store.update(c['id'],{'beneficiary':'','expected_outcome_revision':0})[0]['outcome_revision']==1
    assert store.update(c['id'],{'outcome_status':'accepted','outcome_revision':99})[0] is None


def test_competing_criterion_and_acceptance_one_wins(store):
    c=card(store);barrier=threading.Barrier(2)
    def edit():
        barrier.wait();return store.update(c['id'],dict(success_criterion='Changed',expected_outcome_revision=0))
    def accept():
        barrier.wait();return store.accept_outcome(c['id'],0,'race','Proof')
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(edit),pool.submit(accept)]
        assert sum(f.result()[0] is not None for f in futures)==1


@pytest.mark.parametrize('move', ['update','reorder'])
def test_reopen_once_per_submission_both_routes(store,move):
    c=card(store);store.record_outcome_run(c['id'],'claude','sid','implementation')
    store.update(c['id'],dict(column_name='done'));decide(store,c)
    first=report(store,c)['first_accepted_at']
    if move=='update':store.update(c['id'],dict(column_name='backlog'))
    else:store.reorder(c['id'],'backlog','')
    r=report(store,c);assert not r['accepted'] and r['rework_count']==1
    store.update(c['id'],dict(session_id='',link_state=''))
    decide(store,c,key='reason',evidence='Fix it',accept=False)
    assert report(store,c)['rework_count']==1
    decide(store,c,key='again')
    assert report(store,c)['first_accepted_at']==first
    decide(store,c,key='reason2',evidence='Again',accept=False)
    assert report(store,c)['rework_count']==2


@pytest.mark.parametrize('deletion',['delete','clear','prune'])
def test_ledger_retains_acceptance_after_removal(store,deletion):
    c=card(store,column_name='done');decide(store,c)
    if deletion=='delete':store.delete(c['id'])
    elif deletion=='clear':store.clear_done(*store.done_scope())
    else:
        store.mark_reviewed(c['id']);store.prune_done(time.time()+1)
    assert not store.get(c['id'])
    assert report(store,c)['accepted']
    summary=store.outcome_project_report('/project',time.time()-100,time.time()+100)['summary']
    assert summary['accepted_outcomes']==1


def test_schema_migrate_twice_and_future_marker(store):
    c=card(store)
    store._conn.execute("CREATE TABLE future_outcome_private(value TEXT)")
    store._conn.execute("INSERT INTO future_outcome_private VALUES('keep')")
    store._conn.execute("UPDATE schema_meta SET value='999' WHERE key='version'")
    store._conn.commit();store.close();store.connect();store.close();store.connect()
    assert store._conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]=='999'
    assert store._conn.execute('SELECT value FROM future_outcome_private').fetchone()[0]=='keep'
    assert not report(store,c)['accepted']


def test_rollback_decision_failure(store):
    c=card(store)
    store._conn.execute("CREATE TRIGGER refuse_accept BEFORE INSERT ON outcome_events WHEN NEW.kind='accepted' BEGIN SELECT RAISE(ABORT,'disk refusal'); END")
    with pytest.raises(sqlite3.DatabaseError):decide(store,c)
    assert not report(store,c)['accepted']
    assert store.get(c['id'])['outcome_revision']==0
    assert not store.outcome_card_report(c['id'])['events']


def test_wait_overlap_causes_and_midnight(store):
    c=card(store)
    for t,causes in [(0,{'question'}),(5,{'question','permission'}),(10,set())]:
        store.observe_outcome(c['id'],causes,utc=86395+t,monotonic=t)
    r=report(store,c)
    assert r['wait_seconds']==10
    assert r['wait_causes']=={'question':10,'permission':5}
    assert store._conn.execute('SELECT COUNT(DISTINCT day) FROM outcome_wait_days').fetchone()[0]==2


@pytest.mark.parametrize('next_m,next_u,causes',[(31,131,{'question'}),(5,200,{'question'}),(-1,99,{'question'}),(5,105,None)])
def test_gaps_are_partial_not_filled(store,next_m,next_u,causes):
    c=card(store);store.observe_outcome(c['id'],{'question'},utc=100,monotonic=0)
    store.observe_outcome(c['id'],causes,utc=next_u,monotonic=next_m)
    r=report(store,c);assert r['wait_seconds']==0 and r['wait_coverage']=='partial'


def test_first_missing_unknown_and_restart_partial(store):
    c=card(store);store.observe_outcome(c['id'],None,utc=100,monotonic=0)
    assert report(store,c)['wait_coverage']=='unknown'
    store.observe_outcome(c['id'],{'question'},utc=105,monotonic=5)
    store.observe_outcome(c['id'],{'question'},utc=110,monotonic=10)
    store.close();store.connect()
    store.observe_outcome(c['id'],{'question'},utc=1000,monotonic=0)
    assert report(store,c)['wait_seconds']==5
    assert report(store,c)['wait_coverage']=='partial'


def test_cost_cumulative_zero_null_conflict_and_duplicate(store):
    c=card(store);store.record_outcome_run(c['id'],'claude','s','implementation')
    def cost(n,source='meter'):
        store.observe_outcome(c['id'],set(),costs=[dict(session_id='s',provider='claude',amount=n,currency='USD',source=source,complete=True)])
    assert report(store,c)['cost']['coverage']=='unknown'
    cost(0);assert report(store,c)['cost']['totals']=={'USD':0}
    for n in [2,3,3]:cost(n)
    assert report(store,c)['cost']['totals']=={'USD':3}
    cost(1);assert report(store,c)['cost']['totals']=={'USD':3}
    assert report(store,c)['cost']['coverage']=='partial'
    other=card(store)
    assert not store.record_outcome_run(other['id'],'claude','s','implementation')
    assert report(store,other)['cost']['coverage']=='unknown'
    store.update(c['id'],{'session_id':'','link_state':''})
    assert len(report(store,c)['runs'])==1


@pytest.mark.parametrize('amount',[None,-1,float('nan'),float('inf'),True,'4'])
def test_bad_cost_unknown(amount):
    assert m.cost_reading(amount,'USD','meter') is None


def test_cohort_complete_cost_denominator_distinct_and_original_root(store):
    a=card(store);b=card(store)
    store.record_outcome_run(a['id'],'claude','s','implementation')
    store.observe_outcome(a['id'],set(),costs=[dict(provider='claude',session_id='s',amount=4,currency='USD',source='meter',complete=True)])
    decide(store,a,'a');decide(store,b,'b')
    store.update(a['id'],{'root':'/elsewhere'})
    summary=store.outcome_project_report('/project',time.time()-100,time.time()+100)['summary']
    assert summary['accepted_outcomes']==2
    assert summary['cost_per_outcome']['USD']==dict(total=4,covered=1,per_accepted_outcome=4,outcomes=2)
    decide(store,a,'again')
    assert store.outcome_project_report('/project',time.time()-100,time.time()+100)['summary']['accepted_outcomes']==2


def test_rework_cohort_boundaries_and_carry_in():
    submissions=[dict(card_id='a',ts=10),dict(card_id='b',ts=12),dict(card_id='c',ts=0)]
    reworks=[dict(card_id='a',ts=15,submission_at=10),dict(card_id='a',ts=16,submission_at=10),dict(card_id='c',ts=15,submission_at=0),dict(card_id='b',ts=20,submission_at=12)]
    r=m.project_summary([],submissions,reworks,10,20)
    assert (r['submitted_cards'],r['reworked_cards'],r['carry_in_rework'],r['rework_rate'])==(2,1,1,.5)
    assert m.project_summary([],[],[],10,20)['rework_rate'] is None


@pytest.mark.parametrize('start,end,limit',[(float('nan'),10,1),(0,float('inf'),1),(0,10,101),(True,10,1)])
def test_report_bounds(store,start,end,limit):
    with pytest.raises(ValueError):store.outcome_project_report('/project',start,end,limit=limit)


def test_evidence_pages(store):
    c=card(store)
    for n in range(105):decide(store,c,str(n))
    p=store.outcome_card_report(c['id'],limit=100)
    assert len(p['events'])==100 and p['next_offset']==100
    assert len(store.outcome_card_report(c['id'],offset=200)['events'])==10


def test_starting_accepted_card_without_done_reopens_once(store):
    c=card(store)
    decide(store,c)
    store.update(c['id'],{'link_state':'dispatching','column_name':'in_progress'})
    r=report(store,c)
    assert not r['accepted'] and r['rework_count']==1
    store.update(c['id'],{'link_state':'dispatching'})
    assert report(store,c)['rework_count']==1


def test_review_wait_stops_at_human_decision(store,monkeypatch):
    c=card(store);store.record_outcome_run(c['id'],'claude','sid','implementation')
    store.update(c['id'],{'column_name':'done'})
    store.observe_outcome(c['id'],set(),utc=100,monotonic=0)
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.time',lambda:110)
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.monotonic',lambda:10)
    decide(store,c)
    store.observe_outcome(c['id'],set(),utc=120,monotonic=20)
    r=report(store,c)
    assert r['wait_seconds']==10 and r['wait_causes']=={'review':10}


def test_reacceptance_new_evidence_submits_without_moving_first_cohort(store,monkeypatch):
    c=card(store)
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.time',lambda:1)
    decide(store,c,'first')
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.time',lambda:11)
    decide(store,c,'later',evidence='New explicit evidence')
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.time',lambda:12)
    decide(store,c,'reject',accept=False,evidence='Revise it')
    summary=store.outcome_project_report('/project',10,20)['summary']
    assert summary['submitted_cards']==1 and summary['reworked_cards']==1
    assert summary['carry_in_rework']==0 and summary['rework_rate']==1
    assert report(store,c)['first_accepted_at']==1


def test_real_preupgrade_schema_and_old_writer_round_trip(tmp_path):
    from dark_army_daemon.board import _SCHEMA
    path=tmp_path/'old.db'
    conn=sqlite3.connect(path);conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO schema_meta VALUES('version','13')")
    conn.execute("INSERT INTO cards(id,project,root,title,column_name,created_at,updated_at) VALUES('old','project','/project','Old Done','done',1,1)")
    assert 'outcome_revision' not in {r[1] for r in conn.execute('PRAGMA table_info(cards)')}
    conn.commit();conn.close()
    s=BoardStore(path);s.connect()
    assert s.get('old')['outcome_revision']==0
    assert s.outcome_card_report('old')['card']['submission_at'] is None
    s.update('old',dict(intended_benefit='Benefit',success_criterion='Criterion',expected_outcome_revision=0))
    s.accept_outcome('old',1,'old-accept','Kept evidence')
    s.close()
    # Older writer names only the fields it knows; it neither drops added
    # columns nor cascades into a side table it has never heard of.
    conn=sqlite3.connect(path)
    conn.execute("UPDATE cards SET title='Legacy title' WHERE id='old'")
    conn.execute("UPDATE schema_meta SET value='13' WHERE key='version'")
    conn.commit();conn.close()
    s.connect();r=s.outcome_card_report('old')
    assert r['card']['accepted'] and r['card']['revision']==2
    assert r['events'][0]['evidence']=='Kept evidence'
    assert s.get('old')['title']=='Legacy title'
    s.close()


def test_failed_decision_keeps_confirmed_review_sample(store,monkeypatch):
    c=card(store);store.record_outcome_run(c['id'],'claude','sid','implementation')
    store.update(c['id'],{'column_name':'done'})
    store.observe_outcome(c['id'],set(),utc=100,monotonic=0)
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.time',lambda:110)
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.monotonic',lambda:10)
    store._conn.execute("CREATE TRIGGER refuse_accept BEFORE INSERT ON outcome_events WHEN NEW.kind='accepted' BEGIN SELECT RAISE(ABORT,'disk refusal'); END")
    with pytest.raises(sqlite3.DatabaseError):decide(store,c)
    store.observe_outcome(c['id'],set(),utc=120,monotonic=20)
    r=report(store,c)
    assert r['wait_seconds']==20 and not r['accepted']


def test_mixed_currencies_stay_separate_and_one_card_mixed_is_partial(store):
    a=card(store);b=card(store)
    for c,currency in [(a,'USD'),(b,'EUR')]:
        store.record_outcome_run(c['id'],'claude',c['id'],'implementation')
        store.observe_outcome(c['id'],set(),costs=[dict(provider='claude',session_id=c['id'],amount=4,currency=currency,source='complete_root_meter',complete=True)])
        decide(store,c,c['id'])
    summary=store.outcome_project_report('/project',time.time()-100,time.time()+100)['summary']
    assert set(summary['cost_per_outcome'])=={'USD','EUR'}
    assert all(v['covered']==1 for v in summary['cost_per_outcome'].values())
    store.record_outcome_run(a['id'],'grok','other','refinement')
    store.observe_outcome(a['id'],set(),costs=[dict(provider='grok',session_id='other',amount=2,currency='EUR',source='meter',complete=True)])
    assert report(store,a)['cost']['coverage']=='partial'


def test_missing_first_observation_stays_partial_when_samples_resume(store):
    c=card(store)
    store.observe_outcome(c['id'],None,utc=100,monotonic=0)
    store.observe_outcome(c['id'],{'question'},utc=105,monotonic=5)
    store.observe_outcome(c['id'],{'question'},utc=110,monotonic=10)
    r=report(store,c)
    assert r['wait_seconds']==5 and r['wait_coverage']=='partial'


def test_run_pages_continue_after_event_history_ends(store):
    c = card(store)
    for n in range(5):
        assert store.record_outcome_run(c['id'], 'claude', str(n), 'implementation')
    offset, sessions = 0, []
    while True:
        page = store.outcome_card_report(c['id'], limit=2, offset=offset)
        sessions.extend(r['session_id'] for r in page['card']['runs'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    assert sessions == [str(n) for n in range(5)]


@pytest.mark.parametrize('move', ['update', 'reorder'])
@pytest.mark.parametrize('rollback', [False, True])
def test_human_reopen_closes_review_at_action_and_commits_memory(store, monkeypatch, move, rollback):
    c = card(store)
    store.record_outcome_run(c['id'], 'claude', 'sid', 'implementation')
    store.update(c['id'], {'column_name': 'done'})
    store.observe_outcome(c['id'], set(), utc=100, monotonic=0)
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.time', lambda: 110)
    monkeypatch.setattr('dark_army_daemon.board_outcome_store.time.monotonic', lambda: 10)
    if rollback:
        store._conn.execute("CREATE TRIGGER refuse_reopen BEFORE INSERT ON outcome_events WHEN NEW.kind='reopened' BEGIN SELECT RAISE(ABORT,'disk refusal'); END")
    def reopen():
        if move == 'update':
            return store.update(c['id'], {'column_name': 'backlog'})
        return store.reorder(c['id'], 'backlog', '')
    if rollback:
        with pytest.raises(sqlite3.DatabaseError):
            reopen()
        assert store.get(c['id'])['column_name'] == 'done'
    else:
        assert reopen()[0] is not None
    store.observe_outcome(c['id'], set(), utc=120, monotonic=20)
    r = report(store, c)
    assert r['wait_seconds'] == (20 if rollback else 10)
    assert r['wait_causes'] == {'review': 20 if rollback else 10}


def outcome_row(cid, **fields):
    """The shape `outcome_project_report` hands `project_summary`."""
    row = dict(card_id=cid, accepted=0, first_accepted_at=None, rework_open=0,
               submission_at=None, wait_coverage='unknown', wait_seconds=0.0,
               cost={'totals': {}, 'coverage': 'unknown'})
    row.update(fields)
    return row


def test_observed_cost_uses_the_partial_readings_production_actually_writes(store):
    a=card(store);b=card(store)
    store.record_outcome_run(a['id'],'claude','sa','implementation')
    store.observe_outcome(a['id'],set(),costs=[dict(provider='claude',session_id='sa',amount=6,currency='USD',source='meter',complete=False)])
    decide(store,a,'a');decide(store,b,'b')
    summary=store.outcome_project_report('/project',time.time()-100,time.time()+100)['summary']
    # The complete-only figure is still absent — no reading was flipped.
    assert summary['cost_per_outcome']=={}
    assert summary['partial_cost_totals']=={'USD':6}
    assert summary['observed_cost_per_outcome']['USD']==dict(total=6,covered=1,per_accepted_outcome=6,outcomes=2)
    assert summary['cost_reason']==''
    assert summary['observed_cost_note']==m.OBSERVED_COST_NOTE


def test_a_complete_reading_is_observed_money_too(store):
    a=card(store);b=card(store)
    store.record_outcome_run(a['id'],'claude','s','implementation')
    store.observe_outcome(a['id'],set(),costs=[dict(provider='claude',session_id='s',amount=4,currency='USD',source='meter',complete=True)])
    decide(store,a,'a');decide(store,b,'b')
    summary=store.outcome_project_report('/project',time.time()-100,time.time()+100)['summary']
    assert summary['cost_per_outcome']['USD']==dict(total=4,covered=1,per_accepted_outcome=4,outcomes=2)
    assert summary['observed_cost_per_outcome']['USD']==summary['cost_per_outcome']['USD']


def test_nothing_accepted_names_the_reason_and_shows_awaiting_spend():
    rows=[outcome_row('a',submission_at=12,cost={'totals':{'USD':3.0},'coverage':'partial'}),
          outcome_row('b',submission_at=13)]
    s=m.project_summary(rows,[],[],10,20)
    assert s['observed_cost_per_outcome']=={}
    assert s['cost_reason']==m.cost_reason(0,{}) and 'accepted' in s['cost_reason']
    assert s['awaiting_acceptance']==2
    assert s['awaiting_cost_totals']=={'USD':3.0} and s['awaiting_cost_covered']==1


def test_accepted_without_any_reading_names_the_other_reason():
    s=m.project_summary([outcome_row('a',accepted=1,first_accepted_at=15)],[],[],10,20)
    assert s['cost_reason']==m.cost_reason(1,{})
    assert m.cost_reason(1,{}) and m.cost_reason(1,{})!=m.cost_reason(0,{})
    assert m.cost_reason(0,{'USD':{}})=='' and m.cost_reason(1,{'USD':{}})==''


def test_observed_currencies_are_never_summed_together():
    rows=[outcome_row('a',accepted=1,first_accepted_at=11,cost={'totals':{'USD':4.0},'coverage':'partial'}),
          outcome_row('b',accepted=1,first_accepted_at=12,cost={'totals':{'EUR':10.0},'coverage':'partial'})]
    observed=m.project_summary(rows,[],[],10,20)['observed_cost_per_outcome']
    assert set(observed)=={'USD','EUR'}
    assert observed['USD']['per_accepted_outcome']==4 and observed['EUR']['per_accepted_outcome']==10
    assert all(v['covered']==1 and v['outcomes']==2 for v in observed.values())


def test_rework_open_card_is_not_awaiting_acceptance():
    rows=[outcome_row('a',submission_at=12,rework_open=1,cost={'totals':{'USD':5.0},'coverage':'partial'})]
    s=m.project_summary(rows,[],[],10,20)
    assert s['awaiting_acceptance']==0
    assert s['awaiting_cost_totals']=={} and s['awaiting_cost_covered']==0


def test_genuine_zero_observed_cost_is_a_figure_not_an_absence():
    rows=[outcome_row('a',accepted=1,first_accepted_at=11,cost={'totals':{'USD':0.0},'coverage':'partial'})]
    s=m.project_summary(rows,[],[],10,20)
    assert s['observed_cost_per_outcome']['USD']['per_accepted_outcome']==0.0
    assert s['cost_reason']==''


# ── write only where the row would change, one transaction per pass ────────

def _wal_stat(tmp_path):
    st = (tmp_path / 'board.db-wal').stat()
    return st.st_size, st.st_mtime_ns


def test_unchanged_pass_writes_nothing_and_leaves_the_wal_alone(store, tmp_path):
    c = card(store)
    before = store._conn.total_changes
    store.observe_outcome(c['id'], set(), utc=100, monotonic=0)
    first = store._conn.total_changes
    assert first > before
    wal = _wal_stat(tmp_path)
    time.sleep(0.01)
    store.observe_outcome(c['id'], set(), utc=104, monotonic=4)
    store.observe_outcomes([(c['id'], set(), [])], utc=108, monotonic=8)
    assert store._conn.total_changes == first
    assert _wal_stat(tmp_path) == wal
    # A moved sample still writes: the card now waits on a question.
    store.observe_outcome(c['id'], {'question'}, utc=112, monotonic=12)
    assert store._conn.total_changes > first


def test_unchanged_pass_repeats_no_cost_reading(store):
    c = card(store)
    store.record_outcome_run(c['id'], 'claude', 's', 'implementation')
    reading = [dict(session_id='s', provider='claude', amount=2.5, currency='USD',
                    source='meter', complete=True)]
    store.observe_outcome(c['id'], set(), utc=100, monotonic=0, costs=reading)
    first = store._conn.total_changes
    store.observe_outcome(c['id'], set(), utc=104, monotonic=4, costs=reading)
    assert store._conn.total_changes == first
    assert report(store, c)['cost']['totals'] == {'USD': 2.5}


def test_observe_outcomes_is_one_commit_for_the_whole_pass(store):
    cards = [card(store) for _ in range(5)]
    statements = []
    store._conn.set_trace_callback(statements.append)
    try:
        failed = store.observe_outcomes([(c['id'], set(), []) for c in cards],
                                        utc=100, monotonic=0)
    finally:
        store._conn.set_trace_callback(None)
    assert failed is False
    assert sum(1 for s in statements if s.strip().upper() == 'COMMIT') == 1
    rows = store._conn.execute('SELECT COUNT(*) FROM outcome_checkpoints').fetchone()[0]
    assert rows == 5


def test_restart_partial_holds_for_a_card_whose_later_writes_were_skipped(store):
    """The checkpoint row's existence is what a restart marks partial, so a
    card whose every later pass was skipped must still read partial."""
    c = card(store)
    store.observe_outcome(c['id'], set(), utc=100, monotonic=0)
    store.observe_outcome(c['id'], set(), utc=104, monotonic=4)
    store.observe_outcome(c['id'], set(), utc=108, monotonic=8)
    assert report(store, c)['wait_coverage'] == 'complete_since_tracking'
    store.close()
    store.connect()
    assert report(store, c)['wait_coverage'] == 'partial'
    # And the first pass after the restart writes the row again.
    assert store._conn.execute('SELECT COUNT(*) FROM outcome_checkpoints').fetchone()[0] == 0
    store.observe_outcome(c['id'], set(), utc=1000, monotonic=0)
    assert store._conn.execute('SELECT COUNT(*) FROM outcome_checkpoints').fetchone()[0] == 1


def test_a_failing_card_rolls_back_alone_and_the_pass_still_lands(store, monkeypatch):
    good, bad, other = card(store), card(store), card(store)
    real = store._observe_outcome_locked

    def observe(card_id, *args):
        result = real(card_id, *args)
        if card_id == bad['id']:
            raise sqlite3.OperationalError('disk')
        return result
    monkeypatch.setattr(store, '_observe_outcome_locked', observe)
    failed = store.observe_outcomes(
        [(c['id'], set(), []) for c in (good, bad, other)], utc=100, monotonic=0)
    assert failed is True
    written = {r[0] for r in store._conn.execute('SELECT card_id FROM outcome_checkpoints')}
    assert written == {good['id'], other['id']}
    # The failing card's sample was not remembered either: a committed row and
    # an in-memory sample never disagree.
    assert bad['id'] not in store._outcome_samples
    assert good['id'] in store._outcome_samples


def test_unchanged_pass_repeats_no_conflicting_late_bind(store, tmp_path):
    """One session seen against two cards is a conflict, marked once: the
    observation pass re-sees it every time and must not rewrite the marks."""
    a, b = card(store), card(store)
    assert store.record_outcome_run(a['id'], 'claude', 'shared', 'refinement', late=True)
    assert not store.record_outcome_run(b['id'], 'claude', 'shared', 'refinement', late=True)
    first = store._conn.total_changes
    counter = store.change_counter()
    wal = _wal_stat(tmp_path)
    time.sleep(0.01)
    for _ in range(3):
        assert not store.record_outcome_run(b['id'], 'claude', 'shared', 'refinement', late=True)
        assert store.record_outcome_run(a['id'], 'claude', 'shared', 'refinement', late=True)
    assert store._conn.total_changes == first
    assert store.change_counter() == counter
    assert _wal_stat(tmp_path) == wal
    run = store._conn.execute("SELECT cost_conflict, cost_coverage FROM outcome_runs "
                              "WHERE session_id='shared'").fetchone()
    assert (run['cost_conflict'], run['cost_coverage']) == (1, 'partial')


# ── wait seconds accrue in memory and flush on a schedule ──────────────────

from dark_army_daemon import board_outcome_store as outcome_store  # noqa: E402


def _wait_rows(s):
    return {(r['card_id'], r['day'], r['cause']): r['seconds'] for r in
            s._conn.execute('SELECT card_id, day, cause, seconds FROM outcome_wait_days')}


def test_waiting_cards_unchanged_pass_writes_no_wait_row_and_leaves_the_wal_alone(store, tmp_path):
    """The live check's fault: a card waiting on a question gained seconds in
    `outcome_wait_days` on every pass, so the WAL moved on every pass."""
    cards = [card(store) for _ in range(3)]
    samples = [(c['id'], {'question'}, []) for c in cards]
    store.observe_outcomes(samples, utc=100, monotonic=0)
    first = store._conn.total_changes
    wal = _wal_stat(tmp_path)
    time.sleep(0.01)
    for n in range(1, 6):
        assert store.observe_outcomes(samples, utc=100 + 4 * n, monotonic=4 * n) is False
    assert store._conn.total_changes == first
    assert _wal_stat(tmp_path) == wal
    assert _wait_rows(store) == {}
    # Nothing is lost: every reader adds what is pending.
    assert report(store, cards[0])['wait_seconds'] == 20
    assert report(store, cards[0])['wait_causes'] == {'question': 20}


def test_seconds_accrued_over_many_passes_flush_as_their_sum(store):
    c = card(store)
    step = 4
    interval = outcome_store.WAIT_FLUSH_SECONDS
    assert interval >= 60 and interval % step == 0
    t = 0
    while t + step < interval:
        store.observe_outcomes([(c['id'], {'permission'}, [])], utc=1000 + t, monotonic=t)
        t += step
    assert _wait_rows(store) == {}
    assert report(store, c)['wait_seconds'] == t - step
    # The first pass once the interval has lapsed writes the whole sum.
    t = interval
    store.observe_outcomes([(c['id'], {'permission'}, [])], utc=1000 + t, monotonic=t)
    rows = _wait_rows(store)
    assert sum(v for k, v in rows.items() if k[2] == 'overall') == t
    assert sum(v for k, v in rows.items() if k[2] == 'permission') == t
    assert store._outcome_pending == {}
    assert report(store, c)['wait_seconds'] == t


def test_a_cause_change_flushes_at_once(store):
    c = card(store)
    store.observe_outcome(c['id'], {'question'}, utc=100, monotonic=0)
    store.observe_outcome(c['id'], {'question'}, utc=105, monotonic=5)
    assert _wait_rows(store) == {}
    store.observe_outcome(c['id'], {'question', 'permission'}, utc=110, monotonic=10)
    rows = _wait_rows(store)
    assert sum(v for k, v in rows.items() if k[2] == 'question') == 10
    assert sum(v for k, v in rows.items() if k[2] == 'overall') == 10
    assert store._outcome_pending == {}


def test_a_day_roll_splits_the_seconds_and_flushes(store):
    c = card(store)
    midnight = 86400 * 20000
    store.observe_outcome(c['id'], {'review'}, utc=midnight - 8, monotonic=0)
    store.observe_outcome(c['id'], {'review'}, utc=midnight - 4, monotonic=4)
    assert _wait_rows(store) == {}
    # This interval crosses midnight: yesterday is complete, so it lands now.
    store.observe_outcome(c['id'], {'review'}, utc=midnight + 2, monotonic=10)
    rows = _wait_rows(store)
    days = sorted({k[1] for k in rows})
    assert len(days) == 2
    by_day = {d: rows[(c['id'], d, 'overall')] for d in days}
    assert by_day[days[0]] == 8 and by_day[days[1]] == 2


def test_both_reports_read_the_pending_seconds(store):
    c = card(store)
    decide(store, c)
    store.observe_outcome(c['id'], {'manual_check'}, utc=time.time() - 10, monotonic=0)
    store.observe_outcome(c['id'], {'manual_check'}, utc=time.time() - 3, monotonic=7)
    assert _wait_rows(store) == {}
    assert report(store, c)['wait_seconds'] == 7
    assert report(store, c)['wait_causes'] == {'manual_check': 7}
    summary = store.outcome_project_report('/project', time.time() - 100,
                                           time.time() + 100)['summary']
    assert summary['observed_card_hours'] == 7 / 3600


@pytest.mark.parametrize('batch', [True, False])
def test_a_lost_flush_neither_loses_nor_double_counts(store, batch):
    c = card(store)
    observe = ((lambda causes, u, mono: store.observe_outcomes([(c['id'], causes, [])], utc=u, monotonic=mono))
               if batch else (lambda causes, u, mono: store.observe_outcome(c['id'], causes, utc=u, monotonic=mono)))
    observe({'question'}, 100, 0)
    observe({'question'}, 105, 5)
    store._conn.execute("CREATE TRIGGER refuse_wait BEFORE INSERT ON outcome_wait_days "
                        "BEGIN SELECT RAISE(ABORT,'disk refusal'); END")
    if batch:
        # The cause change asks for a flush; it rolls back alone and the
        # pass reports the failure.
        assert observe({'permission'}, 110, 10) is True
    else:
        with pytest.raises(sqlite3.DatabaseError):
            observe({'permission'}, 110, 10)
    assert _wait_rows(store) == {}
    assert report(store, c)['wait_seconds'] == (10 if batch else 5)
    store._conn.execute('DROP TRIGGER refuse_wait')
    observe({'permission'}, 115, 15)
    observe(set(), 120, 20)
    rows = _wait_rows(store)
    assert sum(v for k, v in rows.items() if k[2] == 'overall') == 20
    # A whole transaction lost (the single-card path) keeps the old sample
    # too, so its interval is confirmed later under the causes it had —
    # exactly what a lost observation always meant.
    question, permission = (10, 10) if batch else (15, 5)
    assert sum(v for k, v in rows.items() if k[2] == 'question') == question
    assert sum(v for k, v in rows.items() if k[2] == 'permission') == permission
    assert store._outcome_pending == {}


def test_a_clean_close_flushes_what_is_pending(store):
    c = card(store)
    store.observe_outcome(c['id'], {'question'}, utc=100, monotonic=0)
    store.observe_outcome(c['id'], {'question'}, utc=106, monotonic=6)
    assert _wait_rows(store) == {}
    store.close()
    store.connect()
    assert report(store, c)['wait_seconds'] == 6
    assert sum(v for k, v in _wait_rows(store).items() if k[2] == 'overall') == 6


def test_a_flush_is_the_observers_write_not_a_board_write(store):
    c = card(store)
    store.observe_outcome(c['id'], {'question'}, utc=100, monotonic=0)
    counter = store.change_counter()
    store.observe_outcomes([(c['id'], {'permission'}, [])], utc=105, monotonic=5)
    assert _wait_rows(store)
    assert store.change_counter() == counter
