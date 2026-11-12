# Fleet activity map contract

The panel rail's **Table | Map** switch. Map draws the selected tab's live
agents as portraits inside a ring of seven stations; each tool call flies to
its station in the agent's colour. Mac panel only: the phone, relay and strip
never receive it. Inspired by lot-map (MIT); no code or art copied.

**Stations (daemon, one place).** `fleet_map.station_for` classifies a call;
the panel never re-derives it. `you` (ask tools), `files` (Read, Edit, Write,
Grep, Glob, NotebookEdit), `shell` (Bash), `web` (WebSearch, WebFetch),
`helpers` (Agent, Task), `skills` (Skill, SlashCommand); `mcp__*`,
`server__tool`, LSP and unknown are `tools`. A sibling of
`TOOL_ANIMATION_MAP`, not a widening.

**End inference.** No general "call ended" hook exists and `hooks.py` is
untouched. `fleet_map.advance`: `tool_use` closes the open call `done` and
opens one; `tool_done`, `add`, `dismiss` close it `done`; `tool_failed` closes
it `failed` and steps `errors`; `permission` parks a running call on `you`; a
`UserPromptSubmit` dismiss zeroes `errors`. One open call per session.

**Wire.** `_note_fleet_call` keeps a `CallRing` (8 calls) per session on the
loop; `_forget_session` pops it; never in `sessions.json`.
`fleet_activity_snapshot()` gives `{version, available, stations, sessions:
{sid: {calls: [{seq, station, tool, end}], station_counts, errors,
errors_by_station}}}`; `errors_by_station` (failures per station) outlives the
eight-call window and is zeroed with `errors` by the next prompt. **No
timestamp under any name** (it would make every frame news; the panel stamps
arrival) and no path, command, prompt or key.

**Loopback-only.** `fleet_activity` is the last `_OMITTABLE_SECTIONS` entry and
the only `_LOOPBACK_ONLY_SECTIONS` one; `api_server.phone_picture` drops it
before both phone digest sites (`_state_answer`, `relay_ws`).

**Panel.** Rules in `FleetMap.swift` (`FleetMapRulesTests`), drawing in
`FleetMapView.swift`. State, bucket and the context ring come from the row
(`ProcessRow.ctxColor`); hue is the index in the tab's `byStart` order, amber
and red never palette members. A station counts running calls across agents;
path width is log-scaled 1 to 4pt; a failed call leaves a red count (from
`errors_by_station`) until that agent's next prompt; the feed and counts read
only the selected tab's sessions. A waiting agent has an amber halo and a steady line to
You, no pulse. Helpers are still dots; sessions on one card are joined by a
line. The feed is the running calls plus the newest sixteen (`●` `·` `✕`),
finished runs at one station folded with `+n`. A portrait press is
`PanelView.tapRow`, the row's own handler.

**Motion.** One `TimelineView` on `AnimationTimelineSchedule(paused:)`, `paused` naming `visible`,
`reduceMotion` and nothing in flight; flight is a pure function of the pane's
arrival stamp, and a call first seen in the first second of watching counts as
landed. Reduce Motion draws still dots. No `GeometryReader`, no `ProgressView`.
The choice is `fleet_rail_view` in `panel-position.json`, not preferences.
