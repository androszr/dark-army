# host/tests/test_relay_box.py
"""Source pins on the mailbox function itself.

``relay/api/box.js`` runs on Vercel, not here — it has no test harness by
design (zero dependencies, one file). What can be pinned from this side is
pinned, `test_phone_remote.py`'s pattern: the properties the plan's security
argument rests on, so a drift fails the suite rather than dying quietly in
production.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOX = ROOT / "relay" / "api" / "box.js"
VERCEL = ROOT / "relay" / "vercel.json"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_no_logging_of_anything_carried():
    assert "console.log" not in _read(BOX)


def test_small_enough_to_audit_in_one_read():
    assert len(_read(BOX).splitlines()) <= 200


def test_rate_window_expires_only_when_it_opens():
    """``EXPIRE … NX``: the TTL is set once, when the window opens.

    Re-issuing the EXPIRE on every request slides the window forever — the
    Mac's continuous long-poll then climbs the counter monotonically, crosses
    the cap within hours, and every request (including the 5s retries the 429
    itself causes) re-arms the lockout. The away path dies and never comes
    back. The NX is the whole fix; do not "simplify" it away.
    """
    text = _read(BOX)
    assert re.search(r'\["EXPIRE",\s*rateKey,\s*String\(60\),\s*"NX"\]', text)
    assert '["INCR", rateKey]' in text


def test_a_per_row_redis_error_fails_loudly():
    """Upstash reports a failed pipeline command as `{error}` in that row —
    mapped silently to `undefined`, a refused ``EXPIRE rateKey 60 NX`` would
    leave the rate key immortal and bring the permanent-429 lockout back with
    nothing saying why. ``redis()`` must throw on a row error (the callers'
    existing catch answers 503) rather than degrade, and still log nothing."""
    text = _read(BOX)
    assert 'if (row.error !== undefined) throw new Error("redis")' in text


def test_get_longpoll_budgeted_separately_from_posts():
    """The rate key splits by method, so the Mac's held GET long-poll never
    spends the budget the phone's POSTs (and the Mac's answers) run on —
    240/min is real headroom per side, not a shared pot."""
    text = _read(BOX)
    assert 'rate:${ch}:' in text
    assert '"GET" ? "get" : "post"' in text


def test_vercel_duration_outlives_the_internal_wait():
    import json

    config = json.loads(_read(VERCEL))
    functions = config["functions"]
    # One entry per function, and no strays: a function file without an
    # entry runs on the platform default and dies mid-long-poll.
    assert set(functions) == {"api/box.js", "api/push.js"}
    assert functions["api/box.js"]["maxDuration"] == 25
    # The push function makes one APNs request, no held poll — 10s is
    # already generous.
    assert functions["api/push.js"]["maxDuration"] == 10
    box = _read(BOX)
    assert "WAIT_MS = 20000" in box  # under maxDuration, with margin


def test_poll_cadence_is_the_only_lever_and_is_slow():
    """The held GET is a poll loop because Upstash's REST API has no blocking
    pop (no BRPOP/BLPOP, no stream BLOCK) — so the cadence is the whole
    lever, and it is billed one RPOP a tick. At 400ms the Mac's continuous
    idle long-poll spent ~52 commands per empty 20-second window, around the
    clock; at 2000ms it spends ~12. Do not speed it back up.
    """
    text = _read(BOX)
    assert "const POLL_MS = 2000;" in text
    assert "BRPOP" in text and "BLPOP" in text
    # The one exception (21 Sep 2026): the first three seconds of a
    # phone's wait for its reply tick at 500 ms — a request in flight,
    # never the clock — and the Mac's continuous `to-mac` hold keeps the
    # slow tick.
    assert "const POLL_FAST_MS = 500;" in text
    assert "const FAST_WINDOW_MS = 3000;" in text
    assert 'dir === "to-phone"' in text
    assert "await sleep(fast ? POLL_FAST_MS : POLL_MS);" in text
    assert text.count("await sleep(") == 1
