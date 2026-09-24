"""The hook door: where `dark-army-notify` hands the daemon each hook event.

A client connects, writes one JSON object and a newline, and either hangs up
or waits for one JSON line back. Only a handler that returns something
produces that line (the statusline collector asks for the other agents that
way); every hook event is fire-and-forget.

**The door is a private Unix socket** (`paths.HOOK_SOCK_PATH`,
`~/.dark-army/hook.sock`): 0600 inside the 0700 state folder, so only this
account can open it, and a connection whose peer reads as another account is
closed unread. The loopback port (`127.0.0.1:19873`) stays open for one
release as a **bridge** for sessions started before the upgrade — terminals
an older pty broker opened, channel processes already running — and logs the
first message it hears from each session so its retirement can be judged.
Every message carries the project's enrolment key, and a port is first come,
first served: whoever binds it while Dark Army restarts hears those keys.
`docs/hook-door-contract.md`.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import socket
from typing import Awaitable, Callable, Optional

from . import paths

logger = logging.getLogger("dark-army.socket")

HOOK_IPC_HOST = "127.0.0.1"

#: Names the hook socket's path for every client and for the daemon itself.
#: Set, it is the one address a client uses (`docs/hook-door-contract.md`,
#: the address rule).
SOCKET_ENV = "DARK_ARMY_HOOK_SOCKET"


def _configured_port() -> int:
    # The bridge listener. The hook scripts read the same variables, so an
    # override moves both ends together. The older variable name is still
    # read after the current one. Retired with the bridge.
    chosen = os.environ.get("BOB_COMPANION_PORT") or os.environ.get("CLAWD_TANK_PORT", "19873")
    return int(chosen)


def _configured_sock() -> str:
    return os.environ.get(SOCKET_ENV) or str(paths.HOOK_SOCK_PATH)


HOOK_IPC_PORT = _configured_port()
HOOK_SOCK_PATH = _configured_sock()

#: `sun_path` is 104 bytes on macOS, the terminating NUL included. A path
#: longer than this cannot be bound; the door logs an ERROR and serves the
#: bridge alone rather than refusing to start.
MAX_SOCK_PATH_BYTES = 103

#: How long the stale-file probe waits for an answer before deciding.
STALE_PROBE_SECONDS = 0.5

#: How many distinct sessions the bridge remembers having logged. Past this
#: the memory is dropped and logging starts again: a bound, not a guarantee.
TCP_SEEN_MAX = 256

# How long a connected client has to deliver its line before we hang up.
READ_DEADLINE_SECONDS = 5.0

Handler = Callable[[dict], Awaitable[Optional[dict]]]


def sock_path_usable(path: str) -> bool:
    """True when `path` names a socket this Mac can bind."""
    if not path:
        return False
    try:
        return len(os.fsencode(path)) <= MAX_SOCK_PATH_BYTES
    except (TypeError, ValueError):
        return False


def _peer_uid(sock) -> Optional[int]:
    """The uid on the other end of an accepted Unix connection, or None.

    `getpeereid(2)` over `ctypes`, a capability probe: no reading at all
    (no such call, a closed descriptor, anything odd) is None, and None is
    accepted — the 0700 folder is the boundary and this is belt and braces.
    """
    try:
        fd = sock.fileno()
        getpeereid = ctypes.CDLL(None, use_errno=True).getpeereid
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32),
                               ctypes.POINTER(ctypes.c_uint32)]
        getpeereid.restype = ctypes.c_int
        uid = ctypes.c_uint32()
        gid = ctypes.c_uint32()
        if getpeereid(int(fd), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            return None
        return int(uid.value)
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _clear_stale_socket(path: str) -> None:
    """Make room for the bind.

    A leftover file from a dead run answers nothing and is unlinked. One
    that answers is unlinked too, with a WARNING: `daemon.lock` already
    proves no second daemon is running, so whatever answers there is not
    one — and nothing else may hold this name. (The pty broker refuses to
    start over an answerer instead, because a live broker owns terminals;
    this door owns nothing but its name.)
    """
    if not os.path.lexists(path):
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(STALE_PROBE_SECONDS)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        pass
    except OSError:
        # Not a socket, or a socket we cannot ask: the name is still ours.
        pass
    else:
        logger.warning(
            "Something was answering on the hook socket %s; replacing it "
            "(no other Dark Army daemon holds the lock)", path)
    finally:
        try:
            probe.close()
        except OSError:
            pass
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove the old hook socket %s: %s", path, exc)


class SocketServer:
    """Serves one message per connection to `on_message`, on the private
    hook socket and on the bridge port."""

    def __init__(self, on_message: Handler, host: str = HOOK_IPC_HOST,
                 port: int = HOOK_IPC_PORT, path: Optional[str] = HOOK_SOCK_PATH):
        self._deliver = on_message
        self._address = (host, port)
        #: `""` or None means no private socket at all (the bridge alone).
        self._path = str(path or "")
        self._listener: Optional[asyncio.Server] = None
        self._unix: Optional[asyncio.Server] = None
        #: `(st_dev, st_ino)` of the socket this process bound, so `stop()`
        #: never unlinks a successor's door. None: nothing bound, or no
        #: reading, and then nothing is unlinked.
        self._sock_ident: Optional[tuple] = None
        #: One WARNING per process for a refused peer.
        self._peer_warned = False
        #: Session ids (first twelve characters) the bridge has logged.
        self._tcp_seen: set = set()

    @property
    def unix_path(self) -> str:
        """The private socket's path while it is bound and served, else `""`
        — the value a hosted terminal is told (`ptyhost.PtyHost.hook_sock`).
        Read after `start()`: a child told a socket nobody serves would, by
        the one address rule, report nowhere at all."""
        return self._path if self._unix is not None else ""

    async def start(self) -> None:
        if self._path:
            if sock_path_usable(self._path):
                await self._start_unix()
            else:
                logger.error(
                    "The hook socket path is %d bytes, over the %d this Mac "
                    "can bind (%s); hooks are served on the network port alone",
                    len(os.fsencode(self._path)), MAX_SOCK_PATH_BYTES, self._path)
        host, port = self._address
        try:
            self._listener = await asyncio.start_server(
                self._handle_tcp_client, host, port, reuse_address=True)
        except OSError:
            if self._unix is None:
                # Neither door: today's behaviour, the daemon does not start.
                raise
            # Somebody else holds the port — possibly another account, which
            # is the very thing the private socket exists for. Never fatal
            # while the socket serves.
            self._listener = None
            logger.error("Hook bridge unavailable on %s:%d, port taken; the "
                         "private socket serves alone", host, port)
            return
        logger.info("Hook bridge open on %s:%d", host, port)

    async def _start_unix(self) -> None:
        path = self._path
        parent = os.path.dirname(path)
        try:
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            _clear_stale_socket(path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # The umask window is this one bind(): process-wide, so it is
            # kept as narrow as a syscall and the chmod below is the belt.
            old = os.umask(0o177)
            try:
                sock.bind(path)
            except OSError:
                sock.close()
                raise
            finally:
                os.umask(old)
            self._unix = await asyncio.start_unix_server(
                self._handle_unix_client, sock=sock)
        except OSError as exc:
            logger.error("Could not open the hook socket %s (%s); hooks are "
                         "served on the network port alone", path, exc)
            self._unix = None
            return
        try:
            os.chmod(path, 0o600)
        except OSError:
            logger.warning("Could not restrict the hook socket %s to 0600", path)
        try:
            st = os.stat(path)
            self._sock_ident = (st.st_dev, st.st_ino)
        except OSError:
            self._sock_ident = None
        logger.info("Hook socket open on %s", path)

    async def _handle_unix_client(self, reader, writer) -> None:
        uid = _peer_uid(writer.get_extra_info("socket"))
        if uid is not None and uid != os.getuid():
            if not self._peer_warned:
                self._peer_warned = True
                logger.warning(
                    "Hook socket refused a connection from another account "
                    "(uid %d); further refusals are not logged", uid)
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        await self._handle_client(reader, writer, "socket")

    async def _handle_tcp_client(self, reader, writer) -> None:
        await self._handle_client(reader, writer, "tcp")

    async def _handle_client(self, reader, writer, via: str) -> None:
        try:
            await self._serve_one(reader, writer, via)
        except TimeoutError:
            logger.warning("Hook client sent nothing within %.0fs", READ_DEADLINE_SECONDS)
        except Exception:
            logger.exception("Hook socket failed while serving a client")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _serve_one(self, reader, writer, via: str = "tcp") -> None:
        # readline() is the message boundary, whatever the stream did to the bytes.
        raw = await asyncio.wait_for(reader.readline(), timeout=READ_DEADLINE_SECONDS)
        if not raw:
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            logger.error("Hook socket got a line that is not JSON: %r", raw[:200])
            return
        if via == "tcp":
            self._note_bridge(message)
        answer = await self._deliver(message)
        if answer is None:
            return
        writer.write(json.dumps(answer).encode("utf-8") + b"\n")
        await writer.drain()

    def _note_bridge(self, message) -> None:
        """One INFO line per session the first time it arrives over the port.

        The session id's first twelve characters and the event name — never
        the key, a folder or any text — so the log says who still uses the
        bridge and nothing about what they said."""
        if not isinstance(message, dict):
            return
        sid = str(message.get("session_id") or "")[:12]
        if sid in self._tcp_seen:
            return
        if len(self._tcp_seen) >= TCP_SEEN_MAX:
            self._tcp_seen.clear()
        self._tcp_seen.add(sid)
        event = str(message.get("event") or "")[:40]
        logger.info("hook over the network port from session %s (%s); the "
                    "private socket is %s", sid or "(none)", event or "?",
                    self._path or "(none)")

    async def stop(self) -> None:
        listener, self._listener = self._listener, None
        unix, self._unix = self._unix, None
        for server in (unix, listener):
            if server is None:
                continue
            server.close()
            await server.wait_closed()
        ident, self._sock_ident = self._sock_ident, None
        if unix is None or ident is None:
            return
        try:
            st = os.stat(self._path)
        except OSError:
            return
        if (st.st_dev, st.st_ino) != ident:
            # Somebody bound the name after us — a successor's door.
            return
        try:
            os.unlink(self._path)
        except OSError:
            pass
