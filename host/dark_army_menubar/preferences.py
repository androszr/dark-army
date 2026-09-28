# host/dark_army_menubar/preferences.py
"""Persistent preferences for the Dark Army menubar app.

Removed keys still on disk are kept by ``save_preferences`` and ignored by
every reader.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

from dark_army_daemon.paths import PREFS_PATH, STATE_DIR, ensure_state_dir

logger = logging.getLogger("dark-army.menubar")

DEFAULTS = {
    # How long a session may go without an event before the daemon evicts
    # it — the one value the daemon reads at startup.
    "session_timeout": 300,
    # Dark Army naming a session Claude Code no longer names (session_title.py).
    # The ⋯ row is gone; the key stays so a stored false still wins — this
    # is the one preference that sends transcript-derived text off the
    # machine, and an upgrade must not re-enable it behind somebody's back.
    "session_title": True,
    "notification_sound": True,
    # macOS banners for alerts that have earned an interruption. On by default
    # like the chime, and safe to default on where the AI-title preference below
    # is not: macOS asks its own permission question before a single banner
    # appears, so the user gets the last word whatever this says.
    "notification_banners": True,
    # Dark Army's channel: the MCP server that lets the daemon put a line in front of
    # a running session, and relay its permission prompts back out. Off, and
    # unlike every other row here it stays off for new installs too — it cannot
    # be switched on for a session that is already open. Turning it on installs
    # the server and prints the command a session has to be *started* with, so
    # a default-on would put a preference in the menu that appears to do nothing.
    "channel_enabled": False,
    # Reply by typing: deliver the panel's and the phone's free-text reply by
    # `sendText` onto the session's own input line in its VS Code terminal,
    # so a session does not have to be *started* with the channel command to
    # be answered. Reaches only a stopped session in a VS Code window whose
    # extension is 0.1.6+; the channel stays the fallback. Off for the MVP so
    # a default install behaves byte-for-byte as it did before the switch
    # existed — it is a parallel option to prove, not yet the migration.
    "typed_reply": False,
    # Off here on purpose, and *not* the value a new user gets: a fresh install
    # writes it on (see first_run.py), while an existing one that never touched
    # the row keeps it off — flipping this default would opt them in behind their
    # back, and it is the only preference that sends transcript-derived text off
    # the machine. Until it is on, the Product tab groups by project.
    #
    # Run `/compact` at a session that is out of context instead of raising a
    # banner asking a person to type it (autocompact.py). Safe to default on
    # even though it *writes into* a session, because it can only reach one
    # sitting in a VS Code window whose extension is new enough to type —
    # a session in Terminal.app, or an unreloaded 0.1.5 window, is never
    # touched, and the banner arrives exactly as it always did.
    "auto_compact": True,
    # Whether Dark Army may *start* a session from the Kanban board (dispatch.py).
    # On by default, and the argument for that is the arm-then-confirm: a card
    # only ever launches because somebody pressed Start twice on a card they
    # were looking at, in a project Dark Army recognises. A switch defaulted off would
    # make the board's central verb look broken on a fresh install, for a gate
    # the button already has.
    #
    # What turning it off costs is exactly one thing: the board becomes a list.
    # Writing, editing, moving and finishing cards all still work, and Start is
    # *absent* from every card rather than present and inert — `dispatch_enabled`
    # rides in the board snapshot for that reason. It is here for anyone who
    # wants Dark Army to go on only watching, which is what it did for its whole life
    # before this.
    #
    # **The upgrade path was considered, and it is the harder case.** A default
    # is not only a fresh-install choice: a `DEFAULTS` entry reaches back over
    # every existing preferences file that has no key for it, so shipping this
    # on opts every current user into "Dark Army may spawn processes" without being
    # asked — the exact reach-back `first_run.py` exists to avoid for the boxes
    # it ticks. It is accepted here because the *preference is not the gate*.
    # The gate is the armed-then-confirmed Start button, which no upgrade can
    # press; an upgraded user with no board has no cards, and a card cannot
    # start itself: the queue's drain is the one thing that reaches
    # `dispatch.spawn` without a fresh press, and nothing enters that queue
    # without a person having pressed Start on that specific card. What the default actually grants is a visible
    # button on a surface that did not exist before the upgrade, which is a
    # different thing from a capability switched on behind somebody's back.
    "board_dispatch": True,
    # Whether a card Dark Army starts runs on a terminal **Dark Army itself owns** — a
    # pty the daemon opened, drawn in the panel and streamed to the phone —
    # rather than in the project's VS Code window. **Off**, and the default
    # is the point: on, Start works with no editor window open at all, and
    # that is a different place for a session to live, so nothing changes
    # for anybody until they turn it on. Refinements always take the editor
    # path whatever this says. Read by `_dispatch_card_locked` at the moment
    # of the press.
    "board_own_terminal": False,
    # Whether Dark Army starts the head of a project's queue by itself when the
    # files it declares come free. **On**, and the argument is narrower than
    # "automation is convenient": nothing is ever in a queue without a person
    # having pressed Start on that specific card or dropped it into In
    # progress, so the drain only ever finishes a gesture already made.
    # Defaulted off, the queue becomes a parking lot where cards a person
    # explicitly started silently never start — the board asserting an intent
    # nobody is serving. Off removes the drain and nothing else: cards still
    # queue, order is still kept, and a press still starts one; a queued
    # card's own wording changes to say so, because a card must never promise
    # an action Dark Army will not take. `board_dispatch` above still removes the
    # whole capability, this switch included.
    "board_autostart": True,
    # How many agents may be working at once in any one project — the whole
    # waiting rule, as of 24 Aug 2026. A *slot* is a card of that project an
    # assistant is actually working (`board_queue.holds`: Done holds nothing,
    # a hand-check flag holds nothing, a `live` card whose session Dark Army can no
    # longer hear holds nothing). A press on a card whose project has no free
    # slot queues it; the drain starts it when one frees.
    #
    # **1, and the default is the point.** At 1 a project runs one agent at a
    # time, which is what an upgrade should keep doing until somebody turns
    # the dial — a default above 1 would reach back over every existing
    # preferences file and let two agents into one tree without being asked.
    # The one place 1 is *stricter* than the file gate it replaces: two cards
    # whose plans declared disjoint file tables could run together, and now
    # the second waits.
    #
    # **Raising it accepts two agents in one tree**, including the same file,
    # with nothing mitigating it — that is the trade the file gate used to
    # make the other way, and it was replaced deliberately rather than kept
    # as a quiet extra condition. Clamped to 1..4 by the daemon's own setter,
    # so a hand-edited 50 runs at 4 and the panel's readout never shows a
    # number Dark Army is not obeying.
    "board_parallel": 1,
    # And which projects say otherwise: `{canonical project root: int}`, the
    # per-project override map over `board_parallel` above. An absent root
    # means the shared default; the values are clamped to 1..4 by the
    # daemon's own setter, for `board_parallel`'s stated reason (one bound,
    # not two that can drift). Additive and downgrade-safe: an older build
    # never reads the key and `save_preferences`' read-modify-write keeps it.
    "board_parallel_by_root": {},
    # Card isolation, per project (`docs/card-worktrees.md`): `{canonical
    # project root: false}`. On by default for every git project — a Start
    # works on its own branch in its own `.worktrees/` folder — so only the
    # projects that switched it **off** are stored, and switching it back on
    # removes the row. Additive and downgrade-safe: an older build never
    # reads the key and `save_preferences`' read-modify-write keeps it.
    # **Never renamed.**
    "board_isolation_by_root": {},
    # Which model each agent and helper runs on, per assistant:
    # `{provider: {slot: model}}` over `agent_models.PROVIDERS` × `SLOTS`
    # (`main` plus the seven roles). An absent provider or slot means the
    # shipped value (`agent_models.SHIPPED`); `""` means Default — no
    # `--model` flag on the argv and no `model:` line in the brief — and is
    # an explicit choice, never read as absent. Values are validated at the
    # setter (`_set_agent_model`, against `agent_models.allowed`) and again
    # by the daemon's `_agent_model_for` on the way out, so a hand-edited
    # name Dark Army does not know is unset rather than launched. Additive
    # and downgrade-safe: an older build never reads the key and
    # `save_preferences`' read-modify-write keeps it. **Never renamed.**
    "agent_models": {},
    # And which projects say otherwise: `{root: {provider: {slot: model}}}`,
    # the per-project override map over `agent_models`. A missing slot
    # inherits the machine-wide value; `"inherit"` on the wire removes the
    # entry rather than being stored. Same validation, same downgrade
    # safety, same rule about the name.
    "agent_models_by_root": {},
    # The panel's size as a percent of the design (100/125/150/175). **100**,
    # because an upgrade must change nothing on screen. The offered steps live
    # in the panel's `PanelScale`; this file only stores the number. The key
    # is never renamed. An out-of-range stored value is drawn at 100 rather
    # than refused — one resolution seam, in the panel.
    "panel_scale": 100,
    # Whether a card arriving in Done disposes the session's terminal tab.
    # Only the toggle ever writes this key, so every stored value is a
    # deliberate press and sticks.
    "board_close_terminal": True,
    # Bind `/api/state` on 0.0.0.0:19875 so a paired phone on the same Wi-Fi
    # can read the fleet and the board. **Off** by default: this is the first
    # time Dark Army's data leaves loopback, and the per-device token is the entire
    # boundary on that socket. Off means nothing on the network can reach Dark Army
    # at all, which is today's behaviour. A switch that opens a LAN door is
    # not a thing an upgrade should flip on behind somebody's back.
    "lan_access": False,
    # Whether the paired phone keeps working *away* from home, through the
    # sealed mailbox the daemon polls (relay.py / relay_client.py). **Off**
    # by default and a second key deliberately — the one above means "bind
    # 19875 on the LAN", and reusing it would silently turn on an internet
    # path for everyone who already had LAN on. New reach = new consent.
    "remote_access": False,
    # Whether the Mac also holds one live socket per paired phone to the
    # socket relay (the relay socket module and the socket relay folder)
    # beside the mailbox — the away link's fast lane, a trial. **Off** by
    # default, its own key and never renamed: inert without `remote_access`
    # (the socket is started only while both are on) and without a socket
    # address, and the mailbox stays the fallback whatever this says. New
    # reach = new consent.
    "relay_ws": False,
    # Buzz the paired phone (APNs, through the relay's push route) at the
    # moments a Mac banner is raised. On by default because it is inert
    # until three other consents exist — `remote_access` on, a phone paired
    # while it was on, and that phone allowing notifications — each of which
    # is a deliberate act; this key exists so the buzzes can be switched off
    # *without* tearing any of that down. A new key, never a rename.
    "phone_push": True,
    # The dictation hotkey MacWhisper is bound to, recorded once. Empty means
    # none has been recorded, which is also what an older build writes back.
    # Stored as {key_code, modifiers, label} when a shortcut is recorded; an
    # empty dict is "no shortcut", not a wildcard.
    "dictation_shortcut": {},
}


def load_preferences(path: Path = PREFS_PATH) -> dict:
    """Load preferences from disk, merged with defaults for missing keys."""
    if path.parent == STATE_DIR:
        ensure_state_dir()
    result = dict(DEFAULTS)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        # A file holding valid JSON that is not an object (a list, a string)
        # would raise out of `update` and take the whole launch down with it.
        if isinstance(stored, dict):
            result.update(stored)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return result


def save_preferences(path: Path = PREFS_PATH, updates: dict = None) -> None:
    """Read-modify-write: load existing, merge updates, save back."""
    if updates is None:
        updates = {}
    if path.parent == STATE_DIR:
        ensure_state_dir()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    existing.update(updates)
    # Write-then-rename. A truncating write that dies partway leaves an
    # unparseable file, and the loader above treats that exactly like a missing
    # one — so a crash mid-save silently resets every preference, including the
    # opt-outs `first_run` promises never to re-tick. A unique `mkstemp`
    # sibling rather than a fixed `.tmp` name: two writers racing on one fixed
    # temp path can interleave and rename each other's half-written file.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(existing, indent=2) + "\n")
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
