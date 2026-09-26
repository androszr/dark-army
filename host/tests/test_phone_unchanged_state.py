# host/tests/test_phone_unchanged_state.py
"""The phone's half of the conditional `state` read — source pins over the Swift.

`ios/` has no test target by decision (`test_phone_event_log.py`'s standing
pattern); this suite reads the Swift as text. What it pins, in order of how
much it would cost to get wrong:

* the snapshot is assigned in exactly one place, inside `applyState`, and
  `applyState` reads the `unchanged` marker **before** it decodes `Snapshot` —
  the empty-snapshot trap, which would blank the fleet, the board and the
  needs-you count on every quiet poll;
* both sealed state reads send `stateRequestBody()`;
* the resync floor is 60s and the held digest never reaches disk;
* the digest is dropped when the pairing is;
* `Models.swift` names `state_digest` and declares `StateAnswer` once;
* no phone file draws any member of `_POLL_ECHO_FIELDS` — imported from the
  daemon, so the two sides cannot drift;
* the check-in cadence is untouched.
"""

from __future__ import annotations

from pathlib import Path

from dark_army_daemon.api_server import _POLL_ECHO_FIELDS

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
CLIENT = PHONE / "Client.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _block(text: str, start: str) -> str:
    """The braces-balanced block that opens at the first `start`."""
    i = text.index(start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError(f"unbalanced block at {start!r}")


def test_the_snapshot_is_assigned_only_inside_apply_state():
    """Two permitted spellings, each exactly once and each in its own
    function: the live one in `applyState`, and the held picture's
    `snapshot = held` in `restoreHeldPicture`, which never quotes a digest."""
    text = _read(CLIENT)
    assert text.count("snapshot = decoded") == 1
    block = _block(text, "private func applyState(")
    assert "snapshot = decoded" in block
    assert text.count("snapshot = held") == 1
    restore = _block(text, "private func restoreHeldPicture(")
    assert "snapshot = held" in restore
    assert "heldStateDigest" not in restore


def test_apply_state_reads_the_unchanged_marker_before_decoding_a_snapshot():
    """The single most damaging bug available here: `Snapshot` decodes
    anything, so an unchanged body read as one is a blank fleet."""
    block = _block(_read(CLIENT), "private func applyState(")
    assert block.index("StateAnswer.self") < block.index("Snapshot.self")
    assert block.index("answer.unchanged") < block.index("Snapshot.self")


def test_both_poll_legs_read_the_answer_off_the_main_actor():
    """A whole picture is ~440 KB; parsed on the main actor it stalled the
    screen mid-scroll. Each poll leg reads it with `StateFrame.offMain`
    straight after the transport's await and hands the frame to both
    readers; the frame keeps `applyState`'s marker-first order."""
    text = _read(CLIENT)
    for name in ("private func pollDirect(", "private func pollViaRelay("):
        leg = _block(text, name)
        read = leg.index("await StateFrame.offMain(answer.body)")
        assert read < leg.index("frame: frame) != .unreadable")
        assert "takeUsage(from: answer.body, record: record, frame: frame)" in leg
    # Both legs read the pairing again after the decode's await, so an
    # unpair while the picture was being read is never drawn over.
    for name in ("private func pollDirect(", "private func pollViaRelay("):
        leg = _block(text, name)
        assert leg.index("StateFrame.offMain") \
            < leg.index("guard record.token == self.record?.token") \
            < leg.index("frame: frame) != .unreadable")
    frame = _block(_read(MODELS), "struct StateFrame")
    assert frame.index("StateAnswer(from: decoder)") \
        < frame.index("if !frame.answer.unchanged") \
        < frame.index("Snapshot(from: decoder)")
    assert "Task.detached" in frame


def test_both_state_reads_send_the_conditional_body():
    text = _read(CLIENT)
    assert text.count("stateRequestBody") == 3  # one definition, two callers
    assert text.count('kind: "state"') == 2
    for line in text.splitlines():
        if 'kind: "state"' in line:
            following = text[text.index(line):]
            assert "stateRequestBody()" in following[:400], line


def test_the_resync_floor_is_a_minute():
    text = _read(CLIENT)
    assert "static let fullStateFloor: TimeInterval = 60" in text
    assert "fullStateFloor" in _block(
        text, "private func stateRequestBody()")


def test_the_held_digest_is_memory_only_and_dies_with_the_pairing():
    text = _read(CLIENT)
    assert "private var heldStateDigest = \"\"" in text
    for line in text.splitlines():
        if "heldStateDigest" in line:
            assert "UserDefaults" not in line
            assert "Keychain" not in line
    assert 'heldStateDigest = ""' in _block(
        text, "private func forgetPairing()")
    assert 'heldStateDigest = ""' in _block(text, "func stop()")
    assert "heldSectionDigests = [:]" in _block(
        text, "private func forgetPairing()")
    assert "heldSectionDigests = [:]" in _block(text, "func stop()")


def test_models_names_the_digest_and_declares_the_answer_once():
    text = _read(MODELS)
    assert '= "state_digest"' in text
    assert text.count("struct StateAnswer") == 1
    assert "var unchanged = false" in _block(text, "struct StateAnswer")


def test_no_phone_file_draws_a_poll_echo_field():
    """Strip a field the phone reads and the screen goes stale for real."""
    for path in sorted(PHONE.rglob("*.swift")):
        text = path.read_text()
        for field in _POLL_ECHO_FIELDS:
            assert field not in text, f"{path.name} draws {field}"


def test_the_check_in_cadence_is_untouched():
    text = _read(CLIENT)
    assert "stateRequestBody" not in _block(text, "func noteAttempt(")
    # The two pause constants `noteAttempt` returns: 4s at home, 8s through
    # the mailbox. This is a payload rule, never a timing one.
    assert "8_000_000_000 : 4_000_000_000" in _block(
        text, "func noteAttempt(")
