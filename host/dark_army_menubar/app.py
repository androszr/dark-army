"""The menu-bar app: the status item, the panel it opens, and the thread
the daemon runs on inside this same process."""

import asyncio
import json
import logging
import math
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple, Optional

import rumps
import objc
from Foundation import NSObject
from PyObjCTools.AppHelper import callAfter

from dark_army_daemon import agent_models
from dark_army_daemon.daemon import BobDaemon, DaemonObserver
from . import (
    channel_install, hooks, kill_switch, launchd, dev_build, first_run, logsetup,
    launch_report, menu_format, notifier, pack_install,
    pack_ledger, pack_render, self_restart, statusline, vscode_extension,
)
from .logsetup import log_file_path
from . import preferences
from .preferences import load_preferences, save_preferences
from .version import get_version

from .panel_process import PanelProcess

logger = logging.getLogger("dark-army.menubar")


def _tilde(path) -> str:
    """`path` as a person would type it: the home folder as `~`. Every path
    a sentence quotes is derived from its constant through this, so a sentence
    cannot go on naming a folder that has moved."""
    text = str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text

#: What this launch did to hooks, the editor extension and the login item —
#: one per process, because there is one launch per process. Written by
#: `main()` (hooks, first run), the `vscode-ext-install` worker (extension)
#: and the stale-plist repair in `__init__`; shipped down the panel context
#: as `settings["launch"]`. Ephemeral: never persisted, never a ledger.
LAUNCH_REPORT = launch_report.LaunchReport()

#: The running app, once `main()` has built it, so a worker that finishes
#: after construction can ask for a context push. `None` before that and
#: under tests; a result landing then is simply held in `LAUNCH_REPORT`
#: until the first push ships it.
_RUNNING_APP = None


def _report_extension_result(status: str, detail: str) -> None:
    """`vscode_extension.ensure_installed`'s callback. Worker thread: records
    the result and hops a **non-respawning** push onto the main thread — a
    background completion may update the line, never open a panel."""
    LAUNCH_REPORT.set(launch_report.EXTENSION, status, detail)
    app = _RUNNING_APP
    if app is not None:
        callAfter(app._push_panel_context, False)


def _record_hook_outcome(before_current: bool, after_current: bool,
                         error: bool = False) -> None:
    """Hooks, judged by comparing the managed script and configuration
    before and after the ordinary conditional install — never by re-running
    an installer to measure it. `changed` needs the after-state to actually
    read as current; an install that ran and still does not is `failed`."""
    if error:
        LAUNCH_REPORT.set(launch_report.HOOKS, launch_report.FAILED,
                          "hook install raised; see the log")
    elif before_current and after_current:
        LAUNCH_REPORT.set(launch_report.HOOKS, launch_report.UNCHANGED,
                          "hooks current")
    elif after_current:
        LAUNCH_REPORT.set(launch_report.HOOKS, launch_report.CHANGED,
                          "hooks installed")
    else:
        LAUNCH_REPORT.set(launch_report.HOOKS, launch_report.FAILED,
                          "hooks still not installed")


def _repair_login_item_if_stale() -> None:
    """Rewrite a login item that names another interpreter or module, and
    record what happened on the launch line. Called from the app's
    `__init__` on the AppKit thread, as the repair always was.

    The repair's outcome replaces first run's report of the login item.
    Written, deliberately not loaded — a `RunAtLoad` job bootstrapped now
    would start a second copy mid-launch — so it is a change effective at
    the next login, the carry's rule. **A failure never stops the launch**:
    every upgraded Mac takes this path once, and a refused `launchctl` or a
    full disk is a `failed` login item and a log line, not an app that does
    not start (`main()` treats the carry the same way)."""
    try:
        if not (launchd.is_enabled() and launchd.is_stale()):
            return
        logger.info("The login item names another interpreter or module; rewriting it")
        result = launchd.repair_stale_login_item()
    except Exception as exc:
        logger.warning("could not repair the login item", exc_info=True)
        status, detail = launch_report.login_outcome(None, error=exc)
        LAUNCH_REPORT.set(launch_report.LOGIN_ITEM, status, detail)
        return
    LAUNCH_REPORT.set(launch_report.LOGIN_ITEM,
                      launch_report.CHANGED if result.written
                      else launch_report.FAILED, result.detail)


def _write_hook_scripts() -> None:
    """The three scripts the hook settings name, each rewritten only when its
    content differs: the notify handler, the `/ship` close-out helper and the
    shunt guard. Always before `hooks.install_hooks()`: a settings group
    naming a script that is not there yet refuses every Read and Bash call in
    every Claude session on the machine (the 20 Sep 2026 lock-out)."""
    hooks.install_notify_script()
    hooks.install_close_out_script()
    hooks.install_shunt_script()


def _hooks_current() -> bool:
    """The whole managed hook surface as installed right now: the handler
    script byte-current *and* both harnesses' configuration current."""
    return (hooks.script_current(hooks.NOTIFY_SCRIPT_PATH, hooks.NOTIFY_SCRIPT)
            and hooks.are_hooks_installed())

# NSEvent.ModifierFlags device-independent bits, the same values CGEventFlags
# uses, so a recorded mask can be handed straight to CGEventSetFlags.
_DICTATION_SHIFT = 1 << 17
_DICTATION_CONTROL = 1 << 18
_DICTATION_OPTION = 1 << 19
_DICTATION_COMMAND = 1 << 20
_DICTATION_MODIFIER_MASK = (
    _DICTATION_SHIFT | _DICTATION_CONTROL | _DICTATION_OPTION | _DICTATION_COMMAND
)
# Carbon virtual key codes for the modifier keys a push-to-talk shortcut
# may be. Caps Lock (57) and Fn (63) stay out — Caps Lock latches.
_DICTATION_MODIFIER_KEY_FLAGS = {
    54: _DICTATION_COMMAND,  # right Command
    55: _DICTATION_COMMAND,  # Command
    56: _DICTATION_SHIFT,    # Shift
    60: _DICTATION_SHIFT,    # right Shift
    58: _DICTATION_OPTION,   # Option
    61: _DICTATION_OPTION,   # right Option
    59: _DICTATION_CONTROL,  # Control
    62: _DICTATION_CONTROL,  # right Control
}
# IOLLEvent.h device-dependent bits. A real right-Command flagsChanged
# carries NX_DEVICERCMDKEYMASK (0x10) on top of the independent Command
# bit; CGEventSetFlags with only the independent mask strips it, and
# MacWhisper's dictationKeyboardButton is "rightCmd".
_DICTATION_DEVICE_BITS = {
    54: 0x0010,  # NX_DEVICERCMDKEYMASK
    55: 0x0008,  # NX_DEVICELCMDKEYMASK
    56: 0x0002,  # NX_DEVICELSHIFTKEYMASK
    60: 0x0004,  # NX_DEVICERSHIFTKEYMASK
    58: 0x0020,  # NX_DEVICELALTKEYMASK
    61: 0x0040,  # NX_DEVICERALTKEYMASK
    59: 0x0001,  # NX_DEVICELCTLKEYMASK
    62: 0x2000,  # NX_DEVICERCTLKEYMASK
}
# Long enough for any real dictation; short enough to bound a stuck ⌘.
DICTATION_HOLD_MAX_SECONDS = 120
_MACWHISPER_APP = "/Applications/MacWhisper.app"


def ax_is_process_trusted() -> bool:
    """Whether this process may post synthetic key events.

    Read live, never latched: a grant can be given while the app runs.
    Failure is false — the Dictate button is absent when this is false.
    """
    try:
        import ctypes
        import ctypes.util
        name = ctypes.util.find_library("ApplicationServices")
        if not name:
            return False
        lib = ctypes.CDLL(name)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        logger.debug("AXIsProcessTrusted failed", exc_info=True)
        return False


def macwhisper_installed() -> bool:
    return os.path.isdir(_MACWHISPER_APP)


def _dictation_event_flags(key_code: int, modifiers: int, down: bool) -> int:
    """Flags for a synthesized dictation event.

    A modifier key's own mask bit is set on down and cleared on up, so a
    listener watching flagsChanged sees the same gain/loss as a real press.
    Ordinary keys keep the recorded mask both ways. Sided modifiers also
    carry their device-dependent bit (NX_DEVICERCMDKEYMASK etc.).
    """
    flags = modifiers & _DICTATION_MODIFIER_MASK
    own = _DICTATION_MODIFIER_KEY_FLAGS.get(key_code)
    if own is None:
        return flags
    device = _DICTATION_DEVICE_BITS.get(key_code, 0)
    if down:
        return flags | own | device
    return (flags & ~own) & ~device


def post_dictation_key(key_code: int, modifiers: int, down: bool) -> None:
    """Post one key-down or key-up at the HID tap. Raises; callers log."""
    import Quartz
    flags = _dictation_event_flags(key_code, modifiers, down)
    event = Quartz.CGEventCreateKeyboardEvent(None, key_code, down)
    if event is None:
        raise RuntimeError("CGEventCreateKeyboardEvent returned None")
    Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    logger.info(
        "Dictate post %s key=%s flags=0x%x type=%s",
        "down" if down else "up", key_code, flags, Quartz.CGEventGetType(event),
    )


# --- Menu-bar status strip (one clickable item, up to 3 animated Bobs) ---
# The menu bar shows a compact "command center": one animated face per active
# category, each with a count. Empty categories are hidden.
# work = agents working (count plus a superscript "+M" for subagents), idle =
# idle sessions, attn = sessions with a pending notification. Numbers are drawn
# with an adaptive label color so they never invert to white on click.
#
# There is no separator glyph between groups. There used to be a "   |   " set at
# the counts' own size and colour — a divider drawn at data weight is a fourth
# number shape the eye has to discard, and it cost ~24pt a gap for something the
# next face already does: every group opens with a sprite, so the sprite *is* the
# delimiter. What separates groups now is measured whitespace (kerning), which is
# the idiom Apple's own status items use.
# One face per live category (working, needs-you), never a roster. A face each
# was tried and it pushed the backlog count off the bar. The named portrait is
# used only when a category holds a single agent; otherwise the aggregate glyph
# plus the count. Sleeping sessions are not drawn.
def _icon_exists(name: str) -> bool:
    """Whether an icon file is on disk.

    Memoised: it is asked once per group on every render, five times a second,
    and the answer cannot change while the app is running. Resolved through
    `importlib.resources` like `_icon_path`, so it is right inside the py2app
    bundle as well as in a checkout.
    """
    hit = _ICON_EXISTS.get(name)
    if hit is None:
        import importlib.resources
        try:
            path = importlib.resources.files("dark_army_menubar") / "icons" \
                / f"{name}.png"
            hit = path.is_file()
        except Exception:
            hit = False
        _ICON_EXISTS[name] = hit
    return hit


def _held(category: str, frames: list) -> list:
    """One frame for a still category, the whole cycle for an animated one.

    Module level rather than a method so both of `_frames_for`'s branches —
    the aggregate glyphs and the named cast faces — go through the one rule,
    and so a test can reach it without an instance.
    """
    index = STILL_FRAMES.get(category)
    return frames if index is None else [frames[index]]


_ICON_EXISTS: dict[str, bool] = {}

# One face per live category, never a roster. A face each was tried and it
# pushed the backlog count off the bar: four working plus idle plus waiting
# is a crowd, and the ladder's first concession was the to-do stack. Working
# and needs-you get at most one portrait (their own, when the category holds
# a single named agent) and always the count beside it. Idle is capped at
# zero because sleeping sessions are not drawn at all.
MAX_FACES = {"work": 1, "idle": 0, "attn": 1}
GAP_FACE = 5.0           # between faces of one category — closer than GAP_GROUP,
                         # because they are one group rather than two.
# Categories may differ in frame count — each list is indexed independently
# (mod its own length), so the 8-frame working cycle and the 4-frame attention
# one coexist. Working is longer because it carries a sway, a nod and a blink.
ICON_STRIP_FRAMES = {
    "work": [f"dark-army-work-{i}" for i in range(8)],
    "attn": ["dark-army-attn-0", "dark-army-attn-1", "dark-army-attn-2", "dark-army-attn-3"],
}
# The sleeping face's "Zzz" must contrast with the bar: black on a light menu
# bar, white on a dark one. The character is unchanged either way; only the Zzz
# differs, so we bake two variants and pick by the current menu-bar appearance.
ICON_IDLE_FRAMES = {
    "light": ["dark-army-idle-light-0", "dark-army-idle-light-1", "dark-army-idle-light-2", "dark-army-idle-light-3"],
    "dark": ["dark-army-idle-dark-0", "dark-army-idle-dark-1", "dark-army-idle-dark-2", "dark-army-idle-dark-3"],
}
# Which single frame a still category holds. Motion on the strip means one
# thing — somebody is working — so a resting face and a waiting face are
# states, not activity, and hold one picture. The index is per category
# because the pose that *says* the state is not in the same place in the two
# cycles: the sleeping face's Zzz is only fully drawn in the last slot, and
# the waiting face's red is in the first half alone (dark-army-attn-2 carries no
# red at all). A category absent from this table animates.
STILL_FRAMES = {"idle": -1, "attn": 0}
# Offline glyph. Rendered through the strip like every other category — never as
# the button's *image*. The button can carry an image and an attributed title at
# the same time, so mixing the two mechanisms left the stale strip visible
# underneath the offline icon (two faces, no counts). One path only.
#
# Two variants, for the same reason as ICON_IDLE_FRAMES. This glyph is pure black
# ink, and it used to be legible on a dark bar only because it was set through
# rumps' template mode, which hands the image to AppKit for tinting. The strip
# draws frames as NSTextAttachments instead, and attachments are never tinted —
# so the black-on-black would vanish exactly when the daemon is down. The dark
# variant is the same silhouette with the ink inverted to white.
ICON_DISCONNECTED = {
    "light": "dark-army-disconnected",
    "dark": "dark-army-disconnected-dark",
}
# Animation tick, in seconds. This is the most expensive number in the app, and
# worth knowing the price of before touching it: a per-thread reading of the
# running process put 91% of its CPU (2.07% of a core, continuously) on the UI
# thread, against 0.19% for the daemon and all its asyncio work put together.
# Most of that is not this file's Python — composing a strip measures ~0.2ms —
# but the AppKit repaint that setAttributedTitle_ schedules afterwards, which is
# why profiling the render function alone makes the strip look nearly free.
#
# 0.4 was tried and reverted. It does halve the cost (measured 2.53% -> 1.00%
# for the whole process), but 2.5fps reads as a stutter rather than a slower
# animation, and the strip is the app's face. So the quiet categories were
# frozen instead (STILL_FRAMES): idle and attn hold a single frame each, which
# composes with the repaint-skip in _render_strip — a Mac with nothing working
# now builds an identical strip every tick and repaints none of them. The
# animating tick is deliberately still 0.2s, because the working figure is the
# one thing whose motion carries meaning.
#
# The clock rests when nothing moves. While no category on the bar animates
# (`_strip_animating`, read off STILL_FRAMES) the strip's own timer runs at
# ICON_TICK_REST instead, so a quiet Mac stops waking the processor five times a
# second to build a strip it will not repaint. A slow tick rather than a stopped
# one, deliberately: six inputs land on the instance without any wake — the
# counts, the to-do count, the faces, the usage limits, the menu bar's
# appearance and a dead daemon thread (the offline glyph) — and one second at
# rest keeps each of them within a second. The one that means motion does not
# wait: `on_activity_change` pokes the strip (`_strip_wake`) on a count change,
# so a session starting work draws its first frame at once and puts the clock
# back on ICON_TICK.
ICON_TICK = 0.2
ICON_TICK_REST = 1.0
STRIP_ICON_PT = 20.0     # displayed height of each face in the menu bar (points)
                         # The status bar is 22pt (NSStatusBar.thickness), so 17
                         # left three points unused above and below. The other
                         # half of the win is in the art: icons are cropped tight
                         # to each state now, where a shared per-character canvas
                         # sized for the tallest pose had one working figure
                         # filling 60% of its box and another 64%, against 97–98%
                         # for the tightest-drawn. One drew at 10.2pt beside a
                         # 16.6pt neighbour; every face is 20pt now. The strip
                         # keeps hand-drawn pixel art at this size on purpose —
                         # the panel and the phone draw photographs, and a
                         # photograph at 20pt is a smudge.

# Counts are set a point smaller than the menu-bar default and a weight heavier:
# 12pt Medium sits denser and more deliberate against a 17pt sprite than 13pt
# Regular, which reads thin and incidental next to it. Monospaced *digits* above
# all — with proportional figures a count ticking 9→10→9 changes the run's width
# and shuffles the whole strip, chip included, several times a minute.
COUNT_PT = 12.0
# The subagent count is a footnote on the working count, not a second count, so
# it is set smaller, dimmer, and raised — the typographic form for exactly that
# relationship. It also drops the parentheses "3 (+2)" spent two glyphs on.
SUFFIX_PT = 9.5
SUFFIX_RISE = 2.5        # baseline offset, points

# Gaps, in points of kerning rather than spaces: a space is one font-defined
# width and we need three different ones.
GAP_FROG = 5.0           # face → its own count. 3pt is the textbook figure and
                         # was a point too tight here: the working face's laptop
                         # reaches the sprite's right edge and touched the digit.
GAP_GROUP = 12.0          # a count → the next category's face
GAP_USAGE = 20.0         # the last count → the usage cluster (which is not a
                         # session number, and says so by standing apart)
USAGE_MARK_PT = 8.0      # provider mark beside each usage percentage
USAGE_MARK_GAP = 3.5     # mark → digits. Tight: they are one object, not two.
GAP_GROK_USAGE = 10.0    # each usage cluster → the rule between them. Applied on
                         # both sides, so the clusters sit 23pt apart. Still less
                         # than GAP_USAGE: the two are one block of the same kind
                         # of reading, and the boundary into that block from the
                         # counts is the larger of the two.
USAGE_RULE_PT = 9.0      # height of that rule; matched to the digits' cap height
USAGE_RULE_ALPHA = 0.22  # a rule, not a glyph — see _divider_image

# The five-hour usage cluster at the right-hand end.
USAGE_PT = 11.0          # a rank below the counts: an ambient account fact
USAGE_BAR_PT = 2.0       # the under-meter's height
USAGE_BAR_GAP = 2.0      # ...and its distance from the digits' descenders
USAGE_BAR_MIN_PT = 12.0  # a meter narrower than this can't show a percentage

# Cards sitting in the board's Backlog column (to-do): a stack of cards and the
# digits, drawn at the counts' own size rather than the budget clusters' — it is
# a count of work, not an ambient account reading, even though it is drawn
# rather than typed.
TODO_PT = 12.0
TODO_MARK_PT = 11.0      # the stacked-card mark beside the digits
TODO_MARK_GAP = 3.5      # ...and its distance from them, matched to USAGE_MARK_GAP

# macOS gives a status item no signal that its neighbours are crowding it, so the
# strip measures itself against a fixed budget and steps down this ladder until
# it fits. Each rung drops the least actionable thing still on screen: the
# subagent footnote, then the usage clusters one
# provider at a time — Codex, then Grok, then Claude. The
# to-do count is last — two faces plus their numbers were not enough to keep it
# on screen while it was the first concession, and the narrower strip exists so
# the backlog stays visible. The floor is faces, counts, and (until that last
# rung) the to-do stack.
#
# The under-meter is *not* a rung, though dropping it looks like an obvious first
# concession: it is exactly as wide as the digits above it, so removing it buys
# nothing horizontally. It costs height, and height is not what runs out.
# Raised from 170 when the strip gained a face per agent and honest margins, and
# from 280 when a third provider's cluster (Codex) tipped today's full reading —
# two faces, two counts, the to-do count and three usage clusters, ~290pt — past
# the budget and the ladder threw away all three percentages at once.
# 170 was set for a strip of three frogs and two numbers; the figure was always a
# self-imposed safety net rather than a system limit, and the ladder — not this
# constant — is what actually keeps the strip from crowding its neighbours. 300
# buys ~10pt of headroom over that reading, at the cost of a status item up to
# 20pt wider at its fullest, crowding the user's other menu-bar items by that
# much. That trade is a judgement, not a calculation.
# Raised again from 300 when the to-do count reached three digits: at 100 cards
# the same full reading measures 300.2pt — the third digit is 8pt — and the
# ladder dropped the Codex cluster for a fifth of a point. 310 restores the
# ~10pt of headroom over the widest everyday reading (three-digit to-do count).
STRIP_BUDGET_PT = 310.0

# The usage clusters this strip can draw.
ALL_USAGE_BRANDS = frozenset({"claude", "grok", "codex"})


class StripRung(NamedTuple):
    """One rung of the collapse ladder: what is still on the strip.

    Named fields, not bare positional booleans. The index discipline was already
    load-bearing enough to need its own comment and its own test, and widening
    the single usage column into three shifts the to-do flag from index 3 to
    index 5 — exactly the silent swap that comment warned about. Every field here
    is something the ladder can actually give up: a placeholder column kept only
    so positional tests stay numbered is how the retired idle flag survived its
    own deletion, and the replacement is a named-field pin, not another bool."""

    suffix: bool          # the subagent footnote
    usage_claude: bool
    usage_grok: bool
    usage_codex: bool
    todo: bool

    def usage_brands(self) -> frozenset:
        """The usage clusters this rung still draws."""
        return frozenset(b for b in ALL_USAGE_BRANDS
                         if getattr(self, f"usage_{b}"))


STRIP_LADDER = (
    # Every column is monotonically non-increasing: no rung re-enables anything,
    # or the width search would oscillate rather than settle.
    #
    # The usage clusters go one provider at a time, least actionable first: Codex,
    # then Grok, then Claude. Your own five-hour window is the budget you actually
    # act on, so it is the last one given up. To-do is the *last* thing of all: a
    # face each had been crowding it off, and the point of capping faces is that
    # the backlog stays on the bar.
    StripRung(True,  True,  True,  True,  True),    # everything
    StripRung(False, True,  True,  True,  True),    # - subagent suffix
    StripRung(False, True,  True,  False, True),    # - Codex usage
    StripRung(False, True,  False, False, True),    # - Grok usage
    StripRung(False, False, False, False, True),    # - Claude usage
    StripRung(False, False, False, False, False),   # - to-do (the floor)
)

# What the emergency menu's Restart row says once it has been clicked. The
# teardown takes as long as the daemon does (up to 8s) and the app disappears at
# the end of it, so the row's job in between is to say that this is expected — a
# greyed row alone reads as "the click didn't register".
RESTART_PENDING_TITLE = "Restarting… going down, please hold"
# The emergency menu's kill-switch row, before and after the click that arms it.
# Arm-then-confirm, the panel's rule for every destructive press: the row is a
# menu item, so the arming is the title changing rather than a second dialog.
KILL_SWITCH_TITLE = "Kill switch — stop everything"
KILL_SWITCH_ARMED_TITLE = "Click again to kill everything"
#: How long the emergency menu's kill-switch row stays armed before it goes
#: back to its plain title. Long enough to reopen the menu and click again,
#: short enough that an armed row is never waiting days later.
KILL_SWITCH_ARM_SECONDS = 10.0
#: How many run-loop passes the self-restart notice waits for the notification
#: authorization to be answered before saying it anyway. rumps fires a Timer on
#: the pass it is started, so pass 1 is immediate and the rest are `interval`
#: apart — ten passes at 3.0s is about half a minute, after which an unanswered
#: prompt is treated as "post and let macOS queue it" rather than "never say it".
ANNOUNCE_WAIT_PASSES = 10


def announce_should_wait(available: bool, authorized, waited: int) -> bool:
    """Whether the launch notice should hold for the authorization answer.

    Pure, and named apart from `main()`'s closure so it can be tested without a
    run loop. Waits only while there is an answer still coming: no bundle means
    no banner is ever possible, and a `False` means the user has said no — in
    both of those the notice is spent rather than held for ever.
    """
    return bool(available) and authorized is None and waited < ANNOUNCE_WAIT_PASSES


def _appearance_is_dark(view) -> bool:
    """True when the given view (the status-bar button) renders in the dark
    appearance. We query the BUTTON, not NSApp — the button's effectiveAppearance
    tracks the actual menu-bar appearance (the same context macOS uses to resolve
    labelColor), whereas NSApp.effectiveAppearance can disagree. Best-effort;
    defaults to light so the black-Zzz variant is chosen if detection fails.

    Memoised on the appearance object itself: the strip asks five times a second
    for an answer that changes when the user switches theme. Comparing the
    NSAppearance we were handed is what makes that safe — a theme switch hands us
    a different one, so the cache misses exactly when it should."""
    try:
        from AppKit import NSAppearanceNameAqua, NSAppearanceNameDarkAqua
        appearance = view.effectiveAppearance()
        cached = _APPEARANCE_CACHE.get(appearance)
        if cached is not None:
            return cached
        best = appearance.bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
        )
        result = bool(best == NSAppearanceNameDarkAqua)
        _APPEARANCE_CACHE.clear()          # only ever one live appearance
        _APPEARANCE_CACHE[appearance] = result
        return result
    except Exception:
        return False


_APPEARANCE_CACHE: dict = {}


class _StatusClickHandler(NSObject):
    """Intercepts clicks on the status item so both buttons open the panel.

    rumps hands the status item its `NSMenu`, and a status item with a menu never
    sends an action — the menu simply drops down. Taking the menu off the item is
    the only way to make a click *do* something, which is what "open the panel,
    not a menu" requires. The right button used to get the dropdown; the dropdown
    is gone, so it gets the panel too. It still routes through `_popup_menu`,
    which pops the emergency menu when there is one and otherwise opens the panel
    — the one case where a second button still has somewhere else to go.

    It also fixes the panel's behaviour rather than working around it. The panel
    misbehaved in three ways — it opened *behind* the menu, a second click
    re-showed it instead of hiding, and clicking away did not dismiss it — and
    all three were the same cause: the NSMenu opening took key from the panel, so
    `windowDidResignKey` fired before the second click could ever land. With no
    menu opening on the left button, none of that happens.
    """

    def initWithApp_(self, app):
        self = objc.super(_StatusClickHandler, self).init()
        if self is None:
            return None
        self._app = app
        return self

    def statusItemClicked_(self, sender):
        from AppKit import NSApp, NSEventTypeRightMouseUp, NSEventTypeRightMouseDown
        event = NSApp.currentEvent()
        right = False
        try:
            right = (event.type() in (NSEventTypeRightMouseUp,
                                      NSEventTypeRightMouseDown)
                     or (event.modifierFlags() & (1 << 18)))   # NSEventModifierFlagControl
        except Exception:
            pass
        if right:
            self._app._popup_menu()
        else:
            self._app._on_open_panel(None)


class BobCompanionApp(rumps.App, DaemonObserver):
    def __init__(self):
        super().__init__("Dark Army", quit_button=None)

        # The daemon and the loop it runs on live on their own thread, both
        # filled in by `_start_daemon_thread`; `_loop_ready` is set once the
        # loop exists (or the thread has given up).
        self._daemon: "Optional[BobDaemon]" = None
        self._loop: "Optional[asyncio.AbstractEventLoop]" = None
        self._loop_ready: threading.Event = threading.Event()
        # Set once the process is on its way out, so a second Restart click (or
        # the panel's button) does not start a second teardown.
        self._restarting = False
        # The emergency menu's kill-switch row, armed by one click and fired
        # by the next. The panel arms on its own surface and never reads this.
        self._kill_armed = False
        # The self-restart trio (self_restart.py). `_health_seen_tick` arms the
        # detector: the first health tick after launch never restarts, so the
        # window between __init__ and _start_daemon_thread cannot be read as a
        # death. `_restart_gave_up` latches once the hourly allowance is spent.
        # `_auto_restart_pending` is the *only* thing that tells `_restart_now`
        # this teardown was Dark Army's own idea — a person's press leaves it False,
        # writes no stamp and is never counted or refused.
        self._health_seen_tick = False
        self._restart_gave_up = False
        self._auto_restart_pending = False
        # Snapshotted key currently held down for MacWhisper, or None.
        # A re-recorded shortcut mid-hold must not strand the old key.
        self._dictation_hold = None
        self._dictation_timer = None
        self._dictation_deadline = None
        # The two long jobs a panel row can start. They used to be debounced by
        # taking the callback off a menu item; with no menu they are flags, and
        # they ride down in the settings block so the row can grey itself.
        self._rebuilding = False
        self._vscode_installing = False
        # Roots whose pack install is in flight. A set, not a bool: two
        # projects can be installing at once, and the panel greys per row.
        self._pack_installing = set()
        # Asked once: `_push_panel_context` runs on every panel action.
        self._pack_root = pack_install.pack_root()
        # Per-root `host/build.sh` stats. Filled on the limits worker (and
        # when enrolment.json actually changes), never on every context push.
        self._pack_self_roots: list[str] = []
        self._pack_self_roots_mtime = None
        # Per-category counts driving the menu-bar strip (see _render_strip). All
        # four come from the daemon's single bucketing (BobDaemon._activity_counts),
        # so the strip, the menu sections and the simulator HUD always agree.
        self._working_count = 0
        self._idle_count = 0
        self._attention_count = 0
        self._subagent_count = 0
        # Cards sitting in the board's Prep and Backlog columns (to-do): a card
        # waiting to be refined is still on the user's plate. A pure board fact
        # with no session in it, which is why it is a separate scalar rather
        # than a fifth thing `on_activity_change` counts.
        self._todo_count = 0
        # Menu-bar strip animation state (see _animate_icon)
        self._anim_i = 0
        # The strip's own clock, owned by the instance so `_animate_icon` can
        # re-arm it between ICON_TICK and ICON_TICK_REST (`_strip_clock_set`).
        # Constructing a rumps.Timer allocates no NSTimer; `main()` starts it.
        self._strip_timer = rumps.Timer(self._animate_icon, ICON_TICK)
        self._frame_cache: dict = {}                 # name -> NSImage
        self._usage_cache: dict = {}                 # usage cluster -> (NSImage, baseline)
        self._todo_cache: dict = {}                   # (count, dark) -> (NSImage, baseline)
        # Which rung of STRIP_LADDER the strip settled on, and the reading it was
        # measured against. Remembered so the width search runs when the numbers
        # change rather than five times a second.
        self._strip_key = None
        self._strip_level = 0
        # What the button currently shows, and its measured width (None when the
        # strip was rendered without measuring). See _render_strip.
        self._strip_sig = None
        self._strip_width = None
        self._fonts = None
        self._notifications: list[dict] = []
        self._agents_snapshot: dict = {}       # latest detailed_snapshot from the daemon
        # The account's rate-limit windows (`limits.snapshot`), refreshed off the
        # main thread by _refresh_limits. Feeds the 5H chip on the strip and the
        # panel's usage chips, so the two can never disagree.
        self._limits: dict = {}
        # Grok's weekly window, from a live billing fetch. Separate from
        # `_limits` on purpose: different account, different source, own chip.
        self._grok_limits: dict = {}
        self._codex_limits: dict = {}

        prefs = load_preferences()

        # Build-staleness safeguard — surfaced only when the repo source is
        # locatable (dev checkout, or a .app that lives inside the repo). See
        # dev_build.py. It reaches the user through the panel's context now, not
        # through a menu row.
        self._repo_root = dev_build.find_repo_root()
        self._pack_self_root = ""
        if self._repo_root is not None:
            try:
                from dark_army_daemon import enrollment as _enrollment
                self._pack_self_root = _enrollment.normalise(str(self._repo_root))
            except Exception:
                self._pack_self_root = str(self._repo_root)

        # Every preference, as plain state.
        #
        # These used to *be* their menu items — `sender.state` was the model, and
        # the toggle read and wrote it in place. With the dropdown gone there is
        # no sender and no row to hold a checkmark, so the values live here and
        # are pushed to the panel with the rest of the context. That is the right
        # way round anyway: a preference is a fact about the app, not about a
        # widget that happens to be on screen.
        self._settings: dict = {
            # Persisted here rather than only in the daemon's config: with the
            # daemon not yet connected there was nothing to read the choice back
            # from, so a picked timeout silently reverted to 5 minutes on relaunch.
            "session_timeout": int(prefs.get("session_timeout", 300)),
            # The ⋯ row is gone; still loaded so a stored false wins.
            "session_title": bool(prefs.get("session_title", True)),
            "notification_sound": bool(prefs.get("notification_sound", True)),
            # A separate choice from the chime, because the two fail in opposite
            # directions: the chime is easy to miss and the banner is hard to
            # ignore, and someone who wants one rarely wants both.
            "notification_banners": bool(prefs.get("notification_banners", True)),
            # Dark Army's channel — the only route that reaches *into* a session. Off
            # by default and never opted in for new installs, because unlike
            # every other row here it cannot take effect on a session that is
            # already open: it is a launch flag, so switching it on hands over a
            # command rather than changing anything on screen.
            "channel_enabled": bool(prefs.get("channel_enabled", False)),
            # What Dark Army does with a session that has run out of context: type
            # `/compact` into its VS Code terminal, or wake you up to type it.
            # Only ever reaches a session whose window can take the keystroke,
            # so a session outside VS Code is never touched.
            "auto_compact": bool(prefs.get("auto_compact", True)),
            # Reply by typing onto the session's own input line in VS Code,
            # so a session need not be started with the channel command to
            # be answered. Off for the MVP; see preferences.DEFAULTS.
            "typed_reply": bool(prefs.get("typed_reply", False)),
            # Whether Dark Army may start a session from a board card at all — the one
            # switch here that removes a *capability* rather than changing what
            # an existing one does. See preferences.DEFAULTS for why it is on.
            "board_dispatch": bool(prefs.get("board_dispatch", True)),
            # Whether a started card runs on a terminal Dark Army itself owns
            # rather than in the project's VS Code window. Off by default;
            # see preferences.DEFAULTS.
            "board_own_terminal": bool(prefs.get("board_own_terminal", False)),
            # Whether Dark Army starts the head of a project's queue by itself.
            # Beside `board_dispatch` because it is the narrower half of the
            # same capability: that switch removes launching entirely, this
            # one leaves the gate and the order and removes only the press.
            "board_autostart": bool(prefs.get("board_autostart", True)),
            # How many agents may work at once in one project — the waiting
            # rule itself. Read as an int and left unclamped here: the daemon's
            # own setter is where an out-of-range value is refused, so there is
            # one bound rather than two that can drift.
            "board_parallel": int(prefs.get("board_parallel", 1) or 1),
            # And the projects that say otherwise. Unclamped here for
            # `board_parallel`'s reason: one bound, at the daemon.
            "board_parallel_by_root": (
                dict(prefs.get("board_parallel_by_root") or {})
                if isinstance(prefs.get("board_parallel_by_root"), dict)
                else {}),
            # Which model each agent and helper runs on, as stored: the
            # machine-wide table and the per-project override map. Held raw
            # here — the push resolves the global table and the daemon's
            # setters validate — so a stored entry Dark Army does not know
            # is neither launched nor silently rewritten.
            "agent_models": (
                dict(prefs.get("agent_models") or {})
                if isinstance(prefs.get("agent_models"), dict) else {}),
            "agent_models_by_root": (
                dict(prefs.get("agent_models_by_root") or {})
                if isinstance(prefs.get("agent_models_by_root"), dict)
                else {}),
            # The panel's size as a percent of the design. 100 is the default
            # because an upgrade must change nothing on screen; the offered
            # steps live in the panel's PanelScale; the key is never renamed;
            # an out-of-range stored value is drawn at 100 rather than refused.
            "panel_scale": int(prefs.get("panel_scale", 100) or 100),
            # Whether the phone listener is up. Off by default — see
            # preferences.DEFAULTS: this is the first time Dark Army's data leaves
            # loopback.
            "lan_access": bool(prefs.get("lan_access", False)),
            # Whether the phone keeps working away from home, through the
            # sealed relay. Its own consent key beside `lan_access` — new
            # reach, new switch. Off by default; see preferences.DEFAULTS.
            "remote_access": bool(prefs.get("remote_access", False)),
            # Whether the Mac also holds a live socket per paired phone to
            # the socket relay — the away link's fast lane, a trial. Its
            # own key; inert without `remote_access`. Off by default; see
            # preferences.DEFAULTS.
            "relay_ws": bool(prefs.get("relay_ws", False)),
            # Whether a raised alert also buzzes the paired phone through
            # the relay's push route. A mute on the buzzes alone — the away
            # path itself stays `remote_access`'s decision.
            "phone_push": bool(prefs.get("phone_push", True)),
            # Empty dict means no shortcut recorded. A recorded triple is
            # {key_code, modifiers, label}; anything else is treated as none.
            "dictation_shortcut": (
                prefs.get("dictation_shortcut")
                if isinstance(prefs.get("dictation_shortcut"), dict)
                else {}
            ),
            # Whether a card arriving in Done disposes the terminal tab.
            # On by default since 30 Aug 2026 — see preferences.DEFAULTS:
            # a cleared-but-open tab opens a fresh empty session that shows
            # up as a ghost row. The literal here matches DEFAULTS so the
            # two can never silently disagree.
            "board_close_terminal": bool(
                prefs.get("board_close_terminal", True)),
            "channel_command": channel_install.launch_command(),
            "hooks_installed": hooks.are_hooks_installed(),
            # Deliberately not `is_installed()` here: it is a `code` subprocess
            # with a 15s timeout and this is the main thread during construction.
            # Start false and let the worker below correct it a moment later.
            "vscode_extension": False,
            "version": get_version(),
        }
        # Idempotent, and cheap enough to do on every launch: it is the only way
        # a settings.json restored from another machine, or edited by hand,
        # still has Dark Army holding the tab pen.
        hooks.set_title_env()
        # The channel script is a *copy* living outside the bundle, so an app
        # update leaves the old one behind — and it fails invisibly: the stale
        # copy still speaks the handshake, so sessions look reachable while
        # every new field the daemon expects is missing. Caught exactly that
        # way, by a reply that the daemon refused for a session that plainly
        # had a channel. Idempotent, and the same argument as the hook
        # auto-update above. On a worker, exactly as `_set_channel` runs it:
        # both halves shell out to `claude mcp` / `codex mcp` with 20s
        # timeouts each, and this used to run on the main thread *before the
        # status item existed* — a slow CLI froze the launch for the duration.
        # The install itself skips the CLI entirely when the recorded install
        # is still current, so the ordinary launch costs one file read.
        if self._settings["channel_enabled"]:
            threading.Thread(target=self._install_channel_quietly,
                             name="channel-install-launch", daemon=True).start()
        self._refresh_vscode_ext_state()

        _repair_login_item_if_stale()

        # The panel. It is now the *only* surface: the dropdown that used to
        # repeat these rows — sessions, notifications, usage, settings — is gone,
        # because everything it said the panel says better and none of it was
        # said in two places on purpose.
        self._panel = PanelProcess(on_action=self._on_panel_action)
        # The context push's cached probes (build staleness, accessibility,
        # MacWhisper), refreshed by the limits worker every 30s. None until
        # the first reading; `_push_panel_context` computes inline once if a
        # gesture beats the worker.
        self._panel_probes: dict | None = None
        # Banner delivery. Constructed here and authorised from the first run
        # loop tick (see main()): the prompt needs a run loop to answer into, and
        # there is none until rumps starts one.
        self._notifier = notifier.Notifier(on_action=self._on_notification_action,
                                           on_status=self._on_notification_status)
        #: The last explicit notification authorization status, in
        #: `notifier.STATUSES`' words. `unknown` until the first read lands;
        #: shipped down the context so the panel can offer Notification
        #: Settings on an explicit denial and nothing else.
        self._notification_status = notifier.STATUS_UNKNOWN

        # No menu at all, in the normal case — both mouse buttons open the panel.
        # `_emergency_menu()` puts one back only if the panel cannot run, so
        # Restart and Quit can never become unreachable (which is exactly why the
        # dropdown was kept around the last time this was reduced).
        self.menu = []
        self._install_emergency_menu_if_needed()

        # No seed icon: _animate_icon paints the strip on its first tick, offline
        # glyph included. Seeding self.icon here would put an image on the button
        # that nothing ever clears — the bug this whole path was rewritten to
        # avoid — so the button starts empty and stays title-only.
        self.title = ""
        self._refresh_build_status()
        # rumps timers wait out their first interval; don't leave the Grok chip
        # blank for 30s after launch when one request would fill it.
        self._refresh_limits(None)

    @property
    def _daemon_alive(self) -> bool:
        """Whether the strip has anything truthful to say — i.e. the daemon
        thread is up. A dead thread must paint the disconnected glyph rather
        than stale counts, and an empty title is worse than either: macOS fills
        one in with the application's name, which is what "the menu bar shows
        the app's name" was."""
        thread = getattr(self, "_daemon_thread", None)
        return thread is not None and thread.is_alive()

    # --- Lifecycle ---

    def _start_daemon_thread(self):
        """Build the daemon from the stored settings and start the thread that
        runs it, waiting at most five seconds for that thread's loop to exist.
        Called once, on the AppKit thread, from `main()`."""
        self._daemon = BobDaemon(
            observer=self, headless=False,
            session_timeout=int(self._settings["session_timeout"]))
        # Restore the persisted mute state — the daemon defaults to on.
        self._daemon.notification_sound_enabled = bool(
            self._settings["notification_sound"]
        )
        self._daemon.auto_compact_enabled = bool(self._settings["auto_compact"])
        self._daemon.typed_reply_enabled = bool(
            self._settings.get("typed_reply", False))
        # The ⋯ row is gone; a stored false still wins so an upgrade does
        # not start sending opening-prompt text off the machine.
        self._daemon.session_title_enabled = bool(
            self._settings.get("session_title", True))
        self._daemon.board_dispatch_enabled = bool(
            self._settings.get("board_dispatch", True))
        self._daemon.board_own_terminal_enabled = bool(
            self._settings.get("board_own_terminal", False))
        self._daemon.board_autostart_enabled = bool(
            self._settings.get("board_autostart", True))
        # Through the clamping setter, not a plain attribute write: a
        # hand-edited preferences.json is exactly the case the clamp exists
        # for, and startup is where such a file first arrives.
        self._daemon.set_board_parallel(
            self._settings.get("board_parallel", 1))
        # And the per-project dials, through the same clamp for the same
        # reason. An unusable entry is dropped by the setter rather than
        # failing the lot.
        self._daemon.set_board_parallel_overrides(
            self._settings.get("board_parallel_by_root", {}))
        # Which model each agent runs on: the stored tables, through the
        # daemon's own cleaning setters so a hand-edited name is dropped
        # here and never reaches an argv.
        self._daemon.set_agent_models(
            self._settings.get("agent_models", {}))
        self._daemon.set_agent_model_overrides(
            self._settings.get("agent_models_by_root", {}))
        self._daemon.board_close_terminal_enabled = bool(
            self._settings.get("board_close_terminal", True))
        # The stored timeout is the one value the daemon reads, handed over
        # once before the loop exists — the same shape as the chime write
        # above. A stored press from an older picker still wins; 0 (Never)
        # is no longer a setting and becomes the five-minute default.
        self._daemon.set_session_timeout(int(self._settings["session_timeout"]))
        # Stash only: the loop does not exist yet. `run()` starts the LAN
        # listener after the loopback API binds, so a collision on 19875
        # cannot take the panel down with it.
        self._daemon.set_lan_access(bool(self._settings.get("lan_access", False)))
        # Same stash-then-apply as the LAN flag: `run()` starts the relay
        # connector after the loopback API binds.
        self._daemon.set_remote_access(
            bool(self._settings.get("remote_access", False)))
        # The socket lane's flag, stashed the same way; `run()` starts it
        # after the connector, and only while away access is on.
        self._daemon.set_relay_ws(
            bool(self._settings.get("relay_ws", False)))
        # A plain bool the alert drain reads at the moment of use — nothing
        # to schedule, so this one is applied, not stashed.
        self._daemon.set_phone_push(
            bool(self._settings.get("phone_push", True)))

        thread = threading.Thread(target=self._serve_daemon, daemon=True)
        self._daemon_thread = thread
        thread.start()
        # Bounded: the AppKit thread never waits longer than this on the daemon.
        self._loop_ready.wait(5)

    def _serve_daemon(self) -> None:
        """The daemon thread's whole life: make a loop, publish it, run the
        daemon on it until `run()` returns. `_loop_ready` is set the moment
        the loop exists and again on the way out, so a failure before that
        point cannot leave `_start_daemon_thread` waiting out its timeout."""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            loop.run_until_complete(self._daemon.run())
        except Exception:
            logger.exception("The daemon's loop stopped on an error")
        else:
            logger.info("The daemon's loop finished")
        finally:
            self._loop_ready.set()

    # --- What the daemon reports (these run on the daemon's own thread) ---

    def on_notification_change(self, notifications: list[dict]) -> None:
        """Cards the daemon is holding. Nothing here draws them any more — the
        panel reads them from `/api/state` and the banners come through
        `on_alerts` — but the strip's alert dot is fed from the counts, and a
        surface that wants the list can still have it."""
        self._notifications = notifications

    def on_activity_change(
        self, working_count: int, idle_count: int, attention_count: int,
        subagent_count: int,
    ) -> None:
        changed = (working_count, idle_count, attention_count,
                   subagent_count) != (
            getattr(self, "_working_count", 0), getattr(self, "_idle_count", 0),
            getattr(self, "_attention_count", 0),
            getattr(self, "_subagent_count", 0))
        self._working_count = working_count
        self._idle_count = idle_count
        self._attention_count = attention_count
        self._subagent_count = subagent_count
        # A count change pokes the strip on the main thread: it is redrawn at
        # once and its clock put back on ICON_TICK if somebody started working,
        # rather than waiting out the resting tick. No timer is touched here.
        if changed:
            callAfter(self._strip_wake)

    def on_board_change(self, board: dict) -> None:
        """The Kanban board changed. One scalar write, off the main thread.

        Safe without a hop: `_animate_icon` is a `rumps.Timer` on the AppKit
        main thread and reads the attribute on its next tick, within
        ICON_TICK_REST at rest (only a count change in `on_activity_change`
        pokes the strip at once). Nothing here touches AppKit, so nothing
        here has to hop."""
        counts = (board or {}).get("counts") or {}
        raw = counts.get("backlog")
        if raw is None:
            raw = counts.get("ready")
        # Prep + Backlog: both are written-down work nobody has started, and a
        # card waiting to be refined must still count. `prep` is simply absent
        # from a daemon one generation behind (as `ready` is one ahead), so
        # each term is parsed on its own — one junk value must not zero the
        # other.
        total = 0
        for value in (raw, counts.get("prep")):
            try:
                total += int(value or 0)
            except (TypeError, ValueError):
                continue
        self._todo_count = total

    def on_agents_change(self, snapshot: dict) -> None:
        """Latest rich agent snapshot from the daemon (called off the main
        thread). Just cache it — the panel reads the same snapshot from the
        daemon's API directly, so nothing here has to redraw on a push."""
        self._agents_snapshot = snapshot or {}
        # Enrol/un-enrol rewrites enrollment.json. One stat here; the
        # per-root walk runs on a worker only when that file actually moved.
        try:
            mtime = self._enrollment_mtime()
        except Exception:
            return
        if mtime == getattr(self, "_pack_self_roots_mtime", None):
            return
        self._pack_self_roots_mtime = mtime

        def work():
            try:
                roots = pack_install.self_roots()
            except Exception:
                roots = []
            callAfter(self._apply_pack_self_roots, roots, mtime)

        threading.Thread(target=work, name="pack-self-roots", daemon=True).start()

    def on_alerts(self, alerts: list[dict]) -> None:
        """Interruptions the daemon has decided on, for us to actually post.

        Arrives on the daemon's loop thread and ends in AppKit, so it hops. The
        daemon drains its queue when it hands these over: what we do not post
        here is gone, which is the right trade — an alert is about right now, and
        replaying an afternoon's worth on the next tick is how an app gets muted.
        """
        if not alerts:
            return
        callAfter(self._post_alerts, alerts)

    def _post_alerts(self, alerts: list[dict]) -> None:
        """Main thread. Checked here rather than in `on_alerts` so the preference
        is read at the moment of posting, the same discipline as the chime."""
        if not self._settings["notification_banners"]:
            return
        for alert in alerts:
            if alert.get("kind") == "security":
                # A security alert is about nobody's session: `post` would
                # stamp the category and hang Open in Editor / Dismiss / Mute
                # on it, all inert against an empty session id. A notice
                # carries no buttons and no thread.
                self._notifier.post_notice(
                    str(alert.get("id") or "access"),
                    str(alert.get("title") or ""),
                    str(alert.get("body") or ""))
                continue
            self._notifier.post(alert)

    def _on_notification_action(self, action: str, session_id: str) -> None:
        """A tap on a banner. Main thread; the daemon's work hops to its loop.

        Only verbs the daemon genuinely has — `alerts.Alert.actions` names these
        three and nothing here may offer a fourth — plus `open`, which is not
        the daemon's at all: it is a tap on the banner's body, and it belongs to
        the panel, so it never reaches the loop.
        """
        if action == notifier.ACTION_OPEN:
            # Before the daemon guard: showing a row is this process's own work,
            # and a panel that can open is worth opening even if the daemon
            # thread has gone (the row it draws is the last state we held).
            self._open_panel_on(session_id)
            return
        if not self._daemon or not self._loop:
            return
        if action == notifier.ACTION_MUTE:
            # Plain call, no loop hop: muting only writes to the policy's own
            # set, and it should take effect before the next snapshot rather
            # than behind it.
            self._daemon.mute_session_alerts(session_id)
            return
        coro = None
        if action == notifier.ACTION_REVEAL:
            coro = self._daemon.reveal_in_vscode(session_id)
        elif action == notifier.ACTION_DISMISS:
            coro = self._daemon.dismiss_notification(session_id)
        if coro is not None:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    @rumps.timer(30)
    def _health_check(self, _):
        """Notice the daemon thread dying, and put the app back on its feet.

        The strip already paints the disconnected glyph off `_daemon_alive` on
        its own tick; this decides what to *do* about it. The detector is
        `_daemon_alive` and nothing else — deliberately. A thread that is alive
        but wedged (a loop blocked on a socket, a deadlocked executor, a daemon
        that has stopped producing snapshots) reads as healthy here, and it
        stays that way: "no snapshot for N seconds" would fire on a quiet
        machine, and a restart nobody asked for on an idle Mac is worse than
        the offline mark.

        Runs on the AppKit main thread (`@rumps.timer`), and everything it
        touches — the ledger, `self.menu`, the notifier — belongs there. The
        teardown does not: that is `_on_restart`'s worker thread, unchanged.
        """
        if self._daemon_alive:
            return
        logger.warning("Daemon thread is not alive")
        # No thread at all is not a death: `main()` creates the app object and
        # only then calls `_start_daemon_thread`, so this is either that window
        # or a start that never happened — and relaunching would reproduce it.
        if not hasattr(self, "_daemon_thread"):
            return
        # Arm on the first tick. `_start_daemon_thread` runs after the app
        # object exists and blocks up to 5s on `_loop_ready`; a tick landing in
        # that window sees no `_daemon_thread` at all. One extra 30s before
        # recovery buys the whole class of startup races.
        if not getattr(self, "_health_seen_tick", False):
            self._health_seen_tick = True
            return
        # A restart already in flight — or a quit, which claims the same flag.
        if getattr(self, "_restarting", False):
            return
        if getattr(self, "_restart_gave_up", False):
            return
        now = time.time()
        if not self_restart.may_auto_restart(
                self_restart.load().get("auto_restarts", []), now):
            logger.warning(
                "Daemon thread died again after %d self-restarts in the last "
                "hour; staying down", self_restart.MAX_AUTO_RESTARTS)
            self._give_up_on_self_restart()
            return
        # Stamp *before* the teardown, and give up if the stamp will not
        # write. The ledger is the only bound on the flapping, so a write that
        # fails silently is a relaunch every seventy seconds for ever on a
        # state directory nobody can write — a failure indistinguishable from
        # health. An allowance that cannot be counted is treated as spent.
        try:
            self_restart.record_auto_restart(now)
        except Exception:
            logger.error("Could not record the self-restart; staying down "
                         "rather than relaunching unbounded", exc_info=True)
            self._give_up_on_self_restart()
            return
        self._auto_restart_pending = True
        logger.warning("Daemon thread is not alive; restarting Dark Army itself")
        self._on_restart(None)

    def _give_up_on_self_restart(self) -> None:
        """Stop trying, keep the offline mark, and say so once.

        Reached two ways — the hourly allowance spent, or a stamp that could
        not be written — and both mean the same thing to the person at the
        machine. Main thread only: it touches `self.menu` and the notifier.

        It deliberately does **not** call `_on_restart`, so the greying of the
        emergency menu's Restart row never happens and a person's own press
        keeps working, however many self-restarts came before it.
        """
        self._restart_gave_up = True
        # The offline mark is not actionable on its own with no dropdown, so
        # put the sanctioned three-row fallback back within reach.
        self._install_emergency_menu_if_needed()
        self._notifier.post_notice(self_restart.GAVE_UP_IDENT,
                                   self_restart.GAVE_UP_TITLE,
                                   self_restart.GAVE_UP_BODY)

    # --- Rate-limit windows ---

    @rumps.timer(30)
    def _refresh_limits(self, _):
        """Re-read the account's limit windows, off the main thread.

        The percentages come from the statusline payloads the tracked sessions
        already report (`limits.pick_live` picks the freshest), so this costs no
        network and no credential — but `limits.snapshot` also parses
        ``~/.claude.json``, which is a six-figure JSON file, and that must not
        happen on the thread drawing the menu bar twenty times a second.

        Polled rather than pushed because the daemon does not notify on a
        statusline tick alone: the agents snapshot is thrown at us on *structural*
        changes, and a session quietly burning through its budget makes none. 30s
        is well under the resolution of a five-hour window."""
        snapshot = self._agents_snapshot

        def work():
            claude, grok, codex = {}, {}, {}
            try:
                from dark_army_daemon import limits
                # Straight from the daemon's own store rather than from the
                # snapshot it pushes us: that push only fires on *structural*
                # changes, so after a restart with nothing starting or stopping
                # it never came, and the Claude cluster sat on a dash for a
                # window that had already reset. The snapshot stays as a fallback
                # for a daemon that is not in this process.
                payloads = None
                if self._daemon is not None:
                    getter = getattr(self._daemon, "live_statusline_metrics", None)
                    if callable(getter):
                        payloads = getter()
                if not payloads:
                    # Claude rows only — a Grok row files its weekly percentage
                    # under `five_hour_pct` and would otherwise draw the
                    # five-hour bar. The primary path above cannot hit this:
                    # `_session_metrics` is fed by Claude Code's statusline hook
                    # and nothing else. See `limits.claude_payloads`.
                    payloads = limits.claude_payloads(
                        entry
                        for group in snapshot.values() if isinstance(group, list)
                        for entry in group
                    )
                metrics = limits.pick_live(payloads)
                claude = limits.snapshot(metrics)
            except Exception:
                logger.warning("could not read rate limits", exc_info=True)
            try:
                from dark_army_daemon import grok_billing
                grok = grok_billing.get_snapshot()
            except Exception:
                logger.warning("could not read Grok usage", exc_info=True)
            try:
                if self._daemon is not None:
                    getter = getattr(self._daemon, "codex_usage_snapshot", None)
                    if callable(getter):
                        codex = getter()
            except Exception:
                logger.warning("could not read Codex usage", exc_info=True)
            # The panel-context probes, computed here so `_push_panel_context`
            # never has to: `check_staleness` is a glob+stat file-tree walk,
            # and it used to run on the AppKit thread on this same 30s cadence.
            probes = self._compute_panel_probes()
            callAfter(self._apply_limits, claude, grok, codex, probes)

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _enrollment_mtime() -> int:
        try:
            from dark_army_daemon.paths import ENROLLMENT_PATH
            return ENROLLMENT_PATH.stat().st_mtime_ns
        except OSError:
            return 0

    def _apply_pack_self_roots(self, roots, mtime=None) -> None:
        try:
            self._pack_self_roots = list(roots or [])
            if mtime is not None:
                self._pack_self_roots_mtime = mtime
            self._push_panel_context(respawn=False)
        except Exception:
            logger.debug("Could not apply pack self_roots", exc_info=True)

    def _compute_panel_probes(self) -> dict:
        """The facts `_push_panel_context` ships that cost real work to read:
        build staleness (a file-tree walk), the accessibility grant and the
        MacWhisper install. Safe off the main thread; every failure is a None
        or a False, never an exception out of the limits worker."""
        info = None
        try:
            info = dev_build.check_staleness(
                self._repo_root, self._panel.executable_path)
        except Exception:
            logger.debug("Could not read build staleness for the panel",
                         exc_info=True)
        try:
            ax_trusted = ax_is_process_trusted()
        except Exception:
            ax_trusted = False
        try:
            whisper = macwhisper_installed()
        except Exception:
            whisper = False
        try:
            pack_self_roots = pack_install.self_roots()
            pack_self_roots_mtime = self._enrollment_mtime()
        except Exception:
            pack_self_roots = list(getattr(self, "_pack_self_roots", []) or [])
            pack_self_roots_mtime = getattr(self, "_pack_self_roots_mtime", None)
        return {
            "build_info": info,
            "accessibility_trusted": ax_trusted,
            "macwhisper_installed": whisper,
            "pack_self_roots": pack_self_roots,
            "pack_self_roots_mtime": pack_self_roots_mtime,
        }

    def _apply_limits(self, snapshot: dict, grok: dict | None = None,
                      codex: dict | None = None, probes: dict | None = None):
        """Adopt a freshly-read limits snapshot. Main thread. The strip picks it
        up on its next animation tick and the panel reads its chips from the
        API, so neither needs poking here — except Grok's window, which is ours
        alone and rides down with the panel context."""
        self._limits = snapshot or {}
        if grok is not None:
            self._grok_limits = grok or {}
        if codex is not None:
            self._codex_limits = codex or {}
        if probes is not None:
            self._panel_probes = probes
            if "pack_self_roots" in probes:
                self._pack_self_roots = list(probes.get("pack_self_roots") or [])
            if "pack_self_roots_mtime" in probes:
                self._pack_self_roots_mtime = probes.get("pack_self_roots_mtime")
        # Grok's window used to ride only on the next panel open. A fetch that
        # landed after the first context push left the chip blank until
        # something else poked the panel. The strip already has this reading;
        # the chips should too. `respawn=False`: this fires every 30s for the
        # life of the process, and `_send` spawns a dead panel — which turned
        # a panel that crashed into one silently relaunched twice a minute,
        # forever. Only a user gesture respawns.
        self._push_panel_context(respawn=False)

    # --- Menu-bar status strip ---

    @staticmethod
    def _compose_strip(working: int, idle: int, attention: int, subagents: int,
                       level: int = 0, faces: dict | None = None):
        """Pure layout logic: the ordered list of (category, count, suffix) groups
        the strip should show.

        Two live categories, never more than one face each: working and
        attention (needs you), each with its count beside it. Sleeping sessions
        (`idle`) are accepted and ignored — they are the number nothing is
        waiting on, and they were the faces that pushed the backlog off the
        bar. When nothing is working and nobody needs anyone, a single resting
        idle face (no number) holds the slot so an empty item does not look
        like a crash.

        `faces` may name the one agent in a category (`{"work": [name],
        "attn": [name]}`). They must agree with the count and stay inside
        `MAX_FACES` (1); otherwise the category draws the aggregate glyph.
        The count sits beside either face — one portrait is not a number you
        can add up at a glance. The subagent footnote still lands on the
        working group.

        `level` indexes STRIP_LADDER, whose rungs are `StripRung`s with named
        fields — `suffix`, `usage_claude`, `usage_grok`, `usage_codex`,
        `todo`. This reads the first of them by name."""
        keep_suffix = STRIP_LADDER[min(level, len(STRIP_LADDER) - 1)].suffix
        faces = faces or {}
        groups = []

        def named(category: str, count: int):
            """The one named face for a category, or None to draw it in
            aggregate. They must agree with the count: if they disagree, the
            count came from the source of truth and the faces did not."""
            people = faces.get(category) or []
            if people and len(people) == count and len(people) <= MAX_FACES[category]:
                return people[0]
            return None

        if working > 0:
            suffix = f"+{subagents}" if subagents > 0 and keep_suffix else ""
            character = named("work", working)
            key = f"cast:{character}:work" if character else "work"
            groups.append((key, str(working), suffix))
        if attention > 0:
            character = named("attn", attention)
            key = f"cast:{character}:attn" if character else "attn"
            groups.append((key, str(attention), ""))
        if not groups:
            groups.append(("idle", "", ""))   # resting state: lone sleeping face
        return groups

    def _strip_faces(self) -> dict:
        """Which character the one agent in a live category wears.

        Read from the same nicknames the panel draws, so the bar and the panel
        can never disagree about who is who — which is the entire value of the
        faces being here rather than being decoration. A nickname with no
        portrait (one assigned before the cast existed, or past the seventh)
        empties that category, dropping it back to its aggregate face rather
        than guessing at a likeness. Only working and waiting are collected:
        sleeping sessions are not drawn, and `MAX_FACES` is 1, so a category
        with two named agents still falls back in `_compose_strip`.

        getattr, like `_grok_limits` beside it: this runs on a timer that can
        fire before __init__ has finished, the same reason `_status_button`
        answers None rather than raising.
        """
        snapshot = getattr(self, "_agents_snapshot", None) or {}
        # Sleeping sessions are not drawn, so they are not collected. A
        # nickname change among idle agents must not re-search the ladder.
        buckets = {"work": ("running",), "attn": ("waiting",)}
        out: dict[str, list] = {}
        for category, names in buckets.items():
            people = []
            for name in names:
                for entry in snapshot.get(name) or []:
                    nickname = (entry.get("nickname") or "").strip().lower()
                    if not nickname or not _icon_exists(
                            f"cast-{nickname}-{category}-light-0"):
                        people = []
                        break
                    people.append(nickname)
                else:
                    continue
                break
            out[category] = people
        return out

    def _animate_icon(self, _):
        """Render the menu-bar strip and advance the animation. Runs on the main
        thread. When offline, falls back to the monochrome disconnected glyph.

        The collapse ladder is walked here, where the counts are: render a rung,
        measure what it came to, and step down if it overran the budget. The
        search only runs when the numbers actually change — `_strip_key` remembers
        what was measured, so a steady strip re-renders once a tick at the rung it
        settled on. Setting the title more than once inside a single tick costs
        nothing on screen: AppKit paints the run loop's last state, not each.

        Measuring is the expensive half and it is only asked for while the ladder
        is actually searching. `strip.size()` lays out the whole run — text,
        kerning, attachment metrics — and the answer is consulted by nothing on a
        tick where the numbers held still, which is nearly every tick.

        Every exit re-arms the strip's own clock (`_strip_clock_set`), after
        the render: ICON_TICK while something on the bar animates, ICON_TICK_REST
        otherwise. On the tick where the last working session finishes the new
        counts are drawn first and only then does the clock slow down."""
        try:
            if not self._daemon_alive:
                self._render_strip([("off", "", "")], measure=False)
                return
            self._anim_i += 1

            faces = self._strip_faces()
            key = (self._working_count, self._attention_count,
                   self._subagent_count, menu_format.usage_text(self._limits),
                   menu_format.grok_usage_text(getattr(self, "_grok_limits", {}) or {}),
                   menu_format.codex_usage_text(getattr(self, "_codex_limits", {}) or {}),
                   tuple(faces.get("work", ())), tuple(faces.get("attn", ())),
                   getattr(self, "_todo_count", 0))
            searching = key != self._strip_key
            if searching:
                self._strip_key = key
                self._strip_level = 0     # re-search from the top: it may fit again

            if not searching:
                level = self._strip_level
                self._render_strip(
                    self._compose_strip(
                        self._working_count, self._idle_count,
                        self._attention_count, self._subagent_count, level, faces,
                    ),
                    usage_brands=STRIP_LADDER[level].usage_brands(),
                    show_todo=STRIP_LADDER[level].todo,
                    measure=False,
                )
                return

            level = self._strip_level
            while True:
                groups = self._compose_strip(
                    self._working_count, self._idle_count,
                    self._attention_count, self._subagent_count, level, faces,
                )
                width = self._render_strip(
                    groups,
                    usage_brands=STRIP_LADDER[level].usage_brands(),
                    show_todo=STRIP_LADDER[level].todo)
                if (width is None or width <= STRIP_BUDGET_PT
                        or level + 1 >= len(STRIP_LADDER)):
                    break
                level += 1
            self._strip_level = level
        finally:
            self._strip_clock_set(
                ICON_TICK if self._strip_animating() else ICON_TICK_REST)

    def _strip_animating(self) -> bool:
        """Whether anything on the strip moves. Main thread, pure.

        Derived from the freeze table rather than naming `work`: a category
        with a live count animates exactly when it is absent from STILL_FRAMES,
        so un-freezing one later keeps the clock honest without a second
        edit. The offline glyph is a single frame, so a dead daemon rests."""
        if not self._daemon_alive:
            return False
        for category, count in (
                ("work", getattr(self, "_working_count", 0)),
                ("attn", getattr(self, "_attention_count", 0))):
            if count > 0 and category not in STILL_FRAMES:
                return True
        return False

    def _strip_clock_set(self, interval: float) -> None:
        """Put the strip's clock on `interval`. Main thread only: rumps'
        `stop()`/`start()` invalidate and add an NSTimer on the *current*
        run loop, so a call from any other thread would arm it there.

        Always re-armed stopped, never adjusted in flight: `Timer.interval` set
        on a live timer is silently dropped inside its first interval
        (rumps.py, the `interval` setter). `start()` fires on the next run-loop
        pass — the interval is the repeat, not a delay — which is what makes a
        wake draw its first frame at once. A bare instance with no clock (the
        tests' `object.__new__`) is left alone."""
        timer = getattr(self, "_strip_timer", None)
        if timer is None:
            return
        if timer.is_alive() and timer.interval == interval:
            return
        if timer.is_alive():
            timer.stop()
        timer.interval = interval
        timer.start()

    def _strip_wake(self) -> None:
        """Draw the new counts now and put the clock on the right cadence.
        Main thread, reached from `on_activity_change` through `callAfter`, so
        a session starting work never waits out the resting tick."""
        self._animate_icon(None)

    def _status_button(self):
        """The NSStatusItem's button, or None if the status item isn't up yet."""
        try:
            return self._nsapp.nsstatusitem.button()
        except Exception:
            return None

    @staticmethod
    def _frames_for(key: str, dark: bool = False):
        """Frame list for a strip group.

        Three shapes of key. `work`/`idle`/`attn` draw the default character in
        aggregate; `off` is the single-frame offline glyph; and
        `cast:<character>:<category>` is one named agent wearing its own face.
        An unknown character falls back to the aggregate art rather than to
        nothing.

        Every face is baked per appearance, not just the sleeping one: each
        carries a state rule beneath it and a neutral rule baked once would be
        invisible on one of the two bars, since attachments are never tinted.

        Every return goes through `_held`, so `STILL_FRAMES` freezes the quiet
        categories on the aggregate glyphs and on the named cast faces alike:
        only the working category comes back as a whole cycle.
        """
        variant = "dark" if dark else "light"
        if key.startswith("cast:"):
            _, character, category = key.split(":", 2)
            slots = len(ICON_STRIP_FRAMES.get(category, ICON_IDLE_FRAMES[variant]))
            frames = [f"cast-{character}-{category}-{variant}-{i}"
                      for i in range(slots)]
            if _icon_exists(frames[0]):
                return _held(category, frames)
            key = category
        if key == "idle":
            return _held("idle", ICON_IDLE_FRAMES[variant])
        if key == "off":
            return [ICON_DISCONNECTED[variant]]  # single frame: offline doesn't animate
        return _held(key, ICON_STRIP_FRAMES[key])

    def _strip_fonts(self):
        """The strip's three fonts, built once. They take constant arguments, and
        the strip used to construct all three five times a second.

        Monospaced digits are the point: with proportional figures a count ticking
        9 → 10 → 9 changes the run's width and shuffles the whole strip."""
        if self._fonts is None:
            from AppKit import NSFont, NSFontWeightMedium, NSFontWeightSemibold
            self._fonts = (
                NSFont.monospacedDigitSystemFontOfSize_weight_(
                    COUNT_PT, NSFontWeightMedium),
                NSFont.monospacedDigitSystemFontOfSize_weight_(
                    COUNT_PT, NSFontWeightSemibold),
                NSFont.monospacedDigitSystemFontOfSize_weight_(
                    SUFFIX_PT, NSFontWeightMedium),
            )
        return self._fonts

    def _frame_image(self, name: str):
        img = self._frame_cache.get(name)
        if img is None:
            from AppKit import NSImage
            img = NSImage.alloc().initByReferencingFile_(self._icon_path(name))
            self._frame_cache[name] = img
        return img

    def _render_strip(self, groups, usage_brands: frozenset = ALL_USAGE_BRANDS,
                      show_todo: bool = True, measure: bool = True):
        """Compose the animated multi-face strip as the status button's attributed
        title: an image attachment (current animation frame) + count per category,
        then the five-hour usage cluster. Returns the composed width in points (or
        None if there is no button yet) so the caller can walk the collapse ladder.

        Counts stay real text in `labelColor` rather than pixels in an image, and
        that is worth more than it looks: AppKit tracks the appearance for us and
        inverts them for free when the item is clicked. Everything drawn into an
        offscreen NSImage has to resolve its own ink, which is the trap this file
        has already been caught by twice (see ICON_DISCONNECTED).

        The one number set in colour is the attention count. It is the place the
        eye should land, and it used to be the only one *without* colour while a
        permanently green budget bar sat beside it — the hierarchy exactly
        inverted. Red here is never colour alone: it is on a count that is only
        drawn when it is non-zero, beside a face with a red alert dot."""
        button = self._status_button()
        if button is None:
            return None
        from AppKit import (
            NSMutableAttributedString, NSAttributedString, NSTextAttachment,
            NSColor, NSMakeRect,
            NSFontAttributeName, NSForegroundColorAttributeName,
            NSKernAttributeName, NSBaselineOffsetAttributeName,
        )
        from Foundation import NSMakeSize, NSMakeRange

        count_font, attn_font, suffix_font = self._strip_fonts()
        dark = _appearance_is_dark(button)

        # Suppression is a brand's *label* going empty, and nothing else: the
        # brand loop below already skips an empty label (the path a machine with
        # no Grok account exercises), so the one leading gap, the rules only
        # between survivors and the never-trailing rule all come for free. A
        # second skip inside that loop would be the two-counts mistake.
        usage = (menu_format.usage_text(self._limits)
                 if "claude" in usage_brands else "")
        grok_limits = getattr(self, "_grok_limits", {}) or {}
        grok_usage = (menu_format.grok_usage_text(grok_limits)
                      if "grok" in usage_brands else "")
        codex_limits = getattr(self, "_codex_limits", {}) or {}
        codex_usage = (menu_format.codex_usage_text(codex_limits)
                       if "codex" in usage_brands else "")

        # Resolve this tick's frames up front so the signature below names the
        # actual pixels rather than the animation counter — the offline glyph has
        # a single frame, so `_anim_i` moving does not change what is on screen.
        frame_names = []
        for key, _count, _suffix in groups:
            frames = self._frames_for(key, dark)
            frame_names.append(frames[self._anim_i % len(frames)])

        percent = menu_format.limit_percent(self._limits) if usage else None
        grok_percent = grok_limits.get("percent") if grok_usage else None
        if grok_limits.get("stale"):
            grok_percent = None
        codex_percent = (menu_format.codex_limit_percent(codex_limits)
                         if codex_usage else None)
        # `getattr` for the reason `_grok_limits` above uses it: the strip is
        # rendered by hand-built objects in the tests, and a new scalar must
        # not make every one of them a construction problem.
        todo = getattr(self, "_todo_count", 0) if show_todo else 0
        sig = (tuple(groups), tuple(frame_names), dark, usage, percent,
               grok_usage, grok_percent, codex_usage, codex_percent, todo)
        if sig == self._strip_sig and (self._strip_width is not None or not measure):
            # Byte-identical to what the button already shows. Rebuilding the
            # attributed string would allocate a dozen ObjC objects and dirty the
            # status item for a repaint that changes nothing.
            return self._strip_width

        # Runs are collected before they are assembled, because every gap on the
        # strip is kerning applied to the run *before* it — and whether a run is
        # the last one isn't known until the usage cluster has had its say.
        runs: list[list] = []

        def push(payload, attrs):
            runs.append([payload, attrs, 0.0])

        def gap(points: float):
            if runs:
                runs[-1][2] = points

        for i, (key, count, suffix) in enumerate(groups):
            img = self._frame_image(frame_names[i])
            sz = img.size()
            h = STRIP_ICON_PT
            w = h * (sz.width / sz.height) if sz.height else h
            # These NSImages are cached and shared, and setSize_ is a mutation:
            # it can drop an image's cached representations and make AppKit
            # re-rasterise it on the next draw. Writing the size it already has,
            # five times a second, is the kind of no-op that is not free.
            if abs(sz.width - w) > 0.01 or abs(sz.height - h) > 0.01:
                img.setSize_(NSMakeSize(w, h))
            att = NSTextAttachment.alloc().init()
            att.setImage_(img)
            # Vertically center the face on the count font's cap height.
            att.setBounds_(NSMakeRect(0, (count_font.capHeight() - h) / 2.0, w, h))
            push(att, {})

            if count:
                gap(GAP_FROG)
                push(count, {
                    NSFontAttributeName: attn_font if key.endswith("attn")
                    else count_font,
                    NSForegroundColorAttributeName: (
                        NSColor.systemRedColor() if key.endswith("attn")
                        else NSColor.labelColor()),
                })
            if suffix:
                # Outside the `if count`, deliberately. A per-agent face carries
                # no count, and nesting the footnote under one silently swallowed
                # it the first time the faces landed — along with the attention
                # count, which is the strip's only coloured number. A footnote
                # annotates the group, not the digit.
                gap(1.0 if count else GAP_FROG)
                push(suffix, {
                    NSFontAttributeName: suffix_font,
                    NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
                    NSBaselineOffsetAttributeName: SUFFIX_RISE,
                })
            if i + 1 < len(groups):
                # Faces of one category are one group and sit closer than two
                # categories do; without this every agent reads as its own
                # section and the strip loses its three-part shape.
                nxt = groups[i + 1][0]
                same = (key.startswith("cast:") and nxt.startswith("cast:")
                        and key.rsplit(":", 1)[-1] == nxt.rsplit(":", 1)[-1])
                gap(GAP_FACE if same else GAP_GROUP)

        # The five-hour budget at the right-hand end, unframed. It used to be
        # boxed, and a rounded border in a menu bar reads as a button — an
        # affordance on the one element of the strip that isn't individually
        # clickable. Distance says "different kind of thing" just as well and
        # costs less width than the frame did.
        # Cards waiting to be started, before the budget clusters. Pushed through
        # `push`/`gap` like everything else, so the attachment-spacer fix in the
        # assembly loop below applies without being restated here.
        #
        # An image rather than a text run, unlike the category counts: it needs a
        # card-stack mark beside the digits, and a drawn mark matched to the
        # digits' own cap height is not expressible as a text attribute. The cost
        # is that it does not invert when the item is clicked — the same property
        # the usage clusters already have, and the same class of thing: not a
        # session number.
        if groups and groups[0][0] != "off" and todo > 0:
            img, baseline = self._todo_image(todo, dark)
            sz = img.size()
            att = NSTextAttachment.alloc().init()
            att.setImage_(img)
            att.setBounds_(NSMakeRect(0, -baseline, sz.width, sz.height))
            gap(GAP_USAGE)
            push(att, {})

        if groups and groups[0][0] != "off":
            # Two clusters, two accounts. Claude's five-hour window first (the
            # unlabeled one, same as before), then Grok's weekly window marked
            # with a G so the two percentages cannot be read as one reading.
            first_usage = True
            for brand, label, pct in (("claude", usage, percent),
                                      ("grok", grok_usage, grok_percent),
                                      ("codex", codex_usage, codex_percent)):
                if not label:
                    continue
                if not first_usage:
                    # A hairline between the two clusters. The old pipe glyph
                    # between the *category groups* was removed for good reasons —
                    # it was set at the counts' own size and colour, so the
                    # divider weighed as much as the data, and every group
                    # already opened with a sprite that delimited it. Neither
                    # objection survives here: this is a rule at a fifth of the
                    # ink, and the two clusters carry under-meters that run into
                    # one continuous bar without it, so `12%` and `51%` read as
                    # a single measurement cut in half. That is worse than a
                    # divider costing a point of width.
                    rule, rule_base = self._divider_image(dark)
                    rsz = rule.size()
                    ratt = NSTextAttachment.alloc().init()
                    ratt.setImage_(rule)
                    ratt.setBounds_(NSMakeRect(0, -rule_base, rsz.width, rsz.height))
                    gap(GAP_GROK_USAGE)
                    push(ratt, {})
                img, baseline = self._usage_image(label, dark, pct, brand)
                sz = img.size()
                att = NSTextAttachment.alloc().init()
                att.setImage_(img)
                att.setBounds_(NSMakeRect(0, -baseline, sz.width, sz.height))
                gap(GAP_USAGE if first_usage else GAP_GROK_USAGE)
                first_usage = False
                push(att, {})

        strip = NSMutableAttributedString.alloc().init()
        # Measured on first use only: most renders never need it, and asking for
        # it up front costs a layout on every tick.
        space_w = None
        for payload, attrs, trailing in runs:
            is_text = isinstance(payload, str)
            if is_text:
                strip.appendAttributedString_(
                    NSAttributedString.alloc().initWithString_attributes_(payload, attrs)
                )
            else:
                piece = NSMutableAttributedString.alloc().initWithAttributedString_(
                    NSAttributedString.attributedStringWithAttachment_(payload)
                )
                if attrs:
                    piece.addAttributes_range_(attrs, NSMakeRange(0, piece.length()))
                strip.appendAttributedString_(piece)
            if not trailing:
                continue
            if is_text:
                # Kerning is added after *every* character in the range it is set
                # on, so a gap set across a whole run spaces out its own digits —
                # "+2" came out as "+  2". It belongs on the last character only.
                strip.addAttribute_value_range_(
                    NSKernAttributeName, trailing,
                    NSMakeRange(strip.length() - 1, 1))
            else:
                # **Kerning after an attachment does nothing at all.** Measured:
                # moving a gap from 4pt to 40pt changed the composed width by
                # exactly 0.0pt. It never showed while every icon was followed by
                # a count, because the kern landed on that text — but a strip of
                # faces carries no numbers, so attachments end every run and all
                # of their gaps were being discarded in silence. The spacer is a
                # real character, with the kern making up the rest of the gap.
                if space_w is None:
                    space_w = NSAttributedString.alloc().initWithString_attributes_(
                        " ", {NSFontAttributeName: count_font}).size().width
                strip.appendAttributedString_(
                    NSAttributedString.alloc().initWithString_attributes_(" ", {
                        NSFontAttributeName: count_font,
                        NSKernAttributeName: max(0.0, trailing - space_w),
                    }))

        # Clearing the image is a one-time job — the strip has been an attributed
        # title since it stopped being a single icon — but writing nil onto it
        # five times a second still marks the button dirty every time.
        if button.image() is not None:
            button.setImage_(None)
        button.setAttributedTitle_(strip)
        self._strip_sig = sig
        # Only the ladder consults the width, and only while it is searching.
        self._strip_width = strip.size().width if measure else None
        return self._strip_width

    def _todo_image(self, count: int, dark: bool):
        """The board's Backlog count — work written down and not started: a stack
        of three rounded cards and the digits.
        `(NSImage, baseline)`, the same shape `_usage_image` returns so
        `_render_strip` can sit it on the strip's one vertical register.

        **Every ink is resolved by hand from `dark`,** and that is not
        boilerplate: a dynamic colour resolves against the *drawing* context, and
        an offscreen NSImage has no menu-bar appearance to resolve against — so
        `labelColor` here would come out black on black exactly when the bar is
        dark. This file has been caught by that twice (`ICON_DISCONNECTED`, then
        the usage cluster).

        Deliberately monochrome and deliberately quiet. The strip has exactly one
        coloured number and it is the attention count; a to-do count is not an
        exception, and spending colour on it is how the genuinely urgent number
        loses its own. Never colour-alone in any case: the digits state the
        number and the glyph says what they are counting.

        Cached per (count, appearance): the strip repaints five times a second
        and this changes when somebody writes a card.
        """
        key = (int(count), bool(dark))
        hit = self._todo_cache.get(key)
        if hit is not None:
            return hit

        from AppKit import (
            NSImage, NSBezierPath, NSColor, NSAttributedString, NSFont,
            NSMakeRect, NSFontAttributeName, NSForegroundColorAttributeName,
            NSFontWeightMedium,
        )
        from Foundation import NSMakeSize, NSMakePoint

        base = NSColor.whiteColor() if dark else NSColor.blackColor()
        # Matched to what `labelColor` actually weighs on a menu bar, so the
        # digits read as a peer of the counts rather than heavier than them.
        ink = base.colorWithAlphaComponent_(0.85)

        font = NSFont.monospacedDigitSystemFontOfSize_weight_(
            TODO_PT, NSFontWeightMedium)
        text = NSAttributedString.alloc().initWithString_attributes_(
            str(int(count)), {NSFontAttributeName: font,
                              NSForegroundColorAttributeName: ink})
        tsz = text.size()
        tw = math.ceil(tsz.width)

        # Three rounded cards, back to front. Front at the origin of the mark,
        # back highest and to the right. Geometry rather than a system symbol,
        # because a symbol image would resolve its own tint against a context
        # that is not the menu bar — the same trap as the colour above.
        card_w, card_h = 8.0, 5.5
        layer_dx, layer_dy = 1.4, 1.6
        radius = 1.4
        n_layers = 3
        stack_w = card_w + (n_layers - 1) * layer_dx
        stack_h = card_h + (n_layers - 1) * layer_dy
        mark_w = stack_w + TODO_MARK_GAP
        w = float(tw + mark_w)
        h = math.ceil(tsz.height)

        img = NSImage.alloc().initWithSize_(NSMakeSize(w, h))
        img.lockFocus()
        mark_y = (font.capHeight() - TODO_MARK_PT) / 2.0
        origin_y = mark_y + (TODO_MARK_PT - stack_h) / 2.0
        # Fill alphas of glyph_ink (base at 0.55): back 0.28, mid 0.40, front 0.55.
        fill_alpha = (0.55, 0.40, 0.28)
        for i in range(n_layers - 1, -1, -1):
            rect = NSMakeRect(i * layer_dx, origin_y + i * layer_dy,
                              card_w, card_h)
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                rect, radius, radius)
            base.colorWithAlphaComponent_(fill_alpha[i]).setFill()
            path.fill()
            if i == 0:
                base.colorWithAlphaComponent_(0.70).setStroke()
                path.setLineWidth_(0.5)
                path.stroke()
        text.drawAtPoint_(NSMakePoint(mark_w, 0))
        img.unlockFocus()

        # drawAtPoint_ puts the line's bottom edge at 0 in this unflipped
        # context and the descender hangs below the baseline (negative), so the
        # baseline is -descender.
        result = (img, -font.descender())
        self._todo_cache[key] = result
        return result

    def _usage_image(self, label: str, dark: bool, percent,
                     brand: str = "claude"):
        """The five-hour cluster: the digits over an under-meter exactly as wide as
        they are. Returns `(NSImage, baseline)` — the baseline being the distance
        from the image's bottom edge to the digits' own baseline, which is what
        lets `_render_strip` sit it on the same line as the counts.

        Drawn rather than composed from attributes because a meter matched to the
        width of the text above it is not expressible as a text attribute. That is
        the only reason left: the border it used to carry is gone.

        Colour is an *exception* signal here, not decoration. The fill is neutral
        ink up to `USAGE_WARN_PERCENT`, amber to `USAGE_CRIT_PERCENT`, red above —
        and at red the digits change weight and colour too, because a 2pt bar is
        not where you put the news. A menu bar that is green whenever things are
        fine has nothing left to say when they aren't. The steps are abrupt on
        purpose: the reason to look is to learn which side of a line you are on,
        and a gradient makes that unreadable.

        Nothing here is colour-alone — the digits always state the number and the
        fill's length always encodes it. That matters twice over, because
        systemOrange manages only 2.14:1 against a light menu bar.

        Every ink is resolved by hand from `dark`. A dynamic colour resolves
        against the *drawing* context, and an offscreen NSImage has no menu-bar
        appearance to resolve against — so a dynamic label colour would come out
        black on black exactly when the bar is dark, which is the bug the offline
        glyph already had (see ICON_DISCONNECTED).

        Each cluster opens with its provider's mark, because two bare
        percentages side by side read as one reading split in half. A drawn glyph
        rather than the letter "G" it replaces: the letter cost the same width,
        sat in the digits' own font, and told you nothing until you had worked out
        what it stood for. Both marks are geometry, not brand artwork — at nine
        points nothing survives but a silhouette, so they aim only to be
        *distinguishable from each other* and to resolve their ink by hand like
        everything else drawn into an offscreen image.

        Cached per (text, appearance, whole percent, brand): the strip repaints
        five times a second and the reading changes at most twice a minute."""
        stale = not isinstance(percent, (int, float))
        pct = 0.0 if stale else max(0.0, min(100.0, float(percent)))
        tier = menu_format.usage_tier(None if stale else pct)
        key = (label, dark, -1 if stale else round(pct), brand)
        hit = self._usage_cache.get(key)
        if hit is not None:
            return hit

        from AppKit import (
            NSImage, NSBezierPath, NSColor, NSAttributedString, NSFont, NSMakeRect,
            NSFontAttributeName, NSForegroundColorAttributeName,
            NSFontWeightMedium, NSFontWeightSemibold,
        )
        from Foundation import NSMakeSize, NSMakePoint

        base = NSColor.whiteColor() if dark else NSColor.blackColor()
        # ink-2 matches labelColor's own ~85% weight one rank down, so the digits
        # read as secondary to the counts instead of heavier than them — which is
        # what pure black/white next to labelColor actually looked like.
        ink2 = base.colorWithAlphaComponent_(0.55)
        ink3 = base.colorWithAlphaComponent_(0.30 if dark else 0.28)
        track_ink = base.colorWithAlphaComponent_(0.14 if dark else 0.12)
        fill_ink = base.colorWithAlphaComponent_(0.40 if dark else 0.35)
        # systemOrange / systemRed, pinned to their sRGB values per appearance
        # rather than taken from the dynamic colours, for the reason above.
        if dark:
            warn_ink = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.624, 0.039, 1.0)
            crit_ink = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.271, 0.227, 1.0)
        else:
            warn_ink = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.584, 0.0, 1.0)
            crit_ink = NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.231, 0.188, 1.0)

        weight = NSFontWeightSemibold if tier == "crit" else NSFontWeightMedium
        font = NSFont.monospacedDigitSystemFontOfSize_weight_(USAGE_PT, weight)
        ink = ink3 if stale else (crit_ink if tier == "crit" else ink2)
        text = NSAttributedString.alloc().initWithString_attributes_(label, {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: ink,
        })
        tsz = text.size()
        tw = math.ceil(tsz.width)

        mark_w = USAGE_MARK_PT + USAGE_MARK_GAP
        draw_meter = not stale
        bar_w = max(USAGE_BAR_MIN_PT, float(tw + mark_w)) if draw_meter else 0.0
        w = max(float(tw + mark_w), bar_w)
        y0 = (USAGE_BAR_PT + USAGE_BAR_GAP) if draw_meter else 0.0
        h = math.ceil(tsz.height) + y0

        img = NSImage.alloc().initWithSize_(NSMakeSize(w, h))
        img.lockFocus()
        if draw_meter:
            radius = USAGE_BAR_PT / 2.0
            # Without the track, a short fill and a missing one look alike.
            track_ink.setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0, 0, bar_w, USAGE_BAR_PT), radius, radius).fill()
            # Never rounded away to nothing: at 1% the fill is a dot, but "some"
            # and "none" must not render identically.
            fill_w = max(USAGE_BAR_PT, bar_w * pct / 100.0) if pct > 0 else 0.0
            if fill_w:
                (crit_ink if tier == "crit"
                 else warn_ink if tier == "warn" else fill_ink).setFill()
                NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(0, 0, fill_w, USAGE_BAR_PT), radius, radius).fill()

        # The mark sits on the digits' own optical centre, not the line box's:
        # the digits have no descenders, so centring on the box would float it.
        mark_y = y0 + (font.capHeight() - USAGE_MARK_PT) / 2.0
        self._draw_brand_mark(brand, NSMakeRect(0, mark_y,
                                                USAGE_MARK_PT, USAGE_MARK_PT), ink)
        text.drawAtPoint_(NSMakePoint(mark_w, y0))
        img.unlockFocus()

        # drawAtPoint_ puts the line's bottom edge at y0 in this unflipped
        # context, and the descender hangs below the baseline (negative), so the
        # baseline itself is y0 - descender.
        result = (img, y0 - font.descender())
        self._usage_cache[key] = result
        return result

    def _divider_image(self, dark: bool):
        """The hairline between the two usage clusters. `(NSImage, baseline)`.

        Drawn rather than typed for the same reason the clusters are: it has to
        sit on the digits' baseline and stop at their cap height, and a text
        divider would inherit the run's own metrics instead. Ink is resolved by
        hand from `dark`, like everything else drawn offscreen.

        It is deliberately faint. The job is to interrupt the two under-meters so
        they stop reading as one bar — not to be seen.
        """
        cached = getattr(self, "_divider_cache", None)
        if cached is None:
            cached = {}
            self._divider_cache = cached
        if dark in cached:
            return cached[dark]

        from AppKit import NSImage, NSBezierPath, NSColor, NSMakeRect, NSFont
        from Foundation import NSMakeSize

        base = NSColor.whiteColor() if dark else NSColor.blackColor()
        font = NSFont.monospacedDigitSystemFontOfSize_weight_(USAGE_PT, 0.23)
        # Sit on the same baseline as the digits and rise to their cap height,
        # so the rule is bounded by the text rather than by the meter below it.
        y0 = (USAGE_BAR_PT + USAGE_BAR_GAP)
        height = USAGE_RULE_PT + y0
        img = NSImage.alloc().initWithSize_(NSMakeSize(1.0, height))
        img.lockFocus()
        base.colorWithAlphaComponent_(USAGE_RULE_ALPHA).setFill()
        NSBezierPath.fillRect_(NSMakeRect(0.0, y0, 1.0, USAGE_RULE_PT))
        img.unlockFocus()
        result = (img, y0 - font.descender())
        cached[dark] = result
        return result

    @staticmethod
    def _draw_brand_mark(brand: str, rect, ink) -> None:
        """Draw a provider's mark inside `rect` in `ink`.

        The colour is a parameter and is set on the *stroke*, which is the whole
        lesson of this method: these marks are stroked paths, and a path stroked
        without `setStroke_` uses black — so the first version came out invisible
        on a dark menu bar while the digits beside it were correctly light. That
        is the third time this file has been caught by ink not resolving itself
        (see ICON_DISCONNECTED and `_usage_image`), and it will not be the last
        unless every drawn thing takes its colour as an argument.

        Claude is the radiating burst its own mark is built from; Grok is the
        angular slash; Codex uses the six-loop OpenAI blossom silhouette. They
        are drawn at whatever size they are given and neither
        is traced from official artwork — a nine-point mark is a silhouette, and
        the only job it can actually do is tell you which of the two accounts the
        number beside it belongs to.
        """
        import math as _math
        from AppKit import NSBezierPath
        from Foundation import NSMakePoint

        x, y = rect.origin.x, rect.origin.y
        size = min(rect.size.width, rect.size.height)
        cx, cy = x + size / 2.0, y + size / 2.0
        path = NSBezierPath.bezierPath()
        ink.setStroke()
        ink.setFill()

        if brand == "grok":
            # A slanted X of two full strokes. The first attempt broke them into
            # three segments with gaps, which at eight points stopped reading as
            # an X and started reading as an arrow — detail below the size of the
            # mark is not detail, it is noise.
            path.setLineWidth_(max(1.0, size * 0.20))
            path.setLineCapStyle_(0)                      # butt: keeps it angular
            inset = size * 0.12
            slant = size * 0.10                           # italic lean, like the mark
            path.moveToPoint_(NSMakePoint(x + inset, y + inset))
            path.lineToPoint_(NSMakePoint(x + size - inset + slant, y + size - inset))
            path.moveToPoint_(NSMakePoint(x + size - inset, y + inset))
            path.lineToPoint_(NSMakePoint(x + inset + slant, y + size - inset))
            path.stroke()
            return

        if brand == "codex":
            # Six rounded links around a clear centre. At nine points the full
            # wordmark geometry would alias into a blob; this preserves the
            # OpenAI blossom's unmistakable outer rhythm and central aperture.
            path.setLineWidth_(max(1.0, size * 0.16))
            path.setLineCapStyle_(1)
            outer = size * 0.39
            inner = size * 0.17
            for i in range(6):
                a0 = _math.pi * 2 * i / 6 - _math.pi / 6
                a1 = a0 + _math.pi / 3
                path.moveToPoint_(NSMakePoint(cx + _math.cos(a0) * inner,
                                               cy + _math.sin(a0) * inner))
                path.curveToPoint_controlPoint1_controlPoint2_(
                    NSMakePoint(cx + _math.cos(a1) * inner,
                                cy + _math.sin(a1) * inner),
                    NSMakePoint(cx + _math.cos(a0) * outer,
                                cy + _math.sin(a0) * outer),
                    NSMakePoint(cx + _math.cos(a1) * outer,
                                cy + _math.sin(a1) * outer))
            path.stroke()
            return

        # Claude: an eight-ray burst. Rays rather than a filled star because a
        # star's points close up into a blob once it is under ten pixels.
        path.setLineWidth_(max(1.0, size * 0.17))
        path.setLineCapStyle_(1)                          # round: softer at 9pt
        outer = size * 0.46
        inner = size * 0.13
        for i in range(8):
            angle = _math.pi * 2 * i / 8 + _math.pi / 8
            dx, dy = _math.cos(angle), _math.sin(angle)
            path.moveToPoint_(NSMakePoint(cx + dx * inner, cy + dy * inner))
            path.lineToPoint_(NSMakePoint(cx + dx * outer, cy + dy * outer))
        path.stroke()

    def _icon_path(self, name: str) -> Optional[str]:
        """A real file path for `icons/<name>.png` in this package, or None
        where the package's resources are not on disk as files (or cannot be
        located at all) and the caller falls back to no image."""
        import importlib.resources
        try:
            icon = importlib.resources.files("dark_army_menubar").joinpath(
                "icons", f"{name}.png")
        except Exception:  # any failure to locate resources means "no icon"
            return None
        return os.fspath(icon) if hasattr(icon, "__fspath__") else None

    # --- Settings, driven from the panel ---
    #
    # Each of these used to be a rumps callback flipping `sender.state`, with the
    # menu item as the model. They take a value now: the panel sends the state it
    # wants, the app applies it, stores it and pushes the whole settings block
    # back. Nothing reads a widget to find out what the app is set to.

    def _set_notification_sound(self, enabled: bool) -> None:
        """Mute/unmute the chime played when a notification card surfaces."""
        self._settings["notification_sound"] = enabled
        save_preferences(updates={"notification_sound": enabled})

        # Plain attribute write — the daemon reads it when its debounce timer
        # fires, so this needs no loop hop and takes effect immediately, even on
        # a chime that is already armed.
        if self._daemon:
            self._daemon.notification_sound_enabled = enabled

    def _set_auto_compact(self, enabled: bool) -> None:
        """Let Dark Army run `/compact` for a full session, or go back to asking.

        Plain attribute write, same as the chime: the policy reads the flag when
        it decides, so switching this off stops the next decision rather than
        having to unwind one. A compact already pushed is somebody else's
        history now — there is nothing here that could take it back.
        """
        self._settings["auto_compact"] = enabled
        save_preferences(updates={"auto_compact": enabled})
        if self._daemon:
            self._daemon.auto_compact_enabled = enabled

    def _set_typed_reply(self, enabled: bool) -> None:
        """Deliver replies by typing into the session's VS Code terminal, or
        go back to the channel alone.

        `_set_board_dispatch`'s shape: save the preference, set the daemon's
        plain attribute (read on the loop at the moment of the reply and on
        the snapshot executor when it publishes `reply_via`), then republish.
        The republish is the point: `channel` and `reply_via` ride every row,
        and the reply box appears or vanishes on the *next frame* rather than
        on whatever hook event happens next. Over the loop, because this runs
        on the AppKit thread; silently dropped with no loop, the same posture
        every other cross-thread call here takes.
        """
        self._settings["typed_reply"] = bool(enabled)
        save_preferences(updates={"typed_reply": bool(enabled)})
        if self._daemon:
            self._daemon.typed_reply_enabled = bool(enabled)
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._daemon._wake_surfaces(), self._loop)

    def _set_lan_access(self, enabled: bool) -> None:
        """Open or shut the phone listener.

        `_set_board_dispatch`'s shape: save the preference, tell the daemon.
        The daemon method is thread-safe and schedules `start_lan`/`stop_lan`
        onto its own loop when that loop is running; a failed LAN bind leaves
        the loopback listener alone.
        """
        self._settings["lan_access"] = bool(enabled)
        save_preferences(updates={"lan_access": bool(enabled)})
        if self._daemon:
            self._daemon.set_lan_access(bool(enabled))

    def _set_remote_access(self, enabled: bool) -> None:
        """Open or shut the away path — the relay connector.

        `_set_lan_access`'s shape exactly: save the preference, tell the
        daemon. `BobDaemon.set_remote_access` is thread-safe and schedules
        the connector's start/stop onto its own loop; nothing here blocks
        the AppKit thread.
        """
        self._settings["remote_access"] = bool(enabled)
        save_preferences(updates={"remote_access": bool(enabled)})
        if self._daemon:
            self._daemon.set_remote_access(bool(enabled))

    def _set_relay_ws(self, enabled: bool) -> None:
        """Open or shut the away link's socket lane.

        `_set_remote_access`'s shape exactly: save the preference, tell the
        daemon. `BobDaemon.set_relay_ws` is thread-safe and schedules the
        socket connector's start/stop onto its own loop — started only
        while away access is on too; nothing here blocks the AppKit thread.
        """
        self._settings["relay_ws"] = bool(enabled)
        save_preferences(updates={"relay_ws": bool(enabled)})
        if self._daemon:
            self._daemon.set_relay_ws(bool(enabled))

    def _set_phone_push(self, enabled: bool) -> None:
        """Buzz the paired phone about alerts, or stop.

        `_set_lan_access`'s shape: save the preference, tell the daemon. The
        daemon setter is a plain thread-safe bool write read at the moment an
        alert drains, so the flip silences (or arms) the very next buzz.
        """
        self._settings["phone_push"] = bool(enabled)
        save_preferences(updates={"phone_push": bool(enabled)})
        if self._daemon:
            self._daemon.set_phone_push(bool(enabled))

    def _set_board_dispatch(self, enabled: bool) -> None:
        """Let Dark Army start a session from a board card, or stop it entirely.

        Plain attribute write, like the chime and auto-compact above, and read at
        the moment of the press rather than held anywhere: `dispatch_card`
        checks it before anything else, so switching this off refuses the *next*
        press. A session already launched belongs to whoever is using it — there
        is nothing here that could take one back, and nothing that tries.
        """
        self._settings["board_dispatch"] = enabled
        save_preferences(updates={"board_dispatch": enabled})
        if self._daemon:
            self._daemon.board_dispatch_enabled = enabled
            # The flag rides in the board snapshot, and the Start button is
            # *absent* when it is false — so the panel has to be told now rather
            # than at whatever the next card edit happens to be. Over the loop
            # (this runs on the AppKit thread and the republish reads SQLite),
            # and silently dropped with no loop, which is the same posture every
            # other cross-thread call here takes.
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._daemon._publish_board(), self._loop)

    def _set_board_own_terminal(self, enabled: bool) -> None:
        """Run started cards on a terminal Dark Army itself owns, or in VS Code.

        `_set_board_dispatch`'s shape: save the preference, tell the daemon.
        Plain attribute write, read by `_dispatch_card_locked` at the moment
        of the press, so switching it changes the *next* Start and nothing
        already running. Republished for `_set_board_dispatch`'s reason: the
        flag rides the board snapshot (`own_terminal_enabled`) so the panel
        can say where the next terminal will open.
        """
        self._settings["board_own_terminal"] = enabled
        save_preferences(updates={"board_own_terminal": enabled})
        if self._daemon:
            self._daemon.board_own_terminal_enabled = enabled
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._daemon._publish_board(), self._loop)

    def _set_board_autostart(self, enabled: bool) -> None:
        """Let Dark Army start the head of a project's queue by itself.

        `_set_board_dispatch`'s shape exactly, republish included, and the
        republish is the point: `autostart_enabled` rides the board snapshot
        because a *queued card's wording* depends on it — "Dark Army will start it
        when the files are free" against "press Start when the files are
        free". Left unpublished, every queued card on screen would go on
        promising an action Dark Army had just been told not to take, until the next
        unrelated card edit happened to refresh the board.

        Checked at the moment the drain fires (`_flush_queue_dispatches`), so
        switching it off stops the *next* auto-start; a session already
        launched belongs to whoever is using it.
        """
        self._settings["board_autostart"] = enabled
        save_preferences(updates={"board_autostart": enabled})
        if self._daemon:
            self._daemon.board_autostart_enabled = enabled
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._daemon._publish_board(), self._loop)

    def _set_board_parallel(self, n: int) -> None:
        """How many agents may work at once in any one project.

        `_set_board_autostart`'s shape, republish included and for its reason:
        `parallel_limit` rides the board snapshot — the Pipeline readout draws
        "RUN 2/3" from it and a queued card's own sentence is composed against
        the count it bounds — so leaving it unpublished would show yesterday's
        number until some unrelated card edit refreshed the board.

        Stored unclamped and applied clamped. The daemon's setter is the one
        bound, so the number Dark Army obeys and the number it publishes are the
        same by construction even when the file on disk says something else.
        """
        self._settings["board_parallel"] = int(n)
        save_preferences(updates={"board_parallel": int(n)})
        if self._daemon:
            self._daemon.set_board_parallel(int(n))
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._daemon._publish_board(), self._loop)

    def _set_panel_scale(self, n: int) -> None:
        """The panel's size as a percent of the design.

        `_set_board_parallel`'s shape minus the daemon half: there is no
        daemon-side consumer and no snapshot to republish. Stored unclamped;
        the panel's `PanelScale.resolved` is the one bound, so a hand-edited
        300 draws at 100% and ticks 100%.
        """
        self._settings["panel_scale"] = int(n)
        save_preferences(updates={"panel_scale": int(n)})

    def _set_board_parallel_root(self, value) -> None:
        """One project's own dial, set from the pipeline heading.

        `_set_board_parallel`'s shape exactly — store, apply, republish — with
        the map as the stored thing. A limit of 0 (or anything below 1) is the
        wire's "back to the shared default" and removes the entry at both ends,
        so the file never grows a row meaning "the same as the default".

        Stored as sent and clamped at the daemon, for `_set_board_parallel`'s
        reason. This verb rides the panel's stdin/stdout channel alone:
        `preferences.json` is written by this process, so the write has to
        land here — it is not on the loopback or LAN action tables.
        """
        if not isinstance(value, dict):
            logger.info("ignoring a per-project parallel dial that is not a "
                        "root/limit pair")
            return
        root = str(value.get("root") or "").strip()
        if not root:
            logger.info("ignoring a per-project parallel dial with no project")
            return
        try:
            limit = int(value.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        current = dict(self._settings.get("board_parallel_by_root") or {})
        if limit < 1:
            current.pop(root, None)
        else:
            current[root] = limit
        self._settings["board_parallel_by_root"] = current
        save_preferences(updates={"board_parallel_by_root": current})
        if self._daemon:
            self._daemon.set_board_parallel_override(root, limit or None)
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._daemon._publish_board(), self._loop)

    def _set_agent_model(self, value) -> None:
        """One chip on the Agent models page: `{provider, slot, model, root?}`.

        `_set_board_parallel_root`'s shape — store, save, daemon setter — with
        two tables as the stored thing. An empty `root` writes the machine-wide
        table; a root writes that project's override, where a `model` of
        `agent_models.INHERIT` removes the entry (the file never grows a row
        meaning "the same as the default"). A name `agent_models.validate`
        refuses is one log line and **no write**: never trimmed to the
        nearest, never stored for the daemon to refuse later.

        No republish: nothing in a snapshot changes. The handler's own
        `_push_panel_context()` redraws the settings window. This verb rides
        the panel's stdin/stdout channel alone — `preferences.json` is this
        process's file — and is deliberately not in `PREFERENCE_REQUESTS`.
        """
        if not isinstance(value, dict):
            logger.info("ignoring an agent model press that is not a "
                        "provider/slot/model triple")
            return
        provider = str(value.get("provider") or "").strip()
        slot = str(value.get("slot") or "").strip()
        model = value.get("model")
        model = str(model if model is not None else "").strip()
        root = str(value.get("root") or "").strip()
        if not agent_models.validate(provider, slot, model, override=bool(root)):
            logger.info("refusing an agent model Dark Army does not know: "
                        "%s/%s = %r", provider, slot, model)
            return
        if not root:
            current = {k: dict(v) for k, v in
                       (self._settings.get("agent_models") or {}).items()
                       if isinstance(v, dict)}
            row = dict(current.get(provider) or {})
            row[slot] = model
            current[provider] = row
            self._settings["agent_models"] = current
            save_preferences(updates={"agent_models": current})
            if self._daemon:
                self._daemon.set_agent_models(current)
            logger.info("Agent model for %s/%s set to %s", provider, slot,
                        model or "Default")
            # The briefs carry the choice, so write it now, not next launch.
            self._resync_agent_packs()
            return
        by_root = {k: v for k, v in
                   (self._settings.get("agent_models_by_root") or {}).items()}
        table = {k: dict(v) for k, v in (by_root.get(root) or {}).items()
                 if isinstance(v, dict)}
        row = dict(table.get(provider) or {})
        if model == agent_models.INHERIT:
            row.pop(slot, None)
        else:
            row[slot] = model
        if row:
            table[provider] = row
        else:
            table.pop(provider, None)
        if table:
            by_root[root] = table
        else:
            by_root.pop(root, None)
        self._settings["agent_models_by_root"] = by_root
        save_preferences(updates={"agent_models_by_root": by_root})
        if self._daemon:
            self._daemon.set_agent_model_override(root, by_root.get(root))
        logger.info("Agent model for %s/%s in %s set to %s", provider, slot,
                    root, model)
        self._resync_agent_packs()

    def _set_board_close_terminal(self, enabled: bool) -> None:
        """Let a card arriving in Done dispose the terminal tab outright.

        Plain attribute write, checked by `_wrap_up_for_done` at the moment
        of the move, so switching it off refuses the *next* drag — a tab
        already disposed is gone, and nothing here pretends otherwise.

        **No republish**: the flag rides in no snapshot — the drag is
        offered either way. A refused close leaves the terminal alone with
        one log line and no orange note on the card.
        """
        self._settings["board_close_terminal"] = enabled
        save_preferences(updates={"board_close_terminal": enabled})
        if self._daemon:
            self._daemon.board_close_terminal_enabled = enabled

    def _set_dictation_shortcut(self, value) -> None:
        """Remember the hotkey MacWhisper is bound to, or decline it.

        Validates and declines rather than saving something unusable: a
        modifier-less key would fire while somebody is typing, and a key
        code outside 0…127 is not a virtual key. The follow-up context
        push corrects the row.
        """
        if not isinstance(value, dict):
            logger.warning("Rejected dictation_shortcut %r", value)
            return
        try:
            key_code = int(value["key_code"])
            modifiers = int(value["modifiers"])
            label = str(value.get("label") or "")
        except (KeyError, TypeError, ValueError):
            logger.warning("Rejected dictation_shortcut %r", value)
            return
        if not 0 <= key_code <= 127:
            logger.warning("Rejected dictation_shortcut key_code %r", key_code)
            return
        flags = modifiers & _DICTATION_MODIFIER_MASK
        if flags == 0 and key_code not in _DICTATION_MODIFIER_KEY_FLAGS:
            logger.warning("Rejected dictation_shortcut with no modifiers")
            return
        if len(label) > 8:
            logger.warning("Rejected dictation_shortcut label %r", label)
            return
        stored = {"key_code": key_code, "modifiers": flags, "label": label}
        self._settings["dictation_shortcut"] = stored
        save_preferences(updates={"dictation_shortcut": stored})

    def _dictation_target(self):
        """The recorded shortcut as (key_code, modifiers), or None if unusable."""
        shortcut = self._settings.get("dictation_shortcut") or {}
        if not isinstance(shortcut, dict) or "key_code" not in shortcut:
            logger.info("Dictate ignored: no shortcut recorded")
            return None
        try:
            key_code = int(shortcut["key_code"])
            modifiers = int(shortcut.get("modifiers") or 0)
        except (TypeError, ValueError):
            logger.info("Dictate ignored: shortcut is unusable")
            return None
        if not ax_is_process_trusted():
            logger.info("Dictate ignored: Accessibility is not granted")
            return None
        return key_code, modifiers

    def _on_dictate(self) -> None:
        """Legacy tap: down then up. An older panel against this menu bar."""
        target = self._dictation_target()
        if target is None:
            return
        key_code, modifiers = target
        try:
            post_dictation_key(key_code, modifiers, True)
            post_dictation_key(key_code, modifiers, False)
        except Exception:
            logger.warning("Dictate failed to post the shortcut", exc_info=True)

    def _on_dictate_down(self) -> None:
        """Hold the recorded shortcut down until the matching up."""
        if getattr(self, "_dictation_hold", None):
            logger.info("Dictate down ignored: already holding")
            return
        target = self._dictation_target()
        if target is None:
            return
        key_code, modifiers = target
        try:
            post_dictation_key(key_code, modifiers, True)
        except Exception:
            logger.warning("Dictate failed to post the shortcut", exc_info=True)
            return
        logger.info(
            "Dictate down key=%s modifiers=%s flags=0x%x",
            key_code, modifiers, _dictation_event_flags(key_code, modifiers, True),
        )
        self._dictation_hold = {"key_code": key_code, "modifiers": modifiers}
        # rumps.Timer's first fire is now; interval is the repeat. One-shot.
        self._dictation_deadline = time.monotonic() + DICTATION_HOLD_MAX_SECONDS
        try:
            timer = threading.Timer(
                DICTATION_HOLD_MAX_SECONDS, self._on_dictation_hold_expired)
            timer.daemon = True
            timer.start()
            self._dictation_timer = timer
        except Exception:
            logger.warning("Dictate hold timer failed to start", exc_info=True)

    def _on_dictate_up(self) -> None:
        self._release_dictation_if_down("up")

    def _on_dictation_hold_expired(self) -> None:
        deadline = getattr(self, "_dictation_deadline", None)
        # 1s slack: a 120s timer a few ms early must still release. An
        # immediate fire (the rumps.Timer trap) is well inside this bound.
        if deadline is not None and time.monotonic() < deadline - 1:
            logger.info("Dictate hold timer ignored: fired before deadline")
            return
        self._release_dictation_if_down("timeout")

    def _release_dictation_if_down(self, reason: str) -> None:
        """Idempotent release of a held dictation key. No AppKit.

        Posts the snapshotted HID up first, while the hold is still set, so
        a failed post can be retried. Bookkeeping and the delay cancel happen
        only after the up lands. A second call with nothing held posts nothing.
        """
        hold = getattr(self, "_dictation_hold", None)
        if not hold:
            logger.info("Dictate release skipped (%s): nothing held", reason)
            return
        key_code = hold["key_code"]
        modifiers = hold["modifiers"]
        try:
            post_dictation_key(key_code, modifiers, False)
        except Exception:
            logger.warning("Dictate failed to release the shortcut (%s)",
                           reason, exc_info=True)
            return
        logger.info("Dictate up key=%s (%s)", key_code, reason)
        self._dictation_hold = None
        self._dictation_deadline = None
        timer = getattr(self, "_dictation_timer", None)
        self._dictation_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                logger.debug("Dictate timer cancel failed", exc_info=True)

    def _on_open_accessibility_settings(self) -> None:
        """Open System Settings on the Accessibility pane."""
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security"
            "?Privacy_Accessibility",
        ])

    def _set_banners(self, enabled: bool) -> None:
        """Turn the macOS banners on or off.

        Ours alone — it does not touch what macOS was told, because the two
        answer different questions: System Settings decides whether this app may
        interrupt at all, this switch decides whether it wants to. Switching it
        back on cannot un-refuse a system-level no, so we say so rather than
        letting the switch pretend it did something.
        """
        self._settings["notification_banners"] = enabled
        save_preferences(updates={"notification_banners": enabled})
        if enabled and self._notifier.authorized is False:
            rumps.alert(
                "Notifications are turned off for Dark Army",
                "macOS is blocking them. Turn them on in System Settings › "
                "Notifications › Dark Army.",
            )

    def _install_channel_quietly(self) -> None:
        """The launch-time half of the channel install: worker thread, no
        alert, no settings echo. A failure is a log line — the toggle in the
        panel is the surface that reports one loudly."""
        try:
            channel_install.install()
        except Exception:
            logger.exception("Launch-time channel install failed")

    def _set_channel(self, enabled: bool) -> None:
        """Install or remove Dark Army's channel, and say what it does *not* do.

        Every other toggle in Settings changes something you can see. This one
        cannot: `--channels` is a flag a session is *started* with, so the six
        sessions already open are unaffected no matter what this says. The alert
        hands over the command rather than claiming success, which is also why
        the command is copied to the clipboard — a line nobody can paste is a
        feature nobody uses.

        Off the AppKit thread, like every other install here. Both halves shell
        out to `claude mcp` — two subprocesses, each with a CLI timeout — and on
        the main thread that froze the status item and the panel for as long as
        they took, on a toggle whose whole job is to feel like a checkbox.
        """
        def worker():
            ok = True
            try:
                if enabled:
                    # force: the toggle is a deliberate press, and it is also
                    # the repair path for a registration removed behind the
                    # skip-marker's back — it must re-run the CLI even when
                    # the recorded install still looks current.
                    ok = channel_install.install(force=True)
                else:
                    channel_install.uninstall()
            except Exception as e:
                logger.exception("Channel %s failed to run",
                                 "install" if enabled else "uninstall")
                ok = False
            callAfter(self._channel_change_finished, enabled, ok)

        threading.Thread(target=worker, name="channel-install", daemon=True).start()

    def _channel_change_finished(self, enabled: bool, ok: bool) -> None:
        # A PyObjC selector: an escaping exception is swallowed by the bridge,
        # so the whole body is guarded. Same shape as _vscode_install_finished.
        try:
            self._finish_channel_change(enabled, ok)
        except Exception:
            logger.exception("Could not finish the channel change")

    def _finish_channel_change(self, enabled: bool, ok: bool) -> None:
        if enabled and not ok:
            rumps.alert(
                "Could not install the channel",
                "The channel server could not be written to "
                f"{_tilde(channel_install.CHANNEL_SCRIPT.parent)}. "
                "The log has the path it tried.",
            )
            return
        self._settings["channel_enabled"] = enabled
        save_preferences(updates={"channel_enabled": enabled})
        if enabled:
            command = channel_install.launch_command()
            try:
                from AppKit import NSPasteboard, NSPasteboardTypeString
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(command, NSPasteboardTypeString)
            except Exception:
                logger.warning("Could not copy the channel command", exc_info=True)
            rumps.alert(
                "Channel installed — start a session with it",
                "Sessions already running cannot join: the channel is a launch "
                "flag. This command is on your clipboard:\n\n" + command,
            )

    def _on_notification_status(self, status: str) -> None:
        """`Notifier`'s explicit-status callback. Framework thread: hop the
        immutable answer to the main thread; nothing here touches AppKit."""
        callAfter(self._apply_notification_status, status)

    def _apply_notification_status(self, status: str) -> None:
        """Main thread. Adopt the read and ship it — `respawn=False`, because
        this also fires from the startup authorization callback and from a
        return to the panel, neither of which may open a window."""
        try:
            self._notification_status = (
                status if status in notifier.STATUSES else notifier.STATUS_UNKNOWN)
            self._push_panel_context(respawn=False)
        except Exception:
            logger.debug("Could not apply the notification status", exc_info=True)

    def _refresh_notification_status(self) -> None:
        """Ask for the current authorization status again. A deliberate
        return to the panel — a status-item press, a banner tap, the panel
        coming to the front after System Settings — is when a person who
        just allowed notifications expects the warning to go. Coalesced in
        the notifier; the answer arrives through `_on_notification_status`.
        Never a second authorization request."""
        n = getattr(self, "_notifier", None)
        if n is None:
            return
        try:
            n.refresh_status()
        except Exception:
            logger.debug("Could not refresh the notification status", exc_info=True)

    def _on_open_notification_settings(self) -> None:
        """Open System Settings on our own notification row.

        The `id=` query is what pins it to this app rather than the top of the
        list; without a bundle identity (a source checkout) there is nothing to
        pin it to, so it opens the section instead of pretending.
        """
        pane = "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
        bundle_id = notifier.bundle_identity()
        url = f"{pane}?id={bundle_id}" if bundle_id else pane
        subprocess.Popen(["open", url])


    def _on_install_hooks(self) -> None:
        """The panel's install button: write the scripts, then the settings
        that name them, and say which of the two it was."""
        refreshing = hooks.are_hooks_installed()
        _write_hook_scripts()
        hooks.install_hooks()
        self._settings["hooks_installed"] = True
        if refreshing:
            rumps.alert(
                title="Hooks Updated",
                message="Hooks have been updated for Claude Code and Grok. "
                        "Restart those sessions for the changes to take effect.",
            )
        else:
            rumps.alert(
                title="Hooks Installed",
                message="Hooks have been added to ~/.claude/settings.json and "
                        f"{_tilde(hooks.GROK_HOOKS_PATH)}. "
                        "Restart your Claude Code and Grok sessions for the hooks to take effect.",
            )

    def _on_install_vscode_extension(self) -> None:
        """(Re)install the bundled dark-army-ide extension so "Reveal in VS
        Code" can focus the terminal tab. Runs `code --install-extension`.

        On a worker, like _on_rebuild: `install_extension` allows itself 90s and the
        `is_installed` re-check another 15, and this is the AppKit main thread — run
        inline, a slow or hung `code` freezes the whole menu bar for as long as it
        takes. `_vscode_installing` keeps a second click from starting a concurrent
        --force install of the same extension; it rides down to the panel with the
        rest of the settings, so the row can show that it is working.
        """
        if self._vscode_installing:
            return
        self._vscode_installing = True
        self._push_panel_context()

        def worker():
            try:
                ok, detail = vscode_extension.install_extension()
                installed = vscode_extension.is_installed()
            except Exception as e:  # never leave the install flag stuck on
                logger.exception("VS Code extension install failed to run")
                ok, detail, installed = False, str(e), False
            callAfter(self._vscode_install_finished, ok, detail, installed)

        threading.Thread(target=worker, name="vscode-ext-install-click",
                         daemon=True).start()

    def _vscode_install_finished(self, ok, detail, installed):
        # A PyObjC selector: an escaping exception is swallowed by the bridge and
        # would leave the install flag stuck on. Same guard as _rebuild_finished.
        try:
            self._vscode_installing = False
            self._apply_vscode_ext_state(installed)
            if ok:
                rumps.alert(
                    title="VS Code Extension Installed",
                    message="The Dark Army extension was installed. Reload or reopen "
                            "a VS Code window for it to activate.",
                )
            else:
                rumps.alert(title="Could not install extension", message=detail)
        except Exception:
            logger.exception("Could not finish the VS Code extension install")

    def _refresh_vscode_ext_state(self) -> None:
        """Read whether the extension is installed, from a worker.

        `is_installed()` shells out to `code --list-extensions` (15s timeout), so it
        cannot run on the main thread. It also cannot be asked only once: main()
        kicks off `ensure_installed` on its own thread just before the app is
        constructed, so a first launch reads the state *while the install is still
        in flight* and would report it missing for the rest of the session.
        """
        def worker():
            try:
                installed = vscode_extension.is_installed()
            except Exception:
                logger.debug("Could not read VS Code extension state", exc_info=True)
                return
            callAfter(self._apply_vscode_ext_state, installed)

        threading.Thread(target=worker, name="vscode-ext-state", daemon=True).start()

    def _apply_vscode_ext_state(self, installed: bool) -> None:
        try:
            self._settings["vscode_extension"] = bool(installed)
            self._push_panel_context()
        except Exception:
            logger.debug("Could not apply VS Code extension state", exc_info=True)

    def _pack_profile_rows(self) -> list:
        """Rows for the panel's Install agent pack submenu. Never raises:
        a bad folder of the person's own leaves the three built-ins."""
        source = getattr(self, "_pack_root", None)
        if source is None:
            return []
        try:
            return pack_render.available_profiles(
                source, pack_install.user_profiles_dir())
        except Exception:
            logger.debug("could not list agent pack profiles", exc_info=True)
            return []

    def _install_agent_pack(self, value) -> None:
        """Install the shared agent pack into one enrolled project.

        Cheap on this thread: mark the root in-flight, push so the row greys,
        hand the render and writes to a worker. The ledger is this process's
        file, so the verb rides the panel channel and is not on loopback.
        """
        if not isinstance(value, dict):
            logger.info("ignoring an agent pack install that is not a "
                        "root/profile pair")
            return
        root = str(value.get("root") or "").strip()
        profile = str(value.get("profile") or "").strip()
        if not root or not pack_render.valid_profile_id(profile):
            logger.info("ignoring an agent pack install with no project or "
                        "an unknown profile")
            return
        if root in self._pack_installing:
            return
        self._pack_installing.add(root)
        self._push_panel_context()

        existing = pack_ledger.entry(root) or {}
        project = str(existing.get("project") or Path(root).name or "project")
        prefix = str(existing.get("prefix") or pack_render.default_prefix(project))
        app_name = str(existing.get("app") or "")
        gitnexus_repo = str(existing.get("gitnexus_repo") or "")
        # The per-role model table this project renders with, read from the
        # settings here on the AppKit thread and handed to the worker: the
        # renderer stays pure and the worker never reads `_settings`.
        models = agent_models.resolve(self._settings, root)

        def worker():
            try:
                ok, detail, owned = pack_install.install_pack(
                    root, profile, prefix, project,
                    app=app_name, gitnexus_repo=gitnexus_repo,
                    models=models)
            except Exception as exc:
                logger.exception("agent pack install failed to run")
                ok, detail, owned = False, str(exc), []
            callAfter(self._pack_install_finished, {
                "root": root,
                "profile": profile,
                "prefix": prefix,
                "project": project,
                "app": app_name,
                "gitnexus_repo": gitnexus_repo,
                "ok": ok,
                "detail": detail,
                "owned": owned,
            })

        threading.Thread(target=worker, name="agent-pack-install",
                         daemon=True).start()

    def _pack_install_finished(self, payload) -> None:
        try:
            root = str((payload or {}).get("root") or "")
            self._pack_installing.discard(root)
            ok = bool(payload and payload.get("ok"))
            detail = str((payload or {}).get("detail") or "")
            if not ok:
                logger.info("agent pack install into %s: %s", root, detail)
                rumps.alert(
                    title="Could not install the agent pack",
                    message=detail or "the install did not finish",
                )
            self._push_panel_context()
        except Exception:
            logger.exception("Could not finish the agent pack install")
            try:
                self._pack_installing.discard(
                    str((payload or {}).get("root") or ""))
            except Exception:
                pass

    def _stop_agent_pack_sync(self, value) -> None:
        root = str(value or "").strip()
        if not root:
            logger.info("ignoring stop pack sync with no project")
            return
        pack_ledger.forget(root)

    def _agent_models_for_root(self, root: str) -> dict:
        """`resync_all`'s `models_for`: the resolved per-role table for one
        project, read from the stored settings at the moment of the render."""
        return agent_models.resolve(self._settings, root)

    def _resync_agent_packs(self) -> None:
        def worker():
            try:
                pack_install.resync_all(models_for=self._agent_models_for_root)
            except Exception:
                logger.exception("agent pack resync failed")
            callAfter(self._push_panel_context)

        threading.Thread(target=worker, name="agent-pack-sync-click",
                         daemon=True).start()


    def _status_anchor(self):
        """The status item's frame in screen coordinates, `(x, y, w, h)`.

        AppKit gives the button's window frame in screen coordinates already, so
        no conversion is needed — but the item genuinely may not be on screen (a
        crowded menu bar hides items rather than shrinking them), and then there
        is no honest anchor and the panel places itself.

        The whole rect rather than a corner: the panel hangs from the right edge,
        and it also needs to recognise a click *on the item* so it does not
        fight the toggle that same click produces.
        """
        try:
            window = self._status_button().window()
            if window is None:
                return None
            frame = window.frame()
            return (float(frame.origin.x), float(frame.origin.y),
                    float(frame.size.width), float(frame.size.height))
        except Exception:
            logger.debug("Could not read the status item frame", exc_info=True)
            return None

    def _install_click_handler(self) -> bool:
        """Give the left button the panel and the right button the menu.

        Called after rumps has built the status item. Returns whether it is done,
        so the one-shot timer waiting for the button knows to stop. Failure is
        not fatal — the menu is still reachable, it just stays on the left button
        too — so it logs and carries on rather than taking the app down for a UI
        nicety.
        """
        if getattr(self, "_click_handler", None) is not None:
            return True
        try:
            from AppKit import NSEventMaskLeftMouseDown, NSEventMaskRightMouseDown

            item = self._nsapp.nsstatusitem
            button = item.button()
            if button is None:
                return False
            self._click_handler = _StatusClickHandler.alloc().initWithApp_(self)
            # The menu has to come *off* the item: an item that owns a menu never
            # sends its action at all.
            item.setMenu_(None)
            button.setTarget_(self._click_handler)
            button.setAction_("statusItemClicked:")
            button.sendActionOn_(NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown)
            logger.info("Status-item click handler installed: both buttons open "
                        "the panel")
            return True
        except Exception:
            logger.warning("Could not install the status-item click handler; "
                           "clicking the strip will do nothing unless the "
                           "emergency menu is up", exc_info=True)
            return True    # do not retry forever on a broken AppKit

    # The verbs the panel may ask for. A value-taking handler gets the payload;
    # the rest ignore it. Kept as one table so the set of things the panel can do
    # to the app is readable in one place — this is a pipe from another process,
    # and anything not named here is ignored rather than guessed at.
    PANEL_ACTIONS = {
        "rebuild":                    lambda app, _v: app._on_rebuild(None),
        "restart":                    lambda app, _v: app._on_restart(None),
        "quit_app":                   lambda app, _v: app._on_quit(None),
        "kill_all":                   lambda app, _v: app._on_kill_switch(None),
        "set_notification_sound":     lambda app, v: app._set_notification_sound(bool(v)),
        "set_notification_banners":   lambda app, v: app._set_banners(bool(v)),
        "set_channel":                lambda app, v: app._set_channel(bool(v)),
        "set_auto_compact":           lambda app, v: app._set_auto_compact(bool(v)),
        "set_typed_reply":            lambda app, v: app._set_typed_reply(bool(v)),
        "set_board_dispatch":         lambda app, v: app._set_board_dispatch(bool(v)),
        "set_board_own_terminal":     lambda app, v: app._set_board_own_terminal(bool(v)),
        "set_board_autostart":        lambda app, v: app._set_board_autostart(bool(v)),
        "set_board_parallel":         lambda app, v: app._set_board_parallel(int(v)),
        "set_board_parallel_root":    lambda app, v: app._set_board_parallel_root(v),
        "set_agent_model":            lambda app, v: app._set_agent_model(v),
        "set_panel_scale":            lambda app, v: app._set_panel_scale(int(v)),
        "set_board_close_terminal":   lambda app, v: app._set_board_close_terminal(bool(v)),
        "set_lan_access":             lambda app, v: app._set_lan_access(bool(v)),
        "set_remote_access":          lambda app, v: app._set_remote_access(bool(v)),
        "set_relay_ws":               lambda app, v: app._set_relay_ws(bool(v)),
        "set_phone_push":             lambda app, v: app._set_phone_push(bool(v)),
        "set_dictation_shortcut":     lambda app, v: app._set_dictation_shortcut(v),
        "dictate":                    lambda app, _v: app._on_dictate(),
        "dictate_down":               lambda app, _v: app._on_dictate_down(),
        "dictate_up":                 lambda app, _v: app._on_dictate_up(),
        "open_accessibility_settings": lambda app, _v: app._on_open_accessibility_settings(),
        "install_hooks":              lambda app, _v: app._on_install_hooks(),
        "install_vscode_extension":   lambda app, _v: app._on_install_vscode_extension(),
        "install_agent_pack":         lambda app, v: app._install_agent_pack(v),
        "stop_agent_pack_sync":       lambda app, v: app._stop_agent_pack_sync(v),
        "resync_agent_packs":         lambda app, _v: app._resync_agent_packs(),
        "open_notification_settings": lambda app, _v: app._on_open_notification_settings(),
        # The panel came to the front (after System Settings, say). A read,
        # not a request: it never raises the authorization dialog.
        "refresh_notification_status": lambda app, _v: app._refresh_notification_status(),
        "open_log":                   lambda app, _v: app._on_open_log(None),
    }

    def _on_panel_action(self, name: str, value=None) -> None:
        """A control in the panel asking the app to do something.

        Arrives on the panel's stdout reader thread, so everything here hops to
        the main thread: these all end in AppKit, and two of them end the
        process.

        One exception, handled before the table: `panel_visibility` is kept
        for an older panel binary and for the EOF clear, and it must not ride
        the generic path — that hops to the AppKit main thread and follows
        every action with `_push_panel_context`, which runs
        `dev_build.check_staleness`, a file-tree walk. The verb costs two
        scalar attribute writes on this reader thread and nothing else (the
        same cross-thread pattern as `notification_sound_enabled`: read at
        the moment of use).

        `dictate_down` / `dictate_up` stay in the table so they still hop via
        `callAfter` (a last-line down already queued on the main-thread FIFO
        must run before the matching up) but skip the context push: they
        change no settings, and EOF fires `dictate_up` after the panel is
        already dead — a push would `_send` → `_spawn` a hidden panel.
        """
        if name == "panel_visibility":
            if self._daemon:
                self._daemon.note_panel_visible(bool(value))
            return
        if name == "panel_terminal":
            # Which session's Dark Army-owned terminal the panel is drawing right
            # now — `panel_visibility`'s path exactly, for its reason: two
            # scalar writes on this reader thread, no `callAfter`, no
            # context push. Read by `_alert_suppressed` with the frontmost
            # poll's own trust window.
            if self._daemon:
                self._daemon.note_panel_terminal(
                    value if isinstance(value, str) else "")
            return
        handler = self.PANEL_ACTIONS.get(name)
        if handler is None:
            logger.warning("Ignoring unknown panel action %r", name)
            return

        def run():
            try:
                handler(self, value)
            except Exception:
                logger.exception("Panel action %r failed", name)
                return
            # Settings verbs change what the panel is drawing; it redraws from
            # the push rather than assuming its own click succeeded. Hold
            # down/up change no settings, and EOF fires `dictate_up` after
            # the panel is already dead — a push would `_send` → `_spawn`.
            # `refresh_notification_status` changes nothing either: the
            # answer arrives through `_apply_notification_status`, which
            # pushes with `respawn=False` — a second push here would race a
            # panel that quit between sending the line and its delivery.
            if name not in ("dictate_down", "dictate_up", "quit_app", "restart",
                            "kill_all", "refresh_notification_status"):
                self._push_panel_context()

        callAfter(run)

    def _push_panel_context(self, respawn: bool = True) -> None:
        """Hand the panel the facts it cannot fetch from the API: build
        staleness, Grok's window (a different account than `/api/usage` reports
        on), and every preference — the panel owns the settings UI now, so this
        is the only way it knows what the switches are set to. Cheap and
        idempotent, so it rides along with every open and every change.

        Cheap because the expensive probes (`check_staleness`'s glob+stat
        walk, the accessibility and MacWhisper reads) are computed on the
        limits worker every 30s and cached in `_panel_probes`; this method
        only ships them. The inline fallback covers a gesture that lands
        before the first worker tick.

        ``respawn=False`` is the periodic caller's flag: `_send` spawns a dead
        panel, so a push riding a timer must skip when the panel is not
        running — otherwise a crashed panel is silently relaunched every 30s,
        which is a crash *loop* wearing a heartbeat. A user gesture keeps the
        default and may respawn."""
        if not self._panel.available:
            return
        if not respawn and not self._panel.alive():
            return
        probes = getattr(self, "_panel_probes", None)
        if probes is None:
            probes = self._compute_panel_probes()
            self._panel_probes = probes
        info = probes.get("build_info")
        grok = getattr(self, "_grok_limits", {}) or {}
        percent = grok.get("percent") if not grok.get("stale") else None
        resets = grok.get("resets_at")
        self._panel.set_context(
            # Three-way, not two. `info is None` covers two different things:
            # a genuine release with no source anywhere (say so — the menu then
            # reads "Build: release (no source)"), and a checkout whose panel
            # binary is simply missing, where claiming "release" would be a
            # lie. `self._repo_root` is what tells them apart.
            build=(dev_build.artifact_label(info) if info is not None
                   else dev_build.artifact_label(None) if self._repo_root is None
                   else ""),
            build_stale=bool(info and info.get("stale")),
            can_rebuild=self._repo_root is not None,
            rebuild_label=dev_build.rebuild_title(self._repo_root),
            grok_percent=float(percent) if isinstance(percent, (int, float)) else None,
            grok_resets_at=float(resets) if isinstance(resets, (int, float)) else None,
            settings={
                **self._settings,
                # Two in-flight jobs, not preferences: the panel greys and
                # relabels their rows on these, so a click that starts minutes
                # of work is not indistinguishable from a click that missed.
                # `_rebuilding` used to stay a local flag, and `Rebuild &
                # Deploy` therefore never became `Rebuilding…` — the button
                # stayed live and silent for the whole of `build.sh --install`.
                "vscode_installing": self._vscode_installing,
                "rebuilding": self._rebuilding,
                # Which model each agent runs on, for the settings window:
                # the machine-wide table *resolved* (stored over shipped,
                # every slot present), the override map as stored, the
                # allowlist per provider and slot (`agent_models.allowed`,
                # so the panel offers only names Dark Army knows), and the
                # drawable slots — `main` plus every role the shipped pack
                # can write a brief for. A slot with no brief anywhere gets
                # no row: a control that changes no file is inert.
                "agent_models": agent_models.resolve_global(self._settings),
                "agent_models_by_root": (
                    self._settings.get("agent_models_by_root")
                    if isinstance(self._settings.get("agent_models_by_root"),
                                  dict) else {}),
                "agent_model_options": {
                    provider: {
                        slot: list(agent_models.allowed(provider, slot))
                        for slot in agent_models.SLOTS
                    }
                    for provider in agent_models.PROVIDERS
                },
                "agent_model_slots": agent_models.slots_for(
                    pack_render.shipped_roles(),
                    worker=pack_render.ships_shunt()),
                "agent_pack": {
                    "available": getattr(self, "_pack_root", None) is not None,
                    "projects": pack_ledger.published(),
                    "installing": sorted(getattr(self, "_pack_installing", ())),
                    "self_root": getattr(self, "_pack_self_root", "") or "",
                    "self_roots": list(
                        getattr(self, "_pack_self_roots", []) or []),
                    # The Install submenu's rows: the built-ins plus every
                    # profile folder, the person's own included. Cached in
                    # the renderer until a profile.json changes.
                    "profiles": self._pack_profile_rows(),
                },
                "accessibility_trusted": bool(
                    probes.get("accessibility_trusted")),
                "macwhisper_installed": bool(
                    probes.get("macwhisper_installed")),
                # What this launch did to hooks, the extension and the
                # login item, and what macOS says about banners. Both are
                # facts only this process has; the panel draws them on the
                # first-run checklist and under Troubleshooting.
                "launch": LAUNCH_REPORT.to_dict(),
                "notification_status": getattr(
                    self, "_notification_status", notifier.STATUS_UNKNOWN),
            },
        )

    def _install_emergency_menu_if_needed(self) -> None:
        """Put a dropdown back on the status item — but only when the panel
        cannot run, or when the self-restart cap is spent.

        The panel is the whole interface now. That is fine right up until it will
        not start (no Swift toolchain on a checkout, a crash, a half-finished
        build), at which point every verb this app has — including Quit — would
        be behind a window that never appears. The second trigger is the same
        argument one rung on: once `_health_check` has given up restarting
        itself, the app sits there with the offline mark and a panel talking to
        a daemon that is not there, so the kill switch / Open Log / Restart /
        Quit have to be reachable. It is the *same rows* either way — this
        widens an existing sanctioned fallback's predicate and adds no verb and
        no second surface.

        The kill switch leads, because this menu exists for exactly the state
        it is for: nothing else of Dark Army's is answering. It is the only row
        here that arms before it fires — see `_on_kill_switch`.

        Idempotent: a second call with rows already up returns rather than
        rebuilding them underneath an open menu. Main thread only.
        """
        if self._panel.available and not getattr(self, "_restart_gave_up", False):
            return
        if self.menu.keys():
            return
        logger.warning("Falling back to a minimal menu")
        self.menu = [
            rumps.MenuItem(KILL_SWITCH_TITLE, callback=self._on_kill_switch),
            None,
            rumps.MenuItem("Open Log", callback=self._on_open_log),
            None,
            rumps.MenuItem("Restart Dark Army", callback=self._on_restart),
            rumps.MenuItem("Quit Dark Army", callback=self._on_quit),
        ]

    def _popup_menu(self) -> None:
        """Show the emergency menu by hand, since the item does not own it.

        No-op in the normal case: with the panel running there is no menu, and a
        right-click gets the panel like a left-click does.
        """
        if not self.menu.keys():
            self._on_open_panel(None)
            return
        try:
            item = self._nsapp.nsstatusitem
            # rumps keeps the NSMenu on its own Menu wrapper, not on the status
            # item, once the item has been detached from it.
            item.setMenu_(self.menu._menu)
            item.button().performClick_(None)
            item.setMenu_(None)
        except Exception:
            logger.warning("Could not show the menu", exc_info=True)

    def _on_open_panel(self, _sender):
        """Show or hide the native panel, hung under the status item."""
        if not self._panel.available:
            rumps.alert(
                title="The panel is not built",
                message="Build it with:\n\n"
                        "    cd panel && swift build -c release",
            )
            return
        self._refresh_notification_status()
        self._push_panel_context()
        if not self._panel.toggle(self._status_anchor()):
            rumps.alert(title="The panel would not start",
                        message=f"See {_tilde(log_file_path())}.")

    def _open_panel_on(self, session_id: str, card: bool = False) -> None:
        """Open the panel with one agent selected, opened and unfolded.

        The tap on a banner, and — with `card` — the "Dark Army: Show this session"
        press inside VS Code, which also asks the board to scroll that
        session's card into view. `show` rather than `toggle`: the caller named
        a session, and closing the panel is never the answer to that — a press
        landing while the panel happens to be open would otherwise hide it.

        An empty `session_id` is the reverse jump's honest miss (Dark Army could not
        place that terminal): the panel still opens, plainly, because the press
        must never look like nothing happened.

        No alert on failure, unlike `_on_open_panel`: this path is entered from
        a banner that is already sliding away, and a modal about a missing Swift
        toolchain is not what a tap on "Gil needs you" asked for. It logs.
        """
        if not self._panel.available:
            logger.info("Panel request for %s: the panel is not built",
                        session_id or "(no session)")
            return
        self._refresh_notification_status()
        self._push_panel_context()
        if not session_id:
            if not self._panel.show(self._status_anchor()):
                logger.warning("Could not open the panel")
            return
        # The card half is passed only when asked for, so the banner tap's call
        # — and its payload — are exactly what they were.
        opened = (self._panel.show(self._status_anchor(), focus=session_id,
                                   card=True) if card
                  else self._panel.show(self._status_anchor(), focus=session_id))
        if not opened:
            logger.warning("Could not open the panel on %s", session_id)

    # The preferences a *phone* may ask this app to change, and the whole set
    # of them. `PANEL_ACTIONS`' shape and for its reason: this is a request
    # arriving from another process, so anything not named here is ignored
    # rather than guessed at. Deliberately a second, smaller table than
    # `PANEL_ACTIONS` — the panel sits at the desk and may set anything; the
    # phone may set the two pipeline dials and nothing else. The daemon keeps
    # the matching list (`daemon.PHONE_PREFERENCES`) because it is the one
    # that answers the wire; this one is what `on_preference_request` below
    # actually writes through.
    PREFERENCE_REQUESTS = {
        "board_autostart":     lambda app, v: app._set_board_autostart(bool(v)),
        "board_parallel_root": lambda app, v: app._set_board_parallel_root(v),
    }

    def on_preference_request(self, key, value) -> None:
        """A phone asked for one preference to change. Store it here.

        Arrives on the daemon's loop thread and ends in `preferences.json`
        plus AppKit-adjacent state, so it hops — `on_alerts`' pattern exactly,
        and nothing here does anything else. `run()` lands on the main thread,
        which is where `_set_board_autostart` / `_set_board_parallel_root`
        already live: the same thread the panel's own presses arrive on, so
        the settings file keeps one writer and `_settings` one mutator.

        `respawn=False` on the push is load-bearing: a press from the phone
        must never *open a window* on the Mac, and `_push_panel_context`'s
        default spawns the panel if it is not running.
        """
        handler = self.PREFERENCE_REQUESTS.get(str(key or ""))
        if handler is None:
            logger.info("ignoring a preference request for %r", key)
            return

        def run():
            try:
                handler(self, value)
            except Exception:
                logger.warning("preference request %r failed", key,
                               exc_info=True)
                return
            self._push_panel_context(respawn=False)

        callAfter(run)

    def on_panel_reveal(self, session_id: str) -> None:
        """A VS Code window asked Dark Army to come forward on its terminal's session.

        Arrives on the daemon's loop thread and ends in AppKit, so it hops —
        `on_alerts`' pattern, and nothing here does anything else: waiting on
        the main thread from a loop callback is the frozen-status-item bug.
        """
        callAfter(self._open_panel_on, session_id or "", True)

    def _refresh_build_status(self):
        """Warn once in the log when the build is stale.

        The user-facing half of this is the panel's `⋯` glyph, which turns amber
        and carries the build string — `_push_panel_context` sends it. There is
        no menu row to update any more."""
        if self._repo_root is None:
            return
        try:
            panel_bin = self._panel.executable_path
            info = dev_build.check_staleness(self._repo_root, panel_bin)
            if info and info["stale"]:
                logger.warning(
                    "Build is STALE: source is newer than the built artifact (%s). "
                    "Use '%s' in the panel.",
                    panel_bin,
                    dev_build.rebuild_title(self._repo_root),
                )
        except Exception:
            logger.exception("Build-staleness check failed")

    def _on_rebuild(self, _):
        """Rebuild the artifact this run mode uses, then reload so it takes effect.
        Runs the (slow) build off the main thread; UI updates hop back via
        callAfter. On success we reuse the normal restart path to relaunch.

        Everything this path touches after the build must already be imported:
        in a frozen app `build.sh` does `rm -rf dist`, i.e. it deletes and
        recreates the very bundle we are executing from, so any *cold* import
        afterwards reads a replaced `python314.zip` and dies with a
        ZipImportError. That is why `shlex`/`subprocess`/`callAfter` are
        module-level imports and not function-local ones."""
        if self._repo_root is None or self._rebuilding:
            return
        self._rebuilding = True   # debounce: the panel greys its own row on this
        self._push_panel_context()

        def worker():
            try:
                result = dev_build.rebuild(self._repo_root)
                ok = result.returncode == 0
                msg = (result.stderr or result.stdout or "")[-400:]
            except Exception as e:  # timeout / launch failure
                logger.exception("Rebuild failed to run")
                ok, msg = False, str(e)
            callAfter(self._rebuild_finished, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _rebuild_finished(self, ok, msg):
        # We run as a PyObjC selector (callAfter): an exception escaping here is
        # swallowed by the ObjC bridge, which would leave the panel stuck on
        # "Rebuilding…" forever with nothing in the log. Catch it ourselves.
        self._rebuilding = False
        try:
            if ok:
                logger.info("Rebuild succeeded; reloading.")
                self._refresh_build_status()
                # Reuse the tested relaunch path: in a bundle this reloads the
                # rebuilt .app; in dev it relaunches the menu bar, which respawns
                # the freshly built sim. (_on_restart exits this process.)
                self._on_restart(None)
                return
            logger.error("Rebuild failed:\n%s", msg)
        except Exception:
            logger.exception("Reload after a successful rebuild failed")
        self._push_panel_context()
        try:
            rumps.notification("Dark Army", "Rebuild failed",
                               "See the log for details.")
        except Exception:
            pass

    def _shutdown_daemon(self):
        """Shut the daemon down cleanly. Blocks until done (or the 8s timeout).
        Shared by quit and restart. Hosted terminals stay up — the broker
        holds them — so a relaunch reconnects instead of SIGHUPing."""
        daemon, loop = self._daemon, self._loop
        if not (daemon and loop):
            return
        stopping = asyncio.run_coroutine_threadsafe(
            daemon._shutdown(keep_terminals=True), loop)
        stopping.result(timeout=8)

    def _on_quit(self, _):
        """Claim the teardown once; neither panel nor daemon waits run on AppKit."""
        if self._restarting:
            return
        self._restarting = True
        threading.Thread(target=self._quit_now, name="quit", daemon=True).start()

    def _quit_now(self):
        """Stop the panel and daemon on a worker, then hand AppKit its quit."""
        try:
            self._release_dictation_if_down("quit")
            # Before the daemon: the panel is holding an SSE connection to it,
            # and a reader watching its socket die logs a reconnect storm on the
            # way out for no reason.
            self._panel.quit()
            self._shutdown_daemon()
            callAfter(self._finish_quit)
        except Exception:
            logger.exception("Quit failed part-way; ending the process now")
            logging.shutdown()
            os._exit(1)

    def _finish_quit(self):
        """The sole AppKit part of Quit, called on its main run loop."""
        try:
            rumps.quit_application()
        except Exception:
            logger.exception("Error finishing quit, force-killing")
            logging.shutdown()
            os._exit(1)

    # --- The kill switch ---------------------------------------------------
    #
    # Quit is polite: it stops the daemon cleanly and deliberately leaves the
    # hosted terminals up so a relaunch can reconnect to them. This is the
    # other verb, for the moment when that is exactly the wrong answer — one
    # press and nothing of Dark Army's is left running on the machine.
    #
    # What may be signalled is `kill_switch`'s proof and nothing looser: a
    # process running Dark Army's own code, plus the tree below it (which is
    # how the agents inside Dark Army's own hosted terminals are reached —
    # they are started in their own session, so killing the broker alone
    # would orphan them). An assistant in a VS Code terminal is the editor's
    # child and the person's work; it loses Dark Army and carries on.

    def _kill_plan(self):
        """What one press would signal. Whole-machine process walk — worker
        thread only."""
        # `is_frozen()` first, and it is load-bearing. Outside a bundle
        # `bundle_path()` falls back to `NSBundle.mainBundle()`, which for a
        # plain interpreter answers the *Python framework's* own
        # `Python.app` — a real bundle, just not ours. Taken at face value
        # that makes every process running under that interpreter a target,
        # which on a development machine is most of them. Both functions read
        # this module's own `__file__`, so the gate is exact: a bundle root
        # here is only ever the bundle this code is executing from.
        bundle = dev_build.bundle_path() if dev_build.is_frozen() else None
        executables = []
        try:
            # In a checkout there is no bundle, and the panel binary sits
            # outside one. The app chose that path at startup, so it can name
            # it outright rather than guessing at it from an argv.
            exe = self._panel.executable_path
            if exe:
                executables.append(exe)
        except Exception:
            logger.debug("kill switch: no panel path to name", exc_info=True)
        from dark_army_daemon.paths import STATE_DIR

        return kill_switch.plan(
            kill_switch.snapshot(),
            bundle_root=str(bundle) if bundle else "",
            state_dir=str(STATE_DIR),
            executables=executables,
            own_pid=os.getpid())

    def _on_kill_switch(self, sender):
        """Armed, then confirmed, then done — the panel's rule for every
        destructive press.

        `sender` is the emergency menu item when the click came from the menu
        bar and None when the panel sent `kill_all`. The panel does its own
        arming, so its press fires; the menu item has no second surface to
        arm on, so the first click retitles the row and the second one goes.
        """
        if self._restarting:
            return
        if sender is not None and not getattr(self, "_kill_armed", False):
            self._kill_armed = True
            self._retitle_kill_row(KILL_SWITCH_ARMED_TITLE)
            # An armed row left armed for ever is a trap. Disarm it after a
            # few seconds, back on the main thread — this is AppKit.
            timer = threading.Timer(KILL_SWITCH_ARM_SECONDS,
                                    lambda: callAfter(self._disarm_kill_switch))
            timer.daemon = True
            timer.start()
            return
        self._kill_armed = False
        self._restarting = True
        logger.warning("Kill switch pressed; stopping everything Dark Army owns")
        threading.Thread(target=self._kill_now, name="kill-switch",
                         daemon=True).start()

    def _retitle_kill_row(self, title: str) -> None:
        """rumps keys a menu item by the title it was built with, so the row
        is found by that key and only its displayed title changes."""
        try:
            for key in list(self.menu.keys()):
                if key.startswith(KILL_SWITCH_TITLE):
                    self.menu[key].title = title
        except Exception:
            logger.debug("kill switch: could not retitle the row", exc_info=True)

    def _disarm_kill_switch(self) -> None:
        if not getattr(self, "_kill_armed", False):
            return
        self._kill_armed = False
        self._retitle_kill_row(KILL_SWITCH_TITLE)

    def _kill_now(self):
        """Signal everything on the plan, then end this process.

        Worker thread (see `_on_kill_switch`): the process walk is a
        whole-machine scan and the pause between SIGTERM and SIGKILL is real
        time. Nothing here is AppKit, and `os._exit` needs no run loop.

        The daemon is *not* shut down cleanly first. That is the difference
        between this and Quit: a clean stop is what you press when the app is
        answering, and this is what you press when it is not. The stores are
        SQLite in WAL mode and survive the process ending under them.
        """
        try:
            self._release_dictation_if_down("kill switch")
        except Exception:
            logger.debug("kill switch: dictation release failed", exc_info=True)
        try:
            plan = self._kill_plan()
            if plan.overflowed:
                # The rules named more than a machine could plausibly be
                # running. Signal nothing: an over-wide sweep is the one
                # failure this button must never have.
                logger.error(
                    "Kill switch refused: the rules named more than %d "
                    "processes, so nothing was signalled",
                    kill_switch.MAX_VICTIMS)
                self._restarting = False
                # Unconditionally, not through `_disarm_kill_switch`: the
                # press already spent the arming, so that would find nothing
                # to do and leave the row reading "click again" for ever.
                callAfter(lambda: self._retitle_kill_row(KILL_SWITCH_TITLE))
                return
            if plan.victims:
                logger.warning(
                    "Kill switch: signalling %d process(es): %s",
                    len(plan.victims),
                    ", ".join("%d (%s)" % (proc.pid, plan.reasons.get(proc.pid, "?"))
                              for proc in plan.victims))
                kill_switch.terminate(plan.victims)
            else:
                logger.warning("Kill switch: nothing else of Dark Army's was running")
        except Exception:
            logger.exception("Kill switch: the sweep failed; ending this "
                             "process anyway")
        logger.warning("Kill switch: Dark Army stopped")
        logging.shutdown()
        os._exit(0)

    def _on_open_log(self, _):
        """Open the app log in whatever handles .log files."""
        path = log_file_path()
        if not path.exists():
            rumps.alert("No log yet", str(path))
            return
        subprocess.Popen(["open", str(path)])

    def _on_restart(self, _):
        """Mark the app as going down, then do the teardown off the main thread.

        The teardown blocks for as long as the daemon takes to stop (up to 8s),
        and it used to run right here — on the AppKit thread. For those seconds
        the status item was dead: the menu would not open, so clicking Restart
        looked like clicking nothing, and the only feedback was the app finally
        vanishing. The work moves to a worker thread, and the row that ends the
        process — the emergency menu's, when there is one — goes grey with a
        "please hold" title, so reopening it during the wait says what is
        happening."""
        if self._restarting:
            return
        self._restarting = True
        logger.info("Restarting Dark Army...")
        for key in list(self.menu.keys()):
            if key.startswith("Restart"):
                item = self.menu[key]
                item.title = RESTART_PENDING_TITLE
                item.set_callback(None)   # greys the row out
        threading.Thread(
            target=self._restart_now, name="restart", daemon=True
        ).start()

    def _restart_now(self):
        """Tear down cleanly, then relaunch the app as a brand-new process.

        We deliberately do NOT use os.execv: replacing the process image in place
        keeps the same PID, and macOS does not re-register the status item
        (NSStatusItem) for a re-exec'd process — the app comes back running but
        with no menu bar icon. Instead we spawn a fully-detached helper that waits
        for this process to fully exit, then launches a fresh one. The new process
        gets a new NSApplication + NSStatusItem, so the icon reliably reappears,
        and waiting for our death avoids racing on the daemon/sim TCP ports.

        Runs on a worker thread (see _on_restart), which is safe because nothing
        here is AppKit: the panel is killed over its pipe, the daemon is stopped
        on its own loop, and os._exit needs no run loop."""
        # Was this Dark Army's own idea? `_health_check` has already stamped the
        # ledger by now — on the main thread, and only after checking that the
        # stamp actually landed, because the count is the only bound on the
        # flapping. A person's press leaves the flag False, writes no stamp,
        # and is never counted against the cap or refused; the log says which
        # route this teardown took.
        if getattr(self, "_auto_restart_pending", False):
            logger.info("Relaunching after an automatic restart")
        try:
            self._release_dictation_if_down("restart")
            # Same order as quit: the panel holds an SSE connection to the
            # daemon, and a reader watching its socket die logs a reconnect
            # storm on the way out for no reason.
            self._panel.quit()
        except Exception:
            logger.exception("Error stopping the panel during restart")
        try:
            self._shutdown_daemon()
        except Exception:
            logger.exception("Error during restart shutdown; relaunching anyway")
        # Detached relauncher: block until our PID is gone, then start a fresh app.
        relaunch = _relaunch_command()
        subprocess.Popen(
            ["/bin/sh", "-c",
             "while kill -0 {pid} 2>/dev/null; do sleep 0.2; done; exec {cmd}".format(
                 pid=os.getpid(), cmd=relaunch)],
            start_new_session=True,
        )
        logging.shutdown()
        os._exit(0)


def _app_bundle_path() -> Optional[str]:
    """The `.app` we are running out of, or None in a source checkout."""
    for parent in Path(os.path.realpath(__file__)).parents:
        if parent.suffix == ".app":
            return str(parent)
    return None


def _relaunch_command() -> str:
    """The shell command that starts the replacement app.

    `open -n -a <bundle>` rather than exec'ing our own interpreter, and the
    difference is not cosmetic. A bare exec inherits the lineage of whatever
    started the app — which, on the restart path, is a terminal inside a VS Code
    window. Processes spawned by *that* app went on to deadlock: every `code` the
    daemon ran (the Jump raiser, the extension installer) hung forever in module
    init and was still hanging hours later, while the identical command from any
    other parent returned in a second. Going through LaunchServices gives the new
    process the same clean lineage a Finder launch does, and the hang disappears.

    In a source checkout there is no bundle to open, so the exec is kept — that
    build is already being launched from a terminal and has the same lineage
    either way. `vscode_reveal` no longer depends on the `code` CLI for the raise,
    so a dev build degrades in comfort, not in function.
    """
    bundle = _app_bundle_path()
    if bundle:
        return "/usr/bin/open -n -a {app}".format(app=shlex.quote(bundle))
    return "{py} -m dark_army_menubar".format(py=shlex.quote(sys.executable))


#: The one line format the log file has always had; its first ten characters
#: are what `logsetup` reads back to decide which day a file belongs to.
_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"


def _log_level() -> int:
    """INFO unless asked otherwise: the per-frame `SSE push:` line and the
    panel's matching trace pair live at DEBUG now (they were 86% of a day's
    log). To get the pair back: quit Dark Army, run
    `launchctl setenv BOB_COMPANION_LOG_LEVEL DEBUG` and
    `launchctl setenv BOB_PANEL_TRACE 1`, then relaunch — the panel inherits
    this process's environment. Unset both and relaunch to quiet it again.
    Anything that is not a level name falls back to INFO rather than raising
    into startup."""
    level = getattr(logging, os.environ.get("BOB_COMPANION_LOG_LEVEL", "INFO").upper(),
                    logging.INFO)
    return level if isinstance(level, int) else logging.INFO


class _EarlyLog:
    """Holds what the first steps of `main()` log — `adopt_login_path` among
    them — until there is a file to write it to. A `MemoryHandler` on the
    root logger with no target."""

    def __init__(self, level: int):
        import logging.handlers
        self.root = logging.getLogger()
        self.handler = logging.handlers.MemoryHandler(
            capacity=100_000, flushLevel=logging.CRITICAL + 10, target=None)
        self.saved_level = self.root.level
        self.root.setLevel(level)
        self.root.addHandler(self.handler)

    def take(self) -> list:
        """Stop holding; hand back what was held, oldest first."""
        self.root.removeHandler(self.handler)
        self.root.setLevel(self.saved_level)
        records = list(self.handler.buffer)
        self.handler.buffer.clear()
        self.handler.close()
        return records


def _configure_logging(level: int, early: "_EarlyLog") -> None:
    """The console and today's file, then everything held since launch
    replayed into both. Handlers added outright rather than through
    `logging.basicConfig`, which does nothing when the root logger already
    has a handler — so a held record could never be flushed through it."""
    records = early.take()
    formatter = logging.Formatter(_LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)
    # Today's file, with the days before it gzipped alongside.
    for handler in (logging.StreamHandler(), logsetup.build_handler()):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    for record in records:
        root.handle(record)


def _migrate_enrolment_keys() -> None:
    """Copy every enrolled project's old-name key to `.dark-army/key`.

    The body of the `enrolment-key-move` thread `main()` starts: a handful of
    stats and small writes per enrolled root, off the AppKit thread because a
    slow network volume must not delay the strip. It touches no AppKit, waits
    on no daemon and reports through the log alone; a failure is a log line.
    """
    try:
        from dark_army_daemon import enrollment
        for line in enrollment.migrate_key_folders():
            logger.info("enrolment key move: %s", line)
    except Exception:
        logger.warning("could not move the enrolment keys", exc_info=True)


def main():
    from dark_army_daemon import paths, subprocess_env
    # A launchd start has the bare PATH; everything this app runs inherits
    # a login shell's instead (`subprocess_env.adopt_login_path`).
    subprocess_env.adopt_login_path()
    # What these first steps log is held (`_EarlyLog`) and written once the
    # log file is configured.
    level = _log_level()
    early = _EarlyLog(level)
    _configure_logging(level, early)
    from .version import get_version
    logger = logging.getLogger("dark-army.menubar")
    logger.info("Dark Army %s starting", get_version())
    logsetup.report_startup(logger)
    paths.ensure_state_dir()

    # Dark Army's own checkout enrols itself, and nothing else does — so upgrading to
    # the enrolment gate does not leave the machine silent with no obvious way
    # back in. One small file write and one stat on the AppKit thread, before
    # the status item exists; it waits on nothing (that is `_restart_now`'s
    # trap) and a failure is a log line, never a startup that does not finish.
    try:
        from dark_army_daemon import enrollment
        enrolled, enrol_detail = enrollment.enroll_self()
        logger.info("enrolment: %s", enrol_detail)
    except Exception:
        logger.warning("could not enrol Dark Army's own project", exc_info=True)
    # The folders an agent may search (`search_scope.py`), rewritten once per
    # launch after the self-enrolment and before the hooks install the script
    # that reads it. The same register: one atomic file write on the AppKit
    # thread before the status item exists, waiting on nothing.
    try:
        from dark_army_daemon import search_scope
        search_scope.refresh()
        logger.info("search scope: %d folders", len(search_scope.roots()))
    except Exception:
        logger.warning("could not write the search scope", exc_info=True)
    # Every enrolled project gains `.dark-army/key`, a copy of the key it
    # already holds under the old folder name; the ledger, the project's own
    # `.gitignore` and the old file are untouched. Joined by nothing.
    threading.Thread(
        target=_migrate_enrolment_keys, name="enrolment-key-move", daemon=True
    ).start()

    # Before the hooks and before the app: on a machine that has never run
    # Dark Army, the marker is written and launch at login is enabled — and nothing
    # else (`first_run.FIRST_RUN_PREFERENCES` is empty). What the login item
    # actually came to is recorded for the launch line, never re-run for it.
    first = first_run.apply_first_run()
    LAUNCH_REPORT.set(launch_report.LOGIN_ITEM, first.login_status,
                      first.login_detail)
    # Hooks are judged by a before/after read around the ordinary conditional
    # install — the launch line must never say "installed" about an attempt.
    hooks_before = _hooks_current()
    try:
        _write_hook_scripts()
        # Refreshed by *content*, like the notify script above: a version-marker
        # gate cannot repair a file whose marker is right and whose body is wrong,
        # but comparing the whole body can — an identical copy is skipped, any
        # difference is rewritten, which still closes the upgrade window where a
        # pre-enrolment copy sends unkeyed messages the daemon throws away.
        statusline.install_statusline_script()
        if not statusline.is_statusline_installed():
            statusline.install_statusline()
        if not hooks.are_hooks_installed():
            logger.info("Hook settings are out of date; rewriting them")
            hooks.install_hooks()
    except Exception:
        _record_hook_outcome(hooks_before, False, error=True)
        raise
    _record_hook_outcome(hooks_before, _hooks_current())

    # Install/upgrade the VS Code extension off the main thread: `code` is a
    # subprocess and the install reloads extension hosts, neither of which should
    # block the menu bar coming up. No-op when already current. Its actual
    # result — current, installed, skipped, failed — lands in the launch
    # report through the callback, which pushes without respawning.
    threading.Thread(
        target=vscode_extension.ensure_installed,
        kwargs={"report": _report_extension_result},
        name="vscode-ext-install", daemon=True
    ).start()

    # Hide the Dock icon so only the menu-bar strip shows. The built .app sets
    # LSUIElement in its plist, but when launched via the Python interpreter
    # (dev / no-admin install) there is no plist, so macOS would otherwise show
    # the interpreter's rocket in the Dock. Set the accessory activation policy
    # at runtime — no admin, no bundle required.
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
    except Exception as e:  # pragma: no cover - platform/AppKit quirks
        logger.warning("Could not set accessory activation policy: %s", e)

    app = BobCompanionApp()
    global _RUNNING_APP
    _RUNNING_APP = app
    app._start_daemon_thread()
    # Bring pressed projects' agent packs back into step off the AppKit
    # thread: a render plus tens of file compares must not delay the status
    # item. Bounded inside resync_all; a failure is a log line.
    def _resync_packs():
        try:
            pack_install.resync_all(models_for=app._agent_models_for_root)
        except Exception:
            logger.exception("agent pack resync failed")

    threading.Thread(
        target=_resync_packs, name="agent-pack-sync", daemon=True
    ).start()
    # The status item does not exist until rumps has built it, which happens
    # inside run(), so the handler cannot be attached before then. A one-shot
    # timer is the first moment on the main loop when the button is real.
    def _attach(timer):
        if app._install_click_handler():
            timer.stop()

    rumps.Timer(_attach, 0.1).start()

    # The strip's clock is instance-owned so `_animate_icon` can re-arm it, and
    # rumps' run() starts only decorated timers.
    app._strip_timer.start()

    # Ask macOS for the right to interrupt. On the first run loop tick rather
    # than in __init__: the authorization callback is delivered on the run loop,
    # and there is not one until run() below. Asking is idempotent — macOS
    # remembers the answer per bundle id and never re-prompts — and it is a no-op
    # from a source checkout, which has no bundle identity to ask with.
    def _authorize(timer):
        timer.stop()
        if app._notifier.available:
            app._notifier.start()
        else:
            logger.info("No banners: %s", "not running from the .app bundle"
                        if not notifier.bundle_identity()
                        else "UserNotifications unavailable")

    rumps.Timer(_authorize, 0.2).start()

    # Say, once, that the last copy went down by itself. The marker is read
    # once and cleared, so an ordinary launch — and a restart somebody pressed
    # — says nothing at all.
    #
    # The interval is **not** a delay: rumps' `Timer.start` builds the NSTimer
    # with `fireDate = NSDate.date()`, so the first fire is the next run-loop
    # pass and 3.0 is only the repeat. `_authorize` above answers on the run
    # loop too, so waiting is something this has to do for itself — it re-arms
    # while `authorized` is still None rather than posting into an
    # authorization that has not been answered. **The notice is not taken out
    # of the ledger until there is going to be a post**: the read clears it, so
    # consuming it on a pass that drops the banner loses it silently.
    waited = [0]

    def _announce(timer):
        n = app._notifier
        if announce_should_wait(n.available, n.authorized, waited[0]):
            # Still asking. Come back in `interval` seconds, notice intact.
            waited[0] += 1
            return
        timer.stop()
        # Past here the notice is spent whatever happens: with no bundle to
        # post from, or a user who said no, a banner can never be drawn and is
        # not worth saying an hour later.
        try:
            notice = self_restart.take_pending_notice()
        except Exception:
            logger.warning("Could not read the restart notice", exc_info=True)
            return
        if notice == self_restart.NOTICE_RESTARTED:
            app._notifier.post_notice(self_restart.RESTARTED_IDENT,
                                      self_restart.RESTARTED_TITLE,
                                      self_restart.RESTARTED_BODY)

    rumps.Timer(_announce, 3.0).start()

    app.run()


if __name__ == "__main__":
    main()
