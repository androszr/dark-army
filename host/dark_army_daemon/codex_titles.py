"""Private decoration of shared-server Codex terminals; never control authority."""

from . import codex_terminal


def title_ttys(targets, legacy):
    """Recheck captured listener identities off-loop before handing ttys to OSC.

    The background server observation identifies the thread. Fresh listeners
    prevent a closed/reused terminal from inheriting that thread's badge.
    Collisions across old CLI and shared-server routes withdraw both owners.
    """
    result = dict(legacy)
    if not targets:
        return result
    before = codex_terminal._listeners()
    after = codex_terminal._listeners()
    for target in targets:
        if (codex_terminal._match(target.root, target.port, before) == target
                and codex_terminal._match(target.root, target.port, after) == target):
            result[target.root.session_id] = target.tty
    return {sid: tty for sid, tty in result.items()
            if sum(other == tty for other in result.values()) == 1}
