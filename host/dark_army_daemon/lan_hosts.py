"""Which addresses of this Mac a phone could actually reach.

Pairing used to name exactly one address, derived by UDP-connecting to
``8.8.8.8:80`` and reading ``getsockname()`` — which asks the routing table,
and on a machine running a VPN answers with the tunnel. A phone on the same
Wi-Fi can never reach that, so pairing failed with "Could not reach the Mac."
on a Mac that was sitting on a perfectly reachable LAN address.

So the QR carries an **ordered candidate list** instead: real LAN addresses
first (the default-route one promoted to the front), then the Bonjour
``.local`` name. Tunnel addresses are **not** candidates: the phone door
refuses a knock that lands on one (`arrived_on_tunnel`, answered 421), and
a phone that walks its list onto a tunnel would only be turned away. The
listener binds ``0.0.0.0``, so every candidate reaches the same server once
it is routable — the ordering is about which one the phone should *try*
first, nothing more.

Everything here is pure except the two named readers at the bottom, which is
what makes the whole thing testable without a routing table: the tests inject
a mapping and never open a socket.
"""

from __future__ import annotations

import logging
import socket
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)

#: Interface-name prefixes that mean "this is a tunnel, not the LAN". A
#: phone on your Wi-Fi cannot reach any of them, and the door turns away a
#: knock that lands on one (`arrived_on_tunnel`), so they are never offered
#: as candidates — `refusal` names the tunnel-only case in words instead.
TUNNEL_PREFIXES = ("utun", "ipsec", "ppp", "tun", "tap", "wg")


def is_tunnel(name: str) -> bool:
    """Whether an interface name looks like a tunnel."""
    return (name or "").strip().lower().startswith(TUNNEL_PREFIXES)


def _usable(addr: str) -> bool:
    """Loopback and link-local are dropped: 127.0.0.0/8 names this machine
    to itself, and 169.254.0.0/16 is what an interface holds when DHCP has
    not answered."""
    text = (addr or "").strip()
    if not text:
        return False
    return not (text.startswith("127.") or text.startswith("169.254."))


def _split(addrs: Mapping[str, Sequence[str]] | None) -> tuple[list[str], list[str]]:
    """``(lan, tunnel)`` IPv4 strings, in interface order, drops applied."""
    lan: list[str] = []
    tunnels: list[str] = []
    for name, values in (addrs or {}).items():
        bucket = tunnels if is_tunnel(name) else lan
        for value in values or ():
            if _usable(value):
                bucket.append(value.strip())
    return lan, tunnels


def bonjour_name(hostname: str) -> str:
    """``Studio-Mac`` / ``Studio-Mac.local.`` → ``studio-mac.local``.

    ``gethostname()`` is ragged — it may or may not carry the suffix, may
    carry a trailing dot, and may be empty. An empty hostname simply omits
    this rung rather than contributing ``.local``.
    """
    name = (hostname or "").strip().strip(".").lower()
    if name.endswith(".local"):
        name = name[: -len(".local")]
    return f"{name}.local" if name else ""


def candidates(addrs: Mapping[str, Sequence[str]] | None,
               default_route: str,
               hostname: str) -> list[str]:
    """Every address a phone could try, best first, de-duplicated.

    LAN addresses (the default-route one first, where it is one of them),
    then the Bonjour name. Tunnels are dropped, not last: the door answers
    421 on one, and a phone that reached it there would be walking off the
    LAN address that works.
    """
    lan, _tunnels = _split(addrs)
    route = (default_route or "").strip()
    if route and route in lan:
        lan = [route] + [addr for addr in lan if addr != route]
    out: list[str] = []
    for item in [*lan, bonjour_name(hostname)]:
        if item and item not in out:
            out.append(item)
    return out


def arrived_on_tunnel(local_addr: str,
                      addrs: Mapping[str, Sequence[str]] | None) -> bool:
    """Whether a connection that landed on ``local_addr`` came in over a
    tunnel — the phone door refuses those unread.

    True iff the address is in the tunnel bucket **and not** in the LAN
    bucket (an address an ``en`` and a ``utun`` interface both hold is the
    LAN's). False for an empty address, ``127.x`` and ``169.254.x`` (dropped
    by `_usable` before either bucket), an address under no interface, and
    an empty mapping. That last one is deliberate: an unreadable interface
    list **fails open**. The seal is the boundary on this door; this check
    is defence in depth, and a psutil hiccup must never lock the phone out.
    """
    text = (local_addr or "").strip()
    if not text:
        return False
    lan, tunnels = _split(addrs)
    return text in tunnels and text not in lan


def refusal(addrs: Mapping[str, Sequence[str]] | None) -> str:
    """The daemon's own sentence for "no code, and here is why", or ``""``
    when at least one real LAN address survives the drops.

    Composed here rather than in the panel: the surfaces display refusals,
    they never compose them. `ApiServer._devices` turns a `ok: False` into a
    409 carrying this text, and `presentPairing`'s alert shows it verbatim.
    """
    lan, tunnels = _split(addrs)
    if lan:
        return ""
    if tunnels:
        return ("Dark Army could not find an address your phone could reach — only a "
                f"VPN address ({tunnels[0]}). Join the same Wi-Fi as the phone, "
                "or turn the VPN off, and try again.")
    return ("Dark Army could not find an address your phone could reach — this Mac "
            "has no network address. Join a Wi-Fi network and try again.")


# --- the two impure readers ---------------------------------------------------


def live_addrs() -> dict[str, list[str]]:
    """This machine's IPv4 addresses, interface name → addresses.

    ``psutil`` is already a runtime dependency and ``net_if_addrs()`` opens
    no socket. Any failure is an empty mapping, which `refusal` turns into
    the "no network address" sentence — never a traceback on a button press.
    """
    try:
        raw = psutil_net_if_addrs()
    except Exception:  # psutil.Error / OSError
        logger.debug("lan_hosts: interface enumeration failed", exc_info=True)
        return {}
    out: dict[str, list[str]] = {}
    for name, entries in (raw or {}).items():
        for entry in entries or ():
            if getattr(entry, "family", None) == socket.AF_INET:
                address = getattr(entry, "address", "") or ""
                if address:
                    out.setdefault(name, []).append(address)
    return out


def psutil_net_if_addrs():
    """Indirection so a test can stub the reader without importing psutil's
    namedtuples."""
    import psutil

    return psutil.net_if_addrs()


def default_route_addr() -> str:
    """The source address the routing table would use, via a UDP connect
    that sends no packet — ``connect`` on a datagram socket only asks. Empty
    on a machine with no route."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        host = sock.getsockname()[0]
        return host if isinstance(host, str) else ""
    except OSError:
        return ""
    finally:
        sock.close()
