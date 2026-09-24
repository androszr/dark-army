"""Synthetic tests for Grok permission dialog choices."""
from dark_army_daemon.prompt_dialogs import grok_choice


def test_allows_once():
    text = """\
Permission required
1. Allow once
2. Always allow
3. Reject once
"""
    assert grok_choice(text, "allow") == "1"


def test_rejects_once():
    text = """\
Permission required
1. Allow once
2. Always allow
3. Reject once
"""
    assert grok_choice(text, "deny") == "3"


def test_renumbered_options_are_matched_by_label():
    text = """\
Permission required
1. Always allow
2. Reject once
3. Allow once
"""
    assert grok_choice(text, "allow") == "3"
    assert grok_choice(text, "deny") == "2"


def test_always_allow_is_refused():
    text = """\
Permission required
1. Always allow: touch /tmp/file
2. Always allow
3. Reject once
"""
    assert grok_choice(text, "allow") is None


def test_missing_dialog_is_refused():
    assert grok_choice("No permission prompt is present.", "allow") is None


def test_duplicate_option_is_refused():
    text = """\
Permission required
1. Allow once
2. Allow once
3. Reject once
"""
    assert grok_choice(text, "allow") is None


def test_reads_the_real_screen_grid_rows():
    """The daemon hands over `vtgrid.Screen.text()`, a list of rows."""
    from dark_army_daemon.vtgrid import Screen

    screen = Screen(80, 24)
    screen.feed(b"Permission required\r\n1. Allow once\r\n"
                b"2. Always allow\r\n3. Reject once\r\n")
    assert grok_choice(screen.text(), "allow") == "1"
    assert grok_choice(screen.text(), "deny") == "3"
