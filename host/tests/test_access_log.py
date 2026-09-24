# host/tests/test_access_log.py
"""The phone doors' access log: the store, the sentence, the fences and the
burst detector's arithmetic. Seams: `AccessLog(path=tmp)` and a bare
`BurstDetector()` handed an explicit `now`."""

from __future__ import annotations

import json
import time

import pytest

from dark_army_daemon import access_log, event_log, paths, relay
from dark_army_daemon.access_log import (
    ALERT_COOLDOWN_SECONDS, ALERT_FLOOR_SECONDS, ALERT_TTL_SECONDS,
    BURST_THRESHOLD,
    BURST_WINDOW_SECONDS, DOORS, FORBIDDEN_KEYS, HOP_KEYS, KINDS,
    MAX_ALERT_ENTRIES, MAX_ENTRIES,
    MAX_PEER_CHARS, MAX_TIMING_ENTRIES, MAX_TRACKED_PEERS, PRUNE_SLACK,
    PUBLISHED_KEYS,
    REASON_WORDS, REASONS, RETENTION_SECONDS, WEIGHTS, AccessLog,
    BurstDetector, sentence,
)


@pytest.fixture
def log(tmp_path):
    store = AccessLog(path=tmp_path / "a.jsonl")
    store.open()
    return store


# --- the vocabularies ------------------------------------------------------------


def test_the_path_is_the_state_directorys_and_private():
    assert paths.ACCESS_LOG_NAME == "access-log.jsonl"
    assert paths.ACCESS_LOG_NAME in paths._PRIVATE_FILES
    assert AccessLog().path == paths.ACCESS_LOG_PATH


def test_every_reason_has_words_and_a_sentence():
    for reason in REASONS:
        assert REASON_WORDS[reason]
        text = sentence("refusal", door="lan", peer="10.0.0.9", reason=reason)
        assert REASON_WORDS[reason] in text
        assert "10.0.0.9" in text


def test_the_five_shared_reasons_use_the_relays_own_words():
    for code, words in relay.REFUSAL_WORDS.items():
        assert code in REASONS
        assert REASON_WORDS[code] == words


def test_every_weight_is_named_and_only_the_keyholder_reasons_are_zero():
    assert set(WEIGHTS) == set(REASONS)
    # `busy` joins `rate`: both fire before any seal is opened, on the
    # accept or the bucket alone, so neither says anything about who knocked.
    assert {r for r, w in WEIGHTS.items() if w == 0} == {"ctr", "ts", "kind",
                                                          "rate", "busy"}


def test_the_fence_widens_the_diarys():
    for key in event_log.FORBIDDEN_KEYS:
        assert key in FORBIDDEN_KEYS
    for key in ("chan", "channel", "frame", "wire", "code", "home_key",
                "relay_key", "digest", "claim"):
        assert key in FORBIDDEN_KEYS
    assert len(FORBIDDEN_KEYS) == len(set(FORBIDDEN_KEYS))


def test_the_sentences():
    assert sentence("refusal", door="lan", peer="10.0.0.9", reason="plaintext") \
        == "the Wi-Fi door refused 10.0.0.9: a request in the old unsealed shape"
    assert sentence("refusal", door="relay", peer="dev1", reason="seal") \
        == "the relay mailbox refused dev1: an envelope failed verification"
    assert sentence("burst", door="pairing", peer="10.0.0.9", count=5,
                    window_seconds=600) \
        == "5 refused attempts from 10.0.0.9 at the pairing door in 10 minutes"
    assert sentence("burst", door="upload", peer="", count=5, window_seconds=45) \
        == "5 refused attempts from an unknown source at the upload door in 1 minute"
    assert sentence("burst_ack", peer="10.0.0.9") \
        == "the alert about 10.0.0.9 was acknowledged"
    assert sentence("nonsense") == ""


# --- the store ---------------------------------------------------------------------


def test_a_line_carries_exactly_the_published_keys(log):
    entry = log.append("refusal", door="lan", peer="10.0.0.9", reason="plaintext")
    assert entry is not None
    assert tuple(entry) == PUBLISHED_KEYS
    on_disk = json.loads(log.path.read_text().splitlines()[0])
    assert tuple(on_disk) == PUBLISHED_KEYS
    assert on_disk["text"] == entry["text"]
    assert on_disk["kind"] == "refusal"
    assert on_disk["device_id"] == ""


def test_append_refuses_an_unknown_kind_door_or_reason(log):
    assert log.append("knock", door="lan", peer="p", reason="plaintext") is None
    assert log.append("refusal", door="garage", peer="p", reason="plaintext") is None
    assert log.append("refusal", door="lan", peer="p", reason="tired") is None
    assert log.append("burst", door="garage", peer="p", count=5) is None
    assert log.append("burst_ack", peer="p") is None
    assert len(log) == 0
    assert not log.path.exists()


def test_append_refuses_a_forbidden_key_at_any_depth(log):
    for key in ("chan", "code", "key", "claim", "channel", "home_key"):
        assert log.append("refusal", door="lan", peer="p", reason="seal",
                          **{key: "x"}) is None
        assert log.append("refusal", door="lan", peer="p", reason="seal",
                          extra={"deep": [{key: "x"}]}) is None
    assert len(log) == 0


def test_the_peer_is_clamped_and_kept_to_one_printable_line(log):
    entry = log.append("refusal", door="lan", peer="a" * 200 + "\n\x00b",
                       reason="plaintext")
    assert len(entry["peer"]) == MAX_PEER_CHARS
    assert entry["peer"] == "a" * MAX_PEER_CHARS
    assert "\n" not in entry["text"]


def test_every_kind_is_appendable(log):
    assert set(KINDS) == {"refusal", "burst", "burst_ack", "burst_more",
                          "timing"}
    assert set(DOORS) == {"lan", "pairing", "upload", "relay", "ws"}
    burst = log.append("burst", id="abc123", door="lan", peer="p", count=5,
                       window_seconds=600)
    assert burst["id"] == "abc123"
    assert burst["count"] == 5
    ack = log.append("burst_ack", alert_id="abc123", peer="p")
    assert ack["alert_id"] == "abc123"
    assert ack["reason"] == ""
    more = log.append("burst_more", door="lan", peer="q", alert_id="abc123",
                      count=10, peers=2)
    assert more["alert_id"] == "abc123"
    # `peers` is a `burst_more` figure alone; every other kind carries 0.
    assert (burst["peers"], ack["peers"], more["peers"]) == (0, 0, 2)
    timing = log.append("timing", door="relay", peer="dev-1",
                        device_id="dev-1", count=3, window_seconds=600,
                        hops={"n": 3, "dwell_p50": 1.5})
    assert timing["kind"] == "timing"
    assert timing["hops"] == {"n": 3.0, "dwell_p50": 1.5}
    # The socket door files the same line; its sentence names its own hops
    # (no mailbox: opened, handled, answered) and begins `socket:`.
    socket = log.append("timing", door="ws", peer="dev-1",
                        device_id="dev-1", count=12, window_seconds=600,
                        hops={"n": 12, "open_p50": 0.0004, "open_p90": 0.001,
                              "run_p50": 0.005, "answer_p50": 0.012})
    assert socket["door"] == "ws"
    assert socket["text"] == (
        "socket: 12 requests in 10 minutes — opened in under 0.001 s typical, "
        "0.001 s slow; handled in 0.005 s; answered in 0.012 s")
    assert "picked up" not in socket["text"] and "sealed in" not in socket["text"]
    # `hops` is a `timing` figure alone; every other kind carries `{}`.
    assert (burst["hops"], ack["hops"], more["hops"]) == ({}, {}, {})


# --- the fold line and the merged sentence ----------------------------------------


def test_the_burst_more_line_and_its_sentence(log):
    """The line's shape and words. An orphan fold — naming no `burst` on the
    file — is pruned on the spot, so the alert it names is written first."""
    log.append("burst", id="a1", door="lan", peer="A", count=5,
               window_seconds=600)
    entry = log.append("burst_more", door="lan", peer="B", alert_id="a1",
                       count=10, peers=2)
    assert entry is not None
    assert tuple(entry) == PUBLISHED_KEYS
    assert entry["peers"] == 2
    assert entry["count"] == 10
    assert entry["alert_id"] == "a1"
    assert entry["reason"] == ""
    assert entry["text"] == ("a burst from B was folded into the open alert: "
                             "10 refused attempts from 2 sources so far")
    one = log.append("burst_more", door="relay", peer="dev1", alert_id="a1",
                     count=5, peers=1)
    assert one["text"].endswith("5 refused attempts from 1 source so far")
    # Refused: no alert named, an unknown door, a forbidden key.
    assert log.append("burst_more", door="lan", peer="B") is None
    assert log.append("burst_more", door="garage", peer="B", alert_id="a1") is None
    assert log.append("burst_more", door="lan", peer="B", alert_id="a1",
                      chan="x") is None
    # The burst and the newest fold: a superseded fold leaves memory on the
    # next prune (the file keeps it until the usual rewrite trigger).
    assert [r["kind"] for r in log.recent()] == ["burst_more", "burst"]
    assert log.recent()[0]["id"] == one["id"]
    assert sum(1 for _ in log.path.open()) == 3
    # A `peers` that is not a number is 0, never a refusal (count's rule).
    odd = log.append("burst_more", door="lan", peer="B", alert_id="a1",
                     count="many", peers="lots")
    assert (odd["count"], odd["peers"]) == (0, 0)
    # An orphan fold is dropped at once.
    assert log.append("burst_more", door="lan", peer="B", alert_id="nobody",
                      count=5, peers=1) is not None
    assert [r["alert_id"] for r in log.recent() if r["kind"] == "burst_more"] \
        == ["a1"]


def test_the_burst_sentence_with_other_sources():
    plain = sentence("burst", door="lan", peer="fe80::1", count=5,
                     window_seconds=600)
    assert plain == "5 refused attempts from fe80::1 at the Wi-Fi door in 10 minutes"
    # `peers` absent, 1 or unparseable: today's sentence, byte-identical.
    assert sentence("burst", door="lan", peer="fe80::1", count=5,
                    window_seconds=600, peers=1) == plain
    assert sentence("burst", door="lan", peer="fe80::1", count=5,
                    window_seconds=600, peers="x") == plain
    assert sentence("burst", door="lan", peer="fe80::1", count=5,
                    window_seconds=600, peers=0) == plain
    assert sentence("burst", door="lan", peer="fe80::1", count=10,
                    window_seconds=600, peers=2) \
        == "10 refused attempts from fe80::1 and 1 other source at the Wi-Fi door"
    assert sentence("burst", door="lan", peer="fe80::1", count=1000,
                    window_seconds=600, peers=200) \
        == "1000 refused attempts from fe80::1 and 199 other sources at the Wi-Fi door"
    # No window clause once folds are in: they span past `window_seconds`.
    assert "minute" not in sentence("burst", door="relay", peer="d", count=15,
                                    window_seconds=600, peers=3)


def test_a_capped_peers_figure_reads_as_at_least():
    """At `MAX_FOLDED_PEERS` the daemon stopped counting distinct sources,
    so the figure is a floor and both sentences say so; one below the bound
    is exact and byte-identical to today's wording."""
    bound = access_log.MAX_FOLDED_PEERS
    assert sentence("burst", door="lan", peer="fe80::1", count=6000,
                    window_seconds=600, peers=bound) \
        == (f"6000 refused attempts from fe80::1 and at least {bound - 1} "
            f"other sources at the Wi-Fi door")
    assert sentence("burst", door="lan", peer="fe80::1", count=6000,
                    window_seconds=600, peers=bound + 7) \
        == (f"6000 refused attempts from fe80::1 and at least {bound + 6} "
            f"other sources at the Wi-Fi door")
    assert sentence("burst", door="lan", peer="fe80::1", count=6000,
                    window_seconds=600, peers=bound - 1) \
        == (f"6000 refused attempts from fe80::1 and {bound - 2} "
            f"other sources at the Wi-Fi door")
    assert sentence("burst_more", door="lan", peer="B", alert_id="a1",
                    count=6000, peers=bound) \
        == (f"a burst from B was folded into the open alert: 6000 refused "
            f"attempts from at least {bound} sources so far")
    assert sentence("burst_more", door="lan", peer="B", alert_id="a1",
                    count=6000, peers=bound - 1) \
        == (f"a burst from B was folded into the open alert: 6000 refused "
            f"attempts from {bound - 1} sources so far")
    # The journal's own line carries the same wording.
    assert "at least" not in sentence("burst_more", door="lan", peer="B",
                                      alert_id="a1", count=10, peers=2)


def test_open_alerts_merges_the_newest_fold_and_leaves_the_line_alone(log):
    now = time.time()
    burst = log.append("burst", id="a1", door="lan", peer="A", count=5,
                       window_seconds=600, ts=now - 100)
    plain = log.append("burst", id="a2", door="relay", peer="dev1", count=5,
                       window_seconds=600, ts=now - 90)
    log.append("burst_more", door="lan", peer="B", alert_id="a1", count=10,
               peers=2, ts=now - 50)
    second = log.append("burst_more", door="pairing", peer="C", alert_id="a1",
                        count=15, peers=3, ts=now - 20)
    rows = log.open_alerts(now)
    assert [r["id"] for r in rows] == ["a1", "a2"]
    merged = rows[0]
    assert merged["count"] == 15
    assert merged["peers"] == 3
    assert merged["last_seen"] == second["ts"]
    assert merged["door"] == "lan"
    assert merged["text"] == ("15 refused attempts from A and 2 other sources "
                              "at the Wi-Fi door")
    # A row with no fold: peers 1, last_seen its own ts, text untouched.
    assert rows[1]["peers"] == 1
    assert rows[1]["last_seen"] == plain["ts"]
    assert rows[1]["text"] == plain["text"]
    assert rows[1]["count"] == 5
    # The journal itself is untouched: the burst line's original text is
    # still there beside the newest fold (the superseded fold left memory on
    # the append's prune; on disk it stays until the rewrite trigger, and
    # `open()` reprunes to the same picture).
    recent = log.recent()
    assert [r["kind"] for r in recent] == ["burst_more", "burst", "burst"]
    assert recent[0]["id"] == second["id"]
    stored = [r for r in recent if r["id"] == "a1"][0]
    assert stored["text"] == burst["text"]
    assert stored["count"] == 5
    assert "peers" in stored and stored["peers"] == 0
    on_disk = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [r["kind"] for r in on_disk] == ["burst", "burst", "burst_more",
                                             "burst_more"]
    assert [r for r in on_disk if r["id"] == "a1"][0]["text"] == burst["text"]
    again = AccessLog(path=log.path)
    again.open()
    assert [r["kind"] for r in again.recent()] == ["burst_more", "burst", "burst"]
    assert again.open_alerts(now)[0]["count"] == 15


def test_a_fold_into_an_acked_alert_is_on_the_log_and_off_the_list(log):
    now = time.time()
    log.append("burst", id="a1", door="lan", peer="A", count=5,
               window_seconds=600, ts=now - 100)
    log.append("burst_ack", alert_id="a1", peer="A", ts=now - 50)
    more = log.append("burst_more", door="lan", peer="B", alert_id="a1",
                      count=10, peers=2, ts=now - 10)
    assert more is not None
    assert log.open_alerts(now) == []
    assert log.recent()[0]["kind"] == "burst_more"
    assert log.recent()[0]["alert_id"] == "a1"


def test_fold_lines_never_push_an_open_alert_off_the_belt(log):
    """Only the newest fold per alert survives a prune, and a surviving
    fold is never counted against `MAX_ALERT_ENTRIES`: the flood leaves the
    older open alert where it was and the file bounded."""
    now = time.time()
    log.append("burst", id="keep", door="lan", peer="K", count=5,
               window_seconds=600, ts=now - 5000)
    log.append("burst", id="flood", door="lan", peer="F", count=5,
               window_seconds=600, ts=now - 4000)
    n = MAX_ALERT_ENTRIES + 50
    for i in range(n):
        log.append("burst_more", door="lan", peer=f"fe80::{i:x}", alert_id="flood",
                   count=5 * (i + 2), peers=i + 2, ts=now - 3000 + i)
    ids = [r["id"] for r in log.open_alerts(now)]
    assert ids == ["keep", "flood"]
    folds = [r for r in log.recent() if r["kind"] == "burst_more"]
    assert len(folds) == 1
    assert folds[0]["peers"] == n + 1
    assert folds[0]["count"] == 5 * (n + 1)
    assert log.open_alerts(now)[1]["count"] == 5 * (n + 1)
    assert log.open_alerts(now)[1]["peers"] == n + 1
    assert len(log) <= MAX_ALERT_ENTRIES + 1 + 1
    assert len(log) == 3
    # The file only shrinks on the usual rewrite trigger, but reopening
    # reprunes to the same three lines.
    again = AccessLog(path=log.path)
    again.open()
    assert len(again) == 3
    assert [r["kind"] for r in again.recent()] == ["burst_more", "burst", "burst"]


def test_the_belt_evicts_a_fold_with_its_alert(log):
    """250 open bursts, each with one fold line: the belt takes the oldest
    fifty bursts and their folds go in the same pass — no fold survives
    naming an evicted alert, and the fold lines spent none of the belt."""
    now = time.time()
    for i in range(250):
        log.append("burst", id=f"b{i}", door="lan", peer=str(i), count=5,
                   window_seconds=600, ts=now - 6000 + 2 * i)
        log.append("burst_more", door="lan", peer=f"m{i}", alert_id=f"b{i}",
                   count=10, peers=2, ts=now - 6000 + 2 * i + 1)
    rows = log.recent()
    burst_ids = {r["id"] for r in rows if r["kind"] == "burst"}
    fold_refs = {r["alert_id"] for r in rows if r["kind"] == "burst_more"}
    assert len(burst_ids) == MAX_ALERT_ENTRIES
    assert fold_refs <= burst_ids
    assert fold_refs == burst_ids
    assert "b0" not in burst_ids and "b49" not in burst_ids
    assert "b50" in burst_ids and "b249" in burst_ids
    assert len(log) == 2 * MAX_ALERT_ENTRIES
    # Every surviving open alert reads its fold's totals.
    assert all(r["count"] == 10 and r["peers"] == 2 for r in log.open_alerts(now))


def test_an_orphan_fold_line_is_dropped_on_open(tmp_path):
    path = tmp_path / "a.jsonl"
    now = time.time()
    rows = [
        {"id": "b1", "ts": now - 100, "kind": "burst", "door": "lan", "peer": "A",
         "reason": "", "device_id": "", "text": "t", "count": 5,
         "window_seconds": 600, "alert_id": "", "peers": 0},
        {"id": "m1", "ts": now - 50, "kind": "burst_more", "door": "lan",
         "peer": "B", "reason": "", "device_id": "", "text": "t", "count": 10,
         "window_seconds": 600, "alert_id": "b1", "peers": 2},
        {"id": "m2", "ts": now - 40, "kind": "burst_more", "door": "lan",
         "peer": "C", "reason": "", "device_id": "", "text": "t", "count": 10,
         "window_seconds": 600, "alert_id": "gone", "peers": 2},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    store = AccessLog(path=path)
    store.open()
    assert [r["id"] for r in store.recent()] == ["m1", "b1"]
    on_disk = [json.loads(line)["id"] for line in path.read_text().splitlines()]
    assert on_disk == ["b1", "m1"]
    assert store.open_alerts(now)[0]["count"] == 10


def test_recent_is_newest_first_and_honours_since_and_limit(log):
    now = time.time()
    for i in range(5):
        log.append("refusal", door="lan", peer=f"10.0.0.{i}", reason="plaintext",
                   ts=now - 100 + i)
    rows = log.recent()
    assert [r["peer"] for r in rows] == [f"10.0.0.{i}" for i in (4, 3, 2, 1, 0)]
    assert [r["peer"] for r in log.recent(limit=2)] == ["10.0.0.4", "10.0.0.3"]
    assert [r["peer"] for r in log.recent(since=now - 100 + 2)] \
        == ["10.0.0.4", "10.0.0.3"]


def test_thirty_day_retention_on_read_and_on_write(tmp_path, log):
    old = time.time() - RETENTION_SECONDS - 5
    log.append("refusal", door="lan", peer="old", reason="plaintext", ts=old)
    assert log.recent() == []
    log.append("refusal", door="lan", peer="new", reason="plaintext")
    assert [r["peer"] for r in log.recent()] == ["new"]
    reopened = AccessLog(path=log.path)
    reopened.open()
    assert [r["peer"] for r in reopened.recent()] == ["new"]
    assert '"peer": "old"' not in log.path.read_text()


def test_the_cap_and_the_prune_slack(log):
    for i in range(MAX_ENTRIES + PRUNE_SLACK - 1):
        log.append("refusal", door="lan", peer=str(i), reason="plaintext")
    assert len(log) == MAX_ENTRIES
    assert len(log.path.read_text().splitlines()) == MAX_ENTRIES + PRUNE_SLACK - 1
    log.append("refusal", door="lan", peer="last", reason="plaintext")
    assert len(log) == MAX_ENTRIES
    assert len(log.path.read_text().splitlines()) == MAX_ENTRIES


def test_a_flood_of_refusals_cannot_push_an_open_alert_off_the_end(log):
    """The cap evicts refusals only. One burst line followed by more than
    MAX_ENTRIES refusals from the same peer must leave the alert open —
    otherwise the attacker closes their own alert and the ack answers
    "that alert is not open"."""
    now = time.time()
    log.append("burst", id="held", door="lan", peer="10.0.0.9", count=5,
               window_seconds=600, ts=now - 30)
    for i in range(MAX_ENTRIES + PRUNE_SLACK + 10):
        log.append("refusal", door="lan", peer="10.0.0.9", reason="plaintext")
    assert len(log) == MAX_ENTRIES
    assert [r["id"] for r in log.open_alerts(now)] == ["held"]
    # The burst survived the rewrite on disk too.
    reopened = AccessLog(path=log.path)
    reopened.open()
    assert [r["id"] for r in reopened.open_alerts(now)] == ["held"]
    # Only an ack closes it, and the ack survives the same flood.
    assert log.append("burst_ack", alert_id="held", peer="10.0.0.9")
    for i in range(PRUNE_SLACK + 10):
        log.append("refusal", door="lan", peer="10.0.0.9", reason="plaintext")
    assert log.open_alerts(now) == []
    kinds = {r["kind"] for r in log.recent(limit=MAX_ENTRIES + 10)}
    assert kinds == {"refusal", "burst", "burst_ack"}


def test_alert_lines_have_their_own_belt(log):
    """The cap spares alert lines, so a second bound keeps the file finite:
    past MAX_ALERT_ENTRIES the oldest alert lines go, a burst before the
    ack that closed it."""
    now = time.time()
    for i in range(MAX_ALERT_ENTRIES + 5):
        log.append("burst", id=f"b{i}", door="lan", peer=str(i), count=5,
                   window_seconds=600, ts=now - 1000 + i)
    ids = [r["id"] for r in log.open_alerts(now)]
    assert len(ids) == MAX_ALERT_ENTRIES
    assert ids[0] == "b5" and ids[-1] == f"b{MAX_ALERT_ENTRIES + 4}"
    # A refusal is never evicted by the alert belt.
    log.append("refusal", door="lan", peer="r", reason="plaintext")
    assert [r["kind"] for r in log.recent(limit=1)] == ["refusal"]


def test_the_belt_evicts_closed_alerts_before_an_open_one(log):
    """250 bursts on the file, 99 of them acknowledged, the oldest open one
    in date: the closed pairs go first and the oldest open alert survives;
    an expired burst goes ahead of an open one too; only with nothing
    closed left does the belt fall back to the oldest open one. The file
    stays bounded throughout."""
    now = time.time()
    for i in range(1, 100):
        log.append("burst", id=f"b{i}", door="lan", peer=str(i), count=5,
                   window_seconds=600, ts=now - 6000 + i)
        log.append("burst_ack", alert_id=f"b{i}", peer=str(i),
                   ts=now - 6000 + i + 0.5)
    log.append("burst", id="oldest-open", door="lan", peer="keep", count=5,
               window_seconds=600, ts=now - 5000)
    for i in range(100, 250):
        log.append("burst", id=f"b{i}", door="lan", peer=str(i), count=5,
                   window_seconds=600, ts=now - 5000 + i)
    # 250 bursts + 99 acks = 349 alert lines, 149 over the belt; the 99
    # closed pairs (198 lines) cover it, so every open burst is still there.
    open_ids = [r["id"] for r in log.open_alerts(now)]
    assert open_ids[0] == "oldest-open"
    assert len(open_ids) == 250 - 99
    lines = [r for r in log.recent() if r["kind"] != "refusal"]
    assert len(lines) <= MAX_ALERT_ENTRIES
    assert sum(1 for _ in log.path.open()) < MAX_ENTRIES + PRUNE_SLACK
    # 151 open bursts and 49 closed lines remain. An expired burst goes
    # ahead of an open one: 30 more open bursts plus the expired one is
    # 31 over, covered by the 49 + 1 closed lines.
    log.append("burst", id="expired", door="lan", peer="x", count=5,
               window_seconds=600, ts=now - ALERT_TTL_SECONDS - 5)
    for i in range(250, 250 + 30):
        log.append("burst", id=f"b{i}", door="lan", peer=str(i), count=5,
                   window_seconds=600, ts=now - 4000 + i)
    assert log.open_alerts(now)[0]["id"] == "oldest-open"
    assert len(log.open_alerts(now)) == 181
    # 19 closed lines and the expired burst left: 40 more open bursts
    # exhaust those 20 first — the expired one goes though it is newer on
    # the file than every open one — and only then does the belt fall
    # back to the oldest open one. Every line left is an open alert.
    for i in range(300, 300 + 40):
        log.append("burst", id=f"b{i}", door="lan", peer=str(i), count=5,
                   window_seconds=600, ts=now - 3000 + i)
    assert "expired" not in [r["id"] for r in log.recent()]
    assert log.open_alerts(now)[0]["id"] != "oldest-open"
    assert len(log.open_alerts(now)) == MAX_ALERT_ENTRIES == len(log)
    assert len(log) <= MAX_ALERT_ENTRIES
    assert sum(1 for _ in log.path.open()) < MAX_ENTRIES + PRUNE_SLACK


def test_open_tolerates_a_torn_tail_and_unknown_keys(tmp_path):
    path = tmp_path / "a.jsonl"
    good = {"id": "x", "ts": time.time(), "kind": "refusal", "door": "lan",
            "peer": "p", "reason": "seal", "text": "t", "device_id": "",
            "count": 0, "window_seconds": 0, "alert_id": "", "stray": 1}
    path.write_text(json.dumps(good) + "\n" + '{"id": "half", "ts"')
    store = AccessLog(path=path)
    store.open()
    assert [r["id"] for r in store.recent()] == ["x"]
    assert "stray" not in store.recent()[0]


def test_open_alerts_excludes_acknowledged_and_expired_bursts(log):
    now = time.time()
    log.append("burst", id="fresh", door="lan", peer="a", count=5,
               window_seconds=600, ts=now - 10)
    log.append("burst", id="acked", door="lan", peer="b", count=5,
               window_seconds=600, ts=now - 5)
    log.append("burst", id="stale", door="lan", peer="c", count=5,
               window_seconds=600, ts=now - ALERT_TTL_SECONDS - 1)
    log.append("burst_ack", alert_id="acked", peer="b")
    assert [r["id"] for r in log.open_alerts(now)] == ["fresh"]
    log.append("burst_ack", alert_id="fresh", peer="a")
    assert log.open_alerts(now) == []


def test_a_closed_store_appends_nothing(log):
    log.close()
    assert log.append("refusal", door="lan", peer="p", reason="seal") is None


# --- the timing lines ---------------------------------------------------------


def _timing_hops(**overrides) -> dict:
    hops = {k: 0.0 for k in HOP_KEYS}
    hops["n"] = 3
    hops.update(overrides)
    return hops


def test_a_timing_line_carries_the_closed_hop_set_and_a_stray_key_is_refused(log):
    entry = log.append("timing", door="relay", peer="dev-1", device_id="dev-1",
                       count=74, window_seconds=600,
                       hops=_timing_hops(dwell_p50=1.9, dwell_p90=3.8,
                                         run_p50=0.04, answer_p50=0.3))
    assert entry is not None
    assert tuple(entry) == PUBLISHED_KEYS
    assert set(entry["hops"]) == set(HOP_KEYS)
    assert all(isinstance(v, float) for v in entry["hops"].values())
    assert entry["text"] == ("relay: 74 requests in 10 minutes — picked up in "
                             "1.9 s typical, 3.8 s slow; handled in 0.04 s; "
                             "answered in 0.3 s")
    assert log.append("timing", door="relay", peer="dev-1", count=1,
                      hops=_timing_hops(mailbox_url="x")) is None
    assert log.append("timing", door="relay", peer="dev-1", count=1,
                      hops=_timing_hops(dwell_p50="slow")) is None
    assert log.append("timing", door="relay", peer="dev-1", count=1) is None
    assert log.append("timing", door="garage", peer="dev-1", count=1,
                      hops=_timing_hops()) is None
    # A forbidden name inside `hops` is a stray key *and* a forbidden key.
    assert log.append("timing", door="relay", peer="dev-1", count=1,
                      hops={"key": 1.0}) is None
    assert len(log) == 1


def test_the_home_doors_timing_sentence_names_its_own_hops():
    text = sentence("timing", door="lan", count=12, window_seconds=600,
                    hops=_timing_hops(open_p50=0.002, open_p90=0.004,
                                      run_p50=0.03, seal_p50=0.01))
    assert text == ("Wi-Fi: 12 requests in 10 minutes — opened in 0.002 s "
                    "typical, 0.004 s slow; handled in 0.03 s; sealed in 0.01 s")
    assert sentence("timing", door="relay", count=1, window_seconds=60,
                    hops=_timing_hops()) == (
        "relay: 1 request in 1 minute — picked up in under 0.001 s typical, "
        "under 0.001 s slow; handled in under 0.001 s; answered in under 0.001 s")
    assert sentence("timing", door="relay", count=2, window_seconds=600,
                    hops=_timing_hops(dwell_p50=123.4, dwell_p90=12.34)) \
        .startswith("relay: 2 requests in 10 minutes — picked up in 123 s "
                    "typical, 12 s slow")
    # `.2g` would say `1e+02 s` from 99.5 up; whole seconds instead.
    assert sentence("timing", door="relay", count=1, window_seconds=600,
                    hops=_timing_hops(dwell_p50=99.6, dwell_p90=250.0)) \
        .startswith("relay: 1 request in 10 minutes — picked up in 100 s "
                    "typical, 250 s slow")
    assert "e+" not in sentence("timing", door="lan", count=1,
                                hops=_timing_hops(open_p50=99.5, run_p50=999.9))


def test_recent_hides_timing_lines_unless_asked(log):
    log.append("refusal", door="lan", peer="p", reason="plaintext")
    log.append("timing", door="relay", peer="dev-1", count=3,
               window_seconds=600, hops=_timing_hops())
    log.append("burst", id="b1", door="lan", peer="p", count=5,
               window_seconds=600)
    assert [r["kind"] for r in log.recent()] == ["burst", "refusal"]
    assert [r["kind"] for r in log.recent(include_timing=True)] \
        == ["burst", "timing", "refusal"]
    # `limit` bounds the refusal and alert lines; a rollup rides beside it.
    assert [r["kind"] for r in log.recent(limit=1, include_timing=True)] \
        == ["burst", "timing"]
    assert [r["kind"] for r in log.recent(limit=1, include_timing=True,
                                          timing_limit=0)] == ["burst"]
    # The rollup survives a reopen and stays hidden by default there too.
    reopened = AccessLog(path=log.path)
    reopened.open()
    assert [r["kind"] for r in reopened.recent()] == ["burst", "refusal"]
    assert [r["hops"]["n"] for r in reopened.recent(include_timing=True)
            if r["kind"] == "timing"] == [3.0]


def test_rollups_ride_beside_the_limit_and_never_crowd_out_a_refusal(log):
    """A phone reads the newest rows with a limit; with rollups folded into
    that limit, a few days of ten-minute lines would push every refusal
    out of the window. Rollups have their own `timing_limit` instead."""
    now = time.time()
    log.append("refusal", door="lan", peer="old", reason="plaintext",
               ts=now - 9000)
    for i in range(MAX_TIMING_ENTRIES):
        log.append("timing", door="relay", peer="dev-1", count=1,
                   window_seconds=600, hops=_timing_hops(), ts=now - 8000 + i)
    rows = log.recent(limit=5, include_timing=True)
    kinds = [r["kind"] for r in rows]
    assert kinds.count("refusal") == 1
    assert kinds.count("timing") == access_log.TIMING_READ_LIMIT
    assert [float(r["ts"]) for r in rows] == sorted(
        (float(r["ts"]) for r in rows), reverse=True)
    assert len(log.recent(limit=5, include_timing=True, timing_limit=3)) == 4
    # `limit` still bounds the refusal and alert lines alone.
    for i in range(10):
        log.append("refusal", door="lan", peer=str(i), reason="plaintext")
    rows = log.recent(limit=5, include_timing=True)
    assert [r["kind"] for r in rows].count("refusal") == 5
    assert len(log.recent(limit=5)) == 5
    # `since` applies to both.
    assert all(r["kind"] == "refusal"
               for r in log.recent(since=now - 100, include_timing=True))


def test_the_timing_belt_evicts_oldest_first_and_nothing_else(log):
    now = time.time()
    log.append("burst", id="held", door="lan", peer="p", count=5,
               window_seconds=600, ts=now - 5000)
    log.append("burst_ack", alert_id="held", peer="p", ts=now - 4999)
    log.append("refusal", door="lan", peer="old", reason="plaintext",
               ts=now - 4998)
    for i in range(MAX_TIMING_ENTRIES + 5):
        log.append("timing", door="relay", peer="dev-1", count=i,
                   window_seconds=600, hops=_timing_hops(), ts=now - 4000 + i)
    rows = log.recent(include_timing=True, timing_limit=MAX_TIMING_ENTRIES + 10)
    timing = [r for r in rows if r["kind"] == "timing"]
    assert len(timing) == MAX_TIMING_ENTRIES
    assert timing[-1]["count"] == 5  # the oldest five went
    assert timing[0]["count"] == MAX_TIMING_ENTRIES + 4
    # Neither the refusal, the burst nor its ack was touched.
    assert [r["kind"] for r in rows if r["kind"] != "timing"] \
        == ["refusal", "burst_ack", "burst"]
    assert len(log) == MAX_TIMING_ENTRIES + 3


def test_timing_lines_never_count_against_the_refusal_cap_or_the_alert_belt(log):
    now = time.time()
    for i in range(MAX_TIMING_ENTRIES):
        log.append("timing", door="lan", peer="dev-1", count=1,
                   window_seconds=600, hops=_timing_hops(), ts=now - 9000 + i)
    for i in range(MAX_ENTRIES):
        log.append("refusal", door="lan", peer=str(i), reason="plaintext",
                   ts=now - 5000 + i)
    for i in range(MAX_ALERT_ENTRIES):
        log.append("burst", id=f"b{i}", door="lan", peer=str(i), count=5,
                   window_seconds=600, ts=now - 2000 + i)
    kinds = [r["kind"] for r in log.recent(include_timing=True,
                                            limit=MAX_ENTRIES * 2,
                                            timing_limit=MAX_ENTRIES)]
    # The cap is `MAX_ENTRIES` over refusals *and* alert lines, refusals
    # evicted first — today's rule — and the rollups sit outside it: were
    # they counted, six hundred more refusals would be gone.
    assert kinds.count("timing") == MAX_TIMING_ENTRIES
    assert kinds.count("refusal") == MAX_ENTRIES - MAX_ALERT_ENTRIES
    assert kinds.count("burst") == MAX_ALERT_ENTRIES
    # One more refusal evicts the oldest *refusal*, never a rollup.
    log.append("refusal", door="lan", peer="last", reason="plaintext")
    kinds = [r["kind"] for r in log.recent(include_timing=True,
                                            limit=MAX_ENTRIES * 2,
                                            timing_limit=MAX_ENTRIES)]
    assert kinds.count("timing") == MAX_TIMING_ENTRIES
    assert kinds.count("refusal") == MAX_ENTRIES - MAX_ALERT_ENTRIES
    assert kinds.count("burst") == MAX_ALERT_ENTRIES
    assert '"peer": "200"' not in "".join(
        json.dumps(r) for r in log.recent(limit=MAX_ENTRIES * 2))
    # And the file is compacted against what is held, not the refusal cap
    # alone: the on-disk line count never runs past held + PRUNE_SLACK.
    assert len(log.path.read_text().splitlines()) < len(log) + PRUNE_SLACK


def test_a_timing_line_never_reaches_the_detector():
    """`WEIGHTS` has no entry for a timing line — it has no reason — and a
    detector handed the word anyway treats it as a hit only because it is
    an unknown reason; the daemon never calls `note` for one
    (`test_relay_client.test_the_timing_rollup_lands_on_the_access_log_and_never_alerts`
    asserts that with a spy)."""
    assert "timing" not in WEIGHTS
    assert "timing" not in REASONS


def test_hop_keys_are_the_closed_set_and_n_leads():
    assert HOP_KEYS[0] == "n"
    assert len(HOP_KEYS) == 1 + 7 * 3
    assert len(set(HOP_KEYS)) == len(HOP_KEYS)
    for name in HOP_KEYS[1:]:
        hop, stat = name.rsplit("_", 1)
        assert hop in ("dwell", "hold", "open", "run", "answer", "seal", "size")
        assert stat in ("p50", "p90", "max")
    assert not (set(HOP_KEYS) & set(FORBIDDEN_KEYS))


# --- the detector ---------------------------------------------------------------


def test_four_hits_are_nothing_and_the_fifth_inside_the_window_alerts():
    d = BurstDetector()
    for i in range(BURST_THRESHOLD - 1):
        assert d.note("10.0.0.9", "plaintext", 1000 + i) is None
    alert = d.note("10.0.0.9", "plaintext", 1000 + BURST_THRESHOLD - 1)
    assert alert == {"peer": "10.0.0.9", "count": BURST_THRESHOLD,
                     "window_seconds": BURST_WINDOW_SECONDS}


def test_five_hits_spread_past_the_window_are_nothing():
    d = BurstDetector()
    step = (BURST_WINDOW_SECONDS + 1) / (BURST_THRESHOLD - 1)
    for i in range(BURST_THRESHOLD):
        assert d.note("10.0.0.9", "plaintext", 1000 + i * step) is None


def test_the_cooldown_holds_a_second_alert_for_an_hour_and_then_lets_it_go():
    d = BurstDetector()
    for i in range(BURST_THRESHOLD):
        first = d.note("10.0.0.9", "plaintext", 1000 + i)
    assert first
    # The deque was cleared at the alert: a sixth hit is one hit.
    assert d.note("10.0.0.9", "plaintext", 1000 + BURST_THRESHOLD) is None
    # Five more inside the cooldown: a burst, but no alert.
    for i in range(BURST_THRESHOLD):
        assert d.note("10.0.0.9", "plaintext", 1100 + i) is None
    # Five more after the cooldown: an alert again.
    base = 1000 + ALERT_COOLDOWN_SECONDS
    for i in range(BURST_THRESHOLD - 1):
        assert d.note("10.0.0.9", "plaintext", base + i) is None
    assert d.note("10.0.0.9", "plaintext", base + BURST_THRESHOLD - 1)


def test_a_keyholders_refusal_is_never_a_hit_and_never_inserts_the_peer():
    d = BurstDetector()
    for i in range(20):
        assert d.note("10.0.0.9", "ctr", 1000 + i) is None
        assert d.note("10.0.0.9", "ts", 1000 + i) is None
        assert d.note("10.0.0.9", "kind", 1000 + i) is None
    assert d.hits("10.0.0.9") == 0
    assert d.tracked() == 0


def test_the_relays_rate_bucket_is_logged_and_never_a_hit():
    """The bucket fires before the seal is opened: a quantity signal, not
    an authenticity one. Twenty of them leave the deque untouched — and a
    stranger flooding the mailbox still produces counted `seal` refusals."""
    d = BurstDetector()
    for i in range(20):
        assert d.note("dev1", "rate", 1000 + i) is None
    assert d.hits("dev1") == 0
    assert d.tracked() == 0
    assert WEIGHTS["rate"] == 0
    for i in range(BURST_THRESHOLD - 1):
        assert d.note("dev1", "seal", 2000 + i) is None
    assert d.note("dev1", "seal", 2000 + BURST_THRESHOLD - 1)


def test_an_oversize_upload_from_a_paired_phone_is_never_a_hit():
    """The body rule opened the upload frame before refusing the body, so
    the sender is a keyholder whose photo was too large. The same reason
    with no device id — a stranger's oversize body — still counts, and a
    device id zeroes nothing else."""
    d = BurstDetector()
    for i in range(20):
        assert d.note("10.0.0.9", "oversize", 1000 + i, device_id="dev1") is None
    assert d.hits("10.0.0.9") == 0
    assert d.tracked() == 0
    for i in range(BURST_THRESHOLD - 1):
        assert d.note("10.0.0.9", "oversize", 2000 + i) is None
    assert d.note("10.0.0.9", "oversize", 2000 + BURST_THRESHOLD - 1)
    e = BurstDetector()
    for i in range(BURST_THRESHOLD - 1):
        assert e.note("10.0.0.8", "seal", 3000 + i, device_id="dev1") is None
    assert e.note("10.0.0.8", "seal", 3000 + BURST_THRESHOLD - 1, device_id="dev1")


def test_peers_are_bounded_and_the_oldest_is_forgotten():
    d = BurstDetector()
    for i in range(MAX_TRACKED_PEERS + 1):
        d.note(f"10.0.{i // 256}.{i % 256}", "plaintext", 1000 + i)
    assert d.tracked() == MAX_TRACKED_PEERS
    assert d.hits("10.0.0.0") == 0
    assert d.hits(f"10.0.0.{MAX_TRACKED_PEERS}") == 1


def test_the_plans_own_arithmetic():
    d = BurstDetector()
    assert all(d.note("10.0.0.9", "ctr", 1000 + i) is None for i in range(20))
    assert all(d.note("10.0.0.9", "plaintext", 2000 + i) is None for i in range(4))
    a = d.note("10.0.0.9", "plaintext", 2004)
    assert a and a["count"] == 5
    assert d.note("10.0.0.9", "plaintext", 2005) is None
    assert d.note("10.0.0.9", "plaintext", 2004 + 3600) is None


def test_an_unknown_reason_counts_as_a_hit():
    """Closed at the store; the detector weighs what it is handed, and a
    reason it has no weight for is a stranger's knock, not a keyholder's."""
    d = BurstDetector()
    for i in range(BURST_THRESHOLD - 1):
        assert d.note("p", "novel", 1000 + i) is None
    assert d.note("p", "novel", 1000 + BURST_THRESHOLD - 1)


def test_a_second_burst_inside_the_floor_folds_and_after_it_raises():
    d = BurstDetector()
    assert d.raised_at() is None
    for i in range(BURST_THRESHOLD):
        first = d.note("A", "plaintext", 1000 + i)
    assert first == {"peer": "A", "count": BURST_THRESHOLD,
                     "window_seconds": BURST_WINDOW_SECONDS}
    assert "fold" not in first
    assert d.raised_at() == 1000 + BURST_THRESHOLD - 1
    for i in range(BURST_THRESHOLD - 1):
        assert d.note("B", "plaintext", 1100 + i) is None
    fold = d.note("B", "plaintext", 1100 + BURST_THRESHOLD - 1)
    assert fold == {"fold": True, "peer": "B", "count": BURST_THRESHOLD,
                    "window_seconds": BURST_WINDOW_SECONDS}
    # A fold moves the floor's clock not at all.
    assert d.raised_at() == 1000 + BURST_THRESHOLD - 1
    base = 1000 + BURST_THRESHOLD - 1 + ALERT_FLOOR_SECONDS
    for i in range(BURST_THRESHOLD - 1):
        assert d.note("C", "plaintext", base + i) is None
    third = d.note("C", "plaintext", base + BURST_THRESHOLD - 1)
    assert third == {"peer": "C", "count": BURST_THRESHOLD,
                     "window_seconds": BURST_WINDOW_SECONDS}
    assert d.raised_at() == base + BURST_THRESHOLD - 1


def test_a_fold_spends_the_peers_own_cooldown():
    d = BurstDetector()
    for i in range(BURST_THRESHOLD):
        d.note("A", "plaintext", 1000 + i)
    for i in range(BURST_THRESHOLD):
        fold = d.note("B", "plaintext", 1100 + i)
    assert fold and fold["fold"] is True
    assert d.hits("B") == 0
    # Five more from B inside its hour: logged, not a second fold.
    for i in range(BURST_THRESHOLD):
        assert d.note("B", "plaintext", 1200 + i) is None


def test_the_per_peer_cooldown_is_applied_before_the_floor():
    d = BurstDetector()
    for i in range(BURST_THRESHOLD):
        d.note("A", "plaintext", 1000 + i)
    # A's own cooldown answers first: None, never a fold.
    for i in range(BURST_THRESHOLD):
        assert d.note("A", "plaintext", 1100 + i) is None


def test_the_floor_is_memory_only():
    d = BurstDetector()
    for i in range(BURST_THRESHOLD):
        d.note("A", "plaintext", 1000 + i)
    fresh = BurstDetector()
    for i in range(BURST_THRESHOLD - 1):
        assert fresh.note("B", "plaintext", 1100 + i) is None
    assert fresh.note("B", "plaintext", 1100 + BURST_THRESHOLD - 1) == {
        "peer": "B", "count": BURST_THRESHOLD,
        "window_seconds": BURST_WINDOW_SECONDS}


def test_the_floor_constants():
    assert ALERT_FLOOR_SECONDS == 600
    assert access_log.MAX_PUBLISHED_ALERTS == 8
    assert access_log.MAX_FOLDED_PEERS == 1024
    assert PUBLISHED_KEYS[-2:] == ("peers", "hops")


def test_no_platform_branch():
    from pathlib import Path
    src = Path(access_log.__file__).read_text()
    assert "sys.platform" not in src
