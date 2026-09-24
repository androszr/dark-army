# host/tests/test_vscode_extension.py
"""Picking and comparing the bundled VS Code extension .vsix.

The install decision is a version comparison between a file on disk and whatever
`code --list-extensions` reports, and `install_extension` passes `--force` — so
choosing the wrong file does not fail loudly, it silently downgrades the user.
"""

from dark_army_menubar import vscode_extension as vx


def _touch(directory, name, mtime):
    p = directory / name
    p.write_bytes(b"PK\x03\x04")
    import os
    os.utime(p, (mtime, mtime))
    return p


def test_best_vsix_picks_the_highest_version_not_the_newest_file(tmp_path):
    """The ordering that matters: git sets checkout mtimes in arbitrary order.

    A clone can land 0.1.1 with the newer mtime while 0.2.0 sits beside it; an
    mtime pick would then hand a 0.2.0 user the older file and --force them back.
    """
    _touch(tmp_path, "dark-army-ide-0.2.0.vsix", 1_000)      # older mtime
    _touch(tmp_path, "dark-army-ide-0.1.1.vsix", 9_000)      # newer mtime
    assert vx._best_vsix(tmp_path).name == "dark-army-ide-0.2.0.vsix"


def test_best_vsix_orders_numerically_not_lexically(tmp_path):
    _touch(tmp_path, "dark-army-ide-0.9.0.vsix", 1_000)
    _touch(tmp_path, "dark-army-ide-0.10.0.vsix", 1_000)
    assert vx._best_vsix(tmp_path).name == "dark-army-ide-0.10.0.vsix"


def test_best_vsix_ignores_unparseable_names(tmp_path):
    _touch(tmp_path, "dark-army-ide-dev.vsix", 5_000)
    _touch(tmp_path, "dark-army-ide-0.1.1.vsix", 1_000)
    assert vx._best_vsix(tmp_path).name == "dark-army-ide-0.1.1.vsix"


def test_best_vsix_on_an_empty_or_missing_directory(tmp_path):
    assert vx._best_vsix(tmp_path) is None
    assert vx._best_vsix(tmp_path / "nope") is None


def test_parse_version_orders_releases():
    assert vx._parse_version("0.2.0") > vx._parse_version("0.1.1")
    assert vx._parse_version("0.10.0") > vx._parse_version("0.9.0")
    # A non-numeric segment sorts below everything rather than raising.
    assert vx._parse_version("0.1.2-pre") == (0,)


# Execute the actual esbuild bundle, with VS Code/process boundaries mocked.
# The probe callback can replace a terminal during any asynchronous await.
STRICT_HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const mode = process.argv[2], requestedOp = process.argv[3], requestedText = process.argv[4];
const disposed = [], shown = [], typed = [];
let probes = 0, shell = 600, removed = false;
const one = {name: 'plan', get processId() {return Promise.resolve(shell)},
 dispose() {disposed.push('plan')}, show() {shown.push('plan')},
 sendText(t) {typed.push(t)}};
const sibling = {name: 'sibling', processId: Promise.resolve(800),
 dispose() {disposed.push('sibling')}};
let terminals = [one, sibling];
if (mode === 'duplicate') terminals.push({...one});
// Trusted unless the test says otherwise: real VS Code (engines ^1.90)
// always defines `isTrusted`, and the bridge fails closed without it.
const vscode = {window: {get terminals() {return terminals}, tabGroups: {all: []}},
 workspace: {workspaceFolders: [], name: 'fixture', isTrusted: process.env.DA_TRUSTED !== '0'}};
const childProcess = {execFile(cmd, args, opts, callback) {
 probes++;
 const pid = Number(args[args.length - 1]);
 let result = args.includes('ppid=') ? ({701: 600, 600: 1, 800: 1}[pid] || 1) : 'ttys040';
 if (mode === 'tty_only' && args.includes('ppid=')) result = 1;
 if (mode === 'wrong_tty' && args.includes('tty=')) result = 'ttys999';
 if (mode === 'ancestry_changed' && probes > 3 && args.includes('ppid=')) result = 1;
 if (mode === 'remove' && probes === 2) {terminals = [sibling]; removed = true}
 if (mode === 'replace' && probes === 2) {terminals = [{...one}, sibling]; removed = true}
 if (mode === 'shell_changed' && probes === 2) shell = 999;
 if (mode === 'new_duplicate' && probes === 2) terminals.push({...one});
 queueMicrotask(() => callback(mode === 'denied' ? new Error('denied') : null, String(result)));
}};
const context = {module: {exports: {}}, exports: {}, Buffer, setTimeout, clearTimeout,
 console, process, require: name => name === 'vscode' ? vscode :
 name === 'child_process' ? childProcess : require(name)};
if (mode === 'deadline') context.Date = {now: () => probes > 1 ? 10000 : 0};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + '\nmodule.exports.run = dispatch;', context);
(async () => {
 let op = 'close_refinement_terminal', pid = 701, tty = '/dev/ttys040';
 if (mode === 'invalid_pid') pid = '701';
 if (mode === 'empty_tty') tty = '';
 if (mode === 'legacy_close') op = 'close_terminal';
 if (mode === 'legacy_reveal') op = 'reveal_terminal';
 if (mode === 'legacy_send') op = 'send_text';
 if (mode === 'tty_only' && !requestedOp) {
   const refused = await context.module.exports.run({op, pid, tty});
   const legacy = await context.module.exports.run({op: 'close_terminal', pid, tty});
   console.log(JSON.stringify({reply: refused, legacy, disposed, shown, typed, probes})); return;
 }
 if (requestedOp) op = requestedOp;
 const reply = await context.module.exports.run({op, pid, tty, text: requestedText === undefined ? 'hello' : requestedText, expires_at_ms: Date.now() + 2000});
 console.log(JSON.stringify({reply, disposed, shown, typed, probes}));
})().catch(e => {console.error(e); process.exitCode = 1});
'''


def _strict_handler(mode, op="", text="hello", trusted=True):
    import json
    import os
    import subprocess
    from pathlib import Path
    bundle = Path(__file__).resolve().parents[2] / 'vscode-extension/dist/extension.js'
    env = dict(os.environ, DA_TRUSTED='1' if trusted else '0')
    result = subprocess.run(['node', '-e', STRICT_HARNESS, str(bundle), mode, op, text],
                            capture_output=True, text=True, timeout=10, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_strict_extension_disposes_exactly_one_owned_terminal():
    result = _strict_handler('success')
    assert result['reply']['matched'] is True and result['reply']['closed'] is True
    assert result['disposed'] == ['plan']
    assert result['shown'] == result['typed'] == []
    assert result['probes'] >= 6


import pytest


@pytest.mark.parametrize('fault', ['wrong_tty', 'duplicate', 'remove', 'replace',
                                  'shell_changed', 'new_duplicate', 'ancestry_changed',
                                  'denied', 'deadline', 'invalid_pid', 'empty_tty'])
def test_strict_extension_refuses_ambiguous_or_changed_terminal(fault):
    result = _strict_handler(fault)
    assert result['reply']['closed'] is False
    assert result['disposed'] == result['shown'] == result['typed'] == []


def test_strict_extension_has_no_tty_fallback_but_legacy_keeps_it():
    result = _strict_handler('tty_only')
    assert result['reply'] == {'matched': False, 'closed': False,
                               'reason': 'no owning terminal'}
    assert result['legacy']['closed'] is True
    assert result['disposed'] == ['plan']


@pytest.mark.parametrize('mode,field', [('legacy_close', 'disposed'),
                                       ('legacy_reveal', 'shown'), ('legacy_send', 'typed')])
def test_strict_extension_preserves_legacy_dispatch(mode, field):
    result = _strict_handler(mode)
    assert result['reply']['matched'] is True
    assert result[field] == (['hello'] if field == 'typed' else ['plan'])


# Workspace Trust: in a folder VS Code has not trusted, the bridge still
# activates (`untrustedWorkspaces: limited`) but refuses the three verbs that
# act on the workspace, in words, before touching any terminal. The reply is
# an `error`, never a throw: `dispatch.spawn` puts it on the card verbatim.
@pytest.mark.parametrize('op,refusal', [
    ('spawn_agent', {'spawned': False}),
    ('send_text', {'matched': False, 'sent': False}),
    ('reply_native_terminal', {'matched': False, 'sent': False}),
])
def test_an_untrusted_folder_refuses_starting_and_typing_in_words(op, refusal):
    result = _strict_handler('success', op, 'hello', trusted=False)
    error = result['reply'].pop('error')
    assert result['reply'] == refusal
    assert 'not trusted' in error and 'Trust' in error
    assert result['typed'] == result['shown'] == result['disposed'] == []
    if op != 'send_text':
        assert result['probes'] == 0


def test_an_untrusted_bystander_window_stays_silent_on_send_text():
    """`send_text` fans out to every window and the daemon surfaces an
    unmatched reply's `error`, so only the window owning the terminal may
    say "not trusted" — the check comes after the match."""
    result = _strict_handler('denied', 'send_text', 'hello', trusted=False)
    assert result['reply'] == {'matched': False, 'sent': False}
    assert result['typed'] == []


def test_the_bridge_runs_ps_by_absolute_path():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / 'vscode-extension/src/extension.ts').read_text()
    assert "execFile('/bin/ps'" in src
    assert "execFile('ps'" not in src


@pytest.mark.parametrize('mode,field', [('legacy_close', 'disposed'),
                                       ('legacy_reveal', 'shown')])
def test_an_untrusted_folder_still_finds_shows_and_closes_terminals(mode, field):
    result = _strict_handler(mode, trusted=False)
    assert result['reply']['matched'] is True
    assert result[field] == ['plan']
    assert result['typed'] == []


def test_the_manifest_declares_limited_untrusted_support():
    import json
    from pathlib import Path
    manifest = json.loads((Path(__file__).resolve().parents[2]
                           / 'vscode-extension/package.json').read_text())
    declared = manifest['capabilities']['untrustedWorkspaces']
    assert declared['supported'] == 'limited'
    assert 'trust' in declared['description']


# The reverse jump has to name the terminal the user is *looking at*. With
# terminals in the editor area, that is a TabInputTerminal tab, not
# window.activeTerminal (which stays on a leftover panel terminal).
PICK_HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const mode = process.argv[2];
class TabInputTerminal {}
class TabInputText { constructor(uri) { this.uri = uri; } }
const grok = {name: 'Grok', processId: Promise.resolve(62575)};
const claude = {name: 'Claude', processId: Promise.resolve(62697)};
const leftover = {name: 'zsh', processId: Promise.resolve(100)};
const grok2 = {name: 'Grok 2', processId: Promise.resolve(700)};
let terminals = [grok, claude, leftover];
let active = leftover;
let last = undefined;
let activeTab = {label: 'Grok', input: new TabInputTerminal()};
let otherTab = null;
const vscode = {
  TabInputTerminal, TabInputText,
  window: {
    get terminals() { return terminals; },
    get activeTerminal() { return active; },
    tabGroups: {
      get activeTabGroup() { return { activeTab }; },
      get all() {
        const groups = [{ activeTab }];
        if (otherTab) groups.push({ activeTab: otherTab });
        return groups;
      }
    }
  },
  workspace: { workspaceFolders: [], name: 'fixture' }
};
const context = {module: {exports: {}}, exports: {}, Buffer, setTimeout, clearTimeout,
 console, process, require: name => name === 'vscode' ? vscode : require(name)};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8')
  + '\nmodule.exports.pick = pickTerminal;'
  + '\nmodule.exports.remember = rememberTerminal;', context);
if (mode === 'file_plus_split') {
  activeTab = {label: 'foo.ts', input: new TabInputText('file://foo.ts')};
  otherTab = {label: 'Grok', input: new TabInputTerminal()};
  active = leftover;
}
if (mode === 'numbered') {
  terminals = [grok, grok2];
  activeTab = {label: 'Grok 2', input: new TabInputTerminal()};
}
if (mode === 'active_fallback') {
  activeTab = {label: 'foo.ts', input: new TabInputText('file://foo.ts')};
  otherTab = null;
  active = grok;
}
if (mode === 'remembered_fallback') {
  activeTab = {label: 'foo.ts', input: new TabInputText('file://foo.ts')};
  otherTab = null;
  active = undefined;
  context.module.exports.remember(claude);
}
if (mode === 'none') {
  activeTab = {label: 'foo.ts', input: new TabInputText('file://foo.ts')};
  otherTab = null;
  active = undefined;
  terminals = [];
}
const picked = context.module.exports.pick();
console.log(JSON.stringify({name: picked ? picked.name : null}));
'''


def _pick(mode):
    import json
    import subprocess
    from pathlib import Path
    bundle = Path(__file__).resolve().parents[2] / 'vscode-extension/dist/extension.js'
    result = subprocess.run(['node', '-e', PICK_HARNESS, str(bundle), mode],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_pick_terminal_prefers_the_focused_editor_tab_over_a_leftover_panel():
    """The bug: activeTerminal is still zsh, the tab on screen is Grok."""
    assert _pick('tab')['name'] == 'Grok'


def test_pick_terminal_uses_the_visible_split_when_a_file_has_focus():
    assert _pick('file_plus_split')['name'] == 'Grok'


def test_pick_terminal_numbered_duplicate_does_not_steal_the_shorter_name():
    assert _pick('numbered')['name'] == 'Grok 2'


def test_pick_terminal_falls_back_to_activeTerminal_without_an_editor_terminal():
    assert _pick('active_fallback')['name'] == 'Grok'


def test_pick_terminal_falls_back_to_the_last_remembered_terminal():
    assert _pick('remembered_fallback')['name'] == 'Claude'


def test_pick_terminal_returns_nothing_when_there_is_no_terminal():
    assert _pick('none')['name'] is None


def test_native_reply_is_one_clear_and_send_without_show_or_disposal():
    result = _strict_handler('success', 'reply_native_terminal', 'Continue please')
    assert result['reply']['matched'] is True and result['reply']['sent'] is True
    assert result['typed'] == ['\x15Continue please']
    assert result['disposed'] == result['shown'] == []
    assert result['probes'] >= 6


@pytest.mark.parametrize('fault', ['wrong_tty', 'duplicate', 'remove', 'replace', 'shell_changed',
    'new_duplicate', 'ancestry_changed', 'denied', 'deadline', 'invalid_pid', 'empty_tty', 'tty_only'])
def test_native_reply_rechecks_ownership_without_legacy_fallback(fault):
    result = _strict_handler(fault, 'reply_native_terminal')
    assert result['reply']['sent'] is False
    assert result['typed'] == result['disposed'] == result['shown'] == []


@pytest.mark.parametrize('text', ['', '  ', '\nhello', 'hello\r', 'a\nb', '\x1bhi', 'hi\x7f', '\thi', '\u0085hi', 'a\u2028b', ' /clear', '\u00a0!shell', '# comment', 'x' * 2001])
def test_native_reply_rejects_non_plain_input_before_any_probe(text):
    result = _strict_handler('success', 'reply_native_terminal', text)
    assert result['typed'] == result['disposed'] == result['shown'] == []
    assert result['probes'] == 0


LIFETIME_HARNESS = r'''
const fs = require('fs'), vm = require('vm'), {EventEmitter} = require('events');
const mode = process.argv[2], typed = [];
const terminal = {name: 'fixture', processId: Promise.resolve(600), sendText(text) {typed.push(text)}};
const vscode = {window: {terminals: [terminal]}, workspace: {workspaceFolders: [], isTrusted: true}};
const childProcess = {execFile(cmd, args, opts, callback) {
 const pid = Number(args[args.length - 1]);
 const result = args.includes('ppid=') ? ({701:600,600:1}[pid] || 1) : 'ttys040';
 setTimeout(() => callback(null, String(result)), 30);
}};
const context = {module: {exports:{}}, exports:{}, Buffer, setTimeout, clearTimeout, console, process,
 require: name => name === 'vscode' ? vscode : name === 'child_process' ? childProcess : require(name)};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + '\nmodule.exports.handle=handleRequest;', context);
const req = new EventEmitter(), res = new EventEmitter();
req.headers = {host:'127.0.0.1', 'x-bob-companion-authorization':'fixture'};
req.method='POST'; req.socket={destroyed:false}; req.aborted=false;
res.destroyed=false; res.writeHead=()=>{}; res.end=()=>{};
context.module.exports.handle(req,res,'fixture');
const expires = mode === 'expired' ? Date.now()-1 : Date.now()+(mode === 'deadline' ? 40 : 2000);
const body={op:'reply_native_terminal',pid:701,tty:'/dev/ttys040',text:'fixture only',expires_at_ms:expires};
if(mode==='missing_deadline') delete body.expires_at_ms;
req.emit('data',JSON.stringify(body)); req.emit('end');
if(mode==='disconnect') setTimeout(()=>{req.socket.destroyed=true;res.destroyed=true;req.aborted=true;res.emit('close');req.emit('aborted')},20);
if(mode==='normal_request_close') req.emit('close');
setTimeout(()=>console.log(JSON.stringify({typed})),350);
'''


@pytest.mark.parametrize('mode', ['disconnect', 'deadline', 'expired', 'missing_deadline', 'normal_request_close'])
def test_native_reply_handler_cancels_disconnected_or_expired_requests(mode):
    import json
    import subprocess
    from pathlib import Path
    bundle = Path(__file__).resolve().parents[2] / 'vscode-extension/dist/extension.js'
    result = subprocess.run(['node', '-e', LIFETIME_HARNESS, str(bundle), mode], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['typed'] == (['\x15fixture only'] if mode == 'normal_request_close' else [])
