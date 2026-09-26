"""The agent's name, on its terminal tab.

A VS Code window with six Claude tabs open reads `✳ Next steps in orchestration`
six times over: every tab says what is being *worked on* and none says *who* is
working on it. The panel and the banners have had nicknames for a while; the tab
strip — the thing you actually click to switch agents — did not.

**Who writes the title, and why it is the daemon.**

Claude Code writes its own title with an OSC escape, and it rewrites it whenever
the work changes. Anything else that writes there is in a fight it loses within
seconds, so the first move is to take the pen away: `install_title_env()` in
`dark_army_menubar/hooks.py` sets `CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1` in
`~/.claude/settings.json`. From then on nobody writes the title and the tab is
frozen on whatever the shell last said — which is why this module has to exist:
having disabled the only writer, we owe the tab a replacement.

The daemon is the replacement rather than a launcher wrapper or the hook script,
for one reason each:

* **A wrapper** (`bob claude`) would have to be the way every session starts, and
  it would know the nickname before the session id exists — the nickname is keyed
  on the session id, so it would have to reserve a name and bind it later. Two
  moving parts and a new way to launch, to end up with a *static* title.
* **The hook script** knows the session id, and runs on the controlling terminal,
  so it could write this. But it fires per event, so the title would only be as
  fresh as the last tool call, and it has no idea which names its siblings hold —
  collision resolution lives in `IdentityStore` and needs the whole live set.
* **The daemon** already computes, every few seconds, exactly the two strings the
  title wants: `nickname` (identity, sticky for the session's life) and `name`
  (what it is working on). It also holds the session's pid. So the title is a
  projection of the agents snapshot and nothing new has to be discovered for it.

**Three letters, not the whole name.** A tab is a few characters wide before VS
Code starts eliding from the right, and the identity has to survive that squeeze
— `Cap · Rewrite the sprite pipeline` keeps both halves legible where `Captcha ·
…` spends a third of the tab on the part that is the same every tick. The
separator is a middle dot rather than a colon: a colon reads as "label: value",
and the nickname is the subject here, not a field name.

**One writer per tty.** Two sessions can map to one terminal — `/clear` starts a
new session id in the same tab, and the old one lingers in the snapshot for a
while. Both would write, and the tab would flicker between two names. The most
recently active row wins the tty; the other is silently skipped.

Grok writes titles too (``[ui.notifications.title] enabled``, default true),
but VS Code never shows them: its agent-CLI classifier is a title matching
``/claude\\s*code/i``, ``/\\bcopilot\\b/i`` or ``/\\bgemini\\b/i`` — no Grok
— so the tab stays on ``${process}``, which for this install is the bare
``grok-macos-aarch``. Same two halves as Claude: take the pen away
(``title.enabled = false`` in ``~/.grok/config.toml``) and write the badge
ourselves, primed with ``Claude Code`` so VS Code will render it.

Codex is the exception to the pid half of that description. Its controlling
terminal may belong to the installed Node launcher while the strict native
process identity used by Jump and Stop is absent. The daemon therefore passes a
private session-to-tty map beside the public snapshot. This writer uses that map
only for Codex, never falls back to the row pid, and keeps it out of the pid
cache; Claude and Grok retain the cached pid lookup above.

**VS Code only shows OSC titles to terminals it believes are Claude's.** Its tab
label defaults to `${process}` — every title we write is parsed, stored and not
rendered — except on terminals it has classified as an agent CLI
(`terminal.integrated.tabs.allowAgentCliTitle`, on by default), which render
`${sequence}` instead. The classifier is the title stream itself: a title
matching /claude\\s*code/i, and the only title that ever matched was Claude
Code's own startup `✳ Claude Code` — the exact write our env flag suppresses.
The flag was therefore quietly un-classifying every new session: this module
wrote into a slot nobody displays, and the tab fell back to the process name,
which for a versioned install is the bare `2.1.233`. Hence `_PRIMER` below.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger("dark-army")

# OSC 0 sets icon name *and* window title. VS Code reads it for the terminal tab
# label; Terminal.app and iTerm read it for the window. BEL-terminated rather
# than ST-terminated — universally understood, including by tmux.
_OSC_TITLE = "\033]0;{}\007"

# What convinces VS Code to render our titles at all — see the module docstring.
# It leads every payload: it trips the agent-CLI classifier (sticky once
# matched) and the real title replaces it in the same write(), so a terminal
# processes both before it repaints and only the last one is ever seen. Sent
# unconditionally rather than remembered per tty: re-priming an already
# classified tab is a no-op, Terminal.app and iTerm just take the last title of
# the write, and a "primed" dict is one more piece of per-tty memory that goes
# stale across a VS Code window reload.
_PRIMER = "Claude Code"

# Buckets whose rows still have a terminal to write to. `finished` is excluded on
# purpose: the process is gone, and its pid — hence its tty — may already belong
# to somebody else's shell.
LIVE_BUCKETS = ("running", "waiting", "sleeping")

# Long enough for a real task description, short enough that a runaway name
# cannot push a control sequence's worth of text through a pty every few seconds.
MAX_TITLE = 72

SHORT_LEN = 3


def short(nickname: str) -> str:
    """The three-letter form of a nickname.

    Unique across the whole cast as it stands (Cipher/Canon/Captcha →
    Cip/Can/Cap, Vex/Velvet → Vex/Vel, Proxy/Ptys → Pro/Pty, Forge/Franio →
    For/Fra), which is not luck but a
    constraint on adding names: a name that collides here would give two tabs
    the same badge. Overflow names (`Cipher-1a2b`) shorten by their stem —
    the suffix exists to disambiguate a twelve-agent pile-up in the panel, and
    three letters cannot carry it anyway.
    """
    stem = (nickname or "").split("-")[0].strip()
    return stem[:SHORT_LEN]


def _clean(text: str) -> str:
    """A one-line, control-free version of a name.

    Everything here reaches a terminal's title parser, so a stray ESC or BEL in a
    session name would end the sequence early and leave the rest of it printed on
    the user's screen. Newlines do the same. This is the only place that can
    happen, and it is cheap to make impossible.
    """
    out = "".join(ch for ch in (text or "") if ch.isprintable())
    return " ".join(out.split())


def compose(nickname: str, name: str, project: str = "") -> str:
    """`Gil · Rewrite the sprite pipeline`, or "" when there is nothing to say.

    An empty nickname returns empty rather than falling back to the description:
    without the identity this is just a worse version of the title Claude Code
    was writing before we disabled it, and writing it would be a downgrade the
    user did not ask for. Better to leave the tab alone.
    """
    badge = short(nickname)
    if not badge:
        return ""
    rest = _clean(name) or _clean(project)
    title = f"{badge} · {rest}" if rest else badge
    return title[:MAX_TITLE]


#: What a tab Dark Army itself opened says about *why*, before the card's title —
#: the words `daemon_board` gave the terminal at the spawn, kept so a planning
#: tab and a building tab for one card still read apart. Start has no prefix:
#: building is the default reading of a card's name.
ORIGIN_PREFIX = {"card-refine": "refine: ", "card-consult": "ask: "}

#: `daemon.UNNAMED_SESSION`, spelled here rather than imported: this module
#: is pure and the daemon imports it, not the other way round.
_UNNAMED = "New session"


def title_for(entry: dict) -> str:
    """The tab title for one snapshot row, or "".

    `compose` over the row's identity and its name, with two things a card
    session knows that a hand-started one does not: a row still called
    `New session` wears the card's title (`card_title` rides the row exactly
    when that judgment has been made), and a row Dark Army opened to plan or to
    answer keeps the verb its terminal was opened with, so the tab reads
    `Gid · refine: Mobile cards` and not merely the card's name. A VS Code
    terminal opened with a fixed name never renders a title written to it,
    which is why the extension leaves the name off and this is the one
    writer for every tab, launched or not.
    """
    name = _clean(entry.get("name", ""))
    card_title = _clean(entry.get("card_title", ""))
    if card_title and (not name or name == _UNNAMED):
        name = card_title
    if name:
        name = ORIGIN_PREFIX.get(str(entry.get("origin_by") or ""), "") + name
    return compose(entry.get("nickname", ""), name, entry.get("project", ""))


def tty_path_for(pid: int) -> str:
    """Absolute path of a process's controlling terminal, or "".

    `ps -o tty=` answers `s004` on macOS, `??` for a process with no terminal
    (a background agent, a daemon-launched session) — and those get no title,
    which is correct: there is no tab to name.
    """
    try:
        r = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True, timeout=1.0,
        )
        raw = r.stdout.decode(errors="replace").strip()
    except (subprocess.SubprocessError, OSError):
        return ""
    if not raw or raw == "??" or "?" in raw:
        return ""
    return raw if raw.startswith("/dev/") else f"/dev/{raw}"


class TitleWriter:
    """Keeps every live session's terminal tab named after its agent.

    Stateful for one reason: `_written` remembers what each session's tab already
    says, so a snapshot that changed nothing writes nothing. The snapshot runs
    every few seconds all day, and a pty write per session per tick — for a string
    that is identical to the one before it — is the kind of habit this codebase
    keeps having to remove.
    """

    def __init__(self, enabled: bool = True, resolver=tty_path_for, owned=None):
        self.enabled = enabled
        self._resolve = resolver
        # `owned(pid)` — `ptyhost.owns` — names the sessions running on a
        # terminal Dark Army itself hosts. Those are skipped: the escape would land
        # in Dark Army's own stream, and the pane draws the name itself.
        self.owned = owned
        self._written: dict[str, tuple[str, str]] = {}
        self._ttys: dict[int, str] = {}

    # ── the tty of a pid, remembered ─────────────────────────────────────────
    def _tty(self, pid: int) -> str:
        """Cached `ps` lookup. A process's controlling terminal does not change,
        and the cache is pruned by pid each pass, so a recycled pid cannot inherit
        the previous owner's tty for longer than one snapshot."""
        if pid not in self._ttys:
            self._ttys[pid] = self._resolve(pid)
        return self._ttys[pid]

    def _write(self, tty: str, title: str) -> bool:
        """Push the primer and the title at a terminal, one write. False on any
        refusal.

        Non-blocking on open *and* on write: this runs on the snapshot's executor
        thread, and a terminal whose reader has wedged (a stopped process, a
        detached ssh session) must cost us nothing. A partial write is treated as
        a failure so the next pass retries the whole sequence rather than sending
        the tail of one — which is also why primer and title share a write: half
        a payload must never count as delivered.
        """
        payload = (_OSC_TITLE.format(_PRIMER)
                   + _OSC_TITLE.format(title)).encode("utf-8", "replace")
        try:
            fd = os.open(tty, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return False
        try:
            return os.write(fd, payload) == len(payload)
        except OSError:
            return False
        finally:
            os.close(fd)

    # ── one pass over a snapshot ─────────────────────────────────────────────
    def apply(self, snapshot: dict, codex_ttys=None) -> None:
        """Name every live Claude, Grok, and Codex tab. Never raises."""
        if not self.enabled:
            return
        codex_ttys = codex_ttys if isinstance(codex_ttys, dict) else {}
        owned = self.owned

        # tty → the row that gets to name it. Ties broken by who moved most
        # recently, so after `/clear` the new session takes the tab from the old
        # one rather than the two of them trading it every few seconds.
        owner: dict[str, tuple[float, dict]] = {}
        pids: set[int] = set()
        for bucket in LIVE_BUCKETS:
            for entry in snapshot.get(bucket) or []:
                provider = entry.get("provider") or "claude"
                if provider not in ("claude", "grok", "codex"):
                    continue
                sid = entry.get("session_id")
                if not sid:
                    continue
                if provider == "codex":
                    tty = codex_ttys.get(sid, "")
                    if not isinstance(tty, str):
                        tty = ""
                else:
                    pid = entry.get("pid")
                    if not pid:
                        continue
                    try:
                        pid = int(pid)
                    except (TypeError, ValueError):
                        continue
                    if owned is not None:
                        try:
                            if owned(pid):
                                continue
                        except Exception:
                            pass
                    pids.add(pid)
                    tty = self._tty(pid)
                if not tty:
                    continue
                idle = float(entry.get("idle_seconds") or 0)
                held = owner.get(tty)
                if held is None or idle < held[0]:
                    owner[tty] = (idle, entry)

        named: set[str] = set()
        for tty, (_idle, entry) in owner.items():
            title = title_for(entry)
            if not title:
                continue
            sid = entry["session_id"]
            named.add(sid)
            # New sessions leave the title to Dark Army through provider config. An
            # already-running provider may still replace this one write, but
            # reasserting every snapshot recreates the two-writer oscillation.
            if self._written.get(sid) == (tty, title):
                continue
            if self._write(tty, title):
                self._written[sid] = (tty, title)

        # Forget rows that have left the live buckets, and pids that left with
        # them. Both maps are otherwise per-session dicts that only grow — the
        # failure mode this codebase has already had to fix several times over.
        for sid in set(self._written) - named:
            del self._written[sid]
        for pid in set(self._ttys) - pids:
            del self._ttys[pid]


__all__ = ["TitleWriter", "compose", "title_for", "short", "tty_path_for",
           "LIVE_BUCKETS", "MAX_TITLE", "SHORT_LEN", "ORIGIN_PREFIX"]
