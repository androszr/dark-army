@AGENTS.md

<!-- The contract lives in AGENTS.md so that Codex, which never opens this
     file, reads the same text as Claude and Grok. Put only Claude-specific
     notes below this line. -->

## Claude-only notes

- Dark Army's board tools (`dark_army_add_card`, `dark_army_attach_plan`,
  `dark_army_close_card`, `dark_army_needs_manual_check`) arrive in every
  session once Dark Army is installed; its channel only in a session started
  with `claude --dangerously-load-development-channels server:dark-army`.
  Their absence is an ordinary state, not a fault — the skills say what to do
  without them. A session started before the rename carries the same verbs as
  `bob_*`; a session keeps the tool list it was born with.
- Sub-agents are spawned with the `Agent` tool by the `name` in their
  frontmatter under `.claude/agents/`.
