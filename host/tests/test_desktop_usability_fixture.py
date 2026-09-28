"""Exercise the research seam on ephemeral ports; never launch the real panel."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import socket
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from dark_army_daemon.board import MAX_BLOCKERS, MAX_TITLE_CHARS, parse_ids

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('r24_fixture', REPO / 'tools/desktop_usability_fixture.py')
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


@pytest.fixture
def dataset():
    return fixture.load_dataset()


@pytest.fixture
def scenario(dataset):
    return fixture.Scenario(dataset)


@pytest.fixture
def server(scenario):
    server = fixture.FixtureServer(scenario, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.stopping.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_both_scenarios_are_bounded_and_referentially_complete(dataset):
    assert set(dataset['variants']) == {'A', 'B'}
    for variant in dataset['variants'].values():
        base = variant['frames']['baseline']
        rows = [r for bucket in ('running', 'waiting', 'sleeping') for r in base['agents'][bucket]]
        assert len(rows) == len({r['session_id'] for r in rows}) == 20
        assert len({r['project'] for r in rows}) == 3
        cards = {c['id']: c for c in base['board']['cards']}
        assert 80 in {len(c['title']) for c in cards.values()}
        assert 200 in {len(c['title']) for c in cards.values()}
        assert all(len(c['title']) <= MAX_TITLE_CHARS for c in cards.values())
        answer = variant['answers']
        target = cards[answer['target_card']]
        assert parse_ids(target['blocked_by']) == answer['blocker_ids']
        assert len(target['blockers']) == 2 <= MAX_BLOCKERS
        assert all(cards[b]['column_name'] != 'done' for b in answer['blocker_ids'])
        names = answer['blocker_names']
        assert names[0] != names[1] and names[0][:170] == names[1][:170]
        assert all('Żółć' in n and 'Łódź' in n for n in names)
        assert any(len(word) > 100 for c in cards.values() for word in c['title'].split())
        historic = variant['history']['7d']['']['sessions']
        assert {r['session_id'] for r in rows}.isdisjoint(h['session_id'] for h in historic)
        assert variant['session_records'][answer['history_session']]['live'] is False
        record = variant['work_records'][target['id']]['record']
        assert answer['limitation'] in record['report']
        assert variant['diffs'][target['id']][0]['path'] == record['files'][0]['path'] == answer['changed_file']
        assert variant['full_cards'][target['id']]['cards'][0]['work_record_full'] == record
        unavailable = variant['work_records'][answer['unavailable_card']]
        assert unavailable['available'] is False and unavailable['record'] is None
        assert 'unavailable' in unavailable['reason']
        assert all('work_record_full' not in c for c in cards.values())
    a, b = dataset['variants'].values()
    assert a['answers']['target_title'] != b['answers']['target_title']
    assert a['frames']['baseline']['board']['cards'][0]['id'] == a['answers']['target_card']
    assert b['frames']['baseline']['board']['cards'][-1]['id'] == b['answers']['target_card']


def test_validator_refuses_executable_capability(tmp_path, dataset):
    bad = copy.deepcopy(dataset)
    bad['variants']['A']['frames']['baseline']['agents']['running'][0]['can_stop'] = True
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(bad))
    with pytest.raises(fixture.Refusal, match='capabilities'):
        fixture.load_dataset(path)


def test_exact_reads_and_unknown_queries_fail_visibly(scenario):
    answer = scenario.data['answers']
    reads = ['/api/state', '/api/events?done=review&sections=changed',
             '/api/events?sections=changed&done=review&cards=delta', '/api/usage',
             '/api/board?column=done', '/api/board?card=' + answer['target_card'],
             '/api/work-record?card=' + answer['target_card'],
             '/api/work-record?card=' + answer['target_card'] + '&file=0',
             '/api/history?session=' + answer['history_session'],
             '/api/history?range=7d&root=' + answer['helper_root'],
             '/api/outcomes?card=' + answer['target_card'], '/api/lifecycle?root=']
    for path in reads:
        assert scenario.route(path)[0] == 200, path
    for path in ['/api/action', '/api/terminal', '/api/history?range=never',
                 '/api/board?card=real', '/api/state?unexpected=1',
                 '/api/board?card=x&card=y', '/api/work-record?card=x&file=../secret',
                 '/api/work-record?card=' + answer['target_card'] + '&file=-1',
                 'https://example.com/api/state']:
        assert scenario.route(path)[0] == 404
    assert scenario.attempts[-1]['kind'] == 'unexpected_read'


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'CONNECT', 'TRACE', 'BOGUS'])
def test_every_non_get_is_refused_before_body_dispatch(server, method):
    address = 'http://127.0.0.1:' + str(server.server_port)
    req = Request(address + '/api/action', data=b'{"action":"stop","session_id":"real"}', method=method)
    with pytest.raises(HTTPError) as error:
        urlopen(req, timeout=2)
    assert error.value.code == 403
    assert server.scenario.attempts[-1]['kind'] == 'refused_http_mutation'
    assert server.scenario.frame == 'baseline'


def test_sse_sends_full_initial_and_deterministic_frames(server):
    with urlopen('http://127.0.0.1:' + str(server.server_port) + '/api/events?sections=changed', timeout=3) as stream:
        def next_frame():
            while True:
                line = stream.readline().decode()
                if line.startswith('data: '):
                    return json.loads(line[6:])
        first = next_frame()
        assert len(first['agents']['waiting']) == 4
        server.scenario.publish('status')
        status = next_frame()
        assert status['agents']['running'][-1]['current_tool'] == 'Write'
        assert [a['session_id'] for a in first['agents']['running']] == [a['session_id'] for a in status['agents']['running']]
        server.scenario.publish('removed')
        assert len(next_frame()['agents']['waiting']) == 3


class FakeChild:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.commands = ''

    def wait(self, timeout):
        assert timeout == 5
        return 0


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / 'BobPanel'
    path.write_text('stub executable; never executed')
    path.chmod(0o700)
    return path


def test_startup_refusals_precede_spawn(tmp_path, scenario, executable):
    def never_spawn(*args, **kwargs):
        pytest.fail('refused startup reached process launch')
    kwargs = dict(home=tmp_path, attest=True, panel=executable, spawn=never_spawn, port=0, roster=lambda: [])
    with pytest.raises(fixture.Refusal, match='attestation'):
        fixture.launch(scenario, **dict(kwargs, attest=False))
    with pytest.raises(fixture.Refusal, match='component'):
        fixture.launch(scenario, **dict(kwargs, roster=lambda: ['BobPanel']))
    state = tmp_path / '.dark-army'
    state.mkdir()
    sentinel = state / 'preferences.json'
    sentinel.write_text('existing preferences')
    with pytest.raises(fixture.Refusal, match='already has files'):
        fixture.launch(scenario, **kwargs)
    assert sentinel.read_text() == 'existing preferences'
    sentinel.unlink()
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        with pytest.raises(fixture.Refusal, match='bind failed'):
            fixture.launch(scenario, **dict(kwargs, port=occupied.getsockname()[1]))
    assert list(state.iterdir()) == []


def test_symlink_state_refused(tmp_path, scenario, executable):
    outside = tmp_path / 'other'
    outside.mkdir()
    (tmp_path / '.dark-army').symlink_to(outside, target_is_directory=True)
    with pytest.raises(fixture.Refusal, match='plain directory'):
        fixture.launch(scenario, home=tmp_path, attest=True, panel=executable, port=0, roster=lambda: [])
    assert list(outside.iterdir()) == []


def test_exact_argv_pipe_context_and_owned_cleanup(tmp_path, scenario, executable):
    seen = []
    class CapturedPipe(io.StringIO):
        def close(self):
            seen.append(self.getvalue())
            super().close()
    child = FakeChild()
    child.stdin = CapturedPipe()
    def spawn(argv, **kwargs):
        assert argv == [str(executable)]
        assert 'env' not in kwargs and 'shell' not in kwargs
        assert kwargs['stdin'] == fixture.subprocess.PIPE
        assert kwargs['stdout'] == fixture.subprocess.PIPE
        state = tmp_path / '.dark-army'
        # The marker alone: the desk token rides stdin, never a file.
        assert set(p.name for p in state.iterdir()) == {fixture.MARKER}
        (state / 'panel-position.json').write_text('{}')
        (state / 'unknown-user-file').write_text('preserve')
        return child
    fixture.launch(scenario, home=tmp_path, attest=True, panel=executable,
                   port=0, roster=lambda: [], spawn=spawn, commands=['status', 'removed', 'quit'])
    commands = [json.loads(line) for line in seen[0].splitlines()]
    assert [c['action'] for c in commands] == ['context', 'show', 'quit']
    # The first context push carries the synthetic desk token.
    assert commands[0]['desk_token'].startswith('r24-synthetic-')
    assert scenario.frame == 'removed'
    state = tmp_path / '.dark-army'
    assert [p.name for p in state.iterdir()] == ['unknown-user-file']
    assert (state / 'unknown-user-file').read_text() == 'preserve'
    with pytest.raises(fixture.Refusal):
        fixture.OwnedState(tmp_path).check()


def test_cleanup_preserves_replaced_marker_and_symlink(tmp_path):
    state = fixture.OwnedState(tmp_path)
    state.create()
    marker = state.path / fixture.MARKER
    replacement = state.path / 'replacement'
    replacement.write_text('not ours')
    replacement.replace(marker)
    target = tmp_path / 'external'
    target.write_text('untouched')
    (state.path / 'panel-position.json').symlink_to(target)
    state.cleanup(panel_started=True)
    assert marker.read_text() == 'not ours'
    assert target.read_text() == 'untouched'
    assert (state.path / 'panel-position.json').is_symlink()


def test_panel_stdout_has_one_in_memory_exception_and_no_forwarding(scenario, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('fixture attempted outbound connection/process')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(fixture.subprocess, 'Popen', forbidden)
    pipe = io.StringIO()
    fixture.panel_output('{"event":"action","name":"set_panel_scale","value":175}', scenario, pipe)
    assert json.loads(pipe.getvalue()) == {'action': 'context', 'settings': {'panel_scale': 175}}
    for payload in [{'event': 'action', 'name': 'restart'}, {'event': 'action', 'name': 'set_panel_scale', 'value': 999}, {'event': 'action', 'name': 'stop', 'session_id': 'real'}]:
        fixture.panel_output(json.dumps(payload), scenario, pipe)
        assert scenario.attempts[-1]['kind'] == 'refused_panel_action'
    assert len(pipe.getvalue().splitlines()) == 1
    assert scenario.route('/api/state')[0] == 200


def test_spawn_failure_cleans_only_owned_files(tmp_path, scenario, executable):
    def failure(*args, **kwargs):
        raise OSError('stub spawn failed')
    with pytest.raises(OSError, match='stub spawn'):
        fixture.launch(scenario, home=tmp_path, attest=True, panel=executable, port=0,
                       roster=lambda: [], spawn=failure, commands=[])
    assert not (tmp_path / '.dark-army').exists()


def test_timeout_never_closes_stdout_held_by_a_live_reader(tmp_path, scenario, executable):
    class WatchedOutput(io.StringIO):
        closes = 0

        def close(self):
            self.closes += 1
            super().close()

    class UnresponsiveChild(FakeChild):
        def wait(self, timeout):
            raise fixture.subprocess.TimeoutExpired('stub panel', timeout)

    child = UnresponsiveChild()
    child.stdout = WatchedOutput()
    with pytest.raises(fixture.Refusal, match='teardown is pending'):
        fixture.launch(scenario, home=tmp_path, attest=True, panel=executable,
                       port=0, roster=lambda: [], spawn=lambda *a, **k: child,
                       commands=['quit'])
    assert child.stdin.closed
    assert child.stdout.closes == 0
    state = tmp_path / '.dark-army'
    assert (state / fixture.MARKER).exists()
    assert not (state / 'api-token').exists()
    assert scenario.attempts[-1]['kind'] == 'teardown_pending'
    # Test fixture owns this in-memory stream; there is no real child/reader.
    child.stdout.close()
