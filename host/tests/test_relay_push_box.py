# host/tests/test_relay_push_box.py
"""Source pins on the relay's push function.

``relay/api/push.js`` runs on Vercel, not here — ``test_relay_box.py``'s
pattern exactly: what the plan's security argument rests on is pinned from
this side, so a drift fails the suite rather than dying quietly against
Apple's servers.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUSH = ROOT / "relay" / "api" / "push.js"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_no_logging_of_anything_carried():
    assert "console.log" not in _read(PUSH)


def test_small_enough_to_audit_in_one_read():
    # 200 before the Bearer gate and the global rate window; 240 before the
    # APNs timeout, the 403 retry and the collapse headers; 280 before `work`
    # — the subtitle line — and the argument for relaxing the old
    # nickname-and-count rule that had to be written down beside it. The code
    # that grew is three lines; the rest is that argument. 380 after the
    # live card's leg (2026-09-20): a second branch building an ActivityKit
    # content-state from the same closed set, its own validation line and
    # the header comment saying why it carries no `title`. 390 after the
    # activity leg learned to tell Apple's transient answers (5xx, 429 →
    # 503 `unavailable`, retried) from its refusals (4xx → 502, remembered),
    # because one 503 on an `end` used to wedge the Lock Screen card. 400
    # after the buzz learned to name its subject (2026-09-20): one block in
    # the alert leg re-checking `session_id` / `card_id` against ID_SHAPE
    # (`bad subject`) and two header lines saying what they are for. 420
    # after the fleet card (2026-09-21): the activity leg validates the
    # five short fields and answers `ok shape=2`; the alert leg's bytes
    # are unchanged. 460 after the alert leg learned a face (2026-09-22):
    # a closed slug list and one block that copies a member onto the
    # payload and sets `mutable-content`. The activity leg is untouched.
    # Still one read.
    assert len(_read(PUSH).splitlines()) <= 460


def test_a_hung_apple_connection_is_bounded_and_torn_down():
    """Vercel kills the function at 10s (vercel.json maxDuration) and answers
    a 504 the Mac never asked for; the route's own 8s timeout answers the
    503 it promises instead. destroy(), not close(): close waits for the
    very stream that hung. Both error paths destroy the session too."""
    text = _read(PUSH)
    assert "APNS_TIMEOUT_MS = 8000" in text
    assert "setTimeout" in text
    assert text.count("session.destroy()") >= 3
    assert text.count("clearTimeout(timer)") >= 3


def test_a_rotated_apns_key_retries_once_with_a_fresh_jwt():
    """A 403 (Invalid/ExpiredProviderToken) usually means the cached JWT was
    signed with a key rotated since; the route clears the cache and retries
    exactly once rather than answering 502 for up to 50 minutes."""
    text = _read(PUSH)
    assert "if (status === 403)" in text
    assert 'jwtCache = { token: "", born: 0 };' in text
    # Once: two sends in the handler, the retry and the original.
    handler = text.split("module.exports")[1]
    # `leg` is which push type the send wears (alert or liveactivity); the
    # retry sends the same payload on the same leg.
    assert handler.count("await apnsSend(host, tok, payload, leg)") == 2


def test_an_offline_phone_wakes_to_one_collapsed_banner():
    text = _read(PUSH)
    # A receipt keeps unrelated decisions separate; legacy pushes share bob.
    assert '"apns-collapse-id": payload.receipt_id || APNS_COLLAPSE_ID' in text
    assert 'APNS_COLLAPSE_ID = "bob"' in text
    assert "APNS_EXPIRATION_SECONDS = 600" in text
    assert '"apns-expiration"' in text


def test_zero_dependencies():
    """`node:` builtins only — the mailbox's own rule. A require of anything
    else means an npm install step the deployment does not have."""
    for match in re.findall(r'require\("([^"]+)"\)', _read(PUSH)):
        assert match.startswith("node:"), match


def test_apns_rides_http2_never_fetch():
    """Vercel's global fetch (undici) is HTTP/1.1 only; APNs requires HTTP/2.
    A fetch() to Apple fails in ways that look like Apple refusing. The one
    fetch allowed in this file is the Upstash rate call."""
    text = _read(PUSH)
    assert 'require("node:http2")' in text
    assert "http2.connect" in text
    # fetch appears only inside the redis helper, aimed at Upstash.
    for line in text.splitlines():
        if "fetch(" in line:
            assert "url" in line and "pipeline" in line, line


def test_jwt_is_ieee_p1363_not_der():
    """Node's default ECDSA signatures are DER; APNs rejects every DER-signed
    JWT with InvalidProviderToken. The dsaEncoding option is the whole fix."""
    assert 'dsaEncoding: "ieee-p1363"' in _read(PUSH)


def test_rate_window_expires_only_when_it_opens():
    """The box.js INCR + `EXPIRE … NX` pattern, on the push route's own key.
    Exactly one EXPIRE, issued when the window opens — re-arming it per
    request slides the window forever and brings the permanent-429 lockout
    back."""
    text = _read(PUSH)
    assert len(re.findall(r'\["EXPIRE",\s*rateKey,\s*String\(60\),\s*"NX"\]',
                          text)) == 1
    assert '["INCR", rateKey]' in text
    assert "rate:${ch}:push" in text
    assert "RATE_PER_MINUTE = 30" in text


def test_a_per_row_redis_error_fails_loudly():
    assert 'if (row.error !== undefined) throw new Error("redis")' \
        in _read(PUSH)


def test_the_caps_are_present():
    text = _read(PUSH)
    assert "MAX_TITLE_CHARS = 120" in text
    assert "MAX_BADGE = 999" in text
    assert re.search(r"TOK_SHAPE = /\^?\[0-9a-f\]\{32,200\}", text)


def test_aps_is_built_server_side():
    """The route carries a title and a count, never a caller-supplied dict —
    the payload is composed in this file, so no future caller can widen what
    transits Apple in plaintext."""
    text = _read(PUSH)
    assert "aps:" in text
    assert "alert: { title }" in text
    assert '"thread-id": "bob"' in text
    # The sound is a lookup in this file's own table, never the caller's word.
    assert 'sound: SOUNDS[kind] || "default"' in text
    assert 'sound: "default"' not in text
    # The parsed body contributes exactly the named scalars.
    for field in ("body.tok", "body.env", "body.title", "body.badge",
                  "body.kind", "body.work", "body.need"):
        assert field in text
    assert text.count("body.kind") == 1
    # The third line lands in the alert's body slot only when non-empty —
    # never an empty key — and only on the alert leg.
    assert "if (need) payload.aps.alert.body = need;" in text
    assert text.count("payload.aps.alert.body") == 1
    # And never rides through whole.
    assert "req.end(JSON.stringify(payload))" in text
    assert "req.end(JSON.stringify(body))" not in text


def test_the_two_apns_hosts_are_chosen_by_env():
    """A dev-signed build's token is invalid against the production host and
    vice versa (BadDeviceToken); the `env` field decides per request."""
    text = _read(PUSH)
    assert "api.push.apple.com" in text
    assert "api.sandbox.push.apple.com" in text
    assert '["prod", "dev"].includes(env)' in text


def test_the_push_route_demands_the_bearer_secret():
    """`ch` is caller-chosen and proves nothing, so without a password anyone
    holding the relay URL could buzz any device token with any title. The
    Bearer comparison is constant-time on equal-length buffers, a mismatch is
    a 401, and an unset PUSH_SECRET is a 503 — off, never open."""
    text = _read(PUSH)
    assert "process.env.PUSH_SECRET" in text
    assert "crypto.timingSafeEqual(got, want)" in text
    assert "got.length !== want.length" in text
    assert 'answer(res, 401, "unauthorized")' in text
    assert 'answer(res, 503, "unconfigured")' in text
    # Unconfigured is one answer for the whole credential set — the topic
    # included, or Apple's MissingTopic came back as a 502 that looked like
    # an outage rather than a missing env var.
    for var in ("APNS_KEY_P8", "APNS_KEY_ID", "APNS_TEAM_ID"):
        assert f"process.env.{var}" in text
    assert "!process.env.APNS_TOPIC)" in text
    # And the send no longer papers over a missing topic with "".
    assert 'process.env.APNS_TOPIC || ""' not in text


def test_a_rotated_channel_id_cannot_dodge_the_rate_cap():
    """The global window beside the per-channel one: minting a fresh 32-hex
    `ch` per request must not mint a fresh 30/min budget. Same INCR +
    `EXPIRE … NX` shape, one EXPIRE per key."""
    text = _read(PUSH)
    assert '["INCR", "rate:push"]' in text
    assert len(re.findall(
        r'\["EXPIRE",\s*"rate:push",\s*String\(60\),\s*"NX"\]', text)) == 1
    assert "GLOBAL_RATE_PER_MINUTE = 120" in text
    assert "Number(total) > GLOBAL_RATE_PER_MINUTE" in text


def _sounds(text: str) -> dict[str, str]:
    block = re.search(r"const SOUNDS = \{(.*?)\};", text, re.S)
    assert block, "no SOUNDS table"
    return dict(re.findall(r'(\w+):\s*"([^"]+)"', block.group(1)))


def test_the_sound_is_chosen_here_from_a_closed_set():
    """The kind word picks a sound through a table in this file — five keys,
    every value a bundled `.wav` — and an absent or unknown kind falls
    through to Apple's default. No caller can name a sound. `security`
    reuses the permission cue: no new file."""
    text = _read(PUSH)
    sounds = _sounds(text)
    assert set(sounds) == {"security", "permission", "question", "attention", "finished"}
    assert sounds["security"] == sounds["permission"]
    assert all(v.startswith("buzz-") and v.endswith(".wav")
               for v in sounds.values())
    assert 'const kind = String(body.kind || "")' in text
    # A plain object literal inherits `constructor` and friends; the lookup
    # must not be able to reach them.
    assert "Object.setPrototypeOf(SOUNDS, null)" in text
    assert "body.sound" not in text


# --- the live card's leg -----------------------------------------------------------


def test_the_activity_leg_is_a_closed_set_and_never_carries_a_title():
    """The ActivityKit branch builds `aps.content-state` in this file from
    six named fields, keyed on `body.event`; the buzz's `title` is not read
    there, which is what makes an undeployed mailbox answer the new leg with
    400 rather than mis-sending an alert."""
    text = _read(PUSH)
    assert 'const ACTIVITY_EVENTS = ["update", "end"];' in text
    assert 'const ACTIVITY_KINDS = ["permission", "question", "attention"];' in text
    assert '(slug !== "" && !FACE_SLUGS.includes(slug))' in text
    assert "ACTIVITY_STALE_SECONDS = 1800" in text
    branch = text.split("if (body.event !== undefined) {")[1].split("} else {")[0]
    assert '"title"' not in branch and "body.title" not in branch
    assert '"content-state": {' in branch
    for field in ("nickname:", "slug,", "kind:", "work,", "since,", "session_id: sid"):
        assert field in branch, field
    assert '"stale-date": timestamp + ACTIVITY_STALE_SECONDS' in branch
    assert 'payload.aps["dismissal-date"] = timestamp' in branch
    assert 'answer(res, 400, "bad activity")' in branch


def test_the_activity_leg_wears_its_own_push_type_and_topic():
    """`liveactivity` on the `.push-type.liveactivity` topic, no collapse id
    (an update and an end must both land, in order); the alert leg keeps
    its type, topic and collapse id byte for byte."""
    text = _read(PUSH)
    assert text.count("apns-push-type") == 2
    assert text.count("push-type.liveactivity") == 1
    activity = text.split('leg === "liveactivity" ? {')[1].split("} : {")[0]
    assert '"apns-push-type": "liveactivity"' in activity
    assert "apns-collapse-id" not in activity
    alert = text.split("} : {")[1].split("};")[0]
    assert '"apns-push-type": "alert"' in alert
    assert '"apns-collapse-id": payload.receipt_id || APNS_COLLAPSE_ID' in alert
