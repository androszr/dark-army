# host/tests/test_lan_upload.py
"""`POST /api/upload` on the phone door, sealed, and Prepare reaching the
door over LAN.

The wire is a sealed header frame (kind `upload`, body `{staging, name}`) in
`X-Bob-Frame` and `relay.seal_blob`'s raw bytes as the body, bound to that
frame's id; the answer is a sealed `reply` whose body is today's
`{ok, path, detail}`. `_upload` seals both halves and opens the answer.

Free ports from held-open sockets and a temp device ledger, exactly as
`test_lan_access.py` does — a fixed port here would recreate the full-suite
hang already paid for. ``ATTACHMENTS_DIR`` is pointed at ``tmp_path`` so
nothing writes the live ``~/.dark-army/attachments``, and
``MAX_ATTACHMENT_BYTES`` is monkeypatched small rather than posting 20 MB.
"""

from __future__ import annotations

import asyncio
import json
import socket
import stat
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import attachments, devices, lan_hosts, paths, relay
from dark_army_daemon.api_server import HOME_UPDATE_REFUSAL, ApiServer
from dark_army_daemon.daemon import BobDaemon
from tests.pake_pair import pair_typed
from tests.free_ports import free_ports

FOLDER = "abcd1234-efgh5678-ijkl9012-mnop34"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _free_ports(count: int) -> list[int]:
    """From this run's and worker's block below the ephemeral range, not a
    bound-then-closed `:0` another worker's connection can take
    (`tests/free_ports.py`)."""
    return free_ports(count)


@pytest.fixture(autouse=True)
def _no_fleet_snapshot(monkeypatch):
    monkeypatch.setattr(BobDaemon, "_schedule_agents_push", lambda self: None)


@pytest.fixture(autouse=True)
def machine(monkeypatch):
    monkeypatch.setattr(lan_hosts, "live_addrs", lambda: {"en0": ["192.168.1.5"]})
    monkeypatch.setattr(lan_hosts, "default_route_addr", lambda: "192.168.1.5")
    monkeypatch.setattr(socket, "gethostname", lambda: "test-mac")


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DEVICES_PATH", tmp_path / "devices.json")
    monkeypatch.setattr(paths, "RELAY_PATH", tmp_path / "relay.json")
    monkeypatch.setattr(paths, "STATE_DIR", tmp_path / "state")
    devices.reset()
    relay.reset()
    yield
    devices.reset()
    relay.reset()


@pytest.fixture(autouse=True)
def attach_dir(tmp_path, monkeypatch):
    """The module binds ``ATTACHMENTS_DIR`` at import, so patch it there."""
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", root)
    return root


@pytest.fixture
def ports(tmp_path, monkeypatch):
    loop_port, lan_port = _free_ports(2)
    monkeypatch.setattr(api_mod, "API_TOKEN_PATH", tmp_path / "api-token")
    monkeypatch.setattr(api_mod, "ensure_state_dir", lambda: tmp_path)
    monkeypatch.setattr(api_mod, "LAN_API_PORT", lan_port)
    return loop_port, lan_port


class Phone:
    """One paired phone's home key and its running counter."""

    def __init__(self, payload: dict):
        self.token = payload["token"]
        self.device_id = payload["device_id"]
        self.key = payload["key"]
        self.ctr = 0

    @property
    def chan(self) -> str:
        return relay.channel_id(self.key, ns=relay.HOME)

    def seal(self, kind: str, body: dict, *, frame_id: str = "") -> str:
        self.ctr += 1
        return relay.seal_frame(self.key, relay.DIR_PHONE_TO_MAC, self.ctr,
                                kind, body, frame_id=frame_id, ns=relay.HOME)

    def open(self, out: bytes):
        frame, err = relay.open_frame(self.key, relay.DIR_MAC_TO_PHONE,
                                      out.decode(), 0, ns=relay.HOME)
        assert err == "", err
        return frame


@pytest_asyncio.fixture
async def paired(ports):
    """A started loopback + LAN listener and one paired phone."""
    loop_port, lan_port = ports
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=loop_port)
    daemon._api = srv
    daemon.lan_access_enabled = True
    await srv.start()
    await srv.start_lan()
    try:
        yield srv, daemon, lan_port, loop_port, Phone(
            await pair_typed(_fetch, lan_port))
    finally:
        await srv.stop()


def _blocking_fetch(path, *, port, data=None, headers=None, method=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers or {},
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


async def _fetch(*args, **kwargs):
    return await asyncio.to_thread(_blocking_fetch, *args, **kwargs)


async def _upload(port, phone, *, staging=FOLDER, name="photo-1.png",
                  body=PNG, extra=None, blob_frame_id=None, plain=False):
    """One sealed upload: header frame + blob. Returns the **inner**
    ``(status, body_dict)`` of the sealed reply, or the raw HTTP
    ``(status, body)`` when the door refused plainly. ``blob_frame_id``
    seals the blob to some other frame's id (the swap case); ``plain``
    sends today's old plaintext request instead."""
    headers = {"Content-Type": "application/octet-stream"}
    headers.update(extra or {})
    if plain or phone is None:
        if phone is not None:
            headers["X-Bob-Device"] = phone.token
        return await _fetch(f"/api/upload?staging={staging}&name={name}",
                            port=port, data=body, headers=headers)
    frame_id = f"up-{phone.ctr + 1}"
    wire = phone.seal("upload", {"staging": staging, "name": name},
                      frame_id=frame_id)
    headers["X-Bob-Channel"] = phone.chan
    headers["X-Bob-Frame"] = wire
    sealed = relay.seal_blob(phone.key, relay.DIR_PHONE_TO_MAC,
                             blob_frame_id or frame_id, body)
    status, out = await _fetch("/api/upload", port=port, data=sealed,
                               headers=headers)
    if status != 200:
        return status, out
    frame = phone.open(out)
    assert frame["kind"] == "reply", frame
    assert frame["body"]["re"] == frame_id
    return int(frame["body"]["status"]), json.loads(frame["body"]["body"])


# --- who may knock ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_upload_is_refused(paired):
    """The old plaintext request — device token, query string — is a
    frame-less upload, and a frame-less upload is not from a paired phone."""
    _srv, _daemon, lan_port, _loop, phone = paired
    status, out = await _upload(lan_port, phone, plain=True)
    assert status == 403
    assert json.loads(out)["error"] == "that phone is not paired"
    status, _ = await _upload(lan_port, None)
    assert status == 403
    # And the three old routes say what fixes it.
    status, out = await _fetch("/api/state", port=lan_port,
                               headers={"X-Bob-Device": phone.token})
    assert status == 426
    assert json.loads(out)["error"] == HOME_UPDATE_REFUSAL


@pytest.mark.asyncio
async def test_upload_from_a_browser_is_refused_on_the_origin_alone(paired):
    _srv, _daemon, lan_port, _loop, phone = paired
    status, _ = await _upload(lan_port, phone,
                              extra={"Origin": "http://evil.example"})
    assert status == 403


@pytest.mark.asyncio
async def test_the_loopback_listener_does_not_serve_upload(paired):
    """The route is on the phone door only — `_handle_client` is untouched."""
    _srv, _daemon, _lan_port, loop_port, phone = paired
    status, _ = await _upload(loop_port, phone)
    assert status == 404


@pytest.mark.asyncio
async def test_a_frame_that_is_not_an_upload_is_refused_in_words(paired):
    """`_home_open(expect_kind="upload")`: a `state` frame in `X-Bob-Frame`
    verifies but is not what this route takes — an authenticated frame, so
    the refusal is a sealed `err` (inner 400), never a plain 403 the phone
    would read as un-pairing."""
    _srv, _daemon, lan_port, _loop, phone = paired
    wire = phone.seal("state", {})
    status, out = await _fetch(
        "/api/upload", port=lan_port, data=b"x" * 40,
        headers={"X-Bob-Channel": phone.chan, "X-Bob-Frame": wire})
    assert status == 200
    frame = phone.open(out)
    assert frame["kind"] == "err"
    assert frame["body"]["status"] == 400
    assert "upload" in frame["body"]["error"]


# --- what it refuses ----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_upload_naming_no_file_is_a_400(paired):
    _srv, _daemon, lan_port, _loop, phone = paired
    status, body = await _upload(lan_port, phone, name="")
    assert status == 400
    assert body["ok"] is False


@pytest.mark.asyncio
async def test_a_blob_bound_to_another_frame_is_refused_in_words(paired,
                                                                attach_dir):
    """The AAD carries the header frame's id: a body sealed for one upload
    cannot ride another's header, so two in flight cannot swap bodies."""
    _srv, _daemon, lan_port, _loop, phone = paired
    status, body = await _upload(lan_port, phone, blob_frame_id="up-999")
    assert status == 403
    assert body == {"ok": False, "detail": "that upload could not be opened"}
    assert not (attach_dir / FOLDER).exists()


@pytest.mark.asyncio
async def test_a_mis_shaped_staging_folder_is_refused_in_words(paired):
    _srv, _daemon, lan_port, _loop, phone = paired
    status, body = await _upload(lan_port, phone, staging="short")
    assert status == 409
    assert body["detail"] == "that is not a stored attachment path"


@pytest.mark.asyncio
async def test_a_name_that_walks_out_of_the_folder_is_refused(paired,
                                                             attach_dir):
    _srv, _daemon, lan_port, _loop, phone = paired
    status, body = await _upload(lan_port, phone, name="../escape.png")
    assert status == 409
    assert body["detail"] == "that is not a stored attachment path"
    assert not (attach_dir.parent / "escape.png").exists()


@pytest.mark.asyncio
async def test_a_disallowed_extension_is_refused_by_name(paired):
    _srv, _daemon, lan_port, _loop, phone = paired
    status, body = await _upload(lan_port, phone, name="run.sh", body=b"echo")
    assert status == 409
    assert body["detail"] == ".sh files are not allowed"


@pytest.mark.asyncio
async def test_an_oversize_file_is_refused_in_words_not_dropped(paired,
                                                               monkeypatch):
    """The route's `+1024` headroom is what buys a *sentence* here."""
    _srv, _daemon, lan_port, _loop, phone = paired
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 32)
    status, body = await _upload(lan_port, phone, body=b"y" * 64)
    assert status == 409
    assert "larger than" in body["detail"]
    # Exactly the cap plus the blob's 28 bytes of nonce and tag still fits
    # the widened read, so the refusal is a sentence rather than a drop.
    status, body = await _upload(lan_port, phone, body=b"y" * 33)
    assert status == 409
    assert "larger than" in body["detail"]


@pytest.mark.asyncio
async def test_a_ninth_file_in_one_folder_is_refused(paired):
    _srv, _daemon, lan_port, _loop, phone = paired
    for index in range(1, 9):
        status, body = await _upload(lan_port, phone, name=f"photo-{index}.png")
        assert status == 200, body
    status, body = await _upload(lan_port, phone, name="photo-9.png")
    assert status == 409
    assert body["detail"] == "a card can have at most 8 attachments"


# --- what it writes -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_good_upload_lands_0600_inside_the_staging_folder(paired,
                                                                 attach_dir):
    _srv, _daemon, lan_port, _loop, phone = paired
    status, payload = await _upload(lan_port, phone)
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["path"] == f"{FOLDER}/photo-1.png"
    written = attach_dir / FOLDER / "photo-1.png"
    assert written.read_bytes() == PNG
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert attachments.field_refusal(payload["path"]) is None


@pytest.mark.asyncio
async def test_a_second_upload_of_one_name_is_deduped_not_clobbered(paired,
                                                                   attach_dir):
    _srv, _daemon, lan_port, _loop, phone = paired
    first, _ = await _upload(lan_port, phone, body=PNG)
    assert first == 200
    status, body = await _upload(lan_port, phone, body=b"second")
    assert status == 200
    assert body["path"] == f"{FOLDER}/photo-1-2.png"
    assert (attach_dir / FOLDER / "photo-1.png").read_bytes() == PNG
    assert (attach_dir / FOLDER / "photo-1-2.png").read_bytes() == b"second"


# --- the body cap stayed a boundary -------------------------------------------


@pytest.mark.asyncio
async def test_the_action_route_still_refuses_a_body_over_the_ordinary_cap(paired):
    """The cap was raised for `/api/upload` alone, never for `/api/home`."""
    _srv, _daemon, lan_port, _loop, phone = paired
    payload = "A" * (api_mod.MAX_BODY_BYTES + 64)
    reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
    try:
        body = payload.encode()
        writer.write(
            b"POST /api/home HTTP/1.1\r\nHost: 192.168.1.5\r\n"
            + f"X-Bob-Channel: {phone.chan}\r\n".encode()
            + b"Content-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        with pytest.raises((asyncio.IncompleteReadError, asyncio.TimeoutError,
                            ConnectionResetError)):
            await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=3)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass


@pytest.mark.asyncio
async def test_a_stranger_cannot_make_the_daemon_buffer_an_attachment(paired):
    """The widened cap is for a *verifying header frame* only.

    The body is read before any handler runs, so a stranger who could still
    send 20 MB would have the daemon hold it before answering 403. The rule
    opens the header frame early enough to matter: with no frame, a frame
    under another key, or a frame that is not an upload, the sender is back
    on `MAX_BODY_BYTES` and an oversize body has its connection dropped.
    """
    _srv, _daemon, lan_port, _loop, phone = paired
    big = b"z" * (api_mod.MAX_BODY_BYTES + 64)
    ordinary = (api_mod.MAX_BODY_BYTES, api_mod.READ_TIMEOUT)
    assert ApiServer._lan_body_rule("POST", "/api/upload", {}) == ordinary
    assert ApiServer._lan_body_rule(
        "POST", "/api/upload", {"x-bob-channel": phone.chan}) == ordinary
    assert ApiServer._lan_body_rule(
        "POST", "/api/upload",
        {"x-bob-channel": phone.chan, "x-bob-frame": "not-a-frame"}) == ordinary
    stranger = relay.seal_frame(relay.mint_key(), relay.DIR_PHONE_TO_MAC, 1,
                                "upload", {"staging": FOLDER, "name": "a.png"},
                                ns=relay.HOME)
    assert ApiServer._lan_body_rule(
        "POST", "/api/upload",
        {"x-bob-channel": phone.chan, "x-bob-frame": stranger}) == ordinary
    assert ApiServer._lan_body_rule(
        "POST", "/api/upload",
        {"x-bob-channel": phone.chan, "x-bob-frame": phone.seal("state", {})}
    ) == ordinary
    reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
    try:
        writer.write(
            b"POST /api/upload HTTP/1.1\r\nHost: 192.168.1.5\r\n"
            + f"Content-Length: {len(big)}\r\n\r\n".encode() + big)
        await writer.drain()
        with pytest.raises((asyncio.IncompleteReadError, asyncio.TimeoutError,
                            ConnectionResetError)):
            await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=3)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass


@pytest.mark.asyncio
async def test_a_verifying_header_frame_gets_the_widened_cap(paired):
    _srv, _daemon, _lan_port, _loop, phone = paired
    wire = phone.seal("upload", {"staging": FOLDER, "name": "a.png"})
    headers = {"x-bob-channel": phone.chan, "x-bob-frame": wire}
    cap, timeout = ApiServer._lan_body_rule("POST", "/api/upload", headers)
    assert cap == attachments.MAX_ATTACHMENT_BYTES + 1024
    assert timeout == api_mod.UPLOAD_READ_TIMEOUT
    # The rule must not note the counter: the handler opens the same frame
    # again and would otherwise refuse its own request as a replay.
    assert devices.last_home_recv_ctr(phone.device_id) == 0
    cap, _timeout = ApiServer._lan_body_rule("POST", "/api/upload", headers)
    assert cap == attachments.MAX_ATTACHMENT_BYTES + 1024
    # And the widening is that route's alone, even with a good frame.
    assert ApiServer._lan_body_rule("POST", "/api/home", headers) == (
        api_mod.MAX_BODY_BYTES, api_mod.READ_TIMEOUT)


@pytest.mark.asyncio
async def test_an_upload_of_that_same_size_is_accepted(paired):
    _srv, _daemon, lan_port, _loop, phone = paired
    status, body = await _upload(lan_port, phone,
                                 body=b"z" * (api_mod.MAX_BODY_BYTES + 64))
    assert status == 200, body
    # The upload's counter was noted durably: a replay of it is refused.
    assert devices.last_home_recv_ctr(phone.device_id) == phone.ctr
    devices.reset()
    assert devices.last_home_recv_ctr(phone.device_id) == phone.ctr


# --- a poll landing mid-upload is not a replay ---------------------------------


async def _read_http(reader):
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    status = int(head.split(b" ")[1])
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    body = await asyncio.wait_for(reader.readexactly(length), timeout=5)
    return status, body


async def _sealed_state(lan_port, phone):
    wire = phone.seal("state", {})
    status, out = await _fetch("/api/home", port=lan_port, data=wire.encode(),
                               headers={"X-Bob-Channel": phone.chan,
                                        "Content-Type": "text/plain"})
    assert status == 200, out
    frame = phone.open(out)
    return frame["kind"], int(frame["body"].get("status") or 0)


@pytest.mark.asyncio
async def test_a_poll_landing_mid_upload_does_not_make_the_upload_a_replay(
        paired, attach_dir):
    """The phone's 4s poll runs on its own task and is not held during an
    upload: its `state` frame (ctr n+1) is noted while the photo (ctr n) is
    still arriving. The frame the body rule opened is the frame the handler
    accepts, so the upload lands anyway."""
    _srv, _daemon, lan_port, _loop, phone = paired
    frame_id = "up-slow"
    wire = phone.seal("upload", {"staging": FOLDER, "name": "photo-1.png"},
                      frame_id=frame_id)
    blob = relay.seal_blob(phone.key, relay.DIR_PHONE_TO_MAC, frame_id, PNG)
    reader, writer = await asyncio.open_connection("127.0.0.1", lan_port)
    try:
        writer.write(
            b"POST /api/upload HTTP/1.1\r\nHost: 192.168.1.5\r\n"
            + f"X-Bob-Channel: {phone.chan}\r\n".encode()
            + f"X-Bob-Frame: {wire}\r\n".encode()
            + b"Content-Type: application/octet-stream\r\n"
            + f"Content-Length: {len(blob)}\r\n\r\n".encode() + blob[:10])
        await writer.drain()
        await asyncio.sleep(0.1)
        # The poll, on a second connection, with the next counter: accepted.
        kind, inner = await _sealed_state(lan_port, phone)
        assert (kind, inner) == ("reply", 200)
        assert devices.last_home_recv_ctr(phone.device_id) == phone.ctr
        # The rest of the photo arrives; the upload is not a replay.
        writer.write(blob[10:])
        await writer.drain()
        status, out = await _read_http(reader)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ConnectionResetError):
            pass
    assert status == 200
    frame = phone.open(out)
    assert frame["kind"] == "reply", frame
    assert frame["body"]["re"] == frame_id
    assert frame["body"]["status"] == 200
    assert json.loads(frame["body"]["body"]) == {
        "ok": True, "path": f"{FOLDER}/photo-1.png", "detail": ""}
    assert (attach_dir / FOLDER / "photo-1.png").read_bytes() == PNG


@pytest.mark.asyncio
async def test_a_replayed_upload_header_on_a_fresh_connection_is_refused(
        paired, attach_dir):
    """The pre-verified acceptance is bound to the connection whose body
    rule opened the frame. The same header frame again, after the floor has
    passed it, meets the ordinary verifier and its ordinary replay refusal —
    and buys no widened cap."""
    _srv, _daemon, lan_port, _loop, phone = paired
    frame_id = "up-once"
    wire = phone.seal("upload", {"staging": FOLDER, "name": "photo-1.png"},
                      frame_id=frame_id)
    blob = relay.seal_blob(phone.key, relay.DIR_PHONE_TO_MAC, frame_id, PNG)
    headers = {"X-Bob-Channel": phone.chan, "X-Bob-Frame": wire,
               "Content-Type": "application/octet-stream"}
    status, out = await _fetch("/api/upload", port=lan_port, data=blob,
                               headers=headers)
    assert status == 200
    assert phone.open(out)["body"]["status"] == 200
    assert ApiServer._lan_body_rule(
        "POST", "/api/upload",
        {"x-bob-channel": phone.chan, "x-bob-frame": wire}) == (
        api_mod.MAX_BODY_BYTES, api_mod.READ_TIMEOUT)
    status, out = await _fetch("/api/upload", port=lan_port, data=blob,
                               headers=headers)
    assert status == 200
    frame = phone.open(out)
    assert frame["kind"] == "err"
    assert frame["body"]["status"] == 409
    assert frame["body"]["error"] == relay.REFUSAL_WORDS["ctr"]
    assert frame["body"]["ctr_expected"] == phone.ctr + 1
    assert not (attach_dir / FOLDER / "photo-1-2.png").exists()


# --- prepare over the LAN door ------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_card_is_a_chosen_phone_verb(paired):
    assert "prepare_card" in ApiServer.LAN_ACTIONS


@pytest.mark.asyncio
async def test_prepare_over_lan_answers_with_the_written_instructions(paired):
    _srv, daemon, lan_port, _loop, phone = paired

    async def fake(payload):
        assert payload["title"] == "a card"
        return {"prompt": "do the thing", "workflow": "plan | build"}, ""

    daemon.prepare_card_text = fake
    status, out = await _home_post(lan_port, phone, {
        "action": "prepare_card", "title": "a card",
        "summary": "one sentence", "tool": "claude",
        "project": "p", "root": "/tmp/p"})
    assert status == 200, out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["prompt"] == "do the thing"
    assert payload["workflow"] == "plan | build"


@pytest.mark.asyncio
async def test_a_refused_prepare_reaches_the_phone_as_a_sentence(paired):
    _srv, daemon, lan_port, _loop, phone = paired

    async def fake(_payload):
        return None, "Dark Army is already writing one"

    daemon.prepare_card_text = fake
    status, out = await _home_post(lan_port, phone, {
        "action": "prepare_card", "summary": "s", "tool": "claude"})
    assert status == 409
    assert json.loads(out)["detail"] == "Dark Army is already writing one"


@pytest.mark.asyncio
async def test_an_unchosen_verb_still_404s_with_a_valid_device_token(paired):
    _srv, _daemon, lan_port, _loop, phone = paired
    status, _ = await _home_post(lan_port, phone,
                                 {"action": "wrap_up", "session_id": "s"})
    assert status == 404


async def _home_post(lan_port, phone, body: dict):
    """One sealed action through `/api/home`; the inner (status, body)."""
    wire = phone.seal("action", body)
    status, out = await _fetch("/api/home", port=lan_port, data=wire.encode(),
                               headers={"X-Bob-Channel": phone.chan,
                                        "Content-Type": "text/plain"})
    assert status == 200, out
    frame = phone.open(out)
    assert frame["kind"] == "reply"
    return int(frame["body"]["status"]), frame["body"]["body"]


# --- the route says what it did -----------------------------------------------
#
# This route was silent in both directions, so a photo that never reached a
# card left no trace anywhere: not on the board, not in the attachments
# folder, not in the log. That is a bug report nobody can answer.


@pytest.mark.asyncio
async def test_a_stored_upload_is_logged_with_its_path(paired, attach_dir, caplog):
    _srv, _daemon, lan_port, _loop, phone = paired
    with caplog.at_level("INFO", logger="dark-army.api"):
        status, _ = await _upload(lan_port, phone)
    assert status == 200
    assert any(f"attachment stored: {FOLDER}/photo-1.png" in r.getMessage()
               for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_a_refused_upload_is_logged_with_bobs_own_sentence(paired,
                                                                attach_dir,
                                                                caplog):
    _srv, _daemon, lan_port, _loop, phone = paired
    with caplog.at_level("INFO", logger="dark-army.api"):
        status, _ = await _upload(lan_port, phone, name="notes.exe")
    assert status == 409
    messages = [r.getMessage() for r in caplog.records]
    assert any("attachment refused" in m and ".exe" in m for m in messages), messages
