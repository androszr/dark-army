"""Review regressions at the real extension and serialized board-event seams."""
import json
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import dispatch
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.decision_capture import DecisionCapture
from dark_army_daemon.decision_store import DecisionStore


NAVIGATION = r'''
const fs = require('fs'), vm = require('vm');
const [bundle, mode] = process.argv.slice(1);
class TabInputTerminal {}
const one = {name: 'Grok', processId: Promise.resolve(100)};
const two = {name: 'Grok', processId: Promise.resolve(200)};
const panel = {name: 'zsh', processId: Promise.resolve(300)};
const terminalTab = {label: mode.includes('prefix') ? 'Grok 2' : 'Grok', input: new TabInputTerminal()};
const fileTab = {label: 'file.ts', input: {}};
let activeTab = mode.includes('split') ? fileTab : terminalTab;
let terminals = [one, two, panel];
let activeTerminal = mode.includes('leftover') ? panel : two;
if (mode === 'unique') terminals = [two, panel];
if (mode === 'longest') {
  two.name = 'Grok 2'; terminalTab.label = 'Grok 2 3';
}
if (mode === 'closed_remembered') {
  activeTab = fileTab; terminals = [panel]; activeTerminal = undefined;
}
const vscode = {TabInputTerminal, window: {terminals, activeTerminal,
  tabGroups: {activeTabGroup: {activeTab}, all: [{activeTab},
    ...(mode.includes('split') ? [{activeTab: terminalTab}] : [])]}},
  workspace: {workspaceFolders: [], name: 'fixture'}};
const ctx = {module: {exports: {}}, exports: {}, Buffer, setTimeout, clearTimeout,
  console, process, require: n => n === 'vscode' ? vscode : require(n)};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(bundle, 'utf8') +
  '\nmodule.exports.pick = pickTerminal; module.exports.remember = rememberTerminal;', ctx);
ctx.module.exports.remember(two);
(async () => {
  const terminal = ctx.module.exports.pick();
  console.log(JSON.stringify({pid: terminal ? await terminal.processId : null}));
})();
'''


def test_extension_advertises_the_packaged_version():
    directory = Path(__file__).resolve().parents[2] / "vscode-extension"
    script = r'''
const fs = require('fs'), vm = require('vm');
const vscode = {window: {tabGroups: {all: []}}, workspace: {workspaceFolders: []}};
const ctx = {module: {exports: {}}, exports: {}, Buffer, setTimeout, clearTimeout,
  console, process, require: n => n === 'vscode' ? vscode : require(n)};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') +
  '\nmodule.exports.ping = () => dispatch({op: "ping"});', ctx);
ctx.module.exports.ping().then(reply => console.log(JSON.stringify(reply)));
'''
    result = subprocess.run(["node", "-e", script, str(directory / "dist/extension.js")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    reply = json.loads(result.stdout)
    assert reply["ok"] is True
    assert reply["version"] == json.loads((directory / "package.json").read_text())["version"]


@pytest.mark.parametrize("mode,expected", [
    ("exact", None), ("exact_leftover", None),
    ("prefix", None), ("prefix_leftover", None),
    ("split_exact", None), ("split_prefix_leftover", None),
    ("unique", 200), ("longest", 200), ("closed_remembered", None),
])
def test_reverse_jump_requires_unambiguous_terminal_identity(mode, expected):
    bundle = Path(__file__).resolve().parents[2] / "vscode-extension/dist/extension.js"
    result = subprocess.run(["node", "-e", NAVIGATION, str(bundle), mode],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pid"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("refine", [False, True])
async def test_real_retry_event_resolves_failure_only_after_spawn(tmp_path, monkeypatch, refine):
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    board = BoardStore(tmp_path / "board.db")
    board.connect()
    daemon._board = board
    capture = DecisionCapture(DecisionStore(tmp_path / "decisions.db"))
    daemon._decisions = capture
    root = str(tmp_path.resolve())
    monkeypatch.setattr(daemon, "_known_project_roots", lambda: {root})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    spawn_ok = False

    async def spawn(*args, **_kw):
        return spawn_ok, "opened" if spawn_ok else "window unavailable", None

    monkeypatch.setattr(dispatch, "spawn", spawn)
    try:
        card, error = board.create(dict(title="Recover", project="test", root=root,
                                        prompt="Implement this", summary="Plan this", tool="claude"))
        assert card, error
        daemon._log_card_event(card, "card_dispatch_failed", error="window unavailable")
        first = (await capture.call("query", roots={root}))["items"][0]
        receipt = await capture.call("receipt", "phone", [first["id"]])
        if refine:
            retry = lambda: daemon.refine_card(card["id"])
        else:
            retry = lambda: daemon.dispatch_card(card["id"], allow_unplanned=True)
        ok, _ = await retry()
        assert not ok
        assert (await capture.call("query", roots={root}))["items"][0]["status"] == "open"
        spawn_ok = True
        ok, error = await retry()
        assert ok, error
        page = await capture.call("query", roots={root}, device="phone", receipt_id=receipt)
        item = page["items"][0]
        assert item["id"] == first["id"] and item["status"] == "superseded"
        assert item["question_text"] == "window unavailable"
        assert ("refinement" if refine else "implementation") in item["outcome"]
        assert "completion is not yet observed" in item["outcome"]
        assert len((await capture.call("query", roots={root}))["items"]) == 1
    finally:
        await capture.close()
        board.close()
