"""The board snapshot's installed CLI map.

The board needs to show which launchers are available even when its store is
closed, so the panel can explain what can be started without making a CLI
launch. The map comes from `dispatch.installed_tools`; pinning it at the
snapshot keeps the board read useful in both the closed and open states.
"""

from pathlib import Path

from dark_army_daemon import dispatch
from dark_army_daemon.daemon import BobDaemon
from dark_army_daemon.board import BoardStore


def test_a_closed_board_snapshot_carries_the_installed_map(monkeypatch):
    monkeypatch.setattr(dispatch, "installed_tools", lambda: {
        "claude": True, "codex": False, "grok": True,
    })
    daemon = BobDaemon(headless=True)

    assert daemon._build_board_state()["installed"] == {
        "claude": True, "codex": False, "grok": True,
    }


def test_daemon_board_has_no_old_cli_refusal():
    source = Path(__file__).parents[1] / "dark_army_daemon" / "daemon_board.py"

    assert "not on Dark Army's PATH" not in source.read_text()
    assert source.read_text().count("NOT_INSTALLED_REFUSAL") >= 6


def test_an_open_board_snapshot_carries_the_installed_map(monkeypatch, tmp_path):
    monkeypatch.setattr(dispatch, "installed_tools", lambda: {
        "claude": True, "codex": False, "grok": True})
    daemon = BobDaemon(headless=True)
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    try:
        daemon._board = store
        snapshot = daemon._build_board_state()
        assert snapshot["available"] is True
        assert snapshot["installed"] == {
            "claude": True, "codex": False, "grok": True}
    finally:
        store.close()
