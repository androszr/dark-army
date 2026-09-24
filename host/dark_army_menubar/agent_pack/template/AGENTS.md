# {{PROJECT}} — working agreement for every coding agent

This file is the contract. Claude Code reads it through `CLAUDE.md` (which
imports it), Codex reads it directly, Grok reads it directly. There is one
text, and this is it. **Read [`docs/context.md`](docs/context.md) next** — it
is the architecture ground truth, the gates, the conventions and the reviewer
table that every agent brief refers to.

## Working agreement

### 1. Act. Don't ask.

Reversible and cheap? Do it, then tell me. A question costs me more than a
re-run costs you.

Ask first only for: anything reaching an audience, anything we cannot undo,
anything expensive.

Something is broken? Fix it. Reporting an issue you could have fixed turns your
work into my to-do list.

### 2. A question is a question

When I ask a question, answer it. Do not implement it.

"Why is this failing?" is not "make it stop failing."
"Should we use X?" is not "migrate everything to X."

When in doubt, assume it is a question. Answer first. Act when I say go.

### 3. Done means done

Not half done. Not done except for the part you decided to skip. And not a
report about how it will be done.

Five things asked means five things delivered. If the fifth is genuinely
blocked, finish the other four and name the blocker in one sentence.

"I'll continue in the next message" is not a state this project has.

---

These govern everything below. The one standing exception is `/ship`: invoking
it is itself a request for a planning interview, so its questions are not
"asking" in the sense of rule 1.

## Gates

The gates are the rows of the **Identity table** in `docs/context.md`. All of
them pass before any handoff, in the order the table lists them. No agent
brief names a gate command; they all read the table.

## How a request becomes work

An explicit request to work directly or avoid skills overrides the default
below. A spawned specialist executes its assigned brief and returns to its
coordinator; it does not start `/ship`, file cards or spawn the crew. Complete
instructions already present in the current context count as read; read only
missing or changed content. Follow linked contracts when the task reaches
their subject, not every link transitively.

**Any request for a change to this codebase goes through `/ship`, whether or not
the word `/ship` is typed.** "Add X", "fix Y", "can we make Z faster" — all of
it. Ask what you genuinely need to ask, write the plan, file it on Dark Army's Kanban
board as a Backlog card, and close out with `close-out.sh --plan`. Then stop.

Three things follow from that, and each of them is the point rather than a side
effect:

- **Do not implement in the same breath as planning.** The card is the handoff.
  It is picked up by a human pressing Start, which dispatches a fresh session
  into `/ship implement <plan path>` with a context holding the plan and nothing
  else.
- **Do not end the turn on "shall I build it?"** A turn that ends in a question
  puts the session under Dark Army's *Needs you*, and a written plan is not an
  interruption — nothing is blocked and nothing is half-done. Leave no
  `<!-- bob-tldr -->` and no `<!-- bob-actions -->` on that last message; both
  of them mean "somebody is waiting on you", and nobody is.
- **Close out** — a plan run's last act is
  `bash .claude/skills/ship/close-out.sh --plan`, which closes its own tab.
  Bare, the script closes nothing and prints one line; end a scout run's
  last message with it (an implement run ends on its `## Work done` report).
  The person closes those tabs; run it with `--close` only when they ask.

The exceptions, and there are only three. A **preflight BLOCK** means the plan is
not fit to file, so that one does end on a question. An **explicit instruction to
build it now** outranks the default — say in one sentence that the usual route is
the card, then do it. And **questions, explanations, one-line reads and anything
that changes no code** are not requests for a change and have nothing to do with
any of this.

## The skills

| Skill | What it does | Ends with |
|---|---|---|
| `/ship <idea>` | interview → plan → preflight → Backlog card → `--plan` close-out | a card, no question |
| `/ship implement <plan>` | implement → blind verify → bug scan → domain review → handoff | the `## Work done` report |
| `/review [sha \| #pr \| branch]` | graph-aware graded review in plain language | a verdict, offered cards |
{{SKILL_ROWS}}

Skills live in `.claude/skills/` (canonical) and are byte-copied to
`.agents/skills/` for Codex and Grok by Dark Army's agent-pack sync. **Never edit a
copy.** Agent briefs live in `.claude/agents/` (canonical); `.codex/agents/`
and `.grok/agents/` hold generated shims that point at them.

## The agents

Five roles, prefixed `{{P}}-`, each a markdown brief under `.claude/agents/`:
planner, implementer, verifier, bug-auditor, and one or more domain reviewers
named in the Reviewers table of `docs/context.md`. A brief carries a `model:`
line where Dark Army's Agent models setting names one; otherwise it inherits
the session's default. The verifier is **blind** — it is never
shown the implementer's report — and the read-only roles (verifier, auditor,
reviewers) never edit a file.

## Non-negotiables

The project's own rules are the `## Conventions` section of `docs/context.md`.
Two hold everywhere:

1. **Never commit or push** unless explicitly asked. Pressing Start on a card
   authorises the work in that card's plan and nothing else.
2. **Persisted shapes are forward-compatible.** A new key gets a default on
   read, a removed key is tolerated on load, a migration is additive.

## Where to search

Never run a recursive file walk — `find`, `grep -r` or `-R`, `rg`, `fd`,
`ls -R`, `du`, `tree`, `mdfind` without `-onlyin`, or anything like them —
rooted at `/`, at your home folder, or at `Documents`, `Desktop`,
`Downloads`, `Pictures`, `Music`, `Movies` or `Library` inside it. On a Mac
running Dark Army such a walk is charged to Dark Army and raises Photos,
Music and Documents privacy prompts naming it. Search only inside this
project, Dark Army's own folder `~/.dark-army`, your session's own scratch
folder, and the folders Dark Army knows (its checkout and every project
onboarded to it, either direction): they are listed in
`~/.dark-army/search-scope.json` — read that one file (it holds folder
paths and nothing else); never search for it. Reading a file at a known
exact path is always fine. A plan or card file is never something to search
for: a session started from a Dark Army card was handed its plan path, a
card's `plan_path` is on the board (the `dark_army_*` board tools or Dark
Army's panel), and otherwise ask the person for the path.

## Finishing a piece of work

When the task is complete and you are not waiting on anything, the final
message ends with the standard completion report. This is written down here
so it remains available regardless of which hooks the assistant loads:

```
## Work done

**Asked:** <the request, one or two sentences, in the user's terms>
**Changed:** <what changed, up to ~6 short bullets; file paths welcome>
**Verified:** <each check you ran and its result, in plain words>
**Unchecked:** <numbered steps a person can follow — open this, press that,
  expect this — then one line beginning "Why not automated:"; or exactly
  "Nothing - every check above ran.">
**Card:** <only when working a board card: "Moved to Done: <note>" or
  "Left open: <why, naming the unchecked items>">
```

The four labelled lines always appear in that order; an empty section says so
out loud rather than going quiet; the whole report stays under 25 lines; the
message carries no `bob-tldr` and no `bob-actions` marker.

**Unchecked items are steps, and there should be almost none.** Numbered,
imperative, one action per line, naming the surface to open, the thing to
press and what the person should see, in words a non-developer could follow.
Then one required line beginning `Why not automated:`. Prefer the test: leave
something here only where the observation genuinely cannot be made in code —
a real screen, a real device, a second real terminal, a timing you can only
feel. Handing back more than two of these means looking again for the seam.

**Never end a finished turn on a question.** A completed piece of work ends
with its report, not with "shall I…?". A turn that ends in a question says
somebody is blocked, and finished work does not block anybody.

## When you are waiting on the user

A message that genuinely stops to wait — a question, a plan blocked by
preflight, a choice — ends with one plain sentence in an HTML comment, and,
when the answers are concrete, the answers on a second line:

```
<!-- bob-tldr: one short sentence, plain words, no paths or identifiers -->
<!-- bob-actions: Accept | Iterate -->
```

Up to three actions, each the word to type back, and only when they are
genuinely the answers. Claude gets this rule from Dark Army's hook and Grok from its
rules file; Codex gets it only from here.

## Setup per assistant

- **Claude Code** — nothing to do. `CLAUDE.md` imports this file; skills and
  agents are read from `.claude/`. Dark Army's board tools arrive in every
  session once Dark Army is installed; its channel (pushes and permission
  answers) only in a session started with
  `--dangerously-load-development-channels server:dark-army`. A session
  started before the rename carries the same verbs as `bob_*`; a session
  keeps the tool list it was born with.
- **Codex** — trust the project once (`codex` will ask). Skills come from
  `.agents/skills`, agents from `.codex/agents` (enabled in `.codex/config.toml`).
  Dark Army's board tools come from the `dark-army-board` MCP in
  `~/.codex/config.toml` when Dark Army is installed. Inspect the actual tools:
  use `dark_army_close_card` only after all required checks pass; if absent or
  refused, report that the card remains open. Closing a card never closes its terminal.
- **Grok** — mark the folder trusted once. Skills come from `.claude/skills`
  and `.agents/skills` (deduplicated by name), agents from `.grok/agents`.
