"""The menu-bar app's logic, exercised without AppKit: the strip's layout,
the login item, preferences and the rest of `BobCompanionApp` that can run
headless.
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from dark_army_daemon.daemon import BobDaemon


class CardCounts:
    """Records how many cards each notification push carried."""

    def __init__(self):
        self.lengths = []

    def on_notification_change(self, notifications: list) -> None:
        self.lengths.append(len(notifications))


@pytest.mark.asyncio
async def test_the_card_count_the_menu_bar_hears_rises_and_falls():
    heard = CardCounts()
    daemon = BobDaemon(observer=heard)
    for message in ({"event": "add", "session_id": "one", "project": "shop", "message": "ready"},
                    {"event": "add", "session_id": "two", "project": "shop", "message": "ready"},
                    {"event": "dismiss", "session_id": "one"},
                    {"event": "dismiss", "session_id": "two"}):
        await daemon._handle_message(message)
    assert heard.lengths == [1, 2, 1, 0]


@pytest.mark.parametrize("on_disk", [True, False])
def test_launch_at_login_is_on_exactly_when_its_plist_exists(tmp_path, monkeypatch, on_disk):
    from dark_army_menubar import launchd
    plist = tmp_path / "LaunchAgents" / "com.dark-army.menubar.plist"
    monkeypatch.setattr(launchd, "PLIST_PATH", plist)
    if on_disk:
        plist.parent.mkdir()
        plist.write_bytes(b"")
    assert launchd.is_enabled() is on_disk


import tempfile
from pathlib import Path
from dark_army_menubar.preferences import DEFAULTS, load_preferences, save_preferences


@pytest.mark.parametrize("contents", [None, "not json{{{", "[]"],
                         ids=["no-file", "damaged", "not-an-object"])
def test_preferences_nobody_can_read_are_the_defaults(tmp_path, contents):
    path = tmp_path / "preferences.json"
    if contents is not None:
        path.write_text(contents)
    assert load_preferences(path) == DEFAULTS


def test_a_saved_preference_reads_back_from_a_new_folder(tmp_path):
    path = tmp_path / "nested" / "preferences.json"
    save_preferences(path=path, updates={"notification_sound": False})
    assert load_preferences(path)["notification_sound"] is False


# --- Menu-bar strip composition (pure layout logic) ---

def _compose(working, idle, attention, subagents, level=0):
    from dark_army_menubar.app import BobCompanionApp
    return BobCompanionApp._compose_strip(working, idle, attention, subagents, level)


def test_strip_shows_working_and_attention_in_order():
    groups = _compose(working=3, idle=5, attention=1, subagents=0)
    assert groups == [("work", "3", ""), ("attn", "1", "")]


def test_strip_never_shows_idle_sessions():
    """Sleeping sessions used to sit between working and needs-you, and their
    faces were what pushed the backlog count off the bar. They are omitted at
    every rung; an idle-only pond still gets the resting frog so the item is
    not empty."""
    from dark_army_menubar.app import STRIP_LADDER
    for level in range(len(STRIP_LADDER)):
        assert _compose(working=0, idle=4, attention=0, subagents=0,
                        level=level) == [("idle", "", "")]
        groups = _compose(working=2, idle=4, attention=1, subagents=0,
                          level=level)
        assert groups == [("work", "2", ""), ("attn", "1", "")]
        assert not any(g[0] == "idle" or str(g[0]).endswith(":idle")
                       for g in groups)


def test_strip_working_carries_subagents_as_a_separate_run():
    """The suffix is its own field, not "3 (+2)" in the count: it is set smaller,
    dimmer and raised, and the collapse ladder drops it without the count."""
    groups = _compose(working=1, idle=0, attention=0, subagents=2)
    assert groups == [("work", "1", "+2")]


def test_strip_hides_empty_categories():
    # Only working + attention present → idle is omitted.
    groups = _compose(working=2, idle=0, attention=1, subagents=0)
    assert groups == [("work", "2", ""), ("attn", "1", "")]


def test_strip_only_notification():
    groups = _compose(working=0, idle=0, attention=1, subagents=0)
    assert groups == [("attn", "1", "")]


def test_strip_nothing_active_shows_resting_idle():
    # No sessions at all → a lone resting idle frog with no number.
    groups = _compose(working=0, idle=0, attention=0, subagents=0)
    assert groups == [("idle", "", "")]


# --- The collapse ladder ------------------------------------------------------
# macOS never tells a status item that its neighbours are crowding it, so the
# strip measures itself and steps down. What each rung gives up is the point.

def test_the_ladder_gives_up_the_todo_count_last():
    """The board's to-do count is why the strip was narrowed: a face each had
    been crowding it off, so it must survive every concession except the floor.
    The red attention count is not a ladder flag at all and cannot be dropped.

    Pinned by *rung order*, by name. The positions used to be pinned too, and a
    dummy column was kept alive for years so those numbers would not move. Names
    are the pin now: a reordered or renamed field fails `_fields` below, and a
    reordered rung fails the concession sequence."""
    from dark_army_menubar.app import STRIP_LADDER, StripRung
    assert StripRung._fields == ("suffix", "usage", "todo")
    assert len(STRIP_LADDER) == 4
    assert [rung.todo for rung in STRIP_LADDER] == [True, True, True, False]
    # The first step down takes *exactly* the subagent suffix and nothing else.
    assert STRIP_LADDER[0].suffix is True and STRIP_LADDER[1].suffix is False
    assert STRIP_LADDER[0]._replace(suffix=False) == STRIP_LADDER[1]
    # The usage stack goes whole, after the footnote and before the to-do card.
    assert [rung.usage for rung in STRIP_LADDER] == [True, True, False, False]


def _bare_app():
    from dark_army_menubar.app import BobCompanionApp
    return object.__new__(BobCompanionApp)


def test_on_board_change_reads_backlog():
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app()
    BobCompanionApp.on_board_change(app, {"counts": {"backlog": 3}})
    assert app._todo_count == 3


def test_on_board_change_falls_back_to_ready():
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app()
    BobCompanionApp.on_board_change(app, {"counts": {"ready": 4}})
    assert app._todo_count == 4


def test_on_board_change_prefers_backlog_over_ready():
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app()
    BobCompanionApp.on_board_change(app, {"counts": {"backlog": 2, "ready": 9}})
    assert app._todo_count == 2


def test_on_board_change_zero_backlog_is_not_missing():
    """`or 0` would treat a real zero as missing and fall through to ready."""
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app()
    BobCompanionApp.on_board_change(app, {"counts": {"backlog": 0, "ready": 9}})
    assert app._todo_count == 0


def test_on_board_change_sums_backlog_and_prep():
    """A card waiting to be refined is still on the user's plate, so the bar's
    to-do figure is Prep + Backlog."""
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app()
    BobCompanionApp.on_board_change(app, {"counts": {"backlog": 3, "prep": 2}})
    assert app._todo_count == 5


def test_on_board_change_tolerates_a_daemon_with_no_prep():
    """A daemon one generation behind sends no `prep` key at all; the sum
    degrades to the backlog figure rather than blanking."""
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app()
    BobCompanionApp.on_board_change(app, {"counts": {"backlog": 3}})
    assert app._todo_count == 3


def test_on_board_change_junk_prep_does_not_zero_backlog():
    """Each term is parsed on its own — one junk value must not zero the
    other."""
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app()
    BobCompanionApp.on_board_change(
        app, {"counts": {"backlog": 3, "prep": "nope"}})
    assert app._todo_count == 3


@pytest.mark.parametrize("payload", [
    {},
    {"counts": None},
    {"counts": {"backlog": None}},
    {"counts": {"backlog": "nope"}},
    None,
])
def test_on_board_change_junk_is_zero(payload):
    from dark_army_menubar.app import BobCompanionApp
    app = _bare_app()
    BobCompanionApp.on_board_change(app, payload)
    assert app._todo_count == 0


def test_the_attention_count_survives_every_rung():
    """There is no rung on which the number of agents waiting on a human is
    dropped. That is the whole reason the to-do count is given up first."""
    for level in range(len(_ladder())):
        groups = _compose(working=4, idle=3, attention=2, subagents=6,
                          level=level)
        assert ("attn", "2", "") in groups, level


def _ladder():
    from dark_army_menubar.app import STRIP_LADDER
    return STRIP_LADDER


def test_the_ladder_drops_the_subagent_footnote_before_any_count():
    """The first concession that costs a *number* takes the annotation — every
    count is still there. Idle is not a count on this strip."""
    full = _compose(working=4, idle=3, attention=1, subagents=6, level=0)
    rung = _compose(working=4, idle=3, attention=1, subagents=6, level=1)
    assert full[0] == ("work", "4", "+6")
    assert rung == [("work", "4", ""), ("attn", "1", "")]


def test_the_floor_still_shows_something():
    """Even squeezed to the last rung, an idle-only strip keeps its resting frog —
    a status item that renders to nothing looks like a crash."""
    from dark_army_menubar.app import STRIP_LADDER
    last = len(STRIP_LADDER) - 1
    assert _compose(working=0, idle=2, attention=0, subagents=0, level=last) == [("idle", "", "")]
    assert _compose(working=9, idle=9, attention=9, subagents=9, level=last) == [
        ("work", "9", ""), ("attn", "9", "")]


def test_every_rung_only_ever_takes_away():
    """A rung that restores something would make the width search oscillate."""
    from dark_army_menubar.app import STRIP_LADDER
    for field in STRIP_LADDER[0]._fields:
        kept = [getattr(rung, field) for rung in STRIP_LADDER]
        assert kept == sorted(kept, reverse=True), (field, kept)
    assert STRIP_LADDER[0] == (True,) * len(STRIP_LADDER[0])
    assert STRIP_LADDER[-1].usage is False, (
        "the last rung must give up the usage stack")


# --- the usage stack is given up whole -----------------------------------------
# Its rows share one track width, so dropping a provider buys height and no
# width; the ladder gives the whole stack up in one rung.


def _three_limits(instance):
    """Readings whose three labels are all non-empty."""
    instance._limits = {"bars": [{"kind": "session", "percent": 9.0}]}
    instance._grok_limits = {"percent": 47.0}
    instance._codex_limits = {"bars": [{"percent": 29.0}]}


def _strip_instance():
    """The hand-built instance the strip tests use — no NSStatusItem needed."""
    from unittest.mock import MagicMock
    from dark_army_menubar import app as A

    instance = object.__new__(A.BobCompanionApp)
    for attr, value in (("_usage_cache", {}), ("_frame_cache", {}),
                        ("_todo_cache", {}), ("_fonts", None),
                        ("_strip_sig", None), ("_strip_width", None),
                        ("_anim_i", 0), ("_limits", None),
                        ("_grok_limits", {}), ("_codex_limits", {}),
                        ("_todo_count", 0)):
        setattr(instance, attr, value)
    button = MagicMock()
    button.image.return_value = None
    instance._status_button = lambda: button
    return instance, button


def test_animate_icon_passes_the_rungs_usage_flag():
    """The rung's own usage flag reaches the render, at the rung it settled on.

    The rung is read back off the ladder rather than written into the test as a
    number: pinning a level by hand is what tied these assertions to a column
    that existed only to keep them numbered."""
    from unittest.mock import MagicMock
    from dark_army_menubar.app import (
        BobCompanionApp, STRIP_BUDGET_PT, STRIP_LADDER)

    app = object.__new__(BobCompanionApp)
    app._daemon_thread = MagicMock(is_alive=lambda: True)
    app._working_count = 2
    app._idle_count = 1
    app._attention_count = 0
    app._subagent_count = 0
    app._limits = None
    app._anim_i = 0
    app._strip_key = None
    app._strip_level = 0
    app._compose_strip = MagicMock(return_value=[("work", "2", "")])
    # Too wide until the ladder has given up two things, so it settles part way
    # down rather than at the top.
    over, under = STRIP_BUDGET_PT + 10.0, STRIP_BUDGET_PT - 10.0
    app._render_strip = MagicMock(side_effect=[over, over, under])

    BobCompanionApp._animate_icon(app, None)     # counts changed -> searches
    assert app._strip_level == 2
    app._render_strip.reset_mock()
    app._render_strip.side_effect = None
    app._render_strip.return_value = under
    BobCompanionApp._animate_icon(app, None)     # same counts -> no search

    settled = STRIP_LADDER[app._strip_level]
    kwargs = app._render_strip.call_args.kwargs
    assert kwargs["show_usage"] is settled.usage is False
    assert kwargs["show_todo"] is settled.todo is True


def test_the_full_reading_fits_with_room_to_spare():
    """The success criterion. Two faces with counts and a footnote, 132 cards to
    do and three providers: ~303pt as three usage clusters and a card-stack
    mark, about 190 as one stack and a number printed on a card. Measured on a
    real attributed string, not assumed."""
    instance, _button = _strip_instance()
    _three_limits(instance)
    instance._todo_count = 132
    width = instance._render_strip([("work", "2", "+1"), ("attn", "1", "")],
                                   measure=True)
    assert width <= 205.0, width


def test_shedding_the_usage_stack_narrows_the_strip():
    """The usage rung has to reach the rendered run, not just the flag."""
    def width(show_usage):
        instance, button = _strip_instance()
        _three_limits(instance)
        instance._render_strip([("work", "2", "")], show_usage=show_usage,
                               measure=True)
        return button.setAttributedTitle_.call_args[0][0].size().width

    assert width(True) > width(False) + 30.0


def test_the_strip_speaks_its_readings():
    """The stack has no digits, so the words are where the percentages are
    read — on the tooltip and on the accessibility label, which replaces the
    title for VoiceOver and so carries the counts too. Set once per change:
    a tooltip written every tick dirties the button five times a second."""
    instance, button = _strip_instance()
    _three_limits(instance)
    instance._todo_count = 132
    groups = [("work", "2", "+1"), ("attn", "1", "")]
    instance._render_strip(groups, measure=True)
    assert button.setToolTip_.call_count == 1
    assert button.setAccessibilityLabel_.call_count == 1
    for call in (button.setToolTip_.call_args, button.setAccessibilityLabel_.call_args):
        words = call[0][0]
        assert "Claude 9% · Grok 47% · Codex 29%" in words, words
        assert "132 cards to do" in words, words
        assert "2 working" in words and "1 needs you" in words, words

    instance._render_strip(groups, measure=True)          # same inputs
    instance._strip_sig = None                            # even on a repaint
    instance._render_strip(groups, measure=True)
    assert button.setToolTip_.call_count == 1
    assert button.setAccessibilityLabel_.call_count == 1

    instance._grok_limits = {"percent": 47.0, "stale": True}
    instance._render_strip(groups, measure=True)
    assert button.setToolTip_.call_count == 2
    assert "Grok: window reset" in button.setToolTip_.call_args[0][0]
    assert "Grok: window reset" in button.setAccessibilityLabel_.call_args[0][0]


def test_the_strip_words_drop_what_the_strip_drops():
    """To-do 0 draws no card and says nothing about cards; offline draws no
    stack and no card and says only that it is offline."""
    instance, button = _strip_instance()
    _three_limits(instance)
    instance._todo_count = 0
    instance._render_strip([("work", "2", "")], measure=True)
    assert "to do" not in button.setToolTip_.call_args[0][0]

    instance, button = _strip_instance()
    _three_limits(instance)
    instance._todo_count = 5
    stacks, cards = [], []
    instance._usage_stack_image = lambda rows, dark: stacks.append(rows)
    instance._todo_image = lambda count, dark: cards.append(count)
    instance._render_strip([("off", "", "")], measure=True)
    assert stacks == [] and cards == []
    assert button.setToolTip_.call_args[0][0] == "Dark Army is offline"


def test_a_collapsed_strip_still_speaks_every_usage_reading():
    """The ladder's usage rung drops the stack from the strip, not from the
    words: they are the only place the percentages are read, and a tooltip
    is not width-bound."""
    instance, button = _strip_instance()
    _three_limits(instance)
    stacks = []
    instance._usage_stack_image = lambda rows, dark: stacks.append(rows)
    instance._render_strip([("work", "2", "")], show_usage=False, measure=True)
    assert stacks == []
    words = button.setAccessibilityLabel_.call_args[0][0]
    assert "Claude" in words and "Grok" in words and "Codex" in words
    assert words == button.setToolTip_.call_args[0][0]


def test_the_stack_rows_follow_the_labels_in_a_fixed_order():
    """Claude, Grok, Codex top to bottom; a stale Grok between two fresh rows
    is a None row; a Codex with no reading draws no row."""
    from dark_army_menubar import app as A

    instance, _button = _strip_instance()
    _three_limits(instance)
    instance._grok_limits = {"percent": 47.0, "stale": True}
    instance._codex_limits = {}
    seen = []
    real = instance._usage_stack_image

    def spy(rows, dark):
        seen.append(rows)
        return real(rows, dark)

    instance._usage_stack_image = spy
    instance._render_strip([("work", "2", "")], measure=True)
    assert seen == [(("claude", 9.0), ("grok", None))]
    assert A.GAP_USAGE == 14.0 and A.GAP_TODO == 12.0


def test_the_floor_is_faces_counts_and_nothing_else():
    """The last rung did not silently gain a column."""
    from dark_army_menubar.app import STRIP_LADDER

    assert STRIP_LADDER[-1].usage is False
    assert STRIP_LADDER[-1].todo is False


def test_strip_frame_keys_exist():
    from dark_army_menubar.app import ICON_STRIP_FRAMES, ICON_IDLE_FRAMES
    for key in ("work", "attn"):
        assert ICON_STRIP_FRAMES[key], key
        assert all(isinstance(f, str) for f in ICON_STRIP_FRAMES[key])
    # idle has per-appearance (light/dark) variants of equal length
    assert ICON_IDLE_FRAMES["light"] and ICON_IDLE_FRAMES["dark"]
    assert len(ICON_IDLE_FRAMES["light"]) == len(ICON_IDLE_FRAMES["dark"])


def test_frames_for_idle_picks_variant_by_appearance():
    """The sleeping face holds one frame, and *which* one still depends on the
    bar's appearance — a hold that collapsed the two variants would put a black
    Zzz on a black menu bar."""
    import dark_army_menubar.app as appmod
    assert appmod.BobCompanionApp._frames_for("idle", dark=True) == [
        appmod.ICON_IDLE_FRAMES["dark"][-1]
    ]
    assert appmod.BobCompanionApp._frames_for("idle", dark=False) == [
        appmod.ICON_IDLE_FRAMES["light"][-1]
    ]
    assert (appmod.BobCompanionApp._frames_for("idle", dark=True)
            != appmod.BobCompanionApp._frames_for("idle", dark=False))
    # non-idle categories ignore appearance, and working still animates whole
    assert appmod.BobCompanionApp._frames_for("work", dark=True) == appmod.ICON_STRIP_FRAMES["work"]
    assert len(appmod.BobCompanionApp._frames_for("work", dark=True)) == 8


def test_the_waiting_face_holds_the_frame_that_carries_its_red():
    """`attn` freezes on slot 0 because that is where the red alert is drawn.
    Later slots of the cycle carry none, so holding the last frame would leave
    "needs you" carried by the count alone."""
    import dark_army_menubar.app as appmod
    assert appmod.BobCompanionApp._frames_for("attn") == [appmod.ICON_STRIP_FRAMES["attn"][0]]

    Image = pytest.importorskip("PIL.Image", reason="Pillow not installed")
    from pathlib import Path
    icons = Path(appmod.__file__).parent / "icons"

    def red_pixels(name):
        with Image.open(icons / name) as im:
            data = list(im.convert("RGBA").getdata())
        return sum(1 for px in data
                   if px[3] > 128 and px[0] > 150 and px[0] - px[1] > 60 and px[0] - px[2] > 60)

    assert red_pixels("dark-army-attn-0.png") > 0
    assert red_pixels("dark-army-attn-2.png") == 0


def test_the_cast_faces_freeze_with_the_aggregate_ones():
    """One table freezes both paths: a named agent's own face holds the same
    slot its aggregate glyph does."""
    import dark_army_menubar.app as appmod
    with patch.object(appmod, "_icon_exists", lambda name: True):
        assert appmod.BobCompanionApp._frames_for("cast:cipher:attn") == [
            "cast-cipher-attn-light-0"
        ]
        assert appmod.BobCompanionApp._frames_for("cast:cipher:idle", dark=True) == [
            "cast-cipher-idle-dark-3"
        ]
        assert len(appmod.BobCompanionApp._frames_for("cast:cipher:work")) == 8


def test_a_quiet_strip_stops_repainting_altogether():
    """With the real `_frames_for`, a machine with nothing running composes an
    identical strip every tick — so the signature skip means the status item is
    told to redraw exactly once, however long the timer runs."""
    from unittest.mock import MagicMock
    from dark_army_menubar.app import BobCompanionApp

    # Real fonts: a group whose count is empty ends its run on the attachment,
    # and the spacer that follows measures itself against the count font.
    NSFont = pytest.importorskip("AppKit", reason="PyObjC not installed").NSFont
    font = NSFont.menuBarFontOfSize_(0)

    app = object.__new__(BobCompanionApp)
    app._anim_i = 0
    app._limits = None
    app._fonts = (font, font, font)
    button = MagicMock()
    button.image.return_value = None
    app._status_button = MagicMock(return_value=button)
    image = MagicMock()
    image.size.return_value = MagicMock(width=22.0, height=22.0)
    app._frame_image = MagicMock(return_value=image)

    for groups in ([("idle", "", "")], [("attn", "1", "")]):
        app._strip_sig = None
        app._strip_width = None
        button.setAttributedTitle_.reset_mock()
        for _ in range(10):
            BobCompanionApp._render_strip(app, groups, measure=False)
            app._anim_i += 1
        assert button.setAttributedTitle_.call_count == 1, groups


def test_the_held_frames_are_on_disk():
    """`_frame_image` resolves by name at render time and a missing file fails
    silently as a blank menu bar, so the frames the hold names have to exist."""
    import dark_army_menubar.app as appmod
    from pathlib import Path
    icons = Path(appmod.__file__).parent / "icons"

    for path in icons.glob("cast-*-idle-light-0.png"):
        assert (icons / path.name.replace("-0.png", "-3.png")).is_file(), path.name
    for path in icons.glob("cast-*-idle-dark-0.png"):
        assert (icons / path.name.replace("-0.png", "-3.png")).is_file(), path.name
    # The waiting hold is slot 0 in *both* appearances, and `_frames_for` picks
    # the dark one whenever the bar is dark, so each has to name the other's
    # sibling. Globbing a name and asserting that name exists proves nothing.
    for path in icons.glob("cast-*-attn-light-0.png"):
        assert (icons / path.name.replace("-light-0.png", "-dark-0.png")).is_file(), path.name
    for path in icons.glob("cast-*-attn-dark-0.png"):
        assert (icons / path.name.replace("-dark-0.png", "-light-0.png")).is_file(), path.name

    for name in ("dark-army-idle-light-3.png", "dark-army-idle-dark-3.png", "dark-army-attn-0.png"):
        assert (icons / name).is_file(), name


# --- Menu-bar strip: one rendering path only ---------------------------------

def test_offline_glyph_has_frames_like_any_other_category():
    """Offline is a strip category, not a button image. It used to be set via
    self.icon while the strip lived in the attributed title — and a status button
    carries both at once, so the stale strip stayed visible under the offline
    icon (two frogs, no counts)."""
    import dark_army_menubar.app as appmod
    assert appmod.BobCompanionApp._frames_for("off") == [appmod.ICON_DISCONNECTED["light"]]


def test_offline_glyph_picks_variant_by_appearance():
    """The glyph is pure black ink. Under rumps' template mode AppKit tinted it
    for a dark bar; strip frames are NSTextAttachments and are never tinted, so
    without a white variant the offline state is invisible in dark mode — the one
    state the user most needs to see."""
    import dark_army_menubar.app as appmod
    light = appmod.BobCompanionApp._frames_for("off", dark=False)
    dark = appmod.BobCompanionApp._frames_for("off", dark=True)
    assert light == [appmod.ICON_DISCONNECTED["light"]]
    assert dark == [appmod.ICON_DISCONNECTED["dark"]]
    assert light != dark, "offline glyph must not reuse one ink colour for both bars"


def test_offline_glyph_assets_exist():
    """Both variants must be on disk — _frame_image resolves them by name at
    render time, so a missing file fails silently as a blank menu bar."""
    from pathlib import Path
    import dark_army_menubar.app as appmod

    icons = Path(appmod.__file__).parent / "icons"
    for variant, name in appmod.ICON_DISCONNECTED.items():
        assert (icons / f"{name}.png").is_file(), f"missing {variant} offline glyph: {name}.png"


def test_no_button_image_assignments_remain():
    """Regression guard for the double-icon bug: nothing may assign self.icon or
    self.template, because that paints an image the strip never clears."""
    import inspect
    import re
    from dark_army_menubar import app as app_mod

    src = inspect.getsource(app_mod)
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.search(r"^\s*self\.(icon|template)\s*=", line)
    ]
    assert not offenders, f"button-image assignment reintroduced: {offenders}"


def test_disconnected_state_renders_through_the_strip():
    """_animate_icon must go through _render_strip when offline, so exactly one
    mechanism ever paints the button."""
    from unittest.mock import MagicMock
    from dark_army_menubar.app import BobCompanionApp

    app = object.__new__(BobCompanionApp)
    # `_daemon_alive` is a read-only property; drive it via what it reads.
    # No _daemon_thread at all -> _daemon_alive False.
    app._render_strip = MagicMock()

    BobCompanionApp._animate_icon(app, None)

    # Offline never measures: the glyph is a single frame, so the ladder has
    # nothing to search and strip.size() would lay out a run nobody consults.
    app._render_strip.assert_called_once_with([("off", "", "")], measure=False)


def test_steady_strip_does_not_re_measure_every_tick():
    """`strip.size()` lays out the whole run, and only the collapse ladder reads
    it. On a tick where the counts held still there is nothing to search, so the
    render must not ask to be measured."""
    from unittest.mock import MagicMock
    from dark_army_menubar.app import BobCompanionApp, STRIP_LADDER

    app = object.__new__(BobCompanionApp)
    app._daemon_thread = MagicMock(is_alive=lambda: True)
    app._working_count = 2
    app._idle_count = 1
    app._attention_count = 0
    app._subagent_count = 0
    app._limits = None
    app._anim_i = 0
    app._strip_key = None
    app._strip_level = 0
    app._compose_strip = MagicMock(return_value=[("work", "2", "")])
    app._render_strip = MagicMock(return_value=10.0)

    BobCompanionApp._animate_icon(app, None)     # counts changed -> searches
    assert app._render_strip.call_args.kwargs.get("measure") is not False

    app._render_strip.reset_mock()
    BobCompanionApp._animate_icon(app, None)     # same counts -> no search
    assert app._render_strip.call_args.kwargs["measure"] is False
    assert app._render_strip.call_count == 1


def test_render_strip_skips_a_repaint_that_would_change_nothing():
    """The offline glyph has one frame, so `_anim_i` advancing does not change
    what is on screen. Rebuilding the attributed string anyway allocated a dozen
    ObjC objects and dirtied the status item five times a second."""
    from unittest.mock import MagicMock
    from dark_army_menubar.app import BobCompanionApp

    app = object.__new__(BobCompanionApp)
    app._anim_i = 0
    app._limits = None
    app._strip_sig = None
    app._strip_width = None
    app._fonts = (MagicMock(), MagicMock(), MagicMock())
    button = MagicMock()
    button.image.return_value = None
    app._status_button = MagicMock(return_value=button)
    app._frames_for = MagicMock(return_value=["off.png"])

    # A real NSImage answers size() with numbers, and _render_strip does
    # arithmetic on them to decide whether the cached image needs re-sizing.
    image = MagicMock()
    image.size.return_value = MagicMock(width=22.0, height=22.0)
    app._frame_image = MagicMock(return_value=image)

    groups = [("off", "", "")]
    BobCompanionApp._render_strip(app, groups, measure=False)
    first = button.setAttributedTitle_.call_count
    assert first == 1

    app._anim_i += 1                              # the timer ticked...
    BobCompanionApp._render_strip(app, groups, measure=False)
    assert button.setAttributedTitle_.call_count == first   # ...nothing repainted


# --- Rate-limit windows: the 5H chip on the strip -----------------------------

def _bars(*bars):
    return {"bars": list(bars), "available": True}


def test_the_strip_reads_the_five_hour_window_as_bare_digits():
    """Just the number. "5H: 25%" spent an abbreviation, a colon and a space
    restating what the Usage section says in full."""
    from dark_army_menubar import menu_format as mf
    snap = _bars({"kind": "session", "percent": 25.4, "stale": False},
                 {"kind": "weekly_all", "percent": 61.0, "stale": False})
    assert mf.usage_text(snap) == "25%"


def test_no_usage_cluster_when_nothing_has_reported():
    from dark_army_menubar import menu_format as mf
    assert mf.usage_text({}) == ""
    assert mf.usage_text(_bars({"kind": "weekly_all", "percent": 61.0})) == ""


def test_a_stale_window_holds_its_slot_with_a_dash():
    """The window that percentage measured has already reset, so the real figure
    restarted at zero — showing the old one would be worse than showing none. But
    vanishing entirely is indistinguishable from a broken feature, so the slot
    stays and promises nothing."""
    from dark_army_menubar import menu_format as mf
    snap = _bars({"kind": "session", "percent": 88.0, "stale": True})
    assert mf.usage_text(snap) == mf.USAGE_UNKNOWN
    assert mf.usage_text(snap) != ""


def test_usage_tiers():
    from dark_army_menubar import menu_format as mf
    assert mf.usage_tier(0.0) == "ok"
    assert mf.usage_tier(mf.USAGE_WARN_PERCENT - 0.1) == "ok"
    assert mf.usage_tier(mf.USAGE_WARN_PERCENT) == "warn"
    assert mf.usage_tier(mf.USAGE_CRIT_PERCENT - 0.1) == "warn"
    assert mf.usage_tier(mf.USAGE_CRIT_PERCENT) == "crit"
    assert mf.usage_tier(None) == "stale"


def _usage_app():
    from dark_army_menubar.app import BobCompanionApp
    app = BobCompanionApp.__new__(BobCompanionApp)
    app._usage_cache = {}
    app._todo_cache = {}
    app._fonts = None
    return app


def _pixel(img, x_pt: float, y_from_top_pt: float):
    """The colour at a point of a drawn image, as (r, g, b, a).

    Bitmap reps are top-left origin and, on a Retina machine, backed at 2x — so
    sample in device pixels, converted from the point geometry it is drawn in."""
    from AppKit import NSBitmapImageRep

    rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    scale = rep.pixelsHigh() / img.size().height
    color = rep.colorAtX_y_(int(x_pt * scale), int(y_from_top_pt * scale))
    return (color.redComponent(), color.greenComponent(), color.blueComponent(),
            color.alphaComponent())


def _fill_start_x():
    """A point a little inside the start of a row's meter."""
    from dark_army_menubar.app import USAGE_STACK_MARK_PT, USAGE_STACK_MARK_GAP
    return USAGE_STACK_MARK_PT + USAGE_STACK_MARK_GAP + 2.0


def _usage_bar_color(percent, dark: bool = False):
    """The colour of a one-row stack's fill at `percent`, sampled from the
    drawing.

    Rendered rather than asserted on a constant: the thresholds matter only if
    they reach the pixels, and the colour is chosen inside the draw."""
    img, _ = _usage_app()._usage_stack_image((("claude", percent),), dark)
    r, g, b, _a = _pixel(img, _fill_start_x(), img.size().height / 2.0)
    return r, g, b


def test_a_healthy_budget_is_monochrome():
    """The old chip was green from 0% to 74%. Colour that is present whenever
    nothing is wrong has nothing left to say when something is — and the strip's
    genuinely urgent number, the attention count, had none at all."""
    from dark_army_menubar import menu_format as mf
    r, g, b = _usage_bar_color(mf.USAGE_WARN_PERCENT - 1)
    assert abs(r - g) < 0.02 and abs(g - b) < 0.02, (r, g, b)


def test_the_meter_turns_amber_at_the_warning_mark():
    from dark_army_menubar import menu_format as mf
    r, g, b = _usage_bar_color(mf.USAGE_WARN_PERCENT)
    assert r > 0.8 and b < 0.3 and g > b, (r, g, b)


def test_the_meter_turns_red_at_the_critical_mark():
    from dark_army_menubar import menu_format as mf
    r, g, b = _usage_bar_color(mf.USAGE_CRIT_PERCENT)
    assert r > 0.8 and g < 0.4 and r > g, (r, g, b)


def test_a_full_window_fills_the_track_in_red():
    """100%: the fill reaches the end of the track, and it is red there."""
    from dark_army_menubar.app import (
        USAGE_STACK_MARK_PT, USAGE_STACK_MARK_GAP, USAGE_STACK_TRACK_PT)
    img, _ = _usage_app()._usage_stack_image((("claude", 100.0),), False)
    end = USAGE_STACK_MARK_PT + USAGE_STACK_MARK_GAP + USAGE_STACK_TRACK_PT - 1.5
    r, g, b, _a = _pixel(img, end, img.size().height / 2.0)
    assert r > 0.8 and g < 0.4, (r, g, b)


def test_the_usage_stack_fits_the_menu_bar():
    """22pt is all there is; a taller image is scaled down or clipped. Three 6pt
    rows with the mockup's 2.5pt gaps came to 23pt — the row gap is 1pt."""
    img, _ = _usage_app()._usage_stack_image(
        (("claude", 7.0), ("grok", 70.0), ("codex", 95.0)), False)
    assert img.size().height <= 20.0
    assert 36.0 <= img.size().width <= 38.0


def test_a_missing_provider_shrinks_the_stack():
    """A provider with no reading takes its row away: the stack gets shorter,
    never narrower — every row shares one track width."""
    app_ = _usage_app()
    three, _ = app_._usage_stack_image(
        (("claude", 7.0), ("grok", 70.0), ("codex", 95.0)), False)
    two, _ = app_._usage_stack_image((("claude", 7.0), ("grok", 70.0)), False)
    assert two.size().height < three.size().height
    assert two.size().width == three.size().width


def test_a_stale_row_draws_its_mark_and_an_empty_track():
    """A reset window holds its row and promises nothing: the mark over an
    empty track, as the bare dash did before. No fill, so no tier hue."""
    app_ = _usage_app()
    stale, _ = app_._usage_stack_image((("grok", None),), False)
    fresh, _ = app_._usage_stack_image((("grok", 51.0),), False)
    assert stale.size().height == fresh.size().height
    r, g, b, a = _pixel(stale, _fill_start_x(), stale.size().height / 2.0)
    assert abs(r - g) < 0.02 and abs(g - b) < 0.02, (r, g, b)
    fr, fg, fb, fa = _pixel(fresh, _fill_start_x(), fresh.size().height / 2.0)
    assert 0 < a < fa, (a, fa)                    # the track, lighter than a fill
    # The mark is still drawn.
    blank, _ = app_._usage_stack_image((("grok", 0.0),), False)
    assert stale.TIFFRepresentation() != blank.TIFFRepresentation()


def test_the_three_marks_differ():
    """Each row is led by its provider's mark, and at six points the three
    marks still have to tell the rows apart."""
    app_ = _usage_app()
    tiffs = [app_._usage_stack_image(((brand, 51.0),), False)[0].TIFFRepresentation()
             for brand in ("claude", "grok", "codex")]
    assert tiffs[0] != tiffs[1] != tiffs[2] and tiffs[0] != tiffs[2]


def test_the_stack_is_cached_per_rows_and_appearance():
    app = _usage_app()
    rows = (("claude", 8.0), ("grok", 30.0))
    first = app._usage_stack_image(rows, False)
    assert app._usage_stack_image(rows, False) is first
    assert app._usage_stack_image(rows, True) is not first                 # appearance
    assert app._usage_stack_image((("claude", 9.0), ("grok", 30.0)),
                                  False) is not first                      # a percent
    assert app._usage_stack_image(tuple(reversed(rows)), False) is not first  # order


def test_the_stack_cache_stays_bounded():
    """The key is three readings together, so a long-running app meets new
    keys forever; the cache must not grow with them."""
    from dark_army_menubar.app import USAGE_STACK_CACHE_MAX
    app = _usage_app()
    for pct in range(0, 101):
        app._usage_stack_image((("claude", float(pct)), ("grok", 100.0 - pct)), False)
        assert len(app._usage_cache) <= USAGE_STACK_CACHE_MAX


def test_crossing_a_tier_inside_one_rounded_percent_redraws():
    """74.6% and 75.2% both round to 75, but one is plain and one amber;
    89.6% and 90.2% both round to 90, but one is amber and one red. The
    cached image must follow the tier, not the rounded figure."""
    app = _usage_app()
    below, _ = app._usage_stack_image((("claude", 74.6),), False)
    above, _ = app._usage_stack_image((("claude", 75.2),), False)
    assert below is not above
    r, g, b, _a = _pixel(above, _fill_start_x(), above.size().height / 2.0)
    assert r > 0.8 and b < 0.3 and g > b, (r, g, b)            # amber

    app._usage_stack_image((("claude", 89.6),), False)
    red, _ = app._usage_stack_image((("claude", 90.2),), False)
    r, g, b, _a = _pixel(red, _fill_start_x(), red.size().height / 2.0)
    assert r > 0.8 and g < 0.4 and r > g, (r, g, b)            # red


def test_the_stack_baseline_centres_on_the_cap_height():
    """_render_strip sets the attachment at -baseline, so the stack's centre
    lands on the counts' cap-height centre, as the faces do."""
    img, baseline = _usage_app()._usage_stack_image(
        (("claude", 8.0), ("grok", 30.0), ("codex", 50.0)), False)
    assert 0 <= baseline < img.size().height


def test_the_todo_card_is_narrower_than_the_old_stack():
    img, baseline = _usage_app()._todo_image(132, False)
    assert img.size().width <= 26.0, img.size().width
    assert img.size().height <= 20.0
    assert 0 <= baseline < img.size().height


def test_the_todo_card_prints_the_number():
    app_ = _usage_app()
    one, _ = app_._todo_image(1, False)
    many, _ = app_._todo_image(132, False)
    many_dark, _ = app_._todo_image(132, True)
    assert one.TIFFRepresentation() != many.TIFFRepresentation()
    assert many.TIFFRepresentation() != many_dark.TIFFRepresentation()


def test_the_todo_card_front_is_not_washed():
    """The back card is filled even-odd with the front card cut out, so the
    front card's interior stays clear where the two overlap — a 45% wash there
    turns the number grey on a translucent bar. Sampled just under the front
    card's right outline at mid-height, beside the digit and inside the back
    card's rect."""
    from dark_army_menubar.app import TODO_CARD_OFFSET, TODO_CARD_STROKE
    img, _ = _usage_app()._todo_image(1, False)
    inset = 0.6                                  # the back card's inset
    card_w = img.size().width - TODO_CARD_OFFSET + inset
    card_h = img.size().height - TODO_CARD_OFFSET + inset
    x = card_w - TODO_CARD_STROKE - 1.0
    y_from_top = (TODO_CARD_OFFSET - inset) + card_h / 2.0
    assert x > TODO_CARD_OFFSET + inset          # inside the back card's rect
    assert _pixel(img, x, y_from_top)[3] < 0.05
    # And where the front card does not overlap the back one: filling the two
    # cards together even-odd inks exactly this strip.
    left = TODO_CARD_STROKE + 0.8
    assert left < TODO_CARD_OFFSET + inset       # outside the back card's rect
    assert _pixel(img, left, y_from_top)[3] < 0.05
    # ...and the back card's crescent is inked where it peeks out.
    assert _pixel(img, img.size().width - 1.5, 1.0)[3] > 0.2


def test_grok_usage_text_is_bare_digits_like_claudes():
    from dark_army_menubar import menu_format as mf
    # The leading "G" moved into a drawn provider mark: same width, nothing to
    # decode. What keeps the two clusters apart is now the glyph, not a letter.
    assert mf.grok_usage_text({"percent": 36.2}) == "36%"
    assert mf.grok_usage_text({"percent": 0}) == "0%"
    assert mf.grok_usage_text({}) == ""
    assert mf.grok_usage_text({"percent": 36.0, "stale": True}) == mf.USAGE_UNKNOWN


def test_codex_usage_text_uses_the_worst_live_window():
    from dark_army_menubar import menu_format as mf
    snap = {"bars": [
        {"kind": "codex_primary", "percent": 36.2, "stale": False},
        {"kind": "codex_secondary", "percent": 61.0, "stale": False},
    ]}
    assert mf.codex_usage_text(snap) == "61%"
    assert mf.codex_limit_percent(snap) == 61.0
    assert mf.codex_usage_text({}) == ""


# --- Installing the VS Code extension -----------------------------------------
# `code --list-extensions` (15s timeout) and `code --install-extension` (90s) are
# subprocesses, and both used to run inline on the AppKit main thread — one while
# the menu was being built, one in the click handler. Worst case froze the menu
# bar for the better part of two minutes.
#
# The debounce used to be "take the callback off the menu item". With no menu it
# is a flag, which the panel reads to grey its own row.

def test_strip_words_spells_every_reading():
    from dark_army_menubar import menu_format as mf
    words = mf.strip_words(
        [("work", "3", "+2"), ("cast:relay:attn", "1", "")], 132,
        [("claude", "7%"), ("grok", "70%"), ("codex", "95%")])
    assert words == ("3 working (+2 helpers) · 1 needs you · 132 cards to do · "
                     "Claude 7% · Grok 70% · Codex 95%")
    assert mf.strip_words([("attn", "2", "")], 1, []) == "2 need you · 1 card to do"
    assert mf.strip_words(
        [("work", "1", "")], 0,
        [("claude", "7%"), ("grok", mf.USAGE_UNKNOWN)]) == (
        "1 working · Claude 7% · Grok: window reset, no reading")


def test_strip_words_offline_says_only_offline():
    from dark_army_menubar import menu_format as mf
    assert mf.strip_words([("off", "", "")], 132,
                          [("claude", "7%")]) == "Dark Army is offline"


def test_strip_words_idle():
    from dark_army_menubar import menu_format as mf
    assert mf.strip_words([("idle", "", "")], 0, []) == "nothing working"
    assert mf.strip_words([("idle", "", "")], 4, [("codex", "12%")]) == (
        "nothing working · 4 cards to do · Codex 12%")
    assert [name for _brand, name in mf.PROVIDER_NAMES] == ["Claude", "Grok", "Codex"]


def _vscode_app():
    from dark_army_menubar.app import BobCompanionApp
    app = object.__new__(BobCompanionApp)
    app._vscode_installing = False
    app._settings = {"vscode_extension": False}
    app._push_panel_context = lambda: None
    return app


class _NoopThread:
    def start(self):
        pass


def test_install_click_does_not_run_code_on_the_calling_thread(monkeypatch):
    """The click handler must hand `code` to a worker, not run it inline."""
    from dark_army_menubar import app as app_mod

    threads = []
    monkeypatch.setattr(app_mod.threading, "Thread",
                        lambda **kw: threads.append(kw) or _NoopThread())
    called = []
    monkeypatch.setattr(app_mod.vscode_extension, "install_extension",
                        lambda: called.append("install") or (True, "ok"))

    app = _vscode_app()
    app._on_install_vscode_extension()

    # Nothing ran yet — it was handed to a thread — and the flag is up so a
    # second click cannot start a concurrent --force install.
    assert called == []
    assert threads and threads[0]["daemon"] is True
    assert app._vscode_installing is True


def test_a_second_click_while_installing_does_nothing(monkeypatch):
    from dark_army_menubar import app as app_mod

    threads = []
    monkeypatch.setattr(app_mod.threading, "Thread",
                        lambda **kw: threads.append(kw) or _NoopThread())
    app = _vscode_app()
    app._on_install_vscode_extension()
    app._on_install_vscode_extension()
    assert len(threads) == 1


def test_install_finished_clears_the_flag_and_reports(monkeypatch):
    from dark_army_menubar import app as app_mod
    alerts = []
    monkeypatch.setattr(app_mod.rumps, "alert",
                        lambda *a, **k: alerts.append(k.get("title", "")))

    app = _vscode_app()
    app._vscode_installing = True
    app._vscode_install_finished(True, "ok", True)

    assert app._settings["vscode_extension"] is True
    # Cleared: a flag left up would make the row uncallable for the session.
    assert app._vscode_installing is False
    assert alerts == ["VS Code Extension Installed"]


def test_install_finished_clears_the_flag_even_on_failure(monkeypatch):
    from dark_army_menubar import app as app_mod
    monkeypatch.setattr(app_mod.rumps, "alert", lambda *a, **k: None)

    app = _vscode_app()
    app._vscode_installing = True
    app._vscode_install_finished(False, "code not found", False)

    assert app._settings["vscode_extension"] is False
    assert app._vscode_installing is False


# ── the status button lookup ─────────────────────────────────────────────────
#
# `_render_strip` begins by asking for the button and returns immediately when
# there isn't one. That made a broken lookup completely silent: every tick threw
# or bailed, nothing was ever drawn, the title stayed empty — and macOS fills an
# empty title with the application's name. The whole strip was replaced by the
# app's own name and all 54 tests here still passed, because none of them
# ever asked for the button.

def _bare_app():
    """An instance without running rumps' __init__ — enough to call one method."""
    from dark_army_menubar.app import BobCompanionApp
    return object.__new__(BobCompanionApp)


def test_status_button_reads_the_status_item():
    instance = _bare_app()

    class _Item:
        def button(self):
            return "the-button"

    class _NSApp:
        nsstatusitem = _Item()

    instance._nsapp = _NSApp()
    assert instance._status_button() == "the-button"


def test_status_button_is_none_before_the_status_item_exists():
    """Called from a timer that starts before the status item does, so it must
    answer None rather than raise."""
    assert _bare_app()._status_button() is None


def test_only_one_status_button_definition():
    """A second definition later in the class silently overrides the real one —
    which is exactly how the strip broke."""
    import inspect
    from dark_army_menubar.app import BobCompanionApp
    source = inspect.getsource(BobCompanionApp)
    assert source.count("def _status_button") == 1


# ── the limits reader's source ───────────────────────────────────────────────

def test_live_statusline_metrics_returns_the_payloads():
    """The rate-limit windows are drawn from these. They used to be reached
    through the agents snapshot the daemon *pushes*, which fires on structural
    changes only — so after a restart with nothing starting or stopping, the
    reader had nothing, fell back to the on-disk cache, and drew a dash for a
    window that had already reset."""
    from dark_army_daemon.daemon import BobDaemon

    daemon = object.__new__(BobDaemon)
    daemon._session_metrics = {
        "s1": {"five_hour_pct": 12, "received_at": 100.0},
        "s2": {"five_hour_pct": 12, "received_at": 200.0},
        "s3": None,                       # a session that never reported
    }
    payloads = daemon.live_statusline_metrics()
    assert len(payloads) == 2
    assert all(isinstance(p, dict) for p in payloads)


def test_live_statusline_metrics_is_empty_when_nothing_reported():
    from dark_army_daemon.daemon import BobDaemon

    daemon = object.__new__(BobDaemon)
    daemon._session_metrics = {}
    assert daemon.live_statusline_metrics() == []


def test_pick_live_takes_the_freshest_of_those_payloads():
    """Newest, not highest: a session parked on a prompt keeps its last payload,
    and a max across those re-elects a percentage from a window that has reset."""
    from dark_army_daemon import limits

    picked = limits.pick_live([
        {"five_hour_pct": 80, "received_at": 100.0},
        {"five_hour_pct": 12, "received_at": 200.0},
    ])
    assert picked["five_hour_pct"] == 12


def test_a_grok_row_cannot_furnish_claudes_five_hour_bar():
    """Grok files its *weekly* percentage under `five_hour_pct`, with a Grok reset
    instant beside it. Gathered unfiltered, it wins the freshness election
    whenever no Claude row has stamped a `received_at` yet — and the "Current
    session" bar then reads a weekly figure that resets next week. This is what
    the panel's footer showed (38%, resetting the 21st) while its header, which
    groups by provider, showed the true 5h number (97%)."""
    from dark_army_daemon import limits

    rows = [
        {"provider": "grok",
         "metrics": {"five_hour_pct": 38.0, "five_hour_resets_at": 2_000_000.0}},
        {"provider": "claude",
         "metrics": {"five_hour_pct": 97.0, "five_hour_resets_at": 1_000_000.0,
                     "received_at": 500.0}},
    ]
    picked = limits.pick_live(limits.claude_payloads(rows))
    assert picked["five_hour_pct"] == 97.0
    assert picked["five_hour_resets_at"] == 1_000_000.0

    # ...and with the Grok row alone there is simply no bar, rather than a bar
    # about somebody else's account.
    assert limits.claude_payloads(rows[:1]) == []


def test_claude_payloads_treats_an_unlabelled_row_as_claudes():
    """Every Claude Code row predates the provider field; absence is not Grok."""
    from dark_army_daemon import limits

    rows = [{"metrics": {"five_hour_pct": 5.0}}, {"provider": "", "metrics": {}}]
    assert limits.claude_payloads(rows) == [{"five_hour_pct": 5.0}, {}]


def test_a_gap_after_an_icon_actually_widens_the_strip():
    """Kerning after an *attachment* does nothing — AppKit discards it.

    Measured: moving the gap from 4pt to 40pt between two icons with no counts
    changed the composed width by exactly 0.0pt, so the gap was being thrown away
    in silence. It hides whenever an icon is followed by a count, because the
    kern then lands on that text. The fix is a real spacer character; this test
    exists because the failure is invisible — the strip just looks cramped and
    nothing errors.
    """
    from unittest.mock import MagicMock
    from dark_army_menubar import app as A

    def width(gap):
        instance = object.__new__(A.BobCompanionApp)
        instance._usage_cache = {}
        instance._frame_cache = {}
        instance._fonts = None
        instance._strip_sig = None
        instance._strip_width = None
        instance._anim_i = 0
        instance._limits = None
        instance._grok_limits = {}
        button = MagicMock()
        button.image.return_value = None
        instance._status_button = lambda: button
        original = A.GAP_GROUP
        try:
            A.GAP_GROUP = gap
            instance._render_strip(
                [("work", "", ""), ("idle", "", "")],
                show_usage=False, measure=True)
        finally:
            A.GAP_GROUP = original
        return button.setAttributedTitle_.call_args[0][0].size().width

    narrow, wide = width(4.0), width(40.0)
    assert wide > narrow + 30.0, (narrow, wide)


# ── at most one face per live category ───────────────────────────────────────

def _faces(working=0, idle=0, attention=0, subagents=0, level=0, faces=None):
    from dark_army_menubar.app import BobCompanionApp
    return BobCompanionApp._compose_strip(working, idle, attention, subagents,
                                          level, faces)


def test_a_single_agent_wears_its_own_face_and_the_count():
    """One portrait is allowed; the digit still sits beside it, because a face
    is not a number you can add up at a glance."""
    groups = _faces(working=1, faces={"work": ["relay"]})
    assert groups == [("cast:relay:work", "1", "")]


def test_two_agents_fall_back_to_one_face_and_a_count():
    """MAX_FACES is 1: a second working agent is a roster, and a roster is
    what pushed the backlog off the bar."""
    from dark_army_menubar.app import MAX_FACES
    assert MAX_FACES["work"] == 1 and MAX_FACES["attn"] == 1
    assert MAX_FACES["idle"] == 0
    groups = _faces(working=2, faces={"work": ["relay", "forge"]})
    assert groups == [("work", "2", "")]


def test_the_subagent_footnote_lands_on_the_working_group():
    """It counts something the face does not show, so it is the one extra
    number that still has to be there."""
    groups = _faces(working=1, subagents=3, faces={"work": ["relay"]})
    assert groups == [("cast:relay:work", "1", "+3")]


def test_attention_gets_a_face_idle_does_not():
    groups = _faces(working=1, idle=1, attention=1,
                    faces={"work": ["relay"], "idle": ["forge"],
                           "attn": ["ledger"]})
    assert groups == [("cast:relay:work", "1", ""),
                      ("cast:ledger:attn", "1", "")]


def test_too_many_agents_falls_back_to_one_face_and_a_count():
    from dark_army_menubar.app import MAX_FACES
    many = ["relay"] * (MAX_FACES["work"] + 1)
    assert _faces(working=len(many), faces={"work": many}) == [
        ("work", str(len(many)), "")]


def test_faces_that_disagree_with_the_count_are_not_trusted():
    """The count came from the source of truth; the faces did not."""
    assert _faces(working=3, faces={"work": ["relay"]}) == [("work", "3", "")]


def test_no_faces_is_the_aggregate_strip():
    assert _faces(working=2, idle=1, attention=1) == [
        ("work", "2", ""), ("attn", "1", "")]


def test_every_drawn_face_is_baked_for_both_appearances():
    """Scoped to `assets/cast/manifest.json`, not `identity.NAMES`: a character
    nobody has drawn yet is legal (the strip falls back to the aggregate glyph)
    but a declared one with a missing bake is a broken pipeline."""
    import json
    from pathlib import Path
    from dark_army_menubar.app import _icon_exists
    manifest = Path(__file__).resolve().parents[2] / "assets" / "cast" / "manifest.json"
    if not manifest.is_file():
        pytest.skip("assets/cast is not present")
    for slug in json.loads(manifest.read_text())["cast"]:
        for category in ("work", "idle", "attn"):
            for variant in ("light", "dark"):
                assert _icon_exists(
                    f"cast-{slug}-{category}-{variant}-0"), (slug, category)


def test_strip_faces_wears_a_baked_face(monkeypatch):
    """The positive half of the fallback rule: one agent whose face *is*
    baked wears it. Pinned through a stubbed `_icon_exists` because the
    pixel-art tree may lawfully hold no drawing for the cast yet."""
    from dark_army_menubar import app as A
    monkeypatch.setattr(A, "_icon_exists",
                        lambda name: name == "cast-relay-work-light-0")
    instance = object.__new__(A.BobCompanionApp)
    instance._agents_snapshot = {
        "running": [{"nickname": "Relay"}],
        "sleeping": [],
        "waiting": [{"nickname": "Cipher"}],
    }
    faces = instance._strip_faces()
    assert faces["work"] == ["relay"]
    assert faces["attn"] == []                 # no bake → the aggregate glyph


def test_an_undrawn_cast_member_falls_back_to_the_aggregate_glyph():
    """The strip's graceful degradation under an incomplete pixel-art tree:
    a cast member with no baked icon draws the aggregate figure and the
    count rather than an empty gap."""
    from dark_army_menubar.app import BobCompanionApp
    instance = object.__new__(BobCompanionApp)
    instance._agents_snapshot = {
        "running": [{"nickname": "Nyx"}], "sleeping": [], "waiting": []}
    if instance._strip_faces()["work"]:
        pytest.skip("nyx has been drawn and baked")
    assert _faces(working=1, faces=instance._strip_faces()) == [("work", "1", "")]
    assert BobCompanionApp._frames_for("cast:nyx:work")[0] == "dark-army-work-0"


def test_strip_faces_empties_the_category_when_one_nickname_has_no_icon():
    """One overflow nickname must not leave the others wearing a face while
    it has none — `_strip_faces` empties the whole category on purpose."""
    from dark_army_menubar.app import BobCompanionApp
    instance = object.__new__(BobCompanionApp)
    instance._agents_snapshot = {
        "running": [
            {"nickname": "relay"},
            {"nickname": "vex-ab12"},
        ],
        "sleeping": [],
        "waiting": [],
    }
    assert instance._strip_faces()["work"] == []


@pytest.mark.parametrize("nickname", ["Androll", "Captcha", "Sawa", "Franio"])
def test_a_newcomer_with_no_pixel_art_still_counts(nickname):
    """The four newest names have photographs but no hand-drawn animation, and
    that is lawful: the strip drops the category back to its aggregate figure
    and keeps the **real** count, so the bar still says how many are working."""
    from dark_army_menubar.app import BobCompanionApp
    instance = object.__new__(BobCompanionApp)
    instance._agents_snapshot = {
        "running": [{"nickname": nickname}], "sleeping": [], "waiting": []}
    faces = instance._strip_faces()
    if faces["work"]:
        pytest.skip(f"{nickname} has been drawn and baked")
    assert _faces(working=1, faces=faces) == [("work", "1", "")]
    assert BobCompanionApp._frames_for(f"cast:{nickname.lower()}:work")[0] == "dark-army-work-0"


def test_newcomers_working_beside_a_drawn_face_still_add_up(monkeypatch):
    """The count is of agents, not of faces. Two working agents where only one
    of them is drawn empties the category and reports **2**, never 1."""
    from dark_army_menubar import app as A
    monkeypatch.setattr(A, "_icon_exists",
                        lambda name: name == "cast-relay-work-light-0")
    instance = object.__new__(A.BobCompanionApp)
    instance._agents_snapshot = {
        "running": [{"nickname": "Relay"}, {"nickname": "Sawa"}],
        "sleeping": [],
        "waiting": [{"nickname": "Franio"}],
    }
    faces = instance._strip_faces()
    assert faces == {"work": [], "attn": []}
    assert _faces(working=2, attention=1, faces=faces) == [
        ("work", "2", ""), ("attn", "1", "")]


def test_a_face_without_art_falls_back_rather_than_rendering_nothing():
    from dark_army_menubar.app import BobCompanionApp
    assert BobCompanionApp._frames_for("cast:nobody:work")[0] == "dark-army-work-0"


def test_the_footnote_renders_without_a_count():
    """`_render_strip` used to draw the suffix only inside `if count:`, which
    silently swallowed it the first time faces landed — the faces have no count.
    """
    from unittest.mock import MagicMock
    from dark_army_menubar import app as A

    def width(groups):
        instance = object.__new__(A.BobCompanionApp)
        for attr, value in (("_usage_cache", {}), ("_frame_cache", {}),
                            ("_fonts", None),
                            ("_strip_sig", None), ("_strip_width", None),
                            ("_anim_i", 0), ("_limits", None),
                            ("_grok_limits", {})):
            setattr(instance, attr, value)
        button = MagicMock()
        button.image.return_value = None
        instance._status_button = lambda: button
        instance._render_strip(groups, show_usage=False, measure=True)
        return button.setAttributedTitle_.call_args[0][0].size().width

    bare = width([("cast:relay:work", "", "")])
    noted = width([("cast:relay:work", "", "+3")])
    assert noted > bare + 5.0, (bare, noted)


# ── the panel's channel back ─────────────────────────────────────────────────
#
# The panel is the only surface, so this pipe carries every verb the app has:
# the three that act on the process, and every preference. A toggle sends the
# state it wants rather than asking the app to flip whatever it holds — the two
# can briefly disagree, and a flip resolves that the wrong way exactly when it
# matters.

def _action_app(**state):
    from dark_army_menubar import app as A
    instance = object.__new__(A.BobCompanionApp)
    instance._settings = {}
    instance._daemon = None
    instance._push_panel_context = lambda: None
    instance._dictation_hold = None
    instance._dictation_timer = None
    instance._dictation_deadline = None
    for key, value in state.items():
        setattr(instance, key, value)
    return instance


def _dispatch(instance, name, value=None):
    from dark_army_menubar import app as A
    original = A.callAfter          # would defer to an AppKit loop that is not running
    A.callAfter = lambda fn, *a: fn(*a)
    try:
        instance._on_panel_action(name, value)
    finally:
        A.callAfter = original


def test_panel_actions_are_dispatched():
    """Rebuild, restart and quit act on the *app*, not the daemon, so the panel
    cannot do them itself — it asks over its stdout."""
    called = []
    instance = _action_app(
        _on_rebuild=lambda _: called.append("rebuild"),
        _on_restart=lambda _: called.append("restart"),
        _on_quit=lambda _: called.append("quit"),
    )
    for name in ("rebuild", "restart", "quit_app"):
        _dispatch(instance, name)
    assert called == ["rebuild", "restart", "quit"]


def test_a_preference_action_carries_the_state_it_wants(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))

    instance = _action_app()
    instance._settings["notification_sound"] = True
    _dispatch(instance, "set_notification_sound", False)

    assert instance._settings["notification_sound"] is False
    assert saved == [{"notification_sound": False}]


def test_set_board_close_terminal_writes_the_preference_and_the_daemon_attribute(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))

    class _Daemon:
        def __init__(self):
            self.board_close_terminal_enabled = True

    daemon = _Daemon()
    instance = _action_app(_daemon=daemon)
    instance._settings["board_close_terminal"] = True
    _dispatch(instance, "set_board_close_terminal", False)

    assert instance._settings["board_close_terminal"] is False
    assert saved == [{"board_close_terminal": False}]
    assert daemon.board_close_terminal_enabled is False


def test_set_dictation_shortcut_accepts_a_valid_triple(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {}
    _dispatch(instance, "set_dictation_shortcut",
              {"key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"})
    assert instance._settings["dictation_shortcut"] == {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}
    assert saved == [{"dictation_shortcut": {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}}]


def test_set_dictation_shortcut_declines_a_modifier_less_one(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {}
    _dispatch(instance, "set_dictation_shortcut",
              {"key_code": 2, "modifiers": 0, "label": "D"})
    assert instance._settings["dictation_shortcut"] == {}
    assert saved == []


def test_set_dictation_shortcut_declines_an_out_of_range_key_code(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {}
    _dispatch(instance, "set_dictation_shortcut",
              {"key_code": 200, "modifiers": A._DICTATION_COMMAND, "label": "⌘"})
    assert instance._settings["dictation_shortcut"] == {}
    assert saved == []


def test_set_dictation_shortcut_declines_a_label_over_8_characters(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {}
    _dispatch(instance, "set_dictation_shortcut",
              {"key_code": 2, "modifiers": A._DICTATION_COMMAND,
               "label": "⌘SHIFT+DX"})
    assert instance._settings["dictation_shortcut"] == {}
    assert saved == []


def test_on_dictate_returns_without_posting_when_shortcut_unset(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {}
    instance._on_dictate()
    assert posted == []


def test_on_dictate_returns_without_posting_when_untrusted(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: False)
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}
    instance._on_dictate()
    assert posted == []


def test_on_dictate_posts_when_trusted_and_a_shortcut_is_set(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append(a))
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}
    instance._on_dictate()
    assert posted == [(2, A._DICTATION_COMMAND, True),
                      (2, A._DICTATION_COMMAND, False)]


def test_set_dictation_shortcut_accepts_a_bare_right_command(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {}
    _dispatch(instance, "set_dictation_shortcut",
              {"key_code": 54, "modifiers": 0, "label": "R⌘"})
    assert instance._settings["dictation_shortcut"] == {
        "key_code": 54, "modifiers": 0, "label": "R⌘"}
    assert saved == [{"dictation_shortcut": {
        "key_code": 54, "modifiers": 0, "label": "R⌘"}}]


def test_set_dictation_shortcut_accepts_right_command_with_its_mask(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {}
    _dispatch(instance, "set_dictation_shortcut",
              {"key_code": 54, "modifiers": 1 << 20, "label": "R⌘"})
    assert instance._settings["dictation_shortcut"] == {
        "key_code": 54, "modifiers": A._DICTATION_COMMAND, "label": "R⌘"}
    assert saved == [{"dictation_shortcut": {
        "key_code": 54, "modifiers": A._DICTATION_COMMAND, "label": "R⌘"}}]


def test_dictation_event_flags_for_a_modifier_and_an_ordinary_key():
    from dark_army_menubar import app as A
    cmd = 1 << 20
    rcmd = cmd | A._DICTATION_DEVICE_BITS[54]
    lcmd = cmd | A._DICTATION_DEVICE_BITS[55]
    assert A._dictation_event_flags(54, cmd, True) == rcmd
    assert A._dictation_event_flags(54, cmd, False) == 0
    assert A._dictation_event_flags(54, 0, True) == rcmd
    assert A._dictation_event_flags(55, cmd, True) == lcmd
    assert A._dictation_event_flags(2, cmd, True) == cmd
    assert A._dictation_event_flags(2, cmd, False) == cmd


def test_dictate_down_posts_once_and_a_second_down_is_ignored(monkeypatch):
    import threading
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append(a))
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}
    try:
        instance._on_dictate_down()
        instance._on_dictate_down()
        timer = instance._dictation_timer
        assert posted == [(2, A._DICTATION_COMMAND, True)]
        assert instance._dictation_hold == {
            "key_code": 2, "modifiers": A._DICTATION_COMMAND}
        assert isinstance(timer, threading.Timer)
        assert timer.interval == A.DICTATION_HOLD_MAX_SECONDS
        assert timer.interval == 120
        assert not timer.finished.is_set()
    finally:
        instance._release_dictation_if_down("test")


def test_dictate_up_posts_the_snapshotted_key_after_a_re_record(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append(a))
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}
    instance._on_dictate_down()
    timer = instance._dictation_timer
    instance._settings["dictation_shortcut"] = {
        "key_code": 54, "modifiers": A._DICTATION_COMMAND, "label": "R⌘"}
    instance._on_dictate_up()
    assert posted == [(2, A._DICTATION_COMMAND, True),
                      (2, A._DICTATION_COMMAND, False)]
    assert instance._dictation_hold is None
    assert instance._dictation_timer is None
    assert timer.finished.is_set()
    instance._on_dictate_up()
    assert posted == [(2, A._DICTATION_COMMAND, True),
                      (2, A._DICTATION_COMMAND, False)]


def test_dictate_up_with_nothing_held_posts_nothing(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append(a))
    instance = _action_app()
    instance._on_dictate_up()
    assert posted == []


def test_dictate_down_and_up_skip_the_panel_context_push(monkeypatch):
    """EOF fires dictate_up after the panel is dead; a push would spawn it.

    The hop via callAfter is kept — a last-line down may already be queued
    on the main-thread FIFO — and the names stay in PANEL_ACTIONS. Skipping
    the push is what stops `_send` → `_spawn` on a dead panel.
    """
    from dark_army_menubar import app as A
    from dark_army_menubar.app import BobCompanionApp

    assert "dictate_down" in BobCompanionApp.PANEL_ACTIONS
    assert "dictate_up" in BobCompanionApp.PANEL_ACTIONS
    # The retired file gate's switch went with the gate (5 Sep 2026): an
    # action nothing can do is a row the panel could still send.
    assert "set_board_queue" not in BobCompanionApp.PANEL_ACTIONS

    pushed = []
    monkeypatch.setattr(A, "post_dictation_key", lambda *a, **k: None)
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    monkeypatch.setattr(A, "save_preferences", lambda updates: None)
    instance = _action_app()
    instance._push_panel_context = lambda: pushed.append(True)
    instance._settings["dictation_shortcut"] = {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}
    try:
        _dispatch(instance, "dictate_down")
        _dispatch(instance, "dictate_up")
        assert pushed == []
        # A settings verb still pushes — the skip is those two names, not
        # the hop.
        instance._settings["notification_sound"] = True
        _dispatch(instance, "set_notification_sound", False)
        assert pushed == [True]
    finally:
        instance._release_dictation_if_down("test")


def test_dictation_hold_timer_releases_and_a_second_fire_is_idle(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append(a))
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}
    try:
        instance._on_dictate_down()
        timer = instance._dictation_timer
        assert timer.interval == A.DICTATION_HOLD_MAX_SECONDS
        instance._on_dictation_hold_expired()
        assert posted == [(2, A._DICTATION_COMMAND, True)]
        instance._dictation_deadline = 0
        instance._on_dictation_hold_expired()
        instance._on_dictation_hold_expired()
        assert posted == [(2, A._DICTATION_COMMAND, True),
                          (2, A._DICTATION_COMMAND, False)]
        assert instance._dictation_hold is None
        assert timer.finished.is_set()
    finally:
        instance._release_dictation_if_down("test")


def test_failed_release_keeps_the_hold_so_a_retry_can_post(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    fail_up = {"once": True}

    def post(key_code, modifiers, down):
        if not down and fail_up["once"]:
            fail_up["once"] = False
            raise RuntimeError("cg failed")
        posted.append((key_code, modifiers, down))

    monkeypatch.setattr(A, "post_dictation_key", post)
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    instance = _action_app()
    instance._settings["dictation_shortcut"] = {
        "key_code": 2, "modifiers": A._DICTATION_COMMAND, "label": "⌘D"}
    try:
        instance._on_dictate_down()
        instance._release_dictation_if_down("up")
        assert posted == [(2, A._DICTATION_COMMAND, True)]
        assert instance._dictation_hold == {
            "key_code": 2, "modifiers": A._DICTATION_COMMAND}
        instance._release_dictation_if_down("up")
        assert posted == [(2, A._DICTATION_COMMAND, True),
                          (2, A._DICTATION_COMMAND, False)]
        assert instance._dictation_hold is None
        instance._release_dictation_if_down("up")
        assert posted == [(2, A._DICTATION_COMMAND, True),
                          (2, A._DICTATION_COMMAND, False)]
    finally:
        instance._release_dictation_if_down("test")


def test_quit_releases_a_held_dictation_key(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append(a))
    instance = _action_app()
    instance._dictation_hold = {
        "key_code": 54, "modifiers": A._DICTATION_COMMAND}
    instance._release_dictation_if_down("quit")
    assert posted == [(54, A._DICTATION_COMMAND, False)]
    assert instance._dictation_hold is None
    instance._release_dictation_if_down("quit")
    assert posted == [(54, A._DICTATION_COMMAND, False)]


def test_restart_releases_a_held_dictation_key(monkeypatch):
    from dark_army_menubar import app as A
    posted = []
    monkeypatch.setattr(A, "post_dictation_key",
                        lambda *a, **k: posted.append(a))
    instance = _action_app()
    instance._dictation_hold = {
        "key_code": 54, "modifiers": A._DICTATION_COMMAND}
    instance._release_dictation_if_down("restart")
    assert posted == [(54, A._DICTATION_COMMAND, False)]
    assert instance._dictation_hold is None
    instance._release_dictation_if_down("restart")
    assert posted == [(54, A._DICTATION_COMMAND, False)]


def test_the_stored_session_timeout_reaches_the_daemon_at_startup():
    """The picker is gone; startup hands the stored value to the daemon."""
    import ast
    from pathlib import Path
    from dark_army_menubar import app as app_mod

    src = Path(app_mod.__file__).read_text()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_start_daemon_thread":
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if (isinstance(func, ast.Attribute)
                    and func.attr == "set_session_timeout"):
                found = True
    assert found, "_start_daemon_thread must call set_session_timeout"


def test_every_action_the_panel_offers_has_a_handler():
    """The two halves of this channel live in different languages, so nothing but
    a test can hold them together."""
    from dark_army_menubar.app import BobCompanionApp
    from pathlib import Path
    import re

    source = (Path(__file__).resolve().parents[2] / "panel" / "Sources"
              / "BobPanel").glob("*.swift")
    sent = set()
    for path in source:
        sent |= set(re.findall(r'Panel\.send\(action: "([a-z_]+)"', path.read_text()))
    assert sent, "no Panel.send calls found — did the helper move?"
    # `panel_visibility` is handled *before* the table in `_on_panel_action`,
    # deliberately: it is kept for an older panel binary and for the EOF
    # clear, and the generic path would hop to the AppKit thread and walk the
    # source tree (`_push_panel_context` → `dev_build.check_staleness`) every
    # beat. It has a handler; it just is not a table row. See
    # test_panel_visibility.py.
    # `panel_visibility`, `panel_terminal` and `context_refresh` (a 403 asking
    # for the desk token again) are handled before the table
    # (`_on_panel_action`), so none has a row in it.
    assert sent <= set(BobCompanionApp.PANEL_ACTIONS) | {
        "panel_visibility", "panel_terminal", "context_refresh"}


def test_an_unknown_panel_action_is_ignored():
    """This is a pipe from another process; a name we do not recognise must not
    be guessed at."""
    instance = _action_app()
    _dispatch(instance, "rm -rf")          # must not raise


def test_a_failing_action_does_not_take_the_reader_thread_down():
    def boom(_):
        raise RuntimeError("nope")

    instance = _action_app(_on_restart=boom)
    _dispatch(instance, "restart")         # must not raise


def test_panel_events_reach_the_callback(tmp_path):
    """The reader turns the panel's stdout into app actions, and ignores
    anything that is not one — a Swift runtime warning on stdout must not be
    able to trigger a rebuild."""
    import io
    from unittest.mock import MagicMock
    from dark_army_menubar.panel_process import PanelProcess

    seen = []
    panel = PanelProcess(on_action=lambda name, value: seen.append((name, value)))
    proc = MagicMock()
    proc.stdout = io.BytesIO(
        b'{"event":"action","name":"restart"}\n'
        b'warning: something from the runtime\n'
        b'{"event":"action","name":""}\n'
        b'[]\n'
        b'{"event":"other","name":"rebuild"}\n'
        b'{"event":"action","name":"set_board_close_terminal","value":true}\n'
        b'{"event":"action","name":"quit_app"}\n'
    )
    panel._read_events(proc)
    # The trailing `panel_visibility: False` is the reader's own last act on
    # EOF, not something the panel sent: a stale "visible" reading must never
    # outlive the process that reported it. Pinned in test_panel_visibility.py.
    assert seen == [("restart", None), ("set_board_close_terminal", True),
                    ("quit_app", None), ("panel_visibility", False),
                    ("dictate_up", None)]


def test_the_bundled_panel_is_found_without_sys_frozen(tmp_path, monkeypatch):
    """Only the bundle's `__boot__.py` sets `sys.frozen`, and Restart relaunches
    the app as `sys.executable -m dark_army_menubar` — the bundled
    interpreter, booted without that script. Gating the bundled lookup on the
    flag therefore worked on first launch and returned None after every Restart,
    with the binary sitting in Contents/Resources the whole time. The symptom was
    "The panel is not built" on an app that had just built it."""
    import sys as _sys
    from dark_army_menubar import panel_process

    app = tmp_path / "Dark Army.app" / "Contents"
    (app / "MacOS").mkdir(parents=True)
    (app / "Resources").mkdir()
    binary = app / "Resources" / panel_process.EXECUTABLE_NAME
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)

    monkeypatch.setattr(_sys, "executable", str(app / "MacOS" / "python"))
    monkeypatch.delattr(_sys, "frozen", raising=False)
    assert panel_process.find_executable() == str(binary)


def test_restart_relaunches_a_bundle_through_launchservices(monkeypatch):
    """A bare `exec` inherits the lineage of whatever started the app. When that
    is a VS Code terminal, every `code` the new daemon spawns deadlocks forever
    in module init — which took Jump's targeted window raise out machine-wide.
    `open -n -a` gives the replacement the lineage a Finder launch has."""
    from dark_army_menubar import app as app_mod

    bundle = "/Applications/Dark Army.app"
    monkeypatch.setattr(app_mod, "_app_bundle_path", lambda: bundle)
    cmd = app_mod._relaunch_command()
    assert cmd.startswith("/usr/bin/open -n -a ")
    assert bundle in cmd
    assert "-m dark_army_menubar" not in cmd


def test_restart_from_a_checkout_still_execs_the_interpreter(monkeypatch):
    """There is no bundle to open in a source tree, and that build is launched
    from a terminal either way."""
    from dark_army_menubar import app as app_mod

    monkeypatch.setattr(app_mod, "_app_bundle_path", lambda: None)
    assert app_mod._relaunch_command().endswith("-m dark_army_menubar")


def test_app_bundle_path_recognises_the_bundle_it_runs_from(monkeypatch):
    from pathlib import Path
    from dark_army_menubar import app as app_mod

    inside = ("/Applications/Dark Army.app/Contents/Resources/lib/"
              "python3.13/dark_army_menubar/app.py")
    monkeypatch.setattr(app_mod.os.path, "realpath", lambda p: inside)
    assert app_mod._app_bundle_path() == "/Applications/Dark Army.app"

    monkeypatch.setattr(app_mod.os.path, "realpath",
                        lambda p: "/Users/x/repo/host/dark_army_menubar/app.py")
    assert app_mod._app_bundle_path() is None


def _portrait_tree(tmp_path, slug="relay", px=512):
    """A one-portrait tree, drawn through AppKit so the test needs no Pillow."""
    from AppKit import (NSBitmapImageRep, NSGraphicsContext, NSColor,
                        NSCalibratedRGBColorSpace)
    rep = NSBitmapImageRep.alloc() \
        .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, px, px, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(ctx)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.3, 0.2, 0.6, 1.0).setFill()
        from AppKit import NSBezierPath
        NSBezierPath.bezierPathWithRect_(((0.0, 0.0), (float(px), float(px)))).fill()
    finally:
        NSGraphicsContext.restoreGraphicsState()
    tree = tmp_path / "portraits"
    tree.mkdir()
    assert rep.representationUsingType_properties_(4, {}) \
        .writeToFile_atomically_(str(tree / f"{slug}.png"), True)
    return tree


def test_the_banner_avatar_is_rendered_from_the_portrait(tmp_path, monkeypatch):
    """The face on the banner is composed, not shipped: the 512px portrait
    scaled *down* onto a disc. Asserted end to end because every failure in
    that chain is silent by design — a banner without a face is still a
    banner. The tree is a stub, because the shipped one may lawfully hold no
    portrait yet."""
    from pathlib import Path
    from dark_army_menubar import notifier

    pytest.importorskip("AppKit")
    tree = _portrait_tree(tmp_path)
    monkeypatch.setattr(notifier, "_portrait_dir", lambda: tree)
    notifier._avatar_cache.clear()
    path = notifier._avatar_png("Relay")
    assert path, "no avatar rendered"
    png = Path(path)
    assert png.is_file() and png.stat().st_size > 0
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # Cached: composing per banner is work done at the one moment something is
    # already urgent. Keyed by the lowercased slug alone — no state.
    assert notifier._avatar_png("relay") == path
    assert set(notifier._avatar_cache) == {"relay"}
    # Never the strip's pixel art: the composer reads the portrait tree only.
    notifier._avatar_cache.clear()


def test_a_missing_portrait_is_no_face_and_the_miss_is_cached(tmp_path, monkeypatch):
    """A character whose photograph has not been chosen yet goes out faceless
    — never wearing somebody else's — and the miss is remembered, so a banner
    is not a disk walk each time."""
    from dark_army_menubar import notifier

    tree = tmp_path / "portraits"
    tree.mkdir()
    monkeypatch.setattr(notifier, "_portrait_dir", lambda: tree)
    notifier._avatar_cache.clear()
    assert notifier._avatar_png("nyx") is None
    assert notifier._avatar_cache == {"nyx": ""}
    monkeypatch.setattr(notifier, "_portrait_path",
                        lambda c: pytest.fail("the miss was not cached"))
    assert notifier._avatar_png("Nyx") is None
    notifier._avatar_cache.clear()


def test_an_unknown_face_is_no_face_rather_than_a_crash():
    from dark_army_menubar import notifier

    notifier._avatar_cache.clear()
    assert notifier._avatar_png("nobody") is None
    assert notifier._avatar_png("") is None
    notifier._avatar_cache.clear()


def test_the_portrait_dir_is_the_photo_tree_not_the_pixel_art(tmp_path, monkeypatch):
    """`_portrait_dir` resolves `assets/portraits` in a checkout — never
    `assets/cast`, whose 32-pixel figures are the strip's and would come out
    of the banner's smooth downscale as a blur."""
    from dark_army_menubar import notifier
    found = notifier._portrait_dir()
    if found is not None:
        assert found.name == "portraits"
    assert notifier._portrait_path("") is None


def test_a_question_attaches_a_portrait_and_attention_does_not(tmp_path, monkeypatch):
    """The kind gate, on the existing portrait seam: a question, a permission
    ask and a finished run still attach; a plain needs-you does not, and it
    does not even look the portrait up."""
    from dark_army_menubar import notifier

    pytest.importorskip("AppKit")
    tree = _portrait_tree(tmp_path)
    monkeypatch.setattr(notifier, "_portrait_dir", lambda: tree)
    notifier._avatar_cache.clear()
    lookups = []
    real = notifier._avatar_png

    def wrapped(character):
        lookups.append(character)
        return real(character)

    monkeypatch.setattr(notifier, "_avatar_png", wrapped)

    class Content:
        def __init__(self):
            self.attachments = None

        def setAttachments_(self, items):
            self.attachments = list(items)

    class Attachment:
        @staticmethod
        def attachmentWithIdentifier_URL_options_error_(ident, url, options, error):
            return ("att", None)

    n = notifier.Notifier.__new__(notifier.Notifier)
    n._ns = {"UNNotificationAttachment": Attachment}

    attention = Content()
    n._attach_avatar(attention, {"kind": "attention", "character": "relay"})
    assert lookups == []
    assert attention.attachments is None

    for kind in ("question", "permission", "finished"):
        lookups.clear()
        content = Content()
        n._attach_avatar(content, {"kind": kind, "character": "relay"})
        assert lookups == ["relay"], kind
        assert content.attachments == ["att"], kind
    notifier._avatar_cache.clear()


def test_attachment_result_is_read_whether_or_not_it_is_a_pair():
    """`UNNotificationAttachment`'s `error:` is an out-parameter only when PyObjC
    has metadata for the selector, and a class from `objc.loadBundle` has none.
    Assuming the pair silently cost every banner its face."""
    from dark_army_menubar.notifier import _unpack_result

    assert _unpack_result(("att", None)) == ("att", None)
    assert _unpack_result(("att", "boom")) == ("att", "boom")
    assert _unpack_result("att") == ("att", None)
    assert _unpack_result(None) == (None, None)


# --- The panel's view of a job that is in flight -------------------------------
# `_on_rebuild` sets `_rebuilding` and immediately pushes context, and the panel
# greys and relabels its row on `settings.rebuilding`. That flag was never put in
# the payload, so `Rebuild & Deploy` stayed live and silent for the several
# minutes of `build.sh --install` — indistinguishable from a click that missed.

class _RecordingPanel:
    available = True
    executable_path = None

    def __init__(self):
        self.contexts = []

    def set_context(self, **fields):
        self.contexts.append(fields)
        return True


def _context_app(**state):
    from dark_army_menubar.app import BobCompanionApp
    app = object.__new__(BobCompanionApp)
    app._panel = _RecordingPanel()
    app._settings = {"notification_sound": True}
    app._vscode_installing = False
    app._rebuilding = False
    app._repo_root = None
    app._grok_limits = {}
    for key, value in state.items():
        setattr(app, key, value)
    return app


def test_panel_context_carries_dictation_facts(monkeypatch):
    from dark_army_menubar import app as A
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    monkeypatch.setattr(A, "macwhisper_installed", lambda: True)
    app = _context_app()
    app._push_panel_context()
    settings = app._panel.contexts[-1]["settings"]
    assert settings["accessibility_trusted"] is True
    assert settings["macwhisper_installed"] is True


def test_panel_context_carries_the_in_flight_jobs():
    """Both long jobs reach the panel, so neither button lies about being idle."""
    app = _context_app(_rebuilding=True, _vscode_installing=True)
    app._push_panel_context()
    settings = app._panel.contexts[-1]["settings"]
    assert settings["rebuilding"] is True
    assert settings["vscode_installing"] is True
    # ...and the preferences still ride along beside them.
    assert settings["notification_sound"] is True


def test_a_release_with_no_source_says_so_and_offers_no_rebuild():
    """`build_info is None` means two different things. In the artifact people
    download there is no checkout anywhere, and the honest line is "release (no
    source)" — the row used to go blank, so the one branch `artifact_label`
    exists for was unreachable."""
    app = _context_app(_repo_root=None, _panel_probes={"build_info": None})
    app._push_panel_context()
    ctx = app._panel.contexts[-1]
    assert ctx["build"] == "Build: release (no source)"
    assert ctx["build_stale"] is False
    assert ctx["can_rebuild"] is False


def test_a_checkout_with_no_panel_binary_claims_nothing():
    """The other half: a repo root is known, so this is somebody's checkout and
    the panel is merely unbuilt. Calling that a release would be a lie, so the
    build row stays empty while Rebuild stays offered."""
    from pathlib import Path
    app = _context_app(_repo_root=Path("/somewhere/checkout"),
                       _panel_probes={"build_info": None})
    app._push_panel_context()
    ctx = app._panel.contexts[-1]
    assert ctx["build"] == ""
    assert ctx["can_rebuild"] is True


def test_panel_context_carries_grok_reset():
    """Billing's week-end rides next to the percent the chip already gets."""
    app = _context_app(_grok_limits={
        "percent": 25.0, "resets_at": 1_787_337_420.0, "stale": False,
    })
    app._push_panel_context()
    ctx = app._panel.contexts[-1]
    assert ctx["grok_percent"] == 25.0
    assert ctx["grok_resets_at"] == 1_787_337_420.0


def test_panel_context_omits_grok_reset_when_billing_did():
    """No date from billing means None — Dark Army never invents one."""
    app = _context_app(_grok_limits={"percent": 25.0})
    app._push_panel_context()
    ctx = app._panel.contexts[-1]
    assert ctx["grok_percent"] == 25.0
    assert ctx["grok_resets_at"] is None


def test_panel_context_empty_grok_snapshot_invents_nothing():
    app = _context_app(_grok_limits={})
    app._push_panel_context()
    ctx = app._panel.contexts[-1]
    assert ctx["grok_percent"] is None
    assert ctx["grok_resets_at"] is None


def test_panel_context_stale_grok_still_withholds_percent():
    """A stale window still hides the percent; the date is not invented."""
    app = _context_app(_grok_limits={
        "percent": 25.0, "resets_at": 1_787_337_420.0, "stale": True,
    })
    app._push_panel_context()
    ctx = app._panel.contexts[-1]
    assert ctx["grok_percent"] is None
    assert ctx["grok_resets_at"] in (None, 1_787_337_420.0)


def test_a_rebuild_click_tells_the_panel_before_it_starts_building():
    """The debounce and the greyed row are the same fact, so the push has to
    happen between setting the flag and handing the build to a worker."""
    from dark_army_menubar import app as A

    app = _context_app(_repo_root=Path("/repo"))
    started = []
    original = A.threading.Thread
    A.threading.Thread = lambda target, daemon=False: _NoopThread()
    try:
        app._on_rebuild(None)
    finally:
        A.threading.Thread = original

    assert app._rebuilding is True
    assert app._panel.contexts[-1]["settings"]["rebuilding"] is True
    # A second click while that one is in flight is dropped.
    app._on_rebuild(None)
    assert len(app._panel.contexts) == 1
    assert started == []


# ── one gate for all three rebuild entry points ──────────────────────────────
#
# The window's button, the phone's tile and an agent's button all end in
# `_on_rebuild`; `on_rebuild_request` is only the hop from the daemon's loop
# thread to the main thread.

class _FakeLoop:
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, fn, *args):
        self.calls.append((fn, args))


class _FakeDaemon:
    def __init__(self):
        self.facts = []

    def set_rebuild_state(self, facts):
        self.facts.append(facts)

    def desk_token(self):
        return ""


def test_a_daemon_rebuild_request_hops_to_the_main_thread(monkeypatch):
    from dark_army_menubar import app as A
    hops = []
    monkeypatch.setattr(A, "callAfter", lambda fn, *a: hops.append((fn, a)))
    app = _context_app()
    app.on_rebuild_request()
    assert hops == [(app._on_rebuild, (None,))]


def test_a_rebuild_is_refused_while_a_restart_is_under_way():
    app = _context_app(_repo_root=Path("/repo"), _restarting=True)
    app._on_rebuild(None)
    assert app._rebuilding is False
    assert app._panel.contexts == []


def test_a_rebuild_click_publishes_the_in_flight_facts(monkeypatch):
    from dark_army_menubar import app as A
    app = _context_app(_repo_root=Path("/repo"), _daemon=_FakeDaemon(),
                       _loop=_FakeLoop())
    monkeypatch.setattr(A.time, "time", lambda: 1234.5)
    monkeypatch.setattr(A.threading, "Thread",
                        lambda target, daemon=False: _NoopThread())
    app._on_rebuild(None)
    ctx = app._panel.contexts[-1]
    assert ctx["settings"]["rebuilding"] is True
    assert ctx["rebuild_started_at"] == 1234.5
    assert ctx["rebuild_outcome"] == "" and ctx["rebuild_error"] == ""
    (fn, (facts,)), = app._loop.calls
    assert fn == app._daemon.set_rebuild_state
    assert facts["rebuilding"] is True and facts["started_at"] == 1234.5


def test_a_successful_rebuild_leaves_a_stamp_then_restarts(monkeypatch, tmp_path):
    from dark_army_daemon import paths, rebuild_state
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    order = []
    app = _context_app(_repo_root=Path("/repo"))
    app._rebuild = {"started_at": 100.0, "last_outcome": "", "last_error": "",
                    "last_finished_at": None}
    app._rebuilding = True
    app._refresh_build_status = lambda: None
    app._on_restart = lambda _: order.append(
        ("restart", rebuild_state.read_stamp()))
    app._rebuild_finished(True, "")
    assert app._rebuilding is False
    (name, stamp), = order
    assert name == "restart"
    assert stamp["ok"] is True and stamp["started_at"] == 100.0


def test_a_restart_under_way_is_published_and_a_success_tells_the_daemon_first(
        monkeypatch, tmp_path):
    from dark_army_daemon import paths
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    app = _context_app(_repo_root=Path("/repo"), _daemon=_FakeDaemon(),
                       _loop=_FakeLoop())
    assert app._rebuild_facts()["restarting"] is False
    app._refresh_build_status = lambda: None
    seen = []
    # An early-returning `_on_restart` (already restarting) tells nobody, so
    # the success itself must have told the daemon before calling it.
    app._on_restart = lambda _: seen.append(len(app._loop.calls))
    app._rebuild_finished(True, "")
    assert seen == [1]
    (_fn, (facts,)), = app._loop.calls
    assert facts["last_outcome"] == "ok" and facts["rebuilding"] is False
    app._restarting = True
    assert app._rebuild_facts()["restarting"] is True


def test_telling_the_daemon_can_never_stop_a_restart(monkeypatch, tmp_path):
    from dark_army_daemon import paths
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    app = _context_app(_repo_root=Path("/repo"), _daemon=_FakeDaemon(),
                       _loop=_FakeLoop())

    def boom():
        raise RuntimeError("facts blew up")
    app._rebuild_facts = boom
    app._tell_daemon_rebuild()      # swallowed, never raised
    restarted = []
    app._refresh_build_status = lambda: None
    app._on_restart = lambda _: restarted.append(True)
    app._rebuild_finished(True, "")
    assert restarted == [True]


def test_the_failure_text_is_redacted_before_it_is_cut(monkeypatch):
    from pathlib import Path as P
    from dark_army_menubar import app as A
    monkeypatch.setattr(A.rumps, "notification", lambda *a, **k: None)
    home = str(P.home())
    app = _context_app(_repo_root=Path("/repo"), _daemon=_FakeDaemon(),
                       _loop=_FakeLoop())
    app._rebuild_finished(False, ("z" * 290) + f" {home}/x")
    assert home not in app._rebuild_facts()["last_error"]


def test_a_stamp_that_cannot_be_written_never_stops_the_restart(monkeypatch):
    from dark_army_daemon import rebuild_state
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(rebuild_state, "write_stamp", boom)
    restarted = []
    app = _context_app(_repo_root=Path("/repo"))
    app._refresh_build_status = lambda: None
    app._on_restart = lambda _: restarted.append(True)
    app._rebuild_finished(True, "")
    assert restarted == [True]


def test_a_failed_rebuild_leaves_no_stamp_and_says_so(monkeypatch, tmp_path):
    from dark_army_menubar import app as A
    from dark_army_daemon import paths, rebuild_state
    monkeypatch.setattr(paths, "REBUILD_STAMP_PATH", tmp_path / "stamp.json")
    monkeypatch.setattr(A.rumps, "notification", lambda *a, **k: None)
    app = _context_app(_repo_root=Path("/repo"), _daemon=_FakeDaemon(),
                       _loop=_FakeLoop())
    app._rebuilding = True
    app._on_restart = lambda _: pytest.fail("a failed build must not restart")
    app._rebuild_finished(False, "boom")
    assert rebuild_state.read_stamp() == {}
    ctx = app._panel.contexts[-1]
    assert ctx["rebuild_outcome"] == "failed" and ctx["rebuild_error"] == "boom"
    assert ctx["settings"]["rebuilding"] is False
    (_fn, (facts,)), = app._loop.calls
    assert facts["rebuilding"] is False and facts["last_outcome"] == "failed"
    assert facts["last_error"] == "boom"


def test_telling_the_daemon_never_waits_on_the_loop():
    import json
    app = _context_app(_repo_root=Path("/repo"), _daemon=_FakeDaemon(),
                       _loop=_FakeLoop())
    app._tell_daemon_rebuild()
    (fn, (facts,)), = app._loop.calls
    assert fn == app._daemon.set_rebuild_state
    assert facts["available"] is True
    assert "/repo" not in json.dumps(facts, default=str)


def test_before_the_loop_exists_the_daemon_is_told_directly():
    app = _context_app(_repo_root=Path("/repo"), _daemon=_FakeDaemon(),
                       _loop=None)
    app._tell_daemon_rebuild()
    assert app._daemon.facts and app._daemon.facts[-1]["available"] is True


def test_a_release_with_no_source_publishes_nothing_to_rebuild():
    app = _context_app(_repo_root=None, _daemon=_FakeDaemon(), _loop=None)
    app._tell_daemon_rebuild()
    assert app._daemon.facts[-1]["available"] is False


# ── the 30s context push stays off the AppKit thread and off dead panels ─────
#
# `_push_panel_context` used to run `dev_build.check_staleness` (a glob+stat
# file-tree walk), the accessibility probe and the MacWhisper probe on the
# main thread every 30 seconds — and because `_send` spawns a dead panel, the
# same timer silently relaunched a crashed panel twice a minute, forever.


class _LivenessPanel(_RecordingPanel):
    def __init__(self, alive=True):
        super().__init__()
        self._alive_now = alive

    def alive(self):
        return self._alive_now


_PROBES = {"build_info": None, "accessibility_trusted": True,
           "macwhisper_installed": True}


def test_the_periodic_push_skips_a_dead_panel():
    """Only a user gesture respawns; the limits timer must not."""
    app = _context_app(_panel=_LivenessPanel(alive=False))
    app._apply_limits({}, {}, {}, dict(_PROBES))
    assert app._panel.contexts == []


def test_the_periodic_push_still_reaches_a_live_panel():
    app = _context_app(_panel=_LivenessPanel(alive=True))
    app._apply_limits({}, {}, {}, dict(_PROBES))
    assert len(app._panel.contexts) == 1


def test_a_user_gesture_push_may_respawn():
    """The default path keeps the old behaviour: `_send` spawns."""
    app = _context_app(_panel=_LivenessPanel(alive=False))
    app._panel_probes = dict(_PROBES)
    app._push_panel_context()
    assert len(app._panel.contexts) == 1


def test_the_push_ships_cached_probes_not_fresh_ones(monkeypatch):
    """The expensive reads happen on the limits worker; the push only ships
    the cache. If the push recomputed, it would be back on the main thread."""
    from dark_army_menubar import app as A

    def trap():
        raise AssertionError("probe recomputed on the push path")

    monkeypatch.setattr(A, "ax_is_process_trusted", trap)
    monkeypatch.setattr(A, "macwhisper_installed", trap)
    monkeypatch.setattr(A.dev_build, "check_staleness",
                        lambda *a, **k: trap())
    app = _context_app(_panel_probes=dict(_PROBES))
    app._push_panel_context()
    settings = app._panel.contexts[-1]["settings"]
    assert settings["accessibility_trusted"] is True
    assert settings["macwhisper_installed"] is True


def test_the_limits_worker_hands_the_probes_in(monkeypatch):
    """`work()` computes them and `_apply_limits` receives them — the seam
    item by item: staleness, accessibility, MacWhisper."""
    from dark_army_menubar import app as A

    class _InlineThread:
        def __init__(self, target=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    landed = []
    monkeypatch.setattr(A.threading, "Thread", _InlineThread)
    monkeypatch.setattr(A, "callAfter", lambda fn, *args: landed.append(args))
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    monkeypatch.setattr(A, "macwhisper_installed", lambda: False)
    monkeypatch.setattr(A.dev_build, "check_staleness", lambda *a: None)

    app = _context_app(_daemon=None, _agents_snapshot={})
    app._refresh_limits(None)

    assert len(landed) == 1
    probes = landed[0][3]
    assert probes == {"build_info": None, "accessibility_trusted": True,
                      "macwhisper_installed": False,
                      "pack_self_roots": [], "pack_self_roots_mtime": 0}


# ── the channel install left the main thread ─────────────────────────────────


def test_construction_no_longer_installs_the_channel_inline():
    """Two `claude mcp` and up to four `codex mcp`, 20s timeout each, used to
    run in __init__ before the status item existed."""
    import inspect
    from dark_army_menubar.app import BobCompanionApp

    source = inspect.getsource(BobCompanionApp.__init__)
    assert "channel_install.install(" not in source
    assert "_install_channel_quietly" in source


def test_the_launch_time_install_does_not_force(monkeypatch):
    from dark_army_menubar import app as A

    recorded = []
    monkeypatch.setattr(A.channel_install, "install",
                        lambda *a, **k: recorded.append((a, k)) or True)
    app = object.__new__(A.BobCompanionApp)
    app._install_channel_quietly()
    assert recorded == [((), {})]


def test_the_channel_toggle_forces_a_real_install(monkeypatch):
    """The deliberate press is also the repair for a registration removed
    behind the skip-marker's back, so it must not be skipped."""
    from dark_army_menubar import app as A

    class _InlineThread:
        def __init__(self, target=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    recorded = []
    monkeypatch.setattr(A.threading, "Thread", _InlineThread)
    monkeypatch.setattr(A, "callAfter", lambda fn, *args: None)
    monkeypatch.setattr(A.channel_install, "install",
                        lambda *a, **k: recorded.append((a, k)) or True)

    app = object.__new__(A.BobCompanionApp)
    app._set_channel(True)
    assert recorded == [((), {"force": True})]


@pytest.fixture
def selected_panel_app(tmp_path, monkeypatch):
    """Real selection/property and spawn argv, isolated from installed Dark Army."""
    import os
    from types import SimpleNamespace
    from dark_army_menubar import app as A, panel_process

    root = tmp_path.resolve()
    checkout = root / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    resources = root / "Dark Army.app/Contents/Resources"
    monkeypatch.setattr(panel_process.sys, "executable",
                        str(resources.parent / "MacOS/Dark Army"))
    monkeypatch.setattr(panel_process, "__file__",
                        str(checkout / "host/dark_army_menubar/panel_process.py"))
    monkeypatch.setenv("PATH", str(root / "empty-path"))
    monkeypatch.setattr(A.dev_build, "is_frozen", lambda: False)
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    monkeypatch.setattr(A, "macwhisper_installed", lambda: False)

    def dated(path, mtime):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("isolated fixture")
        path.chmod(0o755)
        os.utime(path, (mtime, mtime))
        return str(path)

    dated(checkout / "panel/Sources/BobPanel/main.swift", 2000)
    captured = []
    proc = SimpleNamespace(pid=42, poll=lambda: None)
    monkeypatch.setattr(panel_process.subprocess, "Popen",
                        lambda argv, **kw: captured.append(argv) or proc)
    monkeypatch.setattr(panel_process.threading.Thread, "start", lambda self: None)
    checked = []
    real_check = A.dev_build.check_staleness

    def check(repo, binary):
        checked.append(binary)
        return real_check(repo, binary)

    monkeypatch.setattr(A.dev_build, "check_staleness", check)
    return SimpleNamespace(
        app=_context_app(_repo_root=checkout), dated=dated, captured=captured,
        checked=checked, proc=proc, checkout=checkout,
        nested=resources / "BobPanel.app/Contents/MacOS/BobPanel",
        bare=resources / "BobPanel",
    )


@pytest.mark.parametrize("selected", ["nested", "bare"])
@pytest.mark.parametrize("stale", [True, False])
def test_both_build_consumers_describe_the_spawned_panel(selected_panel_app, caplog, selected, stale):
    from dark_army_menubar import dev_build, panel_process

    f = selected_panel_app
    chosen = f.dated(getattr(f, selected), 1000 if stale else 3000)
    panel = panel_process.PanelProcess()
    f.app._panel = panel
    # A competing copy disagrees; in the bare case it appeared after selection.
    competing = f.nested if selected == "bare" else f.bare
    f.dated(competing, 3000 if stale else 1000)
    f.dated(f.checkout / "panel/.build/release/BobPanel", 3000 if stale else 1000)
    if selected == "bare":
        assert panel_process.find_executable() != chosen
    assert panel._spawn()
    probes = f.app._compute_panel_probes()
    with caplog.at_level("WARNING", logger="dark-army"):
        f.app._refresh_build_status()
    assert f.checked == [chosen, chosen]
    assert f.captured == [[chosen, "--hidden"]]
    assert probes["build_info"]["panel_binary"] == f.captured[0][0]
    assert probes["build_info"]["stale"] is stale
    warnings = [r.getMessage() for r in caplog.records if "Build is STALE" in r.getMessage()]
    assert bool(warnings) is stale
    if stale:
        assert warnings[-1] == (
            f"Build is STALE: source is newer than the built artifact ({chosen}). "
            "Use 'Rebuild & Reload' in the panel.")

    # Ship the computed reading through the same context-recording seam as
    # other settings tests; the sender/liveness behavior is exercised below.
    recording = _LivenessPanel()
    f.app._panel = recording
    f.app._apply_limits({}, {}, {}, probes)
    context = recording.contexts[-1]
    assert context["build"] == dev_build.artifact_label(probes["build_info"])
    assert context["build_stale"] is stale
    assert f.checked == [chosen, chosen]  # cached context never recomputes

    f.app._panel = panel
    f.proc.poll = lambda: 1
    f.app._apply_limits({}, {}, {}, probes)
    assert f.captured == [[chosen, "--hidden"]]  # no periodic respawn


@pytest.mark.parametrize("missing", ["source", "selected-file", "selection"])
def test_unavailable_selected_build_never_substitutes_a_copy(selected_panel_app, caplog, missing):
    from dark_army_menubar import panel_process

    f = selected_panel_app
    if missing != "selection":
        f.dated(f.bare, 1000)
    panel = panel_process.PanelProcess()
    f.app._panel = panel
    chosen = panel.executable_path
    f.dated(f.nested, 3000)
    assert panel_process.find_executable() == str(f.nested)
    if missing == "source":
        f.app._repo_root = None
    elif missing == "selected-file":
        f.bare.unlink()
    probes = f.app._compute_panel_probes()
    with caplog.at_level("WARNING", logger="dark-army"):
        f.app._refresh_build_status()
    assert probes["build_info"] is None
    assert f.checked == ([chosen] if missing == "source" else [chosen, chosen])
    assert not any("Build is STALE" in r.getMessage() for r in caplog.records)
    recording = _LivenessPanel()
    f.app._panel = recording
    f.app._apply_limits({}, {}, {}, probes)
    # No artifact to describe. With no repo root either, that is the released
    # copy and the row says so; with a repo root it is an unbuilt checkout and
    # the row stays empty rather than claiming to be a release.
    assert recording.contexts[-1]["build"] == (
        "Build: release (no source)" if missing == "source" else "")
    assert recording.contexts[-1]["build_stale"] is False
    assert f.captured == []


# --- A phone's pipeline press, stored by the one process that owns settings --


def _preference_app(daemon=None):
    """`_action_app`, plus the `respawn=` the new push actually passes."""
    pushed = []
    instance = _action_app(_daemon=daemon)
    instance._push_panel_context = lambda respawn=True: pushed.append(respawn)
    return instance, pushed


def _request(instance, key, value):
    from dark_army_menubar import app as A
    original = A.callAfter
    A.callAfter = lambda fn, *a: fn(*a)
    try:
        instance.on_preference_request(key, value)
    finally:
        A.callAfter = original


class _ParallelDaemon:
    def __init__(self):
        self.board_autostart_enabled = True
        self.overrides = []

    def set_board_parallel_override(self, root, n):
        self.overrides.append((root, n))


def test_a_phones_autostart_press_is_stored_applied_and_pushed(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _ParallelDaemon()
    instance, pushed = _preference_app(daemon)
    instance._loop = None

    _request(instance, "board_autostart", False)

    assert instance._settings["board_autostart"] is False
    assert saved == [{"board_autostart": False}]
    assert daemon.board_autostart_enabled is False
    # `respawn=False` is load-bearing: a phone press must never open a window
    # on the Mac.
    assert pushed == [False]


def test_a_zero_limit_removes_the_row_rather_than_storing_it(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _ParallelDaemon()
    instance, pushed = _preference_app(daemon)
    instance._loop = None
    instance._settings["board_parallel_by_root"] = {"/x": 3}

    _request(instance, "board_parallel_root", {"root": "/x", "limit": 0})

    assert instance._settings["board_parallel_by_root"] == {}
    assert saved == [{"board_parallel_by_root": {}}]
    assert daemon.overrides == [("/x", None)]
    assert pushed == [False]


def test_a_stored_dial_is_what_the_startup_feed_hands_the_daemon(monkeypatch):
    """The restart-survival property, end to end: the app stores, and the
    app's own launch re-feeds what it stored."""
    from dark_army_menubar import app as A
    monkeypatch.setattr(A, "save_preferences", lambda updates: None)
    daemon = _ParallelDaemon()
    instance, _pushed = _preference_app(daemon)
    instance._loop = None

    _request(instance, "board_parallel_root", {"root": "/x", "limit": 2})
    assert instance._settings["board_parallel_by_root"] == {"/x": 2}

    fed = []
    daemon.set_board_parallel_overrides = fed.append
    daemon.set_board_parallel_overrides(
        instance._settings.get("board_parallel_by_root", {}))
    assert fed == [{"/x": 2}]


class _IsolationDaemon:
    def __init__(self):
        self.overrides = []
        self.fed = []

    def set_board_isolation_override(self, root, enabled):
        self.overrides.append((root, enabled))

    def set_board_isolation_overrides(self, mapping):
        self.fed.append(dict(mapping))


def test_switching_isolation_off_stores_saves_and_tells_the_daemon(monkeypatch):
    """Card isolation (`docs/card-worktrees.md`), `_set_board_parallel_root`'s
    shape through the panel's own action table: only *off* is stored."""
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _IsolationDaemon()
    instance = _action_app(_daemon=daemon, _loop=None)

    _dispatch(instance, "set_board_isolation_root",
              {"root": "/x", "enabled": False})
    assert instance._settings["board_isolation_by_root"] == {"/x": False}
    assert saved == [{"board_isolation_by_root": {"/x": False}}]
    assert daemon.overrides == [("/x", False)]

    _dispatch(instance, "set_board_isolation_root",
              {"root": "/x", "enabled": True})
    assert instance._settings["board_isolation_by_root"] == {}
    assert saved[-1] == {"board_isolation_by_root": {}}
    assert daemon.overrides[-1] == ("/x", True)


def test_an_unusable_isolation_switch_writes_nothing(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _IsolationDaemon()
    instance = _action_app(_daemon=daemon, _loop=None)
    for value in ("/x", {"enabled": False}, {"root": "  "}):
        instance._set_board_isolation_root(value)
    assert saved == [] and daemon.overrides == []


def test_a_stored_isolation_switch_is_what_the_startup_feed_hands_over(
        monkeypatch):
    """The restart-survival property: the app stores, and its launch re-feeds
    what it stored — through the daemon's own setter, beside the parallel
    dials."""
    import inspect
    from dark_army_menubar import app as A
    monkeypatch.setattr(A, "save_preferences", lambda updates: None)
    daemon = _IsolationDaemon()
    instance = _action_app(_daemon=daemon, _loop=None)
    instance._set_board_isolation_root({"root": "/x", "enabled": False})
    daemon.set_board_isolation_overrides(
        instance._settings.get("board_isolation_by_root", {}))
    assert daemon.fed == [{"/x": False}]
    feed = inspect.getsource(A.BobCompanionApp._start_daemon_thread)
    assert "set_board_isolation_overrides(" in feed
    assert 'self._settings.get("board_isolation_by_root", {})' in feed


def test_an_unknown_preference_key_is_ignored_with_no_write(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _ParallelDaemon()
    instance, pushed = _preference_app(daemon)
    instance._loop = None

    _request(instance, "board_dispatch", True)

    assert saved == []
    assert pushed == []
    assert instance._settings == {}


def test_the_phones_table_is_narrower_than_the_panels():
    """The panel sits at the desk and may set anything; the phone may set the
    two pipeline dials and nothing else."""
    from dark_army_menubar.app import BobCompanionApp
    from dark_army_daemon.daemon import PHONE_PREFERENCES
    assert set(BobCompanionApp.PREFERENCE_REQUESTS) == set(PHONE_PREFERENCES)
    # Keyed by the *preference*, where `PANEL_ACTIONS` is keyed by the verb;
    # every one of them still lands on a setter the panel can already reach,
    # and on nothing the panel cannot.
    assert {f"set_{key}" for key in BobCompanionApp.PREFERENCE_REQUESTS} < set(
        BobCompanionApp.PANEL_ACTIONS)


# --- reply by typing ------------------------------------------------------------

def test_set_typed_reply_saves_sets_the_flag_and_republishes(monkeypatch):
    """`_set_board_dispatch`'s shape: the preference is saved, the daemon's
    plain attribute is written, and the agents snapshot is republished over
    the loop so `reply_via` / `channel` flip on the next frame rather than
    the next hook event. Nothing here blocks the AppKit thread."""
    from types import SimpleNamespace
    from dark_army_menubar import app as A

    saved = []
    scheduled = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))

    async def _wake():
        pass

    def _threadsafe(coro, loop):
        scheduled.append((coro.cr_code.co_name, loop))
        coro.close()

    monkeypatch.setattr(A.asyncio, "run_coroutine_threadsafe", _threadsafe)
    loop = object()
    instance = _action_app(_loop=loop)
    instance._daemon = SimpleNamespace(typed_reply_enabled=False, _wake_surfaces=_wake)

    instance._set_typed_reply(True)
    assert saved == [{"typed_reply": True}]
    assert instance._settings["typed_reply"] is True
    assert instance._daemon.typed_reply_enabled is True
    assert scheduled == [("_wake", loop)]

    instance._set_typed_reply(False)
    assert saved[-1] == {"typed_reply": False}
    assert instance._daemon.typed_reply_enabled is False
    assert len(scheduled) == 2


def test_set_typed_reply_without_a_loop_or_daemon_still_saves(monkeypatch):
    from dark_army_menubar import app as A

    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    instance = _action_app(_loop=None)
    instance._set_typed_reply(True)
    assert saved == [{"typed_reply": True}]
    assert instance._settings["typed_reply"] is True


def test_set_typed_reply_is_a_panel_action(monkeypatch):
    from dark_army_menubar import app as A

    assert "set_typed_reply" in A.BobCompanionApp.PANEL_ACTIONS
    written = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: written.append(updates))
    pushed = []
    instance = _action_app(_loop=None)
    instance._push_panel_context = lambda: pushed.append(True)
    _dispatch(instance, "set_typed_reply", True)
    assert written == [{"typed_reply": True}]
    assert pushed == [True]


def test_typed_reply_rides_the_settings_block_off_by_default(monkeypatch):
    """The panel's tick reads `settings["typed_reply"]` off the context, and
    an installed `preferences.json` that never heard of the key reads false."""
    from dark_army_menubar import app as A
    import inspect

    src = inspect.getsource(A.BobCompanionApp.__init__)
    assert '"typed_reply": bool(prefs.get("typed_reply", False))' in src
    src_main = inspect.getsource(A.BobCompanionApp)
    assert "self._daemon.typed_reply_enabled = bool(" in src_main


# --- Which model each agent runs on: the setter, the push, the tables -------

class _ModelDaemon:
    def __init__(self):
        self.globals = []
        self.overrides = []

    def set_agent_models(self, mapping):
        self.globals.append(mapping)

    def set_agent_model_override(self, root, mapping):
        self.overrides.append((root, mapping))


def test_a_valid_agent_model_press_saves_the_global_table_and_feeds_the_daemon(
        monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _ModelDaemon()
    instance = _action_app(_daemon=daemon)
    resynced = []
    instance._resync_agent_packs = lambda: resynced.append(True)
    instance._settings["agent_models"] = {"claude": {"planner": "sonnet"}}
    instance._settings["agent_models_by_root"] = {
        "/tmp/p": {"grok": {"main": "grok-4.5"}}}

    _dispatch(instance, "set_agent_model",
              {"provider": "codex", "slot": "main", "model": "gpt-6-sol"})
    _dispatch(instance, "set_agent_model",
              {"provider": "codex", "slot": "worker", "model": "gpt-6-luna"})

    expected = {"claude": {"planner": "sonnet"},
                "codex": {"main": "gpt-6-sol", "worker": "gpt-6-luna"}}
    assert instance._settings["agent_models"] == expected
    assert instance._settings["agent_models_by_root"] == {
        "/tmp/p": {"grok": {"main": "grok-4.5"}}}
    assert saved == [
        {"agent_models": {"claude": {"planner": "sonnet"},
                          "codex": {"main": "gpt-6-sol"}}},
        {"agent_models": expected},
    ]
    assert daemon.globals == [
        {"claude": {"planner": "sonnet"},
         "codex": {"main": "gpt-6-sol"}},
        expected,
    ]
    assert daemon.overrides == []
    assert resynced == [True, True]


def test_a_valid_agent_model_press_with_a_root_writes_the_override_map(
        monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _ModelDaemon()
    instance = _action_app(_daemon=daemon)
    instance._settings["agent_models"] = {"claude": {"planner": "sonnet"}}

    _dispatch(instance, "set_agent_model",
              {"provider": "grok", "slot": "main", "model": "grok-4.5",
               "root": "/tmp/proj"})

    expected = {"/tmp/proj": {"grok": {"main": "grok-4.5"}}}
    assert instance._settings["agent_models_by_root"] == expected
    assert saved == [{"agent_models_by_root": expected}]
    assert daemon.overrides == [("/tmp/proj", {"grok": {"main": "grok-4.5"}})]
    # The global table is not touched by a project press.
    assert instance._settings["agent_models"] == {"claude": {"planner": "sonnet"}}
    assert daemon.globals == []


def test_an_off_list_agent_model_press_writes_nothing_and_calls_nothing(
        monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _ModelDaemon()
    instance = _action_app(_daemon=daemon)

    for value in (
        {"provider": "claude", "slot": "main", "model": "made-up"},
        {"provider": "codex", "slot": "main", "model": "gpt-4-retired"},
        {"provider": "gemini", "slot": "main", "model": ""},
        {"provider": "claude", "slot": "app-reviewer", "model": "opus"},
        {"provider": "claude", "slot": "main", "model": "inherit"},   # no root
        "not a dict",
    ):
        _dispatch(instance, "set_agent_model", value)

    assert saved == []
    assert daemon.globals == [] and daemon.overrides == []
    assert "agent_models" not in instance._settings
    assert "agent_models_by_root" not in instance._settings


def test_inherit_removes_a_projects_entry_and_prunes_the_map(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _ModelDaemon()
    instance = _action_app(_daemon=daemon)
    instance._settings["agent_models_by_root"] = {
        "/tmp/proj": {"claude": {"planner": "opus", "main": "sonnet"}}}

    _dispatch(instance, "set_agent_model",
              {"provider": "claude", "slot": "planner", "model": "inherit",
               "root": "/tmp/proj"})
    assert instance._settings["agent_models_by_root"] == {
        "/tmp/proj": {"claude": {"main": "sonnet"}}}
    assert daemon.overrides[-1] == ("/tmp/proj", {"claude": {"main": "sonnet"}})

    _dispatch(instance, "set_agent_model",
              {"provider": "claude", "slot": "main", "model": "inherit",
               "root": "/tmp/proj"})
    # The last slot gone: no empty provider, no empty root left behind.
    assert instance._settings["agent_models_by_root"] == {}
    assert saved[-1] == {"agent_models_by_root": {}}
    assert daemon.overrides[-1] == ("/tmp/proj", None)


def test_the_panel_context_carries_the_resolved_table_options_and_slots(
        monkeypatch):
    from dark_army_daemon import agent_models
    from dark_army_menubar import app as A, pack_render
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    monkeypatch.setattr(A, "macwhisper_installed", lambda: False)
    app = _context_app()
    app._settings["agent_models"] = {"claude": {"planner": "sonnet"}}
    app._settings["agent_models_by_root"] = {"/tmp/p": {"grok": {"main": "grok-4.5"}}}
    app._push_panel_context()
    settings = app._panel.contexts[-1]["settings"]
    # Resolved, not raw: every provider and slot present, stored over shipped.
    table = settings["agent_models"]
    assert set(table) == set(agent_models.PROVIDERS)
    assert table["claude"]["planner"] == "sonnet"
    assert table["claude"]["implementer"] == agent_models.SHIPPED["claude"]["implementer"]
    assert table["codex"] == agent_models.SHIPPED["codex"]
    assert settings["agent_models_by_root"] == {"/tmp/p": {"grok": {"main": "grok-4.5"}}}
    options = settings["agent_model_options"]
    # The settings chips are this table. grok-4.7 leads every grok slot.
    for names in options["grok"].values():
        assert names[0] == "grok-4.7"
    for provider in agent_models.PROVIDERS:
        for slot in agent_models.SLOTS:
            assert options[provider][slot] == list(agent_models.allowed(provider, slot))
    assert settings["agent_model_slots"] == agent_models.slots_for(
        pack_render.shipped_roles(), worker=pack_render.ships_shunt())
    # The shunt worker rides only because the shipped pack carries the skill.
    assert "worker" in settings["agent_model_slots"]
    # Every profile ships the whole crew, the two reviewers included.
    assert "integration-reviewer" in settings["agent_model_slots"]
    assert "security-reviewer" in settings["agent_model_slots"]
    assert settings["agent_model_slots"][0] == "main"


def test_the_panel_context_resolves_an_absent_table_to_shipped():
    from dark_army_daemon import agent_models
    app = _context_app()
    app._push_panel_context()
    settings = app._panel.contexts[-1]["settings"]
    assert settings["agent_models"] == agent_models.SHIPPED
    assert settings["agent_models_by_root"] == {}


def test_set_agent_model_is_a_panel_verb_and_not_a_phone_one():
    from dark_army_menubar.app import BobCompanionApp
    from dark_army_daemon.daemon import PHONE_PREFERENCES
    assert "set_agent_model" in BobCompanionApp.PANEL_ACTIONS
    assert "agent_model" not in BobCompanionApp.PREFERENCE_REQUESTS
    assert "agent_models" not in BobCompanionApp.PREFERENCE_REQUESTS
    assert "agent_model" not in PHONE_PREFERENCES
    assert len(PHONE_PREFERENCES) == 2


def test_the_stored_tables_are_what_the_startup_feed_hands_the_daemon(
        monkeypatch):
    from dark_army_menubar import app as A
    monkeypatch.setattr(A, "save_preferences", lambda updates: None)
    daemon = _ModelDaemon()
    instance = _action_app(_daemon=daemon)
    _dispatch(instance, "set_agent_model",
              {"provider": "codex", "slot": "verifier", "model": "gpt-5.5"})
    _dispatch(instance, "set_agent_model",
              {"provider": "codex", "slot": "verifier", "model": "gpt-6-astra",
               "root": "/x"})
    fed = []
    daemon.set_agent_models = lambda m: fed.append(("global", m))
    daemon.set_agent_model_overrides = lambda m: fed.append(("overrides", m))
    daemon.set_agent_models(instance._settings.get("agent_models", {}))
    daemon.set_agent_model_overrides(
        instance._settings.get("agent_models_by_root", {}))
    assert fed == [
        ("global", {"codex": {"verifier": "gpt-5.5"}}),
        ("overrides", {"/x": {"codex": {"verifier": "gpt-6-astra"}}}),
    ]


class _EffortDaemon:
    def __init__(self):
        self.globals = []
        self.overrides = []

    def set_agent_efforts(self, mapping):
        self.globals.append(mapping)

    def set_agent_effort_override(self, root, mapping):
        self.overrides.append((root, mapping))


def test_a_valid_agent_effort_press_saves_and_feeds_the_daemon(monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _EffortDaemon()
    instance = _action_app(_daemon=daemon)
    resynced = []
    instance._resync_agent_packs = lambda: resynced.append(True)
    _dispatch(instance, "set_agent_effort",
              {"provider": "claude", "slot": "verifier", "effort": "low"})
    assert instance._settings["agent_efforts"] == {"claude": {"verifier": "low"}}
    assert saved == [{"agent_efforts": {"claude": {"verifier": "low"}}}]
    assert daemon.globals == [{"claude": {"verifier": "low"}}]
    assert daemon.overrides == []
    assert resynced == [True]


def test_a_project_agent_effort_press_writes_the_override_map_and_inherit_removes(
        monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _EffortDaemon()
    instance = _action_app(_daemon=daemon)
    instance._resync_agent_packs = lambda: None
    _dispatch(instance, "set_agent_effort",
              {"provider": "grok", "slot": "main", "effort": "minimal",
               "root": "/tmp/proj"})
    assert instance._settings["agent_efforts_by_root"] == {
        "/tmp/proj": {"grok": {"main": "minimal"}}}
    assert daemon.overrides == [("/tmp/proj", {"grok": {"main": "minimal"}})]
    assert daemon.globals == []
    _dispatch(instance, "set_agent_effort",
              {"provider": "grok", "slot": "main", "effort": "inherit",
               "root": "/tmp/proj"})
    assert instance._settings["agent_efforts_by_root"] == {}
    assert daemon.overrides[-1] == ("/tmp/proj", None)


def test_an_off_list_agent_effort_press_writes_nothing_and_calls_nothing(
        monkeypatch):
    from dark_army_menubar import app as A
    saved = []
    monkeypatch.setattr(A, "save_preferences", lambda updates: saved.append(updates))
    daemon = _EffortDaemon()
    instance = _action_app(_daemon=daemon)
    # codex/planner resolves to gpt-6-astra (has max); gpt-5.5 does not.
    instance._settings["agent_models"] = {"codex": {"verifier": "gpt-5.5"}}
    for value in (
        {"provider": "claude", "slot": "main", "effort": "ultra"},
        {"provider": "grok", "slot": "main", "effort": "max"},
        {"provider": "codex", "slot": "verifier", "effort": "max"},
        {"provider": "claude", "slot": "card-preparer", "effort": "low"},
        {"provider": "claude", "slot": "worker", "effort": "low"},
        {"provider": "gemini", "slot": "main", "effort": ""},
        {"provider": "claude", "slot": "main", "effort": "inherit"},  # no root
        "not a dict",
    ):
        _dispatch(instance, "set_agent_effort", value)
    assert saved == []
    assert daemon.globals == [] and daemon.overrides == []
    assert "agent_efforts" not in instance._settings
    assert "agent_efforts_by_root" not in instance._settings


def test_the_panel_context_carries_the_effort_tables_options_and_slots(
        monkeypatch):
    from dark_army_daemon import agent_models
    from dark_army_menubar import app as A, pack_render
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    monkeypatch.setattr(A, "macwhisper_installed", lambda: False)
    app = _context_app()
    app._settings["agent_models"] = {"codex": {"verifier": "gpt-5.5"}}
    app._settings["agent_efforts"] = {"claude": {"planner": "low"}}
    app._settings["agent_efforts_by_root"] = {"/tmp/p": {"grok": {"main": "low"}}}
    app._push_panel_context()
    settings = app._panel.contexts[-1]["settings"]
    table = settings["agent_efforts"]
    assert table["claude"]["planner"] == "low"
    assert table["codex"]["planner"] == "high"
    assert settings["agent_efforts_by_root"] == {"/tmp/p": {"grok": {"main": "low"}}}
    options = settings["agent_effort_options"]
    assert "max" in options["codex"]["planner"]
    assert "max" not in options["codex"]["verifier"]   # gpt-5.5 resolved
    assert set(options["claude"]) == set(agent_models.EFFORT_SLOTS)
    slots = settings["agent_effort_slots"]
    assert slots == agent_models.effort_slots_for(pack_render.shipped_roles())
    assert "card-preparer" not in slots and "worker" not in slots
    assert slots[0] == "main"


def test_the_panel_context_publishes_model_checked_and_per_root_effort_views(
        monkeypatch):
    from dark_army_menubar import app as A
    monkeypatch.setattr(A, "ax_is_process_trusted", lambda: True)
    monkeypatch.setattr(A, "macwhisper_installed", lambda: False)
    app = _context_app()
    app._settings["agent_models"] = {"codex": {"verifier": "gpt-5.5"}}
    app._settings["agent_models_by_root"] = {
        "/tmp/p": {"codex": {"verifier": "gpt-6-luna"}}}
    app._settings["agent_efforts"] = {"codex": {"verifier": "max"}}
    app._settings["agent_efforts_by_root"] = {
        "/tmp/p": {"codex": {"verifier": "max"}}}
    app._push_panel_context()
    settings = app._panel.contexts[-1]["settings"]
    # Stored `max`, global model gpt-5.5: published as Default, not dropped.
    assert settings["agent_efforts"]["codex"]["verifier"] == ""
    root = "/tmp/p"
    # The project's own model offers max, so its entry stands and its options
    # carry it, while the machine-wide options do not.
    assert settings["agent_efforts_by_root"][root]["codex"]["verifier"] == "max"
    assert "max" in settings["agent_effort_options_by_root"][root]["codex"]["verifier"]
    assert "max" not in settings["agent_effort_options"]["codex"]["verifier"]


def test_set_agent_effort_is_a_panel_verb_and_not_a_phone_one():
    from dark_army_menubar.app import BobCompanionApp
    from dark_army_daemon.daemon import PHONE_PREFERENCES
    assert "set_agent_effort" in BobCompanionApp.PANEL_ACTIONS
    assert "agent_effort" not in BobCompanionApp.PREFERENCE_REQUESTS
    assert "agent_efforts" not in BobCompanionApp.PREFERENCE_REQUESTS
    assert "agent_effort" not in PHONE_PREFERENCES


# --- The strip's clock ---------------------------------------------------------

class FakeStripTimer:
    """Stands in for the strip's `rumps.Timer`. Setting `interval` on a live
    timer raises: rumps silently drops it inside the first interval, so the
    trap is a hard failure here rather than a quiet one on the menu bar."""

    def __init__(self, interval, alive=True):
        self._interval = interval
        self._alive = alive
        self.calls = []

    @property
    def interval(self):
        return self._interval

    @interval.setter
    def interval(self, value):
        if self._alive:
            raise AssertionError("interval set on a live timer")
        self._interval = value

    def is_alive(self):
        return self._alive

    def start(self):
        self.calls.append("start")
        self._alive = True

    def stop(self):
        self.calls.append("stop")
        self._alive = False


def _strip_app(working=0, idle=0, attention=0, subagents=0, alive=True,
               timer=None):
    from dark_army_menubar.app import BobCompanionApp
    app = object.__new__(BobCompanionApp)
    if alive:
        app._daemon_thread = MagicMock(is_alive=lambda: True)
    app._working_count = working
    app._idle_count = idle
    app._attention_count = attention
    app._subagent_count = subagents
    app._limits = None
    app._anim_i = 0
    app._strip_key = None
    app._strip_level = 0
    app._compose_strip = MagicMock(return_value=[("idle", str(idle), "")])
    app._render_strip = MagicMock(return_value=10.0)
    if timer is not None:
        app._strip_timer = timer
    return app


def test_strip_clock_rests_when_nothing_animates():
    """Nobody working: after a tick the clock is on the resting cadence, and a
    second resting tick hands the render the very same strip unmeasured, which
    is what `_render_strip`'s signature skip turns into no repaint."""
    from dark_army_menubar.app import BobCompanionApp, ICON_TICK, ICON_TICK_REST
    timer = FakeStripTimer(ICON_TICK)
    app = _strip_app(working=0, idle=2, attention=1, timer=timer)

    BobCompanionApp._animate_icon(app, None)
    assert timer.is_alive() and timer.interval == ICON_TICK_REST
    assert ICON_TICK_REST >= 1.0          # at most once a second

    first = app._render_strip.call_args
    app._render_strip.reset_mock()
    timer.calls.clear()
    BobCompanionApp._animate_icon(app, None)
    assert app._render_strip.call_args.args == first.args
    assert app._render_strip.call_args.kwargs["measure"] is False
    assert timer.calls == []              # already resting: not re-armed


def test_strip_clock_runs_fast_while_working():
    from dark_army_menubar.app import BobCompanionApp, ICON_TICK, ICON_TICK_REST
    timer = FakeStripTimer(ICON_TICK_REST)
    app = _strip_app(working=1, timer=timer)

    BobCompanionApp._animate_icon(app, None)
    assert timer.is_alive() and timer.interval == ICON_TICK
    assert ICON_TICK == 0.2


def test_offline_strip_rests():
    from dark_army_menubar.app import BobCompanionApp, ICON_TICK, ICON_TICK_REST
    timer = FakeStripTimer(ICON_TICK)
    app = _strip_app(working=3, alive=False, timer=timer)

    BobCompanionApp._animate_icon(app, None)
    app._render_strip.assert_called_once_with([("off", "", "")], measure=False)
    assert timer.is_alive() and timer.interval == ICON_TICK_REST


def test_activity_change_wakes_the_strip_on_the_main_thread(monkeypatch):
    """A session starting work hops to the main thread, is drawn at once and
    puts the clock back on the fast cadence without waiting for the slow tick.
    Unchanged counts hop nowhere."""
    from dark_army_menubar import app as A
    queued = []
    monkeypatch.setattr(A, "callAfter", lambda fn, *args: queued.append((fn, args)))
    timer = FakeStripTimer(A.ICON_TICK_REST)
    app = _strip_app(timer=timer)

    A.BobCompanionApp.on_activity_change(app, 1, 0, 0, 0)
    assert len(queued) == 1
    fn, args = queued[0]
    assert fn == app._strip_wake and args == ()
    assert timer.calls == []              # the daemon thread touched no timer

    fn(*args)
    assert app._render_strip.call_count == 1
    assert timer.is_alive() and timer.interval == A.ICON_TICK
    assert timer.calls == ["stop", "start"]

    queued.clear()
    A.BobCompanionApp.on_activity_change(app, 1, 0, 0, 0)
    assert queued == []


def test_strip_clock_re_arms_by_stop_then_start():
    from dark_army_menubar.app import BobCompanionApp, ICON_TICK, ICON_TICK_REST
    timer = FakeStripTimer(ICON_TICK_REST)
    app = _strip_app(timer=timer)

    BobCompanionApp._strip_clock_set(app, ICON_TICK)
    assert timer.calls == ["stop", "start"]
    assert timer.is_alive() and timer.interval == ICON_TICK

    timer.calls.clear()
    BobCompanionApp._strip_clock_set(app, ICON_TICK)
    assert timer.calls == []

    stopped = FakeStripTimer(ICON_TICK, alive=False)
    app._strip_timer = stopped
    BobCompanionApp._strip_clock_set(app, ICON_TICK_REST)
    assert stopped.calls == ["start"]
    assert stopped.is_alive() and stopped.interval == ICON_TICK_REST


def test_strip_animating_follows_the_freeze_table(monkeypatch):
    from dark_army_menubar import app as A
    working = _strip_app(working=1)
    waiting = _strip_app(attention=1)
    assert A.BobCompanionApp._strip_animating(working) is True
    assert A.BobCompanionApp._strip_animating(waiting) is False

    monkeypatch.setitem(A.STILL_FRAMES, "work", 0)
    assert A.BobCompanionApp._strip_animating(working) is False

    monkeypatch.delitem(A.STILL_FRAMES, "attn")
    assert A.BobCompanionApp._strip_animating(waiting) is True


def test_animate_icon_without_a_timer_is_harmless():
    from dark_army_menubar.app import BobCompanionApp
    app = _strip_app(working=1)
    assert not hasattr(app, "_strip_timer")

    BobCompanionApp._animate_icon(app, None)
    assert app._render_strip.call_count == 1


def test_strip_clock_lives_on_the_instance():
    """rumps starts decorated timers itself and hands the instance no handle,
    so a decorated `_animate_icon` could never be re-armed."""
    import rumps
    from dark_army_menubar.app import BobCompanionApp
    registered = [t.callback for t in rumps.timer.__dict__.get("*timers", [])]
    assert BobCompanionApp._animate_icon not in registered
    assert registered, "the 30s timers should still be decorated"


# The desk token rides the context push, and only the context push
# (`docs/transport-contract.md`, *The loopback door has two tokens*).

class _DeskDaemon:
    def __init__(self, token="desk-secret-value"):
        self.token = token

    def desk_token(self):
        return self.token


def test_panel_context_carries_the_desk_token():
    daemon = _DeskDaemon()
    app = _context_app(_daemon=daemon)
    app._push_panel_context()
    assert app._panel.contexts[-1]["desk_token"] == daemon.desk_token()
    # Never inside the settings dict, which other code reads and logs.
    assert "desk_token" not in app._panel.contexts[-1]["settings"]


def test_panel_context_desk_token_is_empty_before_the_daemon():
    app = _context_app(_daemon=None)
    app._push_panel_context()
    assert app._panel.contexts[-1]["desk_token"] == ""


def test_the_desk_token_reaches_no_log_line(caplog):
    """Across a push through the real `PanelProcess` send path — `_send`,
    `_enqueue` and the sender thread's `_drain_outbox` write — the value
    rides the pipe and appears in no log record."""
    import io
    import json
    import logging
    import threading
    import time
    from collections import deque
    from dark_army_menubar import panel_process as PP

    secret = "desk-secret-never-logged"
    panel = object.__new__(PP.PanelProcess)
    panel._lock = threading.Lock()
    panel._outbox = deque()
    panel._outbox_cond = threading.Condition()
    panel._alive = lambda: True
    panel._spawn = lambda: True
    panel._path = "/stub/BobPanel"
    written = io.BytesIO()

    class _Proc:
        stdin = written

    app = _context_app(_daemon=_DeskDaemon(secret))
    app._panel = panel
    stop = threading.Event()
    with caplog.at_level(logging.DEBUG):
        app._push_panel_context()
        writer = threading.Thread(
            target=panel._drain_outbox,
            args=(_Proc, panel._outbox, panel._outbox_cond, stop))
        writer.start()
        deadline = time.monotonic() + 5
        while secret.encode() not in written.getvalue():
            assert time.monotonic() < deadline, "the line never reached the pipe"
            time.sleep(0.01)
        stop.set()
        with panel._outbox_cond:
            panel._outbox_cond.notify_all()
        writer.join(5)
    assert json.loads(written.getvalue().splitlines()[0])["desk_token"] == secret
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in str(record.args)


def test_context_refresh_pushes_the_context_again(caplog):
    import logging
    pushed = []
    instance = _action_app()
    instance._push_panel_context = lambda: pushed.append(True)
    with caplog.at_level(logging.WARNING):
        _dispatch(instance, "context_refresh")
    assert pushed == [True]
    assert not any("Ignoring unknown" in r.getMessage() for r in caplog.records)
