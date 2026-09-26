# host/tests/test_scout_reports_api.py
"""The scout-report reads on all three doors: loopback `GET
/api/scout-reports` and `GET /api/scout-report?path=` (token / Host /
Bearer / Origin exactly as `/api/knowledge`), the sealed `scout_reports` /
`scout_report` kinds on home and away (reads: neither action tuple grew),
the marker, and the 300 KB page that keeps the newest rows."""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from dark_army_daemon.api_server import ApiServer, _Request
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon import enrollment

REPORT = """# Why the relay is busy

- **Card:** Scout: relay
- **Project:** Alpha
- **Question:** Why so many commands?
- **Verdict:** The idle poll is most of the bill.
- **Confidence:** high
- **Recommendation:** build
- **Sources:** relay/api/box.js

## Question

Why?

## What was found

The poll.

## Evidence

Logs.

## Recommendation

Build.

## Open questions

None.
"""


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


def _enrol(monkeypatch, *roots):
    members = {os.path.realpath(r) for r in roots}

    def _normalise(root):
        text = str(root or "").strip()
        if not text:
            return ""
        return os.path.realpath(text)

    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set(members))
    monkeypatch.setattr(enrollment, "normalise", _normalise)
    monkeypatch.setattr(
        enrollment, "root_enrolled",
        lambda cwd: _normalise(cwd) if _normalise(cwd) in members else "")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    a = tmp_path / "project-a"
    b = tmp_path / "project-b"
    (a / "scout" / "2026-09-25-relay").mkdir(parents=True)
    b.mkdir()
    report = a / "scout" / "2026-09-25-relay" / "report.md"
    report.write_text(REPORT)
    os.utime(report, (2_000_000, 2_000_000))
    root_a = os.path.realpath(a)
    root_b = os.path.realpath(b)
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    _enrol(monkeypatch, root_a, root_b)
    api = ApiServer(daemon, port=0)
    api.token = "token"
    yield api, daemon, {"a": root_a, "b": root_b,
                        "report": os.path.realpath(report),
                        "tmp": tmp_path}
    store.close()


def _headers(token="token", host="localhost"):
    headers = {}
    if host is not None:
        headers["host"] = host
    if token is not None:
        headers["x-bob-token"] = token
    return headers


async def _handle(api, raw: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = _Writer()
    await api._handle_client(reader, writer)
    return bytes(writer.buf)


async def _get(api, target, headers=None):
    lines = [f"GET {target} HTTP/1.1"]
    for key, value in dict(headers or _headers()).items():
        lines.append(f"{key}: {value}")
    lines += ["", ""]
    raw = await _handle(api, "\r\n".join(lines).encode())
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


def _body_target(path):
    return "/api/scout-report?path=" + quote(path, safe="")


@pytest.mark.asyncio
async def test_loopback_index_lists_the_report_without_its_body(setup):
    api, _, env = setup
    status, body = await _get(api, "/api/scout-reports")
    assert status == 200, body
    index = json.loads(body)
    assert index["supported"] is True and index["available"] is True
    assert [r["path"] for r in index["rows"]] == [env["report"]]
    row = index["rows"][0]
    assert row["title"] == "Why the relay is busy"
    assert row["verdict"].startswith("The idle poll")
    assert row["project"] == "project-a"
    assert "body" not in row and "text" not in row
    assert b"## What was found" not in body


@pytest.mark.asyncio
async def test_loopback_index_narrows_to_one_enrolled_root(setup):
    api, _, env = setup
    status, body = await _get(
        api, "/api/scout-reports?root=" + quote(env["b"], safe=""))
    assert status == 200, body
    assert json.loads(body)["rows"] == []
    status, body = await _get(api, "/api/scout-reports?root=")
    assert status == 400
    assert "project" in json.loads(body)["error"]
    status, body = await _get(
        api, "/api/scout-reports?root=" + quote("/tmp/nowhere-xyz", safe=""))
    assert status == 400
    assert "watching" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_loopback_body_read_splits_header_and_body(setup):
    api, _, env = setup
    status, body = await _get(api, _body_target(env["report"]))
    assert status == 200, body
    report = json.loads(body)
    assert report["available"] is True
    assert report["header"]["verdict"].startswith("The idle poll")
    assert report["has_header"] is True
    assert not report["body"].startswith("# ")
    assert report["body"].startswith("## Question")
    assert "- **Verdict:**" not in report["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["/api/scout-reports",
                                    "/api/scout-report?path=%2Fx.md"])
async def test_missing_token_is_403(setup, target):
    api, _, _ = setup
    status, body = await _get(api, target, headers={"host": "localhost"})
    assert status == 403
    assert b"forbidden" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["/api/scout-reports",
                                    "/api/scout-report?path=%2Fx.md"])
async def test_authorization_bearer_is_403(setup, target):
    api, _, _ = setup
    status, _ = await _get(api, target, headers={
        "host": "localhost", "authorization": "Bearer token"})
    assert status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["/api/scout-reports",
                                    "/api/scout-report?path=%2Fx.md"])
async def test_non_loopback_host_is_403(setup, target):
    api, _, _ = setup
    status, body = await _get(api, target, headers={
        "host": "evil.example", "x-bob-token": "token"})
    assert status == 403
    assert b"non-loopback" in body


def test_empty_origin_is_allowed_on_get(setup):
    api, _, _ = setup
    for path in ("/api/scout-reports", "/api/scout-report"):
        req = _Request("GET", path, "",
                       {"x-bob-token": "token", "host": "localhost"}, b"")
        assert api._authorised(req) is True


@pytest.mark.asyncio
async def test_path_outside_the_closed_set_is_unavailable_in_words(setup):
    api, _, env = setup
    stray = Path(env["tmp"]) / "stray.md"
    stray.write_text(REPORT)
    for path in (str(stray), "/etc/hosts",
                 os.path.join(env["a"], "README.md")):
        status, body = await _get(api, _body_target(path))
        assert status == 200, body
        report = json.loads(body)
        assert report["available"] is False
        assert report["reason"] == "that report is not one Dark Army lists"
        assert report["body"] == ""


@pytest.mark.asyncio
async def test_missing_path_is_400_in_words(setup):
    api, _, _ = setup
    for target in ("/api/scout-report", "/api/scout-report?path="):
        status, body = await _get(api, target)
        assert status == 400
        assert "path" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_repeated_parameter_is_400(setup):
    api, _, env = setup
    target = _body_target(env["report"]) + "&path=%2Fx.md"
    status, body = await _get(api, target)
    assert status == 400
    assert "repeat" in json.loads(body)["error"]
    status, body = await _get(api, "/api/scout-reports?root=a&root=b")
    assert status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
async def test_sealed_kinds_are_reads_on_both_doors(setup, door):
    api, _, env = setup
    actions = api.LAN_ACTIONS if door == "home" else api.REMOTE_ACTIONS

    async def run(kind, payload):
        return await api._sealed_run(
            kind, payload, "device", actions=actions,
            check_lease=door == "away", record=False)

    status, _, body = await run("scout_reports", {})
    assert status == 200, body
    assert [r["path"] for r in json.loads(body)["rows"]] == [env["report"]]
    status, _, body = await run("scout_report", {"path": env["report"]})
    assert status == 200, body
    report = json.loads(body)
    assert report["available"] is True
    assert report["header"]["confidence"] == "high"
    status, _, body = await run("scout_report", {"path": "/etc/hosts"})
    assert status == 200 and json.loads(body)["available"] is False
    status, _, body = await run("scout_report", {})
    assert status == 400


def test_the_lan_door_admits_both_kinds():
    import inspect
    src = inspect.getsource(ApiServer._lan_home)
    assert '"scout_reports"' in src and '"scout_report"' in src


def test_neither_action_tuple_grew():
    for kind in ("scout_reports", "scout_report"):
        assert kind not in ApiServer.LAN_ACTIONS
        assert kind not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_the_marker_is_published(setup):
    _, daemon, _ = setup
    assert daemon._pipeline_writable()["scout_reports_supported"] is True


def test_an_older_snapshot_without_the_marker_decodes_false():
    assert {}.get("scout_reports_supported", False) is False


def test_a_page_over_300k_keeps_the_newest_prefix():
    rows = [{"path": f"/r/{i:03d}", "title": "t" * 1200,
             "written_at": 10_000 - i} for i in range(400)]
    report = {"supported": True, "available": True, "rows": list(rows),
              "truncated": False, "omitted": 0, "roots": 1}
    body = ApiServer._scout_reports_page_bytes(report)
    assert len(body) <= 300_000
    page = json.loads(body)
    assert page["truncated"] is True
    assert page["omitted"] > 0
    assert page["omitted"] + len(page["rows"]) == 400
    kept = [r["path"] for r in page["rows"]]
    assert kept == [r["path"] for r in rows[:len(kept)]], \
        "the kept rows must be the newest prefix"


# ---- `q`: the text search over the bodies, on all three doors ----

@pytest.mark.asyncio
async def test_loopback_q_finds_a_body_word_with_a_snippet(setup):
    api, _, env = setup
    status, body = await _get(api, "/api/scout-reports?q=poll")
    assert status == 200, body
    index = json.loads(body)
    assert index["query"] == "poll"
    assert [r["path"] for r in index["rows"]] == [env["report"]]
    row = index["rows"][0]
    assert "poll" in row["snippet"].casefold()
    assert row["match"] == "body" and row["match_line"] >= 1
    assert "body" not in row and "text" not in row
    # Only the matching rows, and no report text: the body's own headings
    # never ride the reply.
    assert b"## What was found" not in body
    assert b"## Evidence" not in body
    assert index["searched"] == 1 and index["unsearched"] == 0


@pytest.mark.asyncio
async def test_a_short_or_empty_q_is_400_in_words(setup):
    api, _, _ = setup
    for target in ("/api/scout-reports?q=po", "/api/scout-reports?q=",
                   "/api/scout-reports?q=%20%20ab%20"):
        status, body = await _get(api, target)
        assert status == 400, target
        assert "3 characters" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_a_q_over_200_characters_is_400(setup):
    api, _, _ = setup
    status, body = await _get(api, "/api/scout-reports?q=" + "x" * 201)
    assert status == 400
    assert "200 characters" in json.loads(body)["error"]
    status, body = await _get(api, "/api/scout-reports?q=" + "x" * 200)
    assert status == 200 and json.loads(body)["rows"] == []


@pytest.mark.asyncio
async def test_q_with_root_narrows_to_that_root(setup):
    api, _, env = setup
    status, body = await _get(
        api, "/api/scout-reports?q=poll&root=" + quote(env["b"], safe=""))
    assert status == 200, body
    assert json.loads(body)["rows"] == []
    status, body = await _get(
        api, "/api/scout-reports?q=poll&root=" + quote(env["a"], safe=""))
    assert status == 200, body
    assert [r["path"] for r in json.loads(body)["rows"]] == [env["report"]]


@pytest.mark.asyncio
async def test_a_word_only_in_the_answer_block_is_no_body_word_hit(setup):
    api, _, _ = setup
    # "commands" is in the Question line, "box.js" in Sources: both are the
    # instant filter's fields, never the daemon's search.
    for word in ("commands", "box.js"):
        status, body = await _get(api, "/api/scout-reports?q=" + word)
        assert status == 200, body
        assert json.loads(body)["rows"] == [], word


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
async def test_sealed_q_is_a_read_on_both_doors(setup, door):
    api, _, env = setup
    actions = api.LAN_ACTIONS if door == "home" else api.REMOTE_ACTIONS

    async def run(payload):
        return await api._sealed_run(
            "scout_reports", payload, "device", actions=actions,
            check_lease=door == "away", record=False)

    status, _, body = await run({"q": "poll"})
    assert status == 200, body
    index = json.loads(body)
    assert [r["path"] for r in index["rows"]] == [env["report"]]
    assert "poll" in index["rows"][0]["snippet"].casefold()
    assert b"## What was found" not in body
    status, _, body = await run({"q": "po"})
    assert status == 400
    assert "3 characters" in json.loads(body)["error"]
    status, _, body = await run({"q": "poll", "root": env["b"]})
    assert status == 200 and json.loads(body)["rows"] == []


def test_the_text_search_grew_neither_action_tuple():
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    for tuple_ in (ApiServer.LAN_ACTIONS, ApiServer.REMOTE_ACTIONS):
        assert not [a for a in tuple_ if "scout" in a or "search" in a]


def test_the_body_search_marker_is_published(setup):
    _, daemon, _ = setup
    writable = daemon._pipeline_writable()
    assert writable["scout_reports_body_search_supported"] is True
    assert writable["scout_reports_supported"] is True


@pytest.mark.asyncio
async def test_the_page_bound_still_trims_a_q_reply(setup, monkeypatch):
    api, daemon, _ = setup
    asked = []
    rows = [{"path": f"/r/{i:03d}", "title": "t", "snippet": "s" * 1200,
             "match_line": 1, "match": "body", "written_at": 10_000 - i}
            for i in range(400)]

    async def fake_index(root="", query=""):
        asked.append((root, query))
        return {"supported": True, "available": True, "rows": list(rows),
                "truncated": False, "omitted": 0, "roots": 1,
                "query": query, "searched": 400, "unsearched": 0,
                "search_truncated": False, "hits_truncated": False}

    monkeypatch.setattr(daemon, "scout_reports_index", fake_index)
    status, body = await _get(api, "/api/scout-reports?q=poll")
    assert status == 200
    assert asked == [("", "poll")]
    assert len(body) <= 300_000
    page = json.loads(body)
    assert page["truncated"] is True and page["omitted"] > 0
    kept = [r["path"] for r in page["rows"]]
    assert kept == [r["path"] for r in rows[:len(kept)]]


@pytest.mark.asyncio
async def test_a_q_of_combining_marks_alone_is_400(setup):
    api, _, _ = setup
    status, body = await _get(api, "/api/scout-reports?q=" + quote(
        "́́́", safe=""))
    assert status == 400
    assert "3 characters" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_q_with_doubled_whitespace_is_collapsed_and_finds_its_line(setup):
    api, _, env = setup
    status, body = await _get(api, "/api/scout-reports?q=the%20%20poll")
    assert status == 200, body
    index = json.loads(body)
    assert index["query"] == "the poll"
    assert index["rows"][0]["snippet"] == "The poll."


@pytest.mark.asyncio
async def test_two_text_searches_never_run_at_once(setup, monkeypatch):
    import threading
    import time
    _, daemon, _ = setup
    guard = threading.Lock()
    state = {"now": 0, "most": 0, "calls": []}

    def slow_sync(root="", query=""):
        with guard:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
            state["calls"].append(query)
        time.sleep(0.15)
        with guard:
            state["now"] -= 1
        return {"rows": [], "query": query}

    monkeypatch.setattr(daemon, "_scout_reports_index_sync", slow_sync)
    await asyncio.gather(daemon.scout_reports_index("", "poll"),
                         daemon.scout_reports_index("", "relay"))
    assert sorted(state["calls"]) == ["poll", "relay"]
    assert state["most"] == 1, "two text searches ran on the executor at once"
    # The plain list is not held behind a search.
    state.update(now=0, most=0, calls=[])
    await asyncio.gather(daemon.scout_reports_index("", "poll"),
                         daemon.scout_reports_index("", ""))
    assert state["most"] == 2
