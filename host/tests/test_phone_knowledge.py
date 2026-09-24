# host/tests/test_phone_knowledge.py
"""Phone knowledge screen: sealed kind, body root, no write names, pbxproj,
byte-equal models.
"""

from pathlib import Path

from dark_army_daemon.api_server import ApiServer

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
PANEL = ROOT / "panel" / "Sources" / "BobPanel"


def test_knowledge_models_are_byte_equal():
    assert (PHONE / "KnowledgeModels.swift").read_bytes() == (
        PANEL / "KnowledgeModels.swift").read_bytes()


def test_phone_reads_sealed_home_and_away_without_new_actions():
    text = (PHONE / "Client.swift").read_text().split(
        "func knowledgeReport(", 1)[1]
    assert text.count('kind: "knowledge"') == 2
    assert 'knowsItIsAway' in text and "home.request" in text
    assert '"root"' in text or "['root']" in text or '["root"]' in text
    assert "record.token == self.record?.token" in text
    assert "!backgroundRun" in text
    assert "query" not in text.split("func ", 1)[0]
    view = (PHONE / "KnowledgeView.swift").read_text()
    for name in ("knowledge_confirm", "knowledge_stale", "knowledge_edit"):
        assert name not in view
        assert name not in ApiServer.LAN_ACTIONS
        assert name not in ApiServer.REMOTE_ACTIONS
    assert set(ApiServer.REMOTE_ACTIONS) <= set(ApiServer.LAN_ACTIONS)


def test_root_rides_the_json_body_not_a_query_string():
    text = (PHONE / "Client.swift").read_text().split(
        "func knowledgeReport(", 1)[1]
    head = text.split("\n    func ", 1)[0]
    assert '["root": root]' in head or '["root": root ]' in head
    assert '"query"' not in head
    assert "addingPercentEncoding" not in head


def test_pbxproj_membership():
    project = (ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj").read_text()
    for name in ("KnowledgeModels.swift", "KnowledgeView.swift"):
        assert project.count(name) >= 4, name


def test_navigation_title_is_the_literal():
    text = (PHONE / "KnowledgeView.swift").read_text()
    assert '.navigationTitle("knowledge")' in text
    assert ".lineLimit(" not in text


def test_phone_load_uses_the_shared_apply_and_clears_on_change():
    """The phone has no easy unit-test seam; the Mac pins KnowledgeLoad.
    The view must call the same apply and drop the previous list before
    the await, or a slower reply for A lands under B."""
    text = (PHONE / "KnowledgeView.swift").read_text()
    assert "let requested = root" in text
    assert "report = KnowledgeReport()" in text
    assert "KnowledgeLoad.apply(" in text
    assert "requested: requested" in text
    assert "current: root" in text
