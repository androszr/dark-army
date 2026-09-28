#!/usr/bin/env python3
"""R24 research-only read server; never imports or forwards to the daemon.

See docs/research/r24-desktop-usability-protocol.md. `--validate` is safe on
an ordinary account. Interactive launch requires an attested disposable Mac
account and refuses existing state/components/port ownership. No HOME rewrite,
no production token, no --hidden eviction, no backend or session control.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import queue
import secrets
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[1]
DATASET = REPO / 'panel/Tests/Fixtures/r24-desktop-usability.json'
PANEL = REPO / 'panel/.build/release/BobPanel'
PORT = 19874
MARKER = 'r24-fixture.json'
# These are the real panel's local stores, not an arbitrary recursive sweep.
PANEL_FILES = frozenset({'panel.pid', 'panel-position.json', 'card-drafts.json'})
# Every component by name: a live copy of any of them must refuse the
# fixture. `pty_broker` is the module's last part, so a broker is caught
# whatever package it runs from.
COMPONENTS = ('BobPanel', 'Dark Army.app',
              'dark_army_daemon', 'dark_army_menubar',
              'dark-army-notify', 'dark-army-channel', 'pty_broker',
              'dark-army-ide')


class Refusal(RuntimeError):
    """A failed isolation or fixture contract; never a reason to use live data."""


def load_dataset(path: Path = DATASET) -> dict:
    data = json.loads(path.read_text())
    if data['metadata'].get('synthetic') is not True:
        raise Refusal('Dataset must explicitly declare synthetic provenance.')
    for variant in data['variants'].values():
        baseline = variant['frames']['baseline']
        agents = baseline['agents']
        rows = [a for bucket in ('running', 'waiting', 'sleeping') for a in agents[bucket]]
        if [len(agents[b]) for b in ('running', 'waiting', 'sleeping')] != [12, 4, 4]:
            raise Refusal('Expected 12 running, four waiting and four sleeping rows.')
        if len({r['session_id'] for r in rows}) != 20:
            raise Refusal('Expected twenty distinct live session IDs.')
        for row in rows:
            if row.get('pid') is not None or row.get('tty') or row.get('address'):
                raise Refusal('Synthetic rows may not name process or editor targets.')
            if any(value for key, value in row.items()
                   if key.startswith('can_') or key in ('channel', 'own_terminal', 'terminal_stream_supported')):
                raise Refusal('Synthetic sessions must have no action capabilities.')
        cards = {c['id']: c for c in baseline['board']['cards']}
        for card in cards.values():
            if not 1 <= len(card['title']) <= 200:
                raise Refusal('Synthetic card title is outside the legal bound.')
            blockers = card.get('blockers', [])
            if len(blockers) > 8 or any(b['id'] not in cards or b['title'] != cards[b['id']]['title'] for b in blockers):
                raise Refusal('Blocker identity does not agree with the board.')
        answer = variant['answers']
        target = cards[answer['target_card']]
        if [b['title'] for b in target['blockers']] != answer['blocker_names']:
            raise Refusal('Answer key disagrees with blocker names.')
        record = variant['work_records'][target['id']]['record']
        if answer['limitation'] not in record['report'] or record['files'][0]['path'] != answer['changed_file']:
            raise Refusal('Answer key disagrees with evidence.')
        if variant['full_cards'][target['id']]['cards'][0]['work_record_full'] != record:
            raise Refusal('Full card and fallback evidence disagree.')
    return data


class Scenario:
    def __init__(self, dataset: dict, variant: str = 'A'):
        self.data = dataset['variants'][variant]
        self.frame = 'baseline'
        self.lock = threading.Lock()
        self.listeners: set[queue.Queue] = set()
        self.attempts: list[dict] = []

    def note(self, kind: str, **fields):
        entry = {'kind': kind, **fields}
        with self.lock:
            self.attempts.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    def subscribe(self):
        listener = queue.Queue(maxsize=32)
        with self.lock:
            listener.put_nowait(self.data['frames'][self.frame])
            self.listeners.add(listener)
        return listener

    def publish(self, frame: str):
        if frame not in self.data['frames']:
            raise Refusal('Unknown frame; choose baseline, status or removed.')
        with self.lock:
            self.frame = frame
            for listener in self.listeners:
                # Never silently lose a deterministic frame. A stalled reader
                # is disconnected and must reconnect to a complete state.
                try:
                    listener.put_nowait(self.data['frames'][frame])
                except queue.Full:
                    raise Refusal('SSE reader stalled; stop this attempt and restart.')
        self.note('frame', name=frame)

    def route(self, target: str):
        parts = urlsplit(target)
        args = parse_qs(parts.query, keep_blank_values=True)
        if parts.scheme or parts.netloc or any(len(v) != 1 for v in args.values()):
            return self.unexpected(target)
        q = {k: v[0] for k, v in args.items()}
        path = parts.path
        allowed = {'done': 'review', 'sections': 'changed', 'cards': 'delta'}
        if path in ('/api/state', '/api/events') and set(q) <= set(allowed):
            if any(q[k] != allowed[k] for k in q):
                return self.unexpected(target)
            with self.lock:
                return 200, self.data['frames'][self.frame]
        if path == '/api/usage' and not q:
            return 200, self.data['usage']
        if path == '/api/board':
            if q == {'column': 'done'}:
                return 200, self.data['done']
            if set(q) == {'card'} and q['card'] in self.data['full_cards']:
                return 200, self.data['full_cards'][q['card']]
        if path == '/api/work-record' and set(q) in ({'card'}, {'card', 'file'}):
            if q['card'] in self.data['work_records']:
                if 'file' not in q:
                    return 200, self.data['work_records'][q['card']]
                diffs = self.data['diffs'].get(q['card'], [])
                if q['file'].isdigit() and int(q['file']) < len(diffs):
                    return 200, diffs[int(q['file'])]
        if path == '/api/history':
            if set(q) == {'session'} and q['session'] in self.data['session_records']:
                return 200, self.data['session_records'][q['session']]
            if set(q) in ({'range'}, {'range', 'root'}):
                ranges = self.data['history'].get(q['range'], {})
                if q.get('root', '') in ranges:
                    return 200, ranges[q.get('root', '')]
        if path in ('/api/outcomes', '/api/lifecycle') and set(q) <= {
            'card', 'root', 'from', 'to', 'sort', 'offset', 'limit', 'generation'
        }:
            return 200, {'available': False, 'supported': False,
                         'reason': 'This optional report is unavailable in the synthetic study.'}
        return self.unexpected(target)

    def unexpected(self, target):
        self.note('unexpected_read', path=target)
        return 404, {'available': False, 'detail': 'Unexpected research read; stop and record a fixture fault.'}


class FixtureServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, scenario: Scenario, port: int = PORT):
        self.scenario = scenario
        self.stopping = threading.Event()
        super().__init__(('127.0.0.1', port), FixtureHandler)


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def parse_request(self):
        if not super().parse_request():
            return False
        # Every non-GET method is refused BEFORE dispatch, including unknown
        # verbs. Never read or interpret a mutation body, and close the socket.
        if self.command != 'GET':
            self.server.scenario.note('refused_http_mutation', method=self.command, path=self.path)
            self.respond(403, {'ok': False, 'detail': 'Research fixture refuses all mutations.'})
            self.close_connection = True
            return False
        return True

    def respond(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        status, body = self.server.scenario.route(self.path)
        if status != 200 or urlsplit(self.path).path != '/api/events':
            self.respond(status, body)
            return
        listener = self.server.scenario.subscribe()
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            while not self.server.stopping.is_set():
                try:
                    frame = listener.get(timeout=1)
                    payload = 'data: ' + json.dumps(frame, ensure_ascii=False) + '\n\n'
                except queue.Empty:
                    payload = ': research heartbeat\n\n'
                self.wfile.write(payload.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.server.scenario.lock:
                self.server.scenario.listeners.discard(listener)


def running_components() -> list[str]:
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,command='], capture_output=True,
                            text=True, check=True, timeout=5)
    return [line.strip() for line in result.stdout.splitlines()
            if any(component in line for component in COMPONENTS)]


class OwnedState:
    """An initially empty directory; unknown files/symlinks are never cleaned."""
    def __init__(self, home: Path):
        self.path = home / '.dark-army'
        self.created_dir = False
        self.identities: dict[str, tuple[int, int]] = {}

    def check(self):
        if self.path.is_symlink() or (self.path.exists() and (not self.path.is_dir() or any(self.path.iterdir()))):
            raise Refusal('State directory already has files or is not a plain directory. Use a fresh disposable account.')

    def create(self):
        self.check()
        if not self.path.exists():
            self.path.mkdir(mode=0o700)
            self.created_dir = True
        # The marker alone: the panel's desk token rides the first context
        # push on stdin (`launch`), never a file, exactly as the app hands it.
        for name, text in ((MARKER, json.dumps({'synthetic': True, 'pid': os.getpid()})),):
            fd = os.open(self.path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
            info = (self.path / name).lstat()
            self.identities[name] = (info.st_dev, info.st_ino)

    def cleanup(self, panel_started: bool):
        if not self.path.exists() or self.path.is_symlink():
            return
        for name in self.identities.keys() | (PANEL_FILES if panel_started else set()):
            file = self.path / name
            try:
                info = file.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                continue
            identity = self.identities.get(name)
            if identity and identity != (info.st_dev, info.st_ino):
                continue
            file.unlink()
        if self.created_dir and not any(self.path.iterdir()):
            self.path.rmdir()


def panel_command(pipe, action, **values):
    pipe.write(json.dumps({'action': action, **values}) + '\n')
    pipe.flush()


def panel_output(line: str, scenario: Scenario, pipe):
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        scenario.note('panel_output_invalid', line=line.rstrip()[:500])
        return
    if (isinstance(payload, dict) and payload.get('event') == 'action'
            and payload.get('name') == 'set_panel_scale'
            and type(payload.get('value')) is int and payload['value'] in (100, 125, 150, 175)
            and set(payload) == {'event', 'name', 'value'}):
        panel_command(pipe, 'context', settings={'panel_scale': payload['value']})
        scenario.note('panel_scale', value=payload['value'])
        return
    scenario.note('refused_panel_action', payload=payload)


def launch(scenario: Scenario, *, home: Path, attest: bool, panel: Path = PANEL,
           port: int = PORT, roster=running_components, spawn=subprocess.Popen,
           commands=None):
    """Dependencies are injectable for hermetic tests; CLI exposes no bypass."""
    if not attest:
        raise Refusal('Interactive launch requires --disposable-account attestation; use --validate here.')
    state = OwnedState(home)
    state.check()
    if roster():
        raise Refusal('An existing Dark Army component was detected. No processes were changed.')
    if not panel.is_file() or not os.access(panel, os.X_OK):
        raise Refusal('Build the source panel first with swift build -c release.')
    try:
        server = FixtureServer(scenario, port)
    except OSError as error:
        raise Refusal('Exclusive loopback bind failed; no panel was launched.') from error
    child = None
    thread = None
    try:
        state.create()
        # Recheck immediately before spawn; no --hidden, so the real launch
        # claims its lock without the incumbent-eviction branch.
        if roster():
            raise Refusal('A Dark Army component appeared during startup.')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        child = spawn([str(panel)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      text=True, bufsize=1, cwd=str(REPO))
        panel_command(child.stdin, 'context', build='R24 synthetic research',
                      desk_token='r24-synthetic-' + secrets.token_urlsafe(32),
                      settings={'panel_scale': 100})
        panel_command(child.stdin, 'show')

        def read_output():
            for line in child.stdout:
                panel_output(line, scenario, child.stdin)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        print('Synthetic study: baseline | status | removed | quit. All writes are refused.', flush=True)
        for command in commands if commands is not None else sys.stdin:
            command = command.strip()
            if command == 'quit':
                break
            if command:
                scenario.publish(command)
    finally:
        stopped = child is None
        if child is not None:
            try:
                panel_command(child.stdin, 'quit')
            except (BrokenPipeError, OSError):
                pass
            child.stdin.close()
            try:
                child.wait(timeout=5)
                stopped = True
            except subprocess.TimeoutExpired:
                scenario.note('teardown_pending', reason='Panel did not exit; state retained. No process eviction attempted.')
            if stopped:
                child.stdout.close()
            # A live child's reader may own BufferedReader's lock while it
            # waits for a newline. Closing it here would block past our five
            # second timeout. On the refused path the CLI exits, which closes
            # the descriptor and lets the panel's parent watchdog take over.
        server.stopping.set()
        if thread is not None:
            server.shutdown()
            thread.join(timeout=2)
        server.server_close()
        if stopped:
            state.cleanup(panel_started=child is not None)
        if not stopped:
            raise Refusal('Panel teardown is pending. Leave this disposable account unused until its panel exits.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate', action='store_true', help='Validate data without a socket, files or launch.')
    parser.add_argument('--variant', choices=('A', 'B'), default='A')
    parser.add_argument('--disposable-account', action='store_true',
                        help='Attest this is a disposable macOS test account with no real work.')
    args = parser.parse_args()
    try:
        dataset = load_dataset()
        if args.validate:
            print('R24 A/B synthetic data validated; no interactive observation performed.')
            return 0
        # The OS account owns isolation. Never accept an environment redirect.
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if Path.home() != home:
            raise Refusal('Environment home differs from the OS account; no home redirection is allowed.')
        launch(Scenario(dataset, args.variant), home=home, attest=args.disposable_account)
        return 0
    except (Refusal, OSError, ValueError, KeyError) as error:
        print('R24 refused: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
