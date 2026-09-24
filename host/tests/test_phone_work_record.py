# host/tests/test_phone_work_record.py
"""The phone's WHAT CHANGED section — source pins over the Swift.

`ios/` has no test target by decision; this suite reads the Swift as text,
`test_phone_event_log.py`'s pattern. What it pins: the four models decode
every key the daemon publishes (`work_record.RECORD_KEYS` and `FILE_KEYS` are
imported, so the two cannot drift apart silently); the client asks for the
sealed `work_record` kind on both legs and defines the fetcher exactly once;
the card screen draws the section; the widget, the background check-in and the
push code never touch it; the new file is in all four project slots; and the
view composes no sentence of its own — the caption and every verdict sentence
appear verbatim, on both clients.
"""

from __future__ import annotations

import re
from pathlib import Path

from dark_army_daemon import work_record

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
CLIENT = PHONE / "Client.swift"
CARD = PHONE / "CardDetailView.swift"
VIEW = PHONE / "WorkRecordView.swift"
REFRESH = PHONE / "BackgroundRefresh.swift"
PUSH = PHONE / "Push.swift"
WIDGET_DIR = ROOT / "ios" / "BobPhoneWidget"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
PANEL_SHEET = ROOT / "panel" / "Sources" / "BobPanel" / "BoardCardSheet.swift"
PANEL_MODELS = ROOT / "panel" / "Sources" / "BobPanel" / "BoardModels.swift"


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


def test_the_record_names_every_key_the_daemon_publishes():
    keys = _block(_block(_read(MODELS), "struct WorkRecord:"),
                  "enum CodingKeys")
    for key in work_record.RECORD_KEYS:
        # `root` and `baseline` are deliberately not drawn: the phone never
        # supplies a path, so neither reaches a screen. Every other stored
        # field has a counterpart here.
        if key in ("root", "baseline"):
            continue
        if "_" in key:
            assert f'= "{key}"' in keys, key
        else:
            assert re.search(rf"\b{key}\b", keys), key


def test_a_file_row_names_every_key_and_keeps_the_counts_optional():
    block = _block(_read(MODELS), "struct WorkRecordFile")
    for key in work_record.FILE_KEYS:
        assert re.search(rf"\b{key}\b", block), key
    # Absent, never zero: git says `-` for a binary file.
    assert "var added: Int?" in block
    assert "var removed: Int?" in block


def test_absence_decodes_to_nil_on_both_clients():
    """A card nobody has run and a run that changed nothing are different
    facts; a defaulted value here would make them the same one."""
    for path in (MODELS, PANEL_MODELS):
        text = _read(path)
        assert "var workRecord: WorkRecordHead?" in text, path
        assert "workRecord = c.maybe(.workRecord)" in text, path


def test_the_client_asks_for_the_sealed_kind_on_both_legs():
    client = _read(CLIENT)
    assert client.count("func workRecord(cardId:") == 1
    assert client.count("func workRecordDiff(cardId:") == 1
    for name in ("func workRecord(cardId:", "func workRecordDiff(cardId:"):
        body = _block(client, name)
        assert 'channel.request(kind: "work_record"' in body
        assert 'home.request(kind: "work_record"' in body
        assert "knowsItIsAway" in body
        assert "!backgroundRun" in body
        assert "record.token" in body


def test_a_404_means_the_mac_is_too_old_not_an_empty_record():
    body = _block(_read(CLIENT), "func workRecord(cardId:")
    assert "answer.status == 404" in body
    assert "return .unsupported" in body
    view = _read(VIEW)
    assert "too old to keep a record" in view


def test_the_read_never_spends_a_lease_or_grows_an_action_tuple():
    from dark_army_daemon.api_server import ApiServer
    assert "work_record" not in ApiServer.LAN_ACTIONS
    assert "work_record" not in ApiServer.REMOTE_ACTIONS


def test_the_card_screen_draws_the_section():
    assert "PhoneWorkRecordSection(client: client, card: card)" in _read(CARD)


def test_the_background_run_and_the_widget_never_fetch_it():
    for path in [REFRESH, PUSH, *sorted(WIDGET_DIR.glob("*.swift"))]:
        text = _read(path)
        assert "workRecord" not in text, path
        assert "work-record" not in text, path


def test_the_file_is_in_all_four_project_slots():
    pbx = _read(PBXPROJ)
    lines = [line for line in pbx.splitlines()
             if "WorkRecordView.swift" in line]
    assert len(lines) == 4, lines
    assert sum("PBXBuildFile" in line for line in lines) == 1
    assert sum("PBXFileReference" in line for line in lines) == 1


def test_neither_client_composes_a_sentence_of_its_own():
    """The caption and every verdict sentence are the daemon's words, drawn
    verbatim on both surfaces — the wording is one decision, made once."""
    for path in (VIEW, PANEL_SHEET):
        text = _read(path)
        assert work_record.CAPTION in text, path
    phone_words = _read(MODELS)
    panel_words = _read(PANEL_SHEET)
    for sentence in work_record.VERDICT_WORDS.values():
        assert sentence in phone_words, sentence
        assert sentence in panel_words, sentence


def test_the_view_draws_a_missing_file_list_as_words_not_a_zero():
    models = _read(MODELS)
    body = _block(models, "static func summary(")
    assert "record.filesReason" in body
    assert "filesAvailable" in body


SHUNT_KEYS = ("shunt_delegations", "shunt_lines_kept_out", "shunt_worker_cost_usd")


def test_both_clients_name_the_three_shunt_keys_on_head_and_record():
    """`RECORD_KEYS` carries the trio, and both Swift twins decode it on the
    headline and the record, tolerantly: a missing key is an older daemon."""
    for key in SHUNT_KEYS:
        assert key in work_record.RECORD_KEYS, key
    for path in (MODELS, PANEL_MODELS):
        text = _read(path)
        head = _block(_block(text, "struct WorkRecordHead"), "enum CodingKeys")
        record = _block(_block(text, "struct WorkRecord:"), "enum CodingKeys")
        for key in SHUNT_KEYS:
            assert f'= "{key}"' in head, (path, key)
            assert f'= "{key}"' in record, (path, key)
        assert '= "shunt_words"' in record, path
        # Unknown is nil, never zero: the cost is an optional on both.
        assert text.count("var shuntWorkerCostUsd: Double?") == 2, path
        assert "shuntWorkerCostUsd = c.maybe(.shuntWorkerCostUsd)" in text, path


def test_both_views_draw_the_daemons_sentence_and_compose_none():
    for path in (VIEW, PANEL_SHEET):
        text = _read(path)
        assert "record.shuntWords" in text, path
        assert "record.shuntDelegations > 0" in text, path
        # No client-side wording of the helper's work: the words come from
        # `work_record.shunt_words` alone.
        assert "out of the main model" not in text, path
        assert "helper cost" not in text, path
