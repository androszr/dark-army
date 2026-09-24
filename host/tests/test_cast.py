"""The face rule, and that it is the *same* face the panel draws.

A banner showing one character for a session while the panel row shows another
is worse than a banner with no face: the cast only works if a face means an
agent, and two answers to "who is this" teaches you to read neither.

The golden values below were not derived by reading the Swift — they were
produced by *running* it. `Cast.character(for:)` was compiled verbatim
(`swiftc`) and asked for these twenty-four pairs, and its answers are what is asserted
here. That is the only way to catch the interesting failure: Swift's `&*`/`&+`
wrap at 64 bits and Python's integers do not, so a naive port agrees on short
inputs and diverges on every real session id — a v4 UUID overflows after nine of
its thirty-six bytes.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dark_army_daemon import cast
from dark_army_daemon.identity import NAMES


# (nickname, session_id, what Cast.character(for:) returns)
# Regenerated 22 Sep 2026 for the Dark Army callsigns (fourteen names swapped
# in place, so every hashed row kept its index under the new slug) by
# compiling the panel's actual `names` and `character(for:)` body with a
# minimal Agent fixture; Swift's answers were printed and pasted in.
# `test_actual_swift_clients_match_the_golden` repeats this execution for both
# clients; expectations below are Swift output, never Python-derived answers.
SWIFT_GOLDEN = [
    ('', '5189817d-8a2f-4c1e-9b33-77a1f0e2c4d5', 'hex'),
    ('', '7ccc421b-85a0-4d2f-8e11-2b3c4d5e6f70', 'zosia'),
    ('Cipher-ab12', '01a00a13-5ef0-4a11-9c22-3d4e5f607182', 'cipher'),
    ('Relay', 'whatever', 'relay'),
    ('', 'a', 'quiet'),
    ('', '', 'vex'),
    ('', 'banner-probe-10', 'forge'),
    ('RELAY', 'x', 'relay'),
    ('Grok', '5c28d7b9-f3a6-4f4e-955a-e010dd6e6431', 'vex'),
    ('Androll', 'any-session', 'androll'),
    ('Captcha', 'any-session', 'captcha'),
    ('Sawa', 'any-session', 'sawa'),
    ('Franio', 'any-session', 'franio'),
    ('Zosia', 'any-session', 'zosia'),
    ('SAWA', 'x', 'sawa'),
    ('franio', 'x', 'franio'),
    ('Sawa-9f3c', '3b1f0c2a-7d44-4e59-9a10-c6e8b2f5d731', 'sawa'),
    ('', '3b1f0c2a-7d44-4e59-9a10-c6e8b2f5d731', 'sawa'),
    ('', 'codex:7f2e4d1a-8b90-4c3d-a5e6-1f2a3b4c5d6e', 'audit'),
    ('nobody-at-all', 'a-very-long-session-identifier-0123456789-0123456789-0123456789', 'relay'),
    ('', 'a-very-long-session-identifier-0123456789-0123456789-0123456789', 'relay'),
    ('Ptyś', 'any-session', 'ptyś'),
    ('PTYŚ', 'any-session', 'ptyś'),
    ('Ptyś-ab12', 'any-session', 'ptyś'),
]


@pytest.mark.parametrize("nickname,session_id,expected", SWIFT_GOLDEN)
def test_matches_the_panels_choice(nickname, session_id, expected):
    assert cast.character_for(nickname, session_id) == expected


def test_every_cast_name_wears_its_own_face():
    for name in NAMES:
        assert cast.character_for(name, "any-session") == name.lower()


def test_a_hashed_face_is_stable_and_in_the_cast():
    slugs = {n.lower() for n in NAMES}
    for i in range(200):
        sid = f"session-{i}"
        first = cast.character_for("", sid)
        assert first in slugs
        assert cast.character_for("", sid) == first     # never moves


def test_states_follow_the_buckets():
    assert cast.state_for("waiting") == cast.STATE_ALERT
    assert cast.state_for("running") == cast.STATE_WORK
    assert cast.state_for("sleeping") == cast.STATE_SLEEP
    assert cast.state_for("finished") == cast.STATE_SLEEP
    assert cast.state_for("abandoned") == cast.STATE_SLEEP
    # An unknown bucket sleeps: it is the pose that claims the least.
    assert cast.state_for("something-new") == cast.STATE_SLEEP


def test_every_character_and_state_has_art():
    """Every character the manifest *declares* has all three poses drawn.

    Scoped to the manifest rather than to `identity.NAMES` on purpose: a name
    with no drawing yet is legal (the strip falls back to the aggregate glyph
    and the count), while a declared slug with a missing pose is a broken
    ingest. `test_identity.py` pins the other direction — that the manifest
    declares nothing outside `NAMES + ART_ONLY`.
    """
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "assets" / "cast"
    if not root.is_dir():                     # art is not shipped with the tests
        pytest.skip("assets/cast is not present")
    manifest = json.loads((root / "manifest.json").read_text())
    missing = [f"{slug}-{s}"
               for slug in manifest["cast"]
               for s in (cast.STATE_WORK, cast.STATE_SLEEP, cast.STATE_ALERT)
               if not any((root / f"{slug}-{s}").glob("*.png"))]
    assert not missing, f"cast art missing for: {missing}"


@pytest.mark.parametrize("relative_path", [
    "panel/Sources/BobPanel/Cast.swift", "ios/BobPhone/Cast.swift"])
def test_actual_swift_clients_match_the_golden(tmp_path, relative_path):
    """Execute each client's real name/hash body, including Unicode overflow.

    UIKit/AppKit drawing is outside this pure seam; Mac resource loading is
    covered by SpecialistGridTests through the built SwiftPM bundle.
    """
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.skip("Swift toolchain unavailable")
    root = Path(__file__).resolve().parents[2]
    source = (root / relative_path).read_text()
    names = re.search(r"    static let names = \[.*?\]", source, re.S)
    assert names, relative_path
    start = source.index("    static func character(for agent: Agent)")
    end = source.index("\n    }", start) + len("\n    }")
    harness = (
        "import Foundation\n"
        "struct Agent { let nickname: String; let sessionId: String }\n"
        "enum Cast {\n" + names.group() + "\n" + source[start:end] + "\n}\n"
        "let args = CommandLine.arguments\n"
        "print(Cast.character(for: Agent(nickname: args[1], sessionId: args[2])))\n")
    path = tmp_path / "CastProbe.swift"
    executable = tmp_path / "CastProbe"
    path.write_text(harness)
    built = subprocess.run([swiftc, str(path), "-o", str(executable)],
                           capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stderr
    for nickname, session_id, expected in SWIFT_GOLDEN:
        ran = subprocess.run([str(executable), nickname, session_id],
                             capture_output=True, text=True, timeout=10)
        assert ran.returncode == 0, ran.stderr
        assert ran.stdout.strip() == expected, (relative_path, nickname, session_id)
