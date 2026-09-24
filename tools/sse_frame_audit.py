#!/usr/bin/env python3
"""Listen to Dark Army's live stream and say what it carried.

The panel's live stream (`/api/events`) was measured at forty frames and
about two megabytes in thirty seconds, most of it repeats. This tool is how
that is measured again, before and after, in numbers rather than
impressions: it listens for ``--seconds`` on the panel's own subscription
and prints, per frame, its size, its top-level keys and — for a board frame
— whether the board was whole or a per-card delta and which card ids rode.
The summary counts `frames`, `bytes_total`, `bytes_by_section`,
`clock_only_frames`, `board_full_frames`, `board_delta_frames`,
`board_delta_cards_max` and `agents_frames`, and `card_changes` names, per
card that moved, the keys that differed from the last copy of it the stream
carried — so "why did the board resend" is answered by the stream itself.

The first frame of a capture is the **attach frame** — always a whole
`state()`, the one every subscription opens with — and is counted as
`attach_frames`, never as a board resend. Attaching also clears the
daemon's per-card maps, so the first board change after it rides whole
once by design: that one is `board_resync_frames`. `board_full_frames`
counts every *other* whole board, which on a healthy `?cards=delta`
subscription is 0.

``--replay <path>`` summarises a saved file of frames (``data:`` lines or
bare JSON lines) with no network, which is what the tests drive; its first
frame is read as the attach frame too.

Reads only: the stream is an ungated loopback read, so no token is read and
nothing is written. Stdlib only.

    python3 tools/sse_frame_audit.py --seconds 30 --json
    python3 tools/sse_frame_audit.py --replay capture.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from urllib.parse import urlsplit

API_PORT = int(os.environ.get("BOB_COMPANION_API_PORT", "19874"))
DEFAULT_URL = (f"http://127.0.0.1:{API_PORT}"
               "/api/events?sections=changed&done=review&cards=delta")
# A frame carrying only these says nothing but the time.
CLOCK_ONLY_KEYS = frozenset({"generated_at", "reconciler_available",
                             "reconciler_error"})
MAX_LINE_BYTES = 64 * 1024 * 1024


def _differing_keys(old: dict, new: dict) -> list:
    """The keys whose values differ between two copies of one card, added
    and removed keys included, sorted."""
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def summarise(frames: list) -> dict:
    """Pure: the summary of a list of decoded frames, in arrival order."""
    summary = {
        "frames": 0,
        "bytes_total": 0,
        "bytes_by_section": {},
        "clock_only_frames": 0,
        "attach_frames": 0,
        "board_resync_frames": 0,
        "board_full_frames": 0,
        "board_delta_frames": 0,
        "board_delta_cards_max": 0,
        "agents_frames": 0,
        "card_changes": [],
        "per_frame": [],
    }
    known: dict = {}        # card id -> the last copy the stream carried
    seen_board = False
    resync_due = False
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        text = json.dumps(frame)
        size = len(text.encode("utf-8"))
        summary["frames"] += 1
        summary["bytes_total"] += size
        for key, value in frame.items():
            summary["bytes_by_section"][key] = (
                summary["bytes_by_section"].get(key, 0)
                + len(json.dumps(value).encode("utf-8")))
        keys = sorted(frame)
        row = {"frame": index, "bytes": size, "keys": keys}
        attach = summary["frames"] == 1
        if attach:
            summary["attach_frames"] += 1
            row["attach"] = True
            resync_due = True       # the attach cleared the per-card maps
        if not attach and set(frame) <= CLOCK_ONLY_KEYS:
            summary["clock_only_frames"] += 1
            row["clock_only"] = True
        if "agents" in frame:
            summary["agents_frames"] += 1
        board = frame.get("board")
        if isinstance(board, dict):
            cards = [c for c in board.get("cards") or []
                     if isinstance(c, dict)]
            delta = board.get("cards_delta") is True
            ids = [str(c.get("id") or "") for c in cards]
            changed = []
            for card, card_id in zip(cards, ids):
                previous = known.get(card_id)
                if previous is None:
                    if seen_board:
                        changed.append({"id": card_id, "keys": ["(new)"]})
                else:
                    moved = _differing_keys(previous, card)
                    if moved:
                        changed.append({"id": card_id, "keys": moved})
                known[card_id] = card
            if delta:
                order = [str(i) for i in board.get("card_order") or []]
                removed = [i for i in known if i not in order]
                for card_id in removed:
                    known.pop(card_id, None)
                    changed.append({"id": card_id, "keys": ["(removed)"]})
                resync_due = False
                summary["board_delta_frames"] += 1
                summary["board_delta_cards_max"] = max(
                    summary["board_delta_cards_max"], len(cards))
                row["board"] = "delta"
            else:
                removed = [i for i in known if i not in ids]
                for card_id in removed:
                    known.pop(card_id, None)
                    if seen_board:
                        changed.append({"id": card_id, "keys": ["(removed)"]})
                if attach:
                    row["board"] = "attach"
                elif resync_due:
                    resync_due = False
                    summary["board_resync_frames"] += 1
                    row["board"] = "resync"
                else:
                    summary["board_full_frames"] += 1
                    row["board"] = "whole"
            row["cards"] = ids
            row["changed"] = changed
            for entry in changed:
                summary["card_changes"].append(dict(entry, frame=index))
            seen_board = True
        summary["per_frame"].append(row)
    return summary


def parse_lines(lines) -> list:
    """Decoded frames from `data:` lines or bare JSON lines; blank lines,
    `:` comments and undecodable lines are skipped."""
    frames = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        try:
            frames.append(json.loads(line))
        except ValueError:
            continue
    return frames


def capture(url: str, seconds: float) -> list:
    """Listen on `url` for `seconds`; the `data:` lines, decoded."""
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    deadline = time.monotonic() + seconds
    sock = socket.create_connection((host, port), timeout=5)
    lines: list = []
    try:
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                      "Accept: text/event-stream\r\n\r\n").encode("latin-1"))
        buffer = b""
        head_done = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(remaining, 1.0))
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
            if not head_done:
                end = buffer.find(b"\r\n\r\n")
                if end < 0:
                    continue
                status = buffer.split(b"\r\n", 1)[0].decode("latin-1")
                if " 200 " not in status + " ":
                    raise SystemExit(f"the stream refused: {status}")
                buffer = buffer[end + 4:]
                head_done = True
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.startswith(b"data:"):
                    lines.append(line.decode("utf-8", "replace"))
            if len(buffer) > MAX_LINE_BYTES:
                raise SystemExit("a frame larger than the tool will hold")
    finally:
        sock.close()
    return parse_lines(lines)


def _print_report(summary: dict) -> None:
    for row in summary["per_frame"]:
        line = f"#{row['frame']:<4} {row['bytes']:>9}B  {','.join(row['keys'])}"
        if row.get("clock_only"):
            line += "  [clock only]"
        if "board" in row:
            line += f"  board={row['board']} cards={len(row['cards'])}"
            if row["changed"]:
                line += " changed=" + " ".join(
                    f"{c['id']}({'/'.join(c['keys'])})" for c in row["changed"])
        print(line)
    shown = {k: v for k, v in summary.items() if k != "per_frame"}
    print(json.dumps(shown, indent=2, sort_keys=True))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--json", action="store_true",
                        help="print the summary as one JSON object")
    parser.add_argument("--replay", metavar="PATH",
                        help="summarise a saved file of frames, no network")
    args = parser.parse_args(argv)
    if args.replay:
        with open(args.replay, encoding="utf-8") as fh:
            frames = parse_lines(fh)
    else:
        frames = capture(args.url, args.seconds)
    summary = summarise(frames)
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        _print_report(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
