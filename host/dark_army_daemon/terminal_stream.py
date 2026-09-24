"""The panel's terminal, as one socket: keys up, screen bytes down, in order.

The pane used to type through one HTTP request per keystroke — each its own
TCP connection, answered `Connection: close`, awaited on a broker drain —
and to read the screen back by polling a JSON frame twenty times a second
that the daemon rebuilt in full and the panel threw away. Two keys typed
quickly could arrive in either order; a burst of mouse reports queued the
keys behind fifty requests a second; and two polls landing in one run-loop
pass lost the first chunk, which is a torn picture.

Here the panel opens **one** connection per pane: an ordinary loopback GET
carrying the write token, answered `200` and then held open as a framed
byte stream in both directions. Down: `D` (bytes the terminal printed),
`X` (the process exited), `E` (a refusal, in Dark Army's words). Up: `I`
(keystrokes, raw), `S` (the pane's size, `cols rows`), `P` (ignored). The
first `D` frames are `Screen.paint()` — the emulator's own drawing of the
current screen, scrollback included — never the raw ring, which begins
wherever 256 KB ago happened to fall. Every frame is `kind (1 byte) +
length (4 bytes, big-endian) + payload`; `MAX_FRAME_BYTES` caps one.

**The opening paint rides as one or more `D` frames, in order.** A wide
screen that drew a full-colour picture paints past the frame cap — every
cell its own SGR run, five hundred lines of history above it — and one
frame that cannot be encoded used to be a socket that closed having sent
nothing, a pane that stayed black and retried once a second. `serve`
slices the paint at `PAINT_CHUNK_BYTES` or the codec's `max_plain`,
whichever is smaller, and writes the slices down the one socket from the
one coroutine before the live queue is touched; the parser on the other
end is incremental, so a boundary inside an escape sequence is harmless
here and only here. Nothing is trimmed and no history is lost. A send
that fails for any reason other than the peer hanging up is one log line
naming the terminal and one `E` frame in `STREAM_SEND_FAILED_REFUSAL`'s
words, so a black pane says why.

The desk types straight through: no drain wait, no permission-prompt
refusal — the dialog is on this very screen and `1` or `y` is how a person
answers it — and no cap below what a paste can be. The phone's line
route keeps every one of those rules (`BobDaemon.terminal_input`).

**The phone rides the same stream, sealed.** `serve` takes a `codec`: the
loopback pane's is `PlainCodec` (identity, byte-for-byte the protocol
above); the phone door's is `SealedCodec`, which wraps every down frame in
one `Z` frame whose payload is `relay.seal_blob` over `kind + payload`
under the device's home key, and unwraps every up frame the same way. The
blob AAD carries `stream_id|direction|counter`, so a captured frame opens
only in its own position on its own stream: a replay, a reorder or a
tampered byte fails to open, and the read loop closes the socket — the
peer is not this phone. Not even the exit event leaks: the outer kind byte
is always `Z`.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque
from typing import Callable, Optional

from . import relay

logger = logging.getLogger("dark-army.terminal_stream")

#: Down.
KIND_DATA = b"D"
KIND_EXIT = b"X"
KIND_ERROR = b"E"
#: Up.
KIND_INPUT = b"I"
KIND_SIZE = b"S"
KIND_PING = b"P"

#: One frame, either direction. A paste is well under this; a paint of a
#: 400x200 screen with 500 lines of history is too.
MAX_FRAME_BYTES = 4 * 1024 * 1024
#: The largest plain slice of the opening paint, and the bound the live
#: coalescer stops gathering at — one figure, read by both. Half the frame
#: cap so a coalesced frame may overshoot by one queue item
#: (`ptyhost.DATA_FRAME_BYTES`, 32 KB) and still fit whole.
PAINT_CHUNK_BYTES = MAX_FRAME_BYTES // 2
#: The `E` frame sent when the screen could not be written down the
#: stream for a reason that is not the peer hanging up. The log holds the
#: traceback and the handle; the pane holds this sentence until real
#: screen bytes arrive.
STREAM_SEND_FAILED_REFUSAL = ("Dark Army could not send this terminal's "
                              "screen. The log says why.")
#: Bytes queued for a client that is not reading before it is dropped —
#: it reconnects and is painted afresh, which beats a daemon holding a
#: session's whole output for a window nobody is looking at.
MAX_QUEUED_BYTES = 8 * 1024 * 1024
#: The response content type; the panel checks it before it trusts the
#: bytes that follow the head.
CONTENT_TYPE = "application/x-bob-terminal"
#: The phone door's content type: the same frames, each sealed inside a
#: `Z` frame. The phone checks it before it trusts the bytes that follow.
SEALED_CONTENT_TYPE = "application/x-bob-terminal-sealed"
#: The one outer kind on a sealed stream, both directions.
KIND_SEALED = b"Z"
#: Up frames a sealed stream takes in one minute before it is dropped — a
#: phone's keys and heartbeats are well under this; a flood is not a person.
#: Applied only where `serve` is told to (the phone door); the loopback
#: pane is byte-for-byte what it was.
STREAM_MAX_UP_FRAMES_PER_MINUTE = 600
#: How often the phone re-sends its last `S` while the stream is open —
#: the keepalive on a socket nothing else times out. Documentation of the
#: phone's cadence; the server enforces nothing on it.
STREAM_HEARTBEAT_SECONDS = 5

_HEAD = struct.Struct(">cI")


def encode(kind: bytes, payload: bytes = b"") -> bytes:
    """One frame."""
    if len(kind) != 1:
        raise ValueError("kind is one byte")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("frame too large")
    return _HEAD.pack(kind, len(payload)) + payload


class FrameReader:
    """Incremental decoder: feed it whatever the socket gave you, take the
    whole frames out. A frame above `MAX_FRAME_BYTES` raises — the peer is
    not speaking this protocol."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        self._buf += data
        out: list = []
        while len(self._buf) >= _HEAD.size:
            kind, length = _HEAD.unpack_from(self._buf, 0)
            if length > MAX_FRAME_BYTES:
                raise ValueError("frame too large")
            if len(self._buf) < _HEAD.size + length:
                break
            payload = bytes(self._buf[_HEAD.size:_HEAD.size + length])
            del self._buf[:_HEAD.size + length]
            out.append((kind, payload))
        return out


def parse_size(payload: bytes) -> Optional[tuple]:
    """`b"120 40"` → `(120, 40)`; None for anything else."""
    try:
        parts = payload.decode("ascii").split()
        if len(parts) != 2:
            return None
        cols, rows = int(parts[0]), int(parts[1])
    except (UnicodeDecodeError, ValueError):
        return None
    if cols <= 0 or rows <= 0:
        return None
    return cols, rows


class PlainCodec:
    """The loopback pane's codec: identity both ways."""

    #: The most `wrap` may be handed and still produce a frame `encode`
    #: accepts: identity adds nothing.
    max_plain = MAX_FRAME_BYTES

    def wrap(self, kind: bytes, payload: bytes) -> tuple:
        return kind, payload

    def unwrap(self, kind: bytes, payload: bytes) -> Optional[tuple]:
        return kind, payload


class SealedCodec:
    """The phone door's codec: every frame sealed under the device's home
    key, bound to this stream and its position in it.

    `wrap` seals `kind + payload` as one `Z` frame with the blob id
    `<stream_id>|m2p|<n>`, `n` counting down frames from 0; `unwrap` opens a
    `Z` frame against `<stream_id>|p2m|<n>` and moves `n` only on success,
    returning `(kind, payload)` or ``None`` — a frame that is not sealed, is
    sealed to another stream, is out of order or was altered. There is no
    counter inside the blob; the position is the AAD (`relay.open_blob`).
    """

    #: The most `wrap` may be handed and still produce a `Z` frame under
    #: `MAX_FRAME_BYTES`: the seal adds exactly `relay.BLOB_OVERHEAD_BYTES`
    #: (a 12-byte nonce and a 16-byte tag, no deflate and no base64 on this
    #: route) and `wrap` folds the one `kind` byte into the plaintext —
    #: 29 bytes per sealed frame, deterministic. Sizing against
    #: `MAX_FRAME_BYTES` alone would overflow by those 29 at the boundary.
    max_plain = MAX_FRAME_BYTES - relay.BLOB_OVERHEAD_BYTES - 1

    def __init__(self, key: bytes, stream_id: str) -> None:
        self._key = bytes(key)
        self._id = str(stream_id or "")
        self._down = 0
        self._up = 0

    @property
    def sent(self) -> int:
        return self._down

    @property
    def received(self) -> int:
        return self._up

    def wrap(self, kind: bytes, payload: bytes) -> tuple:
        frame_id = f"{self._id}|{relay.DIR_MAC_TO_PHONE}|{self._down}"
        sealed = relay.seal_blob(self._key, relay.DIR_MAC_TO_PHONE, frame_id,
                                 bytes(kind) + bytes(payload), ns=relay.HOME)
        self._down += 1
        return KIND_SEALED, sealed

    def unwrap(self, kind: bytes, payload: bytes) -> Optional[tuple]:
        if kind != KIND_SEALED:
            return None
        frame_id = f"{self._id}|{relay.DIR_PHONE_TO_MAC}|{self._up}"
        plain = relay.open_blob(self._key, relay.DIR_PHONE_TO_MAC, frame_id,
                                payload, ns=relay.HOME)
        if plain is None or len(plain) < 1:
            return None
        self._up += 1
        return bytes(plain[:1]), bytes(plain[1:])


async def serve(host, handle: str, paint: bytes, exited: bool,
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                on_input: Callable[[bytes], tuple],
                on_size: Callable[[int, int], None],
                *, codec=None,
                up_frames_per_minute: Optional[int] = None) -> None:
    """Run one attached pane until the socket closes or the process exits.

    `host` is the `PtyHost` (subscribed for `handle`'s bytes); `paint` is
    the first thing sent; `on_input(bytes) -> (ok, detail)` types, and a
    refusal goes back as an `E` frame; `on_size(cols, rows)` resizes. The
    HTTP head has already been written by the caller.

    `codec` wraps every frame down and unwraps every frame up —
    `PlainCodec` (the default, the loopback pane) or `SealedCodec` (the
    phone door). A frame that fails to unwrap ends the stream: on a sealed
    stream that is a replay, a reorder or tampering, and the peer is not
    this phone. `up_frames_per_minute`, where given, is a sliding cap on
    frames read; over it the stream ends too.
    """
    codec = codec if codec is not None else PlainCodec()
    # One slice bound for the opening paint and the live coalescer alike:
    # the plain chunk figure, or less where the codec's wrapping would push
    # a frame of that size over the cap (the sealed door's 29 bytes).
    limit = max(1, min(PAINT_CHUNK_BYTES, int(getattr(codec, "max_plain",
                                                       MAX_FRAME_BYTES))))
    queue: asyncio.Queue = asyncio.Queue()
    queued = 0

    def _sub(data: Optional[bytes]) -> None:
        nonlocal queued
        if data is None:
            queue.put_nowait(None)
            return
        queued += len(data)
        if queued > MAX_QUEUED_BYTES:
            queue.put_nowait(ValueError("client not reading"))
            return
        queue.put_nowait(data)

    async def _sender() -> None:
        nonlocal queued
        try:
            # The opening paint, as ordered `D` frames of at most `limit`
            # bytes each — a paint over the frame cap still arrives whole.
            # An empty paint is still exactly one `D` frame, as before.
            for start in range(0, max(1, len(paint)), limit):
                writer.write(encode(*codec.wrap(KIND_DATA, paint[start:start + limit])))
                await writer.drain()
            if exited:
                writer.write(encode(*codec.wrap(KIND_EXIT, b"")))
                await writer.drain()
                return
            while True:
                item = await queue.get()
                if item is None:
                    writer.write(encode(*codec.wrap(KIND_EXIT, b"")))
                    await writer.drain()
                    return
                if isinstance(item, Exception):
                    raise item
                chunk = bytearray(item)
                # Coalesce what else is already here into one frame. One
                # further item may overshoot `limit` by at most
                # `ptyhost.DATA_FRAME_BYTES` (32 KB), far under
                # `codec.max_plain`.
                while not queue.empty() and len(chunk) < limit:
                    nxt = queue.get_nowait()
                    if nxt is None or isinstance(nxt, Exception):
                        queue.put_nowait(nxt)
                        break
                    chunk += nxt
                queued -= len(chunk)
                writer.write(encode(*codec.wrap(KIND_DATA, bytes(chunk))))
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError, OSError):
            # Ordinary disconnects: the pane closed, the peer hung up, the
            # socket went away. A line here would fire on every pane close.
            pass
        except Exception:
            # Anything else — a frame that could not be encoded, a client
            # that stopped reading — is worth saying out loud, once, and
            # telling the pane in words. `encode` composes the whole frame
            # before `writer.write` runs, so a failure there wrote nothing
            # and the `E` frame lands on a stream that is still in sync.
            logger.exception("terminal stream %s: could not send", handle)
            try:
                writer.write(encode(*codec.wrap(
                    KIND_ERROR, STREAM_SEND_FAILED_REFUSAL.encode("utf-8"))))
                await writer.drain()
            except (asyncio.CancelledError, ConnectionError, OSError, ValueError):
                pass

    async def _receiver() -> None:
        frames = FrameReader()
        recent: deque = deque()
        while True:
            try:
                data = await reader.read(65536)
            except (ConnectionError, OSError):
                return
            if not data:
                return
            try:
                items = frames.feed(data)
            except ValueError:
                return
            for outer_kind, outer_payload in items:
                if up_frames_per_minute is not None:
                    now = time.monotonic()
                    recent.append(now)
                    while recent and now - recent[0] > 60.0:
                        recent.popleft()
                    if len(recent) > up_frames_per_minute:
                        logger.info("terminal stream %s dropped: over %d up "
                                    "frames a minute", handle, up_frames_per_minute)
                        return
                opened = codec.unwrap(outer_kind, outer_payload)
                if opened is None:
                    # Tampered, replayed or reordered: not this peer.
                    logger.info("terminal stream %s dropped: a frame did not open",
                                handle)
                    return
                kind, payload = opened
                if kind == KIND_INPUT:
                    ok, detail = on_input(payload)
                    if not ok:
                        try:
                            writer.write(encode(*codec.wrap(
                                KIND_ERROR, detail.encode("utf-8", "replace"))))
                        except (ConnectionError, OSError):
                            pass
                elif kind == KIND_SIZE:
                    size = parse_size(payload)
                    if size is not None:
                        on_size(*size)
                # `P` and anything unknown: ignored.

    host.subscribe(handle, _sub)
    sender = asyncio.ensure_future(_sender())
    receiver = asyncio.ensure_future(_receiver())
    try:
        # Either side ending ends the stream: the sender after `X` (the
        # process is gone, the socket closes behind it), the receiver on
        # hang-up or a frame that is not this protocol.
        await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        host.unsubscribe(handle, _sub)
        for task in (sender, receiver):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

__all__ = ["encode", "FrameReader", "parse_size", "serve", "PlainCodec",
           "SealedCodec", "KIND_DATA", "KIND_EXIT", "KIND_ERROR", "KIND_INPUT",
           "KIND_SIZE", "KIND_PING", "KIND_SEALED", "MAX_FRAME_BYTES",
           "PAINT_CHUNK_BYTES", "STREAM_SEND_FAILED_REFUSAL",
           "MAX_QUEUED_BYTES", "CONTENT_TYPE", "SEALED_CONTENT_TYPE",
           "STREAM_MAX_UP_FRAMES_PER_MINUTE", "STREAM_HEARTBEAT_SECONDS"]
