"""The phone's History screen, pinned by grep (`test_scout_reports_surface.py`'s
idiom): the Menu tile lights on the Mac's marker, the week is fetched by one
function from one screen and never by the poll, the background refresh or
the widget, and the screen keeps the phone's text, colour and money rules.
The fold itself is `test_ledger_week.py`'s.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
WIDGET = ROOT / "ios" / "BobPhoneWidget"
VIEW = PHONE / "HistoryWeekView.swift"
MODEL = PHONE / "HistoryWeek.swift"
MENU = PHONE / "MenuView.swift"
MODELS = PHONE / "Models.swift"
CLIENT = PHONE / "Client.swift"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _code(text: str) -> str:
    return re.sub(r"//[^\n]*", "", text)


def test_the_menu_has_a_history_tile_lit_by_the_marker():
    menu = _read(MENU)
    assert "case usage, history, comm, scouting, checks, plans, designSystem" in menu
    assert 'case .history: return "History"' in menu
    assert 'case .history: return "calendar"' in menu
    assert "case .history: return historyWeek" in menu
    assert "historyWeek: client.snapshot.board.historyWeekSupported" in menu
    assert "HistoryWeekView(client: client)" in menu
    assert 'MenuNotYet(path: "~/history"' in menu


def test_the_marker_decodes_false_by_default():
    models = _read(MODELS)
    assert "var historyWeekSupported = false" in models
    assert 'case historyWeekSupported = "history_week_supported"' in models
    assert "historyWeekSupported = c.value(.historyWeekSupported, false)" in models


def test_one_fetch_one_caller_never_the_poll():
    client = _read(CLIENT)
    assert len(re.findall(r"func historyWeek\(", client)) == 1
    region = client[client.index("func historyWeek("):]
    region = region[:region.index("\n    }\n")]
    assert "guard let record, !backgroundRun" in region
    assert 'kind: "history_week"' in region
    assert "knowsItIsAway" in region
    callers = []
    for path in list(PHONE.glob("*.swift")) + list(WIDGET.glob("*.swift")):
        if path == CLIENT:
            continue
        if "historyWeek()" in _code(path.read_text()):
            callers.append(path.name)
    assert callers == ["HistoryWeekView.swift"]
    for name in ("BackgroundRefresh.swift", "Outbox.swift"):
        assert "history_week" not in _read(PHONE / name), name
    # Not from the poll, the push or the background leg inside the client.
    for fn in ("func pollOnce(", "func tookPush(", "func backgroundRefresh("):
        if fn in client:
            body = client[client.index(fn):]
            body = body[:body.index("\n    }\n")]
            assert "historyWeek(" not in body, fn


def test_the_screen_keeps_the_phone_rules():
    view = _read(VIEW)
    assert '.navigationTitle("history")' in view
    assert ".lineLimit(" not in view
    assert "ProgressView" not in view
    assert ".dynamicTypeSize(" not in view
    assert "dynamicTypeSize.isAccessibilitySize" in view
    assert ".privacySensitive()" in view
    assert re.search(r"Text\(picture\.total\)[^\n]*\n(?:[^\n]*\n){0,4}[^\n]*\.privacySensitive\(\)",
                     view), "the total is not marked private"
    for word in ("Claude", "Grok", "Codex"):
        assert word in view or word in _read(MODEL), word
    assert "LedgerWeek.notPriced" in view
    assert re.search(r"Theme\.(amber|alarm|attention|danger)", view) is None
    assert 'AgentChatterView(.line, wait: .refreshing, seed: "history"' in view
    assert ".task { await load() }" in view
    assert ".refreshable { await load() }" in view
    assert "LedgerWeek.picture(" in view


def test_the_model_keeps_absent_money_absent():
    model = _read(MODEL)
    assert "tokenCostUsd = c.maybe(.tokenCostUsd)" in model
    assert "reportedCostUsd = c.maybe(.reportedCostUsd)" in model
    assert 'case tokenCostUsd = "token_cost_usd"' in model
    assert 'case reportedCostUsd = "reported_cost_usd"' in model
    assert "func asLedgerInput()" in model
    assert '"not priced"' in _read(PHONE / "LedgerWeek.swift")
