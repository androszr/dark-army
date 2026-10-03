"""The after-steps a project offers on the Review section (docs/review-runs.md).

`GENERIC_STEPS` is what a project with no profile (or a profile that declares
none) offers: commit and push. `check_steps` is the shape check
`pack_render.normalise_profile` applies to a profile's `after_steps`; the
daemon's `review_run` carries a byte-equal copy of `GENERIC_STEPS` (it imports
nothing from this package), pinned by `test_review_run.py`.
"""

from __future__ import annotations

import re

GENERIC_STEPS = (
    {"id": "commit", "label": "Commit",
     "how": "make one commit of the working tree in the repository's "
            "whole-sentence style"},
    {"id": "push", "label": "Push to the remote",
     "how": "git push to the upstream branch"},
)

_ID_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,23}\Z")


def generic_steps() -> list:
    """A fresh list of `[id, label, how]` triples for the generic steps."""
    return [[s["id"], s["label"], s["how"]] for s in GENERIC_STEPS]


def check_steps(raw, name: str = "") -> list:
    """`raw` as a list of `[id, label, how]` triples, or the generic steps when
    absent. Raises `ValueError` (a message safe to show) on a wrong shape:
    ids unique, lowercase, `[a-z][a-z0-9_-]{0,23}`, `how` non-empty."""
    if raw is None:
        return generic_steps()
    where = f"profile {name!r}: " if name else ""
    if not isinstance(raw, list):
        raise ValueError(f"{where}after_steps must be a list")
    out = []
    seen = set()
    for row in raw:
        if not (isinstance(row, list) and len(row) == 3
                and all(isinstance(x, str) for x in row)):
            raise ValueError(f"{where}each after step is [id, label, how]")
        sid, label, how = row
        if not _ID_RE.match(sid) or sid in seen:
            raise ValueError(f"{where}after step id {sid!r} is not unique "
                             "lowercase")
        if not how.strip():
            raise ValueError(f"{where}after step {sid!r} has no how")
        seen.add(sid)
        out.append([sid, label or sid, how])
    return out
