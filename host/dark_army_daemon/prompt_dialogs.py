"""Conservative readers for assistant permission dialogs in hosted terminals.

Each choice is checked against the screen at press time. A changed layout
returns ``None`` so Dark Army leaves the decision to the terminal.
"""

from __future__ import annotations

import re
from typing import Optional


GROK_ALLOW_ONCE = "Allow once"
GROK_REJECT_ONCE = "Reject once"
CODEX_YES_ONCE = "Yes, just this once"
CODEX_NO = "No, and tell Codex what to do differently"
NEVER_ROWS = ("always", "all edits", "always-approve")

_GROK_ROW = re.compile(r"^\s*(?:[>❯›]\s*)?([1-9])(?:[.)]|\s)\s*(.*?)\s*$")


def grok_choice(lines, verdict: str) -> Optional[str]:
    """The digit on exactly one once-only Grok row, or ``None``.

    ``lines`` is the screen as `vtgrid.Screen.text()` gives it — a list of
    rows — or one string of them."""
    wanted = {"allow": GROK_ALLOW_ONCE, "deny": GROK_REJECT_ONCE}.get(verdict)
    if wanted is None:
        return None
    if isinstance(lines, str):
        lines = lines.splitlines()
    rows: list[tuple[str, str]] = []
    for line in lines:
        match = _GROK_ROW.match(line)
        if match:
            rows.append((match.group(1), match.group(2).strip()))
    matches = [digit for digit, label in rows if label.casefold() == wanted.casefold()]
    if len(matches) != 1:
        return None
    digit = matches[0]
    if any(any(word in label.casefold() for word in NEVER_ROWS)
           for row_digit, label in rows if row_digit == digit):
        return None
    if len([1 for row_digit, _ in rows if row_digit == digit]) != 1:
        return None
    return digit


def codex_choice(lines: str, verdict: str) -> Optional[str]:
    """Reserved for the SERIAL branch, which needs its own recorded fixture."""
    return None
