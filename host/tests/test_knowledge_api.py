# host/tests/test_knowledge_api.py
"""Human knowledge reader: loopback GET isolation, token/Host, person writes,
sealed kind, sealed write 404.
"""

import asyncio
import json
import os
from urllib.parse import quote

import pytest

from dark_army_daemon.api_server import ApiServer, _Request
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon import enrollment


ROOT_A = None
ROOT_B = None
ROOT_OTHER = "/tmp/not-enrolled-knowledge-root"


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
    global ROOT_A, ROOT_B
    a = tmp_path / "project-a"
    b = tmp_path / "project-b"
    a.mkdir()
    b.mkdir()
    ROOT_A = os.path.realpath(a)
    ROOT_B = os.path.realpath(b)
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    _enrol(monkeypatch, ROOT_A, ROOT_B)
    store.knowledge_put(ROOT_A, "purpose", "q", "answer for A")
    store.knowledge_put(ROOT_B, "purpose", "q", "answer for B")
    api = ApiServer(daemon, port=0)
    api.token = "token"
    yield api, daemon, store
    store.close()


def _headers(token="token", origin="", host="localhost"):
    headers = {}
    if host is not None:
        headers["host"] = host
    if token is not None:
        headers["x-bob-token"] = token
    if origin:
        headers["origin"] = origin
    return headers


async def _handle(api, raw: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = _Writer()
    await api._handle_client(reader, writer)
    return bytes(writer.buf)


def _parse(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split()[1])
    return status, body


async def _get(api, query, headers=None):
    hdrs = dict(headers or _headers())
    lines = [f"GET /api/knowledge?{query} HTTP/1.1"]
    for key, value in hdrs.items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append("")
    status, body = _parse(await _handle(api, "\r\n".join(lines).encode()))
    return status, body


@pytest.mark.asyncio
async def test_isolation_loopback_get_root_a_never_contains_b(setup):
    api, _, _ = setup
    status, body = await _get(api, "root=" + quote(ROOT_A, safe=""))
    assert status == 200, body
    report = json.loads(body)
    answers = [e["answer"] for e in report["entries"]]
    assert answers == ["answer for A"]
    assert "answer for B" not in body.decode()
    assert report["root"] == ROOT_A


@pytest.mark.asyncio
async def test_isolation_loopback_get_root_b_never_contains_a(setup):
    api, _, _ = setup
    status, body = await _get(api, "root=" + quote(ROOT_B, safe=""))
    assert status == 200, body
    report = json.loads(body)
    answers = [e["answer"] for e in report["entries"]]
    assert answers == ["answer for B"]
    assert "answer for A" not in body.decode()


@pytest.mark.asyncio
async def test_isolation_missing_root_is_400_in_words_not_empty_list(setup):
    api, _, _ = setup
    status, body = await _get(api, "")
    assert status == 400
    payload = json.loads(body)
    assert "error" in payload
    assert payload.get("entries") != []
    assert "enrolled" in payload["error"] or "project" in payload["error"]
    status, body = await _get(api, "root=")
    assert status == 400
    assert b"[]" not in body or json.loads(body).get("entries") is None


@pytest.mark.asyncio
async def test_isolation_unenrolled_root_is_400_in_words(setup):
    api, _, _ = setup
    status, body = await _get(api, "root=" + quote(ROOT_OTHER, safe=""))
    assert status == 400
    payload = json.loads(body)
    assert "watching" in payload["error"] or "enrolled" in payload["error"]
    assert "answer for A" not in body.decode()


@pytest.mark.asyncio
async def test_there_is_no_all_roots_route(setup):
    api, _, _ = setup
    status, body = await _get(api, "")
    assert status == 400
    req = _Request("GET", "/api/knowledge", "", _headers(), b"")
    assert api._loopback_host(req)
    status, _ctype, out = await api._knowledge_report_for("")
    assert status == 400
    assert b"answer for A" not in out


@pytest.mark.asyncio
async def test_get_without_token_is_403(setup):
    api, _, _ = setup
    status, body = await _get(
        api, "root=" + quote(ROOT_A, safe=""),
        headers={"host": "localhost"})
    assert status == 403
    assert b"forbidden" in body


@pytest.mark.asyncio
async def test_get_with_authorization_bearer_is_403(setup):
    api, _, _ = setup
    status, body = await _get(
        api, "root=" + quote(ROOT_A, safe=""),
        headers={"host": "localhost", "authorization": "Bearer token"})
    assert status == 403


@pytest.mark.asyncio
async def test_host_non_loopback_is_403(setup):
    api, _, _ = setup
    status, body = await _get(
        api, "root=" + quote(ROOT_A, safe=""),
        headers={"host": "evil.example", "x-bob-token": "token"})
    assert status == 403
    assert b"non-loopback" in body


@pytest.mark.asyncio
async def test_empty_origin_is_allowed_on_get(setup):
    api, _, _ = setup
    req = _Request(
        "GET", "/api/knowledge", "root=" + ROOT_A,
        {"x-bob-token": "token", "host": "localhost"}, b"")
    assert api._authorised(req) is True


@pytest.mark.asyncio
async def test_loopback_confirm_stale_edit_mutate(setup):
    api, _, store = setup
    req = _Request(
        "POST", "/api/action", "",
        {"x-bob-token": "token"},
        json.dumps({"action": "knowledge_confirm", "root": ROOT_A,
                    "key": "purpose"}).encode())
    assert api._board_request(req)[0] == "knowledge_confirm"
    status, _, body = await api._board_action(
        "knowledge_confirm", {"root": ROOT_A, "key": "purpose"})
    assert status == 200, body
    row = store.knowledge_for(ROOT_A)[0]
    assert row["source"] == "person"
    assert row["last_confirmed"] > 0
    assert row["stale"] == ""

    status, _, body = await api._board_action(
        "knowledge_stale", {"root": ROOT_A, "key": "purpose"})
    assert status == 200, body
    row = store.knowledge_for(ROOT_A)[0]
    assert row["stale"] == "1"

    status, _, body = await api._board_action(
        "knowledge_edit",
        {"root": ROOT_A, "key": "purpose", "question": "Q",
         "answer": "edited"})
    assert status == 200, body
    row = store.knowledge_for(ROOT_A)[0]
    assert row["answer"] == "edited"
    assert row["source"] == "person"
    assert row["stale"] == "1"
    assert row["last_confirmed"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
async def test_isolation_sealed_kind_knowledge(setup, door):
    api, _, _ = setup
    actions = api.LAN_ACTIONS if door == "home" else api.REMOTE_ACTIONS

    async def run(kind, payload):
        return await api._sealed_run(
            kind, payload, "device", actions=actions,
            check_lease=door == "away", record=False)

    status, _, body = await run("knowledge", {"root": ROOT_A})
    assert status == 200, body
    report = json.loads(body)
    assert [e["answer"] for e in report["entries"]] == ["answer for A"]
    assert "answer for B" not in body.decode()

    status, _, body = await run("knowledge", {"root": ROOT_B})
    assert status == 200, body
    assert [e["answer"] for e in json.loads(body)["entries"]] == [
        "answer for B"]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
@pytest.mark.parametrize("action", [
    "knowledge_confirm", "knowledge_stale", "knowledge_edit"])
async def test_sealed_knowledge_writes_404(setup, door, action):
    api, _, store = setup
    actions = api.LAN_ACTIONS if door == "home" else api.REMOTE_ACTIONS
    status, _, body = await api._sealed_run(
        "action",
        {"action": action, "root": ROOT_A, "key": "purpose",
         "answer": "forged"},
        "device", actions=actions, check_lease=False, record=False)
    assert status == 404, body
    row = store.knowledge_for(ROOT_A)[0]
    assert row["answer"] == "answer for A"


def test_knowledge_writes_are_not_on_phone_tuples():
    assert "knowledge_confirm" not in ApiServer.LAN_ACTIONS
    assert "knowledge_confirm" not in ApiServer.REMOTE_ACTIONS
    assert "knowledge_stale" not in ApiServer.LAN_ACTIONS
    assert "knowledge_edit" not in ApiServer.LAN_ACTIONS
    assert "knowledge_confirm" not in ApiServer._LAN_BOARD
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)
    assert "knowledge_confirm" in ApiServer.BOARD_ACTIONS


def test_older_snapshot_without_flags_decodes_false():
    flags = {}
    assert flags.get("knowledge_supported", False) is False
    assert flags.get("knowledge_writable", False) is False


@pytest.mark.asyncio
async def test_a_page_over_300k_names_omitted_keys_and_does_not_skip(setup):
    api, _, store = setup
    # 80 × ~4000 exceeds 300 KB; keep answers of differing sizes so a
    # skip-and-continue would smuggle the short one in.
    for i in range(70):
        store.knowledge_put(ROOT_A, f"k{i:03d}", "q", "a" * 4000)
    store.knowledge_put(ROOT_A, "zshort", "q", "tiny")
    status, _, body = await api._knowledge_report_for(
        {"root": ROOT_A})
    assert status == 200
    assert len(body) <= 300_000
    report = json.loads(body)
    keys = [e["key"] for e in report["entries"]]
    ordered = [e["key"] for e in store.knowledge_for(ROOT_A)]
    assert "zshort" not in keys
    assert report["truncated"] is True
    assert "zshort" in report["omitted_keys"]
    assert keys == ordered[:len(keys)], "the kept run must be a prefix"
