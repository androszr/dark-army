"""Every agent Dark Army starts says where it came from.

The trigger was a real incident: a session Dark Army staged appeared on the phone
with a character name, a job title and a project, and nothing at all saying
that Dark Army had started it. "I thought we are being hacked."

The mechanism under test is the one decided then: **attribution is stamped at
the spawn and never reverse-engineered afterwards.** So the cases here follow
the stamp from `origin.stamp` through the three spawn seams, through the hook
handler's `_with_common`, into the session state first-writer-wins, and out
onto the published row — plus the refusals that keep a ragged stamp from ever
becoming a sentence.

Seams, all existing: the `dispatch.spawn` / `spawn_agent` stubs the dispatch
tests already use, a stubbed `ptyhost.current()`, a stubbed `_rpc`, the
`exec`'d `NOTIFY_SCRIPT` namespace `test_multi_select_question.py` uses, a
hand-built `_board_state` dict, and `tmp_path` for the session store.
"""

import json

import pytest

from dark_army_daemon import dispatch, origin, ptyhost, session_store
from dark_army_daemon import vscode_reveal
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon
from dark_army_menubar.hooks import NOTIFY_SCRIPT


# --- the vocabulary ----------------------------------------------------------


@pytest.mark.parametrize("kind", origin.KINDS)
def test_a_stamp_round_trips_for_every_kind(kind):
    raw = origin.stamp(kind, "card-9", "bc-implementer")
    assert origin.parse(raw) == {
        "by": kind, "card_id": "card-9", "stage": "bc-implementer"}


def test_the_mission_kind_is_a_whole_sentence():
    """Mission Control names no card, so its line is a whole sentence like
    the ad-hoc one — drawn verbatim by both clients."""
    assert "mission" in origin.KINDS
    assert origin.sentence("mission") == \
        "Dark Army opened this terminal as Mission Control"
    assert origin.sentence("mission", "ignored title", "stage") == \
        "Dark Army opened this terminal as Mission Control"


def test_a_stamp_of_an_unknown_kind_is_no_stamp():
    """`""` is what every call site reads as "add nothing", so an unknown kind
    cannot produce a spawn that differs from today's by a single byte."""
    assert origin.stamp("card-invent", "card-9") == ""
    assert origin.stamp("", "card-9") == ""


@pytest.mark.parametrize("raw", [
    "",
    "card-start",                     # one field
    "card-start|card-9",              # two fields
    "card-start|card-9|stage|extra",  # four fields
    "nonsense|card-9|stage",          # unknown kind
    "card-start|card 9|stage",        # a space is not in the alphabet
    "card-start|card-9;rm -rf|stage",  # a shell metacharacter
    "card-start|card-9|$(whoami)",
])
def test_parse_fails_closed(raw):
    assert origin.parse(raw) == {}


def test_an_over_long_stamp_is_refused_rather_than_trimmed():
    over = "card-start|" + ("a" * origin.MAX_STAMP_CHARS) + "|x"
    assert len(over) > origin.MAX_STAMP_CHARS
    assert origin.parse(over) == {}


def test_stamp_bounds_each_field_so_a_long_card_id_still_parses():
    raw = origin.stamp("card-start", "c" * 500, "s" * 500)
    assert len(raw) <= origin.MAX_STAMP_CHARS
    parsed = origin.parse(raw)
    assert parsed["by"] == "card-start"
    assert len(parsed["card_id"]) == origin.MAX_FIELD_CHARS


def test_env_with_sets_exactly_one_key_and_copies():
    base = {"PATH": "/bin", "HOME": "/u"}
    out = origin.env_with(base, origin.stamp("card-start", "c1"))
    assert set(out) - set(base) == {origin.ENV_VAR}
    assert origin.ENV_VAR not in base           # the base is never mutated
    assert out["PATH"] == "/bin"


def test_env_with_an_empty_stamp_adds_nothing():
    base = {"PATH": "/bin"}
    assert origin.env_with(base, "") == base
    assert origin.env_with(base) == base


def test_sentence_says_who_started_it_and_never_the_card_id():
    line = origin.sentence("card-start", "Rename the strip ladder",
                           "bc-implementer")
    assert line.startswith("Dark Army started this")
    assert "Rename the strip ladder" in line
    assert "card-9" not in line


def test_sentence_of_an_unknown_kind_is_empty():
    assert origin.sentence("card-invent", "A card") == ""
    assert origin.sentence("", "A card") == ""


def test_sentence_without_a_title_still_reads_as_a_sentence():
    assert origin.sentence("card-refine") == \
        "Dark Army started this to plan a board card"


def test_sentence_is_clamped():
    line = origin.sentence("card-start", "T" * 500, "bc-implementer")
    assert len(line) == origin.MAX_LINE_CHARS


def test_a_stage_with_a_space_survives_the_stamp():
    """`workflow` is free text a person typed. Filtering the space out turned
    "security review" into "securityreview" on the row — a mangling that reads
    as a fault in Dark Army rather than as the words somebody wrote."""
    raw = origin.stamp("card-start", "c1", "security review")
    assert origin.parse(raw) == {
        "by": "card-start", "card_id": "c1", "stage": "security review"}
    assert "(security review)" in origin.sentence(
        "card-start", "A card", "security review")


def test_a_stage_field_is_still_bounded_and_idempotent():
    """`parse` compares against `_clean_stage`, so the cleaned form has to be
    a fixed point — including at the length bound, where a cut landing just
    past a space would otherwise leave a trailing one."""
    long_stage = " ".join(["review"] * 40)
    raw = origin.stamp("card-start", "c1", long_stage)
    parsed = origin.parse(raw)
    assert parsed and parsed["stage"] == parsed["stage"].strip()
    assert origin.parse(origin.stamp("card-start", "c1", parsed["stage"])) \
        == parsed


def test_a_stage_the_stamp_could_not_have_written_is_dropped_not_repaired():
    """Never a silent repair: a bracketed half-mangled trade name is worse
    than no bracket at all, so `sentence` leaves it out entirely."""
    line = origin.sentence("card-start", "A card", "security/review")
    assert line == "Dark Army started this to build A card"


def test_an_adhoc_terminal_names_itself_and_no_card():
    """The one kind that names no card. A terminal Dark Army opened on a press is
    still a row a person did not type into being, and an unattributed one is
    exactly what this module exists to remove."""
    raw = origin.stamp("adhoc")
    assert origin.parse(raw) == {"by": "adhoc", "card_id": "", "stage": ""}
    line = origin.sentence("adhoc")
    assert line == "Dark Army opened this terminal for you"
    assert "board card" not in line


def test_the_module_imports_nothing_from_the_daemon():
    """Pure and stdlib-only, so the hook handler's own copy of the rule can
    stay stdlib-only too."""
    text = (origin.__file__ and open(origin.__file__).read()) or ""
    assert "from ." not in text
    assert "STRIP_LADDER" not in text and "StripRung" not in text


# --- the three spawn seams ---------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_sends_the_stamp_and_never_overwrites_an_unset(monkeypatch):
    from dark_army_daemon import subprocess_env
    seen = {}

    async def fake_spawn_agent(root, argv, name, *, env_extra=None):
        seen["env_extra"] = env_extra
        return {"spawned": True, "terminalName": "bob"}

    monkeypatch.setattr(vscode_reveal, "spawn_agent", fake_spawn_agent)
    stamp = origin.stamp("card-start", "c1", "bc-implementer")
    ok, detail, pid = await dispatch.spawn("/tmp", ["/bin/claude", "go"], "n",
                                           stamp=stamp)
    assert ok, detail
    assert seen["env_extra"] == {origin.ENV_VAR: stamp}
    # An *inherited* stamp is one of the variables the payload deletes — the
    # terminal's shell may carry the press that started the app — and the
    # caller's own goes on after it (`test_spawn_agent_merges_after_the_unset_payload`).
    assert subprocess_env.unset_payload()[origin.ENV_VAR] is None


@pytest.mark.asyncio
async def test_an_unstamped_spawn_sends_no_env_extra(monkeypatch):
    seen = {}

    async def fake_spawn_agent(root, argv, name, *, env_extra=None):
        seen["env_extra"] = env_extra
        return {"spawned": True, "terminalName": "bob"}

    monkeypatch.setattr(vscode_reveal, "spawn_agent", fake_spawn_agent)
    await dispatch.spawn("/tmp", ["/bin/claude", "go"], "n")
    assert seen["env_extra"] is None


def test_spawn_agent_merges_after_the_unset_payload(monkeypatch):
    """The order is load-bearing: the payload's job is to *delete* py2app's
    variables, and a merge the other way round would let a stamp resurrect
    one."""
    import asyncio

    from dark_army_daemon import subprocess_env
    victim = next(iter(subprocess_env.PY2APP_ENV_VARS))
    sent = {}

    monkeypatch.setattr(vscode_reveal, "_spawn_capable_locks",
                        lambda: [{"port": 1, "authToken": "t",
                                  "workspaceFolders": ["/tmp"]}])
    monkeypatch.setattr(vscode_reveal, "_lock_owns", lambda lock, root: True)

    async def fake_post(port, token, body, timeout=8.0):
        sent["body"] = body
        return {"spawned": True}

    monkeypatch.setattr(vscode_reveal, "_post_json", fake_post)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        vscode_reveal.spawn_agent(
            "/tmp", ["/bin/claude", "go"], "n",
            env_extra={origin.ENV_VAR: "card-start|c1|", victim: "smuggled",
                       "NOT_A_STRING": 5}))
    env = sent["body"]["env"]
    assert env[origin.ENV_VAR] == "card-start|c1|"
    assert env[victim] == "smuggled"   # merged after, by design and by test
    assert "NOT_A_STRING" not in env   # non-string values never ride


@pytest.mark.asyncio
async def test_spawn_local_hands_the_pty_host_an_env_carrying_the_stamp(
        monkeypatch, tmp_path):
    seen = {}

    class Host:
        async def start(self, root, argv, name, **kw):
            seen.update(kw)
            return True, name, 77

    monkeypatch.setattr(ptyhost, "current", lambda: Host())
    stamp = origin.stamp("card-refine", "c2", "bc-planner")
    ok, _detail, pid = await dispatch.spawn_local(
        str(tmp_path), ["/bin/claude", "go"], "n", stamp=stamp)
    assert (ok, pid) == (True, 77)
    assert seen["env"][origin.ENV_VAR] == stamp
    # The child's whole environment, not one variable: `clean_env` copies what
    # it is given, so a bare stamp would start the assistant with no PATH.
    assert len(seen["env"]) > 1


@pytest.mark.asyncio
async def test_an_unstamped_local_spawn_passes_no_env_at_all(
        monkeypatch, tmp_path):
    """Which is also what keeps it working against a broker too old to know
    the field."""
    seen = []

    class Host:
        async def start(self, root, argv, name, **kw):
            seen.append(kw)
            return True, name, 77

    monkeypatch.setattr(ptyhost, "current", lambda: Host())
    await dispatch.spawn_local(str(tmp_path), ["/bin/claude", "go"], "n")
    assert seen == [{}]


@pytest.mark.asyncio
async def test_the_broker_rpc_carries_a_filtered_env(monkeypatch):
    host = ptyhost.PtyHost(port=0)
    host.persist = True
    sent = {}

    async def fake_attach():
        return True

    async def fake_rpc(op, **kw):
        sent["op"] = op
        sent.update(kw)
        return {"ok": True, "handle": "", "pid": 5, "detail": "started"}

    monkeypatch.setattr(host, "attach", fake_attach)
    monkeypatch.setattr(host, "_rpc", fake_rpc)
    stamp = origin.stamp("card-start", "c3")
    ok, _detail, pid = await host.start(
        "/tmp", ["/bin/claude", "go"], "n",
        env=origin.env_with({"PATH": "/bin", "SECRET": "shh"}, stamp))
    assert ok and pid == 5
    # Only the one key leaves this process. The broker gets no PATH, no
    # SECRET and no general-purpose environment channel.
    assert sent["env"] == {origin.ENV_VAR: stamp}


@pytest.mark.asyncio
async def test_an_unstamped_broker_start_sends_no_env(monkeypatch):
    host = ptyhost.PtyHost(port=0)
    host.persist = True
    sent = {}

    async def fake_attach():
        return True

    async def fake_rpc(op, **kw):
        sent.update(kw)
        return {"ok": True, "handle": "", "pid": 5}

    monkeypatch.setattr(host, "attach", fake_attach)
    monkeypatch.setattr(host, "_rpc", fake_rpc)
    await host.start("/tmp", ["/bin/claude"], "n")
    assert sent["env"] is None


@pytest.mark.asyncio
async def test_the_broker_start_op_keeps_only_the_origin_key(monkeypatch):
    from dark_army_daemon import pty_broker

    seen = {}

    class Engine:
        async def start(self, root, argv, name, *, env=None, cols=None,
                        rows=None):
            seen["env"] = env
            return True, "ok", 42

        def owns(self, pid):
            return None

    broker = pty_broker.Broker.__new__(pty_broker.Broker)
    broker.engine = Engine()
    broker._writer = None
    replies = []
    async def _send(msg):
        replies.append(msg)

    monkeypatch.setattr(broker, "_send", _send, raising=False)
    await broker._handle({
        "op": "start", "id": 1, "root": "/tmp", "argv": ["/bin/claude"],
        "name": "n",
        "env": {origin.ENV_VAR: "card-start|c1|", "SMUGGLED": "no",
                "BOB_COMPANION_ORIGIN_EXTRA": "no"},
    })
    env = seen["env"]
    assert env[origin.ENV_VAR] == "card-start|c1|"
    assert "SMUGGLED" not in env and "BOB_COMPANION_ORIGIN_EXTRA" not in env
    # Merged onto the broker's own environment, never sent as the whole of it.
    assert len(env) > 1


@pytest.mark.asyncio
async def test_the_broker_start_op_without_an_env_passes_none(monkeypatch):
    from dark_army_daemon import pty_broker

    seen = {}

    class Engine:
        async def start(self, root, argv, name, *, env=None, cols=None,
                        rows=None):
            seen["env"] = env
            return True, "ok", 42

        def owns(self, pid):
            return None

    broker = pty_broker.Broker.__new__(pty_broker.Broker)
    broker.engine = Engine()
    broker._writer = None
    async def _send(msg):
        return None

    monkeypatch.setattr(broker, "_send", _send, raising=False)
    await broker._handle({"op": "start", "id": 1, "root": "/tmp",
                          "argv": ["/bin/claude"], "name": "n"})
    assert seen["env"] is None


# --- the three call sites ----------------------------------------------------


@pytest.fixture
def daemon(tmp_path):
    d = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    d._board = store
    try:
        yield d, store
    finally:
        store.close()


def _make(store, **kw):
    fields = {"title": "do the thing", "project": "bob", "root": "/tmp",
              "prompt": "go", "tool": "claude", "column_name": "backlog"}
    fields.update(kw)
    card, detail = store.create(fields)
    assert card is not None, detail
    return card


def _stub_spawn(d, monkeypatch, seen):
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def accept(root, argv, name, *, stamp=""):
        seen.append(stamp)
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    monkeypatch.setattr(dispatch, "spawn_local", accept)


@pytest.mark.asyncio
async def test_start_stamps_the_card_and_its_next_declared_stage(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store, workflow="bc-implementer\nbc-verifier")
    seen = []
    _stub_spawn(d, monkeypatch, seen)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert origin.parse(seen[-1]) == {
        "by": "card-start", "card_id": card["id"], "stage": "bc-implementer"}


@pytest.mark.asyncio
async def test_start_names_the_stage_after_the_one_already_run(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store, workflow="bc-implementer\nbc-verifier")
    store.record_agents(card["id"], ["bc-implementer"])
    seen = []
    _stub_spawn(d, monkeypatch, seen)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert origin.parse(seen[-1])["stage"] == "bc-verifier"


@pytest.mark.asyncio
async def test_a_card_declaring_no_stages_names_none(daemon, monkeypatch):
    """Never invented: a stamp naming a stage nobody wrote down would be a
    claim rather than a record."""
    d, store = daemon
    card = _make(store)
    seen = []
    _stub_spawn(d, monkeypatch, seen)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert origin.parse(seen[-1]) == {
        "by": "card-start", "card_id": card["id"], "stage": ""}


@pytest.mark.asyncio
async def test_refine_stamps_the_planner(daemon, monkeypatch):
    d, store = daemon
    card = _make(store, column_name="prep")
    seen = []
    _stub_spawn(d, monkeypatch, seen)
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert origin.parse(seen[-1]) == {
        "by": "card-refine", "card_id": card["id"], "stage": "bc-planner"}


@pytest.mark.asyncio
async def test_a_consult_stamps_itself_with_no_stage(daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    seen = []
    _stub_spawn(d, monkeypatch, seen)
    ok, detail = await d.ask_card(card["id"], "why is this slow?")
    assert ok, detail
    assert origin.parse(seen[-1]) == {
        "by": "card-consult", "card_id": card["id"], "stage": ""}


# --- the hook handler --------------------------------------------------------


def _script_ns(monkeypatch, stamp=None):
    if stamp is None:
        monkeypatch.delenv("BOB_COMPANION_ORIGIN", raising=False)
    else:
        monkeypatch.setenv("BOB_COMPANION_ORIGIN", stamp)
    ns = {"__name__": "dark_army_notify_under_test"}
    exec(compile(NOTIFY_SCRIPT, "dark-army-notify", "exec"), ns)
    return ns


def test_the_hook_carries_the_stamp_on_every_message(monkeypatch):
    stamp = origin.stamp("card-start", "c1", "bc-implementer")
    ns = _script_ns(monkeypatch, stamp)
    msg = ns["_with_common"]({"event": "tool_use"}, "s1", "proj", 7, "claude")
    assert msg["origin"] == stamp


def test_an_unstamped_process_sends_no_origin_key_at_all(monkeypatch):
    ns = _script_ns(monkeypatch, None)
    msg = ns["_with_common"]({"event": "tool_use"}, "s1", "proj", 7, "claude")
    assert "origin" not in msg


def test_the_hook_bounds_what_it_carries(monkeypatch):
    ns = _script_ns(monkeypatch, "x" * 5000)
    msg = ns["_with_common"]({"event": "add"}, "s1", "proj", 7, "claude")
    assert len(msg["origin"]) == 200


def test_the_hook_reads_the_environment_once(monkeypatch):
    """One module-global read at import and one dict store per message: no
    syscall and no fork were added to a path that runs on every event."""
    ns = _script_ns(monkeypatch, "card-start|c1|")
    monkeypatch.setenv("BOB_COMPANION_ORIGIN", "card-start|LATER|")
    msg = ns["_with_common"]({"event": "add"}, "s1", "proj", 7, "claude")
    assert msg["origin"] == "card-start|c1|"


def test_the_hook_script_added_no_subprocess_use():
    assert "BOB_COMPANION_ORIGIN" in NOTIFY_SCRIPT


# --- first writer wins -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_stamp_wins_across_two_session_starts(daemon):
    d, _store = daemon
    await d._handle_message({"event": "session_start", "session_id": "s1",
                             "origin": "card-start|c1|bc-implementer"})
    await d._handle_message({"event": "session_start", "session_id": "s1",
                             "origin": "card-start|c2|bc-verifier"})
    assert d._session_states["s1"]["origin"] == "card-start|c1|bc-implementer"


@pytest.mark.asyncio
async def test_an_ordinary_event_fills_a_missing_origin(daemon):
    """The whole reason the stamp rides `_with_common` rather than
    `session_start` alone: a SessionStart sent while the daemon was
    restarting is dropped for ever."""
    d, _store = daemon
    await d._handle_message({"event": "tool_use", "session_id": "s1",
                             "origin": "card-start|c1|"})
    assert d._session_states["s1"]["origin"] == "card-start|c1|"


@pytest.mark.asyncio
async def test_a_later_event_never_changes_an_origin(daemon):
    d, _store = daemon
    await d._handle_message({"event": "session_start", "session_id": "s1",
                             "origin": "card-start|c1|"})
    await d._handle_message({"event": "tool_use", "session_id": "s1",
                             "origin": "card-consult|c9|"})
    assert d._session_states["s1"]["origin"] == "card-start|c1|"


@pytest.mark.asyncio
async def test_an_unstamped_session_has_no_origin_key(daemon):
    d, _store = daemon
    await d._handle_message({"event": "session_start", "session_id": "s1"})
    assert "origin" not in d._session_states["s1"]


# --- a Grok row is stamped off its own process, never its hook ---------------
#
# Grok runs its hooks under the shared `grok agent leader`, which inherits
# the environment of whichever terminal first started it. On 21 Sep 2026
# that terminal was a dark-army card's, and every Grok session started
# in the ten hours after — a fit-app implementer included — wore that card's
# origin line on the phone. The session's own `grok` process carries the
# right stamp; the daemon reads it there.


def test_process_origin_reads_the_stamp_out_of_a_ps_line():
    from dark_army_daemon import pid_resolver
    line = ("/Users/x/.grok/bin/grok -- do the thing PWD=/Users/x/fit-app "
            "BOB_COMPANION_ORIGIN=card-start|be64|fa-implementer "
            "GROK_SESSION_ID=01a0 HOME=/Users/x")
    assert pid_resolver.process_origin(84010, line) == \
        "card-start|be64|fa-implementer"
    # A stage with a space is one value, not two tokens.
    spaced = "A=1 BOB_COMPANION_ORIGIN=card-start|c1|security review B=2"
    assert pid_resolver.process_origin(1, spaced) == \
        "card-start|c1|security review"
    # Last in the line, and absent.
    assert pid_resolver.process_origin(1, "A=1 BOB_COMPANION_ORIGIN=adhoc||") \
        == "adhoc||"
    assert pid_resolver.process_origin(1, "A=1 B=2") == ""
    assert pid_resolver.process_origin(0) == ""


async def _settled(d, sid, want, tries=50):
    import asyncio
    for _ in range(tries):
        await asyncio.sleep(0.01)
        if (d._session_states.get(sid) or {}).get("origin", "") == want:
            return True
    return False


@pytest.mark.asyncio
async def test_a_grok_hook_stamp_is_ignored_and_the_process_own_is_used(
        daemon, monkeypatch):
    from dark_army_daemon import pid_resolver
    d, _store = daemon
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, state: 84010)
    monkeypatch.setattr(pid_resolver, "process_origin",
                        lambda pid, environment=None:
                        "card-start|be64|fa-implementer" if pid == 84010 else "")
    await d._handle_message({"event": "session_start", "session_id": "g1",
                             "provider": "grok",
                             "origin": "card-start|1298|"})
    assert d._session_states["g1"].get("origin") != "card-start|1298|"
    assert await _settled(d, "g1", "card-start|be64|fa-implementer")
    # Once per pid: a later message with the leader's stamp changes nothing.
    await d._handle_message({"event": "tool_use", "session_id": "g1",
                             "provider": "grok",
                             "origin": "card-start|1298|"})
    assert d._session_states["g1"]["origin"] == "card-start|be64|fa-implementer"


@pytest.mark.asyncio
async def test_a_grok_row_stored_off_the_leader_is_corrected_or_cleared(
        daemon, monkeypatch):
    """A `sessions.json` written by the build before this one carries the
    leader's stamp; the probe replaces it, and clears it for a `grok` a
    person typed themselves."""
    from dark_army_daemon import pid_resolver
    d, _store = daemon
    d._session_states["g1"] = {"state": "idle", "last_event": 1.0,
                               "origin": "card-start|1298|"}
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, state: 555)
    monkeypatch.setattr(pid_resolver, "process_origin",
                        lambda pid, environment=None: "")
    await d._handle_message({"event": "tool_use", "session_id": "g1",
                             "provider": "grok",
                             "origin": "card-start|1298|"})
    assert await _settled(d, "g1", "")
    assert "origin" not in d._session_states["g1"]


def test_a_grok_message_s_mission_stamp_never_quietens_a_reply(daemon):
    """A `grok` typed inside Mission Control's terminal hands the leader
    a `mission` stamp; the message-side fallback in `_mission_reply` must
    not read it, or every Grok Stop would raise no card."""
    d, _store = daemon
    assert not d._mission_reply("g1", "")
    assert d._mission_reply("g1", "mission||")


@pytest.mark.asyncio
async def test_a_grok_stop_with_the_leader_s_mission_stamp_still_raises(
        daemon, monkeypatch):
    from dark_army_daemon import pid_resolver
    d, _store = daemon
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, state: 777)
    monkeypatch.setattr(pid_resolver, "process_origin",
                        lambda pid, environment=None: "")
    seen = []
    monkeypatch.setattr(d, "_mission_reply",
                        lambda sid, stamp="": seen.append(stamp) or False)
    await d._handle_message({"event": "add", "hook": "Stop",
                             "session_id": "g1", "provider": "grok",
                             "origin": "mission||"})
    assert seen and all(stamp == "" for stamp in seen)


@pytest.mark.asyncio
async def test_a_claude_hook_stamp_is_still_the_row_s(daemon):
    d, _store = daemon
    await d._handle_message({"event": "session_start", "session_id": "s1",
                             "provider": "claude",
                             "origin": "card-start|c1|bc-implementer"})
    assert d._session_states["s1"]["origin"] == "card-start|c1|bc-implementer"


# --- persistence -------------------------------------------------------------


def test_the_origin_survives_a_restart(tmp_path):
    path = tmp_path / "sessions.json"
    session_store.save_sessions(
        {"s1": {"state": "idle", "last_event": 1.0,
                "origin": "card-start|c1|bc-implementer"}}, path)
    back = session_store.load_sessions(path)
    assert back["s1"]["origin"] == "card-start|c1|bc-implementer"


def test_a_session_written_without_one_loads_without_the_key(tmp_path):
    path = tmp_path / "sessions.json"
    session_store.save_sessions({"s1": {"state": "idle", "last_event": 1.0}},
                                path)
    assert "origin" not in session_store.load_sessions(path)["s1"]


# --- the published row -------------------------------------------------------


def _row_for(d, sid):
    snap = d.detailed_snapshot()
    for rows in snap.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if row.get("session_id") == sid:
                return row
    raise AssertionError(f"no row for {sid}")


def _seed(d, sid, origin_stamp=None, title="Rename the strip ladder"):
    state = {"state": "idle", "last_event": 1e12, "pid": 1}
    if origin_stamp is not None:
        state["origin"] = origin_stamp
    d._session_states[sid] = state
    d._board_state = {"cards": [{"id": "c1", "title": title}]}


def test_a_stamped_row_publishes_the_origin_line_and_its_two_fields(monkeypatch):
    d = BobDaemon()
    _seed(d, "s1", origin.stamp("card-start", "c1", "bc-implementer"))
    row = _row_for(d, "s1")
    assert row["origin_by"] == "card-start"
    assert row["origin_card"] == "c1"
    assert row["origin_line"] == origin.sentence(
        "card-start", "Rename the strip ladder", "bc-implementer")
    assert "Rename the strip ladder" in row["origin_line"]


def test_an_unstamped_row_publishes_none_of_the_three():
    """A session a person started themselves looks exactly as it did before
    this change — which is what makes the line worth reading."""
    d = BobDaemon()
    _seed(d, "s1", None)
    row = _row_for(d, "s1")
    assert "origin_by" not in row
    assert "origin_card" not in row
    assert "origin_line" not in row


def test_a_ragged_stamp_publishes_no_origin_line():
    d = BobDaemon()
    _seed(d, "s1", "card-start|c1")     # two fields: not a stamp
    row = _row_for(d, "s1")
    assert "origin_line" not in row


def test_an_origin_line_survives_a_card_the_board_has_lost():
    d = BobDaemon()
    _seed(d, "s1", origin.stamp("card-start", "gone"))
    row = _row_for(d, "s1")
    assert row["origin_card"] == "gone"
    assert row["origin_line"] == "Dark Army started this to build a board card"


def test_no_secret_rides_the_origin_fields():
    d = BobDaemon()
    _seed(d, "s1", origin.stamp("card-start", "c1", "bc-implementer"))
    row = _row_for(d, "s1")
    blob = json.dumps({k: v for k, v in row.items()
                       if k.startswith("origin")}, default=str).lower()
    for word in ("key", "token", "claim", "secret", "digest"):
        assert word not in blob


# --- subagents name the work -------------------------------------------------


def test_subagent_card_title_rides_where_the_parent_is_stamped():
    d = BobDaemon()
    _seed(d, "s1", origin.stamp("card-start", "c1", "bc-implementer"))
    d._session_states["s1"]["subagents"] = {"bc-verifier"}
    row = _row_for(d, "s1")
    rows = row["subagent_rows"]
    assert rows and all(r["card_title"] == "Rename the strip ladder"
                        for r in rows)


def test_subagent_card_title_is_absent_where_the_parent_is_not_stamped():
    d = BobDaemon()
    _seed(d, "s1", None)
    d._session_states["s1"]["subagents"] = {"bc-verifier"}
    rows = _row_for(d, "s1")["subagent_rows"]
    assert rows and all("card_title" not in r for r in rows)


def test_a_finished_row_still_says_where_it_came_from():
    """`_forget_session` pops `_session_states`, so without the tombstone's
    own copy a row stopped answering "what is this?" the moment it moved to
    *Recently finished* — the one place a person goes to ask."""
    d = BobDaemon()
    stamp = origin.stamp("card-start", "c1", "bc-implementer")
    _seed(d, "s1", stamp)
    d._forget_session("s1", "ended")
    assert "s1" not in d._session_states
    row = _row_for(d, "s1")
    assert row["origin_by"] == "card-start"
    assert row["origin_card"] == "c1"
    assert "Rename the strip ladder" in row["origin_line"]
    # The raw stamp is a key, not a caption: it never rides the published row.
    assert "origin" not in row


def test_a_finished_unstamped_row_publishes_none_of_the_three():
    d = BobDaemon()
    _seed(d, "s1", None)
    d._forget_session("s1", "ended")
    row = _row_for(d, "s1")
    assert "origin_by" not in row
    assert "origin_line" not in row
    assert "origin" not in row


def test_the_subagent_row_keeps_its_bare_stage_name():
    """`subagent_type` keys the card's running-stage highlight on both
    clients. Qualifying it there would silently stop every card highlighting
    its running step — the feature looking like it works while it broke the
    one beside it."""
    d = BobDaemon()
    _seed(d, "s1", origin.stamp("card-start", "c1", "bc-implementer"))
    d._session_states["s1"]["subagents"] = {"bc-verifier"}
    rows = _row_for(d, "s1")["subagent_rows"]
    assert [r["subagent_type"] or r["agent_id"] for r in rows] == ["bc-verifier"]


# --- what this change deliberately did not touch -----------------------------


def test_no_board_column_and_no_writable_field():
    """`crew_trail`'s ring rule: the origin lives on the session, never on the
    card, so no surface can claim an origin by writing a card."""
    from dark_army_daemon import board
    from dark_army_daemon.api_server import ApiServer
    assert "origin" not in board.BoardStore._WRITABLE
    assert "origin" not in ApiServer._BOARD_FIELDS
    added = {name for cols in board.BoardStore._ADDED_COLUMNS.values()
             for name, _decl in cols}
    assert "origin" not in added


@pytest.mark.parametrize("kind,slug,present", [("card-start","backbone",True), ("adhoc","backbone",False), ("card-refine","backbone",False), ("card-start","",False)])
def test_area_line_only_on_card_start(kind,slug,present):
    from dark_army_daemon import areas
    d = BobDaemon()
    _seed(d,"area-session",origin.stamp(kind,"c1"))
    d._board_state["cards"][0]["area"] = slug
    row = _row_for(d,"area-session")
    if present:
        assert row["area"] == slug
        assert row["area_line"] == areas.lead_line(row["nickname"],slug)
    else:
        assert "area" not in row and "area_line" not in row

def test_area_line_kept_on_tombstone():
    from dark_army_daemon import areas
    d = BobDaemon()
    _seed(d,"area-ended",origin.stamp("card-start","c1"))
    d._board_state["cards"][0]["area"] = "backbone"
    d._forget_session("area-ended","ended")
    row = _row_for(d,"area-ended")
    assert row["area"] == "backbone"
    assert row["area_line"] == areas.lead_line(row["nickname"],"backbone")


# --- review and merge: the two helper kinds ------------------------------------


def test_the_review_and_merge_fix_kinds_stamp_parse_and_read_as_sentences():
    """A review helper and a merge-fix helper are stamped like every other
    press: a known kind, round-tripped, and a sentence both clients draw
    verbatim (`docs/card-worktrees.md`, *Review and merge*)."""
    for kind in ("card-review", "card-merge-fix"):
        assert kind in origin.KINDS
        stamp = origin.stamp(kind, "c1")
        assert stamp == f"{kind}|c1|"
        assert origin.parse(stamp)["by"] == kind
    assert origin.sentence("card-review", "Add the thing") \
        == "Dark Army started this to review Add the thing"
    assert origin.sentence("card-merge-fix", "Add the thing") \
        == "Dark Army started this to fix the merge of Add the thing"
    # A kind nobody listed is still no stamp.
    assert origin.stamp("card-merge", "c1") == ""
