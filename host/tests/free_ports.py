# host/tests/free_ports.py
"""Listener ports for the tests, from a block chosen per run and per worker.

Every file that starts a real listener used to take its port by binding
``127.0.0.1:0``, closing the socket and handing the number on. That number
comes from macOS's ephemeral range (``net.inet.ip.portrange.first``, 49152
and up) — the same range every outgoing loopback connection in every other
worker draws its local port from. Across workers (``-n auto``, the CI
invocation) another worker's connection could take the port between the
close and the listener's bind, and ``ApiServer.start_lan`` logged
``phone access could not bind port …`` and left the phone door shut: a
test green alone and red under workers.

So the ports come from **below** the ephemeral range (20000-48999), where
the kernel never hands one out on its own, split into 58 blocks of 500.

- **Within one run, the workers never share a block** (up to 58 workers).
  The block is the run's offset plus the worker's index
  (``PYTEST_XDIST_WORKER``: ``gw3`` → 3), modulo 58. The run's offset is a
  hash of ``PYTEST_XDIST_TESTRUNUID``, which every worker of one ``-n`` run
  shares; a run without workers has none and draws the offset at random.
- **Two concurrent runs usually do not share a block, and are not
  guaranteed not to.** Their offsets are independent, so any two workers
  land in the same block one time in 58. The walk inside a block starts at
  a random position, so two runs sharing a block still rarely walk in step.
- **A port is probed before it is handed out**: a bind on ``0.0.0.0``,
  ``127.0.0.1`` and ``::1`` (the loopback server binds ``localhost``, both
  families; a Mac without IPv6 skips that probe). A port somebody holds at
  that moment is skipped. A port taken *after* the probe — by another run
  sharing the block, between the probe and the test's bind — is not
  caught; the per-run block makes that rare, not impossible.
- Within a process the walk only moves forward, so two calls never return
  the same port. A process that uses more than its 500 ports (a whole
  suite in one process) walks on into the next block rather than wrapping
  onto ports it bound a moment ago.

Isolation, never serialisation: no marker, no ``xdist_group``, no ``-n 0``.
"""
from __future__ import annotations

import errno
import hashlib
import os
import secrets
import socket

#: Above Dark Army's own fixed ports (19874, 19875) and below the ephemeral
#: range, which starts at 49152 on macOS.
FIRST_PORT = 20000
LAST_PORT = 48999
BLOCK = 500
BLOCKS = (LAST_PORT - FIRST_PORT + 1) // BLOCK


def _worker_index() -> int:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    digits = "".join(ch for ch in worker if ch.isdigit())
    return int(digits) if digits else 0


def _run_offset() -> int:
    run = os.environ.get("PYTEST_XDIST_TESTRUNUID", "")
    if run:
        return int.from_bytes(hashlib.sha256(run.encode()).digest()[:4], "big")
    return secrets.randbelow(BLOCKS)


_BASE = FIRST_PORT + ((_run_offset() + _worker_index()) % BLOCKS) * BLOCK
_cursor = secrets.randbelow(BLOCK)


def _held(family: int, host: str, port: int) -> bool:
    """True when `port` on `host` cannot be bound right now. A family or
    address this machine lacks (no IPv6, no ::1) holds nothing."""
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        sock.bind((host, port))
    except OSError as exc:
        return exc.errno not in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT)
    finally:
        sock.close()
    return False


def _bindable(port: int) -> bool:
    return not (_held(socket.AF_INET, "0.0.0.0", port)
                or _held(socket.AF_INET, "127.0.0.1", port)
                or _held(socket.AF_INET6, "::1", port))


def free_ports(count: int) -> list[int]:
    """`count` distinct ports that bind right now, from this block first."""
    global _cursor
    ports: list[int] = []
    for _ in range(BLOCKS * BLOCK):
        port = FIRST_PORT + (_BASE - FIRST_PORT + _cursor) % (BLOCKS * BLOCK)
        _cursor += 1
        if port not in ports and _bindable(port):
            ports.append(port)
            if len(ports) == count:
                return ports
    raise RuntimeError(f"no {count} free port(s) in {FIRST_PORT}-{LAST_PORT}")


def free_port() -> int:
    return free_ports(1)[0]
