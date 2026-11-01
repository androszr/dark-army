"""The VS Code half of the cyber cursor: the Matrix look the extension writes
into an enrolled, trusted project, run from the built bundle under node with a
fake `vscode` (the `STRICT_HARNESS` pattern in `test_vscode_extension.py`)."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "vscode-extension" / "dist" / "extension.js"
SOURCE = ROOT / "vscode-extension" / "src" / "extension.ts"

# argv: bundle, scenario JSON. The fake configuration keeps a workspace-scope
# store and a user-scope one; `inspect()` reports each side by itself, as VS
# Code does, and `update()` writes only the scope asked for.
HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const scenario = JSON.parse(process.argv[2]);
const workspaceStore = JSON.parse(JSON.stringify(scenario.workspace || {}));
const userStore = JSON.parse(JSON.stringify(scenario.user || {}));
const writes = [];
const memento = {data: JSON.parse(JSON.stringify(scenario.memento || {})),
 get(k) {return this.data[k]}, async update(k, v) {if (v === undefined) delete this.data[k]; else this.data[k] = v}};
const config = {
 inspect(key) {return {key, globalValue: userStore[key], workspaceValue: workspaceStore[key]}},
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
 const out = {};
 if (scenario.op === 'keys') out.keys = ext.MATRIX_LOOK_KEYS;
 if (scenario.op === 'decide') {
  const enrolled = new Set(scenario.enrolled || []);
  out.decision = ext.matrixLookDecision({folders: scenario.folders, trusted: scenario.trusted !== false,
   preference: scenario.preference, enrolled: f => enrolled.has(f), applied: !!scenario.applied});
 }
 if (scenario.op === 'pref') out.pref = ext.readCyberCursorPreference() === undefined ? null : ext.readCyberCursorPreference();
 if (scenario.op === 'targets') out.targets = ext.matrixWatchTargets(scenario.folders);
 if (scenario.op === 'apply') await ext.applyMatrixLook(memento);
 if (scenario.op === 'remove') await ext.removeMatrixLook(memento);
 out.workspace = workspaceStore; out.user = userStore; out.writes = writes; out.memento = memento.data;
 console.log(JSON.stringify(out));
})().catch(e => {console.error(e); process.exitCode = 1});
'''


def _run(_home=None, **scenario):
    env = dict(os.environ, HOME=str(_home)) if _home else None
    result = subprocess.run(["node", "-e", HARNESS, str(BUNDLE), json.dumps(scenario)],
                            capture_output=True, text=True, timeout=20, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module", autouse=True)
def _bundle_is_built():
    if not BUNDLE.exists():
        pytest.skip("vscode-extension/dist/extension.js is not built (npm run build)")


def _decide(**kw):
    return _run(op="decide", **kw)["decision"]


def test_enrolled_trusted_and_on_applies():
    assert _decide(folders=["/p"], enrolled=["/p"], preference=True) == "apply"


def test_an_absent_preference_is_on():
    assert _decide(folders=["/p"], enrolled=["/p"]) == "apply"


def test_a_false_preference_removes():
    assert _decide(folders=["/p"], enrolled=["/p"], preference=False) == "remove"


def test_untrusted_does_nothing():
    assert _decide(folders=["/p"], enrolled=["/p"], preference=True, trusted=False) == "none"
    assert _decide(folders=["/p"], enrolled=["/p"], preference=False, trusted=False) == "none"


def test_not_enrolled_does_nothing():
    assert _decide(folders=["/p"], enrolled=[], preference=True) == "none"


def test_multi_root_with_one_unenrolled_folder_does_nothing():
    assert _decide(folders=["/a", "/b"], enrolled=["/a"], preference=True) == "none"
    assert _decide(folders=["/a", "/b"], enrolled=["/a", "/b"], preference=True) == "apply"


def test_an_unenrolled_folder_with_our_entries_takes_them_back():
    assert _decide(folders=["/p"], enrolled=[], preference=True, applied=True) == "remove"


def test_no_folder_does_nothing():
    assert _decide(folders=[], enrolled=[], preference=True) == "none"


def test_the_key_set_is_closed_and_has_no_theme():
    keys = _run(op="keys")["keys"]
    assert "terminalCursor.foreground" in keys["colors"]
    assert keys["settings"] == {"terminal.integrated.cursorStyle": "block",
                                "terminal.integrated.cursorBlinking": False}
    assert len(keys["colors"]) == 6
    assert "workbench.colorTheme" not in SOURCE.read_text(encoding="utf-8")
    assert "workbench.colorTheme" not in keys["colors"]


def test_apply_writes_only_the_named_entries_at_workspace_scope():
    out = _run(op="apply")
    assert set(out["workspace"]) == {"workbench.colorCustomizations",
                                     "terminal.integrated.cursorStyle",
                                     "terminal.integrated.cursorBlinking"}
    assert out["workspace"]["workbench.colorCustomizations"]["terminalCursor.foreground"] == "#50e080"
    assert out["workspace"]["terminal.integrated.cursorBlinking"] is False
    assert all(w["target"] == 2 for w in out["writes"])
    assert out["memento"]["darkArmy.matrixLook.applied"]["colors"]["terminal.ansiGreen"] == "#50e080"


def test_apply_keeps_the_persons_own_workspace_entries_and_never_copies_user_scope():
    out = _run(op="apply",
               workspace={"editor.fontSize": 14,
                          "terminal.integrated.cursorStyle": "line",
                          "workbench.colorCustomizations": {"editorCursor.foreground": "#ff0000",
                                                           "terminal.foreground": "#123456"}},
               user={"workbench.colorCustomizations": {"statusBar.background": "#000000"}})
    ws = out["workspace"]
    assert ws["editor.fontSize"] == 14
    assert ws["terminal.integrated.cursorStyle"] == "line"
    colours = ws["workbench.colorCustomizations"]
    assert colours["editorCursor.foreground"] == "#ff0000"
    assert colours["terminal.foreground"] == "#123456"
    assert "statusBar.background" not in colours
    recorded = out["memento"]["darkArmy.matrixLook.applied"]
    assert "terminal.foreground" not in recorded["colors"]
    assert "terminal.integrated.cursorStyle" not in recorded["settings"]


def test_apply_twice_writes_nothing_the_second_time():
    first = _run(op="apply")
    second = _run(op="apply", workspace=first["workspace"], memento=first["memento"])
    assert second["writes"] == []
    assert second["workspace"] == first["workspace"]


def test_remove_strips_only_entries_that_are_still_ours():
    applied = _run(op="apply", workspace={"editor.fontSize": 14})
    workspace = applied["workspace"]
    workspace["workbench.colorCustomizations"]["terminal.ansiGreen"] = "#00ff00"
    workspace["workbench.colorCustomizations"]["editorCursor.foreground"] = "#ff0000"
    workspace["terminal.integrated.cursorStyle"] = "underline"
    out = _run(op="remove", workspace=workspace, memento=applied["memento"])
    ws = out["workspace"]
    assert ws["editor.fontSize"] == 14
    assert ws["workbench.colorCustomizations"] == {"terminal.ansiGreen": "#00ff00",
                                                   "editorCursor.foreground": "#ff0000"}
    assert ws["terminal.integrated.cursorStyle"] == "underline"
    assert "terminal.integrated.cursorBlinking" not in ws
    assert "darkArmy.matrixLook.applied" not in out["memento"]


def test_remove_drops_the_colour_object_when_it_empties():
    applied = _run(op="apply")
    out = _run(op="remove", workspace=applied["workspace"], memento=applied["memento"])
    assert out["workspace"] == {}


def test_remove_with_nothing_recorded_touches_nothing():
    out = _run(op="remove", workspace={"terminal.integrated.cursorStyle": "block"})
    assert out["writes"] == []
    assert out["workspace"] == {"terminal.integrated.cursorStyle": "block"}


@pytest.mark.parametrize("stored, expected", [
    (None, None), ("not json", None), ('{"cyber_cursor": false}', False),
    ('{"cyber_cursor": true}', True), ('{"cyber_cursor": 0}', None), ('{}', None),
])
def test_the_preference_reader_treats_only_a_literal_false_as_off(tmp_path, stored, expected):
    home = tmp_path / "home"
    (home / ".dark-army").mkdir(parents=True)
    if stored is not None:
        (home / ".dark-army" / "preferences.json").write_text(stored, encoding="utf-8")
    assert _run(_home=home, op="pref")["pref"] == expected


def test_the_enrolment_folders_under_each_workspace_folder_are_watched(tmp_path):
    enrolled = tmp_path / "a"
    (enrolled / ".dark-army").mkdir(parents=True)
    legacy = tmp_path / "b"
    (legacy / ".bob-companion").mkdir(parents=True)
    plain = tmp_path / "c"
    plain.mkdir()
    out = _run(op="targets", folders=[str(enrolled), str(legacy), str(plain)])
    assert out["targets"] == [str(enrolled / ".dark-army"), str(legacy / ".bob-companion")]


def test_the_extension_rechecks_on_window_focus():
    source = SOURCE.read_text(encoding="utf-8")
    assert "onDidChangeWindowState" in source
    assert "matrixWatchTargets" in source
