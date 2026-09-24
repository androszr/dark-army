# host/dark_army_daemon/search_scope.py
"""The folders an agent Dark Army runs may search, written down as one file.

On 24 Sep 2026 a session in Dark Army's own terminal went looking for a plan
file with `find /` and then `find ~`; the pty broker is a child of the app, so
macOS charged the walk to Dark Army and raised Photos, Music and Documents
privacy prompts naming it. Every agent is now told, in words, to search only
its own project, `~/.dark-army`, its scratch folder and the folders listed
here — and this module keeps that list concrete and current.

**The file is `paths.SEARCH_SCOPE_PATH`** (`~/.dark-army/search-scope.json`):
``{"version": 1, "roots": [...]}``, sorted, de-duplicated, never an empty
string. It holds folder paths and nothing else — never a digest, a key, a code
or a claim — because the enrolment ledger it is derived from holds digests and
this file is read into a model's context. It still names every enrolled
project's full path, so it is 0600 in `paths._PRIVATE_FILES` beside
`enrollment.json`.

**Who writes it**: `enrollment.save()` after every ledger write (so enrol and
un-enrol move it at once) and `app.main()` once at launch. **Who reads it**:
the notify script, by exact path, on `session_start`, printing the roots into
a Claude session's opening context; Grok's home rules and the agent pack's
`AGENTS.md` point an agent at the file instead of carrying a list that would
go stale.

**A stale or missing file is safe.** The notify script prints fallback words
(search only this project, `~/.dark-army` and the scratch folder) when the file
is absent or malformed, and an older build never reads it at all. `version`
is on the dict so a later shape can be told apart.
"""

from __future__ import annotations

import json
import logging

from . import enrollment, paths

logger = logging.getLogger("dark-army")

SCOPE_VERSION = 1


def compose(enrolled_roots, self_root, state_dir) -> dict:
    """The file's contents: every non-empty root, sorted and de-duplicated.

    Pure. Exactly two keys, `version` and `roots`; nothing a caller hands in
    beyond folder strings can reach the dict."""
    candidates = list(enrolled_roots or []) + [self_root, state_dir]
    roots = sorted({str(r) for r in candidates if isinstance(r, str) and r.strip()})
    return {"version": SCOPE_VERSION, "roots": roots}


def refresh() -> bool:
    """Rewrite the file from the enrolment ledger when its contents moved.

    `True` when the file is current (written now, or already equal byte for
    byte, in which case nothing is written); `False` on a failure, logged as a
    warning. **Never raises**: it runs inside `enrollment.save()`, whose
    contract a scope file must not change, and on the AppKit thread at launch.
    """
    try:
        data = compose(enrollment.enrolled_roots(), enrollment.self_root(),
                       str(paths.STATE_DIR))
    except Exception:
        # Reading the ledger must never break save() or launch: logged in
        # full, the file left as it was.
        logger.warning("search scope: could not read the enrolled folders",
                       exc_info=True)
        return False
    text = json.dumps(data, indent=2)
    path = paths.SEARCH_SCOPE_PATH
    try:
        if path.read_text(encoding="utf-8") == text:
            return True
    except (OSError, UnicodeDecodeError):
        pass
    try:
        paths.ensure_state_dir()
        # The state folder in production (already there, 0700); the per-run
        # folder under pytest when a test moved STATE_DIR alone.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths.atomic_write_json(path, data, mode=0o600, indent=2)
    except OSError:
        logger.warning("search scope: could not write %s", path, exc_info=True)
        return False
    return True


def roots() -> list:
    """The roots the file names now, or [] when it is missing or malformed.
    For the launch log line and tests; the notify script reads the file itself."""
    try:
        data = json.loads(paths.SEARCH_SCOPE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(found, list):
        return []
    return [r for r in found if isinstance(r, str) and r]
