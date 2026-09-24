# host/tests/test_phone_section_delta.py
"""The phone's half of the section-partitioned `state` read — source pins.

The Mac leaves out a section the phone's quoted digest still matches and
names it in `sections_unchanged`; the phone keeps that section from the
last answer as it came off the wire (`heldWire`, never the on-screen board,
which has the Done archive spliced in). What this pins, in order of how much it would cost to get
wrong:

* the carry runs on the freshly decoded answer **before** `snapshot =
  decoded`, and a digest that does not match applies nothing and drops every
  held digest — the empty-snapshot trap, per section;
* the held digests are stored after the assignment, the whole-picture floor
  and the held picture move only on a whole answer;
* the per-section quotes ride only beside `digest`, are memory-only and die
  with the pairing;
* the phone's `Snapshot.Section` names are the daemon's `_OMITTABLE_SECTIONS`
  (imported, so the two ends cannot drift) and cover every section the phone
  decodes, and `carry` switches over each case once;
* the relay socket lane's push quotes no `sections`: a push is a whole
  picture.

`ios/BobPhoneTests/DecodeToleranceTests.swift` pins `StateAnswer` and
`carry` in code; this file pins what only the text can show.
"""

from __future__ import annotations

import re
from pathlib import Path

from dark_army_daemon.api_server import _OMITTABLE_SECTIONS

ROOT = Path(__file__).resolve().parents[2]
PHONE = ROOT / "ios" / "BobPhone"
MODELS = PHONE / "Models.swift"
CLIENT = PHONE / "Client.swift"
RELAY_WS = ROOT / "host" / "dark_army_daemon" / "relay_ws.py"


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


def _code(text: str) -> str:
    """`text` with `//` comments removed, so a pin reads code, not prose."""
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _section_cases() -> list[str]:
    enum = _block(_read(MODELS), "enum Section: String, CaseIterable")
    names: list[str] = []
    for line in _code(enum).splitlines():
        line = line.strip()
        if line.startswith("case "):
            names += [n.strip() for n in line[len("case "):].split(",")]
    return names


# --- the answer ---------------------------------------------------------------


def test_the_state_answer_decodes_both_delta_keys_tolerantly():
    answer = _block(_read(MODELS), "struct StateAnswer")
    assert "var sectionsUnchanged: [String] = []" in answer
    assert "var sectionDigests: [String: String] = [:]" in answer
    assert 'case sectionsUnchanged = "sections_unchanged"' in answer
    assert 'case sectionDigests = "section_digests"' in answer
    assert "sectionsUnchanged = c.value(.sectionsUnchanged, [])" in answer
    assert "sectionDigests = c.value(.sectionDigests, [:])" in answer


# --- the carry ----------------------------------------------------------------


def test_the_carry_runs_on_the_fresh_answer_before_the_assignment():
    apply = _code(_block(_read(CLIENT), "private func applyState("))
    carry_loop = apply.index("for name in answer.sectionsUnchanged")
    assert apply.index("Snapshot.self") < carry_loop
    assert carry_loop < apply.index("snapshot = decoded")
    assert apply.index("decoded.carry(name, from: heldWire)") \
        < apply.index("snapshot = decoded")
    assert apply.count("snapshot = decoded") == 1
    # One decode of the answer, reused: the marker and the list read the
    # same `answer`.
    assert apply.count("StateAnswer.self") == 1


def test_a_mismatched_section_digest_applies_nothing_and_drops_every_digest():
    apply = _code(_block(_read(CLIENT), "private func applyState("))
    loop = _block(apply, "for name in answer.sectionsUnchanged")
    guard = loop[loop.index("guard "):loop.index("decoded.carry(")]
    assert "heldSectionDigests[name]" in guard
    assert "answer.sectionDigests[name] == mine" in guard
    assert "heldWire.generatedAt != 0" in guard
    otherwise = _block(guard, "else {")
    assert 'heldStateDigest = ""' in otherwise
    assert "heldSectionDigests = [:]" in otherwise
    assert "heldWire = Snapshot()" in otherwise
    assert "lastFullState = nil" in otherwise
    assert "return .unchanged" in otherwise


def test_a_kept_section_comes_off_the_wire_copy_never_the_screen():
    """The on-screen board has the held Done archive spliced in, and
    `spliceDone` only adds: carrying from `snapshot` would keep a Done card
    the Mac has since cleared until the next whole picture. The carry reads
    the last answer as it came off the wire, taken before the splice."""
    apply = _code(_block(_read(CLIENT), "private func applyState("))
    carries = re.findall(r"carry\([^)]*\)", apply)
    assert carries, "the carry loop is gone"
    for call in carries:
        assert "from: snapshot" not in call, call
        assert "from: heldWire" in call, call
    assert apply.count("heldWire = decoded") == 1
    loop_end = apply.index("for name in answer.sectionsUnchanged") \
        + len(_block(apply, "for name in answer.sectionsUnchanged"))
    wire = apply.index("heldWire = decoded")
    assert loop_end <= wire
    assert wire < apply.index("mergeDoneArchive(into: &freshBoard)")
    assert wire < apply.index("snapshot = decoded")


def test_the_wire_copy_is_memory_only_and_cleared_beside_the_digests():
    text = _read(CLIENT)
    assert "private var heldWire = Snapshot()" in text
    for line in text.splitlines():
        if "heldWire" in line:
            assert "UserDefaults" not in line
            assert "Keychain" not in line
    for block in (_block(text, "private func forgetPairing()"),
                  _block(text, "func stop()")):
        code = _code(block)
        assert "heldSectionDigests = [:]" in code
        assert "heldWire = Snapshot()" in code


def test_the_digests_are_stored_after_the_assignment():
    apply = _code(_block(_read(CLIENT), "private func applyState("))
    assert apply.count("heldSectionDigests = answer.sectionDigests") == 1
    assert apply.index("snapshot = decoded") \
        < apply.index("heldSectionDigests = answer.sectionDigests")


def test_the_floor_and_the_held_picture_move_only_on_a_whole_answer():
    apply = _code(_block(_read(CLIENT), "private func applyState("))
    assert apply.count("lastFullState = Date()") == 1
    floor = _block(apply, "if answer.sectionsUnchanged.isEmpty {")
    assert "lastFullState = Date()" in floor
    guarded = _block(apply, "if !backgroundRun {")
    remember = _block(guarded, "if answer.sectionsUnchanged.isEmpty {")
    assert "heldPicture.remember(" in remember


# --- the quote ----------------------------------------------------------------


def test_the_sections_ride_only_beside_the_digest():
    """`sections` is quoted only in the return that quotes `digest`, and
    only when the phone holds a section digest at all."""
    body = _code(_block(_read(CLIENT), "private func stateRequestBody()"))
    assert body.count('"sections"') == 1
    quoting = body[body.rindex("return "):]
    assert '"digest"' in quoting
    assert '"sections": heldSectionDigests' in quoting
    assert "heldSectionDigests.isEmpty ? [:]" in quoting
    # Never on the floor's whole-picture answer.
    floor = body[body.index("guard "):body.index("}")]
    assert '"sections"' not in floor
    assert '"digest"' not in floor


def test_the_held_section_digests_are_memory_only_and_die_with_the_pairing():
    text = _read(CLIENT)
    assert "private var heldSectionDigests: [String: String] = [:]" in text
    for line in text.splitlines():
        if "heldSectionDigests" in line:
            assert "UserDefaults" not in line
            assert "Keychain" not in line
    assert "heldSectionDigests = [:]" in _block(
        text, "private func forgetPairing()")
    assert "heldSectionDigests = [:]" in _block(text, "func stop()")


# --- the two ends agree -------------------------------------------------------


def test_the_phone_sections_are_the_daemons():
    cases = _section_cases()
    assert cases, "no Section cases found"
    assert len(cases) == len(set(cases))
    assert set(cases) <= set(_OMITTABLE_SECTIONS)


def test_every_section_the_phone_decodes_has_a_case():
    """A section the phone decodes but cannot carry would be omitted by the
    Mac and blanked on the phone."""
    snapshot = _block(_read(MODELS), "struct Snapshot: Decodable")
    keys = _block(snapshot, "enum CodingKeys: String, CodingKey")
    wire: set[str] = set()
    for line in _code(keys).splitlines():
        line = line.strip()
        if not line.startswith("case "):
            continue
        rest = line[len("case "):]
        if "=" in rest:
            wire.add(re.search(r'"([^"]+)"', rest).group(1))
        else:
            wire.update(n.strip() for n in rest.split(","))
    decoded_sections = wire - {"generated_at", "fleet_figures", "state_digest"}
    assert decoded_sections == set(_section_cases())


def test_carry_switches_over_every_case_once():
    snapshot = _block(_read(MODELS), "struct Snapshot: Decodable")
    carry = _code(_block(snapshot, "mutating func carry("))
    arms = [line.strip() for line in carry.splitlines()
            if line.strip().startswith("case .")]
    named = [re.match(r"case \.(\w+):", arm).group(1) for arm in arms]
    assert sorted(named) == sorted(_section_cases())
    assert "default" not in carry


def test_the_socket_lane_push_quotes_no_sections():
    """The relay socket push is a whole picture: `_push_all` quotes the whole
    digest it handed down last and no per-section ones. A change to push
    deltas has to say what it does about a stale server-side memory."""
    text = _read(RELAY_WS)
    start = text.index("async def _push_all(")
    end = text.index("\n    def ", start + 1)
    push = text[start:end]
    assert '"digest"' in push
    assert '"sections"' not in push
