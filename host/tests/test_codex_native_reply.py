"""Native reply authority: real journals, fake OS/editor boundary, no live input."""
import asyncio
from types import MappingProxyType

import pytest

from dark_army_daemon import codex_rollouts, daemon, vscode_reveal
from tests.test_board_refine import refinement_handoff  # noqa: F401 — fixture dependency
from tests.test_codex_human_close import stopped_native, _append, _event, _reload, _row  # noqa: F401 — shared fixture


@pytest.fixture
def native_reply(stopped_native, monkeypatch):
    d, store, card, plan, records, processes, posts = stopped_native
    d.typed_reply_enabled = True
    # The bridge at exactly the native-reply gate, whatever it is today.
    current = '.'.join(str(n) for n in vscode_reveal.NATIVE_REPLY_MIN_VERSION)
    locks = [dict(l, extensionVersion=current) for l in vscode_reveal._bob_ext_locks()]
    monkeypatch.setattr(vscode_reveal, '_bob_ext_locks', lambda: locks)
    async def post(port, token, body, **kwargs):
        if not await kwargs['before_write']():
            return None
        posts.append(body)
        return {'matched': True, 'sent': True}
    monkeypatch.setattr(vscode_reveal, '_post_json', post)
    return stopped_native


@pytest.mark.asyncio
async def test_one_stopped_opted_in_root_submits_once_without_question_or_stop_authority(native_reply):
    d, store, card, _, records, _, posts = native_reply
    sid = records[0].session_id
    row = _row(d, records[0])
    assert row['channel'] and row['reply_via'] == 'typed'
    assert not row['can_type'] and not row['can_stop']
    assert (await d.reply_to_session(sid, 'Continue with the fix')) == (True, '')
    assert [{k: v for k, v in p.items() if k != 'expires_at_ms'} for p in posts] == [{'op': 'reply_native_terminal', 'pid': 701, 'tty': '/dev/ttys040', 'text': 'Continue with the fix'}]
    assert type(posts[0]['expires_at_ms']) is int
    assert not (await d.reply_to_session(sid, 'Again'))[0]
    assert len(posts) == 1
    assert not _row(d, records[0])['channel']
    assert not d._closed_ids and store.get(card['id'])['column_name'] == 'prep'


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['', ' ', 'hello\nworld', '\nhello', 'hi\r', '\x1bhello', 'hi\x7f', '\thi', '\u0085hi', 'hi\u2028there', ' /clear', '\u00a0!shell', '# heading', 'a' * 2001])
async def test_invalid_plain_text_never_writes(native_reply, text):
    d, _, _, _, records, _, posts = native_reply
    assert not (await d.reply_to_session(records[0].session_id, text))[0]
    assert not posts and not d._codex_reply_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['preference', 'running', 'question', 'permission', 'pending', 'child', 'orphan', 'collision', 'helper', 'old_bridge', 'no_bridge', 'ambiguous_bridge', 'unverified', 'busy'])
async def test_ineligible_never_writes(native_reply, monkeypatch, fault):
    d, _, _, _, records, _, posts = native_reply
    record, sid = records[0], records[0].session_id
    if fault == 'preference': d.typed_reply_enabled = False
    elif fault == 'running': record.turn_active = True
    elif fault == 'question': record.stats.question = {'text': 'Choose?'}
    elif fault == 'permission': monkeypatch.setattr(d, '_prompts_by_session', lambda: {sid: {}})
    elif fault == 'pending': d._pending_questions[sid] = {'text': 'Choose?'}
    elif fault == 'child': record.parent_thread_id = 'parent'
    elif fault == 'orphan': record.explicit_subagent = True
    elif fault == 'collision': d._grok_records[sid] = object()
    elif fault == 'helper': record.stats.agents['missing'] = codex_rollouts.AgentInfo(agent_id='missing', activity='running')
    elif fault == 'unverified': d._codex_navigation = MappingProxyType({})
    elif fault == 'busy': d._answering.add(sid)
    else:
        locks = [dict(l) for l in vscode_reveal._bob_ext_locks()]
        if fault == 'old_bridge': locks[0]['extensionVersion'] = '0.1.16'
        elif fault == 'no_bridge': locks = []
        else: locks *= 2
        monkeypatch.setattr(vscode_reveal, '_bob_ext_locks', lambda: locks)
    assert not (await d.reply_to_session(sid, 'Continue'))[0]
    assert not posts and not d._codex_reply_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize(('bridge', 'explanation'), [
    ('stale', 'running Dark Army IDE 0.1.20'),
    ('missing', 'No Dark Army IDE connection owns this project'),
    ('ambiguous', 'More than one VS Code window'),
    ('unknown_version', 'running Dark Army IDE unknown'),
])
async def test_queued_phone_answer_explains_editor_connection_refusal(
        native_reply, monkeypatch, bridge, explanation):
    d, _, _, _, records, _, posts = native_reply
    record = _ask_in_new_turn(d, records, 'request_user_input_async')
    locks = [dict(lock) for lock in vscode_reveal._bob_ext_locks()]
    if bridge == 'stale':
        locks[0]['extensionVersion'] = '0.1.20'
    elif bridge == 'missing':
        locks = []
    elif bridge == 'ambiguous':
        locks *= 2
    else:
        locks[0]['extensionVersion'] = 'unparseable'
    monkeypatch.setattr(vscode_reveal, '_bob_ext_locks', lambda: locks)
    row = _row(d, record)
    assert not row['channel'] and row['reply_via'] == ''
    assert explanation in row['interaction_note']
    if bridge == 'stale':
        assert 'Reload Window' in row['interaction_note']
    assert not (await d.reply_to_session(record.session_id, 'Continue'))[0]
    assert not posts and not d._codex_reply_attempts


@pytest.mark.asyncio
async def test_queued_phone_answer_uses_only_the_matching_project_connection(native_reply, monkeypatch):
    d, _, _, _, records, _, posts = native_reply
    record = _ask_in_new_turn(d, records, 'request_user_input_async')
    owner = dict(vscode_reveal._bob_ext_locks()[0])
    foreign = dict(owner, port=9999, authToken='foreign-token',
                   workspaceFolders=['/another/project'])
    monkeypatch.setattr(vscode_reveal, '_bob_ext_locks', lambda: [foreign, owner])
    async def post(port, token, body, **kwargs):
        assert port == owner['port'] and token == owner['authToken']
        assert await kwargs['before_write']()
        posts.append(body)
        return {'matched': True, 'sent': True}
    monkeypatch.setattr(vscode_reveal, '_post_json', post)
    assert _row(d, record)['channel']
    assert (await d.reply_to_session(record.session_id, 'Continue')) == (True, '')
    assert len(posts) == 1 and posts[0]['pid'] == 701


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', [1, 2])
@pytest.mark.parametrize('fault', ['preference', 'turn', 'generation', 'journal', 'pid', 'ctime', 'cwd', 'argv'])
async def test_each_observation_rejects_identity_and_state_changes(native_reply, monkeypatch, stage, fault):
    d, _, _, _, records, processes, posts = native_reply
    original = codex_rollouts.refinement_close_observation
    calls = 0
    def observe(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == stage:
            if fault == 'preference': d.typed_reply_enabled = False
            elif fault == 'turn': records[0].turn_id = 'new-turn'
            elif fault == 'generation':
                from copy import deepcopy
                d._codex_records = deepcopy(d._codex_records)
            elif fault == 'journal': _append(records[0], _event('task_started', 'next'))
            else:
                field = {'pid': 'pid', 'ctime': 'create_time', 'cwd': 'cwd', 'argv': 'cmdline'}[fault]
                value = {'pid': 999, 'ctime': 999.0, 'cwd': '/different', 'argv': ('other',)}[fault]
                processes[0].fresh[field] = value
                processes[0].info[field] = value
                if fault == 'pid': processes[0].pid = value
            # A journal/OS change during observation is caught by the real
            # observation's own final pin; model it before its returned proof.
            if fault in {'journal', 'pid', 'ctime', 'cwd', 'argv'}:
                result = original(*args, **kwargs)
        return result
    monkeypatch.setattr(codex_rollouts, 'refinement_close_observation', observe)
    assert not (await d.reply_to_session(records[0].session_id, 'Continue'))[0]
    assert not posts and not d._codex_reply_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [None, {}, {'matched': True}, {'matched': 'true', 'sent': True}, {'matched': True, 'sent': False}])
async def test_unconfirmed_submission_is_spent_and_never_retried(native_reply, monkeypatch, result):
    d, _, _, _, records, _, posts = native_reply
    async def post(port, token, body, **kwargs):
        assert await kwargs['before_write']()
        posts.append(body)
        return result
    monkeypatch.setattr(vscode_reveal, '_post_json', post)
    sid = records[0].session_id
    assert not (await d.reply_to_session(sid, 'Continue'))[0]
    assert not (await d.reply_to_session(sid, 'Again'))[0]
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_double_click_does_not_queue_a_later_turn(native_reply, monkeypatch):
    d, _, _, _, records, _, posts = native_reply
    sid = records[0].session_id
    results = await asyncio.gather(d.reply_to_session(sid, 'One'), d.reply_to_session(sid, 'Two'))
    assert sum(ok for ok, _ in results) == 1 and len(posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('after_write', [False, True])
async def test_timeout_has_no_late_write_and_spends_only_started_submission(native_reply, monkeypatch, after_write):
    d, _, _, _, records, _, posts = native_reply
    monkeypatch.setattr(daemon, 'REFINEMENT_CLOSE_TIMEOUT', 0.5)
    async def post(port, token, body, **kwargs):
        if after_write:
            assert await kwargs['before_write']()
            posts.append(body)
        await asyncio.sleep(0.7)
        posts.append({'late': True})
    monkeypatch.setattr(vscode_reveal, '_post_json', post)
    sid = records[0].session_id
    assert not (await d.reply_to_session(sid, 'Continue'))[0]
    await asyncio.sleep(0.75)
    assert len(posts) == int(after_write)
    assert bool(d._codex_reply_attempts) is after_write
    assert sid not in d._answering


@pytest.mark.asyncio
async def test_new_observed_turn_allows_a_new_reply(native_reply):
    d, _, _, _, records, _, posts = native_reply
    sid = records[0].session_id
    assert (await d.reply_to_session(sid, 'First'))[0]
    _append(records[0], _event('task_started', 'turn-2', '2026-09-12T19:00:03Z'))
    _append(records[0], _event('task_complete', 'turn-2', '2026-09-12T19:00:04Z'))
    _reload(d, records)
    d._codex_navigation = MappingProxyType(codex_rollouts.resolve_navigation_proofs(codex_rollouts.project_title_roots(records)))
    assert (await d.reply_to_session(sid, 'Second'))[0]
    assert len(posts) == 2


@pytest.mark.asyncio
async def test_timed_out_observation_worker_cannot_later_authorize_send(native_reply, monkeypatch):
    import threading
    d, _, _, _, records, _, posts = native_reply
    original = codex_rollouts.refinement_close_observation
    released = threading.Event()
    def slow(*args, **kwargs):
        released.wait(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(codex_rollouts, 'refinement_close_observation', slow)
    monkeypatch.setattr(daemon, 'REFINEMENT_CLOSE_TIMEOUT', 0.03)
    try:
        assert not (await d.reply_to_session(records[0].session_id, 'Continue'))[0]
    finally:
        released.set()
    await asyncio.sleep(0.15)
    assert not posts and not d._codex_reply_attempts


@pytest.mark.asyncio
async def test_waiting_for_dispatch_lock_cannot_retarget_new_turn(native_reply):
    d, _, _, _, records, _, posts = native_reply
    await d._dispatch_lock.acquire()
    task = asyncio.create_task(d.reply_to_session(records[0].session_id, 'Continue'))
    await asyncio.sleep(0.01)
    records[0].turn_id = 'next-turn'
    d._dispatch_lock.release()
    assert not (await task)[0]
    assert not posts and not d._codex_reply_attempts


@pytest.mark.asyncio
async def test_native_reply_never_reads_foreign_environment(native_reply, monkeypatch):
    d, _, _, _, records, _, posts = native_reply
    def forbidden(*args, **kwargs):
        pytest.fail('native reply reached legacy foreign-environment inspection')
    monkeypatch.setattr(vscode_reveal, '_in_vscode', forbidden)
    monkeypatch.setattr(vscode_reveal, '_session_in_vscode', forbidden)
    sid = records[0].session_id
    assert d._codex_reply_candidate(sid)[0] is not None
    assert (await d.reply_to_session(sid, 'Continue'))[0]
    assert len(posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('moved', [False, True])
async def test_replaced_or_moved_journal_does_not_rearm_same_turn(native_reply, moved):
    from tests.test_codex_rollouts import _native_holder
    d, _, _, _, records, processes, posts = native_reply
    sid = records[0].session_id
    assert (await d.reply_to_session(sid, 'First'))[0]
    old = records[0].path
    replacement = old.with_name('replacement.jsonl')
    replacement.write_bytes(old.read_bytes())
    if moved:
        old.unlink()
        records[0].path = replacement
    else:
        replacement.replace(old)
    _reload(d, records)
    processes[0] = _native_holder(701, records[0].path, 'ttys040', cwd=records[0].cwd)
    d._codex_navigation = MappingProxyType(codex_rollouts.resolve_navigation_proofs(codex_rollouts.project_title_roots(records)))
    assert d._navigation_proof_current(sid) is not None
    assert not (await d.reply_to_session(sid, 'Second'))[0]
    assert len(posts) == 1


def _ask_in_new_turn(d, records, name):
    """A second turn that asks through `name`, is acknowledged and completes."""
    import json as _json
    call = {"timestamp": "2026-09-12T19:00:03Z", "type": "response_item",
            "payload": {"type": "function_call", "name": name, "call_id": "ask",
                        "arguments": _json.dumps({"questions": [
                            {"title": "What next?", "question": "What next?",
                             "options": ["Continue", "Stop", "Hand back"]}]})}}
    ack = {"timestamp": "2026-09-12T19:00:03Z", "type": "response_item",
           "payload": {"type": "function_call_output", "call_id": "ask",
                       "output": _json.dumps({"accepted": True})}}
    _append(records[0], _event('task_started', 'turn-2', '2026-09-12T19:00:03Z'))
    _append(records[0], call)
    _append(records[0], ack)
    _append(records[0], _event('task_complete', 'turn-2', '2026-09-12T19:00:04Z'))
    record = _reload(d, records)
    d._codex_navigation = MappingProxyType(codex_rollouts.resolve_navigation_proofs(
        codex_rollouts.project_title_roots(records)))
    return record


@pytest.mark.asyncio
async def test_queued_async_question_is_answered_by_the_reply(native_reply):
    # The phone showed a queued Codex question read-only on 24 Sep 2026 with
    # "Answer it in the original Codex session": Codex takes the person's next
    # message as a queued question's answer, so the reply route stays open.
    d, _, _, _, records, _, posts = native_reply
    record = _ask_in_new_turn(d, records, 'request_user_input_async')
    sid = record.session_id
    assert record.stats.question and record.question_async
    assert not codex_rollouts.stopped_turn(record)
    assert codex_rollouts.stopped_turn(record, awaiting_answer=True)
    row = _row(d, record)
    assert row['channel'] and row['reply_via'] == 'typed' and row['interaction_note'] == ''
    assert not row['can_type'] and not row['can_close']
    assert (await d.reply_to_session(sid, 'Continue')) == (True, '')
    assert [p['text'] for p in posts] == ['Continue']


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['sync', 'permission', 'pending'])
async def test_queued_question_reply_keeps_every_other_refusal(native_reply, monkeypatch, fault):
    d, _, _, _, records, _, posts = native_reply
    record = _ask_in_new_turn(d, records, 'request_user_input_async')
    sid = record.session_id
    if fault == 'sync':
        # A live picker: the typed line's Enter would pick the highlighted option.
        record.question_async = False
    elif fault == 'permission':
        monkeypatch.setattr(d, '_prompts_by_session', lambda: {sid: {}})
    else:
        d._pending_questions[sid] = {'text': 'Choose?'}
    assert d._codex_reply_candidate(sid)[0] is None
    assert not (await d.reply_to_session(sid, 'Continue'))[0]
    assert not posts and not d._codex_reply_attempts


def test_queued_question_never_grants_close(native_reply):
    d, _, _, _, records, _, _ = native_reply
    record = _ask_in_new_turn(d, records, 'request_user_input_async')
    assert d._codex_human_close_candidate(record.session_id) is None
    assert d._codex_human_close_candidate(record.session_id, awaiting_answer=True) is not None
