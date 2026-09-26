# The front page's pictures

Every picture in this folder, except the four under `showcase/`, is drawn by
Dark Army's own screens from one made-up working day, and retaken with one command. None is a screenshot of
anybody's real work, and none is edited by hand: the fix for a wrong picture
is a change to the day and a re-run, never a change to the PNG.

## The story

`panel/Tests/Fixtures/demo-shots.json` is the day. Three invented projects —
`pocket-weather` (a weather app), `trailhead-api` (a hiking-trails service)
and `inkwell` (a notes app), each at `/Users/you/Code/<project>`. Seven agents
from the cast: Vex, Forge (Claude) and Hex (Codex) working, Cipher waiting on
a question about temperature units on the card he is building, Ledger and Nyx (Grok) resting, Relay
finished. Eleven cards across the four rows, one open card with a plan, running
cards with their cost and time, a Done card waiting on a person's check, and
usage meters in the middle of their range. Cipher is the one waiting because
his is the one face the menu-bar strip has art for.

## Retake them

```bash
host/.venv/bin/python tools/demo_shots.py all
```

Needs Xcode (the panel's `swift test`) and the host venv (Pillow and PyObjC
come from `host/requirements-dev.txt`). It takes a few minutes, most of it the
panel build. Each step runs alone too: `check`, `strip`, `panel`, `compose`;
`--work <dir>` keeps the layers for a look.

The run ends with `live app untouched: <n> processes, same pids` — every
Dark Army process that was up before it started is up after, same pid.

## What it never touches

- **The running app.** It never starts, signals or stops a Dark Army process,
  never opens the installed app and never runs the panel binary (a second
  panel would evict the person's own).
- **The daemon.** The panel layers are drawn inside the panel's own test
  harness (`panel/Tests/BobPanelTests/DemoShotsTests.swift`): `PanelView` and
  the card window are hosted in borderless off-screen windows, the client is
  never started, and a `URLProtocol` answers every request from the day and
  writes it down. `requests.txt` in the work folder is that log; the run
  fails on any `/api/events` request or any write.
- **Your state folder.** The strip is drawn in its own short-lived process
  whose `HOME` is a fresh temporary folder, checked before anything is drawn.

## Change the story

Edit `panel/Tests/Fixtures/demo-shots.json`, then run `check` before
anything else:

```bash
host/.venv/bin/python tools/demo_shots.py check
```

It refuses a day that is not marked `synthetic`, names a home folder other
than `/Users/you`, contains this machine's account, host or computer name or
the git author's name or email (worked out at run time and never written
down, plus `tools/public-export.json`'s list where that file exists), names a
project outside `metadata.projects` or a nickname outside `identity.NAMES`,
gives any row a pid, a terminal or a capability beyond Cipher's typing, or
whose strip counts differ from the frame's own buckets. Every clock in the
day is written against `metadata.base_epoch`; each harness moves them all to
the moment of the run, so "waiting 1m" reads as a minute on any day.

## What guarantees what

| Guarantee | Held by |
|---|---|
| The day is made up and holds nothing of this machine's | `tools/demo_shots.py check`, `host/tests/test_demo_shots.py` |
| The strip is drawn by the real ladder walk at its top rung, inside its width budget | `strip`; `test_the_strip_draws_a_real_picture_at_the_top_rung` |
| The day decodes through the real models and tells the story | `DemoShotsTests.testDemoDayDecodes` (Mac), `DemoShotsTests.testDemoDayDecodesOnPhone` (phone) |
| The card window is opened the way a person opens it, and its Save is not held | `DemoShotsTests.testTheOpenCardWindowHoldsNothingBack` (the always-on hold check), `test_the_card_window_picture_holds_nothing_back` (the Mac's text recognition on `board.png`) |
| Nothing reached the daemon | the request log, checked by the Mac render and by `panel` |
| No hidden metadata, even sizes, 800 KiB at most | `compose`; `test_every_picture_*` |
| Four pictures, each with a description, drawn at 2x | `shots.json`; `test_the_manifest_names_exactly_the_four_pictures` |
| The running app was left alone | the pid comparison at the end of `all` |

## Using them

`shots.json` holds each picture's `alt`, `display_width` (half its pixel
width, at most 880) and the day's digest. Embed a picture with exactly its
own fields:

```html
<img src="docs/images/<file>" alt="<alt>" width="<display_width>">
```

## The showcase slides

`showcase/` holds four slides in the Signal design system, redrawn from the
Reddit showcase deck (`user-data/reddit-showcase/slides/`, left as it was).
The new deck lives in the git-ignored `user-data/readme-showcase/slides/`:
its frame takes its colours, radii and type from a copy of
`design-system/workshop/tokens.css`, and every picture of the app in it is a
real render of this folder's made-up day — the Mac panel's layers from
`DemoShotsTests`, the iPhone screens from the phone's own views drawn in a
simulator. Only the macOS menu bar around the real strip, the generic
terminals on the problem slide and the iPhone bezel and status bar are drawn
by the deck. The phone screens (Fleet, Board and Cipher's sheet on Main,
with his card's journey) come from `DemoShotsTests.testRenderPhoneShots` on a
throwaway simulator, deleted afterwards; the folder must be absolute:

```bash
TEST_RUNNER_BOB_DEMO_SHOTS_OUT=/abs/out xcodebuild test -project ios/BobPhone.xcodeproj \
  -scheme BobPhone -destination 'platform=iOS Simulator,id=<throwaway udid>' \
  -only-testing:BobPhoneTests/DemoShotsTests CODE_SIGNING_ALLOWED=NO
```

Copy the three PNGs into the deck's `img/`; `phone-fleet-rows.png` is the
Fleet screen cut from y = 690 px to 1680 px. The README copies come from a second render:

```bash
host/.venv/bin/python tools/showcase_ingest.py           # needs Chrome and the deck
host/.venv/bin/python tools/showcase_ingest.py --check   # the committed four alone
```

It switches off the footer line `.foot .brand::after` in `base.css`, shrinks
to 1200 px and saves a palette PNG under 300 KB, choosing the palette by fast
octree so small colours — the window's traffic lights, a red `wait` — keep
their hue. `host/tests/test_showcase_images.py` pins the set, the size, the
footer (no flag red, no name read by the Mac's text recognition) and the
README embeds. Never edit these by hand either: re-run the tool.

## The look a person still takes

1. Open the `docs/images` folder in Finder.
2. Open `agent-question.png` in Preview and zoom to 200%.
3. Read every project name, row name, branch, card title, message and number.
4. Expect only the made-up projects (pocket-weather, trailhead-api, inkwell)
   and cast names, with no name, folder, email, computer name or project of
   your own.
5. Check that no text is half-scrambled, nothing is cut off at an edge, and
   the shadow and dark background look even.
6. Repeat steps 2 to 5 for the other three pictures.
7. If any picture fails, write down which one and what you saw; the fix is a
   change to the demo day and a re-run, never an edit to the picture.

Why not automated: the checks prove no known private string is in the day and
the request log proves nothing live was drawn, but whether made-up content
reads as believable and a frame looks polished is a person's judgement about
a real picture.
