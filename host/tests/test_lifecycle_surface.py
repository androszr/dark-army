"""Wire agreement between the daemon and the lifecycle client.

The phone's Usage screen draws the lifecycle report; the Mac panel does not
(`docs/context-panel.md`, History), so the phone's model is the one pinned."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from dark_army_daemon import board_lifecycle as m

REPO = Path(__file__).resolve().parents[2]
PHONE = REPO / "ios/BobPhone/LifecycleReport.swift"


def _coding_keys(source: str, struct: str) -> set:
    block = source.split(f"struct {struct}:", 1)
    assert len(block) == 2, f"{struct} is not in this file"
    body = block[1].split("enum CodingKeys", 1)[1].split("}", 1)[0]
    names = set()
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("case "):
            continue
        for part in line[len("case "):].split(","):
            part = part.strip()
            if "=" in part:
                names.add(part.split("=", 1)[1].strip().strip('"'))
            elif part:
                names.add(part)
    return names


@pytest.fixture
def payload():
    report = m.build_report(
        root="/p", card_id="", start=1, end=100, as_of=90, now=90,
        cards={"c1": {"title": "A", "root": "/p"}},
        episodes=[{
            "id": 1, "card_id": "c1", "kind": "queue", "cause": "queue",
            "started_at": 10, "ended_at": 20, "disposition": "completed",
            "coverage": "complete", "attempt_id": None, "gap_reasons": "",
            "provenance": "queue",
        }],
        spans=[{
            "card_id": "c1", "episode_id": 1, "utc_start": 10, "utc_end": 20,
            "category": "queue", "cause": "queue", "coverage": "complete",
        }],
        boundaries=[
            {"episode_id": 1, "ts": 10, "kind": "queue_started", "id": 1},
            {"episode_id": 1, "ts": 20, "kind": "completed", "id": 2,
             "disposition": "completed"},
        ],
        attempts=[], generation="1:2",
    )
    return {
        "envelope": set(report),
        "summary": set(report["summaries"]["queue"]),
        "card": set(report["cards"][0]) if report["cards"] else set(),
    }


@pytest.mark.parametrize("struct,section", [
    ("LifecycleReport", "envelope"),
    ("LifecycleSummary", "summary"),
    ("LifecycleDelayedCard", "card"),
])
def test_the_phone_names_no_key_the_daemon_never_sends(payload, struct, section):
    named = _coding_keys(PHONE.read_text(), struct)
    assert named <= payload[section] | {
        "measurements_available", "overlap_note", "histogram_buckets",
        "quantile_algorithm", "units", "retention_days", "tracking_since",
        "removed_note", "live", "title", "episodes", "cards", "next_offset",
        "cause_seconds", "carry_in", "open_age", "period_observed",
        "observed_seconds", "sample_count", "left_censored", "unknown",
        "partial", "cancelled", "open", "coverage", "min", "max", "p50", "p90",
        "buckets", "n", "N", "category", "cards", "retention",
        "expired_count", "expired_seconds",
    }, sorted(named - payload[section])


def test_overlap_note_is_the_daemon_sentence():
    assert m.OVERLAP_NOTE in PHONE.read_text()


def test_the_phone_carries_the_empty_state_copy():
    text = PHONE.read_text()
    assert "No observed episodes in this period" in text
    assert "No longer on the board" in text


