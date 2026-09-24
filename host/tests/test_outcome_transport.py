"""Exercise the actual paired home entry and away response sealing."""
import json

import pytest

from dark_army_daemon import devices, relay
from dark_army_daemon.board import BoardStore
from tests.test_home_seal import (server, ledger, machine, attach_dir, _no_fleet_snapshot,
                                 pair_plain, inner, fetch)


@pytest.mark.asyncio
async def test_outcome_actual_home_away_and_loopback_pagination(server, tmp_path, monkeypatch):
    api, daemon, port = server
    store = BoardStore(tmp_path / 'outcomes.db')
    store.connect()
    daemon._board = store
    try:
        card, error = store.create(dict(title='Outcome', root='/project', tool='claude',
                                       beneficiary='人' * 200, intended_benefit='益' * 1000,
                                       success_criterion='成' * 1000))
        assert error == 'created'
        for n in range(30):
            result = store.accept_outcome(card['id'], n, str(n), '証' * 4000)
            assert result[0] is not None, result
        phone = await pair_plain(port)
        did = devices.device_for_home_channel(relay.channel_id(phone['key'], ns=relay.HOME))
        monkeypatch.setattr(relay, 'lease_valid', lambda _: False)
        offset, counter, ids = 0, 1, []
        while True:
            query = 'card=' + card['id'] + '&offset=' + str(offset)
            status, body = await inner(port, phone['key'], 'outcomes', {'query': query}, counter)
            assert status == 200
            home = json.loads(body)
            assert home['available'] and home['card']['accepted']
            assert len(body.encode()) <= 300_000
            status, ctype, away = await api._remote_run('outcomes', {'query': query}, did)
            assert status == 200 and json.loads(away) == home
            wire = relay.seal_frame(phone['key'], relay.DIR_MAC_TO_PHONE, counter, 'reply',
                                    {'status': status, 'content_type': ctype, 'body': away.decode()})
            frame, refusal = relay.open_frame(phone['key'], relay.DIR_MAC_TO_PHONE, wire, counter - 1)
            assert not refusal and json.loads(frame['body']['body']) == home
            status, local = await fetch('/api/outcomes?' + query, port=api._port)
            assert status == 200 and json.loads(local) == home
            ids.extend(event['id'] for event in home['events'])
            next_offset = home['next_offset']
            if next_offset is None:
                break
            assert next_offset > offset
            offset, counter = next_offset, counter + 1
        expected = store.outcome_card_report(card['id'])['events']
        assert ids == [event['id'] for event in expected]
        assert counter > 1
        status, _ = await inner(port, phone['key'], 'action',
                                {'action': 'board_accept_outcome', 'card_id': card['id']}, counter + 1)
        assert status == 404
        status, _ = await inner(port, phone['key'], 'action',
                                {'action': 'board_update', 'card_id': card['id'],
                                 'success_criterion': 'forged'}, counter + 2)
        assert status == 403
    finally:
        store.close()
