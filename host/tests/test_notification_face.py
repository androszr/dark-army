# host/tests/test_notification_face.py
"""The extension's portrait lookup, run under swiftc.

`NotificationFace` is Foundation only. A slug on the roster resolves to one
file inside the portraits directory; anything else, including a path that
would climb out of it, resolves to nothing. The seam is
`test_phone_background_grace.py`'s: a tiny main beside the source.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unicodedata

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FACE = ROOT / "ios" / "BobPhoneNotification" / "NotificationFace.swift"
SERVICE = ROOT / "ios" / "BobPhoneNotification" / "NotificationService.swift"

HARNESS = r'''
import Foundation

struct Case: Decodable {
    var slug: String
}

let data = FileHandle.standardInput.readDataToEndOfFile()
let cases = try! JSONDecoder().decode([Case].self, from: data)
let dir = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
for c in cases {
    if let url = NotificationFace.portraitURL(portraitsDirectory: dir, slug: c.slug) {
        let parent = url.deletingLastPathComponent().standardizedFileURL.path
        let root = dir.standardizedFileURL.path
        let inside = parent == root ? "inside" : "escaped"
        print("HIT \(url.lastPathComponent) \(inside)")
    } else {
        print("MISS")
    }
}
'''


@pytest.fixture(scope="module")
def face_bin(tmp_path_factory):
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    folder = tmp_path_factory.mktemp("notification-face")
    main = folder / "main.swift"
    main.write_text(HARNESS)
    binary = folder / "notification-face"
    built = subprocess.run(
        [swiftc, str(FACE), str(main), "-o", str(binary)],
        capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    return binary


def _rows(binary: pathlib.Path, directory: pathlib.Path, slugs: list[str]) -> list[str]:
    proc = subprocess.run(
        [str(binary), str(directory)],
        input=json.dumps([{"slug": slug} for slug in slugs]),
        capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    # `URL.lastPathComponent` prints ś decomposed; the slug on the wire is composed.
    return [unicodedata.normalize("NFC", line) for line in proc.stdout.splitlines() if line]


def test_ptys_resolves_inside_the_portraits_directory_and_a_climb_does_not(face_bin, tmp_path):
    """`ptyś` is a file in the folder. `../ptyś`, `mrrobot` and an empty
    slug are not, and the hit's parent is the portraits directory."""
    portraits = tmp_path / "portraits"
    portraits.mkdir()
    (portraits / "ptyś.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (portraits / "vex.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    # A portrait sitting *beside* the folder must not be reachable by climbing.
    (tmp_path / "ptyś.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    rows = _rows(face_bin, portraits, [
        "ptyś", "vex", "../ptyś", "..", ".", "", "mrrobot", "dom",
        "ptyś/../vex", "cipher",
    ])
    assert rows == [
        "HIT ptyś.png inside",
        "HIT vex.png inside",
        "MISS", "MISS", "MISS", "MISS", "MISS", "MISS", "MISS",
        "MISS",  # cipher is on the roster, and the file is not there
    ]


def test_the_extension_imports_neither_swiftui_nor_cast():
    face = FACE.read_text()
    service = SERVICE.read_text()
    assert face.count("import ") == 1 and "import Foundation" in face
    for stranger in ("SwiftUI", "Cast", "Keychain", "App Group", "donate("):
        assert stranger not in face, stranger
        assert stranger not in service, stranger
    assert "UNNotificationServiceExtension" in service
    assert "updating(from:" in service
    assert "serviceExtensionTimeWillExpire" in service


SENDER_HARNESS = r'''
import Foundation

let a = Data([0x89, 0x50, 0x4e, 0x47, 1, 2, 3])
let b = Data([0x89, 0x50, 0x4e, 0x47, 1, 2, 4])
print(NotificationFace.senderIdentifier(slug: "captcha", portrait: a))
print(NotificationFace.senderIdentifier(slug: "captcha", portrait: a))
print(NotificationFace.senderIdentifier(slug: "captcha", portrait: b))
print(NotificationFace.senderIdentifier(slug: "sawa", portrait: a))
'''


def test_new_art_under_a_kept_slug_is_a_new_sender(tmp_path):
    """iOS keeps the first picture it saw for a sender handle. A slug that
    survived the recast (captcha, sawa, …) with new art must therefore
    reach iOS as a different sender: the identity carries a fingerprint of
    the portrait's bytes, stable for the same art, new for new art."""
    swiftc = shutil.which("swiftc")
    if not swiftc:
        pytest.fail("swiftc is required on this macOS repository; it was not found")
    main = tmp_path / "main.swift"
    main.write_text(SENDER_HARNESS)
    binary = tmp_path / "sender"
    built = subprocess.run([swiftc, str(FACE), str(main), "-o", str(binary)],
                           capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, built.stderr
    same, again, changed, other = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=30,
        check=True).stdout.split()
    assert same == again
    assert same != changed
    assert same.startswith("captcha.") and changed.startswith("captcha.")
    assert other.startswith("sawa.")


def test_the_banner_names_its_sender_by_the_fingerprinted_identity():
    """The handle, the custom identifier and the conversation all use the
    fingerprinted identity; the bare slug keys none of them, so iOS never
    reuses a picture cached under it."""
    service = SERVICE.read_text()
    assert "NotificationFace.senderIdentifier(slug: slug, portrait: data)" in service
    assert "INPersonHandle(value: sender," in service
    assert "customIdentifier: sender," in service
    assert "conversationIdentifier: sender," in service
    assert "INPersonHandle(value: slug" not in service
    assert "conversationIdentifier: slug" not in service
