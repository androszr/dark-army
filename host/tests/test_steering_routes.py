"""The structural pin: nothing types into a session outside a lane
(`docs/steering-queue.md`)."""
import ast
from pathlib import Path

from dark_army_daemon import steering

DAEMON_DIR = Path(__file__).resolve().parent.parent / "dark_army_daemon"
FILES = ("daemon.py", "daemon_board.py", "daemon_steering.py",
         "daemon_review.py")
#: The lane's own runners, which may call a typing site.
RUNNERS = {"_steer_run", "_steer_body", "_steer_raw", "_steer_inline"}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_typing_call(call: ast.Call) -> bool:
    name = _dotted(call.func)
    if name in ("session_io.send_text", "self._pty.write",
                "self._leader.prompt", "codex_input.submit",
                "vscode_reveal.reply_native_terminal"):
        return True
    if name.endswith("push_channel_event"):
        # Only the person's own words: `{"kind": "user"}`.
        for arg in list(call.args) + [k.value for k in call.keywords]:
            if isinstance(arg, ast.Dict):
                for k, v in zip(arg.keys, arg.values):
                    if (isinstance(k, ast.Constant) and k.value == "kind"
                            and isinstance(v, ast.Constant)
                            and v.value == "user"):
                        return True
    return False


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _own_nodes(fn):
    """The nodes of `fn` that are not inside a nested function."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def typing_sites(source: str) -> set:
    found = set()
    for fn in _functions(ast.parse(source)):
        for node in _own_nodes(fn):
            if isinstance(node, ast.Call) and _is_typing_call(node):
                found.add(fn.name)
    return found


def _lambda_handed_to_a_lane(tree):
    """The lambdas passed to `_steer` / `_steer_raw` / `_steer_nowait`,
    positionally or as `work=` / `write=`."""
    ok = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and _dotted(node.func).split(".")[-1] in (
                    "_steer", "_steer_raw", "_steer_nowait")):
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(arg, ast.Lambda):
                    ok.add(id(arg))
    return ok


def _references(source: str, names: set) -> list:
    """`(function, name)` for each reference to a typing site: a call, an
    attribute, or a string naming it (the lane's body table)."""
    tree = ast.parse(source)
    lane = _lambda_handed_to_a_lane(tree)
    out = []

    def visit(node, owner, lam):
        for child in ast.iter_child_nodes(node):
            o, l = owner, lam
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                o = child.name
            elif isinstance(child, ast.Lambda):
                l = id(child) in lane or lam
            hit = ""
            if isinstance(child, ast.Attribute) and child.attr in names:
                hit = child.attr
            elif (isinstance(child, ast.Constant)
                  and isinstance(child.value, str) and child.value in names):
                hit = child.value
            elif isinstance(child, ast.Name) and child.id in names:
                hit = child.id
            if hit and not l and not (
                    isinstance(child, (ast.FunctionDef,
                                       ast.AsyncFunctionDef))):
                out.append((o, hit))
            visit(child, o, l)

    visit(tree, "<module>", False)
    return out


def _sources():
    return {name: (DAEMON_DIR / name).read_text() for name in FILES}


def test_the_typing_sites_are_exactly_the_allowlist():
    found = set()
    for source in _sources().values():
        found |= typing_sites(source)
    assert found == set(steering.TYPING_SITES), (
        f"new: {sorted(found - steering.TYPING_SITES)}, "
        f"gone: {sorted(steering.TYPING_SITES - found)}")


def test_every_caller_of_a_typing_site_is_a_site_or_the_lane_itself():
    offenders = []
    for name, source in _sources().items():
        for owner, hit in _references(source, set(steering.TYPING_SITES)):
            if (owner in steering.TYPING_SITES or owner in steering.LANE_BODIES
                    or owner in RUNNERS):
                continue
            if name == "daemon_steering.py" and owner == "<module>":
                continue   # the body table
            offenders.append(f"{name}: {owner} reaches {hit}")
    assert offenders == [], "\n".join(offenders)


def test_the_body_table_names_only_known_sites():
    from dark_army_daemon.daemon_steering import STEER_BODIES
    for kind, method in STEER_BODIES.items():
        assert (method in steering.TYPING_SITES
                or method in steering.LANE_BODIES), (kind, method)


def test_the_steering_module_reaches_no_launcher():
    text = (DAEMON_DIR / "daemon_steering.py").read_text()
    for forbidden in ("dispatch.spawn", "spawn_local", "dispatch_card(",
                      "start_cards(", "_ask_card_consult_locked"):
        assert forbidden not in text, forbidden


def test_the_walker_finds_a_planted_typing_site():
    planted = (
        "async def sneaky(self, pid):\n"
        "    return await session_io.send_text(pid, '', 'x')\n"
        "async def raw(self, h):\n"
        "    self._pty.write(h, 'x')\n"
        "async def channel(self, sid):\n"
        "    await self.push_channel_event(sid, 't', {'kind': 'user'})\n"
        "async def fleet(self, sid):\n"
        "    await self.push_channel_event(sid, 't', {'kind': 'fleet'})\n")
    assert typing_sites(planted) == {"sneaky", "raw", "channel"}


def test_the_walker_finds_an_unlaned_caller_and_lets_a_laned_lambda_through():
    bad = ("async def verb(self):\n"
           "    return await self._message_card_now(1, 'x')\n")
    assert _references(bad, {"_message_card_now"}) == [
        ("verb", "_message_card_now")]
    good = ("async def verb(self):\n"
            "    return await self._steer('k', 's', {}, work=lambda: "
            "self._message_card_now(1, 'x'))\n")
    assert _references(good, {"_message_card_now"}) == []
