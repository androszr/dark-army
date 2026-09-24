"""The phone's top bar: a plus for new cards, a profile screen holding un-pair.

Pins for `plans/2026-08-31-phone-top-bar-plus-and-profile.md`. The top bar's
`⋯` menu is gone and with it the bar's own forget dialog; two buttons sit at
the trailing end of every tab's bar — plus first, person rightmost — each a
44×44 target; the board's `+ NEW` chip is gone so the composer has exactly one
entry point; and the profile screen carries what the menu used to (Forget this
Mac, behind the same are-you-sure) plus the pairing's addresses, the connection
state and a ticking last-heard line.

A **Python** lint over the Swift source, `test_phone_theme_drift.py`'s
pattern: `ios/` has no test target by explicit decision, and `cd host &&
.venv/bin/pytest` is the suite that actually runs on this machine.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BRAND_BAR = ROOT / "ios" / "BobPhone" / "BrandBar.swift"
APP = ROOT / "ios" / "BobPhone" / "BobPhoneApp.swift"
BOARD = ROOT / "ios" / "BobPhone" / "BoardView.swift"
PROFILE = ROOT / "ios" / "BobPhone" / "ProfileView.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"
PHONE_DIR = ROOT / "ios" / "BobPhone"

# The dialog wording, moved verbatim from the old bar menu — a person who has
# read one confirmation must recognise the other.
DIALOG_MESSAGE = "The phone forgets its key; pair again from the Mac's Devices menu."


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


def _tab_root_segment() -> str:
    """The `PhoneTabRoot` struct's own source, not the whole file."""
    text = _read(APP)
    match = re.search(r"struct PhoneTabRoot\b.*", text, re.S)
    assert match, "no PhoneTabRoot struct in BobPhoneApp.swift"
    return match.group(0)


def _content_view_segment() -> str:
    """`ContentView`'s source, ending where `PhoneTabRoot` begins."""
    text = _read(APP)
    match = re.search(r"struct ContentView\b.*?(?=struct PhoneTabRoot\b)", text, re.S)
    assert match, "ContentView must precede PhoneTabRoot in BobPhoneApp.swift"
    return match.group(0)


# ---------------------------------------------------------------- the bar


def test_the_ellipsis_menu_is_gone_from_the_bar():
    text = _read(BRAND_BAR)
    assert "ellipsis" not in text
    assert "Menu {" not in text
    assert "confirmingForget" not in text
    assert "confirmationDialog" not in text


def test_the_bar_no_longer_knows_how_to_forget():
    text = _read(BRAND_BAR)
    assert "Forget this Mac" not in text
    assert "onForget" not in text


def test_the_bar_has_exactly_one_plus_and_one_person():
    text = _read(BRAND_BAR)
    assert text.count('systemName: "plus"') == 1
    assert text.count('systemName: "person"') == 1


def test_plus_sits_left_of_person():
    # Source order inside one HStack is screen order: the plus is emitted
    # first, so it sits immediately left; the person button is rightmost.
    text = _read(BRAND_BAR)
    assert text.index('systemName: "plus"') < text.index('systemName: "person"')


def test_both_buttons_are_finger_sized():
    # Two full 44×44 targets — the fixed frames never compress; the
    # `PromptLine`'s own `.lineLimit(1)` is what gives on a crowded bar.
    text = _read(BRAND_BAR)
    assert len(re.findall(r"width: 44, height: 44", text)) == 2


def test_the_bar_navigates_nothing_itself():
    # The bar takes two closures and flips no navigation state of its own —
    # the per-tab booleans live in PhoneTabRoot, never in bar chrome.
    text = _read(BRAND_BAR)
    assert "let onCompose: () -> Void" in text
    assert "let onProfile: () -> Void" in text
    assert "NavigationLink" not in text
    assert "navigationDestination" not in text


# ---------------------------------------------------------------- the tabs


def test_the_tab_root_owns_both_destinations():
    segment = _tab_root_segment()
    assert segment.count(".navigationDestination(isPresented:") == 2
    # The composer has gained arguments twice now — the offline outbox, then
    # the tab a banked draft reopens on. What this pin cares about is that the
    # tab root is what builds it, so it matches the call's head, not its full
    # signature.
    assert "ComposerView(client: client, outbox: outbox" in segment
    assert "ProfileView(client: client, pairing: pairing" in segment


def test_the_booleans_are_per_tab_not_shared():
    # `@State` on ContentView would be one switch shared by four stacks —
    # setting it would push the destination in every tab at once.
    segment = _tab_root_segment()
    assert "@State private var composing = false" in segment
    assert "@State private var showingProfile = false" in segment
    content_view = _content_view_segment()
    # Named rather than blanket: `ContentView` legitimately owns the tab
    # selection now, so that a resumed draft can bring its own tab forward.
    # These two specifically must not live there.
    assert "composing" not in content_view
    assert "showingProfile" not in content_view
    assert "navigationDestination" not in content_view


def test_every_tab_gets_the_same_chrome():
    content_view = _content_view_segment()
    for path in ("~/needs", "~/fleet", "~/board", "~/comm", "~/usage"):
        assert f'PhoneTabRoot(path: "{path}"' in content_view, path
    app = _read(APP)
    match = re.search(r"enum PhoneTab.*?\n\}", app, re.S)
    assert match, "no enum PhoneTab"
    chunk = match.group(0)
    assert chunk.count("case ") == 1
    assert "case needs, fleet, board, comm, usage" in chunk


def test_forget_stops_the_poller_before_clearing_the_record():
    # `client.stop()` first, so a pull-to-refresh on a screen being torn
    # down cannot reach the Mac.
    text = _read(APP)
    match = re.search(r"func forget\(\)\s*\{(.*?)\}", text, re.S)
    assert match, "no forget() on ContentView"
    body = match.group(1)
    assert body.index("client.stop()") < body.index("pairing.clear()")


# ---------------------------------------------------------------- the board


def test_the_board_lost_its_new_chip():
    # The composer has exactly one entry point: the top bar's plus.
    text = _read(BOARD)
    assert "+ NEW" not in text
    assert "ComposerView" not in text


# ---------------------------------------------------------------- the profile


def test_the_profile_screen_exists_and_is_built():
    assert PROFILE.is_file()
    # PBXFileReference + PBXBuildFile at minimum — an unregistered file
    # compiles nothing.
    assert _read(PBXPROJ).count("ProfileView.swift") >= 2


def test_the_profile_shows_the_three_sections():
    text = _read(PROFILE)
    for title in ("THIS PHONE", "PAIRED MAC", "CONNECTION"):
        assert f'"{title}"' in text, title


def test_the_profile_shows_the_live_address_and_identity():
    text = _read(PROFILE)
    # `promote` rewrites `record.host`, so showing it is showing the live
    # polling address; the record stores no device name to prefer.
    assert "record.host" in text
    assert "record.hosts" in text
    assert "record.deviceId" in text
    assert "UIDevice.current.name" in text


def test_the_last_heard_line_ticks_alone():
    # StaleBanner's rule: the one clock wraps only the text that changes.
    text = _read(PROFILE)
    assert "TimelineView(.periodic" in text
    assert "client.lastHeard" in text


def test_forget_moved_to_the_profile_with_its_wording():
    text = _read(PROFILE)
    assert text.count("Forget this Mac") >= 2  # row/button + dialog
    assert DIALOG_MESSAGE in text
    assert "Keep it" in text
    assert "onForget" in text


def test_the_profile_never_renders_the_pairing_secret():
    assert "token" not in _read(PROFILE)
    assert "Token" not in _read(PROFILE)


# ---------------------------------------------------------------- refusals


def test_no_manual_poll_button_appeared():
    # A "Sync now" button was offered and declined. Pull-to-refresh stays as
    # the way to nudge a poll: the refreshNow call-site count is frozen.
    # Nine today: Catch Up resolves its target after one shared refresh.
    # It was ten until `post`'s three post-write refreshes moved
    # to `refreshAfterWrite` (2 Sep 2026) — a write's refresh has to be a read
    # made *after* the write, and the plain join is not — leaving seven real
    # mentions plus the one in that helper's own doc comment. None of them is
    # a button, and `refreshNow` itself is unchanged.
    # Ten since 5 Sep 2026: the pipeline screen's own pull-to-refresh.
    count = sum(
        text.count("refreshNow")
        for path in sorted(PHONE_DIR.glob("*.swift"))
        for text in [path.read_text()]
    )
    assert count == 10, f"refreshNow appears {count} times; the frozen count is 10"
