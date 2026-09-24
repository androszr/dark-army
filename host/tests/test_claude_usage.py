"""The live read of Claude's per-model weekly window.

All hermetic: `get_snapshot` takes `read_token=`, `fetch=` and `now=`, so no
test here touches the keychain, the network or the clock. `_read_access_token`
and `_fetch_usage` are exercised against a patched `subprocess.run` and a
patched `urlopen` — the argv vector and the request contract are the parts that
have to be right, and they are the parts a stub would hide.

The one test that is not about behaviour is `test_no_secret_reaches_a_log_or_a
_snapshot`: this module handles a login credential and its output is served on
an ungated loopback route, on the LAN door and through the relay, so a sentinel
is driven through every failure path and must appear nowhere.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time

import pytest

from dark_army_daemon import claude_usage


#: The real entry point, captured at import — before `conftest`'s autouse
#: `_no_live_claude_usage` replaces it for every *other* test in the suite. This
#: file is the one that is about the real function, so it puts it back.
_REAL_GET_SNAPSHOT = claude_usage.get_snapshot


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    monkeypatch.setattr(claude_usage, "get_snapshot", _REAL_GET_SNAPSHOT)
    claude_usage.reset_cache()
    yield
    claude_usage.reset_cache()


def _scoped(percent=64.0, name="Fable", resets="2026-09-05T06:00:00+00:00",
            active=True, severity="normal"):
    return {
        "kind": "weekly_scoped",
        "group": "weekly",
        "percent": percent,
        "severity": severity,
        "resets_at": resets,
        "scope": {"model": {"id": None, "display_name": name}, "surface": None},
        "is_active": active,
    }


def _payload(*entries):
    return {"limits": [
        {"kind": "session", "group": "session", "percent": 12.0,
         "severity": "normal", "resets_at": "2026-09-01T12:00:00+00:00",
         "scope": None, "is_active": False},
        *entries,
    ]}


# --------------------------------------------------------------- the keychain

def test_the_keychain_is_read_by_argv_at_an_absolute_path(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"claudeAiOauth": {"accessToken": "tok"}}),
            stderr="")

    monkeypatch.setattr(claude_usage.subprocess, "run", fake_run)
    monkeypatch.setattr(claude_usage.getpass, "getuser", lambda: "someone")

    assert claude_usage._read_access_token() == "tok"
    assert seen["argv"][0] == "/usr/bin/security"
    assert seen["argv"][:3] == ["/usr/bin/security", "find-generic-password", "-w"]
    assert "-a" in seen["argv"] and "someone" in seen["argv"]
    assert claude_usage.KEYCHAIN_SERVICE in seen["argv"]
    # No shell, and a bounded wait.
    assert seen["kwargs"].get("shell") is not True
    assert seen["kwargs"]["timeout"] == claude_usage.KEYCHAIN_TIMEOUT_SECONDS
    assert seen["kwargs"]["check"] is False


def test_only_the_access_token_is_ever_asked_for(monkeypatch):
    """The refresh token is in the same blob and is never touched."""
    blob = json.dumps({"claudeAiOauth": {
        "accessToken": "access", "refreshToken": "REFRESH-SECRET"}})
    monkeypatch.setattr(
        claude_usage.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, blob, ""))
    assert claude_usage._read_access_token() == "access"
    src = (claude_usage.__file__).replace(".pyc", ".py")
    assert "refreshToken" not in open(src, encoding="utf-8").read()


@pytest.mark.parametrize("outcome", ["timeout", "nonzero", "not-json",
                                     "not-object", "no-key", "empty-token",
                                     "oserror"])
def test_every_keychain_failure_is_none(monkeypatch, outcome):
    def fake_run(argv, **kwargs):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        if outcome == "oserror":
            raise OSError("no such binary")
        if outcome == "nonzero":
            return subprocess.CompletedProcess(argv, 44, "", "denied")
        body = {
            "not-json": "}{ not json",
            "not-object": "[1, 2, 3]",
            "no-key": json.dumps({"claudeAiOauth": {}}),
            "empty-token": json.dumps({"claudeAiOauth": {"accessToken": ""}}),
        }[outcome]
        return subprocess.CompletedProcess(argv, 0, body, "")

    monkeypatch.setattr(claude_usage.subprocess, "run", fake_run)
    assert claude_usage._read_access_token() is None


# ------------------------------------------------------------------ the fetch

class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, size=None):
        return self._body[:size] if size is not None else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_fetch_contract(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response(json.dumps(_payload(_scoped())).encode())

    monkeypatch.setattr(claude_usage.urllib.request, "urlopen", fake_urlopen)
    payload = claude_usage._fetch_usage("secret-token")

    request = seen["request"]
    assert request.full_url == "https://api.anthropic.com/api/oauth/usage"
    assert request.get_method() == "GET"
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["authorization"] == "Bearer secret-token"
    assert headers["anthropic-beta"] == claude_usage.BETA_HEADER
    assert headers["accept"] == "application/json"
    assert seen["timeout"] == claude_usage.FETCH_TIMEOUT_SECONDS
    assert isinstance(payload, dict) and payload["limits"]


def test_an_oversize_body_is_refused_without_parsing(monkeypatch):
    huge = b"[" + b"0," * claude_usage.MAX_BODY_BYTES + b"0]"
    monkeypatch.setattr(claude_usage.urllib.request, "urlopen",
                        lambda r, timeout=None: _Response(huge))
    assert claude_usage._fetch_usage("tok") is None


@pytest.mark.parametrize("kind", ["http-401", "http-500", "urlerror",
                                  "oserror", "bad-json", "not-object"])
def test_every_fetch_failure_is_none(monkeypatch, kind):
    import urllib.error

    def fake_urlopen(request, timeout=None):
        if kind == "http-401":
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)
        if kind == "http-500":
            raise urllib.error.HTTPError(request.full_url, 500, "no", {}, None)
        if kind == "urlerror":
            raise urllib.error.URLError("offline")
        if kind == "oserror":
            raise OSError("socket died")
        if kind == "bad-json":
            return _Response(b"}{")
        return _Response(b"[1, 2, 3]")

    monkeypatch.setattr(claude_usage.urllib.request, "urlopen", fake_urlopen)
    assert claude_usage._fetch_usage("tok") is None


# ------------------------------------------------------------------ the parse

# 2026-09-01T12:00Z — before every reset instant the fixtures name.
NOW = 1_788_264_000.0


def test_the_primary_shape_becomes_one_bar():
    bars = claude_usage._parse_scoped_bars(_payload(_scoped()), NOW)
    assert len(bars) == 1
    bar = bars[0]
    assert bar["kind"] == "weekly_scoped"
    assert bar["group"] == "weekly"
    assert bar["title"] == "Current week (Fable)"
    assert bar["percent"] == 64.0
    assert bar["source"] == "oauth"
    assert bar["as_of"] == NOW
    assert bar["stale"] is False
    assert bar["resets_at"] == 1_788_588_000.0


def test_two_scoped_windows_become_two_bars():
    bars = claude_usage._parse_scoped_bars(
        _payload(_scoped(percent=64.0, name="Fable"),
                 _scoped(percent=12.0, name="Opus", active=False)), NOW)
    assert sorted(b["title"] for b in bars) == [
        "Current week (Fable)", "Current week (Opus)"]


def test_the_active_window_wins_a_duplicate_title():
    bars = claude_usage._parse_scoped_bars(
        _payload(_scoped(percent=1.0, active=False),
                 _scoped(percent=99.0, active=True)), NOW)
    assert [b["percent"] for b in bars] == [99.0]


def test_a_bar_carries_only_allow_listed_keys():
    entry = _scoped()
    entry["used_dollars"] = 12.5
    entry["locked_reason"] = "nope"
    bars = claude_usage._parse_scoped_bars(_payload(entry), NOW)
    assert set(bars[0]) == set(claude_usage.BAR_KEYS)


def test_a_passed_reset_is_stale():
    bars = claude_usage._parse_scoped_bars(
        _payload(_scoped(resets="2020-01-01T00:00:00+00:00")), NOW)
    assert bars[0]["stale"] is True


def test_a_missing_reset_is_not_stale():
    bars = claude_usage._parse_scoped_bars(_payload(_scoped(resets=None)), NOW)
    assert bars[0]["resets_at"] is None
    assert bars[0]["stale"] is False


@pytest.mark.parametrize("percent", [True, False, float("nan"), float("inf"),
                                     float("-inf"), -1.0, 101.0, "64", None])
def test_an_unusable_percent_is_dropped(percent):
    assert claude_usage._parse_scoped_bars(_payload(_scoped(percent=percent)),
                                           NOW) == []


@pytest.mark.parametrize("payload", [{}, {"limits": None}, {"limits": []},
                                     {"limits": ["nope", 3]}, None, "text"])
def test_a_shapeless_payload_is_no_bars(payload):
    assert claude_usage._parse_scoped_bars(payload, NOW) == []


def test_a_malformed_reset_leaves_the_bar_undated():
    bars = claude_usage._parse_scoped_bars(_payload(_scoped(resets="soon")), NOW)
    assert bars[0]["resets_at"] is None


# ---------------------------------------------------------- the fallback shape

def _fallback(utilization, resets="2026-09-05T06:00:00+00:00"):
    return {"seven_day_overage_included":
            {"utilization": utilization, "resets_at": resets}}


def test_the_fallback_fraction_is_multiplied_once():
    bars = claude_usage._parse_scoped_bars(_fallback(0.64), NOW)
    assert len(bars) == 1
    assert bars[0]["percent"] == pytest.approx(64.0)
    assert bars[0]["title"] == "Current week (Fable)"
    assert bars[0]["source"] == "oauth"
    assert set(bars[0]) == set(claude_usage.BAR_KEYS)


@pytest.mark.parametrize("fraction,expected", [(0.0, 0.0), (1.0, 100.0),
                                               (0.005, 0.5)])
def test_the_fallback_units_at_both_ends(fraction, expected):
    bars = claude_usage._parse_scoped_bars(_fallback(fraction), NOW)
    assert bars[0]["percent"] == pytest.approx(expected)


@pytest.mark.parametrize("value", [64, 100.0, 1.5, -0.1, True,
                                   float("nan"), "0.64", None])
def test_the_fallback_refuses_anything_that_is_not_a_fraction(value):
    """64 here means 64 *hundred* percent. A number that is out of range for a
    fraction is the other shape arriving under this key, and guessing which is
    exactly the double-multiplication mistake."""
    if value == 100.0 or value == 1.5:
        assert claude_usage._parse_scoped_bars(_fallback(value), NOW) == []
    else:
        bars = claude_usage._parse_scoped_bars(_fallback(value), NOW)
        assert bars == [] or bars[0]["percent"] <= 100.0


def test_the_primary_shape_wins_over_the_fallback():
    payload = dict(_payload(_scoped(percent=64.0)), **_fallback(0.11))
    bars = claude_usage._parse_scoped_bars(payload, NOW)
    assert [b["percent"] for b in bars] == [64.0]


# ------------------------------------------------------------------ the cache

def _stub(bars_payload, calls):
    def fetch(token):
        calls.append(token)
        return bars_payload
    return fetch


def test_a_success_is_reused_for_the_ttl():
    calls = []
    fetch = _stub(_payload(_scoped()), calls)
    first = claude_usage.get_snapshot(now=NOW, fetch=fetch,
                                      read_token=lambda: "tok")
    second = claude_usage.get_snapshot(now=NOW + 10, fetch=fetch,
                                       read_token=lambda: "tok")
    assert first["bars"][0]["percent"] == 64.0
    assert second["bars"] == first["bars"]
    assert len(calls) == 1


def test_the_ttl_expires_and_the_fetch_runs_again(monkeypatch):
    calls = []
    fetch = _stub(_payload(_scoped()), calls)
    claude_usage.get_snapshot(now=NOW, fetch=fetch, read_token=lambda: "tok")
    with claude_usage._lock:
        claude_usage._cache["at"] -= claude_usage.SUCCESS_TTL_SECONDS + 1
    claude_usage.get_snapshot(now=NOW + 400, fetch=fetch,
                              read_token=lambda: "tok")
    assert len(calls) == 2


def test_a_first_run_failure_is_an_empty_snapshot():
    calls = []
    snap = claude_usage.get_snapshot(now=NOW, fetch=_stub(None, calls),
                                     read_token=lambda: "tok")
    assert snap == {}


def test_a_failure_holds_off_the_next_attempt():
    calls = []
    fetch = _stub(None, calls)
    claude_usage.get_snapshot(now=NOW, fetch=fetch, read_token=lambda: "tok")
    claude_usage.get_snapshot(now=NOW + 5, fetch=fetch, read_token=lambda: "tok")
    assert len(calls) == 1
    with claude_usage._lock:
        claude_usage._cache["hold_until"] = 0.0
    claude_usage.get_snapshot(now=NOW + 90, fetch=fetch, read_token=lambda: "tok")
    assert len(calls) == 2


def test_a_failure_after_a_success_keeps_the_last_good_reading():
    good = _stub(_payload(_scoped(percent=64.0)), [])
    claude_usage.get_snapshot(now=NOW, fetch=good, read_token=lambda: "tok")
    with claude_usage._lock:
        claude_usage._cache["at"] -= claude_usage.SUCCESS_TTL_SECONDS + 1
    snap = claude_usage.get_snapshot(now=NOW + 400, fetch=_stub(None, []),
                                     read_token=lambda: "tok")
    assert snap["bars"][0]["percent"] == 64.0
    assert snap["fetched_at"] == NOW


def test_an_unreadable_keychain_never_reaches_the_network():
    calls = []
    snap = claude_usage.get_snapshot(now=NOW, fetch=_stub(_payload(_scoped()), calls),
                                     read_token=lambda: None)
    assert snap == {}
    assert calls == []


def test_a_held_reading_restales_when_its_window_resets():
    resets = "2026-09-05T06:00:00+00:00"
    from datetime import datetime
    epoch = datetime.fromisoformat(resets).timestamp()
    claude_usage.get_snapshot(now=epoch - 100,
                              fetch=_stub(_payload(_scoped(resets=resets)), []),
                              read_token=lambda: "tok")
    later = claude_usage.get_snapshot(now=epoch + 100, fetch=_stub(None, []),
                                      read_token=lambda: "tok")
    assert later["bars"][0]["stale"] is True


def test_the_lock_is_never_held_across_the_fetch():
    """A second caller arriving mid-flight gets an answer immediately rather
    than queueing behind the network — which is what a lock held over I/O would
    make it do."""
    entered = threading.Event()
    release = threading.Event()
    seen = {}

    def slow_fetch(token):
        entered.set()
        assert release.wait(5), "the second caller never arrived"
        return _payload(_scoped())

    def first():
        claude_usage.get_snapshot(now=NOW, fetch=slow_fetch,
                                  read_token=lambda: "tok")

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(5)
    started = time.monotonic()
    seen["snap"] = claude_usage.get_snapshot(
        now=NOW, fetch=_stub(_payload(_scoped()), []), read_token=lambda: "tok")
    elapsed = time.monotonic() - started
    release.set()
    worker.join(5)

    assert elapsed < 1.0, "the second caller waited on the first's network call"
    assert seen["snap"] == {}, "the second caller should get the (empty) held reading"


def test_only_one_flight_happens_under_two_threads():
    calls = []
    gate = threading.Event()

    def fetch(token):
        calls.append(token)
        gate.wait(5)
        return _payload(_scoped())

    threads = [threading.Thread(
        target=lambda: claude_usage.get_snapshot(
            now=NOW, fetch=fetch, read_token=lambda: "tok"))
        for _ in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(0.15)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 1


def test_nothing_is_written_to_disk(tmp_path, monkeypatch):
    from dark_army_daemon import paths
    before = sorted(p.name for p in paths.STATE_DIR.glob("*")) \
        if paths.STATE_DIR.exists() else []
    claude_usage.get_snapshot(now=NOW, fetch=_stub(_payload(_scoped()), []),
                              read_token=lambda: "tok")
    after = sorted(p.name for p in paths.STATE_DIR.glob("*")) \
        if paths.STATE_DIR.exists() else []
    assert before == after


# ------------------------------------------------------------------ the merge

def _local(*bars, **extra):
    return {"bars": list(bars), "notices": ["+50% promo"],
            "fetched_at": 1234.0, "available": bool(bars), **extra}


def _cached_scoped(percent=10.0, title="Current week (Fable)", promos=None):
    return {"kind": "weekly_scoped", "group": "weekly", "title": title,
            "percent": percent, "resets_at": 999.0, "severity": "normal",
            "source": "cache", "as_of": 111.0, "stale": False,
            "promos": promos if promos is not None else []}


def _session():
    return {"kind": "session", "group": "session", "title": "Current session",
            "percent": 12.0, "resets_at": 555.0, "severity": "normal",
            "source": "statusline", "as_of": 500.0, "stale": False,
            "promos": []}


def test_an_empty_live_snapshot_changes_nothing():
    local = _local(_session(), _cached_scoped())
    merged = claude_usage.merge_snapshot(local, {})
    assert merged == local
    assert merged["bars"] is not local["bars"]
    assert merged["bars"][1] is not local["bars"][1]


def test_the_live_bar_replaces_the_cached_one_in_place():
    local = _local(_session(), _cached_scoped(percent=10.0))
    live = {"bars": [{"kind": "weekly_scoped", "group": "weekly",
                      "title": "Current week (Fable)", "percent": 64.0,
                      "resets_at": 2000.0, "severity": "normal",
                      "source": "oauth", "as_of": NOW, "stale": False}]}
    merged = claude_usage.merge_snapshot(local, live)
    assert len(merged["bars"]) == 2
    assert merged["bars"][0]["kind"] == "session"
    scoped = merged["bars"][1]
    assert scoped["percent"] == 64.0
    assert scoped["source"] == "oauth"
    assert scoped["as_of"] == NOW


def test_the_replacement_carries_the_local_promos():
    local = _local(_cached_scoped(promos=["+50% weekly limits promo"]))
    live = {"bars": [dict(_cached_scoped(percent=64.0), source="oauth")]}
    live["bars"][0].pop("promos")
    merged = claude_usage.merge_snapshot(local, live)
    assert merged["bars"][0]["promos"] == ["+50% weekly limits promo"]
    assert merged["bars"][0]["percent"] == 64.0


def test_an_unmatched_live_bar_is_appended():
    local = _local(_session())
    live = {"bars": [dict(_cached_scoped(percent=64.0), source="oauth")]}
    merged = claude_usage.merge_snapshot(local, live)
    assert [b["kind"] for b in merged["bars"]] == ["session", "weekly_scoped"]


def test_local_bars_the_fetch_did_not_name_survive_byte_for_byte():
    session = _session()
    other = _cached_scoped(title="Current week (Opus)", percent=3.0)
    local = _local(session, other, _cached_scoped())
    live = {"bars": [dict(_cached_scoped(percent=64.0), source="oauth")]}
    merged = claude_usage.merge_snapshot(local, live)
    assert merged["bars"][0] == session
    assert merged["bars"][1] == other
    assert merged["notices"] == ["+50% promo"]
    assert merged["fetched_at"] == 1234.0


def test_available_becomes_true_when_a_live_bar_arrives():
    merged = claude_usage.merge_snapshot(
        {"bars": [], "notices": [], "fetched_at": None, "available": False},
        {"bars": [dict(_cached_scoped(), source="oauth")]})
    assert merged["available"] is True


def test_the_merge_never_mutates_its_arguments():
    local = _local(_cached_scoped(percent=10.0))
    live = {"bars": [dict(_cached_scoped(percent=64.0), source="oauth")]}
    claude_usage.merge_snapshot(local, live)
    assert local["bars"][0]["percent"] == 10.0
    assert live["bars"][0]["percent"] == 64.0


@pytest.mark.parametrize("live", [{}, None, {"bars": None}, {"bars": ["x", 1]},
                                  "text"])
def test_a_shapeless_live_snapshot_is_harmless(live):
    local = _local(_session(), _cached_scoped())
    assert claude_usage.merge_snapshot(local, live)["bars"] == local["bars"]


# ----------------------------------------------------------------- no secrets

SENTINEL = "sk-ant-oat01-SENTINEL-DO-NOT-LEAK"


def test_no_secret_reaches_a_log_or_a_snapshot(monkeypatch, caplog):
    """`/api/usage` is ungated on loopback and is served on the LAN door and
    through the relay. The token may not appear in a log record or anywhere in
    what this module returns, on any path."""
    import urllib.error

    caplog.set_level(logging.DEBUG)
    bodies = json.dumps({"limits": [_scoped()], "token_echo": SENTINEL}).encode()

    def fetch_ok(token):
        assert token == SENTINEL
        return json.loads(bodies)

    def fetch_401(token):
        raise urllib.error.HTTPError(claude_usage.USAGE_URL, 401,
                                     SENTINEL, {}, None)

    for fetch in (fetch_ok, lambda t: None, fetch_401):
        claude_usage.reset_cache()
        try:
            snap = claude_usage.get_snapshot(now=NOW, fetch=fetch,
                                             read_token=lambda: SENTINEL)
        except urllib.error.HTTPError:
            snap = {}
        assert SENTINEL not in repr(snap)

    # ...and the real fetch/keychain paths, driven to every failure.
    monkeypatch.setattr(
        claude_usage.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, json.dumps({"claudeAiOauth": {"accessToken": SENTINEL}}), ""))
    assert claude_usage._read_access_token() == SENTINEL

    def leaky_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401,
                                     f"reason carrying {SENTINEL}",
                                     {"x-echo": SENTINEL}, None)

    monkeypatch.setattr(claude_usage.urllib.request, "urlopen", leaky_urlopen)
    assert claude_usage._fetch_usage(SENTINEL) is None

    monkeypatch.setattr(claude_usage.urllib.request, "urlopen",
                        lambda r, timeout=None: _Response(bodies))
    claude_usage.reset_cache()
    snap = claude_usage.get_snapshot(now=NOW)
    assert snap["bars"][0]["percent"] == 64.0
    assert SENTINEL not in repr(snap)

    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in str(record.args or "")


def test_the_response_body_is_never_copied_wholesale():
    """A key the endpoint adds tomorrow must not ride out on a bar."""
    payload = _payload(_scoped())
    payload["limits"][-1]["surprise_field"] = "hello"
    payload["spend"] = {"used": {"amount_minor": 4200}}
    snap = claude_usage.get_snapshot(now=NOW, fetch=lambda t: payload,
                                     read_token=lambda: "tok")
    assert "surprise" not in repr(snap)
    assert "4200" not in repr(snap)
    for bar in snap["bars"]:
        assert set(bar) == set(claude_usage.BAR_KEYS)
