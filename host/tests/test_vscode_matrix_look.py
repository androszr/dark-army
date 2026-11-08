"""The green Matrix look, taken back: from 0.1.24 the extension writes no VS
Code colours or cursor settings and only removes what 0.1.23 recorded writing.
Run from the built bundle under node with a fake `vscode` (the
`STRICT_HARNESS` pattern in `test_vscode_extension.py`)."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "vscode-extension" / "dist" / "extension.js"
SOURCE = ROOT / "vscode-extension" / "src" / "extension.ts"

# What 0.1.23 wrote and recorded in `workspaceState`, verbatim.
OLD_COLOURS = {
    "terminalCursor.foreground": "#50e080",
    "terminalCursor.background": "#0b1a10",
    "terminal.foreground": "#b8f0c8",
    "terminal.ansiGreen": "#50e080",
    "terminal.ansiBrightGreen": "#7dffa6",
    "terminal.selectionBackground": "#1f5a3a80",
}
OLD_SETTINGS = {"terminal.integrated.cursorStyle": "block",
                "terminal.integrated.cursorBlinking": False}
RECORDED = {"darkArmy.matrixLook.applied": {"colors": OLD_COLOURS, "settings": OLD_SETTINGS}}


def _old_workspace(**extra):
    ws = {"workbench.colorCustomizations": dict(OLD_COLOURS), **OLD_SETTINGS}
    ws.update(extra)
    return ws


# argv: bundle, scenario JSON. The fake configuration keeps a workspace-scope
# store and a user-scope one; `inspect()` reports each side by itself, as VS
# Code does, and `update()` writes only the workspace scope.
HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const scenario = JSON.parse(process.argv[2]);
const workspaceStore = JSON.parse(JSON.stringify(scenario.workspace || {}));
const writes = [];
const memento = {data: JSON.parse(JSON.stringify(scenario.memento || {})),
 get(k) {return this.data[k]}, async update(k, v) {if (v === undefined) delete this.data[k]; else this.data[k] = v}};
const config = {
 inspect(key) {return {key, workspaceValue: workspaceStore[key]}},
 async update(key, value, target) {
  writes.push({key, value, target});
  if (value === undefined) delete workspaceStore[key]; else workspaceStore[key] = value;
 }};
const vscode = {
 ConfigurationTarget: {Global: 1, Workspace: 2, WorkspaceFolder: 3},
 window: {tabGroups: {all: []}, terminals: []},
 workspace: {workspaceFolders: [], isTrusted: scenario.trusted !== false,
  getConfiguration() {return config}}};
const context = {module: {exports: {}}, exports: {}, Buffer, setTimeout, clearTimeout,
 console, process, require: name => name === 'vscode' ? vscode : require(name)};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
const ext = context.module.exports;
(async () => {
 const out = {exports: Object.keys(ext)};
 if (scenario.op === 'decide') out.decision = ext.matrixLookDecision(
   {trusted: scenario.trusted !== false, applied: !!scenario.applied});
 if (scenario.op === 'remove') await ext.removeMatrixLook(memento);
 if (scenario.op === 'reconcile') await ext.reconcileMatrixLook(memento);
 out.workspace = workspaceStore; out.writes = writes; out.memento = memento.data;
 console.log(JSON.stringify(out));
})().catch(e => {console.error(e); process.exitCode = 1});
'''


def _run(**scenario):
    result = subprocess.run(["node", "-e", HARNESS, str(BUNDLE), json.dumps(scenario)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module", autouse=True)
def _bundle_is_built():
    if not BUNDLE.exists():
        pytest.skip("vscode-extension/dist/extension.js is not built (npm run build)")


def test_the_extension_never_writes_a_look():
    out = _run(op="decide")
    assert out["decision"] == "none"
    assert "applyMatrixLook" not in out["exports"]
    assert "MATRIX_LOOK_KEYS" not in out["exports"]
    source = SOURCE.read_text(encoding="utf-8")
    for colour in OLD_COLOURS.values():
        assert colour not in source, f"{colour} is still in the extension"


def test_a_window_with_old_entries_takes_them_back():
    assert _run(op="decide", applied=True)["decision"] == "remove"


def test_an_untrusted_window_is_left_alone():
    assert _run(op="decide", applied=True, trusted=False)["decision"] == "none"
    out = _run(op="reconcile", trusted=False, workspace=_old_workspace(), memento=RECORDED)
    assert out["writes"] == []


def test_reconcile_removes_the_old_look_and_forgets_it():
    out = _run(op="reconcile", workspace=_old_workspace(), memento=RECORDED)
    assert out["workspace"] == {}
    assert all(w["target"] == 2 for w in out["writes"])
    assert "darkArmy.matrixLook.applied" not in out["memento"]


def test_reconcile_with_nothing_recorded_touches_nothing():
    out = _run(op="reconcile", workspace=_old_workspace())
    assert out["writes"] == []
    assert out["workspace"] == _old_workspace()


def test_remove_strips_only_entries_that_are_still_ours():
    workspace = _old_workspace(**{"editor.fontSize": 14})
    workspace["workbench.colorCustomizations"]["terminal.ansiGreen"] = "#00ff00"
    workspace["workbench.colorCustomizations"]["editorCursor.foreground"] = "#ff0000"
    workspace["terminal.integrated.cursorStyle"] = "underline"
    out = _run(op="remove", workspace=workspace, memento=RECORDED)
    ws = out["workspace"]
    assert ws["editor.fontSize"] == 14
    assert ws["workbench.colorCustomizations"] == {"terminal.ansiGreen": "#00ff00",
                                                   "editorCursor.foreground": "#ff0000"}
    assert ws["terminal.integrated.cursorStyle"] == "underline"
    assert "terminal.integrated.cursorBlinking" not in ws
    assert "darkArmy.matrixLook.applied" not in out["memento"]


def test_remove_keeps_entries_the_person_set_before_the_look():
    """0.1.23 never recorded a key the person had already set, so it stays."""
    workspace = _old_workspace()
    memento = {"darkArmy.matrixLook.applied": {
        "colors": {k: v for k, v in OLD_COLOURS.items() if k != "terminal.foreground"},
        "settings": OLD_SETTINGS}}
    out = _run(op="remove", workspace=workspace, memento=memento)
    assert out["workspace"] == {"workbench.colorCustomizations": {"terminal.foreground": "#b8f0c8"}}
