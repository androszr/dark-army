# host/tests/test_sse_frame_audit.py
"""`tools/sse_frame_audit.py` replays a saved capture and says what the
stream carried: which frames were only the clock, which boards were whole
and which were per-card deltas, and which keys of which card moved. A
subprocess, `test_relay_latency_bench.py`'s shape: the tool is run the way a
person runs it, with no network on the replay path."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "sse_frame_audit.py"
FIXTURE = ROOT / "host" / "tests" / "fixtures" / "sse" / "ticking-card.jsonl"


def _replay(path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--replay", str(path), "--json"],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def test_the_replay_counts_every_kind_of_frame():
    summary = _replay(FIXTURE)
    assert summary["frames"] == 5
    assert summary["clock_only_frames"] == 1
    # The attach frame is its own count, never a board resend.
    assert summary["attach_frames"] == 1
    assert summary["board_full_frames"] == 0
    assert summary["board_resync_frames"] == 0
    assert summary["board_delta_frames"] == 2
    assert summary["board_delta_cards_max"] == 1
    assert summary["agents_frames"] == 2
    lines = [line for line in FIXTURE.read_text().splitlines() if line]
    wire = sum(len(line[len("data: "):].encode()) for line in lines)
    assert summary["bytes_total"] == wire, "the frame's own bytes, re-dumped"
    assert set(summary["bytes_by_section"]) >= {"board", "agents", "generated_at"}


def test_the_per_card_diff_names_the_key_that_ticked():
    summary = _replay(FIXTURE)
    changes = {(c["frame"], c["id"]): c["keys"] for c in summary["card_changes"]}
    assert changes[(2, "c1")] == ["run_figures"], \
        "the ticking card is named with the key that moved"
    assert changes[(4, "c2")] == ["(removed)"], \
        "a card the order no longer names is reported gone"
    assert len(changes) == 2


def test_a_bare_json_capture_replays_too(tmp_path):
    """A file of bare JSON lines — `jq -c` output, say — reads the same."""
    bare = tmp_path / "bare.jsonl"
    bare.write_text("\n".join(
        line[len("data: "):] for line in FIXTURE.read_text().splitlines()
        if line) + "\n: a comment line\n\n")
    assert _replay(bare)["frames"] == 5


def test_the_tool_is_stdlib_only_and_writes_nothing():
    src = TOOL.read_text()
    assert "sys.platform" not in src
    assert "dark_army_" not in src.replace("Dark Army", "")
    for forbidden in ("api-token", "X-Bob-Token", "open(args.replay, \"w"):
        assert forbidden not in src


def test_the_attach_frame_and_the_one_resync_are_not_resends(tmp_path):
    """What a healthy `?cards=delta` subscription really looks like: the
    attach frame, then — because attaching cleared the daemon's per-card
    maps — one whole board, then deltas. Neither of the first two is a
    board resend, so `board_full_frames` stays 0; a later whole board is
    counted, because that is the thing the tool exists to catch."""
    def board(cards, **extra):
        return dict({"counts": {}, "cards": cards}, **extra)
    card = {"id": "c1", "run_figures": {"cost_usd": 1.0}}
    ticked = {"id": "c1", "run_figures": {"cost_usd": 1.1}}
    frames = [
        {"generated_at": 1, "board": board([card]), "agents": {}},
        {"generated_at": 2, "board": board([card], counts={"prep": 1})},
        {"generated_at": 3, "board": board([ticked], cards_delta=True,
                                           card_order=["c1"])},
        {"generated_at": 4, "board": board([ticked], counts={"prep": 2})},
    ]
    path = tmp_path / "capture.jsonl"
    path.write_text("".join("data: " + json.dumps(f) + "\n" for f in frames))
    summary = _replay(path)
    assert summary["attach_frames"] == 1
    assert summary["board_resync_frames"] == 1
    assert summary["board_delta_frames"] == 1
    assert summary["board_full_frames"] == 1, "the late whole board is caught"
    assert summary["clock_only_frames"] == 0
    rows = [r.get("board") for r in summary["per_frame"]]
    assert rows == ["attach", "resync", "delta", "whole"]
