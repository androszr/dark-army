"""Where things run — a contract over the daemon's two threads.

`BobDaemon` shares one event loop with the strip, the panel's SSE stream and
the terminal titles; everything that blocks (SQLite, transcript reads, `ps`)
hops to the default executor. The split is a convention held by call sites,
so this file pins it structurally, over the AST of `daemon.py` and
`daemon_board.py`:

- a method handed to ``run_in_executor`` is plain ``def`` code — no ``await``,
  no loop-only calls (``call_soon``, ``create_task``, ``run_until_complete``,
  ``asyncio.get_running_loop``). ``call_soon_threadsafe`` is deliberately
  legal: it is *how* executor code hands work back to the loop.
- the decide/consider family runs inside those executor bodies and obeys the
  same rule (decided on the executor, acted on the loop — the
  `_flush_auto_compacts` pattern the code comments name).
- every ``_flush_*`` method is a coroutine, awaited from an async method, and
  never appears inside a ``run_in_executor`` call: the two sets are disjoint.
- `_push_agents_snapshot` copies on the loop (`_collect_agent_stubs`) before
  it enriches on the executor (`_enrich_agent_stubs`).

A refactor that moves one of these across the seam fails here in words,
rather than as a once-a-week deadlock or a torn dict on a live fleet.
"""
import ast
import functools
import inspect
import textwrap
from pathlib import Path

from dark_army_daemon.daemon import BobDaemon

HOST = Path(__file__).resolve().parents[1]
SOURCES = (
    HOST / "dark_army_daemon" / "daemon.py",
    HOST / "dark_army_daemon" / "daemon_board.py",
)

#: Methods the executor must keep calling — anti-vacuity for the AST scan.
#: If the scanner ever stops seeing these, the test is broken, not the code.
KNOWN_EXECUTOR_METHODS = {
    "_enrich_agent_stubs",
    "_apply_terminal_titles",
    "_reconcile_board",
    "_known_project_roots",
    "_claiming_session_ids",
    "_slot_refusal",
    "_refresh_board_state",
    "_load_rosters",
}

#: Sync deciders that run *inside* the executor bodies above (they are called
#: by `_reconcile_board` / `_enrich_agent_stubs`, not handed to the executor
#: by name), plus the categorizer. Named explicitly because the AST scan of
#: `run_in_executor` arguments cannot see one executor method calling another.
DECIDE_ON_EXECUTOR = (
    "_decide_queue_dispatches",
    "_decide_auto_compacts",
    "_consider_title",
    "_consider_priority",
    "_reconciled_categories",
    "_reconcile_board",
)

#: Loop-only calls that must never appear in executor-side code. The plain
#: ``call_soon`` is loop-affine; ``call_soon_threadsafe`` (a different
#: attribute name, so it never matches here) is the sanctioned way back.
LOOP_ONLY_ATTRS = ("call_soon", "create_task", "run_until_complete",
                   "ensure_future")


@functools.lru_cache(maxsize=None)
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _self_method_names_in_executor_calls(tree: ast.Module) -> set:
    """Every ``self._x`` reachable inside a ``run_in_executor(...)`` call."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr == "run_in_executor"):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"):
                found.add(sub.attr)
    return found


def _executor_methods() -> set:
    """The scan, filtered to names that are real methods of BobDaemon.

    ``self._board`` / ``self._history`` also appear inside executor calls —
    they are store *attributes* whose bound methods are the payload, set in
    ``__init__`` rather than on the class, so the getattr filter drops them.
    """
    names = set()
    for path in SOURCES:
        names |= _self_method_names_in_executor_calls(_parse(path))
    return {n for n in names if callable(getattr(BobDaemon, n, None))}


def _method_source_tree(name: str) -> ast.Module:
    fn = getattr(BobDaemon, name)
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _loop_only_violations(name: str) -> list:
    """Await expressions and loop-only calls inside one method's source."""
    bad = []
    for node in ast.walk(_method_source_tree(name)):
        if isinstance(node, ast.Await):
            bad.append(f"await at line {node.lineno}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in LOOP_ONLY_ATTRS:
                bad.append(f"{node.func.attr}() at line {node.lineno}")
            if node.func.attr == "get_running_loop":
                bad.append(f"get_running_loop() at line {node.lineno}")
    return bad


def test_the_scan_still_sees_the_seam():
    """Anti-vacuous: the AST walk must keep finding the known hops."""
    found = _executor_methods()
    missing = KNOWN_EXECUTOR_METHODS - found
    assert not missing, (
        f"the run_in_executor scan no longer sees {sorted(missing)} — either "
        "the seam moved (update this test's known set deliberately) or the "
        "scanner broke and every other assertion here is vacuous")


def test_executor_called_methods_are_plain_sync_code():
    for name in sorted(_executor_methods()):
        fn = getattr(BobDaemon, name)
        assert not inspect.iscoroutinefunction(fn), (
            f"{name} is handed to run_in_executor but is async — the executor "
            "would receive a coroutine object and never run it")
        bad = _loop_only_violations(name)
        assert not bad, (
            f"{name} runs on the executor but contains loop-only code: {bad}")


def test_the_decide_family_is_executor_safe():
    for name in DECIDE_ON_EXECUTOR:
        fn = getattr(BobDaemon, name, None)
        assert fn is not None, (
            f"{name} is gone from BobDaemon — if it was renamed, retarget "
            "this pin rather than deleting it")
        assert not inspect.iscoroutinefunction(fn), (
            f"{name} is decided on the executor and must stay sync")
        bad = _loop_only_violations(name)
        assert not bad, (
            f"{name} runs inside an executor body but contains loop-only "
            f"code: {bad}")


def _flush_methods() -> set:
    return {n for n in dir(BobDaemon)
            if n.startswith("_flush_") and callable(getattr(BobDaemon, n))}


def test_there_are_flush_methods_and_all_are_coroutines():
    flushes = _flush_methods()
    assert "_flush_auto_compacts" in flushes, (
        "the pattern's namesake is gone — retarget, don't delete")
    for name in sorted(flushes):
        assert inspect.iscoroutinefunction(getattr(BobDaemon, name)), (
            f"{name} is a _flush_ method and must be a coroutine: the flushes "
            "act on the loop; the executor only decides")


def test_flush_methods_never_ride_the_executor():
    """The two sets are disjoint: decided on the executor, flushed on the
    loop. A _flush_ name inside a run_in_executor call is the seam torn."""
    overlap = _executor_methods() & _flush_methods()
    assert not overlap, (
        f"{sorted(overlap)} are both handed to the executor and named "
        "_flush_ — one of the two claims is a lie")


def test_every_flush_call_site_is_awaited_in_async_code():
    for path in SOURCES:
        tree = _parse(path)
        stack = []

        class Walker(ast.NodeVisitor):
            def _fn(self, node):
                stack.append(node)
                self.generic_visit(node)
                stack.pop()

            visit_FunctionDef = _fn
            visit_AsyncFunctionDef = _fn

            def visit_Call(self, node):
                func = node.func
                if (isinstance(func, ast.Attribute)
                        and func.attr.startswith("_flush_")
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "self"):
                    enclosing = stack[-1] if stack else None
                    assert isinstance(enclosing, ast.AsyncFunctionDef), (
                        f"{path.name}:{node.lineno} calls {func.attr} from "
                        f"{getattr(enclosing, 'name', '<module>')}, which is "
                        "not async — flushes run on the loop only")
                self.generic_visit(node)

        Walker().visit(tree)


def test_push_agents_snapshot_copies_on_the_loop_then_enriches_off_it():
    """`_collect_agent_stubs` is the loop-thread copy; `_enrich_agent_stubs`
    is the transcript I/O. The copy must stay a plain call (never inside a
    run_in_executor argument) and must come before the enrich hop."""
    tree = _method_source_tree("_push_agents_snapshot")
    collect_lines, enrich_hop_lines = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and func.attr == "_collect_agent_stubs"):
            collect_lines.append(node.lineno)
        if isinstance(func, ast.Attribute) and func.attr == "run_in_executor":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Attribute)
                        and sub.attr == "_collect_agent_stubs"):
                    raise AssertionError(
                        "_collect_agent_stubs moved onto the executor — it "
                        "reads live session dicts and must stay on the loop")
                if (isinstance(sub, ast.Attribute)
                        and sub.attr == "_enrich_agent_stubs"):
                    enrich_hop_lines.append(node.lineno)
    assert collect_lines, "_push_agents_snapshot no longer copies the stubs"
    assert enrich_hop_lines, (
        "_enrich_agent_stubs no longer rides run_in_executor — transcript "
        "I/O landed on the event loop")
    assert min(collect_lines) < min(enrich_hop_lines), (
        "the loop-side copy must happen before the executor enrich")
