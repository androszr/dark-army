# host/tests/pake_pair.py
"""The shared test client for the typed-address pair: SRP-6a start → finish
over a real socket, the reply opened under the derived key.

Imported by the door test files (`test_lan_access.py`, `test_home_seal.py`,
`test_access_log_doors.py`, `test_inbox_ack_door.py`,
`test_phone_review_and_manual_acks.py`, `test_lan_upload.py`,
`test_lan_door.py`) in place of the plain `{code, name}` post the branch
used to answer with the keys in the clear. The returned dict keeps the old
helpers' shape — `token`, `device_id`, `relay_key`, `relay_url`, the raw
derived key under `key` and its base64 under `home_key` (the phone's own
spelling, so a "never published" assertion still has something to look
for) — plus `recv_ctr`, the sealed reply's counter.

`fetch` is each file's own async `fetch(path, *, port, data, headers)`.
The floor is 0 on this route: the two requests are plain JSON, not sealed
frames, so every caller's first home frame is still counter 1.
"""

from __future__ import annotations

import base64
import json

from dark_army_daemon import devices, relay, srp


async def typed_exchange(fetch, lan_port: int, code: str, *,
                         name: str = "phone", identity: str | None = None,
                         a: int | None = None) -> dict:
    """One full exchange with ``code`` as the password. Returns the raw
    record of it — every byte both ways and the client's own quantities —
    so a test can assert on the wire as well as on the outcome::

        {"start": (status, bytes), "finish": (status, bytes),
         "A": hex, "M1": hex, "K": bytes | None, "key": bytes | None,
         "salt": hex, "B": hex, "pairing": str}

    ``K`` / ``key`` are ``None`` until the start answered in shape. A
    refused finish leaves ``finish`` as the door's 403 and the rest as
    computed. ``identity`` overrides the pairing id the Mac names (a wrong
    one makes a wrong proof); ``a`` pins the ephemeral for a fixed
    transcript.
    """
    params = srp.PRODUCTION
    group = params.group
    a_value = a if a is not None else srp.random_ephemeral()
    A = srp.client_public(params, a_value)
    A_hex = srp.pad(group, A).hex()
    record: dict = {"A": A_hex, "M1": "", "K": None, "key": None,
                    "salt": "", "B": "", "pairing": ""}
    body = json.dumps({"pake": "start", "A": A_hex}).encode()
    record["start"] = await fetch(
        "/api/pair", port=lan_port, data=body,
        headers={"Content-Type": "application/json"})
    status, out = record["start"]
    if status != 200:
        record["finish"] = (0, b"")
        return record
    reply = json.loads(out)
    record["pairing"] = str(reply.get("pairing") or "")
    record["salt"] = str(reply.get("salt") or "")
    record["B"] = str(reply.get("B") or "")
    salt = bytes.fromhex(record["salt"])
    B = int(record["B"], 16)
    ident = identity if identity is not None else record["pairing"]
    password = str(code or "").strip().upper()
    x_value = srp.x(params, ident, password, salt)
    u_value = srp.u(params, A, B)
    S = srp.client_secret(params, B, a_value, x_value, u_value)
    K = srp.session_key(params, S)
    M1 = srp.client_proof(params, ident, salt, A, B, K)
    record["M1"] = M1.hex()
    record["K"] = K
    record["key"] = srp.home_key(K)
    body = json.dumps({"pake": "finish", "A": A_hex, "M1": M1.hex(),
                       "name": name}).encode()
    record["finish"] = await fetch(
        "/api/pair", port=lan_port, data=body,
        headers={"Content-Type": "application/json"})
    return record


def open_pair_reply(record: dict) -> tuple:
    """Open a 200 finish under the derived key. ``(frame, err)`` —
    `relay.open_frame`'s own pair, `lastCtr` 0 as the phone opens it."""
    status, out = record["finish"]
    assert status == 200, out
    return relay.open_frame(record["key"], relay.DIR_MAC_TO_PHONE,
                            out.decode("ascii", "replace").strip(), 0,
                            ns=relay.HOME)


async def pair_typed(fetch, lan_port: int, *, name: str = "phone",
                     code: str | None = None) -> dict:
    """`devices.begin(allow_plain=True)` (unless ``code`` is given) then the
    whole exchange, the reply opened and `M2` verified. Returns the paired
    record: ``token``, ``device_id``, ``relay_key``, ``relay_url``, ``key``
    (bytes), ``home_key`` (base64), ``recv_ctr``."""
    if code is None:
        code = devices.begin(allow_plain=True)
    record = await typed_exchange(fetch, lan_port, code, name=name)
    assert record["start"][0] == 200, record["start"]
    frame, err = open_pair_reply(record)
    assert err == "", err
    assert frame["kind"] == "reply", frame
    inner_status = int(frame["body"]["status"])
    assert inner_status == 200, frame["body"]
    payload = json.loads(frame["body"]["body"])
    expected = srp.server_proof(srp.PRODUCTION, int(record["A"], 16),
                                bytes.fromhex(record["M1"]), record["K"])
    assert payload.get("M2") == expected.hex(), "M2 did not verify"
    key = record["key"]
    return {
        "token": payload["token"],
        "device_id": payload["device_id"],
        "relay_key": payload.get("relay_key", ""),
        "relay_url": payload.get("relay_url", ""),
        "relay_ws_url": payload.get("relay_ws_url", ""),
        "key": key,
        "home_key": base64.b64encode(key).decode("ascii"),
        "recv_ctr": int(frame["ctr"]),
    }
