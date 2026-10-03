"""The phone's Worktrees screen: source pins, and the tick rule run.

`WorktreeRows.swift` is Foundation-only and phone-only (no Mac copy, so no
byte pin); it is compiled with `CardMerge.swift` under `swiftc` and its table
is run rather than read (`test_comm_rules.py`'s pattern). The view is pinned
by grep for the wiring only a source read can see.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import api_server

REPO = Path(__file__).resolve().parents[2]
PHONE = REPO / "ios" / "BobPhone"
PROJECT = (REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj").read_text()


def _read(name: str) -> str:
    return (PHONE / name).read_text()


def _code(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def test_the_project_compiles_the_new_files():
    for name in ("WorktreeRows.swift", "WorktreesView.swift",
                 "WorktreeRowsTests.swift"):
        assert PROJECT.count(name) >= 3, name


def test_the_menu_section_and_its_tile():
    menu = _read("MenuView.swift")
    assert "designSystem, review, worktrees" in menu
    assert 'case .worktrees: return "Worktrees"' in menu
    assert 'case .worktrees: return "arrow.triangle.branch"' in menu
    assert "worktrees: client.snapshot.board.worktreesSupported" in menu
    assert '.navigationTitle("worktrees")' in menu
    assert "WorktreesView(client: client)" in menu
    assert 'MenuNotYet(path: "~/worktrees"' in menu


def test_the_action_is_named_and_on_both_tuples():
    actions = _read("Actions.swift")
    assert 'static let boardMergeBatch = "board_merge_batch"' in actions
    assert "board_merge_batch" in api_server.ApiServer.LAN_ACTIONS
    assert "board_merge_batch" in api_server.ApiServer.REMOTE_ACTIONS


def test_the_markers_decode_tolerantly():
    models = _read("Models.swift")
    assert 'case worktreesSupported = "worktrees_supported"' in models
    assert 'case mergeBatchWritable = "merge_batch_writable"' in models
    assert "c.value(.worktreesSupported, false)" in models
    assert "c.value(.mergeBatchWritable, false)" in models


def test_the_press_is_posted_never_banked_and_armed_then_confirmed():
    view = _code(_read("WorktreesView.swift"))
    assert "client.post(" in view
    assert "action: PhoneActions.boardMergeBatch" in view
    assert "enqueue(action: PhoneActions.boardMergeBatch" not in view
    assert '"expected_tips"' in view and '"card_ids"' in view
    assert "arm.confirm(.mergeBatch" in view and "arm.arm(.mergeBatch" in view
    assert "board.mergeBatchWritable && board.mergeWritable" in view


def test_fix_is_the_card_screens_own_route():
    view = _code(_read("WorktreesView.swift"))
    assert "PhoneActions.boardMergeFix" in view
    assert "arm.confirm(.mergeFix" in view
    assert "client.enqueue(action: PhoneActions.boardMergeFix" in view
    assert "scope: live.id" in view
    assert "CardMerge.fixOffered(" in view
    assert "client.queueMark(for: live.id)" in view
    assert "client.queueNote(for: live.id)" in view


def test_the_screen_reads_only_when_looked_at_and_never_on_a_timer():
    for name in ("Client.swift",):
        text = _read(name)
        for caller in ("func poll", "func backgroundRefresh"):
            if caller in text:
                body = text[text.index(caller):text.index(caller) + 6000]
                assert "fetchWorktrees(" not in body, caller
    users = [p.name for p in PHONE.glob("*.swift")
             if "fetchWorktrees(" in p.read_text()]
    assert sorted(users) == ["Client.swift", "WorktreesView.swift"]
    client = _read("Client.swift")
    start = client.index("func fetchWorktrees() async -> WorktreesPage? {")
    body = client[start:client.index("\n    }\n", start)]
    assert 'kind: "worktrees"' in body and "!backgroundRun" in body
    assert "knowsItIsAway" in body
    view = _code(_read("WorktreesView.swift"))
    assert "Timer" not in view and ".task(id: fetchKey)" in view
    assert ".refreshable" in view


def test_the_view_clips_no_prose_and_titles_are_literal():
    view = _code(_read("WorktreesView.swift"))
    assert ".lineLimit(" not in view
    assert ".navigationTitle(" not in view
    assert "CARD · " in view and "sheets.show(.card(live))" in view
    assert "WorktreeRows.maximum" in view
    assert "[-]" in view and "Cannot be ticked" in view
    # The tick follows the row's own rule, not the view's.
    assert "WorktreeRows.tickable(" in view


def test_the_row_rule_stays_foundation_only_and_leaves_card_merge_alone():
    rows = _read("WorktreeRows.swift")
    assert "import Foundation" in rows and "SwiftUI" not in rows
    assert "CardMerge.offered(" in rows
    diff = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--",
         "ios/BobPhone/CardMerge.swift", "panel/Sources/BobPanel/CardMerge.swift"],
        cwd=REPO)
    assert diff.returncode == 0


HARNESS = r'''
struct Case: Decodable {
    let name: String
    let mergeable: Bool
    let tip: String
    let writable: Bool
    let hasCard: Bool
    let column: String
    let state: String
    let offered: Bool
    let manualDue: Bool
}
let cases = try! JSONDecoder().decode([Case].self, from: FileHandle.standardInput.readDataToEndOfFile())
var out: [String: Any] = [:]
for c in cases {
    let card: WorktreeRows.CardFacts? = c.hasCard
        ? WorktreeRows.CardFacts(column: c.column, branch: "card/x", mergeState: c.state,
                                 manualDue: c.manualDue, mergeOffered: c.offered)
        : nil
    out[c.name] = WorktreeRows.tickable(rowMergeable: c.mergeable, branchTip: c.tip,
                                        card: card, mergeWritable: c.writable)
}
out["order"] = WorktreeRows.orderedIds(ticked: ["b", "c"], listed: ["c", "a", "b"])
out["tips"] = WorktreeRows.tips(for: ["c", "b"], entries: [
    WorktreeRows.Entry(id: "a", branchTip: "ta", mergeable: true),
    WorktreeRows.Entry(id: "b", branchTip: "tb", mergeable: true),
    WorktreeRows.Entry(id: "c", branchTip: "tc", mergeable: true)])
out["verb"] = WorktreeRows.verb(count: 2)
out["armed"] = WorktreeRows.armedVerb(count: 3)
out["max"] = WorktreeRows.maximum
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
'''


def _case(name, **kw):
    base = {"name": name, "mergeable": True, "tip": "a" * 40, "writable": True,
            "hasCard": True, "column": "done", "state": "", "offered": True,
            "manualDue": False}
    base.update(kw)
    return base


def test_the_tick_rule_run_under_swiftc(tmp_path):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    path = tmp_path / "main.swift"
    path.write_text(_read("WorktreeRows.swift") + "\n"
                    + _read("CardMerge.swift") + "\n" + HARNESS)
    exe = tmp_path / "probe"
    built = subprocess.run([swiftc, str(path), "-o", str(exe)],
                           capture_output=True, text=True, timeout=240)
    assert built.returncode == 0, built.stderr
    cases = [
        _case("done_offered"),
        _case("conflict_offered", state="conflict"),
        _case("working_row", mergeable=False, column="in_progress"),
        _case("no_card_row", mergeable=False, hasCard=False),
        _case("live_merging", state="merging"),
        _case("live_queued", state="queued"),
        _case("daemon_not_offering", offered=False),
        _case("not_writable", writable=False),
        _case("no_tip", tip=""),
        _case("manual_due", manualDue=True),
    ]
    ran = subprocess.run([str(exe)], input=json.dumps(cases),
                         capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, ran.stderr
    got = json.loads(ran.stdout)
    assert got["done_offered"] is True
    assert got["conflict_offered"] is True
    for name in ("working_row", "no_card_row", "live_merging", "live_queued",
                 "daemon_not_offering", "not_writable", "no_tip", "manual_due"):
        assert got[name] is False, name
    assert got["order"] == ["c", "b"]
    assert got["tips"] == ["tc", "tb"]
    assert got["verb"] == "MERGE 2"
    assert got["armed"] == "Merge 3 into main?"
    assert got["max"] == 8


def test_the_read_is_an_unowned_task_that_task_id_cannot_cancel():
    view = _code(_read("WorktreesView.swift"))
    assert ".task(id: fetchKey) { request() }" in view
    assert "readTask = Task { @MainActor in" in view
    assert "reloadPending = true" in view
    assert "guard !Task.isCancelled else { return }" in view
    # A cancelled read is returned from before it can set `failed`.
    body = view[view.index("readTask = Task { @MainActor in"):]
    assert body.index("Task.isCancelled") < body.index("failed = rows.isEmpty")
    assert "await readTask?.value" in view


def test_the_press_reply_and_detail_line_are_conditional():
    view = _code(_read("WorktreesView.swift"))
    assert "pressReply" in view and "if showsMerge { mergeBar }" in view
    assert "!detailLine(row).isEmpty" in view
    assert "row.ahead >= 0" in view
