"""Python source pins over the phone's dictation button.

`ios/` has no test target, so this is a lint over the Swift sources, the
Info.plist and the pbxproj — `test_phone_writes.py`'s pattern. The microphone
itself is judged on a real phone; everything decidable without one (the merge
rule, the locale ladder, the four refusal messages, the teardown wiring, the
pairing exclusion, the project registration) is pinned here.
"""

import json
import plistlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
DICTATION = PHONE / "Dictation.swift"
INFO_PLIST = PHONE / "Info.plist"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

CONSENT_KEYS = ("NSMicrophoneUsageDescription", "NSSpeechRecognitionUsageDescription")

REFUSAL_CASES = ("consent", "noRecognizer", "unavailable", "audio")


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def test_info_plist_carries_both_consent_strings():
    with INFO_PLIST.open("rb") as handle:
        plist = plistlib.load(handle)
    for key in CONSENT_KEYS:
        assert key in plist, f"{key} missing from Info.plist"
        assert plist[key].strip(), f"{key} must say something"


def test_engine_prefers_on_device_recognition():
    assert "requiresOnDeviceRecognition" in _read(DICTATION)


def test_permission_uses_the_ios_17_record_api():
    source = _read(DICTATION)
    assert "AVAudioApplication.requestRecordPermission" in source
    assert "AVAudioApplication.shared" in source
    # The deprecated pre-iOS-17 form must not come back.
    assert "AVAudioSession.sharedInstance().requestRecordPermission" not in source


def test_recogniser_is_chosen_from_the_supported_set():
    assert "supportedLocales" in _read(DICTATION)


def test_no_locale_is_hardcoded():
    source = _read(DICTATION)
    for literal in ("pl-PL", "pl_PL"):
        assert literal not in source, f"{literal} is hardcoded"


def test_audio_session_is_released_and_resign_active_stops():
    source = _read(DICTATION)
    assert "setActive(false" in source, "the audio session is never released"
    assert "willResignActiveNotification" in source
    assert "interruptionNotification" in source


def test_the_four_refusal_messages_are_distinct_and_non_empty():
    source = _read(DICTATION)
    body = source.split("enum DictationRefusal", 1)
    assert len(body) == 2, "DictationRefusal is gone"
    for case in REFUSAL_CASES:
        assert re.search(rf"case \.{case}:", body[1]), f"no message for .{case}"
    messages = re.findall(r'return "([^"]+)"', body[1].split("// MARK", 1)[0])
    assert len(messages) == len(REFUSAL_CASES), messages
    assert all(message.strip() for message in messages)
    assert len(set(messages)) == len(messages), "two refusals say the same thing"


def test_the_pure_seams_exist():
    source = _read(DICTATION)
    assert "static func merged(base:" in source
    assert "static func resolve(preferred:" in source


def test_no_force_unwrap_in_the_engine():
    source = _read(DICTATION)
    assert not re.search(r"try!|as!|\)!", source), "force unwrap in Dictation.swift"


def test_both_writing_surfaces_offer_the_button():
    # The agent screen's reply moved wholesale into `AnswerBox`, which both
    # it and the "Needs you" tab draw — one decision site, one microphone.
    for name in ("ComposerView.swift", "AnswerBox.swift"):
        source = _read(PHONE / name)
        assert "MicButton" in source, f"{name} offers no microphone"
        assert "DictationEngine.shared" in source or "dictation.stop()" in source


def test_every_composer_prose_field_is_dictatable():
    source = _read(PHONE / "ComposerView.swift")
    for field in ("composer.title", "composer.summary", "composer.notes"):
        assert f'"{field}"' in source, f"{field} has no microphone"


def test_the_pairing_screen_has_no_microphone():
    source = _read(PHONE / "Pairing.swift")
    assert "MicButton" not in source
    assert "Dictation" not in source


def test_the_lock_is_exempted_across_the_consent_alert():
    # A system consent alert makes the scene inactive; locking there would
    # unmount the composer and destroy a half-typed card.
    assert "requestingConsent" in _read(ROOT / "ios" / "BobPhone" / "BobPhoneApp.swift")


def test_dictation_is_registered_in_the_project():
    source = _read(PBXPROJ)
    assert source.count("Dictation.swift") >= 2
    assert "Dictation.swift in Sources" in source
    assert "path = Dictation.swift" in source


# ── the merge, run rather than read ─────────────────────────────────────────

# `DictationSession` is pure Swift with no imports, so it can be compiled and
# *asked* rather than read. The phone lost a long dictated note on 8 Sep 2026:
# the recogniser throws its transcription away at a pause and starts over
# (Apple's own behaviour), so a merge writing `base + hypothesis` wrote the
# newest sentence over everything said before it. These replay that sequence
# through the real Swift; without a toolchain they skip.

MERGE_START = "// MARK: - Merge"
MERGE_END = "// MARK: - Locale"


def _merge_source() -> str:
    source = _read(DICTATION)
    assert MERGE_START in source and MERGE_END in source, "the merge section moved"
    return source.split(MERGE_START, 1)[1].split(MERGE_END, 1)[0]


def _run_session(steps, base: str = "") -> str:
    """Replay `(hypothesis, utteranceEnded)` steps and return the field's text."""
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        pytest.skip("no swiftc on this machine")
    lines = [f'var s = DictationSession(base: {json.dumps(base)})', 'var out = ""']
    for hypothesis, ended in steps:
        lines.append(f'out = s.advance(hypothesis: {json.dumps(hypothesis)}, '
                     f'utteranceEnded: {"true" if ended else "false"})')
    lines.append("print(out)")
    program = _merge_source() + "\n" + "\n".join(lines) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "merge.swift"
        source.write_text(program)
        binary = Path(tmp) / "merge"
        build = subprocess.run([swiftc, str(source), "-o", str(binary)],
                               capture_output=True, text=True)
        assert build.returncode == 0, build.stderr
        run = subprocess.run([str(binary)], capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def test_a_pause_does_not_delete_what_was_already_said():
    """The reported failure, replayed: the recogniser finishes an utterance and
    the next partial starts from nothing. Every word survives."""
    text = _run_session([
        ("the daemon", False),
        ("the daemon should", False),
        ("the daemon should write the card", False),
        # The result carrying `speechRecognitionMetadata`: this utterance ended.
        ("The daemon should write the card.", True),
        ("and then", False),
        ("and then close the terminal", False),
    ])
    assert "The daemon should write the card." in text
    assert text.endswith("and then close the terminal")


def test_a_restart_with_no_metadata_is_caught_by_the_shrink():
    """A result carrying no metadata still keeps its words: a hypothesis at
    most half the length of the one before it is a fresh start, not a
    revision."""
    text = _run_session([
        ("one two three four five six seven eight", False),
        ("nine", False),
        ("nine ten", False),
    ])
    assert "one two three four five six seven eight" in text
    assert text.endswith("nine ten")


def test_an_ordinary_growing_hypothesis_is_never_doubled():
    """The other half of the rule: a recogniser polishing one guess must not
    have its words banked twice."""
    assert _run_session([
        ("hello", False),
        ("hello there", False),
        ("Hello there, world", False),
    ]) == "Hello there, world"


def test_what_was_already_typed_stays_in_front():
    assert _run_session([("spoken words", True), ("and more", False)],
                        base="already typed") == "already typed spoken words and more"
