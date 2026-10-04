# host/tests/test_phone_buzz_kinds.py
"""The buzz's kind word, pinned at all four ends.

A push carries one word from a closed set: `alerts.KINDS` decides it,
`relay_client.PUSH_KINDS` lets it onto the wire, `relay/api/push.js`'s
`SOUNDS` maps it to a bundled sound, and the phone's project bundles the
files that map names. Four copies of one set, in three languages and a
pbxproj, and none of them is imported by another — so this is where they
are held together. `ios/` has no test target by explicit decision
(`test_phone_push.py`), so the phone half is source pins.
"""

from __future__ import annotations

import io
import re
import sys
import wave
from pathlib import Path

from dark_army_daemon import alerts, relay_client

ROOT = Path(__file__).resolve().parents[2]
PUSH_JS = ROOT / "relay" / "api" / "push.js"
SOUNDS_DIR = ROOT / "ios" / "BobPhone" / "Resources" / "sounds"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
PUSH_SWIFT = ROOT / "ios" / "BobPhone" / "Push.swift"

sys.path.insert(0, str(ROOT / "tools"))
import phone_buzz_sounds  # noqa: E402  (tools/ is not a package)


def _sounds_table() -> dict[str, str]:
    text = PUSH_JS.read_text()
    block = re.search(r"const SOUNDS = \{(.*?)\};", text, re.S)
    assert block, "no SOUNDS table in push.js"
    return dict(re.findall(r'(\w+):\s*"([^"]+)"', block.group(1)))


# --- one set, four ends ----------------------------------------------------------


def test_the_policy_and_the_wire_agree():
    assert alerts.KINDS == relay_client.PUSH_KINDS


def test_the_face_is_the_roster_at_every_end():
    """One slug list, copied because nothing imports another: the wire, the
    mailbox, and the extension that resolves the file. The three kinds that
    may carry it are their own tuple, restated in the menu bar."""
    from dark_army_daemon import identity
    from dark_army_menubar import notifier

    expected = tuple(n.lower() for n in identity.NAMES)
    assert alerts.FACE_KINDS == ("question", "permission", "finished")
    assert notifier.FACE_KINDS == alerts.FACE_KINDS
    assert relay_client.PUSH_FACE_SLUGS == expected
    text = PUSH_JS.read_text()
    block = re.search(r"const FACE_SLUGS = \[(.*?)\];", text, re.S)
    assert block, "no FACE_SLUGS in push.js"
    assert tuple(re.findall(r'"([^"]+)"', block.group(1))) == expected
    swift = (ROOT / "ios" / "BobPhoneNotification" / "NotificationFace.swift").read_text()
    slugs = re.search(r"static let slugs: \[String\] = \[(.*?)\]", swift, re.S)
    assert slugs, "no slug list in NotificationFace.swift"
    assert tuple(re.findall(r'"([^"]+)"', slugs.group(1))) == expected
    assert "ptys" in expected


def test_the_two_ends_clamp_the_work_line_at_the_same_length():
    """`work` — the buzz's second line — is bounded twice: once on the Mac so
    nothing unbounded leaves the machine, once in the mailbox because that is
    where the payload is built. Two copies of one number, the `KINDS`
    argument one field on."""
    text = PUSH_JS.read_text()
    found = re.search(r"const MAX_WORK_CHARS = (\d+);", text)
    assert found, "no MAX_WORK_CHARS in push.js"
    assert int(found.group(1)) == relay_client.PUSH_WORK_CHARS


def test_the_two_ends_clamp_the_need_line_at_the_same_length():
    """`need` — the buzz's third line — is bounded the same two ways as
    `work`: on the Mac so nothing unbounded leaves the machine, and in the
    mailbox because that is where the payload is built."""
    text = PUSH_JS.read_text()
    found = re.search(r"const MAX_NEED_CHARS = (\d+);", text)
    assert found, "no MAX_NEED_CHARS in push.js"
    assert int(found.group(1)) == relay_client.PUSH_NEED_CHARS == 120


def test_the_mailbox_knows_exactly_the_same_kinds():
    assert set(_sounds_table()) == set(alerts.KINDS)


def test_the_tool_names_every_sound_the_mailbox_maps_to():
    wanted = {Path(v).stem for v in _sounds_table().values()}
    assert wanted == set(phone_buzz_sounds.SPECS)


# --- the files ----------------------------------------------------------------------


def test_every_mapped_sound_is_a_file_in_the_phone_tree():
    for name in _sounds_table().values():
        assert (SOUNDS_DIR / name).is_file(), name


def test_each_file_re_renders_byte_identical_from_the_tool():
    """Generated, never hand-edited: the tool is the source and `--check`
    is the same comparison at the command line."""
    for name in phone_buzz_sounds.SPECS:
        path = SOUNDS_DIR / f"{name}.wav"
        assert phone_buzz_sounds.render(name) == path.read_bytes(), name


def test_each_file_is_a_short_mono_pcm_cue():
    """What `aps.sound` will play: Linear PCM, one channel, 16-bit, under a
    second — a cue, not a jingle."""
    for name in phone_buzz_sounds.SPECS:
        path = SOUNDS_DIR / f"{name}.wav"
        with wave.open(io.BytesIO(path.read_bytes())) as w:
            assert w.getnchannels() == 1, name
            assert w.getsampwidth() == 2, name
            assert w.getframerate() == 22050, name
            assert w.getnframes() / w.getframerate() <= 1.0, name


def test_the_three_cues_differ_and_finished_is_the_quiet_one():
    rendered = {n: phone_buzz_sounds.render(n) for n in phone_buzz_sounds.SPECS}
    assert len(set(rendered.values())) == 3
    peaks = {n: spec[0] for n, spec in phone_buzz_sounds.SPECS.items()}
    assert peaks["buzz-finished"] < peaks["buzz-permission"]
    assert peaks["buzz-finished"] < peaks["buzz-question"]


def test_the_tool_check_passes_against_the_tree(capsys):
    assert phone_buzz_sounds.main(["--check"]) == 0


# --- the phone bundles them ------------------------------------------------------


def test_the_phone_actually_bundles_each_sound():
    text = PBXPROJ.read_text()
    for name in _sounds_table().values():
        assert f"path = Resources/sounds/{name}" in text, name
        assert text.count(f"{name} in Resources") >= 2, (
            f"{name}: need both the build file and its place in the phase"
        )


def test_the_sounds_ride_the_app_target_not_the_widget():
    """The BobPhone target's Resources phase, by id; the widget has no push."""
    text = PBXPROJ.read_text()
    phase = text.split("7B0B0E1A0000000000000022 /* Resources */ = {")[1]
    phase = phase.split("};")[0]
    for name in _sounds_table().values():
        assert f"{name} in Resources" in phase, name
    widget = text.split("7B0B0E1A0000000000000133 /* Resources */ = {")[1]
    widget = widget.split("};")[0]
    assert "buzz-" not in widget


def test_every_pbxproj_id_is_unique():
    """A duplicate id builds a project Xcode opens but builds wrong."""
    text = PBXPROJ.read_text()
    declared = re.findall(r"^\t\t([0-9A-F]{24}) /\*", text, re.M)
    assert len(declared) == len(set(declared))


# --- the phone's Swift did not move ----------------------------------------------


def test_the_fleet_keys_are_the_same_seven_strings_at_every_end():
    """The counts and the figures: one tuple on the Mac, the same names in
    the mailbox, the same raw values on the phone. A drift here is a card
    the other end cannot read."""
    from dark_army_daemon import live_activity
    text = PUSH_JS.read_text()
    found = re.search(r"const FLEET_INT_KEYS = \[(.*?)\];", text)
    assert found, "no FLEET_INT_KEYS in push.js"
    ints = re.findall(r'"([^"]+)"', found.group(1))
    usd = re.search(r"const FLEET_USD_KEYS = \[(.*?)\];", text)
    assert usd, "no FLEET_USD_KEYS in push.js"
    floats = re.findall(r'"([^"]+)"', usd.group(1))
    swift = (ROOT / "ios" / "Shared" / "NeedsYouActivity.swift").read_text()
    raws = re.findall(r'case \w+ = "([^"]+)"', swift)
    fleet = list(live_activity.COUNT_KEYS + live_activity.FIGURE_KEYS)
    assert list(relay_client.ACTIVITY_FLEET_KEYS) == fleet
    # The token figures sit with the other ints in the mailbox; the two
    # dollar figures are the floats. Same seven strings, split by type.
    assert set(ints + floats) == set(fleet)
    assert ints == ["working", "needs_you", "standing_by", "tokens_k",
                    "tokens_k_hour"]
    assert floats == ["cost_usd", "cost_usd_hour"]
    for name in fleet:
        assert name in raws, name


def test_the_phone_reads_nothing_off_the_payload():
    """`aps.sound` is the phone's whole part in this: no kind is parsed, the
    foreground stays quiet and a tap still lands on Needs you."""
    text = PUSH_SWIFT.read_text()
    assert "willPresent" in text
    present = text.split("willPresent")[1].split("\n    }")[0]
    assert "return []" in present
    receive = text.split("didReceive")[1].split("\n    }")[0]
    assert "PhoneRouter.shared.go(.needs)" in receive
    # The delegate parses no kind and names no sound of its own: `.sound` is
    # the authorization option list, and the file it plays is Apple's to pick.
    assert '"kind"' not in text
    assert "UNNotificationSound" not in text
    assert "buzz-" not in text


def test_picks_plays_the_question_cue_and_adds_no_file():
    sounds = _sounds_table()
    assert sounds["picks"] == sounds["question"]
