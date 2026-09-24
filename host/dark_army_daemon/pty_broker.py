"""The process that owns Dark Army's PTYs so they survive a Dark Army restart.

The menu-bar daemon holds the master fd today, so quitting the app closes
it and SIGHUPs every hosted agent. This helper is a sibling process: it
opens the ptys, keeps the emulator grid in memory, and speaks a JSON-line
protocol over `paths.PTY_SOCK_PATH`. A new daemon connects, takes a
snapshot of every live terminal, and keeps reading. Nothing is written to
disk except the socket and a pid file — the ring never reaches
`_PRIVATE_FILES`.

Quit of Dark Army leaves this process up while any terminal is still
live. Close terminal still kills that child. A `shutdown` op (unused on
the restart path) closes everyone and exits.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import signal
import socket
import sys
import time

from . import origin, ptyhost
from .paths import PTY_PID_PATH, PTY_SOCK_PATH, ensure_state_dir
from .socket_server import HOOK_IPC_PORT

logger = logging.getLogger("dark-army.pty_broker")

#: How often the broker asks itself whether it still has a reason to run.
ORPHAN_CHECK_SECONDS = 30.0
#: How long with no daemon connected — and no live terminal — before it
#: quits. A broker holding a live child exits on this timer only when it
#: has also been stranded (`_stranded`); otherwise it waits.
IDLE_EXIT_SECONDS = 600.0

#: Ops that may block for seconds; handled as their own task so writes
#: and resizes queued behind them on the socket keep flowing.
_SLOW_OPS = frozenset({"drain", "close", "close_all"})


def _describe(term: ptyhost.PtyTerminal) -> dict:
    return {
        "handle": term.handle,
        "pid": term.pid,
        "name": term.name,
        "root": term.root,
        "session_id": term.session_id,
        "cols": term.cols,
        "rows": term.rows,
        "exited": term.exited,
        "returncode": term.returncode,
        "bytes_read": term.bytes_read,
        "ring_b64": base64.b64encode(bytes(term.ring)).decode("ascii"),
        "screen": term.screen.snapshot(),
    }


class Broker:
    def __init__(self, port: int, sock: str, pidfile: str, hook_sock: str = ""):
        # `hook_sock` is the daemon's private hook socket: every terminal
        # this broker opens is told it, and only a broker from before that
        # socket existed falls back to handing its children the port.
        self.engine = ptyhost.PtyHost(port=port, hook_sock=hook_sock or None)
        ptyhost.install(self.engine)
        self.sock = sock
        self.pidfile = pidfile
        self._writer: asyncio.StreamWriter | None = None
        self._server = None
        self._watch_task = None
        self._last_client_at = time.monotonic()
        #: `(st_dev, st_ino)` of the socket this process actually bound,
        #: recorded in `start()` and written nowhere else. `None` means we
        #: never got a reading and must never guess.
        self._sock_ident: tuple | None = None
        #: One log line per process for a stat that could not answer, so a
        #: sick volume does not write a line every `ORPHAN_CHECK_SECONDS`.
        self._stat_warned = False

    async def start(self) -> None:
        ensure_state_dir()
        self._unlink_stale_socket()
        self.engine.on_data = self._on_data
        self.engine.on_exit = self._on_exit
        parent = os.path.dirname(self.sock)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._client, path=self.sock, limit=ptyhost.BROKER_LINE_LIMIT)
        try:
            os.chmod(self.sock, 0o600)
        except OSError:
            pass
        try:
            st = os.stat(self.sock)
            self._sock_ident = (st.st_dev, st.st_ino)
        except OSError:
            self._sock_ident = None
        with open(self.pidfile, "w") as fh:
            fh.write(str(os.getpid()))
        try:
            os.chmod(self.pidfile, 0o600)
        except OSError:
            pass
        self._last_client_at = time.monotonic()
        self._watch_task = asyncio.get_running_loop().create_task(self._watch())
        logger.info("pty broker listening on %s", self.sock)

    def _unlink_stale_socket(self) -> None:
        """A leftover socket from a dead broker would block bind — but one a
        live broker is answering on is that broker's terminals. Unlinking it
        strands them for good, so this asks first and refuses to start when
        somebody answers. The daemon never gets here (it spawns only when
        nothing is listening); the broker is a separately startable process
        and does not trust its caller."""
        if not os.path.exists(self.sock):
            return
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(self.sock)
        except (ConnectionRefusedError, FileNotFoundError):
            pass
        except OSError as exc:
            logger.error("could not tell whether a pty broker is live on %s: "
                         "%s", self.sock, exc)
            sys.exit(3)
        else:
            logger.error("another pty broker is live on %s", self.sock)
            sys.exit(3)
        finally:
            try:
                probe.close()
            except OSError:
                pass
        try:
            os.unlink(self.sock)
        except OSError:
            pass

    async def _watch(self) -> None:
        """The idle exit. Never raises out of its own loop."""
        while True:
            try:
                await asyncio.sleep(ORPHAN_CHECK_SECONDS)
                if self._should_exit():
                    if self._stranded():
                        logger.info(
                            "pty broker: nobody can reach %s any more; "
                            "closing %d terminal(s) and exiting",
                            self.sock, len(self.engine.terminals()))
                    else:
                        logger.info("pty broker: nothing live and nobody "
                                    "connected; exiting")
                    await self.engine.close_all()
                    asyncio.get_running_loop().stop()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("pty broker watch failed", exc_info=True)

    def _stranded(self) -> bool:
        """True when nobody will ever find this broker again.

        Identity, not `os.path.exists`: a *later* broker's socket sitting at
        the same path is a different inode and means exactly the same thing
        for this process — every future daemon connects to that one, and the
        terminals here are unreachable for good. Two refusals, both about
        never guessing: an unrecorded `_sock_ident` (we never got a reading
        of our own door) and a daemon connected right now — that connection
        *is* reachability, whatever the path says.

        A third refusal, for the same reason: only a stat that *answers*
        "there is nothing at that path" strands this broker. Any other
        `OSError` — EIO, a home directory on a volume that briefly went
        away, a mode broken by something other than `ensure_state_dir` — is
        "cannot tell", and this rung now closes live terminals on its way
        out. Stay up, and say so once.
        """
        if self._sock_ident is None:
            return False
        if self._writer is not None:
            return False
        try:
            st = os.stat(self.sock)
        except (FileNotFoundError, NotADirectoryError):
            return True
        except OSError as exc:
            if not self._stat_warned:
                self._stat_warned = True
                logger.warning(
                    "pty broker: cannot read %s (%s); staying up rather "
                    "than closing terminals on a reading that did not answer",
                    self.sock, exc)
            return False
        return (st.st_dev, st.st_ino) != self._sock_ident

    def _should_exit(self) -> bool:
        # The stranded rung outranks the live-terminal rung deliberately: a
        # terminal nobody can reach is not a reason to stay up, and
        # `close_all()` on the way out is what stops the leak moving down a
        # level to the pty children.
        if self._stranded():
            return True
        if any(not t.exited for t in self.engine.terminals()):
            return False
        if self._writer is not None:
            return False
        return (time.monotonic() - self._last_client_at) >= IDLE_EXIT_SECONDS

    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        # One daemon at a time: a restart's new connection displaces the
        # dying one, which is how the grid is handed over.
        if self._writer is not None and self._writer is not writer:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = writer
        self._last_client_at = time.monotonic()
        await self._send({"hello": True, "pid": os.getpid(),
                          "hook_sock": self.engine.hook_sock or "",
                          "terminals": [_describe(t) for t in self.engine.terminals()]})
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if str(msg.get("op") or "") in _SLOW_OPS:
                    # A drain or a close can wait seconds; every keystroke
                    # behind it on this socket must not.
                    asyncio.get_running_loop().create_task(self._handle(msg))
                else:
                    await self._handle(msg)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except (ValueError, asyncio.LimitOverrunError) as exc:
            logger.warning("pty broker: client line exceeded %d bytes; "
                           "dropping that client (%s)",
                           ptyhost.BROKER_LINE_LIMIT, exc)
        finally:
            self._last_client_at = time.monotonic()
            if self._writer is writer:
                self._writer = None
            try:
                writer.close()
            except Exception:
                pass

    async def _handle(self, msg: dict) -> None:
        op = str(msg.get("op") or "")
        req_id = msg.get("id")
        engine = self.engine
        reply: dict = {"id": req_id, "ok": True}
        try:
            if op == "start":
                # The attribution stamp, and nothing else. This process runs
                # on its own and an unfiltered `env` here would be a general
                # environment injector reachable over the broker socket, so
                # only `origin.ENV_KEY_RE`'s one key survives — the rest of
                # the child's environment is `subprocess_env.clean_env`'s
                # business, exactly as before.
                #
                # Merged **onto this process's own environment**, never sent
                # as the whole of the child's: `PtyHost.start` hands `env`
                # straight to `subprocess_env.clean_env`, which copies what it
                # is given, so a bare `{ORIGIN: …}` would start the assistant
                # with no PATH and no HOME. `None` keeps `clean_env`'s own
                # `os.environ` default, which is what this op always did.
                raw_env = msg.get("env")
                env = None
                if isinstance(raw_env, dict):
                    allowed = {k: v for k, v in raw_env.items()
                               if isinstance(k, str) and isinstance(v, str)
                               and origin.ENV_KEY_RE.match(k)}
                    if allowed:
                        env = dict(os.environ)
                        env.update(allowed)
                ok, detail, pid = await engine.start(
                    str(msg.get("root") or ""),
                    list(msg.get("argv") or []),
                    str(msg.get("name") or "agent"),
                    env=env,
                    cols=msg.get("cols"), rows=msg.get("rows"))
                handle = engine.owns(pid) if ok else None
                reply.update(ok=ok, detail=detail, pid=pid, handle=handle)
                if ok and handle:
                    term = engine.get(handle)
                    if term is not None:
                        reply["terminal"] = _describe(term)
            elif op == "write":
                handle = str(msg.get("handle") or "")
                blob = None
                b64 = msg.get("b64")
                if isinstance(b64, str) and b64:
                    try:
                        blob = base64.b64decode(b64)
                    except (ValueError, TypeError):
                        blob = None
                if blob is not None:
                    reply["ok"] = bool(engine.write(handle, data=blob))
                else:
                    reply["ok"] = bool(engine.write(handle, str(msg.get("text") or "")))
            elif op == "resize":
                handle = str(msg.get("handle") or "")
                reply["ok"] = bool(engine.resize(
                    handle, int(msg.get("cols") or 0), int(msg.get("rows") or 0)))
            elif op == "bind":
                reply["ok"] = bool(engine.bind(
                    str(msg.get("handle") or ""), str(msg.get("session_id") or "")))
            elif op == "unbind":
                engine.unbind(str(msg.get("session_id") or ""))
            elif op == "close":
                handle = str(msg.get("handle") or "")
                reply["ok"] = bool(await engine.close(handle))
            elif op == "close_all":
                await engine.close_all()
            elif op == "pending":
                reply["n"] = int(engine.pending(str(msg.get("handle") or "")))
            elif op == "drain":
                handle = str(msg.get("handle") or "")
                timeout = float(msg.get("timeout") or 0)
                reply["ok"] = bool(await engine.drain(handle, timeout))
            elif op == "reap":
                engine.reap()
                reply["terminals"] = [_describe(t) for t in engine.terminals()]
            elif op == "shutdown":
                await engine.close_all()
                await self._send({**reply, "ok": True})
                asyncio.get_running_loop().stop()
                return
            else:
                reply.update(ok=False, detail="unknown op")
        except Exception as exc:
            logger.debug("broker op %s failed", op, exc_info=True)
            reply.update(ok=False, detail=str(exc) or "broker failed")
        await self._send(reply)

    def _on_data(self, handle: str, data: bytes) -> None:
        # Base64, never a decoded string: a 64 KB read routinely ends inside
        # a three-byte box-drawing character, and `decode(..., "replace")`
        # per chunk turned each such seam into two U+FFFD and put the
        # daemon's byte counter six bytes ahead of this one.
        #
        # One task, not one per slice: the slices are the terminal's output
        # and their order is the screen.
        asyncio.get_running_loop().create_task(
            self._send_frames(handle, bytes(data)))

    async def _send_frames(self, handle: str, data: bytes) -> None:
        """One `data` line per `DATA_FRAME_BYTES` of raw output, in order.
        A whole 64 KB read as one line was ~87 KB of JSON — over asyncio's
        default limit, which is what an un-upgraded daemon still reads at."""
        step = ptyhost.DATA_FRAME_BYTES
        for start in range(0, len(data), step):
            await self._send({"event": "data", "handle": handle,
                              "b64": base64.b64encode(
                                  data[start:start + step]).decode("ascii")})

    def _on_exit(self, handle: str, returncode) -> None:
        asyncio.get_running_loop().create_task(
            self._send({"event": "exit", "handle": handle,
                        "returncode": returncode}))

    async def _send(self, obj: dict) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionError, OSError):
            self._writer = None


def main(argv: list[str] | None = None) -> int:
    # Before anything spawns: every terminal this broker ever opens inherits
    # a login shell's PATH, even from a broker older than its caller.
    from . import subprocess_env
    subprocess_env.adopt_login_path()
    parser = argparse.ArgumentParser(prog="pty-broker")
    parser.add_argument("--port", type=int, default=HOOK_IPC_PORT)
    parser.add_argument("--hook-sock", default="")
    parser.add_argument("--sock", default="")
    parser.add_argument("--pidfile", default="")
    args = parser.parse_args(argv)
    sock = args.sock or str(PTY_SOCK_PATH)
    if args.pidfile:
        pidfile = args.pidfile
    elif args.sock:
        # `paths._home()` is per-pid under pytest, so deriving this from
        # `PTY_PID_PATH` in the broker's own process writes it where the
        # caller cannot find it. Beside the socket it was given, instead.
        pidfile = os.path.join(os.path.dirname(args.sock) or ".", "pty.pid")
    else:
        pidfile = str(PTY_PID_PATH)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (ValueError, OSError):
        pass

    async def _run():
        broker = Broker(args.port, sock, pidfile, args.hook_sock)
        await broker.start()
        assert broker._server is not None
        async with broker._server:
            await broker._server.serve_forever()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
