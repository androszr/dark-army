# host/dark_army_daemon/srp.py
"""SRP-6a, both halves, for the typed-address pair.

A phone that cannot scan the Mac's square types the address and the
eight-letter pairing code instead. Until this module the plain branch of
``POST /api/pair`` answered that request with the phone's permanent keys in
the clear; now the phone proves it knows the code and both sides derive the
home key from the exchange, and nothing secret crosses the Wi-Fi at all.

RFC 5054's SRP-6a over its 2048-bit group (``GROUP_2048``, ``g = 2``) with
SHA-256 (``PRODUCTION``), the pairing code as the password and the pairing's
own id as the identity. **The padding rule is the contract**: every hashed
group element — ``g`` in ``k``, ``A`` and ``B`` in ``u`` and the proofs,
``S`` before ``K`` — is left-padded to the group's byte length (``pad``), on
both sides. ``ios/BobPhone/SRP.swift`` is the phone's half, byte-for-byte on
that rule; ``host/tests/fixtures/pake/cross-vector.json`` is the fixed
exchange both ends must reproduce.

    k  = H(N ‖ pad(g))
    x  = H(s ‖ H(I ‖ ":" ‖ P))
    v  = g^x mod N
    A  = g^a mod N
    B  = (k·v + g^b) mod N
    u  = H(pad(A) ‖ pad(B))
    S  = (A · v^u)^b mod N            (server)
       = (B − k·g^x)^(a + u·x) mod N   (client)
    K  = H(pad(S))
    M1 = H((H(N) ⊕ H(g)) ‖ H(I) ‖ s ‖ pad(A) ‖ pad(B) ‖ K)
    M2 = H(pad(A) ‖ M1 ‖ K)
    home key = HKDF-SHA256(K, info = INFO_PAKE_HOME)

Degenerate values are refused in words: ``A mod N == 0`` on the server
(``S`` would be 0 for any code), ``B mod N == 0`` and ``u == 0`` on the
client. Random ephemerals are 32 bytes from ``secrets``; ``server_start``
re-draws on the astronomically unlikely zero ``B``.

Pure over its arguments: nothing here reads the clock, the ledger or the
network. The hash is a parameter (``Params.hash_name``) so RFC 5054's SHA-1
Appendix B vector pins the arithmetic in the tests while production runs
SHA-256. The client half exists for the tests and the cross-language
harness; nothing under ``dark_army_daemon/`` but the tests imports it.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from . import relay

#: The home key's HKDF label. `SRP.homeKeyInfo` on the phone.
INFO_PAKE_HOME = b"bob-home v1 pake"

#: Bytes of randomness in an ephemeral exponent.
EPHEMERAL_BYTES = 32


@dataclass(frozen=True)
class Group:
    N: int
    g: int

    @property
    def byte_len(self) -> int:
        return (self.N.bit_length() + 7) // 8


@dataclass(frozen=True)
class Params:
    group: Group
    hash_name: str


# RFC 5054, Appendix A: the 2048-bit group, generator 2.
_N_2048_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050"
    "A37329CBB4A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50"
    "E8083969EDB767B0CF6095179A163AB3661A05FBD5FAAAE82918A9962F0B93B8"
    "55F97993EC975EEAA80D740ADBF4FF747359D041D5C33EA71D281E446B14773B"
    "CA97B43A23FB801676BD207A436C6481F1D2B9078717461A5B9D32E688F87748"
    "544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6"
    "AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB6"
    "94B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)
GROUP_2048 = Group(N=int(_N_2048_HEX, 16), g=2)

PRODUCTION = Params(GROUP_2048, "sha256")


# ── primitives ────────────────────────────────────────────────────────────────

def hash_bytes(params: Params, *parts: bytes) -> bytes:
    h = hashlib.new(params.hash_name)
    for part in parts:
        h.update(part)
    return h.digest()


def pad(group: Group, value: int) -> bytes:
    """``value`` as big-endian bytes, left-padded to the group's length."""
    return int(value).to_bytes(group.byte_len, "big")


def to_int(data: bytes) -> int:
    return int.from_bytes(data, "big")


def random_ephemeral() -> int:
    """A fresh private exponent: ``EPHEMERAL_BYTES`` of randomness, never 0."""
    while True:
        value = to_int(secrets.token_bytes(EPHEMERAL_BYTES))
        if value:
            return value


# ── the compositions ──────────────────────────────────────────────────────────

def k(params: Params) -> int:
    """The multiplier ``k = H(N ‖ pad(g))``."""
    group = params.group
    return to_int(hash_bytes(params, pad(group, group.N), pad(group, group.g)))


def x(params: Params, identity: str, password: str, salt: bytes) -> int:
    """The private key ``x = H(s ‖ H(I ‖ ":" ‖ P))``."""
    inner = hash_bytes(params, identity.encode("utf-8"), b":",
                       password.encode("utf-8"))
    return to_int(hash_bytes(params, bytes(salt), inner))


def verifier(params: Params, identity: str, password: str, salt: bytes) -> int:
    """``v = g^x mod N`` — what the Mac keeps on the pairing instead of a
    key: it proves nothing to a stranger who reads it, and it is never
    written down or published anyway."""
    group = params.group
    return pow(group.g, x(params, identity, password, salt), group.N)


def client_public(params: Params, a: int) -> int:
    """``A = g^a mod N``."""
    group = params.group
    return pow(group.g, a, group.N)


def server_public(params: Params, b: int, v: int) -> int:
    """``B = (k·v + g^b) mod N``."""
    group = params.group
    return (k(params) * v + pow(group.g, b, group.N)) % group.N


def server_start(params: Params, v: int) -> tuple[int, int]:
    """``(b, B)`` with a fresh ``b``, re-drawn until ``B mod N != 0``."""
    while True:
        b = random_ephemeral()
        B = server_public(params, b, v)
        if B % params.group.N != 0:
            return b, B


def u(params: Params, A: int, B: int) -> int:
    """The scrambler ``u = H(pad(A) ‖ pad(B))``."""
    group = params.group
    return to_int(hash_bytes(params, pad(group, A), pad(group, B)))


def server_secret(params: Params, A: int, b: int, v: int, u_value: int) -> int:
    """``S = (A · v^u)^b mod N``. Refuses ``A mod N == 0`` in words: with
    it ``S`` is 0 whatever the code."""
    group = params.group
    if A % group.N == 0:
        raise ValueError("A mod N is zero")
    return pow((A * pow(v, u_value, group.N)) % group.N, b, group.N)


def client_secret(params: Params, B: int, a: int, x_value: int,
                  u_value: int) -> int:
    """``S = (B − k·g^x)^(a + u·x) mod N``. Refuses ``B mod N == 0`` and
    ``u == 0`` in words — the two values a hostile server could pick to
    make ``S`` independent of the code."""
    group = params.group
    if B % group.N == 0:
        raise ValueError("B mod N is zero")
    if u_value == 0:
        raise ValueError("u is zero")
    base = (B - k(params) * pow(group.g, x_value, group.N)) % group.N
    return pow(base, a + u_value * x_value, group.N)


def session_key(params: Params, S: int) -> bytes:
    """``K = H(pad(S))``."""
    return hash_bytes(params, pad(params.group, S))


def client_proof(params: Params, identity: str, salt: bytes, A: int, B: int,
                 K: bytes) -> bytes:
    """``M1 = H((H(N) ⊕ H(g)) ‖ H(I) ‖ s ‖ pad(A) ‖ pad(B) ‖ K)``."""
    group = params.group
    hn = hash_bytes(params, pad(group, group.N))
    hg = hash_bytes(params, pad(group, group.g))
    mixed = bytes(p ^ q for p, q in zip(hn, hg))
    return hash_bytes(params, mixed, hash_bytes(params, identity.encode("utf-8")),
                      bytes(salt), pad(group, A), pad(group, B), K)


def server_proof(params: Params, A: int, M1: bytes, K: bytes) -> bytes:
    """``M2 = H(pad(A) ‖ M1 ‖ K)``."""
    return hash_bytes(params, pad(params.group, A), bytes(M1), K)


def home_key(K: bytes) -> bytes:
    """The device's 32-byte home key from the session key —
    ``relay._hkdf``'s HKDF-SHA256 with an empty salt, which is exactly what
    `RelayTransport.derive` computes on the phone."""
    return relay._hkdf(bytes(K), INFO_PAKE_HOME)
