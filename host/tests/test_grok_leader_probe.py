"""The Grok client-name probe's pure compare and its two refusals.

Nothing here opens a socket or spawns a leader. The live run is the tool.
"""

import asyncio
import sys
from pathlib import Path

from dark_army_daemon import grok_leader

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import grok_leader_probe as probe  # noqa: E402


def test_compare_legs_equal():
    legs = [
        {"leg": "list", "ok": True, "detail": "count=2"},
        {"leg": "load", "ok": True, "detail": "keys=['cwd']"},
    ]
    assert probe.compare_legs(legs, [dict(leg) for leg in legs]) == "EQUAL"


def test_compare_legs_differs_on_detail():
    left = [{"leg": "list", "ok": True, "detail": "count=1"}]
    right = [{"leg": "list", "ok": True, "detail": "count=2"}]
    assert probe.compare_legs(left, right) == "DIFFERS list"


def test_compare_legs_ignores_client_id():
    left = [{
        "leg": "list",
        "ok": True,
        "detail": 'client_id=11 "client_id": "aaa" count=1',
    }]
    right = [{
        "leg": "list",
        "ok": True,
        "detail": 'client_id=99 "client_id": "bbb" count=1',
    }]
    assert probe.compare_legs(left, right) == "EQUAL"


def test_changed_leg_compares_arrived_and_top_not_the_method_tape():
    """Method lists are not the changed leg. Arrived and top are."""
    left = [{
        "leg": "changed",
        "ok": True,
        "detail": "arrived=no top=[]",
        "tape": [
            "_x.ai/git_head_changed",
            "_x.ai/session_notification",
            "_x.ai/models/update",
        ],
    }]
    right = [{
        "leg": "changed",
        "ok": True,
        "detail": "arrived=no top=[]",
        "tape": ["_x.ai/session_notification", "_x.ai/git_head_changed"],
    }]
    assert probe.compare_legs(left, right) == "EQUAL"
    assert probe.compare_legs(left, [{
        "leg": "changed",
        "ok": True,
        "detail": "arrived=yes top=[]",
    }]) == "DIFFERS changed"
    assert probe.compare_legs(left, [{
        "leg": "changed",
        "ok": True,
        "detail": "arrived=no top=['method']",
    }]) == "DIFFERS changed"


def test_compare_legs_ignores_name_and_timestamps():
    left = [{
        "leg": "load",
        "ok": True,
        "detail": "name=bob-companion at 2026-09-23T10:00:00Z",
    }]
    right = [{
        "leg": "load",
        "ok": True,
        "detail": "name=dark-army at 2026-09-23T11:11:11.5Z",
    }]
    assert probe.compare_legs(left, right) == "EQUAL"


def test_pick_session_skips_residents_and_live_tabs():
    entries = [
        {
            "sessionId": "live",
            "resident": True,
            "updated_at": "2026-09-23T12:00:00Z",
            "cwd": "/live",
        },
        {
            "sessionId": "tab",
            "resident": False,
            "updated_at": "2026-09-23T11:00:00Z",
            "cwd": "/tab",
        },
        {
            "sessionId": "old",
            "resident": False,
            "updated_at": "2026-09-01T00:00:00Z",
            "cwd": "/old",
        },
        {
            "sessionId": "new",
            "resident": False,
            "updated_at": "2026-09-22T00:00:00Z",
            "cwd": "/new",
        },
    ]
    chosen = probe.pick_session(entries, "", {"tab"})
    assert chosen["sessionId"] == "new"
    assert probe.pick_session(entries, "live", {"live"}) is None
    explicit = probe.pick_session(entries, "old", set())
    assert explicit["sessionId"] == "old"
    assert explicit["cwd"] == "/old"


def test_main_refuses_without_the_sock(monkeypatch, capsys):
    saved = sys.modules.pop("pytest", None)

    async def boom(*_args, **_kwargs):
        raise AssertionError("spawned")

    monkeypatch.setattr(grok_leader, "leader_sock_present", lambda path=None: False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    try:
        rc = probe.main([])
    finally:
        if saved is not None:
            sys.modules["pytest"] = saved
    captured = capsys.readouterr()
    assert rc == 2
    assert "CANNOT TELL leader not running" in captured.out


def test_main_refuses_under_pytest(monkeypatch, capsys):
    async def boom(*_args, **_kwargs):
        raise AssertionError("spawned")

    monkeypatch.setattr(grok_leader, "leader_sock_present", lambda path=None: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    assert "pytest" in sys.modules
    rc = probe.main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "CANNOT TELL" not in captured.out
    assert "pytest is loaded" in captured.err
