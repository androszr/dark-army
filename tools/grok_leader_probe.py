#!/usr/bin/env python3
"""Prove whether the Grok leader treats ``clientInfo.name`` as a label.

The handshake name is the only thing that changes between the two connects.
Each name is one ``LeaderClient``, one ``connect()``, no retry. The probe
refuses while pytest is loaded and refuses when ``~/.grok/leader.sock`` is
absent, so it can never be the process that starts a leader.

    cd host && .venv/bin/python ../tools/grok_leader_probe.py

Prints a ``LEADER:`` line, a four-row table per name (``list``, ``load``,
``changed``, ``prompt+approval``), and one ``VERDICT:`` line:
``EQUAL``, ``DIFFERS <leg>``, or ``CANNOT TELL <reason>``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HOST = Path(__file__).resolve().parents[1] / "host"
if str(_HOST) not in sys.path:
    sys.path.insert(0, str(_HOST))

from dark_army_daemon import grok_leader  # noqa: E402

LEG_LIST = "list"
LEG_LOAD = "load"
LEG_CHANGED = "changed"
LEG_PROMPT = "prompt+approval"

_NAME_GAP_SECONDS = 90
_RUN_SECONDS = 300
_CHANGED_SECONDS = 20
_PERMISSION_SECONDS = 30

_PROMPT_PLAIN = "Reply with the single word ok. Use no tools."
_PROMPT_TRUE = "Run the shell command `true`, then reply done."

_STAMP_KEYS = (
    "updated_at", "updatedAt", "last_active_at", "lastActiveAt",
    "created_at", "createdAt", "mtime", "timestamp",
)
_CWD_KEYS = ("cwd", "workingDirectory", "working_directory", "workspace", "path")

_CLIENT_ID = re.compile(
    r"""["']?client[_]?[Ii]d["']?\s*[:=]\s*["']?[^,\s\]}"']+["']?"""
)
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_CLIENT_NAMES = re.compile(r"bob-companion|dark-army")


def _normalize(detail: str) -> str:
    """Drop what a fresh connect mints, and the name under test."""
    text = _CLIENT_ID.sub("", detail)
    text = _TIMESTAMP.sub("", text)
    return _CLIENT_NAMES.sub("", text)


def compare_legs(a: list[dict], b: list[dict]) -> str:
    """``EQUAL`` when every leg's ``ok`` and normalised ``detail`` match.

    Timestamps, per-connection ``client_id`` values and the two client
    names are not differences. Anything else is ``DIFFERS <first leg>``.
    """
    width = max(len(a), len(b))
    for index in range(width):
        if index >= len(a) or index >= len(b):
            present = a[index] if index < len(a) else b[index]
            return f"DIFFERS {present.get('leg')}"
        left, right = a[index], b[index]
        leg = left.get("leg") or right.get("leg")
        if left.get("leg") != right.get("leg"):
            return f"DIFFERS {leg}"
        if bool(left.get("ok")) != bool(right.get("ok")):
            return f"DIFFERS {leg}"
        if _normalize(str(left.get("detail") or "")) != _normalize(
            str(right.get("detail") or "")
        ):
            return f"DIFFERS {leg}"
    return "EQUAL"


def _stamp_key(entry: dict) -> tuple:
    for key in _STAMP_KEYS:
        value = entry.get(key)
        if isinstance(value, (int, float)):
            return (2, float(value), "")
        if isinstance(value, str) and value:
            return (1, 0.0, value)
    info = entry.get("info")
    if isinstance(info, dict):
        for key in _STAMP_KEYS:
            value = info.get(key)
            if isinstance(value, str) and value:
                return (1, 0.0, value)
    return (0, 0.0, grok_leader.session_id_of(entry))


def _cwd_of(entry: dict) -> str:
    for key in _CWD_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    info = entry.get("info")
    if isinstance(info, dict):
        for key in _CWD_KEYS:
            value = info.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def pick_session(entries, explicit_id: str, avoid_ids: set[str]) -> dict | None:
    """The requested id, else the newest entry that is not live.

    A resident entry is the live tab. ``avoid_ids`` is the same rule for
    ids the leader did not flag (this process, and ``active_sessions.json``).
    An explicit id that is live is refused rather than loaded.
    """
    rows = [entry for entry in entries or () if isinstance(entry, dict)]
    if explicit_id:
        if explicit_id in avoid_ids:
            return None
        for entry in rows:
            if grok_leader.session_id_of(entry) == explicit_id:
                if grok_leader.is_resident(entry):
                    return None
                return entry
        return {"sessionId": explicit_id, "cwd": ""}
    candidates = []
    for entry in rows:
        sid = grok_leader.session_id_of(entry)
        if not sid or sid in avoid_ids or grok_leader.is_resident(entry):
            continue
        candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(key=_stamp_key)
    return candidates[-1]


def _live_tab_ids() -> set[str]:
    """Tabs open on this Mac. Never load one of them."""
    avoid: set[str] = set()
    own = os.environ.get("GROK_SESSION_ID", "").strip()
    if own:
        avoid.add(own)
    path = Path.home() / ".grok" / "active_sessions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return avoid
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("session_id") or item.get("sessionId") or "")
            if sid:
                avoid.add(sid)
    return avoid


def _leader_version() -> str:
    binary = grok_leader.find_grok_binary()
    if not binary:
        return "grok unknown"
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "grok unknown"
    text = (proc.stdout or proc.stderr or "").strip()
    return text.splitlines()[0] if text else "grok unknown"


def _schedule_cancel(client, rid) -> None:
    """Answer a permission request. The result body is the plan's shape."""

    async def send() -> None:
        proc = client._proc
        if proc is None or proc.stdin is None:
            return
        payload = {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"outcome": {"outcome": "cancelled"}},
        }
        try:
            proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            await proc.stdin.drain()
        except (OSError, RuntimeError):
            return

    asyncio.get_running_loop().create_task(send())


class _Tape:
    """Notifications ``_dispatch`` sees, plus an immediate permission answer."""

    def __init__(self, client):
        self.client = client
        self.notes: list[dict] = []
        self.permission: dict | None = None
        self.queue_ids: list[str] = []
        self.arm_permission = False
        self._original = client._dispatch
        client._dispatch = self._wrap

    def _wrap(self, msg) -> None:
        self._original(msg)
        if not isinstance(msg, dict):
            return
        if "result" in msg or "error" in msg:
            return
        method = str(msg.get("method") or "")
        if not method:
            return
        self.notes.append(msg)
        if "request_permission" in method and msg.get("id") is not None:
            _schedule_cancel(self.client, msg.get("id"))
            if self.arm_permission and self.permission is None:
                self.permission = msg
        if method == grok_leader.QUEUE_CHANGED or method.endswith("queue/changed"):
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            sid = grok_leader.session_id_of(params)
            if not sid and isinstance(params.get("session"), dict):
                sid = grok_leader.session_id_of(params["session"])
            if sid:
                self.queue_ids.append(sid)

    def methods_since(self, start: int) -> list[str]:
        seen: list[str] = []
        for msg in self.notes[start:]:
            method = str(msg.get("method") or "")
            if method and method not in seen:
                seen.append(method)
        return seen

    def removal_seen(self, sid: str) -> bool:
        for msg in self.notes:
            if "sessions/changed" not in str(msg.get("method") or ""):
                continue
            body = grok_leader._session_fields(msg)
            if isinstance(body, dict):
                for key in ("removed", "deleted"):
                    if sid in grok_leader._as_ids(body.get(key)):
                        return True
            for entry in grok_leader.sessions_from_payload(msg):
                if grok_leader.session_id_of(entry) != sid:
                    continue
                if entry.get("resident") is False:
                    return True
        return False


def _leg(name: str, ok: bool, detail: str) -> dict:
    return {"leg": name, "ok": bool(ok), "detail": detail}


async def _request_entries(client, method: str):
    try:
        result = await client._request(method, {}, timeout=8.0)
    except Exception as exc:  # noqa: BLE001 — one method's failure is data
        return None, f"{method}:error={type(exc).__name__}:{exc}"
    entries = grok_leader.sessions_from_payload(result)
    ordered = sorted(
        (entry for entry in entries if isinstance(entry, dict)),
        key=grok_leader.session_id_of,
    )
    keys = sorted(ordered[0].keys()) if ordered else []
    detail = (
        f"{method}:count={len(ordered)}"
        f":residents={sorted(grok_leader.resident_ids(ordered))}"
        f":keys={keys}"
    )
    return ordered, detail


async def _list_leg(client):
    parts = []
    chosen_rows = None
    ok = False
    for method in (grok_leader.SESSION_LIST, grok_leader.SESSION_LIST_XAI):
        rows, detail = await _request_entries(client, method)
        parts.append(detail)
        if rows is None:
            continue
        ok = True
        if chosen_rows is None:
            chosen_rows = rows
        if grok_leader.list_has_resident_flag(rows):
            chosen_rows = rows
    return _leg(LEG_LIST, ok, " ".join(parts)), chosen_rows or []


def _matching_changed(notes, sid: str) -> dict | None:
    for msg in notes:
        if "sessions/changed" not in str(msg.get("method") or ""):
            continue
        for entry in grok_leader.sessions_from_payload(msg):
            if grok_leader.session_id_of(entry) == sid and grok_leader.is_resident(entry):
                return msg
    return None


async def _wait_for(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return bool(predicate())


async def _four_legs(client, explicit_id: str, avoid_ids: set[str], stored: dict | None):
    tape = _Tape(client)
    list_leg, rows = await _list_leg(client)
    if stored is not None:
        chosen = pick_session(rows, stored.get("sessionId") or "", avoid_ids)
        if chosen is None and stored.get("sessionId"):
            chosen = {
                "sessionId": stored["sessionId"],
                "cwd": stored.get("cwd") or "",
            }
    else:
        chosen = pick_session(rows, explicit_id, avoid_ids)
    if chosen is None:
        return [list_leg], None, False

    sid = grok_leader.session_id_of(chosen) or (stored or {}).get("sessionId") or ""
    cwd = _cwd_of(chosen) or (stored or {}).get("cwd") or ""
    mark = len(tape.notes)
    load_ok = True
    load_detail = ""
    try:
        result = await client._request(
            "session/load",
            {"sessionId": sid, "cwd": cwd, "mcpServers": []},
            timeout=15.0,
        )
    except Exception as exc:  # noqa: BLE001 — the error is the leg's detail
        load_ok = False
        load_detail = f"error={type(exc).__name__}:{exc}"
    else:
        keys = sorted(result.keys()) if isinstance(result, dict) else [type(result).__name__]
        load_detail = f"keys={keys}"
    load_leg = _leg(LEG_LOAD, load_ok, f"session={sid} cwd={cwd} {load_detail}")

    found = await _wait_for(
        lambda: _matching_changed(tape.notes[mark:], sid) is not None,
        _CHANGED_SECONDS,
    )
    matched = _matching_changed(tape.notes[mark:], sid)
    top = sorted(matched.keys()) if isinstance(matched, dict) else []
    # The method tape is incidental traffic during the wait. It is printed,
    # and it is not part of the detail compare_legs reads.
    changed_leg = _leg(
        LEG_CHANGED,
        True,
        f"arrived={'yes' if found else 'no'} top={top}",
    )
    changed_leg["tape"] = tape.methods_since(mark)

    before_queue = len(tape.queue_ids)
    ok1, detail1 = await client.prompt(sid, _PROMPT_PLAIN)
    acked = sid in tape.queue_ids[before_queue:]
    tape.arm_permission = True
    deadline = time.monotonic() + _PERMISSION_SECONDS
    ok2, detail2 = await client.prompt(sid, _PROMPT_TRUE)
    while tape.permission is None and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    approval = "asked" if tape.permission is not None else "not asked"
    prompt_leg = _leg(
        LEG_PROMPT,
        True,
        (
            f"first=({bool(ok1)},{detail1!r}) acked={'yes' if acked else 'no'} "
            f"second=({bool(ok2)},{detail2!r}) approval={approval}"
        ),
    )
    remembered = {"sessionId": sid, "cwd": cwd}
    return [list_leg, load_leg, changed_leg, prompt_leg], remembered, tape.removal_seen(sid)


async def _one_name(name: str, explicit_id: str, avoid_ids: set[str], stored: dict | None):
    grok_leader.CLIENT_NAME = name
    client = grok_leader.LeaderClient()
    try:
        connected = await client.connect()
        if not connected:
            return None
        return await _four_legs(client, explicit_id, avoid_ids, stored)
    finally:
        await client.close()


def _print_legs(legs: list[dict]) -> None:
    for leg in legs:
        print(f"{leg['leg']}\tok={leg['ok']}\t{leg['detail']}", flush=True)
        if "tape" in leg:
            print(f"tape\t{leg['tape']}", flush=True)


async def _run(args) -> str:
    names = [part.strip() for part in args.names.split(",") if part.strip()]
    if len(names) < 2:
        return "CANNOT TELL need two names"
    avoid = _live_tab_ids()
    print(f"AVOID: {len(avoid)} live tab(s)", flush=True)
    saved = grok_leader.CLIENT_NAME
    tables: list[list[dict]] = []
    stored = None
    try:
        for index, name in enumerate(names):
            print(f"== {name} ==", flush=True)
            try:
                outcome = await asyncio.wait_for(
                    _one_name(name, args.session_id or "", avoid, stored),
                    _NAME_GAP_SECONDS,
                )
            except asyncio.TimeoutError:
                print("timed out", flush=True)
                return f"CANNOT TELL leg timed out under {name}"
            if outcome is None:
                return f"CANNOT TELL connect failed under {name}"
            legs, remembered, removal = outcome
            _print_legs(legs)
            if remembered is None:
                return "CANNOT TELL no non-resident session to load"
            if stored is None:
                stored = remembered
                print(
                    f"SESSION: {stored['sessionId']} cwd={stored['cwd']}",
                    flush=True,
                )
            else:
                print(f"SESSION: {stored['sessionId']} (same session)", flush=True)
            tables.append(legs)
            if index < len(names) - 1:
                print(
                    f"removal: {'seen' if removal else 'not seen'}",
                    flush=True,
                )
                await asyncio.sleep(args.pause)
    finally:
        grok_leader.CLIENT_NAME = saved
    if len(tables) != 2:
        return "CANNOT TELL a leg did not finish"
    return compare_legs(tables[0], tables[1])


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--names",
        default="bob-companion,dark-army",
        help="comma-separated clientInfo.name values, one connect each",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=5.0,
        help="seconds between the two clients (default 5)",
    )
    parser.add_argument(
        "--session-id",
        default="",
        help="load this session instead of the newest non-resident one",
    )
    return parser.parse_args([] if argv is None else argv)


def main(argv: list[str] | None = None) -> int:
    # Pytest loads this module to test the pure parts. A live connect from
    # inside the suite is how a leader would be started by accident.
    if "pytest" in sys.modules:
        print("refusing: pytest is loaded", file=sys.stderr)
        return 2
    args = _parse(argv)
    if not grok_leader.leader_sock_present():
        print("CANNOT TELL leader not running; open a Grok tab first")
        return 2
    print(f"LEADER: {_leader_version()}", flush=True)
    try:
        verdict = asyncio.run(asyncio.wait_for(_run(args), _RUN_SECONDS))
    except asyncio.TimeoutError:
        verdict = "CANNOT TELL run exceeded 300s"
    except Exception as exc:  # noqa: BLE001 — the run must still print a verdict
        verdict = f"CANNOT TELL {type(exc).__name__}: {exc}"
    print(f"VERDICT: {verdict}", flush=True)
    if verdict == "EQUAL" or verdict.startswith("DIFFERS "):
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
