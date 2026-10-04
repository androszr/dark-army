# host/tests/test_dispatch.py
"""Dark Army as launcher. **The refusals are the test.**

This is the one capability in the app that starts a process nobody asked for by
typing, so the interesting behaviour is everything it declines to do. One case
each, plus the shape of the argv it builds and the two ways a launch can end
without a session.
"""

import asyncio
import io
import os
import pathlib
import time
import tokenize
from types import SimpleNamespace

import pytest

from dark_army_daemon import daemon as daemon_mod
from dark_army_daemon import dispatch
from dark_army_daemon import vscode_reveal
from dark_army_daemon.board import BoardStore
from dark_army_daemon.daemon import BobDaemon


def _card(**kw):
    card = {
        "id": "card-1",
        "project": "bob",
        "root": "/tmp",
        "title": "do the thing",
        "prompt": "rewrite the sprite pipeline",
        "tool": "claude",
        "column_name": "backlog",
        "session_id": "",
        "link_state": "",
        "dispatched_at": None,
    }
    card.update(kw)
    return card


def _guard(card, roots=("/tmp",), in_flight=(), now=1000.0, last_attempt=None):
    return dispatch.guard(card, roots=roots, in_flight=in_flight, now=now,
                          last_attempt=last_attempt)


# --- the guard ----------------------------------------------------------------


def test_a_ready_card_in_a_known_project_is_allowed():
    ok, detail = _guard(_card())
    assert ok, detail


def test_a_card_in_done_is_refused():
    """Re-checked against the store at the instant of dispatch, in the house
    style of `delete_abandoned_agent`'s category guard: a board three snapshots
    behind cannot start a card that has moved. Done is the one column that is
    never startable — starting work somebody has marked finished is always a
    mistake."""
    ok, detail = _guard(_card(column_name="done"))
    assert not ok
    assert "Backlog or In progress" in detail


def test_a_card_in_progress_with_no_session_is_still_startable():
    """A card can reach In progress without a run behind it — Dark Army's launcher
    switched off, or a session sent back — and refusing it there would leave it
    permanently unstartable with no button on screen able to say why."""
    ok, detail = _guard(_card(column_name="in_progress"))
    assert ok, detail


def test_a_card_already_bound_to_a_session_is_refused():
    ok, detail = _guard(_card(session_id="sess-1"))
    assert not ok
    assert "already being worked on" in detail


def test_a_card_already_starting_is_refused():
    ok, detail = _guard(_card(link_state="dispatching"))
    assert not ok
    assert "already starting" in detail


def test_a_card_with_no_tool_has_nothing_to_start():
    ok, detail = _guard(_card(tool=""))
    assert not ok
    assert "which assistant" in detail


def test_an_unknown_tool_is_refused():
    ok, detail = _guard(_card(tool="eliza"))
    assert not ok
    assert "eliza" in detail


def test_grok_is_startable_and_takes_its_prompt_behind_a_separator():
    """Verified 22 Aug 2026 against grok 1.0.5, not assumed: `grok --help` documents
    `grok [OPTIONS] [PROMPT]`, a flag-shaped prompt without the separator is
    rejected outright, and behind `--` both that and a subcommand name arrive as
    the prompt."""
    ok, _ = _guard(_card(tool="grok"))
    assert ok
    assert dispatch._EXECUTABLES["grok"] == "grok"
    assert dispatch.argv_for("grok", "/bin/grok", "fix it") == [
        "/bin/grok", "--", "fix it"]


def test_a_tool_bob_knows_of_but_cannot_launch_is_refused_in_words(monkeypatch):
    """The list is empty today. What it buys is the refusal *naming* the tool, so a
    chooser can keep offering it rather than silently dropping the entry."""
    monkeypatch.setitem(dispatch._UNSUPPORTED, "codex", "not supported yet")
    ok, detail = _guard(_card(tool="codex"))
    assert not ok
    assert "not supported yet" in detail


def test_a_root_that_is_not_a_known_project_is_refused():
    ok, detail = _guard(_card(root="/tmp"), roots=("/Users/somebody/other",))
    assert not ok
    assert "no open window for that project" in detail


def test_a_card_with_no_root_is_refused():
    ok, detail = _guard(_card(root=""))
    assert not ok
    assert "which folder" in detail


def test_a_root_that_is_not_a_directory_is_refused(tmp_path):
    gone = tmp_path / "was-here"
    ok, detail = _guard(_card(root=str(gone)), roots=(str(gone),))
    assert not ok
    assert "not there any more" in detail


def test_the_concurrency_bound_stops_a_third_launch():
    in_flight = [_card(id="a", project="p1"), _card(id="b", project="p2")]
    ok, detail = _guard(_card(id="c", project="p3"), in_flight=in_flight)
    assert not ok
    assert "already starting" in detail


def test_only_one_card_per_project_may_be_starting():
    """Not politeness: binding matches the first new session in the project, so
    two at once in one project would be two cards racing for one row."""
    ok, detail = _guard(_card(id="c"), in_flight=[_card(id="a", project="bob")])
    assert not ok
    assert "another card in this project" in detail


def test_the_cooldown_refuses_a_second_press():
    ok, detail = _guard(_card(), now=1000.0, last_attempt=995.0)
    assert not ok
    assert "just started" in detail
    ok, _ = _guard(_card(), now=1000.0,
                   last_attempt=1000.0 - dispatch.DISPATCH_COOLDOWN - 1)
    assert ok


def test_a_missing_card_is_refused_rather_than_crashing():
    ok, detail = _guard(None)
    assert not ok
    assert "no such card" in detail


def test_paths_are_compared_after_resolution(tmp_path):
    """`/tmp` and `/private/tmp` are the same folder on macOS, and a card written
    through one must not be refused because the window reported the other."""
    assert dispatch.normalise_root("/tmp") == dispatch.normalise_root("/private/tmp")


# --- the argv -----------------------------------------------------------------


def test_the_argv_is_the_executable_and_the_prompt_and_nothing_else():
    argv = dispatch.argv_for("claude", "/usr/local/bin/claude", "hello")
    assert argv == ["/usr/local/bin/claude", "hello"]


def test_the_prompt_travels_verbatim_as_one_element():
    """The prompt is untrusted — an agent may have written it through the
    channel's card tool — and it is never joined, quoted, escaped or handed to a
    shell. `createTerminal({shellPath, shellArgs})` spawns the array directly, so
    there is nothing for a metacharacter to mean."""
    nasty = '`; rm -rf ~ #$(whoami)'
    argv = dispatch.argv_for("claude", "/bin/claude", nasty)
    assert len(argv) == 2
    assert argv[1] == nasty


def test_codex_gets_the_end_of_options_separator():
    """"No shell" does not cover the CLI's own parser. `codex completion` writes
    a completion script and `codex apply` git-applies a diff into the working
    tree, so a prompt has to be marked as a positional — codex prescribes `--`
    and honours it (`codex -- completion` arrives as a prompt)."""
    argv = dispatch.argv_for("codex", "/opt/homebrew/bin/codex", "completion")
    assert argv == ["/opt/homebrew/bin/codex", "--", "completion"]
    assert argv[-1] == "completion"


def test_claude_gets_no_separator_because_it_ignores_one():
    """Measured: claude's commander parser still dispatched the `mcp`
    subcommand behind a `--`. A separator that does nothing would only suggest
    the problem was handled; the refusal in `guard()` is what handles it."""
    assert dispatch.argv_for("claude", "/bin/claude", "mcp") == ["/bin/claude", "mcp"]


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_a_prompt_that_opens_with_a_dash_is_refused(tool):
    """`codex "--please-fix-the-tests"` came back as an unknown-argument error,
    which is the parser saying it read the prompt as a flag —
    `--dangerously-bypass-approvals-and-sandbox` and `-c key=value` are real
    codex flags, so this is one option away from turning the sandbox off."""
    assert dispatch.prompt_refusal(tool, "--dangerously-bypass-approvals-and-sandbox")
    assert dispatch.prompt_refusal(tool, "  -c model=o3")


def test_a_prompt_that_is_a_subcommand_is_refused_per_tool():
    assert dispatch.prompt_refusal("codex", "apply")
    assert dispatch.prompt_refusal("codex", "logout")
    assert dispatch.prompt_refusal("claude", "mcp")
    # `apply` is codex's, not claude's — the list is per tool, not shared.
    assert dispatch.prompt_refusal("claude", "apply") is None


def test_an_ordinary_prompt_is_untouched():
    """The refusals are narrow on purpose: a real card carries a sentence, and
    a sentence can contain a subcommand's name without being one."""
    for prompt in ("rewrite the sprite pipeline",
                   "apply the review comments in dispatch.py",
                   "mcp servers keep dropping — find out why",
                   'fix the `--verbose` flag',
                   '`; rm -rf ~ #$(whoami)',
                   ""):
        assert dispatch.prompt_refusal("codex", prompt) is None, prompt
        assert dispatch.prompt_refusal("claude", prompt) is None, prompt


def test_the_guard_refuses_a_flag_shaped_prompt():
    ok, detail = _guard(_card(prompt="--dangerously-bypass-approvals-and-sandbox"))
    assert not ok
    assert "'-'" in detail


def test_the_guard_refuses_a_subcommand_prompt():
    ok, detail = _guard(_card(tool="codex", prompt="apply"))
    assert not ok
    assert "apply" in detail


def test_the_guard_passes_an_ordinary_prompt():
    ok, detail = _guard(_card(prompt="apply the review comments"))
    assert ok, detail


def test_claude_is_resolved_through_the_shared_finder(monkeypatch):
    """Bare `shutil.which` finds nothing under the empty launchd PATH a
    login-started Dark Army inherits, which made Start dead in the installed app and
    green in a checkout. Claude goes through the resolver that already has the
    fallback list."""
    monkeypatch.setattr(dispatch.shutil, "which", lambda _n: None)
    monkeypatch.setattr(dispatch.agents_poll, "find_claude_binary",
                        lambda: "/Users/x/.local/bin/claude")
    assert dispatch.resolve_executable("claude") == "/Users/x/.local/bin/claude"


def test_codex_falls_back_to_its_known_install_sites(monkeypatch, tmp_path):
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(dispatch.shutil, "which", lambda _n: None)
    monkeypatch.setattr(dispatch, "CODEX_CANDIDATES", (str(exe),))
    assert dispatch.resolve_executable("codex") == str(exe)


def test_codex_is_still_refused_when_it_is_genuinely_absent(monkeypatch):
    monkeypatch.setattr(dispatch.shutil, "which", lambda _n: None)
    monkeypatch.setattr(dispatch, "CODEX_CANDIDATES", ("/nowhere/codex",))
    assert dispatch.resolve_executable("codex") is None


def _launcher_code() -> str:
    """`dispatch.py` with every comment and string literal removed.

    Tokenised rather than grepped line by line, and that matters here: the file
    argues at length about what it does *not* do — "there is no `subprocess` in
    this file at all", the four flags it deliberately omits — so a plain grep
    would be answered by the prose disclaiming the very thing it looks for.
    """
    source = pathlib.Path(dispatch.__file__).read_text()
    out = []
    readline = io.StringIO(source).readline
    for tok in tokenize.generate_tokens(readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_nothing_in_the_launcher_reaches_a_shell():
    code = _launcher_code()
    assert "shell=True" not in code
    assert "subprocess" not in code
    assert "os.system" not in code
    assert "os.exec" not in code


def test_the_session_title_flags_are_deliberately_absent():
    """`session_title.py` passes four flags whose whole purpose is that the namer
    never becomes a visible session. Every one of them is wrong here, and the
    file says why — but only in comments.

    **`--model` is deliberately not in this loop and must never be added.** It
    was never one of the four (the docstring above has always said four), and
    since 30 Aug 2026 the launcher does pass it, per card, out of
    `dispatch.MODELS`. `_launcher_code()` strips STRING tokens, so the argv
    element `"--model"` is invisible to this audit by construction — the four
    flags below still answer, and a future reader "fixing" the loop by adding
    `--model` would be asserting the opposite of what the file now does.
    """
    code = _launcher_code()
    for flag in ("--no-session-persistence", "--setting-sources",
                 "--strict-mcp-config", "--dangerously-load"):
        assert flag not in code, flag


def test_an_unknown_tool_resolves_to_no_executable():
    assert dispatch.resolve_executable("eliza") is None
    assert dispatch.resolve_executable("") is None


# --- the daemon's half: refusal, binding and the timeout ----------------------


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


@pytest.mark.asyncio
async def test_the_preference_refuses_before_anything_else(daemon, monkeypatch):
    """Checked in `dispatch_card` above every other test, so the refusal is in
    one place and no surface can route around it."""
    d, store = daemon
    card = _make(store)
    d.board_dispatch_enabled = False

    def boom(*a, **k):          # nothing may be looked up, let alone spawned
        raise AssertionError("the guard ran with dispatch switched off")

    monkeypatch.setattr(dispatch, "guard", boom)
    ok, detail = await d.dispatch_card(card["id"])
    assert not ok
    assert "not allowed to start sessions" in detail


@pytest.mark.asyncio
async def test_a_refused_spawn_leaves_the_card_untouched(daemon, monkeypatch):
    """Act first, mutate second — `wrap_up_session`'s ordering, for its reason: a
    card that says it is starting must be one where a terminal really opened."""
    d, store = daemon
    card = _make(store)
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def refuse(root, argv, name, **_kw):
        return False, "no VS Code window could start it", None

    monkeypatch.setattr(dispatch, "spawn", refuse)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert not ok
    assert "no VS Code window" in detail
    got = store.get(card["id"])
    assert got["column_name"] == "backlog"
    assert got["link_state"] == ""
    assert got["dispatched_at"] is None


@pytest.mark.asyncio
async def test_start_names_each_attachment_exactly_once(
        daemon, monkeypatch, tmp_path):
    """Including for a card prepared before the never-bake rule, whose stored
    prompt already carries the path as its own line: the strip cleans the
    spawned text and the block adds it back once. What is saved is untouched."""
    from dark_army_daemon import attachments
    folder = tmp_path / "attachments"
    rel_folder = "abcd1234-efgh5678-ijkl9012-mnop34"
    staging = folder / rel_folder
    staging.mkdir(parents=True)
    dest = staging / "shot.png"
    dest.write_bytes(b"ok")
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", folder)
    abs_path = str(dest.resolve())
    d, store = daemon
    baked = ("Fix the wrap.\n\n"
             "Attached files (open with your file tools):\n"
             f"{abs_path}\n\n{abs_path}")
    card = _make(store, prompt=baked,
                 attachments=f"{rel_folder}/shot.png")
    seen = {}
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        seen["argv"] = argv
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    prompt = seen["argv"][1]
    assert prompt.count(abs_path) == 1
    assert prompt.count("Attached files (open with your file tools):") == 1
    assert prompt.startswith("Fix the wrap.")
    # The stored instructions are never edited.
    assert store.get(card["id"])["prompt"] == baked


@pytest.mark.asyncio
async def test_an_accepted_spawn_marks_the_card_dispatching(daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    seen = {}
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        seen["argv"] = argv
        seen["root"] = root
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"] == ["/bin/claude", "go"]
    got = store.get(card["id"])
    assert got["link_state"] == "dispatching"
    assert got["dispatched_at"] is not None


@pytest.mark.asyncio
async def test_a_planned_card_dispatches_implement_not_the_leftover_idea(
        daemon, monkeypatch, tmp_path):
    """The live failure: grok card `pipeline` had
    `plan_path = plans/2026-08-23-reorder-the-pipeline-queue.md` and
    `prompt` = the leftover idea. Start handed Grok the idea, so the
    session rewrote the plan instead of building it."""
    d, store = daemon
    root = pathlib.Path(tmp_path).resolve()
    plan = root / "work.md"
    plan.write_text("# Work\n", encoding="utf-8")
    leftover = ("Enable users to reorder items within the pipeline queue "
                "through drag-and-drop or similar interaction, persisting "
                "the new order.")
    card = _make(store, root=str(root), column_name="prep",
                 prompt=leftover, tool="grok")
    attached, detail = store.attach_plan(card["id"], str(plan), "")
    assert attached is not None, detail
    seen = {}
    monkeypatch.setattr(d, "_known_project_roots", lambda: {str(root)})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: "/bin/grok")

    async def accept(spawn_root, argv, name, **_kw):
        seen["argv"] = argv
        seen["root"] = spawn_root
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    ok, detail = await d.dispatch_card(card["id"])
    assert ok, detail
    assert seen["argv"][0] == "/bin/grok"
    assert seen["argv"][1] == "--"
    handed = seen["argv"][2]
    assert handed.startswith(f"Plan: {plan}")
    assert f"/ship implement {plan}" in handed
    assert leftover not in handed


def test_a_matching_session_binds_and_moves_the_card(daemon):
    d, store = daemon
    card = _make(store)
    now = time.time()
    store.update(card["id"], {"link_state": "dispatching", "dispatched_at": now})
    d._dispatch_baseline[card["id"]] = {"already-running"}
    snapshot = {"running": [
        {"session_id": "already-running", "provider": "claude",
         "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 1},
        {"session_id": "the-new-one", "provider": "claude",
         "kind": "interactive",
         "project": "bob", "cwd": "/tmp", "started_at": now + 2},
    ]}
    assert d._reconcile_board(snapshot) is True
    got = store.get(card["id"])
    assert got["session_id"] == "the-new-one"
    assert got["column_name"] == "in_progress"
    assert got["link_state"] == "live"


def test_a_session_inside_the_cards_worktree_binds_too(daemon):
    """The sibling for card isolation (`docs/card-worktrees.md`): the session
    a Start opened in `<root>/.worktrees/card-<id8>` wears the project's own
    label and a cwd under the root, and `_candidate_matches`' containment
    rung binds it — the root stays the card's, the cwd is what moved."""
    d, store = daemon
    card = _make(store)
    now = time.time()
    store.update(card["id"], {"link_state": "dispatching", "dispatched_at": now})
    d._dispatch_baseline[card["id"]] = set()
    snapshot = {"running": [
        {"session_id": "in-the-worktree", "provider": "claude",
         "kind": "interactive", "project": "bob",
         "cwd": "/tmp/.worktrees/card-abcd1234", "started_at": now + 1},
    ]}
    assert d._reconcile_board(snapshot) is True
    got = store.get(card["id"])
    assert got["session_id"] == "in-the-worktree"
    assert got["link_state"] == "live"


def test_a_session_in_another_project_is_not_bound(daemon):
    d, store = daemon
    card = _make(store)
    now = time.time()
    store.update(card["id"], {"link_state": "dispatching", "dispatched_at": now})
    snapshot = {"running": [
        {"session_id": "elsewhere", "provider": "claude",
         "kind": "interactive",
         "project": "other", "cwd": "/tmp", "started_at": now + 1},
    ]}
    assert d._reconcile_board(snapshot) is False
    assert store.get(card["id"])["session_id"] == ""


def test_a_card_whose_session_never_appears_goes_back_where_it_belongs(daemon):
    """The terminal is left exactly where it is: Dark Army opened it and may not
    understand what happened in it, and closing somebody's terminal on a guess is
    worse than a card that says so.

    *Where it belongs* is read off `plan_path`, not remembered from the press:
    an unplanned card — a Prep card started through the confirmation — goes
    back to **Prep**, because Backlog's caption promises "planned" and a
    planless card landing there would falsify the one-line column meaning
    that is the whole argument for Prep. A planned card goes to Backlog."""
    d, store = daemon
    stale = time.time() - dispatch.DISPATCH_BIND_WINDOW - 1
    # Unplanned (Prep-born, confirmed unplanned): back to Prep.
    unplanned = _make(store)
    store.update(unplanned["id"], {"link_state": "dispatching",
                                   "dispatched_at": stale})
    # Planned: back to Backlog. `plan_path` is outside `_WRITABLE` on
    # purpose (`attach_plan` is its one writer), so the test sets it the
    # blunt way.
    planned = _make(store, title="planned one")
    store.update(planned["id"], {"link_state": "dispatching",
                                 "dispatched_at": stale})
    store._conn.execute("UPDATE cards SET plan_path = ? WHERE id = ?",
                        ("/tmp/plans/x.md", planned["id"]))
    store._conn.commit()
    assert d._reconcile_board({"running": []}) is True
    got = store.get(unplanned["id"])
    assert got["column_name"] == "prep"
    assert got["link_state"] == ""
    assert "no session appeared" in got["dispatch_error"]
    got = store.get(planned["id"])
    assert got["column_name"] == "backlog"
    assert got["link_state"] == ""
    assert "no session appeared" in got["dispatch_error"]


def test_a_card_still_inside_the_bind_window_is_left_alone(daemon):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"link_state": "dispatching",
                              "dispatched_at": time.time()})
    assert d._reconcile_board({"running": []}) is False
    assert store.get(card["id"])["link_state"] == "dispatching"


def test_a_finished_but_alive_session_keeps_its_card_live(daemon):
    """`_enrich_agent_stubs` moves a live session quiet past
    FINISHED_IDLE_GRACE_SECONDS out of `sleeping` and into `finished` with
    `alive: True`. Reading only the three live buckets stamped such a card
    `ended` after the grace, so the board said an agent sitting in a terminal
    had gone — the one thing it exists not to say."""
    d, store = daemon
    card = _make(store)
    store.bind_session(card["id"], "quiet-but-alive")
    old = time.time() - d.BOARD_SESSION_GRACE - 10
    d._board_missing_since[card["id"]] = old
    snapshot = {"running": [], "waiting": [], "sleeping": [],
                "finished": [{"session_id": "quiet-but-alive", "alive": True}]}
    d._reconcile_board(snapshot)
    got = store.get(card["id"])
    assert got["link_state"] == "live"
    assert got["session_ended_at"] is None
    # And a genuinely finished row still ends the link.
    d._board_missing_since[card["id"]] = old
    d._reconcile_board({"running": [], "waiting": [], "sleeping": [],
                        "finished": [{"session_id": "quiet-but-alive"}]})
    assert store.get(card["id"])["link_state"] == "ended"


@pytest.mark.asyncio
async def test_two_concurrent_dispatches_of_one_card_spawn_once(daemon, monkeypatch):
    """The bound has to be enforced, not merely computed. The guard reads the
    card, the roots and the in-flight list and then awaits three more times
    before anything is written, so without `_dispatch_lock` both presses passed
    a guard neither had claimed and both opened a terminal."""
    import asyncio

    d, store = daemon
    card = _make(store)
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    spawns = []

    async def accept(root, argv, name, **_kw):
        spawns.append(root)
        await asyncio.sleep(0.05)      # the real spawn is a round trip
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    results = await asyncio.gather(
        d.dispatch_card(card["id"], allow_unplanned=True),
        d.dispatch_card(card["id"], allow_unplanned=True))
    assert len(spawns) == 1
    assert sorted(ok for ok, _ in results) == [False, True]


@pytest.mark.asyncio
async def test_reset_clears_the_link_and_the_error_in_one_write(daemon):
    """The recovery path the board offers in words. It has to be atomic: a card
    landing back in Ready still carrying a dead session id is refused by
    `dispatch.guard` as already being worked on, and has no Start button to say
    so with."""
    d, store = daemon
    card = _make(store)
    store.bind_session(card["id"], "dead-session")
    store.mark_ended(card["id"], when=time.time())
    store.update(card["id"], {"dispatch_error": "no session appeared"})
    got, detail = await d.reset_card(card["id"],
                                     {"column_name": "backlog"})
    assert got is not None, detail
    assert got["column_name"] == "backlog"
    assert got["session_id"] == ""
    assert got["link_state"] == ""
    assert got["dispatch_error"] == ""
    assert got["session_ended_at"] is None
    ok, _ = dispatch.guard(got, roots={"/private/tmp", "/tmp"},
                           in_flight=[], now=time.time(),
                           last_attempt=None)
    assert ok


def test_an_update_naming_only_unwritable_fields_is_refused(daemon):
    """`(current, "nothing to change")` made a write that did nothing look
    exactly like one that worked, all the way up to the panel."""
    d, store = daemon
    card = _make(store)
    got, detail = store.update(card["id"], {"nonsense": "1"})
    assert got is None
    assert "no writable fields" in detail
    # The genuinely idempotent case is untouched: a field named with the value
    # it already holds is a real write.
    got, _ = store.update(card["id"], {"title": card["title"]})
    assert got is not None


@pytest.mark.asyncio
async def test_a_stored_unmet_dependency_queues_rather_than_dispatching(
        daemon, monkeypatch):
    """Cards wait on cards again (`docs/card-dependencies.md`). A press on a
    card whose dependency is not done is accepted and queued — never refused,
    never started — and the reply names the card it waits for."""
    d, store = daemon
    blocker = _make(store, title="the blocker")
    card = _make(store, title="once blocked work")
    store.update(card["id"], {"blocked_by": blocker["id"]})
    spawned = []

    async def spawn(*a, **k):
        spawned.append(1)
        return True, "ok", None

    monkeypatch.setattr(dispatch, "spawn", spawn)
    # CI has no `claude` on PATH; the guard's executable lookup is stubbed
    # exactly as the other dispatch tests stub it.
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    monkeypatch.setattr(d, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert not ok
    assert '"the blocker"' in detail
    assert spawned == []
    assert store.get(card["id"])["queue_state"] == "queued"


# --- the return leg: a card arriving in Done wraps up its session -------------
#
# `_reconcile_board` watches the session and updates the card; this is the other
# direction. A card arriving in Done closes the terminal when that switch is
# on, and never clears the conversation.


def _live(store, **kw):
    card = _make(store, **kw)
    store.bind_session(card["id"], "sess-1")
    store.mark_live(card["id"])
    return card


def _close_spy(d, monkeypatch, result=(True, "")):
    """Record `_close_session_terminal` calls without one reaching VS Code."""
    calls = []

    async def _close(session_id):
        calls.append(session_id)
        return result

    monkeypatch.setattr(d, "_close_session_terminal", _close)
    return calls


def _wrap_up_spy(d, monkeypatch, result=(True, "")):
    """Record `wrap_up_session` calls without letting one reach a terminal.

    The Done leg no longer wraps up; delete-card tests still pin that
    `/clear` is not a fallback when a close is refused.
    """
    calls = []

    async def _wrap(session_id):
        calls.append(session_id)
        return result

    monkeypatch.setattr(d, "wrap_up_session", _wrap)
    return calls


def _pid_close_spy(d, monkeypatch, result=(True, "")):
    """Record `_close_terminal_by_pid` calls without one reaching VS Code."""
    calls = []

    async def _close(pid):
        calls.append(pid)
        return result

    monkeypatch.setattr(d, "_close_terminal_by_pid", _close)
    return calls


@pytest.mark.asyncio
async def test_the_close_runs_when_its_switch_is_on(daemon, monkeypatch):
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    closes = _close_spy(d, monkeypatch)
    got, detail = await d.update_card(card["id"], {"column_name": "done"})
    assert got is not None, detail
    assert closes == ["sess-1"]
    assert store.get(card["id"])["column_name"] == "done"
    assert store.get(card["id"])["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_a_gap_drop_into_done_closes_too(daemon, monkeypatch):
    """The case a one-door implementation fails: a drop *between* two cards
    goes through `board_reorder`, not `board_update`."""
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    closes = _close_spy(d, monkeypatch)
    got, detail = await d.reorder_card(card["id"], "done", "")
    assert got is not None, detail
    assert closes == ["sess-1"]
    assert store.get(card["id"])["column_name"] == "done"


@pytest.mark.asyncio
async def test_the_defaults_leave_a_done_card_and_its_session_alone(
        daemon, monkeypatch):
    """With the close switch off, a card arriving in Done touches no session
    at all — the finished session keeps its `## Work done` report on screen,
    and no `dispatch_error` says anything about it."""
    d, store = daemon
    assert d.board_close_terminal_enabled is False
    card = _live(store)
    closes = _close_spy(d, monkeypatch)
    await d.update_card(card["id"], {"column_name": "done"})
    assert closes == []
    got = store.get(card["id"])
    assert got["column_name"] == "done"
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_a_refused_close_leaves_the_terminal_alone_and_writes_no_note(
        daemon, monkeypatch):
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    closes = _close_spy(d, monkeypatch,
                        result=(False, "no 0.1.9+ window matched"))
    await d.update_card(card["id"], {"column_name": "done"})
    assert closes == ["sess-1"]
    got = store.get(card["id"])
    assert got["column_name"] == "done"
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_a_successful_close_clears_a_stale_dispatch_error(daemon,
                                                                monkeypatch):
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    store.update(card["id"], {"dispatch_error": "an older complaint"})
    _close_spy(d, monkeypatch)
    await d.update_card(card["id"], {"column_name": "done"})
    assert store.get(card["id"])["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_re_saving_a_card_already_in_done_closes_nothing(daemon,
                                                               monkeypatch):
    """An arrival, not an edit. Without this the sheet's Save on a Done card
    would fire a fresh close at whoever did the work."""
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    store.update(card["id"], {"column_name": "done"})
    store.mark_live(card["id"])
    closes = _close_spy(d, monkeypatch)
    await d.update_card(card["id"], {"title": "renamed"})
    assert closes == []


@pytest.mark.asyncio
async def test_a_card_that_never_ran_is_filed_to_done_in_silence(daemon,
                                                                 monkeypatch):
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _make(store)
    closes = _close_spy(d, monkeypatch)
    await d.update_card(card["id"], {"column_name": "done"})
    assert closes == []
    got = store.get(card["id"])
    assert got["column_name"] == "done"
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_a_card_whose_session_already_ended_is_not_closed(daemon,
                                                                monkeypatch):
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    store.mark_ended(card["id"])
    closes = _close_spy(d, monkeypatch)
    await d.update_card(card["id"], {"column_name": "done"})
    assert closes == []
    assert store.get(card["id"])["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_a_live_codex_card_is_filed_to_done_in_silence(daemon, monkeypatch):
    """Codex is a silent rung, not a noisy refusal."""
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store, tool="codex")
    closes = _close_spy(d, monkeypatch)
    await d.update_card(card["id"], {"column_name": "done"})
    assert closes == []
    got = store.get(card["id"])
    assert got["column_name"] == "done"
    assert got["dispatch_error"] == ""


@pytest.mark.asyncio
async def test_declare_done_does_not_close_the_session(daemon, monkeypatch):
    """An agent closing its own card is speaking *during its own turn*."""
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    closes = _close_spy(d, monkeypatch)
    got, detail = await d.close_card_by_session("sess-1", "tests pass")
    assert got is not None, detail
    done = store.get(card["id"])
    assert done["column_name"] == "done"
    assert done["closed_by"] == "sess-1"
    assert closes == []


@pytest.mark.asyncio
async def test_a_refused_update_closes_nothing(daemon, monkeypatch):
    """The hook runs only when the write landed — acting on a refused update
    would reach into a session over a change that never happened."""
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    closes = _close_spy(d, monkeypatch)
    got, detail = await d.update_card(card["id"], {"closed_by": "somebody"})
    assert got is None, detail
    assert closes == []


@pytest.mark.asyncio
async def test_two_concurrent_moves_to_done_close_once(daemon, monkeypatch):
    """What `_board_write_lock` is for: without it both presses read a
    non-`done` `before` and both close the tab."""
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    closes = _close_spy(d, monkeypatch)
    await asyncio.gather(
        d.update_card(card["id"], {"column_name": "done"}),
        d.update_card(card["id"], {"column_name": "done"}),
    )
    assert closes == ["sess-1"]


@pytest.mark.asyncio
async def test_a_prompted_session_is_not_closed(daemon, monkeypatch):
    """A session with a relayed permission prompt up is mid-turn asking to
    act, so the close is never attempted and no orange note is written."""
    d, store = daemon
    d.board_close_terminal_enabled = True
    card = _live(store)
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {"sess-1": {}})
    closes = _close_spy(d, monkeypatch)
    await d.update_card(card["id"], {"column_name": "done"})
    assert closes == []
    got = store.get(card["id"])
    assert got["column_name"] == "done"
    assert got["dispatch_error"] == ""


# `_close_session_terminal` itself — the pid ladder, the reply check, and the
# bookkeeping the dispose owes because the SIGHUP'd CLI emits no SessionEnd.


@pytest.mark.asyncio
async def test_close_helper_with_no_pid_refuses_and_forgets_nothing(daemon,
                                                                    monkeypatch):
    d, _ = daemon

    async def boom(pid, tty):
        raise AssertionError("close_terminal was called with no pid on record")

    monkeypatch.setattr(vscode_reveal, "close_terminal", boom)
    ok, detail = await d._close_session_terminal("sess-1")
    assert ok is False
    assert "no pid" in detail
    assert "sess-1" not in d._finished


@pytest.mark.asyncio
async def test_close_helper_treats_no_matching_window_as_not_closed(daemon,
                                                                    monkeypatch):
    """None from the fan-out means no 0.1.9+ window matched — the session must
    stay on the books so the caller's fallback still has something to clear."""
    d, _ = daemon
    d._session_states["sess-1"] = {"pid": 4242, "state": "idle",
                                   "last_event": time.time()}

    async def nobody(pid, tty):
        return None

    monkeypatch.setattr(vscode_reveal, "close_terminal", nobody)
    ok, detail = await d._close_session_terminal("sess-1")
    assert ok is False
    assert "sess-1" in d._session_states
    assert "sess-1" not in d._finished


@pytest.mark.asyncio
async def test_close_helper_success_forgets_the_session_as_closed(daemon,
                                                                  monkeypatch):
    d, _ = daemon
    d._session_states["sess-1"] = {"pid": 4242, "state": "idle",
                                   "last_event": time.time()}
    d._active_notifications["sess-1"] = {"text": "needs you"}
    asked = []

    async def close(pid, tty):
        asked.append((pid, tty))
        return {"matched": True, "closed": True, "terminalName": "claude"}

    monkeypatch.setattr(vscode_reveal, "close_terminal", close)
    ok, detail = await d._close_session_terminal("sess-1")
    assert ok is True
    assert asked == [(4242, "")]
    assert "sess-1" not in d._session_states
    assert "sess-1" not in d._active_notifications
    assert d._finished["sess-1"]["end_reason"] == "closed"


@pytest.mark.asyncio
async def test_close_helper_stamps_a_grok_session_as_ended(daemon, monkeypatch):
    """Without the `_grok_ended` stamp, `_live_grok_records` would resurrect
    the row from the roster file the disposed tab's id is still listed in —
    `"closed"` belongs in GROK_END_REASONS because the tab is *gone*."""
    d, _ = daemon
    d._session_states["grok-1"] = {"pid": 4242, "state": "idle",
                                   "provider": "grok",
                                   "last_event": time.time()}
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, st: 4242)
    monkeypatch.setattr(daemon_mod, "_grok_session_pid_ok",
                        lambda pid: pid == 4242)

    async def close(pid, tty):
        return {"matched": True, "closed": True, "terminalName": "grok"}

    monkeypatch.setattr(vscode_reveal, "close_terminal", close)
    ok, _detail = await d._close_session_terminal("grok-1")
    assert ok is True
    assert "grok-1" in d._grok_ended
    assert d._finished["grok-1"]["end_reason"] == "closed"


@pytest.mark.asyncio
async def test_close_helper_aims_a_grok_session_at_the_tui_not_the_leader(
        daemon, monkeypatch):
    """The hook pid can be the shared `grok agent leader`; close must aim at
    the roster TUI, which is the process that actually owns the tab."""
    d, _ = daemon
    d._session_states["grok-1"] = {"pid": 111, "state": "idle",
                                   "provider": "grok",
                                   "last_event": time.time()}
    d._grok_records["grok-1"] = SimpleNamespace(pid=222)
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, st: 111)
    monkeypatch.setattr(daemon_mod, "_grok_session_pid_ok",
                        lambda pid: pid == 222)
    asked = []

    async def close(pid, tty):
        asked.append((pid, tty))
        return {"matched": True, "closed": True, "terminalName": "grok"}

    monkeypatch.setattr(vscode_reveal, "close_terminal", close)
    ok, detail = await d._close_session_terminal("grok-1")
    assert ok is True, detail
    assert asked == [(222, "")]


@pytest.mark.asyncio
async def test_close_helper_refuses_when_only_the_grok_leader_is_known(
        daemon, monkeypatch):
    """A leader pid has no tty. Aiming at it matches no tab, so refuse
    rather than pretend a close happened and forget the row."""
    d, _ = daemon
    d._session_states["grok-1"] = {"pid": 111, "state": "idle",
                                   "provider": "grok",
                                   "last_event": time.time()}
    monkeypatch.setattr(d, "_ensure_session_pid", lambda sid, st: 111)
    monkeypatch.setattr(daemon_mod, "_grok_session_pid_ok", lambda pid: False)

    async def boom(pid, tty):
        raise AssertionError(
            f"close_terminal was called with leader pid {pid}")

    monkeypatch.setattr(vscode_reveal, "close_terminal", boom)
    ok, detail = await d._close_session_terminal("grok-1")
    assert ok is False
    assert "no pid" in detail
    assert "grok-1" in d._session_states
    assert "grok-1" not in d._finished


# --- delete closes the bound terminal ----------------------------------------
#
# Sibling of the Done-leg close, not a modification of it. Unconditional:
# both preferences stay at their off defaults. A refused close is a log line
# and the tab left alone — never a `/clear`, never a board write after the
# card is gone.


@pytest.mark.asyncio
async def test_deleting_a_live_card_closes_its_terminal_with_preferences_off(
        daemon, monkeypatch):
    """Delete means cancelled: the bound terminal goes even though both
    Done-leg switches default off."""
    d, store = daemon
    assert d.board_close_terminal_enabled is False
    card = _live(store)
    closes = _close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == ["sess-1"]
    assert wraps == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_a_failed_delete_close_still_removes_the_card_and_never_clears(
        daemon, monkeypatch):
    """No pid / no 0.1.9+ window: the card is gone, the tab stays, nothing
    is typed, and there is no card left to write a `dispatch_error` onto."""
    d, store = daemon
    card = _live(store)
    closes = _close_spy(d, monkeypatch, result=(False, "no pid"))
    wraps = _wrap_up_spy(d, monkeypatch)
    orig = d._board_call
    methods = []

    async def tracked(method, *a, **k):
        methods.append(method)
        return await orig(method, *a, **k)

    monkeypatch.setattr(d, "_board_call", tracked)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == ["sess-1"]
    assert wraps == []
    assert store.get(card["id"]) is None
    # Two reads — before the lock, and again under it for the batch
    # judgement (`delete_card`) — and no write but the delete.
    assert methods == ["get", "get", "delete"]


@pytest.mark.asyncio
async def test_a_refused_delete_never_closes_a_terminal(daemon, monkeypatch):
    d, _store = daemon
    closes = _close_spy(d, monkeypatch)
    ok, detail = await d.delete_card("no-such-card")
    assert not ok
    assert "no such card" in detail
    assert closes == []


@pytest.mark.asyncio
async def test_delete_is_silent_when_the_link_is_not_live(daemon, monkeypatch):
    """`""` / `dispatching` / `ended` — nothing is bound, or the session is
    already gone. A session_id on the card is not enough."""
    d, store = daemon
    for state in ("", "dispatching", "ended"):
        card = _make(store)
        store.update(card["id"], {"session_id": "sess-1", "link_state": state})
        closes = _close_spy(d, monkeypatch)
        ok, detail = await d.delete_card(card["id"])
        assert ok, detail
        assert closes == [], state


@pytest.mark.asyncio
async def test_deleting_a_live_codex_card_closes_nothing(daemon, monkeypatch):
    d, store = daemon
    card = _live(store, tool="codex")
    closes = _close_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_deleting_a_done_card_closes_its_terminal(
        daemon, monkeypatch):
    """Delete of a finished card is the same cancel as any other column
    (2026-08-24). The declare_done-mid-report cost was shown and the
    delete gesture outranks it. Wrap-up is never the path."""
    d, store = daemon
    card = _live(store)
    store.update(card["id"], {"column_name": "done"})
    closes = _close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == ["sess-1"]
    assert wraps == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_deleting_a_done_codex_card_closes_nothing(daemon, monkeypatch):
    d, store = daemon
    card = _live(store, tool="codex")
    store.update(card["id"], {"column_name": "done"})
    closes = _close_spy(d, monkeypatch)
    pid_closes = _pid_close_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == []
    assert pid_closes == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ("backlog", "done"))
async def test_deleting_a_dispatching_card_closes_by_spawn_receipt(
        daemon, monkeypatch, column):
    """Dark Army opened this pid. Identity is the receipt, not a grok/claude
    process, so the session-close helper is not the path."""
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"link_state": "dispatching",
                              "column_name": column})
    d._spawn_shell_pids[str(card["id"])] = 4242
    closes = _close_spy(d, monkeypatch)
    pid_closes = _pid_close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert pid_closes == [4242], column
    assert closes == [], column
    assert wraps == [], column
    assert store.get(card["id"]) is None
    assert str(card["id"]) not in d._spawn_shell_pids
    assert card["id"] not in d._spawn_shell_pids


@pytest.mark.asyncio
async def test_delete_of_a_dispatching_card_keeps_the_receipt_across_a_bind_race(
        daemon, monkeypatch):
    """Bind pops `_spawn_shell_pids` while delete awaits the store. The
    handle is captured before that await, so the tab still closes."""
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"link_state": "dispatching"})
    d._spawn_shell_pids[str(card["id"])] = 4242
    closes = _close_spy(d, monkeypatch)
    pid_closes = _pid_close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    orig = d._board_call

    async def steal(method, *a, **k):
        if method in ("get", "delete"):
            d._spawn_shell_pids.pop(str(card["id"]), None)
            d._spawn_shell_pids.pop(card["id"], None)
        return await orig(method, *a, **k)

    monkeypatch.setattr(d, "_board_call", steal)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert pid_closes == [4242]
    assert closes == []
    assert wraps == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_deleting_a_dispatching_codex_card_closes_nothing_even_with_a_receipt(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store, tool="codex")
    store.update(card["id"], {"link_state": "dispatching"})
    d._spawn_shell_pids[str(card["id"])] = 4242
    closes = _close_spy(d, monkeypatch)
    pid_closes = _pid_close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == []
    assert pid_closes == []
    assert wraps == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_deleting_a_dispatching_done_card_without_a_receipt_closes_nothing(
        daemon, monkeypatch):
    """No handle, no close — the old Done+dispatching pin, now scoped to
    the missing receipt rather than the column."""
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"link_state": "dispatching",
                              "column_name": "done"})
    closes = _close_spy(d, monkeypatch)
    pid_closes = _pid_close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == []
    assert pid_closes == []
    assert wraps == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_deleting_a_refining_card_closes_the_interview_terminal(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store, column_name="prep")
    store.update(card["id"], {
        "refine_state": "live", "refine_session_id": "rsid"})
    closes = _close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == ["rsid"]
    assert wraps == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_deleting_a_codex_card_skips_a_codex_refine_session(
        daemon, monkeypatch):
    """A Codex-chipped card's linked session is skipped, and a Codex
    refine session is skipped for the same structural reason, not
    because Refine is secretly Claude."""
    d, store = daemon
    card = _make(store, column_name="prep", tool="codex")
    store.update(card["id"], {
        "refine_state": "live", "refine_session_id": "rsid"})
    d._codex_records["rsid"] = object()
    closes = _close_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == []
    assert store.get(card["id"]) is None


@pytest.mark.asyncio
async def test_deleting_a_card_with_both_sessions_live_closes_each(
        daemon, monkeypatch):
    """The loop, not a first-match: a live link and a live refine are two
    terminals that exist only for this card."""
    d, store = daemon
    card = _live(store)
    store.update(card["id"], {
        "refine_state": "live", "refine_session_id": "rsid"})
    closes = _close_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == ["sess-1", "rsid"]


@pytest.mark.asyncio
async def test_an_open_permission_prompt_does_not_block_the_delete_close(
        daemon, monkeypatch):
    """`dispose()` types nothing, so the wrap-up's prompt guard does not
    apply: skipping would leave a terminal asking about cancelled work."""
    d, store = daemon
    card = _live(store)
    d._permission_requests = {
        "r1": {"request_id": "r1", "session_id": "sess-1", "port": 51000,
               "asked_at": time.time(), "tool_name": "Bash"},
    }
    monkeypatch.setattr(d, "_prompts_by_session", lambda: {"sess-1": {}})
    closes = _close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    ok, detail = await d.delete_card(card["id"])
    assert ok, detail
    assert closes == ["sess-1"]
    assert wraps == []


@pytest.mark.asyncio
async def test_clearing_done_closes_no_terminals(daemon, monkeypatch):
    """Bulk clear is a different gesture from deleting one card: it
    removes finished records and closes no terminals. Delete of a single
    Done card is the opposite — that is the cancel."""
    d, store = daemon
    card = _live(store)
    store.update(card["id"], {"column_name": "done"})
    count, token = store.done_scope()
    closes = _close_spy(d, monkeypatch)
    wraps = _wrap_up_spy(d, monkeypatch)
    ok, deleted, detail = await d.clear_done_cards(count, token)
    assert ok, detail
    assert deleted == 1
    assert closes == []
    assert wraps == []
    assert store.get(card["id"]) is None


# --- the refinement: refine_prompt and refine_guard ---------------------------


def _refine_guard(card, roots=("/tmp",), in_flight=(), now=1000.0,
                  last_attempt=None):
    return dispatch.refine_guard(card, roots=roots, in_flight=in_flight,
                                 now=now, last_attempt=last_attempt)


def _prep_card(**kw):
    fields = {"column_name": "prep", "plan_path": "", "refine_state": ""}
    fields.update(kw)
    return _card(**fields)


def test_prep_is_a_startable_column():
    """The plan gate is a confirmation, not a wall: a confirmed unplanned
    Start from Prep must not dead-end at the column check."""
    assert "prep" in dispatch._STARTABLE_COLUMNS
    ok, detail = _guard(_prep_card())
    assert ok, detail


def test_refine_guard_passes_the_happy_case():
    ok, detail = _refine_guard(_prep_card())
    assert ok, detail


def test_refine_guard_refuses_a_tool_less_card():
    ok, detail = _refine_guard(_prep_card(tool=""))
    assert not ok
    assert "which assistant" in detail


def test_refine_guard_refuses_an_unknown_tool():
    ok, detail = _refine_guard(_prep_card(tool="eliza"))
    assert not ok
    assert "cannot start eliza" in detail


def test_refine_guard_refuses_an_unsupported_tool(monkeypatch):
    monkeypatch.setitem(dispatch._UNSUPPORTED, "codex", "not supported yet")
    ok, detail = _refine_guard(_prep_card(tool="codex"))
    assert not ok
    assert "not supported yet" in detail


def test_refine_guard_refuses_a_card_outside_prep():
    for column in ("backlog", "in_progress", "done"):
        ok, detail = _refine_guard(_prep_card(column_name=column))
        assert not ok, column
        assert "only a card in Prep" in detail


def test_refine_guard_refuses_a_card_that_already_has_a_plan():
    ok, detail = _refine_guard(_prep_card(plan_path="/tmp/plans/x.md"))
    assert not ok
    assert "already has a plan" in detail


def test_refine_guard_refuses_a_session_bound_card():
    ok, detail = _refine_guard(_prep_card(session_id="sess-1"))
    assert not ok
    assert "already being worked on" in detail


def test_refine_guard_refuses_a_card_already_refining():
    for state in ("dispatching", "live"):
        ok, detail = _refine_guard(_prep_card(refine_state=state))
        assert not ok, state
        assert "already being refined" in detail
    # `ended` is a refinement that ran and produced nothing — Refine again.
    ok, detail = _refine_guard(_prep_card(refine_state="ended"))
    assert ok, detail


def test_refine_guard_refuses_a_card_that_is_dispatching():
    ok, detail = _refine_guard(_prep_card(link_state="dispatching"))
    assert not ok
    assert "already starting" in detail


def test_the_guard_refuses_a_card_whose_refinement_is_binding():
    """The other direction of the mutual exclusion. The shared in-flight list
    covers other cards; the card's own id is excluded from `pending`, so
    without this refusal a confirmed Start during the refine bind window would
    race two new claude sessions in one project for one row."""
    ok, detail = _guard(_prep_card(refine_state="dispatching"))
    assert not ok
    assert "being refined" in detail


def test_the_guard_refuses_a_card_whose_refinement_is_live():
    """`refine_guard` refuses both states and so must this one: a Prep card
    mid-interview dragged into In progress and confirmed unplanned would
    dispatch a second claude into the same project while the planner runs,
    and the planner's eventual `dark_army_attach_plan` would then be refused
    ("not in Prep"). The Refine button is withheld in exactly this state
    (`BoardCardView.canRefine`), so guard and panel refuse together."""
    ok, detail = _guard(_prep_card(refine_state="live"))
    assert not ok
    assert "being refined" in detail
    # `ended` is a refinement that ran and produced nothing — startable
    # (unplanned, so through the confirmed press), same as `refine_guard`'s
    # own admission of it.
    ok, detail = _guard(_prep_card(refine_state="ended"))
    assert ok, detail


def test_refine_guard_honours_the_cooldown():
    ok, detail = _refine_guard(_prep_card(), now=1000.0, last_attempt=995.0)
    assert not ok
    assert "just started" in detail


def test_refine_guard_shares_the_machine_wide_bound():
    in_flight = [_card(id="a", project="p1"), _card(id="b", project="p2")]
    ok, detail = _refine_guard(_prep_card(id="c", project="p3"),
                               in_flight=in_flight)
    assert not ok
    assert "already starting" in detail


def test_refine_guard_shares_the_per_project_bound():
    """A refinement is a new claude session in the card's project, so it and a
    dispatch draw on one budget — binding by elimination only works while "the
    first new session in this project" is a single session."""
    ok, detail = _refine_guard(_prep_card(id="c"),
                               in_flight=[_card(id="a", project="bob")])
    assert not ok
    assert "another card in this project" in detail


def test_refine_guard_refuses_an_unknown_root():
    ok, detail = _refine_guard(_prep_card(root="/tmp"),
                               roots=("/Users/somebody/other",))
    assert not ok
    assert "no open window" in detail
    ok, detail = _refine_guard(_prep_card(root=""))
    assert not ok
    assert "which folder" in detail


def _consult_guard(card, roots=("/tmp",), in_flight=(), now=1000.0,
                   last_attempt=None, question="is it done?"):
    return dispatch.consult_guard(
        card, roots=roots, in_flight=in_flight, now=now,
        last_attempt=last_attempt, question=question)


def test_consult_guard_passes_the_happy_case():
    ok, detail = _consult_guard(_card())
    assert ok, detail


def test_consult_guard_admits_a_done_card_and_a_bound_card():
    """A Done or in-progress card may be asked about — unlike Start."""
    ok, detail = _consult_guard(_card(column_name="done"))
    assert ok, detail
    ok, detail = _consult_guard(_card(session_id="sess-1",
                                      link_state="live"))
    assert ok, detail


def test_consult_guard_refuses_an_unknown_root():
    ok, detail = _consult_guard(_card(root="/nowhere-real"),
                                roots=("/Users/somebody/other",))
    assert not ok
    assert "no open window" in detail
    ok, detail = _consult_guard(_card(root=""))
    assert not ok
    assert "which folder" in detail


def test_consult_guard_honours_the_machine_wide_bound():
    in_flight = [_card(id="a", project="p1"), _card(id="b", project="p2")]
    ok, detail = _consult_guard(_card(id="c", project="p3"),
                                in_flight=in_flight)
    assert not ok
    assert "already starting" in detail


def test_consult_guard_refuses_a_dispatching_card_in_the_same_project():
    ok, detail = _consult_guard(
        _card(id="c"),
        in_flight=[_card(id="a", project="bob", link_state="dispatching")])
    assert not ok
    assert "another card in this project" in detail


def test_consult_guard_refuses_a_pending_consult_in_the_same_project():
    ok, detail = _consult_guard(
        _card(id="c"),
        in_flight=[{"id": "consult:other", "project": "bob"}])
    assert not ok
    assert "another card in this project" in detail


def test_consult_guard_refuses_a_second_consult_on_the_same_card():
    ok, detail = _consult_guard(
        _card(id="c"),
        in_flight=[{"id": "consult:c", "project": "bob"}])
    assert not ok
    assert "already looking" in detail


def test_consult_brief_starts_with_the_fixed_preamble_and_passes_prompt_refusal():
    brief = dispatch.consult_brief(
        {"title": "t", "summary": "s", "prompt": "-looks-like-a-flag"},
        "-also-a-flag?")
    assert brief.startswith(dispatch.CONSULT_PREAMBLE.rstrip())
    assert "dark_army_answer_card" in brief
    assert "bob_answer_card" not in brief
    assert "Change no files" in brief
    assert dispatch.prompt_refusal("claude", brief) is None


def test_refine_prompt_is_the_ship_line_and_passes_the_prompt_check():
    prompt = dispatch.refine_prompt(
        {"summary": "add a thing", "title": "t"})
    assert prompt.startswith("/ship add a thing")
    assert "Title: t" in prompt
    assert dispatch.prompt_refusal("claude", prompt) is None
    # The title is the fallback when nobody wrote a summary.
    assert dispatch.refine_prompt({"summary": "", "title": "fix the strip"}) \
        == "/ship fix the strip"
    assert dispatch.prompt_refusal("claude", "/ship add a prep column") is None


def test_refine_prompt_carries_the_card_instructions():
    """The summary is the /ship idea; the stored prompt is the brief already
    on the card. Dropping it was how Refine spawned a planner that had never
    seen the instructions."""
    prompt = dispatch.refine_prompt({
        "title": "[R02] Unify the list",
        "summary": "add the missing decisions",
        "prompt": "Roadmap: docs/audit.md\nSuccess criterion: same count.",
    })
    assert prompt.startswith("/ship add the missing decisions")
    assert "Title: [R02] Unify the list" in prompt
    assert "Instructions:\nRoadmap: docs/audit.md" in prompt
    assert "Success criterion: same count." in prompt
    for tool in ("claude", "grok", "codex"):
        assert dispatch.prompt_refusal(tool, prompt) is None


def test_refine_prompt_does_not_repeat_the_idea_as_instructions():
    idea = "add a thing"
    assert dispatch.refine_prompt(
        {"summary": idea, "title": idea, "prompt": idea}) == "/ship " + idea
    assert dispatch.refine_prompt(
        {"summary": idea, "title": idea, "prompt": ""}) == "/ship " + idea
    # A leading dash in the instructions is body text, not a CLI flag:
    # the whole string still starts with /ship.
    dashed = dispatch.refine_prompt({
        "summary": "add a thing", "title": "t",
        "prompt": "-looks-like-a-flag but is the brief",
    })
    assert dashed.startswith("/ship add a thing")
    assert "-looks-like-a-flag" in dashed
    assert dispatch.prompt_refusal("claude", dashed) is None
    assert dispatch.prompt_refusal("grok", dashed) is None
    assert dispatch.prompt_refusal("codex", dashed) is None


def test_start_prompt_is_implement_when_the_card_has_a_plan():
    """`attach_plan` writes `plan_path` and leaves `prompt` as the leftover
    idea. Start must not hand that idea to the CLI — that is how a planned
    Grok card re-planned this work on 24 Aug 2026."""
    path = "/tmp/plans/reorder.md"
    leftover = ("Enable users to reorder items within the pipeline queue "
                "through drag-and-drop or similar interaction, persisting "
                "the new order.")
    prompt = dispatch.start_prompt({"plan_path": path, "prompt": leftover})
    assert prompt.startswith(f"Plan: {path}")
    assert f"/ship implement {path}" in prompt
    assert leftover not in prompt
    assert dispatch.prompt_refusal("grok", prompt) is None
    assert dispatch.prompt_refusal("claude", prompt) is None


def test_start_prompt_is_the_card_prompt_when_there_is_no_plan():
    assert dispatch.start_prompt({"prompt": "go", "plan_path": ""}) == "go"
    assert dispatch.start_prompt({"prompt": "go"}) == "go"
    assert dispatch.start_prompt({}) == ""
    assert dispatch.start_prompt(None) == ""


def test_a_planned_card_is_not_refused_for_its_leftover_prompt():
    """The leftover idea is not what the CLI will see, so a one-word
    subcommand sitting in `prompt` must not wall a planned Start."""
    ok, detail = _guard(_card(tool="claude", prompt="mcp",
                              plan_path="/tmp/plans/x.md"))
    assert ok, detail


# --- the terminal receipt -----------------------------------------------------


@pytest.mark.asyncio
async def test_an_accepted_spawn_records_the_shell_pid(daemon, monkeypatch):
    """The receipt the bind proves descent against: kept only when the window
    reported one, memory-only like `_dispatch_baseline`."""
    d, store = daemon
    card = _make(store)
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        return True, "bob", 4242

    monkeypatch.setattr(dispatch, "spawn", accept)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert d._spawn_shell_pids[card["id"]] == 4242


@pytest.mark.asyncio
async def test_a_spawn_without_a_receipt_forgets_any_stale_one(daemon, monkeypatch):
    """A pid remembered from an earlier press must not prove the wrong
    terminal: a spawn reply without `shellPid` pops the entry rather than
    leaving it."""
    d, store = daemon
    card = _make(store)
    d._spawn_shell_pids[card["id"]] = 111
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def accept(root, argv, name, **_kw):
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert card["id"] not in d._spawn_shell_pids


@pytest.mark.asyncio
async def test_reset_and_delete_forget_the_shell_pid(daemon):
    """Both recovery verbs already pop the baselines; the receipt rides the
    same bookkeeping or a re-created card id could inherit a dead terminal."""
    d, store = daemon
    first = _make(store)
    second = _make(store, title="the other one")
    d._spawn_shell_pids[first["id"]] = 111
    d._spawn_shell_pids[second["id"]] = 222

    got, detail = await d.reset_card(first["id"], {"column_name": "backlog"})
    assert got is not None, detail
    assert first["id"] not in d._spawn_shell_pids

    ok, detail = await d.delete_card(second["id"])
    assert ok, detail
    assert second["id"] not in d._spawn_shell_pids


# ── an unenrolled project cannot make Dark Army open a terminal ─────────────────────

@pytest.mark.asyncio
async def test_dispatch_re_checks_enrolment_at_the_moment_of_the_press(
        enforce_enrolment, tmp_path, monkeypatch):
    """A re-check rather than a snapshot read: a card armed while its project
    was enrolled must not start after it was un-enrolled."""
    from dark_army_daemon import dispatch as dmod
    from dark_army_daemon import enrollment
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon

    root = tmp_path / "proj"
    root.mkdir()
    assert enrollment.enroll(str(root))[0]
    real_root = os.path.realpath(str(root))

    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        monkeypatch.setattr(daemon, "_known_project_roots",
                            lambda: {real_root})
        monkeypatch.setattr(dmod, "resolve_executable",
                            lambda tool: "/bin/claude")
        opened = []

        async def accept(root_, argv, name, **_kw):
            opened.append(name)
            return True, "bob", None

        monkeypatch.setattr(dmod, "spawn", accept)

        card, detail = store.create({
            "title": "work", "project": "proj", "root": real_root,
            "prompt": "go", "tool": "claude", "column_name": "backlog"})
        assert card is not None, detail

        ok, _ = await daemon.dispatch_card(card["id"], allow_unplanned=True)
        assert ok and opened == ["work"]

        # Put the card back and take the project away.
        store.update(card["id"], {"session_id": "", "link_state": "",
                                  "column_name": "backlog"})
        enrollment.unenroll(real_root)
        ok, detail = await daemon.dispatch_card(card["id"],
                                                allow_unplanned=True)
        assert not ok
        assert "not enrolled" in detail
        assert opened == ["work"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_refine_re_checks_enrolment_too(enforce_enrolment, tmp_path,
                                              monkeypatch):
    from dark_army_daemon import dispatch as dmod
    from dark_army_daemon.board import BoardStore
    from dark_army_daemon.daemon import BobDaemon

    root = tmp_path / "unenrolled"
    root.mkdir()
    daemon = BobDaemon(sessions_path=tmp_path / "sessions.json")
    store = BoardStore(tmp_path / "board.db")
    store.connect()
    daemon._board = store
    try:
        monkeypatch.setattr(daemon, "_known_project_roots",
                            lambda: {os.path.realpath(str(root))})
        monkeypatch.setattr(dmod, "resolve_executable",
                            lambda tool: "/bin/claude")

        async def refuse(root_, argv, name, **_kw):
            raise AssertionError("nothing may be spawned")

        monkeypatch.setattr(dmod, "spawn", refuse)
        card, _ = store.create({
            "title": "idea", "project": "unenrolled",
            "root": os.path.realpath(str(root)),
            "prompt": "go", "tool": "claude", "column_name": "prep"})
        ok, detail = await daemon.refine_card(card["id"])
        assert not ok and "not enrolled" in detail
    finally:
        store.close()


# --- the model flag ----------------------------------------------------------


def test_gpt_6_family_extends_the_codex_catalogue_once_in_order():
    assert dispatch.MODELS["codex"] == (
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5")
    for model in ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna"):
        assert dispatch.argv_for("codex", "codex", "do it", model=model) == [
            "codex", "--model", model, "--", "do it"]


@pytest.mark.parametrize("available", [True, False])
def test_gpt_6_catalogue_is_published_even_without_a_store(daemon, available):
    d, store = daemon
    if available:
        _make(store, tool="codex", model="gpt-6-astra")
    else:
        d._board = None
    snapshot = d._build_board_state()
    assert snapshot["available"] is available
    assert snapshot["models"]["codex"] == [
        "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
    if available:
        assert snapshot["cards"][0]["model"] == "gpt-6-astra"


@pytest.mark.asyncio
@pytest.mark.parametrize("own_terminal", [False, True])
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", ""])
async def test_codex_start_forwards_current_models_or_default_to_the_spawner(
        daemon, monkeypatch, own_terminal, model):
    d, store = daemon
    card = _make(store, tool="codex", model=model, prompt="do it")
    d.board_own_terminal_enabled = own_terminal
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "codex")
    calls = []

    async def local(root, argv, name, **_kw):
        calls.append((True, argv))
        return True, "agent", None

    async def editor(root, argv, name, **_kw):
        calls.append((False, argv))
        return True, "agent", None

    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "spawn", editor)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    expected_model = model or "gpt-6-sol"
    expected = ["codex", "--model", expected_model, "--", "do it"]
    assert calls == [(own_terminal, expected)]
    assert store.get(card["id"])["model"] == model


def test_grok_4_7_extends_the_grok_catalogue_once_in_order():
    """Newest pin first. Card pickers on the Mac and the phone, and the
    Agent models chips, all read this tuple — none of them keeps a copy."""
    assert dispatch.MODELS["grok"] == ("grok-4.7", "grok-4.6", "grok-4.5")
    assert dispatch.argv_for("grok", "grok", "do it", model="grok-4.7") == [
        "grok", "--model", "grok-4.7", "--", "do it"]


@pytest.mark.parametrize("available", [True, False])
def test_grok_4_7_catalogue_is_published_even_without_a_store(daemon, available):
    """The board snapshot is what both card pickers draw. It is present
    with the board shut, same as the Astra catalogue."""
    d, store = daemon
    if available:
        _make(store, tool="grok", model="grok-4.7")
    else:
        d._board = None
    snapshot = d._build_board_state()
    assert snapshot["available"] is available
    assert snapshot["models"]["grok"] == ["grok-4.7", "grok-4.6", "grok-4.5"]
    if available:
        assert snapshot["cards"][0]["model"] == "grok-4.7"


def test_a_default_card_launches_exactly_as_it_did_before():
    """Empty model means Default, and Default means *no flag at all* — the argv
    is byte for byte what it was before per-card models existed."""
    assert dispatch.argv_for("claude", "claude", "do it") == ["claude", "do it"]
    assert dispatch.argv_for("codex", "codex", "do it") == [
        "codex", "--", "do it"]
    assert dispatch.argv_for("grok", "grok", "do it") == ["grok", "--", "do it"]


def test_the_model_flag_sits_before_the_separator():
    """After `--` it would be prompt text and the session would silently run on
    the wrong model — which no eyeball catches, so it is pinned exactly."""
    assert dispatch.argv_for("claude", "claude", "do it", model="opus") == [
        "claude", "--model", "opus", "do it"]
    assert dispatch.argv_for("codex", "codex", "do it",
                             model="gpt-5.6-sol") == [
        "codex", "--model", "gpt-5.6-sol", "--", "do it"]
    assert dispatch.argv_for("grok", "grok", "do it", model="grok-4.5") == [
        "grok", "--model", "grok-4.5", "--", "do it"]


def test_the_prompt_is_still_the_last_element_with_a_model():
    for tool in ("claude", "codex", "grok"):
        model = dispatch.MODELS[tool][0]
        argv = dispatch.argv_for(tool, tool, "the prompt", model=model)
        assert argv[-1] == "the prompt"


def test_the_fable_pins_are_on_the_menu():
    """Fable was alias-only; the two releases are pinnable by exact id, in the
    family's alias-then-pins-newest-first shape (after "fable", 5-1 before 5)."""
    t = dispatch.MODELS["claude"]
    assert "claude-fable-5-1" in t
    assert "claude-fable-5" in t
    assert t.index("fable") < t.index("claude-fable-5-1") < t.index(
        "claude-fable-5")


def test_the_opus_5_5_pins_are_on_the_menu():
    """Opus 5.5 is pinnable by exact id and as its 1M-context variant, newest
    first in the Opus family: after "opus", before Opus 5."""
    t = dispatch.MODELS["claude"]
    assert t.index("opus") < t.index("claude-opus-5-5") < t.index(
        "claude-opus-5-5[1m]") < t.index("claude-opus-5")
    assert dispatch.argv_for("claude", "claude", "do it",
                             model="claude-opus-5-5") == [
        "claude", "--model", "claude-opus-5-5", "do it"]


def test_the_sonnet_5_5_pins_are_on_the_menu():
    """Sonnet 5.5 is pinnable by exact id and as its 1M-context variant,
    newest first in the Sonnet family: after "sonnet", before Sonnet 5."""
    t = dispatch.MODELS["claude"]
    assert t.index("sonnet") < t.index("claude-sonnet-5-5") < t.index(
        "claude-sonnet-5-5[1m]") < t.index("claude-sonnet-5")
    assert dispatch.argv_for("claude", "claude", "do it",
                             model="claude-sonnet-5-5") == [
        "claude", "--model", "claude-sonnet-5-5", "do it"]


def test_a_fable_pin_rides_the_argv_verbatim():
    """A card pinned to Fable 5.1 starts on exactly that id, not the family's
    latest — pinned byte for byte like the other model-flag tests."""
    assert dispatch.argv_for("claude", "claude", "do it",
                             model="claude-fable-5-1") == [
        "claude", "--model", "claude-fable-5-1", "do it"]
    assert dispatch.argv_for("claude", "claude", "do it",
                             model="claude-fable-5") == [
        "claude", "--model", "claude-fable-5", "do it"]


def test_an_unknown_tool_ignores_the_model():
    assert dispatch.argv_for("eliza", "eliza", "hi", model="opus") == [
        "eliza", "hi"]


def test_every_tool_with_models_is_one_bob_can_launch():
    assert set(dispatch.MODELS) <= set(dispatch._EXECUTABLES)


def _model_card(**extra):
    card = {"id": "c1", "column_name": "backlog", "tool": "claude",
            "prompt": "do the thing", "project": "p", "root": "/tmp"}
    card.update(extra)
    return card


def _model_guard(card):
    return dispatch.guard(card, roots=["/tmp"], in_flight=[], now=1000.0)


def test_guard_refuses_a_model_from_another_provider():
    ok, detail = _model_guard(_model_card(model="grok-4.6"))
    assert ok is False
    assert detail == "this card names a model Dark Army does not offer for claude"


def test_guard_refuses_a_model_that_does_not_exist():
    ok, detail = _model_guard(_model_card(model="banana"))
    assert ok is False
    assert "does not offer" in detail


def test_guard_accepts_default_and_a_row_with_no_model_key():
    ok, _ = _model_guard(_model_card(model=""))
    assert ok is True
    # An older DB row has no `model` key at all — the pre-v13 shape.
    older = _model_card()
    older.pop("model", None)
    ok, _ = _model_guard(older)
    assert ok is True


def test_guard_accepts_each_shipped_model_for_its_own_tool():
    for tool, models in dispatch.MODELS.items():
        for model in models:
            ok, detail = _model_guard(_model_card(tool=tool, model=model))
            assert ok is True, (tool, model, detail)


# --- Dark Army's own terminal: `spawn_local` behind the preference ------------------


@pytest.mark.asyncio
async def test_spawn_local_is_selected_only_when_the_preference_is_on(daemon, monkeypatch):
    """Same guard, same plan gate, same slot rule on both paths; only the
    spawner differs, and only the preference chooses it."""
    d, store = daemon
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    calls = []

    async def local(root, argv, name, **_kw):
        calls.append(("local", root, argv))
        return True, "agent", 4242

    async def editor(root, argv, name, **_kw):
        calls.append(("editor", root, argv))
        return True, "zsh", None

    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "spawn", editor)

    card = _make(store)
    assert d.board_own_terminal_enabled is False
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert calls[-1][0] == "editor"
    assert card["id"] not in d._spawn_pty_pids

    other = _make(store, project="other", root="/private/tmp")
    d.board_own_terminal_enabled = True
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(other["id"], allow_unplanned=True)
    assert ok, detail
    assert calls[-1][0] == "local"
    # The child pid is an identity in its own map, never a shell receipt.
    assert d._spawn_pty_pids[other["id"]] == 4242
    assert other["id"] not in d._spawn_shell_pids
    got = store.get(other["id"])
    assert got["column_name"] == "in_progress" and got["link_state"] == "dispatching"


@pytest.mark.asyncio
async def test_the_guard_runs_identically_on_the_pty_path(daemon, monkeypatch):
    d, store = daemon
    d.board_own_terminal_enabled = True
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")

    async def boom(*a, **k):
        raise AssertionError("spawned past a refusal")
    monkeypatch.setattr(dispatch, "spawn_local", boom)
    monkeypatch.setattr(dispatch, "spawn", boom)
    card = _make(store, prompt="-please")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert not ok
    assert "dash" in detail or "-" in detail


@pytest.mark.asyncio
async def test_refinements_follow_the_own_terminal_preference(daemon, monkeypatch):
    """Refine opens where Start opens: `board_own_terminal` decides both.

    It used to take `dispatch.spawn` unconditionally, so a machine set to
    Dark Army's own terminals still had every plan land in a VS Code window.
    """
    d, store = daemon
    d.board_own_terminal_enabled = True
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    local_spawns, editor_spawns = [], []

    async def local(root, argv, name, **_kw):
        local_spawns.append(argv)
        return True, name, 4242

    async def editor(root, argv, name, **_kw):
        editor_spawns.append(argv)
        return True, "zsh", 99
    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "spawn", editor)

    card = _make(store, column_name="prep")
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert len(local_spawns) == 1 and editor_spawns == []
    # The child pid is an identity in its own map, never a shell receipt.
    assert d._spawn_pty_pids[card["id"]] == 4242
    assert card["id"] not in d._spawn_shell_pids

    # Preference off: the editor spawn, exactly as before. A second card, in
    # its own project — one launch per project is in flight at a time.
    d.board_own_terminal_enabled = False
    other = _make(store, column_name="prep", root="/private/tmp",
                  project="tmp2")
    ok, detail = await d.refine_card(other["id"])
    assert ok, detail
    assert len(editor_spawns) == 1 and len(local_spawns) == 1
    assert d._spawn_shell_pids[other["id"]] == 99
    assert other["id"] not in d._spawn_pty_pids


@pytest.mark.asyncio
async def test_spawn_local_starts_nothing_itself_and_reports_the_host(monkeypatch, tmp_path):
    """`ptyhost.start` is the one process starter; `dispatch.py` still has
    no `subprocess` (`_launcher_code` pins that above)."""
    from dark_army_daemon import ptyhost
    seen = []

    class Host:
        async def start(self, root, argv, name):
            seen.append((root, argv, name))
            return True, name, 77
    monkeypatch.setattr(ptyhost, "current", lambda: Host())
    assert await dispatch.spawn_local(str(tmp_path), ["/bin/claude", "go"], "n") == (True, "n", 77)
    assert seen == [(str(tmp_path), ["/bin/claude", "go"], "n")]
    monkeypatch.setattr(ptyhost, "current", lambda: None)
    ok, detail, pid = await dispatch.spawn_local(str(tmp_path), ["/bin/claude"], "n")
    assert (ok, pid) == (False, None) and "terminal host" in detail
    assert await dispatch.spawn_local(str(tmp_path), [], "n") == (False, "nothing to start", None)


def test_a_pty_card_binds_by_pid_identity(daemon, monkeypatch):
    """Dark Army is the parent and holds the child pid exactly: the row whose pid
    is (or descends from) it binds; any other row is unproven."""
    from dark_army_daemon import daemon as dm
    d, store = daemon
    card = _make(store, column_name="in_progress")
    store.update(card["id"], {"link_state": "dispatching", "dispatched_at": 100.0}, bump=False)
    card = store.get(card["id"])
    d._spawn_pty_pids[card["id"]] = 4242
    monkeypatch.setattr(dm, "_pid_descends", lambda pid, anc: pid == anc)
    row = {"session_id": "new", "provider": "claude", "project": "bob", "cwd": "/tmp",
           "kind": "interactive", "started_at": 101.0, "pid": 9999}
    snap = {"running": [row], "waiting": [], "sleeping": []}
    assert d._bind_dispatched_card(card, snap, now=110.0) is False
    row["pid"] = 4242
    assert d._bind_dispatched_card(card, snap, now=110.0) is True
    assert store.get(card["id"])["session_id"] == "new"
    assert card["id"] not in d._spawn_pty_pids


def test_a_card_past_its_window_gives_up_rather_than_binding(daemon):
    """Late means give up, never bind.

    The scan is elimination — "the first new session in this project" — so a
    pass that arrives long after the press (a slept machine, a reconcile that
    stopped and was restarted) would otherwise pair the card with whatever
    terminal somebody happened to open since. The window is therefore asked
    before the candidates, not after them."""
    d, store = daemon
    card = _make(store, column_name="in_progress")
    store.update(card["id"], {"link_state": "dispatching", "dispatched_at": 100.0},
                 bump=False)
    card = store.get(card["id"])
    row = {"session_id": "somebody-else", "provider": "claude", "project": "bob",
           "cwd": "/tmp", "kind": "interactive", "started_at": 5000.0}
    snap = {"running": [row], "waiting": [], "sleeping": []}
    assert d._bind_dispatched_card(
        card, snap, now=100.0 + dispatch.DISPATCH_BIND_WINDOW + 1) is True
    after = store.get(card["id"])
    assert after["session_id"] == ""
    assert after["link_state"] == ""
    assert after["dispatch_error"]


@pytest.mark.parametrize("tool, name", [("codex", "Codex"), ("grok", "Grok")])
def test_a_codex_or_grok_give_up_names_the_trust_question(daemon, tool, name):
    """Codex 0.156 stops on "Trust this folder?" before any session exists
    (the vir-sunset stall of 24 Sep 2026). The give-up line says so and says
    what to press, rather than a bare "no session appeared"."""
    d, store = daemon
    card = _make(store, column_name="in_progress", tool=tool)
    store.update(card["id"], {"link_state": "dispatching", "dispatched_at": 100.0},
                 bump=False)
    assert d._bind_dispatched_card(
        store.get(card["id"]), {"running": []},
        now=100.0 + dispatch.DISPATCH_BIND_WINDOW + 1) is True
    error = store.get(card["id"])["dispatch_error"]
    assert error.startswith("no session appeared")
    assert f"{name} may be asking to trust this folder" in error
    assert error.endswith("press Start again")


def test_first_run_hint_is_only_for_codex_and_grok():
    assert dispatch.first_run_hint("claude") == ""
    assert dispatch.first_run_hint("") == ""
    assert dispatch.first_run_hint(None) == ""
    assert dispatch.first_run_hint("codex", "Refine").endswith("press Refine again")


@pytest.mark.asyncio
async def test_own_terminal_starts_in_an_enrolled_project_with_no_editor_open(daemon, monkeypatch, tmp_path):
    """Gap: Start with the editor closed was still refused. With the
    preference on, the known roots widen to the enrolment ledger — the card's
    root must still be an exact, real, enrolled directory, and the enrolment
    gate itself gains no "Dark Army started it" bypass. Off, it is refused exactly
    as today."""
    from dark_army_daemon import enrollment
    d, store = daemon
    # No VS Code window, no live session anywhere.
    monkeypatch.setattr(d, "_known_project_roots", lambda: set())
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: {"/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    calls = []

    async def local(root, argv, name, **_kw):
        calls.append(("local", root))
        return True, "agent", 4242

    async def editor(root, argv, name, **_kw):
        calls.append(("editor", root))
        return True, "zsh", None
    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "spawn", editor)

    card = _make(store)
    assert d.board_own_terminal_enabled is False
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert not ok
    assert calls == []

    d.board_own_terminal_enabled = True
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert len(calls) == 1 and calls[0][0] == "local"
    assert dispatch.normalise_root(calls[0][1]) == dispatch.normalise_root("/tmp")

    # An unenrolled root is still refused with the preference on: the ledger
    # is the widening, not a wildcard.
    other = _make(store, project="elsewhere", root=str(tmp_path))
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: set())
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(other["id"], allow_unplanned=True)
    assert not ok
    assert calls[-1][0] == "local" and len(calls) == 1


@pytest.mark.asyncio
async def test_a_press_can_spawn_locally_with_the_preference_off(
        daemon, monkeypatch):
    """START HERE: a card with no connected terminal asks for Dark Army's own
    pty, even while the machine-wide switch is off. Ordinary Start still
    takes the editor."""
    d, store = daemon
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    calls = []

    async def local(root, argv, name, **_kw):
        calls.append(("local", root, argv))
        return True, "agent", 4242

    async def editor(root, argv, name, **_kw):
        calls.append(("editor", root, argv))
        return True, "zsh", None

    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "spawn", editor)

    card = _make(store)
    assert d.board_own_terminal_enabled is False
    ok, detail = await d.dispatch_card(
        card["id"], allow_unplanned=True, own_terminal=True)
    assert ok, detail
    assert calls[-1][0] == "local"
    assert d._spawn_pty_pids[card["id"]] == 4242
    assert card["id"] not in d._spawn_shell_pids

    other = _make(store, project="other", root="/private/tmp")
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(other["id"], allow_unplanned=True)
    assert ok, detail
    assert calls[-1][0] == "editor"
    assert other["id"] not in d._spawn_pty_pids


@pytest.mark.asyncio
async def test_own_terminal_on_a_press_widens_to_enrolled_roots(
        daemon, monkeypatch):
    """Same widening the preference uses, so START HERE works with no
    editor window open. The enrolment gate is not bypassed."""
    from dark_army_daemon import enrollment
    d, store = daemon
    monkeypatch.setattr(d, "_known_project_roots", lambda: set())
    monkeypatch.setattr(enrollment, "enrolled_roots", lambda: {"/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    calls = []

    async def local(root, argv, name, **_kw):
        calls.append(("local", root))
        return True, "agent", 4242

    async def editor(root, argv, name, **_kw):
        calls.append(("editor", root))
        return True, "zsh", None
    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "spawn", editor)

    card = _make(store)
    assert d.board_own_terminal_enabled is False
    ok, detail = await d.dispatch_card(
        card["id"], allow_unplanned=True, own_terminal=True)
    assert ok, detail
    assert len(calls) == 1 and calls[0][0] == "local"


@pytest.mark.asyncio
async def test_a_queued_replay_honours_the_press_that_enqueued_it(
        daemon, monkeypatch):
    """The drain does not send `own_terminal`. The press that queued the
    card is remembered, and an ordinary Start (or unqueue) forgets it."""
    d, store = daemon
    monkeypatch.setattr(d, "_known_project_roots", lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    calls = []

    async def local(root, argv, name, **_kw):
        calls.append("local")
        return True, "agent", 4242

    async def editor(root, argv, name, **_kw):
        calls.append("editor")
        return True, "zsh", None
    monkeypatch.setattr(dispatch, "spawn_local", local)
    monkeypatch.setattr(dispatch, "spawn", editor)

    card = _make(store)
    d._own_terminal_dispatch.add(card["id"])
    ok, detail = await d.dispatch_card(
        card["id"], allow_unplanned=True, queued_replay=True)
    assert ok, detail
    assert calls == ["local"]

    other = _make(store, project="other", root="/private/tmp")
    d._own_terminal_dispatch.add(other["id"])
    d._dispatch_attempts.clear()
    ok, detail = await d.dispatch_card(other["id"], allow_unplanned=True,
                                       own_terminal=False)
    assert ok, detail
    assert calls[-1] == "editor"
    assert other["id"] not in d._own_terminal_dispatch


@pytest.mark.asyncio
async def test_unqueue_forgets_a_start_here_press(daemon):
    d, store = daemon
    card = _make(store)
    store.update(card["id"], {"queue_state": "queued", "queued_at": 1.0},
                 bump=False)
    d._own_terminal_dispatch.add(card["id"])
    ok, detail = await d.unqueue_card(card["id"])
    assert ok, detail
    assert card["id"] not in d._own_terminal_dispatch


# --- the objective rides into the work ----------------------------------------


_OBJECTIVE = {"beneficiary": "Ops", "intended_benefit": "fewer pages at night",
              "success_criterion": "No page in a week."}


def _capture(daemon_obj, monkeypatch, executable="/bin/claude"):
    seen = {}
    monkeypatch.setattr(daemon_obj, "_known_project_roots",
                        lambda: {"/private/tmp", "/tmp"})
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: executable)

    async def accept(root, argv, name, **_kw):
        seen["argv"] = argv
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", accept)
    return seen


@pytest.mark.asyncio
async def test_start_ends_the_prompt_with_the_objective_after_the_attachments(
        daemon, monkeypatch, tmp_path):
    from dark_army_daemon import attachments
    folder = tmp_path / "attachments"
    rel_folder = "abcd1234-efgh5678-ijkl9012-mnop34"
    staging = folder / rel_folder
    staging.mkdir(parents=True)
    dest = staging / "shot.png"
    dest.write_bytes(b"ok")
    monkeypatch.setattr(attachments, "ATTACHMENTS_DIR", folder)
    abs_path = str(dest.resolve())
    d, store = daemon
    card = _make(store, attachments=f"{rel_folder}/shot.png", **_OBJECTIVE)
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    prompt = seen["argv"][-1]
    block = dispatch.objective_block(card)
    assert prompt.endswith(block)
    assert prompt.index(abs_path) < prompt.index(dispatch.OBJECTIVE_BLOCK_HEAD)
    assert prompt.startswith("go")
    assert "Who benefits: Ops\n" in block
    assert "Intended benefit: fewer pages at night\n" in block
    assert block.endswith("Success criterion: No page in a week.")


@pytest.mark.asyncio
async def test_a_card_with_only_a_criterion_draws_one_line(daemon, monkeypatch):
    d, store = daemon
    card = _make(store, success_criterion="No page in a week.")
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"] == [
        "/bin/claude",
        "go" + dispatch.OBJECTIVE_BLOCK_HEAD + "Success criterion: No page in a week.",
    ]


@pytest.mark.asyncio
async def test_a_card_with_no_objective_dispatches_byte_identical_argv(
        daemon, monkeypatch):
    """The baseline capture: today's argv for a plain card, compared byte for
    byte against a card whose three fields are blank."""
    d, store = daemon
    baseline = ["/bin/claude", "go"]
    card = _make(store, beneficiary="  ", intended_benefit="",
                 success_criterion="")
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"] == baseline


@pytest.mark.asyncio
async def test_a_criterion_starting_with_a_dash_is_carried_verbatim(
        daemon, monkeypatch):
    """`guard` judged the one-line prompt before the block was appended, so
    a flag-shaped criterion can neither refuse the press nor change what the
    guard saw."""
    d, store = daemon
    card = _make(store, success_criterion="- must not crash")
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"][-1].endswith("Success criterion: - must not crash")


@pytest.mark.asyncio
async def test_codex_keeps_the_separator_before_the_objectived_prompt(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store, tool="codex", **_OBJECTIVE)
    seen = _capture(d, monkeypatch, executable="/bin/codex")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    argv = seen["argv"]
    assert argv[-2] == "--"
    assert argv[-1].endswith(dispatch.objective_block(card))


def test_objective_block_collapses_each_value_to_one_line():
    block = dispatch.objective_block({
        "beneficiary": "  Ops\n  team ",
        "intended_benefit": "",
        "success_criterion": "line one\nline two",
    })
    assert block == (dispatch.OBJECTIVE_BLOCK_HEAD
                     + "Who benefits: Ops team\n"
                     + "Success criterion: line one line two")


# --- The per-assistant default model, resolved at the four launch sites ------

@pytest.mark.asyncio
async def test_a_default_card_launches_on_the_projects_main_slot_override(
        daemon, monkeypatch):
    """`_agent_model_for` at Start: a card left at Default takes the
    main-session model chosen for its assistant in its project."""
    d, store = daemon
    d.set_agent_models({"claude": {"main": "sonnet"}})
    d.set_agent_model_override("/tmp", {"claude": {"main": "opus"}})
    card = _make(store)
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"][seen["argv"].index("--model") + 1] == "opus"


@pytest.mark.asyncio
async def test_a_default_card_falls_back_to_the_machine_wide_main_slot(
        daemon, monkeypatch):
    d, store = daemon
    d.set_agent_models({"claude": {"main": "sonnet"}})
    card = _make(store)
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"][seen["argv"].index("--model") + 1] == "sonnet"


@pytest.mark.asyncio
async def test_an_untouched_codex_card_launches_on_shipped_sol(daemon, monkeypatch):
    d, store = daemon
    card = _make(store, tool="codex")
    seen = _capture(d, monkeypatch, executable="/bin/codex")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"] == ["/bin/codex", "--model", "gpt-6-sol", "--", "go"]


@pytest.mark.asyncio
async def test_a_card_naming_its_own_model_still_wins(daemon, monkeypatch):
    d, store = daemon
    d.set_agent_models({"codex": {"main": "gpt-6-sol"}})
    d.set_agent_model_override("/tmp", {"codex": {"main": "gpt-6-astra"}})
    card = _make(store, tool="codex", model="gpt-5.5")
    seen = _capture(d, monkeypatch, executable="/bin/codex")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"][seen["argv"].index("--model") + 1] == "gpt-5.5"


@pytest.mark.asyncio
async def test_everything_at_default_is_todays_argv_byte_for_byte(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store)
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert "--model" not in seen["argv"]
    assert seen["argv"] == dispatch.argv_for(
        "claude", "/bin/claude", dispatch.start_prompt(card))


@pytest.mark.asyncio
async def test_a_project_override_default_beats_a_global_choice(
        daemon, monkeypatch):
    """`""` in the override is an explicit Default, not a fall-through."""
    d, store = daemon
    d.set_agent_models({"claude": {"main": "sonnet"}})
    d.set_agent_model_override("/tmp", {"claude": {"main": ""}})
    card = _make(store)
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert "--model" not in seen["argv"]


@pytest.mark.asyncio
async def test_refine_launches_on_the_main_slot(daemon, monkeypatch):
    d, store = daemon
    d.set_agent_models({"codex": {"main": "gpt-5.5"}})
    card = _make(store, column_name="prep", tool="codex", model="gpt-6-astra")
    seen = _capture(d, monkeypatch, executable="/bin/codex")
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    argv = seen["argv"]
    # The card's own model is Start's alone; the planner takes the slot,
    # and the flag sits before codex's separator.
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv.index("--model") < argv.index("--")


def _named_plan_project(d, monkeypatch, tmp_path):
    root = tmp_path / "proj"
    (root / "plans").mkdir(parents=True)
    plan = root / "plans" / "b2.md"
    plan.write_text("# B2\n")
    real = os.path.realpath(root)
    monkeypatch.setattr(d, "_known_project_roots", lambda: {real})
    launched = []

    async def spawn(*a, **k):
        launched.append(a)
        return True, "bob", None

    monkeypatch.setattr(dispatch, "spawn", spawn)
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/claude")
    return real, os.path.realpath(plan), launched


@pytest.mark.asyncio
async def test_refine_attaches_the_plan_the_notes_name_and_launches_nothing(
        daemon, monkeypatch, tmp_path):
    """The fit-app stall of 23 Sep 2026: plan cards filed with the path only
    in their notes sat in Prep, and Refine opened a planning run that built
    the work without ever leaving Prep. The press now attaches the plan."""
    d, store = daemon
    root, plan, launched = _named_plan_project(d, monkeypatch, tmp_path)
    card = _make(store, column_name="prep", root=root,
                 prompt=f"Plan: {plan}\n\nRead that plan first, then run: "
                        f"/ship implement {plan}\n\nWhat it does.")
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert launched == []
    after = store.get(card["id"])
    assert after["column_name"] == "backlog"
    assert after["plan_path"] == plan
    assert after["refine_state"] == ""


@pytest.mark.asyncio
async def test_refine_still_plans_when_the_named_path_is_not_in_the_project(
        daemon, monkeypatch, tmp_path):
    d, store = daemon
    root, _plan, launched = _named_plan_project(d, monkeypatch, tmp_path)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# not this project's\n")
    card = _make(store, column_name="prep", root=root,
                 prompt=f"Plan: {outside}\n\nidea")
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    assert len(launched) == 1
    after = store.get(card["id"])
    assert after["column_name"] == "prep"
    assert after["plan_path"] == ""


@pytest.mark.parametrize("prompt,expected", [
    ("Plan: /p/plans/a.md\n\nmore", "/p/plans/a.md"),
    ("  Plan:   plans/a b.md  \nx", "plans/a b.md"),
    ("plan: plans/a.md", "plans/a.md"),
    ("Plan: plans/a.txt", ""),
    ("Follow-up from: plans/a.md", ""),
    ("an idea\nPlan: plans/a.md", ""),
    ("", ""),
])
def test_named_plan_reads_only_a_leading_plan_line(prompt, expected):
    assert dispatch.named_plan({"prompt": prompt}) == expected
    assert dispatch.named_plan({"prompt": prompt, "kind": "scout"}) == ""


@pytest.mark.asyncio
async def test_a_consult_launches_on_claudes_main_slot(daemon, monkeypatch):
    d, store = daemon
    d.set_agent_models({"claude": {"main": "sonnet"}, "codex": {"main": "gpt-5.5"}})
    card = _make(store, tool="codex")
    seen = _capture(d, monkeypatch)
    ok, detail = await d.ask_card(card["id"], "is it done?")
    assert ok, detail
    argv = seen["argv"]
    assert argv[0] == "/bin/claude"
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_the_seam_refuses_an_off_list_name_from_a_hand_edited_file(daemon):
    """The setters clean, and the seam re-checks on the way out: no route
    puts an unknown name on an argv."""
    d, _store = daemon
    d.set_agent_models({"claude": {"main": "made-up", "planner": "opus"}})
    assert d._agent_model_for("/tmp", "claude", "main") == ""
    assert d._agent_model_for("/tmp", "claude", "planner") == "opus"
    d.agent_models = {"claude": {"main": "made-up"}}     # bypass the setter
    assert d._agent_model_for("/tmp", "claude", "main") == ""
    assert d._agent_model_for("/tmp", "gemini", "main") == ""
    assert d._agent_model_for("/tmp", "claude", "app-reviewer") == ""


def test_the_seam_resolves_override_then_global_then_shipped(daemon):
    from dark_army_daemon import agent_models
    d, _store = daemon
    assert d._agent_model_for("/tmp", "claude", "planner") == (
        agent_models.SHIPPED["claude"]["planner"])
    d.set_agent_models({"claude": {"planner": "sonnet"}})
    assert d._agent_model_for("/tmp", "claude", "planner") == "sonnet"
    d.set_agent_model_override("/tmp/", {"claude": {"planner": "haiku"}})
    assert d._agent_model_for("/tmp", "claude", "planner") == "haiku"
    assert d._agent_model_for("/private/tmp/other", "claude", "planner") == "sonnet"
    # Removing the entry — None or an empty table — goes back to the global.
    d.set_agent_model_override("/tmp", None)
    assert d._agent_model_for("/tmp", "claude", "planner") == "sonnet"
    assert d.agent_model_overrides == {}


def test_the_setters_replace_rather_than_mutate(daemon):
    d, _store = daemon
    before = d.agent_model_overrides
    d.set_agent_model_override("/tmp", {"grok": {"main": "grok-4.5"}})
    assert d.agent_model_overrides is not before
    before = d.agent_model_overrides
    d.set_agent_model_overrides({"/tmp": {"grok": {"main": "grok-4.6"}}})
    assert d.agent_model_overrides is not before
    assert d._agent_model_for("/tmp", "grok", "main") == "grok-4.6"


def test_area_block_with_brief():
    assert dispatch.area_block({"area": "backbone"}, True) == "\n\nArea: Backbone — lead Relay. Read .claude/leads/backbone.md first."

def test_area_block_without_brief():
    assert dispatch.area_block({"area": "backbone"}, False) == "\n\nArea: Backbone — lead Relay."

def test_area_block_without_area():
    assert dispatch.area_block({}, True) == ""

def test_area_block_after_objective():
    card = {"area": "backbone", "success_criterion": "It works"}
    prompt = "Do the work" + dispatch.objective_block(card) + dispatch.area_block(card, True)
    assert prompt.index("Objective:") < prompt.index("Area:")


def test_installed_tools_reports_every_supported_assistant(monkeypatch):
    monkeypatch.setattr(dispatch, "_installed_cache", None)
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/" + tool)
    assert dispatch.installed_tools() == {tool: True for tool in dispatch._EXECUTABLES}


def test_installed_tools_marks_one_missing(monkeypatch):
    monkeypatch.setattr(dispatch, "_installed_cache", None)
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: None if tool == "codex" else "/bin/" + tool)
    assert dispatch.installed_tools()["codex"] is False


def test_installed_tools_uses_a_copy_of_its_cache(monkeypatch):
    monkeypatch.setattr(dispatch, "_installed_cache", None)
    monkeypatch.setattr(dispatch, "resolve_executable", lambda tool: "/bin/" + tool)
    found = dispatch.installed_tools()
    found["grok"] = False
    assert dispatch.installed_tools()["grok"] is True


def test_installed_tools_refreshes_after_thirty_seconds(monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(dispatch, "_installed_cache", None)
    monkeypatch.setattr(dispatch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(dispatch, "resolve_executable",
                        lambda tool: calls.append(tool) or "/bin/" + tool)
    dispatch.installed_tools()
    clock[0] += 29.9
    dispatch.installed_tools()
    assert len(calls) == len(dispatch._EXECUTABLES)
    clock[0] += 0.1
    dispatch.installed_tools()
    assert len(calls) == 2 * len(dispatch._EXECUTABLES)


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [True,False])
async def test_area_block_captured_after_objective(daemon, monkeypatch, tmp_path, present):
    d, store = daemon
    root = str(tmp_path.resolve())
    if present:
        lead = tmp_path / ".claude/leads/backbone.md"
        lead.parent.mkdir(parents=True); lead.write_text("# Backbone")
    card = _make(store, root=root, area="backbone", **_OBJECTIVE)
    seen = _capture(d,monkeypatch)
    monkeypatch.setattr(d,"_known_project_roots",lambda: {root})
    ok,detail = await d.dispatch_card(card["id"],allow_unplanned=True)
    assert ok,detail
    prompt = seen["argv"][-1]
    assert prompt.endswith(dispatch.area_block(card,present))
    assert prompt.index("Objective:") < prompt.index("Area:")
    assert ("Read .claude/leads/backbone.md first." in prompt) is present


# --- refine_batch_prompt: several Prep cards in one planning session ----------


def _batch_card(k, **kw):
    card = {"id": f"id{k}", "title": f"Card title {k}",
            "summary": f"what card {k} is for", "prompt": f"notes {k}",
            "tool": "claude"}
    card.update(kw)
    return card


def test_refine_batch_prompt_opens_with_the_batch_head_and_a_block_per_card():
    cards = [_batch_card(1), _batch_card(2), _batch_card(3)]
    prompt = dispatch.refine_batch_prompt(cards)
    assert prompt.startswith(dispatch.BATCH_PROMPT_HEAD)
    assert prompt.startswith("/ship batch: refine 3 Prep cards")
    assert dispatch.prompt_refusal("claude", prompt) is None
    for k in (1, 2, 3):
        block = (f"## Card {k} of 3 — Card title {k}\nCard id: id{k}\n"
                 f"Summary: what card {k} is for\nInstructions:\nnotes {k}")
        assert block in prompt
    assert prompt.index("## Card 1 of 3") < prompt.index("## Card 2 of 3") \
        < prompt.index("## Card 3 of 3")
    # Pure and shared: the blocks are `batch_blocks`' own, verbatim.
    assert prompt.endswith(dispatch.batch_blocks(cards))


def test_refine_batch_prompt_says_the_codex_planning_sentence_once():
    codex = [_batch_card(1, tool="codex"), _batch_card(2, tool="codex")]
    prompt = dispatch.refine_batch_prompt(codex)
    assert prompt.count(".agents/skills/ship/SKILL.md") == 1
    assert dispatch.prompt_refusal("codex", prompt) is None
    claude = dispatch.refine_batch_prompt([_batch_card(1), _batch_card(2)])
    assert ".agents/skills/ship/SKILL.md" not in claude


def test_refine_batch_prompt_carries_each_cards_objective_and_skips_repeats():
    cards = [_batch_card(1, beneficiary="Ops", success_criterion="No pages."),
             _batch_card(2, summary="Card title 2", prompt="")]
    prompt = dispatch.refine_batch_prompt(cards)
    first, second = prompt.split("## Card 2 of 2")
    assert "Objective:\nWho benefits: Ops\nSuccess criterion: No pages." in first
    assert "Objective:" not in second
    # A summary that only repeats the title, and empty instructions, add no line.
    assert "Summary:" not in second and "Instructions:" not in second


def test_refine_prompt_is_untouched_by_the_batch_form():
    card = _batch_card(1)
    assert dispatch.refine_prompt(card) == (
        "/ship what card 1 is for\n\nTitle: Card title 1\n\n"
        "Instructions:\nnotes 1")


# --- implement_batch_prompt: several Backlog cards in one session -------------


def test_implement_batch_prompt_opens_with_the_head_and_a_plan_per_block():
    cards = [_batch_card(k, plan_path=f"/p/plans/{k}.md") for k in (1, 2, 3)]
    prompt = dispatch.implement_batch_prompt(cards)
    assert prompt.startswith(dispatch.BATCH_PROMPT_HEAD + "implement 3 Backlog "
                             "cards in this one session")
    assert "dark_army_next_card" in prompt.splitlines()[0]
    first = next(line for line in prompt.splitlines() if line.strip())
    assert not first.startswith("Plan:"), \
        "a Plan: first line would enter single-card implement mode"
    for k in (1, 2, 3):
        assert (f"## Card {k} of 3 — Card title {k}\nCard id: id{k}\n"
                f"Plan: /p/plans/{k}.md\n") in prompt
    assert prompt.endswith(dispatch.batch_blocks(cards))
    assert dispatch.prompt_refusal("claude", prompt) is None
    assert dispatch.prompt_refusal("codex", prompt) is None


def test_batch_blocks_names_a_plan_only_where_the_card_has_one():
    planless = [_batch_card(1), _batch_card(2)]
    assert "Plan:" not in dispatch.batch_blocks(planless)
    # The refinement's blocks are exactly what they were before the line.
    assert dispatch.batch_blocks(planless).startswith(
        "## Card 1 of 2 — Card title 1\nCard id: id1\nSummary: ")
    mixed = [_batch_card(1, plan_path="/p/a.md"), _batch_card(2)]
    blocks = dispatch.batch_blocks(mixed)
    assert blocks.count("Plan:") == 1
    assert "Card id: id1\nPlan: /p/a.md\n" in blocks


def test_start_prompt_is_untouched_by_the_batch_implement_form():
    card = _batch_card(1, plan_path="/p/plans/1.md")
    assert dispatch.start_prompt(card).startswith("Plan: /p/plans/1.md\n\n")


# --- The reasoning effort (plans/2026-10-03-card-and-role-effort-level.md) ----

def test_effort_before_separator_on_every_tool():
    """The flag must precede `--`: after it, it is prompt text and the session
    silently runs at the wrong effort. Pinned exactly, per CLI."""
    assert dispatch.argv_for("claude", "claude", "do it", effort="high") == [
        "claude", "--effort", "high", "do it"]
    assert dispatch.argv_for("codex", "codex", "do it", effort="high") == [
        "codex", "-c", "model_reasoning_effort=high", "--", "do it"]
    assert dispatch.argv_for("grok", "grok", "do it", effort="high") == [
        "grok", "--reasoning-effort", "high", "--", "do it"]


def test_effort_and_model_both_sit_before_the_separator():
    assert dispatch.argv_for("codex", "codex", "p", model="gpt-6-sol",
                             effort="low") == [
        "codex", "--model", "gpt-6-sol", "-c", "model_reasoning_effort=low",
        "--", "p"]
    assert dispatch.argv_for("claude", "claude", "p", model="opus",
                             effort="max") == [
        "claude", "--model", "opus", "--effort", "max", "p"]
    assert dispatch.argv_for("grok", "grok", "p", model="grok-4.5",
                             effort="minimal") == [
        "grok", "--model", "grok-4.5", "--reasoning-effort", "minimal",
        "--", "p"]


def test_an_empty_effort_is_todays_argv_byte_for_byte():
    for tool in ("claude", "codex", "grok"):
        assert dispatch.argv_for(tool, tool, "p", effort="") == dispatch.argv_for(
            tool, tool, "p")
        assert not any("effort" in part for part in dispatch.argv_for(
            tool, tool, "p", model=dispatch.MODELS[tool][0]))


def test_the_prompt_is_still_last_with_an_effort():
    for tool in ("claude", "codex", "grok"):
        level = dispatch.EFFORTS[tool][0]
        assert dispatch.argv_for(tool, tool, "the prompt", effort=level)[-1] == (
            "the prompt")


def test_effort_levels_per_tool_and_the_gpt_5_5_exclusion():
    assert "ultra" not in dispatch.EFFORTS["codex"]
    assert "none" not in dispatch.EFFORTS["grok"]
    assert "max" in dispatch.efforts_for("codex", "")
    assert "max" not in dispatch.efforts_for("codex", "gpt-5.5")
    assert dispatch.efforts_for("gemini", "") == ()


def test_effort_catalogue_has_the_tool_default_and_every_model():
    cat = dispatch.effort_catalogue()
    assert set(cat) == set(dispatch.EFFORTS)
    for tool, entry in cat.items():
        assert set(entry) == {""} | set(dispatch.MODELS[tool])
    assert "max" in cat["codex"][""]
    assert "max" not in cat["codex"]["gpt-5.5"]


def _effort_card(**kw):
    card = {"id": "c", "column_name": "backlog", "tool": "claude",
            "root": "/tmp", "prompt": "go", "project": "bob"}
    card.update(kw)
    return card


def _effort_guard(card):
    return dispatch.guard(card, roots={"/tmp"}, in_flight=[], now=1e9)


def test_guard_refuses_an_off_list_effort_in_words():
    ok, detail = _effort_guard(_effort_card(effort="ultra"))
    assert not ok and "effort" in detail and "claude" in detail
    ok, detail = _effort_guard(_effort_card(tool="codex", model="gpt-5.5",
                                     effort="max"))
    assert not ok and "effort" in detail


def test_guard_accepts_every_offered_effort_and_default():
    for tool, entry in dispatch.effort_catalogue().items():
        for model, levels in entry.items():
            for level in levels:
                ok, detail = _effort_guard(_effort_card(tool=tool, model=model,
                                                 effort=level))
                assert ok, (tool, model, level, detail)
    assert _effort_guard(_effort_card(effort=""))[0]
    assert _effort_guard(_effort_card())[0]


@pytest.mark.asyncio
async def test_card_effort_wins_over_the_main_slot(daemon, monkeypatch):
    d, store = daemon
    d.set_agent_efforts({"claude": {"main": "low"}})
    card = _make(store, effort="high")
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    argv = seen["argv"]
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[-1].startswith("go")


@pytest.mark.asyncio
async def test_card_effort_wins_before_the_separator_on_codex(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store, tool="codex", effort="medium")
    seen = _capture(d, monkeypatch, executable="/bin/codex")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    argv = seen["argv"]
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=medium"
    assert argv.index("-c") < argv.index("--")


@pytest.mark.asyncio
async def test_card_effort_wins_before_the_separator_on_grok(
        daemon, monkeypatch):
    d, store = daemon
    card = _make(store, tool="grok", effort="minimal")
    seen = _capture(d, monkeypatch, executable="/bin/grok")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    argv = seen["argv"]
    assert argv[argv.index("--reasoning-effort") + 1] == "minimal"
    assert argv.index("--reasoning-effort") < argv.index("--")


@pytest.mark.asyncio
async def test_a_default_card_takes_the_project_effort(daemon, monkeypatch):
    d, store = daemon
    d.set_agent_efforts({"claude": {"main": "medium"}})
    d.set_agent_effort_override("/tmp", {"claude": {"main": "xhigh"}})
    card = _make(store)
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"][seen["argv"].index("--effort") + 1] == "xhigh"


@pytest.mark.asyncio
async def test_a_default_card_falls_back_to_the_machine_wide_effort(
        daemon, monkeypatch):
    d, store = daemon
    d.set_agent_efforts({"claude": {"main": "medium"}})
    card = _make(store)
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert seen["argv"][seen["argv"].index("--effort") + 1] == "medium"


@pytest.mark.asyncio
async def test_a_project_override_default_effort_beats_a_global_choice(
        daemon, monkeypatch):
    d, store = daemon
    d.set_agent_efforts({"claude": {"main": "medium"}})
    d.set_agent_effort_override("/tmp", {"claude": {"main": ""}})
    card = _make(store)
    seen = _capture(d, monkeypatch)
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    assert "--effort" not in seen["argv"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["claude", "codex", "grok"])
async def test_everything_at_default_is_todays_argv_with_no_effort_flag(
        daemon, monkeypatch, tool):
    d, store = daemon
    card = _make(store, tool=tool)
    seen = _capture(d, monkeypatch, executable=f"/bin/{tool}")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    text = " ".join(seen["argv"][:-1])
    for word in ("--effort", "--reasoning-effort", "model_reasoning_effort"):
        assert word not in text, (tool, word)


def test_agent_effort_for_revalidates_against_the_model_on_the_way_out():
    from dark_army_daemon import daemon as daemon_mod
    d = daemon_mod.BobDaemon.__new__(daemon_mod.BobDaemon)
    d.agent_models = {"codex": {"main": "gpt-5.5"}}
    d.agent_model_overrides = {}
    d.agent_efforts = {"codex": {"main": "max"}}
    d.agent_effort_overrides = {}
    # gpt-5.5 has no `max`: a stored level the model rejects reads as Default.
    assert d._agent_effort_for("/tmp", "codex", "main") == ""
    d.agent_models = {"codex": {"main": "gpt-6-sol"}}
    assert d._agent_effort_for("/tmp", "codex", "main") == "max"
    assert d._agent_effort_for("/tmp", "codex", "main", model="gpt-5.5") == ""
    assert d._agent_effort_for("/tmp", "codex", "card-preparer") == ""
    assert d._agent_effort_for("/tmp", "gemini", "main") == ""
    d.agent_efforts = {"codex": {"main": "ultra"}}
    assert d._agent_effort_for("/tmp", "codex", "main") == ""


@pytest.mark.asyncio
async def test_refine_launches_at_the_main_slot_effort(daemon, monkeypatch):
    d, store = daemon
    d.set_agent_efforts({"codex": {"main": "low"}})
    card = _make(store, column_name="prep", tool="codex", effort="high")
    seen = _capture(d, monkeypatch, executable="/bin/codex")
    ok, detail = await d.refine_card(card["id"])
    assert ok, detail
    argv = seen["argv"]
    # The card's own effort is Start's alone; the planner takes the slot.
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=low"
    assert argv.index("-c") < argv.index("--")


@pytest.mark.asyncio
async def test_card_effort_is_judged_against_the_model_the_launch_will_use(
        daemon, monkeypatch):
    """A Default-model Codex card whose main slot resolves to gpt-5.5 must not
    launch with `max`, which Codex refuses at spawn: refused in words before
    any spawn, never trimmed."""
    d, store = daemon
    d.set_agent_models({"codex": {"main": "gpt-5.5"}})
    card = _make(store, tool="codex", effort="max")
    assert store.get(card["id"])["model"] == ""
    seen = _capture(d, monkeypatch, executable="/bin/codex")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert not ok
    assert detail == dispatch.EFFORT_REFUSAL.format(tool="codex")
    assert "argv" not in seen


@pytest.mark.asyncio
async def test_card_effort_the_launch_model_offers_still_starts(
        daemon, monkeypatch):
    d, store = daemon
    d.set_agent_models({"codex": {"main": "gpt-5.5"}})
    card = _make(store, tool="codex", effort="xhigh")
    seen = _capture(d, monkeypatch, executable="/bin/codex")
    ok, detail = await d.dispatch_card(card["id"], allow_unplanned=True)
    assert ok, detail
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=xhigh"


# --- a stacked session's name -------------------------------------------------


def test_stacked_title_puts_the_agent_first():
    assert dispatch.stacked_title("Vex", 3) == "Vex · Stacked cards 3"
    assert dispatch.stacked_title("", 2) == "Stacked cards 2"
    assert len(dispatch.stacked_title("x" * 60, 3)) == 40


def test_with_session_name_is_claude_only_and_keeps_the_prompt_last():
    argv = ["/bin/claude", "--model", "opus", "/ship batch: go"]
    named = dispatch.with_session_name("claude", argv, "Vex · Stacked cards 3")
    assert named == ["/bin/claude", "--name", "Vex · Stacked cards 3",
                     "--model", "opus", "/ship batch: go"]
    codex = ["/bin/codex", "--", "go"]
    assert dispatch.with_session_name("codex", codex, "Vex") == codex
    assert dispatch.with_session_name("claude", argv, "") == argv
