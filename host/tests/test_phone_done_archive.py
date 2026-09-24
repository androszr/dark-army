"""The phone's half of "finished cards are served on demand".

`tests.yml`'s path-gated `phone` job compiles the app and runs both test
bundles, which proves compilation and unit behaviour — never cross-surface
agreement, and never on a push that touches no `ios/**` file. So the phone's
*contracts* stay pinned by greps under `host/tests/`, the suite that runs on
every push and on this machine (`test_phone_theme_drift.py`'s decision).

What must hold, and why each line is here:

- The phone **asks** for the lighter board, unconditionally, on every state
  body. Asking only sometimes would leave it quoting the digest of a picture
  it never received.
- The splice runs **before** `snapshot` is assigned, on **both** exits of
  `applyState`. `Receipts.landed`, `Outbox.alreadyLanded` and
  `CardCacheStore.remember(board:)` all resolve against `snapshot.board.cards`,
  and a card a person drags to Done writes no `closed_by` — so it is withheld
  the instant it lands, and its receipt would never confirm.
- The Done page never says "nothing here" while the column is merely
  incomplete: it says it is loading, or that the fetch failed and offers to
  ask again.
- No prose is clipped (`docs/phone-contract.md`).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"


def _read(name: str) -> str:
    return (PHONE / name).read_text(encoding="utf-8")


def test_the_state_body_always_asks_for_the_lighter_board():
    text = _read("Client.swift")
    body = text.split("private func stateRequestBody()", 1)[1].split("\n    }", 1)[0]
    # Both returns — the one that quotes a digest and the one that does not.
    returns = [line for line in body.splitlines()
               if line.strip().startswith("return")]
    assert len(returns) == 2, returns
    assert all('"done": "review"' in line for line in returns), returns


def test_the_client_speaks_the_sealed_done_kind():
    text = _read("Client.swift")
    assert 'kind: "done"' in text
    assert text.count('kind: "done"') == 2, "one leg per door"
    assert "func loadDoneArchive()" in text
    assert "func retryDoneArchive()" in text


def test_the_splice_runs_before_the_snapshot_is_assigned():
    """On both exits: the unchanged branch keeps a board that is already on
    screen, and the fresh branch installs a new one."""
    text = _read("Client.swift")
    apply_body = text.split("private func applyState(", 1)[1]
    apply_body = apply_body.split("\n    /// The home walk", 1)[0]
    assert apply_body.count("mergeDoneArchive(into:") == 2, apply_body

    kept = apply_body.split("mergeDoneArchive(into: &keptBoard)", 1)[1]
    assert kept.index("snapshot.board = keptBoard") < kept.index(
        "receipts.settled(against: snapshot)")

    fresh = apply_body.split("mergeDoneArchive(into: &freshBoard)", 1)[1]
    assert fresh.index("decoded.board = freshBoard") < fresh.index(
        "snapshot = decoded")


def test_the_two_tokens_are_kept_apart():
    """Passing the view token where the membership token belongs would refuse
    every Clear Done after any finished card was edited."""
    models = _read("Models.swift")
    assert 'case doneViewToken = "done_view_token"' in models
    assert 'case doneClearToken = "done_clear_token"' in models
    assert 'case donePreview = "done_preview"' in models
    client = _read("Client.swift")
    # The clear gate reads the membership token and only that one.
    assert "doneViewToken" not in _read("BoardView.swift")
    assert "doneArchiveClearStamp" in client and "doneArchiveViewStamp" in client


def test_the_decide_rule_has_all_three_answers():
    text = _read("Client.swift")
    for rung in ("case keep", "case dropAndRefresh"):
        assert rung in text.replace("case keep, refresh, dropAndRefresh",
                                    "case keep\ncase refresh\ncase dropAndRefresh")
    assert "static func decideDone(" in text
    assert "static func spliceDone(" in text
    assert "doneArchiveCoalesce" in text


def test_the_done_page_says_loading_and_failed():
    text = _read("BoardView.swift")
    assert "Reading the \\(count) finished items…" in text
    assert "Could not read the finished items." in text
    assert '"Retry"' in text
    assert "retryDoneArchive()" in text
    # And the honest ready-and-empty sentence survives.
    assert "but none are on this page." in text


def test_the_empty_branch_no_longer_falls_through_to_nothing_here():
    """A finished column that is merely incomplete must never draw the generic
    empty line."""
    text = _read("BoardView.swift")
    # Indentation-insensitive: the branch moved from a page's `ScrollView`
    # into `rowBody(for:)` when the board became rows.
    branch = re.split(r"if cards\.isEmpty \{", text, 1)[1]
    branch = re.split(r"\} else \{\s*cardsStack", branch, 1)[0]
    assert 'doneEmptyLine(count:' in branch
    assert 'nothing here' in branch  # still the answer for every other column


def test_no_prose_was_clipped():
    """`docs/phone-contract.md`: the phone does not clip prose. Anti-vacuous —
    the needle is proven to match on a doctored copy."""
    added = _read("BoardView.swift").split("private func doneEmptyLine", 1)[1]
    added = added.split("\n    /// Group under project headings", 1)[0]
    assert ".lineLimit(" not in added
    assert re.search(r"\.fixedSize\(horizontal: false, vertical: true\)", added)
    assert ".lineLimit(" in added.replace("Text(", ".lineLimit(1)\nText(", 1)
