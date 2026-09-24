"""The buzz ledger (`buzz_ledger.py`): one private line per alert the daemon
handed to the phone leg, and nothing but identifiers, kinds, booleans, counts
and clocks on it.

Seams, all pre-existing: `test_alert_delivery._pushable` (a daemon with the
three push gates open and a fake connector recording `push_alert` bodies),
`BuzzLedger(path=tmp_path / ...)` (the `EventLog(path=)` convention),
`paths._home()` already a temp folder under pytest, a monkeypatched
`sys.modules["Quartz"]`, and a recorded `loop.run_in_executor`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import types

import pytest

import dark_army_daemon.paths as paths
from dark_army_daemon import buzz_ledger as bl
from dark_army_daemon.daemon import BobDaemon

from tests.test_alert_delivery import _alert, _pushable, _settle

#: The probe as the module defines it, captured before any test stubs it.
_REAL_IDLE_PROBE = bl.mac_idle_seconds


# ── seams ─────────────────────────────────────────────────────────────────────

SUMMARY = "Should I drop the production table before the migration?"


def _entry(**over) -> dict:
    """A published row in the shape `_enrich_agent_stubs` builds."""
    row = {
        "session_id": "s1",
        "questions": [{"text": "A?"}, {"text": "B?"}, {"text": "C?"}],
        "question": {"text": "A?", "options": ["x", "y"]},
        "reply_options": ["Accept", "Iterate"],
        "last_summary": SUMMARY[:40],
        "last_text": "The whole tail of what the agent said last.",
        "last_report": "## Work done\n- did things",
        "state": "idle",
        "_category": "waiting",
        "provider": "claude",
        "idle_seconds": 12.5,
        "stats": {"user_prompts": 7, "cost_usd": 1.2},
    }
    row.update(over)
    return row


def _card() -> dict:
    """The raw hook message `_active_notifications` holds — key included."""
    return {"hook": "Stop", "message": "Waiting for input", "key": "ENROL-KEY",
            "transcript_path": "/Users/x/.claude/projects/p/t.jsonl", "cwd": "/x"}


def _ledger(tmp_path, name="buzz.jsonl") -> bl.BuzzLedger:
    ledger = bl.BuzzLedger(path=tmp_path / name)
    ledger.open()
    return ledger


def _line(**over) -> dict:
    """One well-formed line, the shape `_ledger_buzz` builds."""
    line = {
        "v": bl.SCHEMA_VERSION, "id": "", "ts": time.time(), "drain": "abcd1234",
        "alert_id": "s1:card:1", "session_id": "s1", "rule": "card",
        "kind": "question", "severity": "warn", "created_at": time.time(),
        "collapsed": 1, "push_kind": "question", "badge": 2, "banner": False,
        "evidence": bl.evidence(_entry(), _card(), cooldown_gap=None),
        "mac": {"frontmost_fresh": False, "frontmost_hit": False,
                "panel_on_screen": False, "idle_seconds": None},
        "phone": {"outcome": "sent:1", "devices": 1, "held_seconds": 0.0},
    }
    line.update(over)
    return line


async def _landed(ledger: bl.BuzzLedger, count: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while len(ledger) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def _pushable_with_ledger(monkeypatch, tmp_path):
    d = _pushable(monkeypatch)
    d._buzz_ledger = _ledger(tmp_path)
    return d


def _question_alert() -> dict:
    """An alert carrying every word the ledger must not: the banner's
    title, body and subtitle, the push's `work`, and the evidence."""
    return dict(_alert(), kind="question", nickname="Vex",
                title="Vex asked you a question",
                body=SUMMARY, subtitle="secret-project · main",
                work="Rename the strip ladder", created_at=time.time(),
                _buzz_evidence=bl.evidence(_entry(), _card(), cooldown_gap=None))


# ── (a) (b) the evidence composer ─────────────────────────────────────────────

def test_evidence_is_counts_and_booleans_with_no_text():
    ev = bl.evidence(_entry(), _card(), cooldown_gap=42.0)
    assert tuple(ev) == bl.EVIDENCE_KEYS
    assert ev == {
        "card_hook": "Stop", "questions": 3, "question": True,
        "reply_options": 2, "summary_chars": 40, "text_chars": 43,
        "report": True, "state": "idle", "category": "waiting",
        "provider": "claude", "idle_seconds": 12.5, "user_prompts": 7,
        "cooldown_gap": 42.0,
    }
    flat = json.dumps(ev)
    for word in ("production", "Accept", "Iterate", "Work done", "ENROL-KEY",
                 "transcript", "/Users", "A?"):
        assert word not in flat


def test_evidence_on_nothing_is_the_same_keys_all_empty():
    ev = bl.evidence(None, None, cooldown_gap=None)
    assert tuple(ev) == bl.EVIDENCE_KEYS
    assert ev == {
        "card_hook": "", "questions": 0, "question": False, "reply_options": 0,
        "summary_chars": 0, "text_chars": 0, "report": False, "state": "",
        "category": "", "provider": "", "idle_seconds": None,
        "user_prompts": 0, "cooldown_gap": None,
    }


def test_evidence_takes_the_bucket_name_where_the_row_has_no_stamp():
    """The published row has `_category` stripped; the caller names the
    bucket it found the row under, and a stamp on the row still wins."""
    assert bl.evidence({"session_id": "s"}, None, cooldown_gap=None,
                       category="waiting")["category"] == "waiting"
    assert bl.evidence({"_category": "running"}, None, cooldown_gap=None,
                       category="waiting")["category"] == "running"


# ── (c) the fence ─────────────────────────────────────────────────────────────

def _extra_key():
    return _line(title="Vex asked you a question")


def _missing_key():
    line = _line()
    del line["badge"]
    return line


def _forbidden_in_evidence():
    line = _line()
    line["evidence"] = dict(line["evidence"], key="ENROL-KEY")
    return line


def _words_under_an_evidence_key():
    line = _line()
    line["evidence"] = dict(line["evidence"], question={"text": SUMMARY})
    return line


def _forbidden_in_mac():
    line = _line()
    line["mac"] = dict(line["mac"], body=SUMMARY)
    return line


@pytest.mark.parametrize("build", [
    _extra_key, _missing_key, _forbidden_in_evidence,
    _words_under_an_evidence_key, _forbidden_in_mac,
], ids=["extra key", "missing key", "forbidden nested in evidence",
        "words under an evidence key", "forbidden nested in mac"])
def test_append_refuses_a_malformed_line_and_writes_nothing(tmp_path, build):
    ledger = _ledger(tmp_path)
    assert ledger.append(build()) is None
    assert len(ledger) == 0
    assert not ledger.path.exists()


def test_the_fence_covers_the_alerts_words_and_the_access_logs_secrets():
    for name in ("title", "body", "subtitle", "work", "last_summary",
                 "question", "questions", "reply_options", "message",
                 "transcript_path", "cwd", "project", "branch",
                 "key", "claim", "token", "secret", "digest", "chan"):
        assert name in bl.FORBIDDEN_KEYS, name


def test_a_well_formed_line_lands_with_exactly_the_pinned_keys(tmp_path):
    ledger = _ledger(tmp_path)
    entry = ledger.append(_line())
    assert entry is not None
    assert tuple(entry) == bl.LINE_KEYS
    assert re.fullmatch(r"[0-9a-f]{12}", entry["id"])
    assert tuple(entry["mac"]) == bl.MAC_KEYS
    assert tuple(entry["phone"]) == bl.PHONE_KEYS
    on_disk = json.loads(ledger.path.read_text().splitlines()[0])
    assert on_disk == entry


# ── (d) (e) (f) the daemon's drain ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_buzz_writes_exactly_one_line_with_no_words(monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._undelivered = [_question_alert()]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 1)
    assert len(d._relay_connector.pushed) == 1          # the leg is untouched
    raw = d._buzz_ledger.path.read_text()
    lines = raw.splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert tuple(line) == bl.LINE_KEYS
    assert line["phone"] == {"outcome": "sent:1", "devices": 1,
                             "held_seconds": 0.0}
    assert line["kind"] == "question" and line["push_kind"] == "question"
    assert line["session_id"] == "s1" and line["alert_id"] == "s1:card:1"
    assert line["badge"] == 2 and line["collapsed"] == 1
    assert line["banner"] is False
    assert line["evidence"]["summary_chars"] == 40
    assert line["evidence"]["card_hook"] == "Stop"
    for word in ("asked you a question", "production table", "secret-project",
                 "strip ladder", SUMMARY[:40], "ENROL-KEY"):
        assert word not in raw
    assert oct(os.stat(d._buzz_ledger.path).st_mode & 0o777) == "0o600"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate, outcome", [
    ("push", "push_off"), ("remote", "remote_off"),
    ("connector", "no_connector"), ("devices", "no_devices"),
])
async def test_a_closed_gate_is_written_down_and_the_leg_is_unchanged(
        monkeypatch, tmp_path, gate, outcome):
    from dark_army_daemon import relay

    d = _pushable_with_ledger(monkeypatch, tmp_path)
    if gate == "push":
        d.phone_push_enabled = False
    elif gate == "remote":
        d.remote_access_enabled = False
    elif gate == "connector":
        d._relay_connector = None
    else:
        monkeypatch.setattr(relay, "push_token", lambda did: None)
    d._undelivered = [_question_alert()]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 1)
    if gate != "connector":
        assert d._relay_connector.pushed == []
    (line,) = d._buzz_ledger.recent()
    assert line["phone"] == {"outcome": outcome, "devices": 0,
                             "held_seconds": 0.0}


@pytest.mark.asyncio
async def test_a_security_row_writes_a_line_with_empty_evidence(monkeypatch, tmp_path):
    """`_raise_access_alert`'s row: an empty `session_id`, no
    `_buzz_evidence`, rule `access_burst`, kind `security`."""
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    row = {
        "id": "access:abcdef012345", "session_id": "", "nickname": "",
        "title": "Dark Army refused repeated connection attempts",
        "subtitle": "the Wi-Fi door",
        "body": "5 refused attempts from 10.0.0.9 at the Wi-Fi door",
        "severity": "crit", "rule": "access_burst", "created_at": time.time(),
        "actions": [], "character": "", "state": "", "kind": "security",
    }
    d._undelivered = [row]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 1)
    (line,) = d._buzz_ledger.recent()
    assert line["rule"] == "access_burst" and line["kind"] == "security"
    assert line["session_id"] == "" and line["alert_id"] == "access:abcdef012345"
    assert line["evidence"] == bl.evidence(None, None, cooldown_gap=None)
    assert "10.0.0.9" not in d._buzz_ledger.path.read_text()
    # Never on the phone's Needs you list, and still sent (23 Sep 2026).
    assert line["phone"]["outcome"] == "sent:1"


@pytest.mark.asyncio
async def test_a_stubbed_push_leg_returning_none_reads_as_unknown(monkeypatch, tmp_path):
    """`test_alert_delivery.py` stubs `_push_phone_alerts` with a function
    returning None; the ledger tolerates a non-string outcome."""
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._push_phone_alerts = lambda pending: None
    d._undelivered = [_question_alert()]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 1)
    (line,) = d._buzz_ledger.recent()
    assert line["phone"] == {"outcome": "unknown", "devices": 0,
                             "held_seconds": 0.0}


@pytest.mark.asyncio
async def test_a_raising_push_leg_is_written_down_as_raised(monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)

    def boom(pending):
        raise RuntimeError("no")

    d._push_phone_alerts = boom
    d._undelivered = [_question_alert()]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 1)
    (line,) = d._buzz_ledger.recent()
    assert line["phone"]["outcome"] == "raised"


@pytest.mark.asyncio
async def test_a_collapsed_drain_writes_one_line_per_alert_under_one_drain_id(
        monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._undelivered = [dict(_question_alert(), kind="question"),
                      dict(_question_alert(), id="s2:card:1", session_id="s2",
                           kind="permission")]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 2)
    rows = d._buzz_ledger.recent()
    assert len(rows) == 2
    assert len({r["drain"] for r in rows}) == 1
    assert {r["collapsed"] for r in rows} == {2}
    assert {r["push_kind"] for r in rows} == {"permission"}
    assert {r["kind"] for r in rows} == {"question", "permission"}


@pytest.mark.asyncio
async def test_a_finished_alert_is_its_own_batch_of_one(monkeypatch, tmp_path):
    """S3 splits a finished alert off before the push: its line is a batch
    of one, withheld, and the buzz the rest made is counted without it."""
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._undelivered = [dict(_question_alert(), kind="finished"),
                      dict(_question_alert(), id="s2:card:1", session_id="s2",
                           kind="permission")]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 2)
    rows = {r["kind"]: r for r in d._buzz_ledger.recent()}
    assert rows["finished"]["phone"] == {"outcome": "withheld:finished",
                                         "devices": 0, "held_seconds": 0.0}
    assert rows["finished"]["collapsed"] == 1
    assert rows["permission"]["phone"]["outcome"] == "sent:1"
    assert rows["permission"]["collapsed"] == 1
    assert rows["finished"]["drain"] != rows["permission"]["drain"]
    ((_did, body),) = d._relay_connector.pushed
    assert body["kind"] == "permission"


# ── (g) the thread hop ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_a_running_loop_the_write_hops_to_the_executor(monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    loop = asyncio.get_running_loop()
    hops = []
    real = loop.run_in_executor

    def spy(executor, fn, *args):
        hops.append(fn)
        return real(executor, fn, *args)

    monkeypatch.setattr(loop, "run_in_executor", spy)
    d._ledger_buzz([_question_alert()], "sent:1", banner=False)
    assert len(hops) == 1                      # the write went to the pool
    await _landed(d._buzz_ledger, 1)
    assert len(d._buzz_ledger) == 1


def test_off_the_loop_the_write_is_made_directly(monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._ledger_buzz([_question_alert()], "sent:1", banner=True)
    (line,) = d._buzz_ledger.recent()
    assert line["banner"] is True
    assert line["phone"] == {"outcome": "sent:1", "devices": 1,
                             "held_seconds": 0.0}


def test_no_ledger_means_no_line_and_no_error(monkeypatch):
    d = _pushable(monkeypatch)
    assert getattr(d, "_buzz_ledger", None) is None
    d._ledger_buzz([_question_alert()], "sent:1", banner=False)   # no raise


# ── (h) the Mac-presence probe ───────────────────────────────────────────────

def _fake_quartz(seconds):
    mod = types.ModuleType("Quartz")
    mod.kCGEventSourceStateHIDSystemState = 1
    mod.kCGAnyInputEventType = ~0
    mod.CGEventSourceSecondsSinceLastEventType = lambda state, kind: seconds
    return mod


def test_the_idle_probe_reads_quartz(monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", _fake_quartz(37.5))
    assert bl.mac_idle_seconds() == 37.5


def test_the_idle_probe_is_none_where_quartz_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "Quartz", None)
    assert bl.mac_idle_seconds() is None


def test_the_idle_probe_is_none_where_the_call_raises(monkeypatch):
    mod = _fake_quartz(0.0)

    def boom(state, kind):
        raise RuntimeError("no display")

    mod.CGEventSourceSecondsSinceLastEventType = boom
    monkeypatch.setitem(sys.modules, "Quartz", mod)
    assert bl.mac_idle_seconds() is None


def test_the_idle_reading_lands_on_the_line(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "Quartz", _fake_quartz(600.0))
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    # `_pushable` stubs the probe open for the phone leg; this case reads
    # the real one against the fake `Quartz`.
    monkeypatch.setattr(bl, "mac_idle_seconds", _REAL_IDLE_PROBE)
    d._ledger_buzz([_question_alert()], "sent:1", banner=False)
    (line,) = d._buzz_ledger.recent()
    assert line["mac"]["idle_seconds"] == 600.0


def test_the_mac_reading_is_the_frontmost_cache_and_the_panel(monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._session_states = {"s1": {"pid": 4242}}
    d._frontmost_pids = {4242}
    d._frontmost_at = time.time()
    d._ledger_buzz([_question_alert()], "sent:1", banner=False)
    (line,) = d._buzz_ledger.recent()
    assert line["mac"]["frontmost_fresh"] is True
    assert line["mac"]["frontmost_hit"] is True
    assert line["mac"]["panel_on_screen"] is False
    d._frontmost_at = time.time() - 60          # stale: distrusted
    d._ledger_buzz([_question_alert()], "sent:1", banner=False)
    line = d._buzz_ledger.recent()[0]
    assert line["mac"]["frontmost_fresh"] is False
    assert line["mac"]["frontmost_hit"] is False


# ── (i) the bounds ───────────────────────────────────────────────────────────

def test_the_file_is_capped_at_max_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(bl, "MAX_ENTRIES", 20)
    monkeypatch.setattr(bl, "PRUNE_SLACK", 5)
    ledger = _ledger(tmp_path)
    for i in range(bl.MAX_ENTRIES + bl.PRUNE_SLACK + 1):
        ledger.append(_line(ts=1000.0 + i + time.time() - 2000.0))
    on_disk = ledger.path.read_text().splitlines()
    assert len(on_disk) == bl.MAX_ENTRIES
    assert len(ledger) == bl.MAX_ENTRIES


def test_a_month_old_line_is_dropped_on_open(tmp_path):
    path = tmp_path / "buzz.jsonl"
    old = dict(_line(id="old"), ts=time.time() - 31 * 86400)
    fresh = dict(_line(id="fresh"), ts=time.time())
    path.write_text(json.dumps(old) + "\n" + json.dumps(fresh) + "\n")
    ledger = bl.BuzzLedger(path=path)
    ledger.open()
    assert [r["id"] for r in ledger.recent()] == ["fresh"]
    assert [json.loads(l)["id"] for l in path.read_text().splitlines()] == ["fresh"]


def test_an_aged_line_leaves_the_file_on_the_next_append(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append(_line(id="old", ts=time.time() - 31 * 86400))
    ledger.append(_line(id="fresh", ts=time.time()))
    assert [json.loads(l)["id"] for l in ledger.path.read_text().splitlines()] \
        == ["fresh"]


def test_open_tolerates_a_half_written_tail_and_unknown_keys(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append(_line())
    with open(ledger.path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(_line(id="known"), future_key="kept as data",
                                 v=99)) + "\n")
        fh.write('{"id": "half-writ')
    again = bl.BuzzLedger(path=ledger.path)
    again.open()
    rows = again.recent()
    assert len(rows) == 2
    newest = [r for r in rows if r["id"] == "known"][0]
    assert newest["v"] == 99                 # a `v` we do not know, kept
    assert "future_key" not in newest        # an unknown key, dropped


# ── (j) the private file ─────────────────────────────────────────────────────

def test_the_ledger_is_a_private_file():
    assert paths.BUZZ_LEDGER_NAME == "buzz-ledger.jsonl"
    assert paths.BUZZ_LEDGER_NAME in paths._PRIVATE_FILES
    assert paths.BUZZ_LEDGER_PATH.name == paths.BUZZ_LEDGER_NAME


def test_a_world_readable_ledger_is_narrowed_on_the_next_sweep(monkeypatch, tmp_path):
    state = tmp_path / ".dark-army"
    monkeypatch.setattr(paths, "STATE_DIR", state)
    monkeypatch.setattr(paths, "_restricted_at", 0.0)
    paths.ensure_state_dir()
    (state / paths.BUZZ_LEDGER_NAME).write_text("{}\n")
    (state / paths.BUZZ_LEDGER_NAME).chmod(0o644)
    monkeypatch.setattr(paths, "_restricted_at", 0.0)
    paths.ensure_state_dir()
    assert (state / paths.BUZZ_LEDGER_NAME).stat().st_mode & 0o777 == 0o600


# ── (k) a ledger that will not open ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_ledger_that_will_not_open_leaves_none_and_the_push_still_fires(
        monkeypatch, tmp_path):
    class Broken(bl.BuzzLedger):
        def open(self):
            raise OSError("disk full")

    monkeypatch.setattr(bl, "BuzzLedger", Broken)
    d = _pushable(monkeypatch)
    # `run()`'s block, extracted: the same try/except shape the daemon uses.
    try:
        ledger = bl.BuzzLedger()
        ledger.open()
        d._buzz_ledger = ledger
    except Exception:
        d._buzz_ledger = None
    assert d._buzz_ledger is None
    d._undelivered = [_question_alert()]
    d._deliver_alerts()
    await _settle(d)
    assert len(d._relay_connector.pushed) == 1


def test_run_opens_the_ledger_on_the_access_logs_terms():
    """The block in `run()` is the access log's, name for name: a failing
    `open()` is logged and leaves `None`, never a daemon that will not
    start."""
    from pathlib import Path

    import dark_army_daemon.daemon as dmod

    text = Path(dmod.__file__).read_text()
    block = text[text.index("ledger = buzz_ledger.BuzzLedger()"):][:400]
    assert "ledger.open()" in block
    assert "self._buzz_ledger = ledger" in block
    assert "buzz ledger unavailable; not recording" in block
    assert "self._buzz_ledger = None" in block
    shutdown = text[text.index("async def _shutdown"):]
    # Closed by name, either as its own call or in the shutdown store list.
    named = text[text.index("_CLOSED_AT_SHUTDOWN"):text.index("async def _shutdown")] if "_CLOSED_AT_SHUTDOWN" in text else ""
    assert "self._buzz_ledger.close()" in shutdown or (
        '"_buzz_ledger"' in named and "store.close()" in shutdown
    )


# ── (l) the evidence at decision time ────────────────────────────────────────

def _waiting_stub(sid="s1") -> dict:
    return {"session_id": sid, "project": "proj", "state": "idle",
            "subagents": 0, "subagent_ids": [], "idle_seconds": 5.0,
            "current_tool": "", "_category": "waiting", "provider": "claude"}


def test_the_decision_attaches_evidence_and_the_cooldown_gap():
    d = BobDaemon()
    d._active_notifications["s1"] = {"hook": "Stop", "message": "Waiting for input",
                                     "key": "ENROL-KEY"}
    d._enrich_agent_stubs([_waiting_stub()])
    assert len(d._undelivered) == 1
    first = d._undelivered[-1]
    ev = first["_buzz_evidence"]
    assert tuple(ev) == bl.EVIDENCE_KEYS
    assert ev["cooldown_gap"] is None
    assert ev["card_hook"] == "Stop"
    assert ev["category"] == "waiting" and ev["provider"] == "claude"
    assert ev["idle_seconds"] == 5.0
    assert "ENROL-KEY" not in json.dumps(ev)
    # A second alert about the same session, after the cooldown has run:
    # the gap is how long after the previous one it came.
    policy = d._alert_policy
    policy._fired.clear()
    policy._last_per_session["s1"] = time.time() - 400.0
    d._enrich_agent_stubs([_waiting_stub()])
    second = d._undelivered[-1]
    assert second is not first
    assert 399.0 <= second["_buzz_evidence"]["cooldown_gap"] <= 402.0


@pytest.mark.asyncio
async def test_the_evidence_never_reaches_the_push_body(monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._undelivered = [_question_alert()]
    d._deliver_alerts()
    await _settle(d)
    ((_did, body),) = d._relay_connector.pushed
    assert "_buzz_evidence" not in body
    assert "_buzz_evidence" not in json.dumps(body)


# ── (m) no platform-name test ────────────────────────────────────────────────

def test_the_module_holds_no_platform_name_test():
    from pathlib import Path

    text = Path(bl.__file__).read_text()
    assert "sys.platform" not in text
    assert "platform" not in re.findall(r"sys\.(\w+)", text)
    assert 'getattr(sys, "platform"' not in text


# ── (n) schema 2: what the phone leg withheld, dropped or held ───────────────

def test_schema_two_carries_how_long_the_leg_held_the_alert():
    assert bl.SCHEMA_VERSION == 2
    assert bl.PHONE_KEYS == ("outcome", "devices", "held_seconds")


@pytest.mark.parametrize("outcome, held", [
    ("withheld:finished", 0.0), ("withheld:unlisted", 0.0),
    ("withheld:mac_active", 0.0),
    ("withheld:mac_active", 30.0), ("dropped:card_gone", 30.0),
    ("dropped:muted", 30.0), ("sent:1", 30.0),
])
def test_every_phone_leg_outcome_is_a_line(tmp_path, outcome, held):
    ledger = _ledger(tmp_path)
    entry = ledger.append(_line(phone={"outcome": outcome, "devices": 0,
                                       "held_seconds": held}))
    assert entry is not None
    assert entry["phone"] == {"outcome": outcome, "devices": 0,
                              "held_seconds": held}


def test_a_line_without_held_seconds_is_refused_on_append(tmp_path):
    """Only `append` checks `PHONE_KEYS`: a writer that forgot the new key
    is refused, never half-recorded."""
    ledger = _ledger(tmp_path)
    assert ledger.append(_line(phone={"outcome": "sent:1", "devices": 1})) is None


def test_a_schema_one_line_on_disk_is_kept_as_data(tmp_path):
    path = tmp_path / "buzz.jsonl"
    v1 = dict(_line(id="old-shape"), v=1,
              phone={"outcome": "sent:1", "devices": 1})
    path.write_text(json.dumps(v1) + "\n")
    ledger = bl.BuzzLedger(path=path)
    ledger.open()
    (row,) = ledger.recent()
    assert row["v"] == 1 and row["id"] == "old-shape"
    assert row["phone"] == {"outcome": "sent:1", "devices": 1}
    assert ledger.append(_line(id="new-shape")) is not None
    assert len(ledger) == 2


def test_the_ledger_writes_the_readings_it_was_handed(monkeypatch, tmp_path):
    """`idle_seconds` handed in is written as-is and the probe is not run
    again — the line and the presence gate agree."""
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(bl, "mac_idle_seconds",
                        lambda: pytest.fail("re-probed"))
    d._ledger_buzz([_question_alert()], "withheld:mac_active", banner=False,
                   idle_seconds=12.0, held_seconds=30.0)
    (line,) = d._buzz_ledger.recent()
    assert line["mac"]["idle_seconds"] == 12.0
    assert line["phone"] == {"outcome": "withheld:mac_active", "devices": 0,
                             "held_seconds": 30.0}


def _s1_predicate(line: dict) -> bool:
    """The research report's S1 `jq` line, in Python: a question buzz whose
    only evidence was a caption."""
    ev = line["evidence"]
    return (line["kind"] == "question" and ev["questions"] == 0
            and ev["question"] is False and ev["reply_options"] == 0
            and ev["summary_chars"] > 0)


def _summary_only_transcript(tmp_path) -> str:
    """A prompt, then a closing message carrying a `bob-tldr` summary and
    no `bob-actions` offer — the turn the phone used to call a question."""
    path = tmp_path / "s1.jsonl"
    rows = [
        {"type": "user", "timestamp": "2026-09-22T10:00:00.000Z",
         "message": {"role": "user", "content": "Tidy the strip ladder."}},
        {"type": "assistant", "timestamp": "2026-09-22T10:05:00.000Z",
         "message": {"role": "assistant", "model": "claude-opus-4-1",
                     "content": [{"type": "text", "text":
                                  "Done: the ladder is tidy.\n"
                                  "<!-- bob-tldr: Tidied the strip ladder. -->"}]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(path)


@pytest.mark.asyncio
async def test_a_summary_only_stop_is_finished_withheld_and_written_down(
        monkeypatch, tmp_path):
    """The success criterion, in process: a summary-only Stop driven through
    `_enrich_agent_stubs` → `_deliver_alerts` writes exactly one line — kind
    `finished`, outcome `withheld:finished`, held 0.0 — pushes nothing, and
    the research report's S1 predicate is false on it."""
    from dark_army_daemon import relay
    from dark_army_daemon import session_stats as ss
    from tests.test_alert_delivery import _Connector

    transcript = _summary_only_transcript(tmp_path)
    monkeypatch.setattr(ss, "resolve_transcript", lambda sid, *a, **k: transcript)
    monkeypatch.setattr(bl, "mac_idle_seconds", lambda: None)
    monkeypatch.setattr(relay, "channel_ids", lambda: {"phone-1": "c" * 32})
    monkeypatch.setattr(relay, "push_token", lambda did: "ab" * 32)
    d = BobDaemon()
    d.phone_push_enabled = True
    d.remote_access_enabled = True
    d._relay_connector = _Connector()
    d._buzz_ledger = _ledger(tmp_path)
    d._active_notifications["s1"] = {"hook": "Stop",
                                     "message": "Waiting for input"}
    d._enrich_agent_stubs([_waiting_stub()])
    assert len(d._undelivered) == 1
    assert d._undelivered[0]["kind"] == "finished"
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 1)
    (line,) = d._buzz_ledger.recent()
    assert line["kind"] == "finished"
    assert line["phone"] == {"outcome": "withheld:finished", "devices": 0,
                             "held_seconds": 0.0}
    assert line["evidence"]["summary_chars"] > 0     # the caption was there
    assert _s1_predicate(line) is False
    assert d._relay_connector.pushed == []


# ── (n) the unlisted rung: a buzz names only what the phone lists ────────────

def _busy_stall(sid="s3") -> dict:
    """A `stall` signal alert on a running row, evidence as decided."""
    return dict(_alert(sid, rule="stall"), kind="attention", nickname="Vex",
                title="Vex has been quiet for an hour", body=SUMMARY,
                created_at=time.time(),
                _buzz_evidence=bl.evidence(
                    _entry(session_id=sid, _category="running", questions=[],
                           question=None, reply_options=[], last_summary=""),
                    None, cooldown_gap=None))


@pytest.mark.asyncio
async def test_a_stall_on_a_busy_agent_is_written_down_unlisted(monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._undelivered = [_busy_stall()]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 1)
    (line,) = d._buzz_ledger.recent()
    assert line["rule"] == "stall" and line["session_id"] == "s3"
    assert line["phone"] == {"outcome": "withheld:unlisted", "devices": 0,
                             "held_seconds": 0.0}
    assert line["evidence"]["category"] == "running"
    assert d._relay_connector.pushed == []
    text = d._buzz_ledger.path.read_text()
    assert SUMMARY not in text and "quiet for an hour" not in text


@pytest.mark.asyncio
async def test_a_listed_and_an_unlisted_alert_are_two_batches_of_one(
        monkeypatch, tmp_path):
    d = _pushable_with_ledger(monkeypatch, tmp_path)
    d._undelivered = [_busy_stall(), _question_alert()]
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 2)
    rows = {r["session_id"]: r for r in d._buzz_ledger.recent()}
    assert rows["s3"]["phone"]["outcome"] == "withheld:unlisted"
    assert rows["s1"]["phone"]["outcome"] == "sent:1"
    assert rows["s3"]["collapsed"] == 1 and rows["s1"]["collapsed"] == 1
    assert rows["s3"]["drain"] != rows["s1"]["drain"]


@pytest.mark.asyncio
async def test_a_summary_only_stop_and_a_stall_on_a_busy_session_leave_no_phantom(
        monkeypatch, tmp_path):
    """The success criterion, in process: a summary-only Stop on a waiting
    row and a stall on a running row, both decided by `_enrich_agent_stubs`
    and drained by `_deliver_alerts`, make no push at all; the ledger holds
    the two withheld lines, no line meets the research report's S1
    predicate, and no `sent:` line names a session the phone does not
    list."""
    from dark_army_daemon import live_activity, relay
    from dark_army_daemon import session_stats as ss
    from tests.test_alert_delivery import _Connector

    transcript = _summary_only_transcript(tmp_path)
    monkeypatch.setattr(ss, "resolve_transcript",
                        lambda sid, *a, **k: transcript if sid == "s1" else "")
    monkeypatch.setattr(bl, "mac_idle_seconds", lambda: None)
    monkeypatch.setattr(relay, "channel_ids", lambda: {"phone-1": "c" * 32})
    monkeypatch.setattr(relay, "push_token", lambda did: "ab" * 32)
    d = BobDaemon()
    d.phone_push_enabled = True
    d.remote_access_enabled = True
    d._relay_connector = _Connector()
    d._buzz_ledger = _ledger(tmp_path)
    d._active_notifications["s1"] = {"hook": "Stop",
                                     "message": "Waiting for input"}
    waiting = _waiting_stub()
    busy = dict(_waiting_stub("s3"), _category="running", state="working",
                idle_seconds=100000.0)
    d._agents_snapshot_cache = {"waiting": [{"session_id": "s1"}],
                                "running": [{"session_id": "s3"}]}
    d._enrich_agent_stubs([waiting, busy])
    d._enrich_agent_stubs([waiting, busy])    # the stall's hysteresis pass
    assert sorted((a["session_id"], a["rule"], a["kind"])
                  for a in d._undelivered) == [("s1", "card", "finished"),
                                               ("s3", "stall", "attention")]
    listed = d._phone_listed_sessions()
    assert "s3" not in listed
    d._deliver_alerts()
    await _settle(d)
    await _landed(d._buzz_ledger, 2)
    assert d._relay_connector.pushed == []
    lines = d._buzz_ledger.recent()
    assert len(lines) == 2
    by_rule = {line["rule"]: line for line in lines}
    assert by_rule["card"]["kind"] == "finished"
    assert by_rule["card"]["phone"]["outcome"] == "withheld:finished"
    assert by_rule["stall"]["phone"]["outcome"] == "withheld:unlisted"
    assert not any(_s1_predicate(line) for line in lines)
    snapshot_listed = live_activity.listed_sessions(
        d._agents_snapshot_cache, d._prompts_by_session(),
        notified=[n["session_id"] for n in d._notification_snapshot()])
    assert not [line for line in lines
                if line["phone"]["outcome"].startswith("sent:")
                and line["session_id"] not in snapshot_listed]
