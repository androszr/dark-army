# host/tests/test_phone_widget.py
"""The home-screen widget, pinned from the Mac's side.

Same pattern as every `test_phone_*.py`: grep-shaped assertions over the
Swift sources, because `ios/` has no test target by explicit decision. The
properties pinned are the design's own: a provider that does no network, the
75/90 usage steps shared with `menu_format`, the menu bar's to-do recipe,
an honestly stale tile, and counts hidden on the lock screen.
"""

from __future__ import annotations

import re
from pathlib import Path

from dark_army_menubar import menu_format

ROOT = Path(__file__).resolve().parents[2]

WIDGET_DIR = ROOT / "ios" / "BobPhoneWidget"
WIDGET = WIDGET_DIR / "BobPhoneWidget.swift"
VIEWS = WIDGET_DIR / "WidgetViews.swift"
WIDGET_INFO = WIDGET_DIR / "Info.plist"
WIDGET_ENTITLEMENTS = WIDGET_DIR / "BobPhoneWidget.entitlements"
APP_ENTITLEMENTS = ROOT / "ios" / "BobPhone" / "BobPhone.entitlements"
SUMMARY = ROOT / "ios" / "Shared" / "FleetSummary.swift"
CLIENT = ROOT / "ios" / "BobPhone" / "Client.swift"
REFRESH = ROOT / "ios" / "BobPhone" / "BackgroundRefresh.swift"
APP = ROOT / "ios" / "BobPhone" / "BobPhoneApp.swift"
PBXPROJ = ROOT / "ios" / "BobPhone.xcodeproj" / "project.pbxproj"

# Spelled from the one identity setting (ios/Config/Identity.xcconfig).
APP_GROUP = "group.$(DARK_ARMY_BUNDLE_ID)"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing file: {path}"
    return path.read_text()


# --- the provider does no network ----------------------------------------------


def test_no_network_anywhere_in_the_widget_target():
    """The widget draws what the app last heard, full stop. A fetch from the
    provider would need the pairing record out of a shared Keychain group
    and would spend the relay budget from a process with nobody in front of
    it."""
    for path in sorted(WIDGET_DIR.glob("**/*.swift")):
        text = path.read_text()
        assert "URLSession" not in text, path.name
        assert "URLRequest" not in text, path.name
    # The shared summary file is compiled into the widget too, so it plays
    # by the same rule.
    assert "URLSession" not in _read(SUMMARY)


def test_the_timeline_is_never_and_reloads_are_app_driven():
    text = _read(WIDGET)
    assert "policy: .never" in text
    assert "FleetSummary.load()" in text
    client = _read(CLIENT)
    assert 'reloadTimelines(ofKind: "BobFleetTile")' in client
    assert 'StaticConfiguration(kind: "BobFleetTile"' in text


def test_two_entries_now_and_a_dim_future():
    text = _read(WIDGET)
    assert "dimmed: false" in text
    assert "dimmed: true" in text
    # The dim horizon is the summary's own `dimAfter` (600 for a summary
    # written before it existed); `test_phone_background_refresh` pins how
    # the app sizes it.
    assert "addingTimeInterval(dimAfter)" in text
    assert "summary?.dimAfter ?? 600" in text


def test_no_system_large_family():
    text = _read(WIDGET)
    assert (
        ".supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular])"
        in text
    )
    assert "systemLarge" not in text
    assert "accessoryRectangular" not in text
    assert "accessoryInline" not in text


# --- the numbers are the published ones, never a second derivation --------------


def test_the_usage_steps_are_the_shared_contract():
    """The panel formats its own rows in Swift and so does the widget; the
    constants are the contract, pinned against `menu_format`'s own."""
    views = _read(VIEWS)
    assert f"warnPercent = {menu_format.USAGE_WARN_PERCENT}" in views
    assert f"critPercent = {menu_format.USAGE_CRIT_PERCENT}" in views


def test_todo_is_prep_plus_backlog_each_on_its_own():
    """The menu bar's own recipe, term by term, so one junk value cannot
    zero the other."""
    client = _read(CLIENT)
    assert '(counts["prep"] ?? 0) + (counts["backlog"] ?? 0)' in client


def test_the_summary_reads_the_daemons_counts():
    client = _read(CLIENT)
    assert "needsYou: snapshot.counts.attention" in client
    assert "working: snapshot.counts.working" in client


def test_both_poll_paths_feed_the_tile():
    client = _read(CLIENT)
    assert client.count("publishWidgetSummary()") >= 3  # 2 calls + the def


def test_the_reload_is_throttled_and_freshness_outruns_the_dim():
    """Two rungs. Changed figures reload at most once a minute. Unchanged
    figures still reload every nine minutes while check-ins continue — the
    widget's own timeline dims itself at ten, so without this rung a warm
    app polling an unchanged fleet sat behind a dimmed tile reading
    "30m ago" over numbers seconds old. The freshness interval must stay
    strictly under the dim horizon, and both halves of that horizon (the
    timeline's dim entry and the view's own stale window) are read out of
    the widget sources here rather than trusted."""
    import re

    client = _read(CLIENT)
    assert "figures != summary.figures" in client
    minimum = re.search(
        r"reloadMinSeconds: TimeInterval = (\d+)", client)
    freshness = re.search(
        r"reloadFreshnessSeconds: TimeInterval = (\d+)", client)
    assert minimum and freshness
    assert "changed ? Self.reloadMinSeconds" in client
    assert ": Self.reloadFreshnessSeconds" in client
    # The dim horizon is the summary's own `dimAfter`: its floor is the 600
    # the tile always had (`max(600, …)` in `BackgroundRefresh`), and both
    # halves of the horizon read that one field rather than a literal.
    floor = re.search(r"max\((\d+), TimeInterval\(minutes \* 60\)", _read(REFRESH))
    assert floor
    assert "addingTimeInterval(dimAfter)" in _read(WIDGET)
    assert "summary.generatedAt > summary.dimAfter" in _read(VIEWS)
    assert int(freshness.group(1)) < int(floor.group(1))
    assert int(minimum.group(1)) <= int(freshness.group(1))


def test_a_warm_return_does_not_sit_behind_a_dimmed_tile():
    """Both throttle slots survive `stop()`, so a warm return with unchanged
    numbers used to skip the reload and leave the tile on its dimmed
    +10-minute entry. Two rungs answer it: `start()` forgets the figures
    (the fresh session's first check-in counts as changed), and
    `.background` asks for one final unthrottled reload so the dim clock
    counts from the departure, not from the last throttled reload."""
    client = _read(CLIENT)
    start = client.split("func start(record: PairingRecord)")[1].split(
        "\n    }")[0]
    assert "lastReloadFigures = nil" in start
    flush = client.split("func flushWidgetReload()")[1].split("\n    }")[0]
    assert 'reloadTimelines(ofKind: "BobFleetTile")' in flush
    app = _read(APP)
    background = app.split("case .background:")[1].split(
        "@unknown default:")[0]
    assert "client.flushWidgetReload()" in background
    # After the client paused (`suspend()`, 21 Sep 2026 — `.background`
    # no longer stops it) — the final publish already happened.
    assert background.index("client.suspend()") < background.index(
        "client.flushWidgetReload()")


def test_the_counts_speak_in_sentences_not_bare_digits():
    """VoiceOver read the big number and its header as two unrelated
    utterances (or, `.privacySensitive`, nothing useful at all): every
    count line carries one label saying what the number is."""
    views = _read(VIEWS)
    assert 'accessibilityLabel("\\(summary.needsYou) need you")' in views
    assert 'accessibilityLabel("\\(summary.working) working")' in views
    # The medium tile's CountLine speaks as one element per line.
    assert ".accessibilityElement(children: .ignore)" in views
    assert '"\\(value) need you"' in views


def test_the_tile_publishes_each_providers_headline_window():
    """One bar per provider, and it is a chosen *window* rather than a
    chosen number: the first bar the daemon lists for that provider.

    `limits.cached_bars` orders every provider's block "session first, then
    the weekly windows", so the head of the block is Claude's 5h, Codex's 5h
    and Grok's week — the same figures the menu-bar strip draws. Picking the
    highest percent instead was the reported bug: the tile hopped between
    the 5h and the weekly meter as the two numbers crossed, settled on
    whichever moves least, and sat there looking frozen while the app's own
    5h figure climbed. Nothing may sort, either — the daemon's order is the
    order, exactly as `UsageView` relies on."""
    client = _read(CLIENT)
    assert "for bar in usage where headline[bar.provider] == nil {" in client
    # The old shape, gone whole: no percent comparison, no fresh/stale
    # tables, and no alphabetical re-sort behind the daemon's own order.
    assert "keepHighest" not in client
    assert "held.merging(fresh)" not in client
    assert "picked.sorted" not in client
    # The tile's label is the Usage tab's shortener, not a second rule.
    assert "UsageChipText.label(kind: bar.kind," in client


def test_an_all_stale_provider_keeps_an_admitted_stale_meter():
    """A provider whose headline window has reset still appears, drawn as
    the widget's "–" rather than vanishing — "no providers" is a different
    (and false) statement from "no fresh reading". And it is never papered
    over with a *different* window: stale means the window this figure
    measured has already reset, so the number is not old but wrong, and
    another span is not an answer to it (`menu_format.limit_percent`).

    A missing percent is the same admission, never a confident 0%."""
    client = _read(CLIENT)
    assert "where !bar.stale" not in client
    assert "stale: bar.stale || bar.percent == nil" in client
    # And the widget's admitted-stale branch is really keyed on the flag.
    views = _read(VIEWS)
    assert 'bar.stale ? "–"' in views
    assert "if !bar.stale {" in views


# --- honest staleness and the lock screen ---------------------------------------


def test_the_age_line_counts_itself_up():
    assert "style: .relative" in _read(VIEWS)


def test_counts_hide_on_the_lock_screen():
    assert _read(VIEWS).count(".privacySensitive()") >= 3


def test_the_empty_cache_says_open_bob():
    assert '"open Dark Army"' in _read(VIEWS)


def test_the_deep_links_route_through_the_scheme():
    views = _read(VIEWS)
    assert 'widgetURL(URL(string: "bobphone://needs"))' in views
    assert 'Link(destination: URL(string: "bobphone://usage")!)' in views


# --- the container and the target plumbing --------------------------------------


def test_both_entitlement_files_carry_the_app_group():
    assert APP_GROUP in _read(APP_ENTITLEMENTS)
    assert APP_GROUP in _read(WIDGET_ENTITLEMENTS)
    # The code reads the group from each target's Info.plist, where the
    # build expands the same setting, instead of spelling it itself.
    summary = _read(SUMMARY)
    assert 'forInfoDictionaryKey: "DarkArmyAppGroup"' in summary
    assert "robertandrosz" not in summary
    for plist in ("BobPhone", "BobPhoneWidget", "BobPhoneNotification"):
        text = _read(ROOT / "ios" / plist / "Info.plist")
        assert f"<key>DarkArmyAppGroup</key>\n\t<string>{APP_GROUP}</string>" in text


def test_the_widget_entitlements_carry_nothing_else():
    text = _read(WIDGET_ENTITLEMENTS)
    assert "aps-environment" not in text
    assert "keychain-access-groups" not in text


def test_the_app_entitlements_carry_push():
    assert "aps-environment" in _read(APP_ENTITLEMENTS)


def test_the_extension_point_is_widgetkit():
    assert "com.apple.widgetkit-extension" in _read(WIDGET_INFO)


def test_the_pbxproj_embeds_the_extension():
    pbx = _read(PBXPROJ)
    assert 'productType = "com.apple.product-type.app-extension";' in pbx
    assert "dstSubfolderSpec = 13;" in pbx
    assert "Embed Foundation Extensions" in pbx
    # The shared summary is compiled into both targets — two build files
    # over one file reference.
    assert pbx.count("FleetSummary.swift in Sources") >= 4  # 2 defs + 2 uses
    assert pbx.count(
        "CODE_SIGN_ENTITLEMENTS = BobPhoneWidget/BobPhoneWidget.entitlements;"
    ) == 2


# --- the brand header -----------------------------------------------------------


def _phase_body(pbx: str, marker: str) -> str:
    """The body of one build phase, by its id-and-name marker.

    Each id appears twice — once in the target's `buildPhases` list, once as
    the section definition — so only the last segment is the phase itself.
    """
    return pbx.split(marker)[-1].split("runOnlyForDeploymentPostprocessing")[0]


def test_the_widget_bundles_the_brand_folder():
    # In an appex `Bundle.main` is the appex, so the mark is only findable
    # if the folder is in the *widget's* Resources phase, not just the app's.
    pbx = _read(PBXPROJ)
    assert pbx.count("brand in Resources") == 4  # 2 defs + 2 uses
    body = _phase_body(pbx, "7B0B0E1A0000000000000133 /* Resources */")
    assert "brand in Resources" in body


def test_the_mark_loads_through_the_appex_bundle():
    views = _read(VIEWS)
    assert 'Bundle.main.url(forResource: "brand"' in views
    assert "fsociety-mark-widget.png" in views
    assert (ROOT / "ios" / "BobPhone" / "Resources" / "brand"
            / "fsociety-mark-widget.png").is_file()


def test_the_prompt_line_matches_the_brand_bars():
    views = _read(VIEWS)
    assert '"root@darkarmy:"' in views
    assert '"root@bob:"' not in views
    assert '"~/board"' in views
    assert '"\\(summary.needsYou) need you"' in views
    assert '"Companion"' in views
    assert '"stale"' in views


def test_the_header_adds_no_second_voice():
    # Exactly two hidden elements: the mark itself, and the medium header
    # row — whose words the count lines below already speak. A third would
    # silence something that has no other voice; a missing one doubles
    # "3 need you".
    views = _read(VIEWS)
    assert views.count("accessibilityHidden(true)") == 2
    assert ".accessibilityElement(children: .combine)" in views


def test_the_prompt_count_redacts_on_the_lock_screen():
    views = _read(VIEWS)
    assert "sensitive: Bool = false" in views
    assert '("\\(summary.needsYou) need you", WidgetTheme.attention, true)' in views
    assert views.count(".privacySensitive()") >= 5


def test_the_widget_still_restates_rather_than_importing_the_theme():
    body = _phase_body(_read(PBXPROJ), "7B0B0E1A0000000000000131 /* Sources */")
    assert "Theme.swift" not in body


def test_the_widget_compiles_the_apps_generated_signal_tokens():
    pbx = _read(PBXPROJ)
    body = _phase_body(pbx, "7B0B0E1A0000000000000131 /* Sources */")
    assert "A51A1D000000000000000005 /* SignalTokens.generated.swift in Sources */" in body
    assert "A51A1D000000000000000001 /* SignalTokens.generated.swift */" in pbx
    assert "A51A1D000000000000000005 /* SignalTokens.generated.swift in Sources */ = {isa = PBXBuildFile; fileRef = A51A1D000000000000000001" in pbx
    tests_body = _phase_body(pbx, "DEC1510B000000000000003E /* Sources */")
    assert "A51A1D000000000000000006 /* SignalTokens.generated.swift in Sources */" in tests_body
    assert not (WIDGET_DIR / "SignalTokens.generated.swift").exists()


def test_widget_signal_roles_keep_attention_and_failures_distinct():
    views = _read(VIEWS)
    theme = views.split("enum WidgetTheme {")[1].split("struct FleetWidgetView")[0]
    assert "Color(red:" not in theme
    for role in ("surface", "text", "muted", "accent", "attention", "danger", "line"):
        assert f"SignalTokens.{role}" in theme
    assert "if percent >= critPercent { return danger }" in theme
    assert "if percent >= warnPercent { return attention }" in theme
    assert "return text" in theme
    assert "case \"alert\": return WidgetTheme.attention" in views
    assert "alarmed ? WidgetTheme.attention" in views
    assert "stale ? WidgetTheme.attention : WidgetTheme.muted" in views

    activity = _read(WIDGET_DIR / "NeedsYouActivityViews.swift")
    assert ".activityBackgroundTint(WidgetTheme.bg)" in activity
    assert ".activitySystemActionForegroundColor(WidgetTheme.accent)" in activity
    assert ".keylineTint(WidgetTheme.attention)" in activity
    assert "stale ? WidgetTheme.muted : WidgetTheme.text" in activity
    assert "stale ? WidgetTheme.muted : WidgetTheme.attention" in activity


def test_the_widget_never_asks_for_the_diary():
    """`GET /api/log` is the app's alone (`test_phone_event_log`); the tile
    reads nothing new and does no network."""
    for path in (WIDGET, VIEWS):
        text = _read(path)
        assert "fetchLog" not in text
        assert "/api/log" not in text


# --- the Lock Screen circular slot ----------------------------------------------

CIRCULAR = WIDGET_DIR / "CircularFleetView.swift"


def test_the_lock_screen_background_is_the_systems_own():
    """The `StaticConfiguration` closure used to paint `WidgetTheme.bg` on
    every family. That dark fill is right on the home screen and wrong on
    the Lock Screen, which expects the platform's own translucent capsule.
    The modifier lives on `FleetWidgetView` now, forked per family."""
    assert "containerBackground" not in _read(WIDGET)
    views = _read(VIEWS)
    assert "containerBackground(WidgetTheme.bg, for: .widget)" in views
    assert "AccessoryWidgetBackground()" in views


def test_the_circular_tile_reads_only_the_published_counts():
    """`needsYou` and `working` are the daemon's published counts. The
    circular tile derives nothing else from the summary — no usage bars,
    no to-do figure."""
    text = _read(CIRCULAR)
    assert "summary.needsYou" in text
    assert "summary.working" in text
    assert "summary.bars" not in text
    assert "summary.todo" not in text


def test_the_circle_speaks_as_one_element():
    text = _read(CIRCULAR)
    assert ".accessibilityElement(children: .ignore)" in text
    assert "TickRing.spoken(" in text


def test_the_lock_screen_tile_shares_one_timeline():
    """Adding `.accessoryCircular` to the existing widget means the app's
    existing reload sites and the one `StaticConfiguration` already serve
    the Lock Screen slot. A second kind would need its own reload call."""
    client = _read(CLIENT)
    assert client.count('reloadTimelines(ofKind: "BobFleetTile")') == 2
    assert _read(WIDGET).count("StaticConfiguration") == 1


# --- the face column ------------------------------------------------------------

CAST = ROOT / "ios" / "BobPhone" / "Cast.swift"
PORTRAITS = ROOT / "ios" / "BobPhone" / "Resources" / "portraits"


def _cast_names() -> list[str]:
    body = re.search(r"static let names = \[(.*?)\]", _read(CAST), re.S)
    assert body, "Cast.names moved"
    return re.findall(r'"([^"]+)"', body.group(1))


def _widget_constant(name: str) -> float:
    match = re.search(rf"static let {name}: CGFloat = ([0-9.]+)", _read(VIEWS))
    assert match, f"WidgetTheme.{name} missing"
    return float(match.group(1))


def test_the_summary_carries_faces_tolerantly():
    """A synthesized decoder throws on a missing key even with a default, so
    an older app's note (no `faces`) must still draw today's tile rather
    than fall through to the "open Dark Army" placeholder."""
    text = _read(SUMMARY)
    assert "decodeIfPresent([Face].self, forKey: .faces) ?? []" in text
    assert "struct Face: Codable, Equatable" in text
    assert "static let maxFaces" in text
    assert "static let maxNameChars" in text
    # `Face` decodes tolerantly too — a fifth key added later must not throw.
    assert text.count("init(from decoder: Decoder) throws") == 2


def test_the_roster_is_not_in_the_reload_throttle():
    """A roster flaps at turn cadence. Adding it to `figures` would spend a
    reload on every bucket flip and burn the budget that keeps the numbers
    fresh."""
    text = _read(SUMMARY)
    assert "var figures: (Int, Int, Int, [Bar]) { (needsYou, working, todo, bars) }" in text
    assert "faces" not in text.split("var figures")[1].split("\n")[0]
    assert "$0.figures != summary.figures" in _read(CLIENT)


def test_the_faces_are_the_daemons_buckets_and_the_apps_cast():
    """The widget derives nothing: it does not hash a session id, does not
    restate the roster and does not decide who is waiting."""
    client = _read(CLIENT)
    for term in ("snapshot.agents.waiting", "snapshot.agents.running",
                 "Cast.character(for:", "Cast.artOnly"):
        assert term in client, term
    names = _cast_names() + ["overwatch"]
    for path in sorted(WIDGET_DIR.glob("*.swift")):
        text = path.read_text()
        for name in names:
            # A restated roster is a string literal. Several callsigns are
            # ordinary words ("ledger", "quiet", "relay") a comment may use,
            # so the check is the quoted name, in either case.
            assert not re.search(rf'"{re.escape(name)}"', text, re.I), (path.name, name)
        assert "&*" not in text, path.name


def test_waiting_agents_win_the_face_outright():
    """Waiting alone, never waiting-then-working: one blocked agent among
    five busy ones would otherwise show for one hour in six."""
    body = _read(CLIENT).split("private func widgetFaces()")[1]
    body = body.split("\n    private func")[0]
    waiting = body.index("snapshot.agents.waiting.isEmpty")
    running = body.index("snapshot.agents.running.isEmpty")
    assert waiting < running
    # Two ordered `if` arms with an early return each, never a concatenation.
    assert body.count("return Array(faces(") == 2
    assert "waiting + snapshot.agents.running" not in body


def test_the_idle_rotation_is_the_whole_cast_including_the_art_only_face():
    """`overwatch` is art-only precisely because no session wears it, so it can
    only ever arrive through the idle branch."""
    body = _read(CLIENT).split("private func widgetFaces()")[1]
    body = body.split("\n    private func")[0]
    idle = body.split('doing: "standing by"')[0]
    assert "Cast.artOnly" in idle
    assert "Cast.names" in idle
    assert '"overwatch"' not in body


def test_the_face_column_arithmetic_fits():
    """The middle column comes out of the flexible count column, so the
    layout is decided by numbers rather than by a `GeometryReader`."""
    content = _widget_constant("mediumContentWidth")
    usage = _widget_constant("usageColumnWidth")
    face = _widget_constant("faceColumnWidth")
    spacing = _widget_constant("columnSpacing")
    floor = _widget_constant("minCountColumnWidth")
    assert content - usage - face - 2 * spacing >= floor
    views = _read(VIEWS)
    medium = views.split("struct MediumFleetView")[1].split("struct CountLine")[0]
    assert "spacing: WidgetTheme.columnSpacing" in medium
    assert "width: WidgetTheme.usageColumnWidth" in medium
    assert ".frame(width: 120)" not in medium
    assert "spacing: 14" not in medium


def test_the_face_column_is_absent_rather_than_empty():
    """An older app's note carries no roster; the column must leave the
    layout entirely, and the tile must never fall through to the
    "open Dark Army" placeholder, which means "never heard from the Mac"."""
    views = _read(VIEWS)
    assert "let face: FleetSummary.Face?" in views
    # Absent, and wrapped in the face's own link where it has a session.
    assert "if let face {\n                    if let url = FleetLinks.agent(face.sessionId) {" in views
    assert "let face: FleetSummary.Face?" in _read(WIDGET)


def test_the_face_column_speaks_as_one_element():
    views = _read(VIEWS)
    column = views.split("struct WidgetFaceColumn")[1]
    column = column.split("struct WidgetFacePortrait")[0]
    assert ".accessibilityElement(children: .ignore)" in column
    assert ".accessibilityLabel(" in column
    # `children: .ignore` already silences the portrait and both lines.
    assert views.count("accessibilityHidden(true)") == 2


def test_the_face_line_redacts_on_the_lock_screen():
    column = _read(VIEWS).split("struct WidgetFaceColumn")[1]
    column = column.split("struct WidgetFacePortrait")[0]
    assert ".privacySensitive()" in column
    assert "\\(face.name) · \\(face.doing)" in column
    # The two new prose lines wrap inside the column; they are not clipped.
    assert "lineLimit" not in column
    assert _read(VIEWS).count("lineLimit") == 1


def test_the_rotation_is_hourly_and_the_policy_is_still_never():
    text = _read(WIDGET)
    assert "rotationHours = 12" in text
    assert "timeIntervalSince1970 / 3600" in text
    assert text.count("policy: .never") == 1
    # A calendar hour repeats once a year at the DST fallback, and two
    # entries claiming one hour is a tile that visibly goes backwards.
    assert "Calendar" not in text
    # The existing two entries keep their shape.
    assert "addingTimeInterval(dimAfter)" in text
    assert "summary?.dimAfter ?? 600" in text
    assert "dimmed: false" in text
    assert "dimmed: true" in text


def test_the_widget_bundles_the_portraits_folder():
    # In an appex `Bundle.main` is the appex, so a portrait is only findable
    # if the folder is in the *widget's* Resources phase, not just the app's.
    pbx = _read(PBXPROJ)
    assert pbx.count("portraits in Resources") == 6  # 3 defs + 3 uses
    assert len(re.findall(r"PBXFileReference.*Resources/portraits", pbx)) == 1
    body = _phase_body(pbx, "7B0B0E1A0000000000000133 /* Resources */")
    assert "portraits in Resources" in body


def test_the_portraits_load_through_the_appex_bundle():
    views = _read(VIEWS)
    assert 'Bundle.main.url(forResource: "portraits"' in views
    assert "Bundle.module" not in views
    # A 512x512 PNG decodes to ~1 MB and the extension has ~30; only the
    # thumbnail is cached.
    assert "preparingThumbnail(of:" in views
    for slug in [n.lower() for n in _cast_names()] + ["overwatch"]:
        assert (PORTRAITS / f"{slug}.png").is_file(), slug


def test_the_slogans_ship_in_code_and_are_short():
    views = _read(VIEWS)
    body = re.search(r"static let lines = \[(.*?)\n    \]", views, re.S)
    assert body, "WidgetSlogans.lines moved"
    lines = re.findall(r'"([^"]*)"', body.group(1))
    assert len(lines) >= 8
    assert "static let maxLineChars = 22" in views
    assert all(len(line) <= 22 for line in lines), lines
    # Not a third copy of `AgentChatter`, whose panel/phone pair is
    # byte-pinned; a copy in a target neither test reads is that drift.
    for path in sorted(WIDGET_DIR.glob("*.swift")):
        assert "AgentChatter" not in path.read_text(), path.name


# --- the Live Activity: the waiting agent on the Lock Screen --------------------

ACTIVITY_VIEWS = WIDGET_DIR / "NeedsYouActivityViews.swift"
ACTIVITY_SHARED = ROOT / "ios" / "Shared" / "NeedsYouActivity.swift"
LIVE_ACTIVITY = ROOT / "ios" / "BobPhone" / "LiveActivity.swift"
APP_INFO = ROOT / "ios" / "BobPhone" / "Info.plist"


def test_the_picks_card_links_to_the_review():
    """(success criterion) The Lock Screen card for a review waiting on
    picks links to the review run; the banner's Open review button routes
    to the same router slot."""
    summary = _read(SUMMARY)
    assert "static func review(_ runId: String)" in summary
    assert 'parts.host = "review"' in summary
    assert 'URLQueryItem(name: "run"' in summary
    assert "static func waiter(" in summary and 'kind == "picks"' in summary
    router = (ROOT / "ios" / "BobPhone" / "Router.swift").read_text()
    assert '$0.name == "run"' in router and "LockScreenActions.idOK(run)" in router
    assert "func takeReviewRun()" in router and "func openReview(runId:" in router
    assert "guard LockScreenActions.idOK(runId)" in router
    review = (ROOT / "ios" / "BobPhone" / "ReviewView.swift").read_text()
    assert "HeldReviewRun.decide(" in review and "pictureAsOf > heldAt" in review
    assert ".onDisappear { heldRun = \"\" }" in review
    assert review.count("takeReviewRun()") == 1       # one consumer ...
    assert ".onChange(of: router.signal)" in review   # ... on appear and on signal
    push = (ROOT / "ios" / "BobPhone" / "Push.swift").read_text()
    assert "LockScreenActions.opens(" in push and "PhoneRouter.shared.openReview(runId:" in push


def test_the_activity_views_render_only_and_clip_nothing():
    """ActivityKit drives every redraw: the widget's second configuration
    draws the portrait through the same loader the tile uses, counts up on
    a system timer, deep-links through the scheme, and does no network —
    the `**/*.swift` glob above already covers `URLSession`."""
    text = _read(ACTIVITY_VIEWS)
    assert text.count("ActivityConfiguration(for: NeedsYouAttributes.self)") == 1
    assert "WidgetFacePortrait(" in text
    assert "style: .timer" in text
    assert ".lineLimit(" not in text
    assert "context.isStale" in text
    assert text.count("FleetLinks.waiter(") == 2
    assert "FleetLinks.agent(" not in text
    for word in ("URLSession", "URLRequest", "TimelineProvider", "fetchLog"):
        assert word not in text, word
    assert "NeedsYouActivityWidget()" in _read(WIDGET)
    assert _read(WIDGET).count("NeedsYouActivityWidget()") == 1
    assert '"cost unknown"' in _read(ACTIVITY_SHARED)
    assert text.count(".privacySensitive()") >= 1


def test_the_shared_attributes_are_foundation_and_activitykit_only():
    text = _read(ACTIVITY_SHARED)
    imports = set(re.findall(r"^import (\w+)", text, re.M))
    assert imports == {"ActivityKit", "Foundation"}
    for word in ("URLSession", "URLRequest", "Keychain", "SecItem"):
        assert word not in text, word
    # The wire's six keys, spelled as `push.js` writes them.
    assert 'case sessionId = "session_id"' in text
    assert "case nickname, slug, kind, work, since" in text
    assert "staleAfter: TimeInterval = 1800" in text
    assert "dismissalDelay: TimeInterval = 0" in text


def test_the_shared_attributes_are_compiled_into_all_three_targets():
    pbx = _read(PBXPROJ)
    assert pbx.count("NeedsYouActivity.swift in Sources") == 3
    for phase in ("7B0B0E1A0000000000000131 /* Sources */",):
        assert "NeedsYouActivity.swift" in _phase_body(pbx, phase)
        assert "NeedsYouActivityViews.swift" in _phase_body(pbx, phase)
    assert "NeedsYouActivityViews.swift" not in _phase_body(
        pbx, "7B0B0E1A0000000000000131 /* Sources */").split("NeedsYouActivity.swift")[0]
    assert "LiveActivity.swift in Sources" in pbx
    assert "NeedsYouActivityStateTests.swift in Sources" in pbx
    assert "LiveActivityRuleTests.swift in Sources" in pbx
    assert _read(APP_INFO).count("NSSupportsLiveActivities") == 1


def test_the_token_is_posted_only_where_the_mac_says_so():
    """`register_activity_token` rides the quiet route and only against a
    Mac stating `live_activity_supported`; the send and the unregister are
    both behind the `supported` read. Against an older Mac the card still
    starts and ends locally."""
    text = _read(LIVE_ACTIVITY)
    assert text.count("registerActivityToken") >= 2
    assert "liveActivitySupported" in text
    assert "supported = snapshot.board.liveActivitySupported" in text
    send = text.split("private func sendTokenIfDue()")[1].split("\n    }")[0]
    assert "guard supported" in send
    unregister = text.split("private func unregisterIfSent()")[1].split("\n    }")[0]
    assert "supported" in unregister
    assert "Activity<NeedsYouAttributes>.activities" in text
    assert "applicationState == .active" in text
    client = _read(CLIENT)
    assert "LiveActivityController.shared.reconcile(snapshot: snapshot" in client
    assert "action: PhoneActions.registerActivityToken" in client
    assert "LiveActivityController.shared.adoptExisting()" in _read(APP)
    assert "LiveActivityController.shared.endAll(unregistering: true)" in _read(APP)


def test_the_lock_screen_card_fits_inside_the_system_height():
    """iOS caps the Lock Screen Live Activity near 160pt and crops anything
    taller flush to the card's edge — the prompt line and the figures line
    were drawn touching the rim. The pane keeps a 36pt face, one-line
    counts, 4pt row gaps and a real inset on all four sides."""
    text = _read(ACTIVITY_VIEWS)
    lock = text.split("struct NeedsYouLockScreenView")[1].split("struct FleetRule")[0]
    assert "VStack(alignment: .leading, spacing: 4)" in lock
    assert "WidgetFacePortrait(slug: state.slug, size: 36)" in lock
    assert "FleetCountsRow(state: state, stale: stale, inline: true)" in lock
    assert ".padding(.horizontal, 18)" in lock
    assert ".padding(.vertical, 13)" in lock
    assert lock.count("FleetRule()") == 1, "one hairline, above the figures"
    # The island keeps its stacked cells: only the Lock Screen goes inline.
    island = text.split("struct NeedsYouLockScreenView")[0]
    assert "inline: true" not in island


def test_the_live_card_names_a_review_run_waiting_on_picks():
    """Source pins for the fourth kind and the run id: the shared state
    decodes `run_id`, draws "pick fixes" and counts a run id as a face; the
    rule lists the review wire among the kinds that may be the subject."""
    shared = _read(ACTIVITY_SHARED)
    assert shared.count('case runId = "run_id"') == 1
    assert shared.count('case "picks": return "pick fixes"') == 1
    assert "!sessionId.isEmpty || !runId.isEmpty" in shared
    rule = _read(LIVE_ACTIVITY)
    assert ".reviewPicks" in rule.split("static let sessionKinds")[1].split("\n\n")[0]
    assert "a.runId == b.runId" in rule
    assert rule.count("reviewPicks") >= 2
