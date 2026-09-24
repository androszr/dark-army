"""Which face an agent wears — the Python half of `panel/Sources/BobPanel/Cast.swift`.

The panel already answers this for the rows it draws. The problem is that a
notification is composed here, in the daemon, and a banner showing a different
face from the row it refers to is worse than a banner showing no face at all:
the whole point of the cast is that you recognise who is calling before you have
read anything, and a face that disagrees with the panel teaches you not to trust
either.

So the rule is duplicated rather than approximated, and it is duplicated
*exactly*, including the hash:

  - a nickname that is one of the cast wears its own face;
  - an **overflow** nickname (``Cipher-ab12``) wears the face of its stem, so
    the banner and the row draw the same person for the same session;
  - anything else — a Grok agent, a session identity has not named yet — is
    hashed by session id onto a stable face, so it never changes under you.

The stem rung was added to `Cast.character(for:)` on the Swift side and this
copy was left behind, which is exactly the divergence the file exists to
prevent: a banner for an overflow name like `Cipher-ab12` drew a hashed stranger
while the row beside it drew Cipher. `test_cast.py`'s golden — produced by *running* the panel's own
function — is what caught it.

The hash is djb2 over the session id's UTF-8 bytes, wrapped to 64 bits, because
that is what Swift's ``&*``/``&+`` do on ``Int``. Doing this in Python's
unbounded integers gives a different answer for every id long enough to overflow,
which is all of them: a v4 UUID is 36 bytes and passes 2^64 after nine.

`NAMES` comes from `identity` rather than being restated, since that is the list
the daemon can actually assign from and the Swift side is already documented as
having to track it.
"""
from __future__ import annotations

from .identity import NAMES

# The three poses the art was drawn in, one per thing an agent can be doing.
STATE_WORK = "work"
STATE_SLEEP = "sleep"
STATE_ALERT = "alert"

_UINT64 = (1 << 64) - 1
_INT64_SIGN = 1 << 63

# Buckets, as `session_stats.categorize` and the panel name them.
_STATE_BY_CATEGORY = {
    "waiting": STATE_ALERT,
    "running": STATE_WORK,
    "sleeping": STATE_SLEEP,
    "finished": STATE_SLEEP,
    "abandoned": STATE_SLEEP,
}


def _djb2_swift(text: str) -> int:
    """djb2 over UTF-8, wrapped exactly as Swift's `&*` / `&+` on a 64-bit Int."""
    h = 5381
    for byte in text.encode("utf-8"):
        h = ((h * 33) + byte) & _UINT64
    return h - (1 << 64) if h >= _INT64_SIGN else h   # back to signed


def character_for(nickname: str, session_id: str) -> str:
    """The cast slug for an agent — lowercase, e.g. ``"vex"``.

    Mirrors `Cast.character(for:)` rung for rung: the whole nickname, then the
    stem before the first ``-``, then the session-id hash. An overflow
    nickname borrows its stem's face **on purpose** — it is the same agent as
    far as a person reading the bar is concerned, and the panel has drawn it
    that way since `character(forNickname:)` arrived.
    """
    nick = (nickname or "").lower()
    for name in NAMES:
        if name.lower() == nick:
            return nick
    stem = nick.split("-", 1)[0]
    for name in NAMES:
        if name.lower() == stem:
            return stem
    return NAMES[abs(_djb2_swift(session_id or "")) % len(NAMES)].lower()


def state_for(category: str) -> str:
    """The pose for a bucket. Unknown buckets sleep — the quietest wrong answer."""
    return _STATE_BY_CATEGORY.get(category, STATE_SLEEP)


__all__ = ["character_for", "state_for", "NAMES",
           "STATE_WORK", "STATE_SLEEP", "STATE_ALERT"]
