# Menu-bar strip contract

This is the full statement of how the menu-bar status item is composed,
typeset, measured and collapsed, and of the usage cluster that ends it.
`CLAUDE.md` restates the invariants and names the tests; the argument and the
detail live here.

## The strip is an animated multi-face line, not an icon

`dark_army_menubar/` is the macOS status bar app (rumps). The status item
is an **animated multi-face strip**, not a single icon: `_compose_strip()` lays
out one animated face per live category (work / attn) with a count, never more
than one face each (`MAX_FACES` is 1), rendered into the button's attributed
title by `_render_strip()` and ticked by `_animate_icon()` at `ICON_TICK`
while something animates, `ICON_TICK_REST` otherwise.
Sleeping sessions are omitted; an empty pond shows a resting idle face. Idle
frames come in light/dark variants chosen from the *status button's*
`effectiveAppearance` (not `NSApp`'s).

**Only the working category animates.** Motion on the strip means one thing —
somebody is running — so a resting face and a waiting face are states rather
than activity and each hold **one** frame, named per category by
`STILL_FRAMES` (`{"idle": -1, "attn": 0}`) and applied by `_held` at every
return of `_frames_for`, so the aggregate glyphs and the named cast faces
freeze together. The index differs because the pose that *says* the state
does: the sleeping face's Zzz is fully drawn only in the **last** slot, and
the waiting face's red alert is in the **first** slot alone (`dark-army-attn-2`
carries no red pixels at all), so freezing both on index 0 would lose the Zzz
and freezing both on the last would lose the red. `off` is already a single
frame and is deliberately not a member.

This is what makes `_render_strip`'s signature skip bite: the frame names are
part of `sig`, so on a Mac with nothing working the composed signature stops
changing and the status item is never told to repaint. `_anim_i` keeps
advancing; the collapse ladder is untouched, since none of its inputs contain
a frame name.

**The clock rests when nothing moves.** The strip owns its `rumps.Timer`
(`_strip_timer`, started in `main()`), and every exit of `_animate_icon`
re-arms it after the render: `ICON_TICK` (0.2s, closed — 0.4s was tried and
read as a stutter) while `_strip_animating()` says a live category is absent
from `STILL_FRAMES`, `ICON_TICK_REST` (1s) otherwise, offline included. A slow
tick, not a stopped one: the to-do count, the faces, the usage limits, the
bar's appearance and a dead daemon thread land without a wake. A count change
does wake it — `on_activity_change` hops `callAfter(self._strip_wake)` to the
main thread, which draws at once and speeds the clock back up.
`_strip_clock_set` always re-arms by `stop()` then `start()`: rumps drops an
`interval` set on a live timer inside its first interval, and `start()` fires
on the next run-loop pass.

## The numbers are typeset, not just printed

Counts are 12pt Medium **monospaced-digit** (`COUNT_PT`); there is **no
separator glyph** — every group opens with a sprite. Gaps are kerning
(`GAP_FROG` 3pt, `GAP_GROUP` 9pt, `GAP_USAGE` 14pt) on each run's *last
character only*, or a 9pt gap between the run's own digits. The subagent count
is a superscript footnote (`SUFFIX_PT`/`SUFFIX_RISE`); the attention count is
the strip's only coloured number — systemRed, Semibold, drawn only when
non-zero beside a frog with a red alert dot.

**Counts stay real text** in `labelColor`. Only the usage cluster is an image,
and everything in an image resolves its own ink (see `ICON_DISCONNECTED`).

**Kerning after an attachment is discarded by AppKit** — a gap after an icon
must be a real spacer character (`_render_strip`).

## The collapse ladder

`STRIP_LADDER`, `STRIP_BUDGET_PT` = 300pt. `_animate_icon` renders a rung,
measures what `_render_strip` returns, and steps down until it fits — the
subagent footnote, then the usage clusters **one provider at a time** (Codex
first, Claude last), then the to-do count; the floor is the two live figures
and their counts. The search runs when the numbers change (`_strip_key` /
`_strip_level`). Dropping the under-meter is deliberately **not** a rung.

The rungs are named `StripRung` fields, not positional booleans. The strip
measures itself and steps down because macOS never says when neighbours crowd
it.

## There is no dropdown

Both mouse buttons open the panel (`_StatusClickHandler`) and `self.menu` is
empty. Preferences live in the panel's settings window (⌘,) over the
stdin/stdout channel (`PANEL_ACTIONS`, `_push_panel_context`);
`_install_emergency_menu_if_needed()` puts three rows (Open Log / Restart /
Quit) back on the status item **when the panel cannot run, or when the
self-restart cap is spent** — `_health_check` restarts the app through
`_on_restart` when the daemon thread dies, at most `MAX_AUTO_RESTARTS` (3) per
hour across processes (`self_restart.py`, `restart-watch.json`), and once that
cap is spent it stays down with the offline mark and calls this, so Open Log,
Restart and Quit are still reachable. Same three rows either way; the build is
idempotent, so a second call cannot rebuild them under an open menu.
Preferences are
plain state on the app (`self._settings`), persisted to
`~/.dark-army/preferences.json` with read-modify-write. Hooks auto-update
on startup when the installed version is outdated.

## The usage cluster ends the strip

**Rate-limit windows** are on both surfaces, from one reading.
`_refresh_limits` (a 30s timer) computes `limits.snapshot()` **off the main
thread** over the statusline metrics in the agents snapshot; polled, not
pushed. The strip ends with the **usage cluster** (`_usage_image`): bare digits
over an under-meter exactly as wide as they are, unframed, still an `NSImage`
resolving every ink by hand from the appearance, matched to `labelColor`'s
alpha, aligned by **baseline**. **Colour is an exception signal**: neutral ink
to `menu_format.USAGE_WARN_PERCENT` (75%), amber to `USAGE_CRIT_PERCENT`
(90%), red above; nothing is colour-alone. A `stale` bar shows a bare `–` and
no meter. The same reading feeds the panel's footer chips.

`menu_format.py` is pure label formatting for the **strip's usage cluster**
and nothing else: `limit_percent`, `usage_text`, `usage_tier`,
`grok_usage_text`. The panel formats its own rows in Swift.

## The art the strip draws

The strip's PNG frames live under `assets/cast`, snapped to the source's
native pixel grid by `tools/pixelgrid_ingest.py`, fifteen slugs x three
states, one character per *category* (at most one face each; sleeping sessions
are not drawn); the menu-bar bake crops **one canvas per cycle**
(`menubar_cast_icons.cycle_box`), never per frame.

The panel's and phone's 512x512 stills under `assets/portraits` are a separate
tree, written and mirrored only by `tools/portrait_ingest.py`; never in
`host/setup.py`'s resource list.
