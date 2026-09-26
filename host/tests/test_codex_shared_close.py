"""Run the human-close contract against shared-server TUI ownership as well."""
from dataclasses import replace
import json

import pytest

from dark_army_daemon import codex_terminal as terminal
from tests.test_codex_human_close import (  # noqa: F401
    stopped_native as legacy_stopped_native, _reload,
    test_finished_native_root_can_acknowledge_without_stop_or_attachment,
    test_ineligible_native_root_never_advertises_or_closes,
    test_unconfirmed_human_close_never_retires_or_finishes,
    test_human_close_finishes_bound_card_only_after_terminal_confirmation,
    test_native_close_checks_retained_child_completion,
    test_final_validation_after_transport_preparation_refuses_changes,
)
from tests.test_board_refine import refinement_handoff  # noqa: F401


@pytest.fixture
def stopped_native(legacy_stopped_native, monkeypatch):
    d, store, card, plan, records, processes, posts = legacy_stopped_native
    record = records[0]
    rows = [json.loads(line) for line in record.path.read_text().splitlines()]
    rows[0]['payload']['source'] = 'vscode'
    record.path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    record = _reload(d, records)
    target = terminal.Target(terminal.roots((record,))[0], 701, '/bin/codex',
                             100.0, ('/bin/codex',), '/dev/ttys040', 4100)
    d._codex_navigation = {}
    d._codex_terminals = {record.session_id: target}
    async def resolve(captured, *, recheck=False, stopped_turns=None):
        assert recheck and stopped_turns == {record.session_id: record.turn_id}
        return {record.session_id: replace(target, created=processes[0].fresh['create_time'])}
    monkeypatch.setattr(terminal, 'resolve', resolve)
    return legacy_stopped_native


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['active', 'other_turn', 'unfinished', 'other_path', 'child', 'flags', None])
async def test_server_must_confirm_addressed_completed_turn(fault, tmp_path):
    root = terminal.Root('codex:root', 'root', tmp_path / 'root.jsonl', str(tmp_path))
    thread = dict(id='root', path=str(root.path), cwd=root.cwd, parentThreadId=None,
                  originator='codex-tui', source='vscode', threadSource='user', status={'type': 'idle'})
    turn = dict(id='turn', status='completed')
    if fault == 'active': thread['status'] = {'type': 'active', 'activeFlags': []}
    if fault == 'flags': thread['status'] = {'type': 'idle', 'activeFlags': ['waitingOnApproval']}
    if fault == 'other_turn': turn['id'] = 'new'
    if fault == 'unfinished': turn['status'] = 'inProgress'
    if fault == 'other_path': thread['path'] = '/other.jsonl'
    if fault == 'child': thread['parentThreadId'] = 'parent'
    async def request(method, params):
        assert params['threadId'] == 'root'
        if method == 'thread/read': return {'thread': thread}
        assert method == 'thread/turns/list'
        return {'data': [turn]}
    assert await terminal._server_stopped(request, root, 'turn') is (fault is None)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['journal_changed', 'target_changed', 'server_refused'])
async def test_recheck_rejects_changes_during_server_observation(stopped_native, monkeypatch, fault):
    from tests.test_codex_human_close import _append, _event
    d, _, _, _, records, _, posts = stopped_native
    record = records[0]
    target = d._codex_terminals[record.session_id]
    async def resolve(*args, **kwargs):
        if fault == 'journal_changed': _append(record, _event('task_started', 'new-turn'))
        if fault == 'server_refused': return {}
        return {record.session_id: replace(target, tty='/dev/other') if fault == 'target_changed' else target}
    monkeypatch.setattr(terminal, 'resolve', resolve)
    assert not (await d.close_session_terminal(record.session_id, by_person=True))[0]
    assert not posts and record.session_id not in d._closed_ids


@pytest.mark.asyncio
async def test_a_closed_shared_thread_comes_back_when_a_new_turn_starts(stopped_native, monkeypatch):
    """A shared-server thread has no process identity to compare after a
    close; the server outlives the terminal. A new turn is the resume, and
    the closed turn written again is not."""
    from dark_army_daemon import enrollment
    from tests.test_codex_human_close import _append, _event
    d, _, _, _, records, _, _ = stopped_native
    sid = records[0].session_id
    closed_turn = records[0].turn_id
    assert (await d.close_session_terminal(sid, by_person=True))[0]
    assert d._closed_codex_turns[sid] == closed_turn
    monkeypatch.setattr(enrollment, 'root_enrolled', lambda cwd: True)
    record = _reload(d, records)
    d._refresh_codex_records(list(records))
    assert sid not in d._codex_records
    _append(record, _event('task_started', 'turn-resumed', '2026-09-12T19:30:00Z'))
    record = _reload(d, records)
    d._codex_records.pop(sid, None)
    assert d._codex_thread_resumed_after_close(record)
    d._refresh_codex_records(list(records))
    assert sid in d._codex_records
    assert sid not in d._closed_ids and sid not in d._closed_codex_turns


@pytest.mark.asyncio
async def test_close_rechecks_every_shared_root_so_a_second_thread_withdraws_it(stopped_native, monkeypatch):
    d, _, _, _, records, _, posts = stopped_native
    sid = records[0].session_id
    seen = []
    async def resolve(captured, *, recheck=False, stopped_turns=None):
        seen.append(captured)
        return {}  # a second thread in the same terminal withdraws both
    monkeypatch.setattr(terminal, 'resolve', resolve)
    assert not (await d.close_session_terminal(sid, by_person=True))[0]
    assert seen and seen[0] == terminal.roots(d._codex_records.values())
    assert not posts and sid not in d._closed_ids


@pytest.mark.asyncio
async def test_a_slow_check_says_nothing_was_sent(stopped_native, monkeypatch):
    import asyncio
    from dark_army_daemon import daemon as daemon_mod
    d, _, _, _, records, _, posts = stopped_native
    monkeypatch.setattr(daemon_mod, 'SHARED_CODEX_CLOSE_TIMEOUT', 0.05)
    async def resolve(captured, *, recheck=False, stopped_turns=None):
        await asyncio.sleep(1)
    monkeypatch.setattr(terminal, 'resolve', resolve)
    ok, detail = await d.close_session_terminal(records[0].session_id, by_person=True)
    assert not ok and 'nothing was sent' in detail and not posts
