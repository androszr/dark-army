# host/tests/test_phone_event_log.py
"""The phone's Recently section — source pins over the Swift.

`ios/` has no test target by decision; this suite reads the Swift as text.
What it pins: `LogEntry` decodes every key the daemon publishes
(`event_log.PUBLISHED_KEYS` is imported, so the two cannot drift apart
silently); the client asks for the diary as the sealed `log` kind on both
legs and defines `fetchLog` exactly once; the Fleet tab calls it; the section
remembers its fold; the widget, the background check-in and the push code
never touch it; the file is in all four project slots; and the view composes
no sentence of its own.
"""

from __future__ import annotations

import re
from pathlib import Path

from dark_army_daemon import event_log

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
CLIENT = PHONE / "Client.swift"
FLEET = PHONE / "FleetView.swift"
RECENTLY = PHONE / "RecentlyView.swift"
REFRESH = PHONE / "BackgroundRefresh.swift"
PUSH = PHONE / "Push.swift"
WIDGET_DIR = ROOT / "ios" / "BobPhoneWidget"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"


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


def test_log_entry_names_every_published_key():
    keys_block = _block(_block(_read(MODELS), "struct LogEntry"), "enum CodingKeys")
    for key in event_log.PUBLISHED_KEYS:
        if "_" in key:
            assert f'= "{key}"' in keys_block, key
        else:
            assert re.search(rf"\b{key}\b", keys_block), key
    assert _read(MODELS).count("struct LogEntry") == 1


def test_log_entry_detail_is_stringified_scalars():
    models = _read(MODELS)
    assert "struct LogScalar" in models
    assert "[String: LogScalar]" in _block(models, "struct LogEntry")
    assert "struct LogPage" in models


def test_the_client_asks_for_the_log_kind_on_both_legs():
    client = _read(CLIENT)
    assert client.count("func fetchLog(") == 1
    body = _block(client, "func fetchLog(")
    assert 'channel.request(kind: "log"' in body
    assert 'kind: "log", body: body, host:' in body
    assert "knowsItIsAway" in body
    assert "since=" in body
    assert "forgetPairing()" in body           # 403 un-pairs, as pollUsage does


def test_the_log_is_fetched_behind_both_polls_and_on_the_fleet_tab():
    client = _read(CLIENT)
    assert "await fetchLog(host: host)" in _block(client, "private func pollDirect(")
    assert "await fetchLog()" in _block(client, "private func pollViaRelay(")
    assert "client.fetchLog(force: true)" in _read(FLEET)
    assert "RecentlySection(client: client)" in _read(FLEET)


def test_the_background_run_never_fetches_it():
    client = _read(CLIENT)
    assert "backgroundRun = true" in _block(client, "func backgroundRefresh(")
    assert "!backgroundRun" in _block(client, "func fetchLog(")
    for path in [REFRESH, PUSH, *sorted(WIDGET_DIR.glob("*.swift"))]:
        text = _read(path)
        assert "fetchLog" not in text, path
        assert "/api/log" not in text, path


def test_un_pairing_clears_the_log():
    assert "log = []" in _block(_read(CLIENT), "private func forgetPairing()")


def test_the_section_remembers_its_fold():
    view = _read(RECENTLY)
    assert '@AppStorage("fleet.recently.collapsed")' in view
    assert "RECENTLY (" in view


def test_the_view_composes_no_sentence():
    view = _read(RECENTLY)
    assert "entry.text" in view
    assert '" started' not in view
    assert '" finished' not in view
    assert "Trouble." not in view


def test_the_view_routes_to_agents_and_cards():
    view = _read(RECENTLY)
    assert "sheets.show(.agent(agent, category))" in view
    assert "sheets.show(.card(card))" in view


def test_the_file_is_in_all_four_project_slots():
    pbx = _read(PBXPROJ)
    lines = [l for l in pbx.splitlines() if "RecentlyView.swift" in l]
    assert len(lines) == 4, lines
    assert sum("PBXBuildFile" in l for l in lines) == 1
    assert sum("PBXFileReference" in l for l in lines) == 1
