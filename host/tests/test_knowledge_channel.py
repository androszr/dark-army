# host/tests/test_knowledge_channel.py
"""The two knowledge verbs: the tool surface, the scoping, and the daemon
handlers.

The scoping is an *omission* — no `project`, no `root`, no `session_id` — so
it is asserted against the schemas' property names rather than by grepping the
file, and driven through the handlers rather than reasoned about.
"""

import io
import os

import pytest

from dark_army_daemon import channel_server as cs
from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import enrollment
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


def _daemon():
    return BobDaemon(headless=True)


def _board_daemon(tmp_path):
    daemon = _daemon()
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    return daemon, store


def _attach(daemon, port=51000, pid=4242, cwd="/tmp",
            host=cs.HOST_CLAUDE, session_id=""):
    return daemon._handle_channel_message(
        {"type": "channel_attach", "port": port, "pid": pid, "cwd": cwd,
         "session_id": session_id, "is_channel": True, "secret": "",
         "host": host})


def _placed(daemon, root, session_id="s1", port=51000, cwd=None, pid=4242):
    """Wire a session Dark Army can place inside `root`.

    `cwd` defaults to `root`; passing a subdirectory is the case the
    enrolled-root fix exists for, and `_known_project_roots` is stubbed to
    contain whatever the session's cwd actually is — which is what the real
    one does, since it unions every live session's cwd.
    """
    where = os.path.realpath(cwd if cwd is not None else root)
    _attach(daemon, port=port, pid=pid, cwd=where)
    daemon._session_states[session_id] = {"pid": pid, "last_event": 0,
                                          "project": "project"}
    daemon._agents_snapshot_cache = {"running": [{
        "session_id": session_id, "cwd": where, "project": "project",
    }]}
    daemon._known_project_roots = lambda: {where, os.path.realpath(root)}
    return os.path.realpath(root)


def _real_enrolment(monkeypatch, *roots):
    """Put `enrollment.root_enrolled` back to its real semantics — the longest
    enrolled root *containing* the path.

    `tests/conftest.py`'s autouse door patches it to identity suite-wide,
    which makes every folder its own project and hides exactly the bug this
    pair of tests is about.
    """
    folders = [os.path.realpath(r) for r in roots]

    def _enrolled(cwd):
        if not cwd:
            return ""
        here = os.path.realpath(cwd)
        best = ""
        for folder in folders:
            if (here == folder or here.startswith(folder + os.sep)) \
                    and len(folder) > len(best):
                best = folder
        return best

    monkeypatch.setattr(enrollment, "root_enrolled", _enrolled)


# --- the tool surface ---------------------------------------------------------


def test_claude_lists_exactly_ten_tools_in_order():
    """Exhaustive on purpose: every name here is something an agent can do to
    Dark Army, and an eleventh arriving unnoticed is what this assertion
    prevents."""
    assert [t["name"] for t in cs.tools_for_host(cs.HOST_CLAUDE)] == [
        "dark_army_add_card", "dark_army_close_card", "dark_army_attach_plan",
        "dark_army_attach_report", "dark_army_needs_manual_check", "dark_army_answer_card",
        "dark_army_knowledge_read", "dark_army_knowledge_write",
        "dark_army_request_start", "dark_army_next_card"]


def test_codex_still_lists_exactly_the_six_board_verbs():
    """Codex gets the verbs that write a board row and type nothing. The
    knowledge notes are not among them, and a seventh arriving unnoticed is
    what this assertion prevents."""
    assert [t["name"] for t in cs.tools_for_host(cs.HOST_CODEX)] == [
        "dark_army_add_card", "dark_army_close_card", "dark_army_attach_plan",
        "dark_army_attach_report", "dark_army_needs_manual_check",
        "dark_army_next_card"]


@pytest.mark.parametrize("tool", [cs.KNOWLEDGE_READ_TOOL,
                                  cs.KNOWLEDGE_WRITE_TOOL])
def test_neither_schema_lets_a_caller_name_a_project(tool):
    """The scoping is the omission. A property here would turn the daemon's
    'which project is this session in' into 'which project did the caller say'."""
    properties = tool["inputSchema"]["properties"]
    for forbidden in ("project", "root", "session_id", "card_id", "cwd"):
        assert forbidden not in properties


def test_the_read_takes_no_arguments_at_all():
    assert cs.KNOWLEDGE_READ_TOOL["inputSchema"]["properties"] == {}
    assert "required" not in cs.KNOWLEDGE_READ_TOOL["inputSchema"]


def test_the_write_takes_exactly_key_question_and_answer():
    schema = cs.KNOWLEDGE_WRITE_TOOL["inputSchema"]
    assert set(schema["properties"]) == {"key", "question", "answer"}
    assert schema["required"] == ["key", "answer"]


def test_codex_cannot_call_either_knowledge_tool(monkeypatch):
    """Omitting a tool from `tools/list` is not a guard: a client may call a
    known name directly, so `call_tool` enforces the same list."""
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO(),
                              host=cs.HOST_CODEX)
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "a forbidden tool must never reach the daemon"))
    for name, args in (("dark_army_knowledge_read", {}),
                       ("dark_army_knowledge_write", {"key": "purpose",
                                                "answer": "a"})):
        result = server.call_tool({"name": name, "arguments": args})
        assert result["isError"] is True
        assert "unavailable for codex" in result["content"][0]["text"]


def test_the_read_ignores_any_argument_that_arrives_anyway(monkeypatch):
    """The schema declares no properties; honouring one that arrived would be
    the first step towards naming somebody else's project."""
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    sent = []
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.append(msg) or {"ok": True,
                                                         "entries": []})
    server.call_tool({"name": "dark_army_knowledge_read",
                      "arguments": {"root": "/etc", "project": "other"}})
    assert sent[0]["type"] == "knowledge_read_request"
    assert set(sent[0]) == {"type", "port", "pid"}


def test_the_write_sends_only_the_three_declared_fields(monkeypatch):
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    sent = []
    monkeypatch.setattr(server, "call_daemon",
                        lambda msg: sent.append(msg) or {"ok": True,
                                                         "detail": "stored"})
    server.call_tool({"name": "dark_army_knowledge_write",
                      "arguments": {"key": "purpose", "question": "q",
                                    "answer": "a", "root": "/etc"}})
    assert set(sent[0]) == {"type", "port", "pid", "note_key", "question",
                            "answer"}
    assert sent[0]["type"] == "knowledge_write_request"
    assert sent[0]["note_key"] == "purpose"


def test_the_catalogue_key_never_rides_the_reserved_key_field(monkeypatch):
    """`key` on the hook socket is the project's *enrolment* key. `_keyed`
    only fills it when it is empty, so a catalogue key sitting there would be
    read by `BobDaemon._enrolled_root` as an enrolment key, resolve to
    nothing, and get every write refused at the door.
    """
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    monkeypatch.setattr(server, "project_key", lambda: "the-enrolment-key")
    sent = []
    # `_keyed` is the real one — it is the thing under test. Only the socket
    # hop is stood in for.
    monkeypatch.setattr(
        server, "call_daemon",
        lambda msg: sent.append(server._keyed(msg)) or {"ok": True,
                                                        "detail": "stored"})
    server.call_tool({"name": "dark_army_knowledge_write",
                      "arguments": {"key": "purpose", "answer": "a"}})
    assert sent, "the write never reached call_daemon"
    assert sent[0]["key"] == "the-enrolment-key"
    assert sent[0]["note_key"] == "purpose"


def test_a_write_with_no_answer_never_reaches_the_daemon(monkeypatch):
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    monkeypatch.setattr(server, "call_daemon", lambda msg: pytest.fail(
        "an empty note must be refused locally"))
    result = server.call_tool({"name": "dark_army_knowledge_write",
                               "arguments": {"key": "purpose", "answer": " "}})
    assert result["isError"] is True
    result = server.call_tool({"name": "dark_army_knowledge_write",
                               "arguments": {"key": " ", "answer": "a"}})
    assert result["isError"] is True


def test_a_silent_daemon_is_an_error_not_an_empty_answer(monkeypatch):
    server = cs.ChannelServer(stdin=io.StringIO(), stdout=io.StringIO())
    monkeypatch.setattr(server, "call_daemon", lambda msg: None)
    for name, args in (("dark_army_knowledge_read", {}),
                       ("dark_army_knowledge_write", {"key": "k", "answer": "a"})):
        result = server.call_tool({"name": name, "arguments": args})
        assert result["isError"] is True
        assert "Dark Army did not answer" in result["content"][0]["text"]


# --- the daemon handlers -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_request_bob_cannot_attribute_is_refused(tmp_path):
    """The forgery case: nothing is read and nothing is written."""
    daemon, store = _board_daemon(tmp_path)
    try:
        reply = await daemon._handle_knowledge_read_request(
            {"type": "knowledge_read_request", "port": 51000})
        assert reply["ok"] is False
        reply = await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "answer": "a"})
        assert reply["ok"] is False
        assert store.knowledge_for(str(tmp_path)) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_session_outside_every_known_root_is_refused(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    try:
        _placed(daemon, root)
        # The same session, with the known-roots set emptied under it.
        daemon._known_project_roots = lambda: set()
        reply = await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "answer": "a"})
        assert reply["ok"] is False
        assert reply["detail"] == daemon_mod.KNOWLEDGE_UNPLACED_REFUSAL
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_named_root_in_the_payload_is_ignored(tmp_path):
    """A forger who adds a `root` key gets the session's own project anyway."""
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    other = tmp_path / "elsewhere"
    root.mkdir()
    other.mkdir()
    try:
        canonical = _placed(daemon, root)
        store.knowledge_put(str(other), "purpose", "q", "somebody else's")
        reply = await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "answer": "ours",
             "root": str(other), "project": "elsewhere",
             "session_id": "someone-else"})
        assert reply["ok"] is True, reply
        assert [r["answer"] for r in store.knowledge_for(canonical)] == ["ours"]
        assert [r["answer"] for r in store.knowledge_for(str(other))] == [
            "somebody else's"]

        read = await daemon._handle_knowledge_read_request(
            {"type": "knowledge_read_request", "port": 51000,
             "root": str(other)})
        assert [e["answer"] for e in read["entries"]] == ["ours"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_write_records_the_calling_session_as_the_author(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    try:
        canonical = _placed(daemon, root, session_id="s-author")
        reply = await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "question": "What for?",
             "answer": "a tool"})
        assert reply["ok"] is True, reply
        row = store.knowledge_for(canonical)[0]
        assert row["author"] == "s-author"
        assert row["question"] == "What for?"
        assert row["source"] == "agent"
        assert row["last_confirmed"] == 0
        assert row["stale"] == ""
        store.knowledge_mark_stale(canonical, "purpose")
        reply = await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "question": "What for?",
             "answer": "rewritten"})
        assert reply["ok"] is True, reply
        row = store.knowledge_for(canonical)[0]
        assert row["stale"] == "1"
        assert row["last_confirmed"] == 0
        assert row["source"] == "agent"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_store_refusal_is_returned_in_bobs_own_words(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    try:
        _placed(daemon, root)
        reply = await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "answer": "   "})
        assert reply["ok"] is False
        assert "answer" in reply["detail"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_closed_board_refuses_rather_than_raising(tmp_path):
    daemon = _daemon()
    root = tmp_path / "project"
    root.mkdir()
    _placed(daemon, root)
    reply = await daemon._handle_knowledge_read_request(
        {"type": "knowledge_read_request", "port": 51000})
    assert reply["ok"] is False
    assert reply["detail"] == daemon_mod.KNOWLEDGE_STORE_REFUSAL


# --- the reply-on-refusal tuple, driven rather than grepped ---------------------


def test_both_types_are_channel_message_types():
    assert "knowledge_read_request" in daemon_mod.CHANNEL_MESSAGE_TYPES
    assert "knowledge_write_request" in daemon_mod.CHANNEL_MESSAGE_TYPES


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["knowledge_read_request",
                                  "knowledge_write_request"])
async def test_an_unenrolled_project_gets_a_reply_not_silence(
        tmp_path, monkeypatch, kind):
    """The sharpest edge in this change. `_handle_message` hardcodes which
    types are answered on a refusal; a type missing from that tuple returns
    `None`, and the calling session then blocks for the full 5s CALL_TIMEOUT
    on every knowledge call.

    The suite's autouse door fixture patches `root_enrolled` to identity, so
    this test patches it back to a closed door explicitly.
    """
    daemon, store = _board_daemon(tmp_path)
    monkeypatch.setattr(enrollment, "resolve", lambda key: "")
    monkeypatch.setattr(enrollment, "root_enrolled", lambda cwd: "")
    try:
        reply = await daemon._handle_message(
            {"type": kind, "port": 51000, "cwd": str(tmp_path),
             "note_key": "purpose", "answer": "a"})
        assert isinstance(reply, dict), (
            "an unanswered id is a session waiting for ever")
        assert reply["ok"] is False
        assert reply["error"] == daemon_mod.ENROLLMENT_REFUSAL
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_enrolled_read_routes_through_handle_message(tmp_path):
    """`_route_channel` has to name both types too — a type in
    `CHANNEL_MESSAGE_TYPES` but not in the router falls through to `None`."""
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    try:
        canonical = _placed(daemon, root)
        store.knowledge_put(canonical, "purpose", "q", "a tool")
        reply = await daemon._handle_message(
            {"type": "knowledge_read_request", "port": 51000,
             "cwd": str(root)})
        assert isinstance(reply, dict)
        assert [e["answer"] for e in reply["entries"]] == ["a tool"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_enrolled_write_routes_through_handle_message(tmp_path):
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    try:
        canonical = _placed(daemon, root)
        reply = await daemon._handle_message(
            {"type": "knowledge_write_request", "port": 51000,
             "cwd": str(root), "note_key": "purpose",
             "answer": "a tool"})
        assert isinstance(reply, dict) and reply["ok"] is True, reply
        assert [r["answer"] for r in store.knowledge_for(canonical)] == [
            "a tool"]
    finally:
        store.close()


# --- the notes are keyed on the enrolled root, not on a cwd --------------------


@pytest.mark.asyncio
async def test_a_session_in_a_subdirectory_writes_under_the_enrolled_root(
        tmp_path, monkeypatch):
    """The common case in this repo, whose own instructions are `cd host && …`.

    `_known_project_roots()` unions every live session's cwd, so `<proj>/host`
    is a member of it in its own right — keying on that would give the
    subdirectory its own bucket, and a session at the project root would then
    read nothing and re-ask every answered question.
    """
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    inner = root / "host"
    inner.mkdir(parents=True)
    _real_enrolment(monkeypatch, root)
    try:
        _placed(daemon, root, cwd=inner)
        reply = await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "answer": "a menu-bar monitor"})
        assert reply["ok"] is True, reply

        assert [r["answer"] for r in
                store.knowledge_for(os.path.realpath(root))] == [
            "a menu-bar monitor"]
        assert store.knowledge_for(os.path.realpath(inner)) == [], (
            "a subdirectory must not become its own note bucket")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_sibling_at_the_root_reads_the_subdirectorys_answer_back(
        tmp_path, monkeypatch):
    """The other half, and the one a person would notice: one project, one set
    of notes, whichever folder the session happens to have started in."""
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    inner = root / "host"
    inner.mkdir(parents=True)
    _real_enrolment(monkeypatch, root)
    try:
        _placed(daemon, root, cwd=inner)
        await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "answer": "a menu-bar monitor"})

        # A second session, same project, started at the root.
        _placed(daemon, root, session_id="s-root", port=51001, pid=4343)
        read = await daemon._handle_knowledge_read_request(
            {"type": "knowledge_read_request", "port": 51001})
        assert read["ok"] is True, read
        assert [e["answer"] for e in read["entries"]] == ["a menu-bar monitor"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_cwd_in_no_enrolled_root_is_refused(tmp_path, monkeypatch):
    """`root_enrolled` answering `""` is a refusal, not an empty-string key.

    The door above has already admitted the message, so this is not the only
    enrolment test and is not meant to be — but a note filed under `""` would
    be one bucket shared by every unplaceable session on the machine.
    """
    daemon, store = _board_daemon(tmp_path)
    stray = tmp_path / "stray"
    stray.mkdir()
    _real_enrolment(monkeypatch, tmp_path / "somewhere-else")
    try:
        _placed(daemon, stray)
        reply = await daemon._handle_knowledge_write_request(
            {"type": "knowledge_write_request", "port": 51000,
             "note_key": "purpose", "answer": "a"})
        assert reply["ok"] is False
        assert reply["detail"] == daemon_mod.KNOWLEDGE_UNPLACED_REFUSAL
        assert store.knowledge_for("") == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_board_that_closes_mid_read_is_a_refusal_not_an_empty_list(
        tmp_path):
    """"Dark Army could not read" and "this project has answered nothing" must not
    reach the model as the same sentence: the second has the skill ask every
    question again."""
    daemon, store = _board_daemon(tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    canonical = _placed(daemon, root)
    store.knowledge_put(canonical, "purpose", "q", "a")

    real_place = daemon._knowledge_place

    async def _place_then_close(msg):
        answer = await real_place(msg)
        daemon._board = None          # the board shuts between the two calls
        return answer

    daemon._knowledge_place = _place_then_close
    try:
        reply = await daemon._handle_knowledge_read_request(
            {"type": "knowledge_read_request", "port": 51000})
        assert reply["ok"] is False
        assert reply["detail"] == daemon_mod.KNOWLEDGE_STORE_REFUSAL
        assert "entries" not in reply
    finally:
        store.close()


# --- the read is bounded -------------------------------------------------------


def test_a_long_answer_is_shortened_and_says_so():
    text = cs.render_knowledge(
        [{"key": "purpose", "question": "What for?", "answer": "a" * 5000}])
    assert len(text) < 1200
    assert "shortened" in text
    assert "the full text is stored and unchanged" in text


def test_a_short_answer_is_returned_whole_with_no_note():
    text = cs.render_knowledge(
        [{"key": "purpose", "question": "What for?", "answer": "a tool",
          "last_confirmed": 1_700_000_000}])
    assert text == "[purpose] What for?\na tool"


def test_a_full_store_cannot_flood_the_callers_context():
    """The store's own bounds allow 80 x 4000 ~ 320 KB, and the skill calls
    the read *first* on every run."""
    entries = [{"key": f"k{i:03d}", "question": "q", "answer": "a" * 4000}
               for i in range(80)]
    text = cs.render_knowledge(entries)
    assert len(text) <= cs.READ_RESULT_CHARS + 500
    assert "left out" in text


def test_the_cap_drops_whole_entries_from_the_end():
    """A half-written last answer would read as the project's actual words."""
    entries = [{"key": f"k{i:03d}", "question": "", "answer": "a" * 4000}
               for i in range(80)]
    text = cs.render_knowledge(entries)
    body = text.split("\n\n(")[0]
    for block in body.split("\n\n"):
        key, _, answer = block.partition("\n")
        assert "[k" in key
        assert answer.endswith(cs._TRIMMED)
    assert "[k000]" in body and "[k079]" not in body


def _keys_in(text):
    import re
    body = text.split("\n\n(")[0]
    keys = []
    for block in body.split("\n\n"):
        match = re.search(r"\[([^\]]+)\]", block.partition("\n")[0])
        keys.append(match.group(1) if match else "")
    return keys


def test_a_small_entry_after_the_cap_is_not_smuggled_in():
    """The cap **stops**; it does not skip-and-carry-on.

    Skipping one oversize entry while appending the smaller ones after it
    returns a by-key list with a hole in the middle, which a model cannot tell
    from those keys being unanswered — so the skill picks them as the next
    questions and `dark_army_knowledge_write` overwrites the stored answer, with no
    delete verb and no confirmation anywhere on the path.

    Entries of differing sizes on purpose: with a uniform fixture this passes
    either way, which is how it got in.
    """
    entries = [{"key": f"k{i:03d}", "question": "", "answer": "a" * 600}
               for i in range(45)]
    entries.append({"key": "zshort", "question": "", "answer": "tiny"})

    text = cs.render_knowledge(entries)
    kept = _keys_in(text)

    assert "zshort" not in kept, (
        "a short entry after the cap must not be smuggled past the ones it "
        "follows")
    assert kept == sorted(kept), "the reply must stay in the store's own order"
    assert kept == [f"k{i:03d}" for i in range(len(kept))], (
        "the kept run must be a prefix — no hole in the middle")


def test_the_omitted_keys_are_named_so_they_cannot_read_as_unanswered():
    entries = [{"key": f"k{i:03d}", "question": "", "answer": "a" * 600}
               for i in range(45)]
    entries.append({"key": "zshort", "question": "", "answer": "tiny"})

    text = cs.render_knowledge(entries)
    kept = set(_keys_in(text))
    trailer = text.split("\n\n(")[1]

    assert "already answered" in trailer
    for entry in entries:
        if entry["key"] not in kept:
            assert entry["key"] in trailer, entry["key"]


def test_shortened_counts_only_answers_the_reply_actually_carries():
    """The trailer's one job is to let the model tell a trimmed answer in
    *this reply* from a complete one, so counting an entry that was then
    dropped describes something the reader cannot see."""
    entries = [{"key": f"k{i:03d}", "question": "", "answer": "a" * 4000}
               for i in range(80)]
    text = cs.render_knowledge(entries)
    kept = _keys_in(text)
    trailer = text.split("\n\n(")[1]

    assert f"{len(kept)} answer(s) were shortened" in trailer
    assert len(kept) < 80


def test_one_oversize_entry_is_still_returned_whole_rather_than_nothing():
    """An empty reply reads as "nothing is answered", which is the same lie by
    another route — so the first entry is exempt however long it is."""
    text = cs.render_knowledge(
        [{"key": "purpose", "question": "", "answer": "a" * 4000}])
    assert _keys_in(text) == ["purpose"]
    assert "left out" not in text


def test_a_row_that_is_not_a_dict_is_skipped_rather_than_fatal():
    text = cs.render_knowledge(["nonsense", {"key": "purpose",
                                             "answer": "a tool",
                                             "last_confirmed": 1}])
    assert text == "[purpose]\na tool"


def test_a_stale_block_contains_stale_and_the_key():
    text = cs.render_knowledge(
        [{"key": "purpose", "question": "What for?", "answer": "old",
          "stale": "1", "last_confirmed": 1_700_000_000}])
    assert "STALE" in text
    assert "[purpose]" in text
    assert "old" in text


def test_an_unconfirmed_block_contains_unconfirmed_and_the_key():
    text = cs.render_knowledge(
        [{"key": "purpose", "question": "What for?", "answer": "new"}])
    assert "UNCONFIRMED" in text
    assert "[purpose]" in text


def test_a_stale_unconfirmed_agent_rewrite_still_includes_the_key():
    text = cs.render_knowledge(
        [{"key": "purpose", "answer": "rewritten", "stale": "1",
          "last_confirmed": 0}])
    assert "STALE" in text
    assert "UNCONFIRMED" in text
    assert "[purpose]" in text
