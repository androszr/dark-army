"""The Changes read: a Done card's branch against the main line.

Seams: `test_manual_check_api.py`'s — a real `BobDaemon` and `BoardStore` on a
temp file, `enrollment` patched to the temp project, the loopback GET driven
through `_handle_client`, the sealed kind through `_sealed_run` on both doors
— over a real git repository under `tmp_path`, so the commits, the counts and
the per-file diff are git's own (`docs/transport-contract.md`, *card_changes
is a sealed read*).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from urllib.parse import quote

import pytest

from dark_army_daemon import (command_receipts, devices, enrollment, merges,
                              paths, relay, work_record)
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


def _git(root, *args) -> str:
    done = subprocess.run(["git", "-C", str(root), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=merges.merge_env())
    assert done.returncode == 0, done.stderr
    return done.stdout.decode()


class _Writer:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)
    devices.reset()
    relay.reset()
    yield
    devices.reset()
    relay.reset()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("one\n")
    (root / "b.txt").write_text("keep\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    real = os.path.realpath(root)
    members = {real}
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(members))
    monkeypatch.setattr(
        enrollment, "root_enrolled",
        lambda cwd: os.path.realpath(cwd) if os.path.realpath(
            str(cwd or "")) in members else "")
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    api = ApiServer(daemon, port=0)
    api.token = "token"
    api._receipts = command_receipts.CommandReceipts()
    yield api, daemon, store, real
    store.close()


def _card_with_branch(store, root, *, commits=2, title="Add the thing",
                      column="done", record=True):
    """A Done card whose branch holds `commits` real commits, its folder
    already released (the Changes read works from the root)."""
    card, detail = store.create({"title": title, "project": "proj",
                                 "root": root, "column_name": column})
    assert card is not None, detail
    branch = f"card/{card['id'][:8]}-add-the-thing"
    wt = os.path.join(root, ".worktrees", "tmp-" + card["id"][:8])
    _git(root, "branch", branch)
    if commits:
        _git(root, "worktree", "add", "-q", wt, branch)
        with open(os.path.join(wt, "a.txt"), "w") as handle:
            handle.write("one\ntwo\n")
        _git(wt, "add", ".")
        _git(wt, "commit", "-q", "-m", "edit a")
        if commits > 1:
            with open(os.path.join(wt, "new.txt"), "w") as handle:
                handle.write("n\n")
            with open(os.path.join(wt, "b.txt"), "w") as handle:
                handle.write("")
            with open(os.path.join(wt, "bin.dat"), "wb") as handle:
                handle.write(b"\0\1\2\3")
            _git(wt, "add", ".")
            _git(wt, "commit", "-q", "-m", "add new, empty b, a binary")
        _git(root, "worktree", "remove", "--force", wt)
    if record:
        got, detail = store.record_worktree(card["id"], "", branch)
        assert got is not None, detail
    return store.get(card["id"]), branch


async def _get(api, query, headers=None):
    hdrs = {"host": "localhost", "x-bob-token": "token"} \
        if headers is None else headers
    lines = [f"GET /api/card-changes?{query} HTTP/1.1"]
    lines += [f"{k}: {v}" for k, v in hdrs.items()] + ["", ""]
    reader = asyncio.StreamReader()
    reader.feed_data("\r\n".join(lines).encode())
    reader.feed_eof()
    writer = _Writer()
    await api._handle_client(reader, writer)
    head, _, body = bytes(writer.buf).partition(b"\r\n\r\n")
    return int(head.split()[1]), body


async def _page(api, card_id):
    status, body = await _get(api, "card=" + quote(card_id, safe=""))
    assert status == 200, body
    return json.loads(body)


# --- the loopback GET -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_loopback_get_wants_a_token_and_takes_either(setup):
    api, daemon, store, root = setup
    card, _ = _card_with_branch(store, root)
    query = "card=" + quote(card["id"], safe="")
    status, _ = await _get(api, query, {"host": "localhost"})
    assert status == 403
    status, _ = await _get(api, query, {"host": "localhost",
                                         "x-bob-token": "wrong"})
    assert status == 403
    status, _ = await _get(api, query)
    assert status == 200
    # The session token is a second key the daemon accepts on SESSION_READS.
    assert "/api/card-changes" in __import__(
        "dark_army_daemon.api_server", fromlist=["x"]).SESSION_READS


@pytest.mark.asyncio
async def test_two_commits_list_two_commits_and_files_with_counts(setup):
    api, daemon, store, root = setup
    card, branch = _card_with_branch(store, root)
    store.record_review_verdict(card["id"], "ship", "a" * 40)
    page = await _page(api, card["id"])
    assert set(page) == set(merges.CHANGES_KEYS)
    assert page["available"] is True and page["reason"] == ""
    assert page["branch"] == branch and page["trunk"] == "main"
    assert page["ahead"] == 2 and page["behind"] == 0
    assert [c["subject"] for c in page["commits"]] == [
        "add new, empty b, a binary", "edit a"]
    for commit in page["commits"]:
        assert set(commit) == set(merges.COMMIT_KEYS)
    files = {f["path"]: f for f in page["files"]}
    assert set(files) == {"a.txt", "b.txt", "bin.dat", "new.txt"}
    for row in files.values():
        assert set(row) == set(merges.FILE_ROW_KEYS)
    assert (files["a.txt"]["added"], files["a.txt"]["removed"]) == (1, 0)
    assert (files["b.txt"]["added"], files["b.txt"]["removed"]) == (0, 1)
    assert files["bin.dat"]["binary"] is True
    assert page["files_total"] == 4
    assert page["files_truncated"] is False
    assert page["branch_tip"] == _git(root, "rev-parse", branch).strip()
    assert len(page["merge_base"]) == 8
    assert set(page["review"]) == set(merges.REVIEW_KEYS)
    assert page["review"]["verdict"] == "ship"
    assert page["review"]["current"] is False  # judged another version
    # No key, digest, claim or channel id rides the page.
    text = json.dumps(page)
    for word in ("claim", "secret", "token\":"):
        assert word not in text


@pytest.mark.asyncio
async def test_a_branch_equal_to_main_is_zero_ahead_with_no_files(setup):
    api, daemon, store, root = setup
    card, _ = _card_with_branch(store, root, commits=0)
    page = await _page(api, card["id"])
    assert page["available"] is True
    assert page["ahead"] == 0 and page["files"] == [] and page["commits"] == []
    assert page["files_total"] == 0


@pytest.mark.asyncio
async def test_a_file_and_its_changes_by_index_and_tip(setup):
    api, daemon, store, root = setup
    card, branch = _card_with_branch(store, root)
    page = await _page(api, card["id"])
    index = [f["path"] for f in page["files"]].index("a.txt")
    base = "card=" + quote(card["id"], safe="")
    status, body = await _get(
        api, f"{base}&file={index}&tip={page['branch_tip']}")
    assert status == 200, body
    diff = json.loads(body)
    assert set(diff) == set(merges.DIFF_KEYS)
    assert diff["available"] is True and diff["path"] == "a.txt"
    assert "+two" in diff["text"] and diff["truncated"] is False
    # A stale tip is 409 in words; a missing or malformed one is 400.
    status, body = await _get(api, f"{base}&file={index}&tip={'0' * 40}")
    assert status == 409
    assert json.loads(body)["error"] == merges.CHANGES_MOVED_REFUSAL
    status, _ = await _get(api, f"{base}&file={index}")
    assert status == 400
    status, _ = await _get(api, f"{base}&file={index}&tip=zzz")
    assert status == 400
    status, _ = await _get(api, f"{base}&file=abc&tip={page['branch_tip']}")
    assert status == 400
    # An index past the list is 404; the moved branch is a 409 not a 500.
    status, body = await _get(api, f"{base}&file=9&tip={page['branch_tip']}")
    assert status == 404
    with open(os.path.join(root, "x.txt"), "w") as handle:
        handle.write("x\n")
    _git(root, "worktree", "add", "-q", os.path.join(root, ".worktrees", "w"),
         branch)
    wt = os.path.join(root, ".worktrees", "w")
    with open(os.path.join(wt, "later.txt"), "w") as handle:
        handle.write("later\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "later")
    status, _ = await _get(api, f"{base}&file={index}&tip={page['branch_tip']}")
    assert status == 409


@pytest.mark.asyncio
async def test_a_large_diff_is_cut_and_says_so(setup):
    api, daemon, store, root = setup
    card, branch = _card_with_branch(store, root, commits=0)
    wt = os.path.join(root, ".worktrees", "big")
    _git(root, "worktree", "add", "-q", wt, branch)
    with open(os.path.join(wt, "big.txt"), "w") as handle:
        handle.write("line of text\n" * 20000)
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "big")
    page = await _page(api, card["id"])
    status, body = await _get(
        api, f"card={quote(card['id'], safe='')}&file=0&tip={page['branch_tip']}")
    assert status == 200
    diff = json.loads(body)
    assert diff["truncated"] is True
    assert len(diff["text"].encode()) <= work_record.MAX_DIFF_BYTES + 4


@pytest.mark.asyncio
async def test_problems_are_available_false_with_the_reason_never_a_500(
        setup, tmp_path):
    api, daemon, store, root = setup
    nobranch, _ = _card_with_branch(store, root, commits=0, record=False)
    page = await _page(api, nobranch["id"])
    assert page["available"] is False and page["reason"] == merges.NO_BRANCH_REFUSAL
    page = await _page(api, "no-such-card")
    assert page["available"] is False and page["reason"] == merges.NO_CARD_REFUSAL
    # An unenrolled root.
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    card, _ = store.create({"title": "t", "project": "p", "root": str(other),
                            "column_name": "done"})
    store.record_worktree(card["id"], "", "card/zzzzzzzz-t")
    page = await _page(api, card["id"])
    assert page["available"] is False
    assert page["reason"] == merges.NOT_ENROLLED_REFUSAL
    # A branch the project no longer has.
    gone, _ = store.create({"title": "t", "project": "p", "root": root,
                            "column_name": "done"})
    store.record_worktree(gone["id"], "", "card/vanished-x")
    page = await _page(api, gone["id"])
    assert page["available"] is False
    # Malformed input is 400 in words.
    status, body = await _get(api, "")
    assert status == 400 and json.loads(body)["error"]
    status, _ = await _get(api, "card=a&card=b")
    assert status == 400
    daemon._board = None
    page = await _page(api, "x")
    assert page["available"] is False
    daemon._board = store


@pytest.mark.asyncio
async def test_the_page_says_whether_merge_is_offered_and_why_not(setup):
    api, daemon, store, root = setup
    card, _ = _card_with_branch(store, root)
    page = await _page(api, card["id"])
    assert page["merge_offered"] is True and page["merge_refusal"] == ""
    daemon._merging = {card["id"]: {"root": root, "since": time.time(),
                                    "token": "t"}}
    page = await _page(api, card["id"])
    assert page["merge_offered"] is False
    assert page["merge_refusal"] == merges.MERGE_RUNNING_REFUSAL
    assert page["merge_state"] == "merging"
    assert page["merge_note"] == merges.MERGING_NOTE
    daemon._merging = {}


@pytest.mark.asyncio
async def test_an_oversize_page_drops_files_from_the_tail_and_says_so(setup):
    report = {"available": True, "files_truncated": False,
              "files": [{"path": "f" * 200 + str(i), "added": 1, "removed": 1,
                         "binary": False} for i in range(3000)]}
    body = ApiServer._card_changes_page_bytes(report)
    assert len(body) <= 300_000
    page = json.loads(body)
    assert page["files_truncated"] is True and 0 < len(page["files"]) < 3000


@pytest.mark.asyncio
async def test_the_changes_lock_serialises_two_reads(setup):
    api, daemon, store, root = setup
    card, _ = _card_with_branch(store, root, commits=0)
    inside = []
    overlap = []
    real = daemon._changes_context

    def slow(card_id):
        if inside:
            overlap.append(card_id)
        inside.append(card_id)
        time.sleep(0.05)
        try:
            return real(card_id)
        finally:
            inside.pop()

    daemon._changes_context = slow
    await asyncio.gather(*[daemon.card_changes_report(card["id"])
                           for _ in range(4)])
    assert overlap == []
    assert isinstance(daemon._changes_lock, type(threading.Lock()))


# --- the sealed kind on both doors ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
async def test_the_sealed_kind_answers_on_both_doors_and_records_nothing(
        setup, door):
    api, daemon, store, root = setup
    card, _ = _card_with_branch(store, root)
    actions = api.LAN_ACTIONS if door == "home" else api.REMOTE_ACTIONS
    status, _, body = await api._sealed_run(
        "card_changes", {"card": card["id"]}, "device", actions=actions,
        check_lease=door == "away", record=door == "away")
    assert status == 200, body
    page = json.loads(body)
    assert page["available"] is True and page["ahead"] == 2
    index = [f["path"] for f in page["files"]].index("a.txt")
    status, _, body = await api._sealed_run(
        "card_changes", {"card": card["id"], "file": index,
                         "tip": page["branch_tip"]}, "device",
        actions=actions, check_lease=door == "away", record=door == "away")
    assert status == 200 and json.loads(body)["path"] == "a.txt"
    status, _, body = await api._sealed_run(
        "card_changes", {"card": card["id"], "file": index,
                         "tip": "1" * 40}, "device", actions=actions,
        check_lease=door == "away", record=door == "away")
    assert status == 409
    assert list(getattr(daemon, "_remote_activity", [])) == []


@pytest.mark.asyncio
async def test_a_lapsed_lease_still_answers_the_sealed_read(setup, monkeypatch):
    api, daemon, store, root = setup
    card, _ = _card_with_branch(store, root)
    relay.create_channel("phone")
    relay.note_lan_proof("phone")
    monkeypatch.setattr(relay, "lease_valid", lambda _device: False)
    status, _, body = await api._remote_run(
        "card_changes", {"card": card["id"]}, "phone")
    assert status == 200 and json.loads(body)["available"] is True


@pytest.mark.asyncio
async def test_the_sealed_kind_checks_no_lease_and_names_no_path(setup):
    import inspect
    from dark_army_daemon import api_server
    src = inspect.getsource(ApiServer._sealed_run)
    branch = src.split('if kind == "card_changes":', 1)[1].split(
        "if kind ==", 1)[0]
    code = "\n".join(line for line in branch.splitlines()
                     if not line.strip().startswith("#"))
    assert "lease" not in code and "_card_changes_for(payload)" in code
    # Above `action`, and joined to the home door's kinds.
    assert src.index('kind == "card_changes"') < src.index('kind == "action"')
    assert '"card_changes"' in inspect.getsource(ApiServer._lan_home)
    # `file` is an integer index: a path in its place is a 400.
    api, daemon, store, root = setup
    status, _, body = await api._sealed_run(
        "card_changes", {"card": "c", "file": "../../etc/passwd",
                         "tip": "a" * 40}, "device",
        actions=ApiServer.LAN_ACTIONS, check_lease=False, record=False)
    assert status == 400, body
    assert api_server.SESSION_READS.count("/api/card-changes") == 1


@pytest.mark.asyncio
async def test_the_sealed_body_reads_card_file_and_tip_and_nothing_else(setup):
    api, daemon, store, root = setup
    card, _ = _card_with_branch(store, root)
    status, _ctype, body = await api._card_changes_for(
        {"card": card["id"], "root": "/etc", "path": "/etc/passwd"})
    assert status == 200
    assert json.loads(body)["card_id"] == card["id"]
