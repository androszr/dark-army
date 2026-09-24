# Interview questions

Ask **at most 3** material design questions, one at a time, using the provider
route in SKILL.md. Resolve each from the brief, objective and codebase first;
zero questions is valid. Record settled assumptions and answers for the planner.
Never repeat an answered objective; "use recommendations" settles remaining
branches with explicit assumptions.

Pick the branches that actually apply to the idea. Skip the rest.

---

## A. Surface

> Where does this show up?

- The menu-bar strip (the animated icons and counts)
- The panel (the window the menu bar opens — the only rich surface; the dropdown
  was removed)
- The agent's own terminal or editor tab (title, reveal, notifications)
- None of them — daemon or hook behaviour only

## B. Source of the data

> Where does the information come from?

- State the daemon already tracks (sessions, subagents, categories)
- A Claude Code hook event — including a hook we don't handle yet
- The session transcript on disk (`session_stats`), or `claude agents --json`
- Something outside Claude Code entirely (Grok, the editor, the shell)

## C. Push or poll

*Ask only when a new number or row has to stay fresh.*

> How should this stay up to date?

- Pushed on a structural change (`on_agents_change` / `on_activity_change`)
- Polled on its own timer — the value changes without a structural event
- Recomputed when the panel refreshes
- One-shot; it does not need to change once shown

## D. Does it act, or only report?

> Does the user do something here, or just look?

- Read-only
- A verb that changes Dark Army's own state (mute, dismiss, a preference)
- A verb that reaches a real session (stop, retire, reveal) — needs a guard and
  a confirmation
- A verb that writes outside Dark Army (`~/.claude/settings.json`, the editor)

## E. Both surfaces, or one

*Ask only when the idea is visible in more than one place.*

> The strip and the panel read the same buckets. Which of them should show this?

- Both — one source of truth, two renderings
- Panel only — it has the room
- The strip only — it is a glance, not a screen
- Whichever is cheapest now; the other is a follow-up

## F. Priority when the trade-off is real

*Ask only when the idea implies a genuine tension.*

> If these conflict, which wins?

- Interrupt less, even if something is missed
- Never miss it, even if it interrupts
- Keep the strip narrow
- Keep the record complete, even when nobody is looking
