"""The phone's picture fits the frame the phone can open.

The phone refuses a sealed frame that inflates to `RELAY_FRAME_MAX_BYTES` or
more. A board that outgrew that used to be sealed anyway — the deflated wire
was small — and every check-in then failed on the phone without a word.
"""
import json
import os

from dark_army_daemon import api_server as api_mod
from dark_army_daemon import relay


def _card(i, column="done", size=4000):
    return {"id": f"c{i}", "column_name": column, "done_at": float(i),
            "title": f"card {i}", "prompt": '"quoted" ' * (size // 9)}


def _picture(done=0, prep=0):
    cards = [_card(i, "prep") for i in range(prep)]
    cards += [_card(i) for i in range(done)]
    return {"counts": {"done": done}, "agents": [],
            "board": {"cards": cards, "counts": {"done": done}}}


def _reply_plain(picture) -> int:
    """The inflated size of the reply frame that would carry `picture`."""
    return len(json.dumps({
        "v": 1, "ctr": 1, "ts": 0.0, "id": "0" * 16, "kind": "reply",
        "body": {"re": "0" * 16, "status": 200,
                 "content_type": "application/json",
                 "body": json.dumps(picture),
                 "timing": {"open": 0.001, "run": 0.01}},
    }, separators=(",", ":")).encode())


def test_a_small_board_is_left_alone():
    state = _picture(done=5, prep=5)
    out = api_mod.phone_picture(state)
    assert out["board"] is state["board"]
    assert api_mod.CARDS_WITHHELD_KEY not in out["board"]


def test_an_oversize_board_sheds_its_oldest_finished_cards():
    state = _picture(done=300, prep=20)
    assert _reply_plain(state) >= relay.RELAY_FRAME_MAX_BYTES
    out = api_mod.phone_picture(state)
    cards = out["board"]["cards"]
    withheld = out["board"][api_mod.CARDS_WITHHELD_KEY]
    assert withheld > 0 and len(cards) == 320 - withheld
    # every unfinished card stays; the finished ones kept are the newest
    assert sum(c["column_name"] == "prep" for c in cards) == 20
    kept = [c["done_at"] for c in cards if c["column_name"] == "done"]
    assert min(kept) == float(withheld) and max(kept) == 299.0
    # the answer the phone gets now opens, with room for the usage sibling
    assert _reply_plain(out) < relay.RELAY_FRAME_MAX_BYTES - 50_000
    # and the caller's own state is untouched
    assert len(state["board"]["cards"]) == 320
    assert api_mod.CARDS_WITHHELD_KEY not in state["board"]


def test_the_digest_fingerprints_the_slimmed_picture():
    state = _picture(done=300)
    slim = api_mod.phone_picture(state)
    assert (api_mod._state_digests(slim)
            == api_mod._state_digests(api_mod.phone_picture(state)))
    assert api_mod._state_digests(slim) != api_mod._state_digests(state)


def test_a_board_with_nothing_finished_to_shed_is_returned_whole():
    state = _picture(prep=300)
    out = api_mod.phone_picture(state)
    assert len(out["board"]["cards"]) == 300
    assert api_mod.CARDS_WITHHELD_KEY not in out["board"]


def test_a_frame_that_only_fits_deflated_is_not_sealed():
    key = os.urandom(32)
    body = {"body": "a" * relay.RELAY_FRAME_MAX_BYTES}   # deflates to nothing
    assert relay.seal_frame(key, relay.DIR_MAC_TO_PHONE, 1, "reply",
                            body, ns=relay.HOME) == ""
    assert relay.seal_frame(key, relay.DIR_MAC_TO_PHONE, 1, "reply",
                            {"body": "a" * 1000}, ns=relay.HOME) != ""
