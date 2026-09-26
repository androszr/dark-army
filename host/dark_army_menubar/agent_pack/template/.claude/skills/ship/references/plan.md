# ship — plan mode

Loaded after `references/common.md` in plan mode. Phases 1–5b: interview,
plan, deterministic preflight, file the plan on Dark Army's Kanban board, close out
with `--plan` (Phase 5b). It **stops there**; a session told to build it now loads
`references/implement.md` before Phase 6.

## Phase 1: interview

Load `templates/questions.md` before assessing the idea. Resolve branches from
the brief, the objective and the tree first; ask **at most 3** material design
questions, one at a time, with at most three unanswered questions. When zero
questions are needed, state the settled assumptions and proceed. Never ask the
person to restate a settled objective. If they say "use recommendations" or
"go with your recommendations", record the recommendations as assumptions and
proceed. Record answers and assumptions as a key/value map and pass that map to
the planner with the `Objective:` block verbatim.

Choose the question route from the tools actually callable in this session:
Claude uses `AskUserQuestion`; Grok uses `ask_user_question`. Codex uses
`request_user_input` only when its current collaboration mode permits it, or
`request_user_input_async` when available. An async acknowledgement only means
the question was registered: await the person's answer before dependent work.
Never use a question tool for permission when the environment forbids that.
If no supported question tool is callable, ask one concise plain-language
question in the final response, add `<!-- bob-tldr: <what needs deciding> -->`,
say "Answer in the original Codex session", and end the turn. Do not invent
reply buttons or require an unavailable Claude tool. On the next answer,
resume the interview and planning stage with the recorded map; the reply is
not a new request to implement. Refine owns this assessment; Prepare only
produces an editable draft.

## Phase 2: derive the plan path

1. Kebab-case slug from the idea, ≤40 chars, ASCII.
2. `<plans dir from the Identity table>/<YYYY-MM-DD>-<slug>.md`. On collision,
   suffix `-2`, `-3`.

## Phase 3: spawn the planner

Show the **Overwatch** banner (`banners/planner.txt`), then spawn `{{P}}-planner`
with this prompt:

```
Mode: new
Idea: <idea text>
Answers:
- <key>: <value>
objective: <the Objective: block from the dispatched prompt, verbatim — omit
  the line when there was none>
plan_path: <abs path>
template_path: <abs path to .claude/skills/ship/templates/plan.md>
context_path: <abs path to docs/context.md>

Write the plan per your brief. Read the context file FIRST, then verify every
path and symbol you cite against the actual tree before quoting it.
```

Wait for `PLAN: <path>` + abstract.

## Phase 4: deterministic preflight

Run the preflight runner — **bash and grep only, no agent spawn**:

```bash
bash .claude/skills/ship/preflight.sh "<plan path>"
```

It executes every check in `PREFLIGHT-CHECKLIST.md` (structural, shared by every
project) and then every `PREFLIGHT-*.md` beside it (the profile's domain checks),
printing one `BLOCK:` or `WARN:` line per finding and exiting 1 on any BLOCK.

- **Any BLOCK** → do not proceed to Phase 5. Show the abstract plus the BLOCK
  findings with fix hints, and offer `iterate` or `stop`.
- WARN-only → carry the warnings into the Phase 5 summary and continue.

This is the one place plan mode is allowed to end on a question. A preflight
BLOCK means the plan is not fit to be filed, and a card carrying a plan nobody
can build is worse than an interruption. Do **not** file it and do not close out:
leave the turn open with a `<!-- bob-tldr -->` saying what needs deciding, and
resume at Phase 4 when the user answers.

**Two rounds, then the user decides.** If the *same* check BLOCKs after two
iterate rounds, stop translating and show the **raw matched lines**, name the
check, and offer an explicit override — the plan proceeds with that check waived
and the waiver recorded in the plan's `## Iteration log`. These checks are
keyword-triggered against prose; a false BLOCK the user cannot see is a stall
they cannot diagnose, and this loop is the only one in `/ship` without a natural
cap.

Whenever a BLOCK or WARN is surfaced to the user (here or in Phase 5),
**translate each finding into one plain sentence** — what it means and why it
matters. Never show raw check names, check numbers, or command output; those
stay in your context.

## Phase 5: file the plan on the board

**The plan is the deliverable, and this is where plan mode stops.**

1. Re-read the plan file. The card is read by a person deciding what to pick up
   next, not by the agent that will pick it up, so everything on it comes from
   the plan's **plain-language zone** — never the technical zone.
Only a confirmed no-refinement attribution result permits creating a new
card. An attribution/ownership refusal or unknown reply is not that result:
report it and leave the existing card alone; never create a duplicate or guess
another identity. If attach is absent, create only when this is known to be a
hand-run planning session without an existing refinement card; otherwise hand
back the plan path and the missing tool. After authoring a card, attach once
to that authored card and report the returned actual column. An unsuccessful
attachment leaves Prep, not an invented Backlog. A preflight BLOCK attaches
nothing and starts no implementation.

2. **Attach first.** If this session was dispatched by Dark Army's Refine button, a
   card for this work already sits in Prep — creating a second one is the
   failure. So call the attach tool before anything else, if it exists:

   ```
   dark_army_attach_plan({ path: "<abs plan path>" })
   ```

   If it answers ok, the card this session was refining now carries the plan
   and has moved to **Backlog** — **do not also create a card**. Skip straight
   to the user summary below, naming that card and saying it moved to Backlog
   with the plan attached.

   If it refuses because this session is not refining any card (a hand-run
   `/ship`), fall through to step 3 and file one.
3. File it, if `dark_army_add_card` exists:

   ```
   dark_army_add_card({
     title: "<the change, one line, imperative — 'Add X', not 'Plan for X'>",
     summary: "<what this is for, 1-3 sentences a non-developer could read>",
     plan: "<abs plan path>",
     notes: "<the plan's `## What this does`, verbatim>",
     tool: "<claude | codex | grok — the assistant running this>",
     stages: ["<each specialist from the plan's Stages header, in order>"]
   })
   ```

   Split the plan's `Stages` value on `|`, trim each name, preserve its order,
   and omit blanks. Pass that normalized list exactly; do not add a stage that
   the plan did not declare.

   **`summary` is not optional and it is not the title again.** It is the line
   the card *leads with* on the board — the title names the change, the summary
   says what it is for — and it is the only field on that card written for
   somebody who will never open the instructions. Take it from the plan's
   `## What this does`, cut to fit (400 characters is the store's ceiling, two
   or three sentences is the point).

   Leave `project` out. The daemon attributes a card to the calling session's own
   project, and naming it by hand is how a card lands on somebody else's board.

   **`plan` is what carries the plan, never `notes`.** With it the card lands
   in **Backlog** with the plan attached, in this one call, and Start opens
   `/ship implement` from it. A plan path written only into `notes` leaves the
   card in Prep, where Start opens a *planning* run that no tool can ever
   finish — the card sticks there however much work the run does.

   **Do not ask for a column, and do not reach past this tool to get one.** A
   card an agent can arm is a loop with no human in it. Arriving in In progress
   is what a dispatch *means*, and pressing Start is the human's half of this
   skill.

   The reply says **Backlog** with the plan attached: do not call
   `dark_army_attach_plan` for it. Only when the tool refuses the `plan`
   argument (a session born before Dark Army learned it) file the card without
   `plan`, then call `dark_army_attach_plan` once with the same path — and file
   this session's other cards (follow-ups, notes) only *after* that attach,
   because a second unplanned Prep card makes the attach fail closed.

   **`dark_army_add_card` present and `dark_army_attach_plan` absent is its own case.** A
   channel process keeps the tool list it was born with, so the card is filed
   and simply stays in **Prep** with no plan. Check whether the tool exists at
   all before reading a refusal into its silence. When it is missing, say so in
   the summary in one plain sentence — the card is in Prep, the plan is written
   and its path is on the card — and name the plan path as the handoff.
4. If the tools are not there at all, say so in one sentence and carry on to
   Phase 5b anyway. This is a real case, not a bug to work around: Claude has
   them (as `mcp__dark-army__dark_army_attach_plan` and its siblings) once Dark
   Army's board server is registered, and only the *channel* needs
   `--dangerously-load-development-channels server:dark-army`; Codex has them
   only through Dark Army's board MCP; a Grok session has them only when Dark
   Army registered its board tools. A session started before the rename
   carries the same verbs as `mcp__bob__bob_*`; a session keeps the tool list
   it was born with. The plan file is still written, and naming its path is
   then the whole handoff.

**The plan's follow-ups are filed here and only here.** After the plan's own
card, each `## Out of scope` bullet beginning `Follow-up card:` becomes one
Prep card (`notes` beginning `Follow-up from: <plan slug> — <abs plan path>,
Out of scope`, `stages: ["{{P}}-planner"]`); with no add verb, list them in the
summary. The implement run never files them, so there is one card each.

Then tell the user, and keep it to four things:

- the card's title, and the column it actually landed in — **Backlog** when the
  attach succeeded, **Prep** when the attach verb was missing, **no card** when
  no board tool existed. State the column the board is really showing, never
  the one the happy path would have produced;
- the plan path;
- the plan's `## What this does`, quoted;
- any Phase 4 WARN, one plain sentence each (see Phase 4).

**Do not paste the technical zone**, do not ask for `accept`, and do not offer
`iterate`. There is nothing to hold the turn open for: a plan that turns out to
be wrong gets edited when it is picked up, and the card is where that is
noticed.

Leave **no** `<!-- bob-tldr -->` and **no** `<!-- bob-actions -->` on that
message. Both of those mark a turn that is waiting on somebody, and this one is
not.

## Phase 5b: close out

After the card is filed, write the summary above as your message and then,
as the **very last act** of the run, run
`bash .claude/skills/ship/close-out.sh --plan`. The plan is on the card, so
the tab has nothing left to read: it frees the baseline worktree and closes
this planning terminal, for Claude, Codex and Grok alike, but only when Dark
Army's board names this session as a card's planning run; otherwise it
prints `close-out: terminal left open; …`. After a close say nothing;
when it prints a `close-out:` line instead, end the turn on that line,
verbatim, as the last line of your final message (that line is what files
the tab under Idle rather than *Needs you*). Never retry a refusal and
never `/clear`. Without Dark Army on the machine it prints a
left-open line, and that is the end of plan mode.

## Batch: several cards in one session

Entered when the prompt's first line begins `/ship batch:` — Dark Army's
Refine on several ticked Prep cards. The prompt lists `## Card k of n —
<title>` blocks, each with a `Card id:` line, the card's summary,
instructions and `Objective:` lines. Every card is its own refinement, but
the person is asked **once, at the start**, and the plans are written
**in parallel**:

1. Phase 1 for **every** card before any planner runs: assess each card
   (at most 3 questions per card), then ask them all together — one
   question call carrying as many as it takes (Claude: up to 4 per
   `AskUserQuestion`, calls back to back), every question's text
   prefixed `Card k of n — <title>: `. A card needing none records its
   assumptions. Never ask again once planning starts.
2. Phase 2 for every card: its plan path from its title, distinct across
   the batch (suffix on a clash within it, as on disk).
3. Phase 3 for every card **at once**: one `bc-planner` spawn per card in
   the same message (parallel), each with its own answers, its own
   `plan_path` and one extra packet line, `card_id: <that Card id>`; the
   planner writes it into the plan's `- **Card:**` header.
4. As each planner returns: Phase 4 on its plan, then Phase 5, attach
   **immediately** — `dark_army_attach_plan({ path })`, no card argument,
   the header names the card — so an early exit leaves every finished
   card attached. A BLOCK holds only that card: attach the rest, then end
   the turn with one `<!-- bob-tldr -->` naming every blocked card; on the
   answer, resume Phase 4 for those cards alone.

Never file a new card for a batch member; a refused attach is reported in
the summary and that card stays in Prep. The summary names every card and
the column it actually landed in. Phase 5b runs once, after the last card.
