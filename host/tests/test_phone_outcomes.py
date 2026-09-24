"""Phone outcome wire/read-door contract, alongside native model tests."""
from pathlib import Path
from dark_army_daemon.api_server import ApiServer
ROOT=Path(__file__).resolve().parents[2]
PHONE=ROOT/'ios'/'BobPhone'


def test_read_models_shared_and_tolerant():
    assert (PHONE/'OutcomeModels.swift').read_bytes()==(ROOT/'panel/Sources/BobPanel/OutcomeModels.swift').read_bytes()
    text=(PHONE/'OutcomeModels.swift').read_text()
    for key in ['intended_benefit','success_criterion','outcome_check_on','card_id','wait_seconds','wait_coverage','rework_rate','measurements_available','next_offset','observed_cost_per_outcome','awaiting_cost_totals','awaiting_cost_covered','cost_reason','observed_cost_note']:
        assert f'"{key}"' in text
    assert 'c.maybe(.waitSeconds)' in text


def test_phone_reads_sealed_home_and_away_without_new_actions():
    text=(PHONE/'Client.swift').read_text().split('func outcomeReport(',1)[1]
    assert text.count('kind: "outcomes"')==2
    assert 'knowsItIsAway' in text and 'home.request' in text and 'channel.request' in text
    assert 'record.token == self.record?.token' in text and '!backgroundRun' in text
    assert 'board_accept_outcome' not in (PHONE/'OutcomeScreens.swift').read_text()
    for actions in [ApiServer.LAN_ACTIONS,ApiServer.REMOTE_ACTIONS]:
        assert 'board_accept_outcome' not in actions and 'board_request_revision' not in actions


def test_card_and_project_readers_and_build_membership():
    assert 'PhoneOutcomeCard' in (PHONE/'CardDetailView.swift').read_text()
    assert 'PhoneOutcomeProjects' in (PHONE/'BoardView.swift').read_text()
    project=(ROOT/'ios/BobPhone.xcodeproj/project.pbxproj').read_text()
    for name in ['OutcomeModels.swift','OutcomeViews.swift','OutcomeScreens.swift']:
        assert project.count(name)>=4
    text=(PHONE/'OutcomeScreens.swift').read_text()
    assert 'requests.apply(fresh, request: request, currentCardId: card.id, currentRevision: currentRevision)' in text
    assert 'requests.begin(cardId: wanted, revision: currentRevision, offset: offset)' in text
    assert 'client.snapshot.board.cards.first' in text
    assert 'Date().timeIntervalSince(lastFetch) >= 30' in text


def test_evidence_plain_text_and_nil_not_zero():
    text=(PHONE/'OutcomeViews.swift').read_text()
    assert 'Text(event.evidence)' in text
    assert 'Link(' not in text and 'URLSession' not in text
    assert 'summary.cardHours ?? 0' not in text


def test_cost_views_shared_and_the_money_words_are_bobs_own():
    from dark_army_daemon import board_outcomes as metrics
    assert (PHONE/'OutcomeViews.swift').read_bytes()==(ROOT/'panel/Sources/BobPanel/OutcomeViews.swift').read_bytes()
    # Composed once in Python, drawn verbatim: neither tree may hold a copy.
    sentences=[metrics.OBSERVED_COST_NOTE,metrics.cost_reason(0,{}),metrics.cost_reason(1,{})]
    for tree in [ROOT/'panel'/'Sources',PHONE]:
        blob=''.join(p.read_text() for p in tree.rglob('*.swift'))
        for sentence in sentences:
            assert sentence and sentence not in blob
    text=(PHONE/'OutcomeViews.swift').read_text()
    assert text.count('Cost per accepted outcome: unavailable')==1
    assert 'summary.costReason' in text and 'summary.observedCostNote' in text
    assert 'summary.observedCost' in text and 'summary.awaitingCost' in text
