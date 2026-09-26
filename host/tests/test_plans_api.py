# host/tests/test_plans_api.py
"""The plan reads on all three doors: loopback `GET /api/plans` and `GET
/api/plan?path=` (token / Host / Bearer / Origin exactly as
`/api/scout-reports`), the sealed `plans` / `plan` kinds on home and away
(reads: neither action tuple grew, nothing recorded), the marker on every
board shape, and the 300 KB page that keeps the newest rows."""

import asyncio
import inspect
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from dark_army_daemon.api_server import ApiServer, _Request
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon import enrollment, plan_index

REPO = Path(__file__).resolve().parents[2]

PLAN = """# The phone reads plans

- **Date:** 2026-09-25
- **Status:** draft
- **Area:** pocket

## What this does

Reads plans.
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
    (a / "plans").mkdir(parents=True)
    b.mkdir()
    plan = a / "plans" / "2026-09-25-phone-reads-plans.md"
    plan.write_text(PLAN)
    (a / "plans" / "README.md").write_text(PLAN)
    (a / "notes.md").write_text(PLAN)
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
                        "plan": os.path.realpath(plan),
                        "readme": os.path.realpath(a / "plans" / "README.md"),
                        "notes": os.path.realpath(a / "notes.md"),
                        "tmp": tmp_path, "store": store}
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
    return "/api/plan?path=" + quote(path, safe="")


@pytest.mark.asyncio
async def test_loopback_index_lists_the_plan_without_its_body(setup):
    api, _, env = setup
    status, body = await _get(api, "/api/plans")
    assert status == 200, body
    index = json.loads(body)
    assert index["supported"] is True and index["available"] is True
    assert [r["path"] for r in index["rows"]] == [env["plan"]]
    row = index["rows"][0]
    assert row["title"] == "The phone reads plans"
    assert row["status"] == "draft" and row["area"] == "pocket"
    assert row["project"] == "project-a"
    assert row["day"] == "2026-09-25"
    assert "body" not in row
    assert b"## What this does" not in body


@pytest.mark.asyncio
async def test_loopback_lists_this_checkouts_plans(tmp_path, monkeypatch):
    """The success criterion's list: this repository's plans, newest
    first, this plan among the first ten, no readme or question list."""
    repo = os.path.realpath(REPO)
    if not os.path.isfile(os.path.join(
            repo, "plans", "2026-09-25-phone-plans-library.md")):
        pytest.skip("this plan is not in the checkout")
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    _enrol(monkeypatch, repo)
    api = ApiServer(daemon, port=0)
    api.token = "token"
    try:
        status, body = await _get(api, "/api/plans")
    finally:
        store.close()
    assert status == 200, body
    rows = json.loads(body)["rows"]
    names = [r["name"] for r in rows]
    first = {r["name"]: r for r in rows[:10]}
    assert "2026-09-25-phone-plans-library.md" in first, names[:10]
    assert first["2026-09-25-phone-plans-library.md"]["title"] == \
        "The phone lists every project's plans and reads one"
    assert "README.md" not in names
    assert "answerable-questions.md" not in names
    days = [r["day"] for r in rows if r["day"]]
    assert days == sorted(days, reverse=True)


@pytest.mark.asyncio
async def test_a_card_plan_carries_its_card_on_the_row(setup):
    api, _, env = setup
    store = env["store"]
    card, _ = store.create({"title": "Phone plans", "root": env["a"]})
    store._conn.execute("UPDATE cards SET plan_path = ? WHERE id = ?",
                        (env["plan"], card["id"]))
    status, body = await _get(api, "/api/plans")
    assert status == 200, body
    row = json.loads(body)["rows"][0]
    assert row["card_id"] == card["id"]
    assert row["card_title"] == "Phone plans"
    assert row["card_column"] == card["column_name"]


@pytest.mark.asyncio
async def test_loopback_index_narrows_to_one_enrolled_root(setup):
    api, _, env = setup
    status, body = await _get(
        api, "/api/plans?root=" + quote(env["b"], safe=""))
    assert status == 200, body
    assert json.loads(body)["rows"] == []
    status, body = await _get(api, "/api/plans?root=")
    assert status == 400
    assert "project" in json.loads(body)["error"]
    status, body = await _get(
        api, "/api/plans?root=" + quote("/tmp/nowhere-xyz", safe=""))
    assert status == 400
    assert "watching" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_loopback_body_read_returns_the_plan(setup):
    api, _, env = setup
    status, body = await _get(api, _body_target(env["plan"]))
    assert status == 200, body
    plan = json.loads(body)
    assert plan["available"] is True
    assert plan["title"] == "The phone reads plans"
    assert not plan["body"].startswith("# ")
    assert "## What this does" in plan["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["/api/plans", "/api/plan?path=%2Fx.md"])
async def test_missing_token_is_403(setup, target):
    api, _, _ = setup
    status, body = await _get(api, target, headers={"host": "localhost"})
    assert status == 403
    assert b"forbidden" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["/api/plans", "/api/plan?path=%2Fx.md"])
async def test_authorization_bearer_is_403(setup, target):
    api, _, _ = setup
    status, _ = await _get(api, target, headers={
        "host": "localhost", "authorization": "Bearer token"})
    assert status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["/api/plans", "/api/plan?path=%2Fx.md"])
async def test_non_loopback_host_is_403(setup, target):
    api, _, _ = setup
    status, body = await _get(api, target, headers={
        "host": "evil.example", "x-bob-token": "token"})
    assert status == 403
    assert b"non-loopback" in body


def test_empty_origin_is_allowed_on_get(setup):
    api, _, _ = setup
    for path in ("/api/plans", "/api/plan"):
        req = _Request("GET", path, "",
                       {"x-bob-token": "token", "host": "localhost"}, b"")
        assert api._authorised(req) is True


@pytest.mark.asyncio
async def test_path_outside_the_closed_set_is_unavailable_in_words(setup):
    api, _, env = setup
    stray = Path(env["tmp"]) / "stray.md"
    stray.write_text(PLAN)
    for path in (str(stray), "/etc/hosts", env["readme"], env["notes"],
                 os.path.join(env["a"], "plans", "..", "notes.md")):
        status, body = await _get(api, _body_target(path))
        assert status == 200, body
        plan = json.loads(body)
        assert plan["available"] is False
        assert plan["reason"] == plan_index.NOT_LISTED
        assert plan["body"] == ""


@pytest.mark.asyncio
async def test_missing_or_oversized_path_is_400_in_words(setup):
    api, _, _ = setup
    for target in ("/api/plan", "/api/plan?path=",
                   "/api/plan?path=%2F" + "x" * 4100):
        status, body = await _get(api, target)
        assert status == 400, target
        assert "path" in json.loads(body)["error"]


@pytest.mark.asyncio
async def test_repeated_parameter_is_400(setup):
    api, _, env = setup
    target = _body_target(env["plan"]) + "&path=%2Fx.md"
    status, body = await _get(api, target)
    assert status == 400
    assert "repeat" in json.loads(body)["error"]
    status, body = await _get(api, "/api/plans?root=a&root=b")
    assert status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
async def test_sealed_kinds_are_reads_on_both_doors(setup, door):
    api, daemon, env = setup
    actions = api.LAN_ACTIONS if door == "home" else api.REMOTE_ACTIONS

    async def run(kind, payload):
        return await api._sealed_run(
            kind, payload, "device", actions=actions,
            check_lease=door == "away", record=door == "away")

    status, _, body = await run("plans", {})
    assert status == 200, body
    assert [r["path"] for r in json.loads(body)["rows"]] == [env["plan"]]
    status, _, body = await run("plans", {"root": ""})
    assert status == 400
    status, _, body = await run("plan", {"path": env["plan"]})
    assert status == 200, body
    plan = json.loads(body)
    assert plan["available"] is True and plan["status"] == "draft"
    status, _, body = await run("plan", {"path": env["notes"]})
    assert status == 200
    assert json.loads(body)["reason"] == plan_index.NOT_LISTED
    status, _, body = await run("plan", {})
    assert status == 400
    assert list(getattr(daemon, "_remote_activity", [])) == []


def test_the_lan_door_admits_both_kinds():
    src = inspect.getsource(ApiServer._lan_home)
    assert '"plans", "plan", "action"' in src


def test_both_kinds_sit_above_the_action_branch():
    src = inspect.getsource(ApiServer._sealed_run)
    action = src.index('if kind == "action":')
    assert src.index('if kind == "plans":') < action
    assert src.index('if kind == "plan":') < action


def test_neither_action_tuple_grew():
    for kind in ("plans", "plan"):
        assert kind not in ApiServer.LAN_ACTIONS
        assert kind not in ApiServer.REMOTE_ACTIONS
    assert len(ApiServer.REMOTE_ACTIONS) <= len(ApiServer.LAN_ACTIONS)
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_every_board_shape_states_the_marker(setup):
    _api, daemon, env = setup
    assert daemon._pipeline_writable()["plans_supported"] is True
    assert daemon._build_board_state()["plans_supported"] is True
    daemon._board = None
    try:
        assert daemon._build_board_state()["plans_supported"] is True
    finally:
        daemon._board = env["store"]


def test_an_older_snapshot_without_the_marker_decodes_false():
    assert {}.get("plans_supported", False) is False


def test_a_page_over_300k_keeps_the_newest_prefix():
    rows = [{"path": f"/p/2026-01-01-{i:03d}.md", "title": "t" * 1200,
             "day": "2026-01-01", "written_at": 10_000 - i}
            for i in range(400)]
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


@pytest.mark.asyncio
async def test_build_stops_reading_heads_past_the_page_bound(
        setup, monkeypatch):
    api, _, env = setup
    for day in range(1, 10):
        Path(env["a"], "plans", f"2026-01-0{day}-p.md").write_text(PLAN)
    monkeypatch.setattr(plan_index, "PAGE_BYTES", 1)
    status, body = await _get(api, "/api/plans")
    assert status == 200, body
    index = json.loads(body)
    assert index["truncated"] is True
    assert index["omitted"] + len(index["rows"]) == 10
    assert index["rows"][0]["path"] == env["plan"]
