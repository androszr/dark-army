# host/dark_army_daemon/relay_bot.py
"""A headless device on the relay socket.

The phone pairs by scanning a QR. A bot cannot, and it must not share the
phone's channel: the socket relay keeps one ``side=phone`` per channel and
the newest connection replaces the older one. ``pair_bot`` (loopback only)
mints a device of its own. This module saves that pair reply and speaks
the phone's sealed frames — ChaCha20-Poly1305, the relay key, the shared
counters — down ``side=phone``.

Run from ``host/`` with the project virtualenv:

    .venv/bin/python -m dark_army_daemon.relay_bot pair --name Grok
    .venv/bin/python -m dark_army_daemon.relay_bot state
    .venv/bin/python -m dark_army_daemon.relay_bot renew
    .venv/bin/python -m dark_army_daemon.relay_bot listen
    .venv/bin/python -m dark_army_daemon.relay_bot serve

``serve`` keeps Proxy's line open and answers a local MCP connector on
``127.0.0.1``. A tunnel in front of that port is what a Grok account
plugs in. The pair file holds the keys. It is mode 0600 under the state
directory. Nothing in this file prints a key or the connector token.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

from . import paths, relay

FILE_VERSION = 1
#: These three leave the pair file. A Grok bot asks for them on its own
#: secret prompt. The Mac keeps them in ``grok-bot.secrets``.
SECRET_FIELDS = ("token", "home_key", "relay_key")
_PEER_WAIT_SECONDS = 20.0
_ANSWER_SECONDS = 20.0


class BotError(Exception):
    """A refusal the command can print. The message names no key."""


class PairBusy(BotError):
    """Another process holds the pair file."""


def record_from_reply(reply: dict) -> dict:
    """The pair file's shape, taken from ``pair_bot``'s reply.

    Checks the channel id against the key before anything is written, so
    a truncated reply cannot become a file that dials the wrong mailbox.
    """
    if not isinstance(reply, dict) or not reply.get("ok"):
        detail = ""
        if isinstance(reply, dict):
            detail = str(reply.get("detail") or "")
        raise BotError(detail or "Dark Army refused the pairing")
    try:
        raw = base64.b64decode(str(reply.get("relay_key") or ""),
                               validate=True)
        home = base64.b64decode(str(reply.get("home_key") or ""),
                                validate=True)
    except (ValueError, TypeError) as exc:
        raise BotError("the pair reply was not a key") from exc
    if len(raw) != 32 or len(home) != 32:
        raise BotError("the pair reply was not a key")
    chan = relay.channel_id(raw)
    stated = str(reply.get("channel_id") or "")
    if stated != chan:
        raise BotError("the pair reply's channel did not match its key")
    ws_url = str(reply.get("relay_ws_url") or "")
    if not ws_url:
        raise BotError("the pair reply carried no socket address")
    device_id = str(reply.get("device_id") or "")
    if not device_id:
        raise BotError("the pair reply named no device")
    return {
        "version": FILE_VERSION,
        "name": str(reply.get("name") or ""),
        "device_id": device_id,
        "token": str(reply.get("token") or ""),
        "home_key": str(reply["home_key"]),
        "relay_key": str(reply["relay_key"]),
        "relay_url": str(reply.get("relay_url") or ""),
        "relay_ws_url": ws_url,
        "channel_id": chan,
        "send_ctr": 0,
        "recv_ctr": 0,
        "home_send_ctr": 0,
        "home_recv_ctr": 0,
    }


def write_record(path, record: dict) -> None:
    """Write the pair file at 0600, replacing any previous bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    os.chmod(path, 0o600)


def secrets_path(pair_path):
    """The sidecar beside a pair file. Same directory, fixed name."""
    return pair_path.with_name(paths.BOT_SECRETS_NAME)


def split_secrets(record: dict) -> tuple[dict, dict]:
    """``(public, secrets)``. The public half is what a bot may be given."""
    public = dict(record)
    secret = {}
    for key in SECRET_FIELDS:
        if public.get(key):
            secret[key] = public.pop(key)
    return public, secret


def _load_secrets(pair_path) -> dict:
    path = secrets_path(pair_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in SECRET_FIELDS if data.get(key)}


def _fill_secrets(record: dict, pair_path) -> set:
    """Copy secrets the pair file no longer holds. The returned set is what
    a later write of that file must leave out."""
    extra = _load_secrets(pair_path)
    borrowed = set()
    for key in SECRET_FIELDS:
        if record.get(key):
            continue
        if extra.get(key):
            record[key] = extra[key]
            borrowed.add(key)
    return borrowed


def _read_fd(fd: int) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = b""
    while True:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            break
        raw += chunk
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BotError("the pair file could not be read") from exc
    if not isinstance(record, dict) or int(record.get("version") or 0) != 1:
        raise BotError("the pair file could not be read")
    return record


def _write_fd(fd: int, record: dict) -> None:
    payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)


def _update(path, change=None) -> dict:
    """Read, change and write the pair file under a short exclusive lock.

    Every counter write goes through here, so a session and a ``renew``
    running beside it each re-read the file before writing and neither
    clobbers the other's counters. ``change`` may raise; nothing is
    written then. Returns the record as written (or read, with no change).
    """
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError as exc:
        raise BotError("there is no pair file yet — run pair first") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        record = _read_fd(fd)
        borrowed = _fill_secrets(record, path)
        if change is not None:
            change(record)
            stored = {key: value for key, value in record.items()
                      if key not in borrowed}
            _write_fd(fd, stored)
        return record
    finally:
        os.close(fd)


def _hold_session(path) -> int:
    """Take the one-socket lock beside the pair file, for a session's life.

    The relay keeps one ``side=phone`` per channel and the newest replaces
    the older, so two sessions would only knock each other off. The lock
    is a separate empty file: ``renew`` and the counter writes lock the
    pair file itself, briefly, and so run while a session is listening.
    """
    lock = path.with_name(path.name + ".session")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise BotError("there is no pair file yet — run pair first") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise PairBusy("another command is already on this socket") from exc
    return fd


class BotSession:
    """One socket as ``side=phone``, counters persisted in the pair file.

    One session at a time (`_hold_session`); each counter write re-reads
    the file under `_update`, so a ``renew`` beside a ``listen`` keeps its
    home counters and the session keeps its relay ones. ``peer:`` words are the relay's own, dropped
    the way the phone drops them; the first verified request is what arms
    the Mac's push.
    """

    def __init__(self, path=None):
        self.path = path if path is not None else paths.BOT_PAIR_PATH
        self.record: dict = {}
        self.ws = None
        self._lock_fd: int | None = None
        self._reader: asyncio.Task | None = None
        self._frames: asyncio.Queue = asyncio.Queue()
        self._pushes: list = []
        self._peer = False
        self._peer_event = asyncio.Event()
        self._closed = False

    async def __aenter__(self) -> "BotSession":
        if not self.path.exists():
            raise BotError("there is no pair file yet — run pair first")
        self._lock_fd = _hold_session(self.path)
        try:
            self.record = _update(self.path)
            import websockets
            url = str(self.record.get("relay_ws_url") or "").rstrip("/")
            chan = str(self.record.get("channel_id") or "")
            if not url or not chan:
                raise BotError("the pair file has no socket address")
            # ``origin=None``: a browser-shaped upgrade is refused by the
            # relay.
            self.ws = await websockets.connect(
                f"{url}/ws?ch={chan}&side=phone",
                origin=None,
                max_size=relay.RELAY_FRAME_MAX_BYTES + 1024,
                ping_interval=20,
                ping_timeout=20,
            )
        except BaseException:
            # ``__aexit__`` does not run when ``__aenter__`` raises.
            os.close(self._lock_fd)
            self._lock_fd = None
            raise
        self._reader = asyncio.get_running_loop().create_task(self._read())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _key(self) -> bytes:
        return base64.b64decode(str(self.record.get("relay_key") or ""),
                                validate=True)

    def _persist(self) -> None:
        """Write this session's relay counters, never lowering the file's,
        and leave every other key — the home counters above all — as the
        file has it."""
        if self._lock_fd is None:
            return
        mine = {k: int(self.record.get(k) or 0)
                for k in ("send_ctr", "recv_ctr")}

        def merge(record: dict) -> None:
            for key, value in mine.items():
                record[key] = max(int(record.get(key) or 0), value)

        merged = _update(self.path, merge)
        self.record["send_ctr"] = merged["send_ctr"]
        self.record["recv_ctr"] = merged["recv_ctr"]

    def _next_send(self) -> int:
        self.record["send_ctr"] = int(self.record.get("send_ctr") or 0) + 1
        self._persist()
        return int(self.record["send_ctr"])

    def _note_recv(self, ctr: int) -> None:
        if ctr > int(self.record.get("recv_ctr") or 0):
            self.record["recv_ctr"] = int(ctr)
            self._persist()

    def _open(self, message: str):
        try:
            key = self._key()
        except (ValueError, TypeError):
            return None
        frame, err = relay.open_frame(
            key, relay.DIR_MAC_TO_PHONE, message,
            int(self.record.get("recv_ctr") or 0))
        if err or frame is None:
            return None
        self._note_recv(int(frame["ctr"]))
        return frame

    async def _read(self) -> None:
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    continue
                if message.startswith("peer:"):
                    self._peer = message == "peer:1"
                    self._peer_event.set()
                    continue
                frame = self._open(message)
                if frame is not None:
                    await self._frames.put(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._peer_event.set()
            if not self._closed:
                await self._frames.put(None)

    async def _wait_peer(self, timeout: float) -> None:
        """Block until the relay says the Mac is on the other side.

        ``clear`` then re-check ``_peer``: a ``peer:1`` that lands in
        between must not be wiped and then waited for.
        """
        deadline = time.monotonic() + timeout
        while not self._peer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BotError("the Mac has not opened this channel yet")
            self._peer_event.clear()
            if self._peer:
                return
            try:
                await asyncio.wait_for(self._peer_event.wait(), remaining)
            except asyncio.TimeoutError as exc:
                if self._peer:
                    return
                raise BotError(
                    "the Mac has not opened this channel yet") from exc

    async def request(self, kind: str, body: dict, *,
                      timeout: float = _ANSWER_SECONDS,
                      _retried: bool = False) -> dict:
        """Send one sealed frame and return the Mac's answer frame.

        A stale-counter refusal fast-forwards once, the phone's rule, and
        the spent counter stays spent.
        """
        await self._wait_peer(timeout)
        frame_id = secrets.token_hex(8)
        ctr = self._next_send()
        try:
            wire = relay.seal_frame(
                self._key(), relay.DIR_PHONE_TO_MAC, ctr, kind, body,
                frame_id=frame_id)
        except (ValueError, TypeError) as exc:
            raise BotError("the pair file was not a key") from exc
        if not wire:
            raise BotError("that request was too large to send")
        await self.ws.send(wire)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BotError("the Mac did not answer")
            try:
                frame = await asyncio.wait_for(
                    self._frames.get(), remaining)
            except asyncio.TimeoutError as exc:
                raise BotError("the Mac did not answer") from exc
            if frame is None:
                raise BotError("the socket closed")
            payload = frame.get("body")
            payload = payload if isinstance(payload, dict) else {}
            if (str(frame.get("kind") or "") == "err"
                    and payload.get("ctr_expected") is not None
                    and not _retried):
                expected = int(payload["ctr_expected"])
                self.record["send_ctr"] = max(
                    int(self.record.get("send_ctr") or 0), expected - 1)
                self._persist()
                return await self.request(
                    kind, body, timeout=max(0.1, remaining), _retried=True)
            if str(frame.get("kind") or "") == "push":
                self._pushes.append(frame)
                continue
            if str(payload.get("re") or "") == frame_id:
                return frame


def _api_port() -> int:
    return int(os.environ.get("BOB_COMPANION_API_PORT", "19874"))


def _lan_port() -> int:
    return int(os.environ.get("BOB_COMPANION_LAN_PORT", "19875"))


#: Where the person's own tools find Dark Army's desk token. The file on disk
#: is the session token, which the device verbs refuse; the desk token lives
#: in memory and the person copies it from Settings → Advanced.
DESK_TOKEN_ENV = "DARK_ARMY_DESK_TOKEN"
DESK_TOKEN_MISSING = ("Dark Army's desk token is not set — Settings → Advanced → "
                      "Copy desk key, then export DARK_ARMY_DESK_TOKEN")


def post_loopback(payload: dict) -> dict:
    """One loopback ``/api/action``. The token stays in the header.

    The desk token, from ``DARK_ARMY_DESK_TOKEN`` — pairing and unpairing are
    desk verbs (`docs/transport-contract.md`, *The loopback door has two
    tokens*), so the on-disk session token would only be refused."""
    port = _api_port()
    token = os.environ.get(DESK_TOKEN_ENV, "").strip()
    if not token:
        raise BotError(DESK_TOKEN_MISSING)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/action",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Bob-Token": token,
            "Host": f"127.0.0.1:{port}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BotError("Dark Army is not running") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BotError("Dark Army refused the pairing") from exc
    if not isinstance(parsed, dict):
        raise BotError("Dark Army refused the pairing")
    return parsed


def cmd_pair(name: str, replace: bool) -> int:
    path = paths.BOT_PAIR_PATH
    if path.exists() and not replace:
        print(
            f"A pair file is already at {path}. "
            "Unpair that device in Devices, or pair again with --replace.")
        return 2
    if path.exists() and replace:
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        old_id = str(old.get("device_id") or "") if isinstance(old, dict) else ""
        if old_id:
            gone = post_loopback(
                {"action": "unpair_device", "device_id": old_id})
            if not gone.get("ok"):
                detail = str(gone.get("detail") or "")
                if "not paired" not in detail:
                    print(detail or "that device could not be unpaired")
                    return 1
    reply = post_loopback({"action": "pair_bot", "name": name})
    try:
        record = record_from_reply(reply)
    except BotError as exc:
        if replace and path.exists():
            print(str(exc))
            print(f"The previous device was unpaired. {path} is no longer "
                  "a live pair.")
            return 1
        raise
    public, secret = split_secrets(record)
    write_record(path, public)
    if secret:
        write_record(secrets_path(path), secret)
    record = public
    print(f"Paired {record['name']} as its own device.")
    print(f"Device id: {record['device_id']}")
    print(f"Saved the pair reply to {path}")
    print("It has its own channel, key and socket.")
    print("Its reads and writes are two grants set under Devices in Dark "
          "Army's settings, on the Mac or a paired phone: reads start with "
          "no timer, writes for 24 hours.")
    print("Unpair it from Devices to revoke the key.")
    return 0


def _print_answer(frame: dict) -> None:
    payload = frame.get("body")
    payload = payload if isinstance(payload, dict) else {}
    status = payload.get("status")
    if str(frame.get("kind") or "") == "err" or status not in (200, None):
        detail = str(payload.get("error") or payload.get("detail") or "")
        print(detail or f"the Mac answered {status}")
        return
    body = payload.get("body")
    if isinstance(body, str):
        print(body)
    else:
        print(json.dumps(payload))


async def _one_request(kind: str, body: dict) -> dict:
    async with BotSession() as session:
        return await session.request(kind, body)


def cmd_state() -> int:
    frame = asyncio.run(_one_request(
        "state", {"done": "review", "with_usage": True}))
    _print_answer(frame)
    return 0


def cmd_action(text: str) -> int:
    try:
        body = json.loads(text)
    except ValueError:
        print("the action has to be a JSON object")
        return 2
    if not isinstance(body, dict) or not str(body.get("action") or ""):
        print("the action has to name an action")
        return 2
    frame = asyncio.run(_one_request("action", body))
    _print_answer(frame)
    payload = frame.get("body") if isinstance(frame.get("body"), dict) else {}
    return 0 if payload.get("status") == 200 else 1


def cmd_listen() -> int:
    async def _run() -> None:
        async with BotSession() as session:
            frame = await session.request(
                "state", {"done": "review", "with_usage": True})
            _print_answer(frame)
            for pushed in session._pushes:
                _print_answer(pushed)
            session._pushes.clear()
            while True:
                nxt = await session._frames.get()
                if nxt is None:
                    raise BotError("the socket closed")
                if str(nxt.get("kind") or "") == "push":
                    _print_answer(nxt)
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_renew() -> int:
    """One sealed home frame: a state read over the home door, which says
    whether this device can reach Dark Army at home and whether its Read
    grant is on. It renews nothing — the bot is not on a phone's day
    window; its reads and writes are the two grants a person sets under
    Devices (`docs/transport-contract.md`, *The bot's access is two
    grants*). A refused read prints the Mac's own words.

    It holds the pair file only for its two counter writes, not for the
    request, so it runs while ``listen`` has the socket."""
    path = paths.BOT_PAIR_PATH

    def home_key(record: dict) -> bytes:
        try:
            home = base64.b64decode(str(record.get("home_key") or ""),
                                    validate=True)
        except (ValueError, TypeError) as exc:
            raise BotError("the pair file was not a key") from exc
        if len(home) != 32:
            raise BotError("the pair file was not a key")
        return home

    def reserve(record: dict) -> None:
        home_key(record)
        record["home_send_ctr"] = int(record.get("home_send_ctr") or 0) + 1

    record = _update(path, reserve)
    home = home_key(record)
    send = int(record["home_send_ctr"])
    chan = relay.channel_id(home, ns=relay.HOME)
    wire = relay.seal_frame(
        home, relay.DIR_PHONE_TO_MAC, send, "state",
        {"done": "review"}, ns=relay.HOME)
    if not wire:
        raise BotError("that request was too large to send")
    port = _lan_port()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/home",
        data=wire.encode("ascii"),
        method="POST",
        headers={
            "Content-Type": "text/plain",
            "X-Bob-Channel": chan,
            "Host": f"127.0.0.1:{port}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise BotError(
            detail or "the home door refused the check-in") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BotError(
            "Phone access is off, so the home door cannot be "
            "reached from here") from exc
    if status != 200:
        raise BotError("the home door refused the check-in")
    text = raw.decode("ascii", "replace").strip()

    def note(record: dict) -> None:
        frame, err = relay.open_frame(
            home, relay.DIR_MAC_TO_PHONE, text,
            int(record.get("home_recv_ctr") or 0), ns=relay.HOME)
        if err or frame is None:
            raise BotError("the home door's answer did not open")
        record["home_recv_ctr"] = int(frame["ctr"])
        opened.append(frame)

    opened: list = []
    _update(path, note)
    refusal = _inner_refusal(opened[0] if opened else {})
    if refusal:
        raise BotError(refusal)
    print("Checked in at home. Reads and writes follow the two grants set "
          "under Devices in Dark Army's settings.")
    return 0


def _inner_refusal(frame: dict) -> str:
    """The Mac's own words when a sealed home answer is not a 200, else
    ``""``. A `reply` carries the inner status and a JSON body whose
    `detail` / `error` is the sentence; an `err` carries `error`."""
    body = frame.get("body") if isinstance(frame, dict) else None
    if not isinstance(body, dict):
        return ""
    try:
        status = int(body.get("status") or 200)
    except (TypeError, ValueError):
        status = 0
    if status == 200:
        return ""
    words = str(body.get("error") or "")
    inner = body.get("body")
    if isinstance(inner, str) and inner:
        try:
            parsed = json.loads(inner)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            words = str(parsed.get("detail") or parsed.get("error") or words)
    return words or f"the home door answered {status}"


MCP_HOST = "127.0.0.1"
MCP_PORT = 19876

_MCP_TOOLS = (
    {
        "name": "picture",
        "description": (
            "Read Dark Army's live picture over the relay line. "
            "Call this when asked whether the line is connected."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "act",
        "description": (
            "Send one action to Dark Army, the same writes a phone can "
            "make away from home. Include action and that action's fields."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action name.",
                },
            },
            "required": ["action"],
        },
    },
)


def load_or_create_mcp_token() -> str:
    """The connector bearer. Created once, mode 0600, never printed."""
    path = paths.BOT_MCP_TOKEN_PATH
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return token


def _bearer_ok(header: str, token: str) -> bool:
    text = str(header or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    if not text or not token:
        return False
    return hmac.compare_digest(text, token)


class _Line:
    """One phone-side socket for the life of the connector.

    A dropped line is opened again on the next call. Two calls share the
    pair-file lock, so the counters stay one sequence.
    """

    def __init__(self):
        self._session: BotSession | None = None
        self._lock = asyncio.Lock()

    async def request(self, kind: str, body: dict) -> dict:
        async with self._lock:
            last: Exception | None = None
            for attempt in (1, 2):
                try:
                    session = await self._open()
                    return await session.request(kind, body)
                except (BotError, OSError, ConnectionError) as exc:
                    last = exc
                    await self._drop()
                    if attempt == 2:
                        break
            raise BotError(str(last) if last else "the socket closed")

    async def _open(self) -> BotSession:
        if self._session is None:
            session = BotSession()
            await session.__aenter__()
            self._session = session
        return self._session

    async def _drop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.__aexit__(None, None, None)

    async def close(self) -> None:
        async with self._lock:
            await self._drop()


def _tool_text(frame: dict) -> tuple[str, bool]:
    payload = frame.get("body") if isinstance(frame.get("body"), dict) else {}
    status = payload.get("status")
    if str(frame.get("kind") or "") == "err" or status not in (200, None):
        detail = str(payload.get("error") or payload.get("detail") or "")
        return (detail or f"Dark Army answered {status}"), True
    body = payload.get("body")
    if not isinstance(body, str):
        body = json.dumps(payload)
    return "Proxy's line is open.\n" + body, False


async def _call_tool(line: _Line, name: str, arguments) -> dict:
    args = arguments if isinstance(arguments, dict) else {}
    if name == "picture":
        frame = await line.request(
            "state", {"done": "review", "with_usage": True})
        text, failed = _tool_text(frame)
        return {"content": [{"type": "text", "text": text}], "isError": failed}
    if name == "act":
        if not str(args.get("action") or ""):
            return {"content": [{"type": "text", "text":
                                 "the action has to name an action"}],
                    "isError": True}
        frame = await line.request("action", args)
        text, failed = _tool_text(frame)
        return {"content": [{"type": "text", "text": text}], "isError": failed}
    return {"content": [{"type": "text", "text": "that tool is not on this connector"}],
            "isError": True}


def _rpc_result(message: dict, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def _rpc_error(message: dict, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"),
            "error": {"code": code, "message": text}}


async def handle_mcp(message: dict, line: _Line) -> dict | None:
    """One JSON-RPC message. ``None`` when the message is a notification."""
    method = str(message.get("method") or "")
    if "id" not in message:
        return None
    if method == "initialize":
        return _rpc_result(message, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "Dark Army", "version": "1"},
        })
    if method == "ping":
        return _rpc_result(message, {})
    if method == "tools/list":
        return _rpc_result(message, {"tools": list(_MCP_TOOLS)})
    if method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            result = await _call_tool(
                line, str(params.get("name") or ""), params.get("arguments"))
        except BotError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}],
                      "isError": True}
        return _rpc_result(message, result)
    return _rpc_error(message, -32601, "method not found")


def _http_bytes(status: int, body: bytes, content_type: str,
                extra: dict | None = None) -> bytes:
    reason = {200: "OK", 202: "Accepted", 401: "Unauthorized",
              404: "Not Found", 405: "Method Not Allowed",
              400: "Bad Request"}.get(status, "OK")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Connection": "close",
    }
    if extra:
        headers.update(extra)
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


async def _read_http(reader: asyncio.StreamReader):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            return None
        data += chunk
        if len(data) > 1_048_576:
            return None
    head, rest = data.split(b"\r\n\r\n", 1)
    try:
        lines = head.decode("iso-8859-1").split("\r\n")
        method, target, _version = lines[0].split(" ", 2)
    except ValueError:
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    try:
        length = int(headers.get("content-length") or "0")
    except ValueError:
        return None
    if length < 0 or length > 1_000_000:
        return None
    while len(rest) < length:
        chunk = await reader.read(length - len(rest))
        if not chunk:
            break
        rest += chunk
    path = target.split("?", 1)[0]
    return method.upper(), path, headers, rest[:length]


async def _serve_client(reader, writer, token: str, line: _Line) -> None:
    try:
        parsed = await _read_http(reader)
        if parsed is None:
            writer.write(_http_bytes(400, b'{"error":"bad request"}',
                                     "application/json"))
            await writer.drain()
            return
        method, path, headers, body = parsed
        if path not in ("/mcp", "/"):
            writer.write(_http_bytes(404, b'{"error":"not found"}',
                                     "application/json"))
            await writer.drain()
            return
        if method == "GET" and path == "/":
            writer.write(_http_bytes(
                200, b"Dark Army connector\n", "text/plain"))
            await writer.drain()
            return
        if method != "POST" or path != "/mcp":
            writer.write(_http_bytes(405, b'{"error":"post /mcp"}',
                                     "application/json"))
            await writer.drain()
            return
        if not _bearer_ok(headers.get("authorization", ""), token):
            writer.write(_http_bytes(401, b'{"error":"unauthorized"}',
                                     "application/json"))
            await writer.drain()
            return
        try:
            message = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            writer.write(_http_bytes(
                400, b'{"error":"bad request"}', "application/json"))
            await writer.drain()
            return
        if not isinstance(message, dict):
            writer.write(_http_bytes(
                400, b'{"error":"bad request"}', "application/json"))
            await writer.drain()
            return
        reply = await handle_mcp(message, line)
        if reply is None:
            writer.write(_http_bytes(202, b"", "application/json"))
        else:
            extra = {"Mcp-Session-Id": "proxy"} if (
                message.get("method") == "initialize") else None
            writer.write(_http_bytes(
                200, json.dumps(reply).encode("utf-8"),
                "application/json", extra))
        await writer.drain()
    except (asyncio.CancelledError, ConnectionError, OSError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def start_connector(host: str = MCP_HOST, port: int = 0):
    """Bind the connector. Returns ``(server, line, token, port)``."""
    if host != MCP_HOST:
        raise BotError("the connector listens on this Mac only")
    token = load_or_create_mcp_token()
    line = _Line()
    server = await asyncio.start_server(
        lambda r, w: _serve_client(r, w, token, line), host, port)
    bound = server.sockets[0].getsockname()[1]
    return server, line, token, bound


def cmd_serve() -> int:
    if not paths.BOT_PAIR_PATH.is_file():
        print("there is no pair file yet — run pair first")
        return 2
    load_or_create_mcp_token()

    async def _run() -> None:
        server, line, _token, _port = await start_connector(MCP_HOST, MCP_PORT)
        print(f"Connector: http://{MCP_HOST}:{MCP_PORT}/mcp")
        print(f"Token file: {paths.BOT_MCP_TOKEN_PATH}")
        print("Tools: picture, act")
        try:
            await server.serve_forever()
        finally:
            server.close()
            await server.wait_closed()
            await line.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"the connector could not listen: {exc.strerror or exc}")
        return 1
    return 0


def cmd_show() -> int:
    path = paths.BOT_PAIR_PATH
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("there is no pair file yet — run pair first")
        return 2
    if not isinstance(record, dict):
        print("the pair file could not be read")
        return 2
    print(f"Name: {record.get('name') or ''}")
    print(f"Device id: {record.get('device_id') or ''}")
    print(f"Socket: {record.get('relay_ws_url') or ''}")
    print(f"Send counter: {record.get('send_ctr')}")
    print(f"Receive counter: {record.get('recv_ctr')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="relay_bot",
        description="Pair a headless device and speak on its relay socket.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    pair = sub.add_parser("pair", help="mint a device and save the pair reply")
    pair.add_argument("--name", default="", help="the Devices row's name")
    pair.add_argument("--replace", action="store_true",
                      help="unpair the saved device, then mint another")
    sub.add_parser("show", help="print the name, device id and socket address")
    sub.add_parser("state", help="one sealed state read over the socket")
    act = sub.add_parser("action", help="one sealed action over the socket")
    act.add_argument("body", help="a JSON object naming the action")
    sub.add_parser("listen", help="arm the socket and print each push")
    sub.add_parser("renew", help="one home check-in: a state read over the home door, which renews nothing")
    sub.add_parser("serve", help="keep the line open and answer the local connector")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "pair":
            return cmd_pair(args.name, args.replace)
        if args.cmd == "show":
            return cmd_show()
        if args.cmd == "state":
            return cmd_state()
        if args.cmd == "action":
            return cmd_action(args.body)
        if args.cmd == "listen":
            return cmd_listen()
        if args.cmd == "renew":
            return cmd_renew()
        if args.cmd == "serve":
            return cmd_serve()
    except BotError as exc:
        print(str(exc))
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
