"""Shared-server titles reach only the exact, still-owned native terminal."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from dark_army_daemon import codex_rollouts, codex_terminal, codex_titles
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.terminal_title import TitleWriter
from tests.test_codex_rollouts import _root


@pytest.fixture
def titles(tmp_path, monkeypatch):
    records = [_root(str(i), cwd=str(tmp_path), path=tmp_path / f'{i}.jsonl')
               for i in range(2)]
    for r in records:
        r.source_kind = 'vscode'
    targets = tuple(codex_terminal.Target(root, 700 + i, '/bin/codex', 10.,
                    ('/bin/codex',), f'/dev/ttys00{i}', 4100 + i)
                    for i, root in enumerate(codex_terminal.roots(records)))
    listeners = {t.port: [SimpleNamespace(pid=t.pid, executable=t.executable,
        create_time=t.created, argv=t.argv, tty=t.tty, cwd=t.root.cwd, native=True)]
        for t in targets}
    monkeypatch.setattr(codex_terminal, '_listeners', lambda: listeners)
    return records, targets, listeners


def test_same_project_titles_use_private_targets_and_keep_controls_off(titles, monkeypatch):
    records, targets, _ = titles
    daemon = BobDaemon()
    daemon._codex_records = {r.session_id: r for r in records}
    writer = TitleWriter()
    write = Mock(return_value=True)
    monkeypatch.setattr(writer, '_write', write)
    daemon._titles = writer
    snapshot = {'running': [dict(provider='codex', session_id=r.session_id,
        nickname=n, name=f'Task {i}', pid=None)
        for i, (r, n) in enumerate(zip(records, ('Sawa', 'Hex')))]}
    assert codex_rollouts.project_title_roots(records) == ()  # original failure
    daemon._apply_terminal_titles(snapshot, (), targets)
    assert write.call_args_list[0].args == ('/dev/ttys000', 'Saw · Task 0')
    assert write.call_args_list[1].args == ('/dev/ttys001', 'Hex · Task 1')
    daemon._apply_terminal_titles(snapshot, (), targets)
    assert write.call_count == 2  # unchanged titles do not oscillate
    for row in snapshot['running']:
        assert row['pid'] is None
        assert daemon._session_capabilities(row) == dict(
            can_jump=False, can_stop=False, can_hide=True, can_resume=False)
        assert 'tty' not in row


@pytest.mark.parametrize('fault', ['closed', 'reused', 'moved', 'ambiguous', 'changed_between'])
def test_stale_or_uncertain_listener_withdraws_title(titles, monkeypatch, fault):
    _, targets, listeners = titles
    target = targets[0]
    if fault == 'closed':
        listeners.pop(target.port)
    elif fault == 'reused':
        listeners[target.port][0].create_time += 1
    elif fault == 'moved':
        listeners[target.port][0].tty = '/dev/ttys999'
    elif fault == 'ambiguous':
        listeners[target.port] *= 2
    else:
        readings = iter((listeners, {}))
        monkeypatch.setattr(codex_terminal, '_listeners', lambda: next(readings))
    assert target.root.session_id not in codex_titles.title_ttys(targets, {})


def test_legacy_and_shared_collision_withdraws_both(titles):
    _, targets, _ = titles
    result = codex_titles.title_ttys(targets, {'codex:legacy': targets[0].tty})
    assert result == {targets[1].root.session_id: targets[1].tty}


def test_shared_collision_withdraws_both(titles):
    _, targets, _ = titles
    duplicate = replace(targets[0], root=targets[1].root)
    assert codex_titles.title_ttys((targets[0], duplicate), {}) == {}


def test_no_shared_targets_keeps_legacy_and_does_not_scan(monkeypatch):
    monkeypatch.setattr(codex_terminal, '_listeners', lambda: pytest.fail('unneeded scan'))
    assert codex_titles.title_ttys((), {'old': '/dev/ttys009'}) == {'old': '/dev/ttys009'}


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'expired', 'hidden', 'changed_root'])
async def test_snapshot_copies_only_fresh_current_targets(titles, monkeypatch, fault):
    from tests.test_push_tiers import _tiered_daemon, _Probe
    records, targets, _ = titles
    d, _ = _tiered_daemon(monkeypatch, _Probe())
    record, target = records[0], targets[0]
    d._codex_records = {record.session_id: record}
    d._codex_terminals = {record.session_id: target}
    d._codex_titles_observed_at = time.monotonic() - (31 if fault == 'expired' else 0)
    if fault == 'hidden':
        d._hidden_codex[record.session_id] = record.path, record.revision
    elif fault == 'changed_root':
        record.cwd = '/different'
    async def no_refresh():
        pass
    monkeypatch.setattr(d, '_refresh_codex_terminals', no_refresh)
    monkeypatch.setattr(d, '_refresh_codex_input', lambda: None)
    seen = []
    d._apply_terminal_titles = lambda snapshot, roots, shared: seen.append(shared)
    await d._push_agents_snapshot()
    assert seen == [(target,) if fault is None else ()]
